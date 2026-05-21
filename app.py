# -*- coding: utf-8 -*-
# =========================
# V14 完整商業版 app.py
# LINE 百家方向判讀系統
# 本版重點：
# - 保留 V13 原本激進點數風控
# - 新增和局 / 3寶副模型
# - 和局納入 current_road 紀錄
# - 支援莊對 / 閒對 訊號記錄
# - 長龍第7顆附近提示和局觀察
# - 和後提示和 / 莊對 / 閒對 3寶觀察
# =========================

from flask import Flask, request, abort
import os
import json
import re
import hmac
import hashlib
import base64
import requests
from datetime import datetime, timedelta, timezone
from collections import Counter

try:
    import psycopg2
    import psycopg2.extras
except Exception:
    psycopg2 = None

app = Flask(__name__)
VERSION_MARKER = "V14_TIE_TREASURE_MODEL"
print("🔥 LOADED", VERSION_MARKER, flush=True)

# =========================
# ENV
# =========================
CHANNEL_ACCESS_TOKEN = (os.getenv("CHANNEL_ACCESS_TOKEN") or os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or "").strip()
CHANNEL_SECRET = (os.getenv("CHANNEL_SECRET") or os.getenv("LINE_CHANNEL_SECRET") or "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_USER_IDS = [x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]

LINE_REPLY_API = "https://api.line.me/v2/bot/message/reply"
TZ_TW = timezone(timedelta(hours=8))

# =========================
# Constants
# =========================
TRIAL_HOURS = 3
MIN_ROAD_LEN = 15

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

PLAY_MODES = ["保守", "標準", "積極", "極限"]
TARGET_PROFIT_MULT = {
    "30%": 0.8,
    "50%": 1.2,
    "100%": 2.0,
}

MEMORY_USERS = {}
MEMORY_LOGS = []

# =========================
# Basic helpers
# =========================
def now_tw():
    return datetime.now(TZ_TW)

def safe_dt(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=TZ_TW)
        return value.astimezone(TZ_TW)
    return value

def dt_to_str(dt):
    if not dt:
        return ""
    dt = safe_dt(dt)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def minutes_left(dt):
    if not dt:
        return 0
    dt = safe_dt(dt)
    return max(0, int((dt - now_tw()).total_seconds() // 60))

def json_loads_maybe(v, default):
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default

def road_to_text(road, limit=None):
    arr = road[-limit:] if limit else road
    return "".join(arr)

def main_only(road):
    return [x for x in road if x in ["莊", "閒"]]

def is_admin_line_id(line_user_id):
    return line_user_id in ADMIN_USER_IDS

# =========================
# DB layer with memory fallback
# =========================
def use_db():
    return bool(DATABASE_URL and psycopg2)

def db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    if not use_db():
        return
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                line_user_id TEXT PRIMARY KEY,
                bound_account TEXT,
                vip_expire_at TIMESTAMP NULL,
                trial_started_at TIMESTAMP NULL,
                trial_end_at TIMESTAMP NULL,
                trial_expired_notice_sent BOOLEAN NOT NULL DEFAULT FALSE,
                current_road JSONB NOT NULL DEFAULT '[]'::jsonb,
                high_count INTEGER NOT NULL DEFAULT 0,
                low_count INTEGER NOT NULL DEFAULT 0,
                tie_count INTEGER NOT NULL DEFAULT 0,
                banker_pair_count INTEGER NOT NULL DEFAULT 0,
                player_pair_count INTEGER NOT NULL DEFAULT 0,
                pending_flow TEXT,
                imported_ready BOOLEAN NOT NULL DEFAULT FALSE,
                analysis_active BOOLEAN NOT NULL DEFAULT FALSE,
                point_range TEXT,
                play_mode TEXT,
                target_profit TEXT,
                last_prediction TEXT,
                round_win INTEGER NOT NULL DEFAULT 0,
                round_loss INTEGER NOT NULL DEFAULT 0,
                win_streak INTEGER NOT NULL DEFAULT 0,
                loss_streak INTEGER NOT NULL DEFAULT 0,
                max_win_streak INTEGER NOT NULL DEFAULT 0,
                max_loss_streak INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """)
            cur.execute("""
            CREATE TABLE IF NOT EXISTS analysis_logs (
                id SERIAL PRIMARY KEY,
                line_user_id TEXT NOT NULL,
                predicted TEXT,
                actual TEXT,
                hit BOOLEAN,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """)
            log_cols = [
                ("line_user_id", "TEXT"),
                ("predicted", "TEXT"),
                ("actual", "TEXT"),
                ("hit", "BOOLEAN"),
                ("created_at", "TIMESTAMP NOT NULL DEFAULT NOW()"),
            ]
            for name, typ in log_cols:
                cur.execute(f"ALTER TABLE analysis_logs ADD COLUMN IF NOT EXISTS {name} {typ};")

            cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name='analysis_logs'
            """)
            existing_log_cols = [r["column_name"] for r in cur.fetchall()]
            for col in ["banker_pct", "player_pct", "confidence", "risk_score", "point_low", "point_high"]:
                if col in existing_log_cols:
                    try:
                        cur.execute(f"ALTER TABLE analysis_logs ALTER COLUMN {col} DROP NOT NULL;")
                        cur.execute(f"ALTER TABLE analysis_logs ALTER COLUMN {col} SET DEFAULT 0;")
                    except Exception as e:
                        print("SKIP_LEGACY_LOG_COL:", col, repr(e), flush=True)

            cols = [
                ("bound_account", "TEXT"),
                ("vip_expire_at", "TIMESTAMP NULL"),
                ("trial_started_at", "TIMESTAMP NULL"),
                ("trial_end_at", "TIMESTAMP NULL"),
                ("trial_expired_notice_sent", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("current_road", "JSONB NOT NULL DEFAULT '[]'::jsonb"),
                ("high_count", "INTEGER NOT NULL DEFAULT 0"),
                ("low_count", "INTEGER NOT NULL DEFAULT 0"),
                ("tie_count", "INTEGER NOT NULL DEFAULT 0"),
                ("banker_pair_count", "INTEGER NOT NULL DEFAULT 0"),
                ("player_pair_count", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_flow", "TEXT"),
                ("imported_ready", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("analysis_active", "BOOLEAN NOT NULL DEFAULT FALSE"),
                ("point_range", "TEXT"),
                ("play_mode", "TEXT"),
                ("target_profit", "TEXT"),
                ("last_prediction", "TEXT"),
                ("round_win", "INTEGER NOT NULL DEFAULT 0"),
                ("round_loss", "INTEGER NOT NULL DEFAULT 0"),
                ("win_streak", "INTEGER NOT NULL DEFAULT 0"),
                ("loss_streak", "INTEGER NOT NULL DEFAULT 0"),
                ("max_win_streak", "INTEGER NOT NULL DEFAULT 0"),
                ("max_loss_streak", "INTEGER NOT NULL DEFAULT 0"),
                ("created_at", "TIMESTAMP NOT NULL DEFAULT NOW()"),
                ("updated_at", "TIMESTAMP NOT NULL DEFAULT NOW()"),
            ]
            for name, typ in cols:
                cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {name} {typ};")
        conn.commit()

def normalize_user(row):
    if not row:
        return row
    row = dict(row)
    row["current_road"] = json_loads_maybe(row.get("current_road"), [])
    for k in ["vip_expire_at", "trial_started_at", "trial_end_at", "created_at", "updated_at"]:
        row[k] = safe_dt(row.get(k))
    return row

def default_user(line_user_id):
    t = now_tw()
    return {
        "line_user_id": line_user_id,
        "bound_account": None,
        "vip_expire_at": None,
        "trial_started_at": t,
        "trial_end_at": t + timedelta(hours=TRIAL_HOURS),
        "trial_expired_notice_sent": False,
        "current_road": [],
        "high_count": 0,
        "low_count": 0,
        "tie_count": 0,
        "banker_pair_count": 0,
        "player_pair_count": 0,
        "pending_flow": None,
        "imported_ready": False,
        "analysis_active": False,
        "point_range": None,
        "play_mode": None,
        "target_profit": None,
        "last_prediction": None,
        "round_win": 0,
        "round_loss": 0,
        "win_streak": 0,
        "loss_streak": 0,
        "max_win_streak": 0,
        "max_loss_streak": 0,
        "created_at": t,
        "updated_at": t,
    }

def ensure_user(line_user_id):
    if use_db():
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE line_user_id=%s", (line_user_id,))
                row = cur.fetchone()
                if row:
                    return normalize_user(row)
                u = default_user(line_user_id)
                cur.execute("""
                INSERT INTO users (
                    line_user_id, trial_started_at, trial_end_at, current_road, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                RETURNING *;
                """, (
                    line_user_id,
                    u["trial_started_at"].replace(tzinfo=None),
                    u["trial_end_at"].replace(tzinfo=None),
                    json.dumps([]),
                    u["created_at"].replace(tzinfo=None),
                    u["updated_at"].replace(tzinfo=None),
                ))
                row = cur.fetchone()
            conn.commit()
            return normalize_user(row)
    if line_user_id not in MEMORY_USERS:
        MEMORY_USERS[line_user_id] = default_user(line_user_id)
    return MEMORY_USERS[line_user_id]

def get_user(line_user_id):
    if use_db():
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE line_user_id=%s", (line_user_id,))
                return normalize_user(cur.fetchone())
    return MEMORY_USERS.get(line_user_id)

def update_user(line_user_id, **fields):
    user = ensure_user(line_user_id)
    user.update(fields)
    user["updated_at"] = now_tw()

    if use_db():
        allowed = [
            "bound_account", "vip_expire_at", "trial_started_at", "trial_end_at",
            "trial_expired_notice_sent", "current_road", "high_count", "low_count",
            "tie_count", "banker_pair_count", "player_pair_count",
            "pending_flow", "imported_ready", "analysis_active", "point_range",
            "play_mode", "target_profit", "last_prediction", "round_win", "round_loss",
            "win_streak", "loss_streak", "max_win_streak", "max_loss_streak", "updated_at"
        ]
        set_parts = []
        values = []
        for k in allowed:
            if k in user:
                if k == "current_road":
                    set_parts.append(f"{k}=%s::jsonb")
                    values.append(json.dumps(user[k], ensure_ascii=False))
                elif isinstance(user[k], datetime):
                    set_parts.append(f"{k}=%s")
                    values.append(user[k].replace(tzinfo=None))
                else:
                    set_parts.append(f"{k}=%s")
                    values.append(user[k])
        values.append(line_user_id)
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE users SET {', '.join(set_parts)} WHERE line_user_id=%s RETURNING *", values)
                row = cur.fetchone()
            conn.commit()
            return normalize_user(row)
    MEMORY_USERS[line_user_id] = user
    return user

def find_user_by_account(account):
    if not account:
        return None
    if use_db():
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE bound_account=%s", (account,))
                return normalize_user(cur.fetchone())
    for u in MEMORY_USERS.values():
        if u.get("bound_account") == account:
            return u
    return None

def add_log(line_user_id, predicted, actual, hit):
    if use_db():
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                    CREATE TABLE IF NOT EXISTS analysis_logs (
                        id SERIAL PRIMARY KEY,
                        line_user_id TEXT,
                        predicted TEXT,
                        actual TEXT,
                        hit BOOLEAN,
                        created_at TIMESTAMP NOT NULL DEFAULT NOW()
                    );
                    """)

                    cur.execute("""
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_name = 'analysis_logs'
                    ORDER BY ordinal_position
                    """)
                    columns_info = cur.fetchall()

                    known_values = {
                        "line_user_id": line_user_id,
                        "predicted": predicted,
                        "actual": actual,
                        "hit": hit,
                        "created_at": now_tw().replace(tzinfo=None),
                        "banker_pct": 0,
                        "player_pct": 0,
                        "confidence": 0,
                        "risk_score": 0,
                        "point_low": 0,
                        "point_high": 0,
                    }

                    cols = []
                    vals = []

                    for info in columns_info:
                        col = info["column_name"]
                        dtype = (info.get("data_type") or "").lower()
                        nullable = info.get("is_nullable")
                        default = info.get("column_default")

                        if col == "id":
                            continue

                        if col in known_values:
                            cols.append(col)
                            vals.append(known_values[col])
                            continue

                        if nullable == "NO" and default is None:
                            cols.append(col)
                            if any(x in dtype for x in ["int", "numeric", "double", "real", "decimal"]):
                                vals.append(0)
                            elif "bool" in dtype:
                                vals.append(False)
                            elif "timestamp" in dtype or "date" in dtype:
                                vals.append(now_tw().replace(tzinfo=None))
                            else:
                                vals.append("")

                    if cols:
                        placeholders = ", ".join(["%s"] * len(cols))
                        cur.execute(
                            f"INSERT INTO analysis_logs ({', '.join(cols)}) VALUES ({placeholders})",
                            vals
                        )
                conn.commit()
        except Exception as e:
            print("ADD_LOG_SKIPPED:", repr(e), flush=True)
        return

    MEMORY_LOGS.append({
        "line_user_id": line_user_id,
        "predicted": predicted,
        "actual": actual,
        "hit": hit,
        "created_at": now_tw()
    })

def recent_hit_rate(line_user_id, limit=30):
    if use_db():
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                SELECT hit FROM analysis_logs
                WHERE line_user_id=%s AND hit IS NOT NULL
                ORDER BY id DESC LIMIT %s
                """, (line_user_id, limit))
                rows = cur.fetchall()
        vals = [r["hit"] for r in rows]
    else:
        rows = [x for x in MEMORY_LOGS if x["line_user_id"] == line_user_id and x["hit"] is not None]
        vals = [x["hit"] for x in rows[-limit:]]
    if not vals:
        return "近30筆回測：尚無資料"
    rate = round(sum(1 for x in vals if x) * 100 / len(vals))
    return f"近{len(vals)}筆回測命中：{rate}%"

# =========================
# Access
# =========================
def is_vip(user):
    return bool(user and user.get("vip_expire_at") and user["vip_expire_at"] > now_tw())

def in_trial(user):
    return bool(user and user.get("trial_end_at") and user["trial_end_at"] > now_tw())

def has_full_access(user):
    return is_vip(user) or in_trial(user)

def check_trial_transition(user):
    if not user or is_vip(user):
        return user, False
    ended = bool(user.get("trial_end_at") and user["trial_end_at"] <= now_tw())
    if ended and not user.get("trial_expired_notice_sent"):
        user = update_user(user["line_user_id"], trial_expired_notice_sent=True)
        return user, True
    return user, False

# =========================
# LINE helpers
# =========================
def verify_signature(req):
    if not CHANNEL_SECRET:
        return True
    signature = req.headers.get("X-Line-Signature", "")
    body = req.get_data(as_text=True)
    digest = hmac.new(CHANNEL_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(signature, expected)

def quick_reply(items):
    return {
        "items": [
            {"type": "action", "action": {"type": "message", "label": label[:20], "text": text}}
            for label, text in items[:13]
        ]
    }

def reply_text(reply_token, text, quick_items=None):
    if not reply_token:
        return
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text[:4900]}]
    }
    if quick_items:
        payload["messages"][0]["quickReply"] = quick_reply(quick_items)
    try:
        requests.post(
            LINE_REPLY_API,
            headers={
                "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=8
        )
    except Exception as e:
        print("LINE reply error:", e)

def qr_main(is_admin=False):
    items = [
        ("點數配置", "點數配置"),
        ("綁定帳號", "綁定帳號"),
        ("功能介紹", "功能介紹"),
        ("找管理員", "找管理員"),
        ("會員說明", "會員說明"),
        ("查詢資格", "查詢資格"),
    ]
    if is_admin:
        items.append(("待開通", "/待開通"))
    return items

def qr_point_ranges():
    return [
        ("1000以下", "1000點以下"),
        ("1000-3000", "1000～3000點"),
        ("3000-5000", "3000～5000點"),
        ("5000-10000", "5000～10000點"),
        ("10000-30000", "10000～30000點"),
        ("30000以上", "30000點以上"),
    ]

def qr_modes():
    return [("保守", "保守"), ("標準", "標準"), ("積極", "積極"), ("極限", "極限")]

def qr_targets():
    return [("30%", "30%"), ("50%", "50%"), ("100%", "100%")]

def qr_analysis():
    return [
        ("莊-高", "莊-高"),
        ("莊-低", "莊-低"),
        ("閒-高", "閒-高"),
        ("閒-低", "閒-低"),
        ("和", "和"),
        ("莊對", "莊對"),
        ("閒對", "閒對"),
        ("詳細分析", "詳細分析"),
        ("結束分析", "結束分析"),
    ]

def qr_import_road():
    return [
        ("莊", "匯入莊"),
        ("閒", "匯入閒"),
        ("和", "匯入和"),
        ("莊對", "匯入莊對"),
        ("閒對", "匯入閒對"),
        ("完成匯入", "完成匯入"),
        ("清空重來", "清空匯入"),
    ]

def import_status_text(user, last_note=""):
    road = user.get("current_road", [])
    main_count = len(main_only(road))
    tie_count = user.get("tie_count", 0) or 0
    banker_pair_count = user.get("banker_pair_count", 0) or 0
    player_pair_count = user.get("player_pair_count", 0) or 0

    note = ""
    if last_note:
        note = f"已記錄：{last_note}\n\n"

    recent = road_to_text(road, 30) or "尚未輸入"

    return (
        f"{note}📥 按鈕匯入牌路中\n\n"
        f"主路進度：{main_count} / {MIN_ROAD_LEN}\n"
        f"和局：{tie_count}\n"
        f"莊對：{banker_pair_count}\n"
        f"閒對：{player_pair_count}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"最近牌路：\n{recent}\n\n"
        "操作方式：\n"
        "這把開閒+莊對 → 先按【莊/閒/和】再按【莊對/閒對】\n"
        "滿15把莊閒主路後，按【完成匯入】。"
    )
# =========================
# Text templates
# =========================
def feature_intro_text():
    return (
        "📊 功能介紹\n\n"
        "本系統提供牌路紀錄與即時分析功能。\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "▍序列比對\n"
        "依據歷史牌路進行相似結構分析\n\n"
        "▍四路觀察\n"
        "大路 / 大眼仔 / 小路 / 曱甴路\n"
        "同步查看變化與穩定度\n\n"
        "▍多模型投票\n"
        "整合序列、尾段、交錯率、長龍衰減與反連續校正\n\n"
        "▍和局 / 3寶副模型\n"
        "偵測長龍第7顆、和後連動、莊對 / 閒對活躍度\n\n"
        "▍點數配置\n"
        "依點數區間、打法模式、期望獲利與風險係數動態調整\n\n"
        "▍高低牌權重（選用）\n"
        "可用莊-高 / 莊-低 / 閒-高 / 閒-低快速記錄牌值結構\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "系統會提供：\n"
        "👉 當前方向參考\n"
        "👉 和局 / 3寶觀察\n"
        "👉 系統狀態\n"
        "👉 風險提示\n"
        "👉 點數配置建議\n"
    )

def member_guide_text():
    return (
        "📘 使用教學\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "① 註冊帳號\n"
        "請先完成註冊：\n"
        "sn043.aaawin88.com\n\n"
        "（已有帳號請跳至第二步）\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "② 綁定帳號\n"
        "點選【綁定帳號】\n"
        "或輸入：綁定 ck76888\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "③ 找管理員開通帳號\n"
        "完成綁定後請聯絡管理員開通權限\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "④ 點數配置\n"
        "選擇：點數區間 → 打法模式 → 期望獲利\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "⑤ 匯入牌路\n"
        "輸入目前牌路，至少15把\n"
        "例：莊莊閒閒莊閒莊閒莊莊閒閒莊閒莊\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "⑥ 開始分析\n"
        "輸入【開始分析】進入即時模式\n"
        "每把輸入：莊 / 閒 / 和\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "⑦ 高低牌與對子\n"
        "即時分析時可點選：\n"
        "莊-高 / 莊-低 / 閒-高 / 閒-低 / 和 / 莊對 / 閒對\n\n"
        "說明：0～5 視為低牌；6～9 視為高牌\n"
        "若不確定高低，也可只輸入：莊 / 閒 / 和\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "⑧ 詳細分析\n"
        "查看序列、四路、穩定度、和局3寶、點數與本輪紀錄\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "⑨ 結束分析\n"
        "輸入【結束分析】查看本輪結果並清空本輪"
    )

def open_full_access_text():
    return (
        "👤 找管理員\n\n"
        "如需開通完整功能，請先完成：\n\n"
        "① 註冊帳號\n"
        "sn043.aaawin88.com\n\n"
        "② 綁定帳號\n"
        "點選【綁定帳號】或輸入：綁定 你的帳號\n\n"
        "③ 聯繫管理員\n"
        "請點以下連結直接聯繫：\n"
        "https://line.me/R/ti/p/@163brkzi\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "開通後即可使用：\n"
        "✔ 即時分析\n"
        "✔ 和局 / 3寶觀察\n"
        "✔ 點數配置\n"
        "✔ 詳細分析\n"
        "✔ 本輪結算"
    )

def trial_expired_text():
    return (
        "⏰ 試用時間已結束\n\n"
        "剛剛的牌路分析系統已完成本局判讀\n"
        "目前完整分析已暫停顯示\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "本局已進入關鍵結構區\n"
        "但方向、3寶觀察與點數配置已鎖定\n\n"
        "👉 需開通後查看\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "如需繼續使用完整分析\n"
        "請點選【找管理員】開通"
    )

def free_user_locked_text():
    return (
        "📊 已完成牌路解析\n\n"
        "系統已建立本局結構判讀\n"
        "包含：\n"
        "▍序列比對\n"
        "▍四路結構\n"
        "▍穩定度分析\n"
        "▍和局 / 3寶偵測\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "⚠️ 關鍵判讀已鎖定\n"
        "目前為完整分析內容\n\n"
        "👉 方向 / 3寶 / 狀態 / 點數配置\n"
        "需開通後查看\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "如需繼續使用\n"
        "請點選【找管理員】開通"
    )

def get_status_text(user):
    if is_vip(user):
        days = minutes_left(user["vip_expire_at"]) // 1440
        return f"目前狀態：VIP\n到期時間：{dt_to_str(user['vip_expire_at'])}\n剩餘：約 {days} 天"
    if in_trial(user):
        mins = minutes_left(user["trial_end_at"])
        return f"目前狀態：3小時免費試用中\n剩餘：約 {mins} 分鐘\n試用期間可使用完整分析。"
    return (
        "目前狀態：免費會員\n\n"
        "完整分析內容已鎖定。\n"
        "如需方向、3寶觀察、詳細分析與點數配置，請點選【找管理員】開通。"
    )

def menu_text(user):
    if is_vip(user):
        tag = "VIP會員"
    elif in_trial(user):
        tag = "3小時免費試用中"
    else:
        tag = "免費會員"
    return (
        f"歡迎使用 AI 百家方向判讀系統（{tag}）\n\n"
        "建議流程：\n"
        "1. 註冊帳號\n"
        "2. 綁定帳號\n"
        "3. 點數配置\n"
        "4. 匯入牌路\n"
        "5. 開始分析\n"
        "6. 結束分析\n\n"
        "請從下方按鈕開始。"
    )

# =========================
# Analysis V14
# =========================
def extract_results(text):
    return re.findall(r"[莊閒和]", text)

def extract_main_results(text):
    return [x for x in extract_results(text) if x in ["莊", "閒"]]

def calc_segments(seq):
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

def current_streak(seq):
    if not seq:
        return 0, None
    last = seq[-1]
    n = 1
    for i in range(len(seq)-2, -1, -1):
        if seq[i] == last:
            n += 1
        else:
            break
    return n, last

def chop_rate(seq):
    if len(seq) < 2:
        return 0
    switches = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i-1])
    return round(switches * 100 / (len(seq)-1))

def sequence_match_score(seq, max_k=6):
    score = {"莊": 0.0, "閒": 0.0}
    details = []
    n = len(seq)
    if n < 8:
        return score, "資料不足", details

    for k in range(min(max_k, n-1), 2, -1):
        tail = seq[-k:]
        b = p = 0
        for i in range(0, n-k):
            if seq[i:i+k] == tail and i+k < n:
                nxt = seq[i+k]
                if nxt in ["莊", "閒"]:
                    if nxt == "莊":
                        b += 1
                    else:
                        p += 1
        total = b + p
        if total:
            weight = k / max_k
            score["莊"] += (b / total) * weight
            score["閒"] += (p / total) * weight
            details.append(f"{k}碼匹配：莊{b} / 閒{p}")

    if not details:
        return score, "尾段暫無明顯重複樣本", details
    return score, "找到尾段相似樣本", details

def subroad_model(seq):
    segs = calc_segments(seq)
    if len(segs) < 6:
        return {"big_eye": "灰", "small": "灰", "cockroach": "灰", "stability": 40, "note": "樣本偏少"}

    lens = [x[1] for x in segs[-8:]]
    changes = sum(1 for i in range(1, len(lens)) if lens[i] != lens[i-1])
    avg_len = sum(lens) / len(lens)
    cr = chop_rate(seq[-20:])
    stability = 50
    if avg_len >= 2.2:
        stability += 12
    if cr < 45:
        stability += 10
    if changes > 5:
        stability -= 12
    stability = max(20, min(85, round(stability)))

    big_eye = "紅" if avg_len >= 2 else "藍"
    small = "紅" if changes <= 4 else "藍"
    cockroach = "紅" if cr < 55 else "藍"
    note = f"段長{avg_len:.2f}，交錯率{cr}%"
    return {"big_eye": big_eye, "small": small, "cockroach": cockroach, "stability": stability, "note": note}

def high_low_weight(user):
    high = user.get("high_count", 0) or 0
    low = user.get("low_count", 0) or 0
    total = high + low
    if total < 3:
        return 1.0, "尚未建立牌值樣本", high, low
    hr = high / total
    lr = low / total
    if hr >= 0.58:
        return 1.12, "高牌偏多，結構偏穩", high, low
    if lr >= 0.58:
        return 0.86, "低牌偏多，波動偏高", high, low
    return 1.0, "高低牌均衡", high, low

def last_raw_result(road):
    for x in reversed(road):
        if x in ["莊", "閒", "和"]:
            return x
    return None

def treasure_model(user):
    """
    和局 / 3寶副模型 V15。
    核心：和、莊對、閒對分開計分，只顯示最有價值的1～2個觀察點。
    3寶 = 和 / 莊對 / 閒對。
    """
    road = user.get("current_road", [])
    seq = main_only(road)

    if len(seq) < MIN_ROAD_LEN:
        return None

    streak_len, last = current_streak(seq)
    raw_last = last_raw_result(road)

    tie_count = user.get("tie_count", 0) or 0
    banker_pair_count = user.get("banker_pair_count", 0) or 0
    player_pair_count = user.get("player_pair_count", 0) or 0

    recent_raw = road[-12:]
    recent_ties = sum(1 for x in recent_raw if x == "和")
    pair_total = banker_pair_count + player_pair_count

    scores = {
        "和": 0,
        "莊對": 0,
        "閒對": 0,
    }
    reasons = {
        "和": [],
        "莊對": [],
        "閒對": [],
    }

    # 1. 長龍第7顆：只加和局，不亂加對子
    if streak_len >= 6:
        scores["和"] += 3
        reasons["和"].append(f"長龍已達{streak_len}顆，第7顆附近有和局觀察點")

    # 2. 和後擴散：和 / 莊對 / 閒對都提高，但不代表一定三個都喊
    if raw_last == "和":
        scores["和"] += 2
        scores["莊對"] += 2
        scores["閒對"] += 2
        reasons["和"].append("上一把開和，和局活躍度提高")
        reasons["莊對"].append("和後進入3寶擴散區")
        reasons["閒對"].append("和後進入3寶擴散區")

    # 3. 近12把和局密度
    if recent_ties >= 2:
        scores["和"] += 2
        reasons["和"].append(f"近12把出現{recent_ties}次和")
    if recent_ties >= 3:
        scores["和"] += 1
        reasons["和"].append("和局密度偏高，但只建議小注觀察")

    # 4. 對子熱度分流：哪邊熱就加哪邊，不再同時亂喊
    if banker_pair_count > player_pair_count and banker_pair_count >= 2:
        scores["莊對"] += 3
        reasons["莊對"].append("本輪莊對比閒對活躍")
    elif player_pair_count > banker_pair_count and player_pair_count >= 2:
        scores["閒對"] += 3
        reasons["閒對"].append("本輪閒對比莊對活躍")
    elif banker_pair_count >= 1 and player_pair_count >= 1:
        scores["莊對"] += 1
        scores["閒對"] += 1
        reasons["莊對"].append("本輪已有莊對訊號")
        reasons["閒對"].append("本輪已有閒對訊號")

    # 5. 長龍方向搭配對子：長莊偏看莊對，長閒偏看閒對，但權重低於和局
    if streak_len >= 5 and last == "莊":
        scores["莊對"] += 1
        reasons["莊對"].append("莊路長段，莊對小幅加權")
    elif streak_len >= 5 and last == "閒":
        scores["閒對"] += 1
        reasons["閒對"].append("閒路長段，閒對小幅加權")

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_name, top_score = ranked[0]

    if top_score < 3:
        return None

    second_name, second_score = ranked[1]
    picks = [top_name]
    if second_score >= 3 and second_score >= top_score - 1:
        picks.append(second_name)

    if top_score >= 6:
        level = "強觀察"
    elif top_score >= 4:
        level = "中觀察"
    else:
        level = "弱觀察"

    reason_lines = []
    for name in picks:
        for r in reasons[name]:
            if r not in reason_lines:
                reason_lines.append(f"{name}：{r}")

    if top_name == "和" and streak_len >= 6:
        note = "長龍第7顆附近，主看和局小注觀察。"
    elif raw_last == "和":
        note = "和後進入3寶擴散區，只看分數最高項目。"
    elif top_name in ["莊對", "閒對"]:
        note = f"{top_name}訊號較活躍，可小注觀察。"
    else:
        note = "3寶訊號出現，但不可重壓。"

    return {
        "level": level,
        "target": " / ".join(picks),
        "main_pick": top_name,
        "sub_pick": second_name if len(picks) > 1 else None,
        "score": top_score,
        "scores": scores,
        "note": note,
        "reasons": reason_lines,
        "tie_count": tie_count,
        "banker_pair_count": banker_pair_count,
        "player_pair_count": player_pair_count,
        "recent_ties": recent_ties,
    }

def analyze_v13(user):
    road = user.get("current_road", [])
    seq = main_only(road)
    if len(seq) < MIN_ROAD_LEN:
        return {"error": f"請至少輸入{MIN_ROAD_LEN}把，目前{len(seq)}把"}

    score = {"莊": 50.0, "閒": 50.0}
    reasons = []

    c = Counter(seq)
    total = c["莊"] + c["閒"]
    freq_b = c["莊"] / max(1, total)
    freq_p = c["閒"] / max(1, total)
    score["莊"] += (freq_b - 0.5) * 12
    score["閒"] += (freq_p - 0.5) * 12

    sm, sm_note, sm_details = sequence_match_score(seq)
    score["莊"] += sm["莊"] * 8
    score["閒"] += sm["閒"] * 8
    reasons.append(sm_note)

    streak_len, last = current_streak(seq)
    if last:
        if streak_len <= 3:
            score[last] += 4 + streak_len
            tail_note = f"尾段{streak_len}連{last}，允許延續"
        elif streak_len == 4:
            score[last] += 1
            other = "閒" if last == "莊" else "莊"
            score[other] += 3
            tail_note = f"尾段4連{last}，轉折風險提高"
        else:
            other = "閒" if last == "莊" else "莊"
            score[last] -= 2
            score[other] += 6
            tail_note = f"尾段{streak_len}連{last}，長龍衰減啟動"
    else:
        tail_note = "尾段不足"

    cr = chop_rate(seq[-25:])
    if cr >= 62:
        if last:
            other = "閒" if last == "莊" else "莊"
            score[other] += 4
        structure_note = f"交錯率{cr}%，跳動偏高"
    elif cr <= 38:
        if last:
            score[last] += 3
        structure_note = f"交錯率{cr}%，連續結構偏明顯"
    else:
        structure_note = f"交錯率{cr}%，中性"

    sub = subroad_model(seq)
    if sub["stability"] >= 60 and last:
        score[last] += 4
    elif sub["stability"] < 40 and last:
        other = "閒" if last == "莊" else "莊"
        score[other] += 3

    raw_gap = abs(score["莊"] - score["閒"])
    if score["莊"] >= score["閒"]:
        direction = "莊"
    else:
        direction = "閒"

    pct = 50 + min(18, round(raw_gap / 2))
    if pct > 68:
        pct = 68

    if pct >= 64:
        signal = "強"
    elif pct >= 59:
        signal = "中強"
    elif pct >= 55:
        signal = "中"
    else:
        signal = "弱"

    risk_score = 0
    if sub["stability"] < 40:
        risk_score += 2
    if streak_len >= 5:
        risk_score += 2
    if cr >= 65:
        risk_score += 1
    if pct <= 54:
        risk_score += 2

    if risk_score >= 5:
        risk = "高"
    elif risk_score >= 3:
        risk = "中高"
    elif risk_score >= 1:
        risk = "中"
    else:
        risk = "低"

    if pct <= 53 and sub["stability"] < 38 and not sm_details:
        state = "⛔ 暫停"
        state_coef = 0.0
    elif risk == "高":
        state = "🧊 保守啟動"
        state_coef = 0.45
    elif risk == "中高":
        state = "⚠️ 降速"
        state_coef = 0.6
    elif signal in ["強", "中強"]:
        state = "🔥 進攻中"
        state_coef = 1.35
    elif signal == "中":
        state = "✅ 可啟動"
        state_coef = 1.0
    else:
        state = "👀 觀察低區間"
        state_coef = 0.7

    hl_coef, hl_note, high, low = high_low_weight(user)
    treasure = treasure_model(user)

    return {
        "direction": direction,
        "pct": pct,
        "banker_pct": pct if direction == "莊" else 100 - pct,
        "player_pct": pct if direction == "閒" else 100 - pct,
        "signal": signal,
        "risk": risk,
        "state": state,
        "state_coef": state_coef,
        "streak_len": streak_len,
        "tail_note": tail_note,
        "structure_note": structure_note,
        "sub": sub,
        "match_note": sm_note,
        "match_details": sm_details,
        "treasure": treasure,
        "metrics": {
            "莊": c["莊"],
            "閒": c["閒"],
            "和": sum(1 for x in road if x == "和"),
            "交錯率": cr,
            "平均段長": round(sum(x[1] for x in calc_segments(seq)) / max(1, len(calc_segments(seq))), 2),
            "最長連續": max([x[1] for x in calc_segments(seq)] or [0]),
        },
        "hl_coef": hl_coef,
        "hl_note": hl_note,
        "high": high,
        "low": low,
        "road_len": len(seq),
    }

# =========================
# Points
# =========================
def get_base_points(user):
    pr = user.get("point_range")
    mode = user.get("play_mode") or "標準"
    if pr not in POINT_CONFIG:
        return None
    return POINT_CONFIG[pr].get(mode, POINT_CONFIG[pr]["標準"])

def calculate_points(user, analysis):
    base = get_base_points(user)
    if not base:
        return None
    low, high = base
    target = user.get("target_profit") or "30%"
    target_mult = TARGET_PROFIT_MULT.get(target, 0.8)

    low = int(low * target_mult)
    high = int(high * target_mult)

    mode = user.get("play_mode") or "標準"
    state_coef = analysis["state_coef"]
    hl_coef = analysis["hl_coef"]

    # 保留原本激進版風控：連勝放大，連敗降速 / 暫停
    win_streak = user.get("win_streak", 0) or 0
    loss_streak = user.get("loss_streak", 0) or 0

    if loss_streak >= 3:
        return {"low": 0, "high": 0, "text": "⛔ 連續失利，暫停啟動", "coef_note": "連續失利"}
    if loss_streak == 2:
        state_coef *= 0.55
    if win_streak >= 3:
        state_coef *= 2.0 if mode == "極限" else 1.6
    elif win_streak == 2:
        state_coef *= 1.3

    final_low = int(low * state_coef * hl_coef)
    final_high = int(high * state_coef * hl_coef)

    if analysis["state_coef"] == 0:
        return {"low": 0, "high": 0, "text": "⛔ 暫停啟動", "coef_note": "狀態暫停"}

    if final_low <= 0 or final_high <= 0:
        return {"low": 0, "high": 0, "text": "⛔ 暫停啟動", "coef_note": "點數過低"}

    return {
        "low": max(1, final_low),
        "high": max(1, final_high),
        "text": f"{max(1, final_low)}～{max(1, final_high)}點",
        "coef_note": f"狀態係數{round(state_coef,2)} × 牌值係數{round(hl_coef,2)}",
    }

def point_config_card(user):
    pr = user.get("point_range")
    mode = user.get("play_mode")
    target = user.get("target_profit")

    if not pr:
        return "💰 點數配置\n\n請選擇點數區間。"
    if not mode:
        return f"💰 點數配置\n\n已選擇：{POINT_CONFIG[pr]['label']}\n請選擇打法模式。"
    if not target:
        return f"💰 點數配置\n\n區間：{POINT_CONFIG[pr]['label']}\n模式：{mode}\n請選擇期望獲利。"

    base = get_base_points(user)
    base_text = f"{base[0]}～{base[1]}點" if base else "尚未設定"
    tm = TARGET_PROFIT_MULT.get(target, 0.8)
    target_text = f"{int(base[0]*tm)}～{int(base[1]*tm)}點" if base else "尚未設定"

    return (
        "💰 點數配置完成\n\n"
        f"點數區間：{POINT_CONFIG[pr]['label']}\n"
        f"打法模式：{mode}\n"
        f"期望獲利：{target}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"基礎區間：{base_text}\n"
        f"期望倍率後：{target_text}\n\n"
        "系統會再依照：\n"
        "👉 狀態係數\n"
        "👉 風險係數\n"
        "👉 連勝 / 連敗\n"
        "👉 高低牌權重\n"
        "自動調整本輪點數。"
    )

# =========================
# Cards
# =========================
def decision_card(user, analysis):
    points = calculate_points(user, analysis)
    point_text = points["text"] if points else "尚未設定，請先點選【點數配置】"

    treasure = analysis.get("treasure")
    if treasure:
        treasure_text = (
            "\n━━━━━━━━━━━━━━━\n\n"
            "🟡 和局 / 3寶觀察\n"
            f"目標：{treasure['target']}\n"
            f"強度：{treasure['level']}\n"
            f"說明：{treasure['note']}\n"
            "⚠️ 僅小注觀察，不建議重壓\n"
        )
    else:
        treasure_text = ""

    return (
        f"已記錄：{road_to_text(user.get('current_road', [])[-1:])}\n\n"
        "🎯 方向\n\n"
        f"👉👉👉 {analysis['direction']} {analysis['pct']}% 👈👈👈\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "🧠 系統狀態\n"
        f"{analysis['state']}\n\n"
        "💰 點數\n"
        f"👉 {point_text}\n\n"
        f"⚠️ 風險：{analysis['risk']}\n"
        f"📌 建議模式：{user.get('play_mode') or '尚未設定'}\n"
        f"{treasure_text}\n"
        "━━━━━━━━━━━━━━━\n\n"
        "操作：莊-高 / 莊-低 / 閒-高 / 閒-低 / 和 / 莊對 / 閒對 / 詳細分析 / 結束分析"
    )

def detail_card(user, analysis):
    points = calculate_points(user, analysis)
    point_text = points["text"] if points else "尚未設定"
    details = "\n".join([f"・{x}" for x in analysis["match_details"]]) if analysis["match_details"] else "・目前無明顯重複樣本"
    total_round = (user.get("round_win", 0) or 0) + (user.get("round_loss", 0) or 0)
    rate = round((user.get("round_win", 0) or 0) * 100 / total_round) if total_round else 0

    treasure = analysis.get("treasure")
    if treasure:
        treasure_detail = (
            "▍和局 / 3寶副模型\n"
            f"目標：{treasure['target']}\n"
            f"強度：{treasure['level']}\n"
            f"分數：{treasure['score']}\n"
            f"本輪和局：{treasure['tie_count']}\n"
            f"莊對：{treasure['banker_pair_count']} / 閒對：{treasure['player_pair_count']}\n"
            + "\n".join([f"・{x}" for x in treasure["reasons"]])
            + "\n\n"
        )
    else:
        treasure_detail = (
            "▍和局 / 3寶副模型\n"
            f"本輪和局：{user.get('tie_count', 0) or 0}\n"
            f"莊對：{user.get('banker_pair_count', 0) or 0} / 閒對：{user.get('player_pair_count', 0) or 0}\n"
            "目前無明顯訊號\n\n"
        )

    return (
        "📊 詳細分析 V14\n\n"
        f"目前牌路：\n{road_to_text(main_only(user.get('current_road', [])), 30)}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "預測機率：\n"
        f"莊：{analysis['banker_pct']}%\n"
        f"閒：{analysis['player_pct']}%\n"
        f"信號強度：{analysis['signal']}\n"
        f"風險：{analysis['risk']}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "▍序列匹配\n"
        f"{analysis['match_note']}\n"
        f"{details}\n\n"
        "▍尾段動能\n"
        f"{analysis['tail_note']}\n\n"
        "▍結構判斷\n"
        f"{analysis['structure_note']}\n\n"
        "▍四路結構\n"
        f"大眼仔：{analysis['sub']['big_eye']}\n"
        f"小路：{analysis['sub']['small']}\n"
        f"曱甴路：{analysis['sub']['cockroach']}\n"
        f"穩定度：{analysis['sub']['stability']}\n"
        f"{analysis['sub']['note']}\n\n"
        f"{treasure_detail}"
        "▍牌值權重\n"
        f"{analysis['hl_note']}\n"
        f"高牌：{analysis['high']} / 低牌：{analysis['low']}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "數據指標：\n"
        f"莊：{analysis['metrics']['莊']}\n"
        f"閒：{analysis['metrics']['閒']}\n"
        f"和：{analysis['metrics']['和']}\n"
        f"交錯率：{analysis['metrics']['交錯率']}%\n"
        f"平均段長：{analysis['metrics']['平均段長']}\n"
        f"最長連續：{analysis['metrics']['最長連續']}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "💰 點數引擎\n"
        f"點數區間：{POINT_CONFIG.get(user.get('point_range'), {}).get('label', '尚未設定')}\n"
        f"打法模式：{user.get('play_mode') or '尚未設定'}\n"
        f"期望獲利：{user.get('target_profit') or '尚未設定'}\n"
        f"參考點數：{point_text}\n"
        f"{points['coef_note'] if points else ''}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "📈 本輪表現\n"
        f"本輪紀錄：{user.get('round_win', 0)}/{total_round}（{rate}%）\n"
        f"連續順利：{user.get('win_streak', 0)}\n"
        f"連續失利：{user.get('loss_streak', 0)}\n"
        f"最大連順：{user.get('max_win_streak', 0)}\n"
        f"最大連失：{user.get('max_loss_streak', 0)}\n"
        f"{recent_hit_rate(user['line_user_id'])}"
    )

def settlement_card(user):
    total = (user.get("round_win", 0) or 0) + (user.get("round_loss", 0) or 0)
    win = user.get("round_win", 0) or 0
    rate = round(win * 100 / total) if total else 0
    max_win = user.get("max_win_streak", 0) or 0
    max_loss = user.get("max_loss_streak", 0) or 0

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
        f"最大連失：{max_loss}\n"
        f"本輪和局：{user.get('tie_count', 0) or 0}\n"
        f"莊對：{user.get('banker_pair_count', 0) or 0} / 閒對：{user.get('player_pair_count', 0) or 0}\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "🧠 評價\n"
        f"{eval_text}\n\n"
        f"👉 建議下輪：{next_mode}"
    )

# =========================
# Route
# =========================
@app.route("/", methods=["GET"])
def home():
    return "OK - LINE Bot is running"

@app.route("/callback", methods=["POST"])
def callback():
    if not verify_signature(request):
        abort(400)

    data = request.get_json(silent=True) or {}
    events = data.get("events", [])

    for event in events:
        event_type = event.get("type")
        source = event.get("source", {})
        line_user_id = source.get("userId")
        reply_token = event.get("replyToken")

        if not line_user_id:
            continue

        user = ensure_user(line_user_id)
        is_admin = is_admin_line_id(line_user_id)

        if event_type == "follow":
            reply_text(
                reply_token,
                menu_text(user) + "\n\n🎁 已開啟3小時免費試用\n試用期間可使用完整分析功能。",
                quick_items=qr_main(is_admin),
            )
            continue

        if event_type != "message":
            continue

        msg = event.get("message", {})
        if msg.get("type") != "text":
            reply_text(reply_token, "目前僅支援文字輸入。", quick_items=qr_main(is_admin))
            continue

        text = msg.get("text", "").strip()
        user, trial_just_expired = check_trial_transition(user)

        # Admin
        if text == "/待開通":
            if not is_admin:
                reply_text(reply_token, "此指令僅限管理員。")
                continue
            if use_db():
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                        SELECT bound_account, line_user_id, created_at
                        FROM users
                        WHERE vip_expire_at IS NULL
                        ORDER BY created_at DESC
                        LIMIT 20
                        """)
                        rows = cur.fetchall()
                if not rows:
                    reply_text(reply_token, "目前沒有待開通名單。")
                else:
                    lines = ["待開通名單："]
                    for r in rows:
                        lines.append(f"帳號：{r.get('bound_account') or '未綁定'} / USER：{r.get('line_user_id')}")
                    reply_text(reply_token, "\n".join(lines))
            else:
                lines = ["待開通名單："]
                for u in MEMORY_USERS.values():
                    if not is_vip(u):
                        lines.append(f"帳號：{u.get('bound_account') or '未綁定'} / USER：{u.get('line_user_id')}")
                reply_text(reply_token, "\n".join(lines[:25]))
            continue

        if text.startswith("/vip"):
            if not is_admin:
                reply_text(reply_token, "此指令僅限管理員。")
                continue
            parts = text.split()
            if len(parts) < 3:
                reply_text(reply_token, "用法：/vip 遊戲帳號 30\n例：/vip ck76888 30")
                continue
            account = parts[1]
            try:
                days = int(parts[2])
            except ValueError:
                reply_text(reply_token, "天數需為數字。")
                continue
            target = find_user_by_account(account)
            if not target:
                reply_text(reply_token, f"找不到帳號：{account}\n請會員先綁定帳號。")
                continue
            expire = now_tw() + timedelta(days=days)
            update_user(target["line_user_id"], vip_expire_at=expire)
            reply_text(reply_token, f"已開通VIP\n帳號：{account}\n天數：{days}天\n到期：{dt_to_str(expire)}")
            continue

        # General commands
        if text == "開始":
            reply_text(reply_token, menu_text(user), quick_items=qr_main(is_admin))
            continue

        if text == "功能介紹":
            reply_text(reply_token, feature_intro_text(), quick_items=qr_main(is_admin))
            continue

        if text in ["會員說明", "使用教學", "開通教學"]:
            reply_text(reply_token, member_guide_text(), quick_items=qr_main(is_admin))
            continue

        if text in ["找管理員", "開通", "VIP", "開通完整功能"]:
            reply_text(reply_token, open_full_access_text(), quick_items=qr_main(is_admin))
            continue

        if text == "查詢資格":
            reply_text(reply_token, get_status_text(user), quick_items=qr_main(is_admin))
            continue

        if text == "綁定帳號":
            update_user(line_user_id, pending_flow="bind_account")
            reply_text(reply_token, "請輸入你的遊戲帳號。\n例：ck76888")
            continue

        if text.startswith("綁定 "):
            account = text.replace("綁定 ", "", 1).strip()
            if not account:
                reply_text(reply_token, "請輸入帳號。\n例：綁定 ck76888")
                continue
            user = update_user(line_user_id, bound_account=account, pending_flow=None)
            reply_text(reply_token, f"已綁定帳號：{account}\n請聯絡管理員確認開通。", quick_items=qr_main(is_admin))
            continue

        if user.get("pending_flow") == "bind_account":
            if len(text) < 3 or any(x in text for x in ["開始", "分析", "會員說明"]):
                reply_text(reply_token, "帳號格式看起來不完整，請重新輸入遊戲帳號。")
                continue
            user = update_user(line_user_id, bound_account=text, pending_flow=None)
            reply_text(reply_token, f"已綁定帳號：{text}\n請聯絡管理員確認開通。", quick_items=qr_main(is_admin))
            continue

        # Point config
        if text == "點數配置":
            if not has_full_access(user):
                reply_text(reply_token, free_user_locked_text(), quick_items=qr_main(is_admin))
                continue
            update_user(line_user_id, pending_flow="point_range")
            reply_text(reply_token, "💰 點數配置\n\n請選擇你的點數區間。", quick_items=qr_point_ranges())
            continue

        if user.get("pending_flow") == "point_range" and text in POINT_RANGE_INPUT_MAP:
            key = POINT_RANGE_INPUT_MAP[text]
            update_user(line_user_id, point_range=key, pending_flow="play_mode")
            reply_text(reply_token, f"已選擇：{POINT_CONFIG[key]['label']}\n\n請選擇打法模式。", quick_items=qr_modes())
            continue

        if user.get("pending_flow") == "play_mode" and text in PLAY_MODES:
            update_user(line_user_id, play_mode=text, pending_flow="target_profit")
            reply_text(reply_token, f"已選擇：{text}\n\n請選擇期望獲利。", quick_items=qr_targets())
            continue

        if user.get("pending_flow") == "target_profit" and text in TARGET_PROFIT_MULT:
            user = update_user(line_user_id, target_profit=text, pending_flow=None)
            reply_text(reply_token, point_config_card(user), quick_items=[("匯入牌路", "匯入牌路"), ("開始分析", "開始分析")])
            continue

        if text == "查詢配置":
            reply_text(reply_token, point_config_card(user), quick_items=qr_main(is_admin))
            continue

        # Access lock
        full_access_commands = ["開始分析", "詳細分析", "莊", "閒", "和", "高", "低", "莊對", "閒對"]
        if trial_just_expired and text in full_access_commands:
            reply_text(reply_token, trial_expired_text(), quick_items=qr_main(is_admin))
            continue
        if not has_full_access(user) and text in full_access_commands:
            reply_text(reply_token, free_user_locked_text(), quick_items=qr_main(is_admin))
            continue

        # Import flow
        if text == "匯入牌路":
            user = update_user(
                line_user_id,
                pending_flow="import_buttons",
                current_road=[],
                imported_ready=False,
                analysis_active=False,
                high_count=0,
                low_count=0,
                tie_count=0,
                banker_pair_count=0,
                player_pair_count=0,
                last_prediction=None,
                round_win=0,
                round_loss=0,
                win_streak=0,
                loss_streak=0,
                max_win_streak=0,
                max_loss_streak=0,
            )
            reply_text(reply_token, import_status_text(user), quick_items=qr_import_road())
            continue

        if user.get("pending_flow") == "import_buttons" and text in ["匯入莊", "匯入閒", "匯入和", "匯入莊對", "匯入閒對", "完成匯入", "清空匯入"]:
            road = user.get("current_road", [])

            if text == "清空匯入":
                user = update_user(
                    line_user_id,
                    current_road=[],
                    high_count=0,
                    low_count=0,
                    tie_count=0,
                    banker_pair_count=0,
                    player_pair_count=0,
                    imported_ready=False,
                    analysis_active=False,
                )
                reply_text(reply_token, import_status_text(user, "已清空，重新開始"), quick_items=qr_import_road())
                continue

            if text == "完成匯入":
                main_count = len(main_only(road))
                if main_count < MIN_ROAD_LEN:
                    reply_text(
                        reply_token,
                        f"目前莊閒主路只有{main_count}把，至少需要{MIN_ROAD_LEN}把。\n"
"
                        "和局會記錄，但不計入莊閒主路把數。",
                        quick_items=qr_import_road()
                    )
                    continue
                user = update_user(line_user_id, imported_ready=True, analysis_active=False, pending_flow=None)
                reply_text(
                    reply_token,
                    "✅ 匯入完成\n\n"
f"主路：{road_to_text(user.get('current_road', []), 40)}\n\n"
f"莊閒主路：{main_count}把\n"
f"和局：{user.get('tie_count', 0) or 0}\n"
f"莊對：{user.get('banker_pair_count', 0) or 0}\n"
f"閒對：{user.get('player_pair_count', 0) or 0}\n\n"
"請點選【開始分析】。"
                    quick_items=[("開始分析", "開始分析"), ("點數配置", "點數配置")]
                )
                continue

            if text == "匯入莊":
                road.append("莊")
                user = update_user(line_user_id, current_road=road)
                reply_text(reply_token, import_status_text(user, "莊"), quick_items=qr_import_road())
                continue

            if text == "匯入閒":
                road.append("閒")
                user = update_user(line_user_id, current_road=road)
                reply_text(reply_token, import_status_text(user, "閒"), quick_items=qr_import_road())
                continue

            if text == "匯入和":
                road.append("和")
                user = update_user(line_user_id, current_road=road, tie_count=(user.get("tie_count", 0) or 0) + 1)
                reply_text(reply_token, import_status_text(user, "和"), quick_items=qr_import_road())
                continue

            if text == "匯入莊對":
                user = update_user(line_user_id, banker_pair_count=(user.get("banker_pair_count", 0) or 0) + 1)
                reply_text(reply_token, import_status_text(user, "莊對"), quick_items=qr_import_road())
                continue

            if text == "匯入閒對":
                user = update_user(line_user_id, player_pair_count=(user.get("player_pair_count", 0) or 0) + 1)
                reply_text(reply_token, import_status_text(user, "閒對"), quick_items=qr_import_road())
                continue

        if user.get("pending_flow") == "import_road":
            results = extract_results(text)
            main = [x for x in results if x in ["莊", "閒"]]
            if len(main) < MIN_ROAD_LEN:
                reply_text(reply_token, f"目前只有{len(main)}把莊閒主路，請至少輸入{MIN_ROAD_LEN}把。
建議改用【匯入牌路】按鈕模式，避免莊對/閒對被誤判。")
                continue
            user = update_user(
                line_user_id,
                current_road=results,
                imported_ready=True,
                analysis_active=False,
                pending_flow=None,
                round_win=0,
                round_loss=0,
                win_streak=0,
                loss_streak=0,
                max_win_streak=0,
                max_loss_streak=0,
                last_prediction=None,
                high_count=0,
                low_count=0,
                tie_count=sum(1 for x in results if x == "和"),
                banker_pair_count=0,
                player_pair_count=0,
            )
            reply_text(reply_token, f"已匯入牌路：莊閒主路{len(main)}把 / 和局{sum(1 for x in results if x == '和')}把
請點選【開始分析】。", quick_items=[("開始分析", "開始分析"), ("點數配置", "點數配置")])
            continue

        if text == "開始分析":
            road = user.get("current_road", [])
            if len(main_only(road)) < MIN_ROAD_LEN:
                user = update_user(line_user_id, pending_flow="import_buttons")
                reply_text(reply_token, f"請先匯入至少{MIN_ROAD_LEN}把莊閒主路。
和局可記錄，但不計入主路把數。", quick_items=qr_import_road())
                continue
            user = update_user(line_user_id, analysis_active=True)
            analysis = analyze_v13(user)
            if "error" in analysis:
                reply_text(reply_token, analysis["error"])
                continue
            reply_text(reply_token, decision_card(user, analysis), quick_items=qr_analysis())
            continue

        if text == "詳細分析":
            analysis = analyze_v13(user)
            if "error" in analysis:
                reply_text(reply_token, analysis["error"])
                continue
            reply_text(reply_token, detail_card(user, analysis), quick_items=qr_analysis())
            continue

        if text == "結束分析":
            summary = settlement_card(user)
            update_user(
                line_user_id,
                current_road=[],
                high_count=0,
                low_count=0,
                tie_count=0,
                banker_pair_count=0,
                player_pair_count=0,
                imported_ready=False,
                analysis_active=False,
                pending_flow=None,
                last_prediction=None,
                round_win=0,
                round_loss=0,
                win_streak=0,
                loss_streak=0,
                max_win_streak=0,
                max_loss_streak=0,
            )
            reply_text(reply_token, summary + "\n\n你可以重新匯入牌路，或調整點數配置。", quick_items=qr_main(is_admin))
            continue

        # Pair only
        if text in ["莊對", "庄對", "閒對", "闲對"]:
            if text in ["莊對", "庄對"]:
                user = update_user(line_user_id, banker_pair_count=(user.get("banker_pair_count", 0) or 0) + 1)
                reply_text(reply_token, "已記錄：莊對", quick_items=qr_analysis())
            else:
                user = update_user(line_user_id, player_pair_count=(user.get("player_pair_count", 0) or 0) + 1)
                reply_text(reply_token, "已記錄：閒對", quick_items=qr_analysis())
            continue

        # High/low only
        if text in ["高", "低", "高牌", "低牌"]:
            if text in ["高", "高牌"]:
                user = update_user(line_user_id, high_count=(user.get("high_count", 0) or 0) + 1)
                reply_text(reply_token, "已記錄：高牌", quick_items=qr_analysis())
            else:
                user = update_user(line_user_id, low_count=(user.get("low_count", 0) or 0) + 1)
                reply_text(reply_token, "已記錄：低牌", quick_items=qr_analysis())
            continue

        # Real-time result input
        if re.search(r"[莊閒和]", text):
            results = extract_results(text)
            if not results:
                reply_text(reply_token, "未讀取到牌路。")
                continue

            road = user.get("current_road", [])
            prior_prediction = user.get("last_prediction")
            actual_for_score = None

            high_add = 1 if "高" in text else 0
            low_add = 1 if "低" in text else 0
            tie_add = sum(1 for x in results if x == "和")
            banker_pair_add = 1 if "莊對" in text or "庄對" in text else 0
            player_pair_add = 1 if "閒對" in text or "闲對" in text else 0

            for r in results:
                road.append(r)
                if r in ["莊", "閒"] and actual_for_score is None:
                    actual_for_score = r

            # 只用莊 / 閒結算上一把預測；和局不算輸贏
            if prior_prediction and actual_for_score:
                hit = prior_prediction == actual_for_score
                add_log(line_user_id, prior_prediction, actual_for_score, hit)
                if hit:
                    new_ws = (user.get("win_streak", 0) or 0) + 1
                    user = update_user(
                        line_user_id,
                        round_win=(user.get("round_win", 0) or 0) + 1,
                        win_streak=new_ws,
                        loss_streak=0,
                        max_win_streak=max(user.get("max_win_streak", 0) or 0, new_ws),
                    )
                else:
                    new_ls = (user.get("loss_streak", 0) or 0) + 1
                    user = update_user(
                        line_user_id,
                        round_loss=(user.get("round_loss", 0) or 0) + 1,
                        loss_streak=new_ls,
                        win_streak=0,
                        max_loss_streak=max(user.get("max_loss_streak", 0) or 0, new_ls),
                    )

            user = update_user(
                line_user_id,
                current_road=road,
                high_count=(user.get("high_count", 0) or 0) + high_add,
                low_count=(user.get("low_count", 0) or 0) + low_add,
                tie_count=(user.get("tie_count", 0) or 0) + tie_add,
                banker_pair_count=(user.get("banker_pair_count", 0) or 0) + banker_pair_add,
                player_pair_count=(user.get("player_pair_count", 0) or 0) + player_pair_add,
            )

            if len(main_only(road)) < MIN_ROAD_LEN:
                extra = ""
                if tie_add:
                    extra = "\n和局已記錄，但不計入莊閒主路把數。"
                reply_text(reply_token, f"已記錄：{''.join(results)}\n目前莊閒主路{len(main_only(road))}把，滿{MIN_ROAD_LEN}把後可開始分析。{extra}")
                continue

            analysis = analyze_v13(user)
            user = update_user(line_user_id, last_prediction=analysis["direction"])
            reply_text(reply_token, decision_card(user, analysis), quick_items=qr_analysis())
            continue

        # fallback
        reply_text(
            reply_token,
            "你剛剛說：" + text + "\n\n可用功能：開始 / 點數配置 / 綁定帳號 / 功能介紹 / 找管理員 / 會員說明 / 匯入牌路 / 開始分析 / 詳細分析 / 結束分析",
            quick_items=qr_main(is_admin)
        )

    return "OK"

# =========================
# Boot
# =========================
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
