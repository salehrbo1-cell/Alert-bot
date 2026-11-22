import json
import math
import os
import statistics
import time
from datetime import datetime, timedelta, timezone

import requests

# Bitcoin paper-signal monitor. It never places orders.
TOKEN = os.environ["BOT_TOKEN"]
CHAT = os.environ["CHAT_ID"]
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
SLIPPAGE_USD_PER_SIDE = float(os.environ.get("BTC_SLIPPAGE_USD_PER_SIDE", "0"))

SYMBOL = "BTC/USD"
ASSET = "BTC"
NAME = "البتكوين"
BASE_URL = "https://api.twelvedata.com"
INTERVAL = "15min"
INTERVAL_MS = 15 * 60 * 1000
SAUDI_TZ = timezone(timedelta(hours=3))

# Strategy requirements
SWEEP_PIVOT_K = 2
SWEEP_LOOKBACK_M30 = 96       # 48 hours of 30-minute liquidity swings
SWEEP_BUFFER_ATR = 0.05
FVG_MIN_ATR = 0.5
SEQUENCE_MAX_BARS = 2         # Sweep to CISD within 30 minutes on M15
CISD_BODY_MEDIAN_MULTIPLE = 2.0
CISD_MIN_RANGE_FRACTION = 0.60
CISD_BREAK_LOOKBACK = 3
MAX_SIGNAL_AGE_MIN = 18
STATE_FILE = "btc_smc_state.json"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "BTC-SMC-PaperBot/1.0"})


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send(message):
    saudi_now = datetime.now(SAUDI_TZ)
    message = (
        f"{message}\n\n"
        f"🕒 وقت الإرسال (السعودية): {saudi_now:%Y-%m-%d %H:%M:%S}"
    )
    try:
        response = SESSION.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT, "text": message},
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            print(
                "Telegram rejected message:",
                result.get("error_code"),
                str(result.get("description", ""))[:180],
            )
            return False
        print(f"Telegram delivered message_id={result.get('result', {}).get('message_id')}")
        return True
    except Exception as exc:
        # Never print an exception URL, which could contain the Telegram token.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        print("Telegram send failed:", type(exc).__name__, "HTTP", status)
        return False


def td_get_candles():
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "outputsize": 500,
        "timezone": "UTC",
        "order": "ASC",
        "format": "JSON",
        "apikey": API_KEY,
    }
    for attempt in range(3):
        try:
            response = SESSION.get(
                f"{BASE_URL}/time_series", params=params, timeout=25
            )
        except requests.RequestException as exc:
            safe = str(exc).replace(API_KEY, "[REDACTED]")
            raise RuntimeError(
                f"Twelve Data request failed ({type(exc).__name__}): {safe[:250]}"
            ) from None
        if response.status_code == 429 and attempt < 2:
            header = response.headers.get("Retry-After")
            try:
                wait = min(max(int(header), 1), 60) if header else 30 * (attempt + 1)
            except (TypeError, ValueError):
                wait = 30 * (attempt + 1)
            print(f"Twelve Data rate limit; retrying in {wait}s.")
            time.sleep(wait)
            continue
        if not response.ok:
            raise RuntimeError(
                f"Twelve Data HTTP {response.status_code} for time_series "
                f"(symbol={SYMBOL}, interval={INTERVAL})"
            )
        break

    data = response.json()
    if isinstance(data, dict) and data.get("status") == "error":
        message = str(data.get("message") or "Unknown provider error")
        raise RuntimeError(
            f"Twelve Data error {data.get('code')}: {message.replace(API_KEY, '[REDACTED]')}"
        )
    values = data.get("values") if isinstance(data, dict) else None
    if not isinstance(values, list):
        raise RuntimeError(f"Invalid Twelve Data candle response: {data}")

    now = datetime.now(timezone.utc)
    candles = []
    for row in values:
        start = datetime.fromisoformat(
            row["datetime"].replace("Z", "+00:00")
        )
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        start = start.astimezone(timezone.utc)
        if start + timedelta(minutes=15) > now:
            continue
        candles.append({
            "t": int(start.timestamp() * 1000),
            "o": float(row["open"]),
            "h": float(row["high"]),
            "l": float(row["low"]),
            "c": float(row["close"]),
            "v": float(row.get("volume") or 0),
        })
    candles.sort(key=lambda x: x["t"])
    if len(candles) < 220:
        raise RuntimeError(f"Not enough closed M15 candles: {len(candles)}")
    return candles


