#!/usr/bin/env python3
"""Research V3: six-plus-month structural stop and IFVG retest LIMIT study.
Preserves original ordered HTF sweep->HTF POI->M15 IFVG->M15 CISD logic.
Does not edit production bot. No hindsight or lookahead for signal/target.
"""
import bisect,csv,json,math,statistics,sys
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import study_sweep_poi_ifvg_cisd as b
import study_sweep_poi_ifvg_cisd_v2 as v2

M=b.M;H=b.H;SPREAD=.30;SLIPPAGE=.10;BUFFER=.20
b.START=b.ts('2026-05-01T00:00:00Z')
b.SPLIT=b.ts('2026-08-01T00:00:00Z')
b.END=b.ts('2026-11-17T09:00:00Z')
RECENT=b.ts('2026-09-17T09:00:00Z')
LDN=ZoneInfo('Europe/London');NY=ZoneInfo('America/New_York')
def in_session(t):
    d=datetime.fromtimestamp(t/1000,timezone.utc)
    l=d.astimezone(LDN);n=d.astimezone(NY)
    return (8<=l.hour<17) or (8<=n.hour<17)
def session_name(t):
    d=datetime.fromtimestamp(t/1000,timezone.utc)
    l=8<=d.astimezone(LDN).hour<17;n=8<=d.astimezone(NY).hour<17
    return 'overlap' if l and n else 'london' if l else 'new_york' if n else 'asia_or_off_hours'
def limit_quote(inv,side,location):
    f=inv['gap']
    if location=='midpoint':return (f['lo']+f['hi'])/2
    if location=='near_edge':return f['lo'] if side=='sell' else f['hi']
    raise ValueError(location)
def stop_price(s,inv,signal,m15,mode):
    if mode=='cisd_candle':
        c=m15[signal['idx']]
        return (c[3]+BUFFER if s['side']=='sell' else c[4]-BUFFER)
    bars=[x for x in m15 if inv['time']<=x[1]<=signal['time']]
    if not bars:return None
    return max(x[3] for x in bars)+BUFFER if s['side']=='sell' else min(x[4] for x in bars)-BUFFER
def prices(side,entry_bid,stop_bid,target_bid):
    buy=side=='buy'
    entry=entry_bid+SPREAD if buy else entry_bid
    stop=stop_bid if buy else stop_bid+SPREAD
    target=target_bid if buy else target_bid+SPREAD
    risk=(entry-stop if buy else stop-entry)+2*SLIPPAGE
    reward=(target-entry if buy else entry-target)-2*SLIPPAGE
    return risk,reward
def limit_sim(m1,starts,at,side,limit_bid,stop_bid,target_bid,session_only):
    # Pending order begins after the CLOSED M15 CISD. A bid/ask midpoint is
    # never treated as an already-filled historical market order.
    buy=side=='buy'
    risk,reward=prices(side,limit_bid,stop_bid,target_bid)
    if risk<=0 or reward<=0:return dict(status='invalid_levels',pnl=None)
    i=bisect.bisect_left(starts,at)
    if i>=len(m1) or m1[i][0]!=at:return dict(status='no_entry_bar',pnl=None)
    if buy and limit_bid>=m1[i][2]-.10:return dict(status='marketable_not_retest',pnl=None)
    if not buy and limit_bid<=m1[i][2]+.10:return dict(status='marketable_not_retest',pnl=None)
    fill_i=None;prev=None
    for j in range(i,min(i+4*60,len(m1))):
        x=m1[j]
        if prev is not None and x[0]!=prev[1]:return dict(status='pending_gap',pnl=None)
        if session_only and not in_session(x[0]):return dict(status='session_expired',pnl=None)
        # Cancel if the opposite liquidity is already consumed BEFORE a retest.
        if buy:
            filled=x[4]+SPREAD<=limit_bid+SPREAD
            target_seen=x[3]>=target_bid
        else:
            filled=x[3]>=limit_bid
            target_seen=x[4]+SPREAD<=target_bid+SPREAD
        if filled:
            fill_i=j;break
        if target_seen:return dict(status='target_before_fill',pnl=None)
        prev=x
    if fill_i is None:return dict(status='unfilled_4h',pnl=None)
    fill_t=m1[fill_i][0]
    if fill_t+12*H>min(b.END,m1[-1][1]):return dict(status='censored',pnl=None)
    # Limit assumed at quoted level, never grant better gap-open fills.
    entry=limit_bid+SPREAD if buy else limit_bid
    stop=stop_bid if buy else stop_bid+SPREAD
    target=target_bid if buy else target_bid+SPREAD
    prev=None;mfe=mae=0.
    for j in range(fill_i,min(fill_i+12*60,len(m1))):
        x=m1[j]
        if prev is not None and x[0]!=prev[1]:
            pnl=(prev[5]-entry if buy else entry-(prev[5]+SPREAD))-2*SLIPPAGE
            return dict(status='gap',pnl=round(pnl,4),risk=round(risk,4),
                reward=round(reward,4),fill=b.iso(fill_t),mfe=round(mfe,3),mae=round(mae,3))
        high=x[3] if buy else x[3]+SPREAD
        low=x[4] if buy else x[4]+SPREAD
        mfe=max(mfe,high-entry if buy else entry-low)
        mae=max(mae,entry-low if buy else high-entry)
        stop_hit=(low<=stop if buy else high>=stop)
        target_hit=(high>=target if buy else low<=target)
        if stop_hit:return dict(status='stop',pnl=round(-risk,4),risk=round(risk,4),
            reward=round(reward,4),fill=b.iso(fill_t),mfe=round(mfe,3),mae=round(mae,3))
        # Target in same M1 bar as limit fill is not credited; the sequence of
        # low/high within that minute is unknown (conservative).
        if target_hit and j>fill_i:
            return dict(status='target',pnl=round(reward,4),risk=round(risk,4),
                reward=round(reward,4),fill=b.iso(fill_t),mfe=round(mfe,3),mae=round(mae,3))
        prev=x
    if prev is None:return dict(status='no_exit',pnl=None)
    pnl=(prev[5]-entry if buy else entry-(prev[5]+SPREAD))-2*SLIPPAGE
    return dict(status='timeout',pnl=round(pnl,4),risk=round(risk,4),
        reward=round(reward,4),fill=b.iso(fill_t),mfe=round(mfe,3),mae=round(mae,3))
