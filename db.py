"""Хранилище бота: SQLite, суммы в центах (int), время по Парижу."""
import os
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Paris"))
DB_PATH = os.getenv("DB_PATH", "budget.db")

TOTAL = 0  # category_id для общего месячного лимита

DEFAULT_CATEGORIES = {
    "expense": [
        ("🛒", "Продукты"), ("🍽", "Кафе и рестораны"), ("🏠", "Жильё"),
        ("💡", "Коммунальные"), ("🚌", "Транспорт"), ("💊", "Здоровье"),
        ("👕", "Одежда"), ("🎭", "Досуг"), ("✈️", "Путешествия"),
        ("📱", "Связь и подписки"), ("🎁", "Подарки"), ("📦", "Другое"),
    ],
    "income": [("💼", "Зарплата"), ("💸", "Подработка"), ("📦", "Другое")],
}


def now() -> datetime:
    return datetime.now(TZ)


def month_key(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m")


def prev_month_key(key: str) -> str:
    y, m = map(int, key.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


class _ClosingConnection(sqlite3.Connection):
    """`with connect() as c:` — коммит и сразу закрытие соединения (иначе копятся открытые файлы)."""

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init() -> None:
    d = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(d, exist_ok=True)
    with connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                name  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS categories (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                kind   TEXT NOT NULL CHECK (kind IN ('expense','income')),
                emoji  TEXT NOT NULL DEFAULT '•',
                name   TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                UNIQUE (kind, name)
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                kind        TEXT NOT NULL CHECK (kind IN ('expense','income')),
                amount      INTEGER NOT NULL CHECK (amount > 0),
                category_id INTEGER NOT NULL REFERENCES categories(id),
                user_id     INTEGER NOT NULL,
                comment     TEXT,
                created_at  TEXT NOT NULL,
                month       TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_tx_month ON transactions(month, kind);
            CREATE TABLE IF NOT EXISTS limits (
                category_id INTEGER PRIMARY KEY,
                amount      INTEGER NOT NULL CHECK (amount > 0)
            );
            """
        )
        if c.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            for kind, items in DEFAULT_CATEGORIES.items():
                c.executemany(
                    "INSERT INTO categories(kind, emoji, name) VALUES (?,?,?)",
                    [(kind, e, n) for e, n in items],
                )


# ---------- суммы ----------

_AMOUNT_RE = re.compile(r"^\s*€?\s*(\d[\d\s]*(?:[.,]\d{1,2})?)(?![\d.,])\s*(?:€|eur|евро)?\s*(.*)$", re.I)


def parse_amount(text: str) -> tuple[int, str] | None:
    """'12,50 кофе' -> (1250, 'кофе'). None, если суммы нет."""
    m = _AMOUNT_RE.match(text or "")
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace(",", ".")
    try:
        cents = round(float(raw) * 100)
    except ValueError:
        return None
    if cents <= 0 or cents > 100_000_000:
        return None
    return cents, m.group(2).strip()


def fmt(cents: int) -> str:
    s = f"{cents / 100:,.2f}".replace(",", " ").replace(".", ",")
    if s.endswith(",00"):
        s = s[:-3]
    return f"{s} €"


# ---------- пользователи ----------

def upsert_user(tg_id: int, name: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO users(tg_id, name) VALUES (?,?) "
            "ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name",
            (tg_id, name),
        )


def users() -> dict[int, str]:
    with connect() as c:
        return {r["tg_id"]: r["name"] for r in c.execute("SELECT * FROM users")}


# ---------- категории ----------

def categories(kind: str) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT * FROM categories WHERE kind=? AND active=1 ORDER BY id", (kind,)
        ).fetchall()


def category(cat_id: int) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM categories WHERE id=?", (cat_id,)).fetchone()


def add_category(kind: str, emoji: str, name: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT INTO categories(kind, emoji, name) VALUES (?,?,?) "
            "ON CONFLICT(kind, name) DO UPDATE SET active=1, emoji=excluded.emoji",
            (kind, emoji, name),
        )


def hide_category(cat_id: int) -> None:
    """Категория скрывается, но старые записи сохраняются."""
    with connect() as c:
        c.execute("UPDATE categories SET active=0 WHERE id=?", (cat_id,))
        c.execute("DELETE FROM limits WHERE category_id=?", (cat_id,))


# ---------- операции ----------

def add_tx(kind: str, amount: int, cat_id: int, user_id: int, comment: str = "",
           when: str | None = None) -> int:
    """when — дата чека 'YYYY-MM-DD'; по умолчанию сейчас."""
    t = now()
    if when:
        d = datetime.fromisoformat(when)
        t = t.replace(year=d.year, month=d.month, day=d.day)
    with connect() as c:
        cur = c.execute(
            "INSERT INTO transactions(kind, amount, category_id, user_id, comment, created_at, month) "
            "VALUES (?,?,?,?,?,?,?)",
            (kind, amount, cat_id, user_id, comment or None, t.isoformat(timespec="seconds"), month_key(t)),
        )
        return cur.lastrowid


def get_tx(tx_id: int) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute(
            "SELECT t.*, c.name AS cat_name, c.emoji FROM transactions t "
            "JOIN categories c ON c.id=t.category_id WHERE t.id=?",
            (tx_id,),
        ).fetchone()


def delete_tx(tx_id: int) -> bool:
    with connect() as c:
        return c.execute("DELETE FROM transactions WHERE id=?", (tx_id,)).rowcount > 0


def recent(limit: int = 10) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT t.*, c.name AS cat_name, c.emoji, u.name AS user_name FROM transactions t "
            "JOIN categories c ON c.id=t.category_id LEFT JOIN users u ON u.tg_id=t.user_id "
            "ORDER BY t.id DESC LIMIT ?",
            (limit,),
        ).fetchall()


# ---------- лимиты ----------

def set_limit(cat_id: int, amount: int) -> None:
    with connect() as c:
        if amount <= 0:
            c.execute("DELETE FROM limits WHERE category_id=?", (cat_id,))
        else:
            c.execute(
                "INSERT INTO limits(category_id, amount) VALUES (?,?) "
                "ON CONFLICT(category_id) DO UPDATE SET amount=excluded.amount",
                (cat_id, amount),
            )


def limits() -> dict[int, int]:
    with connect() as c:
        return {r["category_id"]: r["amount"] for r in c.execute("SELECT * FROM limits")}


def spent(month: str, cat_id: int = TOTAL) -> int:
    sql = "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE kind='expense' AND month=?"
    args: list = [month]
    if cat_id != TOTAL:
        sql += " AND category_id=?"
        args.append(cat_id)
    with connect() as c:
        return c.execute(sql, args).fetchone()[0]


def limit_alerts(cat_id: int, amount: int, month: str | None = None) -> list[str]:
    """Пороги 80 % и 100 %, которые пересекла только что добавленная трата."""
    month = month or month_key()
    lim = limits()
    alerts = []
    for cid in (cat_id, TOTAL):
        if cid not in lim:
            continue
        after = spent(month, cid)
        before = after - amount
        limit = lim[cid]
        label = "Общий бюджет" if cid == TOTAL else _cat_label(cid)
        if before < limit <= after:
            alerts.append(f"🔴 {label}: лимит превышен — {fmt(after)} из {fmt(limit)}")
        elif before < limit * 0.8 <= after:
            alerts.append(f"🟡 {label}: потрачено 80 % — {fmt(after)} из {fmt(limit)}, осталось {fmt(limit - after)}")
    return alerts


def _cat_label(cat_id: int) -> str:
    cat = category(cat_id)
    return f"{cat['emoji']} {cat['name']}" if cat else "?"


# ---------- отчёты ----------

def month_summary(month: str) -> dict:
    with connect() as c:
        by_cat = c.execute(
            "SELECT c.id, c.emoji, c.name, t.kind, SUM(t.amount) AS total "
            "FROM transactions t JOIN categories c ON c.id=t.category_id "
            "WHERE t.month=? GROUP BY c.id, t.kind ORDER BY total DESC",
            (month,),
        ).fetchall()
        by_user = c.execute(
            "SELECT COALESCE(u.name, t.user_id) AS name, SUM(t.amount) AS total "
            "FROM transactions t LEFT JOIN users u ON u.tg_id=t.user_id "
            "WHERE t.month=? AND t.kind='expense' GROUP BY t.user_id ORDER BY total DESC",
            (month,),
        ).fetchall()
        daily = c.execute(
            "SELECT CAST(substr(created_at, 9, 2) AS INTEGER) AS day, SUM(amount) AS total "
            "FROM transactions WHERE month=? AND kind='expense' GROUP BY day ORDER BY day",
            (month,),
        ).fetchall()
    expenses = [dict(r) for r in by_cat if r["kind"] == "expense"]
    incomes = [dict(r) for r in by_cat if r["kind"] == "income"]
    return {
        "month": month,
        "expenses": expenses,
        "incomes": incomes,
        "expense_total": sum(r["total"] for r in expenses),
        "income_total": sum(r["total"] for r in incomes),
        "by_user": [dict(r) for r in by_user],
        "daily": {r["day"]: r["total"] for r in daily},
        "limits": limits(),
    }


def export_rows() -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT t.created_at, t.kind, t.amount, c.name AS category, "
            "COALESCE(u.name, t.user_id) AS user, COALESCE(t.comment,'') AS comment "
            "FROM transactions t JOIN categories c ON c.id=t.category_id "
            "LEFT JOIN users u ON u.tg_id=t.user_id ORDER BY t.created_at"
        ).fetchall()
