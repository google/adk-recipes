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

"""Holds `schemas/trajectories.json` as the only trajectory schema.

The same treatment `test_insights_schema.py` gives the insight tables: the
writer in `tools/trajectories/store.py` and the model behind it must fit the
table Terraform declares, so a column added on one side alone fails here rather
than in a load job against a deployed dataset.

This table has one property the insight tables do not, and it is checked below:
it is partitioned, which is the one thing BigQuery cannot add to a table
afterwards, so the module and the `bq` guide have to agree on it before
anything is created.

The other thing held here is what the table deliberately does *not* hold. A row
is the state at the end of ingestion, before any analysis has judged the
conversation, so no outcome column may creep back in -- a sweep runs one
analysis today and is expected to run several, and a single outcome column
would come to mean "whichever analysis wrote last".
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.tools.trajectories.bigquery_reader import (
    BigQueryTrajectoryReader,
)
from ambient_quality_agent.tools.trajectories.bigquery_store import (
    BigQueryTrajectoryStore,
)
from ambient_quality_agent.tools.trajectories.models import (
    IngestStatus,
    Trajectory,
    TrajectoryProcessingState,
)
from google.cloud import bigquery
from pydantic import BaseModel, ValidationError

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_DIR = _REPO_ROOT / "terraform" / "modules" / "aqa" / "schemas"
TRAJECTORIES_TF = (
    _REPO_ROOT / "terraform" / "modules" / "aqa" / "trajectories.tf"
)

# Python type the writer has to emit for each declared column type. A TIMESTAMP
# crosses as a string, an ISO-8601 instant.
_PYTHON_TYPE: dict[str, type] = {
    "STRING": str,
    "TIMESTAMP": str,
    "BOOL": bool,
}

_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)

# Only the required columns, so what the model does with the unset rest shows.
_MINIMAL: dict[str, Any] = {
    "run_id": "run-1",
    "agent_name": "root_agent",
    "trajectory_id": "case-1",
    "created_at": _NOW,
    "ingest_status": IngestStatus.INGESTED,
}

_LOWER_CASE_WORD = re.compile(r"\b[a-z_]\w*\b")
_TABLE_REFERENCE = re.compile(r"`([^`]*)`")
_QUALIFIED_COLUMN = re.compile(r"\b(\w+)\.(\w+)\b")

# The reader's aliases. ``o`` is deliberately absent: it names the derived
# subquery that flattens `insight_occurrences.trajectory_ids`, not a table, so
# ``o.trajectory_id`` is that subquery's own output column and there is nothing
# in a schema to hold it to. Its provenance is the UNNEST two lines above it.
_ALIASED_TABLES: dict[str, str] = {"t": "trajectories"}
_NON_TABLE_QUALIFIERS = frozenset({"o"})


def _declaration(table_id: str = "trajectories") -> dict[str, Any]:
    """Loads the schema declaration dictionary for `table_id`.

    Args:
        table_id: Identifier of the table schema to load.

    Returns:
        Parsed schema definition dictionary.
    """
    return json.loads(
        (SCHEMA_DIR / f"{table_id}.json").read_text(encoding="utf-8")
    )


def _terraform_columns(
    table_id: str = "trajectories",
) -> dict[str, tuple[str, str]]:
    """Extracts column definitions declared in Terraform for `table_id`.

    Args:
        table_id: Identifier of the target table.

    Returns:
        Mapping of column names to (type, mode) tuples.
    """
    return {
        column["name"]: (column["type"], column.get("mode", "NULLABLE"))
        for column in _declaration(table_id)["schema"]
    }


def _store(client: Any) -> BigQueryTrajectoryStore:
    return BigQueryTrajectoryStore(
        client=client,
        project_id="test-project",
        dataset="aqua_insights",
        agent_name="root_agent",
    )


def _written_row() -> tuple[dict[str, Any], bigquery.LoadJobConfig]:
    """Runs a trajectory record through a mock BigQuery client.

    Every nullable column is populated so that all column types are exercised.

    Returns:
        Tuple of (loaded row dictionary, BigQuery LoadJobConfig).
    """
    client = mock.MagicMock()
    _store(client).record(
        [
            Trajectory(
                run_id="run-1",
                agent_name="root_agent",
                trajectory_id="case-1",
                created_at=_NOW,
                source="cloud_ops",
                source_session_id="session-1",
                source_trace_ids=["trace-1", "trace-2"],
                ingest_status=IngestStatus.PARTIAL,
            )
        ]
    )
    call = client.load_table_from_json.call_args
    return call.args[0][0], call.kwargs["job_config"]


def _executed_statements() -> list[str]:
    """Collects DML statements executed by BigQueryTrajectoryStore.

    Returns:
        List of SQL query strings.
    """
    client = mock.MagicMock()
    _store(client).delete_run_trajectories("run-1")
    return [call.args[0] for call in client.query.call_args_list]


def _read_statements() -> list[str]:
    """Collects query statements executed by BigQueryTrajectoryReader.

    Returns:
        List of SQL query strings.
    """
    client = mock.MagicMock()
    reader = BigQueryTrajectoryReader(
        client=client,
        project_id="test-project",
        dataset="aqua_insights",
        agent_name="root_agent",
    )
    reader.count_daily_outcomes(
        window_start="2026-05-25T00:00:00Z", window_end="2026-06-01T00:00:00Z"
    )
    reader.list_trajectories(
        run_id="run-1",
        outcome=TrajectoryProcessingState.NOT_INGESTED,
        window_start="2026-05-25T00:00:00Z",
        window_end="2026-06-01T00:00:00Z",
        limit=30,
        offset=0,
    )
    reader.get_trajectories([("run-1", "case-1")])
    return [call.args[0] for call in client.query.call_args_list]


def test_written_columns_fit_the_terraform_schema() -> None:
    columns = _terraform_columns()
    row, _ = _written_row()

    for name, value in row.items():
        assert name in columns, (
            f"trajectories.{name} is written but never declared"
        )
        column_type, mode = columns[name]
        if value is None:
            assert mode == "NULLABLE", (
                f"trajectories.{name} is {mode} but written null"
            )
        elif mode == "REPEATED":
            assert isinstance(value, list), (
                f"trajectories.{name} is REPEATED, written scalar"
            )
            for element in value:
                assert isinstance(element, _PYTHON_TYPE[column_type]), (
                    f"trajectories.{name} holds {column_type}, written {type(element)}"
                )
        else:
            assert isinstance(value, _PYTHON_TYPE[column_type]), (
                f"trajectories.{name} is {column_type} but written as {type(value)}"
            )

    required = {
        name for name, (_, mode) in columns.items() if mode == "REQUIRED"
    }
    assert required <= set(row), (
        f"trajectories never writes {required - set(row)}"
    )


def test_the_model_and_the_schema_hold_the_same_columns() -> None:
    """The writer names its columns one at a time, so a field added to the model
    and left out of that dict is written nowhere and read as null."""
    declared = set(_terraform_columns())
    fields = set(Trajectory.model_fields)

    assert fields == declared, (
        f"Trajectory and trajectories disagree: "
        f"model only {sorted(fields - declared)}, "
        f"table only {sorted(declared - fields)}"
    )


def test_a_repeated_column_is_never_written_null() -> None:
    """BigQuery has no null array: a REPEATED column is empty or it is absent,
    and a load job rejects an explicit null for one."""
    row, _ = _written_row()
    for name, (_, mode) in _terraform_columns().items():
        if mode == "REPEATED":
            assert row[name] is not None, (
                f"trajectories.{name} is REPEATED, written null"
            )


def test_the_writer_cannot_create_or_widen_the_table() -> None:
    """`load_table_from_json` autodetects a schema for a table it cannot find,
    which would leave a deployment running against a table Terraform never
    declared -- and, here, one with no partition, since autodetect builds none."""
    _, job_config = _written_row()

    assert (
        job_config.create_disposition == bigquery.CreateDisposition.CREATE_NEVER
    )
    assert job_config.autodetect is False
    # An append-only history, so a truncating load would replace the whole
    # table with whatever one page of one sweep happened to produce.
    assert (
        job_config.write_disposition == bigquery.WriteDisposition.WRITE_APPEND
    )
    assert not job_config.schema_update_options


def test_an_empty_batch_writes_nothing() -> None:
    """A page that mapped nothing still reaches the writer, and a load job over
    no rows is a billed round trip for no effect."""
    client = mock.MagicMock()
    _store(client).record([])
    client.load_table_from_json.assert_not_called()


def test_the_module_builds_the_declared_partition_and_clustering() -> None:
    """Both are read out of the declaration rather than repeated in HCL, and
    the partition is the property a table cannot be given afterwards."""
    declared = _declaration()
    assert declared["partitioning"] == {"type": "DAY", "field": "created_at"}

    module = TRAJECTORIES_TF.read_text(encoding="utf-8")
    assert "local.trajectories_table.partitioning.type" in module
    assert "local.trajectories_table.partitioning.field" in module
    assert "local.trajectories_table.clustering" in module
    assert "local.trajectories_table.schema" in module


def test_the_deploy_guide_repeats_the_declared_layout() -> None:
    """`bq` reads only the schema out of a definition file, so the guide passes
    the clustering and the partition as flags. That is the one place the layout
    is written twice, and neither can be added to a table after it exists."""
    guide = (_REPO_ROOT / "extension" / "README.md").read_text(encoding="utf-8")
    declared = _declaration()

    command = next(
        (b for b in guide.split("bq mk ") if '.trajectories"' in b), None
    )
    assert command is not None, "the guide never creates trajectories"

    clustering = ",".join(declared["clustering"])
    assert f"--clustering_fields {clustering}" in command, (
        "the guide clusters trajectories on something else"
    )
    assert (
        f"--time_partitioning_type {declared['partitioning']['type']}"
        in command
    )
    assert (
        f"--time_partitioning_field {declared['partitioning']['field']}"
        in command
    )


def test_queried_columns_exist_in_the_terraform_schema() -> None:
    """The DML spells its columns into SQL, where nothing checks them."""
    columns = set(_terraform_columns())
    for sql in _executed_statements():
        assert "SELECT *" not in sql, (
            f"a statement selects every column, in:\n{sql}"
        )

        tables = {
            ref.rsplit(".", 1)[-1] for ref in _TABLE_REFERENCE.findall(sql)
        }
        assert tables == {"trajectories"}, (
            f"statement over {tables or 'no table'}:\n{sql}"
        )

        body = sql
        for noise in (_TABLE_REFERENCE.pattern, r"'[^']*'", r"@\w+"):
            body = re.sub(noise, " ", body)
        read = set(_LOWER_CASE_WORD.findall(body))

        undeclared = read - columns
        assert not undeclared, (
            f"trajectories queried for undeclared {sorted(undeclared)}"
        )
        # Reading nothing means the extraction lost sight of the SQL, leaving
        # this green over unchecked columns.
        assert read, f"no column read out of trajectories, in:\n{sql}"


def test_read_columns_exist_in_the_terraform_schemas() -> None:
    """The reads spell their columns into SQL, where nothing checks them.

    Two tables rather than one: an outcome is a join against the insight
    occurrences, so a rename on *either* side leaves this text asking for a
    column that is gone, and only the deployed query finds out.
    """
    declared = {
        table: set(_terraform_columns(table))
        for table in ("trajectories", "insight_occurrences")
    }
    anywhere = set().union(*declared.values())

    for sql in _read_statements():
        assert "SELECT *" not in sql, f"a read selects every column, in:\n{sql}"

        tables = {
            ref.rsplit(".", 1)[-1] for ref in _TABLE_REFERENCE.findall(sql)
        }
        assert tables <= set(declared), (
            f"read over unknown {tables - set(declared)}"
        )

        body = sql
        for noise in (_TABLE_REFERENCE.pattern, r"'[^']*'", r"@\w+"):
            body = re.sub(noise, " ", body)

        qualified = _QUALIFIED_COLUMN.findall(body)
        for qualifier, name in qualified:
            if qualifier in _NON_TABLE_QUALIFIERS:
                continue
            assert qualifier in _ALIASED_TABLES, (
                f"{qualifier!r} qualifies a column but names no known table, in:\n{sql}"
            )
            table = _ALIASED_TABLES[qualifier]
            assert name in declared[table], (
                f"{table} has no column {name!r}, in:\n{sql}"
            )

        body = _QUALIFIED_COLUMN.sub(" ", body)
        # A name the statement introduces itself is not a column, and it is
        # spelled bare wherever it is reused -- `GROUP BY day, outcome` reads
        # exactly like two columns otherwise.
        introduced = set(re.findall(r"\bAS\s+(\w+)", body))
        body = re.sub(r"\bAS\s+\w+", " ", body)
        body = re.sub(r"\w+\s*\(", " ", body)
        bare = set(_LOWER_CASE_WORD.findall(body)) - _ALIASED_TABLES.keys()
        bare -= _NON_TABLE_QUALIFIERS | introduced

        undeclared = bare - anywhere
        assert not undeclared, (
            f"read asks for undeclared {sorted(undeclared)}, in:\n{sql}"
        )
        # Reading nothing means the extraction lost sight of the SQL, leaving
        # this green over unchecked columns.
        assert qualified or bare, f"no column read at all, in:\n{sql}"


@pytest.mark.parametrize(
    "forbidden", ["eval_status", "status", "score", "outcome"]
)
def test_the_table_records_ingestion_and_not_analysis(forbidden: str) -> None:
    """The row is the state at the end of the fetch step. A sweep runs one
    analysis today and is expected to run several, so a single outcome column
    would come to mean "whichever analysis wrote last"; a per-trajectory result,
    if one is ever kept, belongs in its own table keyed back to this one."""
    assert forbidden not in _terraform_columns()
    assert forbidden not in Trajectory.model_fields


def test_a_trajectory_is_keyed_by_the_conversation_and_the_run_alone() -> None:
    """No evaluation scope in the key. The two scopes name their unit
    differently -- a session id for a reviewed conversation, an invocation or
    trace id for a scored turn -- so one run cannot mint the same
    `trajectory_id` twice and a scope column would only restate that."""
    declared = _terraform_columns()

    assert "metric_type" not in declared
    for column in ("run_id", "trajectory_id"):
        assert declared[column] == ("STRING", "REQUIRED")


def test_the_model_serializes_to_the_values_the_columns_hold() -> None:
    """`Trajectory` crosses the same boundaries the insight models do, so its
    JSON form has to be the stored one: the status as its bare string, and an
    unset array as an empty one rather than as null."""
    record = Trajectory(**_MINIMAL)
    assert isinstance(record, BaseModel)
    dumped = record.model_dump(mode="json")
    assert dumped["source_trace_ids"] == []
    assert dumped["ingest_status"] == "ingested"


def test_what_ingestion_made_of_a_trajectory_is_never_unstated() -> None:
    """REQUIRED and undefaulted, so a trajectory nothing could be made of
    cannot be recorded as one that arrived fine -- which is what a nullable
    bool in this position allowed."""
    assert _terraform_columns()["ingest_status"] == ("STRING", "REQUIRED")
    assert Trajectory.model_fields["ingest_status"].is_required()

    with pytest.raises(ValidationError):
        Trajectory(
            **{k: v for k, v in _MINIMAL.items() if k != "ingest_status"}
        )


@pytest.mark.parametrize("status", list(IngestStatus))
def test_every_ingestion_outcome_the_mapper_can_reach_has_a_value(
    status: IngestStatus,
) -> None:
    """`_map_page` puts every attempted row in one of three buckets -- ingested,
    partial, failed -- and each has to be storable as itself."""
    record = Trajectory(**{**_MINIMAL, "ingest_status": status})

    assert record.model_dump(mode="json")["ingest_status"] == status.value
