#!/usr/bin/env python3
"""Pre-registered exploratory M1 gold study: sweeps, false breaks and impulses.

All signals use CLOSED M1 bars and only prior price history. Entry is the
NEXT M1 OPEN. A continuous 30-minute future is required, to exclude market
closures and artificial candles. BID-only results ignore spread/slippage.
"""
import csv, json, math, statistics, sys
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path

MINUTE = 60000
TARGETS = (10, 20, 30)
HORIZONS = (5, 10, 15, 30)
STOP = 5.0
COOLDOWN = 30 * MINUTE

def wilson(w, n):
    if not n: return [None, None]
    z = 1.96
    p = w / n
    d = 1 + z*z/n
    c = (p + z*z/(2*n))/d
    e = z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/d
    return [round(c-e, 4), round(c+e, 4)]

def read(path):
    all_rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            try:
                t = int(float(row["timestamp"]))
                o,h,l,c = (float(row[k]) for k in ("open","high","low","close"))
                if not all(math.isfinite(x) for x in (o,h,l,c)): continue
                if h < max(o,c) or l > min(o,c): continue
                all_rows.append((t,o,h,l,c))
            except (ValueError,KeyError): continue
    all_rows.sort()
    clean = [r for r in all_rows if r[2] > r[3]]
    return len(all_rows), clean

def outcome(rows, i, side, target, horizon):
    entry = rows[i+1][1]
    mfe, mae = 0.0, 0.0
    for j in range(i+1, i+1+horizon):
        h,l = rows[j][2],rows[j][3]
        fav = h-entry if side=="buy" else entry-l
        adv = entry-l if side=="buy" else h-entry
        mfe=max(mfe,fav); mae=max(mae,adv)
        hit_target = fav >= target
        hit_stop = adv >= STOP
        if hit_target and hit_stop: return "ambiguous",round(mfe,3),round(mae,3)
        if hit_target: return "win",round(mfe,3),round(mae,3)
        if hit_stop: return "loss",round(mfe,3),round(mae,3)
    return "timeout",round(mfe,3),round(mae,3)

def scan(rows):
    n=len(rows)
    # Any event needs 60 uninterrupted minutes of past data and 30 future minutes.
    # Requiring every minute to have a non-flat candle avoids synthetic weekend data.
    run_before=[1]*n
    for i in range(1,n):
        if rows[i][0]-rows[i-1][0]==MINUTE:
            run_before[i]=run_before[i-1]+1
    run_after=[1]*n
    for i in range(n-2,-1,-1):
        if rows[i+1][0]-rows[i][0]==MINUTE:
            run_after[i]=run_after[i+1]+1
    seen={}
    events=[]
    eligible=0
    for i in range(61,n-30):
        if run_before[i] < 61 or run_after[i] < 31: continue
        eligible+=1
        t,o,h,l,c=rows[i]
        prev=rows[i-30:i]
        old_high=max(x[2] for x in prev)
        old_low=min(x[3] for x in prev)
        prev20=rows[i-20:i]
        median_body=statistics.median(abs(x[4]-x[1]) for x in prev20)
        mean10=sum(x[2]-x[3] for x in rows[i-10:i])/10
        mean60=sum(x[2]-x[3] for x in rows[i-60:i])/60
        candle_range=h-l
        body=abs(c-o)
        wick_up=h-max(o,c)
        wick_down=min(o,c)-l
        penetration=max(0.25,0.18*mean60)
        proposals=[]
        # A 30-minute local low/high is swept, but price closes back inside.
        if l<old_low-penetration and c>old_low and wick_down/candle_range>=0.35 and c>o:
            proposals.append(("sweep_1bar","buy"))
        if h>old_high+penetration and c<old_high and wick_up/candle_range>=0.35 and c<o:
            proposals.append(("sweep_1bar","sell"))
        # Two-bar false break: previous bar closes outside, current closes back inside.
        p=rows[i-1]; older=rows[i-31:i-1]
        ph=max(x[2] for x in older); pl=min(x[3] for x in older)
        if p[4]<pl-penetration and c>pl and c>o:
            proposals.append(("false_break_2bar","buy"))
        if p[4]>ph+penetration and c<ph and c<o:
            proposals.append(("false_break_2bar","sell"))
        # Momentum continuation after relative contraction of 1m candle ranges.
        if mean10<0.8*mean60 and body>max(0.5,1.8*median_body):
            if c>max(x[2] for x in rows[i-10:i]) and c>o:
                proposals.append(("compression_impulse","buy"))
            if c<min(x[3] for x in rows[i-10:i]) and c<o:
                proposals.append(("compression_impulse","sell"))
        # Time-sampled direction-neutral reference, one buy + one sell each 30m.
        if t%(30*MINUTE)==0:
            proposals.extend((("baseline_30m","buy"),("baseline_30m","sell")))
        for kind,side in proposals:
            key=(kind,side)
            if kind!="baseline_30m" and t-seen.get(key,-10**18)<COOLDOWN: continue
            seen[key]=t
            event={"utc":datetime.fromtimestamp(t/1000,timezone.utc).isoformat(),
                   "kind":kind,"side":side,"entry":rows[i+1][1],
                   "range_1m":round(candle_range,3),"mean_range_60":round(mean60,3),
                   "split":"train" if i < n*0.6 else ("validation" if i<n*0.8 else "test")}
            for horizon in HORIZONS:
                for target in TARGETS:
                    status,mfe,mae=outcome(rows,i,side,target,horizon)
                    event[f"t{target}_h{horizon}"]=status
                    if horizon==15 and target==20:
                        event["mfe_15"]=mfe;event["mae_15"]=mae
            events.append(event)
    return events,eligible

