#!/usr/bin/env python3
"""Hourly Telegram research digest, no trading, never log credentials."""
import json,os,sys,urllib.parse,urllib.request
from datetime import datetime,timezone,timedelta
from pathlib import Path

BASE=Path("research/open_discovery")
REPORT=BASE/"latest.json"
SENT=BASE/"telegram_sent.json"
LINK="https://github.com/salehrbo1-cell/Alert-bot/blob/main/research/open_discovery/README.md"

def number(v,digits=2):
    return "غير متاح" if v is None else str(round(v,digits))

def explain(rule):
    mapping={"trend":"الاتجاه","volatility":"التذبذب","breakout":"الاختراق",
      "body":"حجم الجسم","upper_wick":"الذيل العلوي","lower_wick":"الذيل السفلي",
      "close_position":"موقع الإغلاق","direction":"اتجاه الشمعة",
      "streak":"تتابع الشموع","mean_distance":"بعد السعر عن المتوسط",
      "session":"الجلسة","weekday":"يوم الأسبوع"}
    values={"up":"صاعد","down":"هابط","flat":"عرضي","high":"مرتفع",
      "normal":"عادي","low":"منخفض","inside":"داخل النطاق","large":"كبير",
      "medium":"متوسط","small":"صغير","dominant":"مهيمن","ordinary":"عادي",
      "middle":"المنتصف","up3":"3 صاعدة","down3":"3 هابطة","other":"أخرى",
      "above":"فوق المتوسط","below":"تحت المتوسط","near":"قريب من المتوسط",
      "asia":"آسيا","europe":"أوروبا","us":"أمريكا","late_us":"نهاية أمريكا",
      "mon":"الاثنين","tue":"الثلاثاء","wed":"الأربعاء","thu":"الخميس","fri":"الجمعة"}
    return ("شراء" if rule["side"]=="buy" else "بيع")+" M"+str(rule["tf"])+": "+", ".join(
      mapping.get(c["feature"],c["feature"])+"="+values.get(c["value"],c["value"])
      for c in rule["conditions"])+"، خروج بعد "+str(rule["horizon"])+" شموع"

def main():
    now=datetime.now(timezone.utc)
    event=os.environ.get("GITHUB_EVENT_NAME","")
    # The workflow runs hourly; send at most one report per UTC hour.
    report_slot=now.strftime("%Y-%m-%dT%H:00Z")
    if event not in ("schedule","push","workflow_dispatch"):
        print("Not a notification event.");return
    if event=="workflow_dispatch" and os.environ.get("SEND_TEST","false")!="true":
        print("Manual run without Telegram test request.");return
    token=os.environ.get("BOT_TOKEN","").strip()
    chat=os.environ.get("CHAT_ID","").strip()
    if not token or not chat:
        raise SystemExit("Telegram secrets BOT_TOKEN and CHAT_ID are not configured")
    report=json.loads(REPORT.read_text())
    today=now.strftime("%Y-%m-%d")
    try:previous=json.loads(SENT.read_text()) if SENT.exists() else {}
    except (ValueError,TypeError):previous={}
    if previous.get("last_report_slot")==report_slot and os.environ.get("SEND_TEST","false")!="true":
        print("Hourly research digest already delivered for",report_slot);return
    candidates=[x for x in report.get("new_results",[]) if x.get("passed")]
    # Avoid claiming strong evidence solely from train/validation.
    candidates.sort(key=lambda x:(x.get("score") if x.get("score") is not None else -999),reverse=True)
    saudi_now=now.astimezone(timezone(timedelta(hours=3)))
    lines=["📊 تقرير الباحث المستقل للذهب XAUUSD",
           "🕒 "+saudi_now.strftime("%Y-%m-%d %H:%M")+" بتوقيت السعودية",
           "🤖 حالة Gemini: "+report.get("model_status","غير معروف"),
           "💡 أفكار Gemini: "+str(report.get("ai_proposals",0)),
           "🧪 فرضيات جديدة مختبرة: "+str(report.get("new_tested",0)),
           "📚 مجموع الفرضيات المختلفة المختبرة: "+str(report.get("historical_total",0)),
           "📈 مرشحات اجتازت التدريب والتحقق في هذه الجولة: "+str(len(candidates))]
    if candidates:
        top=candidates[0];t=top["test"]
        lines += ["","🔎 أبرز مرشح جديد:",
                  explain(top["rule"]),
                  "الاختبار المنفصل: "+str(t["n"])+" صفقة؛ نسبة نجاح "+
                  (number(t["win"]*100,1)+"%" if t["win"] is not None else "غير متاح")+
                  "؛ متوسط "+number(t["avg_usd"])+" $/أونصة.",
                  "ملاحظة: الاختبار المنفصل سلبي." if (t["avg_usd"] or 0)<=0 else
                  "ملاحظة: النتيجة إيجابية تاريخياً فقط وليست توصية."]
    else:lines += ["","لم تظهر فرضية جديدة تستوفي حد التدريب والتحقق في هذه الجولة."]
    lines += ["","⚠️ بحث تاريخي فقط، دون وقف خسارة أو تداول فعلي.",
              "📄 "+LINK]
    message="\n".join(lines)
    body=urllib.parse.urlencode({"chat_id":chat,"text":message,
                                 "disable_web_page_preview":"true"}).encode()
    request=urllib.request.Request("https://api.telegram.org/bot"+token+"/sendMessage",
                                    data=body,method="POST")
    with urllib.request.urlopen(request,timeout=20) as response:
        result=json.loads(response.read(5000))
    if not result.get("ok"):raise RuntimeError("Telegram did not confirm delivery")
    SENT.parent.mkdir(parents=True,exist_ok=True)
    SENT.write_text(json.dumps({"last_date":today,"last_report_slot":report_slot,
                                "last_report_utc":report.get("utc"),
                                "telegram_message_id":result["result"]["message_id"]},indent=2)+"\n")
    print("Telegram research digest sent for",report_slot)

if __name__=="__main__":main()
