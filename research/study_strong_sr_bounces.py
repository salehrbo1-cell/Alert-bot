#!/usr/bin/env python3
"""XAUUSD 12m: how often does a *pre-known* strong H1/H4 S/R zone bounce?
Then trade confirmed bounces to the nearest *pre-known* opposite zone.
Research only. Uses closed H1 bars, next M1 open, spread, stop-first.
"""
import bisect,csv,json,math,statistics,sys
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import study_sweep_poi_ifvg_cisd as b

M=b.M;H=b.H;D=24*H
START=b.ts("2025-11-16T00:00:00Z")
END=b.ts("2026-11-16T09:00:00Z")
TEST=b.ts("2026-08-16T00:00:00Z")
SPREAD=.30;SLIPPAGE=.10;MIN_DIST=2.0
LDN=ZoneInfo("Europe/London");NY=ZoneInfo("America/New_York")
def fmt(t):return b.iso(t)
def session(t):
    dt=datetime.fromtimestamp(t/1000,timezone.utc)
    l=8<=dt.astimezone(LDN).hour<17
    n=8<=dt.astimezone(NY).hour<17
    return "overlap" if l and n else "london" if l else "new_york" if n else "other"
def pivots(bars,tf):
    k=3 if tf=="H1" else 2
    found=[]
    for i in range(max(20,2*k),len(bars)):
        p=i-k;x=bars[p];a=b.atr(bars,i)
        if a<=0:continue
        left=bars[p-k:p];right=bars[p+1:i+1]
        if any(z[1]!=q[0] for z,q in zip(bars[p-k:i+1],bars[p-k+1:i+2])):continue
        if all(x[3]>z[3] for z in left+right) and x[3]-max(z[3] for z in left)>=.20*a:
            found.append(dict(id=f"{tf}-R-{p}",side="resistance",tf=tf,
                price=x[3],avail=bars[i][1],atr=a))
        if all(x[4]<z[4] for z in left+right) and min(z[4] for z in left)-x[4]>=.20*a:
            found.append(dict(id=f"{tf}-S-{p}",side="support",tf=tf,
                price=x[4],avail=bars[i][1],atr=a))
    return found
def clusters(active,atr):
    tol=min(7.,max(1.5,.18*atr))
    width=min(6.,max(1.0,.12*atr))
    groups=[]
    for side in ("support","resistance"):
        ps=sorted((p for p in active if p["side"]==side),key=lambda x:x["price"])
        bucket=[]
        def add():
            if not bucket:return
            prices=[p["price"] for p in bucket]
            mid=statistics.median(prices)
            # Distinct confirmed pivots separated by 6h: no fake repeated-touch strength.
            independent=[]
            for p in sorted(bucket,key=lambda x:x["avail"]):
                if not independent or p["avail"]-independent[-1]["avail"]>=6*H:
                    independent.append(p)
            h4=any(p["tf"]=="H4" for p in bucket)
            n=len(independent)
            strong=h4 or n>=2
            groups.append(dict(side=side,center=mid,
                lo=min(prices)-width,hi=max(prices)+width,
                strength=n,h4=h4,strong=strong,
                tier="h4" if h4 else "repeated_h1" if n>=2 else "single_h1",
                ids=[p["id"] for p in bucket]))
        for p in ps:
            if bucket and p["price"]-bucket[0]["price"]>tol:
                add();bucket=[]
            bucket.append(p)
        add()
    return groups
