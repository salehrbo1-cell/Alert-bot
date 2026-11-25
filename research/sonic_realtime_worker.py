#!/usr/bin/env python3
"""Continuous Twelve Data WebSocket worker for Sumo's completed-M1 strategy."""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import websocket

import sonic_pattern_paper as monitor

API_KEY = os.environ["TWELVE_DATA_API_KEY"].strip()
SYMBOL = monitor.SYMBOL
WS_URL = f"wss://ws.twelvedata.com/v1/quotes/price?apikey={API_KEY}"
MAX_BARS = 600
bars = []
current_bar = None
live_signal_since = None
state_lock = threading.RLock()
stop_event = threading.Event()


def minute_start(timestamp_ms):
    return timestamp_ms - timestamp_ms % 60_000


def send_heartbeat(ws):
    while not stop_event.wait(10):
        try:
            if ws.sock and ws.sock.connected:
                ws.send(json.dumps({"action": "heartbeat"}))
        except Exception as exc:
            print("WebSocket heartbeat failed:", type(exc).__name__)
            return


# Gold Momentum observer (buy side), persisted on Railway's /data volume.
from pathlib import Path
MOMENTUM_STATE_PATH = Path("/data/gold_momentum_observer.json")
MOMENTUM_WINDOW_MS = 22 * 60_000
momentum_state = {"armed": True, "pending": None}

def load_momentum_state():
    global momentum_state
    try:
        momentum_state = json.loads(MOMENTUM_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        momentum_state = {"armed": True, "pending": None}

def save_momentum_state():
    try:
        MOMENTUM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        MOMENTUM_STATE_PATH.write_text(json.dumps(momentum_state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print("Momentum state save failed:", type(exc).__name__)

def saudi_time(timestamp_ms):
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).astimezone(
        timezone(timedelta(hours=3))
    ).strftime("%Y-%m-%d %H:%M:%S")

def fetch_closed_candles(interval, outputsize):
    response = monitor.SESSION.get(
        f"{monitor.BASE_URL}/time_series",
        params={"symbol": SYMBOL, "interval": interval, "outputsize": outputsize,
                "timezone": "UTC", "order": "ASC", "format": "JSON",
                "apikey": API_KEY},
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("status") == "error":
        raise RuntimeError(f"Twelve Data {interval}: {data.get('message')}")
    duration = {"15min": 900_000, "1h": 3_600_000, "4h": 14_400_000}[interval]
    now_ms = int(time.time() * 1000)
    result = []
    for row in data.get("values", []):
        dt = monitor.parse_time(row["datetime"])
        t = int(dt.timestamp() * 1000)
        if t + duration > now_ms:
            continue
        result.append({"t": t, "o": float(row["open"]), "h": float(row["high"]),
                       "l": float(row["low"]), "c": float(row["close"])})
    result.sort(key=lambda x: x["t"])
    return result

def ema_last(values, period=20):
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    value = float(values[0])
    for item in values[1:]:
        value = float(item) * alpha + value * (1 - alpha)
    return value

def dmi_last(candles, period=14):
    if len(candles) < period + 2:
        return None
    tr = plus = minus = 0.0
    dx_values = []
    adx = None
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        up, down = cur["h"] - prev["h"], prev["l"] - cur["l"]
        true_range = max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]),
                         abs(cur["l"] - prev["c"]))
        p = up if up > down and up > 0 else 0.0
        m = down if down > up and down > 0 else 0.0
        if i <= period:
            tr += true_range; plus += p; minus += m
        else:
            tr += true_range - tr / period
            plus += p - plus / period
            minus += m - minus / period
        if i < period:
            continue
        pdi = 100 * plus / tr if tr else 0.0
        mdi = 100 * minus / tr if tr else 0.0
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0
        dx_values.append(dx)
        if len(dx_values) < period:
            continue
        adx = sum(dx_values[-period:]) / period if adx is None else (
            adx * (period - 1) + dx
        ) / period
    if adx is None:
        return None
    return pdi, mdi, adx

