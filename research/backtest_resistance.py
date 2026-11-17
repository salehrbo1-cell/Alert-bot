#!/usr/bin/env python3
"""Walk-forward XAUUSD M15 resistance-pattern study.

Only closed H1/H4/D1 candles that existed BEFORE each M15 candle are used
to discover pivot levels. Signal is evaluated at the M15 close, and
execution is simulated at the NEXT M15 open. No future pivot leakage.
"""
import bisect
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ZONE = 0.00035
CLUSTER = 0.0012
RANGE = 0.25
STRONG = 5
COOLDOWN_MS = 2 * 3600 * 1000
TF = [("h1", 1, 4, "1س", 500), ("h4", 2, 4, "4س", 500),
      ("d1", 3, 3, "يومي", 365)]
ORDER = {"يومي": 0, "4س": 1, "1س": 2}


def parse_csv(path):
    raw = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            t = int(r["timestamp"])
            o, h, l, c = (float(r[k]) for k in ("open", "high", "low", "close"))
            if not all(map(math.isfinite, (o, h, l, c))):
                continue
            raw.append({"t": t, "o": o, "h": h, "l": l, "c": c})
    raw.sort(key=lambda x: x["t"])
    # Dukascopy export includes synthetic, entirely flat Sunday and
    # daily-maintenance candles. Remove these BEFORE indicators/aggregation.
    real = [x for x in raw if x["h"] > x["l"]]
    return raw, real


