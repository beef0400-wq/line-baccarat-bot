
from flask import Flask, request, abort
import os
import json
import hmac
import hashlib
import base64
from datetime import datetime, timedelta, timezone

import requests
import psycopg2
import psycopg2.extras


app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_USER_IDS = set(x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip())

TZ_TW = timezone(timedelta(hours=8))
TIMEOUT_MINUTES = 20
MAX_ROAD = 100
MIN_IMPORT_HANDS = 15


POINT_CONFIG = {
    "1000以下": {
        "label": "1000點以下",
        "保守": (50, 80),
        "標準": (100, 150),
        "積極": (150, 250),
        "極限": (250, 400),
    },
    "1000-3000": {
        "label": "1000～3000點",
        "保守": (100, 200),
        "標準": (250, 400),
        "積極": (400, 700),
        "極限": (700, 1200),
    },
    "3000-5000": {
        "label": "3000～5000點",
        "保守": (200, 400),
        "標準": (500, 800),
        "積極": (800, 1300),
        "極限": (1300, 2000),
    },
    "5000-10000": {
        "label": "5000～10000點",
        "保守": (400, 800),
        "標準": (900, 1500),
        "積極": (1500, 2500),
        "極限": (2500, 4000),
    },
    "10000-30000": {
        "label": "10000～30000點",
        "保守": (800, 1200),
        "標準": (1500, 3000),
        "積極": (3000, 5000),
        "極限": (5000, 8000),
    },
    "30000以上": {
        "label": "30000點以上",
        "保守": (1500, 3000),
        "標準": (4000, 7000),
        "積極": (7000, 12000),
        "極限": (12000, 20000),
    },
}

TARGET_MULTIPLIER = {
    "30%": 0.8,
    "50%": 1.2,
    "100%": 2.0,
}

POINT_RANGE_INPUT_MAP = {
    "1000點以下": "1000以下",
    "1000以下": "1000以下",
    "1000～3000點": "1000-3000",
    "1000-3000": "1000-3000",
    "3000～5000點": "3000-5000",
    "3000-5000": "3000-5000",
    "5000～10000點": "5000-10000",
    "5000-10000": "5000-10000",
    "10000～30000點": "10000-30000",
    "10000-30000": "10000-30000",
    "30000點以上": "30000以上",
    "30000以上": "30000以上",
}

PLAY_MODE_INPUTS = ["保守", "標準", "積極", "極限"]
TARGET_INPUTS = ["30%", "50%", "100%"]

# =========================
# Basic helpers
# =========================
def now_tw():
    return datetime.now(TZ_TW).replace(tzinfo=None)


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")
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
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS point_range TEXT NULL;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS play_mode TEXT NULL;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS target_profit TEXT NULL;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS round_win INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS round_loss INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS win_streak INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS loss_streak INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS max_win_streak INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS max_loss_streak INTEGER NOT NULL DEFAULT 0;",
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


def verify_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        return False
    digest = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def line_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }


def qr(items):
    return [
        {
            "type": "action",
            "action": {"type": "message", "label": label, "text": text},
        }
        for label, text in items
    ]


def qr_main(is_admin=False):
    items = [
        ("開始", "開始"),
        ("點數配置", "點數配置"),
        ("綁定帳號", "綁定帳號"),
        ("匯入牌路", "匯入牌路"),
        ("開始分析", "開始分析"),
        ("使用教學", "會員說明"),
    ]
    if is_admin:
        items[-1] = ("/待開通", "/待開通")
    return qr(items)


def qr_point_ranges():
    return qr([
        ("1000以下", "1000點以下"),
        ("1000-3000", "1000～3000點"),
        ("3000-5000", "3000～5000點"),
        ("5000-10000", "5000～10000點"),
        ("10000-30000", "10000～30000點"),
        ("30000以上", "30000點以上"),
        ("返回", "開始"),
    ])

def qr_modes():
    return qr([
        ("保守", "保守"),
        ("標準", "標準"),
        ("積極", "積極"),
        ("極限", "極限"),
        ("返回", "點數配置"),
    ])

def qr_targets():
    return qr([
        ("30%", "30%"),
        ("50%", "50%"),
        ("100%", "100%"),
        ("返回", "點數配置"),
    ])

def qr_after_config():
    return qr([
        ("匯入牌路", "匯入牌路"),
        ("開始分析", "開始分析"),
        ("詳細配置", "查詢配置"),
        ("主選單", "開始"),
    ])


def qr_imported():
    return qr([
        ("開始分析", "開始分析"),
        ("重新匯入", "匯入牌路"),
        ("主選單", "開始"),
    ])


def qr_analysis():
    return qr([
        ("莊", "莊"),
        ("閒", "閒"),
        ("和", "和"),
        ("詳細分析", "詳細分析"),
        ("結束分析", "結束分析"),
    ])


def qr_after_end():
    return qr([
        ("點數配置", "點數配置"),
        ("匯入牌路", "匯入牌路"),
        ("開始", "開始"),
    ])


def reply_message(reply_token, text, quick_items=None):
    msg = {"type": "text", "text": text}
    if quick_items:
        msg["quickReply"] = {"items": quick_items}
    payload = {"replyToken": reply_token, "messages": [msg]}
    r = requests.post(
        "https://api.line.me/v2/bot/message/reply",
        headers=line_headers(),
        data=json.dumps(payload, ensure_ascii=False),
        timeout=15,
    )
    print("REPLY STATUS:", r.status_code)
    print("REPLY BODY:", r.text)


def push_message(user_id, text):
    payload = {"to": user_id, "messages": [{"type": "text", "text": text}]}
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers=line_headers(),
        data=json.dumps(payload, ensure_ascii=False),
        timeout=15,
    )
    print("PUSH STATUS:", r.status_code)
    print("PUSH BODY:", r.text)


