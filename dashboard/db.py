"""
SQLite-слой для дашборда и бота.

Схема:
  sessions      — история загрузок выписок (кто/когда/сколько)
  transactions  — транзакции, привязанные к сессии
  settings      — key/value настройки (пороги алертов, категории) в JSON
  secrets       — key/value секреты (зашифрованные через crypto)
  sheets_config — конфиг экспорта в Google Sheets

Импорт: `from dashboard.db import ...`. БД создаётся автоматически при первом
обращении (data/data.db). Все функции потокобезопасны через check_same_thread=False.
"""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Optional

from dashboard.crypto import decrypt, encrypt

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DB_DIR / "data.db"

# Дефолтные настройки (массив категорий + пороги алертов)
DEFAULT_SETTINGS = {
    "alert_thresholds": {
        # Пороги для detect_anomalies (соответствуют 4 правилам)
        "cash_gap_enabled": True,       # profit < 0
        "category_share_pct": 30,       # категория ≥ X% витрат
        "taxes_multiplier": 3,          # податки ≥ X× среднего
        "salary_income_pct": 80,        # зарплата ≥ X% виручки
    },
    "categories": [
        "продажі", "реклама", "експерти", "зарплата", "оренда",
        "податки", "підписки", "підрядники", "hr", "підтримка сайту",
        "повернення", "фаундери", "переказ", "інше",
    ],
}

_lock = threading.Lock()


# ============================================================
# FTS5-поиск (транзакции + чат)
# ============================================================
def _fts_query(q: str, *, stems: bool = False, op: str = "AND") -> str:
    """Санитизирует пользовательский ввод в безопасный FTS5-запрос.

    'приват+банк' → '"приват"* "банк"*' (префиксный AND-поиск).
    stems: обрезать токены до ствола (первые 6 симв.) — покрывает падежи
           («військового» → «військ*» находит «Військовий»).
    op: 'AND' между токенами (все слова) или 'OR' (хотя бы одно).
    Пустая строка если токенов нет.
    """
    import re
    tokens = re.findall(r"[\w]+", q, re.UNICODE)[:6]
    if not tokens:
        return ""
    parts = []
    for t in tokens:
        stem = t[:6] if stems and len(t) > 6 and not t.isdigit() else t
        parts.append(f'"{stem}"*')
    return f" {op} ".join(parts)


def _init_fts(conn):
    """Создаёт FTS5-индексы + триггеры синхронизации. Идемпотентно.

    Если FTS5 недоступен (экзотическая сборка SQLite) — тихо пропускаем,
    поиск откатится на LIKE (см. search_transactions).
    """
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS transactions_fts USING fts5(
                description, category,
                content='transactions', content_rowid='id'
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chat_history_fts USING fts5(
                content,
                content='chat_history', content_rowid='id'
            )
        """)
    except sqlite3.OperationalError:
        return  # FTS5 нет в сборке — LIKE-fallback

    # триггеры: транзакции
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS transactions_ai AFTER INSERT ON transactions BEGIN
            INSERT INTO transactions_fts(rowid, description, category)
            VALUES (new.id, new.description, new.category);
        END;
        CREATE TRIGGER IF NOT EXISTS transactions_ad AFTER DELETE ON transactions BEGIN
            INSERT INTO transactions_fts(transactions_fts, rowid, description, category)
            VALUES ('delete', old.id, old.description, old.category);
        END;
        CREATE TRIGGER IF NOT EXISTS transactions_au AFTER UPDATE ON transactions BEGIN
            INSERT INTO transactions_fts(transactions_fts, rowid, description, category)
            VALUES ('delete', old.id, old.description, old.category);
            INSERT INTO transactions_fts(rowid, description, category)
            VALUES (new.id, new.description, new.category);
        END;
    """)
    # триггеры: чат
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS chat_history_ai AFTER INSERT ON chat_history BEGIN
            INSERT INTO chat_history_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS chat_history_ad AFTER DELETE ON chat_history BEGIN
            INSERT INTO chat_history_fts(chat_history_fts, rowid, content)
            VALUES ('delete', old.id, old.content);
        END;
    """)
    # backfill: заполнить индекс существующими строками
    conn.execute("INSERT INTO transactions_fts(transactions_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO chat_history_fts(chat_history_fts) VALUES('rebuild')")


def _fts_available(conn) -> bool:
    try:
        conn.execute("SELECT 1 FROM transactions_fts LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False


def search_transactions(q: str, iban: str = "", limit: int = 200, *,
                        stems: bool = False, op: str = "AND",
                        owner: str = "") -> list[dict]:
    """Полнотекстовый поиск по описанию и категории транзакций.

    FTS5 с префиксными токенами; LIKE-fallback если FTS5 нет.
    stems/op — для морфологически нечёткого поиска (вопросы в чате).
    """
    like = f"%{q.strip()}%"
    with get_conn() as conn:
        if _fts_available(conn):
            fts = _fts_query(q, stems=stems, op=op)
            if not fts:
                return []
            sql = (
                "SELECT t.* FROM transactions_fts f "
                "JOIN transactions t ON t.id = f.rowid "
                "WHERE transactions_fts MATCH ?"
            )
            params: list = [fts]
            if owner:
                sql += " AND t.owner = ?"
                params.append(owner)
            elif iban and iban != "all":
                sql += " AND t.iban = ?"
                params.append(iban)
            sql += " ORDER BY t.date DESC LIMIT ?"
            params.append(limit)
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        # LIKE-fallback
        sql = "SELECT * FROM transactions WHERE (description LIKE ? OR category LIKE ?)"
        params = [like, like]
        if owner:
            sql += " AND owner = ?"
            params.append(owner)
        elif iban and iban != "all":
            sql += " AND iban = ?"
            params.append(iban)
        sql += " ORDER BY date DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def search_chat(q: str, chat_id: str, limit: int = 50) -> list[dict]:
    """Поиск по сообщениям чата пользователя. Возвращает совпадения
    с id беседы и её заголовком (для перехода из UI)."""
    fts = _fts_query(q)
    if not fts:
        return []
    like = f"%{q.strip()}%"
    with get_conn() as conn:
        if _fts_available(conn):
            sql = (
                "SELECT h.id, h.session_id, h.role, h.content, h.created_at, "
                "       s.title AS session_title "
                "FROM chat_history_fts f "
                "JOIN chat_history h ON h.id = f.rowid "
                "LEFT JOIN chat_sessions s ON s.id = h.session_id "
                "WHERE chat_history_fts MATCH ? AND h.chat_id = ? "
                "ORDER BY h.id DESC LIMIT ?"
            )
            return [dict(r) for r in conn.execute(sql, (fts, chat_id, limit)).fetchall()]
        sql = (
            "SELECT h.id, h.session_id, h.role, h.content, h.created_at, "
            "       s.title AS session_title "
            "FROM chat_history h "
            "LEFT JOIN chat_sessions s ON s.id = h.session_id "
            "WHERE h.content LIKE ? AND h.chat_id = ? "
            "ORDER BY h.id DESC LIMIT ?"
        )
        return [dict(r) for r in conn.execute(sql, (like, chat_id, limit)).fetchall()]


# ============================================================
# Подключение
# ============================================================
@contextmanager
def get_conn():
    """Контекстный менеджер соединения. Создаёт БД при первом обращении."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # лучшая конкурентность bot<->server
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _add_column_if_missing(conn, table: str, column: str, coltype: str):
    """Идемпотентно добавляет колонку. ALTER TABLE не поддерживает IF NOT EXISTS."""
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def _try_create_index(conn, name: str, on: str):
    """CREATE INDEX IF NOT EXISTS — безопасно."""
    conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {on}")


