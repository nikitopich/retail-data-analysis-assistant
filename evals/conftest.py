"""pytest config for the evals suite.

Keeps DeepEval fully local (no telemetry, no Confident-AI push required) so the
suite runs anywhere with just the app's own credentials.
"""
import os

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("ERROR_REPORTING", "NO")


def pytest_configure(config):
    """Enable Phoenix tracing for the eval run when TRACING=1 (opt-in, same as CLI).

    Instrument once, before any case builds/invokes the graph, so every drive()
    LLM/tool span is captured. No-op (and zero overhead) when TRACING is unset.
    """
    from evals import harness

    url = harness.init_tracing_if_enabled()
    if url:
        print(f"\nPhoenix tracing enabled → {url}")
