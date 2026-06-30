"""Prefs Agent node — extract, persist, re-render (app/agents/prefs_agent.py).

The LLM is faked; deterministic parts under test:
  - JSON extraction → UPSERT into user_prefs → confirmation message.
  - Re-render of the last report when one exists (no new library entry).
  - Confirmation-only when there is no previous report.
  - Empty/invalid extraction → soft message, nothing written.
  - LLM failure surfaces the right scenario message.
"""
from __future__ import annotations

import pytest

from app import config, errors
from app.agents import prefs_agent as pa_mod
from app.agents import report_agent as ra_mod
from app.agents.prefs_agent import prefs_agent

from tests.conftest import FakeLLM


def _state(question="всегда присылай отчёты в виде CSV", **extra):
    return {"question": question, "user_id": config.CURRENT_USER_ID, "debug": False, **extra}


@pytest.fixture
def llm(fake_llm_factory):
    """Install one FakeLLM on BOTH prefs_agent and report_agent.get_llm.

    The same instance is shared so its queued responses are consumed in call
    order: extraction (prefs_agent) first, then revise() (report_agent).
    """
    def _set(responses):
        fake = FakeLLM(responses)
        fake_llm_factory(pa_mod, fake)
        fake_llm_factory(ra_mod, fake)
        return fake
    return _set


# --------------------------------------------------------------------------- #
# Extract + persist (no previous report)
# --------------------------------------------------------------------------- #
def test_extracts_and_saves(llm, patched_conn):
    llm(['{"output_format": "CSV", "tone": "concise", "extra": null}'])
    out = prefs_agent(_state())
    assert "Saved" in out["final_message"]
    assert "CSV" in out["final_message"]
    row = patched_conn.execute(
        "SELECT output_format, tone_preference FROM user_prefs WHERE user_id = ?",
        (config.CURRENT_USER_ID,),
    ).fetchone()
    assert row["output_format"] == "CSV"
    assert row["tone_preference"] == "concise"


def test_partial_extraction_format_only(llm, patched_conn):
    llm(['{"output_format": "bulleted list"}'])
    out = prefs_agent(_state())
    assert "bulleted list" in out["final_message"]
    assert patched_conn.execute(
        "SELECT output_format FROM user_prefs WHERE user_id = ?", (config.CURRENT_USER_ID,)
    ).fetchone()["output_format"] == "bulleted list"


# --------------------------------------------------------------------------- #
# Re-render the last report
# --------------------------------------------------------------------------- #
def test_rerenders_last_report_when_present(llm, patched_conn):
    fake = llm(['{"output_format": "CSV", "tone": null, "extra": null}', "id,orders\n1,42"])
    out = prefs_agent(_state(
        report_md="| orders |\n| - |\n| 42 |",
        rows_markdown="| orders | 42 |",
        last_question="сколько заказов?",
    ))
    assert out["report_md"] == "id,orders\n1,42"
    assert "Saved" in out["final_message"]
    assert "id,orders" in out["final_message"]
    assert out["last_question"] == "сколько заказов?"
    # A pure preference change must NOT create a new saved report.
    assert patched_conn.execute("SELECT COUNT(*) FROM saved_reports").fetchone()[0] == 0
    # Two LLM calls: extraction then revise.
    assert len(fake.calls) == 2


def test_no_previous_report_confirmation_only(llm, patched_conn):
    fake = llm(['{"output_format": "CSV", "tone": null, "extra": null}'])
    out = prefs_agent(_state())
    assert "Saved" in out["final_message"]
    assert "report_md" not in out
    assert len(fake.calls) == 1  # no re-render


# --------------------------------------------------------------------------- #
# Nothing concrete extracted → soft message, no write
# --------------------------------------------------------------------------- #
def test_all_null_returns_soft_message(llm, patched_conn):
    llm(['{"output_format": null, "tone": null, "extra": null}'])
    out = prefs_agent(_state())
    assert out["final_message"] == errors.PREFS_NOT_UNDERSTOOD
    assert patched_conn.execute("SELECT COUNT(*) FROM user_prefs").fetchone()[0] == 0


def test_invalid_json_returns_soft_message(llm, patched_conn):
    llm(["I'm not sure what you mean"])
    out = prefs_agent(_state())
    assert out["final_message"] == errors.PREFS_NOT_UNDERSTOOD
    assert patched_conn.execute("SELECT COUNT(*) FROM user_prefs").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# LLM failure
# --------------------------------------------------------------------------- #
def test_llm_error_returns_scenario_message(llm, patched_conn):
    llm([RuntimeError("429 quota")])
    out = prefs_agent(_state())
    assert out["final_message"] == errors.LLM_UNAVAILABLE
    assert patched_conn.execute("SELECT COUNT(*) FROM user_prefs").fetchone()[0] == 0
