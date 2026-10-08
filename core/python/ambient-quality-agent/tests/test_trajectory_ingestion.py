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

"""The trajectory rows a sweep leaves behind, against the counters it reports.

The store is a **per-trajectory expansion of the run's ingestion counters**,
and that is what this file asserts: the rows a run wrote, tallied by
`ingest_status`, have to equal the counters the same run reported. A divergence
is a lost write, not a disagreement about what happened, which is what lets the
dashboard say so rather than quietly under-report.

Driven through a real fetcher against a fake recorder, because the write is
ingestion's side effect -- there is no node in the path to drive. Both
producers are exercised: a run uses one or the other, `session_review`
is the default, and neither should lose rows.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent import backends
from ambient_quality_agent.core import nodes
from ambient_quality_agent.core.nodes import _common
from ambient_quality_agent.core.state import WorkflowState
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion.bigquery_fetcher import (
    BigQueryFetcher,
)
from ambient_quality_agent.tools.trajectories.bigquery_store import (
    BigQueryTrajectoryStore,
)
from ambient_quality_agent.tools.trajectories.models import IngestStatus
from ambient_quality_agent.tools.trajectories.recorder import TrajectoryRecorder

from .conftest import FakeTrajectoryRecorder

_RUN_ID = "run-1"
_AGENT = "test-agent"


def _state(**overrides: Any) -> dict[str, Any]:
    return (
        WorkflowState(
            observed_agent_name=_AGENT,
            project_id="test-project",
            location="us-central1",
            quality_analysis_mode="eval_service",
            telemetry_ingestion_source="cloud_ops",
            budget_per_metric=10,
            run_id=_RUN_ID,
            window_start=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            window_end=dt.datetime(2026, 1, 8, tzinfo=dt.UTC),
            multi_turn_metrics=["task_success"],
        ).model_dump(mode="json")
        | overrides
    )


def _ctx(state: dict[str, Any]) -> mock.MagicMock:
    ctx = mock.MagicMock()
    ctx.state = state
    return ctx


# --- the counter invariants ---------------------------------------------------


def _session_row(
    session_id: str, *, turns: int = 1, usable: bool = True
) -> dict[str, Any]:
    """Creates a mock analytics row with simulated session events.

    Args:
        session_id: Identifier of the session.
        turns: Number of interaction turns to generate.
        usable: Whether to include valid events.

    Returns:
        Mock analytics row dictionary.
    """
    events = []
    for index in range(turns):
        events += [
            {
                "invocation_id": f"{session_id}-{index}",
                "event_type": "USER_MESSAGE_RECEIVED",
                "agent": "root_agent",
                "content": {"text_summary": "hi"},
            },
            {
                "invocation_id": f"{session_id}-{index}",
                "event_type": "AGENT_RESPONSE",
                "agent": "root_agent",
                "content": {"response": "hello"},
            },
        ]
    return {"session_id": session_id, "events": events if usable else []}


@pytest.fixture
def recorded_ingestion() -> tuple[list[Any], Any]:
    """Map a page of real rows through a real fetcher and a fake recorder."""
    recorder = FakeTrajectoryRecorder()
    with mock.patch(
        "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
        return_value=mock.MagicMock(),
    ):
        fetcher = BigQueryFetcher(
            project_id="test-project",
            dataset="analytics",
            table="events",
            location="us-central1",
            recorder=recorder,
        )
    fetcher._map_page(
        [_session_row("a"), _session_row("b", turns=2)], MetricType.MULTI_TURN
    )
    fetcher._recorder.flush()
    return recorder.records, fetcher.counts


def test_every_attempted_row_becomes_exactly_one_recorded_trajectory(
    recorded_ingestion: tuple[list[Any], Any],
) -> None:
    rows, counts = recorded_ingestion

    assert len(rows) == counts.ingested + counts.partial + counts.failed


def test_the_ingested_rows_equal_the_ingested_counter(
    recorded_ingestion: tuple[list[Any], Any],
) -> None:
    rows, counts = recorded_ingestion
    ingested = [r for r in rows if r.ingest_status is IngestStatus.INGESTED]

    assert len(ingested) == counts.ingested == 2


@pytest.mark.parametrize(
    ("status", "attribute"),
    [
        (IngestStatus.PARTIAL, "partial"),
        (IngestStatus.NOT_INGESTED, "failed"),
    ],
)
def test_each_other_state_equals_its_counter(
    recorded_ingestion: tuple[list[Any], Any],
    status: IngestStatus,
    attribute: str,
) -> None:
    rows, counts = recorded_ingestion

    assert len([r for r in rows if r.ingest_status is status]) == getattr(
        counts, attribute
    )


# --- both producers write, without either knowing they do ---------------------


@pytest.mark.parametrize("node_name", ["eval_multi_turn", "review"])
def test_neither_producer_mentions_trajectories(node_name: str) -> None:
    """The write is ingestion's side effect. A producer that had to remember to
    record would be one that could forget -- and the reviewer, which is the
    default `quality_analysis_mode`, is exactly where that was going to happen.
    """
    module = getattr(nodes, f"{node_name}_node").__module__
    source = __import__(module, fromlist=["__file__"]).__file__

    assert source is not None
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    assert "trajector" not in text.lower()


def test_the_fetcher_factory_binds_the_run_to_the_recorder() -> None:
    """The one place the sweep's identity reaches ingestion."""
    state = _state()
    with (
        mock.patch.object(backends.bigquery, "Client"),
        mock.patch.object(_common, "storage"),
    ):
        recorder = backends.build_bigquery_bindings()["trajectory_recorder"](
            _ctx(state)
        )

    assert isinstance(recorder, TrajectoryRecorder)
    assert recorder._run_id == _RUN_ID
    assert recorder._agent_name == _AGENT
    assert recorder._source == "cloud_ops"


