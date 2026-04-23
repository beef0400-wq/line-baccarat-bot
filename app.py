from flask import Flask, request, abort
import os
import json
import hmac
import hashlib
import base64
import requests
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# =========================
# ENV
# =========================
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
VIP_USER_IDS = set(filter(None, [x.strip() for x in os.getenv("VIP_USER_IDS", "").split(",")]))
ADMIN_USER_IDS = set(filter(None, [x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",")]))
TZ_TW = timezone(timedelta(hours=8))
TIMEOUT_MINUTES = 20
MAX_ROAD = 20
FREE_ANALYSIS_LIMIT = 5

# =========================
# In-memory store (v1)
# =========================
user_state = {}
# structure:
# {
#   user_id: {
#      "road": ["莊","閒","和"],
#      "last_update": datetime,
#      "free_analysis_used": 0,
#      "analysis_date": "YYYY-MM-DD",
#      "config": {
#          "capital_band": str,
#          "capital_value": int,
#          "target_band": str,
#          "target_multiplier": float,
#          "style": str,
#      },
#      "pending_flow": None | "capital_band" | "target_band" | "style",
#   }
# }


# =========================
# Helpers
# =========================
def now_tw() -> datetime:
    return datetime.now(TZ_TW)


def ensure_user(user_id: str):
    today = now_tw().strftime("%Y-%m-%d")
    if user_id not in user_state:
        user_state[user_id] = {
            "road": [],
            "last_update": now_tw(),
            "free_analysis_used": 0,
            "analysis_date": today,
            "config": {},
            "pending_flow": None,
        }
    if user_state[user_id]["analysis_date"] != today:
        user_state[user_id]["analysis_date"] = today
        user_state[user_id]["free_analysis_used"] = 0
    auto_reset_if_timeout(user_id)
    return user_state[user_id]


def auto_reset_if_timeout(user_id: str):
    state = user_state.get(user_id)
    if not state:
        return
    if now_tw() - state["last_update"] > timedelta(minutes=TIMEOUT_MINUTES):
        state["road"] = []


def touch_user(user_id: str):
    state = ensure_user(user_id)
    state["last_update"] = now_tw()


def is_vip(user_id: str) -> bool:
    return user_id in VIP_USER_IDS


def verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected_signature = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature)


def line_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }


def reply_message(reply_token: str, text: str, quick_items=None):
    messages = [{"type": "text", "text": text}]
    if quick_items:
        messages[0]["quickReply"] = {"items": quick_items}

    payload = {
        "replyToken": reply_token,
        "messages": messages,
    }
    r = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers=line_headers(),
        data=json.dumps(payload),
        timeout=15,
    )
    print("REPLY STATUS:", r.status_code)
    print("REPLY BODY:", r.text)


def push_message(to_user_id: str, text: str):
    payload = {
        "to": to_user_id,
        "messages": [{"type": "text", "text": text}],
    }
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers=line_headers(),
        data=json.dumps(payload),
        timeout=15,
    )
    print("PUSH STATUS:", r.status_code)
    print("PUSH BODY:", r.text)


def make_quick_reply(labels_and_texts):
    items = []
    for label, text in labels_and_texts:
        items.append({
            "type": "action",
            "action": {
                "type": "message",
                "label": label,
                "text": text,
            }
        })
    return items


def filter_main_road(road):
    return [x for x in road if x in ["莊", "閒"]]


def road_text(road, limit=None):
    data = road[-limit:] if limit else road
    return " ".join(data) if data else "尚無資料"


def count_tail_same(seq):
    if not seq:
        return 0, None
    last = seq[-1]
    count = 1
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last:
            count += 1
        else:
            break
    return count, last


def segment_lengths(seq):
    if not seq:
        return []
    segs = []
    current = seq[0]
    count = 1
    for x in seq[1:]:
        if x == current:
            count += 1
        else:
            segs.append((current, count))
            current = x
            count = 1
    segs.append((current, count))
    return segs


