#!/usr/bin/env python3
"""AutoResearch-inspired XAUUSD rule search. Research only; never trades or edits alert.py."""
import bisect,csv,itertools,json,math,statistics,sys
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
M=60000;H=60*M;SPREAD=.30;SLIP=.10;HOLD=8*H;WAIT=60*M;EMBARGO=24*H

def load(path):
    a={}
    with open(path,newline="") as f:
        for r in csv.DictReader(f):
            try:
                t=int(float(r["timestamp"])) if "timestamp" in r else int(datetime.fromisoformat(r["Etc/UTC"].replace("Z","+00:00")).timestamp()*1000)
                o,h,l,c=(float(r[k]) for k in ("open","high","low","close")) if "timestamp" in r else (float(r[k]) for k in ("Open","High","Low","Close"))
                if all(map(math.isfinite,(o,h,l,c))) and h>=max(o,c) and l<=min(o,c):a[t]=(t,t+M,o,h,l,c)
            except (ValueError,KeyError,TypeError):pass
    return [a[t] for t in sorted(a)]

def resample(a,n):
    step=n*M;out=[];part=[];bucket=None
    for x in a:
        k=x[0]//step*step
        if bucket is not None and k!=bucket:
            if len(part)==n and part[0][0]==bucket and part[-1][1]==bucket+step:
                out.append((bucket,bucket+step,part[0][2],max(z[3] for z in part),min(z[4] for z in part),part[-1][5]))
            part=[]
        part.append(x);bucket=k
    if len(part)==n and part[0][0]==bucket and part[-1][1]==bucket+step:
        out.append((bucket,bucket+step,part[0][2],max(z[3] for z in part),min(z[4] for z in part),part[-1][5]))
    return out

def key(t,tf):
    d=datetime.fromtimestamp(t/1000,timezone.utc)
    if tf=="D1":return (d.year,d.month,d.day)
    if tf=="W1":return (d.isocalendar().year,d.isocalendar().week)
    return (d.year,d.month)

def periods(a,tf):
    out=[];old=None;cur=None
    for x in a:
        k=key(x[0],tf)
        if k!=old:
            if cur:out.append(tuple(cur))
            cur=[x[0],x[1],x[2],x[3],x[4],x[5]];old=k
        else:
            cur[1]=x[1];cur[3]=max(cur[3],x[3]);cur[4]=min(cur[4],x[4]);cur[5]=x[5]
    if cur:out.append(tuple(cur))
    return out[1:] # skip initial partial period; last is used only as next-period marker

def atrs(a):
    out=[];q=[];total=0.
    for i,x in enumerate(a):
        if i and x[0]!=a[i-1][1]:q=[];total=0.
        tr=x[3]-x[4] if not i or x[0]!=a[i-1][1] else max(x[3]-x[4],abs(x[3]-a[i-1][5]),abs(x[4]-a[i-1][5]))
        q.append(tr);total+=tr
        if len(q)>14:total-=q.pop(0)
        out.append(total/len(q))
    return out

def known_levels(a):
    ev=[]
    for tf,age in (("D1",7*24*H),("W1",35*24*H),("MN1",93*24*H)):
        p=periods(a,tf)
        for i,x in enumerate(p[:-1]):
            avail=p[i+1][0];o,hi,lo,c=x[2:]
            pivot=(hi+lo+c)/3
            for typ,price in (("high",hi),("low",lo),("close",c),("open",o),("pivot",pivot),("r1",2*pivot-lo),("s1",2*pivot-hi)):
                ev.append((avail,tf,typ,price,age))
    for tf,n,k,days in (("H1",60,3,12),("H4",240,2,35)):
        a2=resample(a,n)
        for i in range(k,len(a2)-k):
            if any(a2[j][1]!=a2[j+1][0] for j in range(i-k,i+k)):continue
            x=a2[i];avail=a2[i+k][1]
            if x[3]>max(a2[j][3] for j in range(i-k,i+k+1) if j!=i):
                ev.append((avail,tf,"high",x[3],days*24*H))
            if x[4]<min(a2[j][4] for j in range(i-k,i+k+1) if j!=i):
                ev.append((avail,tf,"low",x[4],days*24*H))
    return sorted(ev,key=lambda z:z[0])

