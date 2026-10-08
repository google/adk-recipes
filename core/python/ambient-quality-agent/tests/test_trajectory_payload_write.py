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

"""`BigQueryTrajectoryStore.record_payloads`: the archive half (offline, no BigQuery).

A mock client, and assertions on the loads it issues. `test_trajectory_store.py`
covers the index half of the same class, and
`test_trajectory_payloads_schema.py` holds this writer against the tables
Terraform declares.

The write appends and compares nothing, so what is worth holding here is what
that buys and what it must still cost nothing:

* nothing is read back, so no write can destroy a copy somebody else stored,
  and no statement mentions the run;
* the turns are written **before** the manifest, which is the only thing
  between a failed page and a manifest promising rows that are not there;
* a copy is self-consistent through ``created_at``, which is what lets a reader
  pick one copy and get that copy's turns rather than a mixture of two;
* every copy carries the ``created_at`` that identifies it, which is the only
  thing `get_archives` has to reconcile them with.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.tools.trajectories import bigquery_store as store_mod
from ambient_quality_agent.tools.trajectories.bigquery_store import (
    PAYLOADS_TABLE,
    TURNS_TABLE,
    BigQueryTrajectoryStore,
)
from ambient_quality_agent.tools.trajectories.models import (
    PayloadStatus,
    TrajectoryPayload,
    TrajectoryPayloadTurn,
)
from ambient_quality_agent.tools.trajectories.payloads import TrajectoryArchive

_PROJECT = "test-project"
_DATASET = "aqua_insights"
_AGENT = "root_agent"

_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry's sleep, removed: these tests exercise the loop, not the wait."""
    monkeypatch.setattr(store_mod.time, "sleep", lambda _seconds: None)


def _store(client: mock.MagicMock) -> BigQueryTrajectoryStore:
    return BigQueryTrajectoryStore(
        client=client, project_id=_PROJECT, dataset=_DATASET, agent_name=_AGENT
    )


def _archive(
    trajectory_id: str = "session-abc",
    *,
    turns: int = 2,
    status: PayloadStatus = PayloadStatus.INGESTED,
    created_at: dt.datetime = _NOW,
) -> TrajectoryArchive:
    return TrajectoryArchive(
        payload=TrajectoryPayload(
            agent_name=_AGENT,
            trajectory_id=trajectory_id,
            created_at=created_at,
            status=status,
            turn_count=turns,
            agents={_AGENT: {"instruction": "be helpful"}},
        ),
        turns=tuple(
            TrajectoryPayloadTurn(
                agent_name=_AGENT,
                trajectory_id=trajectory_id,
                turn_index=index,
                created_at=created_at,
                turn={"turn_index": index, "events": [{"author": "user"}]},
            )
            for index in range(turns)
        ),
    )


def _loads(client: mock.MagicMock) -> list[tuple[str, list[dict[str, Any]]]]:
    """Extracts load calls issued by the store in order of execution.

    Args:
        client: Mock BigQuery client with recorded load calls.

    Returns:
        List of (table_name, rows_list) tuples.
    """
    return [
        (call.args[1].rsplit(".", 1)[-1], call.args[0])
        for call in client.load_table_from_json.call_args_list
    ]


def _rows(client: mock.MagicMock, table: str) -> list[dict[str, Any]]:
    return next(rows for name, rows in _loads(client) if name == table)


def _configs(client: mock.MagicMock) -> list[Any]:
    return [
        call.kwargs["job_config"]
        for call in client.load_table_from_json.call_args_list
    ]


def test_an_empty_batch_writes_nothing() -> None:
    """A page that assembled no case still reaches the writer, and a load job
    over no rows is a billed round trip for no effect."""
    client = mock.MagicMock()

    _store(client).record_payloads([])

    client.load_table_from_json.assert_not_called()
    client.query.assert_not_called()


def test_a_page_is_two_loads_and_nothing_else() -> None:
    """No staging table, no DDL, no transaction, and no read of what is already
    there: the whole write is one append per table."""
    client = mock.MagicMock()

    _store(client).record_payloads([_archive("a"), _archive("b")])

    assert [table for table, _ in _loads(client)] == [
        TURNS_TABLE,
        PAYLOADS_TABLE,
    ]
    client.query.assert_not_called()


def test_the_turns_are_written_before_the_manifest() -> None:
    """The only ordering that makes a half-written page harmless. A manifest
    without its turns promises rows that are not there; turns without their
    manifest are unreachable -- a reader starts from the manifest -- and expire
    with their partition."""
    client = mock.MagicMock()

    _store(client).record_payloads([_archive()])

    assert [table for table, _ in _loads(client)] == [
        TURNS_TABLE,
        PAYLOADS_TABLE,
    ]


