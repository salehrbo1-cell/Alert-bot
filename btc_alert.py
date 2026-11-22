import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

import requests

# ============================================================
# BITCOIN SIGNAL BOT - BTC/USD via Twelve Data
# Signals only: NO order execution.
# GitHub Secrets required:
# BOT_TOKEN, CHAT_ID, TWELVE_DATA_API_KEY
# ============================================================

TOKEN = os.environ["BOT_TOKEN"]
CHAT = os.environ["CHAT_ID"]
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
FORCE = os.environ.get("FORCE", "").lower() == "true"

SYMBOL = "BTC/USD"
ASSET = "BTC"
NAME = "البتكوين"
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
MAX_ACTIVE_PER_SIDE = 1
SLIPPAGE_USD_PER_SIDE = float(os.environ.get("BTC_SLIPPAGE_USD_PER_SIDE", "0"))
SAUDI_TZ = timezone(timedelta(hours=3))

LEVEL_TFS = [
    ("1h", 500, 1, 4, "1س"),
    ("4h", 500, 2, 4, "4س"),
    ("1day", 365, 3, 3, "يومي"),
]
TF_ORDER = {"يومي": 0, "4س": 1, "1س": 2, "يدوي": 3}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "BitcoinSignalBot/1.0"})


def load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_state(state):
    with open("btc_state.json", "w", encoding="utf-8") as f:
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
            print(
                "Telegram rejected message:",
                result.get("error_code"),
                str(result.get("description", ""))[:180],
            )
            return False
        message_id = result.get("result", {}).get("message_id")
        print(f"Telegram delivered message_id={message_id}")
        return True
    except Exception as exc:
        # Do not print request URLs, which may contain the Telegram bot token.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        print("Telegram send error:", type(exc).__name__, "HTTP", status)
        return False


def td_get(endpoint, params):
    query = dict(params)
    query["apikey"] = API_KEY
    for attempt in range(3):
        try:
            r = SESSION.get(f"{BASE_URL}/{endpoint}", params=query, timeout=25)
        except requests.RequestException as exc:
            safe_message = str(exc).replace(API_KEY, "[REDACTED]")
            raise RuntimeError(
                f"Twelve Data request failed ({type(exc).__name__}): {safe_message[:300]}"
            ) from None
        if r.status_code == 429 and attempt < 2:
            retry_after = r.headers.get("Retry-After")
            try:
                wait_seconds = min(max(int(retry_after), 1), 60) if retry_after else 30 * (attempt + 1)
            except (TypeError, ValueError):
                wait_seconds = 30 * (attempt + 1)
            print(f"Twelve Data rate limit on {endpoint}; retrying in {wait_seconds}s.")
            time.sleep(wait_seconds)
            continue
        if not r.ok:
            safe_params = {key: value for key, value in query.items() if key.lower() != "apikey"}
            raise RuntimeError(
                f"Twelve Data HTTP {r.status_code} at {endpoint}; params={safe_params}"
            )
        break
    data = r.json()
    if isinstance(data, dict) and data.get("status") == "error":
        message = str(data.get("message") or "Unknown provider error").replace(
            API_KEY, "[REDACTED]"
        )
        raise RuntimeError(f"Twelve Data error {data.get('code')}: {message}")
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


# Pattern monitoring is informational; it does not create trade entries.
# Confirmations use CLOSED M15 candles only (no future data).
PATTERN_MIN_SCORE = 5
PATTERN_COOLDOWN = 2 * 3600


def pattern_momentum(candles):
    closes = [x["c"] for x in candles]
    rsi_now = rsi(closes)
    rsi_prev = rsi(closes[:-1])
    hist = macd_hist(closes)
    ema20_now = ema(closes, 20)[-1]
    return {
        "rsi_now": rsi_now,
        "rsi_prev": rsi_prev,
        "macd_now": hist[-1],
        "macd_prev": hist[-2],
        "ema20": ema20_now,
    }


