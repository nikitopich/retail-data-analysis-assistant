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

import re

from app import config, errors
from app.graph.state import AgentState
from app.llm import get_llm, llm_text
from app.sources.reports_repo import flush_pending_trio

# Patterns that indicate injection in a destructive request (checked only when
# intent == "destructive" to keep false-positive risk low).
#
# Three categories:
# 1. SQL injection — statement concatenation, comment tokens, UNION/EXEC.
# 2. Natural-language prompt injection — "ignore your rules", "forget your instructions" (EN/RU).
# 3. Protected table reference — user is trying to target a BQ table, not saved_reports.
_INJECTION_RE = re.compile(
    # --- SQL injection ---
    r";\s*(drop|delete|truncate|alter|create|insert|update|grant|revoke|exec)\b"
    r"|--"
    r"|/\*"
    r"|\bunion\s+select\b"
    r"|\bexec\s*\("
    r"|\bexecute\s*\("
    # --- Natural-language prompt injection (EN) ---
    r"|\bignore\s+(your\s+)?(rules|instructions|system|constraints|prompt)\b"
    r"|\bforget\s+(your\s+)?(rules|instructions|previous)\b"
    r"|\bbypass\s+(the\s+)?(rules|restrictions|guard|filter)\b"
    r"|\bdisregard\s+(your\s+)?(rules|instructions)\b"
    # --- Natural-language prompt injection (RU) ---
    r"|\bигнорируй\s+(свои\s+)?(правила|инструкции|ограничения)\b"
    r"|\bзабудь\s+(свои\s+)?(правила|инструкции)\b"
    r"|\bобойди\s+(правила|ограничения|фильтр)\b"
    r"|\bне\s+следуй\s+(правилам|инструкциям)\b"
    # --- Protected BigQuery table reference in a destructive request ---
    r"|\b(?:from|из\s+таблицы|таблицу?)\s+"
    r"(users|orders|products|inventory_items|order_items|events|distribution_centers)\b",
    re.IGNORECASE,
)

_INJECTION_WARNING = (
    "⚠️ The request contains signs of SQL injection and was rejected. "
    "Please phrase the operation on reports without SQL operators."
)

_LABELS = ("query", "destructive", "regenerate", "set_preference", "feedback_positive", "info", "other")

_SUPERVISOR_PROMPT = """You are a router for a retail analytics assistant.
Classify the user's message into EXACTLY one label:
- "query": a request to READ data or schema — analytical questions about retail data
  (customers, products, orders, revenue, time-based metrics), questions about the
  database STRUCTURE/schema (which tables or columns exist, types), OR browsing the
  user's saved-reports library (list/view/search, e.g. "show my reports",
  "покажи мои отчёты", "find reports about client X", "покажи второй отчёт").
- "destructive": a request to DELETE or MODIFY saved reports (e.g. "delete my reports",
  "удали отчёты про клиента X", "переименуй отчёт", "очисти библиотеку за сегодня").
- "regenerate": a ONE-OFF request to FIX, redo, reformat, shorten, or adjust THIS PREVIOUS report
  the assistant just produced (e.g. "сделай короче", "в виде маркированного списка",
  "убери лишнее", "перегенерируй", "не то, переделай", "now show it as bullet points").
  No new data needs to be queried. The change applies only to the current report.
- "set_preference": the user wants the assistant to REMEMBER a standing preference for how ALL
  FUTURE reports should be written — their default format, length, tone, or style. It is a setting
  to save, not an analytical question and not a one-off edit of the current report. Examples (ALL of
  these are set_preference): "всегда присылай отчёты в формате CSV", "присылай отчёты в виде CSV",
  "по умолчанию делай отчёты покороче", "впредь выводи отчёты маркированным списком",
  "запомни: люблю краткие сводки без эмодзи", "хочу всегда видеть таблицу", "формат по умолчанию — CSV",
  "from now on use bullet points", "always format reports as a table", "by default keep it short".
  Strong cues: "всегда", "по умолчанию", "впредь", "запомни", "хочу чтобы"/"хочу всегда", "always",
  "from now on", "by default" — together with a mention of report format / length / tone / style.
- "feedback_positive": the user expresses satisfaction or approval of the previous report
  (e.g. "нравится", "отлично", "хорошо", "спасибо", "супер", "класс", "всё верно",
  "👍", "great", "perfect", "thanks", "cool").
- "info": a meta question about THIS ASSISTANT's capabilities, what databases or data
  sources it can access, or what it can do (e.g. "which databases do you have access to",
  "what can you query", "in which db I have access", "к каким данным у тебя есть доступ",
  "что ты умеешь", "what data do you have").
- "other": greetings, off-topic, or unintelligible requests. NOT for report-format/style
  preferences (those are set_preference) and NOT for analytical questions (those are query).
Tie-break: if the message tells the assistant how it should format or write reports from now on
(a remembered default — format/length/tone/style), choose set_preference over other or regenerate.
Reply with only one word: query | destructive | regenerate | set_preference | feedback_positive | info | other.

User message: {question}"""

