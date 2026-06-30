"""Observability/tracing init — opt-in gate is the deterministic part (app/observability.py)."""
from __future__ import annotations

import builtins

from app.observability import init_tracing


def test_disabled_returns_none():
    assert init_tracing(False) is None


def test_init_failure_is_non_fatal(monkeypatch):
    real_import = builtins.__import__

    def _boom(name, *args, **kwargs):
        if "phoenix" in name or "openinference" in name:
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert init_tracing(True) is None
