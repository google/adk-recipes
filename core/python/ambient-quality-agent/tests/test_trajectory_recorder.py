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

"""`TrajectoryRecorder`: the seam ingestion writes trajectories through.

Two things are worth holding here. The recorder binds what a fetcher cannot
know -- which run, which agent, which telemetry source -- so a row comes out
complete without any ids being carried back out of ingestion. And the rows a
run writes are a **per-trajectory expansion of its ingestion counters**, which
is asserted end to end in `test_trajectory_ingestion.py`; this file covers the
recorder's own behaviour.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest import mock

import pytest
from agentplatform._genai.types import EvalCase
from agentplatform._genai.types.evals import AgentData, ConversationTurn
from ambient_quality_agent.tools.trajectories.models import (
    IngestStatus,
    PayloadStatus,
    Trajectory,
)
from ambient_quality_agent.tools.trajectories.recorder import (
    NullTrajectoryRecorder,
    TrajectoryRecorder,
)

_RUN_ID = "run-1"
_AGENT = "root_agent"


def _recorder(store: Any) -> TrajectoryRecorder:
    return TrajectoryRecorder(
        store=store, run_id=_RUN_ID, agent_name=_AGENT, source="cloud_ops"
    )


def _case(turns: int = 2) -> EvalCase:
    return EvalCase(
        eval_case_id="ignored",
        agent_data=AgentData(
            turns=[
                ConversationTurn(turn_index=i, events=[]) for i in range(turns)
            ]
        ),
    )


def _add(recorder: TrajectoryRecorder, **overrides: Any) -> None:
    recorder.add(
        **{
            "trajectory_id": "traj-1",
            "session_id": None,
            "trace_ids": (),
            "status": IngestStatus.INGESTED,
            **overrides,
        }
    )


def test_the_recorder_supplies_what_a_fetcher_cannot_know() -> None:
    """The run, the agent, the telemetry source and the time. Binding them here
    is what lets ingestion report ids and nothing else."""
    store = mock.MagicMock()
    recorder = _recorder(store)

    _add(
        recorder, trajectory_id="a", session_id="s-1", trace_ids=("t-1", "t-2")
    )
    recorder.flush()

    (rows,), _ = store.record.call_args
    row = rows[0]
    assert isinstance(row, Trajectory)
    assert (row.run_id, row.agent_name, row.source) == (
        _RUN_ID,
        _AGENT,
        "cloud_ops",
    )
    assert row.trajectory_id == "a"
    assert row.source_session_id == "s-1"
    assert row.source_trace_ids == ["t-1", "t-2"]
    assert row.created_at.tzinfo is dt.UTC


@pytest.mark.parametrize("status", list(IngestStatus))
def test_the_status_is_stored_as_ingestion_classified_it(
    status: IngestStatus,
) -> None:
    """The recorder does not second-guess the mapper: `_map_page` makes the one
    classification, and this stores it."""
    store = mock.MagicMock()
    recorder = _recorder(store)

    _add(recorder, status=status)
    recorder.flush()

    assert store.record.call_args.args[0][0].ingest_status is status


def test_a_page_is_written_once_rather_than_a_row_at_a_time() -> None:
    """One load job per page: the unit ingestion produces, and what the store
    is sized for."""
    store = mock.MagicMock()
    recorder = _recorder(store)

    _add(recorder, trajectory_id="a")
    _add(recorder, trajectory_id="b")
    assert store.record.call_count == 0

    recorder.flush()

    assert store.record.call_count == 1
    assert [r.trajectory_id for r in store.record.call_args.args[0]] == [
        "a",
        "b",
    ]


def test_a_flush_with_nothing_pending_writes_nothing() -> None:
    """A page that mapped no rows at all should not cost a billed round trip."""
    store = mock.MagicMock()

    _recorder(store).flush()

    store.record.assert_not_called()


def test_a_page_is_not_written_twice() -> None:
    """The buffer is per page; nothing accumulates across them."""
    store = mock.MagicMock()
    recorder = _recorder(store)

    _add(recorder)
    recorder.flush()
    recorder.flush()

    assert store.record.call_count == 1


def test_a_write_failure_is_logged_and_the_sweep_goes_on() -> None:
    """Reporting: the run counters remain the authoritative totals, and the
    ingestion has already been paid for."""
    store = mock.MagicMock()
    store.record.side_effect = RuntimeError("load job failed")
    recorder = _recorder(store)

    _add(recorder)
    recorder.flush()  # does not raise


def test_a_failed_page_is_not_carried_into_the_next_one() -> None:
    """Otherwise one unlucky page would keep re-failing every later write, and
    a single outage would cost the whole run's rows."""
    store = mock.MagicMock()
    store.record.side_effect = [RuntimeError("boom"), None]
    recorder = _recorder(store)

    _add(recorder, trajectory_id="lost")
    recorder.flush()
    _add(recorder, trajectory_id="kept")
    recorder.flush()

    assert [r.trajectory_id for r in store.record.call_args.args[0]] == ["kept"]


def test_cleanup_deletes_by_the_run_it_was_bound_to() -> None:
    """The recorder knows the run, so the caller does not have to repeat it --
    and cannot pass a different one by mistake."""
    store = mock.MagicMock()

    _recorder(store).delete_run_trajectories()

    store.delete_run_trajectories.assert_called_once_with(_RUN_ID)


def test_the_conversation_is_archived_beside_the_row_that_names_it() -> None:
    """One seam, two stores: ingestion has a single moment holding both the ids
    of a row it attempted and the conversation it built from it."""
    store = mock.MagicMock()
    recorder = _recorder(store)

    _add(recorder, trajectory_id="a", case=_case(turns=3))
    recorder.flush()

    (archives,), _ = store.record_payloads.call_args
    assert len(archives) == 1
    assert archives[0].payload.trajectory_id == "a"
    assert archives[0].payload.turn_count == 3