def aggregate(cs, duration):
    groups = {}
    ms = duration * 60_000
    for c in cs:
        start = (c["t"] // ms) * ms
        if start not in groups:
            groups[start] = {"t": start, "end": start + ms, "o": c["o"],
                             "h": c["h"], "l": c["l"], "c": c["c"]}
        else:
            a = groups[start]
            a["h"] = max(a["h"], c["h"])
            a["l"] = min(a["l"], c["l"])
            a["c"] = c["c"]
    return [groups[k] for k in sorted(groups)]


def pivots(cs, k):
    points = []
    for i in range(k, len(cs) - k):
        w = cs[i-k:i+k+1]
        if cs[i]["h"] >= max(x["h"] for x in w):
            points.append(cs[i]["h"])
        if cs[i]["l"] <= min(x["l"] for x in w):
            points.append(cs[i]["l"])
    return points


def detect_levels(tf, price):
    points = []
    for key, weight, k, label, count in TF:
        for p in pivots(tf[key][-count:], k):
            if abs(p - price) / price <= RANGE:
                points.append((p, weight, label))
    points.sort()
    clusters, current = [], []
    for p in points:
        if current and p[0] > current[-1][0] * (1 + CLUSTER):
            clusters.append(current)
            current = []
        current.append(p)
    if current:
        clusters.append(current)
    levels = []
    for cluster in clusters:
        weights = {}
        per_tf = defaultdict(list)
        for p, w, label in cluster:
            weights[label] = max(weights.get(label, 0), w)
            per_tf[label].append(p)
        score = sum(weights.values())
        if score < STRONG:
            continue
        center = sum(sum(v) / len(v) * weights[k] for k, v in per_tf.items()) / score
        levels.append({"p": center, "score": score,
                       "tfs": "+".join(sorted(weights, key=lambda k: ORDER[k]))})
    return levels


def indicators(cs):
    close = [x["c"] for x in cs]
    n = len(cs)
    def ema(vals, period):
        a = 2 / (period + 1)
        out = [vals[0]]
        for v in vals[1:]:
            out.append(a*v + (1-a)*out[-1])
        return out
    ema20 = ema(close, 20)
    fast, slow = ema(close, 12), ema(close, 26)
    hist = [a-b for a, b in zip(fast, slow)]
    sig = ema(hist, 9)
    hist = [a-b for a, b in zip(hist, sig)]
    rsi = [None]*n
    ag = al = 0
    for i in range(1, n):
        diff = close[i] - close[i-1]
        gain, loss = max(diff, 0), max(-diff, 0)
        if i <= 14:
            ag += gain
            al += loss
            if i == 14:
                ag /= 14
                al /= 14
        else:
            ag = (ag*13 + gain)/14
            al = (al*13 + loss)/14
        if i >= 14:
            rsi[i] = 100 if al == 0 else 100 - 100/(1+ag/al)
    tr = [0.0]*n
    for i in range(1, n):
        x, prev = cs[i], cs[i-1]
        tr[i] = max(x["h"]-x["l"], abs(x["h"]-prev["c"]),
                    abs(x["l"]-prev["c"]))
    atr = [None]*n
    if n > 14:
        v = sum(tr[1:15])/14
        atr[14] = v
        for i in range(15, n):
            v = (v*13+tr[i])/14
            atr[i] = v
    return {"rsi": rsi, "hist": hist, "ema20": ema20, "atr": atr}


def classify(cs, i, level, ind, filtered):
    a, b, c = cs[i-2:i+1]
    p = level["p"]
    lo, hi = p*(1-ZONE), p*(1+ZONE)
    rsi_now, rsi_prev = ind["rsi"][i], ind["rsi"][i-1]
    if rsi_now is None or rsi_prev is None:
        return None
    fall = rsi_now < rsi_prev and ind["hist"][i] < ind["hist"][i-1]
    rise = rsi_now > rsi_prev and ind["hist"][i] > ind["hist"][i-1]
    # Only historical resistance approached from below. This deliberately
    # excludes support and avoids treating the same level as both.
    if a["c"] > hi:
        return None
    if a["c"] > p * 1.005:
        return None
    # Failed break: two closes above zone followed by close below.
    # Use a separate check below, since 'a' must then be above.
    breakout = (a["c"] <= hi and b["c"] > hi and c["c"] > hi
                and c["c"] >= b["c"])
    if breakout and (not filtered or (c["c"] > ind["ema20"][i]
                      and rsi_now >= 50 and rise)):
        return "breakout"
    rejection = (max(b["h"], c["h"]) >= lo and b["c"] >= lo
                 and c["c"] < lo and c["c"] < c["o"])
    if rejection and (not filtered or fall):
        return "rejection"
    return None


def outcome(cs, i, side, distance, horizon):
    if i+1 >= len(cs):
        return None
    entry = cs[i+1]["o"]
    direction = 1 if side == "breakout" else -1
    target, stop = entry+direction*distance, entry-direction*distance
    for j in range(i+1, min(len(cs), i+1+horizon)):
        x = cs[j]
        # Exclude nontrading intervals from the outcome clock.
        if j > i+1 and x["t"] - cs[j-1]["t"] > 60*60*1000:
            return "gap"
        win = x["h"] >= target if direction == 1 else x["l"] <= target
        lose = x["l"] <= stop if direction == 1 else x["h"] >= stop
        if win and lose:
            return "ambiguous"
        if lose:
            return "loss"
        if win:
            return "win"
    return "timeout"


def main():
    source = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    raw, cs = parse_csv(source)
    print("RAW", len(raw), "REAL", len(cs), "REMOVED_FLAT", len(raw)-len(cs))
    ind = indicators(cs)
    agg = {"h1": aggregate(cs, 60), "h4": aggregate(cs, 240),
           "d1": aggregate(cs, 1440)}
    end_times = {k: [x["end"] for x in v] for k, v in agg.items()}
    # Calculate historical levels at most once per hour.
    cache = {}
    events = []
    cooldown = {}
    level_count = Counter()
    last_hour = None
    for i in range(220, len(cs)-17):
        c = cs[i]
        t = c["t"]
        if last_hour != t//3_600_000:
            last_hour = t//3_600_000
            tf = {}
            for key in agg:
                count = bisect.bisect_right(end_times[key], t)
                tf[key] = agg[key][:count]
            cache[last_hour] = detect_levels(tf, cs[i-1]["c"])
        levels = cache[last_hour]
        # Require level already known at start of current candle.
        for lev in levels:
            p = lev["p"]
            if p < cs[i-2]["c"] - p*ZONE:
                continue
            if min(abs(x["c"]-p) for x in cs[i-2:i+1]) > max(
                    2*(ind["atr"][i] or 0), p*0.004):
                continue
            # Compare the momentum filter on exactly the SAME baseline events.
            # Independent cooldowns caused a biased, unmatched comparison.
            baseline_side = classify(cs, i, lev, ind, False)
            if not baseline_side:
                continue
            key = (baseline_side, round(math.log(p)/0.002))
            if t - cooldown.get(key, -10**15) < COOLDOWN_MS:
                continue
            cooldown[key] = t
            filtered_side = classify(cs, i, lev, ind, True)
            for filtered in (False, True):
                if filtered and filtered_side != baseline_side:
                    continue
                side = baseline_side
                # Use fixed $5 first-touch and ATR-normalized alternative.
                outcome5 = outcome(cs, i, side, 5.0, 8)
                atr_dist = max(5, (ind["atr"][i] or 0)*0.75)
                outcome_atr = outcome(cs, i, side, atr_dist, 8)
                events.append({
                    "utc": datetime.fromtimestamp(t/1000, timezone.utc).isoformat(),
                    "side": side, "filtered": filtered,
                    "level": round(p, 3), "score": lev["score"],
                    "tfs": lev["tfs"], "rsi": round(ind["rsi"][i], 2),
                    "macd_slope": round(ind["hist"][i]-ind["hist"][i-1], 4),
                    "close": c["c"], "atr": round(ind["atr"][i], 2),
                    "outcome_5usd": outcome5, "outcome_atr": outcome_atr,
                })
                level_count[lev["score"]] += 1
    with (out_dir/"events.csv").open("w", newline="") as f:
        if events:
            w = csv.DictWriter(f, fieldnames=events[0].keys())
            w.writeheader()
            w.writerows(events)
    report = {
        "source": source.name,
        "raw_candles": len(raw), "usable_candles": len(cs),
        "flat_synthetic_removed": len(raw)-len(cs),
        "first_utc": datetime.fromtimestamp(cs[0]["t"]/1000, timezone.utc).isoformat(),
        "last_utc": datetime.fromtimestamp(cs[-1]["t"]/1000, timezone.utc).isoformat(),
        "method": "historically confirmed H1/H4/D1 pivots (score>=5), M15 closed, entry next open, target/stop +/-$5, 8 M15 candles, no spread/slippage; momentum-filtered events are matched subset of baseline",
        "warnings": [
            "Dukascopy BID feed differs from bot Twelve Data feed.",
            "UTC H4/daily boundaries may differ from broker.",
            "No tick data: bars hitting both target and stop are ambiguous.",
            "No transaction costs included; these are classifications, not proven profitable trades.",
            "Indicators are computed from full cleaned history, not 500-candle rolling API snapshots.",
        ],
        "groups": {}, "monthly": {},
    }
    for filtered in (False, True):
        for side in ("rejection", "breakout"):
            subset = [x for x in events if x["filtered"] == filtered and x["side"] == side]
            k = ("filtered" if filtered else "price_only")+"_"+side
            cnt = Counter(x["outcome_5usd"] for x in subset)
            determinate = cnt["win"] + cnt["loss"]
            report["groups"][k] = {
                "signals": len(subset), "outcomes": dict(cnt),
                "winrate_excluding_timeout_ambiguous": round(
                    cnt["win"]/determinate, 4) if determinate else None,
                "unique_level_buckets": len(set(round(math.log(x["level"])/0.002)
                                                for x in subset)),
            }
            for month in sorted(set(x["utc"][:7] for x in subset)):
                mm = [x for x in subset if x["utc"].startswith(month)]
                cc = Counter(x["outcome_5usd"] for x in mm)
                report["monthly"].setdefault(month, {})[k] = {
                    "signals": len(mm), "wins": cc["win"], "losses": cc["loss"],
                    "ambiguous": cc["ambiguous"], "timeout": cc["timeout"],
                    "gap": cc["gap"],
                }
    (out_dir/"summary.json").write_text(json.dumps(
        report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
