"""User-preferences repository over SQLite (spec §3.6, §6.4).

Read/write the per-user ``user_prefs`` row. Writes are an UPSERT keyed on the
``user_id`` PRIMARY KEY; ``upsert_prefs`` does a read-merge-write so callers can
update just one field (e.g. only the tone) without clobbering the others.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from app import config, errors
from app.retry import retry_with_backoff
from app.sources.db import get_connection

_sqlite_retry = retry_with_backoff(
    retry_on=errors.is_retryable_sqlite,
    max_retries=config.MAX_BACKOFF_RETRIES,
    base_seconds=config.BACKOFF_BASE_SECONDS,
    max_seconds=config.BACKOFF_MAX_SECONDS,
    on_exhausted=lambda e: errors.ServiceUnavailableError(str(e)),
)

# Defaults returned when a user has no row yet. ``output_format`` mirrors the
# table's NOT NULL DEFAULT ('table'); the other fields are optional (NULL).
_DEFAULTS = {"output_format": "table", "tone_preference": None, "extra_prefs": None}


class UserPrefsRepo:
    def __init__(self, conn: Optional[sqlite3.Connection] = None) -> None:
        self._conn = conn

    def _c(self) -> sqlite3.Connection:
        return self._conn or get_connection()

    @_sqlite_retry
    def get_prefs(self, user_id: str) -> dict:
        """Return all stored preferences for ``user_id`` (defaults if no row)."""
        cur = self._c().execute(
            "SELECT output_format, tone_preference, extra_prefs "
            "FROM user_prefs WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return dict(_DEFAULTS)
        return {
            "output_format": row["output_format"] or _DEFAULTS["output_format"],
            "tone_preference": row["tone_preference"],
            "extra_prefs": row["extra_prefs"],
        }

    def get_output_format(self, user_id: str) -> str:
        """Optional read of the default output format (defaults to 'table')."""
        return self.get_prefs(user_id)["output_format"]

    @_sqlite_retry
    def upsert_prefs(
        self,
        user_id: str,
        *,
        output_format: Optional[str] = None,
        tone_preference: Optional[str] = None,
        extra_prefs: Optional[str] = None,
    ) -> dict:
        """Partially update a user's preferences (read-merge-write UPSERT).

        Only the non-``None`` fields overwrite existing values; unspecified
        fields keep whatever was stored before. Returns the merged row.
        """
        current = self.get_prefs(user_id)
        merged = {
            "output_format": output_format if output_format is not None else current["output_format"],
            "tone_preference": tone_preference if tone_preference is not None else current["tone_preference"],
            "extra_prefs": extra_prefs if extra_prefs is not None else current["extra_prefs"],
        }
        conn = self._c()
        conn.execute(
            "INSERT INTO user_prefs (user_id, output_format, tone_preference, extra_prefs, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "    output_format   = excluded.output_format, "
            "    tone_preference = excluded.tone_preference, "
            "    extra_prefs     = excluded.extra_prefs, "
            "    updated_at      = datetime('now')",
            (user_id, merged["output_format"], merged["tone_preference"], merged["extra_prefs"]),
        )
        conn.commit()
        return merged
