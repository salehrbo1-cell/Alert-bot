#!/usr/bin/env python3
"""Independent what-if replay for four Sumo sell candidates rejected on Nov 24."""
import json, os, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
sys.path.insert(0,str(Path(__file__).resolve().parent))
import sonic_pattern_paper as strategy

KSA=timezone(timedelta(hours=3)); UTC=timezone.utc
API_KEY=os.environ["TWELVE_DATA_API_KEY"].strip()
TARGETS=[
 ("2026-11-24 15:02","sell"),
 ("2026-11-24 16:02","sell"),
 ("2026-11-24 16:09","sell"),
 ("2026-11-24 16:28","sell"),
]

def fetch():
    s=requests.Session()
    params={"symbol":"XAU/USD","interval":"1min",
      "start_date":"2026-11-23 00:00:00","end_date":"2026-11-25 00:00:00",
      "timezone":"Asia/Riyadh","order":"ASC","format":"JSON","apikey":API_KEY}
    r=s.get("https://api.twelvedata.com/time_series",params=params,timeout=60)
    d=r.json()
    if r.status_code>=400 or d.get("status")=="error":
        raise RuntimeError("Twelve Data history request failed; cannot compute reliable outcomes.")
    out={}
    for x in d.get("values",[]):
        t=datetime.strptime(x["datetime"],"%Y-%m-%d %H:%M:%S").replace(tzinfo=KSA)
        ms=int(t.astimezone(UTC).timestamp()*1000)
        out[ms]={"t":ms,"o":float(x["open"]),"h":float(x["high"]),"l":float(x["low"]),"c":float(x["close"])}
    candles=[out[k] for k in sorted(out)]
    if len(candles)<1000: raise RuntimeError(f"Incomplete M1 history ({len(candles)} candles).")
    return candles

def ts(s):
    return int(datetime.strptime(s,"%Y-%m-%d %H:%M").replace(tzinfo=KSA).timestamp()*1000)

def find_setup(candles,cisd_t):
    idx={b["t"]:i for i,b in enumerate(candles)}
    i=idx.get(cisd_t)
    if i is None or i+1>=len(candles): return None
    win=candles[max(0,i-599):i+1]
    found=[x for x in strategy.detect_setups(win) if x["cisd_t"]==cisd_t]
    return (found[0],candles[i+1]) if found else None

def pnl(trade,price):
    d=1 if trade["side"]=="buy" else -1
    return round(trade["realized"]+((price-trade["entry"])*d-2*strategy.SLIPPAGE_PER_FILL_USD)
        *strategy.PAPER_LOT*strategy.CONTRACT_OUNCES_PER_LOT*trade["remaining"],2)

def close(trade,t,price,reason):
    trade.update(exit_t=t,exit=price,reason=reason,pnl=pnl(trade,price),status="closed")

def run_trade(setup,entry,entry_i,candles,opposites):
    side="sell"; d=-1
    stop=setup["sweep_extreme"]+.40
    risk=abs(entry["o"]-stop)
    trade={"side":side,"entry":entry["o"],"entry_t":entry["t"],"stop":stop,"risk":risk,
      "remaining":1.0,"realized":0.0,"partial":False,"mfe":0.0,"mae":0.0,"status":"open"}
    if stop<=entry["o"]:
        trade["invalid_initial_stop"]=True
    for i in range(entry_i,len(candles)):
        bar=candles[i]
        opposite=next((x for x in opposites if x["entry_t"]==bar["t"]),None)
        if opposite:
            close(trade,bar["t"],bar["o"],"إشارة شراء معاكسة")
            break
        favorable=entry["o"]-bar["l"]
        adverse=bar["h"]-entry["o"]
        trade["mfe"]=max(trade["mfe"],favorable)
        trade["mae"]=max(trade["mae"],adverse)
        stop_hit=bar["h"]>=trade["stop"]
        if stop_hit:
            close(trade,bar["t"]+60000,trade["stop"],
                  "وقف غير صالح/متحرك" if trade.get("invalid_initial_stop") else
                  ("وقف خسارة" if trade["stop"]>entry["o"]+.001 else "تعادل/وقف متحرك"))
            break
        if favorable>=risk and trade["entry"]<trade["stop"]:
            trade["stop"]=trade["entry"]
        if favorable>=2*risk and not trade["partial"]:
            trade["realized"]=round((2*risk-2*strategy.SLIPPAGE_PER_FILL_USD)
              *strategy.PAPER_LOT*strategy.CONTRACT_OUNCES_PER_LOT*.5,2)
            trade["remaining"]=.5; trade["partial"]=True; trade["stop"]=trade["entry"]
        if trade["partial"]:
            m5=strategy.aggregate_timeframe(candles[max(0,i-600):i],5)
            if len(m5)>=5:
                last=len(m5)-1
                for p in range(2,last-1):
                    if m5[p]["h"]>max(x["h"] for x in m5[p-2:p]+m5[p+1:p+3]):
                        if m5[p]["end_t"]<=bar["t"] and m5[p]["h"]<trade["stop"]:
                            trade["stop"]=m5[p]["h"]
    if trade["status"]=="open":
        trade.update(mark_price=candles[-1]["c"],unrealized_pnl=pnl(trade,candles[-1]["c"]))
    return trade

