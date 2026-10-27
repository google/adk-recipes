# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Holds `terraform/modules/aqa/schemas` as the only investigations schema.

The same guard `test_insights_schema` puts on the insight tables, for the two
the run registry uses. A record's fields are its table's columns one for one, so
these tests are mostly one assertion made three ways: the model agrees with the
declaration, the rows the writer builds fit it, and the statements select
nothing it does not declare. Drift that gets past them only surfaces as a failed
load against a deployed dataset.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.tools.investigations.bigquery_store import (
    EVENTS_TABLE,
    INVESTIGATIONS_TABLE,
    BigQueryInvestigationStore,
)
from ambient_quality_agent.tools.investigations.models import (
    EVENTS_FIELD,
    CustomOverrides,
    InvestigationEvent,
    InvestigationRecord,
    RunStatus,
    list_event_columns,
    list_snapshot_columns,
)
from google.cloud import bigquery
from pydantic import BaseModel

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCHEMA_DIR = _REPO_ROOT / "terraform" / "modules" / "aqa" / "schemas"

# Python type the writer has to emit for each declared column type. A TIMESTAMP
# and a JSON column both cross as strings: an ISO-8601 instant and a serialized
# payload.
_PYTHON_TYPE: dict[str, type] = {
    "STRING": str,
    "TIMESTAMP": str,
    "INT64": int,
    "BOOL": bool,
    "JSON": str,
}

_TABLES = (INVESTIGATIONS_TABLE, EVENTS_TABLE)


def _declaration(table_id: str) -> dict[str, Any]:
    path = _SCHEMA_DIR / f"{table_id}.json"
    assert path.exists(), f"no table {table_id} declared in {_SCHEMA_DIR}"
    return json.loads(path.read_text(encoding="utf-8"))


def _columns(table_id: str) -> dict[str, tuple[str, str]]:
    """Extracts column definitions declared by Terraform for a table.

    Args:
        table_id: Short table identifier.

    Returns:
        Mapping of column name to `(type, mode)` tuple.
    """
    return {
        column["name"]: (column["type"], column.get("mode", "NULLABLE"))
        for column in _declaration(table_id)["schema"]
    }


def _full_record() -> InvestigationRecord:
    """Constructs an InvestigationRecord with all fields populated.

    Returns:
        InvestigationRecord instance with non-null values across all columns.
    """
    return InvestigationRecord(
        run_id="r1",
        created_at="2026-06-01T00:00:00+00:00",
        updated_at="2026-06-01T00:30:00+00:00",
        observed_agent_name="watched",
        trigger_type="scheduled",
        window_start="2026-06-01T00:00:00+00:00",
        window_end="2026-06-01T01:00:00+00:00",
        budget_per_metric=10,
        metrics={"multi_turn": ["task_success"]},
        dry_run=True,
        custom_overrides=CustomOverrides(
            selector_sql="SELECT session_id FROM t", session_review_focus="tone"
        ),
        idempotency_key="k-1",
        effective_config={"data_lookback_window": 7},
        due_at="2026-06-01T00:15:00+00:00",
        status=RunStatus.DONE,
        job_name="jobs/1",
        finished_at="2026-06-01T01:00:00+00:00",
        error="boom",
        summary={"a": 1},
    )


def _written_rows() -> dict[str, list[dict[str, Any]]]:
    """Simulates a full lifecycle and captures rows written to each table.

    Returns:
        Mapping of short table name to list of loaded row dictionaries.
    """
    client = mock.MagicMock()
    store = BigQueryInvestigationStore(
        client=client, project_id="p", dataset="d"
    )
    store.append(_full_record())
    store.append_event("r1", "**step**\n", source="init")

    written: dict[str, list[dict[str, Any]]] = {table: [] for table in _TABLES}
    for call in client.load_table_from_json.call_args_list:
        written[call.args[1].rsplit(".", 1)[-1]].extend(call.args[0])
    return written


def _job_configs() -> list[bigquery.LoadJobConfig]:
    client = mock.MagicMock()
    store = BigQueryInvestigationStore(
        client=client, project_id="p", dataset="d"
    )
    store.append(InvestigationRecord(run_id="r1"))
    store.append_event("r1", "x")
    return [
        call.kwargs["job_config"]
        for call in client.load_table_from_json.call_args_list
    ]


