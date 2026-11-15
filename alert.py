import os
import requests

TOKEN = os.environ["BOT_TOKEN"]
CHAT = os.environ["CHAT_ID"]
FORCE = os.environ.get("FORCE") == "true"

ASSETS = {
    "BTC": ("BTCUSDT", 0.7),
    "الذهب": ("PAXGUSDT", 0.4),
}

def send(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": text},
        timeout=20,
    )

lines = []
alert = False
for name, (symbol, limit) in ASSETS.items():
    r = requests.get(
        "https://data-api.binance.vision/api/v3/klines",
        params={"symbol": symbol, "interval": "5m", "limit": 6},
        timeout=20,
    )
    closes = [float(c[4]) for c in r.json()]
    now, old = closes[-1], closes[-4]
    change = (now - old) / old * 100
    arrow = "🟢" if change >= 0 else "🔴"
    lines.append(f"{arrow} {name}: {now:,.2f}$ ({change:+.2f}% خلال 15 دقيقة)")
    if abs(change) >= limit:
        alert = True

if alert or FORCE:
    title = "⚠️ تنبيه حركة سعر" if alert else "📊 الأسعار الحالية"
    send(title + "\n" + "\n".join(lines))

