"""SavedReportsRepo, StagedTriosRepo, _inject_owner_scope (app/sources/reports_repo.py).

Critical security property: owner-scoping is enforced in every read and write path
so the LLM can never see or affect another user's data.
"""
from __future__ import annotations

import pytest

from app.sources.reports_repo import (
    SavedReportsRepo,
    StagedTriosRepo,
    _inject_owner_scope,
    flush_pending_trio,
)
from app.sources.prefs_repo import UserPrefsRepo

OWNER_A = "alice"
OWNER_B = "bob"


# --------------------------------------------------------------------------- #
# _inject_owner_scope
# --------------------------------------------------------------------------- #
def test_inject_adds_where_when_absent():
    s, params = _inject_owner_scope("DELETE FROM saved_reports", OWNER_A)
    assert s == "DELETE FROM saved_reports WHERE owner_id = ?"
    assert params == [OWNER_A]


def test_inject_ands_onto_existing_where():
    s, params = _inject_owner_scope("DELETE FROM saved_reports WHERE id = 5", OWNER_A)
    assert s == "DELETE FROM saved_reports WHERE id = 5 AND owner_id = ?"
    assert params == [OWNER_A]


def test_inject_strips_trailing_semicolon():
    s, _ = _inject_owner_scope("SELECT * FROM saved_reports ;", OWNER_A)
    assert s.endswith("WHERE owner_id = ?")
    assert ";" not in s


# --------------------------------------------------------------------------- #
# SavedReportsRepo
# --------------------------------------------------------------------------- #
def _seed(conn):
    repo = SavedReportsRepo(conn)
    a1 = repo.save(OWNER_A, "q1", "SELECT 1", "report A1")
    a2 = repo.save(OWNER_A, "q2", "SELECT 2", "report A2")
    b1 = repo.save(OWNER_B, "q3", "SELECT 3", "report B1")
    return repo, a1, a2, b1


def test_save_returns_id_and_persists(conn):
    repo = SavedReportsRepo(conn)
    rid = repo.save(OWNER_A, "q", "SELECT 1", "md")
    assert isinstance(rid, str) and rid
    row = conn.execute(
        "SELECT owner_id, report_md FROM saved_reports WHERE id = ?", (rid,)
    ).fetchone()
    assert row["owner_id"] == OWNER_A
    assert row["report_md"] == "md"


def test_preview_only_sees_own_rows(conn):
    repo, *_ = _seed(conn)
    rows = repo.preview("SELECT id, question FROM saved_reports", OWNER_A)
    assert len(rows) == 2
    assert all(isinstance(r, dict) for r in rows)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "SECURITY GAP: _inject_owner_scope appends 'AND owner_id = ?' without "
        "parenthesizing the LLM's WHERE clause. With an OR predicate, "
        "AND-binds-tighter lets the owner scope be bypassed -> cross-tenant read. "
        "Fix: wrap existing condition in parens. Remove xfail once fixed."
    ),
)
def test_preview_owner_scope_overrides_or_predicate(conn):
    repo, *_ = _seed(conn)
    rows = repo.preview(
        "SELECT id FROM saved_reports WHERE owner_id = 'bob' OR 1=1", OWNER_A
    )
    assert len(rows) == 2


def test_run_select_only_sees_own_rows(conn):
    repo, *_ = _seed(conn)
    rows = repo.run_select("SELECT id, question FROM saved_reports", OWNER_A)
    assert len(rows) == 2
    questions = {r["question"] for r in rows}
    assert "q3" not in questions


def test_execute_destructive_scoped_and_counts(conn):
    repo, *_ = _seed(conn)
    affected = repo.execute_destructive("DELETE FROM saved_reports", OWNER_A)
    assert affected == 2
    remaining = conn.execute("SELECT owner_id FROM saved_reports").fetchall()
    assert [r["owner_id"] for r in remaining] == [OWNER_B]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "SECURITY GAP (same root cause as preview case): an OR predicate escapes "
        "owner-scoping -> deletes another user's reports. Remove xfail once fixed."
    ),
)
def test_execute_destructive_or_predicate_must_not_cross_tenant(conn):
    repo, *_ = _seed(conn)
    affected = repo.execute_destructive(
        "DELETE FROM saved_reports WHERE owner_id = 'bob' OR 1=1", OWNER_A
    )
    assert affected == 2
    survivors = conn.execute("SELECT owner_id FROM saved_reports").fetchall()
    assert [r["owner_id"] for r in survivors] == [OWNER_B]


def test_execute_destructive_update_returns_rowcount(conn):
    repo, a1, a2, b1 = _seed(conn)
    affected = repo.execute_destructive(
        "UPDATE saved_reports SET published_to_golden = 1", OWNER_A
    )
    assert affected == 2
    val = conn.execute(
        "SELECT published_to_golden FROM saved_reports WHERE id = ?", (b1,)
    ).fetchone()[0]
    assert val == 0


def test_repo_uses_get_connection_when_no_conn_given(patched_conn):
    rid = SavedReportsRepo().save(OWNER_A, "q", "SELECT 1", "md")
    assert patched_conn.execute(
        "SELECT 1 FROM saved_reports WHERE id = ?", (rid,)
    ).fetchone()


# --------------------------------------------------------------------------- #
# StagedTriosRepo
# --------------------------------------------------------------------------- #
def test_staged_trio_add_persists_pending(conn):
    rid = SavedReportsRepo(conn).save(OWNER_A, "q", "SELECT 1", "md")
    tid = StagedTriosRepo(conn).add(rid, OWNER_A, "q", "SELECT 1", "md")
    row = conn.execute(
        "SELECT status, report_id FROM staged_trios WHERE id = ?", (tid,)
    ).fetchone()
    assert row["status"] == "pending"
    assert row["report_id"] == rid


# --------------------------------------------------------------------------- #
# flush_pending_trio
# --------------------------------------------------------------------------- #
def test_flush_pending_trio_writes_to_staged(patched_conn):
    rid = SavedReportsRepo(patched_conn).save(OWNER_A, "q", "SELECT 1", "md")
    flush_pending_trio({
        "report_id": rid,
        "owner_id": OWNER_A,
        "question": "q",
        "sql_query": "SELECT 1",
        "report_md": "md",
    })
    count = patched_conn.execute("SELECT COUNT(*) FROM staged_trios").fetchone()[0]
    assert count == 1


def test_flush_pending_trio_swallows_error(monkeypatch):
    monkeypatch.setattr("app.sources.reports_repo.StagedTriosRepo",
                        type("B", (), {"add": lambda *a, **k: (_ for _ in ()).throw(RuntimeError())})())
    flush_pending_trio({"report_id": "x", "owner_id": "u", "question": "q",
                        "sql_query": "SELECT 1", "report_md": "md"})


# --------------------------------------------------------------------------- #
# UserPrefsRepo
# --------------------------------------------------------------------------- #
def test_output_format_defaults_to_table(conn):
    assert UserPrefsRepo(conn).get_output_format("nobody") == "table"


def test_output_format_returns_stored_value(conn):
    conn.execute(
        "INSERT INTO user_prefs (user_id, output_format) VALUES (?, ?)",
        (OWNER_A, "json"),
    )
    conn.commit()
    assert UserPrefsRepo(conn).get_output_format(OWNER_A) == "json"
