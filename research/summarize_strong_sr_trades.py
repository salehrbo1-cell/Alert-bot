#!/usr/bin/env python3
"""Compact audit of 12-month S/R trade results. No market-data re-download."""
import json,statistics
from collections import Counter,defaultdict
from pathlib import Path
p=Path("research/results_strong_sr_bounces")
a=json.loads((p/"trades.json").read_text())
out={}
for mode in ("zone","touch_wick"):
 for confirmation in ("strict","mid"):
  for rr2 in (True,False):
   name=f"{mode}_{confirmation}_{'rr2' if rr2 else 'all'}"
   v=[x for x in a if x["stop_mode"]==mode and x["hours"]==24
     and (confirmation=="mid" or x["confirmed"]=="strict")
     and (not rr2 or x["rr2"]) and x["strong"]]
   by_period={}
   for period in ("all","train","test"):
    rows=[x for x in v if period=="all" or x["period"]==period]
    valid=[x for x in rows if x["result"].get("realized_r") is not None]
    vals=sorted(x["result"]["realized_r"] for x in valid)
    wins=sorted([x for x in valid if x["result"]["realized_r"]>0],
      key=lambda x:x["result"]["realized_r"],reverse=True)
    by_period[period]=dict(n=len(rows),evaluated=len(valid),
      sum_r=round(sum(vals),4),positive=sum(x>0 for x in vals),
      stops=sum(x["result"]["status"]=="stop" for x in valid),
      r_distribution=Counter(str(round(x,1)) for x in vals),
      median_r=round(statistics.median(vals),4) if vals else None,
      top_10=[dict(time=x["time"],side=x["side"],tier=x["tier"],
        r=x["result"]["realized_r"],rr=x["result"]["planned_rr"],
        entry=x["entry_bid"],stop=x["stop_bid"],target=x["target_bid"])
        for x in wins[:10]],
      sum_top_3_r=round(sum(x["result"]["realized_r"] for x in wins[:3]),4))
   out[name]=by_period
(p/"r_distribution.json").write_text(json.dumps(out,indent=2))
print(json.dumps({k:{period:{q:z[q] for q in ("n","sum_r","sum_top_3_r","positive")}
   for period,z in v.items()} for k,v in out.items()},indent=2))
