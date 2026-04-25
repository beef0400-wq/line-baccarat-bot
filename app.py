
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
    "1000點以下": {
        "保守": (50, 50),
        "標準": (50, 100),
        "積極": (100, 150),
    },
    "1000～3000點": {
        "保守": (100, 100),
        "標準": (100, 200),
        "積極": (200, 300),
    },
    "3000～5000點": {
        "保守": (150, 150),
        "標準": (200, 400),
        "積極": (400, 600),
    },
    "5000～10000點": {
        "保守": (250, 250),
        "標準": (300, 600),
        "積極": (600, 1000),
    },
    "10000～30000點": {
        "保守": (400, 400),
        "標準": (500, 1000),
        "積極": (1000, 2000),
    },
    "30000以上": {
        "保守": (500, 500),
        "標準": (1000, 2000),
        "積極": (2000, 4000),
    },
}

TARGET_OPTIONS = ["30%", "50%", "100%"]


# =========================
# Time / DB
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
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS point_mode TEXT NULL;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS profit_target TEXT NULL;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS win_streak INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS loss_streak INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS round_hits INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS round_total INTEGER NOT NULL DEFAULT 0;",
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
# LINE helpers
# =========================
def verify_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        return False
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def line_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }


def make_quick_reply(items):
    return [
        {
            "type": "action",
            "action": {
                "type": "message",
                "label": label,
                "text": text,
            },
        }
        for label, text in items
    ]


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
            ("點數配置", "點數配置"),
            ("匯入牌路", "匯入牌路"),
            ("開始分析", "開始分析"),
            ("會員說明", "會員說明"),
            ("綁定帳號", "綁定帳號"),
        ]

    if is_admin:
        items[-1] = ("/待開通", "/待開通")

    return make_quick_reply(items)


def point_range_quick_reply():
    return make_quick_reply([
        ("1000以下", "點數_1000點以下"),
        ("1000～3000", "點數_1000～3000點"),
        ("3000～5000", "點數_3000～5000點"),
        ("5000～10000", "點數_5000～10000點"),
        ("10000～30000", "點數_10000～30000點"),
        ("30000以上", "點數_30000以上"),
    ])


def point_mode_quick_reply():
    return make_quick_reply([
        ("保守", "打法_保守"),
        ("標準", "打法_標準"),
        ("積極", "打法_積極"),
    ])


def target_quick_reply():
    return make_quick_reply([
        ("30%", "獲利_30%"),
        ("50%", "獲利_50%"),
        ("100%", "獲利_100%"),
    ])


