"""DeepEval-based automation of the Manual Test Plan.

The package turns the cases from ``Manual-Test-Plan.md`` into a runnable
DeepEval suite:

* ``harness``  — drives the real LangGraph app in an isolated SQLite library and
  captures a structured :class:`RunResult`; also provides fault-injection helpers
  for the offline ``[sim]`` cases.
* ``metrics``  — deterministic ``BaseMetric`` checks (intent / preview-flow /
  owner-scoping / resilience counters) plus ``GEval`` LLM-judge metrics for the
  soft properties (answer language, analytical relevance).
* ``cases``    — the mapping of test-plan rows to (question, setup, metrics).
* ``test_assistant`` — pytest entry point (``deepeval test run`` compatible).
* ``run``      — a no-pytest runner that prints a pass/fail summary.
"""
