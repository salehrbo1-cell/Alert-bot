#!/usr/bin/env python3
"""No-cost-first hypothesis agent for XAUUSD. Gemini is optional.
The model can invent bounded, declarative rules; it cannot execute code,
change production files, read secrets, or select using holdout results.
"""
import csv,hashlib,json,os,random,statistics,sys,urllib.error,urllib.request
from datetime import datetime,timezone
from pathlib import Path
from collections import defaultdict
import auto_research_gold as core

# Rules can be novel combinations of market conditions not enumerated by
# the fixed 432-combination researcher. JSON ONLY: never eval/exec AI text.
FEATURES={
 "tf":("int",[5,15]),
 "side":("str",["buy","sell"]),
 "wick_body_min":("float",0.75,4.0),
 "wick_body_max":("float",0.75,6.0),
 "body_atr_min":("float",0.05,1.2),
 "body_atr_max":("float",0.05,1.5),
 "utc_hour_start":("int",0,23),
 "utc_hour_end":("int",0,23),
 "min_dwm_frames":("int",0,3),
 "monthly_close_near":("bool",),
 "h1_h4_pivot_near":("bool",),
 "confirmation":("str",["none","body","extreme"]),
 "entry":("str",["market","body_limit"]),
 "target":("str",["rr1.5","rr2","next_level"]),
}
DEFAULT={"tf":15,"side":"sell","wick_body_min":0.75,"wick_body_max":1.25,
 "body_atr_min":0.1,"body_atr_max":1.5,"utc_hour_start":0,"utc_hour_end":23,
 "min_dwm_frames":1,"monthly_close_near":False,"h1_h4_pivot_near":False,
 "confirmation":"body","entry":"body_limit","target":"rr1.5"}
MAX_CALLS=1;MAX_NEW=4
def normalize(obj):
    if not isinstance(obj,dict):raise ValueError("Not a JSON object")
    if set(obj)-set(FEATURES):raise ValueError("Unknown keys")
    r=dict(DEFAULT)
    for k,v in obj.items():
        spec=FEATURES[k];typ=spec[0]
        if typ=="bool":
            if type(v) is not bool:raise ValueError(k)
        elif typ=="str":
            if type(v) is not str or v not in spec[1]:raise ValueError(k)
        elif typ=="int":
            if type(v) is not int:raise ValueError(k)
            if len(spec)==2 and v not in spec[1]:raise ValueError(k)
            if len(spec)==3 and not spec[1]<=v<=spec[2]:raise ValueError(k)
        elif typ=="float":
            if type(v) not in (float,int) or not spec[1]<=v<=spec[2]:raise ValueError(k)
            v=round(float(v),3)
        r[k]=v
    if r["wick_body_min"]>r["wick_body_max"] or r["body_atr_min"]>r["body_atr_max"]:
        raise ValueError("Invalid ranges")
    if r["utc_hour_start"]>r["utc_hour_end"]:raise ValueError("UTC hours cannot wrap")
    return r
def rule_id(r):
    return hashlib.sha256(json.dumps(r,sort_keys=True).encode()).hexdigest()[:12]
def mutate(r,seed):
    rng=random.Random(seed);out=dict(r)
    k=rng.choice(["wick_body_min","wick_body_max","body_atr_min","body_atr_max",
       "utc_hour_start","utc_hour_end","min_dwm_frames","monthly_close_near",
       "h1_h4_pivot_near","confirmation","entry","target","tf","side"])
    spec=FEATURES[k]
    if spec[0]=="bool":out[k]=not out[k]
    elif spec[0]=="str":out[k]=rng.choice([x for x in spec[1] if x!=out[k]])
    elif spec[0]=="int" and len(spec)==2:out[k]=rng.choice([x for x in spec[1] if x!=out[k]])
    elif spec[0]=="int":out[k]=max(spec[1],min(spec[2],out[k]+rng.choice([-3,-1,1,3])))
    else:out[k]=round(max(spec[1],min(spec[2],out[k]+rng.choice([-.2,-.1,.1,.2]))),3)
    try:return normalize(out)
    except ValueError:return mutate(r,seed+1)
def eligible(e,r):
    if e["tf"]!=r["tf"] or e["side"]!=r["side"]:return False
    x=e["x"];body=abs(x[5]-x[2]);up=x[3]-max(x[2],x[5]);down=min(x[2],x[5])-x[4]
    wick=up if e["side"]=="sell" else down
    if body<=0:return False
    ratio=wick/body;ba=body/max(e["atr"],.001)
    hour=datetime.fromtimestamp(e["t"]/1000,timezone.utc).hour
    return (r["wick_body_min"]<=ratio<=r["wick_body_max"] and
        r["body_atr_min"]<=ba<=r["body_atr_max"] and
        r["utc_hour_start"]<=hour<=r["utc_hour_end"] and
        e["count"]>=r["min_dwm_frames"] and
        (not r["monthly_close_near"] or e["monthly"]) and
        (not r["h1_h4_pivot_near"] or e["htf"]) and
        (r["confirmation"]=="none" or r["confirmation"]=="body" and e["body"] or
         r["confirmation"]=="extreme" and e["extreme"]))
