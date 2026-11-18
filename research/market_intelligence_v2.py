#!/usr/bin/env python3
"""Market Intelligence V2: first leak-resistant XAUUSD bounce prediction baseline.

Research only. Existing H1/H4 zone touch dataset; NO order-book, true volume,
economic calendar or news data. No causal claims and no live trading.
"""
import csv,json,math,sys
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score,brier_score_loss,log_loss,accuracy_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

DAY=86400
FEATURES=[
 "pivot_confluence","has_h4","zone_width_usd","support","session_london",
 "session_new_york","session_overlap","hour_sin","hour_cos","weekday_sin",
 "weekday_cos","prior_45d_touches","prior_45d_bounce_rate",
 "hours_since_prior_touch"
]
FORBIDDEN={"signal_close","confirmed_mid","confirmed_strict","period",
 "outcome_5usd_6h","outcome_5usd_24h","outcome_10usd_6h",
 "outcome_10usd_24h","outcome_20usd_24h"}
assert not FORBIDDEN.intersection(FEATURES)

def iso(ts):return datetime.fromtimestamp(ts,timezone.utc).isoformat()
def parse(ts):return datetime.fromisoformat(ts.replace("Z","+00:00")).timestamp()

def read_events(path):
    rows=[]
    with open(path,newline="") as f:
        for row in csv.DictReader(f):
            outcome=row["outcome_5usd_6h"]
            if outcome in ("gap","censored",""):
                continue
            if outcome not in ("bounce","break","no_bounce"):
                raise ValueError("Unexpected outcome: "+outcome)
            t=parse(row["time"])
            rows.append(dict(time=t,side=row["side"],
                center=float(row["zone_center"]),
                width=float(row["zone_hi"])-float(row["zone_lo"]),
                strength=int(row["strength"]),
                h4=int(row["h4"].lower()=="true"),
                session=row["session"],
                label=int(outcome=="bounce"),
                outcome=outcome,
                prior_outcome_24h=row["outcome_5usd_24h"]))
    rows.sort(key=lambda x:x["time"])
    return rows

def make_dataset(events):
    features=[];labels=[];details=[]
    for i,r in enumerate(events):
        d=datetime.fromtimestamp(r["time"],timezone.utc)
        # A prior outcome becomes eligible only after a full 24 hours.
        # This avoids accidentally using an unresolved future label.
        candidates=[]
        for p in events[:i]:
            if p["time"]>r["time"]-DAY:continue
            if r["time"]-p["time"]>45*DAY:continue
            if p["side"]!=r["side"]:continue
            if abs(p["center"]-r["center"])>max(1.0,0.5*r["width"]):continue
            candidates.append(p)
        n=len(candidates)
        wins=sum(x["prior_outcome_24h"]=="bounce" for x in candidates)
        prior_rate=(wins+1)/(n+2)
        since=(r["time"]-candidates[-1]["time"])/3600 if n else 45*24
        hour=d.hour+d.minute/60
        wd=d.weekday()
        x=[
            math.log1p(r["strength"]),r["h4"],r["width"],
            int(r["side"]=="support"),
            int(r["session"]=="london"),int(r["session"]=="new_york"),
            int(r["session"]=="overlap"),
            math.sin(2*math.pi*hour/24),math.cos(2*math.pi*hour/24),
            math.sin(2*math.pi*wd/7),math.cos(2*math.pi*wd/7),
            math.log1p(n),prior_rate,math.log1p(since)
        ]
        features.append(x);labels.append(r["label"])
        details.append({"time":iso(r["time"]),"side":r["side"],
                        "level_usd":r["center"],"outcome":r["outcome"],
                        "historical_pivots":r["strength"],
                        "previous_mature_touches":n})
    return np.array(features,dtype=float),np.array(labels,dtype=int),details

def chronological_split(events):
    n=len(events)
    c1=int(n*.65);c2=int(n*.80)
    t1=events[c1]["time"];t2=events[c2]["time"]
    # Label horizon 6h; 24h purge is deliberately conservative.
    tr=np.array([i for i,r in enumerate(events) if r["time"]<t1-DAY],dtype=int)
    va=np.array([i for i,r in enumerate(events) if t1+DAY<=r["time"]<t2-DAY],dtype=int)
    te=np.array([i for i,r in enumerate(events) if r["time"]>=t2+DAY],dtype=int)
    return tr,va,te,t1,t2

def metric(y,p):
    p=np.clip(p,1e-5,1-1e-5)
    return {"n":len(y),"base_rate":round(float(np.mean(y)),4),
            "brier":round(float(brier_score_loss(y,p)),5),
            "log_loss":round(float(log_loss(y,p,labels=[0,1])),5),
            "auc":round(float(roc_auc_score(y,p)),4) if len(set(y))>1 else None,
            "accuracy_0_5":round(float(accuracy_score(y,p>=.5)),4)}

