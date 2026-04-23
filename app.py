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

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_USER_IDS = set(
    x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()
)

TZ_TW = timezone(timedelta(hours=8))
TIMEOUT_MINUTES = 20
MAX_ROAD = 100
MIN_IMPORT_HANDS = 15


# =========================
# DB
# =========================
def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    statements = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            line_user_id TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS game_account TEXT UNIQUE;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS vip_expire_at TIMESTAMP NULL;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_end_at TIMESTAMP NULL;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS current_road JSONB NOT NULL DEFAULT '[]'::jsonb;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_flow TEXT NULL;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS analysis_active BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS imported_ready BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;",
        "CREATE INDEX IF NOT EXISTS idx_users_game_account ON users(game_account);",
        """
        CREATE TABLE IF NOT EXISTS analysis_logs (
            id SERIAL PRIMARY KEY,
            line_user_id TEXT NOT NULL,
            road_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
            banker_pct INTEGER NOT NULL,
            player_pct INTEGER NOT NULL,
            pattern TEXT NOT NULL,
            risk TEXT NOT NULL,
            actual_next_result TEXT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_analysis_logs_user ON analysis_logs(line_user_id);",
    ]

    with get_conn() as conn:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
        conn.commit()


# =========================
# Helpers
# =========================
def now_tw():
    return datetime.now(TZ_TW).replace(tzinfo=None)


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


def base_quick_reply(is_admin=False, analysis_active=False):
    if analysis_active:
        items = [
            ("莊", "莊"),
            ("閒", "閒"),
            ("和", "和"),
            ("分析", "分析"),
            ("牌路", "牌路"),
            ("結束分析", "結束分析"),
        ]
    else:
        items = [
            ("開始", "開始"),
            ("匯入牌路", "匯入牌路"),
            ("開始分析", "開始分析"),
            ("牌路", "牌路"),
            ("分析", "分析"),
            ("綁定帳號", "綁定帳號"),
        ]
    if is_admin:
        items[-1] = ("/待開通", "/待開通")
    return make_quick_reply(items)


def reply_message(reply_token: str, text: str, quick_items=None):
    msg = {"type": "text", "text": text}
    if quick_items:
        msg["quickReply"] = {"items": quick_items}

    payload = {"replyToken": reply_token, "messages": [msg]}
    r = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers=line_headers(),
        data=json.dumps(payload),
        timeout=15,
    )
    print("REPLY STATUS:", r.status_code)
    print("REPLY BODY:", r.text)


def push_message(user_id: str, text: str):
    payload = {"to": user_id, "messages": [{"type": "text", "text": text}]}
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers=line_headers(),
        data=json.dumps(payload),
        timeout=15,
    )
    print("PUSH STATUS:", r.status_code)
    print("PUSH BODY:", r.text)


# =========================
# User / state
# =========================
def get_user(line_user_id: str):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE line_user_id = %s", (line_user_id,))
            return cur.fetchone()


def ensure_user(line_user_id: str):
    user = get_user(line_user_id)
    if user:
        last_active = user["last_active_at"]
        if last_active and (now_tw() - last_active > timedelta(minutes=TIMEOUT_MINUTES)):
            with get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        UPDATE users
                        SET current_road = '[]'::jsonb,
                            analysis_active = FALSE,
                            imported_ready = FALSE,
                            updated_at = %s
                        WHERE line_user_id = %s
                        RETURNING *
                        """,
                        (now_tw(), line_user_id),
                    )
                    user = cur.fetchone()
                conn.commit()
        return user

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO users (
                    line_user_id, trial_end_at, current_road, pending_flow,
                    analysis_active, imported_ready, last_active_at, created_at, updated_at
                )
                VALUES (%s, %s, '[]'::jsonb, NULL, FALSE, FALSE, %s, %s, %s)
                RETURNING *
                """,
                (
                    line_user_id,
                    now_tw() + timedelta(hours=3),
                    now_tw(),
                    now_tw(),
                    now_tw(),
                    now_tw(),
                ),
            )
            user = cur.fetchone()
        conn.commit()
        return user


def update_user_fields(line_user_id: str, **fields):
    if not fields:
        return get_user(line_user_id)

    allowed = {
        "game_account",
        "vip_expire_at",
        "trial_end_at",
        "current_road",
        "pending_flow",
        "analysis_active",
        "imported_ready",
        "last_active_at",
        "updated_at",
    }

    set_parts = []
    values = []

    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "current_road":
            set_parts.append(f"{key} = %s::jsonb")
            values.append(json.dumps(value, ensure_ascii=False))
        else:
            set_parts.append(f"{key} = %s")
            values.append(value)

    set_parts.append("updated_at = %s")
    values.append(now_tw())
    values.append(line_user_id)

    sql = f"""
    UPDATE users
    SET {", ".join(set_parts)}
    WHERE line_user_id = %s
    RETURNING *
    """

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, tuple(values))
            row = cur.fetchone()
        conn.commit()
        return row


def touch_user(line_user_id: str):
    return update_user_fields(line_user_id, last_active_at=now_tw())


def is_vip(user) -> bool:
    return bool(user and user.get("vip_expire_at") and user["vip_expire_at"] > now_tw())


def in_trial(user) -> bool:
    return bool(user and user.get("trial_end_at") and user["trial_end_at"] > now_tw())


def minutes_left(dt):
    if not dt:
        return 0
    return max(int((dt - now_tw()).total_seconds() // 60), 0)


# =========================
# Admin / binding
# =========================
def set_game_account(line_user_id: str, game_account: str) -> bool:
    try:
        update_user_fields(line_user_id, game_account=game_account, pending_flow=None)
        return True
    except psycopg2.Error:
        return False


def get_user_by_game_account(game_account: str):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE game_account = %s", (game_account,))
            return cur.fetchone()


def grant_vip_by_game_account(game_account: str, days: int):
    user = get_user_by_game_account(game_account)
    if not user:
        return None, "找不到此遊戲帳號"

    current_expire = user.get("vip_expire_at")
    if current_expire and current_expire > now_tw():
        new_expire = current_expire + timedelta(days=days)
    else:
        new_expire = now_tw() + timedelta(days=days)

    update_user_fields(user["line_user_id"], vip_expire_at=new_expire)
    return get_user_by_game_account(game_account), None


def revoke_vip_by_game_account(game_account: str):
    user = get_user_by_game_account(game_account)
    if not user:
        return False
    update_user_fields(user["line_user_id"], vip_expire_at=None)
    return True


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


# =========================
# Road helpers
# =========================
def road_text(road, limit=None):
    data = road[-limit:] if limit else road
    return "".join(data) if data else "尚無資料"


def filter_main_road(road):
    return [x for x in road if x in ["莊", "閒"]]


def normalize_input_road(raw: str):
    raw = raw.strip().replace(" ", "").replace("\n", "").replace("\r", "")
    tokens = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch in ["莊", "閒", "和"]:
            tokens.append(ch)
            i += 1
        else:
            return None
    return tokens


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
    cur = seq[0]
    cnt = 1
    for x in seq[1:]:
        if x == cur:
            cnt += 1
        else:
            segs.append((cur, cnt))
            cur = x
            cnt = 1
    segs.append((cur, cnt))
    return segs


def alternation_count(seq):
    cnt = 0
    for i in range(1, len(seq)):
        if seq[i] != seq[i - 1]:
            cnt += 1
    return cnt


def classify_pattern(seq):
    if len(seq) < 2:
        return "資料不足", "高"

    tail_count, _ = count_tail_same(seq)
    if tail_count >= 5:
        return "長連續型", "中低"

    segs = segment_lengths(seq)
    if len(segs) >= 4:
        tail = segs[-4:]
        if all(c == 2 for _, c in tail[:-1]) and tail[-1][1] in [1, 2]:
            return "雙跳型態", "中"

    alt = alternation_count(seq[-5:] if len(seq) >= 5 else seq)
    if len(seq) >= 4 and alt >= len((seq[-5:] if len(seq) >= 5 else seq)) - 1:
        return "單跳型態", "中"

    if len(segs) >= 2 and segs[-1][1] == segs[-2][1] and segs[-1][0] != segs[-2][0]:
        return "齊頭型態", "中高"

    return "混合型態", "高"


def pattern_percentages(road):
    seq = filter_main_road(road)[-10:]
    if not seq:
        return {
            "banker_pct": 50,
            "player_pct": 50,
            "pattern": "資料不足",
            "risk": "高",
            "counts": {"莊": 0, "閒": 0, "交錯": 0, "最長連續": 0},
        }

    banker = seq.count("莊")
    player = seq.count("閒")
    alt = alternation_count(seq)
    segs = segment_lengths(seq)
    longest = max((c for _, c in segs), default=0)
    tail_count, tail_side = count_tail_same(seq)

    banker_score = banker * 10
    player_score = player * 10

    if tail_side == "莊":
        banker_score += min(tail_count * 4, 16)
    elif tail_side == "閒":
        player_score += min(tail_count * 4, 16)

    if alt >= max(3, len(seq) - 2):
        if seq[-1] == "莊":
            player_score += 12
        else:
            banker_score += 12

    if len(segs) >= 4:
        tail = segs[-4:]
        if len(tail) == 4 and all(c == 2 for _, c in tail[:-1]):
            expected_side = tail[-1][0] if tail[-1][1] == 1 else ("閒" if tail[-1][0] == "莊" else "莊")
            if expected_side == "莊":
                banker_score += 10
            else:
                player_score += 10

    total = max(banker_score + player_score, 1)
    banker_pct = round(banker_score * 100 / total)
    player_pct = 100 - banker_pct
    pattern, risk = classify_pattern(seq)

    return {
        "banker_pct": banker_pct,
        "player_pct": player_pct,
        "pattern": pattern,
        "risk": risk,
        "counts": {
            "莊": banker,
            "閒": player,
            "交錯": alt,
            "最長連續": longest,
        },
    }


def probability_card(road):
    seq = filter_main_road(road)[-10:]
    data = pattern_percentages(road)

    return (
        "📊 目前牌路分析\n\n"
        f"牌路：\n{road_text(seq)}\n\n"
        "模型判讀：\n"
        f"👉 莊：{data['banker_pct']}%\n"
        f"👉 閒：{data['player_pct']}%\n\n"
        "節奏：\n"
        f"👉 {data['pattern']}\n\n"
        "補充統計：\n"
        f"莊：{data['counts']['莊']}\n"
        f"閒：{data['counts']['閒']}\n"
        f"交錯次數：{data['counts']['交錯']}\n"
        f"最長連續：{data['counts']['最長連續']}\n\n"
        "風險：\n"
        f"{data['risk']}"
    )


# =========================
# Backtest logs
# =========================
def create_analysis_log(line_user_id: str, road):
    data = pattern_percentages(road)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO analysis_logs (
                    line_user_id, road_snapshot, banker_pct, player_pct, pattern, risk, actual_next_result
                )
                VALUES (%s, %s::jsonb, %s, %s, %s, %s, NULL)
                """,
                (
                    line_user_id,
                    json.dumps(filter_main_road(road)[-10:], ensure_ascii=False),
                    data["banker_pct"],
                    data["player_pct"],
                    data["pattern"],
                    data["risk"],
                ),
            )
        conn.commit()