def _selected_columns() -> dict[str, set[str]]:
    """Executes query methods and collects column names referenced in SQL statements.

    Every read is driven through a mock client so what is under test is the
    SQL text alone; the column lists come from the models, and this is what
    checks those against the declaration.

    Returns:
        Mapping of short table name to set of column names queried.
    """
    client = mock.MagicMock()
    # The snapshot read has to find something, or `get` returns before it ever
    # asks the events table and this would check nothing.
    client.query.return_value.result.side_effect = [
        [
            {
                "run_id": "r1",
                "created_at": dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
            }
        ],
        [],
        [],
        [],
        [],
        [],
    ]
    store = BigQueryInvestigationStore(
        client=client, project_id="p", dataset="d"
    )
    store.get("r1")
    store.list_recent()
    store.sum_counters()
    store.list_stale_pending(lease_minutes=30)
    store.list_overdue_scheduled(grace_minutes=60)

    selected: dict[str, set[str]] = {table: set() for table in _TABLES}
    for call in client.query.call_args_list:
        sql = call.args[0]
        table = (
            EVENTS_TABLE if f".{EVENTS_TABLE}`" in sql else INVESTIGATIONS_TABLE
        )
        columns = (
            list_event_columns()
            if table == EVENTS_TABLE
            else list_snapshot_columns()
        )
        selected[table] |= {c for c in columns if c in sql}
    return selected


@pytest.mark.parametrize(
    ("table_id", "model"),
    [
        (INVESTIGATIONS_TABLE, InvestigationRecord),
        (EVENTS_TABLE, InvestigationEvent),
    ],
)
def test_the_model_and_the_schema_hold_the_same_columns(
    table_id: str, model: type[BaseModel]
) -> None:
    """The models are the column lists, so this is the check that matters: a
    field with no column behind it is dropped on write and read back as its
    default forever, silently."""
    declared = set(_columns(table_id))
    fields = set(model.model_fields) - {EVENTS_FIELD}

    assert fields == declared, (
        f"{model.__name__} and {table_id} disagree: "
        f"model only {sorted(fields - declared)}, table only {sorted(declared - fields)}"
    )


@pytest.mark.parametrize("table_id", _TABLES)
def test_written_columns_fit_the_terraform_schema(table_id: str) -> None:
    columns = _columns(table_id)
    required = {
        name for name, (_, mode) in columns.items() if mode == "REQUIRED"
    }
    rows = _written_rows()[table_id]
    assert rows, f"the lifecycle never writes to {table_id}"

    for row in rows:
        for name, value in row.items():
            assert name in columns, (
                f"{table_id}.{name} is written but not declared"
            )
            column_type, mode = columns[name]
            if value is None:
                assert mode == "NULLABLE", f"{name} is {mode} but written null"
            else:
                assert isinstance(value, _PYTHON_TYPE[column_type]), (
                    f"{name} is {column_type} but written as {type(value)}"
                )
        assert required <= set(row), (
            f"a row never writes {sorted(required - set(row))}"
        )


def test_a_full_run_writes_every_declared_column() -> None:
    """A column no writer ever fills is dead weight in the table definition."""
    written = {
        name for row in _written_rows()[INVESTIGATIONS_TABLE] for name in row
    }
    assert set(_columns(INVESTIGATIONS_TABLE)) == written


@pytest.mark.parametrize("table_id", _TABLES)
def test_selected_columns_exist_in_the_terraform_schema(table_id: str) -> None:
    selected = _selected_columns()[table_id]
    assert selected, f"no read names a column of {table_id}"
    undeclared = selected - set(_columns(table_id))
    assert not undeclared, f"a read selects undeclared {sorted(undeclared)}"


