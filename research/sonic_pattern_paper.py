#!/usr/bin/env python3
"""Paper monitor for a 30-minute Sweep -> IFVG -> CISD XAU/USD setup.

This script never places orders. It records a simulated entry at the next
one-minute candle open after a completed CISD candle, then follows the path
for 60 minutes.
"""
import csv
import json
import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API_KEY = os.environ["TWELVE_DATA_API_KEY"].strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
SYMBOL = "XAU/USD"
BASE_URL = "https://api.twelvedata.com"
ROOT = Path("research/sonic_paper")
STATE_PATH = ROOT / "state.json"
TRADES_PATH = ROOT / "trades.csv"
PATH_PATH = ROOT / "minute_path.csv"
MAX_SEQUENCE_MINUTES = 30
OBSERVATION_MINUTES = 60
SAUDI = timezone(timedelta(hours=3))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "SonicPaperResearch/1.0"})

TRADE_COLUMNS = [
    "trade_id", "side", "status", "sweep_at_utc", "ifvg_at_utc",
    "cisd_at_utc", "entry_at_utc", "entry_price", "observation_minutes",
    "max_favorable_usd", "max_adverse_usd", "adverse_close_seen",
    "returned_to_entry_after_adverse", "minutes_to_return",
    "adverse_before_return_usd", "adverse_on_return_candle_usd",
    "pnl_close_5m_usd", "pnl_close_15m_usd", "pnl_close_30m_usd",
    "pnl_close_60m_usd", "completed_at_utc",
]
PATH_COLUMNS = [
    "trade_id", "side", "timestamp_utc", "minutes_from_entry",
    "entry_price", "open", "high", "low", "close", "close_pnl_usd",
    "favorable_excursion_usd", "adverse_excursion_usd",
]
EXIT_RULE_PATH = ROOT / "exit_rule_tp20_sl5.csv"
EXIT_RULE_COLUMNS = [
    "trade_id", "side", "entry_at_utc", "entry_price", "rule",
    "status", "exit_at_utc", "minutes_from_entry", "exit_move_usd",
    "reason", "path_complete",
]
STRUCTURE_EXIT_PATH = ROOT / "exit_rule_structure_break.csv"
STRUCTURE_EXIT_COLUMNS = [
    "trade_id", "side", "entry_at_utc", "entry_price", "initial_level_type",
    "initial_level", "last_trailing_level", "status", "exit_at_utc",
    "minutes_from_entry", "exit_close", "exit_move_usd", "reason",
    "pnl_at_60m_usd", "observed_minutes",
]
M5_EXIT_PATH = ROOT / "exit_rule_m5_structure.csv"
M5_EXIT_COLUMNS = [
    "trade_id", "side", "entry_at_utc", "entry_price", "initial_level_type",
    "initial_level", "last_trailing_level", "status", "exit_at_utc",
    "minutes_from_entry", "exit_close", "exit_move_usd", "reason",
    "pnl_at_60m_usd", "observed_m5_bars",
]


