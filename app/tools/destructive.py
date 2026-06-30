"""Destructive-op helpers: hybrid confirmation, preview fallback, row formatting.

The deterministic guards (``dml_guard``/``preview_guard``) live in ``sql_tools``
and the owner-scoped execution in ``sources.reports_repo``; this module holds the
human-in-the-loop *confirmation* logic used by the reports gate node.
"""
from __future__ import annotations

import re

from app import config
from app.llm import get_llm, llm_text

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
        llm = get_llm(config.SUPERVISOR_MODEL)
        resp = llm.invoke(_CONFIRM_PROMPT.format(answer=answer))
        return llm_text(resp).strip().lower().startswith("confirm")
    except Exception:
        return False


def is_confirmed(answer) -> bool:
    """Hybrid confirmation: deterministic floor first, LLM only on the residual.

    The deterministic sets are both the fast path and the safety floor — the LLM
    can never widen what counts as 'yes', only disambiguate replies that match
    neither set, and always toward the safe (cancel) default.
    """
    if answer is None:
        return False
    norm = str(answer).strip().lower().strip(".!?")
    if norm in _YES:
        return True
    if norm in _NO:
        return False
    return _llm_confirm(answer)


def make_preview_sql(dml_sql: str) -> str:
    """Fallback: derive a preview SELECT from a DELETE/UPDATE by reusing its WHERE.

    Used only when the SQL agent did not supply its own ``preview_sql``. Brittle
    on complex statements (e.g. subqueries with their own WHERE), which is why
    the agent emits the preview directly (see ``tools.reports.parse_reports_output``).
    """
    m = re.search(r'(?i)\bwhere\b\s+(.+)$', dml_sql.strip(), re.DOTALL)
    where = f" WHERE {m.group(1).strip()}" if m else ""
    return f"SELECT id, question, created_at FROM saved_reports{where}"


def format_rows(rows: list) -> str:
    """Render a list of dict rows as a compact markdown pipe-table."""
    if not rows:
        return "Ничего не найдено."
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
