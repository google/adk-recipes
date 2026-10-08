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

"""Tests for `RootCauseStore` (offline, no BigQuery).

A mock client stands in for the load job, so what is under test is the row the
writer builds and the job it configures. `tests/test_insights_schema.py` holds
that row against the declared table.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.tools.insights.models import RootCause
from ambient_quality_agent.tools.insights.root_cause_store import (
    ROOT_CAUSES_TABLE,
    RootCauseStore,
    RootCauseWriter,
)
from google.cloud import bigquery

_PROJECT = "test-project"
_DATASET = "aqua_insights"
_NOW = dt.datetime(2026, 6, 1, 12, 30, tzinfo=dt.UTC)


def _store(client: mock.MagicMock) -> RootCauseStore:
    return RootCauseStore(client=client, project_id=_PROJECT, dataset=_DATASET)


def _saved(
    client: mock.MagicMock,
) -> tuple[dict[str, Any], str, bigquery.LoadJobConfig]:
    """Extracts the saved row, destination table, and load config from a mock client call.

    Args:
        client: Mock BigQuery client with a recorded load call.

    Returns:
        Tuple of (inserted row dict, destination table string, load job config).
    """
    call = client.load_table_from_json.call_args
    rows, table = call.args
    assert len(rows) == 1, "one record is one row"
    return rows[0], table, call.kwargs["job_config"]


def test_the_store_satisfies_the_writer_protocol(
    golden_root_cause: RootCause,
) -> None:
    """Verifies `RootCauseStore` implements `RootCauseWriter`."""
    writer: RootCauseWriter = _store(mock.MagicMock())
    writer.save(golden_root_cause)


def test_save_writes_the_record_to_the_declared_table(
    golden_root_cause: RootCause,
) -> None:
    client = mock.MagicMock()

    _store(client).save(golden_root_cause)

    row, table, _ = _saved(client)
    assert table == f"{_PROJECT}.{_DATASET}.{ROOT_CAUSES_TABLE}"
    assert row["root_cause_id"] == golden_root_cause.root_cause_id
    assert row["insight_id"] == golden_root_cause.insight_id
    assert row["occurrence_id"] == golden_root_cause.occurrence_id
    assert row["agent_revision"] == golden_root_cause.agent_revision
    assert row["summary"] == golden_root_cause.summary


def test_save_writes_the_edits_as_a_json_array(
    golden_root_cause: RootCause,
) -> None:
    """Verifies edits persist as a native list so BigQuery loads a queryable JSON array."""
    client = mock.MagicMock()

    _store(client).save(golden_root_cause)

    row, _, _ = _saved(client)
    edits = row["edits"]
    assert isinstance(edits, list)
    assert [e["path"] for e in edits] == [
        e.path for e in golden_root_cause.edits
    ]
    # Stored before/after code allows the UI to render diffs without additional lookups.
    assert edits[0]["before"] == golden_root_cause.edits[0].before
    assert edits[0]["after"] == golden_root_cause.edits[0].after


def test_a_record_with_no_edits_writes_an_empty_json_array() -> None:
    """Verifies diagnoses without code changes persist edits as an empty JSON array."""
    client = mock.MagicMock()

    _store(client).save(
        RootCause(
            root_cause_id="rc-1",
            insight_id="ins-1",
            occurrence_id="occ-1",
            agent_revision="rev-1",
            summary="The tool is called correctly; the backend rejects the order.",
            created_at=_NOW,
        )
    )

    row, _, _ = _saved(client)
    assert row["edits"] == []


def test_save_writes_the_timestamp_as_an_iso_instant() -> None:
    client = mock.MagicMock()

    _store(client).save(
        RootCause(
            root_cause_id="rc-1",
            insight_id="ins-1",
            occurrence_id="occ-1",
            agent_revision="rev-1",
            summary="s",
            created_at=_NOW,
        )
    )

    row, _, _ = _saved(client)
    assert row["created_at"] == "2026-06-01T12:30:00+00:00"


def test_the_writer_appends_and_cannot_create_or_widen_the_table(
    golden_root_cause: RootCause,
) -> None:
    """Verifies load job config enforces WRITE_APPEND, CREATE_NEVER, and no autodetect."""
    client = mock.MagicMock()

    _store(client).save(golden_root_cause)

    _, _, job_config = _saved(client)
    assert (
        job_config.write_disposition == bigquery.WriteDisposition.WRITE_APPEND
    )
    assert (
        job_config.create_disposition == bigquery.CreateDisposition.CREATE_NEVER
    )
    assert job_config.autodetect is False
    assert not job_config.schema_update_options


def test_save_waits_for_the_load_to_finish(
    golden_root_cause: RootCause,
) -> None:
    """Verifies `save` awaits the load job completion before returning."""
    client = mock.MagicMock()

    _store(client).save(golden_root_cause)

    client.load_table_from_json.return_value.result.assert_called_once()


def test_a_failed_load_reaches_the_caller(golden_root_cause: RootCause) -> None:
    client = mock.MagicMock()
    client.load_table_from_json.return_value.result.side_effect = RuntimeError(
        "boom"
    )

    with pytest.raises(RuntimeError, match="boom"):
        _store(client).save(golden_root_cause)
