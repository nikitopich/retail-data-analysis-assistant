"""SQL Agent node — Schema -> GenerateSQL -> Guard -> RunSQL with self-correction.

The resilience loop (spec §2.2) is implemented as an internal Python loop with
two independent budgets:
  * MAX_SQL_ATTEMPTS  — against bad / guard-rejected / syntactically-wrong SQL
  * MAX_BACKOFF_RETRIES — against BigQuery unavailability (exp backoff)
plus a single empty-result revision. This caps LLM regenerations hard, which is
the anti-cost-inflation guarantee.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import pandas as pd

from app import config, errors, prompts
from app.bq_client import get_bq_runner
from app.guards import select_only_guard
from app.state import AgentState

# --- schema cache (one fetch per process, spec §3.3) ---
_schema_cache: Optional[str] = None

# Heuristic for "what tables/columns exist?" — answered from the cached schema
# without touching BigQuery (spec §13.3).
_SCHEMA_KEYWORDS = (
    "структур", "какие таблиц", "таблицы есть", "какие столбц", "колон",
    "столбц", "какие поля", "schema", "tables", "columns", "fields",
    "what tables", "which tables", "list tables",
)


def _get_schema_text(runner) -> str:
    global _schema_cache
    if _schema_cache is None:
        blocks = []
        for table in config.BQ_TABLES:
            cols = runner.get_table_schema(table)
            lines = [
                f"  - {c['name']} {c['type']} {c['mode']}".rstrip()
                for c in cols
            ]
            blocks.append(
                f"Table `{config.BQ_DATASET}.{table}`:\n" + "\n".join(lines)
            )
        _schema_cache = "\n\n".join(blocks)
    return _schema_cache


def _is_schema_question(question: str) -> bool:
    q = question.lower()
    return any(k in q for k in _SCHEMA_KEYWORDS)


def _strip_sql(text: str) -> str:
    """Remove markdown fences the model may emit despite instructions."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _df_to_markdown(df: pd.DataFrame, max_rows: int) -> str:
    """Compact pipe-table rendering without the optional `tabulate` dep."""
    head = df.head(max_rows)
    cols = [str(c) for c in head.columns]
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for _, row in head.iterrows():
        cells = ["" if pd.isna(v) else str(v) for v in row]
        lines.append("| " + " | ".join(cells) + " |")
    table = "\n".join(lines)
    if len(df) > max_rows:
        table += f"\n(showing first {max_rows} of {len(df)} rows)"
    return table


def _generate_sql(llm, question: str, schema: str, error_hint: str) -> str:
    prompt = prompts.SQL_GEN_PROMPT.format(
        schema=schema, question=question, error_hint=error_hint
    )
    resp = llm.invoke(prompt)
    return _strip_sql(resp.content)


def _revise_sql_empty(llm, question: str, sql: str) -> str:
    prompt = prompts.SQL_EMPTY_REVISION_PROMPT.format(question=question, sql=sql)
    resp = llm.invoke(prompt)
    return _strip_sql(resp.content)


def _run_with_backoff(runner, sql: str) -> pd.DataFrame:
    """Execute SQL, retrying ONLY on retryable (5xx/timeout) errors.

    Raises ``QueryError`` for syntax/query errors (caller regenerates) and
    ``ServiceUnavailableError`` once the backoff budget is exhausted.
    """
    delay = config.BACKOFF_BASE_SECONDS
    tries = 0
    while True:
        try:
            return runner.execute_query(sql)
        except Exception as e:
            if errors.is_query_bq(e):
                raise errors.QueryError(str(e)) from e
            if not errors.is_retryable_bq(e):
                raise
            if tries >= config.MAX_BACKOFF_RETRIES:
                raise errors.ServiceUnavailableError(str(e)) from e
            time.sleep(min(delay, config.BACKOFF_MAX_SECONDS))
            delay *= 2
            tries += 1


def sql_agent(state: AgentState) -> dict:
    debug = state.get("debug", False)
    question = state["question"]
    runner = get_bq_runner()

    # --- schema (cached) ---
    try:
        schema = _get_schema_text(runner)
    except Exception as e:
        msg = errors.SERVICE_UNAVAILABLE if errors.is_retryable_bq(e) else errors.UNEXPECTED
        return {"final_message": errors.format_error(msg, debug, e)}

    out: dict = {"schema_text": schema}

    # --- structure/DB-introspection question -> answer from cached schema ---
    if _is_schema_question(question):
        out.update({
            "sql": "-- schema introspection (cached, no BigQuery call)",
            "rows_markdown": schema,
            "df_row_count": len(config.BQ_TABLES),
        })
        return out

    llm = config.get_llm(config.SQL_MODEL)
    error_hint = ""
    last_reason = ""
    recheck_used = False
    attempt = 0

    while attempt < config.MAX_SQL_ATTEMPTS:
        attempt += 1

        # generate (LLM failures here are non-retryable per §5.2)
        try:
            sql = _generate_sql(llm, question, schema, error_hint)
        except Exception as e:
            return {
                **out,
                "sql_attempts": attempt,
                "final_message": errors.format_error(errors.LLM_UNAVAILABLE, debug, e),
            }

        # deterministic guard (before any execution)
        ok, reason = select_only_guard(sql)
        if not ok:
            last_reason = reason
            error_hint = prompts.SQL_GUARD_HINT.format(reason=reason)
            continue

        # execute (with backoff for transient unavailability)
        try:
            df = _run_with_backoff(runner, sql)
        except errors.ServiceUnavailableError as e:
            return {
                **out, "sql": sql, "sql_attempts": attempt,
                "final_message": errors.format_error(errors.SERVICE_UNAVAILABLE, debug, e),
            }
        except errors.QueryError as e:
            last_reason = str(e)
            error_hint = prompts.SQL_ERROR_HINT.format(error=str(e))
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
                    revised = _revise_sql_empty(llm, question, sql)
                    ok2, _ = select_only_guard(revised)
                    if ok2:
                        df = _run_with_backoff(runner, revised)
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
            "rows_markdown": _df_to_markdown(df, config.LLM_ROWS_LIMIT),
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
