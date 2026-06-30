"""StateGraph assembly + checkpointer wiring (spec §2.1, §3.6)."""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.nodes.destructive import (
    destructive_gate,
    destructive_generate,
    destructive_preview,
)
from app.nodes.report_agent import report_agent
from app.nodes.sql_agent import sql_agent
from app.nodes.supervisor import supervisor
from app.state import AgentState


# --- routers ---
def _route_supervisor(state: AgentState) -> str:
    return state.get("intent", "other")


def _route_after_sql(state: AgentState) -> str:
    # sql_agent sets final_message only on a terminal (empty/exhausted/down) path.
    return "end" if state.get("final_message") else "report"


def _route_after_generate(state: AgentState) -> str:
    return "end" if state.get("final_message") else "preview"


def _route_after_preview(state: AgentState) -> str:
    return "end" if state.get("final_message") else "gate"


def build_graph(checkpointer):
    """Build and compile the agent graph with the given checkpointer."""
    graph = StateGraph(AgentState)

    graph.add_node("supervisor", supervisor)
    graph.add_node("sql_agent", sql_agent)
    graph.add_node("report_agent", report_agent)
    graph.add_node("destructive_generate", destructive_generate)
    graph.add_node("destructive_preview", destructive_preview)
    graph.add_node("destructive_gate", destructive_gate)

    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        _route_supervisor,
        {
            "analytical": "sql_agent",
            "destructive": "destructive_generate",
            "other": END,
        },
    )
    graph.add_conditional_edges(
        "sql_agent", _route_after_sql, {"report": "report_agent", "end": END}
    )
    graph.add_edge("report_agent", END)
    graph.add_conditional_edges(
        "destructive_generate",
        _route_after_generate,
        {"preview": "destructive_preview", "end": END},
    )
    graph.add_conditional_edges(
        "destructive_preview",
        _route_after_preview,
        {"gate": "destructive_gate", "end": END},
    )
    graph.add_edge("destructive_gate", END)

    return graph.compile(checkpointer=checkpointer)
