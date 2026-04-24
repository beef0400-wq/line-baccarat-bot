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
            ("會員說明", "會員說明"),
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
            cur.execute(
                "SELECT * FROM users WHERE line_user_id = %s",
                (line_user_id,),
            )
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
                        SET current_road = %s::jsonb,
                            analysis_active = %s,
                            imported_ready = %s,
                            updated_at = %s
                        WHERE line_user_id = %s
                        RETURNING *
                        """,
                        (
                            json.dumps([], ensure_ascii=False),
                            False,
                            False,
                            now_tw(),
                            line_user_id,
                        ),
                    )
                    user = cur.fetchone()
                conn.commit()
        return user

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO users (
                    line_user_id,
                    trial_end_at,
                    current_road,
                    pending_flow,
                    analysis_active,
                    imported_ready,
                    last_active_at,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    line_user_id,
                    now_tw() + timedelta(hours=3),
                    json.dumps([], ensure_ascii=False),
                    None,
                    False,
                    False,
                    now_tw(),
                    now_tw(),
                    now_tw(),
                ),
            )
            user = cur.fetchone()
        conn.commit()
        return user


def update_user_fields(line_user_id: str, **fields):
    current = get_user(line_user_id)
    if not current:
        ensure_user(line_user_id)
        current = get_user(line_user_id)

    game_account = fields.get("game_account", current.get("game_account"))
    vip_expire_at = fields.get("vip_expire_at", current.get("vip_expire_at"))
    trial_end_at = fields.get("trial_end_at", current.get("trial_end_at"))
    current_road = fields.get("current_road", current.get("current_road") or [])
    pending_flow = fields.get("pending_flow", current.get("pending_flow"))
    analysis_active = fields.get("analysis_active", current.get("analysis_active", False))
    imported_ready = fields.get("imported_ready", current.get("imported_ready", False))
    last_active_at = fields.get("last_active_at", current.get("last_active_at"))

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE users
                SET game_account = %s,
                    vip_expire_at = %s,
                    trial_end_at = %s,
                    current_road = %s::jsonb,
                    pending_flow = %s,
                    analysis_active = %s,
                    imported_ready = %s,
                    last_active_at = %s,
                    updated_at = %s
                WHERE line_user_id = %s
                RETURNING *
                """,
                (
                    game_account,
                    vip_expire_at,
                    trial_end_at,
                    json.dumps(current_road, ensure_ascii=False),
                    pending_flow,
                    analysis_active,
                    imported_ready,
                    last_active_at,
                    now_tw(),
                    line_user_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def touch_user(line_user_id: str):
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
            row = cur.fetchone()
        conn.commit()
        return row


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
    raw = raw.strip().replace(" ", "").replace("
", "").replace("
", "")
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


def longest_run(seq):
    return max((c for _, c in segment_lengths(seq)), default=0)


def avg_segment_length(seq):
    segs = segment_lengths(seq)
    if not segs:
        return 0
    return round(len(seq) / len(segs), 2)


def transition_rate(seq):
    if len(seq) <= 1:
        return 0
    return round(alternation_count(seq) * 100 / (len(seq) - 1))


# =========================
# V3.5 Prediction Engine
# =========================
def sequence_match_model(seq, max_window=6):
    details = []
    bonus_b = 0.0
    bonus_p = 0.0
    total_matches = 0
    best_line = "無明顯相似序列"

    if len(seq) < 8:
        return bonus_b, bonus_p, total_matches, best_line, details

    for window in range(min(max_window, len(seq) - 1), 2, -1):
        pattern = seq[-window:]
        b_next = 0
        p_next = 0
        matches = 0

        for i in range(0, len(seq) - window):
            past = seq[i:i + window]
            if past == pattern and i + window < len(seq):
                nxt = seq[i + window]
                if nxt == "莊":
                    b_next += 1
                    matches += 1
                elif nxt == "閒":
                    p_next += 1
                    matches += 1

        if matches > 0:
            weight = {6: 20, 5: 16, 4: 12, 3: 8}.get(window, 6)
            b_ratio = b_next / matches
            p_ratio = p_next / matches
            bonus_b += weight * b_ratio
            bonus_p += weight * p_ratio
            total_matches += matches
            details.append(f"{window}碼匹配「{''.join(pattern)}」：莊{b_next} / 閒{p_next}")
            if best_line == "無明顯相似序列":
                best_line = f"尾段「{''.join(pattern)}」曾出現 {matches} 次"

    return bonus_b, bonus_p, total_matches, best_line, details[:3]


def tail_momentum_model(seq):
    bonus_b = 0.0
    bonus_p = 0.0
    label = "尾段資料不足"

    if len(seq) < 3:
        return bonus_b, bonus_p, label

    tail_count, tail_side = count_tail_same(seq)

    if tail_count >= 5:
        if tail_side == "莊":
            bonus_b += 9
            bonus_p += 5
        else:
            bonus_p += 9
            bonus_b += 5
        label = f"尾段{tail_count}連{tail_side}，延續與轉折同時升高"
    elif tail_count >= 3:
        if tail_side == "莊":
            bonus_b += 14
        else:
            bonus_p += 14
        label = f"尾段{tail_count}連{tail_side}，延續動能偏強"
    elif tail_count == 2:
        if tail_side == "莊":
            bonus_b += 7
        else:
            bonus_p += 7
        label = f"尾段2連{tail_side}，連續正在建立"
    else:
        last5 = seq[-5:] if len(seq) >= 5 else seq
        alt = alternation_count(last5)
        if len(last5) >= 4 and alt >= len(last5) - 1:
            next_side = "閒" if seq[-1] == "莊" else "莊"
            if next_side == "莊":
                bonus_b += 12
            else:
                bonus_p += 12
            label = "尾段交錯明顯，單跳動能偏強"
        else:
            label = "尾段未形成強動能"

    return bonus_b, bonus_p, label


def structure_model(seq):
    bonus_b = 0.0
    bonus_p = 0.0
    label = "混合結構"

    if len(seq) < 4:
        return bonus_b, bonus_p, label

    segs = segment_lengths(seq)
    tail_count, tail_side = count_tail_same(seq)

    # 單跳
    last5 = seq[-5:] if len(seq) >= 5 else seq
    if len(last5) >= 4 and alternation_count(last5) >= len(last5) - 1:
        next_side = "閒" if seq[-1] == "莊" else "莊"
        if next_side == "莊":
            bonus_b += 12
        else:
            bonus_p += 12
        label = "單跳型態"
        return bonus_b, bonus_p, label

    # 單跳破壞
    if len(seq) >= 6:
        last6 = seq[-6:]
        if (
            last6[0] != last6[1]
            and last6[1] != last6[2]
            and last6[2] != last6[3]
            and last6[3] != last6[4]
            and last6[4] == last6[5]
        ):
            rebound = "閒" if last6[-1] == "莊" else "莊"
            if rebound == "莊":
                bonus_b += 14
            else:
                bonus_p += 14
            label = "單跳破壞後回補觀察"
            return bonus_b, bonus_p, label

    # 雙跳
    if len(segs) >= 4:
        tail = segs[-4:]
        if all(c == 2 for _, c in tail[:-1]) and tail[-1][1] in [1, 2]:
            expected_side = tail[-1][0] if tail[-1][1] == 1 else ("閒" if tail[-1][0] == "莊" else "莊")
            if expected_side == "莊":
                bonus_b += 13
            else:
                bonus_p += 13
            label = "雙跳型態"
            return bonus_b, bonus_p, label

    # 長連續
    if tail_count >= 5:
        if tail_side == "莊":
            bonus_b += 10
            bonus_p += 5
        else:
            bonus_p += 10
            bonus_b += 5
        label = "長連續型態"
        return bonus_b, bonus_p, label

    # 齊頭
    if len(segs) >= 2 and segs[-1][1] == segs[-2][1] and segs[-1][0] != segs[-2][0]:
        next_side = segs[-2][0]
        if next_side == "莊":
            bonus_b += 13
        else:
            bonus_p += 13
        label = "齊頭型態"
        return bonus_b, bonus_p, label

    # 段落偏向
    if tail_count >= 2:
        if tail_side == "莊":
            bonus_b += 5
        else:
            bonus_p += 5
        label = "短連續建立"

    return bonus_b, bonus_p, label


def recency_weight_model(seq):
    bonus_b = 0.0
    bonus_p = 0.0
    last = seq[-8:] if len(seq) >= 8 else seq
    weights = list(range(1, len(last) + 1))
    for x, w in zip(last, weights):
        if x == "莊":
            bonus_b += w * 0.7
        elif x == "閒":
            bonus_p += w * 0.7
    label = "近期權重已套用"
    return bonus_b, bonus_p, label


def risk_and_signal(banker_pct, player_pct, seq, total_matches, structure_label):
    gap = abs(banker_pct - player_pct)
    t_rate = transition_rate(seq)
    avg_len = avg_segment_length(seq)

    if gap >= 22 and total_matches >= 2:
        signal = "強"
    elif gap >= 14 or total_matches >= 1:
        signal = "中強"
    elif gap >= 8:
        signal = "中"
    else:
        signal = "弱"

    if len(seq) < 15:
        risk = "中高"
    elif "混合" in structure_label and gap < 10:
        risk = "高"
    elif t_rate > 75 or avg_len < 1.35:
        risk = "中高"
    elif gap >= 18 and signal in ["強", "中強"]:
        risk = "中低"
    else:
        risk = "中"

    return signal, risk


def prediction_v35(road):
    seq = filter_main_road(road)[-30:]
    if not seq:
        return {
            "banker_pct": 50,
            "player_pct": 50,
            "pattern": "資料不足",
            "risk": "高",
            "signal": "弱",
            "tail_note": "尚無尾段資料",
            "match_note": "尚無匹配資料",
            "structure_note": "尚無結構資料",
            "match_details": [],
            "metrics": {
                "莊": 0,
                "閒": 0,
                "交錯率": 0,
                "平均段長": 0,
                "最長連續": 0,
            },
        }

    # 基礎比例，避免固定 50，低權重但保留背景
    banker = seq.count("莊")
    player = seq.count("閒")
    score_b = 40 + banker * 2.8
    score_p = 40 + player * 2.8

    rb, rp, recency_note = recency_weight_model(seq)
    score_b += rb
    score_p += rp

    tb, tp, tail_note = tail_momentum_model(seq)
    score_b += tb
    score_p += tp

    sb, sp, structure_note = structure_model(seq)
    score_b += sb
    score_p += sp

    mb, mp, total_matches, match_note, match_details = sequence_match_model(seq, max_window=6)
    score_b += mb
    score_p += mp

    # 波動微調：細微變動也會影響百分比
    t_rate = transition_rate(seq)
    avg_len = avg_segment_length(seq)
    if t_rate >= 65:
        # 高交錯時，尾端反向微增
        next_side = "閒" if seq[-1] == "莊" else "莊"
        if next_side == "莊":
            score_b += 4
        else:
            score_p += 4
    if avg_len >= 2.2:
        tail_count, tail_side = count_tail_same(seq)
        if tail_side == "莊":
            score_b += 3
        elif tail_side == "閒":
            score_p += 3

    total = max(score_b + score_p, 1)
    banker_pct = round(score_b * 100 / total)
    player_pct = 100 - banker_pct

    signal, risk = risk_and_signal(banker_pct, player_pct, seq, total_matches, structure_note)

    return {
        "banker_pct": banker_pct,
        "player_pct": player_pct,
        "pattern": structure_note,
        "risk": risk,
        "signal": signal,
        "tail_note": tail_note,
        "match_note": match_note,
        "structure_note": structure_note,
        "match_details": match_details,
        "metrics": {
            "莊": banker,
            "閒": player,
            "交錯率": t_rate,
            "平均段長": avg_len,
            "最長連續": longest_run(seq),
        },
    }


def prediction_card(road):
    seq = filter_main_road(road)[-30:]
    data = prediction_v35(road)
    detail_lines = "
".join([f"・{x}" for x in data["match_details"]]) if data["match_details"] else "・目前無足夠重複樣本"

    return (
        "📊 預測判讀 V3.5

"
        f"目前牌路：
{road_text(seq[-20:])}

"
        "━━━━━━━━━━━━━━━

"
        "預測機率：
"
        f"👉 莊：{data['banker_pct']}%
"
        f"👉 閒：{data['player_pct']}%

"
        f"信號強度：{data['signal']}

"
        "━━━━━━━━━━━━━━━

"
        "判斷依據：

"
        "▍序列匹配
"
        f"{data['match_note']}
"
        f"{detail_lines}

"
        "▍尾段動能
"
        f"{data['tail_note']}

"
        "▍結構判斷
"
        f"{data['structure_note']}

"
        "━━━━━━━━━━━━━━━

"
        "數據指標：
"
        f"莊：{data['metrics']['莊']}
"
        f"閒：{data['metrics']['閒']}
"
        f"交錯率：{data['metrics']['交錯率']}%
"
        f"平均段長：{data['metrics']['平均段長']}
"
        f"最長連續：{data['metrics']['最長連續']}

"
        "風險：
"
        f"{data['risk']}"
    )


# =========================
# Backtest logs
# =========================
def create_analysis_log(line_user_id: str, road):
    data = prediction_v35(road)
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
                    json.dumps(filter_main_road(road)[-30:], ensure_ascii=False),
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
            "目前狀態：VIP

"
            f"到期時間：{user['vip_expire_at']}
"
            f"剩餘：約 {days} 天"
        )

    if in_trial(user):
        mins = minutes_left(user["trial_end_at"])
        return (
            "目前狀態：免費試用中

"
            f"剩餘時間：約 {mins} 分鐘
"
            "試用結束後，完整分析需開通VIP。"
        )

    return (
        "試用已結束

"
        "VIP開通：
"
        "👉 註冊3A帳號 / 已有3A帳號
"
        "👉 聯絡管理員"
    )


def member_guide_text():
    return (
        "【使用教學】

"
        "本系統為「牌路紀錄＋即時分析」模式
"
        "請依照以下流程操作：

"
        "━━━━━━━━━━━━━━━

"
        "① 綁定帳號
"
        "點選【綁定帳號】
"
        "輸入你的遊戲帳號（例：ck76888）

"
        "━━━━━━━━━━━━━━━

"
        "② 匯入牌路
"
        "輸入目前牌路
"
        "（需至少15把以上）

"
        "例：
"
        "莊莊莊閒莊閒閒莊…

"
        "━━━━━━━━━━━━━━━

"
        "③ 開始分析
"
        "點選【開始分析】
"
        "系統會進入即時模式

"
        "━━━━━━━━━━━━━━━

"
        "④ 即時紀錄
"
        "每開一把輸入：

"
        "👉 莊
"
        "👉 閒
"
        "👉 和

"
        "系統會同步更新分析

"
        "━━━━━━━━━━━━━━━

"
        "⑤ 結束分析
"
        "輸入【結束分析】
"
        "結算本局紀錄

"
        "━━━━━━━━━━━━━━━

"
        "⚠️ 未開通VIP將無法使用完整分析功能
"
        "請先綁定帳號並聯絡管理員開通

"
        "━━━━━━━━━━━━━━━
"
        "【開通教學】

"
        "Step1
"
        "請由以下入口完成註冊：
"
        "sn043.aaawin88.com

"
        "👉已有帳號者請跳至 Step 5

"
        "Step 2
"
        "請先點選下方【綁定帳號】

"
        "Step 3
"
        "輸入你的遊戲帳號
"
        "例如：ck76888

"
        "Step 4
"
        "送出後，系統會將你的資料列入待開通名單

"
        "Step 5
"
        "聯絡管理員確認開通狀態

"
        "開通完成後，即可使用完整分析功能。

"
        "※ 實際資格、發放方式及相關規範，請以平台公告為準。"
    )


def menu_text(user):
    tag = "VIP會員" if is_vip(user) else ("免費試用中" if in_trial(user) else "免費版")
    return (
        f"歡迎使用百家即時分析助手（{tag}）

"
        "可用流程：
"
        "1. 匯入牌路
"
        "2. 開始分析
"
        "3. 分析中逐口按 莊 / 閒 / 和
"
        "4. 結束分析

"
        "常用功能：
"
        "牌路
"
        "分析
"
        "會員說明
"
        "綁定帳號
"
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
                    "待開通名單：
" + "
".join(rows),
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
                    f"已開通VIP

帳號：{game_account}
天數：{days}天
到期：{updated_user['vip_expire_at']}",
                    quick_items=base_quick_reply(True, user["analysis_active"]),
                )
                push_message(
                    updated_user["line_user_id"],
                    f"你的VIP已開通

到期時間：{updated_user['vip_expire_at']}

現在可使用完整分析功能。",
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
                    f"已收到你的遊戲帳號：{text}

請等待管理員確認開通VIP。",
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

        if text in ["會員說明", "使用教學", "開通教學"]:
            reply_message(
                reply_token,
                member_guide_text(),
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, user["analysis_active"]),
            )
            continue

        if text == "綁定帳號":
            update_user_fields(user_id, pending_flow="bind_game_account")
            reply_message(
                reply_token,
                "請輸入你的遊戲帳號
例如：ck76888",
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
                "請一次輸入目前牌路
格式例如：
莊莊莊閒莊閒莊閒莊莊閒閒莊閒莊

至少15把才可啟動分析",
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, False),
            )
            continue

        if user.get("pending_flow") == "import_road":
            parsed = normalize_input_road(text)
            if not parsed:
                reply_message(
                    reply_token,
                    "格式錯誤，請只輸入：莊 / 閒 / 和
例如：莊莊莊閒莊閒",
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
                "牌路匯入完成

"
                f"目前牌路：
{road_text(imported_user['current_road'])}

"
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
            card = prediction_card(user["current_road"] or [])
            if is_vip(user):
                card += "

" + hit_rate_summary(user_id)

            reply_message(
                reply_token,
                "分析已啟動

"
                f"{card}

"
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
                "已結束本輪分析，牌路已清空。
如要再次使用，請先重新匯入牌路。",
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, False),
            )
            continue

        if text in ["莊", "閒", "和"]:
            if not user["analysis_active"]:
                reply_message(
                    reply_token,
                    "請先完成：
1. 匯入牌路
2. 開始分析

之後再逐口輸入 莊 / 閒 / 和",
                    quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, False),
                )
                continue

            backfill_previous_actual(user_id, text)
            road = user["current_road"] or []
            road.append(text)
            road = road[-MAX_ROAD:]
            user = update_user_fields(user_id, current_road=road)
            create_analysis_log(user_id, user["current_road"] or [])

            card = prediction_card(user["current_road"] or [])
            latest_user = get_user(user_id)
            if is_vip(latest_user):
                card += "

" + hit_rate_summary(user_id)

            reply_message(
                reply_token,
                f"已記錄：{text}

{card}",
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, True),
            )
            continue

        if text == "牌路":
            limit = 20 if is_vip(user) else 8
            reply_message(
                reply_token,
                f"目前牌路：
{road_text(user['current_road'] or [], limit)}",
                quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, user["analysis_active"]),
            )
            continue

        if text == "分析":
            card = prediction_card(user["current_road"] or [])
            if is_vip(user):
                card += "

" + hit_rate_summary(user_id)

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
            f"你剛剛說：{text}

可用功能：開始 / 會員說明 / 匯入牌路 / 開始分析 / 牌路 / 分析 / 綁定帳號 / 查詢資格 / 結束分析",
            quick_items=base_quick_reply(user_id in ADMIN_USER_IDS, user["analysis_active"]),
        )

    return "OK", 200


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
else:
    init_db()