def test_a_page_of_conversations_is_one_load_per_table() -> None:
    """The page is the unit ingestion produces. A load per conversation would
    spend the 1,500-a-table-a-day quota on a single sweep."""
    client = mock.MagicMock()

    _store(client).record_payloads(
        [_archive("a"), _archive("b"), _archive("c")]
    )

    assert client.load_table_from_json.call_count == 2
    assert len(_rows(client, PAYLOADS_TABLE)) == 3
    assert len(_rows(client, TURNS_TABLE)) == 6


def test_nothing_is_read_back_before_it_is_written() -> None:
    """The property that replaces the staging table: a write that compares
    nothing cannot compare wrongly and destroy a stored conversation."""
    client = mock.MagicMock()

    _store(client).record_payloads([_archive()])

    client.query.assert_not_called()
    assert all(
        config.write_disposition == "WRITE_APPEND"
        for config in _configs(client)
    )


def test_the_payload_write_names_nothing_the_run_owns() -> None:
    """A copy is referenced by every sweep that sampled the conversation, so
    there is no run to attribute it to and none to delete it by."""
    client = mock.MagicMock()

    _store(client).record_payloads([_archive()])

    for _, rows in _loads(client):
        assert all("run_id" not in row for row in rows)


def test_the_run_cleanup_leaves_the_conversations_alone() -> None:
    """The one hazard of one store covering both halves: `delete_run_trajectories` erases
    the index rows of a retried run, and reaching either payload table with it
    would destroy copies other sweeps still point at."""
    client = mock.MagicMock()

    _store(client).delete_run_trajectories("run-1")

    deleted = client.query.call_args.args[0]
    assert PAYLOADS_TABLE not in deleted
    assert TURNS_TABLE not in deleted


def test_a_copy_is_stamped_so_a_reader_can_keep_it_together() -> None:
    """`created_at` is what tells two copies of one conversation apart, and a
    manifest shares it with its own turns -- so a reader that picks a copy
    joins its turns on that column and gets that copy, not a mixture."""
    client = mock.MagicMock()
    later = dt.datetime(2026, 6, 8, tzinfo=dt.UTC)

    _store(client).record_payloads([_archive(created_at=later)])

    stamps = {row["created_at"] for _, rows in _loads(client) for row in rows}
    assert stamps == {later.isoformat()}


def test_the_json_columns_are_loaded_as_objects() -> None:
    """The client serializes the row it is handed, so text here would land as a
    JSON string scalar and `turn.events` would read nothing."""
    client = mock.MagicMock()

    _store(client).record_payloads([_archive()])

    assert isinstance(_rows(client, PAYLOADS_TABLE)[0]["agents"], dict)
    assert isinstance(_rows(client, TURNS_TABLE)[0]["turn"], dict)


def test_the_writer_cannot_create_or_widen_a_table() -> None:
    """`load_table_from_json` autodetects a schema for a table it cannot find,
    which would leave a deployment writing to one Terraform never declared."""
    client = mock.MagicMock()

    _store(client).record_payloads([_archive()])

    for config in _configs(client):
        assert config.create_disposition == "CREATE_NEVER"
        assert config.autodetect is False


def test_a_transient_failure_is_retried() -> None:
    """Safe because a load job commits every row or none: a load that failed
    wrote nothing for the retry to duplicate."""
    client = mock.MagicMock()
    client.load_table_from_json.side_effect = [
        RuntimeError("backend error"),
        mock.DEFAULT,
        mock.DEFAULT,
    ]

    _store(client).record_payloads([_archive()])

    assert client.load_table_from_json.call_count == 3


def test_a_write_that_will_not_go_through_fails_the_sweep() -> None:
    """A sweep that could not archive what it judged has failed, and saying so
    beats leaving an insight pointing at evidence nobody stored."""
    client = mock.MagicMock()
    client.load_table_from_json.side_effect = RuntimeError("backend error")

    with pytest.raises(RuntimeError, match="backend error"):
        _store(client).record_payloads([_archive()])


def test_a_manifest_that_will_not_load_leaves_no_promise_behind() -> None:
    """The turns are already stored and stay stored, but nothing reaches them
    without a manifest -- so the page is invisible rather than half true, and
    the retried run writes fresh copies under a new `created_at`."""
    client = mock.MagicMock()
    client.load_table_from_json.side_effect = [mock.DEFAULT] + [
        RuntimeError("manifest failed")
    ] * 3

    with pytest.raises(RuntimeError, match="manifest failed"):
        _store(client).record_payloads([_archive()])

    assert [table for table, _ in _loads(client)] == [TURNS_TABLE] + [
        PAYLOADS_TABLE
    ] * 3
