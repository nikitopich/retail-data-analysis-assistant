"""SQLite bootstrap and connection helpers (app/sources/db.py)."""
from __future__ import annotations

import sqlite3

import pytest

from app.sources import db


_EXPECTED_TABLES = {"saved_reports", "staged_trios", "trios", "user_prefs"}


def _tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def test_init_db_creates_all_tables(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_db(path)
    conn = sqlite3.connect(path)
    try:
        assert _EXPECTED_TABLES <= _tables(conn)
    finally:
        conn.close()


def test_init_db_is_idempotent(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_db(path)
    db.init_db(path)
    conn = sqlite3.connect(path)
    try:
        assert _EXPECTED_TABLES <= _tables(conn)
    finally:
        conn.close()


def test_init_db_default_path_uses_shared_connection(monkeypatch, tmp_path):
    db_path = str(tmp_path / "default.db")
    monkeypatch.setattr(db.config, "DB_PATH", db_path)
    db.get_connection.cache_clear()
    db.init_db()
    conn = db.get_connection()
    assert _EXPECTED_TABLES <= _tables(conn)
    db.get_connection.cache_clear()


def test_get_table_schema_text_returns_live_ddl(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        ddl = db.get_table_schema_text("saved_reports", conn)
    finally:
        conn.close()
    assert "CREATE TABLE" in ddl
    assert "saved_reports" in ddl
    for col in ("id", "owner_id", "question", "report_md", "published_to_golden"):
        assert col in ddl


def test_get_table_schema_text_unknown_table_raises(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        with pytest.raises(KeyError):
            db.get_table_schema_text("does_not_exist", conn)
    finally:
        conn.close()


def test_get_connection_is_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(db.config, "DB_PATH", str(tmp_path / "shared.db"))
    db.get_connection.cache_clear()
    c1 = db.get_connection()
    c2 = db.get_connection()
    assert c1 is c2
    assert c1.row_factory is sqlite3.Row
    db.get_connection.cache_clear()
