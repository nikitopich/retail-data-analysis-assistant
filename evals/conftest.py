"""pytest config for the evals suite.

Keeps DeepEval fully local (no telemetry, no Confident-AI push required) so the
suite runs anywhere with just the app's own credentials.
"""
import os

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("ERROR_REPORTING", "NO")
