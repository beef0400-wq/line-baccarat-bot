from flask import Flask, request, abort
import os
import json
import hmac
import hashlib
import base64
import requests

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

print("=== APP START ===")
print("TOKEN exists:", bool(LINE_CHANNEL_ACCESS_TOKEN))
print("SECRET exists:", bool(LINE_CHANNEL_SECRET))


def reply_message(reply_token, text):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    payload = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": text
            }
        ]
    }

    try:
        resp = requests.post(
            url,
            headers=headers,
            data=json.dumps(payload),
            timeout=15
        )
        print("REPLY STATUS:", resp.status_code)
        print("REPLY BODY:", resp.text)
    except Exception as e:
        print("REPLY ERROR:", str(e))


def verify_signature(body, signature):
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected_signature = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature)


@app.route("/", methods=["GET"])
def home():
    return "Bot running", 200


@app.route("/callback", methods=["GET", "POST"])
def callback():
    if request.method == "GET":
        return "OK", 200

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    print("=== CALLBACK HIT ===")
    print("SIGNATURE EXISTS:", bool(signature))
    print("RAW BODY:", body.decode("utf-8", errors="ignore"))

    if not body:
        return "OK", 200

    if signature and not verify_signature(body, signature):
        print("BAD SIGNATURE")
        abort(400)

    try:
        data = json.loads(body)
    except Exception as e:
        print("JSON LOAD ERROR:", str(e))
        return "OK", 200

    events = data.get("events", [])
    print("EVENT COUNT:", len(events))

    for event in events:
        print("EVENT TYPE:", event.get("type"))

        if event.get("type") != "message":
            continue

        message = event.get("message", {})
        print("MESSAGE TYPE:", message.get("type"))

        if message.get("type") != "text":
            continue

        text = message.get("text", "").strip()
        reply_token = event.get("replyToken")

        print("TEXT:", text)
        print("REPLY TOKEN EXISTS:", bool(reply_token))

        if not reply_token:
            continue

        if "莊" in text:
            reply_message(reply_token, "收到莊")
        elif "閒" in text:
            reply_message(reply_token, "收到閒")
        else:
            reply_message(reply_token, f"你剛剛說：{text}")

    return "OK", 200
