#!/usr/bin/env python3
"""Live research-only XAUUSD M1 monitor.

Uses only observed Dukascopy BID M1 OHLC. Identifies:
(1) retrospective non-overlapping >=20 USD close-to-close swings using
    3/5 USD directional-change confirmation (NOT a tradable pivot signal),
(2) 60-minute forward opportunities in the last four hours, noting overlaps,
(3) live as-of-last-CLOSED-bar market state with causal price-action features.
No orders, no Telegram signals, no claims of predictive accuracy.
"""
import csv,sys,json,math,statistics
from datetime import datetime,timezone
from pathlib import Path
from collections import Counter
M=60000
def iso(t):return datetime.fromtimestamp(t/1000,timezone.utc).isoformat()
def read(path):
    out={}
    with open(path,newline="") as f:
        for r in csv.DictReader(f):
            try:
                t=int(float(r["timestamp"]))
                o,h,l,c=[float(r[k]) for k in ("open","high","low","close")]
                if all(math.isfinite(x) for x in (o,h,l,c)) and h>=max(o,c) and l<=min(o,c) and h>l:
                    out[t]=(t,o,h,l,c)
            except (ValueError,KeyError):pass
    return [out[k] for k in sorted(out)]

def zigzag(rows,threshold):
    if len(rows)<3:return []
    # The last observed extreme is provisional until an opposite move >= threshold.
    pivots=[]
    trend=0
    low_i=0;high_i=0;extreme_i=0
    for i in range(1,len(rows)):
        c=rows[i][4]
        if trend==0:
            if c<rows[low_i][4]:low_i=i
            if c>rows[high_i][4]:high_i=i
            if c-rows[low_i][4]>=threshold and low_i<i:
                pivots.append(("low",low_i,i))
                trend=1;extreme_i=i
            elif rows[high_i][4]-c>=threshold and high_i<i:
                pivots.append(("high",high_i,i))
                trend=-1;extreme_i=i
        elif trend==1:
            if c>rows[extreme_i][4]:extreme_i=i
            elif rows[extreme_i][4]-c>=threshold:
                pivots.append(("high",extreme_i,i))
                trend=-1;extreme_i=i
        else:
            if c<rows[extreme_i][4]:extreme_i=i
            elif c-rows[extreme_i][4]>=threshold:
                pivots.append(("low",extreme_i,i))
                trend=1;extreme_i=i
    moves=[]
    for (a,ia,ca),(b,ib,cb) in zip(pivots,pivots[1:]):
        if ia>=ib:continue
        delta=rows[ib][4]-rows[ia][4]
        if abs(delta)<20:continue
        moves.append({"direction":"up" if delta>0 else "down",
            "from_utc":iso(rows[ia][0]),"from_price":round(rows[ia][4],2),
            "to_utc":iso(rows[ib][0]),"to_price":round(rows[ib][4],2),
            "dollars":round(abs(delta),2),
            "duration_minutes":round((rows[ib][0]-rows[ia][0])/M),
            "start_pivot_confirmed_utc":iso(rows[ca][0]),
            "start_pivot_confirmation_delay_min":round((rows[ca][0]-rows[ia][0])/M),
            "start_pivot_confirmation_price":round(rows[ca][4],2)})
    return moves

