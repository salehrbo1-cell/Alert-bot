#!/usr/bin/env python3
"""Research: price-reset M5 vs M15 gold rejection at PRE-KNOWN S/R zones.
No fixed 6h touch cooldown; H1/H4 pivots plus repeated confirmed M15 pivots.
No hindsight for zones, targets or confirmation. Research only, no alert.py edits.
"""
import bisect,csv,json,math,statistics,sys
from collections import Counter,defaultdict
from pathlib import Path
from datetime import datetime,timezone
import study_sweep_poi_ifvg_cisd as b

M,H=b.M,b.H
D=24*H
START=b.ts("2025-11-17T00:00:00Z")
END=b.ts("2026-11-17T09:00:00Z")
TEST=b.ts("2026-08-17T00:00:00Z")
SPREAD=.30;SLIP=.10
MAX_HOLD=24*H
def fmt(t):return b.iso(t)
def atr_cache(arr,n=14):
    # rolling ATR for last fully closed bar
    out=[0.]*len(arr);q=[]
    for i,x in enumerate(arr):
        tr=x[3]-x[4] if i==0 else max(x[3]-x[4],abs(x[3]-arr[i-1][5]),abs(x[4]-arr[i-1][5]))
        q.append(tr)
        if i>=n:out[i]=sum(q[i-n+1:i+1])/n
    return out
def pivots(arr,tf,k,atr):
    result=[]
    for i in range(max(20,2*k),len(arr)):
        p=i-k;x=arr[p]
        if any(arr[j][1]!=arr[j+1][0] for j in range(p-k,i)):continue
        left=arr[p-k:p];right=arr[p+1:i+1]
        a=atr[i];minprom=max(.30,.12*a)
        if x[3]>max(z[3] for z in left+right)+minprom:
            result.append(dict(id=f"{tf}R{p}",side="R",tf=tf,
                price=x[3],avail=arr[i][1],atr=a))
        if x[4]<min(z[4] for z in left+right)-minprom:
            result.append(dict(id=f"{tf}S{p}",side="S",tf=tf,
                price=x[4],avail=arr[i][1],atr=a))
    return result
def make_zones(active,atr15,atr1):
    # Pre-known pivots only; cluster by side and price; no future touches.
    tol=max(1.,min(3.0,.16*atr1))
    width=max(.65,min(2.,.16*atr15))
    zones=[]
    for side in ("S","R"):
        ps=sorted((p for p in active if p["side"]==side),key=lambda p:p["price"])
        bucket=[]
        def finish():
            if not bucket:return
            # strong: H1/H4 pivot OR at least two separate confirmed M15 pivots.
            long_tf=any(p["tf"] in ("H1","H4") for p in bucket)
            m15=sorted((p for p in bucket if p["tf"]=="M15"),key=lambda p:p["avail"])
            independent=[]
            for p in m15:
                if not independent or p["avail"]-independent[-1]["avail"]>=30*M:
                    independent.append(p)
            strong=long_tf or len(independent)>=2
            tier="H4" if any(p["tf"]=="H4" for p in bucket) else (
                "H1" if any(p["tf"]=="H1" for p in bucket) else
                "M15_repeated" if len(independent)>=2 else "M15_single")
            prices=[p["price"] for p in bucket]
            # Anchor ID is stable for this pivot cluster as long as its oldest member survives.
            oldest=min(bucket,key=lambda p:p["avail"])
            zones.append(dict(id=oldest["id"],side=side,
                lo=min(prices)-width,hi=max(prices)+width,
                center=statistics.median(prices),strong=strong,tier=tier,
                ids=[p["id"] for p in bucket]))
        for p in ps:
            if bucket and p["price"]-bucket[0]["price"]>tol:
                finish();bucket=[]
            bucket.append(p)
        finish()
    return zones
def zone_targets(zones,side,entry_bid,source_id,only_strong=False):
    arr=[]
    for z in zones:
        if z["id"]==source_id or z["side"]==side:continue
        if only_strong and not z["strong"]:continue
        if side=="S":
            target=z["lo"]-.25
            if target>entry_bid+SPREAD+1.:arr.append((target,z))
        else:
            target=z["hi"]+.25
            if target<entry_bid-1.:arr.append((target,z))
    if not arr:return None
    return min(arr,key=lambda x:x[0]) if side=="S" else max(arr,key=lambda x:x[0])
