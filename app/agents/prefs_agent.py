"""Prefs Agent node — synchronous explicit-preference capture (spec §6.4).

When the supervisor classifies a turn as ``set_preference``, this node extracts
the user's stated report preferences (format / tone / extra), persists them to
``user_prefs`` via ``UserPrefsRepo`` (UPSERT), and — if a report was already
shown this session — re-renders that last report applying the new preferences so
the change is visible immediately.

Unlike the deferred async ``prefs_extractor`` (which infers *implicit* prefs from
dialogue history), this path is explicit and runs inline in the graph.
"""
from __future__ import annotations

import json
from typing import Optional

from app import config, errors
from app.agents.report_agent import revise
from app.graph.state import AgentState
from app.llm import get_llm, llm_text
from app.sources.prefs_repo import UserPrefsRepo

_EXTRACT_PROMPT = """The user is stating standing preferences for how analytics reports should be written.
Extract ONLY what they explicitly express. Reply with a single compact JSON object and nothing else:
{{"output_format": <string or null>, "tone": <string or null>, "extra": <string or null>}}

- output_format: the desired report format if mentioned (e.g. "CSV", "markdown table",
  "bulleted list", "plain text"). Else null.
- tone: the desired tone/voice if mentioned (e.g. "concise", "formal", "friendly"). Else null.
- extra: any other formatting/content preference (e.g. "always include a totals row",
  "no emojis", "round to whole numbers"). Else null.
Keep each value short, in the user's own wording. If they expressed no concrete preference,
set all three to null.

User message: {message}
JSON:"""


def _parse_json(text: str) -> dict:
    """Tolerant parse of the extractor's JSON (strips code fences / surrounding prose)."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        nl = s.find("\n")  # drop a leading language tag like ```json
        if nl != -1:
            s = s[nl + 1:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        obj = json.loads(s[start:end + 1])
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _clean(value) -> Optional[str]:
    """Normalise an extracted field to a non-empty string or None."""
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or v.lower() in ("null", "none"):
        return None
    return v


def _confirmation(output_format: Optional[str], tone: Optional[str], extra: Optional[str]) -> str:
    parts = []
    if output_format:
        parts.append(f"формат — {output_format}")
    if tone:
        parts.append(f"тон — {tone}")
    if extra:
        parts.append(f"ещё — {extra}")
    return "✓ Запомнил ваши предпочтения: " + "; ".join(parts) + "."


def prefs_agent(state: AgentState) -> dict:
    debug = state.get("debug", False)
    message = state["question"]
    user_id = state.get("user_id") or config.CURRENT_USER_ID

    # 1. Extract the preference delta from the user's message.
    try:
        resp = get_llm(config.SUPERVISOR_MODEL).invoke(_EXTRACT_PROMPT.format(message=message))
        raw = _parse_json(llm_text(resp))
    except Exception as e:
        return {"final_message": errors.format_llm_error(e, debug)}

    output_format = _clean(raw.get("output_format"))
    tone = _clean(raw.get("tone"))
    extra = _clean(raw.get("extra"))

    if not (output_format or tone or extra):
        return {"final_message": errors.PREFS_NOT_UNDERSTOOD}

    # 2. Persist (UPSERT). A transient SQLite outage surfaces as "service unavailable".
    try:
        prefs = UserPrefsRepo().upsert_prefs(
            user_id,
            output_format=output_format,
            tone_preference=tone,
            extra_prefs=extra,
        )
    except errors.ServiceUnavailableError:
        return {"final_message": errors.SERVICE_UNAVAILABLE}
    except Exception as e:
        return {"final_message": errors.format_error(errors.UNEXPECTED, debug, e)}

    confirmation = _confirmation(output_format, tone, extra)

    # 3. Re-render the last report (if any) so the new preferences take effect now.
    #    A pure preference change does NOT create a new library entry or pending_trio.
    prev_report = state.get("report_md") or ""
    rows_markdown = state.get("rows_markdown") or ""
    if not prev_report:
        return {"final_message": confirmation}

    orig_question = state.get("last_question") or "(unknown)"
    try:
        new_report = revise(orig_question, rows_markdown, prev_report, message, prefs)
    except Exception:
        # The preference is saved; only the immediate re-render failed.
        return {"final_message": confirmation + "\n\n_(применю к следующему отчёту)_"}

    return {
        "report_md": new_report,
        "last_question": orig_question,
        "final_message": confirmation + "\n\n" + new_report,
    }
