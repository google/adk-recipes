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

"""`InMemoryTrajectoryStore` against the semantics `TrajectoryStore` writes down.

Read against the contract's docstrings, as the in-memory insight store's tests
are, rather than run twice against both implementations: BigQuery's half of this
entity is three append-only tables and a preference applied on the way out, and
a row-level twin of that would be the thing under test rewritten to check itself.

What the two do share is `compute_payload_rank`, so the one rule that decides which copy
of a conversation survives is exercised here in the same form the SQL spells.
The cases below are the ones that docstring names, plus the pair it forbids:
nothing may downgrade a whole conversation, and nothing may take an archive away
from a run that is not being retried.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

import pytest
from ambient_quality_agent.standalone.store import InMemoryStore
from ambient_quality_agent.standalone.trajectories import (
    InMemoryTrajectoryStore,
)
from ambient_quality_agent.tools.trajectories.models import (
    IngestStatus,
    PayloadStatus,
    Trajectory,
    TrajectoryPayload,
    TrajectoryPayloadTurn,
)
from ambient_quality_agent.tools.trajectories.payloads import TrajectoryArchive
from ambient_quality_agent.tools.trajectories.store import compute_payload_rank

_AGENT = "watched-agent"
_OTHER = "somebody-else"
_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


@pytest.fixture
def backing() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def store(backing: InMemoryStore) -> InMemoryTrajectoryStore:
    return InMemoryTrajectoryStore(backing, agent_name=_AGENT)


def _trajectory(run_id: str, trajectory_id: str, **fields: Any) -> Trajectory:
    defaults: dict[str, Any] = {
        "agent_name": _AGENT,
        "created_at": _NOW,
        "source": "big_query",
        "ingest_status": IngestStatus.INGESTED,
    }
    return Trajectory(
        run_id=run_id, trajectory_id=trajectory_id, **{**defaults, **fields}
    )


def _archive(
    trajectory_id: str,
    *,
    status: PayloadStatus = PayloadStatus.INGESTED,
    created_at: dt.datetime = _NOW,
    turns: int = 1,
    agent_name: str = _AGENT,
) -> TrajectoryArchive:
    return TrajectoryArchive(
        payload=TrajectoryPayload(
            agent_name=agent_name,
            trajectory_id=trajectory_id,
            created_at=created_at,
            status=status,
            turn_count=turns,
        ),
        turns=tuple(
            TrajectoryPayloadTurn(
                agent_name=agent_name,
                trajectory_id=trajectory_id,
                turn_index=index,
                created_at=created_at,
                turn={"text": f"{trajectory_id}-{status}-{index}"},
            )
            for index in range(turns)
        ),
    )


# --- the index -------------------------------------------------------------- #


def test_a_page_is_keyed_by_run_and_trajectory(
    store: InMemoryTrajectoryStore, backing: InMemoryStore
) -> None:
    """One row per pair, and two runs of one conversation are two rows."""
    store.record(
        [_trajectory("run-1", "chat-1"), _trajectory("run-2", "chat-1")]
    )

    assert set(backing.trajectories) == {
        ("run-1", "chat-1"),
        ("run-2", "chat-1"),
    }


def test_recording_nothing_writes_nothing(
    store: InMemoryTrajectoryStore, backing: InMemoryStore
) -> None:
    store.record([])

    assert backing.trajectories == {}


def test_a_retry_replaces_the_row_for_its_pair(
    store: InMemoryTrajectoryStore, backing: InMemoryStore
) -> None:
    """The key is the pair, so re-recording it cannot leave two rows behind."""
    store.record(
        [_trajectory("run-1", "chat-1", ingest_status=IngestStatus.PARTIAL)]
    )
    store.record(
        [_trajectory("run-1", "chat-1", ingest_status=IngestStatus.INGESTED)]
    )

    row = backing.trajectories["run-1", "chat-1"]
    assert row.ingest_status is IngestStatus.INGESTED
    assert len(backing.trajectories) == 1


# --- what a retry erases ------------------------------------------------------ #


def test_cleanup_drops_only_the_named_runs_index_rows(
    store: InMemoryTrajectoryStore, backing: InMemoryStore
) -> None:
    store.record(
        [_trajectory("run-1", "chat-1"), _trajectory("run-2", "chat-1")]
    )

    store.delete_run_trajectories("run-1")

    assert set(backing.trajectories) == {("run-2", "chat-1")}


def test_cleanup_leaves_another_agents_rows_alone(
    store: InMemoryTrajectoryStore, backing: InMemoryStore
) -> None:
    """Scoped to the agent, as the deployed store's ``WHERE agent_name`` is."""
    store.record(
        [
            _trajectory("run-1", "chat-1"),
            _trajectory("run-1", "chat-2", agent_name=_OTHER),
        ]
    )

    store.delete_run_trajectories("run-1")

    assert set(backing.trajectories) == {("run-1", "chat-2")}


def test_cleanup_never_touches_the_conversations(
    store: InMemoryTrajectoryStore, backing: InMemoryStore
) -> None:
    """The one thing `delete_run_trajectories` must not do.

    A conversation is evidence behind every insight that named it, so dropping
    the copy this run happened to write would take evidence away from a run that
    is not being retried.
    """
    store.record([_trajectory("run-1", "chat-1")])
    store.record_payloads([_archive("chat-1")])

    store.delete_run_trajectories("run-1")

    assert set(backing.archives) == {"chat-1"}


