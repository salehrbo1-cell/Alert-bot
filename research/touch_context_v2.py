#!/usr/bin/env python3
"""Gold market-intelligence V2: pre-touch M15 context vs zone-only baselines.

M5 zone touches from 2026-05 onward; only *completed* M15 candles at touch time.
No volume, news or order-flow inference. Research-only, no live trading.
"""
import bisect,csv,json,math,sys
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from market_intelligence_v2 import DAY,iso,metric,calibration
from touch_confirm_v2 import load,build,split,NAMES

CTX_NAMES=["m15_momentum_3_atr","m15_momentum_12_atr",
           "m15_momentum_48_atr","m15_acceleration_atr",
           "m15_atr_ratio_48","m15_body_atr","m15_upper_wick_ratio",
           "m15_lower_wick_ratio","m15_close_location",
           "m15_price_vs_sma20_atr","m15_range_4bars_atr",
           "m15_position_48bar_range","m15_distance_from_zone_atr",
           "m15_directional_close_location_proxy"]
STEP=900

def bars(path):
    rows=[]
    with open(path,newline="") as f:
        for r in csv.DictReader(f):
            try:
                t=int(float(r["timestamp"])/1000)
                o,h,l,c=[float(r[k]) for k in ("open","high","low","close")]
                if math.isfinite(o+h+l+c) and h>=max(o,c) and l<=min(o,c):
                    rows.append((t,o,h,l,c))
            except (ValueError,KeyError):pass
    return sorted({r[0]:r for r in rows}.values())

def context(b,ends,t,level):
    # A M15 candle ending after the touch timestamp must NEVER be used.
    idx=bisect.bisect_right(ends,t)-1
    if idx<55 or t-ends[idx]>STEP:return None
    w=b[idx-52:idx+1]
    if any(w[k+1][0]-w[k][0]!=STEP for k in range(len(w)-1)):return None
    close=np.array([r[4] for r in w]);high=np.array([r[2] for r in w])
    low=np.array([r[3] for r in w])
    tr=np.maximum(high-low,np.maximum(abs(high-np.roll(close,1)),
                                      abs(low-np.roll(close,1))))
    atr=float(np.mean(tr[-14:]));atr48=float(np.mean(tr[-48:]))
    if atr<=.01:return None
    last=w[-1];o,h,l,c=last[1:];span=max(h-l,.001)
    body=c-o;upper=h-max(o,c);lower=min(o,c)-l
    h48=float(max(high[-48:]));l48=float(min(low[-48:]))
    r3=(c-close[-4])/atr;r12=(c-close[-13])/atr
    r48=(c-close[-49])/atr
    proxy=((c-l)/span-.5)*2  # OHLC-only proxy, NOT actual buyer volume.
    return [r3,r12,r48,r3-r12/4,atr/atr48,
            body/atr,upper/span,lower/span,(c-l)/span,
            (c-float(np.mean(close[-20:])))/atr,
            (float(max(high[-4:]))-float(min(low[-4:])))/atr,
            (c-l48)/max(h48-l48,.001),
            (c-level)/atr,proxy]