def signals(m1,lev,start):
    out=[]
    for tf in (5,15):
        bars=resample(m1,tf);ats=atrs(bars);active=[];j=0
        for i in range(30,len(bars)-2):
            x=bars[i]
            if x[0]<start:continue
            while j<len(lev) and lev[j][0]<=x[0]:active.append(lev[j]);j+=1
            active=[z for z in active if x[0]-z[0]<=z[4]]
            if bars[i+1][0]!=x[1]:continue
            a=ats[i];body=abs(x[5]-x[2])
            if body<max(.1,.1*a):continue
            up=x[3]-max(x[2],x[5]);down=min(x[2],x[5])-x[4]
            for side,wick,opp,tip in (("sell",up,down,x[3]),("buy",down,up,x[4])):
                if wick<max(.3,.15*a) or wick<1.5*opp:continue
                ratio=wick/body;shape="equal" if .75<=ratio<=1.25 else "long" if ratio>=1.5 else None
                if not shape:continue
                tol=max(1.,min(4.,.15*a*math.sqrt(60/tf)))
                near=[z for z in active if abs(z[3]-tip)<=tol]
                frames={z[1] for z in near if z[1] in ("D1","W1","MN1")}
                ref=max(x[2],x[5]) if side=="sell" else min(x[2],x[5])
                kind="low" if side=="sell" else "high"
                opps=[z[3] for z in active if z[2]==kind and (z[3]<ref-.75 if side=="sell" else z[3]>ref+.75)]
                target=max(opps) if side=="sell" and opps else min(opps) if opps else None
                y=bars[i+1]
                cb=y[5]<min(x[2],x[5]) and y[5]<y[2] if side=="sell" else y[5]>max(x[2],x[5]) and y[5]>y[2]
                ce=y[5]<x[4] and y[5]<y[2] if side=="sell" else y[5]>x[3] and y[5]>y[2]
                out.append(dict(tf=tf,t=x[0],end=x[1],nextend=y[1],side=side,shape=shape,
                    x=x,atr=a,htf=any(z[1] in ("H1","H4") for z in near),
                    count=len(frames),monthly="MN1" in frames,nearest=target,body=cb,extreme=ce))
    return sorted(out,key=lambda z:(z["t"],z["tf"]))

def match(e,filter):
    return (filter=="all" or filter=="htf" and e["htf"] or
        filter=="dwm2" and e["count"]>=2 or filter=="dwm3" and e["count"]==3 or
        filter=="monthly" and e["monthly"] or filter=="away" and not e["htf"] and not e["count"])

def simulate(a,starts,e,confirm,entry_type,target_type):
    if confirm=="body" and not e["body"] or confirm=="extreme" and not e["extreme"]:
        return None,e["nextend"],None
    when=e["end"] if confirm=="none" else e["nextend"]
    i=bisect.bisect_left(starts,when)
    if i>=len(a) or a[i][0]!=when:return None,when,None
    x=e["x"];buy=e["side"]=="buy";stop=x[4]-max(.25,.12*e["atr"]) if buy else x[3]+max(.25,.12*e["atr"])
    if entry_type=="market":
        fill=i;price=a[i][2]+SPREAD+SLIP if buy else a[i][2]-SLIP;start=fill
    else:
        edge=min(x[2],x[5]) if buy else max(x[2],x[5])
        fill=None;prev=None
        while i<len(a) and a[i][0]<when+WAIT:
            b=a[i]
            if prev is not None and b[0]!=prev[1]:return None,b[0],None
            if b[4]<=stop if buy else b[3]>=stop:return None,b[1],None
            if b[4]<=edge if buy else b[3]>=edge:fill=i;break
            prev=b;i+=1
        if fill is None:return None,min(when+WAIT,a[-1][1]),None
        price=edge+SPREAD+SLIP if buy else edge-SLIP
        start=fill+1 # no favorable intrabar outcome credited on limit fill minute
    risk=price-(stop-SLIP) if buy else stop+SPREAD+SLIP-price
    if risk<.75 or risk>max(25.,3*e["atr"]):return None,a[fill][1],None
    if target_type=="next_level":
        target=e["nearest"]
        if target is None:return None,a[fill][1],None
        reward=target-price-SLIP if buy else price-target-SPREAD-SLIP
        if reward/risk<1.:return None,a[fill][1],None
    else:
        rr=1.5 if target_type=="rr1.5" else 2.
        target=price+rr*risk+SLIP if buy else price-rr*risk-SPREAD-SLIP
    end=a[fill][0]+HOLD
    prev=a[fill] if start>fill else None
    for j in range(start,len(a)):
        b=a[j]
        if b[0]>=end:break
        if prev is not None and b[0]!=prev[1]:return None,b[0],a[fill][0]
        stop_hit=b[4]<=stop if buy else b[3]>=stop
        target_hit=b[3]>=target if buy else b[4]<=target
        if stop_hit:return -1.,b[1],a[fill][0] # conservative stop-first
        if target_hit:
            gain=target-price-SLIP if buy else price-target-SPREAD-SLIP
            return gain/risk,b[1],a[fill][0]
        prev=b
    if prev is None or prev[1]<end:return None,a[-1][1],a[fill][0]
    pnl=prev[5]-price-SLIP if buy else price-prev[5]-SPREAD-SLIP
    return pnl/risk,prev[1],a[fill][0]

