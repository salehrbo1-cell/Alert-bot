"""Independent experimental RSI14/M5 tags for Sumo paper-entry alerts.

No external requests. Never used as a mandatory trading gate. Only completed
M1 candles are aggregated into complete M5 candles at the entry timestamp.
"""
RSI_PERIOD = 14
RSI_M5_MS = 5 * 60_000


def _complete_m5(m1_bars, entry_t):
    groups = {}
    for bar in m1_bars:
        t = int(bar["t"])
        if t + 60_000 > entry_t:
            continue
        base = t // RSI_M5_MS * RSI_M5_MS
        groups.setdefault(base, {})[t] = bar
    out = []
    for base in sorted(groups):
        expected = [base + i * 60_000 for i in range(5)]
        if base + RSI_M5_MS > entry_t or any(t not in groups[base] for t in expected):
            continue
        rows = [groups[base][t] for t in expected]
        out.append({
            "t": base,
            "h": max(bar["h"] for bar in rows),
            "l": min(bar["l"] for bar in rows),
            "c": rows[-1]["c"],
        })
    return out


def _rsi_series(closes, period=RSI_PERIOD):
    out = [None] * len(closes)
    if len(closes) < period + 2:
        return out
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gain = sum(max(x, 0.0) for x in deltas[:period]) / period
    loss = sum(max(-x, 0.0) for x in deltas[:period]) / period

    def level(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[period] = level(gain, loss)
    for i in range(period + 1, len(closes)):
        delta = deltas[i - 1]
        gain = (gain * (period - 1) + max(delta, 0.0)) / period
        loss = (loss * (period - 1) + max(-delta, 0.0)) / period
        out[i] = level(gain, loss)
    return out


def _confirmed_divergence(bars, rsi):
    """Only confirmed 2-left/2-right price pivots; never use future candles."""
    highs, lows = [], []
    n = len(bars)
    for i in range(max(14, n - 24), n - 2):
        if i < 2 or rsi[i] is None:
            continue
        if all(bars[i]["h"] > bars[j]["h"] for j in (i-2, i-1, i+1, i+2)):
            highs.append(i)
        if all(bars[i]["l"] < bars[j]["l"] for j in (i-2, i-1, i+1, i+2)):
            lows.append(i)
    observed = []
    if len(highs) > 1 and n - 1 - highs[-1] <= 8:
        old, recent = highs[-2:]
        if bars[recent]["h"] > bars[old]["h"] and rsi[recent] < rsi[old]:
            observed.append((recent, "bearish"))
    if len(lows) > 1 and n - 1 - lows[-1] <= 8:
        old, recent = lows[-2:]
        if bars[recent]["l"] < bars[old]["l"] and rsi[recent] > rsi[old]:
            observed.append((recent, "bullish"))
    return max(observed, default=(-1, "none"))[1]


def evaluate_secondary_rsi(m1_bars, entry_t, side):
    """Observe momentum and divergence separately; do not block any entries."""
    try:
        m5 = _complete_m5(m1_bars, entry_t)
        latest_expected = (entry_t // RSI_M5_MS - 1) * RSI_M5_MS
        if len(m5) < 20 or m5[-1]["t"] != latest_expected:
            return {
                "status": "unavailable", "reason": "missing_or_stale_complete_m5",
                "timeframe": "5min", "period": RSI_PERIOD,
            }
        values = _rsi_series([bar["c"] for bar in m5])
        current, previous = values[-1], values[-2]
        if current is None or previous is None:
            return {"status": "unavailable", "reason": "insufficient_rsi_warmup"}
        aligned = (
            current > 50.0 and current > previous
            if side == "buy" else
            current < 50.0 and current < previous
        )
        divergence = _confirmed_divergence(m5, values)
        return {
            "status": "ok", "period": RSI_PERIOD, "timeframe": "5min",
            "value": round(current, 2), "previous": round(previous, 2),
            "momentum_matches": aligned,
            "divergence": divergence,
            "divergence_matches": (
                (side == "buy" and divergence == "bullish") or
                (side == "sell" and divergence == "bearish")
            ),
            "divergence_opposes": (
                (side == "buy" and divergence == "bearish") or
                (side == "sell" and divergence == "bullish")
            ),
            "last_complete_m5_t": m5[-1]["t"],
        }
    except Exception as error:
        # Never affect the primary SMC/M15 decision for a secondary study.
        print("SUMO_RSI_SECONDARY_ERROR", type(error).__name__)
        return {"status": "unavailable", "reason": "calculation_error"}


def secondary_rsi_message(study):
    if study.get("status") != "ok":
        return "RSI14 (M5) [ثانوي تجريبي]: غير متاح\n"
    momentum = "متوافق" if study["momentum_matches"] else "غير متوافق"
    divergence = {
        "bullish": "صعودي", "bearish": "هبوطي", "none": "لا يوجد",
    }.get(study["divergence"], "غير متاح")
    return (
        f"RSI14 (M5) [ثانوي تجريبي]: {study['value']:.2f} | الزخم: {momentum}\n"
        f"تباعد RSI: {divergence} (مراقبة مستقلة؛ لا يمنع الدخول)\n"
    )
