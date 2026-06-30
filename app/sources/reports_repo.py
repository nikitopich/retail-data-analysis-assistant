"""Repositories over the SQLite saved-reports library (spec §3.6, §4.2).

The critical security property lives here: ``preview``, ``run_select`` and
``execute_destructive`` ALWAYS inject ``owner_id`` server-side, so the user can
only ever see/affect their own reports — regardless of what the LLM produced.

KNOWN GAP (deferred): ``_inject_owner_scope`` appends the predicate by string
concatenation without parenthesizing the existing WHERE and without placing it
before a trailing ORDER BY/LIMIT. See memory ``owner-scope-security-gap``.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from typing import List, Optional, Tuple

from app.sources.db import get_connection


def _inject_owner_scope(sql: str, owner_id: str) -> Tuple[str, list]:
    """Append an owner-scoping predicate to a DELETE/UPDATE/SELECT.

    The ``owner_id`` comes from the application config — NEVER from the LLM.
    If a WHERE clause already exists we AND onto it; otherwise we add one.
    """
    s = sql.strip().rstrip(";").rstrip()
    if re.search(r"\bwhere\b", s, re.IGNORECASE):
        s = f"{s} AND owner_id = ?"
    else:
        s = f"{s} WHERE owner_id = ?"
    return s, [owner_id]


class SavedReportsRepo:
    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        self._conn = conn

    def _c(self) -> sqlite3.Connection:
        return self._conn or get_connection()

    def save(self, owner_id: str, question: str, sql_query: str, report_md: str) -> str:
        report_id = uuid.uuid4().hex
        conn = self._c()
        conn.execute(
            "INSERT INTO saved_reports (id, owner_id, question, sql_query, report_md) "
            "VALUES (?, ?, ?, ?, ?)",
            (report_id, owner_id, question, sql_query, report_md),
        )
        conn.commit()
        return report_id

    def preview(self, preview_sql: str, owner_id: str) -> List[dict]:
        """Run the destructive preview SELECT, scoped to the current owner."""
        scoped, params = _inject_owner_scope(preview_sql, owner_id)
        cur = self._c().execute(scoped, params)
        return [dict(row) for row in cur.fetchall()]

    def run_select(self, select_sql: str, owner_id: str) -> List[dict]:
        """Execute an owner-scoped SELECT on saved_reports."""
        scoped, params = _inject_owner_scope(select_sql, owner_id)
        cur = self._c().execute(scoped, params)
        return [dict(row) for row in cur.fetchall()]

    def execute_destructive(self, dml_sql: str, owner_id: str) -> int:
        """Execute the guarded DELETE/UPDATE, forcibly scoped to the owner.

        Returns the number of affected rows.
        """
        scoped, params = _inject_owner_scope(dml_sql, owner_id)
        conn = self._c()
        cur = conn.execute(scoped, params)
        conn.commit()
        return cur.rowcount


class StagedTriosRepo:
    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        self._conn = conn

    def _c(self) -> sqlite3.Connection:
        return self._conn or get_connection()

    def add(self, report_id: str, owner_id: str, question: str,
            sql_query: str, report_md: str) -> str:
        """Write-only capture of the raw trio (status='pending'). Never read."""
        trio_id = uuid.uuid4().hex
        conn = self._c()
        conn.execute(
            "INSERT INTO staged_trios (id, report_id, owner_id, question, sql_query, report_md) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (trio_id, report_id, owner_id, question, sql_query, report_md),
        )
        conn.commit()
        return trio_id
