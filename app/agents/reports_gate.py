"""Reports gate — DML confirmation for destructive ops (spec §4).

The gate branches on the deterministic SQL verb (NOT on the supervisor intent —
defense in depth): a DELETE/UPDATE goes through the human-in-the-loop
``interrupt()`` + hybrid confirmation, then the owner-scoped execute.

Hybrid confirmation: a deterministic yes/no floor (the fast path AND the safety
floor), and the LLM only on the residual — biased hard toward 'not confirmed'.
The confirmation prompt belongs to the gate (it is LLM-driven), not to a tool.
"""
from __future__ import annotations

import re

from langgraph.types import interrupt

from app import config, errors
from app.graph.state import AgentState
from app.llm import get_llm, llm_text
from app.sources.reports_repo import SavedReportsRepo
from app.tools.reports import make_preview_sql
from app.tools.sql_tools import preview_guard

# Deterministic floor: an explicit, unambiguous affirmative or negative.
_YES = {
    "да", "д", "yes", "y", "ок", "ok", "yeah", "ага", "confirm",
    "подтверждаю", "подтвердить", "удаляй", "go",
}
_NO = {
    "нет", "н", "no", "n", "отмена", "отменить", "cancel", "stop", "стоп",
    "не надо", "не нужно", "отставить", "abort",
}

_CONFIRM_PROMPT = """A user was asked to confirm an IRREVERSIBLE delete/update of THEIR OWN saved reports.
Decide whether their reply is an UNAMBIGUOUS YES (clear go-ahead). Anything uncertain, conditional,
partial, a question, or negative counts as NOT yes.
Reply with exactly one word: confirm | cancel.

User reply: {answer}
One word:"""


def _llm_confirm(answer) -> bool:
    """Residual classifier — only for replies outside the deterministic sets.

    Biased hard toward 'not confirmed': returns True only on an explicit
    ``confirm``. Any classifier failure (LLM down) returns False — we never
    delete on uncertainty.
    """
    try:
        resp = get_llm(config.SUPERVISOR_MODEL).invoke(_CONFIRM_PROMPT.format(answer=answer))
        return llm_text(resp).strip().lower().startswith("confirm")
    except Exception:
        return False


def _is_confirmed(answer) -> bool:
    """Hybrid confirmation: deterministic floor first, LLM only on the residual."""
    if answer is None:
        return False
    norm = str(answer).strip().lower().strip(".!?")
    if norm in _YES:
        return True
    if norm in _NO:
        return False
    return _llm_confirm(answer)


_PICK_PROMPT = """The user was shown a numbered list of saved reports and asked to pick one to update.
Reports:
{rows_list}

User reply: {answer}

Return EXACTLY one token:
- A number (1, 2, …) matching the report the user refers to — by ordinal word ("первый"/"first"/etc.), by title substring, or by description.
- "all" if the user wants to update all of them.
- "cancel" if the user wants to abort or the reply is ambiguous/unclear.

One token only:"""


def _llm_pick(answer: str, rows: list) -> str:
    """Interpret a free-form pick reply. Returns '1'-based index string, 'all', or 'cancel'.

    Biased toward 'cancel' on uncertainty — never mutates on ambiguity.
    """
    rows_list = "\n".join(
        f'{i}. "{r.get("question", "")}"' for i, r in enumerate(rows, 1)
    )
    try:
        resp = get_llm(config.SUPERVISOR_MODEL).invoke(
            _PICK_PROMPT.format(rows_list=rows_list, answer=answer)
        )
        token = llm_text(resp).strip().lower().split()[0]
        if token == "all":
            return "all"
        idx = int(token)
        if 1 <= idx <= len(rows):
            return str(idx)
        return "cancel"
    except Exception:
        return "cancel"


_ALL_WORDS = {"все", "all", "оба", "обе", "всех", "оба варианта"}


def _parse_pick(answer: str, rows: list) -> str:
    """Parse the user's pick into '1'-based index string, 'all', or 'cancel'.

    'yes' words map to 'all' (confirm all matching rows).
    Deterministic fast path; LLM only for ordinals, names, and descriptions.
    """
    if answer is None:
        return "cancel"
    norm = str(answer).strip().lower().strip(".!?")
    if norm in _NO:
        return "cancel"
    if norm in _YES or norm in _ALL_WORDS:
        return "all"
    try:
        idx = int(norm)
        return str(idx) if 1 <= idx <= len(rows) else "cancel"
    except (ValueError, TypeError):
        pass
    return _llm_pick(answer, rows)


def _target_single_row(sql: str, row_id) -> str:
    """Narrow an UPDATE's WHERE clause to a single row by id (string UUID)."""
    safe_id = str(row_id).replace("'", "")
    return re.sub(r'\bWHERE\b.*$', f"WHERE id = '{safe_id}'", sql.rstrip(),
                  flags=re.IGNORECASE | re.DOTALL)


def reports_gate(state: AgentState) -> dict:
    sql = (state.get("sql") or "").strip()
    owner_id = config.CURRENT_USER_ID
    debug = state.get("debug", False)

    first_word = sql.split()[0].upper() if sql.split() else ""
    if first_word not in ("DELETE", "UPDATE"):
        # Defensive: the gate is only reached for destructive intent; a non-DML
        # statement here is a bug upstream, not something to execute.
        return {"final_message": errors.format_error(
            errors.REPORTS_GEN_FAILED, debug, extra=f"non-DML at gate: {first_word or '?'}"
        )}

    # Prefer the agent's structured preview; fall back to deriving one.
    preview_sql = (state.get("preview_sql") or "").strip() or make_preview_sql(sql)
    ok, reason = preview_guard(preview_sql)
    if not ok:
        return {"final_message": errors.format_error(errors.REPORTS_GEN_FAILED, debug, extra=reason)}

    try:
        rows = SavedReportsRepo().preview(preview_sql, owner_id)
    except errors.ServiceUnavailableError:
        return {"final_message": errors.SERVICE_UNAVAILABLE}
    except Exception as e:
        return {"final_message": errors.format_error(errors.UNEXPECTED, debug, e)}

    if not rows:
        return {"final_message": errors.PREVIEW_EMPTY}

    answer = interrupt({
        "kind": "reports_confirm",
        "verb": first_word,
        "count": len(rows),
        "preview_rows": rows,
        "dml_sql": sql,
    })

    if first_word == "UPDATE":
        pick = _parse_pick(answer, rows)
        if pick == "cancel":
            return {"confirmed": False, "final_message": errors.CANCELLED}
        if pick != "all":
            idx = int(pick) - 1
            sql = _target_single_row(sql, rows[idx]["id"])
        # "all" / YES words → proceed with original SQL unchanged
    elif not _is_confirmed(answer):
        return {"confirmed": False, "final_message": errors.CANCELLED}

    try:
        affected = SavedReportsRepo().execute_destructive(sql, owner_id)
    except errors.ServiceUnavailableError:
        return {"confirmed": True, "final_message": errors.SERVICE_UNAVAILABLE}
    except Exception as e:
        return {"confirmed": True, "final_message": errors.format_error(errors.UNEXPECTED, debug, e)}

    verb_en = "Updated" if first_word == "UPDATE" else "Deleted"
    return {"confirmed": True, "final_message": f"✓ {verb_en} {affected} record(s)."}
