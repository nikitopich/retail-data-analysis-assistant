"""Report Agent node — generate the markdown report, then persist it (spec §3.4)."""
from __future__ import annotations

from app import config, errors, prompts
from app.repositories import SavedReportsRepo, StagedTriosRepo, UserPrefsRepo
from app.state import AgentState


def report_agent(state: AgentState) -> dict:
    debug = state.get("debug", False)
    question = state["question"]
    rows_markdown = state.get("rows_markdown", "") or ""

    # optional preference read (management of prefs is out of scope -> almost always 'table')
    try:
        output_format = UserPrefsRepo().get_output_format(config.CURRENT_USER_ID)
    except Exception:
        output_format = "table"

    llm = config.get_llm(config.REPORT_MODEL, temperature=0.3)
    prompt = prompts.REPORT_PROMPT.format(
        output_format=output_format,
        question=question,
        rows_markdown=rows_markdown,
    )

    try:
        resp = llm.invoke(prompt)
        report_md = (resp.content or "").strip()
    except Exception as e:
        return {"final_message": errors.format_error(errors.LLM_UNAVAILABLE, debug, e)}

    sql_query = state.get("sql", "") or ""

    # Save to the library. A persistence failure must not lose the answer the
    # user already has, so we swallow it (and surface it only in debug).
    saved = True
    save_err = None
    try:
        report_id = SavedReportsRepo().save(
            config.CURRENT_USER_ID, question, sql_query, report_md
        )
        StagedTriosRepo().add(
            report_id, config.CURRENT_USER_ID, question, sql_query, report_md
        )
    except Exception as e:  # pragma: no cover
        saved = False
        save_err = e

    if saved:
        note = "\n\n_(отчёт сохранён в библиотеку)_"
    elif debug and save_err is not None:
        note = "\n\n" + errors.format_error("(не удалось сохранить отчёт)", True, save_err)
    else:
        note = ""

    return {"report_md": report_md, "final_message": report_md + note}
