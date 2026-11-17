#!/usr/bin/env python3
"""Focused historical XAUUSD study of the price area 3868.30.
This is descriptive research, not an alert generator or a trading signal.
Only available M1 BID bars are used; all times explicit.
"""
import csv,json,statistics,sys,bisect,math
from collections import Counter,defaultdict
from datetime import datetime,timezone,timedelta
from pathlib import Path
import study_sweep_poi_ifvg_cisd as b
P=3868.30
M=b.M;H=b.H;KSA=timezone(timedelta(hours=3))
def iso(t):return b.iso(t)
def ksa(t):return datetime.fromtimestamp(t/1000,KSA).strftime("%Y-%m-%d %H:%M")
def day(t):return datetime.fromtimestamp(t/1000,KSA).strftime("%Y-%m-%d")
def rnd(v):return round(v,3)
def med(x):return round(statistics.median(x),3) if x else None
def atr(a,i):
    if i<14:return None
    return statistics.mean(max(a[j][3]-a[j][4],
        abs(a[j][3]-a[j-1][5]),abs(a[j][4]-a[j-1][5]))
        for j in range(i-13,i+1))
def pivots(arr,tf,k):
    out=[]
    for i in range(k,len(arr)-k):
        x=arr[i]
        l=arr[i-k:i];r=arr[i+1:i+k+1]
        if x[4]<min(y[4] for y in l+r):
            out.append(dict(tf=tf,kind="swing_low",price=rnd(x[4]),
                occurred_ksa=ksa(x[0]),confirmed_ksa=ksa(arr[i+k][1]),
                diff_from_3868=rnd(x[4]-P)))
        if x[3]>max(y[3] for y in l+r):
            out.append(dict(tf=tf,kind="swing_high",price=rnd(x[3]),
                occurred_ksa=ksa(x[0]),confirmed_ksa=ksa(arr[i+k][1]),
                diff_from_3868=rnd(x[3]-P)))
    return out
def group(m1,which):
    return [x for x in m1 if day(x[0])==which]
def candles(arr):
    if not arr:return {}
    return dict(first_ksa=ksa(arr[0][0]),last_ksa=ksa(arr[-1][0]),
        n=len(arr),open=rnd(arr[0][2]),high=rnd(max(x[3] for x in arr)),
        low=rnd(min(x[4] for x in arr)),close=rnd(arr[-1][5]))
def candle_rejections(bars,width):
    lo=P-width;hi=P+width;out={"R":[],"S":[]}
    for x in bars:
        op,h,l,cl=x[2:]
        body=max(.10,abs(op-cl))
        up=h-max(op,cl);dn=min(op,cl)-l
        if h>=lo and op<lo and cl<lo and cl<op and up>=1.5*body:
            out["R"].append(dict(t=ksa(x[0]),o=rnd(op),h=rnd(h),l=rnd(l),
                c=rnd(cl),wick_body=rnd(up/body)))
        if l<=hi and op>hi and cl>hi and cl>op and dn>=1.5*body:
            out["S"].append(dict(t=ksa(x[0]),o=rnd(op),h=rnd(h),l=rnd(l),
                c=rnd(cl),wick_body=rnd(dn/body)))
    return {side:dict(count=len(v),samples=v[:12]) for side,v in out.items()}
def independent_events(arr,width,reset=3.,minsep=5):
    lo=P-width;hi=P+width
    state=None;last_touch=-10**20;ev=[]
    for i in range(1,len(arr)):
        x=arr[i];prev=arr[i-1]
        if x[0]!=prev[1]:
            state=None;continue
        if state is None:
            if prev[5]<=lo-reset:state="R"
            elif prev[5]>=hi+reset:state="S"
        if state is None:continue
        side=state
        hit=(x[3]>=lo if side=="R" else x[4]<=hi)
        if hit:
            if x[0]-last_touch>=minsep*M:
                ev.append(dict(side=side,touch_ksa=ksa(x[0]),touch_utc=iso(x[0]),
                    i=i,entry_side_price=rnd(prev[5]),
                    touch_high=rnd(x[3]),touch_low=rnd(x[4]),
                    touch_close=rnd(x[5])))
                last_touch=x[0]
            state=None
    return ev