# =========================
# User state
# =========================
def get_user(line_user_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE line_user_id = %s", (line_user_id,))
            return cur.fetchone()


def ensure_user(line_user_id):
    user = get_user(line_user_id)
    if user:
        last_active = user.get("last_active_at")
        if last_active and now_tw() - last_active > timedelta(minutes=TIMEOUT_MINUTES):
            return update_user_fields(
                line_user_id,
                current_road=[],
                analysis_active=False,
                imported_ready=False,
                pending_flow=None,
            )
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
                VALUES (%s, %s, %s::jsonb, NULL, FALSE, FALSE, %s, %s, %s)
                RETURNING *
                """,
                (
                    line_user_id,
                    now_tw() + timedelta(hours=3),
                    json.dumps([], ensure_ascii=False),
                    now_tw(),
                    now_tw(),
                    now_tw(),
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def update_user_fields(line_user_id, **fields):
    current = get_user(line_user_id)
    if not current:
        ensure_user(line_user_id)
        current = get_user(line_user_id)

    current_road = fields.get("current_road", current.get("current_road") or [])

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
                    point_range = %s,
                    play_mode = %s,
                    target_profit = %s,
                    round_win = %s,
                    round_loss = %s,
                    win_streak = %s,
                    loss_streak = %s,
                    max_win_streak = %s,
                    max_loss_streak = %s,
                    updated_at = %s
                WHERE line_user_id = %s
                RETURNING *
                """,
                (
                    fields.get("game_account", current.get("game_account")),
                    fields.get("vip_expire_at", current.get("vip_expire_at")),
                    fields.get("trial_end_at", current.get("trial_end_at")),
                    json.dumps(current_road, ensure_ascii=False),
                    fields.get("pending_flow", current.get("pending_flow")),
                    fields.get("analysis_active", current.get("analysis_active", False)),
                    fields.get("imported_ready", current.get("imported_ready", False)),
                    fields.get("last_active_at", current.get("last_active_at")),
                    fields.get("point_range", current.get("point_range")),
                    fields.get("play_mode", current.get("play_mode")),
                    fields.get("target_profit", current.get("target_profit")),
                    fields.get("round_win", current.get("round_win", 0)),
                    fields.get("round_loss", current.get("round_loss", 0)),
                    fields.get("win_streak", current.get("win_streak", 0)),
                    fields.get("loss_streak", current.get("loss_streak", 0)),
                    fields.get("max_win_streak", current.get("max_win_streak", 0)),
                    fields.get("max_loss_streak", current.get("max_loss_streak", 0)),
                    now_tw(),
                    line_user_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return row


def touch_user(line_user_id):
    return update_user_fields(line_user_id, last_active_at=now_tw())


def is_vip(user):
    return bool(user and user.get("vip_expire_at") and user["vip_expire_at"] > now_tw())


def in_trial(user):
    return bool(user and user.get("trial_end_at") and user["trial_end_at"] > now_tw())


def minutes_left(dt):
    if not dt:
        return 0
    return max(int((dt - now_tw()).total_seconds() // 60), 0)


# =========================
# Admin
# =========================
def set_game_account(line_user_id, game_account):
    try:
        update_user_fields(line_user_id, game_account=game_account, pending_flow=None)
        return True
    except psycopg2.Error as exc:
        print("SET GAME ACCOUNT ERROR:", exc)
        return False


def get_user_by_game_account(game_account):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE game_account = %s", (game_account,))
            return cur.fetchone()


def grant_vip_by_game_account(game_account, days):
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


def revoke_vip_by_game_account(game_account):
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
# Road / Analysis
# =========================
def road_text(road, limit=None):
    data = road[-limit:] if limit else road
    return "".join(data) if data else "尚無資料"


def filter_main_road(road):
    return [x for x in road if x in ["莊", "閒"]]


def normalize_input_road(raw):
    raw = raw.strip().replace(" ", "").replace("\n", "").replace("\r", "")
    tokens = []
    for ch in raw:
        if ch in ["莊", "閒", "和"]:
            tokens.append(ch)
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
    return sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])


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
            bonus_b += weight * (b_next / matches)
            bonus_p += weight * (p_next / matches)
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
        if len(last5) >= 4 and alternation_count(last5) >= len(last5) - 1:
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

    last5 = seq[-5:] if len(seq) >= 5 else seq
    if len(last5) >= 4 and alternation_count(last5) >= len(last5) - 1:
        next_side = "閒" if seq[-1] == "莊" else "莊"
        if next_side == "莊":
            bonus_b += 12
        else:
            bonus_p += 12
        return bonus_b, bonus_p, "單跳型態"

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
            return bonus_b, bonus_p, "單跳破壞後回補觀察"

    if len(segs) >= 4:
        tail = segs[-4:]
        if all(c == 2 for _, c in tail[:-1]) and tail[-1][1] in [1, 2]:
            expected_side = tail[-1][0] if tail[-1][1] == 1 else ("閒" if tail[-1][0] == "莊" else "莊")
            if expected_side == "莊":
                bonus_b += 13
            else:
                bonus_p += 13
            return bonus_b, bonus_p, "雙跳型態"

    if tail_count >= 5:
        if tail_side == "莊":
            bonus_b += 10
            bonus_p += 5
        else:
            bonus_p += 10
            bonus_b += 5
        return bonus_b, bonus_p, "長連續型態"

    if len(segs) >= 2 and segs[-1][1] == segs[-2][1] and segs[-1][0] != segs[-2][0]:
        next_side = segs[-2][0]
        if next_side == "莊":
            bonus_b += 13
        else:
            bonus_p += 13
        return bonus_b, bonus_p, "齊頭型態"

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
    for x, w in zip(last, range(1, len(last) + 1)):
        if x == "莊":
            bonus_b += w * 0.7
        elif x == "閒":
            bonus_p += w * 0.7
    return bonus_b, bonus_p


def analyze_subroads(seq):
    segs = segment_lengths(seq)
    if len(segs) < 4:
        return {
            "big_eye": "不足",
            "small": "不足",
            "cockroach": "不足",
            "stability_score": 50,
            "note": "下三路資料不足",
        }

    lengths = [c for _, c in segs]
    recent = lengths[-6:]
    diffs = [abs(recent[i] - recent[i - 1]) for i in range(1, len(recent))]
    avg_diff = sum(diffs) / len(diffs) if diffs else 0
    same_count = sum(1 for d in diffs if d == 0)

    big_eye = "紅" if same_count >= 2 else "藍"
    small = "紅" if avg_diff <= 1 else "藍"
    cockroach = "紅" if max(diffs or [0]) <= 2 else "藍"

    red_count = [big_eye, small, cockroach].count("紅")
    stability_score = 30 + red_count * 20
    if red_count == 3:
        note = "三路偏紅，結構較穩"
    elif red_count == 0:
        note = "三路偏藍，結構混亂"
    else:
        note = "三路交錯，轉折觀察"

    return {
        "big_eye": big_eye,
        "small": small,
        "cockroach": cockroach,
        "stability_score": stability_score,
        "note": note,
    }


def risk_and_signal(banker_pct, player_pct, seq, total_matches, structure_label, subroad):
    gap = abs(banker_pct - player_pct)
    t_rate = transition_rate(seq)
    avg_len = avg_segment_length(seq)
    stability = subroad.get("stability_score", 50)

    if gap >= 22 and total_matches >= 2 and stability >= 70:
        signal = "強"
    elif gap >= 14 or total_matches >= 1:
        signal = "中強"
    elif gap >= 8:
        signal = "中"
    else:
        signal = "弱"

    if len(seq) < 15:
        risk = "中高"
    elif stability < 45:
        risk = "高"
    elif "混合" in structure_label and gap < 10:
        risk = "高"
    elif t_rate > 75 or avg_len < 1.35:
        risk = "中高"
    elif gap >= 18 and signal in ["強", "中強"] and stability >= 70:
        risk = "中低"
    else:
        risk = "中"

    return signal, risk


def prediction_v6(road):
    seq = filter_main_road(road)[-30:]

    if not seq:
        return {
            "banker_pct": 50,
            "player_pct": 50,
            "direction": "莊",
            "direction_pct": 50,
            "pattern": "資料不足",
            "risk": "高",
            "signal": "弱",
            "tail_note": "尚無尾段資料",
            "match_note": "尚無匹配資料",
            "structure_note": "尚無結構資料",
            "match_details": [],
            "subroad": {"big_eye": "不足", "small": "不足", "cockroach": "不足", "stability_score": 50, "note": "下三路資料不足"},
            "metrics": {"莊": 0, "閒": 0, "交錯率": 0, "平均段長": 0, "最長連續": 0},
        }

    banker = seq.count("莊")
    player = seq.count("閒")

    score_b = 40 + banker * 2.8
    score_p = 40 + player * 2.8

    rb, rp = recency_weight_model(seq)
    tb, tp, tail_note = tail_momentum_model(seq)
    sb, sp, structure_note = structure_model(seq)
    mb, mp, total_matches, match_note, match_details = sequence_match_model(seq, max_window=6)

    score_b += rb + tb + sb + mb
    score_p += rp + tp + sp + mp

    subroad = analyze_subroads(seq)
    if subroad["stability_score"] >= 70:
        if score_b >= score_p:
            score_b += 8
        else:
            score_p += 8
    elif subroad["stability_score"] <= 40:
        score_b *= 0.95
        score_p *= 0.95

    if transition_rate(seq) >= 65:
        next_side = "閒" if seq[-1] == "莊" else "莊"
        if next_side == "莊":
            score_b += 4
        else:
            score_p += 4

    if avg_segment_length(seq) >= 2.2:
        _tail_count, tail_side = count_tail_same(seq)
        if tail_side == "莊":
            score_b += 3
        elif tail_side == "閒":
            score_p += 3

    total = max(score_b + score_p, 1)
    banker_pct = round(score_b * 100 / total)
    player_pct = 100 - banker_pct

    signal, risk = risk_and_signal(banker_pct, player_pct, seq, total_matches, structure_note, subroad)

    if banker_pct >= player_pct:
        direction = "莊"
        direction_pct = banker_pct
    else:
        direction = "閒"
        direction_pct = player_pct

    return {
        "banker_pct": banker_pct,
        "player_pct": player_pct,
        "direction": direction,
        "direction_pct": direction_pct,
        "pattern": structure_note,
        "risk": risk,
        "signal": signal,
        "tail_note": tail_note,
        "match_note": match_note,
        "structure_note": structure_note,
        "match_details": match_details,
        "subroad": subroad,
        "metrics": {
            "莊": banker,
            "閒": player,
            "交錯率": transition_rate(seq),
            "平均段長": avg_segment_length(seq),
            "最長連續": longest_run(seq),
        },
    }


# =========================
# Points / decision V9
# =========================
def point_range_text(user):
    key = user.get("point_range")
    if not key or key not in POINT_CONFIG:
        return "尚未設定"
    return POINT_CONFIG[key]["label"]


def get_base_point_unit(user):
    key = user.get("point_range")
    mode = user.get("play_mode") or "保守"
    if key not in POINT_CONFIG:
        return None
    return POINT_CONFIG[key].get(mode, POINT_CONFIG[key]["保守"])


def apply_target_multiplier(low, high, target_profit):
    multiplier = TARGET_MULTIPLIER.get(target_profit or "30%", 1.0)
    return int(low * multiplier), int(high * multiplier), multiplier


def get_system_state(data, user):
    win = user.get("win_streak") or 0
    loss = user.get("loss_streak") or 0
    risk = data.get("risk", "中")
    stability = data.get("subroad", {}).get("stability_score", 50)
    gap = abs(data.get("banker_pct", 50) - data.get("player_pct", 50))
    signal = data.get("signal", "弱")

    if loss >= 3 or risk == "高":
        return "⛔ 暫停"
    if loss == 2:
        return "⚠️ 降速"
    if win >= 3 and stability >= 60 and signal in ["中強", "強"]:
        return "🔥 強攻中"
    if win >= 2 and stability >= 55:
        return "🔥 進攻中"
    if gap >= 14 and stability >= 45 and signal in ["中", "中強", "強"]:
        return "✅ 可啟動"
    if stability >= 45:
        return "👀 觀察中"
    return "⛔ 暫停"


def recommend_mode(data, user):
    stability = data.get("subroad", {}).get("stability_score", 50)
    signal = data.get("signal", "弱")
    risk = data.get("risk", "中")
    target = user.get("target_profit") or "30%"
    win = user.get("win_streak") or 0
    loss = user.get("loss_streak") or 0

    if risk == "高" or loss >= 2:
        return "保守"
    if target == "100%" and stability >= 70 and signal in ["中強", "強"] and win >= 2:
        return "極限"
    if target in ["50%", "100%"] and stability >= 60 and signal in ["中強", "強"]:
        return "積極"
    if stability >= 45:
        return "標準"
    return "保守"


def apply_state_to_points(low, high, state, mode):
    if "暫停" in state:
        return 0, 0, "⛔ 暫停啟動"
    if "降速" in state:
        return int(low * 0.5), int(high * 0.5), "⚠️ 降速50%"
    if "強攻" in state:
        if mode == "極限":
            return int(low * 2.0), int(high * 2.0), "🔥 極限強攻"
        return int(low * 1.7), int(high * 1.7), "🔥 強攻放大"
    if "進攻" in state:
        if mode == "極限":
            return int(low * 1.6), int(high * 1.6), "🔥 極限進攻"
        return int(low * 1.4), int(high * 1.4), "🔥 進攻中"
    if "可啟動" in state:
        return low, high, "✅ 正常啟動"
    return int(low * 0.7), int(high * 0.7), "👀 觀察低區間"


def get_final_points(user, data=None):
    base = get_base_point_unit(user)
    if not base:
        return None

    base_low, base_high = base
    target = user.get("target_profit") or "30%"
    target_low, target_high, multiplier = apply_target_multiplier(base_low, base_high, target)

    state = get_system_state(data, user) if data else "✅ 可啟動"
    mode = user.get("play_mode") or "保守"
    final_low, final_high, point_state = apply_state_to_points(target_low, target_high, state, mode)

    return {
        "base": base,
        "target_low": target_low,
        "target_high": target_high,
        "final_low": final_low,
        "final_high": final_high,
        "state": state,
        "point_state": point_state,
        "multiplier": multiplier,
        "target": target,
        "recommended_mode": recommend_mode(data, user) if data else mode,
    }


def point_config_card(user):
    key = user.get("point_range")
    mode = user.get("play_mode")
    target = user.get("target_profit")

    if not key:
        return (
            "💰 點數配置\n\n"
            "請先選擇點數區間。\n\n"
            "流程：\n"
            "1. 選擇點數區間\n"
            "2. 選擇打法模式\n"
            "3. 選擇期望獲利\n\n"
            "完成後即可匯入牌路。"
        )

    if not mode:
        return (
            "💰 點數配置\n\n"
            f"點數區間：{POINT_CONFIG[key]['label']}\n\n"
            "請選擇打法模式。"
        )

    fp = get_final_points(user)
    if fp:
        base_low, base_high = fp["base"]
        base_text = f"{base_low}點" if base_low == base_high else f"{base_low}～{base_high}點"
        target_text = f"{fp['target_low']}點" if fp["target_low"] == fp["target_high"] else f"{fp['target_low']}～{fp['target_high']}點"
        final_text = "暫停" if fp["final_low"] == 0 else (f"{fp['final_low']}點" if fp["final_low"] == fp["final_high"] else f"{fp['final_low']}～{fp['final_high']}點")
        unit_text = f"基礎：{base_text}\n期望倍率後：{target_text}\n目前狀態：{final_text}（{fp['point_state']}）"
    else:
        unit_text = "尚未設定"

    target_text = target if target else "尚未設定"

    return (
        "💰 點數配置完成\n\n"
        f"點數區間：{POINT_CONFIG[key]['label']}\n"
        f"打法模式：{mode}\n"
        f"期望獲利：{target_text}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"參考點數：\n{unit_text}\n\n"
        "節奏控管：\n"
        "👉 期望30%：基礎倍率 ×0.8\n"
        "👉 期望50%：基礎倍率 ×1.2\n"
        "👉 期望100%：基礎倍率 ×2.0\n"
        "👉 觀察中：低區間70%\n"
        "👉 進攻中：放大40%～60%\n"
        "👉 強攻中：放大70%｜極限最高100%\n"
        "👉 降速：降至50%\n"
        "👉 暫停：不啟動點數\n\n"
        "✅ 配置完成\n"
        "下一步：請點選【匯入牌路】。"
    )


def point_decision_text(user, data):
    fp = get_final_points(user, data)
    if not fp:
        return "尚未設定，請先點選【點數配置】。"

    if fp["final_low"] == 0:
        return f"{fp['point_state']}"

    low = fp["final_low"]
    high = fp["final_high"]
    if low == high:
        return f"{low}點（{fp['point_state']}）"
    return f"{low}～{high}點（{fp['point_state']}）"


def decision_card(user, road):
    data = prediction_v6(road)
    fp = get_final_points(user, data)
    state = fp["state"] if fp else "👀 觀察中"
    point_text = point_decision_text(user, data)

    return (
        "🎯 方向\n\n"
        f"👉👉👉 {data['direction']} {data['direction_pct']}% 👈👈👈\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "🧠 系統狀態\n"
        f"{state}\n\n"
        "💰 點數\n"
        f"👉 {point_text}\n\n"
        f"⚠️ 風險：{data['risk']}\n"
        f"📌 建議模式：{fp['recommended_mode'] if fp else '保守'}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "操作：莊 / 閒 / 和 / 詳細分析 / 結束分析"
    )


def detail_card(user, road):
    data = prediction_v6(road)
    seq = filter_main_road(road)[-30:]
    details = "\n".join([f"・{x}" for x in data["match_details"]]) if data["match_details"] else "・目前無足夠重複樣本"
    fp = get_final_points(user, data)

    if fp:
        base_low, base_high = fp["base"]
        base_unit = f"{base_low}點" if base_low == base_high else f"{base_low}～{base_high}點"
        target_unit = f"{fp['target_low']}點" if fp["target_low"] == fp["target_high"] else f"{fp['target_low']}～{fp['target_high']}點"
        if fp["final_low"] == 0:
            point_unit = f"基礎：{base_unit}\n期望倍率後：{target_unit}\n目前：暫停（{fp['point_state']}）"
        else:
            final_unit = f"{fp['final_low']}點" if fp["final_low"] == fp["final_high"] else f"{fp['final_low']}～{fp['final_high']}點"
            point_unit = f"基礎：{base_unit}\n期望倍率後：{target_unit}\n目前：{final_unit}（{fp['point_state']}）"
    else:
        point_unit = "尚未設定"

    total = (user.get("round_win") or 0) + (user.get("round_loss") or 0)
    rate = round((user.get("round_win") or 0) * 100 / total) if total else 0

    return (
        "📊 詳細分析 V9\n\n"
        f"目前牌路：\n{road_text(seq[-20:])}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "預測機率：\n"
        f"莊：{data['banker_pct']}%\n"
        f"閒：{data['player_pct']}%\n"
        f"信號強度：{data['signal']}\n"
        f"風險：{data['risk']}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "▍序列匹配\n"
        f"{data['match_note']}\n"
        f"{details}\n\n"
        "▍尾段動能\n"
        f"{data['tail_note']}\n\n"
        "▍結構判斷\n"
        f"{data['structure_note']}\n\n"
        "▍下三路結構\n"
        f"大眼仔：{data['subroad']['big_eye']}\n"
        f"小路：{data['subroad']['small']}\n"
        f"曱甴路：{data['subroad']['cockroach']}\n"
        f"穩定度：{data['subroad']['stability_score']}\n"
        f"{data['subroad']['note']}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "數據指標：\n"
        f"莊：{data['metrics']['莊']}\n"
        f"閒：{data['metrics']['閒']}\n"
        f"交錯率：{data['metrics']['交錯率']}%\n"
        f"平均段長：{data['metrics']['平均段長']}\n"
        f"最長連續：{data['metrics']['最長連續']}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "💰 點數引擎\n"
        f"點數區間：{point_range_text(user)}\n"
        f"打法模式：{user.get('play_mode') or '尚未設定'}\n"
        f"期望獲利：{user.get('target_profit') or '尚未設定'}\n"
        f"系統狀態：{fp['state'] if fp else '尚未設定'}\n"
        f"建議模式：{fp['recommended_mode'] if fp else '尚未設定'}\n"
        f"參考點數：\n{point_unit}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "📈 本輪表現\n"
        f"本輪紀錄：{user.get('round_win', 0)}/{total}（{rate}%）\n"
        f"連續順利：{user.get('win_streak', 0)}\n"
        f"連續失利：{user.get('loss_streak', 0)}\n"
        f"最大連順：{user.get('max_win_streak', 0)}\n"
        f"最大連失：{user.get('max_loss_streak', 0)}"
    )


def settlement_card(user):
    total = (user.get("round_win") or 0) + (user.get("round_loss") or 0)
    win = user.get("round_win") or 0
    rate = round(win * 100 / total) if total else 0
    max_win = user.get("max_win_streak") or 0
    max_loss = user.get("max_loss_streak") or 0

    if rate >= 70 and max_loss <= 1:
        eval_text = "表現強勢，下輪可維持積極或極限。"
        next_mode = "積極 / 極限"
    elif rate >= 55:
        eval_text = "表現穩定，下輪建議標準或積極。"
        next_mode = "標準 / 積極"
    elif max_loss >= 3:
        eval_text = "波動偏大，下輪建議保守觀察。"
        next_mode = "保守"
    else:
        eval_text = "表現普通，下輪建議標準模式。"
        next_mode = "標準"

    return (
        "📊 本輪結算\n\n"
        f"結果：{win} / {total}\n"
        f"命中率：{rate}%\n"
        f"最大連順：{max_win}\n"
        f"最大連失：{max_loss}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "🧠 評價\n"
        f"{eval_text}\n\n"
        f"👉 建議下輪：{next_mode}"
    )


# =========================
# Analysis logs / scoring
# =========================
def create_analysis_log(line_user_id, road):
    data = prediction_v6(road)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO analysis_logs (
                    line_user_id,
                    road_snapshot,
                    banker_pct,
                    player_pct,
                    pattern,
                    risk,
                    actual_next_result
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


def backfill_previous_actual(line_user_id, actual_result):
    if actual_result not in ["莊", "閒"]:
        return None

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, banker_pct, player_pct
                FROM analysis_logs
                WHERE line_user_id = %s
                  AND actual_next_result IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (line_user_id,),
            )
            row = cur.fetchone()

            if not row:
                conn.commit()
                return None

            predicted = "莊" if row["banker_pct"] >= row["player_pct"] else "閒"
            is_hit = predicted == actual_result

            cur.execute(
                "UPDATE analysis_logs SET actual_next_result = %s WHERE id = %s",
                (actual_result, row["id"]),
            )

        conn.commit()
        return is_hit


def update_round_record(user_id, is_hit):
    user = get_user(user_id)
    if is_hit is None:
        return user

    if is_hit:
        new_win_streak = (user.get("win_streak") or 0) + 1
        max_win = max(user.get("max_win_streak") or 0, new_win_streak)
        return update_user_fields(
            user_id,
            round_win=(user.get("round_win") or 0) + 1,
            win_streak=new_win_streak,
            loss_streak=0,
            max_win_streak=max_win,
        )

    new_loss_streak = (user.get("loss_streak") or 0) + 1
    max_loss = max(user.get("max_loss_streak") or 0, new_loss_streak)
    return update_user_fields(
        user_id,
        round_loss=(user.get("round_loss") or 0) + 1,
        loss_streak=new_loss_streak,
        win_streak=0,
        max_loss_streak=max_loss,
    )

def hit_rate_summary(line_user_id):
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

    hit = 0
    for row in rows:
        predicted = "莊" if row["banker_pct"] >= row["player_pct"] else "閒"
        if predicted == row["actual_next_result"]:
            hit += 1

    rate = round(hit * 100 / len(rows))
    return f"近{len(rows)}筆回測命中：{rate}%"


# =========================
# Text templates
# =========================
def get_status_text(user):
    if is_vip(user):
        days = minutes_left(user["vip_expire_at"]) // 1440
        return f"目前狀態：VIP\n\n到期時間：{user['vip_expire_at']}\n剩餘：約 {days} 天"

    if in_trial(user):
        mins = minutes_left(user["trial_end_at"])
        return f"目前狀態：免費試用中\n\n剩餘時間：約 {mins} 分鐘\n試用結束後，完整分析需開通VIP。"

    return "試用已結束\n\nVIP開通：\n👉 註冊3A帳號 / 已有3A帳號\n👉 聯絡管理員"


def member_guide_text():
    return (
        "【使用教學】\n\n"
        "本系統為「牌路紀錄＋方向決策輔助＋點數配置」模式。\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "① 點數配置\n"
        "點選【點數配置】\n"
        "依序選擇：點數區間 → 打法模式（保守/標準/積極/極限）→ 期望獲利。\n\n"
        "② 綁定帳號\n"
        "點選【綁定帳號】\n"
        "輸入你的遊戲帳號，例如：ck76888。\n\n"
        "③ 匯入牌路\n"
        "點選【匯入牌路】\n"
        "輸入目前牌路，至少15把以上。\n\n"
        "例：\n"
        "莊莊莊閒莊閒閒莊莊閒閒莊閒莊閒\n\n"
        "④ 開始分析\n"
        "點選【開始分析】\n"
        "系統會回覆方向決策輔助與點數配置提示。\n\n"
        "⑤ 即時紀錄\n"
        "每開一把，點選或輸入：\n"
        "莊 / 閒 / 和\n\n"
        "系統會即時更新：\n"
        "方向、AI判斷、風險、點數配置。\n\n"
        "⑥ 詳細分析\n"
        "如需查看序列匹配、下三路、交錯率、回測資料，點選【詳細分析】。\n\n"
        "⑦ 結束分析\n"
        "本輪結束時，點選【結束分析】。\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "【開通教學】\n\n"
        "Step 1\n"
        "請由以下入口完成註冊：\n"
        "sn043.aaawin88.com\n\n"
        "👉 已有帳號者請直接綁定帳號。\n\n"
        "Step 2\n"
        "點選【綁定帳號】。\n\n"
        "Step 3\n"
        "輸入你的遊戲帳號。\n\n"
        "Step 4\n"
        "聯絡管理員確認開通狀態。\n\n"
        "※ 實際資格、發放方式及相關規範，請以平台公告為準。"
    )


def menu_text(user):
    tag = "VIP會員" if is_vip(user) else ("免費試用中" if in_trial(user) else "免費版")
    return (
        f"歡迎使用 AI 百家方向決策輔助系統（{tag}）\n\n"
        "建議流程：\n"
        "1. 點數配置\n"
        "2. 綁定帳號\n"
        "3. 匯入牌路\n"
        "4. 開始分析\n"
        "5. 莊 / 閒 / 和 即時紀錄\n"
        "6. 結束分析\n\n"
        "請從下方按鈕開始。"
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
    except Exception as exc:
        print("JSON ERROR:", exc)
        return "OK", 200

    for event in data.get("events", []):
        user_id = event.get("source", {}).get("userId")
        print("USER ID:", user_id)

        if event.get("type") == "follow":
            reply_token = event.get("replyToken")
            if user_id and reply_token:
                user = ensure_user(user_id)
                reply_message(reply_token, menu_text(user), quick_items=qr_main(user_id in ADMIN_USER_IDS))
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
        is_admin = user_id in ADMIN_USER_IDS

        # Admin commands
        if is_admin and text == "/待開通":
            pending = list_pending_accounts()
            if not pending:
                reply_message(reply_token, "目前沒有待開通名單。", quick_items=qr_main(True))
            else:
                rows = [f"{i}. {row['game_account']}" for i, row in enumerate(pending[:20], 1)]
                reply_message(reply_token, "待開通名單：\n" + "\n".join(rows), quick_items=qr_main(True))
            continue

        if is_admin and text.startswith("/vip "):
            parts = text.split()
            if len(parts) != 3:
                reply_message(reply_token, "格式錯誤，請用：/vip 遊戲帳號 天數", quick_items=qr_main(True))
                continue
            try:
                days = int(parts[2])
            except ValueError:
                reply_message(reply_token, "天數請輸入數字，例如：/vip ck76888 30", quick_items=qr_main(True))
                continue

            game_account = parts[1]
            updated_user, err = grant_vip_by_game_account(game_account, days)
            if err:
                reply_message(reply_token, err, quick_items=qr_main(True))
            else:
                reply_message(
                    reply_token,
                    f"已開通VIP\n\n帳號：{game_account}\n天數：{days}天\n到期：{updated_user['vip_expire_at']}",
                    quick_items=qr_main(True),
                )
                push_message(
                    updated_user["line_user_id"],
                    f"你的VIP已開通\n\n到期時間：{updated_user['vip_expire_at']}\n\n現在可使用完整分析功能。",
                )
            continue

        if is_admin and text.startswith("/unvip "):
            parts = text.split()
            if len(parts) != 2:
                reply_message(reply_token, "格式錯誤，請用：/unvip 遊戲帳號", quick_items=qr_main(True))
                continue
            ok = revoke_vip_by_game_account(parts[1])
            reply_message(reply_token, f"已取消VIP：{parts[1]}" if ok else "找不到這個遊戲帳號。", quick_items=qr_main(True))
            continue

        # Pending flows
        if user.get("pending_flow") == "bind_game_account":
            ok = set_game_account(user_id, text)
            if ok:
                reply_message(
                    reply_token,
                    f"已收到你的遊戲帳號：{text}\n\n請等待管理員確認開通VIP。",
                    quick_items=qr_main(is_admin),
                )
            else:
                reply_message(
                    reply_token,
                    "這個遊戲帳號可能已被綁定，請換一個或聯絡管理員。",
                    quick_items=qr_main(is_admin),
                )
            continue

        if user.get("pending_flow") == "import_road":
            parsed = normalize_input_road(text)
            if not parsed:
                reply_message(
                    reply_token,
                    "格式錯誤，請只輸入：莊 / 閒 / 和\n例如：莊莊莊閒莊閒",
                    quick_items=qr_main(is_admin),
                )
                continue
            if len(parsed) < MIN_IMPORT_HANDS:
                reply_message(
                    reply_token,
                    f"目前只有 {len(parsed)} 把，至少要 {MIN_IMPORT_HANDS} 把才能開始分析。",
                    quick_items=qr_main(is_admin),
                )
                continue

            update_user_fields(
                user_id,
                current_road=parsed[-MAX_ROAD:],
                pending_flow=None,
                imported_ready=True,
                analysis_active=False,
                round_win=0,
                round_loss=0,
                win_streak=0,
                loss_streak=0,
                max_win_streak=0,
                max_loss_streak=0,
            )
            imported_user = get_user(user_id)
            reply_message(
                reply_token,
                "牌路匯入完成\n\n"
                f"目前牌路：\n{road_text(imported_user['current_road'])}\n\n"
                "接下來請點選【開始分析】。",
                quick_items=qr_imported(),
            )
            continue

        # Lock trial / VIP
        locked_commands = ["分析", "詳細分析", "牌路", "匯入牌路", "開始分析", "莊", "閒", "和", "點數配置", "查詢配置"]
        if not is_vip(user) and not in_trial(user) and text in locked_commands:
            reply_message(reply_token, get_status_text(user), quick_items=qr_main(is_admin))
            continue

        # General
        if text == "開始":
            reply_message(reply_token, menu_text(user), quick_items=qr_main(is_admin))
            continue

        if text in ["會員說明", "使用教學", "開通教學"]:
            reply_message(reply_token, member_guide_text(), quick_items=qr_main(is_admin))
            continue

        if text == "綁定帳號":
            update_user_fields(user_id, pending_flow="bind_game_account")
            reply_message(reply_token, "請輸入你的遊戲帳號\n例如：ck76888", quick_items=qr_main(is_admin))
            continue

        if text == "查詢資格":
            reply_message(reply_token, get_status_text(user), quick_items=qr_main(is_admin))
            continue

        # Point config flow
        if text in ["點數配置", "本金配置"]:
            update_user_fields(user_id, pending_flow="point_range")
            reply_message(
                reply_token,
                "💰 點數配置\n\n請選擇你的點數區間。",
                quick_items=qr_point_ranges(),
            )
            continue

        if user.get("pending_flow") == "point_range" and text in POINT_RANGE_INPUT_MAP:
            key = POINT_RANGE_INPUT_MAP[text]
            update_user_fields(user_id, point_range=key, pending_flow="play_mode")
            reply_message(
                reply_token,
                f"已選擇：{POINT_CONFIG[key]['label']}\n\n請選擇打法模式。",
                quick_items=qr_modes(),
            )
            continue

        if user.get("pending_flow") == "play_mode" and text in PLAY_MODE_INPUTS:
            update_user_fields(user_id, play_mode=text, pending_flow="target_profit")
            reply_message(
                reply_token,
                f"已選擇：{text}\n\n請選擇期望獲利。",
                quick_items=qr_targets(),
            )
            continue

        if user.get("pending_flow") == "target_profit" and text in TARGET_INPUTS:
            user = update_user_fields(user_id, target_profit=text, pending_flow=None)
            reply_message(
                reply_token,
                point_config_card(user),
                quick_items=qr_after_config(),
            )
            continue

        # Backward-compatible old quick-reply commands
        if text.startswith("點數_"):
            key = text.replace("點數_", "", 1)
            if key not in POINT_CONFIG:
                reply_message(reply_token, "點數區間錯誤，請重新選擇。", quick_items=qr_point_ranges())
                continue
            update_user_fields(user_id, point_range=key, pending_flow="play_mode")
            reply_message(
                reply_token,
                f"已選擇：{POINT_CONFIG[key]['label']}\n\n請選擇打法模式。",
                quick_items=qr_modes(),
            )
            continue

        if text.startswith("打法_"):
            mode = text.replace("打法_", "", 1)
            if mode not in ["保守", "標準", "積極", "極限"]:
                reply_message(reply_token, "打法模式錯誤，請重新選擇。", quick_items=qr_modes())
                continue
            update_user_fields(user_id, play_mode=mode, pending_flow="target_profit")
            reply_message(
                reply_token,
                f"已選擇：{mode}\n\n請選擇期望獲利。",
                quick_items=qr_targets(),
            )
            continue

        if text.startswith("獲利_"):
            target = text.replace("獲利_", "", 1) + "%"
            user = update_user_fields(user_id, target_profit=target, pending_flow=None)
            reply_message(
                reply_token,
                point_config_card(user),
                quick_items=qr_after_config(),
            )
            continue

        if text == "查詢配置":
            reply_message(reply_token, point_config_card(user), quick_items=qr_after_config())
            continue

        # Road / Analysis
        if text == "匯入牌路":
            update_user_fields(user_id, pending_flow="import_road")
            reply_message(
                reply_token,
                "請一次輸入目前牌路\n"
                "格式例如：\n"
                "莊莊莊閒莊閒莊閒莊莊閒閒莊閒莊\n\n"
                "至少15把才可啟動分析。",
                quick_items=qr_main(is_admin),
            )
            continue

        if text == "開始分析":
            if not user["imported_ready"]:
                reply_message(
                    reply_token,
                    f"請先匯入至少 {MIN_IMPORT_HANDS} 把牌路，再開始分析。",
                    quick_items=qr_main(is_admin),
                )
                continue

            user = update_user_fields(
                user_id,
                analysis_active=True,
                round_win=0,
                round_loss=0,
                win_streak=0,
                loss_streak=0,
                max_win_streak=0,
                max_loss_streak=0,
            )
            create_analysis_log(user_id, user["current_road"] or [])
            reply_message(
                reply_token,
                "分析已啟動\n\n" + decision_card(user, user["current_road"] or []),
                quick_items=qr_analysis(),
            )
            continue

        if text == "詳細分析":
            if not user["analysis_active"]:
                reply_message(reply_token, "目前尚未開始分析，請先匯入牌路並點選【開始分析】。", quick_items=qr_main(is_admin))
                continue
            reply_message(reply_token, detail_card(user, user["current_road"] or []), quick_items=qr_analysis())
            continue

        if text == "結束分析":
            summary = settlement_card(user)
            update_user_fields(
                user_id,
                analysis_active=False,
                imported_ready=False,
                current_road=[],
                pending_flow=None,
            )
            reply_message(
                reply_token,
                summary + "\n\n你可以重新匯入牌路，或調整點數配置。",
                quick_items=qr_after_end(),
            )
            continue

        if text in ["莊", "閒", "和"]:
            if not user["analysis_active"]:
                reply_message(
                    reply_token,
                    "請先完成：\n1. 匯入牌路\n2. 開始分析\n\n之後再逐口輸入 莊 / 閒 / 和。",
                    quick_items=qr_main(is_admin),
                )
                continue

            is_hit = backfill_previous_actual(user_id, text)
            user = update_round_record(user_id, is_hit)

            road = user["current_road"] or []
            road.append(text)
            road = road[-MAX_ROAD:]

            user = update_user_fields(user_id, current_road=road)
            create_analysis_log(user_id, user["current_road"] or [])

            latest = get_user(user_id)
            reply_message(
                reply_token,
                f"已記錄：{text}\n\n" + decision_card(latest, latest["current_road"] or []),
                quick_items=qr_analysis(),
            )
            continue

        if text == "牌路":
            limit = 20 if is_vip(user) else 8
            reply_message(
                reply_token,
                f"目前牌路：\n{road_text(user['current_road'] or [], limit)}",
                quick_items=qr_analysis() if user["analysis_active"] else qr_main(is_admin),
            )
            continue

        if text == "分析":
            if not user["analysis_active"]:
                reply_message(reply_token, "目前尚未開始分析，請先匯入牌路並點選【開始分析】。", quick_items=qr_main(is_admin))
                continue
            reply_message(reply_token, decision_card(user, user["current_road"] or []), quick_items=qr_analysis())
            continue

        if text == "重設":
            user = update_user_fields(
                user_id,
                current_road=[],
                imported_ready=False,
                analysis_active=False,
                pending_flow=None,
                round_win=0,
                round_loss=0,
                win_streak=0,
                loss_streak=0,
                max_win_streak=0,
                max_loss_streak=0,
            )
            reply_message(reply_token, "已重設當前牌路與分析狀態。", quick_items=qr_main(is_admin))
            continue

        reply_message(
            reply_token,
            "你剛剛說："
            f"{text}\n\n"
            "可用功能：開始 / 點數配置 / 綁定帳號 / 匯入牌路 / 開始分析 / "
            "莊 / 閒 / 和 / 詳細分析 / 結束分析 / 會員說明",
            quick_items=qr_main(is_admin),
        )

    return "OK", 200


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
else:
    init_db()
