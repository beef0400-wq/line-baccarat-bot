from flask import Flask, request, abort
import os
import json
import hmac
import hashlib
import base64
import requests
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta, timezone

app = Flask(__name__)

# =========================
# ENV
# =========================
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_USER_IDS = set(
    x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()
)

TZ_TW = timezone(timedelta(hours=8))
TIMEOUT_MINUTES = 20
MAX_ROAD = 20


# =========================
# DB
# =========================
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    sql = """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        line_user_id TEXT UNIQUE NOT NULL,
        game_account TEXT UNIQUE,
        vip_expire_at TIMESTAMP NULL,
        free_expire_at TIMESTAMP NULL,
        current_road JSONB NOT NULL DEFAULT '[]'::jsonb,
        pending_flow TEXT NULL,
        bankroll_config JSONB NOT NULL DEFAULT '{}'::jsonb,
        free_analysis_used INTEGER NOT NULL DEFAULT 0,
        free_analysis_date TEXT NULL,
        last_active_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_users_game_account ON users(game_account);
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


# =========================
# Time / helpers
# =========================
def now_tw():
    return datetime.now(TZ_TW).replace(tzinfo=None)


def today_str():
    return now_tw().strftime("%Y-%m-%d")


def ensure_user(line_user_id: str):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM users WHERE line_user_id = %s",
                (line_user_id,),
            )
            user = cur.fetchone()

            if not user:
                free_expire_at = now_tw() + timedelta(hours=3)
                cur.execute(
                    """
                    INSERT INTO users (
                        line_user_id, free_expire_at, current_road,
                        bankroll_config, free_analysis_used, free_analysis_date,
                        last_active_at, created_at, updated_at
                    )
                    VALUES (%s, %s, '[]'::jsonb, '{}'::jsonb, 0, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        line_user_id,
                        free_expire_at,
                        today_str(),
                        now_tw(),
                        now_tw(),
                        now_tw(),
                    ),
                )
                user = cur.fetchone()
                conn.commit()

            # reset daily free analysis counter
            if user["free_analysis_date"] != today_str():
                cur.execute(
                    """
                    UPDATE users
                    SET free_analysis_used = 0,
                        free_analysis_date = %s,
                        updated_at = %s
                    WHERE line_user_id = %s
                    RETURNING *
                    """,
                    (today_str(), now_tw(), line_user_id),
                )
                user = cur.fetchone()
                conn.commit()

            # auto reset road after timeout
            last_active_at = user["last_active_at"]
            if last_active_at and (now_tw() - last_active_at > timedelta(minutes=TIMEOUT_MINUTES)):
                cur.execute(
                    """
                    UPDATE users
                    SET current_road = '[]'::jsonb,
                        updated_at = %s
                    WHERE line_user_id = %s
                    RETURNING *
                    """,
                    (now_tw(), line_user_id),
                )
                user = cur.fetchone()
                conn.commit()

            return user


