#!/usr/bin/env python3
"""XAUUSD V2 study: can pre-known S/R characteristics predict short-term reclaim?
Uses 5,287 historical M5 touches. Research only; no volume/news/causal claims.
"""
import json,math,sys
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from market_intelligence_v2 import DAY,iso,parse,metric,calibration

NAMES=["strong","h4_level","h1_level","repeated_m15","zone_width_usd",
       "support","hour_sin","hour_cos","weekday_sin","weekday_cos",
       "past_14d_same_zone_touches","past_14d_confirmed_rate",
       "hours_since_previous_same_zone_touch"]

def load(path):
    events=json.loads(Path(path).read_text())
    result=[]
    for x in events:
        if x.get("tf")!="M5":continue
        t=parse(x["touch"])
        result.append({"time":t,"center":float(x["center"]),
            "width":float(x["hi"])-float(x["lo"]),
            "side":x["side"],"strong":int(x["strong"]),
            "tier":x["tier"],"y":int("reclaim" in x.get("signals",{}))})
    return sorted(result,key=lambda x:x["time"])

def build(rows):
    x=[];y=[];meta=[]
    # Prior labels are allowed only after the 3-bar outcome window has ended.
    # 1 hour is a conservative maturation delay for M5.
    for i,r in enumerate(rows):
        t=r["time"];d=datetime.fromtimestamp(t,timezone.utc)
        prior=[p for p in rows[:i] if 3600<=t-p["time"]<=14*DAY
               and p["side"]==r["side"]
               and abs(p["center"]-r["center"])<=max(1.0,r["width"]*.5)]
        n=len(prior);win=sum(p["y"] for p in prior)
        elapsed=(t-prior[-1]["time"])/3600 if prior else 14*24
        hour=d.hour+d.minute/60;wd=d.weekday()
        features=[
            r["strong"],int(r["tier"]=="H4"),int(r["tier"]=="H1"),
            int(r["tier"]=="M15_repeated"),r["width"],
            int(r["side"]=="S"),math.sin(2*math.pi*hour/24),
            math.cos(2*math.pi*hour/24),math.sin(2*math.pi*wd/7),
            math.cos(2*math.pi*wd/7),math.log1p(n),
            (win+1)/(n+2),math.log1p(elapsed)
        ]
        x.append(features);y.append(r["y"])
        meta.append({"time":iso(t),"side":r["side"],"center":r["center"],
                     "strength":r["strong"],"tier":r["tier"],
                     "prior_mature_touches":n})
    return np.asarray(x,dtype=float),np.asarray(y,dtype=int),meta

def split(rows):
    n=len(rows);t1=rows[int(n*.65)]["time"];t2=rows[int(n*.80)]["time"]
    tr=np.array([i for i,r in enumerate(rows) if r["time"]<t1-DAY])
    va=np.array([i for i,r in enumerate(rows) if t1+DAY<=r["time"]<t2-DAY])
    te=np.array([i for i,r in enumerate(rows) if r["time"]>=t2+DAY])
    return tr,va,te

