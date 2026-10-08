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

"""Tests for `BigQueryTrajectoryStore` (offline, no BigQuery).

A mock client, and assertions on the SQL each method emits, the parameters it
binds and the rows it loads. `test_trajectories_schema.py` holds the same
writer against the table Terraform declares; this file is about what it does
with the records it is given.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest import mock

from ambient_quality_agent.tools.trajectories.bigquery_store import (
    BigQueryTrajectoryStore,
)
from ambient_quality_agent.tools.trajectories.models import (
    IngestStatus,
    Trajectory,
)

_PROJECT = "test-project"
_DATASET = "aqua_insights"
_AGENT = "root_agent"
_TRAJECTORIES_REF = f"{_PROJECT}.{_DATASET}.trajectories"

_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)

# Only the required columns, so the writer's handling of the unset rest shows.
_MINIMAL: dict[str, Any] = {
    "run_id": "run-1",
    "agent_name": _AGENT,
    "trajectory_id": "case-1",
    "created_at": _NOW,
    "ingest_status": IngestStatus.INGESTED,
}


def _store(client: mock.MagicMock) -> BigQueryTrajectoryStore:
    return BigQueryTrajectoryStore(
        client=client, project_id=_PROJECT, dataset=_DATASET, agent_name=_AGENT
    )


def _params(call: Any) -> dict[str, Any]:
    """Extracts bound query parameter names and values from a mock query call.

    Args:
        call: Mock query call object.

    Returns:
        Mapping of parameter names to values.
    """
    return {p.name: p.value for p in call.kwargs["job_config"].query_parameters}


def _record(**overrides: Any) -> Trajectory:
    fields: dict[str, Any] = {
        "run_id": "run-1",
        "agent_name": _AGENT,
        "trajectory_id": "case-1",
        "created_at": _NOW,
        "source": "cloud_ops",
        "ingest_status": IngestStatus.INGESTED,
    }
    return Trajectory(**(fields | overrides))


def test_cleanup_deletes_only_this_run_and_this_agent() -> None:
    """Two agents' sweeps share the table, so a retry that dropped the agent
    filter would erase another agent's run of the same id."""
    client = mock.MagicMock()
    _store(client).delete_run_trajectories("run-1")

    call = client.query.call_args
    sql = call.args[0]
    assert f"DELETE FROM `{_TRAJECTORIES_REF}`" in sql  # noqa: S608 - trusted test constants
    assert "WHERE run_id = @run_id AND agent_name = @agent_name" in sql
    assert _params(call) == {"run_id": "run-1", "agent_name": _AGENT}


def test_cleanup_labels_its_job_for_billing() -> None:
    """BigQuery bills bytes to the job, so AQA's label is the only handle
    Billing gives us on it."""
    client = mock.MagicMock()
    _store(client).delete_run_trajectories("run-1")

    labels = client.query.call_args.kwargs["job_config"].labels
    assert labels.get("component") == "aqa"


def test_record_writes_one_row_per_trace() -> None:
    client = mock.MagicMock()
    _store(client).record(
        [_record(trajectory_id="a"), _record(trajectory_id="b")]
    )

    rows, table = client.load_table_from_json.call_args.args
    assert table == _TRAJECTORIES_REF
    assert [row["trajectory_id"] for row in rows] == ["a", "b"]


def test_record_preserves_trace_id_order() -> None:
    """A multi-turn case links to its first trace -- the conversation's opening
    turn -- so the array is ordered evidence, not a set."""
    client = mock.MagicMock()
    _store(client).record(
        [_record(source_trace_ids=["first", "second", "third"])]
    )

    row = client.load_table_from_json.call_args.args[0][0]
    assert row["source_trace_ids"] == ["first", "second", "third"]


def test_a_trajectory_nothing_could_be_made_of_keeps_its_ids() -> None:
    """The most valuable link on the page: one the run could not ingest is the
    case where "what does this actually look like?" has no other answer."""
    client = mock.MagicMock()
    _store(client).record(
        [
            _record(
                ingest_status=IngestStatus.NOT_INGESTED,
                source_trace_ids=["trace-1"],
            )
        ]
    )

    row = client.load_table_from_json.call_args.args[0][0]
    assert row["ingest_status"] == "not_ingested"
    assert row["source_trace_ids"] == ["trace-1"]


def test_the_ingest_status_is_written_as_its_bare_string() -> None:
    """The column holds the value, not ``IngestStatus.PARTIAL``."""
    client = mock.MagicMock()
    _store(client).record([_record(ingest_status=IngestStatus.PARTIAL)])

    assert (
        client.load_table_from_json.call_args.args[0][0]["ingest_status"]
        == "partial"
    )


def test_a_row_carries_no_analysis_outcome() -> None:
    """The row is the end of ingestion. A sweep runs one analysis today and is
    expected to run several, so an outcome here would mean "whichever wrote
    last"; the run counters hold the aggregate instead. `ingest_status` is the
    one status on the row, and it is ingestion's."""
    client = mock.MagicMock()
    _store(client).record([_record()])

    row = client.load_table_from_json.call_args.args[0][0]
    assert [key for key in row if key.endswith("status")] == ["ingest_status"]
    assert not [key for key in row if "score" in key or "outcome" in key]


def test_record_writes_every_declared_column_even_when_unset() -> None:
    """Absent keys would be filled by the load job's defaults rather than by
    the writer, which is a second place for a column's meaning to live."""
    client = mock.MagicMock()
    _store(client).record([Trajectory(**_MINIMAL)])

    row = client.load_table_from_json.call_args.args[0][0]
    assert set(row) == set(Trajectory.model_fields)
    assert row["source"] is None
    assert row["source_session_id"] is None
    assert row["source_trace_ids"] == []
    assert row["ingest_status"] == "ingested"
