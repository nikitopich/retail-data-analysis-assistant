"""Configuration: env reading + constants (models, limits, paths).

All tunables for the prototype live here. Env is loaded once from a local .env
(if present) via python-dotenv.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


# --- Gemini models (one per role, per spec §3.2/§3.3/§3.4) ---
SUPERVISOR_MODEL = "gemini-2.5-flash-lite"   # fast intent classification
SQL_MODEL = "gemini-2.5-flash"               # SQL generation / NL->DML
REPORT_MODEL = "gemini-2.5-flash"            # report writing


# --- Resilience budgets (spec §2.2 / §5.2) ---
MAX_SQL_ATTEMPTS = 3        # SQL (re)generation budget against bad/guard-rejected SQL
MAX_BACKOFF_RETRIES = 5     # backoff budget against BigQuery unavailability
BACKOFF_BASE_SECONDS = 1    # 1 -> 2 -> 4 -> 8 -> 16
BACKOFF_MAX_SECONDS = 16
EMPTY_RECHECK_LIMIT = 1     # single filter-revision pass on empty result
LLM_ROWS_LIMIT = 100        # max rows handed to the report LLM


# --- Paths / identity ---
DB_PATH = os.getenv("DB_PATH", "./prototype.db")
CHECKPOINTS_PATH = os.getenv("CHECKPOINTS_PATH", "./checkpoints.db")
CURRENT_USER_ID = os.getenv("CURRENT_USER_ID", "demo-user")


# --- BigQuery ---
GCP_PROJECT = os.getenv("GCP_PROJECT") or None
BQ_DATASET = "bigquery-public-data.thelook_ecommerce"
BQ_TABLES = ["orders", "order_items", "products", "users"]


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


BQ_MAX_BYTES_BILLED = _int_env("BQ_MAX_BYTES_BILLED", 1_000_000_000)  # ~1 GB cost-guard


# --- Gemini API key ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() not in ("", "0", "false", "no", "off")


# --- Debug ---
DEBUG = _truthy(os.getenv("DEBUG", "0"))


def get_llm(model: str, temperature: float = 0.0):
    """Factory for a Gemini chat model via langchain-google-genai.

    Imported lazily so that lightweight entrypoints (e.g. ``init_db``) do not
    require the LLM stack to be importable.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=model,
        temperature=temperature,
        google_api_key=GOOGLE_API_KEY,
    )
