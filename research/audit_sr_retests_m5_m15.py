#!/usr/bin/env python3
"""Small, readable audit for M5/M15 support/resistance retest study."""
import csv,json,statistics,math
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
p=Path("research/results_sr_retests_m5_m15")
with (p/"trades.csv").open(newline="") as f:rows=list(csv.DictReader(f))
events=json.loads((p/"events.json").read_text())
def f(x):return float(x)
def stats(rows):
    valid=[x for x in rows if x["realized_r"]]
    vals=[f(x["realized_r"]) for x in valid]
    st=Counter(x["status"] for x in rows)
    target=[x for x in valid if x["status"]=="target"]
    timed=[x for x in valid if x["status"]=="timeout"]
    stops=[x for x in valid if x["status"]=="stop"]
    def sm(a):return round(sum(f(x["realized_r"]) for x in a),3)
    return dict(n=len(rows),valid=len(valid),status=dict(st),
        net_r=round(sum(vals),3),median_r=round(statistics.median(vals),3) if vals else None,
        median_planned_rr=round(statistics.median(f(x["planned_rr"]) for x in rows),3) if rows else None,
        median_risk=round(statistics.median(f(x["risk"]) for x in rows),3) if rows else None,
        target_r=sm(target),timeout_r=sm(timed),stop_r=sm(stops),
        top_3_r=round(sum(sorted(vals,reverse=True)[:3]),3),
        bottom_3_r=round(sum(sorted(vals)[:3]),3))
out={"scenarios":{},"touches":{},"audit":{}}
for tf in ("M5","M15"):
    e=[x for x in events if x["tf"]==tf]
    out["touches"][tf]={}
    for src in ("all","strong","weak"):
        a=[x for x in e if src=="all" or bool(x["strong"])==(src=="strong")]
        d=Counter(round(x["center"]) for x in a)
        out["touches"][tf][src]=dict(n=len(a),
            reclaim=sum("reclaim" in x["signals"] for x in a),
            break2=sum("break2" in x["signals"] for x in a),
            unique_rounded_levels=len(d),
            levels_3plus=sum(v>=3 for v in d.values()),
            max_retests_rounded=max(d.values()) if d else 0,
            top_10_levels=d.most_common(10))
    out["scenarios"][tf]={}
    for confirm in ("reclaim","break2"):
        a=[x for x in rows if x["tf"]==tf and x["confirm"]==confirm
            and x["strong"]=="True" and x["target_strong"]=="False"
            and x["rr2"]=="True"]
        out["scenarios"][tf][confirm]={}
        for period in ("all","train","test"):
            out["scenarios"][tf][confirm][period]=stats([x for x in a if period=="all" or x["period"]==period])
    best=[x for x in rows if x["tf"]==tf and x["confirm"]=="reclaim"
        and x["strong"]=="True" and x["target_strong"]=="False" and x["rr2"]=="True"
        and x["realized_r"]]
    best.sort(key=lambda x:f(x["realized_r"]),reverse=True)
    out["audit"][tf]=dict(
        top_12=[{k:x[k] for k in ("touch","signal","side","tier","zone_center",
            "entry_bid","stop_bid","target_bid","planned_rr","realized_r","status","risk","period")}
            for x in best[:12]],
        worst_8=[{k:x[k] for k in ("touch","side","tier","zone_center",
            "planned_rr","realized_r","status","risk","period")}
            for x in best[-8:]],
        min_risk=round(min(f(x["risk"]) for x in best),3) if best else None,
        under_1_risk=sum(f(x["risk"])<1 for x in best),
        under_2_risk=sum(f(x["risk"])<2 for x in best),
        top_3_timeout=sum(x["status"]=="timeout" for x in best[:3]))
# Screenshot is retrospectively chosen 3868.28, not a precommitted level.
# Only show pre-known zones with a center within $2.5 of it in Nov 16-17.
for tf in ("M5","M15"):
    out["audit"][tf]["screenshot_zone_3868"]=[
        {k:x[k] for k in ("touch","side","center","lo","hi","tier","strong","signals")}
        for x in events if x["tf"]==tf and x["touch"].startswith(("2026-11-16","2026-11-17"))
        and abs(f(x["center"])-3868.28)<=2.5][:30]
(p/"audit.json").write_text(json.dumps(out,indent=2))
print(json.dumps({k:out[k] for k in ("scenarios","touches")},indent=2))