def momentum_setup():
    m15 = fetch_closed_candles("15min", 300)
    h1 = fetch_closed_candles("1h", 250)
    h4 = fetch_closed_candles("4h", 250)
    if len(m15) < 40 or len(h1) < 25 or len(h4) < 25:
        return None
    last = m15[-1]
    e20_15 = ema_last([x["c"] for x in m15], 20)
    dmi = dmi_last(m15)
    if not e20_15 or not dmi:
        return None
    # Week starts Monday 00:00 UTC, matching the historical study.
    dt = datetime.fromtimestamp(last["t"] / 1000, timezone.utc)
    monday = (dt.replace(hour=0, minute=0, second=0, microsecond=0)
              - timedelta(days=dt.weekday()))
    weekly = [x for x in m15 if x["t"] >= int(monday.timestamp() * 1000)]
    if not weekly:
        return None
    h1_close, h4_close = h1[-1]["c"], h4[-1]["c"]
    conditions = {
        "M15 فوق EMA20": last["c"] > e20_15,
        "+DI أعلى من -DI": dmi[0] > dmi[1],
        "ADX14 ≥ 20": dmi[2] >= 20,
        "H1 فوق EMA20": h1_close > ema_last([x["c"] for x in h1], 20),
        "H4 فوق EMA20": h4_close > ema_last([x["c"] for x in h4], 20),
        "السعر فوق افتتاح الأسبوع": last["c"] > weekly[0]["o"],
    }
    return {"t": last["t"] + 900_000, "price": last["c"],
            "conditions": conditions, "qualified": all(conditions.values())}

def observe_momentum_tick(timestamp_ms, price):
    pending = momentum_state.get("pending")
    if not pending:
        return
    if price > pending["high"]:
        pending["high"], pending["high_t"] = price, timestamp_ms
    if price < pending["low"]:
        pending["low"], pending["low_t"] = price, timestamp_ms
    if timestamp_ms >= pending["start_t"] + MOMENTUM_WINDOW_MS:
        rise = pending["high"] - pending["entry"]
        dip = pending["entry"] - pending["low"]
        message = (
            "✅ انتهت متابعة استراتيجية زخم الذهب (22 دقيقة)\\n"
            f"سعر الرصد: {pending['entry']:.2f} · الوقت: {saudi_time(pending['start_t'])} السعودية\\n"
            f"أقصى اندفاع صاعد: +{rise:.2f}$ إلى {pending['high']:.2f} · {saudi_time(pending['high_t'])}\\n"
            f"أقصى حركة ضد الشراء: -{dip:.2f}$ إلى {pending['low']:.2f} · {saudi_time(pending['low_t'])}\\n"
            f"السعر عند نهاية المتابعة: {price:.2f} · الصافي من سعر الرصد: {price-pending['entry']:+.2f}$"
        )
        if monitor.send_telegram(message):
            momentum_state["pending"] = None
            save_momentum_state()

def check_momentum_signal(completed_bar):
    # Evaluate only on a completed M15 boundary; conditions are based on closed candles.
    end_t = completed_bar["t"] + 60_000
    if end_t % 900_000 != 0:
        return
    try:
        setup = momentum_setup()
        if not setup:
            return
        if not setup["qualified"]:
            momentum_state["armed"] = True
            save_momentum_state()
            return
        if not momentum_state.get("armed", True) or momentum_state.get("pending"):
            return
        entry = setup["price"]
        start_t = setup["t"]
        pending = {"entry": entry, "start_t": start_t, "high": entry, "high_t": start_t,
                   "low": entry, "low_t": start_t}
        momentum_state["pending"] = pending
        momentum_state["armed"] = False
        save_momentum_state()
        message = (
            "🟢 تم رصد استراتيجية زخم الذهب\\n"
            f"سعر الرصد: {entry:.2f} · {saudi_time(start_t)} السعودية\\n"
            "الشروط: إغلاق M15 فوق EMA20، +DI أعلى من -DI، ADX14≥20، "
            "H1 وH4 فوق EMA20، والسعر فوق افتتاح الأسبوع.\\n"
            "بدأت متابعة السعر لمدة 22 دقيقة؛ سأرسل أقصى اندفاع صاعد وأقصى حركة معاكسة مع وقت كل منهما."
        )
        if not monitor.send_telegram(message):
            momentum_state["pending"] = None
            momentum_state["armed"] = True
        save_momentum_state()
    except Exception as exc:
        print("Momentum signal check failed:", type(exc).__name__, safe_error_text(exc))


def run_analysis():
    global bars, current_bar
    if current_bar is None or not bars:
        return
    candles = bars[-MAX_BARS:] + [dict(current_bar)]
    monitor.main(
        candles_override=candles,
        partial_current=True,
        live_signal_since=live_signal_since,
    )


