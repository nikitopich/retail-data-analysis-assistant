"""Report Agent node — generate the markdown report, then persist it (spec §3.4).

Two modes:
  * normal     — build a fresh report from the SQL result, save it to the library.
  * regenerate — revise the PREVIOUS report with the user's correction. The prior
    report + its data rows are read from state (the session shares one checkpointer
    thread), so no new query is run. Reached directly from the supervisor.

Both modes honour the user's stored preferences (format/tone/extra), read from
``user_prefs`` via ``UserPrefsRepo``. The ``revise`` renderer is also reused by
the prefs agent to re-render the last report when a preference changes.
"""
from __future__ import annotations

from app import config, errors
from app.graph.state import AgentState
from app.llm import get_llm, llm_text
from app.sources.prefs_repo import UserPrefsRepo
from app.sources.reports_repo import SavedReportsRepo

_REPORT_PROMPT = """You are an analytics assistant for company executives. Write a concise, clear report in
{output_format} format answering the user's question, based ONLY on the query result below.

LANGUAGE RULE (highest priority): detect the language of the Question below and write your ENTIRE
response — including headers, labels, units, and all prose — in that same language. English question →
English report. Russian question → Russian report. Do not mix languages.

Rules:
- Do not invent numbers.
- Always include column names as labels — never show a bare number without saying what it represents.
- Infer units from column names: columns named revenue/sales/amount/price/total/sum are monetary values
  (show as currency, e.g. "12,642 ₽" or "12,642 USD" — pick the most likely currency or write "12,642 (revenue)").
  Columns named qty/quantity/count/units are item counts. Percentage columns — add "%".
- If the data has a natural ranking (top-N), number the rows.
{prefs_clause}
Question: {question}
SQL result (first rows):
{rows_markdown}"""

_REVISION_PROMPT = """You are an analytics assistant for company executives. The user wants to ADJUST the
previous report. Apply their correction exactly. Keep it factual: base any numbers ONLY on the data
rows below (the source of truth) — do not invent or change figures unless the data supports it.
Always label numbers with what they represent and include units (currency for revenue/amount/price,
counts for qty/quantity, "%" for percentages) — never show a bare number without context.
Reply in English by default; switch to another language only if the user's correction is clearly written in that language.
{prefs_clause}
Original question: {orig_question}
Data rows (source of truth):
{rows_markdown}

Previous report:
{prev_report}

User's correction: {correction}

Return the revised report only (no preamble)."""


def _prefs() -> dict:
    """Read the current user's stored preferences (defaults on any failure)."""
    try:
        return UserPrefsRepo().get_prefs(config.CURRENT_USER_ID)
    except Exception:
        return {"output_format": "table", "tone_preference": None, "extra_prefs": None}


def _prefs_clause(prefs: dict) -> str:
    """Render the active (non-default) preferences as a prompt fragment.

    Returns an empty string when the user has expressed no preferences beyond
    the default table format, so the prompt stays clean.
    """
    parts = []
    fmt = prefs.get("output_format")
    if fmt and fmt != "table":
        parts.append(f"- Output format: {fmt}")
    tone = prefs.get("tone_preference")
    if tone:
        parts.append(f"- Tone: {tone}")
    extra = prefs.get("extra_prefs")
    if extra:
        parts.append(f"- Additional preferences: {extra}")
    if not parts:
        return ""
    return "\nUSER PREFERENCES (apply all of these to the report):\n" + "\n".join(parts) + "\n"


def revise(orig_question: str, rows_markdown: str, prev_report: str,
           correction: str, prefs: dict) -> str:
    """Render a revised report from the previous one + a correction (no new query).

    Shared by the regenerate flow and the prefs agent's re-render. Raises on LLM
    failure — callers decide how to surface it.
    """
    llm = get_llm(config.REPORT_MODEL, temperature=0.3)
    prompt = _REVISION_PROMPT.format(
        prefs_clause=_prefs_clause(prefs),
        orig_question=orig_question,
        rows_markdown=rows_markdown,
        prev_report=prev_report,
        correction=correction,
    )
    return llm_text(llm.invoke(prompt)).strip()


def _persist(question: str, sql_query: str, report_md: str, debug: bool) -> tuple[str, str]:
    """Save report to the library. Returns (report_id, user_note).

    The staged-trio capture is deferred: the caller stores the trio data in
    ``pending_trio`` state so the supervisor (or CLI on exit/AFK) can flush it
    only when there is positive signal that the user found the report useful.
    """
    try:
        report_id = SavedReportsRepo().save(
            config.CURRENT_USER_ID, question, sql_query, report_md
        )
        return report_id, ""
    except Exception as e:  # pragma: no cover
        if debug:
            return "", "\n\n" + errors.format_error("(failed to save report)", True, e)
        return "", ""


def _regenerate(state: AgentState) -> dict:
    """Revise the previous report with the user's correction (no new query)."""
    debug = state.get("debug", False)
    correction = state["question"]
    prev_report = state.get("report_md") or ""
    if not prev_report:
        return {"final_message": errors.REGEN_NO_PREVIOUS}

    rows_markdown = state.get("rows_markdown") or ""
    orig_question = state.get("last_question") or "(unknown)"

    try:
        report_md = revise(orig_question, rows_markdown, prev_report, correction, _prefs())
    except Exception as e:
        return {"final_message": errors.format_llm_error(e, debug)}

    # Save the revised version; keep the original question as the report's question.
    report_id, note = _persist(orig_question, state.get("sql", "") or "", report_md, debug)
    return {
        "report_md": report_md,
        "last_question": orig_question,
        "final_message": report_md + note,
        "pending_trio": {
            "report_id": report_id,
            "owner_id": config.CURRENT_USER_ID,
            "question": orig_question,
            "sql_query": state.get("sql", "") or "",
            "report_md": report_md,
        } if report_id else None,
    }


def report_agent(state: AgentState) -> dict:
    if state.get("intent") == "regenerate":
        return _regenerate(state)

    debug = state.get("debug", False)
    question = state["question"]
    rows_markdown = state.get("rows_markdown", "") or ""

    prefs = _prefs()
    llm = get_llm(config.REPORT_MODEL, temperature=0.3)
    prompt = _REPORT_PROMPT.format(
        output_format=prefs["output_format"],
        prefs_clause=_prefs_clause(prefs),
        question=question,
        rows_markdown=rows_markdown,
    )

    try:
        report_md = llm_text(llm.invoke(prompt)).strip()
    except Exception as e:
        return {"final_message": errors.format_llm_error(e, debug)}

    report_id, note = _persist(question, state.get("sql", "") or "", report_md, debug)
    # Snapshot the question so the next turn's `regenerate` can revise this report.
    return {
        "report_md": report_md,
        "last_question": question,
        "final_message": report_md + note,
        "pending_trio": {
            "report_id": report_id,
            "owner_id": config.CURRENT_USER_ID,
            "question": question,
            "sql_query": state.get("sql", "") or "",
            "report_md": report_md,
        } if report_id else None,
    }