def bounce_sim(m1,starts,touch_time,side,zone,atr):
    # Observe from first actual M1 touch. Do not credit same-minute favorable
    # movement: OHLC does not reveal whether high/low came before contact.
    i=bisect.bisect_left(starts,touch_time)
    if i==len(m1) or m1[i][0]!=touch_time:return None
    ref=zone["hi"] if side=="support" else zone["lo"]
    buffer=max(.50,.10*atr)
    stop=(zone["lo"]-buffer if side=="support" else zone["hi"]+buffer)
    first_hit={5:None,10:None,20:None};stop_at=None;gap_at=None
    prev=None
    end=touch_time+24*H
    for j in range(i,len(m1)):
        x=m1[j];t=x[0]
        if t>=end:break
        if prev is not None and t!=prev[1]:
            gap_at=t;break
        broken=(x[4]<=stop if side=="support" else x[3]>=stop)
        if broken:stop_at=t;break
        if j>i:
            fav=(x[3]-ref if side=="support" else ref-x[4])
            for k in first_hit:
                if first_hit[k] is None and fav>=k:first_hit[k]=t
        prev=x
    results={}
    for dollars,hours in ((5,6),(5,24),(10,6),(10,24),(20,24)):
        horizon=touch_time+hours*H
        hit=first_hit[dollars]
        if hit is not None and hit<horizon:status="bounce"
        elif stop_at is not None and stop_at<horizon:status="break"
        elif gap_at is not None and gap_at<horizon:status="gap"
        elif horizon>m1[-1][1]:status="censored"
        else:status="no_bounce"
        results[f"{dollars}usd_{hours}h"]=status
    return results
def find_touch_minute(m1,starts,bar,zone):
    i=bisect.bisect_left(starts,bar[0])
    for x in m1[i:i+60]:
        if x[0]>=bar[1]:break
        if x[3]>=zone["lo"] and x[4]<=zone["hi"]:return x[0]
    return None
def target_zone(groups,side,entry_bid):
    opposite="resistance" if side=="support" else "support"
    arr=[]
    for z in groups:
        if z["side"]!=opposite:continue
        target=(z["lo"]-.25 if side=="support" else z["hi"]+.25)
        if side=="support" and target>entry_bid+SPREAD+MIN_DIST:arr.append((target,z))
        if side=="resistance" and target<entry_bid-MIN_DIST:arr.append((target,z))
    if not arr:return None
    return min(arr,key=lambda a:a[0]) if side=="support" else max(arr,key=lambda a:a[0])
def trade_sim(m1,starts,at,side,stop_bid,target_bid,hours):
    i=bisect.bisect_left(starts,at)
    if i>=len(m1) or m1[i][0]!=at:return dict(status="missing_entry")
    if at+hours*H>m1[-1][1]:return dict(status="censored")
    buy=side=="support"
    bid=m1[i][2];entry=bid+SPREAD if buy else bid
    stop=stop_bid if buy else stop_bid+SPREAD
    target=target_bid if buy else target_bid+SPREAD
    risk=(entry-stop if buy else stop-entry)+2*SLIPPAGE
    reward=(target-entry if buy else entry-target)-2*SLIPPAGE
    if risk<=0 or reward<=0:return dict(status="invalid",risk=round(risk,3),reward=round(reward,3))
    rr=reward/risk
    prev=None;mfe=mae=0.
    for x in m1[i:i+hours*60]:
        if prev is not None and x[0]!=prev[1]:
            return dict(status="gap",risk=round(risk,3),reward=round(reward,3),
                planned_rr=round(rr,4),realized_r=None,pnl=None)
        hi=x[3] if buy else x[3]+SPREAD
        lo=x[4] if buy else x[4]+SPREAD
        mfe=max(mfe,hi-entry if buy else entry-lo)
        mae=max(mae,entry-lo if buy else hi-entry)
        hit_sl=lo<=stop if buy else hi>=stop
        hit_tp=hi>=target if buy else lo<=target
        if hit_sl:status="stop";pnl=-risk;break
        if hit_tp:status="target";pnl=reward;break
        prev=x
    else:
        status="timeout"
        pnl=((prev[5]-entry) if buy else (entry-prev[5]-SPREAD))-2*SLIPPAGE
    return dict(status=status,risk=round(risk,3),reward=round(reward,3),
        planned_rr=round(rr,4),realized_r=round(pnl/risk,4),pnl=round(pnl,3),
        mfe=round(mfe,3),mae=round(mae,3))
