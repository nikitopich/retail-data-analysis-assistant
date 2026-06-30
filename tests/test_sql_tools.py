"""Deterministic SQL guards, text helpers, and run_with_backoff (app/tools/sql_tools.py)."""
from __future__ import annotations

import pandas as pd
import pytest
from google.api_core import exceptions as gexc

from app import errors
from app.tools.sql_tools import (
    dml_guard,
    df_to_markdown,
    preview_guard,
    reports_sql_guard,
    run_with_backoff,
    select_only_guard,
    strip_sql,
)

from tests.conftest import FakeRunner


# --------------------------------------------------------------------------- #
# select_only_guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select id from orders",
    "  SELECT * FROM users ;  ",
    "WITH t AS (SELECT 1) SELECT * FROM t",
])
def test_select_allowed(sql):
    ok, reason = select_only_guard(sql)
    assert ok and reason == ""


@pytest.mark.parametrize("sql,fragment", [
    ("", "empty"),
    ("   ", "empty"),
    ("SELECT 1; SELECT 2", "multiple"),
    ("SELECT 1 -- comment", "comment"),
    ("SELECT /* x */ 1", "comment"),
    ("DELETE FROM orders", "only a single SELECT"),
    ("UPDATE orders SET x=1", "only a single SELECT"),
])
def test_select_rejections(sql, fragment):
    ok, reason = select_only_guard(sql)
    assert not ok
    assert fragment in reason


@pytest.mark.parametrize("kw", [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "MERGE", "GRANT", "REVOKE", "ATTACH", "DETACH", "PRAGMA", "VACUUM",
])
def test_forbidden_keywords_in_select(kw):
    ok, reason = select_only_guard(f"SELECT 1 FROM t WHERE x = '{kw}' OR {kw} (1)")
    assert not ok
    assert kw in reason


def test_replace_allowed_as_string_function():
    ok, _ = select_only_guard("SELECT REPLACE(name, 'a', 'b') FROM users")
    assert ok


def test_keyword_word_boundary():
    ok, _ = select_only_guard("SELECT created_at FROM orders")
    assert ok


# --------------------------------------------------------------------------- #
# preview_guard
# --------------------------------------------------------------------------- #
def test_preview_ok_on_saved_reports():
    ok, reason = preview_guard("SELECT id FROM saved_reports WHERE id = 1")
    assert ok and reason == ""


def test_preview_rejects_other_table():
    ok, reason = preview_guard("SELECT id FROM orders")
    assert not ok
    assert "saved_reports" in reason


def test_preview_inherits_select_rejections():
    ok, reason = preview_guard("DELETE FROM saved_reports")
    assert not ok


# --------------------------------------------------------------------------- #
# dml_guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sql", [
    "DELETE FROM saved_reports WHERE id = 1",
    "delete from saved_reports where created_at < date('now')",
    "UPDATE saved_reports SET question = 'x' WHERE id = 1",
    "  UPDATE saved_reports SET published_to_golden = 1 ;  ",
])
def test_dml_allowed(sql):
    ok, reason = dml_guard(sql)
    assert ok and reason == ""


@pytest.mark.parametrize("sql,fragment", [
    ("", "empty"),
    ("DELETE FROM saved_reports; DELETE FROM saved_reports", "multiple"),
    ("DELETE FROM saved_reports -- all", "comment"),
    ("SELECT * FROM saved_reports", "only DELETE/UPDATE"),
    ("DELETE FROM orders", "DELETE must target saved_reports"),
    ("UPDATE orders SET x = 1", "UPDATE must target saved_reports"),
    ("DROP TABLE saved_reports", "only DELETE/UPDATE"),
])
def test_dml_rejections(sql, fragment):
    ok, reason = dml_guard(sql)
    assert not ok
    assert fragment in reason


def test_dml_blocks_smuggled_ddl_keyword():
    ok, reason = dml_guard("DELETE FROM saved_reports WHERE x IN (TRUNCATE)")
    assert not ok
    assert "TRUNCATE" in reason


# --------------------------------------------------------------------------- #
# reports_sql_guard
# --------------------------------------------------------------------------- #
def test_reports_sql_guard_allows_select():
    ok, _ = reports_sql_guard("SELECT id, question FROM saved_reports")
    assert ok


def test_reports_sql_guard_allows_delete():
    ok, _ = reports_sql_guard("DELETE FROM saved_reports WHERE id = '1'")
    assert ok


def test_reports_sql_guard_rejects_insert():
    ok, reason = reports_sql_guard("INSERT INTO saved_reports VALUES (1)")
    assert not ok


def test_reports_sql_guard_select_must_ref_saved_reports():
    ok, reason = reports_sql_guard("SELECT * FROM orders")
    assert not ok
    assert "saved_reports" in reason


# --------------------------------------------------------------------------- #
# strip_sql
# --------------------------------------------------------------------------- #
def test_strip_plain_sql_unchanged():
    assert strip_sql("SELECT 1") == "SELECT 1"


def test_strip_removes_sql_fence():
    assert strip_sql("```sql\nSELECT 1\n```") == "SELECT 1"


def test_strip_removes_bare_fence():
    assert strip_sql("```\nSELECT 1\n```") == "SELECT 1"


def test_strip_handles_none_and_empty():
    assert strip_sql(None) == ""
    assert strip_sql("   ") == ""


# --------------------------------------------------------------------------- #
# df_to_markdown
# --------------------------------------------------------------------------- #
def test_markdown_basic_shape():
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    md = df_to_markdown(df, max_rows=10)
    lines = md.splitlines()
    assert lines[0] == "| a | b |"
    assert lines[1] == "| --- | --- |"
    assert "| 1 | x |" in md
    assert "| 2 | y |" in md


def test_markdown_truncates_and_notes_total():
    df = pd.DataFrame({"a": list(range(5))})
    md = df_to_markdown(df, max_rows=2)
    assert "showing first 2 of 5 rows" in md
    assert md.count("\n") == 4


def test_markdown_renders_nan_as_blank():
    df = pd.DataFrame({"a": [1, None]})
    md = df_to_markdown(df, max_rows=10)
    assert "|  |" in md


# --------------------------------------------------------------------------- #
# run_with_backoff — error-type conversion (no retry in these cases)
# --------------------------------------------------------------------------- #
def test_run_with_backoff_success():
    df = pd.DataFrame({"x": [1, 2]})
    runner = FakeRunner(results=[df])
    result = run_with_backoff(runner, "SELECT 1")
    assert len(result) == 2
    assert runner.queries == ["SELECT 1"]


def test_run_with_backoff_converts_bad_request_to_query_error():
    runner = FakeRunner(results=[gexc.BadRequest("bad column")])
    with pytest.raises(errors.QueryError):
        run_with_backoff(runner, "SELECT 1")


def test_run_with_backoff_converts_not_found_to_query_error():
    runner = FakeRunner(results=[gexc.NotFound("no such table")])
    with pytest.raises(errors.QueryError):
        run_with_backoff(runner, "SELECT 1")


def test_run_with_backoff_passes_through_non_retryable():
    boom = gexc.Forbidden("billing disabled")
    runner = FakeRunner(results=[boom])
    with pytest.raises(gexc.Forbidden):
        run_with_backoff(runner, "SELECT 1")
