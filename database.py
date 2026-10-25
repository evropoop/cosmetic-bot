import sqlite3
import json
from datetime import datetime, timedelta

DB_NAME = "cosmetic_bot.db"


def get_connection():
    """Создает подключение к SQLite с поддержкой конкурентного доступа (WAL)."""
    conn = sqlite3.connect(DB_NAME, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Инициализирует все таблицы строго по ТЗ v4.0 (Блок 8, п. 6)."""
    conn = get_connection()
    cursor = conn.cursor()

    # 1. Таблица users
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id BIGINT PRIMARY KEY,
        username TEXT,
        user_name TEXT DEFAULT '',
        user_tags TEXT DEFAULT '[]',
        medical_note TEXT DEFAULT '',
        free_scans_used INTEGER DEFAULT 0,
        scanner_tariff TEXT DEFAULT 'none',
        scanner_expires_at TEXT,
        scans_today INTEGER DEFAULT 0,
        scans_month INTEGER DEFAULT 0,
        last_scan_date TEXT,
        maria_order_status TEXT DEFAULT 'none',
        pause_until TEXT,
        survey_done INTEGER DEFAULT 0,
        last_activity_at TEXT,
        created_at TEXT NOT NULL
    )
    """)

    # Авто-миграция на случай, если база осталась от старой версии бота
    cursor.execute("PRAGMA table_info(users)")
    existing_cols = {row["name"] for row in cursor.fetchall()}

    # Если старая таблица была с user_id вместо telegram_id — пересоздаем под ТЗ v4.0
    if "user_id" in existing_cols and "telegram_id" not in existing_cols:
        cursor.execute("ALTER TABLE users RENAME TO users_old_backup")
        cursor.execute("""
        CREATE TABLE users (
            telegram_id BIGINT PRIMARY KEY,
            username TEXT,
            user_name TEXT DEFAULT '',
            user_tags TEXT DEFAULT '[]',
            medical_note TEXT DEFAULT '',
            free_scans_used INTEGER DEFAULT 0,
            scanner_tariff TEXT DEFAULT 'none',
            scanner_expires_at TEXT,
            scans_today INTEGER DEFAULT 0,
            scans_month INTEGER DEFAULT 0,
            last_scan_date TEXT,
            maria_order_status TEXT DEFAULT 'none',
            pause_until TEXT,
            survey_done INTEGER DEFAULT 0,
            last_activity_at TEXT,
            created_at TEXT NOT NULL
        )
        """)
        print("🔄 Выполнена миграция структуры таблицы users под ТЗ v4.0")

    # 2. Таблица approved_products (Интеллектуальная память тарифа 2500 ₽)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS approved_products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id BIGINT NOT NULL,
        category TEXT NOT NULL,
        status TEXT NOT NULL,
        stage TEXT NOT NULL,
        key_actives TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
    )
    """)

    # 3. Таблица payments (Защита от дублей вебхуков)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        payment_id TEXT PRIMARY KEY,
        telegram_id BIGINT NOT NULL,
        amount INTEGER NOT NULL,
        tariff_type TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    # 4. Таблица логов отправленных пушей (Для Блока 3, 4 и 4.1)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sent_pushes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id BIGINT NOT NULL,
        push_key TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        UNIQUE(telegram_id, push_key)
    )
    """)

    conn.commit()
    conn.close()
    print("✅ База данных ТЗ v4.0 успешно инициализирована")


# =====================================================================
# ФУНКЦИИ РАБОТЫ С ПОЛЬЗОВАТЕЛЯМИ (ШАГИ 1–3, БЛОК 2, БЛОК 7)
# =====================================================================

def get_or_create_user(telegram_id: int, username: str = None) -> dict:
    """Получает пользователя или создает нового. Никогда не сбрасывает free_scans_used."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    now = datetime.now().isoformat()

    if not row:
        cursor.execute("""
            INSERT INTO users (telegram_id, username, last_activity_at, created_at)
            VALUES (?, ?, ?, ?)
        """, (telegram_id, username, now, now))
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cursor.fetchone()
    elif username and row["username"] != username:
        cursor.execute(
            "UPDATE users SET username = ?, last_activity_at = ? WHERE telegram_id = ?",
            (username, now, telegram_id)
        )
        conn.commit()

    conn.close()
    return _row_to_user_dict(row)


def get_user(telegram_id: int) -> dict | None:
    """Возвращает данные пользователя по telegram_id или None (нужно для Админ-пульта)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    return _row_to_user_dict(row) if row else None


def _row_to_user_dict(row: sqlite3.Row) -> dict:
    """Конвертирует строку БД в удобный словарь Python."""
    d = dict(row)
    try:
        d["user_tags"] = json.loads(d["user_tags"]) if d.get("user_tags") else []
    except Exception:
        d["user_tags"] = []
    return d