def eval_event(ev,arr,width):
    i=ev["i"];side=ev["side"]
    direction=-1 if side=="R" else 1
    result={}
    for horizon in (5,15,30,60):
        seq=arr[i+1:i+1+horizon]
        # skip incomplete, weekend gaps, and last incomplete horizon
        if len(seq)<horizon or seq[-1][1]!=arr[i][1]+horizon*M:
            result[str(horizon)]={"status":"incomplete"};continue
        closes=direction*(seq[-1][5]-P)
        mf=max(direction*(x[3]-P if direction>0 else x[4]-P) for x in seq)
        ma=min(direction*(x[4]-P if direction>0 else x[3]-P) for x in seq)
        # First passage: $3 favorable vs $2 adverse, after touch,
        # and if both in same minute mark ambiguous (no tick sequence).
        status="neither"
        for x in seq:
            fav=(x[3]>=P+3 if direction>0 else x[4]<=P-3)
            adv=(x[4]<=P-2 if direction>0 else x[3]>=P+2)
            if fav or adv:
                status="ambiguous" if fav and adv else ("reject_3_first" if fav else "break_2_first")
                break
        result[str(horizon)]=dict(status=status,close_move=rnd(closes),
            favorable_excursion=rnd(mf),adverse_excursion=rnd(-ma))
    return result
def summarize(events):
    out={}
    for d in ("2026-11-16","2026-11-17"):
        out[d]={}
        for side in ("R","S"):
            subset=[x for x in events if x["touch_ksa"].startswith(d) and x["side"]==side]
            entry=[x.get("outcome",{}).get("30",{}) for x in subset]
            c=Counter(x.get("status","incomplete") for x in entry)
            completed=[x for x in entry if x.get("status") not in (None,"incomplete")]
            out[d][side]=dict(touches=len(subset),outcome_30m=dict(c),
                median_30m_close_move=med([x["close_move"] for x in completed]),
                median_30m_favorable_excursion=med([x["favorable_excursion"] for x in completed]),
                first_touches=[x["touch_ksa"] for x in subset[:15]])
    return out
def crossings(bars,width):
    lo=P-width;hi=P+width
    close_above=[x for x in bars if x[5]>hi]
    close_below=[x for x in bars if x[5]<lo]
    within=[x for x in bars if lo<=x[5]<=hi]
    up=down=0;last=None
    for x in bars:
        status="above" if x[5]>hi else "below" if x[5]<lo else "inside"
        if status in ("above","below"):
            if last=="below" and status=="above":up+=1
            if last=="above" and status=="below":down+=1
            last=status
    return dict(total=len(bars),close_above=len(close_above),close_below=len(close_below),
        close_inside=len(within),crossed_up=up,crossed_down=down)
