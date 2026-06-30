"""CLI REPL: questions in, scenario answers out, with the confirmation-flow
resume loop for destructive ops (spec §3.1, §8, §12).
"""
from __future__ import annotations

import os
import select
import sqlite3
import sys
import uuid
import warnings

# Suppress gRPC C++ fork/poll info messages (printed to stderr before Python logging).
os.environ.setdefault("GRPC_VERBOSITY", "error")
# BigQuery Storage optional-module notice is harmless — REST fallback works fine.
warnings.filterwarnings("ignore", "BigQuery Storage module not found", category=UserWarning)

from langgraph.types import Command

from app import config, errors
from app.graph.build import build_graph
from app.observability import init_tracing
from app.sources.db import init_db
from app.sources.reports_repo import flush_pending_trio

BANNER = "Retail Analysis Assistant. Enter your question ('exit' to quit)."


def _make_checkpointer() -> "object":
    """Persistent SqliteSaver for interrupt()/resume() across REPL turns."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(config.CHECKPOINTS_PATH, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def _timed_input(prompt: str, timeout: int):
    """input() with an AFK timeout. Returns None on timeout.

    Uses select() on stdin (POSIX). Falls back to a blocking read when stdin is
    not an interactive tty (pipes/tests) or select is unavailable.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        interactive = timeout and timeout > 0 and sys.stdin and sys.stdin.isatty()
    except Exception:
        interactive = False
    if interactive:
        try:
            ready, _, _ = select.select([sys.stdin], [], [], timeout)
        except (OSError, ValueError):
            ready = [sys.stdin]
        if not ready:
            sys.stdout.write("\n")
            return None
    line = sys.stdin.readline()
    if line == "":  # EOF
        return None
    return line.rstrip("\n")


def _print_preview(payload: dict) -> None:
    rows = payload.get("preview_rows", []) or []
    verb = payload.get("verb", "DELETE")
    action = "changes" if verb == "UPDATE" else "deletions"
    print(f"⚠️  Records matched ({action}): {len(rows)}")
    for i, r in enumerate(rows, 1):
        print(f'  {i}. "{r.get("question", "")}" ({r.get("created_at", "")})')


def _resume_loop(app, result: dict, run_config: dict) -> dict:
    """Drive the human-in-the-loop confirmation until the graph finishes.

    If the user is away for AFK_TIMEOUT_S, the pending destructive op is
    auto-cancelled (the safe default — never auto-delete).
    """
    while result.get("__interrupt__"):
        interrupt_obj = result["__interrupt__"][0]
        payload = getattr(interrupt_obj, "value", {}) or {}
        _print_preview(payload)
        verb = payload.get("verb", "DELETE")
        if verb == "UPDATE":
            prompt_str = "Confirm the change or clarify which records to apply it to (number/yes/no): "
        else:
            prompt_str = "Confirm deletion? (yes/no): "
        try:
            answer = input(prompt_str)
        except (EOFError, KeyboardInterrupt):
            print()
            answer = "no"
        result = app.invoke(Command(resume=answer), run_config)
    return result


def main() -> None:
    debug = ("--debug" in sys.argv) or config.DEBUG
    tracing_enabled = ("--trace" in sys.argv) or config.TRACING

    # Fail fast on missing required configuration.
    try:
        config.validate_required()
    except config.ConfigError as e:
        print(str(e))
        sys.exit(1)

    phoenix_url = init_tracing(
        tracing_enabled, endpoint=config.PHOENIX_COLLECTOR_ENDPOINT, debug=debug
    )
    if phoenix_url:
        print(f"Phoenix tracing: {phoenix_url}")
    elif tracing_enabled:
        print("Phoenix tracing: requested but unavailable (continuing without it)")

    init_db()
    app = build_graph(_make_checkpointer())

    session_id = uuid.uuid4().hex
    turn = 0
    # Mirrors the graph's pending_trio state so the CLI can flush it on AFK/exit
    # without re-querying the checkpointer.
    pending_trio: dict | None = None

    print(BANNER)
    if debug:
        print("[debug mode enabled]")

    while True:
        try:
            if pending_trio:
                # After a report: use a timed prompt to detect AFK implicit approval.
                line = _timed_input("> ", config.TRIO_AFK_TIMEOUT_S)
                if line is None:
                    flush_pending_trio(pending_trio)
                    pending_trio = None
                    print(f"(no response for {config.TRIO_AFK_TIMEOUT_S}s — report added to training set)")
                    # Continue with a blocking prompt for the next question.
                    try:
                        line = input("> ")
                    except (EOFError, KeyboardInterrupt):
                        print()
                        break
            else:
                line = input("> ")
        except (EOFError, KeyboardInterrupt):
            if pending_trio:
                flush_pending_trio(pending_trio)
            print()
            break

        question = line.strip()
        if not question:
            continue
        if question.lower() in ("exit", "quit"):
            if pending_trio:
                flush_pending_trio(pending_trio)
            break

        turn += 1
        # One checkpointer thread for the whole session (spec §3.2): state persists
        # across turns so `regenerate` can revise the previous report. Per-turn
        # control fields are reset in the supervisor to avoid stale-state leakage.
        run_config = {"configurable": {"thread_id": session_id}}
        init_state = {
            "question": question,
            "user_id": config.CURRENT_USER_ID,
            "debug": debug,
        }

        try:
            result = app.invoke(init_state, run_config)
            result = _resume_loop(app, result, run_config)
            print(result.get("final_message", "") or "")
            # Track pending_trio locally; supervisor already consumed/cleared the
            # previous one in graph state before report_agent set the new one.
            pending_trio = result.get("pending_trio")
        except Exception as e:
            # The REPL must never die on an unexpected error (spec §5.4).
            print(errors.format_error(errors.UNEXPECTED, debug, e))
        print()

    print("Goodbye!")


if __name__ == "__main__":
    main()