def is_single_jump(seq):
    if len(seq) < 4:
        return False
    tail = seq[-5:] if len(seq) >= 5 else seq[:]
    for i in range(1, len(tail)):
        if tail[i] == tail[i - 1]:
            return False
    return True


def single_jump_next(seq):
    if not seq:
        return None
    return "閒" if seq[-1] == "莊" else "莊"


def is_double_jump(seq):
    if len(seq) < 5:
        return False
    segs = segment_lengths(seq)
    if len(segs) < 2:
        return False
    tail = segs[-4:]
    if len(tail) < 2:
        return False
    for side, cnt in tail[:-1]:
        if cnt != 2:
            return False
    if tail[-1][1] not in [1, 2]:
        return False
    return True


def double_jump_next(seq):
    segs = segment_lengths(seq)
    if not segs:
        return None
    last_side, last_count = segs[-1]
    other = "閒" if last_side == "莊" else "莊"
    if last_count == 1:
        return last_side
    return other


def detect_qitou(seq):
    segs = segment_lengths(seq)
    if len(segs) < 2:
        return False
    a, b = segs[-2], segs[-1]
    return a[1] == b[1] and a[0] != b[0]


def analyze_rule(road):
    seq = filter_main_road(road)[-10:]
    if not seq:
        return {
            "rule": "尚無規律",
            "next_side": "觀望",
            "reason": "請先輸入莊 / 閒 / 和開始記錄",
            "risk": "高",
        }

    # Long dragon
    tail_count, tail_side = count_tail_same(seq)
    if tail_count >= 5:
        return {
            "rule": "長龍",
            "next_side": tail_side,
            "reason": "同邊連開超過5顆，先均注順跟到斷",
            "risk": "低",
        }

    # Double jump
    if is_double_jump(seq):
        nxt = double_jump_next(seq)
        return {
            "rule": "雙跳",
            "next_side": nxt,
            "reason": "目前維持雙跳節奏，第5顆後順規律",
            "risk": "低",
        }

    # Single jump intact
    if is_single_jump(seq):
        nxt = single_jump_next(seq)
        return {
            "rule": "單跳",
            "next_side": nxt,
            "reason": "近期維持交叉節奏，先跟到斷",
            "risk": "中",
        }

    # Single jump broken first mouth: e.g. 莊閒莊閒莊莊 -> 回打閒
    if len(seq) >= 6:
        last6 = seq[-6:]
        if last6[0] != last6[1] and last6[1] != last6[2] and last6[2] != last6[3] and last6[3] != last6[4] and last6[4] == last6[5]:
            nxt = "閒" if last6[-1] == "莊" else "莊"
            return {
                "rule": "單跳中斷",
                "next_side": nxt,
                "reason": "單跳斷第一口，先反打回原規律",
                "risk": "中",
            }

    # 3-run reverse hit: AAA B -> A
    if len(seq) >= 4:
        last4 = seq[-4:]
        if last4[0] == last4[1] == last4[2] and last4[3] != last4[2]:
            return {
                "rule": "三連反打",
                "next_side": last4[2],
                "reason": "3連後出現反開，先反打回原邊",
                "risk": "中",
            }

    # 3-run extend: AAA A -> A (up to 6)
    if len(seq) >= 4:
        tail_count, tail_side = count_tail_same(seq)
        if tail_count in [4, 5, 6]:
            return {
                "rule": "三連順跟",
                "next_side": tail_side,
                "reason": "3連後延續，順勢跟到第6顆",
                "risk": "中",
            }

    # 齊頭
    if detect_qitou(seq):
        segs = segment_lengths(seq)
        a, b = segs[-2], segs[-1]
        next_side = a[0]
        return {
            "rule": "齊頭",
            "next_side": next_side,
            "reason": "前後兩段長度一致，可依齊頭節奏反打延續",
            "risk": "中",
        }

    # Majority fallback
    banker = seq.count("莊")
    player = seq.count("閒")
    if banker > player:
        next_side = "莊"
    elif player > banker:
        next_side = "閒"
    else:
        tail3 = seq[-3:]
        next_side = "莊" if tail3.count("莊") >= tail3.count("閒") else "閒"

    return {
        "rule": "偏勢補位",
        "next_side": next_side,
        "reason": "目前無明顯主規律，以近10口方向補位",
        "risk": "高",
    }


