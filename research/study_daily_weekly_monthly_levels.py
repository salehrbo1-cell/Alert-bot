#!/usr/bin/env python3
"""Daily/weekly/monthly XAUUSD overlap as of latest available M1 BID."""
import csv,json,sys,statistics
from collections import defaultdict
from datetime import datetime,timezone
from pathlib import Path
import study_sweep_poi_ifvg_cisd as b
def dt(t):return datetime.fromtimestamp(t/1000,timezone.utc)
def aggregate(a,fn):
    groups={}
    for x in a:
        k=fn(dt(x[0]))
        if k not in groups:groups[k]=[x[0],x[1],x[2],x[3],x[4],x[5]]
        else:
            y=groups[k];y[1]=x[1];y[3]=max(y[3],x[3]);y[4]=min(y[4],x[4]);y[5]=x[5]
    return [(k,*v) for k,v in sorted(groups.items())]
def pvt(bar,tf,tag):
    k,ts,te,o,h,l,c=bar;p=(h+l+c)/3;r1=2*p-l;s1=2*p-h;r2=p+(h-l);s2=p-(h-l)
    return [dict(tf=tf,price=round(v,3),label=f"{tag} pivot {label}",date=str(k))
            for label,v in (("P",p),("R1",r1),("S1",s1),("R2",r2),("S2",s2))]
def val(bar,tf,tag):
    k,ts,te,o,h,l,c=bar
    return [dict(tf=tf,price=round(v,3),label=f"{tag} {name}",date=str(k))
            for name,v in (("high",h),("low",l),("close",c),("open",o))]
def swings(a,tf,k):
    out=[]
    for i in range(k,len(a)-k):
        bar=a[i]
        if bar[4]>max(a[j][4] for j in range(i-k,i+k+1) if j!=i):
            out.append(dict(tf=tf,price=round(bar[4],3),label="confirmed swing high",date=str(bar[0]),confirmed_on=str(a[i+k][0])))
        if bar[5]<min(a[j][5] for j in range(i-k,i+k+1) if j!=i):
            out.append(dict(tf=tf,price=round(bar[5],3),label="confirmed swing low",date=str(bar[0]),confirmed_on=str(a[i+k][0])))
    return out
