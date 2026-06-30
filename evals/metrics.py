"""DeepEval metrics for the test-plan suite.

Two families:

* **Deterministic** ``BaseMetric`` subclasses read the structured
  :class:`~evals.harness.RunResult` off ``test_case.metadata['run']`` and assert
  on graph state (intent, preview rows, owner-scoping, resilience counters). No
  LLM, no network — these are the bulk and they pin down the requirements.
* **LLM-judge** ``GEval`` factories cover the soft properties that genuinely
  need a model: answer language and analytical relevance. They use Gemini as the
  judge (same key as the app) so the suite needs no OpenAI credentials.
"""
from __future__ import annotations

import os
from typing import List, Optional

from deepeval.metrics import BaseMetric, GEval

try:  # deepeval >= 4: SingleTurnParams; older: LLMTestCaseParams
    from deepeval.test_case import SingleTurnParams as _Params
except ImportError:  # pragma: no cover
    from deepeval.test_case import LLMTestCaseParams as _Params

from app import config, errors
from evals.harness import RunResult


# --------------------------------------------------------------------------- #
# Deterministic base
# --------------------------------------------------------------------------- #
class _DeterministicMetric(BaseMetric):
    """Base for non-LLM checks. Subclasses implement :meth:`_check`."""

    _label = "Check"

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self.async_mode = False
        self.include_reason = True
        self.strict_mode = False
        self.evaluation_model = None
        self.evaluation_cost = None
        self.error = None
        self.skipped = False
        self.score = 0.0
        self.success = False
        self.reason = ""

    # subclasses return (passed: bool, reason: str)
    def _check(self, run: RunResult):  # pragma: no cover - abstract
        raise NotImplementedError

    def measure(self, test_case, *args, **kwargs) -> float:
        run: RunResult = (test_case.metadata or {}).get("run")
        if run is None:
            self.error = "no RunResult in test_case.metadata['run']"
            self.success = False
            self.score = 0.0
            self.reason = self.error
            return self.score
        passed, reason = self._check(run)
        self.success = bool(passed)
        self.score = 1.0 if self.success else 0.0
        self.reason = reason
        return self.score

    async def a_measure(self, test_case, *args, **kwargs) -> float:
        return self.measure(test_case, *args, **kwargs)

    def is_successful(self) -> bool:
        return self.success

    @property
    def __name__(self):  # deepeval reads this for display
        return self._label


def _contains(haystack: str, needle: str) -> bool:
    return needle.lower() in (haystack or "").lower()


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
class IntentMetric(_DeterministicMetric):
    """Match the expected routing label.

    The refactor split routing into two fields: the supervisor's flow ``intent``
    (``query``/``destructive``/``regenerate``/``other``) and the read's
    ``data_source`` (``analytical``/``schema``/``reports``), chosen downstream by
    the SQL agent. Accept a hit on either, so legacy expectations like
    ``"analytical"`` keep working alongside ``"query"``/``"destructive"``.
    """

    def __init__(self, expected: str):
        super().__init__()
        self.expected = expected
        self._label = f"Intent == {expected}"

    def _check(self, run):
        ok = self.expected in (run.intent, run.data_source)
        return ok, f"intent={run.intent!r}, data_source={run.data_source!r} (expected {self.expected!r})"


class RoutedToOtherMetric(_DeterministicMetric):
    """intent=other AND BigQuery was never touched (no SQL agent trace)."""

    _label = "Routed to 'other' (no SQL)"

    def _check(self, run):
        ok = run.intent == "other" and not run.touched_bigquery
        return ok, f"intent={run.intent!r}, touched_bigquery={run.touched_bigquery}"


# --------------------------------------------------------------------------- #
# Analytical shape
# --------------------------------------------------------------------------- #
class RowCountMetric(_DeterministicMetric):
    def __init__(self, equals: Optional[int] = None,
                 minimum: Optional[int] = None, maximum: Optional[int] = None):
        super().__init__()
        self.equals, self.minimum, self.maximum = equals, minimum, maximum
        self._label = "Row count"

    def _check(self, run):
        n = run.df_row_count
        if n is None:
            return False, "df_row_count is absent (analytical path did not execute)"
        if self.equals is not None and n != self.equals:
            return False, f"df_row_count={n}, expected exactly {self.equals}"
        if self.minimum is not None and n < self.minimum:
            return False, f"df_row_count={n} < min {self.minimum}"
        if self.maximum is not None and n > self.maximum:
            return False, f"df_row_count={n} > max {self.maximum}"
        return True, f"df_row_count={n}"


