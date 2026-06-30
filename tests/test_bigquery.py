"""BigQueryRunner wrapper + lazy singleton factory (app/sources/bigquery.py)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.sources import bigquery as bq_mod
from app.sources.bigquery import BigQueryRunner, get_bq_runner


@pytest.fixture
def mock_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(bq_mod.bigquery, "Client", MagicMock(return_value=client))
    return client


def test_init_sets_cost_guard(mock_client):
    runner = BigQueryRunner(project_id="p", dataset_id="ds", max_bytes_billed=123)
    assert runner.client is mock_client
    assert runner.dataset_id == "ds"
    assert runner.max_bytes_billed == 123


def test_init_failure_reraises(monkeypatch):
    monkeypatch.setattr(
        bq_mod.bigquery, "Client", MagicMock(side_effect=RuntimeError("no creds"))
    )
    with pytest.raises(RuntimeError):
        BigQueryRunner()


def test_execute_query_returns_dataframe(mock_client):
    df = pd.DataFrame({"x": [1, 2]})
    mock_client.query.return_value.result.return_value.to_dataframe.return_value = df

    runner = BigQueryRunner(max_bytes_billed=999)
    out = runner.execute_query("SELECT 1")

    assert out is df
    _, kwargs = mock_client.query.call_args
    assert kwargs["job_config"] is not None


def test_execute_query_without_cost_guard_passes_none(mock_client):
    mock_client.query.return_value.result.return_value.to_dataframe.return_value = pd.DataFrame()
    runner = BigQueryRunner(max_bytes_billed=None)
    runner.execute_query("SELECT 1")
    _, kwargs = mock_client.query.call_args
    assert kwargs["job_config"] is None


def test_execute_query_reraises(mock_client):
    mock_client.query.side_effect = RuntimeError("boom")
    runner = BigQueryRunner()
    with pytest.raises(RuntimeError):
        runner.execute_query("SELECT 1")


def test_get_table_schema_maps_fields(mock_client):
    mock_client.get_table.return_value = SimpleNamespace(schema=[
        SimpleNamespace(name="id", field_type="INTEGER", mode="REQUIRED", description="pk"),
        SimpleNamespace(name="x", field_type="FLOAT", mode="NULLABLE", description=None),
    ])
    runner = BigQueryRunner(dataset_id="ds")
    schema = runner.get_table_schema("orders")

    assert schema[0] == {"name": "id", "type": "INTEGER", "mode": "REQUIRED", "description": "pk"}
    assert schema[1]["description"] == ""
    mock_client.get_table.assert_called_once_with("ds.orders")


def test_get_table_schema_reraises(mock_client):
    mock_client.get_table.side_effect = RuntimeError("nope")
    with pytest.raises(RuntimeError):
        BigQueryRunner().get_table_schema("orders")


def test_list_tables_maps_table_ids(mock_client):
    mock_client.list_tables.return_value = [
        SimpleNamespace(table_id="orders"),
        SimpleNamespace(table_id="users"),
    ]
    runner = BigQueryRunner(dataset_id="ds")
    assert runner.list_tables() == ["orders", "users"]
    mock_client.list_tables.assert_called_once_with("ds")


def test_list_tables_reraises(mock_client):
    mock_client.list_tables.side_effect = RuntimeError("nope")
    with pytest.raises(RuntimeError):
        BigQueryRunner().list_tables()


def test_get_bq_runner_is_cached(monkeypatch):
    made = []

    def _factory(**kwargs):
        made.append(kwargs)
        return MagicMock()

    monkeypatch.setattr(bq_mod, "_BigQueryRunner_singleton", None, raising=False)
    get_bq_runner.cache_clear()
    monkeypatch.setattr(bq_mod, "BigQueryRunner", MagicMock(side_effect=_factory))

    r1 = get_bq_runner()
    r2 = get_bq_runner()

    assert r1 is r2
    assert len(made) == 1
    assert "max_bytes_billed" in made[0]

    get_bq_runner.cache_clear()
