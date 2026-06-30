"""StateGraph assembly + checkpointer wiring (spec §2.1, §3.6)."""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.agents.report_agent import report_agent
from app.agents.reports_gate import reports_gate
from app.agents.sql_agent import sql_agent
from app.agents.supervisor import supervisor
from app.graph.state import AgentState


# --- routers ---
def _route_supervisor(state: AgentState) -> str:
    return state.get("intent", "other")


def _route_after_sql(state: AgentState) -> str:
    if state.get("final_message"):
        return "end"
    # Destructive ops and saved-reports reads both go through the gate (which
    # branches on the SQL verb: DML -> confirm, SELECT -> owner-scoped read).
    if state.get("intent") == "destructive" or state.get("data_source") == "reports":
        return "reports_gate"
    return "report_agent"


def build_graph(checkpointer):
    """Build and compile the agent graph with the given checkpointer."""
    graph = StateGraph(AgentState)

    graph.add_node("supervisor", supervisor)
    graph.add_node("sql_agent", sql_agent)
    graph.add_node("report_agent", report_agent)
    graph.add_node("reports_gate", reports_gate)

    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        _route_supervisor,
        {
            "query": "sql_agent",
            "destructive": "sql_agent",
            "regenerate": "report_agent",
            "other": END,
        },
    )
    graph.add_conditional_edges(
        "sql_agent",
        _route_after_sql,
        {"report_agent": "report_agent", "reports_gate": "reports_gate", "end": END},
    )
    graph.add_edge("report_agent", END)
    graph.add_edge("reports_gate", END)

    return graph.compile(checkpointer=checkpointer)
