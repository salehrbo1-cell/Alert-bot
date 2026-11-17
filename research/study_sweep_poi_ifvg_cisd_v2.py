#!/usr/bin/env python3
"""Research V2: user's clarified strategy, NOT a live-trading signal bot.

HTF wick sweep -> pre-existing independent HTF POI -> M15 IFVG -> M15 CISD.
CISD is a displacement CLOSE beyond the LOW of the FIRST bullish M15 candle
in the run that produced the sweep (vice versa for buy).
Stop: beyond post-IFVG extreme; target: nearest untouched confirmed opposing
M15/H1/H4 swing. Entry next M1 open after M15 close.
Unknowns are explicit assumptions, not facts from the video.
"""
import bisect,json,math,statistics,sys
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
import study_sweep_poi_ifvg_cisd as b

M=b.M;H=b.H
def pivot15(arr):
    out=[];k=3
    for i in range(20,len(arr)-k):
        c=arr[i];confirm=arr[i+k]
        if confirm[1]>b.END:break
        before=arr[i-k:i];after=arr[i+1:i+k+1]
        if not all(x[1]==y[0] for x,y in zip(arr[i-k:i+k+1],arr[i-k+1:i+k+1])):continue
        a=b.atr(arr,i)
        if a<=0:continue
        if all(c[4]<x[4] for x in before+after) and min(x[4] for x in before)-c[4]>=.2*a:
            out.append(dict(tf='M15',side='sell_target',price=c[4],avail=confirm[1],id=f'M15-low-{i}'))
        if all(c[3]>x[3] for x in before+after) and c[3]-max(x[3] for x in before)>=.2*a:
            out.append(dict(tf='M15',side='buy_target',price=c[3],avail=confirm[1],id=f'M15-high-{i}'))
    return out

def origin_of_sweep(s,m15,ends):
    """First same-direction candle of contiguous M15 run nearest sweep extreme.
    The HTF sweep bar itself must be closed before any downstream signal."""
    length=H if s['tf']=='H1' else 4*H
    lo=bisect.bisect_right(ends,s['time']-length)
    hi=bisect.bisect_right(ends,s['time'])
    candidates=[(i,m15[i]) for i in range(lo,hi) if m15[i][1]<=s['time']]
    if not candidates:return None
    extreme_i=(max(candidates,key=lambda z:z[1][3])[0] if s['side']=='sell'
        else min(candidates,key=lambda z:z[1][4])[0])
    bullish=s['side']=='sell'
    def aligned(c):return c[5]>c[2] if bullish else c[5]<c[2]
    last=next((j for j in range(extreme_i,max(-1,extreme_i-3),-1) if j>=lo and aligned(m15[j])),None)
    if last is None:return None
    first=last
    while first>max(0,last-9) and m15[first-1][1]==m15[first][0] and aligned(m15[first-1]):
        first-=1
    if last-first+1<2:return None
    return dict(time=m15[first][1],first=first,last=last,
        price=m15[first][4] if bullish else m15[first][3],
        count=last-first+1,extreme_time=m15[extreme_i][1])

def find_ifvg(s,poi,m15,gaps,hold):
    z=poi['zone'];events=[]
    for f in gaps:
        if f['side']!=s['side'] or not(poi['time']<f['time']<=poi['time']+6*H):continue
        if f['lo']>z['hi'] or f['hi']<z['lo']:continue
        for j in range(f['i']+1,min(f['i']+17,len(m15))):
            c=m15[j];p=m15[j-1]
            if c[1]>f['time']+4*H:break
            if p[1]!=c[0]:break
            if not f['lo']<=c[5]<=f['hi']:continue
            if s['side']=='sell':
                valid=c[5]<c[2] and p[5]>f['hi']
            else:
                valid=c[5]>c[2] and p[5]<f['lo']
            if not valid:continue
            conf=c[1]
            if hold:
                if j+1>=len(m15) or m15[j+1][0]!=c[1]:continue
                nextbar=m15[j+1]
                # One closed bar holds on the inverted side; future indefinite
                # stability cannot be required without look-ahead bias.
                if s['side']=='sell' and nextbar[5]>f['hi']:continue
                if s['side']=='buy' and nextbar[5]<f['lo']:continue
                conf=nextbar[1]
            if conf>b.END:continue
            events.append(dict(time=c[1],confirm=conf,gap=f,idx=j))
    return min(events,key=lambda x:(x['confirm'],x['time'])) if events else None

