"""LangGraph shared state (spec §11)."""
from __future__ import annotations

from typing import Any, List, Literal, Optional, TypedDict


class AgentState(TypedDict, total=False):
    question: str
    user_id: str
    debug: bool
    intent: Literal["analytical", "destructive", "other"]

    # SQL agent
    schema_text: str
    sql: str
    sql_attempts: int
    last_error: Optional[str]
    rows_markdown: Optional[str]
    df_row_count: int

    # report
    report_md: Optional[str]

    # destructive
    preview_sql: Optional[str]
    dml_sql: Optional[str]
    preview_rows: Optional[List[dict]]
    confirmed: Optional[bool]

    # output to user
    final_message: str