def on_open(ws):
    print("Twelve Data WebSocket connected; subscribing to", SYMBOL)
    ws.send(json.dumps({"action": "subscribe", "params": {"symbols": SYMBOL}}))
    threading.Thread(target=send_heartbeat, args=(ws,), daemon=True).start()


def on_message(ws, message):
    global bars, current_bar, live_signal_since
    try:
        payload = json.loads(message)
    except (TypeError, json.JSONDecodeError):
        print("Ignoring malformed WebSocket message")
        return

    event = payload.get("event")
    if event == "subscribe-status":
        successes = payload.get("success") or []
        failures = payload.get("fails") or []
        print("Twelve Data subscription status:", payload.get("status"),
              "symbols accepted:", len(successes),
              "symbols rejected:", len(failures))
        for failure in failures:
            print("Twelve Data rejected symbol:", failure.get("symbol"),
                  failure.get("message", failure.get("code", "unknown reason")))
        return

    if event != "price" or payload.get("symbol") != SYMBOL:
        return

    try:
        timestamp = float(payload["timestamp"])
        timestamp_ms = int(timestamp * 1000 if timestamp < 10_000_000_000 else timestamp)
        price = float(payload["price"])
    except (KeyError, TypeError, ValueError):
        print("Ignoring malformed price update")
        return

    tick_minute = minute_start(timestamp_ms)
    with state_lock:
        observe_momentum_tick(timestamp_ms, price)
        if current_bar is None:
            live_signal_since = tick_minute
            current_bar = {"t": tick_minute, "o": price, "h": price, "l": price, "c": price}
            try:
                run_analysis()
            except Exception as exc:
                print("Live analysis error:", type(exc).__name__, safe_error_text(exc))
            return

        if tick_minute < current_bar["t"]:
            return
        if tick_minute == current_bar["t"]:
            current_bar["h"] = max(current_bar["h"], price)
            current_bar["l"] = min(current_bar["l"], price)
            current_bar["c"] = price
            return

        previous_minute = current_bar["t"]
        completed_bar = dict(current_bar)
        bars.append(completed_bar)
        bars = bars[-MAX_BARS:]
        check_momentum_signal(completed_bar)
        if tick_minute - previous_minute > 60_000:
            live_signal_since = tick_minute
            print("Market-data gap detected; resetting live setup warmup")
        current_bar = {"t": tick_minute, "o": price, "h": price, "l": price, "c": price}
        try:
            run_analysis()
        except Exception as exc:
            print("Live analysis error:", type(exc).__name__, safe_error_text(exc))


def safe_error_text(error):
    detail = str(error)
    for secret in (API_KEY, os.environ.get("BOT_TOKEN", "").strip()):
        if secret:
            detail = detail.replace(secret, "[redacted]")
    return detail[:240]


def on_error(ws, error):
    print("Twelve Data WebSocket error:", type(error).__name__, safe_error_text(error))


def on_close(ws, status_code, message):
    print("Twelve Data WebSocket closed:", status_code)


def main():
    global bars
    load_momentum_state()
    bars = monitor.load_candles(datetime.now(timezone.utc))
    bars = bars[-MAX_BARS:]
    if len(bars) < 50:
        raise RuntimeError("Not enough historical M1 candles to seed Sumo")
    print("Loaded", len(bars), "historical M1 candles; live analysis starts on the next price ticks")
    if monitor.STATE_PATH.exists():
        state = json.loads(monitor.STATE_PATH.read_text(encoding="utf-8"))
        for trade in state.get("trades", []):
            trade["entry_notified"] = True
            if trade.get("status") == "complete":
                trade["completion_notified"] = True
            if trade.get("opposite_signal_t") is not None:
                trade["opposite_exit_notified"] = True
        monitor.save_state(state)
        print("Suppressed Telegram replay for existing paper-trade history")

    retry_seconds = 2
    while True:
        app = websocket.WebSocketApp(
            WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        app.run_forever(ping_interval=20, ping_timeout=10)
        print("WebSocket disconnected; reconnecting in", retry_seconds, "seconds")
        time.sleep(retry_seconds)
        retry_seconds = min(retry_seconds * 2, 60)


if __name__ == "__main__":
    main()