def last_bar_features(rows):
    if len(rows)<80:return {}
    t,o,h,l,c=rows[-1];prev=rows[-61:-1]
    ranges=[x[2]-x[3] for x in prev]
    vol=sum(ranges)/len(ranges)
    high30=max(x[2] for x in rows[-31:-1])
    low30=min(x[3] for x in rows[-31:-1])
    body=abs(c-o)
    wickup=h-max(o,c);wickdown=min(o,c)-l
    p=rows[-2]
    ptop=max(x[2] for x in rows[-33:-3])
    pbot=min(x[3] for x in rows[-33:-3])
    prev_break_up=p[4]>ptop and p[2]>ptop
    prev_break_down=p[4]<pbot and p[3]<pbot
    return {
      "last_closed_bar_utc":iso(t),"last_close_bid":round(c,3),
      "previous_30m_high":round(high30,3),"previous_30m_low":round(low30,3),
      "average_1m_range_60":round(vol,3),
      "current_1m_range":round(h-l,3),
      "current_range_vs_60m":round((h-l)/vol,3) if vol else None,
      "current_body":round(body,3),
      "upper_wick_ratio":round(wickup/(h-l),3),
      "lower_wick_ratio":round(wickdown/(h-l),3),
      "close_change_3m":round(c-rows[-4][4],3),
      "close_change_5m":round(c-rows[-6][4],3),
      "close_change_15m":round(c-rows[-16][4],3),
      "closed_above_prior30m_high":c>high30,
      "closed_below_prior30m_low":c<low30,
      "failed_break_up_last_2bars":prev_break_up and c<ptop and c<o,
      "failed_break_down_last_2bars":prev_break_down and c>pbot and c>o,
      "early_stall_after_break_up":prev_break_up and c<o and c<p[4] and h<=p[2],
      "early_stall_after_break_down":prev_break_down and c>o and c>p[4] and l>=p[3],
      "signal_status":"research observation only; NOT a verified buy/sell signal"}

def main(path,out):
    rows=read(path)
    if len(rows)<300:raise SystemExit(f"Need >=300 nonflat M1 bars, got {len(rows)}")
    last=rows[-1][0]
    start=last-4*3600000
    window=[r for r in rows if start<=r[0]<=last]
    # avoid claiming continuous data if missing candles.
    gaps=sum(1 for a,b in zip(window,window[1:]) if b[0]-a[0]!=M)
    maxhigh=max(x[2] for x in window);minlow=min(x[3] for x in window)
    opportunities=Counter()
    first_examples=[]
    for i,r in enumerate(window):
        t,o,h,l,c=r
        if i+60>=len(window):continue
        future=window[i+1:i+61]
        if any(b[0]-a[0]!=M for a,b in zip([r]+future, future)):continue
        entry=future[0][1]
        up=max(x[2] for x in future)-entry
        down=entry-min(x[3] for x in future)
        if up>=20:opportunities["buy_overlapping_minutes"]+=1
        if down>=20:opportunities["sell_overlapping_minutes"]+=1
        if (up>=20 or down>=20) and len(first_examples)<8:
            first_examples.append({"entry_utc":iso(future[0][0]),
                "entry":round(entry,2),"max_up_next60":round(up,2),
                "max_down_next60":round(down,2)})
    outdata={
      "source":"Dukascopy XAUUSD BID M1 (delayed feed, not broker's executable ask)",
      "latest_available_utc":iso(last),
      "window_start_utc":iso(start),"window_end_utc":iso(last),
      "bars":len(window),"missing_minute_gaps":gaps,
      "window_high":round(maxhigh,2),"window_low":round(minlow,2),
      "window_high_low_range":round(maxhigh-minlow,2),
      "independent_close_swings_reversal3":zigzag(window,3),
      "independent_close_swings_reversal5":zigzag(window,5),
      "rolling_60m_20usd_opportunities_overlapping":dict(opportunities),
      "rolling_60m_examples":first_examples,
      "latest_closed_bar_state":last_bar_features(rows),
      "cautions":["Zigzag pivot is identified retrospectively after reversal threshold, not at pivot time.",
        "Rolling 60m opportunities heavily overlap and are not independent trades.",
        "If range<20, there cannot be a >=20 USD move inside that window.",
        "All BID-only and no spreads, ASK, slippage, latency or news context.",
        "No model shown to predict a 20 USD move reliably; latest state is descriptive.",
        "Source may lag actual live broker chart by minutes or more."]}
    dest=Path(out);dest.mkdir(parents=True,exist_ok=True)
    (dest/"latest.json").write_text(json.dumps(outdata,indent=2,ensure_ascii=False))
    print(json.dumps({"latest_utc":outdata["latest_available_utc"],"bars":len(window),
        "range":outdata["window_high_low_range"],
        "independent_swings_3":len(outdata["independent_close_swings_reversal3"]),
        "independent_swings_5":len(outdata["independent_close_swings_reversal5"]),
        "overlapping_opportunities":outdata["rolling_60m_20usd_opportunities_overlapping"]},indent=2))
if __name__=="__main__":main(sys.argv[1],sys.argv[2])