def market_sim(m1,starts,at,side,stop_bid,target_bid):
    return v2.execute(m1,starts,at,side,stop_bid,target_bid,
        spread=SPREAD,slippage=SLIPPAGE,hours=12)
def metrics(trades,scope='all'):
    if scope=='recent_two_months':
        a=[t for t in trades if b.ts(t['signal'])>=RECENT]
    elif scope=='first_three_months':
        a=[t for t in trades if b.ts(t['signal'])<b.SPLIT]
    elif scope=='last_three_plus_months':
        a=[t for t in trades if b.ts(t['signal'])>=b.SPLIT]
    else:a=trades
    vals=[t['outcome']['pnl'] for t in a if t['outcome'].get('pnl') is not None]
    rvals=[t['realized_r'] for t in a if t['realized_r'] is not None]
    eq=peak=dd=0.
    for r in rvals:eq+=r;peak=max(peak,eq);dd=max(dd,peak-eq)
    pos=sum(max(0,x) for x in vals);neg=-sum(min(0,x) for x in vals)
    wins=sum(t['outcome']['status']=='target' for t in a)
    return dict(trades=len(a),targets=wins,stops=sum(t['outcome']['status']=='stop' for t in a),
        timeouts=sum(t['outcome']['status']=='timeout' for t in a),
        gaps=sum(t['outcome']['status']=='gap' for t in a),
        hit_rate=round(wins/len(a),4) if a else None,
        mean_planned_rr=round(sum(t['planned_rr'] for t in a)/len(a),3) if a else None,
        median_planned_rr=round(statistics.median(t['planned_rr'] for t in a),3) if a else None,
        net_price_dollars=round(sum(vals),3),mean_price_dollars=round(statistics.mean(vals),3) if vals else None,
        total_r=round(sum(rvals),3),mean_r=round(statistics.mean(rvals),3) if rvals else None,
        median_r=round(statistics.median(rvals),3) if rvals else None,
        profit_factor=round(pos/neg,3) if neg else None,max_drawdown_r=round(dd,3),
        worst_r=round(min(rvals),3) if rvals else None,
        best_r=round(max(rvals),3) if rvals else None)