class ContainsMetric(_DeterministicMetric):
    """All/any of ``needles`` appear in the final message or rows markdown."""

    def __init__(self, needles: List[str], source: str = "final",
                 mode: str = "all", label: Optional[str] = None):
        super().__init__()
        self.needles, self.source, self.mode = needles, source, mode
        self._label = label or f"Contains {mode} {needles} in {source}"

    def _check(self, run):
        text = run.rows_markdown if self.source == "rows" else run.final_message
        hits = {n: _contains(text, n) for n in self.needles}
        if self.mode == "any":
            ok = any(hits.values())
        else:
            ok = all(hits.values())
        missing = [n for n, h in hits.items() if not h]
        return ok, f"missing={missing}" if missing else "all present"


class ScenarioMessageMetric(_DeterministicMetric):
    """Final message equals (or contains) a fixed scenario string."""

    def __init__(self, expected: str, mode: str = "exact", label: Optional[str] = None):
        super().__init__()
        self.expected, self.mode = expected, mode
        self._label = label or "Scenario message"

    def _check(self, run):
        msg = (run.final_message or "").strip()
        if self.mode == "contains":
            ok = _contains(msg, self.expected)
        else:
            ok = msg == self.expected.strip()
        return ok, f"final_message={msg!r}"


# --------------------------------------------------------------------------- #
# High-Stakes Oversight (destructive flow)
# --------------------------------------------------------------------------- #
class PreviewBeforeDeleteMetric(_DeterministicMetric):
    """A confirmation interrupt fired, the preview held N rows, and the run
    finished with a delete/update confirmation of those rows."""

    def __init__(self, expected_count: int, verb: str = "Deleted"):
        super().__init__()
        self.expected_count, self.verb = expected_count, verb
        self._label = f"Preview→confirm→{verb} ({expected_count})"

    def _check(self, run):
        if not run.interrupted:
            return False, "confirmation was not requested (interrupt did not fire)"
        n = len(run.preview_rows)
        if n != self.expected_count:
            return False, f"preview has {n} rows, expected {self.expected_count}"
        if self.verb not in run.final_message:
            return False, f"final message missing '{self.verb}': {run.final_message!r}"
        return True, f"preview={n}, result={run.final_message!r}"


class CancelledMetric(_DeterministicMetric):
    """User declined: nothing deleted, library size unchanged."""

    def __init__(self, expected_remaining: int):
        super().__init__()
        self.expected_remaining = expected_remaining
        self._label = "Cancelled — nothing deleted"

    def _check(self, run):
        if not run.interrupted:
            return False, "confirmation was not requested"
        if run.final_message.strip() != errors.CANCELLED:
            return False, f"expected '{errors.CANCELLED}', got {run.final_message!r}"
        remaining = len(run.saved_reports)
        if remaining != self.expected_remaining:
            return False, f"library has {remaining}, expected {self.expected_remaining}"
        return True, f"cancelled, library has {remaining}"


class EmptyPreviewMetric(_DeterministicMetric):
    """Empty preview short-circuits BEFORE any confirmation prompt."""

    _label = "Empty preview — no confirm"

    def _check(self, run):
        if run.interrupted:
            return False, "confirmation was requested on empty preview"
        if run.final_message.strip() != errors.PREVIEW_EMPTY:
            return False, f"expected '{errors.PREVIEW_EMPTY}', got {run.final_message!r}"
        return True, "empty preview, no confirmation requested"