def classify_level_pattern(candles, level, m):
    if len(candles) < 60:
        return None
    a, b, c = candles[-3:]
    upper = level * (1 + ZONE_PCT)
    lower = level * (1 - ZONE_PCT)
    rsi_falling = m["rsi_now"] < m["rsi_prev"]
    macd_falling = m["macd_now"] < m["macd_prev"]
    rsi_rising = m["rsi_now"] > m["rsi_prev"]
    macd_rising = m["macd_now"] > m["macd_prev"]

    # A break that had closed above resistance, then lost that area.
    if (a["c"] > upper and b["c"] > upper and c["c"] < lower
            and c["c"] < c["o"] and rsi_falling and macd_falling):
        return ("failed_break", "⚠️ فشل اختراق مقاومة",
                "عاد الإغلاق أسفل نطاق المقاومة مع تراجع RSI وزخم MACD")

    # First candle crosses above the zone; next CLOSED candle holds above it.
    if (a["c"] <= upper and b["c"] > upper and c["c"] > upper
            and c["c"] >= b["c"] and c["c"] > m["ema20"]
            and m["rsi_now"] >= 50 and rsi_rising and macd_rising):
        return ("breakout", "🟢 تأكيد اختراق مقاومة",
                "إغلاقان متتاليان فوق النطاق مع استمرار الزخم الصاعد")

    # A retest of resistance, followed by a bearish close below the zone.
    if (max(b["h"], c["h"]) >= lower and b["c"] >= lower
            and c["c"] < lower and c["c"] < c["o"]
            and rsi_falling and macd_falling):
        return ("rejection", "🔴 رفض هابط من مقاومة",
                "فشل الثبات قرب المقاومة مع تراجع RSI وزخم MACD")

    # Price is still above the zone, but the breakout is losing momentum.
    if (b["c"] > upper and c["c"] > upper and c["c"] < b["c"]
            and rsi_falling and macd_falling):
        return ("weakening", "🟡 اختراق يفقد الزخم",
                "السعر فوق المقاومة لكن RSI وزخم MACD يتراجعان")

    return None


def send_level_patterns(candles, levels, state, now, age_min):
    if age_min < -0.1 or age_min > MAX_SIGNAL_AGE_MIN:
        return
    state.setdefault("patterns", {})
    m = pattern_momentum(candles)
    latest = candles[-1]
    closed_at = datetime.fromtimestamp(
        (latest["t"] + INTERVAL_MS) / 1000, timezone.utc
    ).astimezone(timezone(timedelta(hours=3)))
    for level_data in levels:
        if level_data["score"] < PATTERN_MIN_SCORE:
            continue
        level = level_data["p"]
        # Avoid unrelated distant levels and excessive alerts.
        if min(abs(x["c"] - level) for x in candles[-3:]) > max(
                2 * atr(candles), level * 0.004):
            continue
        pattern = classify_level_pattern(candles, level, m)
        if not pattern:
            continue
        kind, title, reason = pattern
        key = f"{ASSET}|{kind}|{level_bucket(level)}"
        if now - state["patterns"].get(key, 0) < PATTERN_COOLDOWN:
            continue
        delivered = send(
            f"{title} (M15 مغلقة)\n"
            f"المستوى: {fmt(level_data)}\n"
            f"إغلاق الشمعة: {latest['c']:,.2f}\n"
            f"RSI(14): {m['rsi_prev']:.1f} ← {m['rsi_now']:.1f}\n"
            f"زخم MACD: {'يتحسن' if m['macd_now'] > m['macd_prev'] else 'يتراجع'}\n"
            f"EMA20: {m['ema20']:,.2f}\n"
            f"التفسير: {reason}\n"
            f"وقت إغلاق الشمعة (السعودية): {closed_at:%Y-%m-%d %H:%M}\n"
            f"مراقبة تحليلية، وليست توصية دخول مؤكدة."
        )
        if delivered:
            state["patterns"][key] = now


def summarize(results):
    count = len(results)
    wins = sum(1 for x in results if x["r"] > 0)
    losses = sum(1 for x in results if x["r"] < 0)
    rate = wins / (wins + losses) * 100 if wins + losses else 0
    net_r = sum(x["r"] for x in results)
    return count, wins, losses, rate, net_r


