"""Error taxonomy + scenario-message mapping (spec §5.2, §8).

The classifiers here decide whether a failure is retried (backoff), regenerated,
or surfaced — so they are part of the resilience contract.
"""
from __future__ import annotations

import pytest
from google.api_core import exceptions as gexc

from app import errors


# --------------------------------------------------------------------------- #
# BigQuery classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("exc", [
    gexc.ServiceUnavailable("x"),
    gexc.InternalServerError("x"),
    gexc.GatewayTimeout("x"),
    gexc.DeadlineExceeded("x"),
    ConnectionError("x"),
    TimeoutError("x"),
])
def test_retryable_bq_true(exc):
    assert errors.is_retryable_bq(exc)


@pytest.mark.parametrize("exc", [
    gexc.BadRequest("x"),
    gexc.NotFound("x"),
    gexc.Forbidden("x"),
    ValueError("x"),
])
def test_retryable_bq_false(exc):
    assert not errors.is_retryable_bq(exc)


@pytest.mark.parametrize("exc", [gexc.BadRequest("x"), gexc.NotFound("x")])
def test_query_bq_true(exc):
    assert errors.is_query_bq(exc)


@pytest.mark.parametrize("exc", [
    gexc.ServiceUnavailable("x"), gexc.Forbidden("x"), ValueError("x"),
])
def test_query_bq_false(exc):
    assert not errors.is_query_bq(exc)


# --------------------------------------------------------------------------- #
# LLM classification — quota vs transient
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("exc", [
    gexc.ResourceExhausted("x"),
    gexc.TooManyRequests("x"),
    RuntimeError("Error 429: RESOURCE_EXHAUSTED"),
    RuntimeError("quota exceeded"),
    RuntimeError("rate limit reached"),
    RuntimeError("The model is overloaded"),
])
def test_quota_llm_true(exc):
    assert errors.is_quota_llm(exc)


@pytest.mark.parametrize("exc", [
    RuntimeError("bad input"),
    ValueError("nothing relevant"),
])
def test_quota_llm_false(exc):
    assert not errors.is_quota_llm(exc)


@pytest.mark.parametrize("exc", [
    gexc.ServiceUnavailable("x"),
    gexc.GatewayTimeout("x"),
    RuntimeError("Service is temporarily unavailable"),
    RuntimeError("deadline exceeded"),
    RuntimeError("bad gateway"),
])
def test_unavailable_llm_true(exc):
    assert errors.is_unavailable_llm(exc)


def test_unavailable_llm_false_for_pure_quota():
    # A plain 429 with no transient wording is quota, NOT 'unavailable'.
    assert not errors.is_unavailable_llm(RuntimeError("429"))


def test_unavailable_llm_matches_by_type_name():
    class GatewayTimeoutError(Exception):
        pass
    assert errors.is_unavailable_llm(GatewayTimeoutError("boom"))


# --------------------------------------------------------------------------- #
# llm_error_message — ordering matters
# --------------------------------------------------------------------------- #
def test_message_quota_takes_priority():
    assert errors.llm_error_message(gexc.ResourceExhausted("x")) == errors.LLM_UNAVAILABLE


def test_message_transient_is_service_unavailable():
    assert errors.llm_error_message(gexc.ServiceUnavailable("x")) == errors.SERVICE_UNAVAILABLE


def test_message_other_is_unexpected():
    assert errors.llm_error_message(ValueError("weird")) == errors.UNEXPECTED


# --------------------------------------------------------------------------- #
# format_error — normal vs debug
# --------------------------------------------------------------------------- #
def test_format_error_normal_returns_message_only():
    out = errors.format_error("oops", debug=False, exc=ValueError("boom"), extra="ctx")
    assert out == "oops"


def test_format_error_debug_includes_traceback_and_link():
    try:
        raise ValueError("boom")
    except ValueError as e:
        out = errors.format_error("oops", debug=True, exc=e, extra="ctx")
    assert "oops" in out
    assert "--- DEBUG ---" in out
    assert "ctx" in out
    assert "ValueError: boom" in out
    assert "Phoenix trace" in out


def test_format_error_debug_without_exc():
    out = errors.format_error("oops", debug=True)
    assert "--- DEBUG ---" in out
    assert "Phoenix trace" in out