class DeletedSurvivorsMetric(_DeterministicMetric):
    """After a targeted delete: no surviving report matches ``deleted_substr``,
    while every ``survivor`` substring is still present in the library."""

    def __init__(self, deleted_substr: str, survivors: List[str]):
        super().__init__()
        self.deleted_substr, self.survivors = deleted_substr, survivors
        self._label = "Deleted target only"

    def _check(self, run):
        qs = run.questions()
        still_there = [q for q in qs if _contains(q, self.deleted_substr)]
        if still_there:
            return False, f"target report was not deleted: {still_there}"
        missing = [s for s in self.survivors if not any(_contains(q, s) for q in qs)]
        if missing:
            return False, f"extra reports were affected, missing: {missing}"
        return True, f"remaining: {qs}"


class OwnerScopeMetric(_DeterministicMetric):
    """A foreign-owned report survives a delete-all, and only the current
    user's rows are gone."""

    def __init__(self, foreign_owner: str):
        super().__init__()
        self.foreign_owner = foreign_owner
        self._label = "Owner-scoped delete"

    def _check(self, run):
        owners = run.owners()
        if self.foreign_owner not in owners:
            return False, f"foreign report ({self.foreign_owner}) was deleted!"
        own_left = [o for o in owners if o == config.CURRENT_USER_ID]
        if own_left:
            return False, f"own reports that should have been deleted remain: {len(own_left)}"
        return True, f"foreign report intact, own deleted (owners={owners})"


# --------------------------------------------------------------------------- #
# Resilience
# --------------------------------------------------------------------------- #
class NoCrashMetric(_DeterministicMetric):
    """The graph returned a user-facing message instead of propagating."""

    _label = "No crash — graceful message"

    def _check(self, run):
        if run.raised is not None:
            return False, f"exception not caught: {run.raised!r}"
        if not run.final_message.strip():
            return False, "empty final message"
        return True, "error handled, scenario message present"


class MaxCallsMetric(_DeterministicMetric):
    """A telemetry counter stays within budget (e.g. no retries on 429)."""

    def __init__(self, counter_key: str, maximum: int, label: Optional[str] = None):
        super().__init__()
        self.counter_key, self.maximum = counter_key, maximum
        self._label = label or f"{counter_key} <= {maximum}"

    def _check(self, run):
        got = run.counters.get(self.counter_key)
        if got is None:
            return False, f"counter {self.counter_key!r} not recorded"
        return got <= self.maximum, f"{self.counter_key}={got} (budget {self.maximum})"


class BackoffSequenceMetric(_DeterministicMetric):
    """Backoff delays grew exactly as configured before giving up."""

    def __init__(self, expected_delays: List[float]):
        super().__init__()
        self.expected_delays = expected_delays
        self._label = "Exponential backoff sequence"

    def _check(self, run):
        sleeps = run.counters.get("sleeps")
        if sleeps is None:
            return False, "backoff delays not recorded"
        if sleeps != self.expected_delays:
            return False, f"delays {sleeps}, expected {self.expected_delays}"
        return True, f"backoff={sleeps}"


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
class ReportSavedMetric(_DeterministicMetric):
    """The just-asked question was persisted to the library under the current user."""

    _label = "Report saved to library"

    def _check(self, run):
        for r in run.saved_reports:
            if r.get("owner_id") == config.CURRENT_USER_ID and r.get("question") == run.question:
                return True, f"report saved id={r.get('id')}"
        return False, "report not found in saved_reports for the current user"