def init_db():
    """Создаёт таблицы и заполняет дефолтные настройки. Идемпотентно."""
    with _lock, get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     TEXT,
                username    TEXT,
                tx_count    INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  INTEGER NOT NULL REFERENCES sessions(id),
                date        TEXT,
                description TEXT,
                amount      REAL,
                type        TEXT,
                category    TEXT,
                week        TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
            CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);
            CREATE INDEX IF NOT EXISTS idx_tx_session ON transactions(session_id);

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS secrets (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sheets_config (
                id      INTEGER PRIMARY KEY CHECK (id = 1),
                sheet_id TEXT,
                enabled INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS accounts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     TEXT NOT NULL,
                iban        TEXT NOT NULL,
                holder      TEXT,
                first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(chat_id, iban)
            );
            CREATE TABLE IF NOT EXISTS chat_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     TEXT NOT NULL,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_chat ON chat_history(chat_id);
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     TEXT NOT NULL,
                title       TEXT NOT NULL DEFAULT 'Нова бесіда',
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_chat_sessions ON chat_sessions(chat_id);
            CREATE TABLE IF NOT EXISTS usage_log (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                TEXT NOT NULL DEFAULT (datetime('now')),
                user              TEXT NOT NULL,
                source            TEXT NOT NULL DEFAULT 'web',
                action            TEXT NOT NULL,
                model             TEXT DEFAULT '',
                api_calls         INTEGER DEFAULT 0,
                prompt_tokens     INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                cached_tokens     INTEGER DEFAULT 0,
                seconds           REAL DEFAULT 0,
                ok                INTEGER DEFAULT 1,
                details           TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts);
            CREATE INDEX IF NOT EXISTS idx_usage_user ON usage_log(user);
            CREATE TABLE IF NOT EXISTS access_grants (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                phone         TEXT NOT NULL UNIQUE,
                label         TEXT NOT NULL DEFAULT '',
                level         TEXT NOT NULL DEFAULT 'view',
                scope         TEXT NOT NULL DEFAULT 'all',
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at    TEXT,
                revoked       INTEGER NOT NULL DEFAULT 0,
                login_count   INTEGER NOT NULL DEFAULT 0,
                last_login_at TEXT
            );
        """)
        # --- миграции (добавляем колонки, если их ещё нет; идемпотентно) ---
        _add_column_if_missing(conn, "transactions", "iban", "TEXT")
        # власник-кабінет (телефон/chat_id, хто загрузив) — ізоляція даних клієнтів
        _add_column_if_missing(conn, "transactions", "owner", "TEXT")
        _try_create_index(conn, "idx_tx_owner", "transactions(owner)")
        _try_create_index(conn, "idx_tx_iban", "transactions(iban)")
        _add_column_if_missing(conn, "transactions", "country", "TEXT")
        _try_create_index(conn, "idx_tx_country", "transactions(country)")
        _add_column_if_missing(conn, "sheets_config", "credentials_json", "TEXT")
        _add_column_if_missing(conn, "chat_history", "session_id", "INTEGER")
        _try_create_index(conn, "idx_chat_session", "chat_history(session_id)")
        _add_column_if_missing(conn, "access_grants", "telegram_chat_id", "TEXT")
        _try_create_index(conn, "idx_grants_tg", "access_grants(telegram_chat_id)")
        # мультирахунок: спільний дашборд по всіх рахунках клієнта (платна опція)
        _add_column_if_missing(conn, "access_grants", "multi_account", "INTEGER NOT NULL DEFAULT 1")
        # дефолтные настройки (только если пусто)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (k, json.dumps(v, ensure_ascii=False)),
            )
        # миграция категорий: если в БД старые 8 → обновить на 14
        cur_cats = conn.execute(
            "SELECT value FROM settings WHERE key = 'categories'"
        ).fetchone()
        if cur_cats:
            try:
                old_cats = json.loads(cur_cats["value"])
                if len(old_cats) <= 8 and "експерти" not in old_cats:
                    conn.execute(
                        "UPDATE settings SET value = ? WHERE key = 'categories'",
                        (json.dumps(DEFAULT_SETTINGS["categories"], ensure_ascii=False),),
                    )
            except (json.JSONDecodeError, TypeError):
                pass

        # backfill: власник = chat_id сесії загрузки (для старих даних)
        conn.execute("""UPDATE transactions SET owner = (
                          SELECT s.chat_id FROM sessions s WHERE s.id = transactions.session_id)
                        WHERE owner IS NULL OR owner = ''""")

        # FTS5-индексы (после создания всех таблиц)
        _init_fts(conn)


# ============================================================
# Сессии (история загрузок)
# ============================================================
def create_session(chat_id: str, username: str = "") -> int:
    """Создаёт запись сессии загрузки. Возвращает id."""
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sessions(chat_id, username) VALUES (?, ?)",
            (chat_id, username),
        )
        return cur.lastrowid


def update_session_count(session_id: int, tx_count: int):
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET tx_count = ? WHERE id = ?",
            (tx_count, session_id),
        )


def list_sessions(limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# Транзакции
# ============================================================
def add_transactions(session_id: int, transactions: list[dict], iban: str = "",
                     owner: str = "") -> tuple[int, int]:
    """Пакетная вставка транзакций. Каждая: {date, description, amount, type, category, week, country}.
    iban: если задан, проставляется всем транзакциям этой партии.
    owner: кабинет-владелец (телефон) — изоляция данных между клиентами.
    Дедупликация: строки (дата, опис, сума), які ВЖЕ є у цього власця по цьому
    рахунку, пропускаються (повторна загрузка того ж файлу нічого не додає).
    Возвращает (вставлено, пропущено_дублікатів)."""
    if not transactions:
        return (0, 0)
    with _lock, get_conn() as conn:
        existing = set()
        if owner:
            rows_db = conn.execute(
                "SELECT date, description, amount FROM transactions WHERE owner = ?",
                (owner,)).fetchall()
            existing = {(r["date"], r["description"], r["amount"]) for r in rows_db}
        fresh, skipped = [], 0
        for t in transactions:
            key = (t["date"], t["description"], t["amount"])
            if key in existing:
                skipped += 1
                continue
            existing.add(key)
            fresh.append((session_id, t["date"], t["description"], t["amount"],
                          t["type"], t["category"], t.get("week", ""), iban,
                          t.get("country", ""), owner))
        rows = fresh
        conn.executemany(
            """INSERT INTO transactions
               (session_id, date, description, amount, type, category, week, iban, country, owner)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.execute(
            "UPDATE sessions SET tx_count = "
            "(SELECT COUNT(*) FROM transactions WHERE session_id = ?) WHERE id = ?",
            (session_id, session_id),
        )
    return (len(rows), skipped)


def get_transactions(*, date_from: str = "", date_to: str = "",
                     category: str = "", iban: str = "", chat_id: str = "",
                     month: str = "", year: str = "",
                     limit: int = 500) -> list[dict]:
    """Транзакции с фильтрами (для дашборда и ИИ-контекста).

    month: "YYYY-MM" (напр. "2026-03") — фильтр по месяцу.
    year: "YYYY" (напр. "2026") — фильтр по году.
    """
    sql = "SELECT t.* FROM transactions t WHERE 1=1"
    params: list = []
    if chat_id:
        # ізоляція кабінету: лише транзакції, загружені цим власником
        sql += " AND t.owner = ?"
        params.append(chat_id)
    if date_from:
        sql += " AND t.date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND t.date <= ?"
        params.append(date_to)
    if category and category != "all":
        sql += " AND t.category = ?"
        params.append(category)
    if iban and iban != "all":
        sql += " AND t.iban = ?"
        params.append(iban)
    if month:
        sql += " AND t.date LIKE ?"
        params.append(f"{month}-%")
    if year:
        sql += " AND t.date LIKE ?"
        params.append(f"{year}-%")
    sql += " ORDER BY t.date DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def delete_iban_data(iban: str) -> dict:
    """Удаляет ВСЕ данные, привязанные к IBAN:
    транзакции, история чата, беседы, сессии загрузки, привязка счёта.

    Возвращает количество удалённых записей по каждой таблице.
    Чисто для отчёта в UI. Пароль проверяется в route, не здесь.
    """
    if not iban:
        return {}
    with _lock, get_conn() as conn:
        # ID сессий загрузок, где chat_id = iban (бот сохраняет chat_id=iban для веб-загрузок,
        # но могут быть и реальные telegram chat_id, привязанные через accounts)
        session_ids = [
            r["id"] for r in conn.execute(
                "SELECT DISTINCT s.id FROM sessions s WHERE s.chat_id = ?",
                (iban,)
            ).fetchall()
        ]
        # транзакции по этому IBAN
        tx = conn.execute("DELETE FROM transactions WHERE iban = ?", (iban,)).rowcount
        # чат-история и беседы (chat_id хранит IBAN в веб-контексте)
        chat_msg = conn.execute("DELETE FROM chat_history WHERE chat_id = ?", (iban,)).rowcount
        chat_sess = conn.execute("DELETE FROM chat_sessions WHERE chat_id = ?", (iban,)).rowcount
        # сессии загрузок
        if session_ids:
            placeholders = ",".join("?" * len(session_ids))
            sess = conn.execute(
                f"DELETE FROM sessions WHERE id IN ({placeholders})", session_ids
            ).rowcount
        else:
            sess = 0
        # привязка счёта
        acc = conn.execute("DELETE FROM accounts WHERE iban = ?", (iban,)).rowcount
        return {
            "transactions": tx,
            "chat_messages": chat_msg,
            "chat_sessions": chat_sess,
            "upload_sessions": sess,
            "accounts": acc,
        }


def get_counterparties(iban: str = "", limit: int = 60, owner: str = "") -> list[dict]:
    """Агрегация транзакций по описанию (контрагентам) — для контекста ИИ-чата.

    Позволяет отвечать на вопросы вида «сколько принёс клиент #5619»:
    каждая строка = описание, число платежей, сумма прихода и расхода.
    Сортировка по общему обороту.
    """
    sql = (
        "SELECT description, COUNT(*) AS cnt, "
        "  SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS income, "
        "  SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) AS expense "
        "FROM transactions WHERE description IS NOT NULL AND description != ''"
    )
    params: list = []
    if owner:
        sql += " AND owner = ?"
        params.append(owner)
    elif iban and iban != "all":
        sql += " AND iban = ?"
        params.append(iban)
    sql += " GROUP BY description ORDER BY (income + expense) DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return [
            {
                "description": r["description"],
                "count": r["cnt"],
                "income": round(r["income"] or 0, 2),
                "expense": round(r["expense"] or 0, 2),
            }
            for r in conn.execute(sql, params).fetchall()
        ]