_INFO_RESPONSE = (
    "I have access to two data sources:\n\n"
    "1. **BigQuery** — retail data (customers, products, orders, revenue, and time-based metrics).\n"
    "2. **SQLite** — your personal library of saved reports.\n\n"
    "Ask analytical questions about retail, inquire about the database structure, "
    "or manage your saved reports (view, search, delete)."
)


def supervisor(state: AgentState) -> dict:
    """Classify the user's message into one flow intent.

    Also resets the per-turn control fields that must NOT leak across turns now
    that a single checkpointer thread is shared for the whole session (a stale
    ``final_message`` would short-circuit routing straight to END). Data fields
    (``sql``/``rows_markdown``/``report_md``/``last_question``) are intentionally
    preserved — the ``regenerate`` flow reads the previous report from them.

    Trio capture: if a ``pending_trio`` exists in state from the previous turn:
    - ``regenerate`` → discard (user wants to fix the report, not approve it)
    - ``feedback_positive`` → flush immediately (explicit approval)
    - anything else → flush (implicit approval: user moved on without complaining)
    """
    question = state["question"]
    pending_trio = state.get("pending_trio")

    llm = get_llm(config.SUPERVISOR_MODEL)
    prompt = _SUPERVISOR_PROMPT.format(question=question)

    try:
        resp = llm.invoke(prompt)
        text = llm_text(resp).strip().lower()
    except Exception as e:
        return {
            "intent": "other",
            "final_message": errors.format_llm_error(e, state.get("debug", False)),
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

    # Reject prompt injection before it reaches the SQL agent.
    if intent == "destructive" and _INJECTION_RE.search(question):
        return {
            "intent": "other",
            "final_message": _INJECTION_WARNING,
            "confirmed": None,
            "preview_sql": None,
            "last_error": None,
            "pending_trio": None,
        }

    # Trio flush / discard based on intent. `set_preference` is treated like
    # `regenerate` (discard): we re-render the last report rather than approve it.
    if pending_trio and intent not in ("regenerate", "set_preference"):
        flush_pending_trio(pending_trio)

    # Per-turn hygiene (shared-thread): clear control fields from the prior turn.
    out: dict = {
        "intent": intent,
        "final_message": "",
        "confirmed": None,
        "preview_sql": None,
        "last_error": None,
        "pending_trio": None,  # consumed (flushed or discarded) on every turn
    }

    if intent == "feedback_positive":
        out["final_message"] = "Glad you liked it!"
        out["intent"] = "other"  # routes to END
    elif intent == "info":
        out["final_message"] = _INFO_RESPONSE
        out["intent"] = "other"  # routes to END
    elif intent == "other":
        out["final_message"] = errors.OTHER_INTENT

    return out