def run(path,out):
    m1=b.read(path)
    if not m1:raise SystemExit("No data")
    m5=b.resample(m1,5);m15=b.resample(m1,15)
    h1=b.resample(m1,60);h4=b.resample(m1,240)
    allp=pivots(h1,"H1",3)+pivots(h4,"H4",2)+pivots(m15,"M15",2)
    near=sorted((x for x in allp if abs(x["diff_from_3868"])<=3),
        key=lambda x:(abs(x["diff_from_3868"]),x["occurred_ksa"]))
    # Data must include both days. If source is delayed, explicitly show end.
    periods={}
    for d in ("2026-11-12","2026-11-13","2026-11-16","2026-11-17"):
        s=group(m1,d)
        periods[d]=dict(ohlc=candles(s))
        for tf,series in (("M1",m1),("M5",m5),("M15",m15)):
            bars=group(series,d)
            periods[d][tf]={f"width_{w}":crossings(bars,w) for w in (.5,1.,1.5)}
            if tf!="M1":
                periods[d][tf]["wick_rejections"]=candle_rejections(bars,1.)
                periods[d][tf]["atr14_last"]=rnd(atr(series,series.index(bars[-1]))) if bars and atr(series,series.index(bars[-1])) is not None else None
    focus=[x for x in m1 if b.ts("2026-11-15T21:00:00Z")<=x[0]<b.ts("2026-11-17T21:00:00Z")]
    sensitivity={}
    detailed=[]
    for width in (.5,1.,1.5):
        for reset in (3.,5.):
            events=independent_events(focus,width,reset)
            for x in events:x["outcome"]=eval_event(x,focus,width)
            key=f"halfwidth_{width}_reset_{reset}"
            sensitivity[key]=summarize(events)
            if width==1. and reset==3.:
                detailed=events
    # Volume is broker-provided price feed volume, not centralized COMEX trade volume.
    vrows=[]
    with open(path,newline="") as f:
        for row in csv.DictReader(f):
            try:
                t=b.ts(row.get("Etc/UTC","")) if "Etc/UTC" in row else int(float(row["timestamp"]))
                vol=float(row.get("Volume",row.get("volume",0)))
                if math.isfinite(vol):vrows.append((t,vol))
            except (ValueError,KeyError,TypeError):pass
    vol_by_t=dict(vrows)
    bins=[]
    for center in (P-6,P-4,P-2,P,P+2,P+4,P+6):
        a=[x for x in focus if x[3]>=center-1 and x[4]<=center+1]
        bins.append(dict(center=rnd(center),bars_touching=len(a),
            sum_feed_volume=(rnd(sum(vol_by_t.get(x[0],0) for x in a)) if vol_by_t else None)))
    results=dict(price=P,data=dict(first_utc=iso(m1[0][0]),last_utc=iso(m1[-1][0]),
        last_ksa=ksa(m1[-1][0]),m1=len(m1),m5=len(m5),m15=len(m15),h1=len(h1),h4=len(h4)),
        periods=periods,nearby_confirmed_pivots=near[:45],
        independent_touch_sensitivity=sensitivity,
        detailed_touches=detailed,nearby_price_activity_bins=bins,
        rules=dict(timezone="Saudi Arabia UTC+03:00; day boundary 21:00 UTC",
            quote="Dukascopy XAUUSD BID (not live MT5 broker quote)",
            area="center 3868.30, +/- 0.5 / 1.0 / 1.5 USD tested",
            reset="Independent touch only after price closes at least 3 or 5 USD away, minimum 5 minutes separation",
            approach="Resistance approached from below, support approached from above; state known before touch",
            bounce="Within next 5/15/30/60 min: $3 in rejection direction before $2 against = reject_3_first",
            break_rule="Within next 5/15/30/60 min: $2 adverse before $3 favorable = break_2_first",
            incomplete="Insufficient future data not counted as success/failure",
            ambiguity="Both thresholds in same minute classified ambiguous",
            wick="M5/M15 bearish/bullish wick >= 1.5x candle body and candle closes back on approach side",
            htf_pivots="Strict k=3 H1, k=2 H4, k=2 M15 pivots confirmed only after right bars close",
            volume="This Dukascopy download omits volume; any missing volume metric is null. No COMEX/order book volume is available."),
        caveats=["Price 3868.30 is not a point with infinite precision; broker spreads, liquidity and volatility create zones.",
            "Multiple retests of one level are not independent statistical samples.",
            "A resistance may flip to support after a confirmed break; count both separately.",
            "No order book, resting orders, institutional positioning, or actual MT5 broker spread in this data.",
            "If most recent Dukascopy candle is earlier than current time, today's study is incomplete.",
            "Observed historical reactions cannot guarantee the next reaction."])
    dest=Path(out);dest.mkdir(parents=True,exist_ok=True)
    (dest/"analysis.json").write_text(json.dumps(results,indent=2,ensure_ascii=False))
    with (dest/"touches.csv").open("w",newline="") as f:
        fields=["touch_ksa","touch_utc","side","entry_side_price","touch_high","touch_low",
            "touch_close","status_15m","close_move_15m","status_30m","close_move_30m",
            "favorable_30m","adverse_30m","status_60m","close_move_60m"]
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for x in detailed:
            y={k:x.get(k) for k in fields}
            for n in (15,30,60):
                r=x["outcome"].get(str(n),{})
                y[f"status_{n}m"]=r.get("status")
                y[f"close_move_{n}m"]=r.get("close_move")
            y["favorable_30m"]=x["outcome"].get("30",{}).get("favorable_excursion")
            y["adverse_30m"]=x["outcome"].get("30",{}).get("adverse_excursion")
            w.writerow(y)
    print("DATA",json.dumps(results["data"]))
    print("PIVOTS",json.dumps(near[:12]))
    print("PRIMARY",json.dumps(sensitivity["halfwidth_1.0_reset_3.0"]))
    print("DAY17",json.dumps(periods["2026-11-17"]))
if __name__=="__main__":run(sys.argv[1],sys.argv[2])
