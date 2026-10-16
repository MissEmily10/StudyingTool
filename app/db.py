"""Хранилище уроков в SQLite."""
import sqlite3
from datetime import datetime
from typing import Any

from . import config

STATUSES = {
    "scheduled": "Запланирован",
    "joining": "Бот заходит в вебинар",
    "recording": "Идёт запись",
    "queued": "В очереди на обработку",
    "transcribing": "Распознаю речь",
    "summarizing": "Пишу конспект",
    "waiting": "Ждёт конспекта от Claude",
    "done": "Готово",
    "error": "Ошибка",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    teacher TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    webinar_url TEXT NOT NULL DEFAULT '',
    materials_url TEXT NOT NULL DEFAULT '',
    starts_at TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    stop_requested INTEGER NOT NULL DEFAULT 0,
    transcript TEXT NOT NULL DEFAULT '',
    konspekt TEXT NOT NULL DEFAULT '',
    shpora TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    log TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


# Колонки, добавленные после первой версии (для старых баз добавляются при запуске)
NEW_COLUMNS = {
    "shpora": "TEXT NOT NULL DEFAULT ''",
    "quiz": "TEXT NOT NULL DEFAULT ''",      # quiz.json: вопросы теста
    "videos": "TEXT NOT NULL DEFAULT ''",    # videos.md: подборка видео
    "notified": "INTEGER NOT NULL DEFAULT 0",  # конспект уже отправлен в Telegram
    "wait_minutes": "INTEGER NOT NULL DEFAULT 0",  # начало может сдвинуться на столько минут (плавающий график)
    "repeat_weekly": "INTEGER NOT NULL DEFAULT 0",  # повторять запись каждую неделю
}

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER,
    text TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',   -- new → asked (ушёл Claude) → answered
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(EXTRA_SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(lessons)")}
        for col, ddl in NEW_COLUMNS.items():  # база из прошлой версии
            if col not in cols:
                conn.execute(f"ALTER TABLE lessons ADD COLUMN {col} {ddl}")


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create(**fields: Any) -> int:
    fields.setdefault("created_at", now())
    fields["updated_at"] = now()
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with connect() as conn:
        cur = conn.execute(f"INSERT INTO lessons ({cols}) VALUES ({marks})", list(fields.values()))
        return int(cur.lastrowid)


def get(lesson_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    return dict(row) if row else None


def all_lessons() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM lessons ORDER BY COALESCE(starts_at, created_at) DESC, id DESC").fetchall()
    return [dict(r) for r in rows]


def update(lesson_id: int, **fields: Any) -> None:
    fields["updated_at"] = now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE lessons SET {sets} WHERE id = ?", [*fields.values(), lesson_id])


def log(lesson_id: int, message: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n"
    with connect() as conn:
        conn.execute("UPDATE lessons SET log = log || ?, updated_at = ? WHERE id = ?", (line, now(), lesson_id))


def delete(lesson_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM lessons WHERE id = ?", (lesson_id,))


def due_scheduled(until_iso: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM lessons WHERE status = 'scheduled' AND starts_at <= ?", (until_iso,)
        ).fetchall()
    return [dict(r) for r in rows]



def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def add_question(lesson_id: int | None, text: str, status: str = "new") -> int:
    with connect() as conn:
        cur = conn.execute("INSERT INTO questions (lesson_id, text, status, created_at) VALUES (?, ?, ?, ?)",
                           (lesson_id, text, status, now()))
        return int(cur.lastrowid)


def questions(status: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM questions WHERE status = ? ORDER BY id", (status,)).fetchall()
    return [dict(r) for r in rows]


def answer_question(qid: int, answer: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE questions SET answer = ?, status = 'answered' WHERE id = ?", (answer, qid))


def set_question_status(qid: int, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE questions SET status = ? WHERE id = ?", (status, qid))