def calibration(y,p):
    result=[]
    for lo,hi in zip(np.arange(0,1,.2),np.arange(.2,1.01,.2)):
        mask=(p>=lo)&(p<(hi if hi<1 else 1.0001))
        if mask.sum():
            result.append({"p_from":round(float(lo),2),"p_to":round(float(hi),2),
                "n":int(mask.sum()),"predicted":round(float(np.mean(p[mask])),3),
                "observed":round(float(np.mean(y[mask])),3)})
    return result

def permutation_brier(model,x,y):
    rng=np.random.default_rng(19)
    base=brier_score_loss(y,model.predict_proba(x)[:,1])
    scores=[]
    for j,name in enumerate(FEATURES):
        diffs=[]
        for _ in range(4):
            z=x.copy();z[:,j]=rng.permutation(z[:,j])
            diffs.append(brier_score_loss(y,model.predict_proba(z)[:,1])-base)
        scores.append({"feature":name,"brier_increase":round(float(np.mean(diffs)),6)})
    return sorted(scores,key=lambda z:z["brier_increase"],reverse=True)

def main(source,dest):
    events=read_events(source)
    if len(events)<300:raise SystemExit("Too few labeled events for baseline")
    x,y,details=make_dataset(events)
    tr,va,te,t1,t2=chronological_split(events)
    if min(map(len,(tr,va,te)))<60:raise SystemExit("Too few events after chronological split")
    if len(set(y[tr]))<2:raise SystemExit("Training data contains one class")
    models={
      "logistic":make_pipeline(StandardScaler(),
                              LogisticRegression(C=.15,max_iter=2000,random_state=19)),
      "boosted":HistGradientBoostingClassifier(max_iter=100,max_leaf_nodes=6,
          min_samples_leaf=35,learning_rate=.035,l2_regularization=12,
          early_stopping=False,random_state=19)
    }
    # Train-only constant-prior benchmark; no fitted model assumed superior.
    base=DummyClassifier(strategy="prior")
    base.fit(x[tr],y[tr])
    baseline_va=metric(y[va],base.predict_proba(x[va])[:,1])
    baseline_test=metric(y[te],base.predict_proba(x[te])[:,1])
    selection={}
    for name,model in models.items():
        model.fit(x[tr],y[tr])
        selection[name]=metric(y[va],model.predict_proba(x[va])[:,1])
    best=min(selection,key=lambda k:selection[k]["brier"])
    use_model=selection[best]["brier"]<baseline_va["brier"]
    chosen=best if use_model else "constant_prior"
    model=models[best] if use_model else base
    # Holdout is evaluated ONCE after validation selection.
    p=model.predict_proba(x[te])[:,1]
    test=metric(y[te],p)
    rng=np.random.default_rng(7)
    improvements=[]
    pb=base.predict_proba(x[te])[:,1]
    for _ in range(1000):
        ix=rng.integers(0,len(te),len(te))
        improvements.append(float(np.mean((y[te][ix]-pb[ix])**2-
                                          (y[te][ix]-p[ix])**2)))
    ci=np.quantile(improvements,[.025,.975])
    confident=p>=.65
    high={"n":int(confident.sum()),"observed_bounce_rate":
          round(float(np.mean(y[te][confident])),4) if confident.sum() else None}
    imp=permutation_brier(model,x[va],y[va]) if use_model else []
    report={
      "generated_utc":datetime.now(timezone.utc).isoformat(),
      "source":str(source),"target":"5 USD bounce within 6h before zone invalidation",
      "data":{"raw_eligible_events":len(events),"train":len(tr),
          "validation":len(va),"holdout":len(te),
          "train_start":iso(events[tr[0]]["time"]),
          "validation_start":iso(events[va[0]]["time"]),
          "holdout_start":iso(events[te[0]]["time"]),
          "train_validation_boundary":iso(t1),
          "validation_holdout_boundary":iso(t2),
          "embargo_hours":24},
      "features":FEATURES,"validation_models":selection,
      "validation_constant_baseline":baseline_va,
      "selected_model":chosen,"holdout_model":test,
      "holdout_constant_baseline":baseline_test,
      "holdout_brier_improvement_ci95":[round(float(q),6) for q in ci],
      "holdout_calibration":calibration(y[te],p),
      "holdout_predicted_bounce_ge_65pct":high,
      "validation_permutation_importance":imp,
      "model_beats_holdout_baseline":bool(test["brier"]<baseline_test["brier"]),
      "approved_for_live":False,
      "limitations":[
        "This is a baseline on 749 historical H1/H4 zone touches, not a finished intelligence bot.",
        "No actual order flow, traded volume, bid-ask depth, geopolitical news or timestamped macro events.",
        "Level strength measures historical confirmed pivots, not actual liquidity.",
        "Inputs at first touch only; post-touch confirmation fields are deliberately excluded.",
        "Prior outcomes are allowed only after 24 hours, with 45-day lookback.",
        "One year of already explored data is not a pristine untouched forward test.",
        "Same price area can be retested; time splits and 24h purge reduce but do not eliminate dependence.",
        "The label is a price-path event, not a profitable trade after spreads, slippage and stop execution.",
        "Feature importance is predictive association, not proof of why price moved.",
        "News-driven causal claims require timestamped independent event data and a counterfactual."
      ]}
    dest=Path(dest);dest.mkdir(parents=True,exist_ok=True)
    (dest/"latest.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    with (dest/"holdout_predictions.csv").open("w",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=["time","side","level_usd","outcome",
                     "historical_pivots","previous_mature_touches",
                     "predicted_bounce_probability","actual_bounce"])
        writer.writeheader()
        for k,pr in zip(te,p):
            writer.writerow({**details[k],"predicted_bounce_probability":round(float(pr),5),
                             "actual_bounce":int(y[k])})
    md=["# XAUUSD Market Intelligence V2 — المرحلة الأولى: نموذج ارتداد تجريبي","",
        "**هذا ليس بوت تداول ولا نموذجاً سببياً.**",
        "الهدف: توقع احتمال ارتداد الذهب 5 دولارات خلال 6 ساعات من منطقة دعم/مقاومة معروفة مسبقاً، قبل إبطال المنطقة.",
        "","## البيانات والفصل الزمني",
        f"- أحداث صالحة: {len(events)}؛ تدريب: {len(tr)}؛ تحقق: {len(va)}؛ اختبار لاحق: {len(te)}.",
        f"- بداية الاختبار الأخير: {iso(events[te[0]]['time'])}.",
        "- فصل زمني 65/15/20 مع عزل 24 ساعة حول حدود الفترات.",
        "- لا يستخدم إغلاق ساعة اللمس أو نتائج المستقبل أو أخباراً لم تكن معروفة.",
        "","## النتائج",
        f"- نموذج اختير من التحقق: **{chosen}**.",
        f"- Brier الاختبار: {test['brier']} مقابل {baseline_test['brier']} لتوقع ثابت.",
        f"- AUC الاختبار: {test['auc']} (0.5 ≈ عشوائي).",
        f"- فرق Brier (موجب أفضل) 95% bootstrap: {report['holdout_brier_improvement_ci95']}.",
        f"- عدد التوقعات >=65%: {high['n']}؛ تحقق الارتداد: {high['observed_bounce_rate']}.",
        f"- تفوق على التوقع الثابت في الاختبار: {report['model_beats_holdout_baseline']}.",
        "","## ما لا يستطيع هذا النموذج إثباته",
        "- لا يقيس ضغط المشترين الفعلي: يلزم بيانات صفقات أو دفتر أوامر.",
        "- لا يحدد سبباً جيوسياسياً: يلزم مصدر أخبار مؤرخ بتوقيت النشر والحدث.",
        "- لا يُثبت أن قوة المنطقة سببت الارتداد: يعرض علاقة قابلة للاختبار فقط.",
        "- لا يتضمن وقف خسارة أو تنفيذ صفقة؛ لا يُستخدم لإرسال توصيات.",
        "- الدراسة التاريخية الحالية خضعت لأبحاث سابقة؛ يلزم اختبار مستقبلي حقيقي.",
        "","## الملفات",
        "- latest.json: النتائج والتقسيم والقيود.",
        "- holdout_predictions.csv: كل توقع في فترة الاختبار.",
        "","## الخطوات القادمة",
        "1. توسيع سجل الأحداث إلى آلاف اللمسات عبر عدة سنوات وأطر زمنية.",
        "2. إضافة زخم وتقلبات متعددة الأطر وقياسات سابقة للحدث فقط.",
        "3. ربط أخبار وتقويم اقتصادي مؤرَّخين ومصدر حجم/تدفق أوامر إن توفر.",
        "4. معايرة احتمالات الارتداد والكسر وعدم الحسم عبر فترات سوق متعاقبة.",
        "5. اختبار استراتيجية تنفيذ ووقف/هدف مستقلة عن تدريب نموذج الاحتمالات."]
    (dest/"README.md").write_text("\n".join(md)+"\n")
    print(json.dumps({"selected":chosen,"data":report["data"],
         "holdout":test,"baseline":baseline_test,"ci":report["holdout_brier_improvement_ci95"]},
        ensure_ascii=False),flush=True)

if __name__=="__main__":
    if len(sys.argv)!=3:raise SystemExit("Usage: market_intelligence_v2.py touches.csv output_dir")
    main(sys.argv[1],sys.argv[2])
