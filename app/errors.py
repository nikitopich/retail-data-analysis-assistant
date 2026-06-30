"""Error taxonomy + scenario user-messages + debug formatting (spec §5.2, §8).

In normal mode the user only ever sees the scenario strings below. In debug
mode ``format_error`` appends the traceback, any extra context, and a Phoenix
trace link.
"""
from __future__ import annotations

import sqlite3
import traceback
from typing import Optional

# google exceptions are an optional import: lightweight entrypoints (init_db)
# must not require the BigQuery stack.
try:  # pragma: no cover - exercised only when google libs are installed
    from google.api_core import exceptions as gexc

    _RETRYABLE_BQ: tuple = (
        gexc.ServiceUnavailable,
        gexc.InternalServerError,
        gexc.GatewayTimeout,
        gexc.DeadlineExceeded,
        gexc.RetryError,
    )
    # Only errors a regenerated SELECT could plausibly fix. Forbidden
    # (permissions/billing) is deliberately excluded — regenerating wastes the
    # budget; it surfaces as an "unexpected" error (with traceback in debug).
    _QUERY_BQ: tuple = (
        gexc.BadRequest,
        gexc.NotFound,
    )
    _QUOTA_LLM: tuple = (
        gexc.ResourceExhausted,
        gexc.TooManyRequests,
    )
    _UNAVAILABLE_LLM: tuple = (
        gexc.ServiceUnavailable,
        gexc.InternalServerError,
        gexc.GatewayTimeout,
        gexc.DeadlineExceeded,
    )
except Exception:  # pragma: no cover
    _RETRYABLE_BQ = ()
    _QUERY_BQ = ()
    _QUOTA_LLM = ()
    _UNAVAILABLE_LLM = ()


# --- Scenario messages (normal mode) ---
SQL_GEN_FAILED = "Failed to generate a query. Please rephrase your question."
NO_DATA = "No data found for your query."
SERVICE_UNAVAILABLE = "Service temporarily unavailable. Please try again later."
LLM_UNAVAILABLE = "Model temporarily unavailable (rate limit exceeded). Please try again later."
UNEXPECTED = "An unexpected error occurred. Please try again."
OTHER_INTENT = (
    "I am a retail analytics assistant. Ask about customers, products, orders, "
    "revenue, or database structure — or manage your saved reports "
    "(view, search, delete)."
)
PREVIEW_EMPTY = "No reports matched the condition."
REPORTS_GEN_FAILED = (
    "Failed to safely generate the report operation. Please rephrase your request."
)
CANCELLED = "Operation cancelled."
REGEN_NO_PREVIOUS = (
    "No previous report to revise. Please ask an analytical question first."
)
PREFS_NOT_UNDERSTOOD = (
    "Could not determine what preference to save. Please clarify the format or style "
    "you prefer (e.g. 'always send reports as CSV')."
)


# --- Classification of BigQuery exceptions ---
def is_retryable_sqlite(exc: BaseException) -> bool:
    """True for transient SQLite errors (locked database, file not ready)."""
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        return "database is locked" in msg or "unable to open database" in msg
    return False


def is_retryable_bq(exc: BaseException) -> bool:
    """5xx / timeout / connection — eligible for exponential backoff."""
    return isinstance(exc, _RETRYABLE_BQ) or isinstance(exc, (ConnectionError, TimeoutError))


def is_query_bq(exc: BaseException) -> bool:
    """4xx / syntax / bad column — fixable only by regenerating the SQL."""
    return isinstance(exc, _QUERY_BQ)


def is_quota_llm(exc: BaseException) -> bool:
    """True for Gemini rate-limit / quota / overload errors (429)."""
    if _QUOTA_LLM and isinstance(exc, _QUOTA_LLM):
        return True
    s = str(exc).lower()
    return any(
        k in s
        for k in (
            "429", "resource_exhausted", "resourceexhausted", "quota",
            "rate limit", "rate-limit", "too many requests", "overloaded",
        )
    )


def is_unavailable_llm(exc: BaseException) -> bool:
    """True for transient Gemini service errors (503/500/UNAVAILABLE/timeout).

    Distinct from quota (429): these are server-side spikes, not our quota. We
    do NOT retry them at the app level (fail-fast policy) — we just label them
    accurately as 'service temporarily unavailable'.
    """
    if _UNAVAILABLE_LLM and isinstance(exc, _UNAVAILABLE_LLM):
        return True
    name = type(exc).__name__.lower()
    if any(
        n in name
        for n in ("servererror", "serviceunavailable", "internalservererror",
                  "gatewaytimeout", "deadlineexceeded")
    ):
        return True
    s = str(exc).lower()
    return any(
        k in s
        for k in (
            "unavailable", "internal server", "high demand", "try again later",
            "overloaded", "deadline exceeded", "bad gateway", "gateway timeout",
        )
    )


def llm_error_message(exc: BaseException) -> str:
    """Pick the right scenario message for an LLM failure.

    Order matters: quota (429) first, then transient 5xx, then everything else.
      * quota/overload     -> LLM_UNAVAILABLE   (no retry — would burn quota)
      * transient 5xx/down -> SERVICE_UNAVAILABLE (no app retry — fail fast)
      * anything else       -> UNEXPECTED        (a bug/bad input; debug shows it)
    """
    if is_quota_llm(exc):
        return LLM_UNAVAILABLE
    if is_unavailable_llm(exc):
        return SERVICE_UNAVAILABLE
    return UNEXPECTED


class ServiceUnavailableError(Exception):
    """Raised after the backoff budget is exhausted (BigQuery still down)."""


class QueryError(Exception):
    """Raised for a non-retryable query/syntax error — triggers regeneration."""


def format_llm_error(exc: BaseException, debug: Optional[bool]) -> str:
    """User-facing string for a failed LLM call.

    Wraps ``llm_error_message`` and, in debug mode, appends the exception type
    and message only — not the full LangChain stack trace, which is always
    provider internals and not actionable.
    """
    user_msg = llm_error_message(exc)
    if not debug:
        return user_msg
    return f"{user_msg}\n\n--- DEBUG ---\n{type(exc).__name__}: {exc}"


def format_error(
    user_message: str,
    debug: Optional[bool],
    exc: Optional[BaseException] = None,
    extra: Optional[str] = None,
) -> str:
    """Return the user-facing string for an error.

    Normal mode: just ``user_message``.
    Debug mode: appends extra context, the traceback and a Phoenix link.
    """
    if not debug:
        return user_message
    parts = [user_message, "", "--- DEBUG ---"]
    if extra:
        parts.append(str(extra))
    if exc is not None:
        parts.append(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
        )
    parts.append("Phoenix trace: http://localhost:6006")
    return "\n".join(parts)