def get_periods(iban: str = "", chat_id: str = "") -> dict:
    """Доступные месяцы и годы из БД (для dropdown переключателя).

    chat_id — кабинет клиента (телефон): только его счета.
    """
    sql = "SELECT DISTINCT substr(date,1,7) as ym FROM transactions WHERE date IS NOT NULL AND length(date) >= 7"
    params: list = []
    if chat_id:
        sql += " AND owner = ?"
        params.append(chat_id)
    elif iban and iban != "all":
        sql += " AND iban = ?"
        params.append(iban)
    sql += " ORDER BY ym"
    months = []
    years_set = set()
    with get_conn() as conn:
        for row in conn.execute(sql, params).fetchall():
            ym = row["ym"]
            months.append(ym)
            years_set.add(ym[:4])
    return {"months": months, "years": sorted(years_set)}


# Маппинг украинских названий месяцев → номер (для P&L по B1)
_MONTHS_UA = {
    "січень": 1, "лютий": 2, "березень": 3, "квітень": 4, "травень": 5, "червень": 6,
    "липень": 7, "серпень": 8, "вересень": 9, "жовтень": 10, "листопад": 11, "грудень": 12,
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}


def parse_month_label(label: str) -> tuple[int, int] | None:
    """'Березень' → (3, текущий_год). 'Березень 2026' → (3, 2026). None если не распознано."""
    if not label:
        return None
    parts = label.strip().lower().split()
    month = _MONTHS_UA.get(parts[0])
    if not month:
        return None
    year = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else datetime.now().year
    return (month, year)