def scenarios():
    out=[]
    out.append(dict(id='old_market_old_stop_no_filters',mode='market',stop='old',session=False,rr=False,scope='all',location='midpoint'))
    for session in (False,True):
        suffix='london_ny' if session else 'all_sessions'
        out.append(dict(id=f'market_cisd_stop_rr2_{suffix}',mode='market',stop='cisd_candle',session=session,rr=True,scope='all',location='midpoint'))
        out.append(dict(id=f'limit_mid_cisd_stop_rr2_{suffix}',mode='limit',stop='cisd_candle',session=session,rr=True,scope='all',location='midpoint'))
    for mode in ('market','limit'):
        out.append(dict(id=f'{mode}_old_stop_rr2_london_ny',mode=mode,stop='old',session=True,rr=True,scope='all',location='midpoint'))
    out.append(dict(id='limit_edge_cisd_stop_rr2_london_ny',mode='limit',stop='cisd_candle',session=True,rr=True,scope='all',location='near_edge'))
    out.append(dict(id='limit_mid_cisd_stop_rr2_london_ny_htf_target',mode='limit',stop='cisd_candle',session=True,rr=True,scope='htf_only',location='midpoint'))
    return out
def build_candidates(m1,m15,h1,h4):
    htf_pivots,zones=b.build_htf(h1,h4)
    sweeps=b.sweeps(h1,h4,htf_pivots);gaps=b.m15_gaps(m15)
    ends=[x[1] for x in m15]
    pivots=v2.pivot15(m15)+[dict(tf=p['tf'],side='sell_target' if p['side']=='buy' else 'buy_target',
        price=p['price'],avail=p['avail'],id=p['id']) for p in htf_pivots]
    stages=Counter();candidates=[]
    for s in sweeps:
        stages['sweep']+=1
        poi=b.poi_after(s,m15,zones)
        if poi is None:continue
        stages['poi']+=1
        origin=v2.origin_of_sweep(s,m15,ends)
        if origin is None:continue
        stages['sweep_origin']+=1
        inv=v2.find_ifvg(s,poi,m15,gaps,hold=False)
        if inv is None:continue
        stages['ifvg']+=1
        signal=v2.cisd(s,inv,origin,m15,ends)
        if signal is None:continue
        stages['m15_cisd']+=1
        candidates.append(dict(s=s,poi=poi,origin=origin,inv=inv,signal=signal))
    candidates.sort(key=lambda x:(x['signal']['time'],x['s']['side'],x['s']['time']))
    return candidates,pivots,ends,stages
