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
        "🧪 دخول وهمي جديد على الذهب XAU/USD\n"
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
        "📋 انتهت متابعة صفقة سونيك الوهمية (60 دقيقة)\n"
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