def get_pl_data(month: tuple[int, int] | None = None) -> dict:
    """Агрегат транзакций по (country, category) за указанный месяц.

    month: (номер_месяца, год). Если None — берёт из B1 (через get_pl_month) или текущий.
    Возвращает {(country, category): {"income": float, "expense": float}}.
    """
    if month is None:
        month = (datetime.now().month, datetime.now().year)
    m, y = month
    # даты в формате YYYY-MM-DD; фильтр по префиксу "YYYY-MM"
    prefix = f"{y:04d}-{m:02d}"
    sql = (
        "SELECT country, category, "
        "  SUM(CASE WHEN amount >= 0 THEN amount ELSE 0 END) AS income, "
        "  SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) AS expense "
        "FROM transactions WHERE date LIKE ? "
        "GROUP BY country, category"
    )
    result = {}
    with get_conn() as conn:
        for row in conn.execute(sql, (f"{prefix}%",)):
            country = (row["country"] or "UA").upper()
            cat = row["category"] or "інше"
            result[(country, cat)] = {
                "income": round(row["income"] or 0, 2),
                "expense": round(row["expense"] or 0, 2),
            }
    return result


def get_pl_data_year(year: int) -> dict:
    """Агрегат транзакций по (country, category) за весь год.

    Показує що є, навіть якщо рік ще не закінчено.
    """
    prefix = f"{year:04d}-"
    sql = (
        "SELECT country, category, "
        "  SUM(CASE WHEN amount >= 0 THEN amount ELSE 0 END) AS income, "
        "  SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) AS expense "
        "FROM transactions WHERE date LIKE ? "
        "GROUP BY country, category"
    )
    result = {}
    with get_conn() as conn:
        for row in conn.execute(sql, (f"{prefix}%",)):
            country = (row["country"] or "UA").upper()
            cat = row["category"] or "інше"
            result[(country, cat)] = {
                "income": round(row["income"] or 0, 2),
                "expense": round(row["expense"] or 0, 2),
            }
    return result


