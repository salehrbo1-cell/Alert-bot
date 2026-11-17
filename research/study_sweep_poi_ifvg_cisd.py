#!/usr/bin/env python3
"""Research only: user's FOUR ordered conditions. All numeric definitions are
explicit provisional assumptions, not necessarily the original analyst's.
HTF H1/H4 wick sweep -> independent HTF POI -> M15 IFVG -> M5 CISD.
Signals on closed candles; entry on next M1 open; no live bot changes.
"""
import bisect,csv,json,math,statistics,sys
from datetime import datetime,timezone
from collections import Counter
from pathlib import Path
M=60000;H=60*M
def ts(s):return int(datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()*1000)
START=ts('2026-09-17T09:00:00Z')
SPLIT=ts('2026-10-17T09:00:00Z')
END=ts('2026-11-17T09:00:00Z')
def iso(t):return datetime.fromtimestamp(t/1000,timezone.utc).isoformat()
def read(path):
    rows={}
    with open(path,newline='') as f:
        for r in csv.DictReader(f):
            try:
                if 'timestamp' in r:
                    t=int(float(r['timestamp']));o,h,l,c=(float(r[k]) for k in ('open','high','low','close'))
                else:
                    t=ts(r['Etc/UTC']);o,h,l,c=(float(r[k]) for k in ('Open','High','Low','Close'))
                if all(map(math.isfinite,(o,h,l,c))) and h>=max(o,c) and l<=min(o,c):
                    rows[t]=(t,t+M,o,h,l,c)
            except (ValueError,KeyError,TypeError):pass
    return [rows[t] for t in sorted(rows)]