class PrefsSavedMetric(_DeterministicMetric):
    """A user preference was persisted to ``user_prefs``.

    Reads a snapshot the case captured into ``counters['prefs']`` (taken while the
    isolated library DB was still active), so the check never touches the real DB
    at metric time. Each ``*_contains`` is a case-insensitive substring; use
    ``format_equals`` to pin the exact (scripted) format value.
    """

    def __init__(self, format_contains: Optional[str] = None,
                 format_equals: Optional[str] = None,
                 tone_contains: Optional[str] = None,
                 extra_contains: Optional[str] = None,
                 label: Optional[str] = None):
        super().__init__()
        self.format_contains = format_contains
        self.format_equals = format_equals
        self.tone_contains = tone_contains
        self.extra_contains = extra_contains
        self._label = label or "Preference persisted to user_prefs"

    def _check(self, run):
        prefs = run.counters.get("prefs")
        if not prefs:
            return False, "user_prefs snapshot not recorded (counters['prefs'])"
        fmt = prefs.get("output_format") or ""
        tone = prefs.get("tone_preference") or ""
        extra = prefs.get("extra_prefs") or ""
        if self.format_equals is not None and fmt != self.format_equals:
            return False, f"output_format={fmt!r}, expected exactly {self.format_equals!r}"
        if self.format_contains is not None and not _contains(fmt, self.format_contains):
            return False, f"output_format={fmt!r} missing {self.format_contains!r}"
        if self.tone_contains is not None and not _contains(tone, self.tone_contains):
            return False, f"tone_preference={tone!r} missing {self.tone_contains!r}"
        if self.extra_contains is not None and not _contains(extra, self.extra_contains):
            return False, f"extra_prefs={extra!r} missing {self.extra_contains!r}"
        return True, f"prefs={prefs}"


class LibrarySizeMetric(_DeterministicMetric):
    """The library holds exactly ``expected`` reports after the run."""

    def __init__(self, expected: int):
        super().__init__()
        self.expected = expected
        self._label = f"Library size == {expected}"

    def _check(self, run):
        n = len(run.saved_reports)
        return n == self.expected, f"library has {n}, expected {self.expected}"


class SqlAttemptsMetric(_DeterministicMetric):
    """The SQL (re)generation budget was respected (anti-cost-inflation)."""

    def __init__(self, maximum: int):
        super().__init__()
        self.maximum = maximum
        self._label = f"SQL attempts <= {maximum}"

    def _check(self, run):
        attempts = run.state.get("sql_attempts")
        if attempts is None:
            return False, "sql_attempts not recorded"
        return attempts <= self.maximum, f"sql_attempts={attempts} (budget {self.maximum})"


class DmlSafeMetric(_DeterministicMetric):
    """The DML the graph actually accepted passes the deterministic DML-guard
    (single statement, DELETE/UPDATE on saved_reports, no DDL)."""

    _label = "Accepted DML is guard-safe"

    def _check(self, run):
        from app.tools.sql_tools import dml_guard

        dml = run.dml_sql
        if not dml:
            return False, "dml_sql is empty (generation did not reach safe DML)"
        ok, reason = dml_guard(dml)
        return ok, f"dml={dml!r}" if ok else f"guard would reject: {reason} ({dml!r})"


# --------------------------------------------------------------------------- #
# LLM-judge metrics (Gemini)
# --------------------------------------------------------------------------- #
_judge = None


def judge_model():
    """Lazily build a Gemini judge from the app's GOOGLE_API_KEY."""
    global _judge
    if _judge is None:
        from deepeval.models import GeminiModel

        _judge = GeminiModel(
            model=os.getenv("JUDGE_MODEL", "gemini-2.5-flash"),
            api_key=config.GOOGLE_API_KEY,
            temperature=0,
        )
    return _judge


def language_match_metric(threshold: float = 0.7) -> GEval:
    return GEval(
        name="Language Match",
        evaluation_steps=[
            "Identify the natural language of the user's input question.",
            "Identify the natural language of the actual output answer.",
            "Score 1.0 only if the answer is written in the same language as the question.",
            "Ignore SQL keywords, table/column names and numbers when judging language.",
        ],
        evaluation_params=[_Params.INPUT, _Params.ACTUAL_OUTPUT],
        model=judge_model(),
        threshold=threshold,
        async_mode=False,
    )


def analytical_relevance_metric(threshold: float = 0.6) -> GEval:
    return GEval(
        name="Analytical Relevance",
        evaluation_steps=[
            "Check the actual output directly answers the analytical question asked.",
            "Check the answer presents concrete figures, ideally as a markdown table.",
            "Penalize refusals, apologies, or 'no data' responses when the question is answerable.",
            "Do not penalize reasonable wording or formatting differences.",
        ],
        evaluation_params=[_Params.INPUT, _Params.ACTUAL_OUTPUT],
        model=judge_model(),
        threshold=threshold,
        async_mode=False,
    )
