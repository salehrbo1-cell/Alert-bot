#!/usr/bin/env python3
"""Research-only replay of the deployed Sumo strategy over KSA week 2026-11-16..20."""
import csv, json, os, sys, time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sonic_pattern_paper as strategy

KSA = timezone(timedelta(hours=3))
UTC = timezone.utc
START = datetime(2026, 11, 16, tzinfo=KSA)
END = datetime(2026, 11, 21, tzinfo=KSA)
FETCH_START = datetime(2026, 11, 11, tzinfo=KSA)
FETCH_END = END
API_KEY = os.environ["TWELVE_DATA_API_KEY"].strip()
BASE = "https://api.twelvedata.com/time_series"

def fetch_candles():
    session = requests.Session()
    rows_by_ts = {}
    chunk_start = FETCH_START
    while chunk_start < FETCH_END:
        chunk_end = min(chunk_start + timedelta(days=3), FETCH_END)
        params = {
            "symbol": "XAU/USD", "interval": "1min",
            "start_date": chunk_start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "Asia/Riyadh", "order": "ASC", "format": "JSON",
            "apikey": API_KEY,
        }
        response = session.get(BASE, params=params, timeout=45)
        data = response.json()
        if response.status_code >= 400 or data.get("status") == "error":
            raise RuntimeError("Twelve Data historical request failed; check API limits and date access.")
        for item in data.get("values", []):
            dt = datetime.strptime(item["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=KSA)
            ts = int(dt.astimezone(UTC).timestamp() * 1000)
            rows_by_ts[ts] = {"t": ts, "o": float(item["open"]), "h": float(item["high"]),
                              "l": float(item["low"]), "c": float(item["close"])}
        chunk_start = chunk_end
        time.sleep(2)
    candles = [rows_by_ts[t] for t in sorted(rows_by_ts)]
    if len(candles) < 3000:
        raise RuntimeError(f"Too few M1 candles returned ({len(candles)}); refusing a partial backtest.")
    return candles

def gate_for(entry_t, entry_price, side, m15):
    complete = [b for b in m15 if b["end_t"] <= entry_t][-500:]
    if len(complete) < 200:
        return {"allowed": False, "reason": "insufficient_m15_history",
                "conditions": {"ema20_50": False, "ema200": False, "ichimoku": False}}
    closes = [b["c"] for b in complete]
    ema20, ema50, ema200 = (strategy._gate_ema(closes, n) for n in (20, 50, 200))
    origin = len(complete) - 1 - 26
    tenkan = strategy._gate_midpoint(complete, origin, 9)
    kijun = strategy._gate_midpoint(complete, origin, 26)
    span_a = (tenkan + kijun) / 2
    span_b = strategy._gate_midpoint(complete, origin, 52)
    if side == "buy":
        conditions = {"ema20_50": ema20 > ema50, "ema200": entry_price > ema200,
                      "ichimoku": entry_price > max(span_a, span_b)}
    else:
        conditions = {"ema20_50": ema20 < ema50, "ema200": entry_price < ema200,
                      "ichimoku": entry_price < min(span_a, span_b)}
    return {"allowed": all(conditions.values()), "conditions": conditions}

def pnl_at(trade, price):
    direction = 1 if trade["side"] == "buy" else -1
    move = (price - trade["entry"]) * direction
    return round(trade["realized"] + (move - 2 * strategy.SLIPPAGE_PER_FILL_USD)
                 * strategy.PAPER_LOT * strategy.CONTRACT_OUNCES_PER_LOT * trade["remaining"], 2)

def close_trade(trade, ts, price, reason):
    trade["exit_t"] = ts
    trade["exit"] = price
    trade["reason"] = reason
    trade["pnl"] = pnl_at(trade, price)
    trade["status"] = "closed"

def manage_bar(trade, i, candles):
    bar = candles[i]
    direction = 1 if trade["side"] == "buy" else -1
    favorable = bar["h"] - trade["entry"] if direction == 1 else trade["entry"] - bar["l"]
    adverse = trade["entry"] - bar["l"] if direction == 1 else bar["h"] - trade["entry"]
    trade["mfe"] = max(trade["mfe"], favorable)
    trade["mae"] = max(trade["mae"], adverse)
    stop = trade["stop"]
    hit = bar["l"] <= stop if direction == 1 else bar["h"] >= stop
    if hit:
        close_trade(trade, bar["t"] + 60_000, stop,
                    "وقف خسارة" if abs(stop-trade["entry"]) > .001 else "تعادل/وقف متحرك")
        return
    if favorable >= trade["risk"]:
        if (direction == 1 and trade["entry"] > stop) or (direction == -1 and trade["entry"] < stop):
            trade["stop"] = trade["entry"]
    if favorable >= 2 * trade["risk"] and not trade["partial"]:
        trade["realized"] = round(trade["realized"] +
            (2 * trade["risk"] - 2 * strategy.SLIPPAGE_PER_FILL_USD)
            * strategy.PAPER_LOT * strategy.CONTRACT_OUNCES_PER_LOT * .5, 2)
        trade["remaining"] = .5
        trade["partial"] = True
        trade["stop"] = trade["entry"]
    if trade["partial"] and i > 0:
        window = candles[max(0, i-600):i]
        m5 = strategy.aggregate_timeframe(window, 5)
        if len(m5) >= 5:
            last = len(m5)-1
            for p in range(2, last-1):
                if direction == 1 and m5[p]["l"] < min(x["l"] for x in m5[p-2:p] + m5[p+1:p+3]):
                    if m5[p]["end_t"] <= bar["t"] and m5[p]["l"] > trade["stop"]:
                        trade["stop"] = m5[p]["l"]
                elif direction == -1 and m5[p]["h"] > max(x["h"] for x in m5[p-2:p] + m5[p+1:p+3]):
                    if m5[p]["end_t"] <= bar["t"] and m5[p]["h"] < trade["stop"]:
                        trade["stop"] = m5[p]["h"]

def main():
    candles = fetch_candles()
    m15 = strategy.aggregate_timeframe(candles, 15)
    start_ms, end_ms = int(START.timestamp()*1000), int(END.timestamp()*1000)
    # Reproduce the live 600-minute rolling window and run setup detection only on
    # bars whose strong-body MSS/CISD event is new.
    all_cisds = [e for e in strategy.detect_cisds(candles) if start_ms <= e["t"] < end_ms]
    candidates = []
    timestamp_index = {b["t"]: j for j, b in enumerate(candles)}
    for event in all_cisds:
        i = timestamp_index.get(event["t"])
        if i is None or i + 1 >= len(candles) or candles[i+1]["t"] >= end_ms:
            continue
        window = candles[max(0, i-599):i+1]
        setups = [s for s in strategy.detect_setups(window) if s["cisd_t"] == event["t"]]
        for setup in setups:
            candidates.append({"setup": setup, "entry_bar": candles[i+1], "signal_t": event["t"]})
    candidates.sort(key=lambda x: (x["entry_bar"]["t"], x["signal_t"]))
    by_entry = {}
    for c in candidates:
        by_entry.setdefault(c["entry_bar"]["t"], []).append(c)

    trades, candidate_rows = [], []
    active = None
    last_direction = None
    cooldown_until = 0
    checked = set()
    for i, bar in enumerate(candles):
        entry_events = by_entry.get(bar["t"], [])
        for c in entry_events:
            setup, entry = c["setup"], c["entry_bar"]
            tid = f'{setup["direction"]}-{setup["sweep_t"]}-{setup["ifvg_t"]}-{setup["cisd_t"]}'
            if tid in checked:
                continue
            checked.add(tid)
            side, price = setup["direction"], entry["o"]
            same = last_direction == side
            gate = gate_for(entry["t"], price, side, m15)
            news_blocked = False  # The deployed Railway variable is absent.
            cooldown = entry["t"] < cooldown_until
            stop = setup["sweep_extreme"] - .40 if side == "buy" else setup["sweep_extreme"] + .40
            invalid_stop = stop >= price if side == "buy" else stop <= price
            record = {"trade_id": tid, "signal_t": c["signal_t"], "entry_t": entry["t"],
                      "side": side, "entry": price, "sweep_tf": setup["sweep_timeframe"],
                      "sweep_t": setup["sweep_t"], "ifvg_t": setup["ifvg_t"],
                      "ifvg_size": setup["ifvg_size"], "ifvg_atr": setup["ifvg_atr"],
                      "mss_t": setup["cisd_t"], "mss_level": setup["mss_level"],
                      "filter_conditions": gate["conditions"], "same_direction_block": same,
                      "news_block": news_blocked, "cooldown_block": cooldown,
                      "invalid_stop": invalid_stop, "entered": False}
            if same:
                record["block_reason"] = "same_direction"
                candidate_rows.append(record)
                continue
            if active and active["side"] != side:
                close_trade(active, entry["t"], price, "إشارة معاكسة")
                active = None
            if not gate["allowed"] or news_blocked or cooldown or invalid_stop:
                last_direction = side
                record["block_reason"] = ("فلتر المؤشرات" if not gate["allowed"] else
                    "نافذة خبر" if news_blocked else "تهدئة 30 دقيقة" if cooldown else
                    "الوقف المقترح ليس خلف الدخول")
                candidate_rows.append(record)
                continue
            risk = abs(price-stop)
            if risk <= 0:
                record["block_reason"] = "مخاطرة صفرية"
                candidate_rows.append(record)
                continue
            active = {"trade_id": tid, "side": side, "entry": price, "entry_t": entry["t"],
                      "stop": stop, "risk": risk, "remaining": 1.0, "realized": 0.0,
                      "partial": False, "mfe": 0.0, "mae": 0.0, "status": "open",
                      "sweep_t": setup["sweep_t"], "ifvg_t": setup["ifvg_t"],
                      "signal_t": c["signal_t"], "sweep_tf": setup["sweep_timeframe"],
                      "filter_conditions": gate["conditions"]}
            trades.append(active)
            last_direction = side
            record["entered"] = True
            record["initial_stop"] = stop
            record["risk"] = risk
            candidate_rows.append(record)
        if active and active["status"] == "open" and bar["t"] >= active["entry_t"]:
            manage_bar(active, i, candles)
            if active["status"] == "closed":
                if active["reason"] == "وقف خسارة":
                    cooldown_until = max(cooldown_until, active["exit_t"] + 30*60_000)
                    last_direction = None
                active = None

    if active and active["status"] == "open":
        active["mark_price"] = candles[-1]["c"]
        active["unrealized_pnl"] = pnl_at(active, active["mark_price"])
    if len(candles) < 5000:
        raise RuntimeError("Historical response looks truncated; refusing to publish incomplete results.")

    accepted = [t for t in trades]
    closed = [t for t in accepted if t["status"] == "closed"]
    wins = [t for t in closed if t["pnl"] > .01]
    losses = [t for t in closed if t["pnl"] < -.01]
    breakeven = [t for t in closed if abs(t["pnl"]) <= .01]
    entered_ids = {t["trade_id"] for t in accepted}
    blocked = [c for c in candidate_rows if not c["entered"]]
    summary = {
        "period_ksa": ["2026-11-16 00:00", "2026-11-20 23:59"],
        "data_source": "Twelve Data XAU/USD M1, same symbol used by live worker",
        "m1_candles_loaded": len(candles),
        "candidate_setups": len(candidate_rows),
        "entered_trades": len(accepted),
        "blocked_candidates": len(blocked),
        "closed_trades": len(closed),
        "wins": len(wins), "losses": len(losses), "breakeven": len(breakeven),
        "open_at_period_end": len([t for t in accepted if t["status"] == "open"]),
        "net_closed_pnl_usd_at_0_01_lot": round(sum(t["pnl"] for t in closed), 2),
        "mean_closed_pnl_usd": round(sum(t["pnl"] for t in closed)/len(closed), 2) if closed else None,
        "win_rate_excluding_open_and_breakeven": round(100*len(wins)/(len(wins)+len(losses)), 1) if wins or losses else None,
        "max_favorable_mean": round(sum(t["mfe"] for t in accepted)/len(accepted), 2) if accepted else None,
        "max_adverse_mean": round(sum(t["mae"] for t in accepted)/len(accepted), 2) if accepted else None,
        "blocked_reasons": {},
        "assumptions": [
            "Clean state at Monday 00:00 KSA; 5 days of prior candles used only for indicator warmup.",
            "Uses deployed setup functions and 600-candle rolling window; all-hours mode.",
            "Intrabar order is unknowable from OHLC; stop is evaluated before new targets, as in the deployed manager.",
            "News blocking is off because SUMO_NEWS_EVENTS_UTC is not configured on Railway.",
            "This is paper replay, not a guarantee of future performance."
        ]
    }
    for c in blocked:
        reason=c.get("block_reason","unknown")
        summary["blocked_reasons"][reason]=summary["blocked_reasons"].get(reason,0)+1
    for t in trades:
        t["entry_ksa"] = datetime.fromtimestamp(t["entry_t"]/1000, UTC).astimezone(KSA).strftime("%Y-%m-%d %H:%M")
        if t.get("exit_t"):
            t["exit_ksa"] = datetime.fromtimestamp(t["exit_t"]/1000, UTC).astimezone(KSA).strftime("%Y-%m-%d %H:%M")
    for c in candidate_rows:
        c["entry_ksa"] = datetime.fromtimestamp(c["entry_t"]/1000, UTC).astimezone(KSA).strftime("%Y-%m-%d %H:%M")
        c["signal_ksa"] = datetime.fromtimestamp(c["signal_t"]/1000, UTC).astimezone(KSA).strftime("%Y-%m-%d %H:%M")
    out=Path("research/results_sumo_current_2026-11-16_to_20")
    out.mkdir(parents=True,exist_ok=True)
    (out/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (out/"trades.json").write_text(json.dumps(trades,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (out/"candidates.json").write_text(json.dumps(candidate_rows,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False))
    print("SUMO_BACKTEST_RESULT_READY")

if __name__=="__main__":
    main()
