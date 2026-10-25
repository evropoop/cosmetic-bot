import sqlite3
from datetime import datetime, timedelta
import json

DB_NAME = "cosmetic_bot.db"


def get_connection():
    conn = sqlite3.connect(DB_NAME, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        username TEXT,
        user_name TEXT,
        user_tags TEXT DEFAULT '[]',
        medical_note TEXT,
        free_scans_used INTEGER DEFAULT 0,
        scanner_tariff TEXT DEFAULT 'none',
        scanner_expires_at TEXT,
        scans_today INTEGER DEFAULT 0,
        scans_month INTEGER DEFAULT 0,
        maria_order_status TEXT DEFAULT 'none',
        last_activity_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        survey_done INTEGER DEFAULT 0,
        survey_completed_at TEXT,
        bad_bottles_in_a_row INTEGER DEFAULT 0
    )
    """)

    cursor.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cursor.fetchall()]
    for col in ["medical_note", "free_scans_used", "scanner_tariff", "scanner_expires_at",
                "scans_today", "scans_month", "maria_order_status", "last_activity_at"]:
        if col not in columns:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS approved_products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        composition TEXT NOT NULL,
        status TEXT NOT NULL,
        category TEXT,
        stage TEXT,
        key_actives TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cursor.execute("PRAGMA table_info(approved_products)")
    ap_cols = [row[1] for row in cursor.fetchall()]
    for col in ["category", "stage", "key_actives"]:
        if col not in ap_cols:
            cursor.execute(f"ALTER TABLE approved_products ADD COLUMN {col} TEXT")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        payment_id TEXT PRIMARY KEY,
        telegram_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        tariff_type TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        username TEXT,
        tags TEXT,
        photos TEXT,
        status TEXT DEFAULT 'new',
        created_at TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        composition TEXT NOT NULL,
        verdict TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scans_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scheduled_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER NOT NULL,
        notification_type TEXT NOT NULL,
        scheduled_at TEXT NOT NULL,
        sent INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_stages (
        telegram_id INTEGER NOT NULL,
        stage_key TEXT NOT NULL,
        completed INTEGER DEFAULT 0,
        PRIMARY KEY (telegram_id, stage_key)
    )
    """)

    conn.commit()
    conn.close()
    print("✅ База данных инициализирована")


