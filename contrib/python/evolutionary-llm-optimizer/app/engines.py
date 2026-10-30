# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Query engines that run and meter SQL: DuckDB (default) and BigQuery.

Both return the same shape from `run(sql)`, so the workflow does not care
which one is active:

    {"ok": True, "bytes_scanned": int, "runtime_ms": float,
     "columns": [...], "rows": [[...], ...]}
    {"ok": False, "error": "short, model-readable reason"}

- DuckDB runs locally against the bundled sample, in a separate process (see
  sandbox.py); the score is the bytes actually read from the Parquet file.
- BigQuery runs against a real table (by default the public
  `bigquery-public-data.london_bicycles.cycle_hire`). The score is the
  dry-run `total_bytes_processed`, which is what on-demand pricing bills; the
  query then runs once, capped by `maximum_bytes_billed`, so the result can
  be compared with the baseline's.
"""

import time
from collections.abc import Callable
from typing import Any, Protocol

from . import problem
from .sandbox import TABLE, run_sql

_MAX_RESULT_ROWS = 10_000


class Engine(Protocol):
    """Runs one read-only SELECT and meters what it scans."""

    name: str
    dialect: str
    table: str
    notes: str

    def run(self, sql: str) -> dict[str, Any]: ...


class DuckDBEngine:
    """Runs SQL on the bundled sample with DuckDB, in a sandboxed process."""

    name = "duckdb"
    dialect = "DuckDB SQL"
    table = TABLE
    notes = (
        "DuckDB reads the table from one Parquet file. Only the columns a"
        " query references are read. Rows are sorted by start_date and stored"
        " in row groups of 4,096 rows; a filter on the raw start_date column"
        " lets DuckDB skip row groups by their min/max statistics, but a"
        " filter on an expression of the column (for example a formatted"
        " date) cannot skip anything. Every reference to the table in a FROM"
        " clause is a separate scan. rental_id is unique per row."
    )

    def __init__(self, data_path: str, timeout_seconds: float) -> None:
        self._data_path = data_path
        self._timeout = timeout_seconds

    def run(self, sql: str) -> dict[str, Any]:
        return run_sql(sql, self._data_path, timeout_seconds=self._timeout)


class BigQueryEngine:
    """Runs SQL on a BigQuery table: dry-run for bytes, then one capped run."""

    name = "bigquery"
    dialect = "GoogleSQL (BigQuery)"
    notes = (
        "BigQuery on-demand pricing bills the bytes of every column a query"
        " references, in every scan of the table. LIMIT does not reduce bytes."
        " Filters only prune data on partitioned or clustered tables, and only"
        " when they compare the raw partitioning or clustering column."
        " rental_id is unique per row."
    )

    def __init__(
        self,
        client_factory: Callable[[], Any],
        table: str,
        max_bytes_billed: int,
        timeout_seconds: float,
    ) -> None:
        self._client_factory = client_factory
        self._client: Any = None
        self.table = table
        self._max_bytes_billed = max_bytes_billed
        self._timeout = timeout_seconds

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def run(self, sql: str) -> dict[str, Any]:
        from google.cloud import bigquery

        client = self._get_client()
        try:
            dry = client.query(
                sql,
                job_config=bigquery.QueryJobConfig(
                    dry_run=True, use_query_cache=False
                ),
                timeout=self._timeout,
            )
            if dry.statement_type != "SELECT":
                return {
                    "ok": False,
                    "error": f"must be a SELECT, got {dry.statement_type}",
                }
            scanned = int(dry.total_bytes_processed or 0)
            start = time.perf_counter()
            job = client.query(
                sql,
                job_config=bigquery.QueryJobConfig(
                    maximum_bytes_billed=self._max_bytes_billed
                ),
                timeout=self._timeout,
            )
            result = job.result(
                timeout=self._timeout, max_results=_MAX_RESULT_ROWS + 1
            )
            rows = [list(row.values()) for row in result]
            runtime_ms = round((time.perf_counter() - start) * 1000, 2)
        except Exception as error:  # reported to the model as feedback
            return {"ok": False, "error": str(error).splitlines()[0][:500]}
        if len(rows) > _MAX_RESULT_ROWS:
            return {
                "ok": False,
                "error": f"returns more than {_MAX_RESULT_ROWS} rows",
            }
        return {
            "ok": True,
            "bytes_scanned": scanned,
            "runtime_ms": runtime_ms,
            "columns": [field.name for field in result.schema],
            "rows": rows,
        }


def _bigquery_client(project: str | None) -> Any:
    from google.cloud import bigquery

    return bigquery.Client(project=project)


def create_engine(
    engine: str,
    *,
    timeout_seconds: float,
    bigquery_table: str | None = None,
    bigquery_project: str | None = None,
    max_bytes_billed: int | None = None,
) -> Engine:
    """Builds the engine named by QUERY_ENGINE."""
    if engine == "duckdb":
        return DuckDBEngine(str(problem.DATA_PATH), timeout_seconds)
    if engine == "bigquery":
        if not max_bytes_billed:
            raise RuntimeError(
                "QUERY_ENGINE=bigquery needs BIGQUERY_MAX_BYTES_BILLED."
            )
        return BigQueryEngine(
            lambda: _bigquery_client(bigquery_project),
            bigquery_table or problem.DEFAULT_BIGQUERY_TABLE,
            max_bytes_billed,
            timeout_seconds,
        )
    raise RuntimeError(
        f"Unknown QUERY_ENGINE {engine!r}; use duckdb or bigquery."
    )
