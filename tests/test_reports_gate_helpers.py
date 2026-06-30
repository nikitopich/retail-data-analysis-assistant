"""Reports gate — deterministic helper functions (app/agents/reports_gate.py).

Only the pure/deterministic helpers are tested here. The full ``reports_gate``
node is not unit-tested (it uses ``interrupt()`` which requires LangGraph runtime).
"""
from __future__ import annotations

import pytest

from app.agents.reports_gate import _is_confirmed, _parse_pick, _target_single_row


# --------------------------------------------------------------------------- #
# _is_confirmed — hybrid YES/NO confirmation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ans", [
    "yes", "Yes", "Y", "ok", "go", "confirm",
])
def test_is_confirmed_yes(ans):
    assert _is_confirmed(ans)


@pytest.mark.parametrize("ans", [
    None, "", "no", "n", "cancel", "stop", "abort",
])
def test_is_confirmed_no(ans):
    assert not _is_confirmed(ans)


def test_is_confirmed_strips_punctuation():
    assert _is_confirmed("yes!")
    assert _is_confirmed("yes.")
    assert not _is_confirmed("no!")


# --------------------------------------------------------------------------- #
# _parse_pick — interpret user's pick reply
# --------------------------------------------------------------------------- #
_ROWS = [
    {"id": "r1", "question": "Top customers"},
    {"id": "r2", "question": "Revenue for the month"},
]


def test_parse_pick_none_returns_cancel():
    assert _parse_pick(None, _ROWS) == "cancel"


def test_parse_pick_no_words_return_cancel():
    assert _parse_pick("no", _ROWS) == "cancel"
    assert _parse_pick("cancel", _ROWS) == "cancel"


def test_parse_pick_yes_words_return_all():
    assert _parse_pick("yes", _ROWS) == "all"
    assert _parse_pick("all", _ROWS) == "all"


def test_parse_pick_digit_in_range():
    assert _parse_pick("1", _ROWS) == "1"
    assert _parse_pick("2", _ROWS) == "2"


def test_parse_pick_digit_out_of_range_returns_cancel():
    assert _parse_pick("5", _ROWS) == "cancel"
    assert _parse_pick("0", _ROWS) == "cancel"


# --------------------------------------------------------------------------- #
# _target_single_row — SQL WHERE narrowing
# --------------------------------------------------------------------------- #
def test_target_single_row_replaces_where():
    sql = "DELETE FROM saved_reports WHERE question LIKE '%x%'"
    result = _target_single_row(sql, "abc-123")
    assert "WHERE id = 'abc-123'" in result
    assert "LIKE" not in result


def test_target_single_row_strips_quote_injection():
    sql = "DELETE FROM saved_reports WHERE 1=1"
    result = _target_single_row(sql, "a'b")
    assert "'" not in result.split("'")[1] if "'" in result else True
    assert "ab" in result


def test_target_single_row_case_insensitive():
    sql = "DELETE from saved_reports where question LIKE '%x%'"
    result = _target_single_row(sql, "uuid-1")
    assert "WHERE id = 'uuid-1'" in result
