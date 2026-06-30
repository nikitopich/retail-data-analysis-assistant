"""Config helpers: env coercion and required-var validation (app/config.py)."""
from __future__ import annotations

import pytest

from app import config


# --- _int_env ---
def test_int_env_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_INT", raising=False)
    assert config._int_env("SOME_INT", 7) == 7


def test_int_env_parses_value(monkeypatch):
    monkeypatch.setenv("SOME_INT", "42")
    assert config._int_env("SOME_INT", 7) == 42


def test_int_env_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("SOME_INT", "not-a-number")
    assert config._int_env("SOME_INT", 7) == 7


def test_int_env_falls_back_on_empty(monkeypatch):
    monkeypatch.setenv("SOME_INT", "")
    assert config._int_env("SOME_INT", 7) == 7


# --- _truthy ---
@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", "  yes  "])
def test_truthy_true(raw):
    assert config._truthy(raw)


@pytest.mark.parametrize("raw", [None, "", "0", "false", "no", "off", "FALSE"])
def test_truthy_false(raw):
    assert not config._truthy(raw)


# --- validate_required ---
def test_validate_required_raises_when_missing(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", None)
    monkeypatch.setattr(config, "GCP_PROJECT", None)
    with pytest.raises(config.ConfigError) as ei:
        config.validate_required()
    assert "GOOGLE_API_KEY" in str(ei.value)
    assert "GCP_PROJECT" in str(ei.value)


def test_validate_required_passes_when_present(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "key")
    monkeypatch.setattr(config, "GCP_PROJECT", "proj")
    config.validate_required()
