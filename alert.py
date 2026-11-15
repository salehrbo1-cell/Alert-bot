import json
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


def get_candles(symbol, offset):
    r = requests.get(
        "https://data-api.binance.vision/api/v3/klines",
        params={"symbol": symbol, "interval": INTERVAL, "limit": 80},
        timeout=20,
    )
    r.raise_for_status()
    now = int(time.time() * 1000)
    out = []
    for k in r.json():
        if k[6] < now:
            out.append({
                "t": k[0],
                "o": float(k[1]) + offset,
                "h": float(k[2]) + offset,
                "l": float(k[3]) + offset,
                "c": float(k[4]) + offset,
            })
    return out


def get_price(symbol, offset):
    r = requests.get(
        "https://data-api.binance.vision/api/v3/ticker/price",
        params={"symbol": symbol},
        timeout=20,
    )
    r.raise_for_status()
    return float(r.json()["price"]) + offset


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


def make_plan(c, side, sups, ress, margin):
    entry = c["c"]
    if side == "sell":
        sl = c["h"] + margin
        cands = [x for x in sups if x < entry]
        if not cands:
            return None
        target = max(cands)
        risk, reward = sl - entry, entry - target
    else:
        sl = c["l"] - margin
        cands = [x for x in ress if x > entry]
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


def evaluate(cs, sups, ress):
    found = []
    for back in (0, 1):
        sub = cs[:len(cs) - back]
        if len(sub) < BODY_AVG_N + 3:
            continue
        c = sub[-1]
        for level in ress:
            if rejection(c, level, "sell"):
                found.append(("رفض عند مقاومة", "sell", level, c))
        for level in sups:
            if rejection(c, level, "buy"):
                found.append(("رفض عند دعم", "buy", level, c))
        for level in ress + sups:
            side = retest_dir(sub, level)
            if side and rejection(c, level, side):
                found.append(("كسر وإعادة اختبار", side, level, c))
    return found


levels_cfg = load("levels.json", {})
state = load("state.json", {})
state.setdefault("sent", {})
state.setdefault("active", [])
state.setdefault("near", {})
now_s = time.time()
report = []

for key, a in ASSETS.items():
    cfg = levels_cfg.get(key, {})
    ress = sorted(cfg.get("resistance", []))
    sups = sorted(cfg.get("support", []))
    off = cfg.get("offset", 0)
    name = a["name"]
    try:
        cs = get_candles(a["symbol"], off)
        price = get_price(a["symbol"], off)
    except Exception as e:
        print("error", key, e)
        continue
    report.append(
        f"{name}: {price:,.2f}$ | مقاومات {len(ress)} | دعوم {len(sups)}"
    )

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
    for kind, side, level, c in evaluate(cs, sups, ress):
        sid = f"{key}|{kind}|{side}|{level}|{c['t']}"
        if sid in state["sent"]:
            continue
        p = make_plan(c, side, sups, ress, a["margin"])
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
            f"المستوى: {level:,.2f}\n"
            f"الدخول: {p['entry']:,.2f}\n"
            f"الوقف: {p['sl']:,.2f}\n"
            f"الهدف: {p['target']:,.2f}\n"
            f"النسبة: {p['rr']:.2f}\n"
            f"أُغلقت الشمعة قبل {age} دقيقة"
        )

    # اقتراب من مستوى
    for level in ress:
        if level * (1 - NEAR_PCT) <= price <= level * (1 + ZONE):
            k = f"near|{key}|R|{level}"
            if now_s - state["near"].get(k, 0) > NEAR_COOLDOWN:
                state["near"][k] = now_s
                dist = abs(level - price) / level * 100
                send(
                    f"⚠️ {name} يقترب من مقاومة {level:,.2f}\n"
                    f"السعر الحالي: {price:,.2f} (يبعد {dist:.2f}%)"
                )
    for level in sups:
        if level * (1 - ZONE) <= price <= level * (1 + NEAR_PCT):
            k = f"near|{key}|S|{level}"
            if now_s - state["near"].get(k, 0) > NEAR_COOLDOWN:
                state["near"][k] = now_s
                dist = abs(level - price) / level * 100
                send(
                    f"⚠️ {name} يقترب من دعم {level:,.2f}\n"
                    f"السعر الحالي: {price:,.2f} (يبعد {dist:.2f}%)"
                )

state["sent"] = {k: v for k, v in state["sent"].items() if now_s - v < 3 * 86400}
state["near"] = {k: v for k, v in state["near"].items() if now_s - v < 3 * 86400}
with open("state.json", "w") as f:
    json.dump(state, f, indent=1, sort_keys=True)

if FORCE:
    msg = "📊 فحص يدوي\n" + "\n".join(report)
    has = any(
        levels_cfg.get(k, {}).get("resistance") or levels_cfg.get(k, {}).get("support")
        for k in ASSETS
    )
    if not has:
        msg += "\nما أضفت مستويات دعم/مقاومة للحين (ملف levels.json)"
    send(msg)
