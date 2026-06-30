"""Error taxonomy + scenario user-messages + debug formatting (spec §5.2, §8).

In normal mode the user only ever sees the scenario strings below. In debug
mode ``format_error`` appends the traceback, any extra context, and a Phoenix
trace link.
"""
from __future__ import annotations

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
except Exception:  # pragma: no cover
    _RETRYABLE_BQ = ()
    _QUERY_BQ = ()


# --- Scenario messages (normal mode) ---
SQL_GEN_FAILED = "Не удалось сформировать запрос, переформулируйте вопрос."
NO_DATA = "По вашему запросу данных нет."
SERVICE_UNAVAILABLE = "Сервис временно недоступен, попробуйте позже."
LLM_UNAVAILABLE = "Модель временно недоступна (превышен лимит запросов). Попробуйте позже."
UNEXPECTED = "Произошла непредвиденная ошибка. Попробуйте ещё раз."
OTHER_INTENT = (
    "Я ассистент по аналитике ритейла. Спросите про клиентов, товары, заказы, "
    "выручку или структуру базы данных — либо попросите удалить сохранённые отчёты."
)
PREVIEW_EMPTY = "Под условие не попал ни один отчёт."
DESTRUCTIVE_GEN_FAILED = (
    "Не удалось безопасно сформировать операцию над отчётами. Переформулируйте запрос."
)
CANCELLED = "Операция отменена."


# --- Classification of BigQuery exceptions ---
def is_retryable_bq(exc: BaseException) -> bool:
    """5xx / timeout / connection — eligible for exponential backoff."""
    return isinstance(exc, _RETRYABLE_BQ) or isinstance(exc, (ConnectionError, TimeoutError))


def is_query_bq(exc: BaseException) -> bool:
    """4xx / syntax / bad column — fixable only by regenerating the SQL."""
    return isinstance(exc, _QUERY_BQ)


class ServiceUnavailableError(Exception):
    """Raised after the backoff budget is exhausted (BigQuery still down)."""


class QueryError(Exception):
    """Raised for a non-retryable query/syntax error — triggers regeneration."""


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
