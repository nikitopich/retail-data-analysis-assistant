"""LLM factory + response normalization (extracted from config, spec §3.3).

Provider is swapped by changing this single module; everything else depends on
``get_llm`` / ``llm_text`` rather than on a concrete SDK.
"""
from __future__ import annotations

from functools import lru_cache

from app import config


def llm_text(response) -> str:
    """Return an LLM response's text as a plain string.

    Gemini 3.x models return ``.content`` as a list of content blocks
    (e.g. ``[{'type': 'text', 'text': '...'}]``) instead of a plain string;
    2.x models return a string. Normalize both shapes (and skip non-text
    reasoning blocks).
    """
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and "text" in block and block.get("type", "text") == "text":
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


@lru_cache(maxsize=8)
def get_llm(model: str, temperature: float = 0.0):
    """Factory for a Gemini chat model via langchain-google-genai.

    Cached per ``(model, temperature)`` so the per-node calls (supervisor / SQL /
    report agents) reuse one client instead of reconstructing it every turn. The
    client is used read-only via ``.invoke``, so sharing is safe. Imported lazily
    so that lightweight entrypoints (e.g. ``init_db``) do not require the LLM
    stack to be importable.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=model,
        temperature=temperature,
        google_api_key=config.GOOGLE_API_KEY,
        max_retries=config.LLM_MAX_RETRIES,   # fail fast: no SDK retry/backoff on 5xx/429
        timeout=config.LLM_TIMEOUT_S,         # bound a single hung request
    )
