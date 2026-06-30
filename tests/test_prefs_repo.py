"""UserPrefsRepo — read defaults + read-merge-write UPSERT (app/sources/prefs_repo.py)."""
from __future__ import annotations

from app.sources.prefs_repo import UserPrefsRepo

USER = "alice"


# --------------------------------------------------------------------------- #
# Reads / defaults
# --------------------------------------------------------------------------- #
def test_get_prefs_defaults_for_unknown_user(patched_conn):
    prefs = UserPrefsRepo().get_prefs(USER)
    assert prefs == {"output_format": "table", "tone_preference": None, "extra_prefs": None}


def test_get_output_format_defaults_to_table(patched_conn):
    assert UserPrefsRepo().get_output_format(USER) == "table"


# --------------------------------------------------------------------------- #
# UPSERT
# --------------------------------------------------------------------------- #
def test_upsert_inserts_new_row(patched_conn):
    UserPrefsRepo().upsert_prefs(USER, output_format="CSV", tone_preference="concise")
    prefs = UserPrefsRepo().get_prefs(USER)
    assert prefs["output_format"] == "CSV"
    assert prefs["tone_preference"] == "concise"
    assert prefs["extra_prefs"] is None
    # One row only (PK on user_id).
    assert patched_conn.execute("SELECT COUNT(*) FROM user_prefs").fetchone()[0] == 1


def test_upsert_updates_existing_row(patched_conn):
    repo = UserPrefsRepo()
    repo.upsert_prefs(USER, output_format="CSV")
    repo.upsert_prefs(USER, output_format="bulleted list")
    assert repo.get_output_format(USER) == "bulleted list"
    assert patched_conn.execute("SELECT COUNT(*) FROM user_prefs").fetchone()[0] == 1


def test_partial_upsert_preserves_other_fields(patched_conn):
    repo = UserPrefsRepo()
    repo.upsert_prefs(USER, output_format="CSV", tone_preference="formal")
    # A later turn changes only the tone — format must survive.
    repo.upsert_prefs(USER, tone_preference="friendly")
    prefs = repo.get_prefs(USER)
    assert prefs["output_format"] == "CSV"
    assert prefs["tone_preference"] == "friendly"


def test_upsert_returns_merged_row(patched_conn):
    merged = UserPrefsRepo().upsert_prefs(USER, output_format="CSV", extra_prefs="no emojis")
    assert merged == {"output_format": "CSV", "tone_preference": None, "extra_prefs": "no emojis"}


def test_upsert_scoped_per_user(patched_conn):
    repo = UserPrefsRepo()
    repo.upsert_prefs("alice", output_format="CSV")
    repo.upsert_prefs("bob", output_format="bulleted list")
    assert repo.get_output_format("alice") == "CSV"
    assert repo.get_output_format("bob") == "bulleted list"