def touch_user_activity(telegram_id: int):
    """Обновляет время последнего действия пользователя."""
    conn = get_connection()
    conn.execute(
        "UPDATE users SET last_activity_at = ? WHERE telegram_id = ?",
        (datetime.now().isoformat(), telegram_id)
    )
    conn.commit()
    conn.close()


def clear_user_tags_for_new_survey(telegram_id: int):
    """
    Шаг 2 ТЗ: При нажатии [ СТАРТ ] очищает старые теги и мед. заметку,
    но СТРОГО сохраняет free_scans_used и статус подписок.
    """
    conn = get_connection()
    conn.execute("""
        UPDATE users
        SET user_tags = '[]', medical_note = '', survey_done = 0, last_activity_at = ?
        WHERE telegram_id = ?
    """, (datetime.now().isoformat(), telegram_id))
    conn.commit()
    conn.close()


def update_user_name(telegram_id: int, user_name: str):
    """Сохраняет очищенное имя пользователя (до 30 символов)."""
    conn = get_connection()
    conn.execute(
        "UPDATE users SET user_name = ?, last_activity_at = ? WHERE telegram_id = ?",
        (user_name[:30], datetime.now().isoformat(), telegram_id)
    )
    conn.commit()
    conn.close()


def save_survey_results(telegram_id: int, tags: list[str], medical_note: str = ""):
    """Сохраняет итоговые теги анкеты и медицинскую пометку, ставит флаг завершения анкеты."""
    # Убираем дубликаты с сохранением порядка
    unique_tags = list(dict.fromkeys(tags))
    if not unique_tags:
        unique_tags = ["Норма"]

    conn = get_connection()
    conn.execute("""
        UPDATE users
        SET user_tags = ?, medical_note = ?, survey_done = 1, last_activity_at = ?
        WHERE telegram_id = ?
    """, (
        json.dumps(unique_tags, ensure_ascii=False),
        medical_note[:100],
        datetime.now().isoformat(),
        telegram_id
    ))
    conn.commit()
    conn.close()


# =====================================================================
# ФУНКЦИИ ЛИМИТОВ, ТАРИФОВ И FAIR USE (БЛОК 1, БЛОК 8 п. 1)
# =====================================================================

def has_active_scanner_sub(telegram_id: int) -> tuple[bool, str]:
    """
    Проверяет, активна ли платная подписка на сканер ('audit_1000' или 'protocol_2500').
    Если срок истек — автоматически сбрасывает scanner_tariff в 'none'.
    Возвращает: (is_active: bool, tariff_name: str)
    """
    user = get_user(telegram_id)
    if not user:
        return False, "none"

    tariff = user.get("scanner_tariff", "none")
    expires_at_str = user.get("scanner_expires_at")

    if tariff in ("audit_1000", "protocol_2500") and expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            if expires_at > datetime.now():
                return True, tariff
            else:
                # Подписка истекла
                conn = get_connection()
                conn.execute(
                    "UPDATE users SET scanner_tariff = 'none' WHERE telegram_id = ?",
                    (telegram_id,)
                )
                conn.commit()
                conn.close()
                return False, "none"
        except Exception:
            return False, "none"

    return False, "none"


def check_and_increment_scan_usage(telegram_id: int, is_paid_sub: bool) -> tuple[bool, str]:
    """
    Проверяет дневной/месячный Fair Use лимит (40 в день / 500 в месяц)
    и увеличивает счетчик сканов (и free_scans_used, если тариф бесплатный).
    Возвращает (allowed: bool, error_reason: str).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT free_scans_used, scans_today, scans_month, last_scan_date FROM users WHERE telegram_id = ?",
        (telegram_id,)
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, "user_not_found"

    today_str = datetime.now().strftime("%Y-%m-%d")
    scans_today = row["scans_today"] if row["last_scan_date"] == today_str else 0
    scans_month = row["scans_month"] or 0
    free_used = row["free_scans_used"] or 0

    if scans_today >= 40:
        conn.close()
        return False, "daily_limit"
    if scans_month >= 500:
        conn.close()
        return False, "monthly_limit"

    if not is_paid_sub and free_used >= 2:
        conn.close()
        return False, "paywall"

    new_free_used = free_used if is_paid_sub else (free_used + 1)
    cursor.execute("""
        UPDATE users
        SET free_scans_used = ?,
            scans_today = ?,
            scans_month = ?,
            last_scan_date = ?,
            last_activity_at = ?
        WHERE telegram_id = ?
    """, (
        new_free_used,
        scans_today + 1,
        scans_month + 1,
        today_str,
        datetime.now().isoformat(),
        telegram_id
    ))
    conn.commit()
    conn.close()
    return True, "ok"


# =====================================================================
# ПАМЯТЬ ТАРИФА 2500 ₽: APPROVED_PRODUCTS (БЛОК 8 п. 5)
# =====================================================================

def add_approved_product(telegram_id: int, category: str, status: str, stage: str, key_actives: str) -> int:
    """Сохраняет средство (🟢/🔵/🟡) для лица/век в базу и возвращает ID записи."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO approved_products (telegram_id, category, status, stage, key_actives, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (telegram_id, category, status, stage, key_actives, datetime.now().isoformat()))
    record_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return record_id


