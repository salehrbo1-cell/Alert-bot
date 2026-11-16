import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

import requests

# ============================================================
# GOLD SIGNAL BOT - XAU/USD via Twelve Data
# Signals only: NO order execution.
# GitHub Secrets required:
# BOT_TOKEN, CHAT_ID, TWELVE_DATA_API_KEY
# ============================================================

TOKEN = os.environ["BOT_TOKEN"]
CHAT = os.environ["CHAT_ID"]
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
FORCE = os.environ.get("FORCE", "").lower() == "true"

SYMBOL = "XAU/USD"
ASSET = "GOLD"
NAME = "الذهب"
BASE_URL = "https://api.twelvedata.com"

SIGNAL_INTERVAL = "15min"
INTERVAL_MS = 15 * 60 * 1000

# Conservative filters: fewer signals, stricter confirmation.
ZONE_PCT = 0.00035
WICK_RATIO = 2.2
MIN_RR = 1.8
MAX_SIGNAL_AGE_MIN = 18
MAX_ENTRY_DRIFT_ATR = 0.35
SIGNAL_LIFE = 12
BODY_AVG_N = 10
RETEST_LOOKBACK = 30
MIN_CONFLUENCE = 4
LEVEL_RANGE = 0.25
CLUSTER_PCT = 0.0012
MIN_LEVEL_SCORE = 3
STRONG_SCORE = 5
NEAR_PCT = 0.0015
COOLDOWN = 2 * 3600
DIGEST_EVERY = 6 * 3600
SCORE_EVERY = 24 * 3600
STOP_ATR_BUFFER = 0.25
MAX_ACTIVE_PER_SIDE = 2

LEVEL_TFS = [
    ("1h", 500, 1, 4, "1س"),
    ("4h", 500, 2, 4, "4س"),
    ("1day", 365, 3, 3, "يومي"),
]
TF_ORDER = {"يومي": 0, "4س": 1, "1س": 2, "يدوي": 3}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "GoldSignalBot/2.0"})


def load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_state(state):
    with open("state.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send(message):
    try:
        # Show when the alert was observed/sent in Saudi Arabia (UTC+3).
        saudi_now = datetime.now(timezone(timedelta(hours=3)))
        timestamp = saudi_now.strftime("%Y-%m-%d %H:%M:%S")
        message = f"{message}\n\n🕒 وقت إرسال التنبيه (السعودية): {timestamp}"
        r = SESSION.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT, "text": message},
            timeout=20,
        )
        r.raise_for_status()
        result = r.json()
        if not result.get("ok"):
            print("Telegram error:", result)
    except Exception as exc:
        print("Telegram send error:", exc)


def td_get(endpoint, params):
    query = dict(params)
    query["apikey"] = API_KEY
    r = SESSION.get(f"{BASE_URL}/{endpoint}", params=query, timeout=25)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(
            f"Twelve Data error {data.get('code')}: {data.get('message')}"
        )
    return data


