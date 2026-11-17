#!/usr/bin/env python3
"""Exploratory multi-feature reversal model.

Train: September to mid-November 2026 (chronological train/validation).
Independent *previously unexamined* retrospective test: August 2026.
August is earlier than training, so NOT a prospective test.
No lookahead in features; signal on closed M1, next-open entry.
"""
import csv,sys,json,math
from collections import defaultdict,Counter
from datetime import datetime,timezone
from pathlib import Path
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score,average_precision_score

M=60000
def read(path):
    a=[]
    with open(path,newline="") as f:
        for r in csv.DictReader(f):
            try:
                t=int(float(r["timestamp"]))
                o,h,l,c=[float(r[k]) for k in ("open","high","low","close")]
                if h>l and all(math.isfinite(x) for x in (o,h,l,c)):
                    a.append((t,o,h,l,c))
            except (KeyError,ValueError):pass
    return sorted(a)

def calc(rows):
    n=len(rows)
    before=[1]*n;after=[1]*n
    for i in range(1,n):
        if rows[i][0]-rows[i-1][0]==M:before[i]=before[i-1]+1
    for i in range(n-2,-1,-1):
        if rows[i+1][0]-rows[i][0]==M:after[i]=after[i+1]+1
    out=[];last={}
    for i in range(65,n-11):
        if before[i]<65 or after[i]<12:continue
        t,o,h,l,c=rows[i];p=rows[i-1]
        prior=rows[i-32:i-2];top=max(x[2] for x in prior);bottom=min(x[3] for x in prior)
        r60=np.array([x[2]-x[3] for x in rows[i-62:i-2]])
        vol=max(.05,float(r60.mean()));threshold=max(.20,.15*vol)
        side=0;level=0.
        if p[2]>top+threshold and p[4]>top and c<o and c<p[4] and h<=p[2]:
            side=-1;level=top
        elif p[3]<bottom-threshold and p[4]<bottom and c>o and c>p[4] and l>=p[3]:
            side=1;level=bottom
        if not side:continue
        if t-last.get(side,-10**18)<20*M:continue
        last[side]=t
        entry=rows[i+1][1];outcome="timeout"
        for j in range(i+1,i+11):
            hh,ll=rows[j][2],rows[j][3]
            fav=(hh-entry)*side if side==1 else entry-ll
            adv=entry-ll if side==1 else hh-entry
            if fav>=5 and adv>=3:outcome="ambiguous";break
            if fav>=5:outcome="win";break
            if adv>=3:outcome="loss";break
        # Current and past candle data only, normalize to recent price range.
        prev=rows[i-60:i]
        close=np.array([x[4] for x in prev]+[c])
        ranges=np.array([x[2]-x[3] for x in prev])
        bodies=np.array([abs(x[4]-x[1]) for x in prev])
        pbody=abs(p[4]-p[1]);b=abs(c-o)
        pw=(p[2]-max(p[1],p[4])) if side==-1 else (min(p[1],p[4])-p[3])
        cw=(h-max(o,c)) if side==-1 else (min(o,c)-l)
        high60=max(x[2] for x in prev);low60=min(x[3] for x in prev)
        mean5=float(ranges[-5:].mean());mean10=float(ranges[-10:].mean())
        hour=datetime.fromtimestamp(t/1000,timezone.utc).hour
        features=[
            side, # direction (buy vs sell)
            (p[2]-p[3])/vol,(h-l)/vol,pbody/vol,b/vol,
            pbody/max(.05,float(np.median(bodies[-20:]))),
            b/max(.05,pbody),pw/max(.05,p[2]-p[3]),cw/max(.05,h-l),
            (p[4]-level)*(-side)/vol,(c-level)*(-side)/vol,
            (p[2]-top)/vol if side==-1 else (bottom-p[3])/vol,
            mean5/vol,mean10/vol,float(ranges[-15:].mean())/vol,
            float(ranges[-30:].std())/vol,float(ranges[-10:].std())/vol,
            (high60-low60)/vol,
            (close[-1]-close[-2])*side/vol,
            (close[-1]-close[-4])*side/vol,
            (close[-1]-close[-6])*side/vol,
            (close[-1]-close[-11])*side/vol,
            (close[-1]-close[-16])*side/vol,
            (close[-1]-close[-31])*side/vol,
            (close[-1]-close[-61])*side/vol,
            (close[-1]-float(close[-20:].mean()))*side/vol,
            (close[-1]-float(close[-60:].mean()))*side/vol,
            (c-low60)/max(.05,high60-low60),
            (p[4]-p[1])*side/vol,
            (c-o)*side/vol,
            (p[4]-c)*(-side)/vol,
            (p[2]-h)/vol,(l-p[3])/vol,
            float(np.median(ranges[-20:]))/vol,
            float(np.quantile(ranges[-30:],.9))/vol,
            math.sin(2*math.pi*hour/24),math.cos(2*math.pi*hour/24),
            int(12<=hour<18),int(7<=hour<12),
            (t//M)%60/60,
        ]
        if all(math.isfinite(x) for x in features):
            out.append({"t":t,"x":features,"outcome":outcome,"y":int(outcome=="win")})
    return out

def report(data,pred,threshold):
    if len(data)==0:return {"n":0}
    mask=pred>=threshold
    selected=[x for x,m in zip(data,mask) if m]
    ct=Counter(x["outcome"] for x in selected)
    allct=Counter(x["outcome"] for x in data)
    return {"all_n":len(data),"all_win":allct["win"],"all_win_rate":round(allct["win"]/len(data),4),
            "threshold":round(float(threshold),4),"selected_n":len(selected),
            "selected_win":ct["win"],"selected_loss":ct["loss"],
            "selected_timeout":ct["timeout"],"selected_ambiguous":ct["ambiguous"],
            "selected_win_rate":round(ct["win"]/len(selected),4) if selected else None,
            "auc":round(float(roc_auc_score([x["y"] for x in data],pred)),4) if len(set(x["y"] for x in data))>1 else None,
            "average_precision":round(float(average_precision_score([x["y"] for x in data],pred)),4)}

def main():
    trainfile,testfile,out=sys.argv[1:]
    training=calc(read(trainfile))
    test=calc(read(testfile))
    cut=int(len(training)*.8)
    tr=training[:cut];va=training[cut:]
    Xtr=np.array([x["x"] for x in tr]);Ytr=np.array([x["y"] for x in tr])
    Xva=np.array([x["x"] for x in va]);Xte=np.array([x["x"] for x in test])
    # No features use future candles. Test period not used for selection.
    models={
        "logistic":LogisticRegression(C=.03,max_iter=1000,class_weight=None),
        "boosted":HistGradientBoostingClassifier(max_iter=110,max_leaf_nodes=7,
                    min_samples_leaf=35,learning_rate=.04,l2_regularization=10,
                    early_stopping=False,random_state=7)}
    results={"train_file":Path(trainfile).name,"holdout_file":Path(testfile).name,
             "train_n":len(tr),"validation_n":len(va),"holdout_n":len(test),
             "target":"BUY/SELL early fade +5 USD before -3 USD within 10m; timeouts non-win",
             "features":len(tr[0]["x"]),
             "holdout":"August 2026 retrospective independent month; EARLIER than train months, not forward validation",
             "warnings":["This is exploratory, not a verified trade system.",
                "August is a historical backward holdout, not prospective; later fresh data required.",
                "September-November event definitions were developed using this era; August was not used.",
                "No spread, slippage, commission, order-book or ASK data.",
                "Threshold chosen on validation to maximize observed success among at least 25 signals; selection bias remains.",
                "Intraminute both-stop-and-target counted non-win; market gaps excluded.",
                "Prediction is not probability calibration; threshold and apparent win rate are not guarantees."],
             "models":{}}
    for name,model in models.items():
        model.fit(Xtr,Ytr)
        pv=model.predict_proba(Xva)[:,1]
        pt=model.predict_proba(Xte)[:,1]
        # Require at least 25 validation trades; use only validation to pick.
        thresholds=np.unique(np.quantile(pv,np.linspace(.50,.97,30)))
        options=[q for q in thresholds if (pv>=q).sum()>=25]
        if not options:options=[float(np.min(pv))]
        q=max(options,key=lambda q:(np.mean(np.array([x["y"] for x in va])[pv>=q]),(pv>=q).sum()))
        results["models"][name]={
            "validation":report(va,pv,q),
            "holdout":report(test,pt,q),
            "train_baseline_win_rate":round(float(Ytr.mean()),4)}
    dest=Path(out);dest.mkdir(parents=True,exist_ok=True)
    (dest/"summary.json").write_text(json.dumps(results,indent=2))
    print(json.dumps(results["models"],indent=2))
if __name__=="__main__":main()
