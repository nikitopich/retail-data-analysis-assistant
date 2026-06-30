"""SQL Agent node — source-aware query building with self-correction.

Thin orchestration over ``app.tools``: it dispatches by the supervisor's flow
intent and, for a `query`, RECOGNISES THE DATA SOURCE (analytical BigQuery /
database schema / saved-reports library) and drives the right tool. The SQL
guards, generation, schema cache and backoff live in ``app.tools``.

  * intent == "destructive"  -> build a guarded DELETE/UPDATE (+ preview) over the
    saved-reports library; the reports_gate runs the human-in-the-loop confirm.
  * intent == "query"        -> pick the source, then build the read query.

The analytical resilience loop (spec §2.2) keeps two independent budgets:
  * MAX_SQL_ATTEMPTS    — against bad / guard-rejected / syntactically-wrong SQL
  * MAX_BACKOFF_RETRIES — against BigQuery unavailability (in run_with_backoff)
plus a single empty-result revision.
"""
from __future__ import annotations

from app import config, errors
from app.graph.state import AgentState
from app.llm import get_llm, llm_text
from app.sources.bigquery import get_bq_runner
from app.sources.db import get_table_schema_text
from app.tools import reports as reports_tool
from app.tools import schema as schema_tool
from app.tools import sql_tools

_SOURCE_PROMPT = """Decide which data source answers the user's question. Reply with ONE word:
- "analytical": needs querying the retail warehouse data values
  (customers / products / orders / revenue / time-based metrics).
- "schema": asks about the DATABASE STRUCTURE itself (which tables/columns/types exist) — no data values.
- "reports": asks to READ the user's SAVED REPORTS library (list / view / search saved reports,
  individual fields of the reports table).

User question: {question}
One word: analytical | schema | reports"""


# --- data-source recognition ----------------------------------------------------
def _select_source(question: str) -> str:
    """Classify a `query` into its data source: analytical | schema | reports.

    A misread only changes which tables are queried — never whether a destructive
    op is gated. Defaults to ``analytical``.
    """
    try:
        resp = get_llm(config.SUPERVISOR_MODEL).invoke(_SOURCE_PROMPT.format(question=question))
        text = llm_text(resp).strip().lower()
    except Exception:
        return "analytical"
    for src in ("schema", "reports", "analytical"):
        if src in text:
            return src
    return "analytical"


# --- per-source handlers --------------------------------------------------------
def _answer_schema() -> dict:
    """Answer a DB-structure question from the cached schema (no BigQuery call)."""
    text = schema_tool.get_schema_text()
    return {
        "data_source": "schema",
        "schema_text": text,
        "sql": "-- schema introspection (cached, no BigQuery call)",
        "rows_markdown": text,
        "df_row_count": len(schema_tool.get_bq_tables()),
    }


def _answer_reports_read(state: AgentState) -> dict:
    """Build a SELECT over saved_reports; reports_gate executes + formats it."""
    debug = state.get("debug", False)
    question = state["question"]
    llm = get_llm(config.SQL_MODEL)

    try:
        schema = get_table_schema_text("saved_reports")
    except Exception as e:
        return {"final_message": errors.format_error(errors.REPORTS_GEN_FAILED, debug, e)}

    hint = ""
    last_reason = ""
    attempt = 0
    while attempt < config.MAX_SQL_ATTEMPTS:
        attempt += 1
        try:
            sql = reports_tool.generate_read_sql(llm, question, schema, hint)
        except Exception as e:
            return {
                "sql_attempts": attempt,
                "final_message": errors.format_error(errors.llm_error_message(e), debug, e),
            }
        ok, reason = sql_tools.preview_guard(sql)  # SELECT-only over saved_reports
        if not ok:
            last_reason = reason
            hint = reports_tool.GUARD_HINT.format(reason=reason)
            continue
        return {"sql": sql, "data_source": "reports", "sql_attempts": attempt}

    return {
        "sql_attempts": attempt,
        "last_error": last_reason,
        "final_message": errors.format_error(
            errors.REPORTS_GEN_FAILED, debug, extra=f"last reason: {last_reason}"
        ),
    }


