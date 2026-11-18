#!/usr/bin/env python3
"""Research: confirmed near-body wick at historical XAU resistance or monthly close.
After next candle confirmation, place sell LIMIT at upper edge of rejection
candle body, measure favorable excursion until first revisit of entry.
No changes to production trading bot.
"""
import bisect,csv,json,math,statistics,sys
from collections import Counter,deque,defaultdict
from datetime import datetime,timezone
from pathlib import Path
import study_sweep_poi_ifvg_cisd as b
M,H=b.M,b.H
START=b.ts("2025-11-17T00:00:00Z")
TEST=b.ts("2026-08-17T00:00:00Z")
END=b.ts("2026-11-18T00:00:00Z")
WAIT=90*M;HOLD=24*H;RESET=.50
def atrs(a,n=14):
    out=[];q=deque();s=0
    for i,x in enumerate(a):
        if i and x[0]!=a[i-1][1]:q.clear();s=0
        tr=x[3]-x[4] if not i or x[0]!=a[i-1][1] else max(
            x[3]-x[4],abs(x[3]-a[i-1][5]),abs(x[4]-a[i-1][5]))
        q.append(tr);s+=tr
        if len(q)>n:s-=q.popleft()
        out.append(s/len(q))
    return out
def pivots(a,k,tf):
    out=[]
    for i in range(2*k,len(a)):
        p=i-k;x=a[p];left=a[p-k:p];right=a[p+1:i+1]
        if any(a[j][1]!=a[j+1][0] for j in range(p-k,i)):continue
        if x[3]>max(y[3] for y in left+right):
            out.append(dict(t=a[i][1],price=x[3],tf=tf,kind="high"))
        if x[4]<min(y[4] for y in left+right):
            out.append(dict(t=a[i][1],price=x[4],tf=tf,kind="low"))
    return out
def month_id(t):
    dt=datetime.fromtimestamp(t/1000,timezone.utc)
    return dt.year*12+dt.month
def monthly_closes(m1):
    monthly={}
    for x in m1:monthly[month_id(x[0])]=(x[1],x[5])
    return monthly
def fill_and_excursion(m1,starts,after,entry,tf):
    """Sell limit at entry BID, first touch after confirmation. Return to entry
    after 0.5$ departure. M1 high/low within same candle order is unknown:
    exclude fill candle and revisit candle lows; treat touch as immediate return
    when observed before documented departure.
    """
    i=bisect.bisect_left(starts,after)
    endfill=after+WAIT
    fill=None;prev=None
    while i<len(m1) and m1[i][0]<endfill:
        x=m1[i]
        if prev is not None and x[0]!=prev[1]:return dict(status="gap_pre_fill")
        if x[3]>=entry:
            fill=i;break
        prev=x;i+=1
    if fill is None:return dict(status="unfilled")
    start=m1[fill][0];low=entry;prev=m1[fill];departed=False
    j=fill+1
    while j<len(m1) and m1[j][0]<start+HOLD:
        x=m1[j]
        if x[0]!=prev[1]:return dict(status="gap_after_fill",mfe=round(max(0,entry-low),3))
        if not departed:
            # Conservative: if candle both touches entry and crosses departure
            # threshold, assume return happened first.
            if x[3]>=entry:
                return dict(status="returned",mfe=0.0,mins=int((x[0]-start)/M),fill_utc=b.iso(start))
            low=min(low,x[4])
            if low<=entry-RESET:departed=True
        else:
            if x[3]>=entry:
                return dict(status="returned",mfe=round(max(0,entry-low),3),
                    mins=int((x[0]-start)/M),fill_utc=b.iso(start))
            low=min(low,x[4])
        prev=x;j+=1
    if j>=len(m1) or prev[1]<start+HOLD:
        return dict(status="censored_end",mfe=round(max(0,entry-low),3),fill_utc=b.iso(start))
    return dict(status="not_returned_24h",mfe=round(max(0,entry-low),3),fill_utc=b.iso(start))
