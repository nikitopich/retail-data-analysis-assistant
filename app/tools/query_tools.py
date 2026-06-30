"""The query agent's tool surface — deterministic ``@tool`` functions (spec §2.3).

These are what the SQL agent binds to the LLM. Each is deterministic, runs its
guard BEFORE executing, and returns a plain string the model can read. On failure
they return a string starting with ``ERROR:`` (so the model can fix its SQL and
retry) or ``NO_DATA:``. The SQL is the LLM-written tool *argument* — there is no
prompt in this module.
"""
from __future__ import annotations

from langchain_core.tools import tool

from app import config, errors
from app.sources.bigquery import get_bq_runner
from app.sources.reports_repo import SavedReportsRepo
from app.tools import reports, sql_tools


@tool
def fetch_bq_schema() -> str:
    """Return the BigQuery dataset schema: every table with its columns and types.

    Call this when you need to know which tables/columns exist before writing a
    query, or to answer a question about the database STRUCTURE itself.
    """
    from app.tools import schema
    try:
        return schema.get_schema_text()
    except errors.ServiceUnavailableError:
        return "ERROR: BigQuery is temporarily unavailable"
    except Exception as e:
        return f"ERROR: {e}"


@tool
def run_bigquery_query(sql: str) -> str:
    """Execute ONE read-only BigQuery Standard SQL SELECT and return rows as a markdown table.

    Use for analytical questions about the retail data (customers, products, orders,
    revenue, time-based metrics). ``sql`` must be a single SELECT (or WITH ... SELECT);
    it is validated before execution. On a guard/query error returns a string starting
    with 'ERROR:' — fix the SQL and call again. Returns 'NO_DATA:' if 0 rows.
    """
    ok, reason = sql_tools.select_only_guard(sql)
    if not ok:
        return f"ERROR: rejected by safety guard ({reason})"
    try:
        df = sql_tools.run_with_backoff(get_bq_runner(), sql)
    except errors.ServiceUnavailableError:
        return "ERROR: BigQuery is temporarily unavailable"
    except errors.QueryError as e:
        return f"ERROR: query failed: {e}"
    except Exception as e:
        return f"ERROR: {e}"
    if len(df) == 0:
        return "NO_DATA: the query returned 0 rows; broaden or fix the filters and try again"
    return sql_tools.df_to_markdown(df, config.LLM_ROWS_LIMIT)


@tool
def query_saved_reports(select_sql: str) -> str:
    """Run ONE read-only SELECT against the user's saved-reports library; return rows as markdown.

    Use to list / search / view the user's SAVED REPORTS. ``select_sql`` must be a
    single SELECT on the ``saved_reports`` table; ownership is enforced automatically
    in code (never filter on owner_id yourself). On a guard error returns 'ERROR:';
    returns 'NO_DATA:' if nothing matches.
    """
    ok, reason = sql_tools.preview_guard(select_sql)
    if not ok:
        return f"ERROR: rejected by safety guard ({reason})"
    try:
        rows = SavedReportsRepo().run_select(select_sql, config.CURRENT_USER_ID)
    except errors.ServiceUnavailableError:
        return "ERROR: saved-reports store is temporarily unavailable"
    except Exception as e:
        return f"ERROR: {e}"
    if not rows:
        return "NO_DATA: nothing found in the saved-reports library"
    return reports.format_rows(rows)


# Full tool list (kept for TOOLS_BY_NAME fallback lookups).
QUERY_TOOLS = [fetch_bq_schema, run_bigquery_query, query_saved_reports]
TOOLS_BY_NAME = {t.name: t for t in QUERY_TOOLS}

# Subset bound to the LLM: schema is injected into the system prompt instead,
# so fetch_bq_schema is not exposed as a callable tool.
DATA_TOOLS = [run_bigquery_query, query_saved_reports]

# Which data source a successful tool call represents (drives post-SQL routing).
SOURCE_BY_TOOL = {
    "run_bigquery_query": "analytical",
    "query_saved_reports": "reports",
    "fetch_bq_schema": "schema",
}