# ============================================================
# Настройки (пороги, категории)
# ============================================================
def get_setting(key: str, default=None):
    """Читает настройку (десериализует JSON)."""
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]


def set_setting(key: str, value):
    """Записывает настройку (сериализует в JSON)."""
    v = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, v),
        )


# ============================================================
# Секреты (зашифрованные)
# ============================================================
def get_secret(key: str) -> str:
    """Читает и РАСШИФРОВЫВАЕТ секрет. Пусто, если нет."""
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM secrets WHERE key = ?", (key,)).fetchone()
        if not row:
            return ""
        return decrypt(row["value"])


def set_secret(key: str, plaintext: str):
    """Шифрует и сохраняет секрет. Пустое значение очищает запись."""
    enc = encrypt(plaintext) if plaintext else ""
    with _lock, get_conn() as conn:
        if enc:
            conn.execute(
                "INSERT INTO secrets(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, enc),
            )
        else:
            conn.execute("DELETE FROM secrets WHERE key = ?", (key,))


def secret_is_set(key: str) -> bool:
    """Есть ли секрет (без расшифровки) — для показа статуса в UI."""
    with get_conn() as conn:
        return bool(
            conn.execute("SELECT 1 FROM secrets WHERE key = ? AND value != ''", (key,)).fetchone()
        )


# ============================================================
# Google Sheets конфиг (опция экспорта)
# ============================================================
def get_sheets_config() -> dict:
    """Возвращает конфиг Sheets: sheet_id, enabled, has_credentials (bool).

    Сам JSON-ключ НЕ возвращает здесь (он зашифрован в credentials_json).
    Для доступа к расшифрованному ключу — get_sheets_credentials().
    """
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sheets_config WHERE id = 1").fetchone()
        if not row:
            return {"sheet_id": "", "enabled": False, "has_credentials": False}
        return {
            "sheet_id": row["sheet_id"] or "",
            "enabled": bool(row["enabled"]),
            "has_credentials": bool(row["credentials_json"]),
        }


def set_sheets_config(sheet_id: str, enabled: bool, credentials_json: str = ""):
    """Сохраняет конфиг. credentials_json шифруется перед записью.

    Пустой credentials_json = не менять существующие креды (для формы, где поле опционально).
    Чтобы ОЧИСТИТЬ креды — передай credentials_json="__CLEAR__".
    """
    from dashboard.crypto import encrypt, decrypt  # локально, чтобы избежать цикла
    with _lock, get_conn() as conn:
        # текущая строка (для сохранения кредов, если не переданы новые)
        cur = conn.execute("SELECT credentials_json FROM sheets_config WHERE id = 1").fetchone()
        cur_creds = cur["credentials_json"] if cur else None

        if credentials_json == "__CLEAR__":
            enc_creds = ""
        elif credentials_json:
            enc_creds = encrypt(credentials_json)
        else:
            enc_creds = cur_creds or ""  # не меняем

        conn.execute(
            "INSERT INTO sheets_config(id, sheet_id, enabled, credentials_json) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "sheet_id = excluded.sheet_id, enabled = excluded.enabled, "
            "credentials_json = excluded.credentials_json",
            (sheet_id, int(enabled), enc_creds),
        )


def get_sheets_credentials() -> str:
    """Расшифрованный JSON Service Account или пустая строка."""
    from dashboard.crypto import decrypt
    with get_conn() as conn:
        row = conn.execute(
            "SELECT credentials_json FROM sheets_config WHERE id = 1"
        ).fetchone()
        if not row or not row["credentials_json"]:
            return ""
        try:
            return decrypt(row["credentials_json"])
        except Exception:
            return ""


# ============================================================
# Статистика (для главной админки / здоровья)
# ============================================================
def stats() -> dict:
    with get_conn() as conn:
        tx = conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"]
        sess = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
        return {"transactions": tx, "sessions": sess}


