"""Shared fixtures + test doubles for the deterministic unit suite.

Tests cover non-LLM, non-network code paths: error taxonomy, retry/backoff,
SQL guards, repositories, config helpers, graph routers, and agent control
flow with LLM and BigQuery replaced by in-memory fakes. LLM-judge cases live
under ``evals/``.

Nothing here touches Google APIs, real BigQuery, or sleeps for real time.
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from app.sources.db import DDL


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class _Resp:
    """Mimics a LangChain message: ``llm_text`` reads ``.content``."""
    def __init__(self, content):
        self.content = content


class FakeLLM:
    """Deterministic stand-in for a Gemini chat model.

    ``responses`` is consumed one item per ``invoke`` call. Each item may be:
      * a ``str``           -> wrapped in a response with that ``.content``
      * a ``_Resp``/object  -> returned as-is
      * a ``BaseException`` -> raised (simulate an API error)
      * a callable          -> called with the prompt, its result returned
    Once exhausted, the last behaviour repeats (an empty string by default).
    """
    def __init__(self, responses=None, repeat_last=True):
        self._responses = list(responses or [])
        self.repeat_last = repeat_last
        self._last = ""
        self.calls = []

    def invoke(self, prompt):
        self.calls.append(prompt)
        if self._responses:
            item = self._responses.pop(0)
            self._last = item
        else:
            item = self._last if self.repeat_last else ""
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(prompt)
        if isinstance(item, _Resp):
            return item
        return _Resp(item)

    def bind_tools(self, tools):
        return self


_DEFAULT_SCHEMA = [
    {"name": "id", "type": "INTEGER", "mode": "REQUIRED", "description": ""},
    {"name": "sale_price", "type": "FLOAT", "mode": "NULLABLE", "description": ""},
]

_DEFAULT_TABLES = ["orders", "order_items", "products", "users"]


class FakeRunner:
    """In-memory stand-in for ``BigQueryRunner``.

    ``results`` is consumed one item per ``execute_query`` call. Each item may be
    a ``DataFrame``, a ``BaseException`` (raised), or a callable(sql)->df. Once
    exhausted, an empty DataFrame is returned.
    """
    def __init__(self, results=None, schema=None, schema_error=None,
                 tables=None, tables_error=None):
        self._results = list(results or [])
        self.schema = schema if schema is not None else _DEFAULT_SCHEMA
        self.schema_error = schema_error
        self.tables = list(tables) if tables is not None else list(_DEFAULT_TABLES)
        self.tables_error = tables_error
        self.queries = []
        self.schema_calls = []
        self.tables_calls = 0

    def list_tables(self):
        self.tables_calls += 1
        if self.tables_error is not None:
            raise self.tables_error
        return self.tables

    def execute_query(self, sql):
        self.queries.append(sql)
        if not self._results:
            return pd.DataFrame()
        item = self._results.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(sql)
        return item

    def get_table_schema(self, table_name):
        self.schema_calls.append(table_name)
        if self.schema_error is not None:
            raise self.schema_error
        return self.schema


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_llm_factory(monkeypatch):
    """Return a setter: ``set(module, FakeLLM([...]))`` installs it on ``module.get_llm``."""
    def _set(module, llm):
        monkeypatch.setattr(module, "get_llm", lambda *a, **k: llm)
        return llm
    return _set


@pytest.fixture
def conn():
    """A fresh in-memory SQLite DB with the full schema applied."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(DDL)
    c.commit()
    yield c
    c.close()


@pytest.fixture
def patched_conn(conn, monkeypatch):
    """Route repos' ``get_connection()`` calls to ``conn``."""
    monkeypatch.setattr("app.sources.reports_repo.get_connection", lambda: conn)
    monkeypatch.setattr("app.sources.prefs_repo.get_connection", lambda: conn)
    return conn