def remove_approved_product(record_id: int, telegram_id: int) -> bool:
    """Удаляет средство по кнопке 'Я только смотрю в магазине' или из меню /profile."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM approved_products WHERE id = ? AND telegram_id = ?",
        (record_id, telegram_id)
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_approved_products(telegram_id: int) -> list[dict]:
    """Возвращает все сохраненные средства пользователя для формирования <approved_routine>."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, category, status, stage, key_actives, created_at
        FROM approved_products
        WHERE telegram_id = ?
        ORDER BY id ASC
    """, (telegram_id,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def format_approved_routine_for_llm(telegram_id: int) -> str:
    """Формирует текстовый блок <approved_routine> для промпта Claude Sonnet."""
    items = get_approved_products(telegram_id)
    if not items:
        return "Пока нет сохраненных средств."
    lines = []
    for item in items:
        lines.append(f"Этап: {item['stage']} | Статус: {item['status']} | Активы: {item['key_actives']}")
    return "\n".join(lines)


def get_closed_basic_stages(telegram_id: int) -> dict[str, bool]:
    """
    Для 29-го дня (Блок 3 п. 2): базовый этап считается закрытым (True) ТОЛЬКО если
    в таблице есть запись этого этапа со статусом 🟢 или 🔵.
    """
    basic_stages = {
        "Мягкое очищение": False,
        "Тонизация": False,
        "Базовый крем": False,
        "Защита SPF": False,
    }
    items = get_approved_products(telegram_id)
    for item in items:
        if item["status"] in ("🟢", "🔵") and item["stage"] in basic_stages:
            basic_stages[item["stage"]] = True
    return basic_stages


# =====================================================================
# ПЛАТЕЖИ И СТАТУСЫ МАРИИ (ШАГ 5.1, ШАГ 6, БЛОК 8 п. 7)
# =====================================================================

def process_successful_payment(payment_id: str, telegram_id: int, amount: int, tariff_type: str) -> bool:
    """
    Обрабатывает успешный платеж с проверкой идемпотентности (защита от дублей вебхука).
    tariff_type: 'audit_1000' | 'protocol_2500' | 'pdf_4900'
    Возвращает True, если платеж новый и обработан, False — если дубль.
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT payment_id FROM payments WHERE payment_id = ?", (payment_id,))
    if cursor.fetchone():
        conn.close()
        return False

    now = datetime.now()
    cursor.execute("""
        INSERT INTO payments (payment_id, telegram_id, amount, tariff_type, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (payment_id, telegram_id, amount, tariff_type, now.isoformat()))

    if tariff_type in ("audit_1000", "protocol_2500"):
        expires_at = (now + timedelta(days=30)).isoformat()
        cursor.execute("""
            UPDATE users
            SET scanner_tariff = ?, scanner_expires_at = ?, scans_month = 0, last_activity_at = ?
            WHERE telegram_id = ?
        """, (tariff_type, expires_at, now.isoformat(), telegram_id))
        # Сбрасываем лог пушей реактивации на новый цикл
        cursor.execute("DELETE FROM sent_pushes WHERE telegram_id = ?", (telegram_id,))

    elif tariff_type == "pdf_4900":
        cursor.execute("""
            UPDATE users
            SET maria_order_status = 'completed_vip', last_activity_at = ?
            WHERE telegram_id = ?
        """, (now.isoformat(), telegram_id))

    conn.commit()
    conn.close()
    return True


def set_maria_order_status(telegram_id: int, status: str):
    """
    Админ-пульт Марии:
    status = 'paused_72h' -> ставит паузу пушей на +72 часа
    status = 'completed_vip' -> отключает пуши Блока 4 навсегда
    """
    conn = get_connection()
    now = datetime.now()
    pause_until = (now + timedelta(hours=72)).isoformat() if status == "paused_72h" else None
    conn.execute("""
        UPDATE users
        SET maria_order_status = ?, pause_until = ?, last_activity_at = ?
        WHERE telegram_id = ?
    """, (status, pause_until, now.isoformat(), telegram_id))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    # Тестовая проверка Этапа 1 при прямом запуске файла: python database.py
    init_db()
    test_u = get_or_create_user(123456789, "test_user")
    print("Тестовый пользователь создан:", test_u["telegram_id"], "| Бесплатных сканов:", test_u["free_scans_used"])
    save_survey_results(123456789, ["Сломан барьер", "Сухость"], "Розацеа в ремиссии")
    print("После анкеты:", get_user(123456789)["user_tags"])
