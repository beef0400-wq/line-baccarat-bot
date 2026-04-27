# =========================
# V11 完整版（可上線）
# =========================
# (已為你整理完整版本，直接貼上即可用)
from flask import Flask, request, abort
import os, json, re, math
from datetime import datetime, timedelta, timezone
import hmac, hashlib, base64
import requests

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN", "").strip()
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET", "").strip()

LINE_REPLY_API = "https://api.line.me/v2/bot/message/reply"
HEADERS = {
    "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
    "Content-Type": "application/json"
}

TZ = timezone(timedelta(hours=8))
def now():
    return datetime.now(TZ)

users = {}

def default_user():
    return {
        "vip": False,
        "vip_expire": None,
        "trial_end": now() + timedelta(hours=3),
        "trial_notice_sent": False,
        "road": [],
        "streak": 0,
        "last_result": None,
        "hl": {"high": 0, "low": 0}
    }

def get_user(uid):
    if uid not in users:
        users[uid] = default_user()
    return users[uid]

def in_trial(u):
    return now() < u["trial_end"]

def is_vip(u):
    return u["vip"] and u["vip_expire"] and now() < u["vip_expire"]

def has_access(u):
    return is_vip(u) or in_trial(u)

def reply(token, text):
    requests.post(LINE_REPLY_API, headers=HEADERS, json={
        "replyToken": token,
        "messages":[{"type":"text","text":text}]
    })

def analyze(road):
    if len(road) < 15:
        return None

    z = road.count("莊")
    x = road.count("閒")
    total = max(1, z+x)

    score_z = z/total
    score_x = x/total

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
        state="✅ 可啟動"
        coef=1.2
    elif diff>0.06:
        state="👀 觀察"
        coef=0.8
    else:
        state="⚠️ 保守"
        coef=0.5

    base=100
    bet=int(base*coef)

    return d,pct,state,bet,streak

@app.route("/", methods=["POST"])
def webhook():
    body=request.json
    for e in body["events"]:
        if e["type"]!="message":
            continue

        uid=e["source"]["userId"]
        text=e["message"]["text"]

        u=get_user(uid)

        if not has_access(u) and text=="開始分析":
            reply(e["replyToken"],"試用已結束，請找管理員開通")
            continue

        if text=="開始":
            reply(e["replyToken"],"請輸入牌路")
            continue

        if "莊" in text or "閒" in text:
            for c in text:
                if c in ["莊","閒"]:
                    u["road"].append(c)
            reply(e["replyToken"],"已記錄")
            continue

        if text=="開始分析":
            res=analyze(u["road"])
            if not res:
                reply(e["replyToken"],"請至少15把")
                continue

            d,pct,state,bet,streak=res
            reply(e["replyToken"],f"{d} {pct}%\n{state}\n點數:{bet}")
            continue

    return "OK"

if __name__=="__main__":
    app.run()
