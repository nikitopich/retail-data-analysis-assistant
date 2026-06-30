"""Retry + exponential-backoff decorator (app/retry.py).

Tests every branch and the exact backoff schedule. The ``sleep`` parameter
lets us inject a recorder instead of real time.sleep.
"""
from __future__ import annotations

import pytest

from app import config
from app.retry import retry_with_backoff


def _make_retried(*, max_retries, sleeps, results, base=1.0, max_s=16.0,
                  on_exhausted=None, retry_on=None):
    if retry_on is None:
        retry_on = lambda e: isinstance(e, RuntimeError)

    @retry_with_backoff(
        retry_on=retry_on,
        max_retries=max_retries,
        base_seconds=base,
        max_seconds=max_s,
        on_exhausted=on_exhausted,
        sleep=sleeps.append,
    )
    def fn():
        item = results.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return fn


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_success_first_try_no_sleep():
    sleeps = []
    fn = _make_retried(max_retries=3, sleeps=sleeps, results=[42])
    assert fn() == 42
    assert sleeps == []


def test_one_transient_then_success():
    sleeps = []
    fn = _make_retried(max_retries=3, sleeps=sleeps,
                       results=[RuntimeError("down"), 7])
    assert fn() == 7
    assert sleeps == [1.0]


# --------------------------------------------------------------------------- #
# Backoff schedule + exhaustion
# --------------------------------------------------------------------------- #
def test_exhausts_budget_reraises_last():
    sleeps = []
    fn = _make_retried(max_retries=3, sleeps=sleeps,
                       results=[RuntimeError("x")] * 10)
    with pytest.raises(RuntimeError, match="x"):
        fn()
    assert sleeps == [1.0, 2.0, 4.0]


def test_exhausts_budget_calls_on_exhausted():
    class Wrapped(Exception):
        pass

    sleeps = []
    fn = _make_retried(max_retries=2, sleeps=sleeps,
                       results=[RuntimeError("raw")] * 10,
                       on_exhausted=lambda e: Wrapped(str(e)))
    with pytest.raises(Wrapped):
        fn()
    assert sleeps == [1.0, 2.0]


def test_backoff_capped_at_max_seconds():
    sleeps = []
    fn = _make_retried(max_retries=6, sleeps=sleeps, max_s=4.0,
                       results=[RuntimeError()] * 20)
    with pytest.raises(RuntimeError):
        fn()
    assert sleeps == [1.0, 2.0, 4.0, 4.0, 4.0, 4.0]
    assert all(d <= 4.0 for d in sleeps)


def test_recovers_on_last_allowed_attempt():
    sleeps = []
    max_r = 3
    fn = _make_retried(max_retries=max_r, sleeps=sleeps,
                       results=[RuntimeError()] * max_r + [99])
    assert fn() == 99
    assert len(sleeps) == max_r


# --------------------------------------------------------------------------- #
# Non-retryable exceptions pass through immediately
# --------------------------------------------------------------------------- #
def test_non_retryable_raises_immediately():
    sleeps = []
    fn = _make_retried(max_retries=5, sleeps=sleeps,
                       retry_on=lambda e: isinstance(e, RuntimeError),
                       results=[ValueError("not retryable")])
    with pytest.raises(ValueError):
        fn()
    assert sleeps == []


def test_wrapped_function_name_preserved():
    @retry_with_backoff(retry_on=lambda e: False, max_retries=1)
    def my_func():
        pass

    assert my_func.__name__ == "my_func"