# ============================================================
# Accounts (связка chat_id ↔ IBAN)
# ============================================================
def register_account(chat_id: str, iban: str, holder: str = "") -> bool:
    """Привязывает IBAN к chat_id. Идемпотентно (UNIQUE). Возвращает True если новый."""
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO accounts(chat_id, iban, holder) VALUES (?, ?, ?)",
            (chat_id, iban, holder),
        )
        return cur.rowcount > 0


def list_accounts(chat_id: str = "") -> list[dict]:
    """Счета пользователя (или все, если chat_id не задан)."""
    with get_conn() as conn:
        if chat_id:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE chat_id = ? ORDER BY first_seen", (chat_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM accounts ORDER BY first_seen").fetchall()
        return [dict(r) for r in rows]


def account_stats(chat_id: str = "") -> list[dict]:
    """Счета + агрегированные метрики по каждому (для /accounts и дашборда)."""
    with get_conn() as conn:
        if chat_id:
            rows = conn.execute(
                """SELECT a.iban, a.holder,
                          COUNT(t.id) AS tx_count,
                          COALESCE(SUM(CASE WHEN t.amount > 0 THEN t.amount END), 0) AS income,
                          COALESCE(SUM(CASE WHEN t.amount < 0 THEN ABS(t.amount) END), 0) AS expense
                   FROM accounts a
                   LEFT JOIN transactions t ON t.iban = a.iban AND t.owner = ?
                   WHERE a.chat_id = ?
                   GROUP BY a.iban, a.holder
                   ORDER BY a.first_seen""",
                (chat_id, chat_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT a.iban, a.holder,
                          COUNT(t.id) AS tx_count,
                          COALESCE(SUM(CASE WHEN t.amount > 0 THEN t.amount END), 0) AS income,
                          COALESCE(SUM(CASE WHEN t.amount < 0 THEN ABS(t.amount) END), 0) AS expense
                   FROM accounts a
                   LEFT JOIN transactions t ON t.iban = a.iban
                   GROUP BY a.iban, a.holder
                   ORDER BY a.first_seen""",
            ).fetchall()
        return [dict(r) for r in rows]


# ============================================================
# Chat sessions (беседы как в ChatGPT)
# ============================================================
def create_chat_session(chat_id: str, title: str = "Нова бесіда") -> int:
    """Создаёт новую беседу. Возвращает id."""
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO chat_sessions(chat_id, title) VALUES (?, ?)",
            (chat_id, title),
        )
        return cur.lastrowid


def list_chat_sessions(chat_id: str) -> list[dict]:
    """Список бесед пользователя (новые сверху)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_sessions WHERE chat_id = ? ORDER BY updated_at DESC",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def rename_chat_session(session_id: int, title: str) -> bool:
    """Переименовать беседу."""
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "UPDATE chat_sessions SET title = ?, updated_at = datetime('now') WHERE id = ?",
            (title, session_id),
        )
        return cur.rowcount > 0


def delete_chat_session(session_id: int, chat_id: str) -> bool:
    """Удаляет беседу + её сообщения (cascade). Проверяет владение (chat_id)."""
    with _lock, get_conn() as conn:
        # проверка владения
        row = conn.execute(
            "SELECT 1 FROM chat_sessions WHERE id = ? AND chat_id = ?",
            (session_id, chat_id),
        ).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM chat_history WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        return True


def update_session_title_if_first(session_id: int, user_message: str):
    """Если это первое сообщение в беседе — ставит заголовок из него (до 40 символов)."""
    with _lock, get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) c FROM chat_history WHERE session_id = ? AND role = 'user'",
            (session_id,),
        ).fetchone()["c"]
        if count <= 1:  # первое сообщение (только что добавленное)
            title = user_message[:40] + ("..." if len(user_message) > 40 else "")
            conn.execute(
                "UPDATE chat_sessions SET title = ?, updated_at = datetime('now') WHERE id = ?",
                (title, session_id),
            )


def touch_session(session_id: int):
    """Обновляет updated_at беседы (для сортировки)."""
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE chat_sessions SET updated_at = datetime('now') WHERE id = ?",
            (session_id,),
        )


# ============================================================
# Chat history (сообщения — с поддержкой session_id)
# ============================================================
def add_chat_message(chat_id: str, role: str, content: str, session_id: int | None = None):
    """Сохраняет сообщение чата. session_id привязывает к конкретной беседе."""
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_history(chat_id, role, content, session_id) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, session_id),
        )


