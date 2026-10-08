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

"""`BigQueryTrajectoryReader.get_archives`: reading an archived conversation back.

The pair of `test_trajectory_payload_write.py`. The write appends and compares
nothing, so everything that keeps the archive coherent happens here, and this is
where it is held:

* one copy is chosen per conversation, preferring a whole one over a later
  lossy one, so a re-sample cannot downgrade what a reader sees;
* the turns come from **that** copy and not from a mixture of two, which is
  what joining on ``created_at`` buys;
* duplicate turn rows are collapsed, since an append-only write cannot enforce
  a key;
* a conversation nobody archived is absent and one whose turns have expired is
  empty, and those are different answers.

Offline: a mock client, and assertions on the SQL and on what is built from the
rows it returns.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.tools.trajectories.bigquery_reader import (
    PAYLOAD_PREFERENCE,
    BigQueryTrajectoryReader,
)
from ambient_quality_agent.tools.trajectories.models import PayloadStatus

_PROJECT = "test-project"
_DATASET = "aqua_insights"
_AGENT = "root_agent"
_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


def _reader(client: mock.MagicMock) -> BigQueryTrajectoryReader:
    return BigQueryTrajectoryReader(
        client=client, project_id=_PROJECT, dataset=_DATASET, agent_name=_AGENT
    )


def _row(
    trajectory_id: str = "session-abc",
    *,
    turn_index: int | None = 0,
    status: PayloadStatus = PayloadStatus.INGESTED,
    turn_count: int = 1,
    created_at: dt.datetime = _NOW,
) -> dict[str, Any]:
    """Generates a mock joined row containing manifest and turn columns.

    Args:
        trajectory_id: Trajectory identifier string.
        turn_index: Optional index of the turn.
        status: Payload status enum value.
        turn_count: Total turn count in the manifest.
        created_at: Timestamp for the row.

    Returns:
        Row dictionary representing BigQuery query output.
    """
    return {
        "agent_name": _AGENT,
        "trajectory_id": trajectory_id,
        "created_at": created_at,
        "status": status.value,
        "turn_count": turn_count,
        "agents": {_AGENT: {"instruction": "be helpful"}},
        "turn_index": turn_index,
        "turn": None
        if turn_index is None
        else {"turn_index": turn_index, "events": [{"author": "user"}]},
    }


def _returning(rows: list[dict[str, Any]]) -> mock.MagicMock:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = rows
    return client


def _sql(client: mock.MagicMock) -> str:
    return client.query.call_args.args[0]


def _params(client: mock.MagicMock) -> dict[str, Any]:
    return {
        p.name: getattr(p, "value", None)
        if hasattr(p, "value")
        else list(p.values)
        for p in client.query.call_args.kwargs["job_config"].query_parameters
    }


def test_asking_for_nothing_queries_nothing() -> None:
    """A page of occurrences that named no trajectory should not cost a billed
    round trip."""
    client = mock.MagicMock()

    assert _reader(client).get_archives([]) == {}
    client.query.assert_not_called()


def test_a_conversation_comes_back_as_its_manifest_and_its_turns() -> None:
    client = _returning([_row(turn_index=0, turn_count=2), _row(turn_index=1)])

    archives = _reader(client).get_archives(["session-abc"])

    archive = archives["session-abc"]
    assert archive.payload.trajectory_id == "session-abc"
    assert archive.payload.status is PayloadStatus.INGESTED
    assert [turn.turn_index for turn in archive.turns] == [0, 1]


def test_the_turn_json_needs_no_decoding() -> None:
    """The client parses a JSON column on the way out, which is only true
    because the writer stored an object rather than text of one."""
    client = _returning([_row()])

    archive = _reader(client).get_archives(["session-abc"])["session-abc"]

    assert archive.turns[0].turn == {
        "turn_index": 0,
        "events": [{"author": "user"}],
    }
    assert archive.payload.agents == {_AGENT: {"instruction": "be helpful"}}


def test_one_copy_is_chosen_per_conversation() -> None:
    """The write appends, so `(agent_name, trajectory_id)` can hold several
    copies. Without this the same conversation would come back more than once,
    or worse, as two copies' turns interleaved."""
    sql = _sql(_queried(["session-abc"]))

    assert "QUALIFY ROW_NUMBER() OVER (" in sql
    assert "PARTITION BY agent_name, trajectory_id" in sql
    assert PAYLOAD_PREFERENCE in sql