def summarise(rows):
    s=Counter(x["status"] for x in rows)
    fills=[x for x in rows if x["status"] in ("returned","not_returned_24h")]
    returned=[x for x in fills if x["status"]=="returned"]
    vals=[x["mfe"] for x in fills]
    rvals=[x["mfe"] for x in returned]
    def pct(v,p):
        if not v:return None
        return round(sorted(v)[min(len(v)-1,max(0,math.ceil(len(v)*p)-1))],3)
    return dict(signals=len(rows),filled=len(fills),fill_rate=round(len(fills)/len(rows),4) if rows else None,
        returned=len(returned),not_returned_24h=s["not_returned_24h"],
        unfilled=s["unfilled"],gaps=s["gap_pre_fill"]+s["gap_after_fill"]+s["censored_end"],
        returned_pct_of_filled=round(len(returned)/len(fills),4) if fills else None,
        median_mfe_returned=pct(rvals,.5),p25_mfe_returned=pct(rvals,.25),
        p75_mfe_returned=pct(rvals,.75),p90_mfe_returned=pct(rvals,.90),
        median_mfe_all_filled=pct(vals,.5),
        pct_ge_3=round(sum(x>=3 for x in vals)/len(vals),4) if vals else None,
        pct_ge_5=round(sum(x>=5 for x in vals)/len(vals),4) if vals else None,
        pct_ge_10=round(sum(x>=10 for x in vals)/len(vals),4) if vals else None,
        pct_ge_20=round(sum(x>=20 for x in vals)/len(vals),4) if vals else None,
        median_minutes_to_return=pct([x["mins"] for x in returned],.5))