def cisd(s,inv,origin,m15,ends):
    # CISD on M15, and close must cross the FIRST candle's origin.
    start=bisect.bisect_left(ends,inv['confirm'])
    f=inv['gap'];price=origin['price']
    for i in range(start,min(start+17,len(m15))):
        if i<21:continue
        c=m15[i];p=m15[i-1]
        if c[1]>inv['confirm']+4*H or c[1]>b.END:break
        if p[1]!=c[0]:continue
        history=m15[i-20:i]
        if any(x[1]!=y[0] for x,y in zip(history,history[1:])):continue
        med=max(.1,statistics.median(abs(x[5]-x[2]) for x in history))
        body=abs(c[5]-c[2]);span=max(.01,c[3]-c[4])
        if body<1.2*med or body/span<.60:continue
        # Require IFVG proximity in the current or last 3 M15 bars.
        near=any(x[3]>=f['lo'] and x[4]<=f['hi'] for x in m15[max(inv['idx'],i-3):i+1])
        if not near:continue
        if s['side']=='sell':
            valid=c[5]<c[2] and p[5]>=price and c[5]<price-.1
        else:
            valid=c[5]>c[2] and p[5]<=price and c[5]>price+.1
        if valid:return dict(time=c[1],idx=i,close=c[5],body=body,origin=price)
    return None

def nearest_target(s,entry_bid,at,pivots,m15,ends,spread):
    # Target is nearest PREVIOUSLY confirmed, UNSWEPT swing on the other side.
    # No hindsight: availability and consumption both checked as of 'at'.
    target_side='sell_target' if s['side']=='sell' else 'buy_target'
    i=bisect.bisect_right(ends,at)
    choices=[]
    for p in pivots:
        if p['side']!=target_side or p['avail']>=at:continue
        if s['side']=='sell' and p['price']>=entry_bid-1:continue
        if s['side']=='buy' and p['price']<=entry_bid+spread+1:continue
        k=bisect.bisect_right(ends,p['avail'])
        # Price must not have already consumed the liquidity since it became known.
        if s['side']=='sell' and any(x[4]<=p['price'] for x in m15[k:i]):continue
        if s['side']=='buy' and any(x[3]>=p['price'] for x in m15[k:i]):continue
        choices.append(p)
    if not choices:return None
    return max(choices,key=lambda p:p['price']) if s['side']=='sell' else min(choices,key=lambda p:p['price'])

def execute(m1,starts,at,side,stop_bid,target_bid,spread=.30,slippage=.10,hours=12):
    i=bisect.bisect_left(starts,at)
    if i>=len(m1) or m1[i][0]!=at:return dict(status='no_entry',pnl=None)
    if m1[i][0]+hours*H>min(b.END,m1[-1][1]):return dict(status='censored',pnl=None)
    entry_bid=m1[i][2];buy=side=='buy'
    # Buy at ASK, sell at BID. Exit buy at BID, exit sell at ASK.
    entry=entry_bid+spread if buy else entry_bid
    stop=stop_bid if buy else stop_bid+spread
    target=target_bid if buy else target_bid+spread
    risk=(entry-stop if buy else stop-entry)+2*slippage
    reward=(target-entry if buy else entry-target)-2*slippage
    if risk<=0 or reward<=0:return dict(status='invalid_rr',pnl=None,risk=round(risk,3),reward=round(reward,3))
    mfe=mae=0.;prev=None
    for x in m1[i:i+hours*60]:
        if prev is not None and x[0]!=prev[1]:
            pnl=(prev[5]-entry if buy else entry-(prev[5]+spread))-2*slippage
            return dict(status='gap',pnl=round(pnl,3),risk=round(risk,3),reward=round(reward,3),mfe=round(mfe,3),mae=round(mae,3))
        high=x[3] if buy else x[3]+spread
        low=x[4] if buy else x[4]+spread
        fav=high-entry if buy else entry-low
        adv=entry-low if buy else high-entry
        mfe=max(mfe,fav);mae=max(mae,adv)
        stop_hit=(low<=stop if buy else high>=stop)
        target_hit=(high>=target if buy else low<=target)
        if stop_hit:return dict(status='stop',pnl=round(-risk,3),risk=round(risk,3),reward=round(reward,3),mfe=round(mfe,3),mae=round(mae,3))
        if target_hit:return dict(status='target',pnl=round(reward,3),risk=round(risk,3),reward=round(reward,3),mfe=round(mfe,3),mae=round(mae,3))
        prev=x
    if prev is None:return dict(status='no_entry',pnl=None)
    pnl=(prev[5]-entry if buy else entry-(prev[5]+spread))-2*slippage
    return dict(status='timeout',pnl=round(pnl,3),risk=round(risk,3),reward=round(reward,3),mfe=round(mfe,3),mae=round(mae,3))