def free_analysis_allowed(user_id: str) -> bool:
    if is_vip(user_id):
        return True
    state = ensure_user(user_id)
    return state["free_analysis_used"] < FREE_ANALYSIS_LIMIT


def consume_free_analysis(user_id: str):
    if is_vip(user_id):
        return
    state = ensure_user(user_id)
    state["free_analysis_used"] += 1


def capital_band_to_value(text):
    mapping = {
        "1000以下": 1000,
        "1000～3000": 2000,
        "3000～5000": 4000,
        "5000～10000": 7500,
        "10000～30000": 20000,
        "30000以上": 30000,
    }
    return mapping.get(text)


def target_band_to_multiplier(text):
    mapping = {
        "基礎": 1,
        "穩定": 2,
        "進階": 3,
        "高階": 5,
        "衝刺": 8,
        "極限": 10,
    }
    return mapping.get(text)


def style_base_percent(style):
    if style == "保守":
        return 0.02
    if style == "標準":
        return 0.035
    return 0.05


def multiplier_factor(multiplier):
    mapping = {1: 1.0, 2: 1.1, 3: 1.2, 5: 1.35, 8: 1.5, 10: 1.7}
    return mapping.get(multiplier, 1.0)


def build_bankroll_plan(capital_value: int, target_multiplier: int, style: str):
    base = round(capital_value * style_base_percent(style) * multiplier_factor(target_multiplier))
    if base < 50:
        base = 50

    if style == "保守":
        bet1 = base
        bet2 = base
        bet3 = round(base * 0.8)
        lose_text = f"輸1口 → 維持 {bet2}\n輸2口 → 降到 {round(base*0.8)}\n輸3口 → 停手"
        win_text = f"贏1口 → 維持 {bet1}\n連贏2口 → 升到 {round(base*1.1)}\n連贏3口 → 回基礎碼"
        stop_loss = round(capital_value * 0.10)
        stop_win = round(capital_value * min(0.20, 0.05 * target_multiplier))
    elif style == "標準":
        bet1 = base
        bet2 = round(base * 1.2)
        bet3 = base
        lose_text = f"輸1口 → 下口 {bet2}\n輸2口 → 降回 {round(base*0.8)}～{bet1}\n輸3口 → 停手"
        win_text = f"贏1口 → 回基礎碼 {bet1}\n連贏2口 → 可升到 {round(base*1.2)}\n達階段目標 → 建議停利"
        stop_loss = round(capital_value * 0.15)
        stop_win = round(capital_value * min(0.30, 0.08 * target_multiplier))
    else:
        bet1 = base
        bet2 = round(base * 1.3)
        bet3 = round(base * 1.5)
        lose_text = f"輸1口 → 下口 {bet2}\n輸2口 → 強制降碼或停手\n輸3口 → 結束本輪"
        win_text = f"贏1口 → 可維持 {bet1}～{round(base*1.2)}\n連贏2口 → 推進至 {bet2}\n達目標 → 立即收手"
        stop_loss = round(capital_value * 0.20)
        stop_win = round(capital_value * min(0.40, 0.12 * target_multiplier))

    return {
        "base": bet1,
        "bet1": bet1,
        "bet2": bet2,
        "bet3": bet3,
        "lose_text": lose_text,
        "win_text": win_text,
        "stop_loss": stop_loss,
        "stop_win": stop_win,
    }


