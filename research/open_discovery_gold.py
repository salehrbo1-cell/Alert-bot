#!/usr/bin/env python3
"""XAUUSD free-form OHLC pattern research, independent of past user strategies.
AI outputs declarative JSON rules only; no code execution or live trades.
"""
import collections,hashlib,json,os,random,statistics,sys,urllib.request,urllib.error
from datetime import datetime,timezone
from pathlib import Path
import auto_research_gold as core

CATEGORIES={
 "trend":["up","down","flat"],"volatility":["high","normal","low"],
 "breakout":["up","down","inside"],"body":["large","medium","small"],
 "upper_wick":["dominant","ordinary"],"lower_wick":["dominant","ordinary"],
 "close_position":["high","middle","low"],"direction":["up","down","flat"],
 "streak":["up3","down3","other"],"mean_distance":["above","below","near"],
 "session":["asia","europe","us","late_us"],"weekday":["mon","tue","wed","thu","fri"]}
HORIZONS=(3,6,12);TFS=(5,15);COST=core.SPREAD+2*core.SLIP
def dt(t):return datetime.fromtimestamp(t/1000,timezone.utc).isoformat()
def dataset(m1):
    out={}
    for tf in TFS:
        bars=core.resample(m1,tf);atrs=core.atrs(bars);rows=[]
        for i in range(60,len(bars)-13):
            x=bars[i]
            if any(bars[j][1]!=bars[j+1][0] for j in range(i-48,i)):continue
            a=max(atrs[i],.01);o,hi,lo,c=x[2],x[3],x[4],x[5]
            span=max(hi-lo,.001);p=(c-lo)/span
            past=bars[i-24:i];avg=statistics.mean(z[5] for z in past)
            trend=(c-bars[i-12][5])/a;vol=a/max(statistics.mean(atrs[i-49:i+1]),.01)
            hour=datetime.fromtimestamp(x[0]/1000,timezone.utc).hour
            wd=datetime.fromtimestamp(x[0]/1000,timezone.utc).weekday()
            if wd>4:continue
            features={
             "trend":"up" if trend>1.5 else "down" if trend< -1.5 else "flat",
             "volatility":"high" if vol>1.3 else "low" if vol<.75 else "normal",
             "breakout":"up" if c>max(z[3] for z in past) else "down" if c<min(z[4] for z in past) else "inside",
             "body":"large" if abs(c-o)/span>=.65 else "small" if abs(c-o)/span<=.25 else "medium",
             "upper_wick":"dominant" if (hi-max(o,c))/span>=.45 else "ordinary",
             "lower_wick":"dominant" if (min(o,c)-lo)/span>=.45 else "ordinary",
             "close_position":"high" if p>=.75 else "low" if p<=.25 else "middle",
             "direction":"up" if c-o>.05*a else "down" if o-c>.05*a else "flat",
             "streak":"up3" if all(z[5]>z[2] for z in bars[i-2:i+1]) else
                      "down3" if all(z[5]<z[2] for z in bars[i-2:i+1]) else "other",
             "mean_distance":"above" if (c-avg)/a>1 else "below" if (c-avg)/a< -1 else "near",
             "session":"asia" if hour<7 else "europe" if hour<12 else "us" if hour<20 else "late_us",
             "weekday":("mon","tue","wed","thu","fri")[wd]}
            future={}
            for h in HORIZONS:
                if all(bars[j][1]==bars[j+1][0] for j in range(i,i+h)):
                    future[h]=bars[i+h][5]-bars[i+1][2]
            if future:rows.append((x[0],features,future))
        out[tf]=rows
    return out

def clean(r):
    if not isinstance(r,dict) or set(r)!={"tf","side","horizon","conditions"}:raise ValueError()
    if type(r["tf"]) is not int or r["tf"] not in TFS or r["side"] not in ("buy","sell") or r["horizon"] not in HORIZONS:raise ValueError()
    cs=r["conditions"]
    if not isinstance(cs,list) or not 1<=len(cs)<=3:raise ValueError()
    d={}
    for c in cs:
        if not isinstance(c,dict) or set(c)!={"feature","value"}:raise ValueError()
        f,v=c["feature"],c["value"]
        if f not in CATEGORIES or v not in CATEGORIES[f] or f in d:raise ValueError()
        d[f]=v
    return {"tf":r["tf"],"side":r["side"],"horizon":r["horizon"],
            "conditions":[{"feature":f,"value":d[f]} for f in sorted(d)]}
