"""SQLite schema bootstrap + a process-wide connection helper (spec §3.6, §10)."""
from __future__ import annotations

import sqlite3
from typing import Optional

from app import config

DDL = """
-- Библиотека сохранённых отчётов (ядро High-Stakes Oversight)
CREATE TABLE IF NOT EXISTS saved_reports (
    id                  TEXT PRIMARY KEY,
    owner_id            TEXT NOT NULL,
    question            TEXT NOT NULL,
    sql_query           TEXT NOT NULL,
    report_md           TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    published_to_golden INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_saved_reports_owner   ON saved_reports(owner_id);
CREATE INDEX IF NOT EXISTS idx_saved_reports_created ON saved_reports(created_at);

-- Сырые тройки question->sql->report (write-only capture)
CREATE TABLE IF NOT EXISTS staged_trios (
    id          TEXT PRIMARY KEY,
    report_id   TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    question    TEXT NOT NULL,
    sql_query   TEXT NOT NULL,
    report_md   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Golden Bucket (схема определена; заполнение/ретривинг вне scope прототипа)
CREATE TABLE IF NOT EXISTS trios (
    id                 TEXT PRIMARY KEY,
    question           TEXT NOT NULL,
    sql_query          TEXT NOT NULL,
    report_md          TEXT NOT NULL,
    embedding          BLOB,
    faithfulness_score REAL,
    relevancy_score    REAL,
    format_score       REAL,
    usage_count        INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Предпочтения пользователя (схема определена; управление через диалог вне scope)
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id         TEXT PRIMARY KEY,
    output_format   TEXT NOT NULL DEFAULT 'table',
    tone_preference TEXT,
    extra_prefs     TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_shared: Optional[sqlite3.Connection] = None


def get_connection() -> sqlite3.Connection:
    """Return a cached, process-wide connection to the default DB.

    ``check_same_thread=False`` is safe here: LangGraph runs nodes synchronously
    on the main thread within the CLI loop.
    """
    global _shared
    if _shared is None:
        _shared = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _shared.row_factory = sqlite3.Row
    return _shared


def init_db(db_path: Optional[str] = None) -> None:
    """Create all tables (idempotent). Used by §9.4 bootstrap step."""
    if db_path is None:
        conn = get_connection()
        close = False
    else:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        close = True
    conn.executescript(DDL)
    conn.commit()
    if close:
        conn.close()
    print(f"SQLite initialized at {db_path or config.DB_PATH}")
