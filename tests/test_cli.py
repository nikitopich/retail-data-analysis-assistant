"""CLI helpers — the deterministic, non-REPL pieces (app/cli.py)."""
from __future__ import annotations

import io

import pytest

from app import cli


# --------------------------------------------------------------------------- #
# _print_preview
# --------------------------------------------------------------------------- #
def test_print_preview_lists_rows(capsys):
    cli._print_preview({"preview_rows": [
        {"question": "about x", "created_at": "2026-06-29"},
        {"question": "about y", "created_at": "2026-06-28"},
    ]})
    out = capsys.readouterr().out
    assert "2" in out
    assert '"about x" (2026-06-29)' in out


def test_print_preview_empty(capsys):
    cli._print_preview({})
    assert "0" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# _timed_input (non-interactive fallback)
# --------------------------------------------------------------------------- #
def test_timed_input_reads_line(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("yes\n"))
    assert cli._timed_input("prompt: ", timeout=0) == "yes"


def test_timed_input_eof_returns_none(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    assert cli._timed_input("prompt: ", timeout=0) is None


# --------------------------------------------------------------------------- #
# _resume_loop
# --------------------------------------------------------------------------- #
class _FakeApp:
    def __init__(self, final):
        self.final = final
        self.resumed_with = None

    def invoke(self, command, run_config):
        self.resumed_with = command.resume
        return self.final


def _interrupt_result(count=1):
    obj = type("I", (), {"value": {
        "preview_rows": [{"question": "q", "created_at": "t"}],
        "count": count,
    }})()
    return {"__interrupt__": [obj]}


def test_resume_loop_forwards_user_answer(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    app = _FakeApp(final={"final_message": "ok"})
    cli._resume_loop(app, _interrupt_result(), {"configurable": {}})
    assert app.resumed_with == "yes"


def test_resume_loop_eof_sends_no(monkeypatch):
    def _raise(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", _raise)
    app = _FakeApp(final={"final_message": "done"})
    result = cli._resume_loop(app, _interrupt_result(), {"configurable": {}})
    assert result == {"final_message": "done"}
    assert app.resumed_with == "no"


def test_resume_loop_noop_without_interrupt():
    app = _FakeApp(final=None)
    result = {"final_message": "already done"}
    assert cli._resume_loop(app, result, {}) is result