def test_the_preference_takes_a_whole_copy_over_a_later_lossy_one() -> None:
    """A conversation re-sampled later is reassembled from telemetry that has
    had longer to age out, so a plain "latest wins" would downgrade more often
    than it repaired."""
    assert PAYLOAD_PREFERENCE.index(
        "status = 'ingested'"
    ) < PAYLOAD_PREFERENCE.index("created_at")
    assert "created_at ASC" in PAYLOAD_PREFERENCE


def test_the_turns_are_joined_to_the_copy_that_was_chosen() -> None:
    """`created_at` is what makes a copy self-consistent. Joining without it
    would attach every copy's turns to whichever manifest won, and the
    conversation would read as two conversations spliced together."""
    sql = _sql(_queried(["session-abc"]))

    assert "USING (agent_name, trajectory_id, created_at)" in sql


def test_duplicate_turn_rows_are_collapsed() -> None:
    """An append-only write cannot enforce a key, and the load is retried: one
    that timed out after committing would leave every turn of that copy stored
    twice."""
    sql = _sql(_queried(["session-abc"]))

    assert (
        "PARTITION BY agent_name, trajectory_id, created_at, turn_index" in sql
    )


def test_a_conversation_nobody_archived_is_absent() -> None:
    """Distinct from one archived with no turns: the first says the store never
    saw it, the second that it held nothing. A placeholder would merge them."""
    client = _returning([])

    assert _reader(client).get_archives(["gone"]) == {}


def test_a_conversation_whose_turns_expired_keeps_its_manifest() -> None:
    """The degraded state the read path is expected to show rather than fail
    on -- the UI can still say what was sampled and that the copy is gone."""
    client = _returning([_row(turn_index=None, turn_count=3)])

    archive = _reader(client).get_archives(["session-abc"])["session-abc"]

    assert archive.turns == ()
    assert archive.payload.turn_count == 3


def test_several_conversations_come_back_from_one_query() -> None:
    """A page of occurrences names many, and one query per conversation would
    make the insight detail pay a round trip per piece of evidence."""
    client = _returning(
        [
            _row("a", turn_index=0),
            _row("b", turn_index=0),
            _row("b", turn_index=1),
        ]
    )

    archives = _reader(client).get_archives(["a", "b"])

    assert set(archives) == {"a", "b"}
    assert len(archives["b"].turns) == 2
    assert client.query.call_count == 1


def test_the_read_is_scoped_to_one_agent_and_the_ids_asked_for() -> None:
    """`agent_name` is the first part of both keys, and a reader that dropped
    it would resolve another agent's conversation of the same id."""
    client = _queried(["b", "a", "a"])

    assert _params(client) == {
        "agent_name": _AGENT,
        "trajectory_ids": ["a", "b"],
    }
    assert "agent_name = @agent_name" in _sql(client)


def test_the_read_names_no_run() -> None:
    """A payload records the conversation, not one sweep's ingestion of it, so
    every sweep that sampled a session resolves to the same copy."""
    assert "run_id" not in _sql(_queried(["session-abc"]))


def test_the_turns_come_back_in_order() -> None:
    """A reader reassembles the conversation by reading them in sequence, and
    the store has no other order to offer."""
    assert "ORDER BY c.trajectory_id, t.turn_index" in _sql(
        _queried(["session-abc"])
    )


def test_the_read_never_selects_every_column() -> None:
    """A rename in the declaration should fail a test rather than a query
    against a deployed dataset."""
    assert "SELECT *" not in _sql(_queried(["session-abc"]))


def test_the_read_is_labelled_for_billing() -> None:
    client = _queried(["session-abc"])

    labels = client.query.call_args.kwargs["job_config"].labels
    assert labels.get("component") == "aqa"


def test_the_reader_never_writes() -> None:
    """The reason a reader is a class of its own: nothing serving a
    dashboard should be able to reach the write machinery."""
    client = _queried(["session-abc"])

    client.load_table_from_json.assert_not_called()
    assert not any(
        verb in _sql(client)
        for verb in ("INSERT ", "DELETE ", "UPDATE ", "MERGE ")
    )


@pytest.mark.parametrize("status", list(PayloadStatus))
def test_every_stored_status_reads_back_as_itself(
    status: PayloadStatus,
) -> None:
    client = _returning([_row(status=status)])

    archive = _reader(client).get_archives(["session-abc"])["session-abc"]

    assert archive.payload.status is status


def _queried(trajectory_ids: list[str]) -> mock.MagicMock:
    """Returns a mock client after serving one get_archives read.

    Args:
        trajectory_ids: List of trajectory IDs to query.

    Returns:
        Configured mock client instance.
    """
    client = _returning([_row()])
    _reader(client).get_archives(trajectory_ids)
    return client