# ================== ПОЛЬЗОВАТЕЛИ ==================
def get_user(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT telegram_id, username, user_name, user_tags, medical_note,
               free_scans_used, scanner_tariff, scanner_expires_at, scans_today, scans_month,
               maria_order_status, last_activity_at, survey_done, survey_completed_at, bad_bottles_in_a_row
        FROM users WHERE telegram_id = ?
    """, (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "user_id": row[0], "telegram_id": row[0], "username": row[1],
        "user_name": row[2] or "", "user_tags": json.loads(row[3]) if row[3] else [],
        "medical_note": row[4], "free_scans_used": row[5] or 0,
        "scanner_tariff": row[6] or "none", "scanner_expires_at": row[7],
        "scans_today": row[8] or 0, "scans_month": row[9] or 0,
        "maria_order_status": row[10] or "none", "last_activity_at": row[11],
        "survey_done": row[12], "survey_completed_at": row[13],
        "bad_bottles_in_a_row": row[14] or 0
    }


def get_or_create_user(telegram_id: int, username: str = None):
    user = get_user(telegram_id)
    if user:
        return user

    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute(
        "INSERT INTO users (telegram_id, username, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (telegram_id, username, now, now)
    )
    conn.commit()
    conn.close()
    return {
        "user_id": telegram_id, "telegram_id": telegram_id, "username": username,
        "user_name": "", "user_tags": [], "medical_note": None,
        "free_scans_used": 0, "scanner_tariff": "none", "scanner_expires_at": None,
        "scans_today": 0, "scans_month": 0, "maria_order_status": "none",
        "last_activity_at": None, "survey_done": 0, "survey_completed_at": None,
        "bad_bottles_in_a_row": 0
    }


def set_maria_order_status(telegram_id: int, status: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET maria_order_status = ? WHERE telegram_id = ?", (status, telegram_id))
    conn.commit()
    conn.close()


def update_user_name(telegram_id: int, name: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET user_name = ?, updated_at = ? WHERE telegram_id = ?", (name, datetime.now().isoformat(), telegram_id))
    conn.commit()
    conn.close()


def get_user_name(telegram_id: int) -> str:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_name FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] else ""


def update_user_tags(telegram_id: int, tags: list, medical_note: str = None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET user_tags = ?, medical_note = COALESCE(?, medical_note), survey_done = 1, updated_at = ? WHERE telegram_id = ?",
        (json.dumps(tags, ensure_ascii=False), medical_note, datetime.now().isoformat(), telegram_id)
    )
    conn.commit()
    conn.close()


def clear_user_tags(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET user_tags = '[]', medical_note = NULL, updated_at = ? WHERE telegram_id = ?", (datetime.now().isoformat(), telegram_id))
    conn.commit()
    conn.close()


def touch_user_activity(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET last_activity_at = ? WHERE telegram_id = ?", (datetime.now().isoformat(), telegram_id))
    conn.commit()
    conn.close()


def get_last_activity(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT last_activity_at FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        try:
            return datetime.fromisoformat(row[0])
        except Exception:
            return None
    return None


def mark_survey_completed(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET survey_completed_at = ?, updated_at = ? WHERE telegram_id = ?", (datetime.now().isoformat(), datetime.now().isoformat(), telegram_id))
    conn.commit()
    conn.close()


def increment_free_scans_used(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET free_scans_used = free_scans_used + 1, updated_at = ? WHERE telegram_id = ?", (datetime.now().isoformat(), telegram_id))
    conn.commit()
    conn.close()


def increment_bad_bottles(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET bad_bottles_in_a_row = bad_bottles_in_a_row + 1, updated_at = ? WHERE telegram_id = ?", (datetime.now().isoformat(), telegram_id))
    conn.commit()
    conn.close()


def reset_bad_bottles(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET bad_bottles_in_a_row = 0, updated_at = ? WHERE telegram_id = ?", (datetime.now().isoformat(), telegram_id))
    conn.commit()
    conn.close()


# ================== ОДОБРЕННЫЕ СРЕДСТВА ==================
def save_approved_product(telegram_id: int, composition: str, status: str, category: str, stage: str, key_actives: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO approved_products (telegram_id, composition, status, category, stage, key_actives, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (telegram_id, composition, status, category, stage, key_actives, datetime.now().isoformat())
    )
    record_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return record_id


def get_approved_products(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, composition, status, category, stage, key_actives, created_at FROM approved_products WHERE telegram_id = ? ORDER BY id DESC",
        (telegram_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "composition": r[1], "status": r[2], "category": r[3], "stage": r[4], "key_actives": r[5], "created_at": r[6]} for r in rows]


def count_approved_products(telegram_id: int) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM approved_products WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


def delete_approved_product(record_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM approved_products WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()


def get_completed_stages(telegram_id: int) -> list:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT stage FROM approved_products WHERE telegram_id = ? AND status IN ('good', 'hydrophilic')",
        (telegram_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    base_stages = ["Мягкое очищение", "Тонизация", "Базовый крем", "Защита SPF"]
    completed = []
    for r in rows:
        if r[0] in base_stages and r[0] not in completed:
            completed.append(r[0])
    return completed


# ================== СТАДИИ ПРОТОКОЛА ==================
def get_user_stages(telegram_id: int) -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT stage_key, completed FROM user_stages WHERE telegram_id = ?",
        (telegram_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    result = {"cleansing": False, "toning": False, "cream": False, "spf": False}

    if not rows:
        completed_from_db = get_completed_stages(telegram_id)
        init_map = {
            "cleansing": "Мягкое очищение" in completed_from_db,
            "toning": "Тонизация" in completed_from_db,
            "cream": "Базовый крем" in completed_from_db,
            "spf": "Защита SPF" in completed_from_db,
        }
        for key, val in init_map.items():
            if val:
                set_user_stage(telegram_id, key, True)
                result[key] = True
        return result

    for key, completed in rows:
        if key in result:
            result[key] = bool(completed)
    return result


def set_user_stage(telegram_id: int, stage_key: str, completed: bool):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO user_stages (telegram_id, stage_key, completed)
           VALUES (?, ?, ?)
           ON CONFLICT(telegram_id, stage_key) DO UPDATE SET completed = excluded.completed""",
        (telegram_id, stage_key, 1 if completed else 0),
    )
    conn.commit()
    conn.close()