def run(source,out):
    rows=load(source)
    if len(rows)<3000:raise SystemExit("Insufficient M5 touch events")
    x,y,meta=build(rows);tr,va,te=split(rows)
    if min(map(len,(tr,va,te)))<100:raise SystemExit("Insufficient events in split")
    base=DummyClassifier(strategy="prior").fit(x[tr],y[tr])
    models={
        "logistic":make_pipeline(StandardScaler(),
            LogisticRegression(C=.15,max_iter=2000,random_state=21)),
        "boosted":HistGradientBoostingClassifier(max_iter=110,max_leaf_nodes=8,
            min_samples_leaf=55,learning_rate=.04,l2_regularization=15,
            early_stopping=False,random_state=21)
    }
    valbase=metric(y[va],base.predict_proba(x[va])[:,1])
    val={}
    for name,m in models.items():
        m.fit(x[tr],y[tr]);val[name]=metric(y[va],m.predict_proba(x[va])[:,1])
    best=min(val,key=lambda k:val[k]["brier"])
    selected=best if val[best]["brier"]<valbase["brier"] else "constant_prior"
    model=models[best] if selected!="constant_prior" else base
    p=model.predict_proba(x[te])[:,1];pb=base.predict_proba(x[te])[:,1]
    rng=np.random.default_rng(23)
    improvement=[]
    for _ in range(1000):
        s=rng.integers(0,len(te),len(te))
        improvement.append(float(np.mean((y[te][s]-pb[s])**2-(y[te][s]-p[s])**2)))
    summary={
        "utc":datetime.now(timezone.utc).isoformat(),
        "source":str(source),
        "target":"Reclaim confirmation within touch candle or following 3 M5 candles",
        "n_all":len(rows),"n_train":len(tr),"n_validation":len(va),
        "n_holdout":len(te),"holdout_start":meta[te[0]]["time"],
        "features":NAMES,"validation_models":val,"validation_baseline":valbase,
        "selected":selected,"holdout":metric(y[te],p),
        "holdout_baseline":metric(y[te],pb),
        "holdout_brier_improvement_ci95":[round(float(v),6) for v in
           np.quantile(improvement,[.025,.975])],
        "calibration":calibration(y[te],p),
        "approved_for_live":False,
        "warnings":[
            "The target is short-term reclaim candle confirmation, NOT a profitable trade or a 5 USD bounce.",
            "M5-only events avoid comparing incompatible M15 touch detection samples.",
            "Only pre-known zone and time features are used at first touch.",
            "Past same-zone labels require >=1 hour maturity; 24h split embargo.",
            "No price momentum, traded volume, order flow, news or macro releases are present.",
            "This retrospective dataset was previously studied and is not untouched prospective data.",
            "Repeated touches can be correlated; model probabilities require forward verification."
        ]
    }
    dest=Path(out);dest.mkdir(parents=True,exist_ok=True)
    (dest/"touch_confirm_latest.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    lines=["# اختبار 5,287 لمسة تاريخية على M5 — تأكيد الارتداد",
        "","**الهدف هنا هو توقع ظهور شمعة استرداد Reclaim خلال ثلاث شموع M5 بعد اللمس، وليس ربح صفقة أو ارتداد 5 دولارات.**",
        f"- اللمسات: {len(rows)}؛ تدريب {len(tr)}؛ تحقق {len(va)}؛ اختبار {len(te)}.",
        f"- النموذج المختار وفق التحقق: {selected}.",
        f"- Brier الاختبار: {summary['holdout']['brier']} مقابل {summary['holdout_baseline']['brier']} لتوقع ثابت.",
        f"- AUC الاختبار: {summary['holdout']['auc']}.",
        f"- فرق Brier وفاصل ثقة 95%: {summary['holdout_brier_improvement_ci95']}.",
        "","المميزات المتاحة عند أول لمس فقط: نوع المنطقة، قوتها التعريفية، عرضها، توقيت اللمس، وتاريخ اللمسات المؤكدة القديمة.",
        "","**القيود:** لا يوجد حجم تداول أو ضغط أوامر حقيقي أو زخم متعدد الأطر أو أخبار. لا تداول حي ولا إثبات سببي.",
        "","نتائج مفصلة: touch_confirm_latest.json."]
    (dest/"TOUCH_CONFIRM_README.md").write_text("\n".join(lines)+"\n")
    print(json.dumps({"all":len(rows),"selected":selected,"holdout":summary["holdout"],
                      "baseline":summary["holdout_baseline"],
                      "ci":summary["holdout_brier_improvement_ci95"]}),flush=True)

if __name__=="__main__":
    if len(sys.argv)!=3:raise SystemExit("Usage: touch_confirm_v2.py events.json outdir")
    run(sys.argv[1],sys.argv[2])
