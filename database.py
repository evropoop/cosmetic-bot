import sqlite3
from datetime import datetime, timedelta
import json

DB_NAME = "cosmetic_bot.db"


def get_connection():
    return sqlite3.connect(DB_NAME)


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
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

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

    conn.commit()
    conn.close()
    print("✅ База данных инициализирована")


def get_or_create_user(user_id: int, username: str = None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()

    if not row:
        now = datetime.now().isoformat()
        cursor.execute(
            "INSERT INTO users (user_id, username, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, username, now, now)
        )
        conn.commit()
        conn.close()
        return {
            "user_id": user_id,
            "username": username,
            "tags": [],
            "free_checks": 2,
            "bad_bottles_in_a_row": 0,
            "survey_done": 0
        }
    conn.close()
    return {
        "user_id": row[0],
        "username": row[1],
        "tags": json.loads(row[2]) if row[2] else [],
        "free_checks": row[3],
        "bad_bottles_in_a_row": row[4],
        "survey_done": row[5]
    }


def update_user_tags(user_id: int, tags: list):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET tags = ?, survey_done = 1, updated_at = ? WHERE user_id = ?",
        (json.dumps(tags, ensure_ascii=False), datetime.now().isoformat(), user_id)
    )
    conn.commit()
    conn.close()


def decrement_free_checks(user_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET free_checks = free_checks - 1, updated_at = ? WHERE user_id = ?",
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


def get_bad_bottles_count(user_id: int) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT bad_bottles_in_a_row FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0


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


def save_analysis(user_id: int, composition: str, verdict: str, status: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO analyses (user_id, composition, verdict, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, composition, verdict, status, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_last_analyses(user_id: int, limit: int = 5):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT composition, verdict, status FROM analyses WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return [{"composition": r[0], "verdict": r[1], "status": r[2]} for r in rows]


def save_payment(user_id: int, amount: int, tariff: str, payment_id: str, status: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO payments (user_id, amount, tariff, payment_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, amount, tariff, payment_id, status, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def update_payment_status(payment_id: str, status: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE payments SET status = ? WHERE payment_id = ?", (status, payment_id))
    conn.commit()
    conn.close()


def save_lead(user_id: int, username: str, tags: list, photos: list):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO leads (user_id, username, tags, photos, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, json.dumps(tags, ensure_ascii=False), json.dumps(photos), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