def main(csvpath,outpath):
    a=b.read(csvpath);last=a[-1];d=dt(last[0]);price=last[5]
    days=aggregate(a,lambda z:z.strftime("%Y-%m-%d"))
    weeks=aggregate(a,lambda z:(z.isocalendar().year,z.isocalendar().week))
    months=aggregate(a,lambda z:z.strftime("%Y-%m"))
    current_day=d.strftime("%Y-%m-%d")
    current_week=(d.isocalendar().year,d.isocalendar().week)
    current_month=d.strftime("%Y-%m")
    dc=[x for x in days if x[0]<current_day];wc=[x for x in weeks if x[0]<current_week];mc=[x for x in months if x[0]<current_month]
    # daily prev 20 sessions, weekly prev 12 weeks, monthly prev 15 months
    levels=[]
    for tf,seq,n,sw in (("D1",dc,20,2),("W1",wc,12,1),("MN1",mc,15,1)):
        for j,x in enumerate(seq[-n:][::-1]):
            if j<7 if tf=="D1" else j<5 if tf=="W1" else j<6:
                levels+=val(x,tf,"prior" if j==0 else f"lookback_{j+1}")
            else:
                for name,v in (("high",x[4]),("low",x[5]),("close",x[6])):
                    levels.append(dict(tf=tf,price=round(v,3),label=f"lookback_{j+1} {name}",date=str(x[0])))
        for j,x in enumerate(seq[-(5 if tf=="D1" else 4 if tf=="W1" else 3):][::-1]):
            levels+=pvt(x,tf,"latest" if j==0 else f"lookback_{j+1}")
        levels+=swings(seq[-(80 if tf=="D1" else 52 if tf=="W1" else 24):],tf,sw)
    # near price +/- 180, only actual prior completed candle values
    near=[x for x in levels if abs(x["price"]-price)<=180]
    # Confluence as pairs within 8 USD (levels from distinct TF). Group candidates anchored on a monthly, weekly, daily level.
    # greedy connected components can chain too far; generate windows with total range <=8.
    unique={}
    for x in near:
        if x["tf"] not in ("D1","W1","MN1"):continue
        g=[y for y in near if abs(y["price"]-x["price"])<=4.0]
        if len({z["tf"] for z in g})<2:continue
        # refine span to 8 USD around x; keep closest 3 per timeframe to avoid noise.
        selected=[]
        for tf in ("D1","W1","MN1"):
            opts=sorted((z for z in g if z["tf"]==tf),key=lambda z:(abs(z["price"]-x["price"]),0 if z["label"].startswith("prior") or z["label"].startswith("latest") else 1))
            selected+=opts[:3]
        types=sorted(set(z["tf"] for z in selected))
        if len(types)<2:continue
        mid=round(statistics.median(z["price"] for z in selected),2)
        key=round(mid/3)*3
        if key not in unique or (len(types),-abs(mid-price),-len(selected))>(len(unique[key]["frames"]),-abs(unique[key]["center"]-price),-len(unique[key]["sources"])):
            unique[key]=dict(center=mid,range=[round(min(z["price"] for z in selected),3),round(max(z["price"] for z in selected),3)],
                frames=types,sources=selected)
    clusters=sorted(unique.values(),key=lambda x:(-len(x["frames"]),abs(x["center"]-price)))
    # remove overlapping clusters within 4.5 USD
    clean=[]
    for x in clusters:
        if any(abs(x["center"]-y["center"])<4.5 and set(x["frames"]).issubset(set(y["frames"])) for y in clean):continue
        clean.append(x)
    # classify using last price, don't assume current support if broken
    for x in clean:
        x["side"]="above_price_resistance_candidate" if x["center"]>price+2 else "below_price_support_candidate" if x["center"]<price-2 else "current_decision_zone"
        x["distance"]=round(x["center"]-price,2)
    recent=dict(D1=[dict(date=str(x[0]),open=x[3],high=x[4],low=x[5],close=x[6]) for x in dc[-5:]],
        W1=[dict(date=str(x[0]),open=x[3],high=x[4],low=x[5],close=x[6]) for x in wc[-4:]],
        MN1=[dict(date=str(x[0]),open=x[3],high=x[4],low=x[5],close=x[6]) for x in mc[-5:]])
    current=dict(D1=[dict(date=str(x[0]),open=x[3],high=x[4],low=x[5],close=x[6]) for x in days[-1:]],
        W1=[dict(date=str(x[0]),open=x[3],high=x[4],low=x[5],close=x[6]) for x in weeks[-1:]],
        MN1=[dict(date=str(x[0]),open=x[3],high=x[4],low=x[5],close=x[6]) for x in months[-1:]])
    pvs={tf:[x for x in pvt(seq[-1],tf,"previous")] for tf,seq in (("D1",dc),("W1",wc),("MN1",mc))}
    result=dict(source="Dukascopy XAUUSD M1 BID; not executable broker price",last_utc=b.iso(last[0]),last_bid=price,
        total_m1=len(a),day_boundary="UTC 00:00",week_boundary="Monday UTC",month_boundary="calendar month UTC",
        confluence_threshold="within $4 of a candidate; total span up to $8",
        pivots=pvs,recent=recent,current=current,confluences=clean,levels_near_price=sorted(near,key=lambda x:x["price"]))
    p=Path(outpath);p.mkdir(parents=True,exist_ok=True)
    (p/"summary.json").write_text(json.dumps(result,indent=2))
    print("LAST",b.iso(last[0]),price,flush=True)
    print("PIVOTS",json.dumps(pvs),flush=True)
    print("RECENT",json.dumps(recent),flush=True)
    print("CONFLUENCES",len(clean),flush=True)
    for x in clean[:35]:
        print(json.dumps(x),flush=True)
if __name__=="__main__":main(sys.argv[1],sys.argv[2])