def refresh_user(line_user_id: str):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE users
                SET last_active_at = %s,
                    updated_at = %s
                WHERE line_user_id = %s
                RETURNING *
                """,
                (now_tw(), now_tw(), line_user_id),
            )
            user = cur.fetchone()
            conn.commit()
            return user


def get_user(line_user_id: str):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE line_user_id = %s", (line_user_id,))
            return cur.fetchone()


def is_vip(user) -> bool:
    vip_expire_at = user.get("vip_expire_at")
    return bool(vip_expire_at and vip_expire_at > now_tw())


def free_active(user) -> bool:
    free_expire_at = user.get("free_expire_at")
    return bool(free_expire_at and free_expire_at > now_tw())


def minutes_left(dt):
    if not dt:
        return 0
    delta = dt - now_tw()
    mins = int(delta.total_seconds() // 60)
    return max(mins, 0)


def verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected_signature = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature)


# =========================
# LINE API
# =========================
def line_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }


def reply_message(reply_token: str, text: str, quick_items=None):
    msg = {"type": "text", "text": text}
    if quick_items:
        msg["quickReply"] = {"items": quick_items}

    payload = {
        "replyToken": reply_token,
        "messages": [msg],
    }
    r = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers=line_headers(),
        data=json.dumps(payload),
        timeout=15,
    )
    print("REPLY STATUS:", r.status_code)
    print("REPLY BODY:", r.text)


def push_message(user_id: str, text: str):
    payload = {
        "to": user_id,
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
        items.append(
            {
                "type": "action",
                "action": {
                    "type": "message",
                    "label": label,
                    "text": text,
                },
            }
        )
    return items


# =========================
# Road / Analysis
# =========================
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

    tail_count, tail_side = count_tail_same(seq)
    if tail_count >= 5:
        return {
            "rule": "長龍",
            "next_side": tail_side,
            "reason": "同邊連開超過5顆，先均注順跟到斷",
            "risk": "低",
        }

    if is_double_jump(seq):
        nxt = double_jump_next(seq)
        return {
            "rule": "雙跳",
            "next_side": nxt,
            "reason": "目前維持雙跳節奏，第5顆後順規律",
            "risk": "低",
        }

    if is_single_jump(seq):
        nxt = single_jump_next(seq)
        return {
            "rule": "單跳",
            "next_side": nxt,
            "reason": "近期維持交叉節奏，先跟到斷",
            "risk": "中",
        }

    if len(seq) >= 6:
        last6 = seq[-6:]
        if (
            last6[0] != last6[1]
            and last6[1] != last6[2]
            and last6[2] != last6[3]
            and last6[3] != last6[4]
            and last6[4] == last6[5]
        ):
            nxt = "閒" if last6[-1] == "莊" else "莊"
            return {
                "rule": "單跳中斷",
                "next_side": nxt,
                "reason": "單跳斷第一口，先反打回原規律",
                "risk": "中",
            }

    if len(seq) >= 4:
        last4 = seq[-4:]
        if last4[0] == last4[1] == last4[2] and last4[3] != last4[2]:
            return {
                "rule": "三連反打",
                "next_side": last4[2],
                "reason": "3連後出現反開，先反打回原邊",
                "risk": "中",
            }

    if len(seq) >= 4:
        tail_count, tail_side = count_tail_same(seq)
        if tail_count in [4, 5, 6]:
            return {
                "rule": "三連順跟",
                "next_side": tail_side,
                "reason": "3連後延續，順勢跟到第6顆",
                "risk": "中",
            }

    if detect_qitou(seq):
        segs = segment_lengths(seq)
        a, b = segs[-2], segs[-1]
        return {
            "rule": "齊頭",
            "next_side": a[0],
            "reason": "前後兩段長度一致，可依齊頭節奏反打延續",
            "risk": "中",
        }

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


def add_result(line_user_id: str, result: str):
    user = ensure_user(line_user_id)
    road = user["current_road"] or []
    road.append(result)
    road = road[-MAX_ROAD:]

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE users
                SET current_road = %s::jsonb,
                    last_active_at = %s,
                    updated_at = %s
                WHERE line_user_id = %s
                RETURNING *
                """,
                (json.dumps(road, ensure_ascii=False), now_tw(), now_tw(), line_user_id),
            )
            row = cur.fetchone()
            conn.commit()
            return row


def clear_road(line_user_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET current_road = '[]'::jsonb,
                    updated_at = %s
                WHERE line_user_id = %s
                """,
                (now_tw(), line_user_id),
            )
        conn.commit()


# =========================
# Free / VIP analysis limit
# =========================
def free_analysis_allowed(user) -> bool:
    if is_vip(user):
        return True
    if free_active(user):
        return True
    return False


def get_status_text(user):
    if is_vip(user):
        mins = minutes_left(user["vip_expire_at"])
        days = mins // 1440
        return (
            "目前狀態：VIP\n\n"
            f"到期時間：{user['vip_expire_at']}\n"
            f"剩餘：約 {days} 天"
        )

    if free_active(user):
        mins = minutes_left(user["free_expire_at"])
        return (
            "目前狀態：免費試用中\n\n"
            f"剩餘時間：約 {mins} 分鐘\n"
            "試用到期後，將可使用基本功能，完整分析與本金配置需開通VIP。"
        )

    return (
        "目前狀態：未開通VIP\n\n"
        "可使用基本功能。\n"
        "如需完整規律判斷 / 本金配置 / 注碼建議，請先綁定遊戲帳號並由管理員開通。"
    )


# =========================
# Bankroll config
# =========================
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
        lose_text = f"輸1口 → 維持 {bet2}\n輸2口 → 降到 {round(base * 0.8)}\n輸3口 → 停手"
        win_text = f"贏1口 → 維持 {bet1}\n連贏2口 → 升到 {round(base * 1.1)}\n連贏3口 → 回基礎碼"
        stop_loss = round(capital_value * 0.10)
        stop_win = round(capital_value * min(0.20, 0.05 * target_multiplier))
    elif style == "標準":
        bet1 = base
        bet2 = round(base * 1.2)
        bet3 = base
        lose_text = f"輸1口 → 下口 {bet2}\n輸2口 → 降回 {round(base * 0.8)}～{bet1}\n輸3口 → 停手"
        win_text = f"贏1口 → 回基礎碼 {bet1}\n連贏2口 → 可升到 {round(base * 1.2)}\n達階段目標 → 建議停利"
        stop_loss = round(capital_value * 0.15)
        stop_win = round(capital_value * min(0.30, 0.08 * target_multiplier))
    else:
        bet1 = base
        bet2 = round(base * 1.3)
        bet3 = round(base * 1.5)
        lose_text = f"輸1口 → 下口 {bet2}\n輸2口 → 強制降碼或停手\n輸3口 → 結束本輪"
        win_text = f"贏1口 → 可維持 {bet1}～{round(base * 1.2)}\n連贏2口 → 推進至 {bet2}\n達目標 → 立即收手"
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


def update_pending_flow(line_user_id: str, flow: str | None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET pending_flow = %s,
                    updated_at = %s
                WHERE line_user_id = %s
                """,
                (flow, now_tw(), line_user_id),
            )
        conn.commit()


