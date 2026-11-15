import json
import math
import os
import time
import requests

TOKEN = os.environ["BOT_TOKEN"]
CHAT = os.environ["CHAT_ID"]
FORCE = os.environ.get("FORCE") == "true"

INTERVAL = "15m"
INTERVAL_MS = 15 * 60 * 1000
ZONE = 0.0005
WICK_RATIO = 2.0
MIN_RR = 1.5
NEAR_PCT = 0.0015
NEAR_COOLDOWN = 2 * 3600
SIGNAL_LIFE = 12
BODY_AVG_N = 10
RETEST_LOOKBACK = 30

LEVEL_TFS = [
    ("1h", 500, 1, 4, "1س"),
    ("4h", 500, 2, 4, "4س"),
    ("1d", 365, 3, 3, "يومي"),
]
CLUSTER_PCT = 0.0012
MIN_SCORE = 3
STRONG_SCORE = 5
LEVEL_RANGE = 0.25
TF_ORDER = {"يومي": 0, "4س": 1, "1س": 2, "يدوي": 3}

ASSETS = {
    "BTC": {"symbol": "BTCUSDT", "margin": 60, "name": "BTC"},
    "GOLD": {"symbol": "PAXGUSDT", "margin": 3, "name": "الذهب"},
}


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def send(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": text},
        timeout=20,
    )


def get_candles(symbol, interval, limit):
    r = requests.get(
        "https://data-api.binance.vision/api/v3/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=20,
    )
    r.raise_for_status()
    now = int(time.time() * 1000)
    out = []
    for k in r.json():
        if k[6] < now:
            out.append({
                "t": k[0],
                "o": float(k[1]),
                "h": float(k[2]),
                "l": float(k[3]),
                "c": float(k[4]),
            })
    return out


def get_price(symbol):
    r = requests.get(
        "https://data-api.binance.vision/api/v3/ticker/price",
        params={"symbol": symbol},
        timeout=20,
    )
    r.raise_for_status()
    return float(r.json()["price"])


def body(c):
    return abs(c["c"] - c["o"])


def avg_body(cs, i):
    seg = cs[max(0, i - BODY_AVG_N):i]
    return sum(body(x) for x in seg) / len(seg) if seg else 0


def touches(c, level):
    return c["h"] >= level * (1 - ZONE) and c["l"] <= level * (1 + ZONE)


def rejection(c, level, side):
    rng = c["h"] - c["l"]
    if rng <= 0 or not touches(c, level):
        return False
    b = max(body(c), rng * 0.001)
    pos = (c["c"] - c["l"]) / rng
    if side == "sell":
        wick = c["h"] - max(c["o"], c["c"])
        return (wick >= WICK_RATIO * b
                and c["c"] < level * (1 - ZONE) and pos <= 1 / 3)
    wick = min(c["o"], c["c"]) - c["l"]
    return (wick >= WICK_RATIO * b
            and c["c"] > level * (1 + ZONE) and pos >= 2 / 3)


def make_plan(c, side, lv, margin):
    entry = c["c"]
    if side == "sell":
        sl = c["h"] + margin
        cands = [x for x in lv if x < entry * (1 - ZONE)]
        if not cands:
            return None
        target = max(cands)
        risk, reward = sl - entry, entry - target
    else:
        sl = c["l"] - margin
        cands = [x for x in lv if x > entry * (1 + ZONE)]
        if not cands:
            return None
        target = min(cands)
        risk, reward = entry - sl, target - entry
    if risk <= 0 or reward / risk < MIN_RR:
        return None
    return {"entry": entry, "sl": sl, "target": target, "rr": reward / risk}


def retest_dir(cs, level):
    n = len(cs)
    lo = max(BODY_AVG_N + 1, n - RETEST_LOOKBACK)
    for i in range(n - 2, lo - 1, -1):
        prev, c = cs[i - 1], cs[i]
        if body(c) <= avg_body(cs, i):
            continue
        up = c["c"] > level * (1 + ZONE) and prev["c"] <= level * (1 + ZONE)
        down = c["c"] < level * (1 - ZONE) and prev["c"] >= level * (1 - ZONE)
        after = cs[i + 1:]
        if up and all(x["c"] > level * (1 - ZONE) for x in after):
            return "buy"
        if down and all(x["c"] < level * (1 + ZONE) for x in after):
            return "sell"
    return None


def evaluate(cs, lv):
    found = []
    for back in (0, 1):
        sub = cs[:len(cs) - back]
        if len(sub) < BODY_AVG_N + 3:
            continue
        c = sub[-1]
        for level in lv:
            if rejection(c, level, "sell"):
                found.append(("رفض عند مقاومة", "sell", level, c))
            if rejection(c, level, "buy"):
                found.append(("رفض عند دعم", "buy", level, c))
            side = retest_dir(sub, level)
            if side and rejection(c, level, side):
                found.append(("كسر وإعادة اختبار", side, level, c))
    return found


def pivots(cs, k):
    out = []
    for i in range(k, len(cs) - k):
        win = cs[i - k:i + k + 1]
        if cs[i]["h"] >= max(x["h"] for x in win):
            out.append(cs[i]["h"])
        if cs[i]["l"] <= min(x["l"] for x in win):
            out.append(cs[i]["l"])
    return out


