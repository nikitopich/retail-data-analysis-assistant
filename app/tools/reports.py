"""Saved-reports SQL generation + parsing (SQLite library).

LLM-driven SQL *building* for the saved-reports library — read (SELECT) and
destructive (DELETE/UPDATE + preview). The deterministic guards live in
``sql_tools``; the owner-scoped execution lives in ``sources.reports_repo``.
The agent orchestrates the attempt/guard loop; these helpers are single-shot.
"""
from __future__ import annotations

import re

from app.llm import llm_text
from app.tools.sql_tools import strip_sql

_REPORTS_READ_PROMPT = """You write ONE SQLite SELECT against the user's saved-reports library.

Schema (live, from the database):
{schema}

Rules:
- A single SELECT on saved_reports only. Never DML/DDL.
- NEVER reference or filter on owner_id — ownership is enforced in code.
- Listing/browsing: SELECT id, question, created_at FROM saved_reports ORDER BY created_at DESC LIMIT 20
- Viewing content: SELECT question, report_md, created_at FROM saved_reports WHERE <condition>
- Searching by topic: WHERE question LIKE '%term%' OR report_md LIKE '%term%'
- "second" / "2nd" / "второй": LIMIT 1 OFFSET 1 with ORDER BY created_at DESC
- "today" → date(created_at) = date('now')

Output ONLY the SQL (no prose, no markdown fences).
User question: {question}
{error_hint}"""

_DESTRUCTIVE_PROMPT = """You write a destructive SQLite statement against the user's saved-reports library.

Schema (live, from the database):
{schema}

Rules:
- The operation is a DELETE or an UPDATE on saved_reports only. Never SELECT-only, never DDL/INSERT.
- NEVER reference or filter on owner_id — ownership is enforced in code.
- Deleting: DELETE FROM saved_reports WHERE <condition>
- Updating: UPDATE saved_reports SET <col> = <val> WHERE <condition>
  Allowed columns to SET: question, report_md, published_to_golden
- Searching by topic: WHERE question LIKE '%term%' OR report_md LIKE '%term%'
- "today" → date(created_at) = date('now')

Output EXACTLY TWO lines (no prose, no markdown fences):
PREVIEW: SELECT id, question, created_at FROM saved_reports WHERE <condition>
ACTION: <DELETE FROM saved_reports WHERE <condition> | UPDATE saved_reports SET ... WHERE <condition>>
The PREVIEW must select the EXACT same rows the ACTION affects — identical WHERE clause.

User question: {question}
{error_hint}"""

GUARD_HINT = (
    "\nPrevious SQL was rejected ({reason}). "
    "Return a valid statement on saved_reports — no DDL, no INSERT. "
    "Keep the required output format."
)

_MARKER_RE = re.compile(r"(?im)^\s*(ACTION|PREVIEW)\s*:")


def parse_reports_output(text: str) -> tuple[str, str]:
    """Split the destructive-agent output into ``(action_sql, preview_sql)``.

    The agent emits a labeled, structured response (``ACTION:`` and ``PREVIEW:``).
    Parsing the model's own preview is robust where deriving one by regex-rewriting
    the DML's WHERE clause is not (subqueries, multiple WHEREs, etc.). If the model
    ignored the format and returned a bare statement, the whole text is the action.
    """
    t = strip_sql(text)
    markers = list(_MARKER_RE.finditer(t))
    if not markers:
        return t.strip(), ""
    sections: dict[str, str] = {}
    for i, m in enumerate(markers):
        label = m.group(1).upper()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(t)
        sections[label] = strip_sql(t[m.end():end])
    return sections.get("ACTION", "").strip(), sections.get("PREVIEW", "").strip()


def generate_read_sql(llm, question: str, schema: str, error_hint: str = "") -> str:
    """One SELECT over saved_reports (guard/loop handled by the caller)."""
    prompt = _REPORTS_READ_PROMPT.format(schema=schema, question=question, error_hint=error_hint)
    return strip_sql(llm_text(llm.invoke(prompt)))


def generate_destructive_sql(llm, question: str, schema: str, error_hint: str = "") -> tuple[str, str]:
    """A DELETE/UPDATE (+ preview SELECT) over saved_reports. Returns (action, preview)."""
    prompt = _DESTRUCTIVE_PROMPT.format(schema=schema, question=question, error_hint=error_hint)
    return parse_reports_output(llm_text(llm.invoke(prompt)))
