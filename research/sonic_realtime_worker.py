#!/usr/bin/env python3
"""Continuous Twelve Data WebSocket worker for Sumo's completed-M1 strategy."""
import json
import os
import threading
import time
from datetime import datetime, timezone

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
    global current_bar, live_signal_since
    try:
        payload = json.loads(message)
    except (TypeError, json.JSONDecodeError):
        print("Ignoring malformed WebSocket message")
        return

    event = payload.get("event")
    if event == "subscribe-status":
        print("Twelve Data subscription status:", payload.get("status"),
              "symbols accepted:", len(payload.get("success", [])),
              "symbols rejected:", len(payload.get("fails", [])))
        for failure in payload.get("fails", []):
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
        if current_bar is None:
            live_signal_since = tick_minute
            current_bar = {"t": tick_minute, "o": price, "h": price, "l": price, "c": price}
            run_analysis()
            return

        if tick_minute < current_bar["t"]:
            return
        if tick_minute == current_bar["t"]:
            current_bar["h"] = max(current_bar["h"], price)
            current_bar["l"] = min(current_bar["l"], price)
            current_bar["c"] = price
            return

        previous_minute = current_bar["t"]
        bars.append(current_bar)
        bars = bars[-MAX_BARS:]
        if tick_minute - previous_minute > 60_000:
            live_signal_since = tick_minute
            print("Market-data gap detected; resetting live setup warmup")
        current_bar = {"t": tick_minute, "o": price, "h": price, "l": price, "c": price}
        run_analysis()


def on_error(ws, error):
    print("Twelve Data WebSocket error:", type(error).__name__)


def on_close(ws, status_code, message):
    print("Twelve Data WebSocket closed:", status_code)


def main():
    global bars
    bars = monitor.load_candles(datetime.now(timezone.utc))
    bars = bars[-MAX_BARS:]
    if len(bars) < 50:
        raise RuntimeError("Not enough historical M1 candles to seed Sumo")
    print("Loaded", len(bars), "historical M1 candles; live analysis starts on the next price ticks")

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