def simulate(m1,starts,entry_t,side,stop_bid,target_bid):
    i=bisect.bisect_left(starts,entry_t)
    if i>=len(m1) or m1[i][0]!=entry_t:return dict(status="no_entry")
    buy=side=="S";bid=m1[i][2]
    entry=(bid+SPREAD+SLIP if buy else bid-SLIP)
    stop=(stop_bid if buy else stop_bid+SPREAD)
    target=(target_bid if buy else target_bid+SPREAD)
    risk=(entry-stop+SLIP if buy else stop-entry+SLIP)
    reward=(target-entry-SLIP if buy else entry-target-SLIP)
    if risk<=0 or reward<=0:return dict(status="invalid",risk=round(risk,3),reward=round(reward,3))
    rr=reward/risk;prev=None;max_fav=0.;max_adv=0.
    for x in m1[i:i+24*60]:
        if x[0]>=entry_t+MAX_HOLD:break
        if prev is not None and x[0]!=prev[1]:
            return dict(status="gap",planned_rr=round(rr,4),risk=round(risk,3),
                reward=round(reward,3),realized_r=None,exit=fmt(prev[1]))
        hi=(x[3] if buy else x[3]+SPREAD)
        lo=(x[4] if buy else x[4]+SPREAD)
        max_fav=max(max_fav,hi-entry if buy else entry-lo)
        max_adv=max(max_adv,entry-lo if buy else hi-entry)
        stop_hit=lo<=stop if buy else hi>=stop
        target_hit=hi>=target if buy else lo<=target
        if stop_hit or target_hit:
            pnl=-risk if stop_hit else reward
            return dict(status="stop" if stop_hit else "target",
                planned_rr=round(rr,4),risk=round(risk,3),reward=round(reward,3),
                realized_r=round(pnl/risk,4),pnl=round(pnl,3),
                exit=fmt(x[1]),mfe=round(max_fav,3),mae=round(max_adv,3))
        prev=x
    if prev is None:return dict(status="no_exit")
    if prev[1]<entry_t+MAX_HOLD:
        return dict(status="censored",planned_rr=round(rr,4),risk=round(risk,3),
            reward=round(reward,3),realized_r=None,exit=fmt(prev[1]))
    pnl=(prev[5]-entry-SLIP if buy else entry-(prev[5]+SPREAD)-SLIP)
    return dict(status="timeout",planned_rr=round(rr,4),risk=round(risk,3),
        reward=round(reward,3),realized_r=round(pnl/risk,4),pnl=round(pnl,3),
        exit=fmt(prev[1]),mfe=round(max_fav,3),mae=round(max_adv,3))
def summarize(rows):
    st=Counter(x["result"]["status"] for x in rows)
    vals=[x["result"]["realized_r"] for x in rows if x["result"].get("realized_r") is not None]
    pos=sum(max(0,v) for v in vals);neg=-sum(min(0,v) for v in vals)
    eq=peak=dd=0.
    for v in vals:eq+=v;peak=max(peak,eq);dd=max(dd,peak-eq)
    return dict(n=len(rows),evaluated=len(vals),targets=st["target"],stops=st["stop"],
        timeouts=st["timeout"],gaps=st["gap"],censored=st["censored"],
        hit_rate=round(st["target"]/len(vals),4) if vals else None,
        total_r=round(sum(vals),3),mean_r=round(statistics.mean(vals),4) if vals else None,
        median_r=round(statistics.median(vals),4) if vals else None,
        profit_factor_r=round(pos/neg,3) if neg else None,
        max_drawdown_r=round(dd,3),
        top_3_r=round(sum(sorted((v for v in vals if v>0),reverse=True)[:3]),3))
def nonoverlap(rows):
    result=[];free=-1
    for x in sorted(rows,key=lambda z:z["entry_t"]):
        if x["entry_t"]<free:continue
        if x["result"].get("realized_r") is None:continue
        result.append(x);free=b.ts(x["result"]["exit"])
    return result