def aggregate_events(events,subset):
    rows=[x for x in events if subset(x)]
    result={"touches":len(rows)}
    for key in ("5usd_6h","5usd_24h","10usd_6h","10usd_24h","20usd_24h"):
        status=Counter(x["bounce"][key] for x in rows)
        valid=len(rows)-status["gap"]-status["censored"]
        result[key]=dict(eligible=valid,bounces=status["bounce"],
            breaks=status["break"],no_bounce=status["no_bounce"],
            gaps=status["gap"],censored=status["censored"],
            rate=round(status["bounce"]/valid,4) if valid else None)
    return result
def aggregate_trades(trades,subset):
    rows=[x for x in trades if subset(x)]
    r=[x["result"]["realized_r"] for x in rows if x["result"].get("realized_r") is not None]
    st=Counter(x["result"]["status"] for x in rows)
    pos=sum(max(0,v) for v in r);neg=-sum(min(0,v) for v in r)
    eq=peak=dd=0.
    for v in r:eq+=v;peak=max(peak,eq);dd=max(dd,peak-eq)
    return dict(n=len(rows),target=st["target"],stop=st["stop"],
        timeout=st["timeout"],gap=st["gap"],censored=st["censored"],
        hit_rate=round(st["target"]/len(r),4) if r else None,
        total_r=round(sum(r),3),avg_r=round(statistics.mean(r),4) if r else None,
        median_r=round(statistics.median(r),4) if r else None,
        avg_planned_rr=round(statistics.mean(x["result"]["planned_rr"] for x in rows
            if x["result"].get("planned_rr") is not None),3) if rows else None,
        profit_factor_r=round(pos/neg,3) if neg else None,
        max_drawdown_r=round(dd,3))