# tuple bar: start,end,open,high,low,close
def resample(rows,n):
    step=n*M;result=[];part=[];bucket=None
    for b in rows:
        x=(b[0]//step)*step
        if bucket is not None and x!=bucket:
            if len(part)==n and part[0][0]==bucket and part[-1][1]==bucket+step:
                result.append((bucket,bucket+step,part[0][2],max(a[3] for a in part),
                    min(a[4] for a in part),part[-1][5]))
            part=[]
        bucket=x;part.append(b)
    if part and len(part)==n and part[0][0]==bucket and part[-1][1]==bucket+step:
        result.append((bucket,bucket+step,part[0][2],max(a[3] for a in part),
                    min(a[4] for a in part),part[-1][5]))
    return result
def atr(bars,i):
    if i<14:return 0
    return sum(max(bars[j][3]-bars[j][4],abs(bars[j][3]-bars[j-1][5]),
        abs(bars[j][4]-bars[j-1][5])) for j in range(i-13,i+1))/14
def zone_dist(x,z):return max(z['lo']-x,x-z['hi'],0)
def build_htf(h1,h4):
    pivots=[];zones=[]
    for tf,arr in [('H1',h1),('H4',h4)]:
        k=3 if tf=='H1' else 2
        life=(5 if tf=='H1' else 18)*24*H
        for i in range(20,len(arr)):
            b=arr[i];a=atr(arr,i)
            if a<=0:continue
            p=i-k
            if p>=k:
                candidate=arr[p];left=arr[p-k:p];right=arr[p+1:i+1]
                if all(candidate[3]>x[3] for x in left+right) and candidate[3]-max(x[3] for x in left)>=.2*a:
                    pivots.append(dict(id=f'{tf}-high-{p}',tf=tf,side='sell',price=candidate[3],avail=b[1],atr=a))
                if all(candidate[4]<x[4] for x in left+right) and min(x[4] for x in left)-candidate[4]>=.2*a:
                    pivots.append(dict(id=f'{tf}-low-{p}',tf=tf,side='buy',price=candidate[4],avail=b[1],atr=a))
            if i>=2 and arr[i-2][1]==arr[i-1][0] and arr[i-1][1]==b[0]:
                old=arr[i-2];g=max(.25,.04*a)
                if b[4]>old[3]+g:
                    zones.append(dict(id=f'{tf}-fvg-buy-{i}',tf=tf,kind='FVG',side='buy',lo=old[3],hi=b[4],avail=b[1],expire=b[1]+life,atr=a))
                if b[3]<old[4]-g:
                    zones.append(dict(id=f'{tf}-fvg-sell-{i}',tf=tf,kind='FVG',side='sell',lo=b[3],hi=old[4],avail=b[1],expire=b[1]+life,atr=a))
            if i>=12 and arr[i-1][1]==b[0]:
                prev=arr[i-1]
                med=max(.1,statistics.median(abs(x[5]-x[2]) for x in arr[i-11:i-1]))
                if b[5]>b[2] and prev[5]<prev[2] and b[5]>max(x[3] for x in arr[i-6:i]) and b[5]-b[2]>=1.5*med and prev[3]-prev[4]<=1.7*a:
                    zones.append(dict(id=f'{tf}-ob-buy-{i}',tf=tf,kind='OB',side='buy',lo=prev[4],hi=prev[3],avail=b[1],expire=b[1]+life,atr=a))
                if b[5]<b[2] and prev[5]>prev[2] and b[5]<min(x[4] for x in arr[i-6:i]) and b[2]-b[5]>=1.5*med and prev[3]-prev[4]<=1.7*a:
                    zones.append(dict(id=f'{tf}-ob-sell-{i}',tf=tf,kind='OB',side='sell',lo=prev[4],hi=prev[3],avail=b[1],expire=b[1]+life,atr=a))
        for p in [x for x in pivots if x['tf']==tf]:
            w=max(.35,.12*p['atr'])
            zones.append(dict(id=p['id'],tf=tf,kind='SR',side=p['side'],
                lo=p['price']-w,hi=p['price']+w,avail=p['avail'],expire=p['avail']+life,atr=p['atr']))
    # Precompute invalidation times; when evaluating at time t, only invalidate if invalidation <=t.
    for z in zones:
        arr=h1 if z['tf']=='H1' else h4
        z['invalid']=10**18
        for b in arr:
            if b[1]<=z['avail']:continue
            if b[1]>z['expire']:break
            if (z['side']=='sell' and b[5]>z['hi']+.12*z['atr']) or (z['side']=='buy' and b[5]<z['lo']-.12*z['atr']):
                z['invalid']=b[1];break
    return pivots,zones
def sweeps(h1,h4,pivots):
    result=[]
    for tf,arr in [('H1',h1),('H4',h4)]:
        ps=[p for p in pivots if p['tf']==tf]
        for b in arr:
            if b[1]<START or b[1]>END:continue
            eligible=[p for p in ps if p['avail']<=b[0] and b[0]-p['avail']<10*24*H]
            highs=[p for p in eligible if p['side']=='sell' and b[2]<p['price'] and b[5]<p['price'] and b[3]>p['price']+max(.15,.02*p['atr'])]
            lows=[p for p in eligible if p['side']=='buy' and b[2]>p['price'] and b[5]>p['price'] and b[4]<p['price']-max(.15,.02*p['atr'])]
            for side,arr2,ext in [('sell',highs,b[3]),('buy',lows,b[4])]:
                if arr2:
                    p=(max(arr2,key=lambda x:x['price']) if side=='sell' else min(arr2,key=lambda x:x['price']))
                    result.append(dict(time=b[1],side=side,tf=tf,level=p['price'],extreme=ext,pivot=p,atr=p['atr']))
    return sorted(result,key=lambda x:x['time'])
def poi_after(s,m15,zones):
    valid=[z for z in zones if z['side']==s['side'] and z['id']!=s['pivot']['id']
        and z['avail']<=s['time']<min(z['expire'],z['invalid'])
        and zone_dist(s['extreme'],z)<=max(2.5,.45*s['atr'])
        and not(z['kind']=='SR' and abs((z['lo']+z['hi'])/2-s['level'])<.35)]
    if not valid:return None
    end=s['time']+(8 if s['tf']=='H1' else 16)*H
    for b in m15:
        if b[1]<=s['time']:continue
        if b[1]>end:break
        found=[z for z in valid if b[1]<min(z['expire'],z['invalid']) and b[3]>=z['lo'] and b[4]<=z['hi']]
        if found:
            priority={'OB':0,'FVG':1,'SR':2}
            z=min(found,key=lambda z:(0 if z['tf']=='H4' else 1,priority[z['kind']],zone_dist(b[5],z)))
            return dict(time=b[1],zone=z)
    return None
def m15_gaps(arr):
    gaps=[]
    for i in range(2,len(arr)):
        a,b,c=arr[i-2:i+1]
        if a[1]!=b[0] or b[1]!=c[0]:continue
        avg=sum(x[3]-x[4] for x in arr[max(0,i-20):i])/min(20,i)
        gap=max(.25,.06*avg)
        if c[4]>a[3]+gap:gaps.append(dict(i=i,time=c[1],side='sell',lo=a[3],hi=c[4]))
        if c[3]<a[4]-gap:gaps.append(dict(i=i,time=c[1],side='buy',lo=c[3],hi=a[4]))
    return gaps
def inverted(s,poi,m15,gaps):
    z=poi['zone']
    for f in gaps:
        if f['side']!=s['side'] or not(poi['time']<f['time']<=poi['time']+6*H):continue
        if f['lo']>z['hi'] or f['hi']<z['lo']:continue
        for j in range(f['i']+1,min(f['i']+17,len(m15))):
            b=m15[j];p=m15[j-1]
            if b[1]>f['time']+4*H:break
            if p[1]!=b[0]:break
            if not(f['lo']<=b[5]<=f['hi']):continue
            if s['side']=='sell' and b[5]<b[2] and p[5]>f['hi']:return dict(time=b[1],gap=f)
            if s['side']=='buy' and b[5]>b[2] and p[5]<f['lo']:return dict(time=b[1],gap=f)
    return None
def cisd_after(s,inv,m5):
    times=[b[1] for b in m5]
    start=bisect.bisect_right(times,inv['time']);f=inv['gap']
    for j in range(start,min(start+18,len(m5))):
        if j<24:continue
        b=m5[j]
        if b[1]>inv['time']+90*M:break
        last=m5[j-2:j+1];prior=m5[j-22:j-2]
        if any(x[1]!=y[0] for x,y in zip(last,last[1:])):continue
        if any(x[1]!=y[0] for x,y in zip(prior,prior[1:])):continue
        med=max(.1,statistics.median(abs(x[5]-x[2]) for x in prior))
        avg=sum(x[3]-x[4] for x in prior)/20
        if max(f['lo']-b[5],b[5]-f['hi'],0)>max(2.,avg):continue
        if s['side']=='sell':
            count=sum(x[5]<x[2] for x in last)
            valid=b[5]<b[2] and b[5]<min(x[4] for x in m5[j-8:j-2]) and last[0][2]-b[5]>=1.2*avg
        else:
            count=sum(x[5]>x[2] for x in last)
            valid=b[5]>b[2] and b[5]>max(x[3] for x in m5[j-8:j-2]) and b[5]-last[0][2]>=1.2*avg
        if count>=2 and abs(b[5]-b[2])>=1.25*med and valid:return b[1]
    return None
def exit_trade(m1,starts,at,side,tp,sl,minutes,cost):
    i=bisect.bisect_left(starts,at)
    if i==len(m1) or m1[i][0]!=at:return dict(status='no_entry',pnl=None)
    entry=m1[i][2];sign=1 if side=='buy' else -1;prev=None;mae=mfe=0
    for b in m1[i:i+minutes]:
        if prev is not None and b[0]!=prev[1]:
            return dict(status='gap',pnl=round((prev[5]-entry)*sign-cost,3),mfe=round(mfe,3),mae=round(mae,3))
        fav=b[3]-entry if sign==1 else entry-b[4]
        adv=entry-b[4] if sign==1 else b[3]-entry
        mfe=max(mfe,fav);mae=max(mae,adv)
        if adv+cost>=sl:return dict(status='stop',pnl=-sl,mfe=round(mfe,3),mae=round(mae,3))
        if fav-cost>=tp:return dict(status='target',pnl=tp,mfe=round(mfe,3),mae=round(mae,3))
        prev=b
    if prev is None:return dict(status='no_entry',pnl=None)
    return dict(status='timeout',pnl=round((prev[5]-entry)*sign-cost,3),mfe=round(mfe,3),mae=round(mae,3))
def score(trades,key):
    x=[t['outcomes'][key] for t in trades if t['outcomes'][key]['pnl'] is not None]
    n=len(x);wins=sum(z['status']=='target' for z in x)
    pnl=[z['pnl'] for z in x];pos=sum(max(0,p) for p in pnl);neg=-sum(min(0,p) for p in pnl)
    eq=peak=dd=0
    for p in pnl:eq+=p;peak=max(peak,eq);dd=max(dd,peak-eq)
    return dict(n=n,target_hits=wins,target_hit_rate=round(wins/n,4) if n else None,
        stops=sum(z['status']=='stop' for z in x),timeouts=sum(z['status']=='timeout' for z in x),
        net_price_dollars=round(sum(pnl),2),avg_price_dollars=round(sum(pnl)/n,3) if n else None,
        profit_factor=round(pos/neg,3) if neg else None,max_drawdown_price_dollars=round(dd,2))
def run(path,out):
    m1=read(path);m5=resample(m1,5);m15=resample(m1,15)
    h1=resample(m1,60);h4=resample(m1,240)
    pivots,zones=build_htf(h1,h4);ss=sweeps(h1,h4,pivots);gaps=m15_gaps(m15)
    counts=Counter();trades=[];last={};starts=[b[0] for b in m1]
    for s in ss:
        counts['sweep']+=1
        p=poi_after(s,m15,zones)
        if p is None:continue
        counts['poi']+=1
        inv=inverted(s,p,m15,gaps)
        if inv is None:continue
        counts['ifvg']+=1
        c=cisd_after(s,inv,m5)
        if c is None:continue
        counts['cisd']+=1
        if c>END-120*M or c-last.get(s['side'],-10**18)<4*H:
            counts['not_traded_cooldown_or_horizon']+=1;continue
        last[s['side']]=c
        outcomes={}
        for spread in (.30,.60):
            for tp,sl,mins in ((5,3,30),(10,5,60),(15,5,60),(20,5,120)):
                outcomes[f't{tp}_s{sl}_m{mins}_sp{spread:.2f}']=exit_trade(m1,starts,c,s['side'],tp,sl,mins,spread+.20)
        trades.append(dict(sweep=iso(s['time']),sweep_tf=s['tf'],side=s['side'],
            sweep_level=round(s['level'],3),poi=iso(p['time']),poi_tf=p['zone']['tf'],
            poi_kind=p['zone']['kind'],poi_lo=round(p['zone']['lo'],3),poi_hi=round(p['zone']['hi'],3),
            fvg=iso(inv['gap']['time']),ifvg=iso(inv['time']),cisd=iso(c),
            entry_bid=next((b[2] for b in m1 if b[0]==c),None),
            period='first_month' if c<SPLIT else 'second_month',outcomes=outcomes))
    keys=['t5_s3_m30_sp0.30','t10_s5_m60_sp0.30','t15_s5_m60_sp0.30','t20_s5_m120_sp0.30','t10_s5_m60_sp0.60']
    stats={period:{k:score([t for t in trades if period=='all' or t['period']==period],k) for k in keys}
        for period in ('all','first_month','second_month')}
    summary=dict(strategy='HTF wick sweep -> HTF independent POI -> M15 IFVG -> M5 CISD',
        start_utc=iso(START),end_utc=iso(END),data=dict(m1=len(m1),m5=len(m5),m15=len(m15),
        h1=len(h1),h4=len(h4),pivots=len(pivots),htf_poi=len(zones),m15_fvgs=len(gaps),
        first_utc=iso(m1[0][0]),last_utc=iso(m1[-1][0])),
        stage_counts=dict(counts),trades=len(trades),stats=stats,
        assumptions=dict(HTF='H1/H4 confirmed swing pivots; H1 3 right bars/H4 2; wick only, open and close inside',
        POI='Pre-existing independent HTF OB/FVG/SR, within max(2.5 USD,0.45 ATR) of sweep, later M15 touch',
        OB='Last opposite HTF candle before displacement beyond previous six bars; provisional proxy',
        FVG='HTF 3-bar gap; M15 3-bar gap formed after POI touch, overlapping POI',
        IFVG='M15 opposite-color close INSIDE original FVG, with prior close outside original side',
        CISD='M5: 2 of 3 candles in reversal direction, strong body >=1.25 median20, break preceding internal 6-bar extreme, near IFVG',
        entry='Next M1 open after closed M5 CISD; same-side cooldown 4 hours',
        risk='TP 5/10/15/20 USD, SL 3/5 USD, horizons 30/60/120 minutes; assumed spread 0.30/0.60 and slippage 0.10 per side'),
        caveats=['Numeric definitions are assumptions, not specified by analyst.','No order-book liquidity; BID OHLC only.',
        'Target and stop touched within same minute counted as stop.','Spread/slippage estimates not actual broker quotes.',
        'If very few trades, strict interpretation may be too narrow; not evidence of strategy failure.',
        'Results research only; alert.py untouched.'])
    dest=Path(out);dest.mkdir(parents=True,exist_ok=True)
    (dest/'summary.json').write_text(json.dumps(summary,indent=2))
    (dest/'trades.json').write_text(json.dumps(trades,indent=2))
    print(json.dumps(dict(stages=summary['stage_counts'],trades=len(trades),stats=stats),indent=2))
if __name__=='__main__':run(sys.argv[1],sys.argv[2])
