"""Graph routing predicates (app/graph/build.py). Pure functions over state."""
from __future__ import annotations

import pytest

from app.graph.build import _route_after_sql, _route_supervisor


@pytest.mark.parametrize("intent,expected", [
    ("query", "query"),
    ("destructive", "destructive"),
    ("regenerate", "regenerate"),
    ("set_preference", "set_preference"),
    ("other", "other"),
])
def test_route_supervisor(intent, expected):
    assert _route_supervisor({"intent": intent}) == expected


def test_route_supervisor_defaults_to_other():
    assert _route_supervisor({}) == "other"


def test_route_after_sql_to_report_agent_on_analytical():
    assert _route_after_sql({"rows_markdown": "x"}) == "report_agent"


def test_route_after_sql_to_reports_gate_on_destructive():
    assert _route_after_sql({"intent": "destructive", "sql": "DELETE ..."}) == "reports_gate"


def test_route_after_sql_to_end_on_terminal_message():
    assert _route_after_sql({"final_message": "no data"}) == "end"


def test_route_after_sql_schema_goes_to_end():
    assert _route_after_sql({"data_source": "schema", "final_message": ""}) == "end"


def test_route_after_sql_no_data_source_goes_to_report_agent():
    assert _route_after_sql({}) == "report_agent"