def summarize(trades,period):
    a=[x['outcome'] for x in trades if period=='all' or x['period']==period]
    x=[y for y in a if y.get('pnl') is not None]
    pnl=[z['pnl'] for z in x];positive=sum(max(0,y) for y in pnl);negative=-sum(min(0,y) for y in pnl)
    peak=eq=dd=0.
    for y in pnl:eq+=y;peak=max(peak,eq);dd=max(dd,peak-eq)
    return dict(signals=len(a),evaluated=len(x),target=sum(z['status']=='target' for z in x),
        stop=sum(z['status']=='stop' for z in x),timeout=sum(z['status']=='timeout' for z in x),
        gap=sum(z['status']=='gap' for z in x),
        target_hit_rate=round(sum(z['status']=='target' for z in x)/len(x),4) if x else None,
        avg_rr=round(sum(z['reward']/z['risk'] for z in x)/len(x),3) if x else None,
        net_price_dollars=round(sum(pnl),2),expectancy_price_dollars=round(sum(pnl)/len(x),3) if x else None,
        profit_factor=round(positive/negative,3) if negative else None,
        max_drawdown_price_dollars=round(dd,2))

def run(path,out):
    m1=b.read(path);m15=b.resample(m1,15);h1=b.resample(m1,60);h4=b.resample(m1,240)
    htf_pivots,zones=b.build_htf(h1,h4);ss=b.sweeps(h1,h4,htf_pivots)
    gaps=b.m15_gaps(m15);ends=[x[1] for x in m15];starts=[x[0] for x in m1]
    all_pivots=pivot15(m15)+[dict(tf=p['tf'],side='sell_target' if p['side']=='buy' else 'buy_target',
        price=p['price'],avail=p['avail'],id=p['id']) for p in htf_pivots]
    variants={}
    for hold in (False,True):
        name='ifvg_immediate' if not hold else 'ifvg_one_closed_bar_hold'
        counts=Counter();trades=[];last={}
        for s in ss:
            counts['sweep']+=1
            poi=b.poi_after(s,m15,zones)
            if not poi:continue
            counts['poi']+=1
            origin=origin_of_sweep(s,m15,ends)
            if not origin:continue
            counts['sweep_run_origin']+=1
            inv=find_ifvg(s,poi,m15,gaps,hold)
            if not inv:continue
            counts['ifvg']+=1
            signal=cisd(s,inv,origin,m15,ends)
            if not signal:continue
            counts['m15_cisd']+=1
            at=signal['time']
            if at-last.get(s['side'],-10**18)<4*H:
                counts['cooldown']+=1;continue
            i=bisect.bisect_left(starts,at)
            if i>=len(m1) or m1[i][0]!=at:
                counts['missing_entry_bar']+=1;continue
            entry_bid=m1[i][2]
            if s['side']=='sell':
                post=[x[3] for x in m15 if inv['time']<=x[1]<=at]
                stop=max(post)+.20 if post else None
            else:
                post=[x[4] for x in m15 if inv['time']<=x[1]<=at]
                stop=min(post)-.20 if post else None
            if stop is None:continue
            target=nearest_target(s,entry_bid,at,all_pivots,m15,ends,.30)
            if not target:
                counts['no_untouched_opposing_liquidity']+=1;continue
            outcome=execute(m1,starts,at,s['side'],stop,target['price'])
            if outcome['status'] in ('censored','no_entry','invalid_rr'):
                counts[outcome['status']]+=1;continue
            last[s['side']]=at
            trades.append(dict(sweep=b.iso(s['time']),sweep_tf=s['tf'],side=s['side'],
                poi=b.iso(poi['time']),poi_kind=poi['zone']['kind'],poi_tf=poi['zone']['tf'],
                run_origin=b.iso(origin['time']),run_candles=origin['count'],
                run_origin_price=round(origin['price'],3),
                ifvg=b.iso(inv['time']),ifvg_confirm=b.iso(inv['confirm']),
                ifvg_lo=round(inv['gap']['lo'],3),ifvg_hi=round(inv['gap']['hi'],3),
                cisd=b.iso(at),cisd_close=round(signal['close'],3),
                entry_bid=round(entry_bid,3),stop_bid=round(stop,3),
                target_bid=round(target['price'],3),target_tf=target['tf'],
                period='first_month' if at<b.SPLIT else 'second_month',outcome=outcome))
        variants[name]=dict(stage_counts=dict(counts),n=len(trades),
            results={p:summarize(trades,p) for p in ('all','first_month','second_month')},
            trades=trades)
    summary=dict(strategy='HTF wick sweep -> independent HTF POI -> M15 IFVG -> M15 sweep-origin CISD -> structural stop and opposing-liquidity target',
        start_utc=b.iso(b.START),end_utc=b.iso(b.END),
        data=dict(m1=len(m1),m15=len(m15),h1=len(h1),h4=len(h4),last_utc=b.iso(m1[-1][0]),
            htf_pivots=len(htf_pivots),m15_pivots=len(all_pivots)-len(htf_pivots)),
        assumptions=dict(sweep='H1/H4 confirmed swing wick-only sweep; same as V1',
            poi='Pre-existing independent H1/H4 OB, FVG or SR; same as V1',
            run_origin='First of >=2 contiguous same-direction M15 candles nearest extreme of closed HTF sweep bar, <=2 candles before extreme',
            ifvg='M15 opposite-color close inside FVG from original side, optional one CLOSED M15 hold below upper bound (sell)/above lower (buy)',
            cisd='M15 displacement body >=1.2x median last20 and >=60% range; fresh CLOSE beyond first sweep-run candle low/high; IFVG overlap in last 3 M15 candles',
            entry='Next M1 BID open after M15 CISD close; BUY pays 0.30 USD assumed ASK spread',
            stop='Highest/lowest M15 wick since IFVG inversion until CISD plus 0.20 USD buffer',
            target='Nearest prior confirmed and unconsumed M15/H1/H4 swing on opposing side, >=1 USD away',
            exit='Target or stop first within 12 hours, otherwise close at 12h; within-M1 ambiguity treated as stop; 0.10 USD slippage each side',
            cooldown='Four hours same direction; excludes signals lacking full 12h observation'),
        warnings=['The supplied explanation is from a second model, not independently verified from video.',
            'The definition of the first sweep-run candle, POI and what constitutes a major liquidity pool remain discretionary; these are documented proxies.',
            'One-bar IFVG hold is an assumption; cannot demand that price NEVER returns above IFVG without future leakage.',
            'Targets are only chart-confirmed prior swing highs/lows, not actual resting orders or order book.',
            'Sample sizes may be very small; do not infer strategy quality from isolated winners.',
            'No real broker spread/ASK or slippage; assumed execution only.',
            'Both variants are exploratory, not out-of-sample validated; do not select the winner on this same test period.',
            'No changes to production alert.py.'],
        variants={k:{key:v for key,v in d.items() if key!='trades'} for k,d in variants.items()})
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    (out/'summary.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False))
    for name,data in variants.items():
        (out/(name+'_trades.json')).write_text(json.dumps(data['trades'],indent=2,ensure_ascii=False))
    print(json.dumps(summary['variants'],indent=2))
if __name__=='__main__':run(sys.argv[1],sys.argv[2])
