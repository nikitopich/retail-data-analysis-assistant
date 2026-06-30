"""SQL Agent node — a tool-calling agent for reads + a gated generator for writes.

  * intent == "query"        -> bind the deterministic query tools to the LLM and
    run a capped tool-calling loop. The model RECOGNISES THE DATA SOURCE by
    choosing a tool (run_bigquery_query / query_saved_reports / fetch_bq_schema)
    and writes the SQL as the tool *argument*. Guards live inside the tools.
  * intent == "destructive"  -> generate a guarded DELETE/UPDATE (+ preview) for
    saved_reports; the reports_gate runs the human-in-the-loop confirm.

The prompts live here (the agent owns them); the tools in ``app.tools`` are
prompt-free and deterministic. The hard resilience budget (spec §2.2) is kept by
capping the loop at ``MAX_SQL_ATTEMPTS`` turns; guards + backoff live in the tools.
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from app import config, errors
from app.graph.state import AgentState
from app.llm import get_llm, llm_text
from app.sources.db import get_table_schema_text
from app.tools import reports, sql_tools
from app.tools import schema as schema_mod
from app.tools.query_tools import DATA_TOOLS, SOURCE_BY_TOOL, TOOLS_BY_NAME

_QUERY_SYSTEM_TPL = """You are a data analyst for a retail analytics assistant. Answer the user's question by
calling the available tools — choose the right data source yourself:

BigQuery schema (full — use this to write correct SQL, no schema tool call needed):
{schema_text}

- Values in the retail warehouse (customers, products, orders, revenue, time-based metrics):
  write ONE BigQuery Standard SQL SELECT and call run_bigquery_query. Do this even if you think
  the time range has no data — run the query and let the result speak for itself.
- A question about the database STRUCTURE itself (tables, columns, types, which tables exist,
  what a table contains): answer directly from the schema above — no tool call needed. If the user
  asks about a table that is NOT in the schema, say so explicitly.
- The user's SAVED REPORTS library (list / search / view saved reports): write ONE SQLite SELECT on the
  saved_reports table and call query_saved_reports.
Rules: never write DML/DDL here. Never answer data questions from assumptions or training knowledge —
always call run_bigquery_query to get actual figures. If a tool returns 'ERROR:', fix your SQL and call
it again. If it returns 'NO_DATA:', you may broaden the filters once. Stop calling tools as soon as you
have the data."""

_DESTRUCTIVE_PROMPT = """You write a destructive SQLite statement against the user's saved-reports library.

Schema (live, from the database):
{schema}

Rules:
- The operation is a DELETE or an UPDATE on saved_reports only. Never SELECT-only, never DDL/INSERT.
- NEVER reference or filter on owner_id — ownership is enforced in code.
- Deleting: DELETE FROM saved_reports WHERE <condition>
- Updating: UPDATE saved_reports SET <col> = <val> WHERE <condition>
  Allowed columns to SET: question, report_md, published_to_golden
- Searching by topic: ALWAYS use LIKE '%term%', even when the user puts the name in quotes —
  saved titles may have extra words (e.g. WHERE question LIKE '%Топ-5 клиентов%')
- "today" → date(created_at) = date('now')

Output EXACTLY TWO lines (no prose, no markdown fences):
PREVIEW: SELECT id, question, created_at FROM saved_reports WHERE <condition>
ACTION: <DELETE FROM saved_reports WHERE <condition> | UPDATE saved_reports SET ... WHERE <condition>>
The PREVIEW must select the EXACT same rows the ACTION affects — identical WHERE clause.

