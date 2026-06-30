"""Configuration: env reading + constants (models, limits, paths).

All tunables for the prototype live here. Env is loaded once from a local .env
(if present) via python-dotenv. Required vars are validated at CLI startup via
``validate_required`` (fail-fast), NOT at import time — so lightweight
entrypoints like ``init_db`` work without API keys.

The LLM factory (``get_llm`` / ``llm_text``) lives in ``app.llm``.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() not in ("", "0", "false", "no", "off")


# --- Required (validated at startup) ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GCP_PROJECT = os.getenv("GCP_PROJECT") or None

# Google Auth libraries look for GOOGLE_CLOUD_PROJECT; mirror our GCP_PROJECT so
# they don't emit "No project ID could be determined" warnings at import time.
if GCP_PROJECT:
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", GCP_PROJECT)


# --- Per-agent Gemini models (spec §3.2/§3.3/§3.4) ---
SUPERVISOR_MODEL = os.getenv("SUPERVISOR_MODEL", "gemini-2.5-flash-lite")
SQL_MODEL = os.getenv("SQL_MODEL", "gemini-3.5-flash")
REPORT_MODEL = os.getenv("REPORT_MODEL", "gemini-2.5-flash")


# --- Query guardrails ---
DEFAULT_LIMIT = _int_env("DEFAULT_LIMIT", 100)             # suggested LIMIT + rows-to-LLM cap
MAX_BYTES_BILLED = _int_env("MAX_BYTES_BILLED", 1073741824)  # 1 GiB cost-guard
LLM_ROWS_LIMIT = DEFAULT_LIMIT                              # rows handed to the report LLM
BQ_MAX_BYTES_BILLED = MAX_BYTES_BILLED                      # alias used by bq_client


# --- Resilience / UX budgets (spec §2.2 / §5.2) ---
RETRY_ATTEMPTS = _int_env("RETRY_ATTEMPTS", 3)   # SQL (re)generation budget
MAX_SQL_ATTEMPTS = RETRY_ATTEMPTS                # internal alias used by nodes
AFK_TIMEOUT_S = _int_env("AFK_TIMEOUT_S", 30)          # auto-cancel a pending delete confirmation
TRIO_AFK_TIMEOUT_S = _int_env("TRIO_AFK_TIMEOUT_S", 300)  # idle after report → implicit trio approval
# Backoff is an independent budget (not env-exposed) — keeps the two-budget design.
MAX_BACKOFF_RETRIES = 5     # against BigQuery unavailability
BACKOFF_BASE_SECONDS = 1    # 1 -> 2 -> 4 -> 8 -> 16
BACKOFF_MAX_SECONDS = 16
EMPTY_RECHECK_LIMIT = 1     # single filter-revision pass on empty result

# Gemini calls: fail fast. The genai SDK otherwise retries 5xx/429 up to 5x with
# backoff (up to ~4 min) — we disable that (attempts=1) and surface a scenario
# message immediately. LLM_TIMEOUT_S bounds a single hung request.
LLM_MAX_RETRIES = 1
LLM_TIMEOUT_S = _int_env("LLM_TIMEOUT_S", 60)


# --- Local store / identity ---
DB_PATH = os.getenv("DB_PATH", "agentic.db")
CHECKPOINTS_PATH = os.getenv("CHECKPOINTS_PATH", "checkpoints.db")  # internal default
CURRENT_USER_ID = os.getenv("CURRENT_USER", "default_user")


# --- BigQuery dataset (connection target) ---
# Only the dataset is configured; the table list and per-table schema are read
# live from BigQuery (see app/nodes/sql_agent.py), never hardcoded here.
BQ_DATASET = "bigquery-public-data.thelook_ecommerce"


# --- Observability: Phoenix tracing (opt-in; same effect as --trace) ---
TRACING = _truthy(os.getenv("TRACING"))
PHOENIX_COLLECTOR_ENDPOINT = os.getenv("PHOENIX_COLLECTOR_ENDPOINT") or None


# --- Debug error verbosity (spec §8/§13.7; same effect as --debug) ---
DEBUG = _truthy(os.getenv("DEBUG", "0"))


class ConfigError(RuntimeError):
    """Raised at startup when required configuration is missing."""


def validate_required() -> None:
    """Fail fast if any required env var is missing (called from cli.main)."""
    missing = [
        name
        for name, value in (("GOOGLE_API_KEY", GOOGLE_API_KEY), ("GCP_PROJECT", GCP_PROJECT))
        if not value
    ]
    if missing:
        raise ConfigError(
            "Required environment variables are not set: "
            + ", ".join(missing)
            + ". Please configure them in .env (see .env.example)."
        )
