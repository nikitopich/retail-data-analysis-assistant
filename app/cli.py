"""CLI REPL: questions in, scenario answers out, with the confirmation-flow
resume loop for destructive ops (spec §3.1, §8, §12).
"""
from __future__ import annotations

import sqlite3
import sys
import uuid

from langgraph.types import Command

from app import config, errors
from app.db import init_db
from app.graph import build_graph
from app.tracing import init_tracing

BANNER = "Retail Analysis Assistant. Введите вопрос ('exit' для выхода)."


def _make_checkpointer() -> "object":
    """Persistent SqliteSaver for interrupt()/resume() across REPL turns."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(config.CHECKPOINTS_PATH, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def _print_preview(payload: dict) -> None:
    rows = payload.get("preview_rows", []) or []
    print(f"⚠️  Под условие попали записи: {len(rows)}")
    for r in rows:
        question = r.get("question", "")
        created = r.get("created_at", "")
        print(f'  - "{question}" ({created})')


def _resume_loop(app, result: dict, run_config: dict) -> dict:
    """Drive the human-in-the-loop confirmation until the graph finishes."""
    while result.get("__interrupt__"):
        interrupt_obj = result["__interrupt__"][0]
        payload = getattr(interrupt_obj, "value", {}) or {}
        _print_preview(payload)
        try:
            answer = input("Подтвердить удаление? (да/нет): ")
        except EOFError:
            answer = "нет"
        result = app.invoke(Command(resume=answer), run_config)
    return result


def main() -> None:
    debug = ("--debug" in sys.argv) or config.DEBUG

    phoenix_url = init_tracing(debug=debug)
    if phoenix_url:
        print(f"Phoenix трейсинг: {phoenix_url}")
    else:
        print("Phoenix трейсинг: недоступен (продолжаем без него)")

    init_db()
    app = build_graph(_make_checkpointer())

    session_id = uuid.uuid4().hex
    turn = 0

    print(BANNER)
    if debug:
        print("[debug-режим включён]")

    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        question = line.strip()
        if not question:
            continue
        if question.lower() in ("exit", "quit", "выход"):
            break

        turn += 1
        # Fresh thread per turn isolates state; the same thread is reused for
        # the interrupt/resume of THIS turn only.
        run_config = {"configurable": {"thread_id": f"{session_id}-{turn}"}}
        init_state = {
            "question": question,
            "user_id": config.CURRENT_USER_ID,
            "debug": debug,
        }

        try:
            result = app.invoke(init_state, run_config)
            intent = result.get("intent")
            if intent:
                print(f"[intent: {intent}]")
            result = _resume_loop(app, result, run_config)
            print(result.get("final_message", "") or "")
        except Exception as e:
            # The REPL must never die on an unexpected error (spec §5.4).
            print(errors.format_error(errors.UNEXPECTED, debug, e))
        print()

    print("До свидания!")


if __name__ == "__main__":
    main()
