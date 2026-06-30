"""Supervisor node — thin flow-intent classification (spec §3.2, §6.1).

Deliberately thin: the supervisor only decides the *flow* (does this turn read
data, mutate the report library, revise the previous report, or none of these).
The *data-source* decision for a read (`query`) is made downstream by the SQL
agent — see ``app/agents/sql_agent.py``. Destructive routing is steered here, but
the human-in-the-loop gate ultimately triggers on the deterministic SQL verb in
``reports_gate``, not on this label — so a misclassification can never skip the
confirmation (defense in depth).
"""
from __future__ import annotations

from app import config, errors
from app.graph.state import AgentState
from app.llm import get_llm, llm_text

_LABELS = ("query", "destructive", "regenerate", "other")

_SUPERVISOR_PROMPT = """You are a router for a retail analytics assistant.
Classify the user's message into EXACTLY one label:
- "query": any request to READ information — analytical questions about retail data
  (customers, products, orders, revenue, time-based metrics), questions about the
  database STRUCTURE/schema (which tables or columns exist, types), OR browsing the
  user's saved-reports library (list/view/search, e.g. "show my reports",
  "покажи мои отчёты", "find reports about client X", "покажи второй отчёт").
- "destructive": a request to DELETE or MODIFY saved reports (e.g. "delete my reports",
  "удали отчёты про клиента X", "переименуй отчёт", "очисти библиотеку за сегодня").
- "regenerate": a request to FIX, redo, reformat, shorten, or adjust the PREVIOUS report
  the assistant just produced (e.g. "сделай короче", "в виде маркированного списка",
  "убери лишнее", "перегенерируй", "не то, переделай", "now show it as bullet points").
  No new data needs to be queried.
- "other": greetings, off-topic, or unintelligible requests.
Reply with only one word: query | destructive | regenerate | other.

User message: {question}"""


def supervisor(state: AgentState) -> dict:
    """Classify the user's message into one flow intent.

    Also resets the per-turn control fields that must NOT leak across turns now
    that a single checkpointer thread is shared for the whole session (a stale
    ``final_message`` would short-circuit routing straight to END). Data fields
    (``sql``/``rows_markdown``/``report_md``/``last_question``) are intentionally
    preserved — the ``regenerate`` flow reads the previous report from them.
    """
    question = state["question"]
    llm = get_llm(config.SUPERVISOR_MODEL)
    prompt = _SUPERVISOR_PROMPT.format(question=question)

    try:
        resp = llm.invoke(prompt)
        text = llm_text(resp).strip().lower()
    except Exception as e:
        return {
            "intent": "other",
            "final_message": errors.format_error(
                errors.llm_error_message(e), state.get("debug", False), e
            ),
        }

    intent = "other"
    tokens = text.replace("|", " ").split()
    if tokens and tokens[0] in _LABELS:
        intent = tokens[0]
    else:
        for label in _LABELS:
            if label in text:
                intent = label
                break

    # Per-turn hygiene (shared-thread): clear control fields from the prior turn.
    out: dict = {
        "intent": intent,
        "final_message": "",
        "confirmed": None,
        "preview_sql": None,
        "last_error": None,
    }
    if intent == "other":
        out["final_message"] = errors.OTHER_INTENT
    return out