def test_the_archive_carries_the_key_the_index_row_carries() -> None:
    """The trajectory id comes from the caller rather than from
    `case.eval_case_id`, so one value keys the payload and the row pointing at
    it -- a case whose own id disagreed would archive under a name nothing
    resolves."""
    store = mock.MagicMock()
    recorder = _recorder(store)

    _add(recorder, trajectory_id="from-the-mapper", case=_case())
    recorder.flush()

    assert store.record.call_args.args[0][0].trajectory_id == "from-the-mapper"
    archive = store.record_payloads.call_args.args[0][0]
    assert archive.payload.trajectory_id == "from-the-mapper"
    assert archive.payload.agent_name == _AGENT


def test_a_row_ingestion_gave_up_on_is_named_and_not_archived() -> None:
    """There is nothing to archive. The row still says the run sampled it,
    which is what explains a sample smaller than the budget, and the read path
    already copes with a conversation that has no payload."""
    store = mock.MagicMock()
    recorder = _recorder(store)

    _add(recorder, status=IngestStatus.NOT_INGESTED, case=None)
    recorder.flush()

    assert store.record.call_count == 1
    store.record_payloads.assert_not_called()


def test_ingestions_verdict_reaches_the_stored_copy() -> None:
    """A copy built from lossy telemetry is `partial`, which is what makes a
    later whole one an upgrade rather than a duplicate."""
    store = mock.MagicMock()
    recorder = _recorder(store)

    _add(recorder, status=IngestStatus.PARTIAL, case=_case())
    recorder.flush()

    archive = store.record_payloads.call_args.args[0][0]
    assert archive.payload.status is PayloadStatus.PARTIAL


def test_a_row_and_its_copy_are_stamped_at_the_same_instant() -> None:
    """`created_at` partitions both tables. Two clock reads could straddle
    midnight, and the copy would then expire a day off its index row."""
    store = mock.MagicMock()
    recorder = _recorder(store)

    _add(recorder, case=_case())
    recorder.flush()

    row = store.record.call_args.args[0][0]
    archive = store.record_payloads.call_args.args[0][0]
    assert row.created_at == archive.payload.created_at
    assert all(turn.created_at == row.created_at for turn in archive.turns)


def test_the_index_is_written_before_the_conversations() -> None:
    """So the failure is the harmless one: an index row without its payload is
    a conversation the UI can name and cannot show, while a payload without its
    index row would be a copy nothing points at."""
    calls: list[str] = []
    store = mock.MagicMock()
    store.record.side_effect = lambda _rows: calls.append("index")
    store.record_payloads.side_effect = lambda _archives: calls.append(
        "payload"
    )
    recorder = _recorder(store)

    _add(recorder, case=_case())
    recorder.flush()

    assert calls == ["index", "payload"]


def test_a_payload_that_will_not_store_fails_the_sweep() -> None:
    """Losing an index row costs a dot on a chart, while losing a payload
    loses the evidence an insight points at."""
    store = mock.MagicMock()
    store.record_payloads.side_effect = RuntimeError("archive failed")
    recorder = _recorder(store)

    _add(recorder, case=_case())
    with pytest.raises(RuntimeError, match="archive failed"):
        recorder.flush()


def test_a_lost_index_row_does_not_stop_the_conversation_being_archived() -> (
    None
):
    """The index write stays best-effort, and the payload is the valuable
    half -- so one must not take the other down with it."""
    store = mock.MagicMock()
    store.record.side_effect = RuntimeError("load job failed")
    recorder = _recorder(store)

    _add(recorder, case=_case())
    recorder.flush()

    store.record_payloads.assert_called_once()


def test_a_failed_page_of_conversations_is_not_carried_into_the_next() -> None:
    """The sweep is failing anyway, but a retried page must not write the
    previous one's conversations a second time."""
    store = mock.MagicMock()
    store.record_payloads.side_effect = [RuntimeError("boom"), None]
    recorder = _recorder(store)

    _add(recorder, trajectory_id="lost", case=_case())
    with pytest.raises(RuntimeError):
        recorder.flush()
    _add(recorder, trajectory_id="kept", case=_case())
    recorder.flush()

    archives = store.record_payloads.call_args.args[0]
    assert [a.payload.trajectory_id for a in archives] == ["kept"]


def test_a_page_reaches_the_store_as_two_writes_and_not_two_stores() -> None:
    """One store covers both tables, as `InsightStore` covers an insight and
    its occurrences. Two would have to be built with the same four arguments
    everywhere one is."""
    store = mock.MagicMock()
    recorder = _recorder(store)

    _add(recorder, case=_case())
    recorder.flush()

    assert store.record.call_count == 1
    assert store.record_payloads.call_count == 1


def test_the_null_recorder_accepts_everything_and_records_nothing() -> None:
    """A fetcher built outside a sweep -- the offline harness, most tests --
    needs no wiring, and a null object means no caller has to guard."""
    recorder = NullTrajectoryRecorder()

    recorder.add(
        trajectory_id="a",
        session_id=None,
        trace_ids=(),
        status=IngestStatus.INGESTED,
        case=_case(),
    )
    recorder.flush()


def test_the_real_recorder_is_substitutable_for_the_null_one() -> None:
    """Ingestion holds one or the other and calls the same two methods."""
    assert issubclass(TrajectoryRecorder, NullTrajectoryRecorder)
