"""Graph assembly (spec §2.1, §3.6). Build is deterministic; smoke test that the
StateGraph compiles with every node and the routers wired in."""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from app.graph.build import build_graph

_NODES = {"supervisor", "sql_agent", "report_agent", "reports_gate", "prefs_agent"}


def test_build_graph_compiles_with_all_nodes():
    app = build_graph(MemorySaver())
    nodes = set(app.get_graph().nodes)
    assert _NODES <= nodes


def test_build_graph_uses_given_checkpointer():
    saver = MemorySaver()
    app = build_graph(saver)
    assert app.checkpointer is saver