def scan(tf,bars,m1,starts,piv,atr15_by_time,atr1_by_time):
    atr=atr_cache(bars)
    confirmed=sorted(piv,key=lambda p:p["avail"])
    ptr=0;active=[];states={};events=[];trades=[];counts=Counter()
    # Pivot snapshots updated only on close of the M15 bar. A pivot confirmed
    # at 15:00 can be used on the M5 bar starting at 15:00, not earlier.
    zones=[];last_snapshot=None
    for i in range(5,len(bars)-1):
        x=bars[i];t=x[0]
        if t<START-2*D:continue
        snapshot=(t//(15*M))*(15*M)
        if snapshot!=last_snapshot:
            while ptr<len(confirmed) and confirmed[ptr]["avail"]<=snapshot:
                active.append(confirmed[ptr]);ptr+=1
            active=[p for p in active if snapshot-p["avail"]<(
                (2 if p["tf"]=="M15" else 21 if p["tf"]=="H1" else 60)*D)
                and not p.get("invalid",False)]
            # invalidate only after PREVIOUSLY closed M15 bar, never current
            prev_idx=bisect.bisect_right(atr15_by_time[0],snapshot)-1
            if prev_idx>=0:
                prevbar=atr15_by_time[1][prev_idx]
                if prevbar[1]<=snapshot:
                    for p in active:
                        buffer=max(.75,.12*p["atr"])
                        if (p["side"]=="R" and prevbar[5]>p["price"]+buffer) or (
                            p["side"]=="S" and prevbar[5]<p["price"]-buffer):
                            p["invalid"]=True
                    active=[p for p in active if not p.get("invalid",False)]
            a15=atr15_by_time[2][max(0,prev_idx)] if prev_idx>=0 else 5.
            a1=atr1_by_time[2][max(0,bisect.bisect_right(atr1_by_time[0],snapshot)-1)]
            zones=make_zones(active,a15,a1)
            last_snapshot=snapshot
        if not START<=t<END:continue
        if not zones:continue
        prev=bars[i-1]
        # Price-based reset, not time cooldown. The reset is evaluated on the
        # PREVIOUS bar, preventing same-bar departure and retouch ambiguity.
        for z in zones:
            key=z["id"];state=states.setdefault(key,dict(armed=True,last_touch=-1))
            dist=max(1.5,.22*max(1.,atr1_by_time[2][max(0,bisect.bisect_right(atr1_by_time[0],t)-1)]))
            if not state["armed"]:
                if z["side"]=="R" and prev[5]<z["lo"]-dist:state["armed"]=True
                if z["side"]=="S" and prev[5]>z["hi"]+dist:state["armed"]=True
        choices=[]
        for z in zones:
            state=states[z["id"]]
            if not state["armed"] or state["last_touch"]>=t:continue
            if z["side"]=="R":
                approached=prev[5]<z["lo"] and x[3]>=z["lo"] and x[4]<=z["hi"]
            else:
                approached=prev[5]>z["hi"] and x[4]<=z["hi"] and x[3]>=z["lo"]
            if approached:choices.append(z)
        if not choices:continue
        counts["touch_bars"]+=1
        if len({z["side"] for z in choices})>1:
            counts["both_sides_skipped"]+=1;continue
        side=choices[0]["side"]
        z=min(choices,key=lambda z:abs(prev[5]-z["center"]))
        state=states[z["id"]];state["armed"]=False;state["last_touch"]=t
        counts["touches"]+=1
        if z["strong"]:counts["strong_touches"]+=1
        # Store one event per *independent* touch. Signal may arrive within
        # next three candles, with no use of later bars in target/stop.
        ev=dict(tf=tf,touch=fmt(t),side=side,strong=z["strong"],tier=z["tier"],
            center=round(z["center"],3),lo=round(z["lo"],3),hi=round(z["hi"],3),
            period="test" if t>=TEST else "train",signals={})
        for confirm in ("reclaim","break2"):
            wick=(min(x[4],z["lo"]) if side=="S" else max(x[3],z["hi"]))
            signal=None
            for j in range(i,min(i+4,len(bars))):
                y=bars[j]
                if j>i and bars[j-1][1]!=y[0]:break
                wick=(min(wick,y[4]) if side=="S" else max(wick,y[3]))
                reclaim=(y[5]>z["hi"] and y[5]>y[2] if side=="S" else
                    y[5]<z["lo"] and y[5]<y[2])
                if not reclaim:continue
                if confirm=="break2":
                    if j<2:continue
                    prev2=bars[j-2:j]
                    if any(prev2[k][1]!=prev2[k+1][0] for k in range(len(prev2)-1)):continue
                    structure=(y[5]>max(a[3] for a in prev2) if side=="S" else
                        y[5]<min(a[4] for a in prev2))
                    if not structure:continue
                signal=j;break
            if signal is None:
                counts[f"{confirm}_not_confirmed"]+=1;continue
            at=bars[signal][1]
            if at>END:continue
            counts[f"{confirm}_confirmed"]+=1
            ev["signals"][confirm]=fmt(at)
            # Target must use the snapshot at signal close, NOT at touch.
            # For deferred confirmation, include only newly CONFIRMED pivots
            # and remove pivots invalidated by intervening CLOSED M15 candles.
            target_snapshot=zones
            at_snapshot=(at//(15*M))*(15*M)
            if at_snapshot>snapshot:
                j=ptr
                while j<len(confirmed) and confirmed[j]["avail"]<=at_snapshot:j+=1
                updated=[p for p in active if at_snapshot-p["avail"]<
                    ((2 if p["tf"]=="M15" else 21 if p["tf"]=="H1" else 60)*D)]
                updated += [p for p in confirmed[ptr:j] if at_snapshot-p["avail"]<
                    ((2 if p["tf"]=="M15" else 21 if p["tf"]=="H1" else 60)*D)]
                # Do not mutate active/confirmed here: future candles must
                # not alter the current loop's state.
                closed=[v for v in atr15_by_time[1]
                    [bisect.bisect_right(atr15_by_time[0],snapshot):
                     bisect.bisect_right(atr15_by_time[0],at_snapshot)]
                    if v[1]<=at_snapshot]
                updated=[p for p in updated if not any(
                    (p["side"]=="R" and bar[5]>p["price"]+max(.75,.12*p["atr"]))
                    or (p["side"]=="S" and bar[5]<p["price"]-max(.75,.12*p["atr"]))
                    for bar in closed)]
                i15=max(0,bisect.bisect_right(atr15_by_time[0],at_snapshot)-1)
                i1=max(0,bisect.bisect_right(atr1_by_time[0],at_snapshot)-1)
                target_snapshot=make_zones(updated,atr15_by_time[2][i15],atr1_by_time[2][i1])
            idx=bisect.bisect_left(starts,at)
            if idx>=len(m1) or m1[idx][0]!=at:continue
            entry_bid=m1[idx][2]
            for only_strong in (False,True):
                tz=zone_targets(target_snapshot,side,entry_bid,z["id"],only_strong)
                if tz is None:
                    counts["no_opposite_target"]+=1;continue
                target,other=tz
                a15=atr15_by_time[2][max(0,bisect.bisect_right(atr15_by_time[0],at)-1)]
                buf=max(.50,.08*a15)
                stop=(wick-buf if side=="S" else wick+buf)
                result=simulate(m1,starts,at,side,stop,target)
                if result.get("planned_rr") is None:
                    counts["invalid_risk_or_entry"]+=1;continue
                trades.append(dict(tf=tf,confirm=confirm,strong=z["strong"],tier=z["tier"],
                    touch=fmt(t),signal=fmt(at),entry_t=at,side=side,
                    zone_center=round(z["center"],3),zone_lo=round(z["lo"],3),
                    zone_hi=round(z["hi"],3),stop_bid=round(stop,3),
                    entry_bid=round(entry_bid,3),target_bid=round(target,3),
                    target_tier=other["tier"],target_strong=only_strong,
                    rr2=result["planned_rr"]>=2.,period="test" if t>=TEST else "train",
                    result=result))
        events.append(ev)
    return events,trades,counts
def run(path,out):
    m1=b.read(path);starts=[x[0] for x in m1]
    m5=b.resample(m1,5);m15=b.resample(m1,15);h1=b.resample(m1,60);h4=b.resample(m1,240)
    a15=atr_cache(m15);a1=atr_cache(h1)
    pivotlist=(pivots(m15,"M15",2,a15)+pivots(h1,"H1",3,a1)
        +pivots(h4,"H4",2,atr_cache(h4)))
    d15=([x[1] for x in m15],m15,a15)
    d1=([x[1] for x in h1],h1,a1)
    all_events=[];all_trades=[];counts={}
    for tf,bars in (("M5",m5),("M15",m15)):
        events,trades,c=scan(tf,bars,m1,starts,pivotlist,d15,d1)
        all_events.extend(events);all_trades.extend(trades);counts[tf]=dict(c)
        print("SCANNED",tf,"touches",len(events),"trades",len(trades),flush=True)
    output=Path(out);output.mkdir(parents=True,exist_ok=True)
    # CSV contains complete trade-by-trade risk distribution, including both
    # planned and realized R, with both timeframe and confirmation.
    with (output/"trades.csv").open("w",newline="") as f:
        fields=["tf","confirm","strong","tier","touch","signal","side","zone_center",
            "zone_lo","zone_hi","entry_bid","stop_bid","target_bid","target_tier",
            "target_strong","rr2","period","status","planned_rr","realized_r","pnl","risk"]
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for x in all_trades:
            y={k:x.get(k) for k in fields if k not in ("status","planned_rr","realized_r","pnl","risk")}
            for k in ("planned_rr","realized_r","pnl","risk"):y[k]=x["result"].get(k)
            y["status"]=x["result"]["status"];w.writerow(y)
    (output/"events.json").write_text(json.dumps(all_events,indent=2))
    summary={}
    for tf in ("M5","M15"):
        summary[tf]={}
        for confirm in ("reclaim","break2"):
            summary[tf][confirm]={}
            for only_strong in (False,True):
                tag="strong_target" if only_strong else "nearest_target"
                base=[x for x in all_trades if x["tf"]==tf and x["confirm"]==confirm
                    and x["target_strong"]==only_strong]
                summary[tf][confirm][tag]={}
                for src in ("strong","all"):
                    for rr in ("rr2","all_rr"):
                        for period in ("all","train","test"):
                            group=[x for x in base if (src=="all" or x["strong"])
                                and (rr=="all_rr" or x["rr2"])
                                and (period=="all" or x["period"]==period)]
                            name=f"{src}_{rr}_{period}"
                            summary[tf][confirm][tag][name]=dict(
                                overlapping=summarize(group),
                                one_position=summarize(nonoverlap(group)))
    output_summary=dict(period=[fmt(START),fmt(END)],holdout=fmt(TEST),
        data=dict(m1=len(m1),m5=len(m5),m15=len(m15),h1=len(h1),h4=len(h4),
            pivots=len(pivotlist),first=fmt(m1[0][0]),last=fmt(m1[-1][0])),
        counts=counts,results=summary,
        methodology=dict(levels="Confirmed H1/H4 pivots OR 2 independent confirmed M15 pivots clustered; M15 single pivots weak",
            no_lookahead="Pivot available after right bars close; touch uses only pre-known pivots",
            retouch="A new touch is armed after a completed previous candle CLOSE moves away by max($1.50,0.22 H1 ATR); no fixed 6h cooldown",
            reclaim="Within 3 more candles after touch, bullish candle closes ABOVE support zone upper edge / bearish closes BELOW resistance zone lower edge",
            break2="Reclaim candle additionally closes beyond previous two candle highs/lows",
            entry="Next M1 open after confirmed M5/M15 candle close",
            stop="Beyond most extreme wick from touch through signal and beyond zone by max($0.50,0.08 M15 ATR)",
            target="Nearest pre-known opposite S/R zone edge, .25 USD cushion; alternative strong targets only",
            spread="Constant $0.30 bid-ask + $0.10 slippage each side",
            rr="Projected net RR>=2 filter and unfiltered side-by-side",
            exit="First target/stop within 24h, stop first on ambiguous M1, gaps censored",
            overlap="Report both all signals (may overlap) and sequential one-open-position portfolio",
            validation="Train until 2026-08-17; last three months separate"),
        caveats=["Zone definitions approximate visible chart levels; different broker may differ.",
            "Intrabar OHLC does not reveal actual order of target and stop.",
            "Results are historical, no out-of-sample untouched after exploring strategy.",
            "One-position filter uses actual exit time and prevents simultaneous exposure.",
            "No edits to production bot."])
    (output/"summary.json").write_text(json.dumps(output_summary,indent=2))
    for tf in ("M5","M15"):
        for confirm in ("reclaim","break2"):
            x=summary[tf][confirm]["nearest_target"]["strong_rr2_all"]
            print("RESULT",tf,confirm,json.dumps(x),flush=True)
if __name__=="__main__":run(sys.argv[1],sys.argv[2])
