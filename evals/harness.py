"""Test harness: drive the real agent graph and capture a structured result.

Two responsibilities:

1. **Live driving** — :func:`drive` runs a question through the *real* compiled
   graph (real Gemini + BigQuery), including the destructive interrupt/resume
   loop, against an *isolated* SQLite library so destructive cases are
   reproducible and never touch the developer's ``agentic.db``.

2. **Fault injection** — context managers (:func:`fake_llms`, :func:`fake_bq`,
   :func:`no_sleep`) replace the LLM / BigQuery boundary with deterministic
   doubles so the ``[sim]`` resilience cases (D4/D5/D6) run offline, for free,
   and without flakiness.
"""
from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from langgraph.types import Command

from app import config
from app.graph.build import build_graph

try:  # langgraph >= 0.2
    from langgraph.checkpoint.memory import InMemorySaver as _MemorySaver
except ImportError:  # pragma: no cover - older langgraph
    from langgraph.checkpoint.memory import MemorySaver as _MemorySaver


# --------------------------------------------------------------------------- #
# Result object
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    """Everything a metric might want to assert on for a single graph run."""

    question: str
    state: Dict[str, Any]
    interrupted: bool
    raised: Optional[BaseException]
    saved_reports: List[dict]                 # snapshot of the library AFTER the run
    counters: Dict[str, Any] = field(default_factory=dict)  # fault-mode telemetry
    interrupt_value: Dict[str, Any] = field(default_factory=dict)  # destructive interrupt payload

    # --- convenience accessors over graph state ---
    @property
    def intent(self) -> Optional[str]:
        return self.state.get("intent")

    @property
    def sql(self) -> str:
        return self.state.get("sql") or ""

    @property
    def preview_sql(self) -> str:
        return self.state.get("preview_sql") or ""

    @property
    def data_source(self) -> Optional[str]:
        return self.state.get("data_source")

    @property
    def dml_sql(self) -> str:
        # The refactored destructive flow keeps the accepted DML in ``sql`` and
        # exposes it again in the interrupt payload; fall back across both.
        return (
            self.state.get("dml_sql")
            or self.interrupt_value.get("dml_sql")
            or self.state.get("sql")
            or ""
        )

    @property
    def preview_rows(self) -> List[dict]:
        # reports_gate carries the preview rows inside the ``interrupt()`` payload
        # (not graph state), so read them from the captured interrupt.
        return self.state.get("preview_rows") or self.interrupt_value.get("preview_rows") or []

    @property
    def df_row_count(self) -> Optional[int]:
        """Row count of the analytical result.

        The tool-calling query agent surfaces rows only as a markdown table
        (``rows_markdown``), not as a numeric field, so derive the count: prefer
        an explicit ``df_row_count`` if present, then a ``"of N rows"`` truncation
        footer, else count the table's body rows.
        """
        n = self.state.get("df_row_count")
        if n is not None:
            return n
        md = self.rows_markdown
        if not md.strip():
            return None
        footer = re.search(r"of (\d+) rows", md)
        if footer:
            return int(footer.group(1))
        body = [ln for ln in md.splitlines() if ln.lstrip().startswith("|")]
        return max(0, len(body) - 2) or None  # minus header + separator

    @property
    def rows_markdown(self) -> str:
        return self.state.get("rows_markdown") or ""

    @property
    def final_message(self) -> str:
        return self.state.get("final_message") or ""

    @property
    def touched_bigquery(self) -> bool:
        """A real BigQuery query ran iff sql is set and is not the cached-schema marker."""
        sql = self.sql
        return bool(sql) and "schema introspection" not in sql

    def owners(self) -> List[str]:
        return [r.get("owner_id") for r in self.saved_reports]

    def questions(self) -> List[str]:
        return [r.get("question", "") for r in self.saved_reports]


# --------------------------------------------------------------------------- #
# Environment / credentials
# --------------------------------------------------------------------------- #
def has_creds() -> bool:
    """True when the live stack (Gemini + BigQuery billing project) is configured."""
    return bool(config.GOOGLE_API_KEY and config.GCP_PROJECT)


# --------------------------------------------------------------------------- #
# Isolated library DB
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def isolated_db():
    """Point the SQLite library at a fresh temp file for the duration of a case.

    Clears the ``lru_cache``-backed connection so repositories reopen against the
    new path, then restores everything afterwards. The BigQuery runner and schema
    cache are deliberately left untouched so live cases reuse one schema fetch.
    """
    from app.sources import db

    fd, path = tempfile.mkstemp(prefix="evals_lib_", suffix=".db")
    os.close(fd)

    prev_path = config.DB_PATH
    config.DB_PATH = path
    db.get_connection.cache_clear()  # drop any handle to the developer's DB
    db.init_db()                      # create tables in the fresh temp file
    try:
        yield path
    finally:
        with contextlib.suppress(Exception):
            db.get_connection().close()  # close the cached temp-file handle
        db.get_connection.cache_clear()
        config.DB_PATH = prev_path
        with contextlib.suppress(OSError):
            os.remove(path)