def true_ranges(candles):
    result = [0.0] * len(candles)
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        result[i] = max(c["h"] - c["l"], abs(c["h"] - p["c"]),
                        abs(c["l"] - p["c"]))
    return result


def atr_series(candles, period=14):
    tr = true_ranges(candles)
    values = [0.0] * len(candles)
    for i in range(period, len(candles)):
        values[i] = sum(tr[i - period + 1:i + 1]) / period
    return values


def ema_series(values, period):
    alpha = 2 / (period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(float(value) * alpha + result[-1] * (1 - alpha))
    return result


def midpoint(candles, end_index, period):
    start = end_index - period + 1
    if start < 0:
        return None
    section = candles[start:end_index + 1]
    return (max(x["h"] for x in section) + min(x["l"] for x in section)) / 2


def ichimoku_cloud(candles, index):
    # Senkou spans are plotted 26 bars forward; align the visible cloud
    # under the current candle using values calculated 26 bars earlier.
    source = index - 26
    if source < 51:
        return None
    tenkan = midpoint(candles, source, 9)
    kijun = midpoint(candles, source, 26)
    span_a = (tenkan + kijun) / 2
    span_b = midpoint(candles, source, 52)
    return min(span_a, span_b), max(span_a, span_b)


def aggregate_30m(candles):
    groups = {}
    bucket_ms = 30 * 60 * 1000
    for candle in candles:
        bucket = candle["t"] // bucket_ms * bucket_ms
        groups.setdefault(bucket, []).append(candle)
    result = []
    for bucket, parts in sorted(groups.items()):
        parts.sort(key=lambda x: x["t"])
        if (len(parts) != 2 or parts[0]["t"] != bucket
                or parts[1]["t"] != bucket + INTERVAL_MS):
            continue
        result.append({
            "t": bucket,
            "end": bucket + bucket_ms,
            "o": parts[0]["o"],
            "h": max(x["h"] for x in parts),
            "l": min(x["l"] for x in parts),
            "c": parts[-1]["c"],
        })
    return result


def confirmed_m30_pivots(bars):
    pivots = []
    k = SWEEP_PIVOT_K
    for i in range(k, len(bars) - k):
        window = bars[i - k:i + k + 1]
        if bars[i]["h"] >= max(x["h"] for x in window):
            pivots.append({
                "side": "high", "price": bars[i]["h"],
                "pivot_t": bars[i]["t"],
                "available_at": bars[i + k]["end"],
            })
        if bars[i]["l"] <= min(x["l"] for x in window):
            pivots.append({
                "side": "low", "price": bars[i]["l"],
                "pivot_t": bars[i]["t"],
                "available_at": bars[i + k]["end"],
            })
    return pivots


def find_sweep(candles, pivots, index, atrs):
    candle = candles[index]
    atr_value = atrs[index]
    if atr_value <= 0:
        return []
    buffer = max(atr_value * SWEEP_BUFFER_ATR, candle["c"] * 0.00002)
    age_limit = 48 * 60 * 60 * 1000
    eligible = [
        p for p in pivots
        if p["available_at"] <= candle["t"]
        and candle["t"] - p["pivot_t"] <= age_limit
    ]
    found = []
    highs = [
        p for p in eligible if p["side"] == "high"
        and candle["h"] > p["price"] + buffer
        and candle["c"] < p["price"]
    ]
    lows = [
        p for p in eligible if p["side"] == "low"
        and candle["l"] < p["price"] - buffer
        and candle["c"] > p["price"]
    ]
    if highs:
        level = max(highs, key=lambda p: p["price"])
        found.append({
            "side": "sell", "level": level["price"],
            "extreme": candle["h"], "index": index,
            "pivot_t": level["pivot_t"], "atr": atr_value,
        })
    if lows:
        level = max(lows, key=lambda p: p["price"])
        found.append({
            "side": "buy", "level": level["price"],
            "extreme": candle["l"], "index": index,
            "pivot_t": level["pivot_t"], "atr": atr_value,
        })
    return found


def inverse_fvg(candles, atrs, sweep_index, index, side):
    # FVG must be nearby in time, large enough relative to ATR, and then
    # close through the opposite gap boundary in the trade direction.
    first_gap_index = max(2, sweep_index - 2)
    for gap_index in range(first_gap_index, index):
        a, c = candles[gap_index - 2], candles[gap_index]
        if c["l"] > a["h"]:
            gap_side, lo, hi = "buy", a["h"], c["l"]
        elif c["h"] < a["l"]:
            gap_side, lo, hi = "sell", c["h"], a["l"]
        else:
            continue
        gap_size = hi - lo
        if atrs[gap_index] <= 0 or gap_size < FVG_MIN_ATR * atrs[gap_index]:
            continue
        bar = candles[index]
        if side == "buy" and gap_side == "sell":
            if bar["c"] > hi and bar["c"] > bar["o"]:
                return {
                    "index": index, "lo": lo, "hi": hi,
                    "size": gap_size, "atr": atrs[gap_index],
                    "gap_index": gap_index, "gap_side": gap_side,
                }
        if side == "sell" and gap_side == "buy":
            if bar["c"] < lo and bar["c"] < bar["o"]:
                return {
                    "index": index, "lo": lo, "hi": hi,
                    "size": gap_size, "atr": atrs[gap_index],
                    "gap_index": gap_index, "gap_side": gap_side,
                }
    return None


def cisd_confirmation(candles, atrs, sweep, ifvg, index):
    if index <= ifvg["index"] or index - sweep["index"] > SEQUENCE_MAX_BARS:
        return None
    candle = candles[index]
    span = candle["h"] - candle["l"]
    if span <= 0:
        return None
    bodies = [abs(x["c"] - x["o"]) for x in candles[max(0, index - 20):index]]
    if len(bodies) < 20:
        return None
    median_body = statistics.median(bodies)
    body_size = abs(candle["c"] - candle["o"])
    if body_size < CISD_BODY_MEDIAN_MULTIPLE * median_body:
        return None
    if body_size / span < CISD_MIN_RANGE_FRACTION:
        return None

    internal = candles[max(0, index - CISD_BREAK_LOOKBACK):index]
    if len(internal) < CISD_BREAK_LOOKBACK:
        return None
    side = sweep["side"]
    if side == "buy":
        valid = (
            candle["c"] > candle["o"]
            and candle["c"] > max(x["h"] for x in internal)
        )
    else:
        valid = (
            candle["c"] < candle["o"]
            and candle["c"] < min(x["l"] for x in internal)
        )
    if not valid:
        return None
    return {
        "index": index, "median_body": median_body,
        "body": body_size, "body_multiple": body_size / max(median_body, 1e-12),
        "range_fraction": body_size / span,
    }


def indicators_confirm(candles, index, side, closes, e20, e50, e200):
    cloud = ichimoku_cloud(candles, index)
    if cloud is None:
        return None
    close = candles[index]["c"]
    if side == "buy":
        passed = e20[index] > e50[index] and close > e200[index] and close > cloud[1]
    else:
        passed = e20[index] < e50[index] and close < e200[index] and close < cloud[0]
    return {
        "passed": passed, "ema20": e20[index], "ema50": e50[index],
        "ema200": e200[index], "cloud_low": cloud[0], "cloud_high": cloud[1],
    }


def latest_signal(candles):
    n = len(candles)
    index = n - 1
    candle = candles[index]
    age_min = (time.time() * 1000 - (candle["t"] + INTERVAL_MS)) / 60000
    stats = {"sweeps": 0, "ifvg": 0, "cisd": 0, "indicator_reject": 0}
    if age_min < -0.1 or age_min > MAX_SIGNAL_AGE_MIN:
        return None, stats, age_min

    atrs = atr_series(candles)
    m30 = aggregate_30m(candles)
    pivots = confirmed_m30_pivots(m30)
    closes = [x["c"] for x in candles]
    e20 = ema_series(closes, 20)
    e50 = ema_series(closes, 50)
    e200 = ema_series(closes, 200)

    for sweep_index in range(max(0, index - SEQUENCE_MAX_BARS), index):
        for sweep in find_sweep(candles, pivots, sweep_index, atrs):
            stats["sweeps"] += 1
            for ifvg_index in range(sweep_index, index):
                if ifvg_index - sweep_index > SEQUENCE_MAX_BARS:
                    break
                inv = inverse_fvg(
                    candles, atrs, sweep_index, ifvg_index, sweep["side"]
                )
                if not inv:
                    continue
                stats["ifvg"] += 1
                cisd = cisd_confirmation(
                    candles, atrs, sweep, inv, index
                )
                if not cisd:
                    continue
                stats["cisd"] += 1
                ind = indicators_confirm(
                    candles, index, sweep["side"], closes, e20, e50, e200
                )
                if not ind or not ind["passed"]:
                    stats["indicator_reject"] += 1
                    continue
                signal = {
                    "side": sweep["side"], "entry": candle["c"],
                    "t": candle["t"], "sweep": sweep,
                    "ifvg": inv, "cisd": cisd, "indicators": ind,
                    "age_min": age_min,
                }
                signal["id"] = (
                    f"{signal['side']}|{sweep['index']}|{sweep['level']:.2f}|"
                    f"{inv['index']}|{index}"
                )
                return signal, stats, age_min
    return None, stats, age_min


def saudi_time(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(SAUDI_TZ)


def update_excursions(position, candles):
    side = position["side"]
    entry = position["entry"]
    observed = [c for c in candles if c["t"] >= position["t"]]
    for c in observed:
        favorable = entry - c["l"] if side == "sell" else c["h"] - entry
        adverse = c["h"] - entry if side == "sell" else entry - c["l"]
        position["max_favorable"] = max(position.get("max_favorable", 0), favorable)
        position["max_adverse"] = max(position.get("max_adverse", 0), adverse)


def close_pnl_per_btc(position, exit_price):
    side = position["side"]
    entry_fill = (
        position["entry"] + SLIPPAGE_USD_PER_SIDE
        if side == "buy" else position["entry"] - SLIPPAGE_USD_PER_SIDE
    )
    exit_fill = (
        exit_price - SLIPPAGE_USD_PER_SIDE
        if side == "buy" else exit_price + SLIPPAGE_USD_PER_SIDE
    )
    return exit_fill - entry_fill if side == "buy" else entry_fill - exit_fill


def entry_message(signal):
    side_word = "شراء" if signal["side"] == "buy" else "بيع"
    ind = signal["indicators"]
    sweep = signal["sweep"]
    inv = signal["ifvg"]
    return (
        f"🧪 إشارة {side_word} بتكوين وفق SMC\n"
        f"BTC/USD · M15 مغلقة\n"
        f"التسلسل: Sweep → IFVG → CISD خلال 30 دقيقة\n"
        f"الدخول الافتراضي: {signal['entry']:,.2f}\n"
        f"مستوى السيولة المسحوب: {sweep['level']:,.2f}\n"
        f"قمة/قاع السحب: {sweep['extreme']:,.2f}\n"
        f"منطقة IFVG: {inv['lo']:,.2f}–{inv['hi']:,.2f} "
        f"(حجمها {inv['size']:,.2f}، الحد الأدنى 0.5 ATR)\n"
        f"CISD: جسم {signal['cisd']['body']:,.2f} "
        f"({signal['cisd']['body_multiple']:.2f}× وسيط 20 شمعة، "
        f"{signal['cisd']['range_fraction']:.0%} من المدى)\n"
        f"EMA20/50: {ind['ema20']:,.2f} / {ind['ema50']:,.2f}\n"
        f"EMA200: {ind['ema200']:,.2f}\n"
        f"Ichimoku Cloud: {ind['cloud_low']:,.2f}–{ind['cloud_high']:,.2f}\n"
        f"وقت CISD (السعودية): {saudi_time(signal['t'] + INTERVAL_MS):%Y-%m-%d %H:%M}\n"
        "كل الشروط الأساسية متحققة على شموع M15 المغلقة. "
        "دخول افتراضي فقط، بلا تنفيذ تداول."
    )


def make_daily_report(results, open_position, report_date):
    day_results = [
        x for x in results
        if x.get("closed_at_saudi", "")[:10] == report_date
    ]
    buys = sum(x["side"] == "buy" for x in day_results)
    sells = sum(x["side"] == "sell" for x in day_results)
    wins = sum(max(0, x["pnl_per_btc"]) for x in day_results)
    losses = sum(min(0, x["pnl_per_btc"]) for x in day_results)
    net = wins + losses
    open_text = (
        "لا توجد" if not open_position else
        ("شراء" if open_position["side"] == "buy" else "بيع")
    )
    return (
        f"📊 تقرير البتكوين اليومي | {report_date} (السعودية)\n"
        f"صفقات شراء مغلقة: {buys}\n"
        f"صفقات بيع مغلقة: {sells}\n"
        f"الإجمالي: {len(day_results)}\n"
        f"الربح لكل 1 BTC: {wins:+.2f}$\n"
        f"الخسارة لكل 1 BTC: {losses:+.2f}$\n"
        f"الصافي لكل 1 BTC: {net:+.2f}$\n"
        f"الصفقة المفتوحة: {open_text}\n"
        f"انزلاق مفترض: {SLIPPAGE_USD_PER_SIDE:.2f}$ لكل تنفيذ؛ "
        "الأرقام ليست ربح عقد MT5."
    )


def main():
    candles = td_get_candles()
    state = load_state()
    state.setdefault("results", [])
    state.setdefault("position", None)
    state.setdefault("last_signal_id", None)

    signal, stats, age_min = latest_signal(candles)
    print(
        f"BTC strategy scan: {stats}; latest closed M15 age={age_min:.1f}m; "
        f"qualified_signal={bool(signal)}"
    )
    if state["position"]:
        update_excursions(state["position"], candles)

    if signal and signal["id"] != state.get("last_signal_id"):
        previous = state.get("position")
        if previous and previous["side"] == signal["side"]:
            print("Signal suppressed: same-direction virtual position is open.")
        else:
            message = entry_message(signal)
            closed_result = None
            if previous:
                pnl = close_pnl_per_btc(previous, signal["entry"])
                closed_at = datetime.now(SAUDI_TZ).isoformat()
                closed_result = {
                    "side": previous["side"],
                    "entry": previous["entry"],
                    "exit": signal["entry"],
                    "pnl_per_btc": round(pnl, 2),
                    "closed_at_saudi": closed_at,
                    "max_favorable": round(previous.get("max_favorable", 0), 2),
                    "max_adverse": round(previous.get("max_adverse", 0), 2),
                }
                old_side = "شراء" if previous["side"] == "buy" else "بيع"
                message = (
                    f"🔄 إغلاق افتراضي {old_side} عند إشارة معاكسة\n"
                    f"الدخول: {previous['entry']:,.2f} · الخروج: {signal['entry']:,.2f}\n"
                    f"الصافي لكل 1 BTC: {pnl:+.2f}$\n\n{message}"
                )

            if send(message):
                if closed_result:
                    state["results"].append(closed_result)
                stop = (
                    signal["sweep"]["extreme"] - 0.1 * signal["sweep"]["atr"]
                    if signal["side"] == "buy" else
                    signal["sweep"]["extreme"] + 0.1 * signal["sweep"]["atr"]
                )
                state["position"] = {
                    "side": signal["side"], "entry": signal["entry"],
                    "t": signal["t"], "stop_reference": stop,
                    "max_favorable": 0.0, "max_adverse": 0.0,
                    "signal_id": signal["id"],
                }
                state["last_signal_id"] = signal["id"]
            else:
                print("Signal state not committed because Telegram did not confirm delivery.")

    saudi_now = datetime.now(SAUDI_TZ)
    yesterday = (saudi_now - timedelta(days=1)).date().isoformat()
    if "daily_report_date" not in state:
        # Avoid sending a meaningless backfill report when first starting.
        state["daily_report_date"] = yesterday
    elif state["daily_report_date"] != yesterday:
        if send(make_daily_report(state["results"], state.get("position"), yesterday)):
            state["daily_report_date"] = yesterday

    state["results"] = state["results"][-5000:]
    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        safe = str(exc).replace(API_KEY, "[REDACTED]").replace(TOKEN, "[REDACTED]")
        print("BOT FATAL ERROR:", type(exc).__name__, safe[:400])
        send(f"⚠️ تعذر تحديث بوت البتكوين:\n{safe[:500]}")
        raise
