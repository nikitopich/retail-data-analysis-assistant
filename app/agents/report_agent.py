"""Report Agent node — generate the markdown report, then persist it (spec §3.4).

Two modes:
  * normal     — build a fresh report from the SQL result, save it to the library.
  * regenerate — revise the PREVIOUS report with the user's correction. The prior
    report + its data rows are read from state (the session shares one checkpointer
    thread), so no new query is run. Reached directly from the supervisor.
"""
from __future__ import annotations

from app import config, errors
from app.graph.state import AgentState
from app.llm import get_llm, llm_text
from app.sources.prefs_repo import UserPrefsRepo
from app.sources.reports_repo import SavedReportsRepo, StagedTriosRepo

_REPORT_PROMPT = """You are an analytics assistant for company executives. Write a concise, clear report in
{output_format} format answering the user's question, based ONLY on the query result below.
Do not invent numbers. Reply in the same language as the question.

Question: {question}
SQL result (first rows):
{rows_markdown}"""

_REVISION_PROMPT = """You are an analytics assistant for company executives. The user wants to ADJUST the
previous report. Apply their correction exactly. Keep it factual: base any numbers ONLY on the data
rows below (the source of truth) — do not invent or change figures unless the data supports it.
Reply in the same language as the user's correction.

Original question: {orig_question}
Data rows (source of truth):
{rows_markdown}

Previous report:
{prev_report}

User's correction: {correction}

Return the revised report only (no preamble)."""


def _output_format() -> str:
    try:
        return UserPrefsRepo().get_output_format(config.CURRENT_USER_ID)
    except Exception:
        return "table"


def _persist(question: str, sql_query: str, report_md: str, debug: bool) -> str:
    """Save report + staged trio. A persistence failure must not lose the answer
    the user already has, so we swallow it (surfaced only in debug)."""
    try:
        report_id = SavedReportsRepo().save(
            config.CURRENT_USER_ID, question, sql_query, report_md
        )
        StagedTriosRepo().add(
            report_id, config.CURRENT_USER_ID, question, sql_query, report_md
        )
        return "\n\n_(отчёт сохранён в библиотеку)_"
    except Exception as e:  # pragma: no cover
        if debug:
            return "\n\n" + errors.format_error("(не удалось сохранить отчёт)", True, e)
        return ""


def _regenerate(state: AgentState) -> dict:
    """Revise the previous report with the user's correction (no new query)."""
    debug = state.get("debug", False)
    correction = state["question"]
    prev_report = state.get("report_md") or ""
    if not prev_report:
        return {"final_message": errors.REGEN_NO_PREVIOUS}

    rows_markdown = state.get("rows_markdown") or ""
    orig_question = state.get("last_question") or "(unknown)"

    llm = get_llm(config.REPORT_MODEL, temperature=0.3)
    prompt = _REVISION_PROMPT.format(
        orig_question=orig_question,
        rows_markdown=rows_markdown,
        prev_report=prev_report,
        correction=correction,
    )
    try:
        report_md = llm_text(llm.invoke(prompt)).strip()
    except Exception as e:
        return {"final_message": errors.format_error(errors.llm_error_message(e), debug, e)}

    # Save the revised version; keep the original question as the report's question.
    note = _persist(orig_question, state.get("sql", "") or "", report_md, debug)
    return {
        "report_md": report_md,
        "last_question": orig_question,
        "final_message": report_md + note,
    }


def report_agent(state: AgentState) -> dict:
    if state.get("intent") == "regenerate":
        return _regenerate(state)

    debug = state.get("debug", False)
    question = state["question"]
    rows_markdown = state.get("rows_markdown", "") or ""

    llm = get_llm(config.REPORT_MODEL, temperature=0.3)
    prompt = _REPORT_PROMPT.format(
        output_format=_output_format(),
        question=question,
        rows_markdown=rows_markdown,
    )

    try:
        report_md = llm_text(llm.invoke(prompt)).strip()
    except Exception as e:
        return {"final_message": errors.format_error(errors.llm_error_message(e), debug, e)}

    note = _persist(question, state.get("sql", "") or "", report_md, debug)
    # Snapshot the question so the next turn's `regenerate` can revise this report.
    return {
        "report_md": report_md,
        "last_question": question,
        "final_message": report_md + note,
    }
