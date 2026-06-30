"""LangGraph shared state (spec §11)."""
from __future__ import annotations

from typing import List, Literal, Optional, TypedDict


class AgentState(TypedDict, total=False):
    question: str
    user_id: str
    debug: bool
    # Flow intent (thin supervisor): the data-source decision for `query` is made
    # downstream by the SQL agent, not here (see app/agents/sql_agent.py).
    intent: Literal["query", "destructive", "regenerate", "other"]

    # SQL agent (BQ analytical path)
    schema_text: str
    sql: str
    sql_attempts: int
    last_error: Optional[str]
    rows_markdown: Optional[str]
    df_row_count: int
    # Which source the SQL agent resolved a `query` to: analytical | schema | reports.
    # Drives post-SQL routing (reports -> gate, else -> report_agent).
    data_source: Optional[str]

    # report agent
    report_md: Optional[str]
    # Original question of the last produced report — carried across turns (shared
    # thread) so the `regenerate` flow can revise it with the user's correction.
    last_question: Optional[str]

    # reports gate (DML confirmation)
    preview_sql: Optional[str]
    preview_rows: Optional[List[dict]]
    confirmed: Optional[bool]

    # output to user
    final_message: str