def seed_report(question: str, *, owner_id: Optional[str] = None,
                sql_query: str = "SELECT 1", report_md: str = "seeded",
                created_at: Optional[str] = None) -> str:
    """Insert one row into ``saved_reports`` in the currently-active library DB.

    Used to set up destructive cases deterministically (instead of running
    A1–A3 first). ``created_at`` defaults to ``datetime('now')`` so the row
    matches "за сегодня" predicates.
    """
    from app.sources import db

    conn = db.get_connection()
    report_id = uuid.uuid4().hex
    if created_at is None:
        conn.execute(
            "INSERT INTO saved_reports (id, owner_id, question, sql_query, report_md) "
            "VALUES (?, ?, ?, ?, ?)",
            (report_id, owner_id or config.CURRENT_USER_ID, question, sql_query, report_md),
        )
    else:
        conn.execute(
            "INSERT INTO saved_reports (id, owner_id, question, sql_query, report_md, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (report_id, owner_id or config.CURRENT_USER_ID, question, sql_query,
             report_md, created_at),
        )
    conn.commit()
    return report_id


def snapshot_reports() -> List[dict]:
    """Return all rows currently in ``saved_reports`` (across all owners)."""
    from app.sources import db

    cur = db.get_connection().execute(
        "SELECT id, owner_id, question, created_at FROM saved_reports ORDER BY created_at"
    )
    return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Live graph driver
# --------------------------------------------------------------------------- #
def drive(question: str, *, confirm: Optional[str] = None, debug: bool = False,
          user_id: Optional[str] = None, counters: Optional[dict] = None) -> RunResult:
    """Run ``question`` through the compiled graph; drive the confirm loop.

    ``confirm`` is the answer fed to a destructive ``interrupt()`` (e.g. "да" /
    "нет"); ``None`` means "no confirmation provided" which the gate treats as a
    cancel — exactly like the CLI's AFK default.
    """
    app = build_graph(_MemorySaver())
    cfg = {"configurable": {"thread_id": uuid.uuid4().hex}}
    init_state = {
        "question": question,
        "user_id": user_id or config.CURRENT_USER_ID,
        "debug": debug,
    }

    interrupted = False
    interrupt_value: Dict[str, Any] = {}
    raised: Optional[BaseException] = None
    state: Dict[str, Any] = {}
    try:
        state = app.invoke(init_state, cfg)
        while state.get("__interrupt__"):
            interrupted = True
            if not interrupt_value:
                # ``__interrupt__`` is a tuple of Interrupt objects; the payload we
                # passed to interrupt() (count / preview_rows / dml_sql) is ``.value``.
                intr = state["__interrupt__"]
                with contextlib.suppress(Exception):
                    val = intr[0].value
                    if isinstance(val, dict):
                        interrupt_value = val
            state = app.invoke(Command(resume=confirm), cfg)
    except BaseException as e:  # noqa: BLE001 - the REPL must survive anything
        raised = e

    return RunResult(
        question=question,
        state=state,
        interrupted=interrupted,
        raised=raised,
        saved_reports=snapshot_reports(),
        counters=counters or {},
        interrupt_value=interrupt_value,
    )


# --------------------------------------------------------------------------- #
# Fault injection (offline [sim] cases)
# --------------------------------------------------------------------------- #
class FakeLLM:
    """A langchain-chat-model stand-in driven by a scripted response list.

    Each ``invoke`` consumes the next scripted item. Item shapes:

    * ``str``                       -> an AI message with that ``.content`` and no
      tool calls (the destructive generator + supervisor read ``.content``).
    * ``dict`` with ``tool_calls``  -> an AI message exposing ``.tool_calls`` (the
      tool-calling query agent reads these); optional ``content`` key.
    * an ``Exception`` instance/class -> *raised* (to simulate quota / overload).

    ``bind_tools`` is a no-op that returns ``self`` so the query agent's
    ``get_llm(...).bind_tools(QUERY_TOOLS)`` works offline. The last item repeats
    once the script is exhausted. ``.calls`` counts ``invoke`` hits (used to
    assert "no retries").
    """

    def __init__(self, script: List[Any]):
        self._script = list(script)
        self.calls = 0

    def bind_tools(self, _tools, **_kwargs):
        return self

    def invoke(self, _prompt):
        self.calls += 1
        item = self._script[min(self.calls - 1, len(self._script) - 1)]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item("injected failure")
        if isinstance(item, dict) and "tool_calls" in item:
            return SimpleNamespace(content=item.get("content", ""), tool_calls=item["tool_calls"])
        return SimpleNamespace(content=item, tool_calls=[])