# --- which copy of a conversation survives ------------------------------------- #


def test_a_conversation_is_stored_once(
    store: InMemoryTrajectoryStore, backing: InMemoryStore
) -> None:
    """No run in the key: two sweeps sampling one session leave one archive."""
    store.record_payloads([_archive("chat-1")])
    store.record_payloads(
        [_archive("chat-1", created_at=_NOW + dt.timedelta(days=4))]
    )

    assert list(backing.archives) == ["chat-1"]


def test_a_later_whole_copy_repairs_an_earlier_partial_one(
    store: InMemoryTrajectoryStore, backing: InMemoryStore
) -> None:
    friday = _NOW + dt.timedelta(days=4)
    store.record_payloads([_archive("chat-1", status=PayloadStatus.PARTIAL)])

    store.record_payloads(
        [_archive("chat-1", status=PayloadStatus.INGESTED, created_at=friday)]
    )

    kept = backing.archives["chat-1"].payload
    assert kept.status is PayloadStatus.INGESTED
    assert kept.created_at == friday


def test_a_later_partial_copy_cannot_downgrade_a_whole_one(
    store: InMemoryTrajectoryStore, backing: InMemoryStore
) -> None:
    """The likelier direction, and the one a plain "latest wins" would get wrong.

    A conversation re-sampled later is reassembled from telemetry that has had
    longer to age out.
    """
    friday = _NOW + dt.timedelta(days=4)
    store.record_payloads([_archive("chat-1", status=PayloadStatus.INGESTED)])

    store.record_payloads(
        [_archive("chat-1", status=PayloadStatus.PARTIAL, created_at=friday)]
    )

    kept = backing.archives["chat-1"].payload
    assert kept.status is PayloadStatus.INGESTED
    assert kept.created_at == _NOW


def test_two_copies_of_a_kind_keep_the_earlier(
    store: InMemoryTrajectoryStore, backing: InMemoryStore
) -> None:
    """The one nearest the conversation."""
    store.record_payloads(
        [_archive("chat-1", created_at=_NOW + dt.timedelta(days=4))]
    )

    store.record_payloads([_archive("chat-1", created_at=_NOW)])

    assert backing.archives["chat-1"].payload.created_at == _NOW


def test_a_truncated_copy_does_not_outrank_a_whole_one(
    store: InMemoryTrajectoryStore, backing: InMemoryStore
) -> None:
    """`TRUNCATED` ranks with `PARTIAL`: both are short, however they got there."""
    store.record_payloads([_archive("chat-1", status=PayloadStatus.INGESTED)])

    store.record_payloads(
        [
            _archive(
                "chat-1",
                status=PayloadStatus.TRUNCATED,
                created_at=_NOW - dt.timedelta(days=1),
            )
        ]
    )

    assert backing.archives["chat-1"].payload.status is PayloadStatus.INGESTED


def test_the_chosen_copy_brings_its_own_turns(
    store: InMemoryTrajectoryStore, backing: InMemoryStore
) -> None:
    """The manifest and its turns move together, so no read has to rejoin them.

    BigQuery ties a turn to its copy through a shared ``created_at``; here the
    archive is one object, so a turn of the copy that lost cannot survive.
    """
    friday = _NOW + dt.timedelta(days=4)
    store.record_payloads(
        [_archive("chat-1", status=PayloadStatus.PARTIAL, turns=1)]
    )

    store.record_payloads([_archive("chat-1", created_at=friday, turns=3)])

    archive = backing.archives["chat-1"]
    assert archive.payload.turn_count == 3
    assert [turn.turn_index for turn in archive.turns] == [0, 1, 2]
    assert {turn.created_at for turn in archive.turns} == {friday}


def test_payload_rank_orders_whole_before_early(
    store: InMemoryTrajectoryStore,
) -> None:
    """The rule itself, in the form `PAYLOAD_PREFERENCE` spells as SQL."""
    early_partial = _archive(
        "chat-1", status=PayloadStatus.PARTIAL, created_at=_NOW
    ).payload
    late_whole = _archive(
        "chat-1", created_at=_NOW + dt.timedelta(days=4)
    ).payload

    assert compute_payload_rank(late_whole) < compute_payload_rank(
        early_partial
    )


# --- one writer, many readers --------------------------------------------------- #


def test_concurrent_archiving_of_one_conversation_keeps_the_better_copy(
    store: InMemoryTrajectoryStore, backing: InMemoryStore
) -> None:
    """The reason the comparison is inside the store rather than in the adapter.

    Read-then-write across the lock would let both threads see no stored copy
    and the loser land last. Every thread archives the same conversation at
    once; exactly one copy is whole, and it must be the one left.
    """
    started = threading.Barrier(8)
    copies = [
        _archive(
            "chat-1",
            status=PayloadStatus.INGESTED
            if index == 3
            else PayloadStatus.PARTIAL,
            created_at=_NOW + dt.timedelta(minutes=index),
        )
        for index in range(8)
    ]

    def archive(copy: TrajectoryArchive) -> None:
        started.wait(timeout=5)
        store.record_payloads([copy])

    threads = [
        threading.Thread(target=archive, args=(copy,)) for copy in copies
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    kept = backing.archives["chat-1"].payload
    assert kept.status is PayloadStatus.INGESTED
    assert kept.created_at == _NOW + dt.timedelta(minutes=3)