def gemini_suggest(history):
    key=os.environ.get("GEMINI_API_KEY","").strip()
    if not key:return [],"no_api_key_offline_only"
    # Discover models enabled for THIS key instead of assuming a model name.
    # Model listing does not invoke generation or incur token charges.
    model="gemini-2.5-flash-lite"
    try:
        req_models=urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=100",
            headers={"x-goog-api-key":key},method="GET")
        with urllib.request.urlopen(req_models,timeout=25) as response:
            available=json.loads(response.read(400000)).get("models",[])
        names=[m["name"].split("/")[-1] for m in available
            if "generateContent" in m.get("supportedGenerationMethods",[])]
        priority=[m for m in names if "flash-lite" in m and "preview" not in m]
        priority += [m for m in names if "flash" in m and "preview" not in m and m not in priority]
        if priority:model=priority[0]
        elif names:return [],"gemini_no_flash_model_available"
        else:return [],"gemini_no_generation_model_available"
    except urllib.error.HTTPError as exc:
        return [],"gemini_model_list_http_"+str(exc.code)
    except (urllib.error.URLError,TimeoutError,ValueError,KeyError):
        return [],"gemini_model_list_unavailable"
    # The model sees only abstract rules and TRAIN/VALIDATION metrics, never test.
    past=[dict(rule=x["rule"],train=x["train"],validation=x["validation"])
        for x in history[-12:] if isinstance(x,dict) and all(z in x for z in ("rule","train","validation"))]
    prompt=("You are a quantitative XAUUSD research hypothesis generator. "
        "Invent exactly 3 DISTINCT, testable candlestick + historical level + session rules "
        "as a JSON array of objects, using ONLY the allowed keys. Do not claim profitability. "
        "Explore unexpected interactions, not just change a parameter. "
        "The engine will backtest without seeing held-out test data. "
        "All keys and constraints: "+json.dumps(FEATURES)+"; default values: "+
        json.dumps(DEFAULT)+"; past training/validation only: "+json.dumps(past)+
        ". Respond with JSON array only, no markdown.")
    body=json.dumps({"contents":[{"parts":[{"text":prompt}]}],
        "generationConfig":{"temperature":0.9,"maxOutputTokens":1700,
            "responseMimeType":"application/json"}}).encode()
    req=urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=body,headers={"x-goog-api-key":key,"Content-Type":"application/json"},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=35) as response:
            result=json.loads(response.read(250000))
        s="".join(p.get("text","") for p in result["candidates"][0]["content"]["parts"])
        ideas=json.loads(s)
        if not isinstance(ideas,list):return [],"bad_ai_response"
        good=[]
        for x in ideas[:MAX_NEW]:
            try:good.append(normalize(x))
            except ValueError:pass
        return good,"gemini_free_tier_requested_"+model
    except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError,KeyError,ValueError,IndexError) as exc:
        # 429 = exhausted free quota. No paid fallback and no retries.
        return [],"gemini_http_"+str(exc.code) if isinstance(exc,urllib.error.HTTPError) else "gemini_unavailable_"+type(exc).__name__

def evaluate(m1,starts,events,rule,begin,train_end,valid_end,end):
    grouped=defaultdict(list);occupied=-1;count=0
    for e in events:
        if e["t"]<begin or e["t"]>end or not eligible(e,rule) or e["t"]<=occupied:continue
        if abs(e["t"]-train_end)<core.EMBARGO or abs(e["t"]-valid_end)<core.EMBARGO:continue
        period="train" if e["t"]<train_end else "validation" if e["t"]<valid_end else "test"
        r,release,filled=core.simulate(m1,starts,e,rule["confirmation"],rule["entry"],rule["target"])
        occupied=max(occupied,release);count+=1
        if r is not None:grouped[period].append(r)
    train=core.metrics(grouped["train"]);valid=core.metrics(grouped["validation"]);test=core.metrics(grouped["test"])
    passed=train["n"]>=30 and valid["n"]>=10 and train["mean_r"]>0 and valid["mean_r"]>0
    score=round(min(train["mean_r"],valid["mean_r"])*min(1,valid["n"]/25)-.002*valid["dd_r"],5) if passed else None
    return dict(id=rule_id(rule),rule=rule,signals=count,train=train,validation=valid,
        test=test,passed=passed,score=score)

