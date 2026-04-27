
# =========================
# V11 FINAL 商業完整版（穩定可上線）
# =========================
from flask import Flask, request, abort
import os, json, re
from datetime import datetime, timedelta, timezone
import hmac, hashlib, base64
import requests

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN","")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET","")

LINE_API = "https://api.line.me/v2/bot/message/reply"
HEADERS = {
    "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
    "Content-Type":"application/json"
}

TZ = timezone(timedelta(hours=8))
def now():
    return datetime.now(TZ)

users = {}

def get_user(uid):
    if uid not in users:
        users[uid] = {
            "vip": False,
            "vip_expire": None,
            "trial_end": now()+timedelta(hours=3),
            "trial_notice": False,
            "road": [],
            "hl": {"high":0,"low":0}
        }
    return users[uid]

def is_vip(u):
    return u["vip"] and u["vip_expire"] and now()<u["vip_expire"]

def in_trial(u):
    return now()<u["trial_end"]

def has_access(u):
    return is_vip(u) or in_trial(u)

def reply(token,text):
    requests.post(LINE_API,headers=HEADERS,json={
        "replyToken":token,
        "messages":[{"type":"text","text":text}]
    })

# =========================
# 分析引擎 V11
# =========================

def analyze(u):
    road = u["road"]
    if len(road)<15:
        return None

    z = road.count("莊")
    x = road.count("閒")
    total = max(1,z+x)

    score_z = z/total
    score_x = x/total

    # 長龍
    streak = 1
    for i in range(len(road)-1,0,-1):
        if road[i]==road[i-1]:
            streak+=1
        else:
            break

    if streak>=5:
        decay=-0.08
    elif streak==4:
        decay=-0.04
    else:
        decay=0.02

    if road[-1]=="莊":
        score_z+=decay
    else:
        score_x+=decay

    if score_z>score_x:
        d="莊"
        pct=int(score_z*100)
        diff=score_z-score_x
    else:
        d="閒"
        pct=int(score_x*100)
        diff=score_x-score_z

    if diff>0.15:
        state="🔥 強攻"
        coef=1.6
    elif diff>0.1:
        state="✅ 進攻"
        coef=1.3
    elif diff>0.06:
        state="👀 觀察"
        coef=0.8
    else:
        state="⚠️ 保守"
        coef=0.5

    # 高低牌
    high=u["hl"]["high"]
    low=u["hl"]["low"]
    total_hl=max(1,high+low)

    if high/total_hl>0.55:
        hl_coef=1.15
        hl_text="高牌偏多"
    elif low/total_hl>0.55:
        hl_coef=0.85
        hl_text="低牌偏多"
    else:
        hl_coef=1.0
        hl_text="均衡"

    bet=int(100*coef*hl_coef)

    return d,pct,state,bet,streak,hl_text

# =========================
# webhook
# =========================

@app.route("/",methods=["POST"])
def webhook():
    body=request.json

    for e in body["events"]:
        if e["type"]=="follow":
            uid=e["source"]["userId"]
            get_user(uid)
            reply(e["replyToken"],
                "🎁 已開啟3小時試用\n請輸入牌路開始分析")
            continue

        if e["type"]!="message":
            continue

        uid=e["source"]["userId"]
        text=e["message"]["text"]
        u=get_user(uid)

        # 試用結束提示
        if not in_trial(u) and not is_vip(u) and not u["trial_notice"]:
            u["trial_notice"]=True
            reply(e["replyToken"],
                "⏰ 試用已結束\n👉 請找管理員開通")
            continue

        if text=="開始":
            reply(e["replyToken"],"請輸入牌路")
            continue

        if text=="功能介紹":
            reply(e["replyToken"],
                "V11系統：多模型+長龍控制+點數策略")
            continue

        if text=="會員說明":
            reply(e["replyToken"],
                "流程：註冊→綁定→找管理員")
            continue

        if text in ["找管理員","開通"]:
            reply(e["replyToken"],"請聯絡管理員")
            continue

        # 高低牌
        if text=="高":
            u["hl"]["high"]+=1
            reply(e["replyToken"],"已記錄高牌")
            continue

        if text=="低":
            u["hl"]["low"]+=1
            reply(e["replyToken"],"已記錄低牌")
            continue

        # 牌路
        if "莊" in text or "閒" in text:
            for c in text:
                if c in ["莊","閒"]:
                    u["road"].append(c)
            reply(e["replyToken"],"已記錄")
            continue

        if text=="開始分析":
            if not has_access(u):
                reply(e["replyToken"],
                    "📊 分析已鎖\n請找管理員開通")
                continue

            res=analyze(u)
            if not res:
                reply(e["replyToken"],"請至少15把")
                continue

            d,pct,state,bet,streak,hl=res

            reply(e["replyToken"],
                f"🎯 {d} {pct}%\n"
                f"{state}\n"
                f"連續:{streak}\n"
                f"牌值:{hl}\n"
                f"💰點數:{bet}")
            continue

        if text=="結束分析":
            u["road"]=[]
            u["hl"]={"high":0,"low":0}
            reply(e["replyToken"],"已結束")
            continue

    return "OK"

if __name__=="__main__":
    app.run()