def metrics(rs):
    if not rs:return dict(n=0,mean_r=None,total_r=None,win=None,dd_r=None,pf=None)
    p=0.;peak=0.;dd=0.
    for r in rs:
        p+=r;peak=max(peak,p);dd=max(dd,peak-p)
    gains=sum(max(0,r) for r in rs);loss=-sum(min(0,r) for r in rs)
    return dict(n=len(rs),mean_r=round(statistics.mean(rs),4),total_r=round(sum(rs),3),
        win=round(sum(r>0 for r in rs)/len(rs),4),dd_r=round(dd,3),pf=round(gains/loss,3) if loss else None)

def main(src,dst):
    a=load(src)
    if len(a)<15000 or a[-1][0]-a[0][0]<110*24*H:raise SystemExit("Not enough valid M1 history")
    starts=[x[0] for x in a];begin=a[0][0]+35*24*H;end=a[-1][0]-HOLD
    train=begin+int(.7*(end-begin));valid=begin+int(.85*(end-begin))
    lev=known_levels(a);ev=signals(a,lev,begin)
    grouped=defaultdict(list)
    for x in ev:grouped[(x["tf"],x["shape"])].append(x)
    combos=list(itertools.product((5,15),("equal","long"),("none","body","extreme"),
        ("market","body_limit"),("all","htf","dwm2","dwm3","monthly","away"),
        ("rr1.5","rr2","next_level")))
    rows=[];cache={}
    for tf,shape,conf,entry,flt,target in combos:
        config=dict(tf=f"M{tf}",wick=shape,confirmation=conf,entry=entry,level=flt,target=target)
        data=defaultdict(list);occupied=-1;eligible=0;not_filled=0
        for e in grouped[(tf,shape)]:
            if e["t"]<=occupied or not match(e,flt):continue
            if conf=="body" and not e["body"] or conf=="extreme" and not e["extreme"]:continue
            if abs(e["t"]-train)<EMBARGO or abs(e["t"]-valid)<EMBARGO:continue
            period="train" if e["t"]<train else "validation" if e["t"]<valid else "test"
            eligible+=1;k=(tf,e["t"],e["side"],conf,entry,target)
            if k not in cache:cache[k]=simulate(a,starts,e,conf,entry,target)
            r,released,filled=cache[k];occupied=max(occupied,released)
            if filled is None:not_filled+=1
            if r is not None:data[period].append(r)
        s={p:metrics(data[p]) for p in ("train","validation","test")}
        t=s["train"];v=s["validation"]
        passed=t["n"]>=30 and v["n"]>=10 and t["mean_r"]>0 and v["mean_r"]>0
        score=round(min(t["mean_r"],v["mean_r"])*min(1,v["n"]/25)-.002*v["dd_r"],5) if passed else None
        rows.append(dict(config=config,eligible=eligible,unfilled_or_rejected=not_filled,
            passed=passed,score=score,**s))
    good=sorted((r for r in rows if r["passed"]),key=lambda r:r["score"],reverse=True)
    last=datetime.fromtimestamp(a[-1][0]/1000,timezone.utc).isoformat()
    out=dict(generated_utc=datetime.now(timezone.utc).isoformat(),source="Dukascopy XAUUSD M1 BID",
        first_utc=datetime.fromtimestamp(a[0][0]/1000,timezone.utc).isoformat(),
        last_utc=last,last_bid=a[-1][5],m1_bars=len(a),signals=len(ev),variants=len(combos),
        qualified=len(good),status="research_candidate_only" if good else "no_robust_candidate",
        split=dict(train_end=datetime.fromtimestamp(train/1000,timezone.utc).isoformat(),
            validation_end=datetime.fromtimestamp(valid/1000,timezone.utc).isoformat(),
            method="chronological 70/15/15 after 35d warmup; 24h embargo; no test data used for ranking"),
        assumptions=dict(spread=SPREAD,slippage_each_side=SLIP,max_hold_hours=8,
            pending_limit_minutes=60,one_trade_per_variant=True,
            target="1.5R, 2R, or nearest opposing pre-known historical high/low with RR>=1",
            stop="beyond signal wick by max($0.25,0.12 ATR)",
            ambiguous="M1 stop wins if stop and target both touched",
            level="previous completed D/W/M OHLC + pivots, confirmed H1/H4 swings",
            warning="Repeated retrospective test reviews are not prospective validation; costs are fixed estimates"),
        champion=good[0] if good else None,top10=good[:10])
    p=Path(dst);p.mkdir(parents=True,exist_ok=True)
    (p/"latest.json").write_text(json.dumps(out,ensure_ascii=False,indent=2)+"\n")
    fields=["tf","wick","confirmation","entry","level","target","eligible","unfilled_or_rejected","passed","score"]+[
        f"{period}_{k}" for period in ("train","validation","test") for k in ("n","mean_r","total_r","win","dd_r","pf")]
    with (p/"all_variants.csv").open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for r in rows:
            flat={**r["config"],**{k:r[k] for k in ("eligible","unfilled_or_rejected","passed","score")}}
            for period in ("train","validation","test"):
                flat.update({f"{period}_{k}":v for k,v in r[period].items()})
            w.writerow(flat)
    md=["# الباحث الآلي للذهب — AutoResearch","",
        f"آخر شمعة بيانات: **{last} UTC**، آخر سعر BID: **{a[-1][5]}**.",
        f"اختُبرت **{len(combos)}** نسخة قواعدية، واجتازت **{len(good)}** نسخة شروط التدريب والتحقق.","",
        "**هذا بحث آلي يجرب تركيبات قواعد، وليس وكيلاً لغوياً يكتب استراتيجيات جديدة من تلقاء نفسه.**",
        "لا يعدل alert.py ولا ينفذ صفقات ولا يغير إشارات البوت.","",
        "## أفضل مرشح بحثي"]
    if good:
        b=good[0];md += [json.dumps(b["config"],ensure_ascii=False),
            f"التدريب: {b['train']}",f"التحقق: {b['validation']}",
            f"الاختبار الزمني المنفصل (لم يستخدم للترتيب): {b['test']}"]
    else:md += ["**لا يوجد مرشح اجتاز شروط المتانة. لا تُعتمد أي استراتيجية.**"]
    md += ["","## المنهجية والحدود",
        "- 432 تركيبة: M5/M15، ذيل مقارب للجسم أو أطول، 3 شروط تأكيد، طريقتا دخول، 6 مرشحات مستويات، 3 أهداف.",
        "- الدعم والمقاومة من شموع مكتملة أو قمم/قيعان مؤكدة مسبقاً، دون استخدام مستويات مستقبلية.",
        "- الوقف خلف طرف الذيل، والأهداف 1.5R أو 2R أو أقرب مستوى فني معاكس معروف مسبقاً.",
        "- سعر BID، سبريد افتراضي 0.30 دولار وانزلاق 0.10 دولار لكل جهة؛ إغلاق بعد 8 ساعات إذا لم يصل الهدف أو الوقف.",
        "- تداول افتراضي واحد مفتوح أو معلق لكل نسخة؛ عند غموض حركة الدقيقة يحسب الوقف أولاً.",
        "- اختيار المرشح بالتدريب والتحقق فقط؛ الاختبار الأخير للتقييم وليس للاختيار.",
        "- تكرار فحص فترة الاختبار يومياً يجعلها غير مستقلة تماماً؛ يلزم اختبار ورقي مستقبلي قبل أي استخدام.",
        "- لا يوجد وكيل ذكاء اصطناعي خارجي ولا مفاتيح API مدفوعة.",
        "","جميع النسخ ونتائجها في all_variants.csv، والنتيجة الآلية في latest.json."]
    (p/"README.md").write_text("\n".join(md)+"\n")
    print("DONE",last,"signals",len(ev),"variants",len(combos),"qualified",len(good),flush=True)

if __name__=="__main__":main(sys.argv[1],sys.argv[2])
