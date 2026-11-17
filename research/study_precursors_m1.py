#!/usr/bin/env python3
"""Precursor scan: fixed candidate rules vs 20 USD XAUUSD move within 15m.

No future inputs to any feature. Every fifth closed M1 bar is sampled to
reduce (not eliminate) event overlap. Signal is entered at next M1 open.
All candidate definitions are specified in this file before evaluation.
"""
import csv,json,math,statistics,sys
from datetime import datetime,timezone
from collections import defaultdict,Counter
from pathlib import Path
M=60000

def wilson(w,n):
    if not n:return [None,None]
    z=1.96;p=w/n;d=1+z*z/n
    c=(p+z*z/(2*n))/d
    e=z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/d
    return [round(c-e,4),round(c+e,4)]

def main(path,dest):
    raw=0;rows=[]
    with open(path,newline="") as f:
        for r in csv.DictReader(f):
            raw+=1
            try:
                t=int(float(r["timestamp"]))
                o,h,l,c=[float(r[k]) for k in ("open","high","low","close")]
                if math.isfinite(o+h+l+c) and h>l:rows.append((t,o,h,l,c))
            except (ValueError,KeyError):pass
    rows.sort()
    n=len(rows)
    before=[1]*n;after=[1]*n
    for i in range(1,n):
        if rows[i][0]-rows[i-1][0]==M:before[i]=before[i-1]+1
    for i in range(n-2,-1,-1):
        if rows[i+1][0]-rows[i][0]==M:after[i]=after[i+1]+1
    d=defaultdict(lambda:Counter())
    daycounts=defaultdict(lambda:Counter())
    sampled=0
    for i in range(61,n-15):
        t,o,h,l,c=rows[i]
        if t%(5*M)!=0 or before[i]<61 or after[i]<16:continue
        sampled+=1
        pre=rows[i-60:i];pre15=rows[i-15:i];pre20=rows[i-20:i]
        mean60=sum(x[2]-x[3] for x in pre)/60
        mean15=sum(x[2]-x[3] for x in pre15)/15
        if mean60<=0:continue
        medbody=statistics.median(abs(x[4]-x[1]) for x in pre20)
        high30=max(x[2] for x in rows[i-30:i])
        low30=min(x[3] for x in rows[i-30:i])
        rg=h-l
        hour=datetime.fromtimestamp(t/1000,timezone.utc).hour
        split="train" if i<n*.6 else ("validation" if i<n*.8 else "test")
        for side in ("buy","sell"):
            up=side=="buy"
            wick=(min(o,c)-l) if up else (h-max(o,c))
            breakout=(c>high30) if up else (c<low30)
            sweep=(l<low30 and c>low30) if up else (h>high30 and c<high30)
            impulse=abs(c-o)>max(0.4,2*medbody) and ((c>o) if up else (c<o))
            momentum=((c-rows[i-3][4])>2*mean60) if up else ((rows[i-3][4]-c)>2*mean60)
            volhigh=mean15>1.6*mean60
            vollow=mean15<0.65*mean60
            longwick=wick/rg>=0.55
            groups=["all","high_vol" if volhigh else "not_high_vol",
                    "low_vol" if vollow else "not_low_vol",
                    "london_ny_hours" if 12<=hour<18 else "other_hours"]
            if impulse:groups.append("large_body")
            if breakout:groups.append("breakout_30m")
            if sweep:groups.append("sweep_30m")
            if longwick:groups.append("long_wick")
            if momentum:groups.append("momentum_3m")
            if volhigh and impulse:groups.append("high_vol_and_large_body")
            if volhigh and momentum:groups.append("high_vol_and_momentum")
            if sweep and longwick:groups.append("sweep_and_long_wick")
            if breakout and impulse:groups.append("breakout_and_large_body")
            entry=rows[i+1][1]
            future=rows[i+1:i+16]
            mfe=max((x[2]-entry if up else entry-x[3]) for x in future)
            label_no_stop=mfe>=20
            trade="timeout"
            for x in future:
                fav=(x[2]-entry if up else entry-x[3])
                adv=(entry-x[3] if up else x[2]-entry)
                if fav>=20 and adv>=5:trade="ambiguous";break
                if fav>=20:trade="win";break
                if adv>=5:trade="loss";break
            for group in groups:
                for sp in ("all",split):
                    key=(group,sp)
                    d[key]["n"]+=1
                    if label_no_stop:d[key]["20usd_excursion"]+=1
                    d[key][trade]+=1
    out={}
    for (group,split),ct in sorted(d.items()):
        count=ct["n"]
        out[group+"__"+split]={
            "n":count,"20usd_excursion":ct["20usd_excursion"],
            "excursion_rate":round(ct["20usd_excursion"]/count,5),
            "excursion_ci95":wilson(ct["20usd_excursion"],count),
            "target_before_stop":ct["win"],
            "target_before_stop_rate":round(ct["win"]/count,5),
            "target_before_stop_ci95":wilson(ct["win"],count),
            "loss":ct["loss"],"timeout":ct["timeout"],"ambiguous":ct["ambiguous"]}
    result={
        "source":"Dukascopy M1 BID XAUUSD Sep1-Nov17 2026",
        "raw_rows":raw,"nonflat_rows":n,"sampled_times":sampled,
        "sample_rule":"every fifth closed M1 candle, 60 continuous past minutes and 15 continuous future minutes",
        "prediction":"for each direction, 20USD favorable move within next 15m; plus first target20 vs stop5",
        "predefined_features":["high_vol","low_vol","large_body","breakout_30m","sweep_30m","long_wick","momentum_3m","high_vol_and_large_body","high_vol_and_momentum","sweep_and_long_wick","breakout_and_large_body","london_ny_hours"],
        "warnings":["BID-only, no spread, slippage, or transaction costs.","Sampled 5 minutes apart: 15-minute outcomes overlap, confidence intervals optimistic.","Multiple candidate comparisons: treat as exploratory; select using train/validation, verify only on held-out test.","An excursion is NOT an executable trade. Target-before-stop is stricter.","Price action cannot verify stop hunts, institutional orders, or market-maker intent."],
        "groups":out}
    dest=Path(dest);dest.mkdir(parents=True,exist_ok=True)
    (dest/"summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print("raw",raw,"nonflat",n,"sampled",sampled)
    for key,v in out.items():
        if key.endswith("__all"):
            print(key,"n",v["n"],"20USD excursion",v["excursion_rate"],"target-before-stop",v["target_before_stop_rate"])

if __name__=="__main__":main(sys.argv[1],sys.argv[2])
