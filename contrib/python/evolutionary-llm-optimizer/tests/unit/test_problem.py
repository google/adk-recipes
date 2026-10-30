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
"""Unit tests for the data, baselines, sandbox and result comparison."""

import datetime
import decimal

import duckdb
import pytest

from app import problem
from app.sandbox import TABLE, run_sql, validate_sql

DATA = str(problem.DATA_PATH)


def test_bundled_sample_matches_documented_shape() -> None:
    rows = duckdb.sql(
        "SELECT COUNT(*), MIN(start_date), MAX(start_date),"
        " COUNT(DISTINCT rental_id) FROM read_parquet(?)",
        params=[DATA],
    ).fetchone()
    assert rows is not None
    count, first, last, unique_ids = rows
    assert count == unique_ids == 60_672
    assert first.date() >= datetime.date(2016, 3, 1)
    assert last.date() <= datetime.date(2016, 5, 31)
    columns = [
        c[0]
        for c in duckdb.sql(
            "DESCRIBE SELECT * FROM read_parquet(?)", params=[DATA]
        ).fetchall()
    ]
    assert columns == [c["name"] for c in problem.SCHEMA]


@pytest.mark.parametrize("query", problem.QUERIES, ids=lambda q: q["id"])
def test_every_baseline_runs_in_both_dialect_templates(query: dict) -> None:
    result = run_sql(query["sql"]["duckdb"].format(table=TABLE), DATA)
    assert result["ok"], result
    assert result["bytes_scanned"] > 0
    assert 0 < len(result["rows"]) <= 20
    assert "{table}" in query["sql"]["bigquery"]
    assert "`{table}`" in query["sql"]["bigquery"]


def test_bytes_scanned_is_reproducible() -> None:
    sql = problem.QUERIES[1]["sql"]["duckdb"].format(table=TABLE)
    first, second = run_sql(sql, DATA), run_sql(sql, DATA)
    assert first["bytes_scanned"] == second["bytes_scanned"]


def test_fewer_columns_and_skippable_filters_scan_less() -> None:
    everything = run_sql("SELECT * FROM cycle_hire", DATA)
    assert not everything["ok"]  # more rows than the result cap
    wide = run_sql(
        "SELECT COUNT(DISTINCT end_station_name) FROM cycle_hire", DATA
    )
    narrow = run_sql("SELECT COUNT(DISTINCT duration) FROM cycle_hire", DATA)
    pruned = run_sql(
        "SELECT COUNT(DISTINCT duration) FROM cycle_hire"
        " WHERE start_date >= TIMESTAMP '2016-05-20'",
        DATA,
    )
    assert (
        wide["bytes_scanned"]
        > narrow["bytes_scanned"]
        > pruned["bytes_scanned"]
    )


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("SELECT 1; SELECT 2", "exactly one statement"),
        ("DELETE FROM cycle_hire", "must be a SELECT, got DELETE"),
        ("COPY cycle_hire TO 'x.csv'", "must be a SELECT, got COPY"),
        ("CREATE TABLE t AS SELECT 1", "must be a SELECT, got CREATE"),
        ("SELEC 1", "does not parse"),
    ],
)
def test_only_single_select_statements_are_accepted(
    sql: str, reason: str
) -> None:
    error = validate_sql(sql)
    assert error is not None and reason in error


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_text('/etc/hostname')",
        "SELECT * FROM read_parquet('/tmp/other.parquet')",
    ],
)
def test_candidates_cannot_read_other_files(sql: str) -> None:
    result = run_sql(sql, DATA)
    assert not result["ok"]
    assert "Permission Error" in result["error"]


def test_slow_query_times_out() -> None:
    result = run_sql(
        "SELECT COUNT(*) FROM cycle_hire a, cycle_hire b, cycle_hire c",
        DATA,
        timeout_seconds=2,
        repeats=1,
    )
    assert not result["ok"]
    assert "timed out" in result["error"] or "exit code" in result["error"]


def test_compare_results_ordered_and_unordered() -> None:
    expected = [["a", 1, 2.0], ["b", 2, 3.25]]
    assert (
        problem.compare_results(expected, [["a", 1, 2], ["b", 2, 3.25]], True)
        is None
    )
    swapped = [["b", 2, 3.25], ["a", 1, 2.0]]
    assert "row 1 differs" in (
        problem.compare_results(expected, swapped, True) or ""
    )
    assert problem.compare_results(expected, swapped, False) is None
    assert "columns" in (
        problem.compare_results(expected, [["a", 1]] * 2, True) or ""
    )
    assert "rows" in (
        problem.compare_results(expected, expected[:1], True) or ""
    )


def test_values_are_normalized_across_engines() -> None:
    assert (
        problem.compare_results(
            [[decimal.Decimal("14.8"), datetime.datetime(2016, 4, 1)]],
            [[14.800000000001, "2016-04-01T00:00:00"]],
            True,
        )
        is None
    )


def test_select_query_uses_fenced_custom_sql() -> None:
    chosen = problem.select_query(
        "Optimize this:\n```sql\nSELECT COUNT(*) FROM cycle_hire ORDER BY 1;\n```",
        "duckdb",
        "cycle_hire",
    )
    assert chosen["id"] == "custom"
    assert chosen["sql"] == "SELECT COUNT(*) FROM cycle_hire ORDER BY 1"
    assert chosen["ordered"]


def test_select_query_fills_the_bigquery_table() -> None:
    chosen = problem.select_query(
        "station demand", "bigquery", problem.DEFAULT_BIGQUERY_TABLE
    )
    assert f"`{problem.DEFAULT_BIGQUERY_TABLE}`" in chosen["sql"]
