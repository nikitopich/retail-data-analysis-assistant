"""Supervisor node — intent classification (spec §3.2, §6.1)."""
from __future__ import annotations

from app import config, errors, prompts
from app.state import AgentState

_LABELS = ("analytical", "destructive", "other")


def supervisor(state: AgentState) -> dict:
    """Classify the user's message into one of analytical/destructive/other."""
    question = state["question"]
    llm = config.get_llm(config.SUPERVISOR_MODEL)
    prompt = prompts.SUPERVISOR_PROMPT.format(question=question)

    try:
        resp = llm.invoke(prompt)
        text = (resp.content or "").strip().lower()
    except Exception:
        # Classification failed (quota/overload/etc.) — fall back to a safe
        # scenario answer rather than crashing or guessing a destructive intent.
        return {"intent": "other", "final_message": errors.OTHER_INTENT}

    intent = "other"
    tokens = text.replace("|", " ").split()
    if tokens and tokens[0] in _LABELS:
        intent = tokens[0]
    else:
        for label in _LABELS:
            if label in text:
                intent = label
                break

    out: dict = {"intent": intent}
    if intent == "other":
        out["final_message"] = errors.OTHER_INTENT
    return out