def run(path,dest):
    m1=b.read(path);starts=[x[0] for x in m1]
    m5=b.resample(m1,5);m15=b.resample(m1,15)
    h1=b.resample(m1,60);h4=b.resample(m1,240)
    a1=atrs(h1);piv=sorted(pivots(h1,3,"H1")+pivots(h4,2,"H4"),key=lambda x:x["t"])
    monthly=monthly_closes(m1);h1_ends=[x[1] for x in h1]
    print("DATA",len(m1),len(m5),len(m15),"MONTH SEP25",monthly.get(2025*12+9),flush=True)
    allrows=[]
    for tf,bars in ((5,m5),(15,m15)):
        ats=atrs(bars);ptr=0;active=[]
        for i in range(20,len(bars)-2):
            x=bars[i];t=x[0];end=x[1]
            if t<START or t>=END:continue
            while ptr<len(piv) and piv[ptr]["t"]<=t:
                active.append(piv[ptr]);ptr+=1
            active=[z for z in active if t-z["t"]<=(21 if z["tf"]=="H1" else 60)*24*H]
            a=ats[i];body=abs(x[5]-x[2]);upper=x[3]-max(x[2],x[5]);lower=min(x[2],x[5])-x[4]
            if body<max(.1,.1*a) or upper<max(.3,.15*a) or upper<1.5*lower:continue
            ratio=upper/body
            if not (.75<=ratio<=1.25):continue
            if bars[i+1][0]!=end:continue
            nextbar=bars[i+1]
            # Only confirmed before signal opens; price close must reject from below.
            hi=max(0,bisect.bisect_right(h1_ends,t)-1)
            tol=max(1.,.12*a1[hi])
            near_high=[z for z in active if z["kind"]=="high" and
                abs(z["price"]-x[3])<=tol and x[5]<z["price"]]
            # A former support pivot counts as flipped resistance only if
            # a completed H1 bar prior to signal had closed beneath it.
            h1past=h1[max(0,hi-48):hi+1]
            near_flip=[z for z in active if z["kind"]=="low" and
                abs(z["price"]-x[3])<=tol and x[5]<z["price"] and
                any(y[1]>=z["t"] and y[1]<=t and y[5]<z["price"]-max(.2,.05*a1[hi])
                    for y in h1past)]
            mid=month_id(t)
            mon=[(mm,price) for mm,(endmonth,price) in monthly.items()
                if mm<mid and mm>=mid-18 and abs(price-x[3])<=tol and x[5]<price]
            # all historical closes; the closest matching month is selected
            mon.sort(key=lambda z:abs(z[1]-x[3]))
            monthmatch=mon[0] if mon else None
            at_high=bool(near_high);at_flip=bool(near_flip);at_month=bool(mon)
            # Confirmation at next close: below body low, and strict below wick candle low.
            confirm_body=nextbar[5]<min(x[2],x[5]) and nextbar[5]<nextbar[2]
            confirm_extreme=nextbar[5]<x[4]
            # Entry limit at upper edge of rejection candle's BODY, not wick tip.
            entry=max(x[2],x[5])
            # No hypothetical fill on the confirmation candle; after it closes.
            outcome=fill_and_excursion(m1,starts,nextbar[1],entry,tf)
            row=dict(tf=f"M{tf}",signal_utc=b.iso(t),period="test" if t>=TEST else "train",
                upper_wick=round(upper,3),body=round(body,3),ratio=round(ratio,3),
                signal_high=round(x[3],3),signal_low=round(x[4],3),
                signal_close=round(x[5],3),entry=round(entry,3),
                confirm_body=confirm_body,confirm_extreme=confirm_extreme,
                historical_high=at_high,flipped_support=at_flip,
                historical_resistance=at_high or at_flip,
                monthly_close=at_month,prior_month_close=bool(mon and any(mm==mid-1 for mm,_ in mon)),
                month_match=monthmatch[0] if monthmatch else None,
                month_price=round(monthmatch[1],3) if monthmatch else None,
                level="monthly_and_resistance" if at_month and (at_high or at_flip)
                    else "monthly" if at_month else "resistance" if at_high or at_flip else "none",
                **outcome)
            allrows.append(row)
        print("FINISHED",tf,sum(x["tf"]==f"M{tf}" for x in allrows),flush=True)
    dest=Path(dest);dest.mkdir(parents=True,exist_ok=True)
    with (dest/"events.csv").open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(allrows[0]));w.writeheader();w.writerows(allrows)
    groups={}
    for tf in ("M5","M15"):
        groups[tf]={}
        for conf in ("body","extreme","unconfirmed"):
            groups[tf][conf]={}
            for level in ("all","historical_resistance","monthly_close","prior_month_close",
                "monthly_or_resistance","monthly_and_resistance","none","flipped_support","historical_high"):
                groups[tf][conf][level]={}
                for period in ("all","train","test"):
                    v=[x for x in allrows if x["tf"]==tf and
                        (conf=="body" and x["confirm_body"] or conf=="extreme" and x["confirm_extreme"]
                            or conf=="unconfirmed" and not x["confirm_body"]) and
                        (level=="all" or level=="monthly_or_resistance" and
                            (x["monthly_close"] or x["historical_resistance"]) or
                         level=="none" and x["level"]=="none" or
                         level=="monthly_and_resistance" and
                            x["monthly_close"] and x["historical_resistance"] or
                         level in x and bool(x[level])) and
                        (period=="all" or x["period"]==period)]
                    groups[tf][conf][level][period]=summarise(v)
    sep=2025*12+9
    around=[x for x in allrows if b.ts("2026-11-16T21:00:00Z")<=
        b.ts(x["signal_utc"])<b.ts("2026-11-17T21:00:00Z") and
        abs(x["signal_high"]-3868.3)<=1.5]
    out=dict(data=dict(m1=len(m1),start=b.iso(m1[0][0]),end=b.iso(m1[-1][0]),
        september_2025_month_close=monthly.get(sep,[None,None])[1]),
        definitions=dict(near_body="upper wick/body ratio 0.75–1.25; upper wick >=1.5x lower wick",
            confirmation_body="immediately next candle bearish close below lower edge of rejection candle body",
            confirmation_extreme="immediately next candle close below rejection candle low",
            historical_resistance="confirmed H1/H4 swing high, or H1/H4 swing low broken by earlier H1 close",
            monthly_close="any completed month close within 18 months, known before signal",
            tolerance="max($1,0.12 x H1 ATR) from upper wick tip, with signal close below level",
            entry="sell limit at upper edge of rejection candle body, placed after next confirmation candle closes",
            wait="90 min to fill",reset="after fill, require $0.50 downward departure before first revisit",
            revisit="first M1 high >= entry after documented departure; 0 excursion if immediate revisit",
            excursion="entry - lowest M1 low BEFORE revisit minute; conservative when M1 candle ordering unknown",
            censor="maximum 24h after fill; unreturned reported separately, not assumed returned",
            no_trade_costs="price excursion in USD/oz; no spread or slippage deducted",
            overlap="every qualifying candle; many overlap, so not independent portfolio trades",
            train="2025-11-17 through 2026-08-16",test="2026-08-17 through 2026-11-17"),
        groups=groups,around_3868=around,
        caveats=["A confirmed candle may not fill the limit; fill rate reported.",
            "24h non-return observations are right-censored; report separately.",
            "If the fill minute moved down before touching the limit, M1 OHLC cannot reveal order; fill-minute movement excluded.",
            "First-return excursion is not realized profit, and no costs or stop are modeled.",
            "The near-level filter uses a pre-confirmed pivot, but overlap makes naive confidence intervals invalid.",
            "This is not a proof of an institutional or secret level."])
    (dest/"summary.json").write_text(json.dumps(out,indent=2))
    for tf in ("M5","M15"):
        for conf in ("body","extreme"):
            for lev in ("monthly_close","historical_resistance","monthly_or_resistance","none"):
                print("RESULT",tf,conf,lev,json.dumps(groups[tf][conf][lev]["all"]),flush=True)
    print("AROUND",len(around),flush=True)
if __name__=="__main__":run(sys.argv[1],sys.argv[2])
