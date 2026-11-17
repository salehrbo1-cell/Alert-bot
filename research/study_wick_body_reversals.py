#!/usr/bin/env python3
"""XAUUSD: is a dominant wick approximately the size of its candle body a reversal clue?
Causal H1/H4 pivot classification; M5/M15; next 1/3/6 closes and structural-stop
1R/2R first-passage on M1. Pure research; does not modify alert.py.
"""
import bisect,csv,json,math,statistics,sys
from collections import Counter,deque
from pathlib import Path
import study_sweep_poi_ifvg_cisd as b
M,H=b.M,b.H
START=b.ts("2025-11-17T00:00:00Z")
END=b.ts("2026-11-18T00:00:00Z")
TEST=b.ts("2026-08-17T00:00:00Z")
SPREAD=.30;SLIP=.10
def atrs(a,n=14):
    out=[];q=deque();s=0
    for i,x in enumerate(a):
        if i and x[0]!=a[i-1][1]:
            q.clear();s=0
        tr=x[3]-x[4] if not i or x[0]!=a[i-1][1] else max(
            x[3]-x[4],abs(x[3]-a[i-1][5]),abs(x[4]-a[i-1][5]))
        q.append(tr);s+=tr
        if len(q)>n:s-=q.popleft()
        out.append(s/len(q))
    return out
def pivots(a,k,atr,tf):
    out=[]
    for i in range(2*k,len(a)):
        p=i-k;x=a[p];left=a[p-k:p];right=a[p+1:i+1]
        if not left or not right:continue
        if any(a[j][1]!=a[j+1][0] for j in range(p-k,i)):continue
        if x[3]>max(y[3] for y in left+right):
            out.append(dict(t=a[i][1],price=x[3],tf=tf,side="R"))
        if x[4]<min(y[4] for y in left+right):
            out.append(dict(t=a[i][1],price=x[4],tf=tf,side="S"))
    return out
def one_trade(m1,starts,entry_t,side,stop_bid,atr,tf,n=6):
    idx=bisect.bisect_left(starts,entry_t)
    if idx>=len(m1) or m1[idx][0]!=entry_t:return None
    buy=side=="buy";bid=m1[idx][2]
    entry=bid+SPREAD+SLIP if buy else bid-SLIP
    risk=(entry-stop_bid+SLIP) if buy else (stop_bid+SPREAD-entry+SLIP)
    if risk<=.2 or risk>max(35.,4*atr):return None
    tgt1=entry+risk+SLIP if buy else entry-risk-SLIP
    tgt2=entry+2*risk+SLIP if buy else entry-2*risk-SLIP
    result={1:"timeout",2:"timeout"}
    pnl={1:None,2:None}
    prev=None
    end=entry_t+n*tf*M
    for x in m1[idx:]:
        if x[0]>=end:break
        if prev is not None and x[0]!=prev[1]:
            for r in (1,2):
                if pnl[r] is None:result[r]="gap"
            break
        hi=x[3];lo=x[4]
        stop=lo<=stop_bid if buy else hi>=stop_bid
        t1=hi>=tgt1 if buy else lo+SPREAD<=tgt1
        t2=hi>=tgt2 if buy else lo+SPREAD<=tgt2
        for r,hit in ((1,t1),(2,t2)):
            if pnl[r] is not None:continue
            if stop:
                result[r]="stop";pnl[r]=-1.
            elif hit:
                result[r]="target";pnl[r]=float(r)
        prev=x
        if pnl[1] is not None and pnl[2] is not None:break
    if prev is None:return None
    for r in (1,2):
        if pnl[r] is not None:continue
        if result[r]=="gap" or prev[1]<end:
            result[r]="gap";continue
        close=prev[5]
        cash=(close-entry-SLIP) if buy else (entry-(close+SPREAD)-SLIP)
        pnl[r]=cash/risk
    return dict(entry=round(entry,3),stop=round(stop_bid,3),
        risk=round(risk,3),r1_status=result[1],r1=round(pnl[1],4) if pnl[1] is not None else None,
        r2_status=result[2],r2=round(pnl[2],4) if pnl[2] is not None else None)
def summarise(rows):
    out=dict(n=len(rows))
    for r in (1,2):
        k=f"r{r}";vals=[x[k] for x in rows if x[k] is not None]
        c=Counter(x[f"{k}_status"] for x in rows)
        out[k]=dict(evaluated=len(vals),target=c["target"],stop=c["stop"],
            timeout=c["timeout"],gap=c["gap"],
            target_rate=round(c["target"]/len(vals),4) if vals else None,
            total_r=round(sum(vals),2),avg_r=round(statistics.mean(vals),4) if vals else None,
            median_r=round(statistics.median(vals),4) if vals else None)
    for h in (1,3,6):
        vals=[x[f"close_{h}"] for x in rows if x[f"close_{h}"] is not None]
        out[f"close_{h}"]=dict(n=len(vals),positive=sum(x>0 for x in vals),
            positive_rate=round(sum(x>0 for x in vals)/len(vals),4) if vals else None,
            median=round(statistics.median(vals),3) if vals else None)
    return out