def parse_utc_datetime(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_candles(interval, outputsize):
    data = td_get("time_series", {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "timezone": "UTC",
        "order": "ASC",
        "format": "JSON",
    })
    values = data.get("values")
    if not isinstance(values, list) or not values:
        raise RuntimeError(f"No candles returned for {interval}: {data}")

    duration = {
        "15min": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "1day": timedelta(days=1),
    }[interval]
    now = datetime.now(timezone.utc)
    candles = []
    for row in values:
        start = parse_utc_datetime(row["datetime"])
        end = start + duration
        if end > now:
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
    if len(candles) < 220 and interval in ("1h", "4h"):
        raise RuntimeError(f"Not enough closed {interval} candles: {len(candles)}")
    if len(candles) < 60 and interval == "15min":
        raise RuntimeError(f"Not enough closed 15min candles: {len(candles)}")
    return candles


def get_price():
    data = td_get("price", {"symbol": SYMBOL})
    try:
        return float(data["price"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError(f"Invalid live price response: {data}")


def body(c):
    return abs(c["c"] - c["o"])


def avg_body(cs, i):
    seg = cs[max(0, i - BODY_AVG_N):i]
    return sum(body(x) for x in seg) / len(seg) if seg else 0.0


def touches(c, level):
    return c["h"] >= level * (1 - ZONE_PCT) and c["l"] <= level * (1 + ZONE_PCT)


def reject_reason(c, level, side):
    rng = c["h"] - c["l"]
    if rng <= 0 or not touches(c, level):
        return "الشمعة لم تلمس المستوى"
    b = max(body(c), rng * 0.001)
    pos = (c["c"] - c["l"]) / rng

    if side == "sell":
        wick = c["h"] - max(c["o"], c["c"])
        if c["c"] >= c["o"]:
            return "شمعة الرفض ليست هابطة"
        if wick < WICK_RATIO * b:
            return "الذيل العلوي غير كافٍ"
        if c["c"] >= level * (1 - ZONE_PCT):
            return "الإغلاق لم يعد أسفل منطقة المقاومة"
        if pos > 1 / 3:
            return "الإغلاق ليس في الثلث السفلي"
    else:
        wick = min(c["o"], c["c"]) - c["l"]
        if c["c"] <= c["o"]:
            return "شمعة الرفض ليست صاعدة"
        if wick < WICK_RATIO * b:
            return "الذيل السفلي غير كافٍ"
        if c["c"] <= level * (1 + ZONE_PCT):
            return "الإغلاق لم يعد أعلى منطقة الدعم"
        if pos < 2 / 3:
            return "الإغلاق ليس في الثلث العلوي"
    return None


def rejection(c, level, side):
    return reject_reason(c, level, side) is None


def ema(values, period):
    if len(values) < period:
        raise ValueError(f"Need at least {period} values for EMA")
    alpha = 2 / (period + 1)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(float(value) * alpha + result[-1] * (1 - alpha))
    return result


def rsi(values, period=14):
    if len(values) <= period:
        raise ValueError("Not enough values for RSI")
    gains = [0.0] * period
    losses = [0.0] * period
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains[i - 1] = max(change, 0)
        losses[i - 1] = max(-change, 0)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def macd_hist(values):
    fast = ema(values, 12)
    slow = ema(values, 26)
    line = [a - b for a, b in zip(fast, slow)]
    signal = ema(line, 9)
    return [a - b for a, b in zip(line, signal)]


def atr(candles, period=14):
    if len(candles) < period + 1:
        raise ValueError("Not enough candles for ATR")
    trs = []
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i - 1]
        trs.append(max(
            c["h"] - c["l"],
            abs(c["h"] - prev["c"]),
            abs(c["l"] - prev["c"]),
        ))
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def trend(candles):
    closes = [x["c"] for x in candles]
    e50 = ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1]
    last = closes[-1]
    if last > e50 > e200:
        return "صاعد"
    if last < e50 < e200:
        return "هابط"
    return "محايد"


def momentum(c15, h1, h4):
    closes = [x["c"] for x in c15]
    hist = macd_hist(closes)
    r = rsi(closes)
    e20 = ema(closes, 20)[-1]
    return {
        "rsi": r,
        "macd_up": hist[-1] > hist[-2],
        "above20": closes[-1] > e20,
        "h1": trend(h1),
        "h4": trend(h4),
    }


def describe(m):
    return (
        f"RSI {m['rsi']:.0f} · MACD "
        f"{'صاعد' if m['macd_up'] else 'هابط'} · "
        f"{'فوق' if m['above20'] else 'تحت'} EMA20 · "
        f"H1 {m['h1']} · H4 {m['h4']}"
    )


def confluence(m, side, kind):
    sell = side == "sell"
    expected_trend = "هابط" if sell else "صاعد"
    checks = [
        m["rsi"] <= 50 if sell else m["rsi"] >= 50,
        (not m["macd_up"]) if sell else m["macd_up"],
        (not m["above20"]) if sell else m["above20"],
        m["h1"] == expected_trend,
        m["h4"] == expected_trend,
    ]
    return sum(checks), checks[3] and checks[4]


def grade(score):
    return "قوية جداً" if score == 5 else "قوية" if score == 4 else "غير كافية"


def pivots(candles, k):
    points = []
    for i in range(k, len(candles) - k):
        window = candles[i - k:i + k + 1]
        if candles[i]["h"] >= max(x["h"] for x in window):
            points.append(candles[i]["h"])
        if candles[i]["l"] <= min(x["l"] for x in window):
            points.append(candles[i]["l"])
    return points


def detect_levels(tf, price, manual):
    points = []
    for interval, _, weight, k, label in LEVEL_TFS:
        for p in pivots(tf[interval], k):
            if abs(p - price) / price <= LEVEL_RANGE:
                points.append((p, weight, label))
    for p in manual:
        points.append((float(p), STRONG_SCORE, "يدوي"))

    points.sort(key=lambda x: x[0])
    clusters, current = [], []
    for point in points:
        if current and point[0] > current[-1][0] * (1 + CLUSTER_PCT):
            clusters.append(current)
            current = []
        current.append(point)
    if current:
        clusters.append(current)

    levels = []
    for cluster in clusters:
        weights = {}
        for _, weight, label in cluster:
            weights[label] = max(weights.get(label, 0), weight)
        score = sum(weights.values())
        if score < MIN_LEVEL_SCORE:
            continue
        weighted_prices = {}
        for p, _, label in cluster:
            weighted_prices.setdefault(label, []).append(p)
        center = sum(
            (sum(prices) / len(prices)) * weights[label]
            for label, prices in weighted_prices.items()
        ) / score
        labels = sorted(weights, key=lambda x: TF_ORDER[x])
        levels.append({
            "p": center,
            "score": score,
            "strong": score >= STRONG_SCORE,
            "tfs": "+".join(labels),
        })
    return levels


def level_bucket(level):
    return round(math.log(level) / 0.002)


def strength(score):
    return "قوي" if score >= STRONG_SCORE else "متوسط"


def strength_f(score):
    return "قوية" if score >= STRONG_SCORE else "متوسطة"


def fmt(level):
    return f"{level['p']:,.2f} · {strength(level['score'])} {level['score']} ({level['tfs']})"


def retest_direction(candles, level):
    n = len(candles)
    latest = candles[-1]
    if not touches(latest, level):
        return None
    start = max(2, n - RETEST_LOOKBACK)
    for i in range(n - 2, start - 1, -1):
        previous, breakout = candles[i - 1], candles[i]
        average = avg_body(candles, i)
        if average <= 0 or body(breakout) <= average * 1.25:
            continue
        up = (
            breakout["c"] > level * (1 + ZONE_PCT)
            and breakout["o"] < breakout["c"]
            and previous["c"] <= level * (1 + ZONE_PCT)
        )
        down = (
            breakout["c"] < level * (1 - ZONE_PCT)
            and breakout["o"] > breakout["c"]
            and previous["c"] >= level * (1 - ZONE_PCT)
        )
        after = candles[i + 1:]
        if up and all(x["c"] > level * (1 - ZONE_PCT) for x in after):
            if rejection(latest, level, "buy"):
                return "buy"
        if down and all(x["c"] < level * (1 + ZONE_PCT) for x in after):
            if rejection(latest, level, "sell"):
                return "sell"
    return None


def make_plan(candle, side, levels, atr_value, current_price):
    if atr_value <= 0:
        return None
    if abs(current_price - candle["c"]) > MAX_ENTRY_DRIFT_ATR * atr_value:
        return None
    entry = current_price
    buffer = max(atr_value * STOP_ATR_BUFFER, entry * 0.00015)

    if side == "sell":
        sl = candle["h"] + buffer
        risk = sl - entry
        candidates = sorted(
            [x for x in levels if x < entry * (1 - ZONE_PCT)],
            reverse=True,
        )
    else:
        sl = candle["l"] - buffer
        risk = entry - sl
        candidates = sorted(
            [x for x in levels if x > entry * (1 + ZONE_PCT)]
        )

    if risk <= 0 or not candidates:
        return None

    target = candidates[0]
    reward = abs(target - entry)
    rr = reward / risk
    if rr < MIN_RR:
        return None
    return {"entry": entry, "sl": sl, "target": target, "rr": rr, "risk": risk}


def evaluate(candles, levels):
    candle = candles[-1]
    found = []
    for level in levels:
        sell_reason = reject_reason(candle, level, "sell")
        buy_reason = reject_reason(candle, level, "buy")
        if sell_reason is None:
            found.append(("رفض مقاومة", "sell", level, candle))
        if buy_reason is None:
            found.append(("رفض دعم", "buy", level, candle))
        side = retest_direction(candles, level)
        if side:
            found.append(("كسر وإعادة اختبار", side, level, candle))
    found.sort(key=lambda x: 0 if x[0] == "كسر وإعادة اختبار" else 1)
    unique, seen = [], set()
    for item in found:
        key = (item[1], level_bucket(item[2]), item[3]["t"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def summarize(results):
    count = len(results)
    wins = sum(1 for x in results if x["r"] > 0)
    losses = sum(1 for x in results if x["r"] < 0)
    rate = wins / (wins + losses) * 100 if wins + losses else 0
    net_r = sum(x["r"] for x in results)
    return count, wins, losses, rate, net_r


def scoreboard(results):
    if not results:
        return "📈 لوحة نتائج الذهب\nلا توجد إشارات منتهية حتى الآن."
    n, wins, losses, rate, net = summarize(results)
    lines = [
        "📈 لوحة نتائج الذهب (بوحدة R، وليست دولارات)",
        f"الإجمالي: {n} · فوز {wins} · خسارة {losses} · نجاح {rate:.1f}%",
        f"الصافي: {net:+.2f}R",
        "",
        "حسب النوع:",
    ]
    for kind in sorted({x["kind"] for x in results}):
        group = [x for x in results if x["kind"] == kind]
        gn, gw, gl, gr, gp = summarize(group)
        lines.append(f"• {kind}: {gn} · نجاح {gr:.0f}% · {gp:+.2f}R")
    return "\n".join(lines)


def diagnose(candles, level, levels):
    candle = candles[-1]
    for side, word in (("sell", "بيع"), ("buy", "شراء")):
        reason = reject_reason(candle, level, side)
        if reason is None:
            return f"شروط شمعة {word} مكتملة، بانتظار فلتر الاتجاه ونسبة العائد."
    return "لم تكتمل شروط شمعة الرفض."


def main():
    manual_levels = load("levels.json", {}).get(ASSET, {}).get("manual", [])
    state = load("state.json", {})
    state.setdefault("sent", {})
    state.setdefault("active", [])
    state.setdefault("near", {})
    state.setdefault("results", [])

    now = time.time()
    report = []

    candles = get_candles("15min", 500)
    tf = {
        "1h": get_candles("1h", 500),
        "4h": get_candles("4h", 500),
        "1day": get_candles("1day", 365),
    }
    price = get_price()
    levels = detect_levels(tf, price, manual_levels)
    if not levels:
        raise RuntimeError("No valid support/resistance levels were detected.")

    mom = momentum(candles, tf["1h"], tf["4h"])
    atr_value = atr(candles)
    signal_levels = [x["p"] for x in levels if x["score"] >= MIN_LEVEL_SCORE]

    above = sorted([x for x in levels if x["p"] > price], key=lambda x: x["p"])[:3]
    below = sorted([x for x in levels if x["p"] <= price], key=lambda x: x["p"], reverse=True)[:3]
    rows = [f"{NAME}: {price:,.2f}$ | ATR(14): {atr_value:.2f} | مستويات {len(levels)}"]
    rows += ["▲ " + fmt(x) for x in reversed(above)]
    rows += ["▼ " + fmt(x) for x in below]
    rows.append("الاتجاه والمؤشرات: " + describe(mom))
    report.append("\n".join(rows))

    still_active = []
    for signal in state["active"]:
        if signal["asset"] != ASSET:
            still_active.append(signal)
            continue

        side = signal["side"]
        is_sell = side == "sell"
        end_text = outcome = exit_price = None
        newer = [c for c in candles if c["t"] > signal["t"]]

        for c in newer:
            hit_stop = c["h"] >= signal["sl"] if is_sell else c["l"] <= signal["sl"]
            hit_target = c["l"] <= signal["target"] if is_sell else c["h"] >= signal["target"]
            if hit_stop:
                end_text, outcome, exit_price = "⛔ ضرب وقف الإشارة", "وقف", signal["sl"]
            elif hit_target:
                end_text, outcome, exit_price = "🎯 وصل الهدف", "هدف", signal["target"]
            else:
                continue
            break

        if not outcome and len(newer) <= SIGNAL_LIFE:
            still_active.append(signal)
            continue
        if not outcome:
            outcome, exit_price = "انتهت المدة", candles[-1]["c"]

        risk = abs(signal["entry"] - signal["sl"])
        pnl_r = (
            (signal["entry"] - exit_price) / risk
            if is_sell else (exit_price - signal["entry"]) / risk
        )
        state["results"].append({
            "asset": ASSET,
            "kind": signal.get("kind", "غير معروف"),
            "side": side,
            "conf": signal.get("conf", 0),
            "outcome": outcome,
            "r": round(pnl_r, 3),
            "t": signal["t"],
        })
        if end_text:
            send(
                f"{end_text}\n{NAME} {'بيع' if is_sell else 'شراء'}\n"
                f"الدخول: {signal['entry']:,.2f} · الخروج: {exit_price:,.2f}\n"
                f"النتيجة: {pnl_r:+.2f}R"
            )
    state["active"] = still_active

    latest = candles[-1]
    age_min = (time.time() * 1000 - (latest["t"] + INTERVAL_MS)) / 60000
    if age_min < -0.1:
        raise RuntimeError("Latest candle is not fully closed.")
    if age_min <= MAX_SIGNAL_AGE_MIN:
        candidates = evaluate(candles, signal_levels)
        for kind, side, level, candle in candidates:
            sid = f"{ASSET}|{side}|{level_bucket(level)}|{candle['t']}"
            if sid in state["sent"]:
                continue

            active_same_side = [
                x for x in state["active"]
                if x["asset"] == ASSET and x["side"] == side
            ]
            if len(active_same_side) >= MAX_ACTIVE_PER_SIDE:
                continue

            conf, trend_aligned = confluence(mom, side, kind)
            if not trend_aligned or conf < MIN_CONFLUENCE:
                continue

            plan = make_plan(candle, side, signal_levels, atr_value, price)
            if not plan:
                continue

            state["sent"][sid] = now
            state["active"].append({
                "asset": ASSET,
                "side": side,
                "entry": plan["entry"],
                "sl": plan["sl"],
                "target": plan["target"],
                "t": candle["t"],
                "kind": kind,
                "conf": conf,
            })

            is_sell = side == "sell"
            icon = "🔴" if is_sell else "🟢"
            word = "بيع" if is_sell else "شراء"
            level_info = min(levels, key=lambda x: abs(x["p"] - level))
            send(
                f"{icon} إشارة {word} ذهب ({kind})\n"
                f"XAU/USD · M15\n"
                f"المستوى: {fmt(level_info)}\n"
                f"الدخول التقريبي: {plan['entry']:,.2f}\n"
                f"وقف الخسارة: {plan['sl']:,.2f}\n"
                f"الهدف: {plan['target']:,.2f}\n"
                f"العائد/المخاطرة: {plan['rr']:.2f}R\n"
                f"توافق المؤشرات: {conf}/5 ({grade(conf)})\n"
                f"الاتجاه H1 وH4: {'متوافق' if trend_aligned else 'غير متوافق'}\n"
                f"{describe(mom)}\n"
                f"عمر الشمعة المغلقة: {max(0, age_min):.1f} دقيقة\n"
                f"تنبيه: إشارة تحليلية فقط وليست أمراً للتنفيذ."
            )
    else:
        print(f"Signal scan skipped: last candle is {age_min:.1f} minutes old.")

    for level_data in levels:
        level = level_data["p"]
        is_resistance = level > price
        word = "مقاومة" if is_resistance else "دعم"
        in_zone = level * (1 - ZONE_PCT) <= price <= level * (1 + ZONE_PCT)
        hit = in_zone or any(touches(x, level) for x in candles[-2:])
        near = level_data["strong"] and abs(price - level) <= level * NEAR_PCT

        if hit:
            key = f"hit|{ASSET}|{level_bucket(level)}"
            verb = "وصل" if in_zone else "لمس"
            title = f"📍 الذهب {verb} {word} {strength_f(level_data['score'])} {level:,.2f}"
            reason = diagnose(candles, level, signal_levels)
        elif near:
            key = f"near|{ASSET}|{level_bucket(level)}"
            distance = abs(level - price) / level * 100
            title = f"⚠️ الذهب يقترب من {word} قوية {level:,.2f} (يبعد {distance:.2f}%)"
            reason = ""
        else:
            continue

        if now - state["near"].get(key, 0) > COOLDOWN:
            state["near"][key] = now
            message = (
                f"{title}\n"
                f"القوة: {level_data['score']} ({level_data['tfs']})\n"
                f"السعر الحالي: {price:,.2f}\n"
                f"الاتجاه والمؤشرات: {describe(mom)}"
            )
            if reason:
                message += f"\nالشمعة: {reason}"
            send(message)

    if report and (FORCE or now - state.get("digest", 0) > DIGEST_EVERY):
        state["digest"] = now
        send("🗺️ تحديث مستويات الذهب\n\n" + "\n\n".join(report))

    # Legacy state.json includes BTC and old GOLD results with "pnl", not "r".
    # Only score valid GOLD results produced by the current R-based strategy.
    gold_results = [
        x for x in state["results"]
        if x.get("asset") == ASSET and isinstance(x.get("r"), (int, float))
    ]
    if gold_results and (FORCE or now - state.get("score_t", 0) > SCORE_EVERY):
        state["score_t"] = now
        send(scoreboard(gold_results))

    state["results"] = state["results"][-500:]
    state["sent"] = {k: v for k, v in state["sent"].items() if now - v < 7 * 86400}
    state["near"] = {k: v for k, v in state["near"].items() if now - v < 7 * 86400}
    save_state(state)

    if FORCE:
        send("📊 فحص يدوي\n\n" + "\n\n".join(report))
        send(scoreboard(gold_results))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("BOT FATAL ERROR:", repr(exc))
        send(f"⚠️ تعذر تحديث بوت الذهب:\n{str(exc)[:900]}")
        raise