def train(events_path,ohlc_path,dest):
    raw=load(events_path)
    zone_x,y_all,meta_all=build(raw)
    candles=bars(ohlc_path);ends=[r[0]+STEP for r in candles]
    filtered=[];zs=[];cx=[];ys=[];meta=[]
    for i,r in enumerate(raw):
        feat=context(candles,ends,r["time"],r["center"])
        if feat is None:continue
        filtered.append(r);zs.append(zone_x[i]);cx.append(feat)
        ys.append(y_all[i]);meta.append(meta_all[i])
    if len(filtered)<1000:raise SystemExit("Insufficient completed M15 contexts")
    z=np.array(zs,dtype=float);xc=np.hstack((z,np.array(cx,dtype=float)))
    y=np.array(ys,dtype=int)
    tr,va,te=split(filtered)
    if min(map(len,(tr,va,te)))<100:raise SystemExit("Insufficient chronological data")
    base=DummyClassifier(strategy="prior").fit(z[tr],y[tr])
    options={}
    for family,data in (("zone_only",z),("zone_plus_m15",xc)):
        options[family+"_logistic"]=(make_pipeline(StandardScaler(),
            LogisticRegression(C=.15,max_iter=2000,random_state=29)),data)
        options[family+"_boosted"]=(HistGradientBoostingClassifier(
            max_iter=110,max_leaf_nodes=8,min_samples_leaf=55,
            learning_rate=.04,l2_regularization=15,early_stopping=False,
            random_state=29),data)
    validation={}
    for name,(model,data) in options.items():
        model.fit(data[tr],y[tr])
        validation[name]=metric(y[va],model.predict_proba(data[va])[:,1])
    val_base=metric(y[va],base.predict_proba(z[va])[:,1])
    best=min(validation,key=lambda k:validation[k]["brier"])
    chosen=best if validation[best]["brier"]<val_base["brier"] else "constant_prior"
    if chosen=="constant_prior":p=base.predict_proba(z[te])[:,1]
    else:
        model,data=options[chosen]
        p=model.predict_proba(data[te])[:,1]
    pb=base.predict_proba(z[te])[:,1]
    # Holdout metrics of other models are for diagnosis, not selection.
    test_all={name:metric(y[te],model.predict_proba(data[te])[:,1])
              for name,(model,data) in options.items()}
    rng=np.random.default_rng(47);improvements=[]
    for _ in range(1000):
        ix=rng.integers(0,len(te),len(te))
        improvements.append(float(np.mean((y[te][ix]-pb[ix])**2-
                                          (y[te][ix]-p[ix])**2)))
    report={
        "generated_utc":datetime.now(timezone.utc).isoformat(),
        "source_events":str(events_path),"source_m15":str(ohlc_path),
        "target":"M5 reclaim confirmation in touch or next three M5 candles",
        "raw_m5_touches":len(raw),"usable_m15_context_touches":len(filtered),
        "train":len(tr),"validation":len(va),"holdout":len(te),
        "holdout_start":meta[te[0]]["time"],
        "zone_features":NAMES,"additional_context_features":CTX_NAMES,
        "validation_baseline":val_base,"validation_models":validation,
        "selected":chosen,
        "holdout_selected":metric(y[te],p),
        "holdout_baseline":metric(y[te],pb),
        "holdout_diagnostic_models":test_all,
        "holdout_brier_improvement_ci95":[round(float(v),6) for v in
            np.quantile(improvements,[.025,.975])],
        "calibration":calibration(y[te],p),
        "approved_for_live":False,
        "limitations":[
            "M15 context uses only fully closed candles at first M5 zone touch.",
            "All features derived from BID OHLC; close-location is NOT real buyer/seller order flow.",
            "No volume, market depth, timestamped news, economic surprises or geopolitics.",
            "This is a short-term reclaim target, NOT price direction over hours or trade profitability.",
            "The historical sample has been repeatedly studied; prospective paper test is required.",
            "Temporal embargo 24h; correlated repeated zone touches may still inflate effective sample.",
            "The selected model is based on validation only, never on holdout."
        ]}
    dest=Path(dest);dest.mkdir(parents=True,exist_ok=True)
    (dest/"context_latest.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    lines=["# تجربة سياق السوق قبل لمس المنطقة — M15 + M5",
        "",f"- لمسـات M5 الأصلية: {len(raw)}؛ ذات سياق M15 مكتمل: {len(filtered)}.",
        f"- تدريب {len(tr)}، تحقق {len(va)}، اختبار {len(te)}.",
        f"- النموذج المختار من التحقق: **{chosen}**.",
        f"- Brier الاختبار: {report['holdout_selected']['brier']} مقابل {report['holdout_baseline']['brier']} للتوقع الثابت.",
        f"- AUC الاختبار: {report['holdout_selected']['auc']}.",
        f"- فاصل ثقة تحسن Brier 95%: {report['holdout_brier_improvement_ci95']}.",
        "","يضيف زخم 3/12/48 شمعة، تسارعاً، ATR، موقع الإغلاق، نسبة الذيول، وبعد السعر عن المتوسط والمنطقة، **قبل اللمس**.",
        "","مهم: هذه مؤشرات OHLC وليست حجم أوامر فعلياً. لم تُربط أخبار أو تقارير اقتصادية بعد.",
        "لا يتداول ولا يغيّر alert.py. النتائج التاريخية لا تثبت أفضلية مستقبلية."]
    (dest/"CONTEXT_README.md").write_text("\n".join(lines)+"\n")
    print(json.dumps({"usable":len(filtered),"selected":chosen,
       "holdout":report["holdout_selected"],"baseline":report["holdout_baseline"],
       "diagnostic":test_all},ensure_ascii=False),flush=True)

if __name__=="__main__":
    if len(sys.argv)!=4:raise SystemExit("Usage: touch_context_v2.py events.json m15.csv outdir")
    train(*sys.argv[1:])