def main():
    candles=fetch()
    index={b["t"]:i for i,b in enumerate(candles)}
    targets=[]
    for entry_text,side in TARGETS:
        entry_t=ts(entry_text)
        cisd_t=entry_t-60000
        result=find_setup(candles,cisd_t)
        if not result:
            entry_t=ts(entry_text)
            near=[b for b in candles if abs(b["t"]-entry_t)<=120000]
            allcs=[e for e in strategy.detect_cisds(candles[max(0,index.get(entry_t-60000,0)-599):index.get(entry_t-60000,0)+1])]
            targets.append({"entry_ksa":entry_text,"side":side,"error":"Setup not reproduced from current deployed detector.",
              "data_first":candles[0]["t"],"data_last":candles[-1]["t"],"target_entry_t":entry_t,
              "target_cisd_t":entry_t-60000,"target_candle_found":entry_t in index,
              "nearby":[{"t":b["t"],"o":b["o"],"h":b["h"],"l":b["l"],"c":b["c"]} for b in near],
              "detected_cisds_near":[e["t"] for e in allcs]})
            continue
        setup,entry=result
        targets.append({"entry_text":entry_text,"side":side,"entry_t":entry["t"],"setup":setup,"entry":entry})
    # Find later opposite-direction setups, irrespective of entry filters, because Sumo's
    # current exit logic closes an open position on an opposite SMC setup.
    opposite_times=[]
    for cisd in strategy.detect_cisds(candles):
        if cisd["direction"]!="buy" or cisd["t"]<min(x.get("entry_t",10**30) for x in targets):
            continue
        idx_c=index.get(cisd["t"])
        if idx_c is None or idx_c+1>=len(candles): continue
        win=candles[max(0,idx_c-599):idx_c+1]
        for s in strategy.detect_setups(win):
            if s["cisd_t"]==cisd["t"] and s["direction"]=="buy":
                opposite_times.append({"entry_t":candles[idx_c+1]["t"],"price":candles[idx_c+1]["o"],"cisd_t":cisd["t"]})
                break
    opposite_times=sorted({x["entry_t"]:x for x in opposite_times}.values(),key=lambda x:x["entry_t"])
    results=[]
    for x in targets:
        if "error" in x:
            results.append(x); continue
        setup,entry=x["setup"],x["entry"]
        entry_i=index[entry["t"]]
        t=run_trade(setup,entry,entry_i,candles,[o for o in opposite_times if o["entry_t"]>entry["t"]])
        t.update(entry_ksa=x["entry_text"],entry_price=entry["o"],initial_stop=setup["sweep_extreme"]+.40,
          sweep_extreme=setup["sweep_extreme"],sweep_level=setup["sweep_level"],
          risk=abs(entry["o"]-(setup["sweep_extreme"]+.40)),sweep_tf=setup["sweep_timeframe"],
          first_opposite_buy=next((datetime.fromtimestamp(o["entry_t"]/1000,UTC).astimezone(KSA).strftime("%Y-%m-%d %H:%M")
                                  for o in opposite_times if o["entry_t"]>entry["t"]),None))
        if t.get("exit_t"):
            t["exit_ksa"]=datetime.fromtimestamp(t["exit_t"]/1000,UTC).astimezone(KSA).strftime("%Y-%m-%d %H:%M")
        results.append(t)
    output={"period":"2026-11-24; prices and setup definitions from Twelve Data/current Sumo code",
      "method":"Each rejected sell replayed independently; this intentionally ignores the one-direction lock for comparison. PnL uses .01 lot and $0.37 slippage per fill.",
      "opposite_buy_candidates":[{"time":datetime.fromtimestamp(o["entry_t"]/1000,UTC).astimezone(KSA).strftime("%H:%M"),"price":o["price"]} for o in opposite_times],
      "trades":results}
    out=Path("research/results_sumo_four_candidates_2026-11-24");out.mkdir(parents=True,exist_ok=True)
    (out/"analysis.json").write_text(json.dumps(output,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(output,ensure_ascii=False))
    print("FOUR_CANDIDATE_REPLAY_READY")

if __name__=="__main__": main()
