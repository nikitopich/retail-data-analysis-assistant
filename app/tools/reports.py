"""Deterministic saved-reports helpers — parsing, preview fallback, row formatting.

No prompts, no LLM. SQL *generation* for the destructive path lives with the agent
(it owns the prompt); these are the deterministic pieces the agent/gate reuse.
"""
from __future__ import annotations

import re

from app.tools.sql_tools import strip_sql

_MARKER_RE = re.compile(r"(?im)^\s*(ACTION|PREVIEW)\s*:")


def parse_reports_output(text: str) -> tuple[str, str]:
    """Split a destructive-generation output into ``(action_sql, preview_sql)``.

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


def make_preview_sql(dml_sql: str) -> str:
    """Fallback: derive a preview SELECT from a DELETE/UPDATE by reusing its WHERE.

    Used only when the agent did not supply its own preview. Brittle on complex
    statements (subqueries with their own WHERE), which is why the agent emits the
    preview directly (see ``parse_reports_output``).
    """
    m = re.search(r'(?i)\bwhere\b\s+(.+)$', dml_sql.strip(), re.DOTALL)
    where = f" WHERE {m.group(1).strip()}" if m else ""
    return f"SELECT id, question, created_at FROM saved_reports{where}"


def format_rows(rows: list) -> str:
    """Render a list of dict rows as a compact markdown pipe-table."""
    if not rows:
        return "Nothing found."
    keys = list(rows[0].keys())
    lines = [
        "| " + " | ".join(keys) + " |",
        "| " + " | ".join("---" for _ in keys) + " |",
    ]
    for row in rows:
        cells = []
        for k in keys:
            v = str(row.get(k) or "")
            if k in ("report_md", "sql_query") and len(v) > 120:
                v = v[:120] + "…"
            cells.append(v.replace("|", "\\|"))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