def uid(r):return hashlib.sha256(json.dumps(r,sort_keys=True).encode()).hexdigest()[:12]
def metrics(xs):
    if not xs:return {"n":0,"avg_usd":None,"total_usd":None,"win":None,"pf":None,"dd_usd":None}
    total=peak=dd=0.
    for x in xs:total+=x;peak=max(peak,total);dd=max(dd,peak-total)
    g=sum(max(0,x) for x in xs);l=-sum(min(0,x) for x in xs)
    return {"n":len(xs),"avg_usd":round(statistics.mean(xs),4),
            "total_usd":round(sum(xs),3),"win":round(sum(x>0 for x in xs)/len(xs),4),
            "pf":round(g/l,3) if l else None,"dd_usd":round(dd,3)}
def evaluate(data,r,begin,train,valid,end):
    results=collections.defaultdict(list);last={"train":-1,"validation":-1,"test":-1}
    h=r["horizon"];tf=r["tf"];sign=1 if r["side"]=="buy" else -1
    for t,f,returns in data[tf]:
        if not begin<=t<end or h not in returns:continue
        if abs(t-train)<core.EMBARGO or abs(t-valid)<core.EMBARGO:continue
        split="train" if t<train else "validation" if t<valid else "test"
        if t<last[split] or any(f[x["feature"]]!=x["value"] for x in r["conditions"]):continue
        results[split].append(sign*returns[h]-COST)
        last[split]=t+(h+1)*tf*core.M
    s={x:metrics(results[x]) for x in ("train","validation","test")}
    a,b=s["train"],s["validation"]
    ok=a["n"]>=80 and b["n"]>=20 and a["avg_usd"]>0 and b["avg_usd"]>0
    score=round(min(a["avg_usd"],b["avg_usd"])*min(1,b["n"]/50)-.001*b["dd_usd"],5) if ok else None
    return {"id":uid(r),"rule":r,"passed":ok,"score":score,**s}
def screen(data,begin,train):
    agg=collections.defaultdict(lambda:[0,0.])
    for tf in TFS:
        for i,(t,f,returns) in enumerate(data[tf]):
            if not begin<=t<train-core.EMBARGO:continue
            for h,delta in returns.items():
                if i%(h+1):continue
                for k,v in f.items():
                    for side,sign in (("buy",1),("sell",-1)):
                        z=agg[(tf,k,v,h,side)];z[0]+=1;z[1]+=sign*delta-COST
    rows=[{"tf":tf,"feature":k,"value":v,"horizon":h,"side":side,
           "n":n,"avg_usd":round(total/n,4)}
          for (tf,k,v,h,side),(n,total) in agg.items() if n>=80]
    pos=sorted((x for x in rows if x["avg_usd"]>0),key=lambda x:x["avg_usd"]*min(1,x["n"]/300),reverse=True)
    neg=sorted((x for x in rows if x["avg_usd"]<0),key=lambda x:x["avg_usd"])
    return pos[:24],neg[:8]