def run(path,out):
    m1=b.read(path);m15=b.resample(m1,15);h1=b.resample(m1,60);h4=b.resample(m1,240)
    starts=[x[0] for x in m1]
    candidates,pivots,ends,stages=build_candidates(m1,m15,h1,h4)
    output=Path(out);output.mkdir(parents=True,exist_ok=True)
    allstats={}
    for cfg in scenarios():
        name=cfg['id'];counts=Counter();trades=[];last={}
        for cand in candidates:
            s=cand['s'];sig=cand['signal'];inv=cand['inv'];at=sig['time']
            side=s['side'];counts['candidate']+=1
            if cfg['session'] and not in_session(at):
                counts['asia_signal_excluded']+=1;continue
            if at-last.get(side,-10**18)<4*H:
                counts['cooldown']+=1;continue
            stop=stop_price(s,inv,sig,m15,cfg['stop'])
            if stop is None:continue
            i=bisect.bisect_left(starts,at)
            if i>=len(m1) or m1[i][0]!=at:
                counts['missing_next_m1_open']+=1;continue
            if cfg['mode']=='market':
                quote_bid=m1[i][2]
            else:
                quote_bid=limit_quote(inv,side,cfg['location'])
                if side=='buy':quote_bid-=SPREAD # ask limit is IFVG level
            target=v2.nearest_target(s,quote_bid,at,pivots,m15,ends,SPREAD,cfg['scope'])
            if target is None:
                counts['no_opposing_liquidity']+=1;continue
            risk,reward=prices(side,quote_bid,stop,target['price'])
            if risk<=0 or reward<=0:
                counts['invalid_risk_reward']+=1;continue
            rr=reward/risk
            if cfg['rr'] and rr<2.0:
                counts['rr_below_2_excluded']+=1;continue
            counts['rr_passed']+=1
            if cfg['mode']=='market':
                result=market_sim(m1,starts,at,side,stop,target['price'])
                entry_bid=quote_bid
            else:
                result=limit_sim(m1,starts,at,side,quote_bid,stop,target['price'],cfg['session'])
                entry_bid=quote_bid
            status=result['status']
            if result.get('pnl') is None:
                counts[status]+=1;continue
            counts['filled']+=1
            last[side]=at
            realized=result['pnl']/result['risk']
            trades.append(dict(signal=b.iso(at),side=side,session=session_name(at),
                sweep=b.iso(s['time']),sweep_tf=s['tf'],
                poi_kind=cand['poi']['zone']['kind'],poi_tf=cand['poi']['zone']['tf'],
                ifvg=b.iso(inv['time']),ifvg_lo=round(inv['gap']['lo'],3),
                ifvg_hi=round(inv['gap']['hi'],3),
                cisd=b.iso(at),cisd_high=round(m15[sig['idx']][3],3),
                cisd_low=round(m15[sig['idx']][4],3),
                planned_entry_bid=round(entry_bid,3),
                stop_bid=round(stop,3),target_bid=round(target['price'],3),
                target_tf=target['tf'],planned_risk=round(risk,4),
                planned_reward=round(reward,4),planned_rr=round(rr,4),
                realized_r=round(realized,4),outcome=result))
        with (output/(name+'_trades.json')).open('w') as f:json.dump(trades,f,indent=2)
        allstats[name]=dict(config=cfg,stages=dict(counts),periods={
            period:metrics(trades,period) for period in
            ('all','recent_two_months','first_three_months','last_three_plus_months')})
        print(name,json.dumps(allstats[name]['periods']['all']),flush=True)
    with (output/'per_trade_r.csv').open('w',newline='') as f:
        fields=['scenario','signal_utc','side','session','fill_utc','planned_entry_bid','stop_bid',
            'target_bid','target_tf','planned_risk_usd','planned_reward_usd','planned_rr',
            'realized_r','result','pnl_usd','ifvg_utc']
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for name in allstats:
            for t in json.loads((output/(name+'_trades.json')).read_text()):
                z=t['outcome']
                w.writerow(dict(scenario=name,signal_utc=t['signal'],side=t['side'],
                    session=t['session'],fill_utc=z.get('fill',t['signal']),
                    planned_entry_bid=t['planned_entry_bid'],stop_bid=t['stop_bid'],
                    target_bid=t['target_bid'],target_tf=t['target_tf'],
                    planned_risk_usd=t['planned_risk'],planned_reward_usd=t['planned_reward'],
                    planned_rr=t['planned_rr'],realized_r=t['realized_r'],
                    result=z['status'],pnl_usd=z['pnl'],ifvg_utc=t['ifvg']))
    summary=dict(strategy='M15 CISD -> market versus IFVG limit retest, stop sensitivity, session and min 2R',
        window_utc=[b.iso(b.START),b.iso(b.END)],recent_two_months_from=b.iso(RECENT),
        data=dict(m1=len(m1),m15=len(m15),h1=len(h1),h4=len(h4),
            first=b.iso(m1[0][0]),last=b.iso(m1[-1][0])),
        raw_stages=dict(stages),raw_candidates=len(candidates),results=allstats,
        assumptions=dict(POI_and_signal='Same provisional strict HTF sweep POI M15 IFVG M15 sweep-origin CISD as V2',
            session='Signal and limit fill only 08:00-17:00 Europe/London or America/New_York, including DST',
            market='Market entry next M1 BID open after closed M15 CISD',
            limit='Sell limit at IFVG midpoint BID; buy limit at IFVG midpoint ASK, alternative nearest boundary. Order only if not immediately marketable; cancel after 4h or when session closes or target touched before fill',
            stop='CISD candle high +0.20 USD for sell, low -0.20 USD for buy; old extreme-since-IFVG stop as control',
            target='Nearest pre-confirmed, unconsumed opposing M15/H1/H4 swing; HTF-only sensitivity',
            min_rr='Projected reward / projected risk >=2 before order; inclusive of 0.30 USD spread and 0.10 USD per-side slippage',
            exit='12h after fill, stop before target within ambiguous M1 bar, no credit for target within limit fill minute',
            costs='BID OHLC with assumed constant ASK-BID 0.30 USD and slippage 0.10 each side; no actual broker quote',
            cooldown='4 hours per side after a filled trade, no overlapping same-side trade signal within cooldown',
            realized_r='Net P&L divided by risk at entry; stopped trade = -1R'),
        caveats=['Exploratory, no genuine independent forward holdout for six-month window.',
            'At least 30 trades is an aspiration, not guaranteed after strict filters.',
            'Limit fill order sequence within M1 unknown; conservative stop-first, target not credited in fill minute.',
            'M15 CISD first sweep-run candle and liquidity/POI definitions are still approximations.',
            'No production bot modified.'])
    (output/'summary.json').write_text(json.dumps(summary,indent=2))
    print('FINAL',json.dumps(dict(raw_candidates=len(candidates),
        scenarios={k:v['periods']['all'] for k,v in allstats.items()}),indent=2))
if __name__=='__main__':run(sys.argv[1],sys.argv[2])
