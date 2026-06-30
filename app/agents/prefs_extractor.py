"""Prefs Extractor — implicit user-preference extraction (spec §6.4).

NOT YET WIRED. In the target design this runs as an async worker (Cloud Run Job
polling the ``async_jobs`` outbox): it reads a session's dialogue history,
extracts implicit preferences (tone, format, detail level) and upserts them into
``user_prefs``. Kept here as a placeholder so the module layout matches the
intended architecture; the prototype only applies *explicit* prefs (read in the
report agent).
"""
from __future__ import annotations


def extract_prefs(session_history: list) -> dict:
    """Return implicit preferences inferred from a session. Not implemented yet."""
    raise NotImplementedError("Prefs Extractor worker is not implemented in the prototype.")
