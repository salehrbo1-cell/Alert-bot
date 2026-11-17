#!/usr/bin/env python3
"""Matched cohort: the SAME independent M5 touches, two confirmation clocks.
Uses already recorded M5 touch events, then confirms on M15 closes. This
avoids selection bias from separately detecting touches on M5 vs M15.
"""
import bisect,csv,json,statistics,sys
from collections import Counter,defaultdict
from pathlib import Path
import study_sweep_poi_ifvg_cisd as b
import study_sr_retests_m5_m15 as q
M,H=b.M,b.H
p=Path("research/results_sr_retests_m5_m15")
def choose_signal(e,m15,ends,confirm):
    t=b.ts(e["touch"]);i=bisect.bisect_right(ends,t)
    side=e["side"];lo=e["lo"];hi=e["hi"]
    for j in range(i,min(i+4,len(m15))):
        x=m15[j]
        if x[1]>t+60*M:break
        if j>i and m15[j-1][1]!=x[0]:break
        reclaim=(x[5]>hi and x[5]>x[2] if side=="S" else
            x[5]<lo and x[5]<x[2])
        if not reclaim:continue
        if confirm=="break2":
            if j<2 or m15[j-2][1]!=m15[j-1][0] or m15[j-1][1]!=x[0]:continue
            if side=="S" and x[5]<=max(y[3] for y in m15[j-2:j]):continue
            if side=="R" and x[5]>=min(y[4] for y in m15[j-2:j]):continue
        return x[1]
    return None