def update_bankroll_config(line_user_id: str, config: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET bankroll_config = %s::jsonb,
                    updated_at = %s
                WHERE line_user_id = %s
                """,
                (json.dumps(config, ensure_ascii=False), now_tw(), line_user_id),
            )
        conn.commit()


def bankroll_result_text(user):
    cfg = user.get("bankroll_config") or {}
    capital_value = cfg.get("capital_value")
    target_multiplier = cfg.get("target_multiplier")
    style = cfg.get("style")

    if not (capital_value and target_multiplier and style):
        return "本金配置尚未完成。"

    plan = build_bankroll_plan(capital_value, target_multiplier, style)
    return (
        f"本金配置完成\n\n"
        f"本金區間：{cfg.get('capital_band')}\n"
        f"目標級別：{cfg.get('target_band')}（{target_multiplier}倍）\n"
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


def analysis_text(user, vip: bool):
    road = user["current_road"] or []
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


def menu_text(user):
    vip_tag = "VIP會員" if is_vip(user) else ("免費試用中" if free_active(user) else "免費版")
    return (
        f"歡迎使用百家節奏分析助手（{vip_tag}）\n\n"
        f"可直接輸入：\n"
        f"莊 / 閒 / 和\n\n"
        f"常用功能：\n"
        f"牌路\n分析\n重設\n綁定帳號\n查詢資格\n本金配置\n注碼"
    )


# =========================
# Account binding / admin VIP
# =========================
def set_game_account(line_user_id: str, game_account: str) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE users
                    SET game_account = %s,
                        pending_flow = NULL,
                        updated_at = %s
                    WHERE line_user_id = %s
                    """,
                    (game_account, now_tw(), line_user_id),
                )
            conn.commit()
        return True
    except psycopg2.Error:
        return False


def get_user_by_game_account(game_account: str):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE game_account = %s", (game_account,))
            return cur.fetchone()


def list_pending_accounts():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT game_account, line_user_id, created_at
                FROM users
                WHERE game_account IS NOT NULL
                  AND (vip_expire_at IS NULL OR vip_expire_at < %s)
                ORDER BY created_at DESC
                """,
                (now_tw(),),
            )
            return cur.fetchall()


def grant_vip_by_game_account(game_account: str, days: int):
    user = get_user_by_game_account(game_account)
    if not user:
        return None, "找不到此遊戲帳號"

    current_expire = user.get("vip_expire_at")
    if current_expire and current_expire > now_tw():
        new_expire = current_expire + timedelta(days=days)
    else:
        new_expire = now_tw() + timedelta(days=days)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET vip_expire_at = %s,
                    updated_at = %s
                WHERE game_account = %s
                """,
                (new_expire, now_tw(), game_account),
            )
        conn.commit()

    return get_user_by_game_account(game_account), None


