"""BigQuery schema introspection, cached process-wide (spec §3.3).

Both the table list and the per-table schema are read live from BigQuery and
cached via ``lru_cache``; nothing about the dataset's shape is hardcoded. Use
``.cache_clear()`` on the helpers to force a re-fetch.
"""
from __future__ import annotations

from functools import lru_cache

from app import config
from app.sources.bigquery import get_bq_runner


@lru_cache(maxsize=1)
def get_bq_tables() -> tuple:
    """Return (cached) the table names in the BigQuery dataset, fetched live."""
    return tuple(get_bq_runner().list_tables())


def bq_table_list() -> str:
    """Newline-joined fully-qualified table names for the generation prompt."""
    return "\n".join(f"`{config.BQ_DATASET}.{t}`" for t in get_bq_tables())


@lru_cache(maxsize=1)
def get_schema_text() -> str:
    runner = get_bq_runner()
    blocks = []
    for table in get_bq_tables():
        cols = runner.get_table_schema(table)
        lines = [
            f"  - {c['name']} {c['type']} {c['mode']}".rstrip()
            for c in cols
        ]
        blocks.append(
            f"Table `{config.BQ_DATASET}.{table}`:\n" + "\n".join(lines)
        )
    return "\n\n".join(blocks)
