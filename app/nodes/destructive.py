"""Destructive flow — the High-Stakes Oversight core (spec §4).

Three nodes:
  destructive_generate -> NL to {preview_sql, dml_sql}, DML-guard, retry x3
  destructive_preview  -> run owner-scoped preview; empty -> stop without asking
  destructive_gate     -> interrupt() for confirmation, then owner-scoped execute
"""
from __future__ import annotations

import json
import re
from typing import Tuple

from langgraph.types import interrupt

from app import config, errors, prompts
from app.guards import dml_guard, preview_guard
from app.repositories import SavedReportsRepo
from app.state import AgentState

_YES = {
    "да", "д", "yes", "y", "ок", "ok", "yeah", "ага", "confirm",
    "подтверждаю", "подтвердить", "удаляй", "go",
}


def _is_yes(answer) -> bool:
    if answer is None:
        return False
    return str(answer).strip().lower().strip(".!?") in _YES


def _parse_json(text: str) -> dict:
    """Extract a JSON object from the LLM output (tolerant of fences/prose)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        if t.endswith("```"):
            t = t[:-3]
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1 and end > start:
        t = t[start:end + 1]
    return json.loads(t)


def destructive_generate(state: AgentState) -> dict:
    """NL -> (preview SELECT, DML) with deterministic DML-guard + retry budget."""
    debug = state.get("debug", False)
    question = state["question"]
    llm = config.get_llm(config.SQL_MODEL)

    hint = ""
    last_reason = ""
    attempt = 0
    while attempt < config.MAX_SQL_ATTEMPTS:
        attempt += 1
        prompt = prompts.DESTRUCTIVE_PROMPT.format(question=question) + hint

        try:
            resp = llm.invoke(prompt)
        except Exception as e:
            return {
                "sql_attempts": attempt,
                "final_message": errors.format_error(errors.LLM_UNAVAILABLE, debug, e),
            }

        try:
            data = _parse_json(resp.content)
            preview_sql = str(data["preview_sql"]).strip()
            dml_sql = str(data["dml_sql"]).strip()
        except Exception:
            last_reason = "invalid JSON / missing fields"
            hint = prompts.DESTRUCTIVE_JSON_HINT
            continue

        ok_dml, reason_dml = dml_guard(dml_sql)
        ok_prev, reason_prev = preview_guard(preview_sql)
        if not (ok_dml and ok_prev):
            last_reason = reason_dml or reason_prev
            hint = prompts.DESTRUCTIVE_GUARD_HINT.format(reason=last_reason)
            continue

        return {"preview_sql": preview_sql, "dml_sql": dml_sql, "sql_attempts": attempt}

    return {
        "sql_attempts": attempt,
        "final_message": errors.format_error(
            errors.DESTRUCTIVE_GEN_FAILED, debug, extra=f"last reason: {last_reason}"
        ),
    }


def destructive_preview(state: AgentState) -> dict:
    """Run the preview SELECT (owner-scoped). Empty -> stop, never ask to confirm."""
    debug = state.get("debug", False)
    try:
        rows = SavedReportsRepo().preview(state["preview_sql"], config.CURRENT_USER_ID)
    except Exception as e:
        return {"final_message": errors.format_error(errors.UNEXPECTED, debug, e)}

    if not rows:
        return {"final_message": errors.PREVIEW_EMPTY}
    return {"preview_rows": rows}


def destructive_gate(state: AgentState) -> dict:
    """Pause for confirmation, then execute (owner-scoped) on 'yes'."""
    debug = state.get("debug", False)
    rows = state.get("preview_rows") or []
    dml_sql = state.get("dml_sql", "")

    # The payload is surfaced to the CLI, which prints the preview and prompts
    # the user. interrupt() returns the resume value on the second pass.
    answer = interrupt({
        "kind": "destructive_confirm",
        "count": len(rows),
        "preview_rows": rows,
        "dml_sql": dml_sql,
    })

    if not _is_yes(answer):
        return {"confirmed": False, "final_message": errors.CANCELLED}

    try:
        affected = SavedReportsRepo().execute_destructive(dml_sql, config.CURRENT_USER_ID)
    except Exception as e:
        return {
            "confirmed": True,
            "final_message": errors.format_error(errors.UNEXPECTED, debug, e),
        }

    verb = "Изменено" if dml_sql.strip().upper().startswith("UPDATE") else "Удалено"
    return {"confirmed": True, "final_message": f"✓ {verb} записей: {affected}."}
