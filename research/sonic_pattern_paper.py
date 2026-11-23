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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import rsi_secondary

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
OBSERVATION_MINUTES = 60  # Legacy research horizons; positions close only on reversal.
SLIPPAGE_PER_FILL_USD = 0.37
PAPER_LOT = 0.01
CONTRACT_OUNCES_PER_LOT = 100
SAUDI = timezone(timedelta(hours=3))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "SonicPaperResearch/1.0"})

TRADE_COLUMNS = [
    "trade_id", "side", "status", "sweep_at_utc", "ifvg_at_utc",
    "cisd_at_utc", "entry_at_utc", "entry_price",
    "opposite_signal_at_utc", "opposite_signal_price", "observation_minutes",
    "stop_price", "initial_risk_usd", "partial_taken", "exit_reason", "net_pnl_usd",
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
STEP_EXIT_PATH = ROOT / "exit_rule_step_resistance.csv"
STEP_EXIT_COLUMNS = [
    "trade_id", "side", "entry_at_utc", "entry_price", "initial_stop",
    "last_stop", "broken_levels_count", "broken_levels", "status",
    "exit_at_utc", "minutes_from_entry", "exit_close", "exit_move_usd",
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



# SUMO_M15_GATE_V1: PRIMARY MANDATORY entry conditions for the three-trading-day paper trial.
# Entry requires the core Sweep -> IFVG -> CISD sequence AND aligned EMA20/50, EMA200, and Ichimoku M15 gates.
# Calculations use only completed M15 candles, to avoid future data leakage.
GATE_VERSION = "institutional_sweep_atr_ifvg_mss_m15_gates_v2_all_hours"
GATE_INTERVAL_MS = 15 * 60 * 1000
_Gate_Cache = {"target": None, "at": 0.0, "bars": None}


def _gate_m15_history(entry_t_ms):
    """Fetch 500 completed M15 candles only when a new filter period needs them."""
    target = (entry_t_ms // GATE_INTERVAL_MS - 1) * GATE_INTERVAL_MS
    now = time.monotonic()
    if _Gate_Cache["target"] == target and _Gate_Cache["bars"] is not None:
        return _Gate_Cache["bars"], None
    if _Gate_Cache["target"] == target and now - _Gate_Cache["at"] < 60:
        return None, "history_retry_wait"
    _Gate_Cache.update({"target": target, "at": now, "bars": None})
    try:
        response = SESSION.get(
            f"{BASE_URL}/time_series",
            params={
                "symbol": SYMBOL, "interval": "15min",
                "outputsize": 500, "timezone": "UTC",
                "order": "ASC", "format": "JSON", "apikey": API_KEY,
            },
            timeout=12,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "error":
            raise RuntimeError("Twelve_Data_technical_history_error")
        values = data.get("values")
        if not isinstance(values, list):
            raise RuntimeError("Twelve_Data_missing_values")
        history = []
        for row in values:
            start_ms = int(parse_time(row["datetime"]).timestamp() * 1000)
            if start_ms + GATE_INTERVAL_MS > entry_t_ms:
                continue
            history.append({
                "t": start_ms,
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
            })
        history.sort(key=lambda bar: bar["t"])
        if len(history) < 350:
            raise RuntimeError("Not_enough_M15_candles_for_EMA200")
        if history[-1]["t"] != target:
            raise RuntimeError("Latest_completed_M15_candle_is_missing")
        _Gate_Cache["bars"] = history
        return history, None
    except Exception as exc:
        # Do not print exception messages: HTTP exceptions can include API keys.
        print("SUMO_M15_HISTORY_ERROR:", type(exc).__name__)
        return None, type(exc).__name__


def _gate_ema(values, length):
    value = sum(values[:length]) / length
    weight = 2.0 / (length + 1.0)
    for price in values[length:]:
        value += (price - value) * weight
    return value


def _gate_midpoint(bars, end_index, lookback):
    subset = bars[end_index - lookback + 1:end_index + 1]
    return (max(bar["h"] for bar in subset) +
            min(bar["l"] for bar in subset)) / 2.0


def indicator_entry_gate(entry_t_ms, entry_price, side):
    """All three selected indicators are mandatory entry gates (M15)."""
    bars, error = _gate_m15_history(entry_t_ms)
    if bars is None:
        return {
            "allowed": False, "version": GATE_VERSION,
            "reason": "indicator_data_unavailable", "error_type": error,
            "conditions": {"ema20_50": False, "ema200": False, "ichimoku": False},
        }
    closes = [bar["c"] for bar in bars]
    ema20 = _gate_ema(closes, 20)
    ema50 = _gate_ema(closes, 50)
    ema200 = _gate_ema(closes, 200)

    # Ichimoku projected cloud at current bar: spans were calculated 26 M15
    # bars earlier. Using current-bar spans would introduce look-ahead bias.
    origin = len(bars) - 1 - 26
    tenkan_then = _gate_midpoint(bars, origin, 9)
    kijun_then = _gate_midpoint(bars, origin, 26)
    senkou_a = (tenkan_then + kijun_then) / 2.0
    senkou_b = _gate_midpoint(bars, origin, 52)
    cloud_top = max(senkou_a, senkou_b)
    cloud_bottom = min(senkou_a, senkou_b)

    if side == "buy":
        conditions = {
            "ema20_50": ema20 > ema50,
            "ema200": entry_price > ema200,
            "ichimoku": entry_price > cloud_top,
        }
    else:
        conditions = {
            "ema20_50": ema20 < ema50,
            "ema200": entry_price < ema200,
            "ichimoku": entry_price < cloud_bottom,
        }
    return {
        "allowed": all(conditions.values()), "version": GATE_VERSION,
        "reason": "passed" if all(conditions.values()) else "indicator_conditions_failed",
        "conditions": conditions,
        "m15_last_closed_t": bars[-1]["t"],
        "m15_last_closed_price": round(closes[-1], 4),
        "ema20": round(ema20, 4), "ema50": round(ema50, 4),
        "ema200": round(ema200, 4),
        "ichimoku_span_a": round(senkou_a, 4),
        "ichimoku_span_b": round(senkou_b, 4),
        "cloud_top": round(cloud_top, 4),
        "cloud_bottom": round(cloud_bottom, 4),
    }


def aggregate_timeframe(candles, minutes):
    width = minutes * 60_000
    buckets = {}
    for bar in candles:
        start = (bar["t"] // width) * width
        buckets.setdefault(start, []).append(bar)
    result = []
    for start, rows in sorted(buckets.items()):
        rows = sorted(rows, key=lambda x: x["t"])
        expected = [start + i * 60_000 for i in range(minutes)]
        if [x["t"] for x in rows] != expected:
            continue
        result.append({
            "t": start, "end_t": start + width,
            "o": rows[0]["o"], "h": max(x["h"] for x in rows),
            "l": min(x["l"] for x in rows), "c": rows[-1]["c"],
        })
    return result


def is_news_blackout(timestamp_ms):
    raw = os.environ.get("SUMO_NEWS_EVENTS_UTC", "").strip()
    if not raw:
        return False
    current = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)
    for value in raw.split(","):
        try:
            event = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if event.tzinfo is None:
                event = event.replace(tzinfo=timezone.utc)
            if abs((current - event.astimezone(timezone.utc)).total_seconds()) <= 30 * 60:
                return True
        except ValueError:
            print("Ignoring invalid SUMO_NEWS_EVENTS_UTC timestamp")
    return False


def detect_sweeps(candles):
    """Detect only confirmed M15/H1 swing sweeps; M1 five-bar noise is excluded."""
    events = []
    for minutes in (15, 60):
        htf = aggregate_timeframe(candles, minutes)
        width = minutes * 60_000
        levels = []
        for i in range(2, len(htf) - 2):
            pivot = htf[i]
            if pivot["h"] > max(x["h"] for x in htf[i-2:i] + htf[i+1:i+3]):
                levels.append(("high", pivot["h"], htf[i+2]["end_t"], "M15" if minutes == 15 else "H1"))
            if pivot["l"] < min(x["l"] for x in htf[i-2:i] + htf[i+1:i+3]):
                levels.append(("low", pivot["l"], htf[i+2]["end_t"], "M15" if minutes == 15 else "H1"))
        for side, level, confirmed_t, timeframe in levels:
            for bar in candles:
                if bar["t"] < confirmed_t:
                    continue
                swept_low = side == "low" and bar["l"] < level and bar["c"] > level
                swept_high = side == "high" and bar["h"] > level and bar["c"] < level
                if swept_low or swept_high:
                    events.append({
                        "t": bar["t"], "direction": "buy" if swept_low else "sell",
                        "level": level, "extreme": bar["l"] if swept_low else bar["h"],
                        "timeframe": timeframe,
                    })
    # Keep only the earliest touch per direction/level to prevent repeated alerts.
    unique = {}
    for event in events:
        key = (event["direction"], round(event["level"], 4), event["timeframe"])
        unique.setdefault(key, event)
    return sorted(unique.values(), key=lambda item: item["t"])

def atr_at(candles, index, length=14):
    start = max(1, index - length + 1)
    ranges = []
    for i in range(start, index + 1):
        bar, prev = candles[i], candles[i - 1]
        ranges.append(max(bar["h"] - bar["l"], abs(bar["h"] - prev["c"]), abs(bar["l"] - prev["c"])))
    return statistics.mean(ranges) if ranges else 0.0


def detect_ifvgs(candles):
    events = []
    max_bars = MAX_SEQUENCE_MINUTES
    for i in range(2, len(candles) - 1):
        first, third = candles[i - 2], candles[i]
        if third["l"] > first["h"]:
            lower, upper, inverted_direction = first["h"], third["l"], "sell"
        elif third["h"] < first["l"]:
            lower, upper, inverted_direction = third["h"], first["l"], "buy"
        else:
            continue
        gap_size = upper - lower
        if gap_size < 0.4 * atr_at(candles, i):
            continue
        stop = min(len(candles), i + max_bars + 1)
        for j in range(i + 1, stop):
            close = candles[j]["c"]
            inverted = (inverted_direction == "buy" and close > upper) or (
                inverted_direction == "sell" and close < lower
            )
            if inverted:
                events.append({
                    "t": candles[j]["t"], "direction": inverted_direction,
                    "fvg_at": candles[i]["t"], "lower": lower, "upper": upper,
                    "size": gap_size, "atr": atr_at(candles, i),
                })
                break
    return events

def detect_cisds(candles):
    """MSS confirmation: strong M1 body closes through a confirmed local swing."""
    events = []
    for i in range(10, len(candles)):
        bar = candles[i]
        previous = candles[i - 10:i]
        body = abs(bar["c"] - bar["o"])
        threshold = max(0.05, 1.3 * statistics.median(abs(x["c"] - x["o"]) for x in previous))
        if body < threshold or i < 4:
            continue
        left, right = candles[i - 4:i - 2], candles[i - 2:i]
        swing_high = max(x["h"] for x in left + right)
        swing_low = min(x["l"] for x in left + right)
        if bar["c"] > bar["o"] and bar["c"] > swing_high:
            events.append({"t": bar["t"], "direction": "buy", "mss_level": swing_high, "body": body})
        elif bar["c"] < bar["o"] and bar["c"] < swing_low:
            events.append({"t": bar["t"], "direction": "sell", "mss_level": swing_low, "body": body})
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
            "sweep_timeframe": sweep.get("timeframe", "M15"),
            "mss_level": cisd.get("mss_level"),
            "ifvg_size": ifvg.get("size"),
            "ifvg_atr": ifvg.get("atr"),
            "ifvg_t": ifvg["t"],
            "ifvg_origin_t": ifvg["fvg_at"],
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
        status = getattr(getattr(exc, "response", None), "status_code", None)
        description = "Telegram request failed"
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                description = str(response.json().get("description", description))
            except Exception:
                pass
        print("Telegram paper-monitor notification error:", status or type(exc).__name__, description[:180])
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
        "opposite_signal_at_utc": iso(trade["opposite_signal_t"]) if trade.get("opposite_signal_t") else "",
        "opposite_signal_price": round(trade["opposite_signal_price"], 4) if trade.get("opposite_signal_price") is not None else "",
        "stop_price": round(trade.get("stop_price", 0), 4),
        "initial_risk_usd": round(trade.get("risk_usd", 0), 4),
        "partial_taken": trade.get("partial_taken", False),
        "exit_reason": trade.get("exit_reason", ""),
        "net_pnl_usd": trade.get("net_pnl_usd", ""),
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
        f"التسلسل: Sweep {trade.get('sweep_timeframe', 'M15')} → IFVG (≥0.4×ATR) → MSS خلال 30 دقيقة\n"
        f"وقف افتراضي: {trade.get('stop_price', 0):.2f} · المخاطرة: {trade.get('risk_usd', 0):.2f}$\n"
        f"{rsi_secondary.secondary_rsi_message(trade.get('rsi_secondary', {}))}"
        "هذا تسجيل بحثي؛ لا توجد صفقة حقيقية أو أمر للوسيط."
    )


def opposite_exit_message(trade, signal):
    original_side = "شراء" if trade["direction"] == "buy" else "بيع"
    opposite_side = "شراء" if signal["direction"] == "buy" else "بيع"
    direction = 1 if trade["direction"] == "buy" else -1
    move = (signal["entry_price"] - trade["entry_price"]) * direction
    return (
        "🚨 إنذار خروج افتراضي من صفقة سمو\n"
        f"الصفقة القائمة: {original_side} · الدخول: {trade['entry_price']:.2f}\n"
        f"ظهرت إشارة معاكسة: {opposite_side}\n"
        f"سعر الخروج الافتراضي: {signal['entry_price']:.2f}\n"
        f"وقت الإشارة المعاكسة: {fmt_saudi(signal['entry_t'])} السعودية\n"
        f"الحركة عند الإشارة: {move:+.2f}$\n"
        f"صافي الربح/الخسارة بعد الانزلاق: {trade.get('net_pnl_usd', 0):+.2f}$ (0.01 لوت)\n"
        "هذا إنذار بحثي فقط، ولا ينفذ أمرًا لدى الوسيط."
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
        "📋 انتهت الصفقة الافتراضية لسمو\n"
        f"الاتجاه: {side} · الدخول: {trade['entry_price']:.2f}\n"
        f"أقصى ابتعاد لصالحها: {trade['max_favorable']:.2f}$\n"
        f"أقصى ابتعاد ضدها: {trade['max_adverse']:.2f}$\n"
        f"أقصى انعكاس قبل العودة للإغلاق عند/فوق الدخول: {before_text}\n"
        f"سبب الخروج: {trade.get('exit_reason', 'إشارة معاكسة')} · الصافي: {trade.get('net_pnl_usd', 0):+.2f}$\n"
        f"الخلاصة: {recover}\n"
        "حساب افتراضي 0.01 لوت؛ احتُسب انزلاق 0.37$ لكل تنفيذ، دون العمولة والسبريد."
    )


def process_trade(trade, candles):
    if trade["status"] != "open":
        return []
    entry_t = trade["entry_t"]
    end_t = trade.get("opposite_signal_t") or float("inf")
    last_path_t = trade.get("last_path_t", entry_t - 60_000)
    new_path = []
    direction = 1 if trade["direction"] == "buy" else -1
    for bar in candles:
        if not (entry_t <= bar["t"] < end_t) or bar["t"] <= last_path_t:
            continue
        favorable, adverse, close_pnl = excursion(bar, trade["entry_price"], direction)
        managed = trade.get("management_version") == "mtf_sl_1r_2r_m5_v1"
        risk = max(float(trade.get("risk_usd", 0.4)), 0.01) if managed else 1e99
        stop = float(trade.get("stop_price", trade["entry_price"] - direction * risk))
        stop_hit = managed and ((bar["l"] <= stop) if direction == 1 else (bar["h"] >= stop))
        # If OHLC touches both a stop and a target in one minute, assume the stop came first.
        if stop_hit:
            remaining = float(trade.get("remaining_fraction", 1.0))
            gross = (stop - trade["entry_price"]) * direction
            trade["net_pnl_usd"] = round(float(trade.get("realized_pnl_usd", 0.0)) +
                (gross - 2 * SLIPPAGE_PER_FILL_USD) * PAPER_LOT * CONTRACT_OUNCES_PER_LOT * remaining, 2)
            trade["exit_t"] = bar["t"] + 60_000
            trade["exit_price"] = stop
            trade["exit_reason"] = "stop_loss" if abs(stop - trade["entry_price"]) > 0.001 else "break_even_or_trailing_stop"
            trade["status"] = "complete"
            trade["completed_t"] = trade["exit_t"]
            trade["last_path_t"] = bar["t"]
            break

        if managed and favorable >= risk:
            improved = trade["entry_price"]
            if (direction == 1 and improved > stop) or (direction == -1 and improved < stop):
                trade["stop_price"] = improved
                trade["break_even_armed"] = True
                stop = improved

        if managed and favorable >= 2 * risk and not trade.get("partial_taken"):
            trade["realized_pnl_usd"] = round(float(trade.get("realized_pnl_usd", 0.0)) +
                (2 * risk - 2 * SLIPPAGE_PER_FILL_USD) * PAPER_LOT * CONTRACT_OUNCES_PER_LOT * 0.5, 2)
            trade["remaining_fraction"] = 0.5
            trade["partial_taken"] = True
            trade["partial_notified"] = False
            trade["stop_price"] = trade["entry_price"]
            stop = trade["entry_price"]

        if managed and trade.get("partial_taken"):
            m5 = aggregate_timeframe([x for x in candles if x["t"] < bar["t"]], 5)
            if len(m5) >= 5:
                last = len(m5) - 1
                for p in range(2, last - 1):
                    pivot = m5[p]
                    if direction == 1 and pivot["l"] < min(x["l"] for x in m5[p-2:p] + m5[p+1:p+3]):
                        if pivot["end_t"] <= bar["t"] and pivot["l"] > float(trade.get("stop_price", trade["entry_price"])):
                            trade["stop_price"] = pivot["l"]
                    elif direction == -1 and pivot["h"] > max(x["h"] for x in m5[p-2:p] + m5[p+1:p+3]):
                        if pivot["end_t"] <= bar["t"] and pivot["h"] < float(trade.get("stop_price", trade["entry_price"])):
                            trade["stop_price"] = pivot["h"]

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
    if trade.get("opposite_signal_t") is not None and latest_close_ms >= end_t:
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
    """Paper exit: use a valid confirmed M5 pivot, then trail structural pivots."""
    bars = aggregate_closed_5m(candles, now)
    out = []
    horizon_ms = OBSERVATION_MINUTES * 60_000
    by_time = {bar["t"]: i for i, bar in enumerate(candles)}

    for trade in state.get("trades", []):
        direction = 1 if trade["direction"] == "buy" else -1
        entry_t = trade["entry_t"]
        entry_price = trade["entry_price"]
        pre = [b for b in bars if b["end_t"] <= entry_t]
        initial_level = None
        initial_type = ""

        # Choose the latest confirmed pivot on the protective side of entry
        # that had not already been closed through before entry.
        for confirm_i in range(4, len(pre)):
            pivot_i = confirm_i - 2
            if direction == 1 and is_pivot_low(pre, pivot_i):
                level = pre[pivot_i]["l"]
                valid_side = level < entry_price
                invalidated = any(b["c"] < level for b in pre[confirm_i + 1:])
                if valid_side and not invalidated:
                    initial_level = level
                    initial_type = "last_confirmed_m5_support"
            elif direction == -1 and is_pivot_high(pre, pivot_i):
                level = pre[pivot_i]["h"]
                valid_side = level > entry_price
                invalidated = any(b["c"] > level for b in pre[confirm_i + 1:])
                if valid_side and not invalidated:
                    initial_level = level
                    initial_type = "last_confirmed_m5_resistance"

        # If no valid M5 pivot exists, fall back to the swept candle's extreme
        # only when price has not closed through it since the sweep.
        if initial_level is None:
            sweep_index = by_time.get(trade["sweep_t"])
            if sweep_index is not None:
                sweep_level = candles[sweep_index]["l"] if direction == 1 else candles[sweep_index]["h"]
            else:
                sweep_level = trade.get("sweep_extreme")
            if sweep_level is not None:
                sweep_level = float(sweep_level)
                entry_index = by_time.get(entry_t, len(candles))
                side_valid = sweep_level < entry_price if direction == 1 else sweep_level > entry_price
                post_sweep = candles[sweep_index + 1:entry_index] if sweep_index is not None else []
                crossed = any(
                    (b["c"] < sweep_level if direction == 1 else b["c"] > sweep_level)
                    for b in post_sweep
                )
                if side_valid and not crossed:
                    initial_level = sweep_level
                    initial_type = "sweep_extreme_fallback"

        if initial_level is None:
            out.append({
                "trade_id": trade["trade_id"], "side": trade["direction"],
                "entry_at_utc": iso(entry_t), "entry_price": round(entry_price, 4),
                "initial_level_type": "", "initial_level": "",
                "last_trailing_level": "", "status": "no_valid_structure_at_entry",
                "exit_at_utc": "", "minutes_from_entry": "", "exit_close": "",
                "exit_move_usd": "", "reason": "لا يوجد دعم/مقاومة M5 صالح تحت/فوق الدخول",
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

            # Promote only confirmed higher lows for longs / lower highs for shorts.
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
                "exit_move_usd": "", "reason": "لم يُكسر المستوى البنيوي خلال الرصد",
                "pnl_at_60m_usd": round(last_close_pnl, 4) if completed and last_close_pnl is not None else "",
                "observed_m5_bars": observed,
            })
    write_csv(M5_EXIT_PATH, M5_EXIT_COLUMNS, out)


def evaluate_step_resistance_exit_rule(state, candles, now):
    """Step trailing: a confirmed M5 resistance/support break ratchets the stop."""
    bars = aggregate_closed_5m(candles, now)
    by_time = {bar["t"]: i for i, bar in enumerate(candles)}
    output = []
    horizon_ms = OBSERVATION_MINUTES * 60_000

    for trade in state.get("trades", []):
        direction = 1 if trade["direction"] == "buy" else -1
        entry_t = trade["entry_t"]
        entry = trade["entry_price"]
        pre = [b for b in bars if b["end_t"] <= entry_t]
        initial_stop = None

        # Initial invalidation uses the latest intact M5 pivot on the protective side.
        for confirm_i in range(4, len(pre)):
            pivot_i = confirm_i - 2
            if direction == 1 and is_pivot_low(pre, pivot_i):
                level = pre[pivot_i]["l"]
                if level < entry and not any(b["c"] < level for b in pre[confirm_i + 1:]):
                    initial_stop = level
            elif direction == -1 and is_pivot_high(pre, pivot_i):
                level = pre[pivot_i]["h"]
                if level > entry and not any(b["c"] > level for b in pre[confirm_i + 1:]):
                    initial_stop = level

        if initial_stop is None:
            si = by_time.get(trade["sweep_t"])
            if si is not None:
                level = candles[si]["l"] if direction == 1 else candles[si]["h"]
                side_ok = level < entry if direction == 1 else level > entry
                if side_ok:
                    initial_stop = level

        if initial_stop is None:
            output.append({
                "trade_id": trade["trade_id"], "side": trade["direction"],
                "entry_at_utc": iso(entry_t), "entry_price": round(entry, 4),
                "initial_stop": "", "last_stop": "", "broken_levels_count": 0,
                "broken_levels": "", "status": "no_valid_initial_structure",
                "exit_at_utc": "", "minutes_from_entry": "", "exit_close": "",
                "exit_move_usd": "", "pnl_at_60m_usd": "", "observed_m5_bars": 0,
            })
            continue

        stop = initial_stop
        broken_levels = []
        exit_bar = None
        observed = 0
        last_close_pnl = None
        end_t = entry_t + horizon_ms

        # Keep only confirmed, unbroken M5 levels beyond the entry as step candidates.
        resistance_levels = []
        support_levels = []
        for confirm_i in range(4, len(pre)):
            pivot_i = confirm_i - 2
            if direction == 1 and is_pivot_high(pre, pivot_i):
                level = pre[pivot_i]["h"]
                if level > entry and not any(b["c"] > level for b in pre[confirm_i + 1:]):
                    resistance_levels.append(level)
            elif direction == -1 and is_pivot_low(pre, pivot_i):
                level = pre[pivot_i]["l"]
                if level < entry and not any(b["c"] < level for b in pre[confirm_i + 1:]):
                    support_levels.append(level)

        first_bar = next((i for i,b in enumerate(bars) if b["end_t"] > entry_t), len(bars))
        for i in range(first_bar, len(bars)):
            bar = bars[i]
            if bar["end_t"] > end_t:
                break
            observed += 1
            last_close_pnl = (bar["c"] - entry) * direction

            crossed_stop = bar["c"] < stop if direction == 1 else bar["c"] > stop
            if crossed_stop:
                exit_bar = bar
                break

            if direction == 1:
                broken = [level for level in resistance_levels if stop < level < bar["c"]]
                if broken:
                    new_stop = max(broken)
                    if new_stop > stop:
                        stop = new_stop
                        broken_levels.append((new_stop, bar["end_t"]))
                resistance_levels = [level for level in resistance_levels if level > stop]
            else:
                broken = [level for level in support_levels if bar["c"] < level < stop]
                if broken:
                    new_stop = min(broken)
                    if new_stop < stop:
                        stop = new_stop
                        broken_levels.append((new_stop, bar["end_t"]))
                support_levels = [level for level in support_levels if level < stop]

            # New pivot levels only become usable after two M5 bars confirm them.
            pivot_i = i - 2
            if direction == 1 and is_pivot_high(bars, pivot_i):
                level = bars[pivot_i]["h"]
                if level > stop and level not in resistance_levels:
                    resistance_levels.append(level)
            elif direction == -1 and is_pivot_low(bars, pivot_i):
                level = bars[pivot_i]["l"]
                if level < stop and level not in support_levels:
                    support_levels.append(level)

        encoded_levels = ";".join(f"{level:.4f}@{iso(ts)}" for level, ts in broken_levels)
        if exit_bar:
            output.append({
                "trade_id": trade["trade_id"], "side": trade["direction"],
                "entry_at_utc": iso(entry_t), "entry_price": round(entry, 4),
                "initial_stop": round(initial_stop, 4), "last_stop": round(stop, 4),
                "broken_levels_count": len(broken_levels), "broken_levels": encoded_levels,
                "status": "exited_on_step_stop", "exit_at_utc": iso(exit_bar["end_t"]),
                "minutes_from_entry": round((exit_bar["end_t"] - entry_t) / 60_000, 2),
                "exit_close": round(exit_bar["c"], 4),
                "exit_move_usd": round((exit_bar["c"] - entry) * direction, 4),
                "pnl_at_60m_usd": "", "observed_m5_bars": observed,
            })
        else:
            complete = trade["status"] == "complete" and now.timestamp() * 1000 >= end_t
            output.append({
                "trade_id": trade["trade_id"], "side": trade["direction"],
                "entry_at_utc": iso(entry_t), "entry_price": round(entry, 4),
                "initial_stop": round(initial_stop, 4), "last_stop": round(stop, 4),
                "broken_levels_count": len(broken_levels), "broken_levels": encoded_levels,
                "status": "no_step_stop_in_60m" if complete else "monitoring",
                "exit_at_utc": "", "minutes_from_entry": "", "exit_close": "",
                "exit_move_usd": "", "pnl_at_60m_usd": round(last_close_pnl, 4) if complete and last_close_pnl is not None else "",
                "observed_m5_bars": observed,
            })
    write_csv(STEP_EXIT_PATH, STEP_EXIT_COLUMNS, output)


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


def main(candles_override=None, partial_current=False, live_signal_since=None):
    now = datetime.now(timezone.utc)
    candles = candles_override if candles_override is not None else load_candles(now)
    analysis_candles = candles[:-1] if partial_current else candles
    if not analysis_candles:
        raise RuntimeError("No completed candles available for analysis")
    ROOT.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    else:
        # Start prospectively; never create pretend entries from pre-install history.
        state = {
            "schema_version": 1,
            "started_at": now.isoformat(),
            "last_processed_candle_t": analysis_candles[-1]["t"],
            "trades": [],
            "initialized": True,
        }
        save_state(state)
        write_csv(TRADES_PATH, TRADE_COLUMNS, [])
        write_csv(PATH_PATH, PATH_COLUMNS, [])
        print("Paper monitor initialized prospectively at", iso(analysis_candles[-1]["t"]))
        return

    # Start the new directional regime prospectively; keep prior research records unchanged.
    if "last_direction" not in state:
        state["last_direction"] = None
        state["active_trade_id"] = None
    previous_last = int(state.get("last_processed_candle_t", 0))
    existing_ids = {t["trade_id"] for t in state.get("trades", [])}
    new_setups = sorted(detect_setups(analysis_candles), key=lambda item: item["cisd_t"])
    for setup in new_setups:
        if setup["cisd_t"] <= previous_last:
            continue
        if live_signal_since is not None and min(
            setup["sweep_t"], setup["ifvg_origin_t"], setup["ifvg_t"], setup["cisd_t"]
        ) < live_signal_since:
            continue
        cisd_index = next((i for i, x in enumerate(analysis_candles) if x["t"] == setup["cisd_t"]), None)
        if cisd_index is None or cisd_index + 1 >= len(candles):
            continue
        entry_bar = candles[cisd_index + 1]
        trade_id = f"{setup['direction']}-{setup['sweep_t']}-{setup['ifvg_t']}-{setup['cisd_t']}"
        if trade_id in existing_ids:
            continue

        # Record every original SMC candidate, whether it passes or fails
        # the three PRIMARY MANDATORY indicators. This preserves an A/B audit
        # without altering the instantaneous Telegram notification format.
        checked_ids = {r["trade_id"] for r in state.get("indicator_filter_checks", [])}
        if trade_id in checked_ids:
            continue
        indicator_check = indicator_entry_gate(
            entry_bar["t"], entry_bar["o"], setup["direction"]
        )
        rsi_study = rsi_secondary.evaluate_secondary_rsi(
            analysis_candles, entry_bar["t"], setup["direction"]
        )
        same_direction = state.get("last_direction") == setup["direction"]
        news_blocked = is_news_blackout(entry_bar["t"])
        cooldown_until = int(state.get("cooldown_until_t", 0) or 0)
        cooldown_blocked = entry_bar["t"] < cooldown_until
        proposed_stop = setup["sweep_extreme"] - 0.40 if setup["direction"] == "buy" else setup["sweep_extreme"] + 0.40
        stop_invalid = proposed_stop >= entry_bar["o"] if setup["direction"] == "buy" else proposed_stop <= entry_bar["o"]
        candidate = {
            "trade_id": trade_id,
            "entry_t": entry_bar["t"],
            "entry_price": round(entry_bar["o"], 5),
            "direction": setup["direction"],
            "sweep_t": setup["sweep_t"],
            "ifvg_t": setup["ifvg_t"],
            "cisd_t": setup["cisd_t"],
            "indicators": indicator_check,
            "rsi_secondary": rsi_study,
            "entry_requirements_class": "primary_mandatory_all_three_plus_smc",
            "blocked_same_direction": same_direction,
            "blocked_news": news_blocked,
            "blocked_cooldown": cooldown_blocked,
            "blocked_invalid_stop": stop_invalid,
            "entered": bool(indicator_check["allowed"] and not same_direction and not news_blocked and not cooldown_blocked and not stop_invalid),
        }
        checks = state.setdefault("indicator_filter_checks", [])
        checks.append(candidate)
        # Bound state growth; persist enough history for the short trial.
        if len(checks) > 3000:
            del checks[:-3000]
        save_state(state)
        print("SUMO_INDICATOR_GATE", json.dumps(candidate, ensure_ascii=False))
        # Primary mandatory indicators control ENTRY only. Original opposite SMC signals
        # must still close the current position, even if a new entry is rejected.
        if same_direction:
            continue

        opposite_trades = []
        for open_trade in state.get("trades", []):
            if open_trade.get("trade_id") != state.get("active_trade_id"):
                continue
            if open_trade.get("status") != "open" or open_trade.get("opposite_signal_t") is not None:
                continue
            if open_trade["direction"] == setup["direction"]:
                continue
            open_trade["opposite_signal_t"] = entry_bar["t"]
            open_trade["opposite_signal_price"] = entry_bar["o"]
            open_trade["opposite_signal_direction"] = setup["direction"]
            open_trade["opposite_signal_trade_id"] = trade_id
            open_trade["opposite_exit_notified"] = False
            direction = 1 if open_trade["direction"] == "buy" else -1
            price_move = (entry_bar["o"] - open_trade["entry_price"]) * direction
            open_trade["net_pnl_usd"] = round(
                float(open_trade.get("realized_pnl_usd", 0.0)) +
                (price_move - 2 * SLIPPAGE_PER_FILL_USD) * PAPER_LOT *
                CONTRACT_OUNCES_PER_LOT * float(open_trade.get("remaining_fraction", 1.0)), 2
            )
            open_trade["exit_reason"] = "opposite_signal"
            open_trade["exit_t"] = entry_bar["t"]
            open_trade["exit_price"] = entry_bar["o"]
            opposite_trades.append(open_trade)

        if not indicator_check["allowed"] or news_blocked or cooldown_blocked or stop_invalid:
            # Opposite SMC events still close existing paper positions; filters only block new entries.
            state["last_direction"] = setup["direction"]
            state["active_trade_id"] = None
            save_state(state)
            exit_context = {
                "direction": setup["direction"],
                "entry_t": entry_bar["t"],
                "entry_price": entry_bar["o"],
            }
            for old_trade in opposite_trades:
                if send_telegram(opposite_exit_message(old_trade, exit_context)):
                    old_trade["opposite_exit_notified"] = True
                    save_state(state)
            print("SUMO_GATE_EXIT_ONLY", trade_id, "news_blocked", news_blocked,
                  "cooldown_blocked", cooldown_blocked, "invalid_stop", stop_invalid,
                  "closed_positions", len(opposite_trades))
            continue

        trade = {
            **setup,
            "rsi_secondary": rsi_study,
            "trade_id": trade_id,
            "entry_t": entry_bar["t"],
            "entry_price": entry_bar["o"],
            "status": "open",
            "management_version": "mtf_sl_1r_2r_m5_v1",
            "stop_price": proposed_stop,
            "risk_usd": abs(entry_bar["o"] - proposed_stop),
            "remaining_fraction": 1.0,
            "realized_pnl_usd": 0.0,
            "partial_taken": False,
            "partial_notified": False,
            "break_even_armed": False,
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
        state["active_trade_id"] = trade_id
        state["last_direction"] = setup["direction"]
        existing_ids.add(trade_id)

        # Persist entry/exit events before sending so a failed Telegram call
        # can be retried on the next workflow run without duplicating entries.
        save_state(state)
        signal_context = {
            "direction": trade["direction"],
            "entry_t": trade["entry_t"],
            "entry_price": trade["entry_price"],
        }
        for open_trade in opposite_trades:
            if send_telegram(opposite_exit_message(open_trade, signal_context)):
                open_trade["opposite_exit_notified"] = True
                save_state(state)
        if send_telegram(entry_message(trade)):
            trade["entry_notified"] = True
            save_state(state)
        print("New paper entry:", trade_id, "at", trade["entry_price"])

    new_path_rows = []
    for trade in state.get("trades", []):
        new_path_rows.extend(process_trade(trade, analysis_candles))
        if trade.get("exit_reason") == "stop_loss" and trade.get("exit_t"):
            state["cooldown_until_t"] = max(
                int(state.get("cooldown_until_t", 0) or 0), int(trade["exit_t"] + 30 * 60_000)
            )
            state["last_direction"] = None
            if state.get("active_trade_id") == trade.get("trade_id"):
                state["active_trade_id"] = None
    state["last_processed_candle_t"] = max(previous_last, analysis_candles[-1]["t"])
    save_state(state)
    write_csv(TRADES_PATH, TRADE_COLUMNS, [trade_row(t) for t in state.get("trades", [])])
    if not PATH_PATH.exists():
        write_csv(PATH_PATH, PATH_COLUMNS, [])
    if new_path_rows:
        with PATH_PATH.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=PATH_COLUMNS, extrasaction="ignore")
            writer.writerows(new_path_rows)

    evaluate_exit_hypothesis(state)
    evaluate_structure_exit_rule(state, analysis_candles)
    evaluate_m5_structure_exit_rule(state, analysis_candles, now)
    evaluate_step_resistance_exit_rule(state, analysis_candles, now)

    for trade in state.get("trades", []):
        if trade.get("partial_taken") and not trade.get("partial_notified", False):
            partial_msg = (
                "🟡 سمو: تحقق هدف +2R، إغلاق افتراضي لنصف الصفقة ونقل الوقف إلى الدخول.\n"
                f"الدخول {trade['entry_price']:.2f} · الوقف الجديد {trade.get('stop_price', trade['entry_price']):.2f}\n"
                f"الربح المحقق جزئيًا بعد الانزلاق: {trade.get('realized_pnl_usd', 0):+.2f}$"
            )
            if send_telegram(partial_msg):
                trade["partial_notified"] = True
                save_state(state)
        if trade.get("opposite_signal_t") is not None and not trade.get("opposite_exit_notified", False):
            opposite_context = {
                "direction": trade["opposite_signal_direction"],
                "entry_t": trade["opposite_signal_t"],
                "entry_price": trade["opposite_signal_price"],
            }
            if send_telegram(opposite_exit_message(trade, opposite_context)):
                trade["opposite_exit_notified"] = True
                save_state(state)
        if not trade.get("entry_notified", False) and send_telegram(entry_message(trade)):
            trade["entry_notified"] = True
            save_state(state)
        if trade["status"] == "complete" and not trade["completion_notified"]:
            if send_telegram(completion_message(trade)):
                trade["completion_notified"] = True
                save_state(state)

    # Report the last completed Saudi calendar day, only once after a successful send.
    report_date = (now.astimezone(SAUDI) - timedelta(days=1)).date().isoformat()
    if state.get("last_daily_report_date") != report_date:
        closed = [
            t for t in state.get("trades", [])
            if t.get("exit_t") is not None
            and datetime.fromtimestamp(t["exit_t"] / 1000, SAUDI).date().isoformat() == report_date
            and isinstance(t.get("net_pnl_usd"), (float, int))
        ]
        buys = sum(t["direction"] == "buy" for t in closed)
        sells = sum(t["direction"] == "sell" for t in closed)
        profits = sum(max(0, t["net_pnl_usd"]) for t in closed)
        losses = sum(min(0, t["net_pnl_usd"]) for t in closed)
        net = profits + losses
        report = (
            f"📊 تقرير سمو اليومي | {report_date} السعودية\n"
            f"شراء مغلق: {buys} | بيع مغلق: {sells}\n"
            f"عدد الصفقات المغلقة: {len(closed)}\n"
            f"الأرباح: {profits:+.2f}$ | الخسائر: {losses:+.2f}$\n"
            f"الصافي بعد الانزلاق: {net:+.2f}$\n"
            "حساب افتراضي: 0.01 لوت، 100 أونصة/لوت، انزلاق 0.37$ لكل تنفيذ.\n"
            "الصفقة المفتوحة غير محسوبة ضمن الربح المحقق."
        )
        if send_telegram(report):
            state["last_daily_report_date"] = report_date
            save_state(state)

    print(
        "Paper monitor complete:",
        len(state.get("trades", [])), "entries;",
        sum(t["status"] == "open" for t in state.get("trades", [])), "still observed;",
        len(new_path_rows), "new path bars",
    )


if __name__ == "__main__":
    main()
