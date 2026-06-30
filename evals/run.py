"""No-pytest runner: execute cases and print a compact pass/fail summary.

    python -m evals.run                 # all cases (live cases skipped without credentials)
    python -m evals.run --subset faults # only offline fault cases
    python -m evals.run --subset live   # only live cases (credentials required)
    python -m evals.run --quiet         # suppress detailed DeepEval output

Exit code is non-zero if any case FAILed or ERRORed (CI-friendly).
"""
from __future__ import annotations

import argparse
import os
import sys

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("ERROR_REPORTING", "NO")

from deepeval import assert_test  # noqa: E402
from deepeval.test_case import LLMTestCase  # noqa: E402

from evals import harness  # noqa: E402
from evals.cases import cases_for  # noqa: E402

_STATUS = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️ ", "ERROR": "💥"}


def _run_case(case) -> tuple[str, str]:
    if case.requires_creds and not harness.has_creds():
        return "SKIP", "GOOGLE_API_KEY/GCP_PROJECT not set"
    try:
        with harness.isolated_db():
            run = case.execute()
        tc = LLMTestCase(
            input=case.question,
            actual_output=run.final_message or "(empty response)",
            metadata={"run": run, "case_id": case.id},
        )
        assert_test(tc, case.build_metrics(), run_async=False)
        return "PASS", case.title
    except AssertionError as e:
        first = str(e).strip().splitlines()[0] if str(e).strip() else "metric failed"
        return "FAIL", first[:160]
    except Exception as e:  # noqa: BLE001
        return "ERROR", repr(e)[:160]


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the Manual-Test-Plan DeepEval suite")
    ap.add_argument("--subset", default="all",
                    choices=["all", "live", "acceptance", "faults", "fault", "offline"])
    ap.add_argument("--quiet", action="store_true",
                    help="suppress detailed DeepEval output per metric")
    args = ap.parse_args()

    # Opt-in Phoenix tracing (TRACING=1) — capture every case's graph spans.
    trace_url = harness.init_tracing_if_enabled()
    if trace_url:
        print(f"Phoenix tracing enabled → {trace_url}")

    if args.quiet:
        import contextlib
        import io

    cases = cases_for(args.subset)
    results = []
    for case in cases:
        if args.quiet:
            with contextlib.redirect_stdout(io.StringIO()):
                status, detail = _run_case(case)
        else:
            print(f"\n===== {case.id} · {case.title} =====")
            status, detail = _run_case(case)
        results.append((case.id, status, detail))

    print("\n" + "=" * 70)
    print("RUN SUMMARY")
    print("=" * 70)
    for cid, status, detail in results:
        print(f"{_STATUS.get(status, '?')} {status:5} {cid:4} {detail}")

    counts = {s: sum(1 for _, st, _ in results if st == s) for s in _STATUS}
    print("-" * 70)
    print(" · ".join(f"{s}: {counts[s]}" for s in ("PASS", "FAIL", "ERROR", "SKIP")))

    return 1 if (counts["FAIL"] or counts["ERROR"]) else 0


if __name__ == "__main__":
    sys.exit(main())
