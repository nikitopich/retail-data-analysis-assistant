"""Deterministic SQL guards + SQL-building helpers (spec §5.1, §4.1).

The guards run BEFORE any execution and are the first of the two defence lines
for destructive ops (the second being owner-scoping in the repository). The
generation/execution helpers wrap the analytical (BigQuery) path; they take the
LLM and runner as arguments, so this module stays free of node/state coupling.
"""
from __future__ import annotations

import re
from typing import Tuple

import pandas as pd

from app import config, errors
from app.llm import llm_text
from app.retry import retry_with_backoff

# --- guards ---------------------------------------------------------------------
# Keywords that must never appear in an analytical (SELECT-only) query.
# NOTE: REPLACE is intentionally omitted (it is also a BigQuery string function).
_FORBIDDEN_IN_SELECT = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "MERGE", "GRANT", "REVOKE", "ATTACH", "DETACH", "PRAGMA", "VACUUM",
]

_FORBIDDEN_IN_DML = [
    "DROP", "ALTER", "TRUNCATE", "CREATE", "ATTACH", "DETACH", "PRAGMA",
    "VACUUM", "INSERT", "MERGE",
]


def _normalize(sql: str) -> str:
    """Strip surrounding whitespace and trailing semicolons."""
    return (sql or "").strip().rstrip(";").rstrip()


def _has_comment(sql: str) -> bool:
    return "--" in sql or "/*" in sql


def select_only_guard(sql: str) -> Tuple[bool, str]:
    """Allow exactly one SELECT (or WITH ... SELECT). Reject everything else."""
    if not sql or not sql.strip():
        return False, "empty query"
    s = _normalize(sql)
    if ";" in s:
        return False, "multiple statements are not allowed"
    if _has_comment(s):
        return False, "SQL comments are not allowed"
    upper = s.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        return False, "only a single SELECT (or WITH ... SELECT) is allowed"
    for kw in _FORBIDDEN_IN_SELECT:
        if re.search(rf"\b{kw}\b", upper):
            return False, f"forbidden keyword: {kw}"
    return True, ""


def preview_guard(sql: str) -> Tuple[bool, str]:
    """A destructive-preview (and reports-read) must be a SELECT over saved_reports."""
    ok, reason = select_only_guard(sql)
    if not ok:
        return ok, reason
    if not re.search(r"\bsaved_reports\b", sql, re.IGNORECASE):
        return False, "preview must select from saved_reports"
    return True, ""


def reports_sql_guard(sql: str) -> Tuple[bool, str]:
    """Allow SELECT / DELETE / UPDATE on saved_reports. Block everything else."""
    if not sql or not sql.strip():
        return False, "empty query"
    s = _normalize(sql)
    if ";" in s:
        return False, "multiple statements are not allowed"
    if _has_comment(s):
        return False, "SQL comments are not allowed"
    upper = s.upper()
    parts = upper.split()
    verb = parts[0] if parts else ""
    if verb in ("SELECT", "WITH"):
        for kw in _FORBIDDEN_IN_SELECT:
            if re.search(rf"\b{kw}\b", upper):
                return False, f"forbidden keyword: {kw}"
        if not re.search(r"\bsaved_reports\b", s, re.IGNORECASE):
            return False, "query must reference saved_reports"
        return True, ""
    if verb in ("DELETE", "UPDATE"):
        return dml_guard(sql)
    return False, f"only SELECT/DELETE/UPDATE allowed (got '{verb or '?'}')"


def dml_guard(sql: str) -> Tuple[bool, str]:
    """Allow exactly one DELETE/UPDATE on saved_reports. Reject everything else."""
    if not sql or not sql.strip():
        return False, "empty statement"
    s = _normalize(sql)
    if ";" in s:
        return False, "multiple statements are not allowed"
    if _has_comment(s):
        return False, "SQL comments are not allowed"
    upper = s.upper()
    parts = upper.split()
    verb = parts[0] if parts else ""
    if verb not in ("DELETE", "UPDATE"):
        return False, f"only DELETE/UPDATE allowed (got '{verb or '?'}')"
    if verb == "DELETE":
        if not re.match(r"DELETE\s+FROM\s+SAVED_REPORTS\b", upper):
            return False, "DELETE must target saved_reports"
    else:  # UPDATE
        if not re.match(r"UPDATE\s+SAVED_REPORTS\b", upper):
            return False, "UPDATE must target saved_reports"
    for kw in _FORBIDDEN_IN_DML:
        if re.search(rf"\b{kw}\b", upper):
            return False, f"forbidden keyword: {kw}"
    return True, ""


# --- SQL text helpers -----------------------------------------------------------
def strip_sql(text: str) -> str:
    """Remove markdown fences the model may emit despite instructions."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def df_to_markdown(df: pd.DataFrame, max_rows: int) -> str:
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


# --- analytical (BigQuery) generation + execution -------------------------------
_SQL_GEN_PROMPT = """You are a senior data analyst. Generate ONE BigQuery Standard SQL query that answers the
user's question using ONLY these tables (full names required):
{tables}

Schema:
{schema}

Rules:
- Output ONLY the SQL, no prose, no markdown fences.
- A single SELECT statement. Never DML/DDL.
- Add a reasonable LIMIT (e.g. {default_limit}) for row-listing queries; do NOT add LIMIT to pure aggregates.
- Use only the tables listed above.

User question: {question}
{error_hint}"""

SQL_ERROR_HINT = "\nPrevious SQL failed with: {error}. Fix it and output ONLY the corrected SQL."
SQL_GUARD_HINT = (
    "\nPrevious SQL was rejected by the safety guard ({reason}). "
    "Return a single valid SELECT statement only — no DML, no DDL, no comments, no extra statements."
)

_SQL_EMPTY_REVISION_PROMPT = """The previous query returned 0 rows. The filters may be too strict or wrong.
Revise the SQL (broaden/fix filters, check date ranges and joins). Output ONLY the SQL.
Question: {question}
Previous SQL: {sql}"""


def generate_analytical_sql(llm, question: str, schema: str, tables: str, error_hint: str) -> str:
    prompt = _SQL_GEN_PROMPT.format(
        schema=schema,
        tables=tables,
        question=question,
        error_hint=error_hint,
        default_limit=config.DEFAULT_LIMIT,
    )
    return strip_sql(llm_text(llm.invoke(prompt)))


def revise_analytical_empty(llm, question: str, sql: str) -> str:
    prompt = _SQL_EMPTY_REVISION_PROMPT.format(question=question, sql=sql)
    return strip_sql(llm_text(llm.invoke(prompt)))


@retry_with_backoff(
    retry_on=errors.is_retryable_bq,
    max_retries=config.MAX_BACKOFF_RETRIES,
    base_seconds=config.BACKOFF_BASE_SECONDS,
    max_seconds=config.BACKOFF_MAX_SECONDS,
    on_exhausted=lambda e: errors.ServiceUnavailableError(str(e)),
)
def run_with_backoff(runner, sql: str) -> pd.DataFrame:
    """Execute SQL, retrying ONLY on retryable (5xx/timeout) errors.

    Query/syntax errors are translated to ``QueryError`` here; since those are
    not ``is_retryable_bq``, the decorator re-raises them immediately (the caller
    regenerates). When the backoff budget is exhausted the decorator wraps the
    last error as ``ServiceUnavailableError``.
    """
    try:
        return runner.execute_query(sql)
    except Exception as e:
        if errors.is_query_bq(e):
            raise errors.QueryError(str(e)) from e
        raise