def parse_time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def fmt_saudi(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(SAUDI).strftime("%Y-%m-%d %H:%M")


def load_candles(now):
    response = SESSION.get(
        f"{BASE_URL}/time_series",
        params={
            "symbol": SYMBOL,
            "interval": "1min",
            "outputsize": 500,
            "timezone": "UTC",
            "order": "ASC",
            "format": "JSON",
            "apikey": API_KEY,
        },
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("status") == "error":
        raise RuntimeError(f"Twelve Data error {data.get('code')}: {data.get('message')}")
    values = data.get("values")
    if not isinstance(values, list) or not values:
        raise RuntimeError("Twelve Data returned no one-minute candles")
    candles = []
    now_ms = int(now.timestamp() * 1000)
    for row in values:
        start = parse_time(row["datetime"])
        start_ms = int(start.timestamp() * 1000)
        # Only use completed bars; each bar's datetime is its opening time.
        if start_ms + 60_000 > now_ms:
            continue
        candles.append({
            "t": start_ms,
            "o": float(row["open"]),
            "h": float(row["high"]),
            "l": float(row["low"]),
            "c": float(row["close"]),
        })
    if len(candles) < 50:
        raise RuntimeError(f"Only {len(candles)} completed one-minute candles returned")
    return candles


def detect_sweeps(candles):
    events = []
    for i in range(5, len(candles)):
        prior = candles[i - 5:i]
        bar = candles[i]
        prior_high = max(x["h"] for x in prior)
        prior_low = min(x["l"] for x in prior)
        swept_low = bar["l"] < prior_low and bar["c"] > prior_low
        swept_high = bar["h"] > prior_high and bar["c"] < prior_high
        # Ignore an outside bar that sweeps both sides; direction is ambiguous.
        if swept_low != swept_high:
            events.append({
                "t": bar["t"],
                "direction": "buy" if swept_low else "sell",
                "level": prior_low if swept_low else prior_high,
                "extreme": bar["l"] if swept_low else bar["h"],
            })
    return events


def detect_ifvgs(candles):
    events = []
    max_bars = MAX_SEQUENCE_MINUTES
    for i in range(2, len(candles) - 1):
        first, third = candles[i - 2], candles[i]
        # Classical three-candle FVG, then its first close-through inversion.
        if third["l"] > first["h"]:
            lower, upper, inverted_direction = first["h"], third["l"], "sell"
        elif third["h"] < first["l"]:
            lower, upper, inverted_direction = third["h"], first["l"], "buy"
        else:
            continue
        stop = min(len(candles), i + max_bars + 1)
        for j in range(i + 1, stop):
            close = candles[j]["c"]
            inverted = (inverted_direction == "buy" and close > upper) or (
                inverted_direction == "sell" and close < lower
            )
            if inverted:
                events.append({
                    "t": candles[j]["t"],
                    "direction": inverted_direction,
                    "fvg_at": candles[i]["t"],
                    "lower": lower,
                    "upper": upper,
                })
                break
    return events


def detect_cisds(candles):
    events = []
    for i in range(10, len(candles)):
        bar = candles[i]
        previous = candles[i - 10:i]
        body = abs(bar["c"] - bar["o"])
        median_body = statistics.median(abs(x["c"] - x["o"]) for x in previous)
        threshold = max(0.05, 1.3 * median_body)
        if body < threshold:
            continue
        if bar["c"] > bar["o"]:
            direction = "buy"
            opposite = next((x for x in reversed(previous) if x["c"] < x["o"]), None)
            qualifies = opposite is not None and bar["c"] > opposite["o"]
        elif bar["c"] < bar["o"]:
            direction = "sell"
            opposite = next((x for x in reversed(previous) if x["c"] > x["o"]), None)
            qualifies = opposite is not None and bar["c"] < opposite["o"]
        else:
            continue
        if qualifies:
            events.append({"t": bar["t"], "direction": direction})
    return events


def detect_setups(candles):
    sweeps = detect_sweeps(candles)
    ifvgs = detect_ifvgs(candles)
    cisds = detect_cisds(candles)
    setups = []
    consumed_ifvgs = set()
    max_span_ms = MAX_SEQUENCE_MINUTES * 60_000
    for cisd in cisds:
        choices = []
        for ifvg in ifvgs:
            if ifvg["direction"] != cisd["direction"] or not (ifvg["t"] < cisd["t"]):
                continue
            if ifvg["t"] in consumed_ifvgs:
                continue
            for sweep in sweeps:
                if sweep["direction"] != cisd["direction"]:
                    continue
                if not (sweep["t"] < ifvg["t"] < cisd["t"]):
                    continue
                if cisd["t"] - sweep["t"] > max_span_ms:
                    continue
                choices.append((ifvg["t"], sweep["t"], ifvg, sweep))
        if not choices:
            continue
        # Use the latest valid chain and only its first subsequent CISD trigger.
        _, _, ifvg, sweep = max(choices, key=lambda x: (x[0], x[1]))
        consumed_ifvgs.add(ifvg["t"])
        setups.append({
            "direction": cisd["direction"],
            "sweep_t": sweep["t"],
            "sweep_level": sweep["level"],
            "sweep_extreme": sweep["extreme"],
            "ifvg_t": ifvg["t"],
            "ifvg_lower": ifvg["lower"],
            "ifvg_upper": ifvg["upper"],
            "cisd_t": cisd["t"],
        })
    return setups


def excursion(bar, entry, direction):
    signed_high = (bar["h"] - entry) * direction
    signed_low = (bar["l"] - entry) * direction
    favorable = max(0.0, signed_high, signed_low)
    adverse = max(0.0, -signed_high, -signed_low)
    close_pnl = (bar["c"] - entry) * direction
    return favorable, adverse, close_pnl


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram notification skipped: BOT_TOKEN/CHAT_ID unavailable")
        return False
    try:
        response = SESSION.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": message},
            timeout=20,
        )
        response.raise_for_status()
        if not response.json().get("ok"):
            raise RuntimeError("Telegram did not confirm delivery")
        return True
    except Exception as exc:
        print("Telegram paper-monitor notification error:", exc)
        return False


def save_state(state):
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def trade_row(trade):
    horizons = trade.get("horizons", {})
    return {
        "trade_id": trade["trade_id"],
        "side": trade["direction"],
        "status": trade["status"],
        "sweep_at_utc": iso(trade["sweep_t"]),
        "ifvg_at_utc": iso(trade["ifvg_t"]),
        "cisd_at_utc": iso(trade["cisd_t"]),
        "entry_at_utc": iso(trade["entry_t"]),
        "entry_price": round(trade["entry_price"], 4),
        "observation_minutes": OBSERVATION_MINUTES,
        "max_favorable_usd": round(trade["max_favorable"], 4),
        "max_adverse_usd": round(trade["max_adverse"], 4),
        "adverse_close_seen": trade["adverse_close_seen"],
        "returned_to_entry_after_adverse": trade.get("returned_after_adverse"),
        "minutes_to_return": trade.get("minutes_to_return"),
        "adverse_before_return_usd": trade.get("adverse_before_return"),
        "adverse_on_return_candle_usd": trade.get("adverse_on_return_candle"),
        "pnl_close_5m_usd": horizons.get("5", {}).get("close_pnl"),
        "pnl_close_15m_usd": horizons.get("15", {}).get("close_pnl"),
        "pnl_close_30m_usd": horizons.get("30", {}).get("close_pnl"),
        "pnl_close_60m_usd": horizons.get("60", {}).get("close_pnl"),
        "completed_at_utc": iso(trade["completed_t"]) if trade.get("completed_t") else "",
    }


def entry_message(trade):
    side = "شراء" if trade["direction"] == "buy" else "بيع"
    return (
        "🧪 دخول سمو الوهمي الجديد على الذهب XAU/USD\n"
        f"الاتجاه: {side}\n"
        f"سعر الدخول الافتراضي: {trade['entry_price']:.2f}\n"
        f"وقت الدخول: {fmt_saudi(trade['entry_t'])} السعودية\n"
        "التسلسل: Sweep → IFVG → CISD خلال 30 دقيقة\n"
        "هذا تسجيل بحثي؛ لا توجد صفقة حقيقية أو أمر للوسيط."
    )


def completion_message(trade):
    side = "شراء" if trade["direction"] == "buy" else "بيع"
    recover = "لم يعد إلى نقطة الدخول بعد حركة إغلاق معاكسة" if trade.get("returned_after_adverse") is False else (
        "لم تسجل إغلاقًا معاكسًا قبل العودة" if trade.get("returned_after_adverse") is None else
        f"عاد إلى/تجاوز نقطة الدخول بعد {trade.get('minutes_to_return')} دقيقة"
    )
    before = trade.get("adverse_before_return")
    before_text = "غير منطبق" if before is None else f"{before:.2f}$"
    return (
        "📋 انتهت متابعة صفقة سمو الوهمية (60 دقيقة)\n"
        f"الاتجاه: {side} · الدخول: {trade['entry_price']:.2f}\n"
        f"أقصى ابتعاد لصالحها: {trade['max_favorable']:.2f}$\n"
        f"أقصى ابتعاد ضدها: {trade['max_adverse']:.2f}$\n"
        f"أقصى انعكاس قبل العودة للإغلاق عند/فوق الدخول: {before_text}\n"
        f"الخلاصة: {recover}\n"
        "القيم من شموع الدقيقة، بلا احتساب سبريد أو عمولة."
    )


def process_trade(trade, candles):
    if trade["status"] != "open":
        return []
    entry_t = trade["entry_t"]
    end_t = entry_t + OBSERVATION_MINUTES * 60_000
    last_path_t = trade.get("last_path_t", entry_t - 60_000)
    new_path = []
    direction = 1 if trade["direction"] == "buy" else -1
    for bar in candles:
        if not (entry_t <= bar["t"] < end_t) or bar["t"] <= last_path_t:
            continue
        favorable, adverse, close_pnl = excursion(bar, trade["entry_price"], direction)
        trade["max_favorable"] = max(trade["max_favorable"], favorable)
        trade["max_adverse"] = max(trade["max_adverse"], adverse)
        relative_minute = int((bar["t"] - entry_t) / 60_000)
        for horizon in (5, 15, 30, 60):
            if relative_minute < horizon:
                item = trade["horizons"].setdefault(str(horizon), {})
                item["max_favorable"] = max(item.get("max_favorable", 0.0), favorable)
                item["max_adverse"] = max(item.get("max_adverse", 0.0), adverse)
                item["close_pnl"] = close_pnl
        # "Return" is defined from completed M1 closes after at least one
        # close against the entry direction. Intrabar order within a candle
        # cannot be recovered from OHLC, so that candle's adverse wick is
        # recorded separately.
        if trade["adverse_close_seen"] and close_pnl >= 0 and trade.get("returned_after_adverse") is None:
            trade["returned_after_adverse"] = True
            trade["minutes_to_return"] = relative_minute + 1
            trade["adverse_before_return"] = round(trade["adverse_before_return_running"], 4)
            trade["adverse_on_return_candle"] = round(adverse, 4)
        elif trade.get("returned_after_adverse") is None:
            if close_pnl < 0:
                trade["adverse_close_seen"] = True
            if trade["adverse_close_seen"]:
                trade["adverse_before_return_running"] = max(
                    trade.get("adverse_before_return_running", 0.0), adverse
                )
        new_path.append({
            "trade_id": trade["trade_id"],
            "side": trade["direction"],
            "timestamp_utc": iso(bar["t"]),
            "minutes_from_entry": relative_minute,
            "entry_price": round(trade["entry_price"], 4),
            "open": bar["o"], "high": bar["h"], "low": bar["l"], "close": bar["c"],
            "close_pnl_usd": round(close_pnl, 4),
            "favorable_excursion_usd": round(favorable, 4),
            "adverse_excursion_usd": round(adverse, 4),
        })
        trade["last_path_t"] = bar["t"]
    latest_close_ms = candles[-1]["t"] + 60_000
    if latest_close_ms >= end_t:
        trade["status"] = "complete"
        trade["completed_t"] = end_t
        if trade.get("returned_after_adverse") is None:
            trade["returned_after_adverse"] = False if trade["adverse_close_seen"] else None
            trade["minutes_to_return"] = None
            trade["adverse_before_return"] = (
                round(trade.get("adverse_before_return_running", 0.0), 4)
                if trade["adverse_close_seen"] else None
            )
            trade["adverse_on_return_candle"] = None
    return new_path


def evaluate_exit_hypothesis(state):
    """Track a paper-only fixed target/stop hypothesis against each recorded path."""
    grouped = {}
    if PATH_PATH.exists():
        with PATH_PATH.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                grouped.setdefault(row["trade_id"], []).append(row)

    output = []
    for trade in state.get("trades", []):
        bars = sorted(grouped.get(trade["trade_id"], []), key=lambda x: int(x["minutes_from_entry"]))
        outcome = None
        for bar in bars:
            favorable = float(bar["favorable_excursion_usd"])
            adverse = float(bar["adverse_excursion_usd"])
            if adverse >= 5.0 and favorable >= 20.0:
                outcome = (bar, -5.0, "أُصيبا في الدقيقة نفسها؛ افتراض محافظ: الوقف أولاً")
                break
            if adverse >= 5.0:
                outcome = (bar, -5.0, "وقف خسارة سعري −5$")
                break
            if favorable >= 20.0:
                outcome = (bar, 20.0, "هدف سعري +20$")
                break

        if outcome:
            bar, move, reason = outcome
            output.append({
                "trade_id": trade["trade_id"],
                "side": trade["direction"],
                "entry_at_utc": iso(trade["entry_t"]),
                "entry_price": round(trade["entry_price"], 4),
                "rule": "TP +20$ / SL −5$ / max 60m",
                "status": "exited",
                "exit_at_utc": bar["timestamp_utc"],
                "minutes_from_entry": int(bar["minutes_from_entry"]) + 1,
                "exit_move_usd": move,
                "reason": reason,
                "path_complete": trade["status"] == "complete",
            })
        elif trade["status"] == "complete" and bars:
            last = bars[-1]
            output.append({
                "trade_id": trade["trade_id"],
                "side": trade["direction"],
                "entry_at_utc": iso(trade["entry_t"]),
                "entry_price": round(trade["entry_price"], 4),
                "rule": "TP +20$ / SL −5$ / max 60m",
                "status": "timed_out",
                "exit_at_utc": last["timestamp_utc"],
                "minutes_from_entry": int(last["minutes_from_entry"]) + 1,
                "exit_move_usd": round(float(last["close_pnl_usd"]), 4),
                "reason": "إغلاق عند نهاية أفق الرصد 60 دقيقة",
                "path_complete": True,
            })
        else:
            output.append({
                "trade_id": trade["trade_id"],
                "side": trade["direction"],
                "entry_at_utc": iso(trade["entry_t"]),
                "entry_price": round(trade["entry_price"], 4),
                "rule": "TP +20$ / SL −5$ / max 60m",
                "status": "monitoring",
                "exit_at_utc": "",
                "minutes_from_entry": "",
                "exit_move_usd": "",
                "reason": "لم يُضرب الهدف أو الوقف بعد",
                "path_complete": False,
            })
    write_csv(EXIT_RULE_PATH, EXIT_RULE_COLUMNS, output)


def is_pivot_low(bars, i):
    if i < 2 or i + 2 >= len(bars):
        return False
    level = bars[i]["l"]
    neighbors = [x["l"] for x in bars[i - 2:i]] + [x["l"] for x in bars[i + 1:i + 3]]
    return level < min(neighbors)


def is_pivot_high(bars, i):
    if i < 2 or i + 2 >= len(bars):
        return False
    level = bars[i]["h"]
    neighbors = [x["h"] for x in bars[i - 2:i]] + [x["h"] for x in bars[i + 1:i + 3]]
    return level > max(neighbors)


def aggregate_closed_5m(candles, now):
    buckets = {}
    for bar in candles:
        start = (bar["t"] // 300_000) * 300_000
        buckets.setdefault(start, []).append(bar)
    now_ms = int(now.timestamp() * 1000)
    result = []
    for start, rows in sorted(buckets.items()):
        rows = sorted(rows, key=lambda x: x["t"])
        expected = [start + i * 60_000 for i in range(5)]
        if [x["t"] for x in rows] != expected or start + 300_000 > now_ms:
            continue
        result.append({
            "t": start, "end_t": start + 300_000,
            "o": rows[0]["o"], "h": max(x["h"] for x in rows),
            "l": min(x["l"] for x in rows), "c": rows[-1]["c"],
        })
    return result


def evaluate_m5_structure_exit_rule(state, candles, now):
    """Paper exit: close beyond confirmed M5 support/resistance; no fixed target."""
    bars = aggregate_closed_5m(candles, now)
    out = []
    horizon_ms = OBSERVATION_MINUTES * 60_000
    for trade in state.get("trades", []):
        direction = 1 if trade["direction"] == "buy" else -1
        entry_t = trade["entry_t"]
        entry_price = trade["entry_price"]
        pre = [b for b in bars if b["end_t"] <= entry_t]
        initial_type = "sweep_extreme"
        if direction == 1:
            initial_level = float(trade.get("sweep_extreme", trade["sweep_level"]))
        else:
            initial_level = float(trade.get("sweep_extreme", trade["sweep_level"]))
        last_confirm = None
        for confirm_i in range(4, len(pre)):
            pivot_i = confirm_i - 2
            if direction == 1 and is_pivot_low(pre, pivot_i):
                initial_level = pre[pivot_i]["l"]
                initial_type = "last_confirmed_m5_support"
                last_confirm = pre[confirm_i]["end_t"]
            elif direction == -1 and is_pivot_high(pre, pivot_i):
                initial_level = pre[pivot_i]["h"]
                initial_type = "last_confirmed_m5_resistance"
                last_confirm = pre[confirm_i]["end_t"]

        invalid_at = None
        if last_confirm is not None:
            for b in pre:
                if b["end_t"] <= last_confirm:
                    continue
                if (direction == 1 and b["c"] < initial_level) or (direction == -1 and b["c"] > initial_level):
                    invalid_at = b
                    break
        if invalid_at:
            out.append({
                "trade_id": trade["trade_id"], "side": trade["direction"],
                "entry_at_utc": iso(entry_t), "entry_price": round(entry_price, 4),
                "initial_level_type": initial_type, "initial_level": round(initial_level, 4),
                "last_trailing_level": round(initial_level, 4),
                "status": "invalidated_before_entry", "exit_at_utc": iso(invalid_at["end_t"]),
                "minutes_from_entry": "", "exit_close": round(invalid_at["c"], 4),
                "exit_move_usd": "", "reason": "إغلاق M5 تجاوز المستوى قبل الدخول؛ يُلغى الإعداد",
                "pnl_at_60m_usd": "", "observed_m5_bars": 0,
            })
            continue

        stop_level = initial_level
        exit_bar = None
        observed = 0
        end_t = entry_t + horizon_ms
        last_close_pnl = None
        first_bar = next((i for i,b in enumerate(bars) if b["end_t"] > entry_t), len(bars))
        for i in range(first_bar, len(bars)):
            bar = bars[i]
            if bar["end_t"] > end_t:
                break
            observed += 1
            last_close_pnl = (bar["c"] - entry_price) * direction
            crossed = bar["c"] < stop_level if direction == 1 else bar["c"] > stop_level
            if crossed:
                exit_bar = bar
                break

            pivot_i = i - 2
            if direction == 1 and is_pivot_low(bars, pivot_i):
                new_level = bars[pivot_i]["l"]
                if new_level > stop_level and new_level < bar["c"]:
                    stop_level = new_level
            elif direction == -1 and is_pivot_high(bars, pivot_i):
                new_level = bars[pivot_i]["h"]
                if new_level < stop_level and new_level > bar["c"]:
                    stop_level = new_level

        if exit_bar:
            out.append({
                "trade_id": trade["trade_id"], "side": trade["direction"],
                "entry_at_utc": iso(entry_t), "entry_price": round(entry_price, 4),
                "initial_level_type": initial_type, "initial_level": round(initial_level, 4),
                "last_trailing_level": round(stop_level, 4), "status": "exited_on_m5_structure",
                "exit_at_utc": iso(exit_bar["end_t"]),
                "minutes_from_entry": round((exit_bar["end_t"] - entry_t) / 60_000, 2),
                "exit_close": round(exit_bar["c"], 4),
                "exit_move_usd": round((exit_bar["c"] - entry_price) * direction, 4),
                "reason": "إغلاق M5 أسفل الدعم" if direction == 1 else "إغلاق M5 أعلى المقاومة",
                "pnl_at_60m_usd": "", "observed_m5_bars": observed,
            })
        else:
            completed = trade["status"] == "complete" and now.timestamp() * 1000 >= end_t
            out.append({
                "trade_id": trade["trade_id"], "side": trade["direction"],
                "entry_at_utc": iso(entry_t), "entry_price": round(entry_price, 4),
                "initial_level_type": initial_type, "initial_level": round(initial_level, 4),
                "last_trailing_level": round(stop_level, 4),
                "status": "no_break_in_60m" if completed else "monitoring",
                "exit_at_utc": "", "minutes_from_entry": "", "exit_close": "",
                "exit_move_usd": "", "reason": "لم يُكسر مستوى M5 خلال الرصد",
                "pnl_at_60m_usd": round(last_close_pnl, 4) if completed and last_close_pnl is not None else "",
                "observed_m5_bars": observed,
            })
    write_csv(M5_EXIT_PATH, M5_EXIT_COLUMNS, out)


def evaluate_structure_exit_rule(state, candles):
    """Paper exit: close beyond last confirmed M1 support/resistance, no fixed TP."""
    path_by_id = {}
    if PATH_PATH.exists():
        with PATH_PATH.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                path_by_id.setdefault(row["trade_id"], []).append(row)

    by_time = {bar["t"]: i for i, bar in enumerate(candles)}
    out = []
    horizon_ms = OBSERVATION_MINUTES * 60_000
    for trade in state.get("trades", []):
        direction = 1 if trade["direction"] == "buy" else -1
        entry_t = trade["entry_t"]
        entry_price = trade["entry_price"]
        entry_index = by_time.get(entry_t)
        if entry_index is None:
            out.append({
                "trade_id": trade["trade_id"], "side": trade["direction"],
                "entry_at_utc": iso(entry_t), "entry_price": round(entry_price, 4),
                "initial_level_type": "", "initial_level": "",
                "last_trailing_level": "", "status": "context_unavailable",
                "exit_at_utc": "", "minutes_from_entry": "", "exit_close": "",
                "exit_move_usd": "", "reason": "شموع ما قبل الدخول غير متاحة",
                "pnl_at_60m_usd": "", "observed_minutes": "",
            })
            continue

        sweep_index = by_time.get(trade["sweep_t"])
        initial_type = "sweep_extreme"
        if sweep_index is not None:
            initial_level = candles[sweep_index]["l"] if direction == 1 else candles[sweep_index]["h"]
        elif trade.get("sweep_extreme") is not None:
            initial_level = float(trade["sweep_extreme"])
        else:
            initial_level = float(trade["sweep_level"])

        # Use the most recent confirmed two-left/two-right pivot before entry.
        last_pivot_level = None
        last_pivot_confirm_index = None
        for confirm_i in range(max(4, entry_index - 120), entry_index):
            pivot_i = confirm_i - 2
            if direction == 1 and is_pivot_low(candles, pivot_i):
                last_pivot_level = candles[pivot_i]["l"]
                last_pivot_confirm_index = confirm_i
                initial_type = "last_confirmed_support"
            elif direction == -1 and is_pivot_high(candles, pivot_i):
                last_pivot_level = candles[pivot_i]["h"]
                last_pivot_confirm_index = confirm_i
                initial_type = "last_confirmed_resistance"
        if last_pivot_level is not None:
            initial_level = last_pivot_level

        # If the selected support/resistance had already been closed through
        # before the paper entry, this structure rule would have skipped entry.
        invalid_before = None
        for i in range((last_pivot_confirm_index or entry_index - 1) + 1, entry_index):
            crossed = candles[i]["c"] < initial_level if direction == 1 else candles[i]["c"] > initial_level
            if crossed:
                invalid_before = i
                break
        rows = sorted(path_by_id.get(trade["trade_id"], []), key=lambda x: int(x["minutes_from_entry"]))
        if invalid_before is not None:
            bar = candles[invalid_before]
            out.append({
                "trade_id": trade["trade_id"], "side": trade["direction"],
                "entry_at_utc": iso(entry_t), "entry_price": round(entry_price, 4),
                "initial_level_type": initial_type, "initial_level": round(initial_level, 4),
                "last_trailing_level": round(initial_level, 4),
                "status": "invalidated_before_entry",
                "exit_at_utc": iso(bar["t"]), "minutes_from_entry": "",
                "exit_close": round(bar["c"], 4), "exit_move_usd": "",
                "reason": "إغلاق تجاوز الدعم/المقاومة قبل تحقق الدخول؛ يُلغى الإعداد",
                "pnl_at_60m_usd": "", "observed_minutes": 0,
            })
            continue

        stop_level = initial_level
        exit_bar = None
        exit_reason = ""
        start_i = entry_index
        end_t = entry_t + horizon_ms
        last_close = None
        observed = 0
        for i in range(start_i, len(candles)):
            bar = candles[i]
            if bar["t"] >= end_t:
                break
            if bar["t"] < entry_t:
                continue
            observed += 1
            last_close = (bar["c"] - entry_price) * direction
            crossed = bar["c"] < stop_level if direction == 1 else bar["c"] > stop_level
            if crossed:
                exit_bar = bar
                exit_reason = "إغلاق دقيقة تحت الدعم" if direction == 1 else "إغلاق دقيقة فوق المقاومة"
                break

            # A pivot is only known after two full bars to its right have closed.
            pivot_i = i - 2
            if direction == 1 and is_pivot_low(candles, pivot_i):
                new_level = candles[pivot_i]["l"]
                if new_level > stop_level and new_level < bar["c"]:
                    stop_level = new_level
            elif direction == -1 and is_pivot_high(candles, pivot_i):
                new_level = candles[pivot_i]["h"]
                if new_level < stop_level and new_level > bar["c"]:
                    stop_level = new_level

        if exit_bar is not None:
            out.append({
                "trade_id": trade["trade_id"], "side": trade["direction"],
                "entry_at_utc": iso(entry_t), "entry_price": round(entry_price, 4),
                "initial_level_type": initial_type, "initial_level": round(initial_level, 4),
                "last_trailing_level": round(stop_level, 4), "status": "exited_on_structure",
                "exit_at_utc": iso(exit_bar["t"]),
                "minutes_from_entry": int((exit_bar["t"] - entry_t) / 60_000) + 1,
                "exit_close": round(exit_bar["c"], 4),
                "exit_move_usd": round((exit_bar["c"] - entry_price) * direction, 4),
                "reason": exit_reason, "pnl_at_60m_usd": "",
                "observed_minutes": observed,
            })
        else:
            status = "no_break_in_60m" if trade["status"] == "complete" else "monitoring"
            out.append({
                "trade_id": trade["trade_id"], "side": trade["direction"],
                "entry_at_utc": iso(entry_t), "entry_price": round(entry_price, 4),
                "initial_level_type": initial_type, "initial_level": round(initial_level, 4),
                "last_trailing_level": round(stop_level, 4), "status": status,
                "exit_at_utc": "", "minutes_from_entry": "", "exit_close": "",
                "exit_move_usd": "", "reason": "لم يُكسر المستوى البنيوي خلال الرصد",
                "pnl_at_60m_usd": round(last_close, 4) if trade["status"] == "complete" and last_close is not None else "",
                "observed_minutes": observed,
            })
    write_csv(STRUCTURE_EXIT_PATH, STRUCTURE_EXIT_COLUMNS, out)


def main():
    now = datetime.now(timezone.utc)
    candles = load_candles(now)
    ROOT.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    else:
        # Start prospectively; never create pretend entries from pre-install history.
        state = {
            "schema_version": 1,
            "started_at": now.isoformat(),
            "last_processed_candle_t": candles[-1]["t"],
            "trades": [],
            "initialized": True,
        }
        save_state(state)
        write_csv(TRADES_PATH, TRADE_COLUMNS, [])
        write_csv(PATH_PATH, PATH_COLUMNS, [])
        print("Paper monitor initialized prospectively at", iso(candles[-1]["t"]))
        return

    previous_last = int(state.get("last_processed_candle_t", 0))
    existing_ids = {t["trade_id"] for t in state.get("trades", [])}
    for setup in detect_setups(candles):
        if setup["cisd_t"] <= previous_last:
            continue
        cisd_index = next((i for i, x in enumerate(candles) if x["t"] == setup["cisd_t"]), None)
        if cisd_index is None or cisd_index + 1 >= len(candles):
            continue
        entry_bar = candles[cisd_index + 1]
        trade_id = f"{setup['direction']}-{setup['sweep_t']}-{setup['ifvg_t']}-{setup['cisd_t']}"
        if trade_id in existing_ids:
            continue
        trade = {
            **setup,
            "trade_id": trade_id,
            "entry_t": entry_bar["t"],
            "entry_price": entry_bar["o"],
            "status": "open",
            "max_favorable": 0.0,
            "max_adverse": 0.0,
            "adverse_close_seen": False,
            "adverse_before_return_running": 0.0,
            "returned_after_adverse": None,
            "minutes_to_return": None,
            "adverse_before_return": None,
            "adverse_on_return_candle": None,
            "last_path_t": entry_bar["t"] - 60_000,
            "horizons": {},
            "entry_notified": False,
            "completion_notified": False,
        }
        state.setdefault("trades", []).append(trade)
        existing_ids.add(trade_id)
        print("New paper entry:", trade_id, "at", trade["entry_price"])

    new_path_rows = []
    for trade in state.get("trades", []):
        new_path_rows.extend(process_trade(trade, candles))
    state["last_processed_candle_t"] = max(previous_last, candles[-1]["t"])
    save_state(state)
    write_csv(TRADES_PATH, TRADE_COLUMNS, [trade_row(t) for t in state.get("trades", [])])
    if not PATH_PATH.exists():
        write_csv(PATH_PATH, PATH_COLUMNS, [])
    if new_path_rows:
        with PATH_PATH.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=PATH_COLUMNS, extrasaction="ignore")
            writer.writerows(new_path_rows)

    evaluate_exit_hypothesis(state)
    evaluate_structure_exit_rule(state, candles)
    evaluate_m5_structure_exit_rule(state, candles, now)

    for trade in state.get("trades", []):
        if not trade["entry_notified"] and send_telegram(entry_message(trade)):
            trade["entry_notified"] = True
            save_state(state)
        if trade["status"] == "complete" and not trade["completion_notified"]:
            if send_telegram(completion_message(trade)):
                trade["completion_notified"] = True
                save_state(state)

    print(
        "Paper monitor complete:",
        len(state.get("trades", [])), "entries;",
        sum(t["status"] == "open" for t in state.get("trades", [])), "still observed;",
        len(new_path_rows), "new path bars",
    )


if __name__ == "__main__":
    main()