def main(src,dest):
    m1=core.load(src)
    if len(m1)<15000:raise SystemExit("Insufficient data")
    starts=[x[0] for x in m1];begin=m1[0][0]+35*24*core.H;end=m1[-1][0]-core.HOLD
    train=begin+int(.7*(end-begin));valid=begin+int(.85*(end-begin))
    lev=core.known_levels(m1);events=core.signals(m1,lev,begin)
    dest=Path(dest);dest.mkdir(parents=True,exist_ok=True)
    memory=dest/"memory.json"
    try:history=json.loads(memory.read_text()).get("history",[]) if memory.exists() else []
    except (ValueError,TypeError):history=[]
    # Never let the AI read held-out test results through memory.
    ai,status=gemini_suggest(history)
    seed=int(datetime.now(timezone.utc).strftime("%Y%m%d"))+len(history)*1031
    prior=sorted((x for x in history if x.get("passed") and x.get("score") is not None),
        key=lambda x:x["score"],reverse=True)
    parent=prior[0]["rule"] if prior else DEFAULT
    candidates=ai[:3]
    for i in range(4):
        candidates.append(mutate(parent,seed+i*7919))
    unique=[];seen={x.get("id") for x in history}
    for x in candidates:
        k=rule_id(x)
        if k not in seen and k not in {rule_id(z) for z in unique}:unique.append(x)
    rows=[evaluate(m1,starts,events,r,begin,train,valid,end) for r in unique[:7]]
    # Holdout results must never be used for ranking, next-generation prompt,
    # or for determining if a strategy is "passed".
    history=(history+rows)[-150:]
    memory.write_text(json.dumps(dict(history=history),ensure_ascii=False,indent=2)+"\n")
    ranked=sorted((x for x in history if x["passed"]),key=lambda x:x["score"],reverse=True)
    result=dict(utc=datetime.now(timezone.utc).isoformat(),provider_status=status,
        generated_by_ai=len(ai),new_hypotheses=len(rows),historical_total=len(history),
        last_data_utc=datetime.fromtimestamp(m1[-1][0]/1000,timezone.utc).isoformat(),
        training_end_utc=datetime.fromtimestamp(train/1000,timezone.utc).isoformat(),
        validation_end_utc=datetime.fromtimestamp(valid/1000,timezone.utc).isoformat(),
        selection="training+validation ONLY; held-out test never used for ranking",
        approved_for_live=False,champion=ranked[0] if ranked else None,
        new_results=rows)
    (dest/"latest.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n")
    md=["# وكيل ابتكار فرضيات الذهب (تجريبي وآمن)","",
        f"آخر بيانات: {result['last_data_utc']} UTC.",
        f"حالة النموذج: **{status}**.",
        f"فرضيات جديدة مختبرة: **{len(rows)}**؛ فرضيات مقترحة بالذكاء الاصطناعي: **{len(ai)}**.",
        "","**بدون GEMINI_API_KEY يعمل التطوير التطوري المحلي فقط؛ لا يدّعي أنه نموذج ذكاء اصطناعي.**",
        "عند إضافة مفتاح Gemini Free Tier يقترح النموذج فرضيات جديدة في JSON محصور، ثم يختبرها المحرك.",
        "لا يكتب النموذج أي كود قابل للتنفيذ ولا يعدل البوت ولا ينفذ صفقات.",
        "","## أفضل مرشح حسب التدريب والتحقق"]
    if ranked:
        best=ranked[0]
        md.extend([f"المعرف: {best['id']}",f"الشروط: {json.dumps(best['rule'],ensure_ascii=False)}",
            f"التدريب: {best['train']}",f"التحقق: {best['validation']}",
            f"الاختبار المنفصل (للتقرير فقط): {best['test']}"])
    else:md.append("لا يوجد مرشح مستوفٍ للشروط.")
    md+=["","## القيود","- قد تتداخل الإشارات؛ لا تعتمد الربحية المستقبلية.",
        "- اختبار آخر 15% لا يستخدم للترتيب، لكن تكرار مشاهدة نتائجه عبر الأيام يضعف استقلاليته.",
        "- السبريد والانزلاق تقديريان؛ لا يوجد تنفيذ حي.",
        "- كل طلب Gemini محدود إلى طلب واحد يومياً، بدون إعادة محاولة أو ترقية مدفوعة.",
        "- المستوى المجاني لـ Gemini قد يسمح باستخدام المدخلات لتحسين منتجات المزود؛ لا تُرسل أسرار أو بيانات شخصية.",
        "- لا تعتمد أي استراتيجية حتى تمر باختبار ورقي مستقبلي على بيانات جديدة.",
        "","لإضافة الذكاء الاصطناعي: GitHub repo → Settings → Secrets and variables → Actions → New repository secret → GEMINI_API_KEY.",
        "احصل على المفتاح من Google AI Studio بمشروع Free Tier دون ربط الفوترة."]
    (dest/"README.md").write_text("\n".join(md)+"\n")
    print("AI HYPOTHESIS AGENT",status,"ai",len(ai),"tested",len(rows),"qualified",len(ranked),flush=True)
if __name__=="__main__":main(sys.argv[1],sys.argv[2])
