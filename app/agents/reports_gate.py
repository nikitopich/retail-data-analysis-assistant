"""Reports gate — owner-scoped SELECT execution and DML confirmation (spec §4).

Branches on the deterministic SQL verb (NOT on the supervisor intent — defense in
depth): a DELETE/UPDATE goes through the human-in-the-loop ``interrupt()`` +
hybrid confirmation; a SELECT is executed owner-scoped and formatted.
"""
from __future__ import annotations

from langgraph.types import interrupt

from app import config, errors
from app.graph.state import AgentState
from app.sources.reports_repo import SavedReportsRepo
from app.tools.destructive import format_rows, is_confirmed, make_preview_sql
from app.tools.sql_tools import preview_guard


def reports_gate(state: AgentState) -> dict:
    sql = (state.get("sql") or "").strip()
    owner_id = config.CURRENT_USER_ID
    debug = state.get("debug", False)

    first_word = sql.split()[0].upper() if sql.split() else ""
    is_dml = first_word in ("DELETE", "UPDATE")

    if is_dml:
        # Prefer the agent's structured preview; fall back to deriving one.
        preview_sql = (state.get("preview_sql") or "").strip() or make_preview_sql(sql)
        ok, reason = preview_guard(preview_sql)
        if not ok:
            return {"final_message": errors.format_error(
                errors.REPORTS_GEN_FAILED, debug, extra=reason
            )}

        try:
            rows = SavedReportsRepo().preview(preview_sql, owner_id)
        except Exception as e:
            return {"final_message": errors.format_error(errors.UNEXPECTED, debug, e)}

        if not rows:
            return {"final_message": errors.PREVIEW_EMPTY}

        answer = interrupt({
            "kind": "reports_confirm",
            "count": len(rows),
            "preview_rows": rows,
            "dml_sql": sql,
        })

        if not is_confirmed(answer):
            return {"confirmed": False, "final_message": errors.CANCELLED}

        try:
            affected = SavedReportsRepo().execute_destructive(sql, owner_id)
        except Exception as e:
            return {"confirmed": True, "final_message": errors.format_error(errors.UNEXPECTED, debug, e)}

        verb = "Изменено" if first_word == "UPDATE" else "Удалено"
        return {"confirmed": True, "final_message": f"✓ {verb} записей: {affected}."}

    # SELECT path
    try:
        rows = SavedReportsRepo().run_select(sql, owner_id)
    except Exception as e:
        return {"final_message": errors.format_error(errors.UNEXPECTED, debug, e)}

    if not rows:
        return {"final_message": "Ничего не найдено."}

    return {"final_message": format_rows(rows)}
