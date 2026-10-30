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
"""Unit tests for the query engines, with a fake BigQuery client."""

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from app import problem
from app.engines import BigQueryEngine, DuckDBEngine, create_engine


class _FakeResult(list):
    schema: ClassVar[list[SimpleNamespace]] = [
        SimpleNamespace(name="station"),
        SimpleNamespace(name="hires"),
    ]


class _FakeClient:
    def __init__(self) -> None:
        self.statement_type = "SELECT"
        self.bytes_by_sql: dict[str, int] = {"SELECT cheap": 1_000}
        self.rows: list[tuple] = [("Hyde Park", 178)]
        self.configs: list[Any] = []
        self.result_calls: list[tuple] = []
        self.fail_with: Exception | None = None

    def query(self, sql: str, job_config: Any, timeout: float) -> Any:
        if self.fail_with:
            raise self.fail_with
        self.configs.append(job_config)
        fake = self

        class Job:
            statement_type = fake.statement_type
            total_bytes_processed = (
                fake.bytes_by_sql.get(sql, 0) if job_config.dry_run else None
            )

            def result(self, timeout: float, max_results: int) -> _FakeResult:
                fake.result_calls.append((timeout, max_results))
                return _FakeResult(
                    SimpleNamespace(values=lambda r=r: r) for r in fake.rows
                )

        return Job()


def _engine(client: _FakeClient) -> BigQueryEngine:
    return BigQueryEngine(
        lambda: client,
        problem.DEFAULT_BIGQUERY_TABLE,
        max_bytes_billed=5_000,
        timeout_seconds=30,
    )


def test_bigquery_scores_dry_run_bytes_and_returns_rows() -> None:
    client = _FakeClient()
    result = _engine(client).run("SELECT cheap")
    assert result["ok"], result
    assert result["bytes_scanned"] == 1_000
    assert result["rows"] == [["Hyde Park", 178]]
    assert result["columns"] == ["station", "hires"]
    dry, real = client.configs
    assert dry.dry_run and not dry.use_query_cache
    assert real.maximum_bytes_billed == 5_000
    assert client.result_calls == [(30, 10_001)]


def test_bigquery_rejects_non_select_statements() -> None:
    client = _FakeClient()
    client.statement_type = "SCRIPT"
    result = _engine(client).run("SELECT 1; DELETE FROM t")
    assert result == {"ok": False, "error": "must be a SELECT, got SCRIPT"}
    assert len(client.configs) == 1  # never executed


def test_bigquery_errors_become_feedback() -> None:
    client = _FakeClient()
    client.fail_with = RuntimeError("400 Unrecognized name: nope\nmore detail")
    result = _engine(client).run("SELECT nope")
    assert result == {"ok": False, "error": "400 Unrecognized name: nope"}


def test_bigquery_caps_result_size() -> None:
    client = _FakeClient()
    client.rows = [("s", 1)] * 10_001
    assert "more than" in _engine(client).run("SELECT cheap")["error"]


def test_create_engine() -> None:
    assert isinstance(create_engine("duckdb", timeout_seconds=5), DuckDBEngine)
    engine = create_engine(
        "bigquery",
        timeout_seconds=5,
        max_bytes_billed=1,
        bigquery_table="p.d.t",
    )
    assert isinstance(engine, BigQueryEngine)
    assert engine.table == "p.d.t"
    with pytest.raises(RuntimeError, match="BIGQUERY_MAX_BYTES_BILLED"):
        create_engine("bigquery", timeout_seconds=5)
    with pytest.raises(RuntimeError, match="Unknown QUERY_ENGINE"):
        create_engine("spark", timeout_seconds=5)