def run(path):
    m1=b.read(path);m15=b.resample(m1,15);h1=b.resample(m1,60);h4=b.resample(m1,240)
    starts=[x[0] for x in m1];ends=[x[1] for x in m15]
    with (p/"events.json").open() as f:events=json.load(f)
    with (p/"trades.csv").open(newline="") as f:old=list(csv.DictReader(f))
    touches=[x for x in events if x["tf"]=="M5" and x["strong"]]
    piv=(q.pivots(m15,"M15",2,q.atr_cache(m15))+
        q.pivots(h1,"H1",3,q.atr_cache(h1))+
        q.pivots(h4,"H4",2,q.atr_cache(h4)))
    piv.sort(key=lambda x:x["avail"])
    a15=q.atr_cache(m15);a1=q.atr_cache(h1)
    cand=[];counts=Counter()
    for confirm in ("reclaim","break2"):
        seen=set()
        for e in touches:
            at=choose_signal(e,m15,ends,confirm)
            if at is None:counts[f"{confirm}_unconfirmed"]+=1;continue
            if at>q.END:continue
            key=(confirm,at,e["side"],round(e["center"]))
            if key in seen:
                counts[f"{confirm}_duplicate_m15_close"]+=1;continue
            seen.add(key)
            cand.append(dict(e=e,at=at,confirm=confirm))
    cand.sort(key=lambda x:x["at"])
    active=[];ptr=0;target_by_time={}
    # At each closed M15 bar: make causal level snapshot for any signal at
    # this exact timestamp. Do not use future pivots or future invalidations.
    times=set(x["at"] for x in cand)
    for j,x in enumerate(m15):
        at=x[1]
        while ptr<len(piv) and piv[ptr]["avail"]<=at:
            active.append(piv[ptr]);ptr+=1
        active=[v for v in active if at-v["avail"]<(
            (2 if v["tf"]=="M15" else 21 if v["tf"]=="H1" else 60)*q.D)
            and not v.get("invalid",False)]
        for v in active:
            buf=max(.75,.12*v["atr"])
            if (v["side"]=="R" and x[5]>v["price"]+buf) or (
                v["side"]=="S" and x[5]<v["price"]-buf):
                v["invalid"]=True
        active=[v for v in active if not v.get("invalid",False)]
        if at in times:
            a1idx=max(0,bisect.bisect_right([z[1] for z in h1],at)-1)
            target_by_time[at]=q.make_zones(active,a15[j],a1[a1idx])
    trades=[]
    for z in cand:
        e=z["e"];at=z["at"];side=e["side"]
        idx=bisect.bisect_left(starts,at)
        if idx>=len(m1) or m1[idx][0]!=at:counts["no_m1_entry"]+=1;continue
        zones=target_by_time.get(at,[])
        bid=m1[idx][2]
        tz=q.zone_targets(zones,side,bid,"",False)
        if tz is None:counts["no_opposite_target"]+=1;continue
        target,other=tz
        start=bisect.bisect_left(starts,b.ts(e["touch"]))
        pathbars=m1[start:idx]
        if not pathbars:continue
        buf=max(.5,.08*a15[max(0,bisect.bisect_right(ends,at)-1)])
        wick=(min(x[4] for x in pathbars) if side=="S" else max(x[3] for x in pathbars))
        stop=(min(wick,e["lo"])-buf if side=="S" else max(wick,e["hi"])+buf)
        result=q.simulate(m1,starts,at,side,stop,target)
        if result.get("planned_rr") is None:counts["invalid_rr"]+=1;continue
        trades.append(dict(tf="M15_matched",confirm=z["confirm"],
            strong=True,tier=e["tier"],touch=e["touch"],signal=q.fmt(at),
            entry_t=at,side=side,zone_center=e["center"],
            stop_bid=round(stop,3),entry_bid=round(bid,3),
            target_bid=round(target,3),rr2=result["planned_rr"]>=2.,
            period="test" if b.ts(e["touch"])>=q.TEST else "train",
            result=result))
    # Matched M5 baseline: subset of original M5 trades whose original
    # touch is also in the set that generated a deduplicated M15 signal.
    out={}
    for confirm in ("reclaim","break2"):
        cs=[x for x in cand if x["confirm"]==confirm]
        touched={x["e"]["touch"] for x in cs}
        for rr2 in (False,True):
            name=f"{confirm}_{'rr2' if rr2 else 'all_rr'}"
            m15t=[x for x in trades if x["confirm"]==confirm and (not rr2 or x["rr2"])]
            # Existing M5 signal for same touch (not necessarily same
            # confirmation), counted only if M5 confirmation also happened.
            m5t=[x for x in old if x["tf"]=="M5" and x["confirm"]==confirm
                and x["touch"] in touched and x["strong"]=="True"
                and x["target_strong"]=="False"
                and (not rr2 or x["rr2"]=="True")]
            def oldstats(rows):
                a=[float(x["realized_r"]) for x in rows if x["realized_r"]]
                s=Counter(x["status"] for x in rows)
                return dict(n=len(rows),valid=len(a),target=s["target"],stop=s["stop"],
                    timeout=s["timeout"],censored=s["censored"],gap=s["gap"],
                    total_r=round(sum(a),3),mean_r=round(sum(a)/len(a),3) if a else None)
            m5recalc=[]
            for x in m5t:
                at=b.ts(x["signal"])
                result=q.simulate(m1,starts,at,x["side"],float(x["stop_bid"]),float(x["target_bid"]))
                if result.get("planned_rr") is None:continue
                m5recalc.append(dict(entry_t=at,period=x["period"],result=result))
            out[name]=dict(m15_all=q.summarize(m15t),
                m15_one_position=q.summarize(q.nonoverlap(m15t)),
                m5_same_touches=oldstats(m5t),
                m5_recalculated=q.summarize(m5recalc),
                m5_one_position=q.summarize(q.nonoverlap(m5recalc)),
                m15_train=q.summarize([x for x in m15t if x["period"]=="train"]),
                m15_test=q.summarize([x for x in m15t if x["period"]=="test"]),
                m5_train=q.summarize([x for x in m5recalc if x["period"]=="train"]),
                m5_test=q.summarize([x for x in m5recalc if x["period"]=="test"]),
                matched_touch_count=len(touched),m15_signals=len(cs))
    dest=p/"matched_m5_touch_comparison.json"
    dest.write_text(json.dumps(dict(counts=dict(counts),results=out,
        methodology="Same pre-detected independent M5 touches; M15 confirmation within <=60m, duplicates at same zone/close merged; causal target levels; M5 original trades subset of identical touch timestamps"),indent=2))
    print(json.dumps(out,indent=2),flush=True)
if __name__=="__main__":run(sys.argv[1])
