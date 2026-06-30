"""BigQuery runner (given by the customer) + a lazy singleton factory.

The only mandatory extension over the given class is the cost-guard
(``maximum_bytes_billed``), already wired through ``max_bytes_billed`` below
(spec §7.1, §5.3).
"""
import logging
from typing import Any, Dict, List, Optional

import pandas as pd
from google.cloud import bigquery


class BigQueryRunner:
    """A lean BigQuery client for executing SQL queries and returning DataFrame results."""

    def __init__(self, project_id: Optional[str] = None,
                 dataset_id: Optional[str] = "bigquery-public-data.thelook_ecommerce",
                 max_bytes_billed: Optional[int] = None) -> None:
        logging.info("Initializing BigQuery client")
        try:
            self.client = bigquery.Client(project=project_id)
            self.dataset_id = dataset_id
            self.max_bytes_billed = max_bytes_billed  # cost-guard (см. §5.3)
            logging.info(f"BigQuery client initialized for dataset: {self.dataset_id}")
        except Exception as e:
            logging.error(f"Failed to initialize BigQuery client: {str(e)}")
            raise

    def execute_query(self, sql_query: str) -> pd.DataFrame:
        try:
            logging.info("Executing BigQuery query")
            job_config = bigquery.QueryJobConfig(
                maximum_bytes_billed=self.max_bytes_billed
            ) if self.max_bytes_billed else None
            query_job = self.client.query(sql_query, job_config=job_config)
            df = query_job.result().to_dataframe()
            logging.info(f"Query completed successfully, returned {len(df)} rows")
            return df
        except Exception as e:
            logging.error(f"BigQuery execution failed: {str(e)}")
            raise

    def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        try:
            table_ref = f"{self.dataset_id}.{table_name}"
            table = self.client.get_table(table_ref)
            schema_info = [{
                "name": field.name, "type": field.field_type,
                "mode": field.mode, "description": field.description or ""
            } for field in table.schema]
            logging.info(f"Retrieved schema for table {table_name}")
            return schema_info
        except Exception as e:
            logging.error(f"Failed to get schema for table {table_name}: {str(e)}")
            raise


_runner: Optional[BigQueryRunner] = None


def get_bq_runner() -> BigQueryRunner:
    """Return a lazily-created, process-wide BigQueryRunner with the cost-guard set."""
    global _runner
    if _runner is None:
        from app import config

        _runner = BigQueryRunner(
            project_id=config.GCP_PROJECT,
            dataset_id=config.BQ_DATASET,
            max_bytes_billed=config.BQ_MAX_BYTES_BILLED,
        )
    return _runner