def bankroll_result_text(user_id: str):
    state = ensure_user(user_id)
    cfg = state.get("config", {})
    capital_value = cfg.get("capital_value")
    target_multiplier = cfg.get("target_multiplier")
    style = cfg.get("style")
    if not (capital_value and target_multiplier and style):
        return "本金配置尚未完成。"

    plan = build_bankroll_plan(capital_value, target_multiplier, style)
    return (
        f"本金配置完成\n\n"
        f"本金區間：{cfg['capital_band']}\n"
        f"目標級別：{cfg['target_band']}（{target_multiplier}倍）\n"
        f"風格：{style}\n\n"
        f"建議基礎碼：{plan['base']}\n\n"
        f"注碼節奏：\n"
        f"第1口：{plan['bet1']}\n"
        f"第2口：{plan['bet2']}\n"
        f"第3口：{plan['bet3']}\n\n"
        f"若輸：\n{plan['lose_text']}\n\n"
        f"若贏：\n{plan['win_text']}\n\n"
        f"建議停損：{plan['stop_loss']}\n"
        f"建議停利：{plan['stop_win']}\n\n"
        f"提醒：只在規律明確時進場，牌路混亂時先觀望。"
    )


def analysis_text(user_id: str, vip: bool):
    state = ensure_user(user_id)
    road = state["road"]
    analysis = analyze_rule(road)
    visible_limit = 20 if vip else 8

    if not vip:
        return (
            f"目前牌路：\n{road_text(road, visible_limit)}\n\n"
            f"規律：\n{analysis['rule']}\n\n"
            f"下一手參考：\n👉 {analysis['next_side']}"
        )

    return (
        f"目前牌路：\n{road_text(road, visible_limit)}\n\n"
        f"規律判定：\n{analysis['rule']}\n\n"
        f"下一手參考：\n👉 {analysis['next_side']}\n\n"
        f"判斷依據：\n{analysis['reason']}\n\n"
        f"風險：\n{analysis['risk']}"
    )


def menu_text(user_id: str):
    vip_tag = "VIP會員" if is_vip(user_id) else "免費版"
    return (
        f"歡迎使用百家節奏分析助手（{vip_tag}）\n\n"
        f"可直接輸入：\n"
        f"莊 / 閒 / 和\n\n"
        f"常用功能：\n"
        f"牌路\n分析\n重設\n本金配置\n注碼\n狀態"
    )


def bankroll_entry_text(vip: bool):
    if not vip:
        return (
            "本金配置為會員功能\n\n"
            "VIP可使用：\n"
            "．互動式本金配置\n"
            "．目標級別選擇\n"
            "．注碼升降建議\n"
            "．停損停利規劃\n\n"
            "開通後可用按鈕一步步完成配置。"
        )
    return "請選擇你的本金區間"


# =========================
# Routes
# =========================
@app.route("/", methods=["GET"])
def home():
    return "Bot running", 200


