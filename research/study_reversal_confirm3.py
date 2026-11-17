#!/usr/bin/env python3
"""Test a causal rule inspired by 2026-11-17 04:30 UTC gold reversal.

Rule pre-registered: a CLOSED M1 candle breaks prior 30-minute high/low,
within next 5 CLOSED M1 candles price recovers the old boundary and has
bounced >=3 USD from intervening extreme. Entry NEXT M1 open, opposite break.
No future inputs to signal. One signal per side per 60 minutes.
Compare Sep-Nov and untouched August retrospective holdout.
"""
import csv,json,math,sys,statistics
from collections import defaultdict,Counter
from datetime import datetime,timezone
from pathlib import Path
M=60000
STOPS=(3,5)
TARGETS=(5,10,15,20)
HORIZONS=(15,30,60)
def wilson(w,n):
    if not n:return [None,None]
    z=1.96;p=w/n;d=1+z*z/n
    c=(p+z*z/(2*n))/d;e=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return [round(c-e,4),round(c+e,4)]
def read(path):
    data={}
    with open(path,newline="") as f:
        for r in csv.DictReader(f):
            try:
                t=int(float(r["timestamp"]));o,h,l,c=[float(r[k]) for k in ("open","high","low","close")]
                if all(map(math.isfinite,(o,h,l,c))) and h>l:data[t]=(t,o,h,l,c)
            except (KeyError,ValueError):pass
    return [data[t] for t in sorted(data)]
def eval(rows,i,side,target,stop,horizon):
    entry=rows[i+1][1];up=side=="buy"
    for j in range(i+1,i+1+horizon):
        _,o,h,l,c=rows[j]
        fav=h-entry if up else entry-l
        adv=entry-l if up else h-entry
        if fav>=target and adv>=stop:return "ambiguous"
        if fav>=target:return "win"
        if adv>=stop:return "loss"
    return "timeout"
def analyze(rows,label):
    n=len(rows);before=[1]*n;after=[1]*n
    for i in range(1,n):
        if rows[i][0]-rows[i-1][0]==M:before[i]=before[i-1]+1
    for i in range(n-2,-1,-1):
        if rows[i+1][0]-rows[i][0]==M:after[i]=after[i+1]+1
    data=defaultdict(lambda:defaultdict(Counter))
    last={}
    samples=[]
    for i in range(65,n-60):
        if before[i]<65 or after[i]<61:continue
        t,o,h,l,c=rows[i]
        matches=[]
        for k in range(i-5,i):
            if k<32:continue
            p=rows[k];old=rows[k-30:k]
            top=max(x[2] for x in old);bottom=min(x[3] for x in old)
            # Require the BREAKOUT candle to CLOSE beyond old 30m level.
            if p[4]<bottom-.20 and c>bottom and c-min(x[3] for x in rows[k:i+1])>=3:
                matches.append(("buy",k,bottom))
            if p[4]>top+.20 and c<top and max(x[2] for x in rows[k:i+1])-c>=3:
                matches.append(("sell",k,top))
        # One candidate per direction at this minute; most recent breakout.
        for side in ("buy","sell"):
            eligible=[x for x in matches if x[0]==side]
            if not eligible:continue
            if t-last.get(side,-10**18)<60*M:continue
            last[side]=t
            _,k,level=eligible[-1]
            split="august_holdout" if label=="august" else (
                "train" if i<n*.6 else ("validation" if i<n*.8 else "test"))
            outcomes={}
            for stop in STOPS:
                for target in TARGETS:
                    for horizon in HORIZONS:
                        key=f"t{target}_s{stop}_h{horizon}"
                        outcomes[key]=eval(rows,i,side,target,stop,horizon)
            for group in ("all",split):
                for key,status in outcomes.items():
                    data[group][key][status]+=1
            if len(samples)<40:
                samples.append({"time_utc":datetime.fromtimestamp(t/1000,timezone.utc).isoformat(),
                    "side":side,"breakout_utc":datetime.fromtimestamp(rows[k][0]/1000,timezone.utc).isoformat(),
                    "broken_level":round(level,3),"entry_next_open":round(rows[i+1][1],3),
                    "t20_s5_h60":outcomes["t20_s5_h60"]})
    result={}
    for group,settings in data.items():
        n=sum(settings["t5_s3_h15"].values())
        result[group]={"n":n}
        for key,counts in settings.items():
            w=counts["win"]
            result[group][key]={"win":w,"loss":counts["loss"],"timeout":counts["timeout"],
                "ambiguous":counts["ambiguous"],"rate":round(w/n,5),"wilson95":wilson(w,n)}
    return result,samples

def main(train,aug,out):
    a=read(train);b=read(aug)
    trainres,samples=analyze(a,"train");augres,augsamples=analyze(b,"august")
    result={"rule":"Break beyond preceding 30m extreme, close back inside within 5 M1 bars, >=3 USD rebound from intervening extreme; next M1 open entry opposite breakout",
        "execution":"60m cooldown per side, 65m continuous history and 60m future, targets 5/10/15/20 USD, stops 3/5 USD, 15/30/60min horizons",
        "training_bars":len(a),"august_bars":len(b),
        "training_groups":trainres,"august_groups":augres,
        "examples_training":samples,"examples_august":augsamples,
        "warnings":["This rule was suggested by a single known successful November 17 reversal; retrospective selection bias.",
                    "August is earlier than the development period and not a prospective validation.",
                    "No spread, ask, slippage, commission, news or actual fill data.",
                    "Within-minute target and stop ambiguity counted as non-win.",
                    "Multiple targets and stops create multiple comparisons.",
                    "Some selected examples can overlap in future outcomes; confidence intervals optimistic.",
                    "The 3 USD rebound may sacrifice much of a 20 USD move."]}
    dest=Path(out);dest.mkdir(parents=True,exist_ok=True)
    (dest/"summary.json").write_text(json.dumps(result,indent=2))
    print("Train",len(a),"August",len(b))
    for name,g in list(trainres.items())+list(augres.items()):
        print(name,"n",g["n"],"5/3/15",g["t5_s3_h15"]["rate"],"10/5/30",g["t10_s5_h30"]["rate"],"20/5/60",g["t20_s5_h60"]["rate"])
if __name__=="__main__":main(*sys.argv[1:])