def test_one_store_covers_the_index_and_the_conversations() -> None:
    """One agent, one dataset, one client -- and the last of those is not a
    convenience: the payload write folds a staged batch into two tables in a
    transaction, which cannot span job regions."""
    state = _state()
    with (
        mock.patch.object(backends.bigquery, "Client"),
        mock.patch.object(_common, "storage"),
    ):
        recorder = backends.build_bigquery_bindings()["trajectory_recorder"](
            _ctx(state)
        )

    assert isinstance(recorder._store, BigQueryTrajectoryStore)
    assert recorder._store._agent_name == _AGENT


def test_a_fetcher_built_without_a_recorder_still_works() -> None:
    """The offline quality harness builds fetchers outside a sweep."""
    with mock.patch(
        "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
        return_value=mock.MagicMock(),
    ):
        fetcher = BigQueryFetcher(
            project_id="p", dataset="d", table="t", location="us-central1"
        )

    cases, _ = fetcher._map_page([_session_row("a")], MetricType.MULTI_TURN)

    assert len(cases) == 1


# --- the conversation reaches the archive, through the same seam --------------


@pytest.fixture
def archived_ingestion() -> Any:
    """Map a page of real rows through a real recorder onto a mock store."""
    store = mock.MagicMock()
    recorder = TrajectoryRecorder(
        store=store, run_id=_RUN_ID, agent_name=_AGENT, source="big_query"
    )
    with mock.patch(
        "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
        return_value=mock.MagicMock(),
    ):
        fetcher = BigQueryFetcher(
            project_id="test-project",
            dataset="analytics",
            table="events",
            location="us-central1",
            recorder=recorder,
        )
    fetcher._map_page(
        [_session_row("a"), _session_row("b", turns=2)], MetricType.MULTI_TURN
    )
    recorder.flush()
    return store


def test_a_page_of_conversations_is_archived_in_one_write(
    archived_ingestion: Any,
) -> None:
    """The page is the unit ingestion produces, and the load quota is per table
    per day -- a write per conversation would spend it on a single sweep."""
    assert archived_ingestion.record_payloads.call_count == 1


def test_a_conversation_is_archived_under_the_name_the_index_gives_it(
    archived_ingestion: Any,
) -> None:
    """The two tables are joined on that name, so an archive keyed differently
    from the row pointing at it would be a copy nothing resolves.

    The analytics source never drops a row, so here every named trajectory also
    has a copy. Where a source *can* drop one, the row is written and the copy
    is not -- there is nothing to copy -- which
    `test_trajectory_recorder.py` covers.
    """
    store = archived_ingestion
    indexed = {row.trajectory_id for row in store.record.call_args.args[0]}
    archived = {
        a.payload.trajectory_id for a in store.record_payloads.call_args.args[0]
    }

    assert indexed == archived == {"a", "b"}


def test_an_archived_conversation_keeps_its_turns(
    archived_ingestion: Any,
) -> None:
    """End to end from a real fetcher: the turns the mapper assembled are the
    turns the store is handed, numbered in order."""
    archives = {
        a.payload.trajectory_id: a
        for a in archived_ingestion.record_payloads.call_args.args[0]
    }

    assert archives["a"].payload.turn_count == 1
    assert archives["b"].payload.turn_count == 2
    assert [turn.turn_index for turn in archives["b"].turns] == [0, 1]


# --- retry safety -------------------------------------------------------------


def test_init_erases_the_rows_of_an_earlier_attempt(
    patched_factories: dict[str, Any],
) -> None:
    """Both producers build their own recorder and a run may build several, so
    the cleanup cannot live with the write; `init` runs first and once."""
    nodes.init_node._func(_ctx(_state()))

    assert patched_factories["trajectory_recorder"].cleanup_calls == 1


def test_init_survives_a_store_it_cannot_reach(
    patched_factories: dict[str, Any],
) -> None:
    """A doubled bar on a chart is a better outcome than a sweep that produces
    no quality analysis at all."""
    recorder = mock.MagicMock()
    recorder.delete_run_trajectories.side_effect = RuntimeError("no such table")
    patched_factories["trajectory_recorder"] = recorder

    assert nodes.init_node._func(_ctx(_state())).content is not None
