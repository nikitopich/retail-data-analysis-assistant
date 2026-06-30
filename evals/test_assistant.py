"""pytest entry point — one parametrized test per Manual-Test-Plan.md case.

Run with either::

    pytest evals/test_assistant.py            # plain pytest
    deepeval test run evals/test_assistant.py # + DeepEval test-run cache

Live cases are skipped automatically when GOOGLE_API_KEY/GCP_PROJECT are unset,
so the offline fault cases still run (and pass) on a bare checkout.
"""
from __future__ import annotations

import pytest
from deepeval import assert_test
from deepeval.test_case import LLMTestCase

from evals import harness
from evals.cases import Case, all_cases

_CREDS = harness.has_creds()


@pytest.mark.parametrize("case", all_cases(), ids=lambda c: c.id)
def test_manual_plan_case(case: Case):
    if case.requires_creds and not _CREDS:
        pytest.skip("live-кейс: не заданы GOOGLE_API_KEY/GCP_PROJECT")

    with harness.isolated_db():
        run = case.execute()

    test_case = LLMTestCase(
        input=case.question,
        actual_output=run.final_message or "(пустой ответ)",
        metadata={"run": run, "case_id": case.id, "title": case.title},
    )
    assert_test(test_case, case.build_metrics(), run_async=False)