def _sql_agent_destructive(state: AgentState) -> dict:
    """Generate a guarded DELETE/UPDATE (+ preview) for saved_reports.

    No execution here — reports_gate runs the preview, the human-in-the-loop
    ``interrupt()``, and the owner-scoped execute.
    """
    debug = state.get("debug", False)
    question = state["question"]
    llm = get_llm(config.SQL_MODEL)

    try:
        schema = get_table_schema_text("saved_reports")
    except Exception as e:
        return {"final_message": errors.format_error(errors.REPORTS_GEN_FAILED, debug, e)}

    hint = ""
    last_reason = ""
    attempt = 0
    while attempt < config.MAX_SQL_ATTEMPTS:
        attempt += 1
        try:
            sql, preview_sql = reports_tool.generate_destructive_sql(llm, question, schema, hint)
        except Exception as e:
            return {
                "sql_attempts": attempt,
                "final_message": errors.format_error(errors.llm_error_message(e), debug, e),
            }

        ok, reason = sql_tools.dml_guard(sql)  # DELETE/UPDATE on saved_reports only
        if not ok:
            last_reason = reason
            hint = reports_tool.GUARD_HINT.format(reason=reason)
            continue

        out = {"sql": sql, "data_source": "reports", "sql_attempts": attempt}
        # Carry the agent's own preview SELECT forward only if it passes the
        # preview guard; otherwise reports_gate derives one from the DML.
        if preview_sql:
            ok_p, _ = sql_tools.preview_guard(preview_sql)
            if ok_p:
                out["preview_sql"] = preview_sql
        return out

    return {
        "sql_attempts": attempt,
        "last_error": last_reason,
        "final_message": errors.format_error(
            errors.REPORTS_GEN_FAILED, debug, extra=f"last reason: {last_reason}"
        ),
    }


def _answer_analytical(state: AgentState) -> dict:
    """BigQuery analytical path: generate -> guard -> run -> self-correct."""
    debug = state.get("debug", False)
    question = state["question"]
    runner = get_bq_runner()

    try:
        schema = schema_tool.get_schema_text()
        tables = schema_tool.bq_table_list()
    except Exception as e:
        msg = errors.SERVICE_UNAVAILABLE if errors.is_retryable_bq(e) else errors.UNEXPECTED
        return {"final_message": errors.format_error(msg, debug, e)}

    out: dict = {"data_source": "analytical", "schema_text": schema}
    llm = get_llm(config.SQL_MODEL)
    error_hint = ""
    last_reason = ""
    recheck_used = False
    attempt = 0

    while attempt < config.MAX_SQL_ATTEMPTS:
        attempt += 1

        # generate (LLM failures here are non-retryable per §5.2)
        try:
            sql = sql_tools.generate_analytical_sql(llm, question, schema, tables, error_hint)
        except Exception as e:
            return {
                **out,
                "sql_attempts": attempt,
                "final_message": errors.format_error(errors.llm_error_message(e), debug, e),
            }

        # deterministic guard (before any execution)
        ok, reason = sql_tools.select_only_guard(sql)
        if not ok:
            last_reason = reason
            error_hint = sql_tools.SQL_GUARD_HINT.format(reason=reason)
            continue

        # execute (with backoff for transient unavailability)
        try:
            df = sql_tools.run_with_backoff(runner, sql)
        except errors.ServiceUnavailableError as e:
            return {
                **out, "sql": sql, "sql_attempts": attempt,
                "final_message": errors.format_error(errors.SERVICE_UNAVAILABLE, debug, e),
            }
        except errors.QueryError as e:
            last_reason = str(e)
            error_hint = sql_tools.SQL_ERROR_HINT.format(error=str(e))
            continue
        except Exception as e:
            return {
                **out, "sql": sql, "sql_attempts": attempt,
                "final_message": errors.format_error(errors.UNEXPECTED, debug, e),
            }

        # empty result -> single revision pass (independent of attempt budget)
        if len(df) == 0:
            if not recheck_used:
                recheck_used = True
                try:
                    revised = sql_tools.revise_analytical_empty(llm, question, sql)
                    ok2, _ = sql_tools.select_only_guard(revised)
                    if ok2:
                        df = sql_tools.run_with_backoff(runner, revised)
                        sql = revised
                except errors.ServiceUnavailableError as e:
                    return {
                        **out, "sql": sql,
                        "final_message": errors.format_error(errors.SERVICE_UNAVAILABLE, debug, e),
                    }
                except Exception:
                    # revision failed to help — fall through to "no data"
                    pass
            if len(df) == 0:
                return {
                    **out, "sql": sql, "df_row_count": 0, "sql_attempts": attempt,
                    "final_message": errors.NO_DATA,
                }

        # success
        return {
            **out,
            "sql": sql,
            "rows_markdown": sql_tools.df_to_markdown(df, config.LLM_ROWS_LIMIT),
            "df_row_count": int(len(df)),
            "sql_attempts": attempt,
        }

    # attempts exhausted
    return {
        **out,
        "sql_attempts": attempt,
        "last_error": last_reason,
        "final_message": errors.format_error(
            errors.SQL_GEN_FAILED, debug, extra=f"last reason: {last_reason}"
        ),
    }


def sql_agent(state: AgentState) -> dict:
    """Dispatch by flow intent; for `query`, recognise the data source first."""
    if state.get("intent") == "destructive":
        return _sql_agent_destructive(state)

    source = _select_source(state["question"])
    if source == "schema":
        try:
            return _answer_schema()
        except Exception as e:
            debug = state.get("debug", False)
            msg = errors.SERVICE_UNAVAILABLE if errors.is_retryable_bq(e) else errors.UNEXPECTED
            return {"final_message": errors.format_error(msg, debug, e)}
    if source == "reports":
        return _answer_reports_read(state)
    return _answer_analytical(state)
