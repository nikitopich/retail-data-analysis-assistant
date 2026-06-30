"""Reusable retry/backoff decorators (spec §2.2, §5.2).

Extracted from the SQL agent's inline backoff loop so the same exponential-
backoff policy can be reused and unit-tested in isolation.
"""
from __future__ import annotations

import functools
import time
from typing import Callable, Optional


def retry_with_backoff(
    *,
    retry_on: Callable[[BaseException], bool],
    max_retries: int,
    base_seconds: float = 1.0,
    max_seconds: float = 16.0,
    on_exhausted: Optional[Callable[[BaseException], BaseException]] = None,
    sleep: Callable[[float], None] = time.sleep,
):
    """Retry the wrapped call with exponential backoff on transient failures.

    Retries while ``retry_on(exc)`` is true, sleeping ``base, 2*base, 4*base ...``
    capped at ``max_seconds``, for up to ``max_retries`` retries. Exceptions for
    which ``retry_on`` is false propagate immediately. When the budget is
    exhausted the last exception is re-raised — wrapped via ``on_exhausted`` if
    provided.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            delay = base_seconds
            tries = 0
            while True:
                try:
                    return fn(*args, **kwargs)
                except BaseException as exc:
                    if not retry_on(exc):
                        raise
                    if tries >= max_retries:
                        if on_exhausted is not None:
                            raise on_exhausted(exc) from exc
                        raise
                    sleep(min(delay, max_seconds))
                    delay *= 2
                    tries += 1
        return wrapper
    return decorator