def summary(events,eligible,raw,rows):
    groups=defaultdict(list)
    for e in events:
        groups[(e["kind"],"all")].append(e)
        groups[(e["kind"],e["split"])].append(e)
    out={}
    for (kind,split),data in sorted(groups.items()):
        key=f"{kind}__{split}"
        out[key]={"signals":len(data)}
        for h in HORIZONS:
            for target in TARGETS:
                k=f"t{target}_h{h}"
                ct=Counter(e[k] for e in data)
                wins=ct["win"]; total=len(data)
                out[key][k]={"win":wins,"loss":ct["loss"],
                             "timeout":ct["timeout"],"ambiguous":ct["ambiguous"],
                             "success_all":round(wins/total,4) if total else None,
                             "wilson95_all":wilson(wins,total)}
        out[key]["median_mfe_15"]=round(statistics.median(e["mfe_15"] for e in data),3)
        out[key]["median_mae_15"]=round(statistics.median(e["mae_15"] for e in data),3)
    return {"source":"Dukascopy XAUUSD M1 BID, 2026-09-01 to 2026-11-17",
            "raw_rows":raw,"nonflat_rows":len(rows),"eligible_signal_minutes":eligible,
            "first_utc":datetime.fromtimestamp(rows[0][0]/1000,timezone.utc).isoformat(),
            "last_utc":datetime.fromtimestamp(rows[-1][0]/1000,timezone.utc).isoformat(),
            "predefined_patterns":["sweep_1bar","false_break_2bar","compression_impulse"],
            "execution":"M1 candle CLOSE signal, NEXT minute OPEN entry, $5 stop, target $10/$20/$30, 5/10/15/30 min, no intrabar ordering, no transaction costs",
            "splits":"chronological 60% train, 20% validation, 20% untouched test (split by candle index)",
            "warnings":["Exploratory predefined patterns, not a validated trading system.",
                        "BID-only prices, ignores ASK/spread/slippage/commissions and market impact.",
                        "Same-minute target and stop is ambiguous and counts as failure in success_all.",
                        "Market-closed, missing, and flat candles excluded; 60-minute continuous warmup and 30-minute future required.",
                        "Some overlapping event horizons; sample counts do not equal independent trials.",
                        "Dukascopy quote activity is not consolidated institutional executed volume.",
                        "Multiple hypotheses/horizons tested: apparent winners need fresh out-of-sample replication.",
                        "Success_all includes stop and timeout; does not imply net profitability."],
            "groups":out}

def main():
    if len(sys.argv)!=3: raise SystemExit("usage: script.py input.csv outdir")
    raw,rows=read(sys.argv[1])
    if len(rows)<1000: raise SystemExit(f"Too few nonflat M1 candles: {len(rows)}")
    events,eligible=scan(rows)
    dest=Path(sys.argv[2]);dest.mkdir(parents=True,exist_ok=True)
    result=summary(events,eligible,raw,rows)
    (dest/"summary.json").write_text(json.dumps(result,indent=2,ensure_ascii=False))
    if events:
        with (dest/"events.csv").open("w",newline="") as f:
            w=csv.DictWriter(f,fieldnames=events[0].keys())
            w.writeheader();w.writerows(events)
    print("Raw:",raw,"nonflat:",len(rows),"eligible:",eligible,"events:",len(events))
    for key,group in result["groups"].items():
        if key.endswith("__all"):
            x=group["t20_h15"]
            print(key, "n",group["signals"],"20USD/15min",x["win"],"wins",x["success_all"],"CI",x["wilson95_all"])

if __name__=="__main__":main()
