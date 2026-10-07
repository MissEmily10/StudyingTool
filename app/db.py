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
    error TEXT NOT NULL DEFAULT '',
    log TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


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