User question: {question}
{error_hint}"""

_GUARD_HINT = (
    "\nPrevious SQL was rejected ({reason}). "
    "Return a valid DELETE/UPDATE on saved_reports — no DDL, no INSERT. "
    "Keep the PREVIEW:/ACTION: output format."
)


# --- query: tool-calling loop ---------------------------------------------------
def _run_query_agent(state: AgentState) -> dict:
    """Drive the LLM tool-calling loop and capture the resulting data + source.

    Schema is fetched once per session (lru_cache on get_schema_text prevents
    repeated BQ calls) and injected into the system prompt so the LLM never
    needs to call fetch_bq_schema as a tool.
    """
    debug = state.get("debug", False)

    # Reuse schema cached from a previous turn; fetch (and cache) if first turn.
    schema_text = state.get("schema_text") or ""
    if not schema_text:
        try:
            schema_text = schema_mod.get_schema_text()
        except errors.ServiceUnavailableError:
            return {"final_message": errors.SERVICE_UNAVAILABLE}
        except Exception as e:
            return {"final_message": errors.format_error(errors.UNEXPECTED, debug, e)}

    llm = get_llm(config.SQL_MODEL).bind_tools(DATA_TOOLS)
    system_prompt = _QUERY_SYSTEM_TPL.format(schema_text=schema_text)
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=state["question"])]

    last_data = None       # (source, markdown, sql) for the last SUCCESSFUL data call
    last_data_raw = None   # raw string of the last data-tool call (any outcome)
    direct_answer = ""     # LLM prose when it answers from context (no tool call)

    # Cap LLM turns: MAX_SQL_ATTEMPTS bounds the generate/retry budget; the extra
    # turn lets the model produce a final no-tool answer.
    for _ in range(config.MAX_SQL_ATTEMPTS + 1):
        try:
            ai = llm.invoke(messages)
        except Exception as e:
            return {"final_message": errors.format_llm_error(e, debug)}
        messages.append(ai)

        tool_calls = getattr(ai, "tool_calls", None) or []
        if not tool_calls:
            direct_answer = llm_text(ai).strip()
            break

        turn_cache: dict[tuple, str] = {}
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("args", {}) or {}
            fn = TOOLS_BY_NAME.get(name)
            cache_key = (name, str(sorted(args.items())))
            if cache_key in turn_cache:
                result = turn_cache[cache_key]
            else:
                result = str(fn.invoke(args)) if fn is not None else f"ERROR: unknown tool {name}"
                turn_cache[cache_key] = result
            messages.append(ToolMessage(content=result, tool_call_id=tc.get("id", name)))

            src = SOURCE_BY_TOOL.get(name)
            if src in ("analytical", "reports"):
                last_data_raw = result
                if not result.startswith(("ERROR", "NO_DATA")):
                    sql_arg = args.get("sql") or args.get("select_sql") or ""
                    last_data = (src, result, sql_arg)

    # Propagate schema into state so the next turn skips the fetch entirely.
    base = {"schema_text": schema_text}

    # decide the node output (precedence: data ok > data failed > direct > none)
    if last_data:
        source, markdown, sql_arg = last_data
        if source == "reports":
            return {**base, "data_source": "reports", "sql": sql_arg, "final_message": markdown}
        return {**base, "data_source": "analytical", "sql": sql_arg, "rows_markdown": markdown}

    if last_data_raw is not None:
        if last_data_raw.startswith("NO_DATA"):
            return {**base, "final_message": errors.NO_DATA}
        if "temporarily unavailable" in last_data_raw:
            return {**base, "final_message": errors.SERVICE_UNAVAILABLE}
        return {**base, "final_message": errors.SQL_GEN_FAILED}

    if direct_answer:
        # LLM answered a schema/meta question from context without any tool call.
        # Goes directly to END (no report_agent) — final_message carries the output.
        return {**base, "data_source": "schema", "final_message": direct_answer}

    return {**base, "final_message": errors.SQL_GEN_FAILED}


# --- destructive: gated generation ----------------------------------------------
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
        prompt = _DESTRUCTIVE_PROMPT.format(schema=schema, question=question, error_hint=hint)
        try:
            sql, preview_sql = reports.parse_reports_output(llm_text(llm.invoke(prompt)))
        except Exception as e:
            return {
                "sql_attempts": attempt,
                "final_message": errors.format_llm_error(e, debug),
            }

        ok, reason = sql_tools.dml_guard(sql)  # DELETE/UPDATE on saved_reports only
        if not ok:
            last_reason = reason
            hint = _GUARD_HINT.format(reason=reason)
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


def sql_agent(state: AgentState) -> dict:
    """Dispatch by flow intent: tool-calling reads, or gated destructive generation."""
    if state.get("intent") == "destructive":
        return _sql_agent_destructive(state)
    return _run_query_agent(state)
