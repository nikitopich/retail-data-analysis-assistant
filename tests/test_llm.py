"""LLM factory and response normalizer (app/llm.py)."""
from __future__ import annotations

import sys
import types

from app.llm import llm_text, get_llm
from app import config


# --------------------------------------------------------------------------- #
# llm_text
# --------------------------------------------------------------------------- #
def test_llm_text_plain_string():
    class R:
        content = "hello"
    assert llm_text(R()) == "hello"


def test_llm_text_block_list():
    class R:
        content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert llm_text(R()) == "ab"


def test_llm_text_skips_non_text_blocks():
    class R:
        content = [
            {"type": "thinking", "text": "reasoning"},
            {"type": "text", "text": "answer"},
            "raw",
        ]
    assert llm_text(R()) == "answerraw"


def test_llm_text_object_without_content_falls_back():
    assert llm_text(123) == "123"


# --------------------------------------------------------------------------- #
# get_llm — fail-fast retry contract
# --------------------------------------------------------------------------- #
def test_get_llm_passes_failfast_params(monkeypatch):
    captured = {}

    class FakeChat:
        def __init__(self, **kw):
            captured.update(kw)

    mod = types.ModuleType("langchain_google_genai")
    mod.ChatGoogleGenerativeAI = FakeChat
    monkeypatch.setitem(sys.modules, "langchain_google_genai", mod)

    get_llm.cache_clear()
    get_llm("gemini-x", temperature=0.5)
    get_llm.cache_clear()

    assert captured["model"] == "gemini-x"
    assert captured["temperature"] == 0.5
    assert captured["max_retries"] == config.LLM_MAX_RETRIES == 1
    assert captured["timeout"] == config.LLM_TIMEOUT_S