def run(path,out):
    m1=b.read(path);h1=b.resample(m1,60);h4=b.resample(m1,240)
    starts=[x[0] for x in m1]
    all_pivots=sorted(pivots(h1,"H1")+pivots(h4,"H4"),key=lambda p:p["avail"])
    ptr=0;active=[];events=[];trades=[];counts=Counter()
    last_touch=defaultdict(lambda:-10**18)
    for i in range(25,len(h1)):
        x=h1[i];t=x[0]
        while ptr<len(all_pivots) and all_pivots[ptr]["avail"]<=t:
            active.append(all_pivots[ptr]);ptr+=1
        # Discard levels that are stale or invalidated by an earlier closed H1 candle.
        active=[p for p in active if t-p["avail"]<((60 if p["tf"]=="H4" else 21)*D)
            and not p.get("broken",False)]
        if t<START or t>END:
            # Must invalidate even during historical warmup.
            for p in active:
                w=max(1.,.12*p["atr"]);buf=max(.5,.10*p["atr"])
                if (p["side"]=="resistance" and x[5]>p["price"]+w+buf) or (
                    p["side"]=="support" and x[5]<p["price"]-w-buf):
                    p["broken"]=True
            continue
        prev=h1[i-1][5];atr=b.atr(h1,i-1)
        if atr<=0:continue
        gs=clusters(active,atr)
        choices=[]
        for z in gs:
            if z["side"]=="support" and prev>z["hi"]+.25 and x[4]<=z["hi"] and x[3]>=z["lo"]:
                choices.append(z)
            elif z["side"]=="resistance" and prev<z["lo"]-.25 and x[3]>=z["lo"] and x[4]<=z["hi"]:
                choices.append(z)
        counts["h1_bars"]+=1
        if choices:
            counts["bars_touch_any"]+=1
            if len({z["side"] for z in choices})>1:
                counts["bars_touch_both_sides_skipped"]+=1
            else:
                side=choices[0]["side"]
                # The nearest encountered zone is the first from prior close.
                z=max(choices,key=lambda z:z["hi"]) if side=="support" else min(choices,key=lambda z:z["lo"])
                if any(t-last_touch[pid]<6*H for pid in z["ids"]):
                    counts["repeated_touch_cooldown"]+=1
                else:
                    touch=find_touch_minute(m1,starts,x,z)
                    if touch is not None:
                        for pid in z["ids"]:last_touch[pid]=t
                        counts["independent_touches"]+=1
                        bounce=bounce_sim(m1,starts,touch,side,z,atr)
                        if bounce is not None:
                            strict=(x[5]>z["hi"] if side=="support" else x[5]<z["lo"])
                            mid=(x[5]>z["center"] if side=="support" else x[5]<z["center"])
                            event=dict(time=fmt(touch),bar_close=fmt(x[1]),side=side,
                                zone_center=round(z["center"],3),zone_lo=round(z["lo"],3),
                                zone_hi=round(z["hi"],3),strength=z["strength"],
                                h4=z["h4"],tier=z["tier"],strong=z["strong"],
                                signal_close=round(x[5],3),confirmed_mid=mid,
                                confirmed_strict=strict,session=session(x[1]),
                                period="test" if t>=TEST else "train",
                                bounce=bounce)
                            events.append(event)
                            if mid:
                                at=x[1];idx=bisect.bisect_left(starts,at)
                                if idx<len(m1) and m1[idx][0]==at:
                                    entry_bid=m1[idx][2]
                                    tz=target_zone(gs,side,entry_bid)
                                    if tz is None:counts["no_next_opposite_zone"]+=1
                                    else:
                                        target,other=tz
                                        for stop_mode in ("zone","touch_wick"):
                                            buf=max(.50,.10*atr)
                                            stop=(z["lo"]-buf if side=="support" else z["hi"]+buf)
                                            if stop_mode=="touch_wick":
                                                stop=(min(z["lo"],x[4])-buf if side=="support"
                                                    else max(z["hi"],x[3])+buf)
                                            for hours in (12,24):
                                                result=trade_sim(m1,starts,at,side,stop,target,hours)
                                                if result.get("planned_rr") is None:continue
                                                trades.append(dict(time=fmt(at),side=side,
                                                    tier=z["tier"],strong=z["strong"],
                                                    confirmed="strict" if strict else "mid_only",
                                                    stop_mode=stop_mode,hours=hours,
                                                    rr2=result["planned_rr"]>=2.,
                                                    session=session(at),
                                                    period="test" if t>=TEST else "train",
                                                    entry_bid=round(entry_bid,3),
                                                    stop_bid=round(stop,3),
                                                    target_bid=round(target,3),
                                                    target_tier=other["tier"],
                                                    zone_center=round(z["center"],3),
                                                    result=result))
        # Only the just-closed H1 bar can invalidate levels for the NEXT bar.
        for p in active:
            w=max(1.,.12*p["atr"]);buf=max(.5,.10*p["atr"])
            if (p["side"]=="resistance" and x[5]>p["price"]+w+buf) or (
                p["side"]=="support" and x[5]<p["price"]-w-buf):
                p["broken"]=True
    output=Path(out);output.mkdir(parents=True,exist_ok=True)
    (output/"events.json").write_text(json.dumps(events,indent=2))
    (output/"trades.json").write_text(json.dumps(trades,indent=2))
    def save_csv(name,rows):
        if not rows:return
        flattened=[]
        for x in rows:
            y={k:v for k,v in x.items() if k not in ("bounce","result")}
            nested=x.get("bounce",x.get("result",{}))
            y.update({f"outcome_{k}":v for k,v in nested.items()})
            flattened.append(y)
        with (output/name).open("w",newline="") as f:
            w=csv.DictWriter(f,fieldnames=list(flattened[0]));w.writeheader();w.writerows(flattened)
    save_csv("touches.csv",events);save_csv("trades.csv",trades)
    groups={
        "all":lambda x:True,
        "strong":lambda x:x["strong"],
        "weak_single_h1":lambda x:not x["strong"],
        "strong_h4":lambda x:x["tier"]=="h4",
        "strong_repeated_h1":lambda x:x["tier"]=="repeated_h1",
        "support":lambda x:x["side"]=="support",
        "resistance":lambda x:x["side"]=="resistance",
        "train":lambda x:x["period"]=="train",
        "test":lambda x:x["period"]=="test",
        "strong_test":lambda x:x["strong"] and x["period"]=="test",
    }
    touch_stats={key:aggregate_events(events,pred) for key,pred in groups.items()}
    scenarios={}
    for stop_mode in ("zone","touch_wick"):
        for hours in (12,24):
            for strict in (False,True):
                for rr2 in (False,True):
                    name=f"{stop_mode}_{hours}h_{'strict' if strict else 'mid'}_{'rr2' if rr2 else 'all_rr'}"
                    base=[x for x in trades if x["stop_mode"]==stop_mode and x["hours"]==hours
                        and (not strict or x["confirmed"]=="strict") and (not rr2 or x["rr2"])]
                    scenarios[name]={key:aggregate_trades(base,pred) for key,pred in groups.items()}
                    scenarios[name]["london_ny"]=aggregate_trades(base,
                        lambda x:x["session"]!="other")
                    scenarios[name]["strong_london_ny"]=aggregate_trades(base,
                        lambda x:x["strong"] and x["session"]!="other")
    summary=dict(strategy="Pre-confirmed H1/H4 structural support/resistance: touch bounce and trade to next opposite zone",
        start=fmt(START),end=fmt(END),test_start=fmt(TEST),
        data=dict(m1=len(m1),h1=len(h1),h4=len(h4),confirmed_pivots=len(all_pivots),
            first=fmt(m1[0][0]),last=fmt(m1[-1][0])),
        counts=dict(counts),touch_stats=touch_stats,trade_scenarios=scenarios,
        assumptions=dict(levels="H1 3-bar, H4 2-bar confirmed swing pivots, .20 ATR prominence, no future data",
            clustering="Same-side pivots within min(7,max(1.5,.18*H1 ATR)) USD; zone width min(6,max(1,.12*ATR)) USD",
            strength="Strong if >=2 independent confirmed pivots >=6h apart OR >=1 H4 pivot. Weak = single H1 pivot",
            invalidation="Closed H1 beyond pivot price plus width and .10 pivot ATR invalidates future touches",
            expiry="H1 21d, H4 60d; approach from correct side; at most one side touched per H1; 6h level cooldown",
            raw_bounce="First M1 touch, $5/$10/$20 move away before structural break; no same-minute favorable credit",
            entry="H1 touches zone and closes beyond center (mid) or beyond far edge (strict), market next M1 open",
            target="Nearest already-known opposite support/resistance zone boundary with .25 USD cushion",
            stops="zone boundary plus .10 H1 ATR (min .50 USD), or beyond touched H1 candle wick plus same buffer",
            costs="Dukascopy BID M1; synthetic spread .30 USD and .10 USD per-side slippage",
            rr2="Planned net target/risk >=2, before entry; reports also no RR filter",
            holding="12h and 24h; intraminute stop-first, weekend gaps censored; no leverage/lot assumption",
            holdout="2026-08-16 onward, temporal last-quarter split, no fitting performed"),
        caveats=["Exploratory operational definition of strong zones; not market depth/actual resting orders.",
            "Raw bounce rate is conditional on touching a pre-known zone; not trade win rate.",
            "Historical spreads are approximated; minute OHLC has no intra-minute path.",
            "A few concurrent trades can overlap; total R is gross sum, not a portfolio equity curve.",
            "No live bot modifications."])
    (output/"summary.json").write_text(json.dumps(summary,indent=2))
    print("RESULT",json.dumps(dict(counts=dict(counts),
        touches={k:{q:v[q] for q in ("touches","5usd_6h","10usd_24h","20usd_24h")}
            for k,v in touch_stats.items()},
        trades={k:{q:v[q] for q in ("all","strong","strong_test","london_ny")}
            for k,v in scenarios.items() if k in (
                "zone_24h_strict_rr2","zone_24h_mid_rr2",
                "touch_wick_24h_strict_rr2","zone_24h_strict_all_rr")}),indent=2),flush=True)
if __name__=="__main__":run(sys.argv[1],sys.argv[2])