def scoreboard(results):
    if not results:
        return "📈 لوحة نتائج البتكوين\nلا توجد إشارات منتهية حتى الآن."
    n, wins, losses, rate, net = summarize(results)
    lines = [
        "📈 لوحة نتائج البتكوين (بوحدة R، وليست دولارات)",
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
    state = load("btc_state.json", {})
    state.setdefault("sent", {})
    state.setdefault("active", [])
    state.setdefault("near", {})
    state.setdefault("results", [])
    if "last_direction" not in state:
        previous_signals = [x for x in state["active"] if x.get("asset") == ASSET]
        if previous_signals:
            state["last_direction"] = previous_signals[-1]["side"]

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

    # Legacy active signals are consolidated to one directional paper position.
    btc_active = [x for x in state["active"] if x.get("asset") == ASSET]
    other_active = [x for x in state["active"] if x.get("asset") != ASSET]
    if btc_active and not state.get("position"):
        last = btc_active[-1]
        state["position"] = dict(last, opened_at=last.get("t", int(now * 1000)),
                                 max_favorable=0.0, max_adverse=0.0)
    state["active"] = other_active

    position = state.get("position")
    if position:
        is_sell = position["side"] == "sell"
        # Observe price extremes since entry, including completed candles.
        observed = [c for c in candles if c["t"] >= position.get("t", 0)]
        observed.append({"h": price, "l": price})
        for c in observed:
            favorable = (position["entry"] - c["l"]) if is_sell else (c["h"] - position["entry"])
            adverse = (c["h"] - position["entry"]) if is_sell else (position["entry"] - c["l"])
            position["max_favorable"] = max(position.get("max_favorable", 0), favorable)
            position["max_adverse"] = max(position.get("max_adverse", 0), adverse)

    latest = candles[-1]
    age_min = (time.time() * 1000 - (latest["t"] + INTERVAL_MS)) / 60000
    send_level_patterns(candles, levels, state, now, age_min)
    if age_min < -0.1:
        raise RuntimeError("Latest candle is not fully closed.")
    if age_min <= MAX_SIGNAL_AGE_MIN:
        candidates = evaluate(candles, signal_levels)
        for kind, side, level, candle in candidates:
            sid = f"{ASSET}|{side}|{level_bucket(level)}|{candle['t']}"
            if sid in state["sent"]:
                continue

            # Only an opposite valid signal can replace the current direction.
            if side == state.get("last_direction"):
                continue

            conf, trend_aligned = confluence(mom, side, kind)
            if not trend_aligned or conf < MIN_CONFLUENCE:
                continue

            plan = make_plan(candle, side, signal_levels, atr_value, price)
            if not plan:
                continue

            # Close the previous paper position at the opposite signal price.
            previous = state.get("position")
            if previous:
                prev_sell = previous["side"] == "sell"
                entry_fill = previous["entry"] - SLIPPAGE_USD_PER_SIDE if prev_sell else previous["entry"] + SLIPPAGE_USD_PER_SIDE
                exit_fill = price + SLIPPAGE_USD_PER_SIDE if prev_sell else price - SLIPPAGE_USD_PER_SIDE
                pnl_per_btc = entry_fill - exit_fill if prev_sell else exit_fill - entry_fill
                closed_at = datetime.now(SAUDI_TZ)
                state["results"].append({
                    "asset": ASSET, "kind": previous.get("kind", "غير معروف"),
                    "side": previous["side"], "outcome": "إشارة معاكسة",
                    "entry": previous["entry"], "exit": price,
                    "pnl_per_btc": round(pnl_per_btc, 2),
                    "closed_at_saudi": closed_at.isoformat(),
                    "t": previous.get("t", 0),
                    "max_favorable": previous.get("max_favorable", 0),
                    "max_adverse": previous.get("max_adverse", 0),
                })
                send(
                    f"🔄 إغلاق افتراضي {'بيع' if prev_sell else 'شراء'} بسبب إشارة معاكسة\n"
                    f"الدخول: {previous['entry']:,.2f} · الخروج: {price:,.2f}\n"
                    f"حركة الصفقة لكل 1 BTC: {pnl_per_btc:+.2f}$\n"
                    f"أقصى حركة لصالح الصفقة: {previous.get('max_favorable', 0):.2f}$\n"
                    f"أقصى حركة ضد الصفقة: {previous.get('max_adverse', 0):.2f}$"
                )
            if previous:
                state["position"] = None

            is_sell = side == "sell"
            icon = "🔴" if is_sell else "🟢"
            word = "بيع" if is_sell else "شراء"
            level_info = min(levels, key=lambda x: abs(x["p"] - level))
            send(
                f"{icon} إشارة {word} بتكوين ({kind})\n"
                f"BTC/USD · M15\n"
                f"المستوى: {fmt(level_info)}\n"
                f"الدخول التقريبي: {plan['entry']:,.2f}\n"
                f"مستوى وقف مرجعي: {plan['sl']:,.2f}\n"
                f"الهدف الفني المرجعي: {plan['target']:,.2f}\n"
                f"العائد/المخاطرة: {plan['rr']:.2f}R\n"
                f"توافق المؤشرات: {conf}/5 ({grade(conf)})\n"
                f"الاتجاه H1 وH4: {'متوافق' if trend_aligned else 'غير متوافق'}\n"
                f"{describe(mom)}\n"
                f"عمر الشمعة المغلقة: {max(0, age_min):.1f} دقيقة\n"
                f"المتابعة الافتراضية تستمر حتى ظهور إشارة معاكسة؛ مستويات الوقف والهدف مرجعية فقط. لا يوجد تنفيذ تداول."
            )
            if delivered:
                state["sent"][sid] = now
                state["last_direction"] = side
                state["position"] = {
                    "asset": ASSET, "side": side, "entry": plan["entry"],
                    "sl": plan["sl"], "target": plan["target"],
                    "t": candle["t"], "opened_at": int(now * 1000),
                    "kind": kind, "conf": conf,
                    "max_favorable": 0.0, "max_adverse": 0.0,
                }
            break
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
            title = f"📍 البتكوين {verb} {word} {strength_f(level_data['score'])} {level:,.2f}"
            reason = diagnose(candles, level, signal_levels)
        elif near:
            key = f"near|{ASSET}|{level_bucket(level)}"
            distance = abs(level - price) / level * 100
            title = f"⚠️ البتكوين يقترب من {word} قوية {level:,.2f} (يبعد {distance:.2f}%)"
            reason = ""
        else:
            continue

        if now - state["near"].get(key, 0) > COOLDOWN:
            message = (
                f"{title}\n"
                f"القوة: {level_data['score']} ({level_data['tfs']})\n"
                f"السعر الحالي: {price:,.2f}\n"
                f"الاتجاه والمؤشرات: {describe(mom)}"
            )
            if reason:
                message += f"\nالشمعة: {reason}"
            if send(message):
                state["near"][key] = now

    if report and (FORCE or now - state.get("digest", 0) > DIGEST_EVERY):
        if send("🗺️ تحديث مستويات البتكوين\n\n" + "\n\n".join(report)):
            state["digest"] = now

    # Track only BTC results created by this independent paper monitor.
    # Only score valid GOLD results produced by the current R-based strategy.
    btc_results = [
        x for x in state["results"]
        if x.get("asset") == ASSET and isinstance(x.get("r"), (int, float))
    ]
    if btc_results and (FORCE or now - state.get("score_t", 0) > SCORE_EVERY):
        if send(scoreboard(btc_results)):
            state["score_t"] = now

    # Send the report for the most recently completed Saudi calendar day.
    saudi_now = datetime.now(SAUDI_TZ)
    yesterday = (saudi_now - timedelta(days=1)).date().isoformat()
    if state.get("daily_report_date") != yesterday:
        day_results = [
            x for x in state["results"]
            if x.get("asset") == ASSET
            and str(x.get("closed_at_saudi", ""))[:10] == yesterday
            and isinstance(x.get("pnl_per_btc"), (int, float))
        ]
        buys = sum(x["side"] == "buy" for x in day_results)
        sells = sum(x["side"] == "sell" for x in day_results)
        profit = sum(max(0, x["pnl_per_btc"]) for x in day_results)
        loss = sum(min(0, x["pnl_per_btc"]) for x in day_results)
        net = profit + loss
        open_position = state.get("position")
        open_info = ("لا توجد" if not open_position else
                     ("شراء" if open_position["side"] == "buy" else "بيع"))
        if send(
            f"📊 تقرير البتكوين اليومي | {yesterday} (السعودية)\n"
            f"صفقات شراء مغلقة: {buys}\nصفقات بيع مغلقة: {sells}\n"
            f"إجمالي الصفقات المغلقة: {len(day_results)}\n"
            f"الحركة الإيجابية لكل 1 BTC: {profit:+.2f}$\nالحركة السلبية لكل 1 BTC: {loss:+.2f}$\n"
            f"الصافي لكل 1 BTC: {net:+.2f}$\n"
            f"الصفقة المستمرة: {open_info}\n"
            f"الحساب لكل 1 BTC؛ انزلاق مفترض {SLIPPAGE_USD_PER_SIDE:.2f}$ لكل تنفيذ. لا يمثل ربح اللوت في MT5."
        ):
            state["daily_report_date"] = yesterday

    state["patterns"] = {
        k: v for k, v in state.get("patterns", {}).items()
        if now - v < 7 * 86400
    }
    # Keep enough closed positions for daily reporting across interruptions.
    state["results"] = state["results"][-5000:]
    state["sent"] = {k: v for k, v in state["sent"].items() if now - v < 7 * 86400}
    state["near"] = {k: v for k, v in state["near"].items() if now - v < 7 * 86400}
    save_state(state)

    if FORCE:
        send("📊 فحص يدوي\n\n" + "\n\n".join(report))
        send(scoreboard(btc_results))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        safe_message = str(exc).replace(API_KEY, "[REDACTED]")
        print("BOT FATAL ERROR:", type(exc).__name__, safe_message[:400])
        send(f"⚠️ تعذر تحديث بوت البتكوين:\n{safe_message[:500]}")
        raise