def detect_levels(symbol, price, manual):
    pts = []
    for interval, limit, weight, k, label in LEVEL_TFS:
        cs = get_candles(symbol, interval, limit)
        for p in pivots(cs, k):
            if abs(p - price) / price <= LEVEL_RANGE:
                pts.append((p, weight, label))
    for p in manual:
        pts.append((float(p), STRONG_SCORE, "يدوي"))
    pts.sort()
    clusters = []
    cur = []
    for pt in pts:
        if cur and pt[0] > cur[0][0] * (1 + CLUSTER_PCT):
            clusters.append(cur)
            cur = []
        cur.append(pt)
    if cur:
        clusters.append(cur)
    levels = []
    for cl in clusters:
        score = sum(x[1] for x in cl)
        if score < MIN_SCORE:
            continue
        center = sum(x[0] * x[1] for x in cl) / score
        labels = sorted({x[2] for x in cl}, key=lambda s: TF_ORDER[s])
        levels.append({
            "p": center,
            "score": score,
            "strong": score >= STRONG_SCORE,
            "tfs": "+".join(labels),
        })
    return levels


def bucket(level):
    return round(math.log(level) / 0.002)


def fmt(d):
    kind = "قوي" if d["strong"] else "متوسط"
    return f"{d['p']:,.2f} · {kind} {d['score']} ({d['tfs']})"


all_levels = load("levels.json", {})
state = load("state.json", {})
state.setdefault("sent", {})
state.setdefault("active", [])
state.setdefault("near", {})
now_s = time.time()
report = []

for key, a in ASSETS.items():
    name = a["name"]
    manual = all_levels.get(key, {}).get("manual", [])
    try:
        cs = get_candles(a["symbol"], INTERVAL, 80)
        price = get_price(a["symbol"])
        levels = detect_levels(a["symbol"], price, manual)
    except Exception as e:
        print("error", key, e)
        continue
    lv = [d["p"] for d in levels]
    meta = {d["p"]: d for d in levels}

    above = sorted([d for d in levels if d["p"] > price], key=lambda d: d["p"])[:3]
    below = sorted([d for d in levels if d["p"] <= price], key=lambda d: -d["p"])[:3]
    rows = [f"{name}: {price:,.2f}$ | مستويات {len(levels)}"]
    rows += ["▲ " + fmt(d) for d in reversed(above)]
    rows += ["▼ " + fmt(d) for d in below]
    report.append("\n".join(rows))

    # متابعة الإشارات المفتوحة: إلغاء / وقف / هدف
    still = []
    for s in state["active"]:
        if s["asset"] != key:
            still.append(s)
            continue
        sell = s["side"] == "sell"
        end = None
        for i, c in enumerate(cs):
            if c["t"] <= s["t"]:
                continue
            if (sell and c["h"] >= s["sl"]) or (not sell and c["l"] <= s["sl"]):
                end = "⛔ ضرب وقف الإشارة"
            elif (sell and c["l"] <= s["target"]) or (not sell and c["h"] >= s["target"]):
                end = "🎯 وصل السعر هدف الإشارة"
            elif body(c) > avg_body(cs, i) and (
                (sell and c["c"] > s["ext"]) or (not sell and c["c"] < s["ext"])
            ):
                end = "❌ إلغاء الإشارة: شمعة قوية عكس الفكرة"
            if end:
                break
        word = "بيع" if sell else "شراء"
        if end:
            send(f"{end}\n{name} {word} (دخول {s['entry']:,.2f})")
        elif sum(1 for c in cs if c["t"] > s["t"]) <= SIGNAL_LIFE:
            still.append(s)
    state["active"] = still

    # إشارات جديدة
    for kind, side, level, c in evaluate(cs, lv):
        sid = f"{key}|{kind}|{side}|{bucket(level)}|{c['t']}"
        if sid in state["sent"]:
            continue
        p = make_plan(c, side, lv, a["margin"])
        if not p:
            continue
        state["sent"][sid] = now_s
        age = int((now_s * 1000 - (c["t"] + INTERVAL_MS)) / 60000)
        ext = c["h"] if side == "sell" else c["l"]
        state["active"].append({
            "asset": key, "side": side, "entry": p["entry"],
            "sl": p["sl"], "target": p["target"], "ext": ext, "t": c["t"],
        })
        icon = "🔴" if side == "sell" else "🟢"
        word = "بيع" if side == "sell" else "شراء"
        send(
            f"{icon} إشارة {word} ({kind})\n"
            f"{name} · {INTERVAL}\n"
            f"المستوى: {fmt(meta[level])}\n"
            f"الدخول: {p['entry']:,.2f}\n"
            f"الوقف: {p['sl']:,.2f}\n"
            f"الهدف: {p['target']:,.2f}\n"
            f"النسبة: {p['rr']:.2f}\n"
            f"أُغلقت الشمعة قبل {age} دقيقة"
        )

    # اقتراب من مستوى قوي
    for d in levels:
        if not d["strong"]:
            continue
        level = d["p"]
        if level * (1 - NEAR_PCT) <= price <= level:
            word, k = "مقاومة", "R"
        elif level < price <= level * (1 + NEAR_PCT):
            word, k = "دعم", "S"
        else:
            continue
        nk = f"near|{key}|{k}|{bucket(level)}"
        if now_s - state["near"].get(nk, 0) > NEAR_COOLDOWN:
            state["near"][nk] = now_s
            dist = abs(level - price) / level * 100
            send(
                f"⚠️ {name} يقترب من {word} قوية {level:,.2f}\n"
                f"القوة: {d['score']} ({d['tfs']})\n"
                f"السعر الحالي: {price:,.2f} (يبعد {dist:.2f}%)"
            )

state["sent"] = {k: v for k, v in state["sent"].items() if now_s - v