def get_chat_history(chat_id: str, limit: int = 10, session_id: int | None = None) -> list[dict]:
    """Последние N сообщений (хронологически). Если session_id — только этой беседы."""
    with get_conn() as conn:
        if session_id is not None:
            rows = conn.execute(
                "SELECT role, content FROM chat_history WHERE chat_id = ? AND session_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (chat_id, session_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT role, content FROM chat_history WHERE chat_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def clear_chat_history(chat_id: str, session_id: int | None = None) -> int:
    """Очищает историю. Если session_id — только этой беседы."""
    with _lock, get_conn() as conn:
        if session_id is not None:
            cur = conn.execute(
                "DELETE FROM chat_history WHERE chat_id = ? AND session_id = ?",
                (chat_id, session_id),
            )
        else:
            cur = conn.execute("DELETE FROM chat_history WHERE chat_id = ?", (chat_id,))
        return cur.rowcount


# ============================================================
# Журнал использования (действия + токены AI) — для админки
# ============================================================
def log_usage(user: str, action: str, *, source: str = "web", model: str = "",
              api_calls: int = 0, prompt_tokens: int = 0, completion_tokens: int = 0,
              cached_tokens: int = 0, seconds: float = 0.0, ok: bool = True,
              details: str = ""):
    """Логирует действие пользователя (чат, загрузка, вход, админ-операция).

    Статистика не должна валить основной поток — все ошибки глотаются.
    """
    try:
        with _lock, get_conn() as conn:
            conn.execute(
                """INSERT INTO usage_log(user, source, action, model, api_calls,
                                         prompt_tokens, completion_tokens,
                                         cached_tokens, seconds, ok, details)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user, source, action, model, api_calls,
                 prompt_tokens, completion_tokens, cached_tokens,
                 round(seconds, 2), 1 if ok else 0, details[:500]),
            )
    except Exception:
        pass


def usage_totals() -> dict:
    """Сводка по всему журналу: действия, API-запросы, токены, среднее время."""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) AS actions,
                   COALESCE(SUM(api_calls), 0)         AS api_calls,
                   COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(cached_tokens), 0)     AS cached_tokens,
                   COALESCE(AVG(NULLIF(seconds, 0)), 0) AS avg_seconds
            FROM usage_log
        """).fetchone()
        return dict(row)


def usage_by_user(limit: int = 20) -> list[dict]:
    """Агрегация по пользователям: активность и токены, сортировка по свежести."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT user,
                   MAX(ts) AS last_ts,
                   COUNT(*) AS actions,
                   COALESCE(SUM(api_calls), 0) AS api_calls,
                   COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
                   COALESCE(SUM(cached_tokens), 0) AS cached_tokens
            FROM usage_log
            GROUP BY user
            ORDER BY last_ts DESC
            LIMIT ?
        """, (limit,)).fetchall()
        result = [dict(r) for r in rows]
    for r in result:
        r["last_ts_kyiv"] = human_kyiv(r.get("last_ts"))
    return result


def usage_recent(limit: int = 50) -> list[dict]:
    """Последние N записей журнала (свежие сверху)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ts, user, source, action, model, api_calls, prompt_tokens,
                      completion_tokens, cached_tokens, seconds, ok, details
               FROM usage_log ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        result = [dict(r) for r in rows]
    for r in result:
        r["ts_kyiv"] = utc_to_kyiv(r.get("ts"), "%d.%m %H:%M")
    return result


# ============================================================
# Доступы по телефону (выдаёт/отзывает админ)
# ============================================================
GRANT_LEVELS = {"view": "перегляд", "chat": "перегляд + чат", "full": "повний"}


def normalize_phone(raw: str) -> str:
    """Приводит телефон к формату +380XXXXXXXXX. Пусто — если не похож на укр. номер."""
    import re
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("380") and len(digits) == 12:
        return "+" + digits
    if digits.startswith("80") and len(digits) == 11:
        return "+3" + digits
    if len(digits) == 10 and digits.startswith("0"):
        return "+380" + digits[1:]      # 0671234567 → +380671234567
    if len(digits) == 9:
        return "+380" + digits
    return ""


def mask_phone(phone: str) -> str:
    """+380671234567 → +38067•••67 (для отображения в админке)."""
    return f"{phone[:6]}•••{phone[-2:]}" if len(phone) >= 8 else phone


def create_grant(phone: str, label: str, level: str, scope: str,
                 days: int | None, multi_account: bool = True) -> dict:
    """Создаёт/обновляет доступ по телефону (телефон уникален — повтор = перевыдача).

    days=None → без срока. multi_account — спільний дашборд по всіх рахунках
    (платна опція; без нього клієнт бачить лише перший рахунок).
    """
    expires = None
    if days is not None:
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    with _lock, get_conn() as conn:
        conn.execute(
            """INSERT INTO access_grants(phone, label, level, scope, expires_at, multi_account)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(phone) DO UPDATE SET
                 label=excluded.label, level=excluded.level, scope=excluded.scope,
                 expires_at=excluded.expires_at, revoked=0,
                 multi_account=excluded.multi_account""",
            (phone, label, level, scope, expires, 1 if multi_account else 0),
        )
    return {"phone": phone, "label": label, "level": level, "scope": scope,
            "expires_at": expires}


def list_grants() -> list[dict]:
    """Все доступы + вычисленный статус (active/expired/revoked) + киевское время."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM access_grants ORDER BY id DESC").fetchall()
        grants = [dict(r) for r in rows]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    for g in grants:
        if g["revoked"]:
            g["status"] = "revoked"
        elif g["expires_at"] and g["expires_at"] < now:
            g["status"] = "expired"
        else:
            g["status"] = "active"
        g["expires_kyiv"] = utc_to_kyiv(g.get("expires_at"))
        g["last_login_kyiv"] = human_kyiv(g.get("last_login_at"))
    return grants