def reset_user_stages(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_stages WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()


# ================== ПЛАТЕЖИ ==================
def save_payment(payment_id: str, telegram_id: int, amount: int, tariff_type: str):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO payments (payment_id, telegram_id, amount, tariff_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (payment_id, telegram_id, amount, tariff_type, datetime.now().isoformat())
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def update_user_subscription(telegram_id: int, tariff: str, days: int = 30):
    expires_at = (datetime.now() + timedelta(days=days)).isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET scanner_tariff = ?, scanner_expires_at = ? WHERE telegram_id = ?",
        (tariff, expires_at, telegram_id)
    )
    conn.commit()
    conn.close()


def has_active_subscription(user: dict) -> bool:
    if user.get("scanner_tariff") in ["audit_1000", "protocol_2500"]:
        exp = user.get("scanner_expires_at")
        if exp:
            try:
                return datetime.fromisoformat(exp) > datetime.now()
            except Exception:
                return False
    return False


# ================== FAIR USE ==================
def log_scan(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute("INSERT INTO scans_log (telegram_id, created_at) VALUES (?, ?)", (telegram_id, now))
    cursor.execute("UPDATE users SET scans_today = scans_today + 1, scans_month = scans_month + 1 WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()


def check_rate_limit(telegram_id: int):
    now = datetime.now()
    conn = get_connection()
    cursor = conn.cursor()

    one_min_ago = (now - timedelta(minutes=1)).isoformat()
    cursor.execute("SELECT COUNT(*) FROM scans_log WHERE telegram_id = ? AND created_at > ?", (telegram_id, one_min_ago))
    if cursor.fetchone()[0] >= 3:
        conn.close()
        return "minute"

    cursor.execute("SELECT scans_today FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    if row and row[0] >= 40:
        conn.close()
        return "day"

    cursor.execute("SELECT scans_month FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    if row and row[0] >= 500:
        conn.close()
        return "month"

    conn.close()
    return None


# ================== ИСТОРИЯ И ЛИДЫ ==================
def save_analysis(telegram_id: int, composition: str, verdict: str, status: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO analyses (telegram_id, composition, verdict, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (telegram_id, composition, verdict, status, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def save_lead(telegram_id: int, username: str, tags: list, photos: list):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO leads (telegram_id, username, tags, photos, created_at) VALUES (?, ?, ?, ?, ?)",
        (telegram_id, username, json.dumps(tags, ensure_ascii=False), json.dumps(photos), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


# ================== RETENTION ==================
def schedule_retention_notifications(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM scheduled_notifications WHERE telegram_id = ? AND sent = 0", (telegram_id,))
    if cursor.fetchone()[0] > 0:
        conn.close()
        return

    now = datetime.now()
    schedule = [
        ("2h", now + timedelta(hours=2)),
        ("24h", now + timedelta(hours=24)),
        ("48h", now + timedelta(hours=48)),
        ("5d", now + timedelta(days=5)),
        ("7d", now + timedelta(days=7)),
        ("10d", now + timedelta(days=10)),
        ("14d", now + timedelta(days=14)),
        ("21d", now + timedelta(days=21)),
        ("30d", now + timedelta(days=30)),
        ("60d", now + timedelta(days=60)),
        ("90d", now + timedelta(days=90)),
        ("180d", now + timedelta(days=180)),
    ]
    for ntype, when in schedule:
        cursor.execute(
            "INSERT INTO scheduled_notifications (telegram_id, notification_type, scheduled_at, created_at) VALUES (?, ?, ?, ?)",
            (telegram_id, ntype, when.isoformat(), now.isoformat())
        )
    conn.commit()
    conn.close()


def cancel_retention_notifications(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM scheduled_notifications WHERE telegram_id = ? AND sent = 0", (telegram_id,))
    conn.commit()
    conn.close()


def delay_retention_notifications(telegram_id: int, hours: int = 72):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, scheduled_at FROM scheduled_notifications WHERE telegram_id = ? AND sent = 0", (telegram_id,))
    rows = cursor.fetchall()
    for notif_id, scheduled_at in rows:
        dt = datetime.fromisoformat(scheduled_at)
        new_dt = dt + timedelta(hours=hours)
        cursor.execute("UPDATE scheduled_notifications SET scheduled_at = ? WHERE id = ?", (new_dt.isoformat(), notif_id))
    conn.commit()
    conn.close()


def get_due_notifications():
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute("SELECT id, telegram_id, notification_type FROM scheduled_notifications WHERE sent = 0 AND scheduled_at <= ? ORDER BY scheduled_at ASC", (now,))
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "telegram_id": r[1], "type": r[2]} for r in rows]


def mark_notification_sent(notif_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE scheduled_notifications SET sent = 1 WHERE id = ?", (notif_id,))
    conn.commit()
    conn.close()


def reschedule_notification(notif_id: int, new_time: datetime):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE scheduled_notifications SET scheduled_at = ? WHERE id = ?", (new_time.isoformat(), notif_id))
    conn.commit()
    conn.close()


def schedule_single_notification(telegram_id: int, notif_type: str, when: datetime):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM scheduled_notifications WHERE telegram_id = ? AND notification_type = ? AND sent = 0", (telegram_id, notif_type))
    cursor.execute(
        "INSERT INTO scheduled_notifications (telegram_id, notification_type, scheduled_at, created_at) VALUES (?, ?, ?, ?)",
        (telegram_id, notif_type, when.isoformat(), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
