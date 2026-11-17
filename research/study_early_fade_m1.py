#!/usr/bin/env python3
"""Early fade of gold M1 false breakouts: no lookahead exploratory test.

All signals use the last CLOSED 1-minute candle. Entry next M1 open.
Compare early loss-of-momentum versus full re-entry, and wick sweep.
All stop/target outcomes require uninterrupted next 15 minutes.
"""
import csv,json,math,statistics,sys
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
M=60000
STOPS=(2,3,5)
TARGETS=(5,10,15)
HORIZONS=(5,10,15)
COOLDOWN=20*M

def wilson(w,n):
    if not n:return [None,None]
    z=1.96;p=w/n;d=1+z*z/n
    c=(p+z*z/(2*n))/d
    e=z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/d
    return [round(c-e,4),round(c+e,4)]

def read(path):
    raw=0;rows=[]
    with open(path,newline="") as f:
        for r in csv.DictReader(f):
            raw+=1
            try:
                t=int(float(r["timestamp"]));o,h,l,c=(float(r[k]) for k in ("open","high","low","close"))
                if all(math.isfinite(x) for x in (o,h,l,c)) and h>l:rows.append((t,o,h,l,c))
            except (ValueError,KeyError):pass
    rows.sort()
    return raw,rows

def outcome(rows,i,side,stop,target,horizon):
    entry=rows[i+1][1]
    up=side=="buy"
    mfe=0.;mae=0.
    for j in range(i+1,i+1+horizon):
        _,o,h,l,c=rows[j]
        fav=h-entry if up else entry-l
        adv=entry-l if up else h-entry
        mfe=max(mfe,fav);mae=max(mae,adv)
        if fav>=target and adv>=stop:return "ambiguous",mfe,mae
        if fav>=target:return "win",mfe,mae
        if adv>=stop:return "loss",mfe,mae
    return "timeout",mfe,mae

def main(path,outdir):
    raw,rows=read(path)
    n=len(rows)
    before=[1]*n;after=[1]*n
    for i in range(1,n):
        if rows[i][0]-rows[i-1][0]==M:before[i]=before[i-1]+1
    for i in range(n-2,-1,-1):
        if rows[i+1][0]-rows[i][0]==M:after[i]=after[i+1]+1
    groups=defaultdict(lambda:defaultdict(Counter))
    last={}
    event_count=Counter()
    for i in range(64,n-15):
        if before[i]<64 or after[i]<16:continue
        t,o,h,l,c=rows[i]
        p=rows[i-1]
        old=rows[i-32:i-2]
        upper=max(x[2] for x in old);lower=min(x[3] for x in old)
        atr60=sum(x[2]-x[3] for x in rows[i-62:i-2])/60
        threshold=max(.20,.15*atr60)
        cr=h-l
        proposals=[]
        # Immediate sweep: close back inside in SAME 1m bar.
        if h>upper+threshold and c<upper and c<o and (h-max(o,c))/cr>=.35:
            proposals.append(("wick_reentry","sell"))
        if l<lower-threshold and c>lower and c>o and (min(o,c)-l)/cr>=.35:
            proposals.append(("wick_reentry","buy"))
        # Break candle p has already closed beyond 30m range.
        if p[2]>upper+threshold and p[4]>upper:
            # Weakening as soon as next candle closes lower, BEFORE full reentry.
            if c<o and c<p[4] and h<=p[2]:
                proposals.append(("stall_early","sell"))
                if c<upper:proposals.append(("reentry_confirmed","sell"))
        if p[3]<lower-threshold and p[4]<lower:
            if c>o and c>p[4] and l>=p[3]:
                proposals.append(("stall_early","buy"))
                if c>lower:proposals.append(("reentry_confirmed","buy"))
        # Controls at the same timestamp grid; no level condition.
        if t%(30*M)==0:
            proposals.extend([("clock_baseline","buy"),("clock_baseline","sell")])
        for kind,side in proposals:
            key=(kind,side)
            if kind!="clock_baseline" and t-last.get(key,-10**18)<COOLDOWN:continue
            last[key]=t
            split="train" if i<n*.6 else ("validation" if i<n*.8 else "test")
            event_count[(kind,split)]+=1
            for stop in STOPS:
                for target in TARGETS:
                    for horizon in HORIZONS:
                        status,mfe,mae=outcome(rows,i,side,stop,target,horizon)
                        setting=f"s{stop}_t{target}_h{horizon}"
                        for segment in ("all",split):
                            groups[(kind,segment)][setting][status]+=1
    summary={}
    for (kind,segment),tests in sorted(groups.items()):
        key=f"{kind}__{segment}"
        count=sum(tests["s5_t10_h10"].values())
        summary[key]={"signals":count}
        for setting,counts in tests.items():
            wins=counts["win"]
            summary[key][setting]={"win":wins,"loss":counts["loss"],
                 "timeout":counts["timeout"],"ambiguous":counts["ambiguous"],
                 "success_all":round(wins/count,5),"wilson95":wilson(wins,count)}
    result={"source":"Dukascopy XAUUSD M1 BID Sep1-Nov17 2026",
       "raw_rows":raw,"nonflat_rows":n,
       "method":"30-minute prior range, signal after CLOSED M1 bar, next M1 OPEN entry; contiguous 64m past + 15m future; 20m cooldown per side/pattern",
       "patterns":["wick_reentry","stall_early","reentry_confirmed","clock_baseline"],
       "stops_usd":STOPS,"targets_usd":TARGETS,"horizons_minutes":HORIZONS,
       "splits":"chronological 60/20/20",
       "warnings":["No spread, ASK, slippage, fees, news, or execution latency.",
                   "A stop/target hit within the same minute is ambiguous, counted as non-win.",
                   "Overlapping events across patterns, directions and settings; Wilson intervals optimistic.",
                   "Multiple parameter combinations: treat best settings as exploratory only.",
                   "Price-only candle pattern cannot establish actual institutional stop-hunting.",
                   "A $2 stop may be untradeable during fast spreads and slippage.",
                   "This is not a verified profitable strategy."],
       "groups":summary}
    outdir=Path(outdir);outdir.mkdir(parents=True,exist_ok=True)
    (outdir/"summary.json").write_text(json.dumps(result,indent=2,ensure_ascii=False))
    print("raw",raw,"nonflat",n)
    for k,v in summary.items():
        if k.endswith("__all"):
            print(k,v["signals"],"5/3 10m",v["s3_t5_h10"],"10/5 15m",v["s5_t10_h15"])

if __name__=="__main__":main(sys.argv[1],sys.argv[2])
