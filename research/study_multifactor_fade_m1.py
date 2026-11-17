#!/usr/bin/env python3
"""Pre-specified interaction study: does combining reversal clues improve an
EARLY fade of a failed 30-minute XAUUSD breakout on closed M1 candles?

No lookahead in features. Signal at close M1, entry at next M1 open.
Chronological 60/20/20 split. BID-only; no spread/slippage.
"""
import csv,json,math,statistics,sys
from collections import defaultdict,Counter
from pathlib import Path
from datetime import datetime,timezone
M=60000
def wilson(w,n):
    if not n:return [None,None]
    z=1.96;p=w/n;d=1+z*z/n;c=(p+z*z/(2*n))/d
    e=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return [round(c-e,4),round(c+e,4)]
def outcome(rows,i,side,target,stop,horizon):
    entry=rows[i+1][1];up=side=="buy"
    for j in range(i+1,i+1+horizon):
        _,o,h,l,c=rows[j]
        fav=h-entry if up else entry-l
        adv=entry-l if up else h-entry
        if fav>=target and adv>=stop:return "ambiguous"
        if fav>=target:return "win"
        if adv>=stop:return "loss"
    return "timeout"
def main(path,out):
    rows=[];raw=0
    with open(path,newline="") as f:
        for r in csv.DictReader(f):
            raw+=1
            try:
                t=int(float(r["timestamp"]))
                o,h,l,c=[float(r[k]) for k in ("open","high","low","close")]
                if h>l and all(map(math.isfinite,(o,h,l,c))):rows.append((t,o,h,l,c))
            except (ValueError,KeyError):continue
    rows.sort();n=len(rows)
    before=[1]*n;after=[1]*n
    for i in range(1,n):
        if rows[i][0]-rows[i-1][0]==M:before[i]=before[i-1]+1
    for i in range(n-2,-1,-1):
        if rows[i+1][0]-rows[i][0]==M:after[i]=after[i+1]+1
    counters=defaultdict(lambda:defaultdict(Counter));last={}
    events=Counter()
    for i in range(64,n-15):
        if before[i]<64 or after[i]<16:continue
        t,o,h,l,c=rows[i];p=rows[i-1]
        old=rows[i-32:i-2]
        upper=max(x[2] for x in old);lower=min(x[3] for x in old)
        avg60=sum(x[2]-x[3] for x in rows[i-62:i-2])/60
        medbody=statistics.median(abs(x[4]-x[1]) for x in rows[i-22:i-2])
        threshold=max(.20,.15*avg60)
        setups=[]
        if p[2]>upper+threshold and p[4]>upper and c<o and c<p[4] and h<=p[2]:
            setups.append(("sell",upper))
        if p[3]<lower-threshold and p[4]<lower and c>o and c>p[4] and l>=p[3]:
            setups.append(("buy",lower))
        for side,level in setups:
            if t-last.get(side,-10**18)<20*M:continue
            last[side]=t
            up=side=="buy"
            pw=(min(p[1],p[4])-p[3]) if up else (p[2]-max(p[1],p[4]))
            cw=(min(o,c)-l) if up else (h-max(o,c))
            wick=max(pw/(p[2]-p[3]),cw/(h-l))
            bodyp=abs(p[4]-p[1]);bodyc=abs(c-o)
            deep=abs(p[4]-level)>=.7*avg60
            spike=(p[2]-p[3])>=1.6*avg60
            exhaustion=bodyp>=1.5*medbody and bodyc<=.75*bodyp
            long_wick=wick>=.35
            reentry=(c>level) if up else (c<level)
            hour=datetime.fromtimestamp(t/1000,timezone.utc).hour
            session=12<=hour<18
            names=["stall_early_all"]
            if long_wick:names.append("wick35")
            if spike:names.append("breakout_range_spike")
            if exhaustion:names.append("large_breakout_then_smaller_reversal")
            if deep:names.append("deep_breakout")
            if reentry:names.append("confirmed_reentry")
            if session:names.append("london_ny_overlap_utc")
            if exhaustion and spike:names.append("exhaustion_and_spike")
            if exhaustion and long_wick:names.append("exhaustion_and_wick")
            if spike and long_wick:names.append("spike_and_wick")
            if exhaustion and spike and long_wick:names.append("exhaustion_spike_wick")
            if exhaustion and spike and reentry:names.append("exhaustion_spike_reentry")
            if exhaustion and spike and session:names.append("exhaustion_spike_session")
            if exhaustion and long_wick and reentry:names.append("exhaustion_wick_reentry")
            split="train" if i<n*.6 else ("validation" if i<n*.8 else "test")
            outcomes={}
            for target,stop,horizon in ((5,3,10),(5,5,10),(10,5,15),(15,5,15),(10,3,10)):
                key=f"t{target}_s{stop}_h{horizon}"
                outcomes[key]=outcome(rows,i,side,target,stop,horizon)
            for name in names:
                for sp in ("all",split):
                    for k,v in outcomes.items():counters[(name,sp)][k][v]+=1
            events[(split,side)]+=1
    groups={}
    for (name,sp),tests in sorted(counters.items()):
        count=sum(tests["t5_s3_h10"].values())
        groups[f"{name}__{sp}"]={"signals":count}
        for key,counts in tests.items():
            wins=counts["win"]
            groups[f"{name}__{sp}"][key]={
                "win":wins,"loss":counts["loss"],"timeout":counts["timeout"],
                "ambiguous":counts["ambiguous"],"rate":round(wins/count,5),
                "wilson95":wilson(wins,count)}
    result={"source":"Dukascopy M1 BID XAUUSD Sep1-Nov17 2026","raw":raw,"nonflat":n,
        "signal":"Closed 1m bar loses momentum immediately after close beyond preceding 30m high/low; entry next open",
        "sample":"20-minute dedup per direction; chronological 60/20/20",
        "features":"Prior breakout candle range spike >=1.6 x mean range60; body >=1.5 x median body20 then next body <=0.75 prior; wick >=35%; deep close >=0.7 x mean range60; close back inside; UTC 12-18 session; fixed combinations",
        "warnings":["No spread, slippage, commission, ASK quotes or news filtering.","Multiple comparisons and overlapping subsets; test split is exploratory after prior studies on same calendar.","Bar high/low simultaneous target-stop is ambiguous, counted as non-win.","Confidence intervals ignore time clustering and are optimistic.","High win rate on tiny sample is not robust evidence of a trading edge."],
        "groups":groups,"base_events":{str(k):v for k,v in events.items()}}
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    (out/"summary.json").write_text(json.dumps(result,indent=2))
    print("raw",raw,"rows",n,"groups",len(groups))
    for k,g in groups.items():
        if k.endswith("__all") or k.endswith("__test"):
            print(k,"n",g["signals"],"5/3",g["t5_s3_h10"]["rate"],"10/5",g["t10_s5_h15"]["rate"])
if __name__=="__main__":main(sys.argv[1],sys.argv[2])
