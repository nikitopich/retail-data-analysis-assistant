"""User-preferences repository over SQLite (spec §3.6, §6.4)."""
from __future__ import annotations

import sqlite3
from typing import Optional

from app.sources.db import get_connection


class UserPrefsRepo:
    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        self._conn = conn

    def _c(self) -> sqlite3.Connection:
        return self._conn or get_connection()

    def get_output_format(self, user_id: str) -> str:
        """Optional read of the default output format (defaults to 'table')."""
        cur = self._c().execute(
            "SELECT output_format FROM user_prefs WHERE user_id = ?", (user_id,)
        )
        row = cur.fetchone()
        if row and row["output_format"]:
            return row["output_format"]
        return "table"