@app.route("/callback", methods=["GET", "POST"])
def callback():
    if request.method == "GET":
        return "OK", 200

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not body:
        return "OK", 200

    if signature and not verify_signature(body, signature):
        abort(400)

    try:
        data = json.loads(body)
    except Exception:
        return "OK", 200

    for event in data.get("events", []):
        if event.get("type") == "follow":
            user_id = event.get("source", {}).get("userId")
            reply_token = event.get("replyToken")
            if user_id and reply_token:
                ensure_user(user_id)
                reply_message(reply_token, menu_text(user_id), quick_items=make_quick_reply([
                    ("開始", "開始"), ("分析", "分析"), ("牌路", "牌路"), ("本金配置", "本金配置")
                ]))
            continue

        if event.get("type") != "message":
            continue

        message = event.get("message", {})
        if message.get("type") != "text":
            continue

        user_id = event.get("source", {}).get("userId")
        reply_token = event.get("replyToken")
        text = message.get("text", "").strip()

        if not user_id or not reply_token:
            continue

        state = ensure_user(user_id)
        vip = is_vip(user_id)
        touch_user(user_id)

        # Pending bankroll flow
        if vip and state.get("pending_flow") == "capital_band":
            value = capital_band_to_value(text)
            if value:
                state["config"]["capital_band"] = text
                state["config"]["capital_value"] = value
                state["pending_flow"] = "target_band"
                reply_message(reply_token, "請選擇目標級別", quick_items=make_quick_reply([
                    ("基礎", "基礎"), ("穩定", "穩定"), ("進階", "進階"), ("高階", "高階"), ("衝刺", "衝刺"), ("極限", "極限")
                ]))
                continue

        if vip and state.get("pending_flow") == "target_band":
            mult = target_band_to_multiplier(text)
            if mult:
                state["config"]["target_band"] = text
                state["config"]["target_multiplier"] = mult
                state["pending_flow"] = "style"
                reply_message(reply_token, "請選擇操作風格", quick_items=make_quick_reply([
                    ("保守", "保守"), ("標準", "標準"), ("積極", "積極")
                ]))
                continue

        if vip and state.get("pending_flow") == "style":
            if text in ["保守", "標準", "積極"]:
                state["config"]["style"] = text
                state["pending_flow"] = None
                reply_message(reply_token, bankroll_result_text(user_id), quick_items=make_quick_reply([
                    ("分析", "分析"), ("牌路", "牌路"), ("注碼", "注碼")
                ]))
                continue

        # Commands
        if text == "開始":
            reply_message(reply_token, menu_text(user_id), quick_items=make_quick_reply([
                ("分析", "分析"), ("牌路", "牌路"), ("本金配置", "本金配置"), ("重設", "重設")
            ]))
            continue

        if text in ["莊", "閒", "和"]:
            state["road"].append(text)
            state["road"] = state["road"][-MAX_ROAD:]
            if free_analysis_allowed(user_id):
                consume_free_analysis(user_id)
                reply_message(reply_token, f"已記錄：{text}\n\n" + analysis_text(user_id, vip), quick_items=make_quick_reply([
                    ("牌路", "牌路"), ("分析", "分析"), ("注碼", "注碼" if vip else "本金配置")
                ]))
            else:
                reply_message(reply_token, f"已記錄：{text}\n\n今日免費分析次數已用完。\n開通會員可不限次查看完整分析。")
            continue

        if text == "牌路":
            limit = 20 if vip else 8
            reply_message(reply_token, f"目前牌路：\n{road_text(state['road'], limit)}")
            continue

        if text == "分析":
            if not free_analysis_allowed(user_id):
                reply_message(reply_token, "今日免費分析次數已用完。\n開通會員可不限次查看完整分析。")
            else:
                consume_free_analysis(user_id)
                reply_message(reply_token, analysis_text(user_id, vip), quick_items=make_quick_reply([
                    ("牌路", "牌路"), ("本金配置", "本金配置"), ("重設", "重設")
                ]))
            continue

        if text == "重設":
            state["road"] = []
            reply_message(reply_token, "已重設當前牌路。")
            continue

        if text == "狀態":
            mins = int((now_tw() - state["last_update"]).total_seconds() // 60)
            reply_message(reply_token, f"目前已記錄 {len(state['road'])} 顆\n最近更新：{mins} 分鐘內")
            continue

        if text == "本金配置":
            if not vip:
                reply_message(reply_token, bankroll_entry_text(vip))
            else:
                state["pending_flow"] = "capital_band"
                reply_message(reply_token, bankroll_entry_text(vip), quick_items=make_quick_reply([
                    ("1000以下", "1000以下"), ("1000～3000", "1000～3000"), ("3000～5000", "3000～5000"),
                    ("5000～10000", "5000～10000"), ("10000～30000", "10000～30000"), ("30000以上", "30000以上")
                ]))
            continue

        if text == "注碼":
            if not vip:
                reply_message(reply_token, "注碼功能為會員專用。\n開通後可使用互動式本金配置與升降碼建議。")
            else:
                reply_message(reply_token, bankroll_result_text(user_id))
            continue

        # Admin simple VIP toggle
        if user_id in ADMIN_USER_IDS and text.startswith("/vip "):
            target = text.replace("/vip ", "", 1).strip()
            VIP_USER_IDS.add(target)
            reply_message(reply_token, f"已加入VIP：{target}")
            continue

        reply_message(reply_token, f"你剛剛說：{text}\n\n可用功能：開始 / 分析 / 牌路 / 本金配置 / 重設")

    return "OK", 200
