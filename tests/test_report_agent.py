"""Report agent node — generation, persistence, trio capture, error handling (app/agents/report_agent.py).

The LLM is faked; deterministic parts under test:
  - Report text flows from LLM to state + DB.
  - Save failure is graceful (report still returned).
  - LLM error surfaces the right scenario message.
  - Pending trio is set on success, absent on failure.
  - Regenerate mode reads from state (no new query).
"""
from __future__ import annotations

import pytest

from app import config, errors
from app.agents import report_agent as ra_mod
from app.agents.report_agent import report_agent

from tests.conftest import FakeLLM


def _state(question="сколько заказов?", debug=False, sql="SELECT 1",
           rows="| a |\n| - |\n| 1 |", **extra):
    return {"question": question, "debug": debug, "sql": sql,
            "rows_markdown": rows, **extra}


@pytest.fixture
def llm(fake_llm_factory):
    def _set(responses):
        return fake_llm_factory(ra_mod, FakeLLM(responses))
    return _set


# --------------------------------------------------------------------------- #
# Normal mode — generate + save
# --------------------------------------------------------------------------- #
def test_report_generated_and_saved(llm, patched_conn):
    llm(["Отчёт: 42 заказа."])
    out = report_agent(_state())

    assert out["report_md"] == "Отчёт: 42 заказа."
    assert "отчёт сохранён" in out["final_message"]
    assert patched_conn.execute("SELECT COUNT(*) FROM saved_reports").fetchone()[0] == 1


def test_report_persists_question_and_sql(llm, patched_conn):
    llm(["body"])
    report_agent(_state(question="вопрос", sql="SELECT 7"))
    row = patched_conn.execute(
        "SELECT owner_id, question, sql_query FROM saved_reports"
    ).fetchone()
    assert row["owner_id"] == config.CURRENT_USER_ID
    assert row["question"] == "вопрос"
    assert row["sql_query"] == "SELECT 7"


def test_pending_trio_set_on_success(llm, patched_conn):
    llm(["text"])
    out = report_agent(_state(question="q"))
    assert out["pending_trio"] is not None
    assert out["pending_trio"]["question"] == "q"
    assert out["pending_trio"]["report_md"] == "text"


def test_last_question_set_in_state(llm, patched_conn):
    llm(["report body"])
    out = report_agent(_state(question="my question"))
    assert out["last_question"] == "my question"


# --------------------------------------------------------------------------- #
# LLM error
# --------------------------------------------------------------------------- #
def test_llm_error_returns_scenario_message(llm, patched_conn):
    llm([RuntimeError("503 unavailable")])
    out = report_agent(_state())
    assert out["final_message"] == errors.SERVICE_UNAVAILABLE
    assert "report_md" not in out
    assert out.get("pending_trio") is None
    assert patched_conn.execute("SELECT COUNT(*) FROM saved_reports").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# Save failure — graceful degradation
# --------------------------------------------------------------------------- #
class _BrokenSave:
    def save(self, *a, **k):
        raise RuntimeError("disk full")


def test_save_failure_debug_shows_note(llm, patched_conn, monkeypatch):
    monkeypatch.setattr(ra_mod, "SavedReportsRepo", lambda *a, **k: _BrokenSave())
    llm(["the answer"])
    out = report_agent(_state(debug=True))
    assert out["report_md"] == "the answer"
    assert "не удалось сохранить" in out["final_message"]


def test_save_failure_silent_in_normal_mode(llm, patched_conn, monkeypatch):
    monkeypatch.setattr(ra_mod, "SavedReportsRepo", lambda *a, **k: _BrokenSave())
    llm(["the answer"])
    out = report_agent(_state(debug=False))
    assert out["final_message"] == "the answer"


# --------------------------------------------------------------------------- #
# Prefs read failure → defaults to 'table'
# --------------------------------------------------------------------------- #
class _BrokenPrefs:
    def get_prefs(self, _):
        raise RuntimeError("prefs locked")


def test_prefs_read_failure_falls_back(llm, patched_conn, monkeypatch):
    monkeypatch.setattr(ra_mod, "UserPrefsRepo", lambda *a, **k: _BrokenPrefs())
    llm(["body"])
    out = report_agent(_state())
    assert out["report_md"] == "body"


def test_stored_prefs_reach_the_prompt(llm, patched_conn):
    patched_conn.execute(
        "INSERT INTO user_prefs (user_id, output_format, tone_preference, extra_prefs) "
        "VALUES (?, ?, ?, ?)",
        (config.CURRENT_USER_ID, "CSV", "concise", "no emojis"),
    )
    patched_conn.commit()
    fake = llm(["body"])
    report_agent(_state())
    prompt = fake.calls[0]
    assert "CSV" in prompt
    assert "concise" in prompt
    assert "no emojis" in prompt


# --------------------------------------------------------------------------- #
# Regenerate mode (revises previous report from state)
# --------------------------------------------------------------------------- #
def test_regenerate_revises_previous_report(llm, patched_conn):
    llm(["revised report"])
    out = report_agent({
        "question": "сделай короче",
        "debug": False,
        "intent": "regenerate",
        "report_md": "original long report",
        "rows_markdown": "| a | 1 |",
        "last_question": "сколько заказов?",
        "sql": "SELECT 1",
    })
    assert out["report_md"] == "revised report"
    assert "revised report" in out["final_message"]


def test_regenerate_no_previous_returns_error(llm, patched_conn):
    llm(["ignored"])
    out = report_agent({
        "question": "сделай короче",
        "debug": False,
        "intent": "regenerate",
        "report_md": "",
    })
    assert out["final_message"] == errors.REGEN_NO_PREVIOUS