@contextlib.contextmanager
def fake_llms(*, supervisor: FakeLLM, sql: Optional[FakeLLM] = None,
              report: Optional[FakeLLM] = None):
    """Patch each agent's ``get_llm`` so nodes get scripted doubles keyed by model.

    ``get_llm`` now lives in ``app.llm`` and is imported *by name* into every
    agent module, so patching ``app.llm.get_llm`` alone would not reach those
    bindings — we swap ``get_llm`` on each agent module instead.
    """
    # Build keyed by model name, but NEVER let a None-fallback clobber an
    # explicitly-provided fake: several roles can share a model name (e.g.
    # SQL_MODEL == REPORT_MODEL in .env), and dict-literal ordering would
    # otherwise overwrite the SQL double with the report's supervisor fallback.
    mapping: Dict[str, FakeLLM] = {}
    for model, fake in (
        (config.SUPERVISOR_MODEL, supervisor),
        (config.SQL_MODEL, sql),
        (config.REPORT_MODEL, report),
    ):
        if fake is not None:
            mapping[model] = fake

    def _fake_get_llm(model, temperature: float = 0.0):
        # Unmapped models (e.g. a report model when only sql was scripted) fall
        # back to the supervisor double.
        return mapping.get(model, supervisor)

    import app.agents.report_agent as rep_mod
    import app.agents.reports_gate as gate_mod
    import app.agents.sql_agent as sql_mod
    import app.agents.supervisor as sup_mod

    targets = [sup_mod, sql_mod, rep_mod, gate_mod]
    originals = [(m, getattr(m, "get_llm", None)) for m in targets]
    for m in targets:
        m.get_llm = _fake_get_llm  # type: ignore[assignment]
    try:
        yield mapping
    finally:
        for m, orig in originals:
            if orig is not None:
                m.get_llm = orig  # type: ignore[assignment]


class FakeBQRunner:
    """A BigQueryRunner double. ``execute_query`` either raises a scripted
    exception or returns a DataFrame; ``list_tables`` / ``get_table_schema``
    return stubs so the agent's schema fetch succeeds offline. ``.query_calls``
    counts attempts.
    """

    def __init__(self, *, execute=None, schema=None, tables=None):
        self._execute = execute            # callable(sql) -> DataFrame | raises
        self._schema = schema or [
            {"name": "id", "type": "INTEGER", "mode": "NULLABLE", "description": ""}
        ]
        self._tables = tables or ["orders", "order_items", "products", "users"]
        self.query_calls = 0

    def execute_query(self, sql_query):
        self.query_calls += 1
        if self._execute is None:
            import pandas as pd
            return pd.DataFrame()
        return self._execute(sql_query)

    def list_tables(self):
        return list(self._tables)

    def get_table_schema(self, _table_name):
        return list(self._schema)


@contextlib.contextmanager
def fake_bq(runner: FakeBQRunner):
    """Install a fake BigQuery runner and reset the schema cache around it.

    ``get_bq_runner`` (in ``app.sources.bigquery``) is imported by name into the
    query tool and the schema helper, so patch it on those call sites. The schema
    text/table lists are ``lru_cache``-d in ``app.tools.schema`` — clear them so a
    prior live fetch can't leak into an offline case (and vice-versa).
    """
    import app.tools.query_tools as qt
    import app.tools.schema as schema_mod

    def _runner():
        return runner

    prev_qt = qt.get_bq_runner
    prev_schema_runner = getattr(schema_mod, "get_bq_runner", None)
    cache_fns = [getattr(schema_mod, n, None) for n in ("get_bq_tables", "get_schema_text")]

    qt.get_bq_runner = _runner  # type: ignore[assignment]
    if prev_schema_runner is not None:
        schema_mod.get_bq_runner = _runner  # type: ignore[assignment]
    for fn in cache_fns:
        if fn is not None and hasattr(fn, "cache_clear"):
            fn.cache_clear()
    try:
        yield runner
    finally:
        qt.get_bq_runner = prev_qt  # type: ignore[assignment]
        if prev_schema_runner is not None:
            schema_mod.get_bq_runner = prev_schema_runner  # type: ignore[assignment]
        for fn in cache_fns:
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()


@contextlib.contextmanager
def no_sleep(record: List[float]):
    """Record backoff delays instead of sleeping (no real waiting).

    Backoff now lives in the ``retry_with_backoff`` decorator (``app.retry``),
    which captures ``time.sleep`` at decoration time — so patching a module
    attribute can't intercept it. Instead, rebuild ``sql_tools.run_with_backoff``
    from its undecorated ``__wrapped__`` with a recording ``sleep`` and swap it in
    (the query tool calls it as ``sql_tools.run_with_backoff``, so the swap takes).
    """
    import app.tools.sql_tools as sql_tools
    from app import errors
    from app.retry import retry_with_backoff

    original = sql_tools.run_with_backoff
    raw = getattr(original, "__wrapped__", None)

    def _record(seconds):
        record.append(seconds)

    if raw is not None:
        sql_tools.run_with_backoff = retry_with_backoff(  # type: ignore[assignment]
            retry_on=errors.is_retryable_bq,
            max_retries=config.MAX_BACKOFF_RETRIES,
            base_seconds=config.BACKOFF_BASE_SECONDS,
            max_seconds=config.BACKOFF_MAX_SECONDS,
            on_exhausted=lambda e: errors.ServiceUnavailableError(str(e)),
            sleep=_record,
        )(raw)
    try:
        yield record
    finally:
        sql_tools.run_with_backoff = original  # type: ignore[assignment]
