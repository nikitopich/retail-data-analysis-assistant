"""Deterministic saved-reports helpers: parsing, preview, row formatting (app/tools/reports.py)."""
from __future__ import annotations

from app.tools.reports import format_rows, make_preview_sql, parse_reports_output


# --------------------------------------------------------------------------- #
# parse_reports_output
# --------------------------------------------------------------------------- #
def test_parse_action_only():
    action, preview = parse_reports_output(
        "ACTION: SELECT id, question FROM saved_reports ORDER BY created_at DESC"
    )
    assert action.startswith("SELECT")
    assert preview == ""


def test_parse_action_and_preview():
    text = (
        "PREVIEW: SELECT id, question, created_at FROM saved_reports WHERE question LIKE '%x%'\n"
        "ACTION: DELETE FROM saved_reports WHERE question LIKE '%x%'"
    )
    action, preview = parse_reports_output(text)
    assert action.startswith("DELETE")
    assert preview.startswith("SELECT")
    assert "question LIKE '%x%'" in preview


def test_parse_order_independent():
    text = (
        "ACTION: UPDATE saved_reports SET published_to_golden = 1 WHERE id = '5'\n"
        "PREVIEW: SELECT id, question, created_at FROM saved_reports WHERE id = '5'"
    )
    action, preview = parse_reports_output(text)
    assert action.startswith("UPDATE")
    assert preview.startswith("SELECT")


def test_parse_bare_statement_is_action():
    action, preview = parse_reports_output("SELECT id FROM saved_reports")
    assert action == "SELECT id FROM saved_reports"
    assert preview == ""


def test_parse_strips_fences():
    action, preview = parse_reports_output(
        "```sql\nACTION: SELECT id FROM saved_reports\n```"
    )
    assert action == "SELECT id FROM saved_reports"


def test_parse_empty_string():
    action, preview = parse_reports_output("")
    assert action == ""
    assert preview == ""


# --------------------------------------------------------------------------- #
# make_preview_sql
# --------------------------------------------------------------------------- #
def test_make_preview_from_delete():
    sql = "DELETE FROM saved_reports WHERE question LIKE '%x%'"
    preview = make_preview_sql(sql)
    assert preview.upper().startswith("SELECT")
    assert "saved_reports" in preview
    assert "question LIKE '%x%'" in preview


def test_make_preview_from_update():
    sql = "UPDATE saved_reports SET published_to_golden = 1 WHERE question LIKE '%x%'"
    preview = make_preview_sql(sql)
    assert preview.upper().startswith("SELECT")
    assert "question LIKE '%x%'" in preview


def test_make_preview_no_where():
    sql = "DELETE FROM saved_reports"
    preview = make_preview_sql(sql)
    assert "WHERE" not in preview.upper()
    assert "saved_reports" in preview


# --------------------------------------------------------------------------- #
# format_rows
# --------------------------------------------------------------------------- #
def test_format_rows_empty():
    assert format_rows([]) == "Nothing found."


def test_format_rows_renders_table():
    rows = [{"question": "q1", "created_at": "2026-01-01"}]
    out = format_rows(rows)
    assert "question" in out
    assert "q1" in out


def test_format_rows_truncates_report_md():
    rows = [{"report_md": "x" * 200}]
    out = format_rows(rows)
    assert "…" in out


def test_format_rows_escapes_pipe_in_cell():
    rows = [{"question": "a|b"}]
    out = format_rows(rows)
    assert "a\\|b" in out
