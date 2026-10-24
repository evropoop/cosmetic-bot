import sqlite3
from datetime import datetime, timedelta
import json

DB_NAME = "cosmetic_bot.db"


def get_connection():
    conn = sqlite3.connect(DB_NAME, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        tags TEXT DEFAULT '[]',
        free_checks INTEGER DEFAULT 2,
        bad_bottles_in_a_row INTEGER DEFAULT 0,
        survey_done INTEGER DEFAULT 0,
        survey_completed_at TEXT,
        user_name TEXT,
        last_activity_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    cursor.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cursor.fetchall()]
    if "user_name" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN user_name TEXT")
    if "last_activity_at" not in columns:
        cursor.execute("ALTER TABLE users ADD COLUMN last_activity_at TEXT")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        user_id INTEGER PRIMARY KEY,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        tariff TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        composition TEXT NOT NULL,
        verdict TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        tariff TEXT NOT NULL,
        payment_id TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        tags TEXT,
        photos TEXT,
        status TEXT DEFAULT 'new',
        created_at TEXT NOT NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scheduled_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        notification_type TEXT NOT NULL,
        scheduled_at TEXT NOT NULL,
        sent INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)

    conn.commit()
    conn.close()
    print("✅ База данных инициализирована (WAL mode)")


# ================== ПОЛЬЗОВАТЕЛИ ==================
def get_or_create_user(user_id: int, username: str = None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT user_id, username, tags, free_checks, bad_bottles_in_a_row,
               survey_done, survey_completed_at, user_name
        FROM users WHERE user_id = ?
    """, (user_id,))
    row = cursor.fetchone()

    if not row:
        now = datetime.now().isoformat()
        cursor.execute(
            "INSERT INTO users (user_id, username, created_at, updated_at, last_activity_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, now, now, now)
        )
        conn.commit()
        conn.close()
        return {
            "user_id": user_id,
            "username": username,
            "tags": [],
            "free_checks": 2,
            "bad_bottles_in_a_row": 0,
            "survey_done": 0,
            "survey_completed_at": None,
            "user_name": None
        }

    if username and row[1] != username:
        cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        conn.commit()

    conn.close()
    return {
        "user_id": row[0],
        "username": username or row[1],
        "tags": json.loads(row[2]) if row[2] else [],
        "free_checks": row[3],
        "bad_bottles_in_a_row": row[4],
        "survey_done": row[5],
        "survey_completed_at": row[6],
        "user_name": row[7] if len(row) > 7 else None
    }


def update_user_name(user_id: int, name: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET user_name = ?, updated_at = ? WHERE user_id = ?",
        (name, datetime.now().isoformat(), user_id)
    )
    conn.commit()
    conn.close()


def get_user_name(user_id: int) -> str:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT user_name FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if (row and row[0]) else ""


def touch_user_activity(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET last_activity_at = ? WHERE user_id = ?",
        (datetime.now().isoformat(), user_id)
    )
    conn.commit()
    conn.close()


def get_last_activity(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT last_activity_at FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        try:
            return datetime.fromisoformat(row[0])
        except Exception:
            return None
    return None


def update_user_tags(user_id: int, tags: list):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET tags = ?, survey_done = 1, updated_at = ? WHERE user_id = ?",
        (json.dumps(tags, ensure_ascii=False), datetime.now().isoformat(), user_id)
    )
    conn.commit()
    conn.close()


def mark_survey_completed(user_id: int):
    now = datetime.now().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET survey_completed_at = ?, updated_at = ? WHERE user_id = ?",
        (now, now, user_id)
    )
    conn.commit()
    conn.close()


def decrement_free_checks(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET free_checks = MAX(0, free_checks - 1), updated_at = ? WHERE user_id = ?",
        (datetime.now().isoformat(), user_id)
    )
    conn.commit()
    conn.close()


def get_free_checks(user_id: int) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT free_checks FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


def increment_bad_bottles(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET bad_bottles_in_a_row = bad_bottles_in_a_row + 1, updated_at = ? WHERE user_id = ?",
        (datetime.now().isoformat(), user_id)
    )
    conn.commit()
    conn.close()


def reset_bad_bottles(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET bad_bottles_in_a_row = 0, updated_at = ? WHERE user_id = ?",
        (datetime.now().isoformat(), user_id)
    )
    conn.commit()
    conn.close()


# ================== ПОДПИСКИ ==================
def save_subscription(user_id: int, tariff: str):
    conn = get_connection()
    cursor = conn.cursor()
    durations = {"month": 30, "3months": 90, "6months": 180, "year": 365}
    expires_at = datetime.now() + timedelta(days=durations.get(tariff, 30))
    cursor.execute(
        "INSERT OR REPLACE INTO subscriptions (user_id, expires_at, created_at, tariff) VALUES (?, ?, ?, ?)",
        (user_id, expires_at.isoformat(), datetime.now().isoformat(), tariff)
    )
    conn.commit()
    conn.close()
    cancel_retention_notifications(user_id)


def get_subscription(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT expires_at, tariff FROM subscriptions WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {"expires_at": datetime.fromisoformat(row[0]), "tariff": row[1]}


def has_active_subscription(user_id: int) -> bool:
    sub = get_subscription(user_id)
    if not sub:
        return False
    return sub["expires_at"] > datetime.now()


# ================== ИСТОРИЯ РАЗБОРОВ ==================
def save_analysis(user_id: int, composition: str, verdict: str, status: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO analyses (user_id, composition, verdict, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, composition, verdict, status, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


# ================== RETENTION-УВЕДОМЛЕНИЯ ==================
def schedule_retention_notifications(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT COUNT(*) FROM scheduled_notifications WHERE user_id = ? AND sent = 0",
        (user_id,)
    )
    if cursor.fetchone()[0] > 0:
        conn.close()
        return

    now = datetime.now()
    schedule = [
        ("1m",  now + timedelta(minutes=1)),
        ("2h",  now + timedelta(hours=2)),
        ("24h", now + timedelta(hours=24)),
        ("48h", now + timedelta(hours=48)),
        ("7d",  now + timedelta(days=7)),
        ("30d", now + timedelta(days=30)),
    ]
    for ntype, when in schedule:
        cursor.execute(
            "INSERT INTO scheduled_notifications (user_id, notification_type, scheduled_at, created_at) VALUES (?, ?, ?, ?)",
            (user_id, ntype, when.isoformat(), now.isoformat())
        )
    conn.commit()
    conn.close()


def cancel_retention_notifications(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM scheduled_notifications WHERE user_id = ? AND sent = 0", (user_id,))
    conn.commit()
    conn.close()


def get_due_notifications():
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.now().isoformat()
    cursor.execute("""
        SELECT id, user_id, notification_type
        FROM scheduled_notifications
        WHERE sent = 0 AND scheduled_at <= ?
        ORDER BY scheduled_at ASC
    """, (now,))
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "user_id": r[1], "type": r[2]} for r in rows]


def mark_notification_sent(notif_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE scheduled_notifications SET sent = 1 WHERE id = ?", (notif_id,))
    conn.commit()
    conn.close()


def reschedule_notification(notif_id: int, new_time: datetime):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE scheduled_notifications SET scheduled_at = ? WHERE id = ?",
        (new_time.isoformat(), notif_id)
    )
    conn.commit()
    conn.close()