def backfill_previous_actual(line_user_id: str, actual_result: str):
    if actual_result not in ["莊", "閒"]:
        return

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id
                FROM analysis_logs
                WHERE line_user_id = %s
                  AND actual_next_result IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (line_user_id,),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    """
                    UPDATE analysis_logs
                    SET actual_next_result = %s
                    WHERE id = %s
                    """,
                    (actual_result, row["id"]),
                )
        conn.commit()


def hit_rate_summary(line_user_id: str):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT banker_pct, player_pct, actual_next_result
                FROM analysis_logs
                WHERE line_user_id = %s
                  AND actual_next_result IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 30
                """,
                (line_user_id,),
            )
            rows = cur.fetchall()

    if not rows:
        return "回測資料不足"

    total = 0
    hit = 0
    for row in rows:
        predicted = "莊" if row["banker_pct"] >= row["player_pct"] else "閒"
        if predicted == row["actual_next_result"]:
            hit += 1
        total += 1

    rate = round(hit * 100 / total)
    return f"近{total}筆回測命中：{rate}%"


# =========================
# Status / menus
# =========================
def get_status_text(user):
    if is_vip(user):
        mins = minutes_left(user["vip_expire_at"])
        days = mins // 1440
        return (
            "目前狀態：VIP\n\n"
            f"到期時間：{user['vip_expire_at']}\n"
            f"剩餘：約 {days} 天"
        )

    if in_trial(user):
        mins = minutes_left(user["trial_end_at"])
        return (
            "目前狀態：免費試用中\n\n"
            f"剩餘時間：約 {mins} 分鐘\n"
            "試用結束後，完整分析需開通VIP。"
        )

    return (
        "試用已結束\n\n"
        "VIP開通：\n"
        "👉 註冊3A帳號 / 已有3A帳號\n"
        "👉 聯絡管理員"
    )


def menu_text(user):
    tag = "VIP會員" if is_vip(user) else ("免費試用中" if in_trial(user) else "免費版")
    return (
        f"歡迎使用百家即時分析助手（{tag}）\n\n"
        "可用流程：\n"
        "1. 匯入牌路\n"
        "2. 開始分析\n"
        "3. 分析中逐口按 莊 / 閒 / 和\n"
        "4. 結束分析\n\n"
        "常用功能：\n"
        "牌路\n"
        "分析\n"
        "綁定帳號\n"
        "查詢資格"
    )


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
        user_id = event.get("source", {}).get("userId")
        print("USER ID:", user_id)

        if event.get("type") == "follow":
            reply_token = event.get("replyToken")
            if user_id and reply_token:
                user = ensure_user(user_id)
                reply_message(
                    reply_token,
                    menu_text(user),
                    quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, user["analysis_active"]),
                )
            continue

        if event.get("type") != "message":
            continue

        message = event.get("message", {})
        if message.get("type") != "text":
            continue

        reply_token = event.get("replyToken")
        text = message.get("text", "").strip()

        if not user_id or not reply_token:
            continue

        user = ensure_user(user_id)
        touch_user(user_id)
        user = get_user(user_id)

        # 管理員指令
        if user_id in ADMIN_USER_IDS and text == "/待開通":
            pending = list_pending_accounts()
            if not pending:
                reply_message(reply_token, "目前沒有待開通名單。", quick_items=base_quick_reply(True, user["analysis_active"]))
            else:
                rows = [f"{i}. {row['game_account']}" for i, row in enumerate(pending[:20], start=1)]
                reply_message(
                    reply_token,
                    "待開通名單：\n" + "\n".join(rows),
                    quick_items=base_quick_reply(True, user["analysis_active"]),
                )
            continue

        if user_id in ADMIN_USER_IDS and text.startswith("/vip "):
            parts = text.split()
            if len(parts) != 3:
                reply_message(reply_token, "格式錯誤，請用：/vip 遊戲帳號 天數", quick_items=base_quick_reply(True, user["analysis_active"]))
                continue

            game_account = parts[1]
            try:
                days = int(parts[2])
            except ValueError:
                reply_message(reply_token, "天數請輸入數字，例如：/vip ck76888 30", quick_items=base_quick_reply(True, user["analysis_active"]))
                continue

            updated_user, err = grant_vip_by_game_account(game_account, days)
            if err:
                reply_message(reply_token, err, quick_items=base_quick_reply(True, user["analysis_active"]))
            else:
                reply_message(
                    reply_token,
                    f"已開通VIP\n\n帳號：{game_account}\n天數：{days}天\n到期：{updated_user['vip_expire_at']}",
                    quick_items=base_quick_reply(True, user["analysis_active"]),
                )
                push_message(
                    updated_user["line_user_id"],
                    f"你的VIP已開通\n\n到期時間：{updated_user['vip_expire_at']}\n\n現在可使用完整分析功能。",
                )
            continue

        if user_id in ADMIN_USER_IDS and text.startswith("/unvip "):
            parts = text.split()
            if len(parts) != 2:
                reply_message(reply_token, "格式錯誤，請用：/unvip 遊戲帳號", quick_items=base_quick_reply(True, user["analysis_active"]))
                continue

            ok = revoke_vip_by_game_account(parts[1])
            reply_message(
                reply_token,
                f"已取消VIP：{parts[1]}" if ok else "找不到這個遊戲帳號。",
                quick_items=base_quick_reply(True, user["analysis_active"]),
            )
            continue

        # 綁定流程
        if user.get("pending_flow") == "bind_game_account":
            ok = set_game_account(user_id, text)
            if ok:
                reply_message(
                    reply_token,
                    f"已收到你的遊戲帳號：{text}\n\n請等待管理員確認開通VIP。",
                    quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, user["analysis_active"]),
                )
            else:
                reply_message(
                    reply_token,
                    "這個遊戲帳號可能已被綁定，請換一個或聯絡管理員。",
                    quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, user["analysis_active"]),
                )
            continue

        # 試用 / VIP 鎖定
        if not is_vip(user) and not in_trial(user):
            if text in ["分析", "牌路", "匯入牌路", "開始分析", "莊", "閒", "和"]:
                reply_message(
                    reply_token,
                    get_status_text(user),
                    quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, user["analysis_active"]),
                )
                continue

        # 一般功能
        if text == "開始":
            reply_message(
                reply_token,
                menu_text(user),
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, user["analysis_active"]),
            )
            continue

        if text == "綁定帳號":
            update_user_fields(user_id, pending_flow="bind_game_account")
            reply_message(
                reply_token,
                "請輸入你的遊戲帳號\n例如：ck76888",
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, user["analysis_active"]),
            )
            continue

        if text == "查詢資格":
            reply_message(
                reply_token,
                get_status_text(user),
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, user["analysis_active"]),
            )
            continue

        if text == "匯入牌路":
            update_user_fields(user_id, pending_flow="import_road")
            reply_message(
                reply_token,
                "請一次輸入目前牌路\n格式例如：\n莊莊莊閒莊閒莊閒莊莊閒閒莊閒莊\n\n至少15把才可啟動分析",
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, False),
            )
            continue

        if user.get("pending_flow") == "import_road":
            parsed = normalize_input_road(text)
            if not parsed:
                reply_message(
                    reply_token,
                    "格式錯誤，請只輸入：莊 / 閒 / 和\n例如：莊莊莊閒莊閒",
                    quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, False),
                )
                continue

            if len(parsed) < MIN_IMPORT_HANDS:
                reply_message(
                    reply_token,
                    f"目前只有 {len(parsed)} 把，至少要 {MIN_IMPORT_HANDS} 把才能開始分析。",
                    quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, False),
                )
                continue

            update_user_fields(
                user_id,
                current_road=parsed[-MAX_ROAD:],
                pending_flow=None,
                imported_ready=True,
                analysis_active=False,
            )
            imported_user = get_user(user_id)
            reply_message(
                reply_token,
                "牌路匯入完成\n\n"
                f"目前牌路：\n{road_text(imported_user['current_road'])}\n\n"
                "接下來請輸入：開始分析",
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, False),
            )
            continue

        if text == "開始分析":
            if not user["imported_ready"]:
                reply_message(
                    reply_token,
                    f"請先匯入至少 {MIN_IMPORT_HANDS} 把牌路，再開始分析。",
                    quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, False),
                )
                continue

            user = update_user_fields(user_id, analysis_active=True)
            create_analysis_log(user_id, user["current_road"] or [])
            card = probability_card(user["current_road"] or [])
            if is_vip(user):
                card += "\n\n" + hit_rate_summary(user_id)

            reply_message(
                reply_token,
                "分析已啟動\n\n"
                f"{card}\n\n"
                "之後每開一口，直接按 莊 / 閒 / 和",
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, True),
            )
            continue

        if text == "結束分析":
            user = update_user_fields(
                user_id,
                analysis_active=False,
                imported_ready=False,
                current_road=[],
            )
            reply_message(
                reply_token,
                "已結束本輪分析，牌路已清空。\n如要再次使用，請先重新匯入牌路。",
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, False),
            )
            continue

        if text in ["莊", "閒", "和"]:
            if not user["analysis_active"]:
                reply_message(
                    reply_token,
                    "請先完成：\n1. 匯入牌路\n2. 開始分析\n\n之後再逐口輸入 莊 / 閒 / 和",
                    quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, False),
                )
                continue

            backfill_previous_actual(user_id, text)
            road = user["current_road"] or []
            road.append(text)
            road = road[-MAX_ROAD:]
            user = update_user_fields(user_id, current_road=road)
            create_analysis_log(user_id, user["current_road"] or [])

            card = probability_card(user["current_road"] or [])
            latest_user = get_user(user_id)
            if is_vip(latest_user):
                card += "\n\n" + hit_rate_summary(user_id)

            reply_message(
                reply_token,
                f"已記錄：{text}\n\n{card}",
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, True),
            )
            continue

        if text == "牌路":
            limit = 20 if is_vip(user) else 8
            reply_message(
                reply_token,
                f"目前牌路：\n{road_text(user['current_road'] or [], limit)}",
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, user["analysis_active"]),
            )
            continue

        if text == "分析":
            card = probability_card(user["current_road"] or [])
            if is_vip(user):
                card += "\n\n" + hit_rate_summary(user_id)

            reply_message(
                reply_token,
                card,
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, user["analysis_active"]),
            )
            continue

        if text == "重設":
            user = update_user_fields(
                user_id,
                current_road=[],
                imported_ready=False,
                analysis_active=False,
            )
            reply_message(
                reply_token,
                "已重設當前牌路與分析狀態。",
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, False),
            )
            continue

        reply_message(
            reply_token,
            f"你剛剛說：{text}\n\n可用功能：開始 / 匯入牌路 / 開始分析 / 牌路 / 分析 / 綁定帳號 / 查詢資格 / 結束分析",
            quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, user["analysis_active"]),
        )

    return "OK", 200


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
else:
    init_db()