def after_config_quick_reply():
    return make_quick_reply([
        ("匯入牌路", "匯入牌路"),
        ("開始分析", "開始分析"),
        ("查詢配置", "查詢配置"),
        ("會員說明", "會員說明"),
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
# User / state
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
                    point_mode = %s,
                    profit_target = %s,
                    win_streak = %s,
                    loss_streak = %s,
                    round_hits = %s,
                    round_total = %s,
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
                    fields.get("point_mode", current.get("point_mode")),
                    fields.get("profit_target", current.get("profit_target")),
                    fields.get("win_streak", current.get("win_streak", 0)),
                    fields.get("loss_streak", current.get("loss_streak", 0)),
                    fields.get("round_hits", current.get("round_hits", 0)),
                    fields.get("round_total", current.get("round_total", 0)),
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
# Admin / binding
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
# Road helpers
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


# =========================
# V4/V5 derived-road structure
# =========================
def derived_color(seq, offset):
    segs = segment_lengths(seq)
    if len(segs) <= offset:
        return "無"

    current_len = segs[-1][1]
    reference_len = segs[-1 - offset][1]

    if current_len == reference_len:
        return "紅"
    if current_len >= 2 and reference_len >= 2:
        return "紅"
    return "藍"


def subroad_analysis(seq):
    if len(seq) < 8:
        return {
            "big_eye": "無",
            "small": "無",
            "cockroach": "無",
            "score": 50,
            "label": "下三路資料不足",
        }

    big_eye = derived_color(seq, 1)
    small = derived_color(seq, 2)
    cockroach = derived_color(seq, 3)

    colors = [big_eye, small, cockroach]
    red = colors.count("紅")
    blue = colors.count("藍")

    if red == 3:
        score = 78
        label = "三路偏紅，結構穩定度高"
    elif blue == 3:
        score = 32
        label = "三路偏藍，結構混亂"
    elif red == 2:
        score = 62
        label = "下三路偏穩，但仍需確認"
    elif blue == 2:
        score = 42
        label = "下三路偏亂，轉折機率提高"
    else:
        score = 50
        label = "下三路中性"

    return {
        "big_eye": big_eye,
        "small": small,
        "cockroach": cockroach,
        "score": score,
        "label": label,
    }


# =========================
# Prediction engine V5
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


def risk_and_signal(banker_pct, player_pct, seq, total_matches, structure_label, sub_score):
    gap = abs(banker_pct - player_pct)
    t_rate = transition_rate(seq)
    avg_len = avg_segment_length(seq)

    if gap >= 22 and total_matches >= 2 and sub_score >= 60:
        signal = "強"
    elif gap >= 14 or total_matches >= 1:
        signal = "中強"
    elif gap >= 8:
        signal = "中"
    else:
        signal = "弱"

    if len(seq) < 15:
        risk = "中高"
    elif sub_score <= 40:
        risk = "高"
    elif "混合" in structure_label and gap < 10:
        risk = "高"
    elif t_rate > 75 or avg_len < 1.35:
        risk = "中高"
    elif gap >= 18 and signal in ["強", "中強"] and sub_score >= 60:
        risk = "中低"
    else:
        risk = "中"

    return signal, risk


def prediction_v5(road):
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
            "subroad": subroad_analysis([]),
            "metrics": {
                "莊": 0,
                "閒": 0,
                "交錯率": 0,
                "平均段長": 0,
                "最長連續": 0,
            },
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

    sub = subroad_analysis(seq)
    if sub["score"] >= 70:
        # structure stable: amplify leader
        if score_b >= score_p:
            score_b += 8
        else:
            score_p += 8
    elif sub["score"] <= 40:
        # structure messy: reduce overconfidence
        score_b += 2
        score_p += 2

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

    signal, risk = risk_and_signal(banker_pct, player_pct, seq, total_matches, structure_note, sub["score"])

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
        "subroad": sub,
        "metrics": {
            "莊": banker,
            "閒": player,
            "交錯率": transition_rate(seq),
            "平均段長": avg_segment_length(seq),
            "最長連續": longest_run(seq),
        },
    }


def ai_decision_text(data, user):
    gap = abs(data["banker_pct"] - data["player_pct"])
    signal = data["signal"]
    risk = data["risk"]
    sub_score = data["subroad"]["score"]

    if gap >= 18 and signal in ["中強", "強"] and risk != "高" and sub_score >= 55:
        status = "✅ 可觀察進場"
        note = "目前訊號與結構偏一致，可列入觀察。"
    elif gap >= 8 and risk != "高":
        status = "⚠️ 等待確認"
        note = "目前有方向，但結構仍需要下一口確認。"
    else:
        status = "⛔ 暫停本輪"
        note = "目前差距不足或風險偏高，建議先觀察。"

    point_line = point_action_line(user, status)

    return (
        "🧠 AI 決策提示\n\n"
        f"狀態：{status}\n"
        f"說明：{note}\n\n"
        f"{point_line}\n\n"
        "提醒：此為牌路結構與點數控管提示，請自行控管節奏。"
    )


def prediction_card(road, user=None):
    seq = filter_main_road(road)[-30:]
    data = prediction_v5(road)

    if data["match_details"]:
        detail_lines = "\n".join([f"・{x}" for x in data["match_details"]])
    else:
        detail_lines = "・目前無足夠重複樣本"

    sub = data["subroad"]

    card = (
        "📊 預測判讀 V5\n\n"
        f"目前牌路：\n{road_text(seq[-20:])}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "預測機率：\n"
        f"👉 莊：{data['banker_pct']}%\n"
        f"👉 閒：{data['player_pct']}%\n\n"
        f"信號強度：{data['signal']}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "判斷依據：\n\n"
        "▍序列匹配\n"
        f"{data['match_note']}\n"
        f"{detail_lines}\n\n"
        "▍尾段動能\n"
        f"{data['tail_note']}\n\n"
        "▍結構判斷\n"
        f"{data['structure_note']}\n\n"
        "▍下三路結構\n"
        f"大眼仔：{sub['big_eye']}\n"
        f"小路：{sub['small']}\n"
        f"曱甴路：{sub['cockroach']}\n"
        f"穩定度：{sub['score']}\n"
        f"{sub['label']}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "數據指標：\n"
        f"莊：{data['metrics']['莊']}\n"
        f"閒：{data['metrics']['閒']}\n"
        f"交錯率：{data['metrics']['交錯率']}%\n"
        f"平均段長：{data['metrics']['平均段長']}\n"
        f"最長連續：{data['metrics']['最長連續']}\n\n"
        "風險：\n"
        f"{data['risk']}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"{ai_decision_text(data, user or {})}"
    )

    return card


# =========================
# Point config
# =========================
def point_range_from_text(text):
    return text.replace("點數_", "", 1)


def point_mode_from_text(text):
    return text.replace("打法_", "", 1)


def profit_target_from_text(text):
    return text.replace("獲利_", "", 1)


def get_point_range(user):
    return user.get("point_range") or "尚未設定"


def get_point_mode(user):
    return user.get("point_mode") or "尚未設定"


def get_profit_target(user):
    return user.get("profit_target") or "尚未設定"


def get_point_bet_range(user):
    point_range = user.get("point_range")
    mode = user.get("point_mode")

    if point_range not in POINT_CONFIG or mode not in ["保守", "標準", "積極"]:
        return None

    return POINT_CONFIG[point_range][mode]


def format_point_range(pair):
    if not pair:
        return "尚未設定"
    low, high = pair
    if low == high:
        return f"{low}點"
    return f"{low}～{high}點"


def point_action_line(user, decision_status):
    pair = get_point_bet_range(user)
    if not pair:
        return "點數配置：尚未完成，建議先完成點數配置。"

    low, high = pair
    win_streak = user.get("win_streak", 0) or 0
    loss_streak = user.get("loss_streak", 0) or 0

    if "暫停" in decision_status:
        return "點數建議：本輪暫停，不建議啟動點數。"

    if loss_streak >= 5:
        return "點數建議：連續失利已達5次，建議暫停本輪。"
    if loss_streak >= 3:
        return f"點數建議：連續失利，降至最低區間 {low}點。"
    if loss_streak >= 2:
        return f"點數建議：連失2次，建議壓低至 {low}點附近。"

    if win_streak >= 5:
        mid = (low + high) // 2
        return f"點數建議：連續順利，建議鎖利並回到中段 {mid}點附近。"
    if win_streak >= 3:
        return f"點數建議：連續順利，可使用上限區間 {high}點附近。"
    if win_streak >= 2:
        mid = (low + high) // 2
        return f"點數建議：連順2次，可提升至 {mid}～{high}點。"

    return f"點數建議：正常區間 {format_point_range(pair)}。"


def point_config_card(user):
    pair = get_point_bet_range(user)
    win_streak = user.get("win_streak", 0) or 0
    loss_streak = user.get("loss_streak", 0) or 0
    total = user.get("round_total", 0) or 0
    hits = user.get("round_hits", 0) or 0
    rate = round(hits * 100 / total) if total else 0

    return (
        "💰 點數配置\n\n"
        f"點數區間：{get_point_range(user)}\n"
        f"打法模式：{get_point_mode(user)}\n"
        f"期望獲利：{get_profit_target(user)}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"建議點數：{format_point_range(pair)}\n\n"
        "升降邏輯：\n"
        "👉 連失2次：降至低區間\n"
        "👉 連失5次：建議暫停本輪\n"
        "👉 連順2次：提高至中高區間\n"
        "👉 連順5次：建議鎖利回中段\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"本輪紀錄：{hits}/{total}（{rate}%）\n"
        f"連續順利：{win_streak}\n"
        f"連續失利：{loss_streak}\n\n"
        "接下來請點選【匯入牌路】，輸入至少15把後即可開始分析。"
    )


def point_setup_intro():
    return (
        "💰 點數配置\n\n"
        "請先選擇目前點數區間。\n\n"
        "系統會依照：\n"
        "① 點數區間\n"
        "② 打法模式\n"
        "③ 期望獲利\n\n"
        "自動給出建議點數與升降邏輯。"
    )


# =========================
# Analysis logs
# =========================
def create_analysis_log(line_user_id, road):
    data = prediction_v5(road)

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


def backfill_previous_actual_and_update_streak(user, actual_result):
    if actual_result not in ["莊", "閒"]:
        return user

    line_user_id = user["line_user_id"]

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
                return user

            predicted = "莊" if row["banker_pct"] >= row["player_pct"] else "閒"
            hit = predicted == actual_result

            cur.execute(
                "UPDATE analysis_logs SET actual_next_result = %s WHERE id = %s",
                (actual_result, row["id"]),
            )
        conn.commit()

    current_hits = user.get("round_hits", 0) or 0
    current_total = user.get("round_total", 0) or 0
    win_streak = user.get("win_streak", 0) or 0
    loss_streak = user.get("loss_streak", 0) or 0

    if hit:
        current_hits += 1
        win_streak += 1
        loss_streak = 0
    else:
        loss_streak += 1
        win_streak = 0

    current_total += 1

    return update_user_fields(
        line_user_id,
        round_hits=current_hits,
        round_total=current_total,
        win_streak=win_streak,
        loss_streak=loss_streak,
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


def member_guide_text():
    return (
        "【使用教學】\n\n"
        "本系統為「牌路紀錄＋即時分析＋點數配置」模式。\n"
        "請依照以下流程操作：\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "① 開始使用\n"
        "點選【開始】，查看目前可用功能。\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "② 綁定帳號\n"
        "點選【綁定帳號】\n"
        "輸入你的遊戲帳號（例：ck76888）\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "③ 點數配置\n"
        "點選【點數配置】\n"
        "依序選擇：\n"
        "・點數區間\n"
        "・打法模式：保守 / 標準 / 積極\n"
        "・期望獲利：30% / 50% / 100%\n\n"
        "系統會產生建議點數與升降邏輯。\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "④ 匯入牌路\n"
        "點選【匯入牌路】\n"
        "一次輸入目前牌路，至少15把以上。\n\n"
        "例：\n"
        "莊莊莊閒莊閒閒莊莊閒莊閒閒莊莊\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "⑤ 開始分析\n"
        "點選【開始分析】\n"
        "系統會輸出：\n"
        "・莊 / 閒預測機率\n"
        "・信號強度\n"
        "・序列匹配\n"
        "・尾段動能\n"
        "・結構判斷\n"
        "・下三路結構\n"
        "・AI決策提示\n"
        "・點數建議\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "⑥ 即時紀錄\n"
        "每開一把，直接點選：\n\n"
        "👉 莊\n"
        "👉 閒\n"
        "👉 和\n\n"
        "系統會同步更新分析與連順 / 連失狀態。\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "⑦ 結束分析\n"
        "本輪結束請點選【結束分析】。\n"
        "系統會清空本輪牌路，下一輪需重新匯入牌路。\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "⚠️ 未開通VIP將無法使用完整分析功能。\n"
        "請先綁定帳號並聯絡管理員開通。\n\n"
        "━━━━━━━━━━━━━━━\n"
        "【開通教學】\n\n"
        "Step1\n"
        "請由以下入口完成註冊：\n"
        "sn043.aaawin88.com\n\n"
        "👉 已有帳號者請跳至 Step 5\n\n"
        "Step 2\n"
        "請先點選下方【綁定帳號】\n\n"
        "Step 3\n"
        "輸入你的遊戲帳號\n"
        "例如：ck76888\n\n"
        "Step 4\n"
        "送出後，系統會將你的資料列入待開通名單\n\n"
        "Step 5\n"
        "聯絡管理員確認開通狀態\n\n"
        "開通完成後，即可使用完整分析功能。\n\n"
        "※ 實際資格、發放方式及相關規範，請以平台公告為準。"
    )


def menu_text(user):
    tag = "VIP會員" if is_vip(user) else ("免費試用中" if in_trial(user) else "免費版")
    return (
        f"歡迎使用百家即時分析助手（{tag}）\n\n"
        "建議流程：\n"
        "1. 點數配置\n"
        "2. 匯入牌路\n"
        "3. 開始分析\n"
        "4. 分析中逐口按 莊 / 閒 / 和\n"
        "5. 結束分析\n\n"
        "常用功能：\n"
        "點數配置\n"
        "牌路\n"
        "分析\n"
        "會員說明\n"
        "綁定帳號\n"
        "查詢資格"
    )


def append_vip_extras(card, user, user_id):
    if is_vip(user):
        card += "\n\n" + hit_rate_summary(user_id)
        if user.get("point_range") and user.get("point_mode"):
            card += "\n\n" + point_config_card(user)
    return card


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
        is_admin = user_id in ADMIN_USER_IDS

        # =========================
        # Admin commands
        # =========================
        if is_admin and text == "/待開通":
            pending = list_pending_accounts()
            if not pending:
                reply_message(reply_token, "目前沒有待開通名單。", quick_items=base_quick_reply(True, user["analysis_active"]))
            else:
                rows = [f"{i}. {row['game_account']}" for i, row in enumerate(pending[:20], 1)]
                reply_message(
                    reply_token,
                    "待開通名單：\n" + "\n".join(rows),
                    quick_items=base_quick_reply(True, user["analysis_active"]),
                )
            continue

        if is_admin and text.startswith("/vip "):
            parts = text.split()
            if len(parts) != 3:
                reply_message(reply_token, "格式錯誤，請用：/vip 遊戲帳號 天數", quick_items=base_quick_reply(True, user["analysis_active"]))
                continue

            try:
                days = int(parts[2])
            except ValueError:
                reply_message(reply_token, "天數請輸入數字，例如：/vip ck76888 30", quick_items=base_quick_reply(True, user["analysis_active"]))
                continue

            game_account = parts[1]
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

        if is_admin and text.startswith("/unvip "):
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

        # =========================
        # Pending flows
        # =========================
        if user.get("pending_flow") == "bind_game_account":
            ok = set_game_account(user_id, text)
            if ok:
                reply_message(
                    reply_token,
                    f"已收到你的遊戲帳號：{text}\n\n請等待管理員確認開通VIP。",
                    quick_items=base_quick_reply(is_admin, user["analysis_active"]),
                )
            else:
                reply_message(
                    reply_token,
                    "這個遊戲帳號可能已被綁定，請換一個或聯絡管理員。",
                    quick_items=base_quick_reply(is_admin, user["analysis_active"]),
                )
            continue

        if user.get("pending_flow") == "point_range":
            if not text.startswith("點數_"):
                reply_message(reply_token, "請使用下方按鈕選擇點數區間。", quick_items=point_range_quick_reply())
                continue

            selected_range = point_range_from_text(text)
            if selected_range not in POINT_CONFIG:
                reply_message(reply_token, "點數區間錯誤，請重新選擇。", quick_items=point_range_quick_reply())
                continue

            user = update_user_fields(user_id, point_range=selected_range, pending_flow="point_mode")
            reply_message(
                reply_token,
                f"已選擇點數區間：{selected_range}\n\n接著請選擇打法模式。",
                quick_items=point_mode_quick_reply(),
            )
            continue

        if user.get("pending_flow") == "point_mode":
            if not text.startswith("打法_"):
                reply_message(reply_token, "請使用下方按鈕選擇打法模式。", quick_items=point_mode_quick_reply())
                continue

            selected_mode = point_mode_from_text(text)
            if selected_mode not in ["保守", "標準", "積極"]:
                reply_message(reply_token, "打法模式錯誤，請重新選擇。", quick_items=point_mode_quick_reply())
                continue

            user = update_user_fields(user_id, point_mode=selected_mode, pending_flow="profit_target")
            reply_message(
                reply_token,
                f"已選擇打法模式：{selected_mode}\n\n接著請選擇期望獲利。",
                quick_items=target_quick_reply(),
            )
            continue

        if user.get("pending_flow") == "profit_target":
            if not text.startswith("獲利_"):
                reply_message(reply_token, "請使用下方按鈕選擇期望獲利。", quick_items=target_quick_reply())
                continue

            selected_target = profit_target_from_text(text)
            if selected_target not in TARGET_OPTIONS:
                reply_message(reply_token, "期望獲利選項錯誤，請重新選擇。", quick_items=target_quick_reply())
                continue

            user = update_user_fields(
                user_id,
                profit_target=selected_target,
                pending_flow=None,
                win_streak=0,
                loss_streak=0,
                round_hits=0,
                round_total=0,
            )
            reply_message(
                reply_token,
                "點數配置完成\n\n" + point_config_card(user),
                quick_items=after_config_quick_reply(),
            )
            continue

        if user.get("pending_flow") == "import_road":
            parsed = normalize_input_road(text)

            if not parsed:
                reply_message(
                    reply_token,
                    "格式錯誤，請只輸入：莊 / 閒 / 和\n例如：莊莊莊閒莊閒",
                    quick_items=base_quick_reply(is_admin, False),
                )
                continue

            if len(parsed) < MIN_IMPORT_HANDS:
                reply_message(
                    reply_token,
                    f"目前只有 {len(parsed)} 把，至少要 {MIN_IMPORT_HANDS} 把才能開始分析。",
                    quick_items=base_quick_reply(is_admin, False),
                )
                continue

            update_user_fields(
                user_id,
                current_road=parsed[-MAX_ROAD:],
                pending_flow=None,
                imported_ready=True,
                analysis_active=False,
                win_streak=0,
                loss_streak=0,
                round_hits=0,
                round_total=0,
            )
            imported_user = get_user(user_id)
            reply_message(
                reply_token,
                "牌路匯入完成\n\n"
                f"目前牌路：\n{road_text(imported_user['current_road'])}\n\n"
                "接下來請點選【開始分析】。",
                quick_items=make_quick_reply([
                    ("開始分析", "開始分析"),
                    ("重新匯入", "匯入牌路"),
                    ("點數配置", "點數配置"),
                ]),
            )
            continue

        # =========================
        # Lock trial / VIP
        # =========================
        locked_commands = [
            "分析",
            "牌路",
            "匯入牌路",
            "開始分析",
            "莊",
            "閒",
            "和",
            "點數配置",
            "本金配置",
            "查詢配置",
        ]
        if not is_vip(user) and not in_trial(user) and text in locked_commands:
            reply_message(
                reply_token,
                get_status_text(user),
                quick_items=base_quick_reply(is_admin, user["analysis_active"]),
            )
            continue

        # =========================
        # General commands
        # =========================
        if text == "開始":
            reply_message(
                reply_token,
                menu_text(user),
                quick_items=base_quick_reply(is_admin, user["analysis_active"]),
            )
            continue

        if text in ["會員說明", "使用教學", "開通教學"]:
            reply_message(
                reply_token,
                member_guide_text(),
                quick_items=make_quick_reply([
                    ("點數配置", "點數配置"),
                    ("匯入牌路", "匯入牌路"),
                    ("開始分析", "開始分析"),
                    ("結束分析", "結束分析"),
                ]),
            )
            continue

        if text == "綁定帳號":
            update_user_fields(user_id, pending_flow="bind_game_account")
            reply_message(
                reply_token,
                "請輸入你的遊戲帳號\n例如：ck76888",
                quick_items=base_quick_reply(is_admin, user["analysis_active"]),
            )
            continue

        if text == "查詢資格":
            reply_message(
                reply_token,
                get_status_text(user),
                quick_items=base_quick_reply(is_admin, user["analysis_active"]),
            )
            continue

        # =========================
        # Point config commands
        # =========================
        if text in ["點數配置", "本金配置"]:
            update_user_fields(user_id, pending_flow="point_range")
            reply_message(
                reply_token,
                point_setup_intro(),
                quick_items=point_range_quick_reply(),
            )
            continue

        if text == "查詢配置":
            reply_message(
                reply_token,
                point_config_card(user),
                quick_items=after_config_quick_reply(),
            )
            continue

        if text == "重設配置":
            user = update_user_fields(
                user_id,
                point_range=None,
                point_mode=None,
                profit_target=None,
                win_streak=0,
                loss_streak=0,
                round_hits=0,
                round_total=0,
            )
            reply_message(
                reply_token,
                "已重設點數配置。\n\n請重新點選【點數配置】。",
                quick_items=make_quick_reply([("點數配置", "點數配置")]),
            )
            continue

        # =========================
        # Road commands
        # =========================
        if text == "匯入牌路":
            update_user_fields(user_id, pending_flow="import_road")
            reply_message(
                reply_token,
                "請一次輸入目前牌路\n"
                "格式例如：\n"
                "莊莊莊閒莊閒莊閒莊莊閒閒莊閒莊\n\n"
                "至少15把才可啟動分析。",
                quick_items=base_quick_reply(is_admin, False),
            )
            continue

        if text == "開始分析":
            if not user["imported_ready"]:
                reply_message(
                    reply_token,
                    f"請先匯入至少 {MIN_IMPORT_HANDS} 把牌路，再開始分析。",
                    quick_items=base_quick_reply(is_admin, False),
                )
                continue

            user = update_user_fields(
                user_id,
                analysis_active=True,
                win_streak=0,
                loss_streak=0,
                round_hits=0,
                round_total=0,
            )
            create_analysis_log(user_id, user["current_road"] or [])

            card = prediction_card(user["current_road"] or [], user)
            card = append_vip_extras(card, user, user_id)

            reply_message(
                reply_token,
                "分析已啟動\n\n"
                f"{card}\n\n"
                "之後每開一口，直接按 莊 / 閒 / 和。\n"
                "本輪結束請按【結束分析】。",
                quick_items=base_quick_reply(is_admin, True),
            )
            continue

        if text == "結束分析":
            user = update_user_fields(
                user_id,
                analysis_active=False,
                imported_ready=False,
                current_road=[],
                pending_flow=None,
                win_streak=0,
                loss_streak=0,
                round_hits=0,
                round_total=0,
            )
            reply_message(
                reply_token,
                "已結束本輪分析，牌路與本輪紀錄已清空。\n\n"
                "下一輪請重新點選【匯入牌路】。",
                quick_items=make_quick_reply([
                    ("匯入牌路", "匯入牌路"),
                    ("點數配置", "點數配置"),
                    ("會員說明", "會員說明"),
                ]),
            )
            continue

        if text in ["莊", "閒", "和"]:
            if not user["analysis_active"]:
                reply_message(
                    reply_token,
                    "請先完成：\n1. 匯入牌路\n2. 開始分析\n\n之後再逐口輸入 莊 / 閒 / 和",
                    quick_items=base_quick_reply(is_admin, False),
                )
                continue

            user = backfill_previous_actual_and_update_streak(user, text)

            road = user["current_road"] or []
            road.append(text)
            road = road[-MAX_ROAD:]

            user = update_user_fields(user_id, current_road=road)
            create_analysis_log(user_id, user["current_road"] or [])

            latest_user = get_user(user_id)
            card = prediction_card(latest_user["current_road"] or [], latest_user)
            card = append_vip_extras(card, latest_user, user_id)

            reply_message(
                reply_token,
                f"已記錄：{text}\n\n{card}",
                quick_items=base_quick_reply(is_admin, True),
            )
            continue

        if text == "牌路":
            limit = 20 if is_vip(user) else 8
            reply_message(
                reply_token,
                f"目前牌路：\n{road_text(user['current_road'] or [], limit)}",
                quick_items=base_quick_reply(is_admin, user["analysis_active"]),
            )
            continue

        if text == "分析":
            card = prediction_card(user["current_road"] or [], user)
            card = append_vip_extras(card, user, user_id)
            reply_message(
                reply_token,
                card,
                quick_items=base_quick_reply(is_admin, user["analysis_active"]),
            )
            continue

        if text == "重設":
            user = update_user_fields(
                user_id,
                current_road=[],
                imported_ready=False,
                analysis_active=False,
                pending_flow=None,
                win_streak=0,
                loss_streak=0,
                round_hits=0,
                round_total=0,
            )
            reply_message(
                reply_token,
                "已重設當前牌路與分析狀態。",
                quick_items=base_quick_reply(is_admin, False),
            )
            continue

        reply_message(
            reply_token,
            "你剛剛說："
            f"{text}\n\n"
            "可用功能：開始 / 點數配置 / 會員說明 / 匯入牌路 / 開始分析 / "
            "牌路 / 分析 / 結束分析 / 綁定帳號 / 查詢資格",
            quick_items=base_quick_reply(is_admin, user["analysis_active"]),
        )

    return "OK", 200


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
else:
    init_db()