def gemini(pos,neg,history):
    key=os.environ.get("GEMINI_API_KEY","").strip()
    if not key:return [],"offline_no_key"
    try:
        req=urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=100",
                                   headers={"x-goog-api-key":key})
        with urllib.request.urlopen(req,timeout=25) as response:
            names=[m["name"].split("/")[-1] for m in json.loads(response.read(400000)).get("models",[])
                   if "generateContent" in m.get("supportedGenerationMethods",[])]
        choices=[m for m in names if "flash-lite" in m and "preview" not in m]
        choices += [m for m in names if "flash" in m and "preview" not in m and m not in choices]
        if not choices:return [],"no_flash_model"
        model=choices[0]
    except urllib.error.HTTPError as e:return [],"models_http_"+str(e.code)
    except (urllib.error.URLError,TimeoutError,ValueError,KeyError):return [],"models_failed"
    prior=[{"rule":x["rule"],"train":x["train"],"validation":x["validation"]}
           for x in history[-10:] if all(k in x for k in ("rule","train","validation"))]
    prompt=("Independently discover XAUUSD trading behavior. IGNORE all previous user "
        "strategy constraints, support/resistance, wick requirements or preferred patterns. "
        "Explore momentum, mean reversion, breakouts, volatility, streaks, session effects "
        "and unexpected interactions. Suggest 3 DIFFERENT hypotheses grounded in "
        "TRAINING-only associations with enough observations. Do not claim profit. "
        "Return a JSON array; each object has exactly tf (5 or 15), side (buy/sell), "
        "horizon (3/6/12 bars), conditions (1-3 feature/value objects). Allowed values: "+
        json.dumps(CATEGORIES)+"; training positives: "+json.dumps(pos[:18])+
        "; training negatives: "+json.dumps(neg[:6])+
        "; earlier training/validation only: "+json.dumps(prior)+
        ". Never use unseen test outcomes.")
    body=json.dumps({"contents":[{"parts":[{"text":prompt}]}],
        "generationConfig":{"temperature":.9,"maxOutputTokens":1800,
                            "responseMimeType":"application/json"}}).encode()
    req=urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models/"+
        model+":generateContent",data=body,headers={"x-goog-api-key":key,
        "Content-Type":"application/json"},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=40) as response:
            answer=json.loads(response.read(250000))
        raw=json.loads("".join(p.get("text","") for p in answer["candidates"][0]["content"]["parts"]))
        if not isinstance(raw,list):return [],"invalid_ai_json"
        rules=[]
        for r in raw[:5]:
            try:rules.append(clean(r))
            except (ValueError,TypeError,KeyError):pass
        return rules[:3],"gemini_"+model
    except urllib.error.HTTPError as e:return [],"generation_http_"+str(e.code)
    except (urllib.error.URLError,TimeoutError,ValueError,TypeError,KeyError,IndexError):
        return [],"generation_failed"
def offline(pos,seed):
    rng=random.Random(seed);out=[]
    for x in pos[:12]:
        out.append({"tf":x["tf"],"side":x["side"],"horizon":x["horizon"],
                    "conditions":[{"feature":x["feature"],"value":x["value"]}]})
    for x in pos[:10]:
        f=rng.choice([k for k in CATEGORIES if k!=x["feature"]])
        out.append({"tf":x["tf"],"side":x["side"],"horizon":x["horizon"],
                    "conditions":[{"feature":x["feature"],"value":x["value"]},
                                  {"feature":f,"value":rng.choice(CATEGORIES[f])}]})
    for _ in range(12):
        fs=rng.sample(list(CATEGORIES),rng.choice((1,2,3)))
        out.append({"tf":rng.choice(TFS),"side":rng.choice(("buy","sell")),
                    "horizon":rng.choice(HORIZONS),
                    "conditions":[{"feature":f,"value":rng.choice(CATEGORIES[f])} for f in fs]})
    return out