def test_the_writer_cannot_create_or_widen_a_table() -> None:
    """`load_table_from_json` autodetects a schema for a table it cannot find,
    which would leave a deployment running against a table Terraform never
    declared."""
    for job_config in _job_configs():
        assert (
            job_config.create_disposition
            == bigquery.CreateDisposition.CREATE_NEVER
        )
        assert job_config.autodetect is False
        # Both tables are append-only histories: a truncating load would drop
        # every run but the one being written.
        assert (
            job_config.write_disposition
            == bigquery.WriteDisposition.WRITE_APPEND
        )
        assert not job_config.schema_update_options


@pytest.mark.parametrize("table_id", _TABLES)
def test_the_deploy_guide_repeats_the_declared_layout(table_id: str) -> None:
    """`bq` reads only the schema out of a definition file, so the guide passes
    the clustering as a flag. That is the one place the layout is written twice,
    and a table gains clustering only when it is created."""
    guide = (_REPO_ROOT / "extension" / "README.md").read_text(encoding="utf-8")
    declared = _declaration(table_id)

    command = next(
        (b for b in guide.split("bq mk ") if f'.{table_id}"' in b), None
    )
    assert command is not None, f"the guide never creates {table_id}"
    assert f"--clustering_fields {','.join(declared['clustering'])}" in command

    # Neither table is partitioned: every read filters on `run_id` and none
    # bounds itself by time, so a partition would never be pruned. Keep the
    # guide and the module agreeing on that -- partitioning cannot be added to
    # a table afterwards, and only one of the two would grow it.
    assert "partition_field" not in declared, (
        f"{table_id} declares an unconfigured partition"
    )
    assert "--time_partitioning" not in command, (
        f"the guide partitions {table_id}, the definition does not"
    )


def test_a_timestamp_column_round_trips_through_the_record() -> None:
    """BigQuery hands back a datetime and takes ISO text, and the models sit in
    between; a validator dropped on one of those fields breaks the write, not
    the read, so check the whole loop."""
    client = mock.MagicMock()
    store = BigQueryInvestigationStore(
        client=client, project_id="p", dataset="d"
    )
    read_back = InvestigationRecord.model_validate(
        {"run_id": "r1", "created_at": dt.datetime(2026, 6, 1, tzinfo=dt.UTC)}
    )
    store.append(read_back)

    (row,) = _loaded(client)
    assert row["created_at"] == "2026-06-01T00:00:00+00:00"


def test_a_json_column_round_trips_through_the_record() -> None:
    client = mock.MagicMock()
    store = BigQueryInvestigationStore(
        client=client, project_id="p", dataset="d"
    )
    read_back = InvestigationRecord.model_validate(
        {"run_id": "r1", "summary": '{"metrics_passed": 3}'}
    )
    store.append(read_back)

    (row,) = _loaded(client)
    assert json.loads(row["summary"]) == {"metrics_passed": 3}


def _loaded(client: mock.MagicMock) -> list[dict[str, Any]]:
    return [
        row
        for call in client.load_table_from_json.call_args_list
        for row in call.args[0]
    ]


def test_the_overrides_round_trip_as_one_json_column() -> None:
    """Verify custom overrides serialize to JSON and deserialize back to CustomOverrides."""
    client = mock.MagicMock()
    store = BigQueryInvestigationStore(
        client=client, project_id="p", dataset="d"
    )
    overrides = CustomOverrides(
        selector_sql="SELECT session_id FROM t", session_review_focus="refunds"
    )

    store.append(InvestigationRecord(run_id="r1", custom_overrides=overrides))

    (row,) = _loaded(client)
    assert json.loads(row["custom_overrides"]) == overrides.model_dump()
    read_back = InvestigationRecord.model_validate(
        {"run_id": "r1", "custom_overrides": row["custom_overrides"]}
    )
    assert read_back.custom_overrides == overrides


def test_an_ambient_sweep_writes_no_overrides_column() -> None:
    """Verify runs without custom overrides omit the custom_overrides column on write."""
    client = mock.MagicMock()
    store = BigQueryInvestigationStore(
        client=client, project_id="p", dataset="d"
    )

    store.append(InvestigationRecord(run_id="r1"))

    (row,) = _loaded(client)
    assert "custom_overrides" not in row
    assert (
        InvestigationRecord.model_validate({"run_id": "r1"}).custom_overrides
        is None
    )