# ============================================================
# Время для отображения в админке (БД хранит UTC → показываем Киев)
# ============================================================
def utc_to_kyiv(ts: str | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """'2026-09-18 16:11' (UTC) → '18.09.2026 19:11' (Київ)."""
    if not ts:
        return ""
    try:
        dt = datetime.strptime(ts[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("Europe/Kyiv")).strftime(fmt)
    except ValueError:
        return ts


def human_kyiv(ts: str | None) -> str:
    """Человеческое «когда было»: сьогодні 19:37 / вчора 21:05 / 17.08 14:20."""
    if not ts:
        return ""
    try:
        dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S" if ts.count(":") == 2 else "%Y-%m-%d %H:%M")
        dt = dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/Kyiv"))
    except ValueError:
        return ts
    now_k = datetime.now(ZoneInfo("Europe/Kyiv"))
    if dt.date() == now_k.date():
        return "сьогодні " + dt.strftime("%H:%M")
    if (now_k.date() - dt.date()).days == 1:
        return "вчора " + dt.strftime("%H:%M")
    if dt.year == now_k.year:
        return dt.strftime("%d.%m %H:%M")
    return dt.strftime("%d.%m.%Y")


def find_active_grant(phone: str) -> dict | None:
    """Активный доступ по телефону (не отозван и не истёк) или None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM access_grants WHERE phone = ? AND revoked = 0",
            (phone,)).fetchone()
    if not row:
        return None
    g = dict(row)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if g["expires_at"] and g["expires_at"] < now:
        return None
    return g


def set_grant_level(gid: int, level: str) -> bool:
    """Меняет уровень прав выданного доступа (view/chat/full)."""
    if level not in GRANT_LEVELS:
        return False
    with _lock, get_conn() as conn:
        cur = conn.execute("UPDATE access_grants SET level = ? WHERE id = ?",
                           (level, gid))
        return cur.rowcount > 0


def set_grant_multi(gid: int, on: bool) -> bool:
    """Вмикає/вимикає мультирахунок (спільний дашборд по всіх рахунках)."""
    with _lock, get_conn() as conn:
        cur = conn.execute("UPDATE access_grants SET multi_account = ? WHERE id = ?",
                           (1 if on else 0, gid))
        return cur.rowcount > 0


def revoke_grant(gid: int) -> bool:
    with _lock, get_conn() as conn:
        cur = conn.execute("UPDATE access_grants SET revoked = 1 WHERE id = ?", (gid,))
        return cur.rowcount > 0


def extend_grant(gid: int, days: int) -> bool:
    """Продлевает доступ на N дней от максимума (сейчас / текущий срок)."""
    with get_conn() as conn:
        row = conn.execute("SELECT expires_at FROM access_grants WHERE id = ?", (gid,)).fetchone()
        if not row:
            return False
        now = datetime.now(timezone.utc)
        if row["expires_at"]:
            try:
                base = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                base = max(base, now)
            except ValueError:
                base = now
        else:
            base = now
        new_exp = (base + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
        conn.execute("UPDATE access_grants SET expires_at = ?, revoked = 0 WHERE id = ?",
                     (new_exp, gid))
        return True


def touch_grant_login(gid: int):
    """Отмечает вход: счётчик + время."""
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE access_grants SET login_count = login_count + 1,"
            " last_login_at = datetime('now') WHERE id = ?", (gid,))


# ============================================================
# Привязка Telegram-чата к кабинету клиента (номер = ключ)
# ============================================================
def bind_telegram(chat_id, phone: str) -> dict | None:
    """Привязывает Telegram-чат к активному доступу по телефону."""
    norm = normalize_phone(phone)
    if not norm:
        return None
    grant = find_active_grant(norm)
    if not grant:
        return None
    with _lock, get_conn() as conn:
        # один Telegram-чат — один кабинет; у телефона может быть только один чат
        conn.execute("UPDATE access_grants SET telegram_chat_id = NULL WHERE telegram_chat_id = ?",
                     (str(chat_id),))
        conn.execute("UPDATE access_grants SET telegram_chat_id = ? WHERE id = ?",
                     (str(chat_id), grant["id"]))
    return grant


def unbind_telegram(chat_id) -> bool:
    """Отвязывает чат (например, при отзыве доступа)."""
    with _lock, get_conn() as conn:
        cur = conn.execute("UPDATE access_grants SET telegram_chat_id = NULL WHERE telegram_chat_id = ?",
                           (str(chat_id),))
        return cur.rowcount > 0


def find_grant_by_telegram(chat_id) -> dict | None:
    """Активный доступ, привязанный к Telegram-чату (или None)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM access_grants WHERE telegram_chat_id = ? AND revoked = 0",
            (str(chat_id),)).fetchone()
    if not row:
        return None
    g = dict(row)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if g["expires_at"] and g["expires_at"] < now:
        return None
    return g


def owner_telegram_chats() -> list[str]:
    """Telegram-чаты владельца (привязаны командой /owner с паролем админки)."""
    val = get_setting("owner_telegram_chats")
    try:
        return [str(x) for x in (val or [])]
    except TypeError:
        return []


def add_owner_telegram_chat(chat_id) -> bool:
    chats = owner_telegram_chats()
    if str(chat_id) in chats:
        return False
    chats.append(str(chat_id))
    set_setting("owner_telegram_chats", chats)
    return True