def main(src,dst):
    m1=core.load(src)
    if len(m1)<15000 or m1[-1][0]-m1[0][0]<110*24*core.H:raise SystemExit("Insufficient data")
    data=dataset(m1);begin=m1[0][0]+35*24*core.H
    end=m1[-1][0]-12*15*core.M-core.H
    train=begin+int(.7*(end-begin));valid=begin+int(.85*(end-begin))
    pos,neg=screen(data,begin,train)
    path=Path(dst);path.mkdir(parents=True,exist_ok=True);mem=path/"memory.json"
    try:history=json.loads(mem.read_text()).get("history",[]) if mem.exists() else []
    except (ValueError,TypeError):history=[]
    ai,status=gemini(pos,neg,history)
    ideas=ai+offline(pos,int(datetime.now(timezone.utc).strftime("%Y%m%d"))+len(history)*1049)
    seen={x.get("id") for x in history};rules=[]
    for x in ideas:
        try:r=clean(x)
        except (ValueError,TypeError,KeyError):continue
        if uid(r) not in seen:seen.add(uid(r));rules.append(r)
        if len(rules)>=30:break
    rows=[evaluate(data,r,begin,train,valid,end) for r in rules]
    ai_ids={uid(r) for r in ai}
    for x in rows:x["origin"]="gemini" if x["id"] in ai_ids else "offline"
    history=(history+rows)[-180:]
    mem.write_text(json.dumps({"history":history},ensure_ascii=False,indent=2)+"\n")
    ranked=sorted((x for x in history if x.get("passed") and x.get("score") is not None),
                  key=lambda x:x["score"],reverse=True)
    result={"utc":datetime.now(timezone.utc).isoformat(),"last_data_utc":dt(m1[-1][0]),
            "model_status":status,"ai_proposals":len(ai),"new_tested":len(rows),
            "historical_total":len(history),"features":list(CATEGORIES),
            "train_patterns":pos[:15],"train_negative_patterns":neg,
            "split":{"train_end":dt(train),"validation_end":dt(valid),"test_end":dt(end)},
            "cost_usd_per_oz":COST,"approved_for_live":False,
            "candidate":ranked[0] if ranked else None,"new_results":rows,
            "warning":"Multiple testing, historical data and estimated costs. No stop loss. Requires forward paper trading."}
    (path/"latest.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    md=["# الباحث الحر لاكتشاف أنماط الذهب","",
        "لا يلتزم بشروط التداول السابقة. يفحص 12 عائلة سلوك: الزخم، الاختراق، التذبذب، الشموع، الجلسات وأيام الأسبوع.",
        "آخر بيانات: "+result["last_data_utc"]+" UTC.",
        "Gemini: "+status+"؛ فرضيات AI: "+str(len(ai))+"؛ فرضيات مختبرة: "+str(len(rows))+".",
        "","## أبرز العلاقات في التدريب فقط (ليست توصيات)"]
    for x in pos[:10]:
        md.append("- M"+str(x["tf"])+" "+x["feature"]+"="+x["value"]+
                  "، "+x["side"]+"، "+str(x["horizon"])+" شموع، "+str(x["n"])+
                  " مشاهدات، متوسط "+str(x["avg_usd"])+" دولار/أونصة.")
    md.extend(["","## أفضل مرشح حسب التدريب والتحقق"])
    if ranked:
        x=ranked[0];md.extend(["الشروط: "+json.dumps(x["rule"],ensure_ascii=False),
            "التدريب: "+str(x["train"]),"التحقق: "+str(x["validation"]),
            "الاختبار المنفصل للتقرير فقط: "+str(x["test"])])
    else:md.append("لا يوجد مرشح اجتاز الشروط.")
    md.extend(["","## القيود",
        "- هذه أنماط إحصائية، ليست صفقات جاهزة: لا يوجد وقف خسارة أو تنفيذ حي.",
        "- دخول افتراضي بافتتاح الشمعة التالية وخروج بعد 3 أو 6 أو 12 شمعة.",
        "- سبريد وانزلاق تقديريان بإجمالي 0.50 دولار للأونصة.",
        "- تدريب 70%، تحقق 15%، اختبار 15%، وعزل 24 ساعة؛ الاختبار لا يستخدم لترتيب الأفكار.",
        "- تكرار الاختبارات قد يسبب ملاءمة زائدة، ويلزم اختبار مستقبلي جديد.",
        "- Gemini ينتج JSON شروط فقط؛ لا ينفذ كوداً ولا يعدل alert.py.",
        "- عند نفاد حصة Gemini يستمر الاكتشاف الإحصائي المحلي مجاناً."])
    (path/"README.md").write_text("\n".join(md)+"\n")
    print("OPEN DISCOVERY",status,"ai",len(ai),"tested",len(rows),
          "qualified",len(ranked),"bars",{k:len(v) for k,v in data.items()},flush=True)
if __name__=="__main__":main(sys.argv[1],sys.argv[2])