def run(path,dest):
    m1=b.read(path);starts=[x[0] for x in m1]
    m5=b.resample(m1,5);m15=b.resample(m1,15)
    h1=b.resample(m1,60);h4=b.resample(m1,240)
    a1=atrs(h1)
    piv=sorted(pivots(h1,3,a1,"H1")+pivots(h4,2,atrs(h4),"H4"),
        key=lambda x:x["t"])
    h1_ends=[x[1] for x in h1]
    allrows=[];day3868=[]
    for tf,bars in ((5,m5),(15,m15)):
        ats=atrs(bars);ptr=0;active=[]
        for i in range(20,len(bars)-7):
            x=bars[i];t=x[0];end=x[1]
            if t<START or t>=END:continue
            while ptr<len(piv) and piv[ptr]["t"]<=t:
                active.append(piv[ptr]);ptr+=1
            active=[z for z in active if t-z["t"]<=
                (21 if z["tf"]=="H1" else 60)*24*H]
            a=ats[i];body=abs(x[5]-x[2])
            if body<max(.10,.10*a):continue
            upper=x[3]-max(x[2],x[5]);lower=min(x[2],x[5])-x[4]
            if upper>=lower*1.5 and upper>=max(.3,.15*a):
                side="sell";wick=upper;opposite=lower;tip=x[3]
            elif lower>=upper*1.5 and lower>=max(.3,.15*a):
                side="buy";wick=lower;opposite=upper;tip=x[4]
            else:continue
            ratio=wick/body
            category=("near_body" if .75<=ratio<=1.25 else
                "very_long" if ratio>=1.5 else
                "short" if .2<=ratio<=.5 else "other")
            if category=="other":continue
            # Only pivots available before the signal candle opened.
            hi=max(0,bisect.bisect_right(h1_ends,t)-1)
            tol=max(1.,.12*a1[hi])
            near=[z for z in active if abs(z["price"]-tip)<=tol]
            level=("H4" if any(z["tf"]=="H4" for z in near) else
                "H1" if near else "none")
            matched_side=("same" if any(z["side"]==("R" if side=="sell" else "S")
                for z in near) else "flip" if near else "none")
            # Require complete forward candle sequence for all horizons.
            if bars[i+6][1]!=end+6*tf*M:continue
            if any(bars[j][1]!=bars[j+1][0] for j in range(i,i+6)):continue
            stop=x[4]-max(.25,.05*a) if side=="buy" else x[3]+max(.25,.05*a)
            result=one_trade(m1,starts,end,side,stop,a,tf)
            if result is None:continue
            row=dict(tf=f"M{tf}",utc=b.iso(t),side=side,
                category=category,ratio=round(ratio,3),wick=round(wick,3),
                body=round(body,3),atr=round(a,3),wick_tip=round(tip,3),
                level=level,level_side=matched_side,
                period="test" if t>=TEST else "train",**result)
            for h in (1,3,6):
                future=bars[i+h][5]
                move=(future-x[5]) if side=="buy" else (x[5]-future)
                row[f"close_{h}"]=round(move,3)
            allrows.append(row)
            if b.ts("2026-11-16T21:00:00Z")<=t<b.ts("2026-11-17T21:00:00Z") and abs(tip-3868.30)<=1.5:
                day3868.append(row)
        print("DONE",tf,"events",len([z for z in allrows if z["tf"]==f"M{tf}"]),flush=True)
    dest=Path(dest);dest.mkdir(parents=True,exist_ok=True)
    keys=list(allrows[0]) if allrows else []
    with (dest/"events.csv").open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(allrows)
    groups={}
    for tf in ("M5","M15"):
        groups[tf]={}
        for cat in ("near_body","very_long","short"):
            groups[tf][cat]={}
            for loc in ("all","near_H1_H4","away","near_H4"):
                groups[tf][cat][loc]={}
                for period in ("all","train","test"):
                    v=[x for x in allrows if x["tf"]==tf and x["category"]==cat
                        and (loc=="all" or loc=="near_H1_H4" and x["level"]!="none"
                            or loc=="away" and x["level"]=="none"
                            or loc=="near_H4" and x["level"]=="H4")
                        and (period=="all" or x["period"]==period)]
                    groups[tf][cat][loc][period]=summarise(v)
    out=dict(data=dict(m1=len(m1),first=b.iso(m1[0][0]),last=b.iso(m1[-1][0])),
        definitions=dict(near_body="dominant wick/body ratio 0.75-1.25",
            very_long="dominant wick/body >=1.5",short="dominant wick/body 0.2-0.5",
            dominance="chosen wick >=1.5 x opposite wick",
            significant="body >=max($0.10,0.10 ATR timeframe), wick >=max($0.30,0.15 ATR)",
            direction="upper wick => bearish reversal hypothesis; lower => bullish",
            near_level="wick tip within max($1,0.12 H1 ATR) of pre-confirmed H1/H4 pivot",
            pivot_confirmation="H1 3 bars to right, H4 2 bars to right; known before signal candle opens",
            entry="next M1 open after M5/M15 signal candle closes",
            stop="beyond wick tip by max($0.25,0.05 ATR timeframe)",
            r1="1R favorable before structural stop within next six timeframe candles",
            r2="2R favorable before structural stop within next six timeframe candles",
            close="directional move from signal candle close to next 1/3/6 candle close",
            spread="assumed constant $0.30 and slippage $0.10 each side",
            gaps="uninterrupted future bars required, censored M1 gaps",
            overlap="all qualifying signals counted; many overlap and are statistically dependent"),
        groups=groups,around_3868_17nov=day3868,
        warnings=["Wick shape alone is not proof of trend reversal; often a pullback.",
            "Pivot proximity may include previously broken levels; flipped side separately labeled.",
            "This is a post-hoc study of wick categories suggested by observation; not out-of-sample validation.",
            "No real execution, commissions or changing broker spreads; overlapping trades not a portfolio.",
            "A wick close pattern does not establish hidden institutional orders."])
    (dest/"summary.json").write_text(json.dumps(out,indent=2))
    for tf in ("M5","M15"):
        for cat in ("near_body","very_long","short"):
            print("RESULT",tf,cat,
                json.dumps(groups[tf][cat]["all"]["all"]),flush=True)
    print("NEAR3868",len(day3868),json.dumps(day3868[:12]),flush=True)
if __name__=="__main__":run(sys.argv[1],sys.argv[2])
