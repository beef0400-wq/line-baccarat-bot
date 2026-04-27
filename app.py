
# =========================
# V13 鎖死版（100%一定回應）
# =========================
from flask import Flask, request
import os, requests, json

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN","")

def reply(token, text):
    print("REPLY_CALLED", flush=True)
    if not CHANNEL_ACCESS_TOKEN:
        print("❌ TOKEN沒設", flush=True)
        return

    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/reply",
            headers={
                "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
                "Content-Type":"application/json"
            },
            json={
                "replyToken": token,
                "messages":[{"type":"text","text":text}]
            }
        )
        print("STATUS:", r.status_code, r.text, flush=True)
    except Exception as e:
        print("ERROR:", e, flush=True)

@app.route("/callback", methods=["POST"])
def callback():
    print("🔥 CALLBACK_HIT", flush=True)

    data = request.json
    print("BODY:", json.dumps(data, ensure_ascii=False), flush=True)

    for e in data["events"]:
        token = e.get("replyToken")
        text = ""

        if e["type"] == "message":
            text = e["message"].get("text","")
            print("USER_TEXT:", text, flush=True)

            if text == "開始":
                reply(token, "✅ 機器人正常運作")
                return "OK"

            elif text == "測試":
                reply(token, "🔥 測試成功")
                return "OK"

            else:
                reply(token, f"你剛剛說：{text}")
                return "OK"

    return "OK"

@app.route("/", methods=["GET"])
def home():
    return "BOT RUNNING"

if __name__ == "__main__":
    app.run()