def revoke_vip_by_game_account(game_account: str):
    user = get_user_by_game_account(game_account)
    if not user:
        return False

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET vip_expire_at = NULL,
                    updated_at = %s
                WHERE game_account = %s
                """,
                (now_tw(), game_account),
            )
        conn.commit()
    return True


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
        print("USER ID:", event.get("source", {}).get("userId"))

        if event.get("type") == "follow":
            user_id = event.get("source", {}).get("userId")
            reply_token = event.get("replyToken")
            if user_id and reply_token:
                user = ensure_user(user_id)
                reply_message(
                    reply_token,
                    menu_text(user),
                    quick_items=make_quick_reply([
                        ("開始", "開始"),
                        ("分析", "分析"),
                        ("牌路", "牌路"),
                        ("綁定帳號", "綁定帳號"),
                    ]),
                )
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

        user = ensure_user(user_id)
        user = refresh_user(user_id)
        user = get_user(user_id)
        vip = is_vip(user)

        # ========= pending flows =========
        if user.get("pending_flow") == "bind_game_account":
            ok = set_game_account(user_id, text)
            if ok:
                reply_message(
                    reply_token,
                    f"已收到你的遊戲帳號：{text}\n\n請等待管理員確認開通VIP。",
                    quick_items=make_quick_reply([
                        ("查詢資格", "查詢資格"),
                        ("分析", "分析"),
                    ]),
                )
            else:
                reply_message(reply_token, "這個遊戲帳號可能已被綁定，請換一個或聯絡管理員。")
            continue

        if vip and user.get("pending_flow") == "capital_band":
            value = capital_band_to_value(text)
            if value:
                cfg = user.get("bankroll_config") or {}
                cfg["capital_band"] = text
                cfg["capital_value"] = value
                update_bankroll_config(user_id, cfg)
                update_pending_flow(user_id, "target_band")
                reply_message(
                    reply_token,
                    "請選擇目標級別",
                    quick_items=make_quick_reply([
                        ("基礎", "基礎"),
                        ("穩定", "穩定"),
                        ("進階", "進階"),
                        ("高階", "高階"),
                        ("衝刺", "衝刺"),
                        ("極限", "極限"),
                    ]),
                )
                continue

        user = get_user(user_id)
        if vip and user.get("pending_flow") == "target_band":
            mult = target_band_to_multiplier(text)
            if mult:
                cfg = user.get("bankroll_config") or {}
                cfg["target_band"] = text
                cfg["target_multiplier"] = mult
                update_bankroll_config(user_id, cfg)
                update_pending_flow(user_id, "style")
                reply_message(
                    reply_token,
                    "請選擇操作風格",
                    quick_items=make_quick_reply([
                        ("保守", "保守"),
                        ("標準", "標準"),
                        ("積極", "積極"),
                    ]),
                )
                continue

        user = get_user(user_id)
        if vip and user.get("pending_flow") == "style":
            if text in ["保守", "標準", "積極"]:
                cfg = user.get("bankroll_config") or {}
                cfg["style"] = text
                update_bankroll_config(user_id, cfg)
                update_pending_flow(user_id, None)
                user = get_user(user_id)
                reply_message(
                    reply_token,
                    bankroll_result_text(user),
                    quick_items=make_quick_reply([
                        ("分析", "分析"),
                        ("牌路", "牌路"),
                        ("注碼", "注碼"),
                    ]),
                )
                continue

        # ========= admin =========
        if user_id in ADMIN_USER_IDS and text == "/待開通":
            pending = list_pending_accounts()
            if not pending:
                reply_message(reply_token, "目前沒有待開通名單。")
            else:
                rows = []
                for i, row in enumerate(pending[:20], start=1):
                    rows.append(f"{i}. {row['game_account']}")
                reply_message(reply_token, "待開通名單：\n" + "\n".join(rows))
            continue

        if user_id in ADMIN_USER_IDS and text.startswith("/vip "):
            parts = text.split()
            if len(parts) != 3:
                reply_message(reply_token, "格式錯誤，請用：/vip 遊戲帳號 天數")
                continue

            game_account = parts[1]
            try:
                days = int(parts[2])
            except ValueError:
                reply_message(reply_token, "天數請輸入數字，例如：/vip ck76888 30")
                continue

            updated_user, err = grant_vip_by_game_account(game_account, days)
            if err:
                reply_message(reply_token, err)
            else:
                reply_message(
                    reply_token,
                    f"已開通VIP\n\n帳號：{game_account}\n天數：{days}天\n到期：{updated_user['vip_expire_at']}",
                )
                push_message(
                    updated_user["line_user_id"],
                    f"你的VIP已開通\n\n到期時間：{updated_user['vip_expire_at']}\n\n現在可使用完整分析 / 本金配置 / 注碼建議。",
                )
            continue

        if user_id in ADMIN_USER_IDS and text.startswith("/unvip "):
            parts = text.split()
            if len(parts) != 2:
                reply_message(reply_token, "格式錯誤，請用：/unvip 遊戲帳號")
                continue

            ok = revoke_vip_by_game_account(parts[1])
            if ok:
                reply_message(reply_token, f"已取消VIP：{parts[1]}")
            else:
                reply_message(reply_token, "找不到這個遊戲帳號。")
            continue

        if user_id in ADMIN_USER_IDS and text.startswith("/查帳號 "):
            parts = text.split()
            if len(parts) != 2:
                reply_message(reply_token, "格式錯誤，請用：/查帳號 遊戲帳號")
                continue

            row = get_user_by_game_account(parts[1])
            if not row:
                reply_message(reply_token, "找不到這個遊戲帳號。")
            else:
                status = "VIP" if is_vip(row) else "非VIP"
                reply_message(
                    reply_token,
                    f"帳號：{parts[1]}\n狀態：{status}\n到期：{row.get('vip_expire_at')}\nLINE ID：{row.get('line_user_id')}",
                )
            continue

        # ========= user commands =========
        if text == "開始":
            reply_message(
                reply_token,
                menu_text(user),
                quick_items=make_quick_reply([
                    ("分析", "分析"),
                    ("牌路", "牌路"),
                    ("綁定帳號", "綁定帳號"),
                    ("本金配置", "本金配置"),
                ]),
            )
            continue

        if text == "綁定帳號":
            update_pending_flow(user_id, "bind_game_account")
            reply_message(reply_token, "請輸入你的遊戲帳號\n例如：ck76888")
            continue

        if text == "查詢資格":
            user = get_user(user_id)
            reply_message(reply_token, get_status_text(user))
            continue

        if text in ["莊", "閒", "和"]:
            user = add_result(user_id, text)
            user = get_user(user_id)
            vip = is_vip(user)
            reply_message(
                reply_token,
                f"已記錄：{text}\n\n" + analysis_text(user, vip),
                quick_items=make_quick_reply([
                    ("牌路", "牌路"),
                    ("分析", "分析"),
                    ("本金配置", "本金配置"),
                ]),
            )
            continue

        if text == "牌路":
            user = get_user(user_id)
            vip = is_vip(user)
            limit = 20 if vip else 8
            reply_message(reply_token, f"目前牌路：\n{road_text(user['current_road'] or [], limit)}")
            continue

        if text == "分析":
            user = get_user(user_id)
            vip = is_vip(user)
            reply_message(reply_token, analysis_text(user, vip))
            continue

        if text == "重設":
            clear_road(user_id)
            reply_message(reply_token, "已重設當前牌路。")
            continue

        if text == "狀態":
            user = get_user(user_id)
            mins = int((now_tw() - user["last_active_at"]).total_seconds() // 60)
            reply_message(reply_token, f"目前已記錄 {len(user['current_road'] or [])} 顆\n最近更新：{mins} 分鐘內")
            continue

        if text == "本金配置":
            user = get_user(user_id)
            if not is_vip(user):
                reply_message(
                    reply_token,
                    "本金配置為會員功能\n\nVIP可使用：\n．互動式本金配置\n．目標級別選擇\n．注碼升降建議\n．停損停利規劃\n\n開通後可用按鈕一步步完成配置。",
                )
            else:
                update_pending_flow(user_id, "capital_band")
                reply_message(
                    reply_token,
                    "請選擇你的本金區間",
                    quick_items=make_quick_reply([
                        ("1000以下", "1000以下"),
                        ("1000～3000", "1000～3000"),
                        ("3000～5000", "3000～5000"),
                        ("5000～10000", "5000～10000"),
                        ("10000～30000", "10000～30000"),
                        ("30000以上", "30000以上"),
                    ]),
                )
            continue

        if text == "注碼":
            user = get_user(user_id)
            if not is_vip(user):
                reply_message(reply_token, "注碼功能為會員專用。\n開通後可使用互動式本金配置與升降碼建議。")
            else:
                reply_message(reply_token, bankroll_result_text(user))
            continue

        reply_message(
            reply_token,
            f"你剛剛說：{text}\n\n可用功能：開始 / 分析 / 牌路 / 綁定帳號 / 查詢資格 / 本金配置 / 重設",
        )

    return "OK", 200


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
else:
    init_db()
