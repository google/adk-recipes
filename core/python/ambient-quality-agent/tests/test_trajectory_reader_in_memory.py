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

"""`InMemoryTrajectoryReader` against the semantics `TrajectoryReader` writes down.

Most of the surface is product behaviour that survives the change of storage
whole -- the window filters, the paging, the ordering, and above all the outcome,
which is derived here exactly as the ``CASE`` derives it there. Those are what
these cover, because they are what a dashboard would notice.

The two reconciliations that do *not* survive are covered by their absence:
`get_trajectory_bundles` is asserted to be a lookup of one copy, and the copy it
finds is the one the write chose. Which copy that is belongs to
`test_trajectory_store_in_memory`; that it is the same one both halves mean is
the join between the two files.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from ambient_quality_agent.standalone.store import InMemoryStore, Insights
from ambient_quality_agent.standalone.trajectories import (
    InMemoryTrajectoryReader,
    InMemoryTrajectoryStore,
)
from ambient_quality_agent.tools.insights.models import (
    InsightOccurrence,
    OccurrenceState,
)
from ambient_quality_agent.tools.trajectories.bundles import TrajectoryBundle
from ambient_quality_agent.tools.trajectories.models import (
    IngestStatus,
    InvestigationTrajectory,
    TrajectoryManifest,
    TrajectoryProcessingState,
    TrajectoryStatus,
)

_AGENT = "watched-agent"
_OTHER = "somebody-else"
_NOW = dt.datetime(2026, 6, 1, 12, tzinfo=dt.UTC)


@pytest.fixture
def backing() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def writer(backing: InMemoryStore) -> InMemoryTrajectoryStore:
    return InMemoryTrajectoryStore(backing, observed_agent_key=_AGENT)


@pytest.fixture
def reader(backing: InMemoryStore) -> InMemoryTrajectoryReader:
    return InMemoryTrajectoryReader(backing, observed_agent_key=_AGENT)


def _trajectory(
    investigation_id: str, trajectory_id: str, **fields: Any
) -> InvestigationTrajectory:
    defaults: dict[str, Any] = {
        "observed_agent_key": _AGENT,
        "created_at": _NOW,
        "source": "cloud_ops",
        "source_trace_ids": [f"trace-{trajectory_id}"],
        "ingest_status": IngestStatus.INGESTED,
    }
    return InvestigationTrajectory(
        investigation_id=investigation_id,
        trajectory_id=trajectory_id,
        **{**defaults, **fields},
    )


def _occurrence(
    occurrence_id: str,
    *,
    investigation_id: str,
    trajectory_ids: list[str],
    insight_id: str | None = "insight-1",
    observed_agent_key: str = _AGENT,
) -> InsightOccurrence:
    return InsightOccurrence(
        occurrence_id=occurrence_id,
        insight_id=insight_id,
        observed_agent_key=observed_agent_key,
        investigation_id=investigation_id,
        occurrence_state=OccurrenceState.TRACKED,
        label="an issue",
        created_at=_NOW,
        trajectory_ids=trajectory_ids,
    )


def _seed_occurrences(
    backing: InMemoryStore, occurrences: list[InsightOccurrence]
) -> None:
    backing.update_insights(
        lambda current: Insights(
            current.insights, occurrences, current.root_causes
        )
    )


def _build_bundle(
    trajectory_id: str,
    *,
    status: TrajectoryStatus = TrajectoryStatus.INGESTED,
    observed_agent_key: str = _AGENT,
) -> TrajectoryBundle:
    return TrajectoryBundle(
        manifest=TrajectoryManifest(
            observed_agent_key=observed_agent_key,
            trajectory_id=trajectory_id,
            created_at=_NOW,
            status=status,
            step_count=0,
        ),
        steps=(),
    )


# --- the outcome, derived the same way in both queries -------------------------- #


def test_an_unusable_trajectory_is_not_ingested_even_inside_an_insight(
    writer: InMemoryTrajectoryStore,
    reader: InMemoryTrajectoryReader,
    backing: InMemoryStore,
) -> None:
    """The order the states are checked in, and the reason for it.

    Something no analysis could see must not be filed under "evaluated and
    clean", nor under an insight it cannot have contributed to.
    """
    writer.record_investigation_trajectories(
        [
            _trajectory(
                "run-1", "chat-1", ingest_status=IngestStatus.NOT_INGESTED
            )
        ]
    )
    _seed_occurrences(
        backing,
        [
            _occurrence(
                "occ-1", investigation_id="run-1", trajectory_ids=["chat-1"]
            )
        ],
    )

    page, _ = reader.list_investigation_trajectories(limit=10, offset=0)

    assert page[0].outcome is TrajectoryProcessingState.NOT_INGESTED


def test_a_trajectory_an_insight_names_is_in_an_insight(
    writer: InMemoryTrajectoryStore,
    reader: InMemoryTrajectoryReader,
    backing: InMemoryStore,
) -> None:
    writer.record_investigation_trajectories([_trajectory("run-1", "chat-1")])
    _seed_occurrences(
        backing,
        [
            _occurrence(
                "occ-1", investigation_id="run-1", trajectory_ids=["chat-1"]
            )
        ],
    )

    page, _ = reader.list_investigation_trajectories(limit=10, offset=0)

    assert page[0].outcome is TrajectoryProcessingState.IN_AN_INSIGHT


def test_an_unclustered_trajectory_has_no_insight(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    writer.record_investigation_trajectories([_trajectory("run-1", "chat-1")])

    page, _ = reader.list_investigation_trajectories(limit=10, offset=0)

    assert page[0].outcome is TrajectoryProcessingState.NO_INSIGHT


def test_a_rejected_candidates_trajectories_are_not_evidence(
    writer: InMemoryTrajectoryStore,
    reader: InMemoryTrajectoryReader,
    backing: InMemoryStore,
) -> None:
    """An occurrence naming no insight is a candidate verification threw out.

    It still lists what it was built from, so counting those would contradict
    the verdict reached and the insights pane.
    """
    writer.record_investigation_trajectories([_trajectory("run-1", "chat-1")])
    _seed_occurrences(
        backing,
        [
            _occurrence(
                "occ-1",
                investigation_id="run-1",
                trajectory_ids=["chat-1"],
                insight_id=None,
            )
        ],
    )

    page, _ = reader.list_investigation_trajectories(limit=10, offset=0)

    assert page[0].outcome is TrajectoryProcessingState.NO_INSIGHT


def test_evidence_is_matched_on_the_run_as_well_as_the_conversation(
    writer: InMemoryTrajectoryStore,
    reader: InMemoryTrajectoryReader,
    backing: InMemoryStore,
) -> None:
    """A later sweep's finding does not retroactively colour an earlier sample."""
    writer.record_investigation_trajectories(
        [_trajectory("run-1", "chat-1"), _trajectory("run-2", "chat-1")]
    )
    _seed_occurrences(
        backing,
        [
            _occurrence(
                "occ-1", investigation_id="run-2", trajectory_ids=["chat-1"]
            )
        ],
    )

    page, _ = reader.list_investigation_trajectories(limit=10, offset=0)

    outcomes = {view.investigation_id: view.outcome for view in page}
    assert outcomes["run-1"] is TrajectoryProcessingState.NO_INSIGHT
    assert outcomes["run-2"] is TrajectoryProcessingState.IN_AN_INSIGHT


def test_another_agents_occurrence_is_not_evidence(
    writer: InMemoryTrajectoryStore,
    reader: InMemoryTrajectoryReader,
    backing: InMemoryStore,
) -> None:
    writer.record_investigation_trajectories([_trajectory("run-1", "chat-1")])
    _seed_occurrences(
        backing,
        [
            _occurrence(
                "occ-1",
                investigation_id="run-1",
                trajectory_ids=["chat-1"],
                observed_agent_key=_OTHER,
            )
        ],
    )

    page, _ = reader.list_investigation_trajectories(limit=10, offset=0)

    assert page[0].outcome is TrajectoryProcessingState.NO_INSIGHT


# --- the per-day chart ----------------------------------------------------------- #


def test_a_day_is_counted_once_per_outcome(
    writer: InMemoryTrajectoryStore,
    reader: InMemoryTrajectoryReader,
    backing: InMemoryStore,
) -> None:
    writer.record_investigation_trajectories(
        [
            _trajectory("run-1", "chat-1"),
            _trajectory("run-1", "chat-2"),
            _trajectory(
                "run-1", "chat-3", ingest_status=IngestStatus.NOT_INGESTED
            ),
        ]
    )
    _seed_occurrences(
        backing,
        [
            _occurrence(
                "occ-1", investigation_id="run-1", trajectory_ids=["chat-1"]
            )
        ],
    )

    counts = reader.count_daily_outcomes()

    # Ordered by day then outcome, and an outcome sorts as its own string --
    # which is what ``ORDER BY day, outcome`` does on the other implementation.
    assert [(row.outcome, row.trajectories) for row in counts] == [
        (TrajectoryProcessingState.IN_AN_INSIGHT, 1),
        (TrajectoryProcessingState.NO_INSIGHT, 1),
        (TrajectoryProcessingState.NOT_INGESTED, 1),
    ]


def test_a_day_with_no_trajectories_is_absent_rather_than_zero(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    """The caller draws the buckets it wants; the store reports what happened."""
    writer.record_investigation_trajectories(
        [
            _trajectory("run-1", "chat-1"),
            _trajectory(
                "run-2", "chat-2", created_at=_NOW + dt.timedelta(days=2)
            ),
        ]
    )

    counts = reader.count_daily_outcomes()

    assert [row.day for row in counts] == [
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 3),
    ]


def test_the_day_is_the_utc_one(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    """Bucketed in UTC, as the partition column is, whatever tzinfo arrived."""
    late = dt.datetime(
        2026, 6, 1, 23, tzinfo=dt.timezone(dt.timedelta(hours=-5))
    )

    writer.record_investigation_trajectories(
        [_trajectory("run-1", "chat-1", created_at=late)]
    )

    assert [row.day for row in reader.count_daily_outcomes()] == [
        dt.date(2026, 6, 2)
    ]


def test_the_chart_and_its_drilldown_agree(
    writer: InMemoryTrajectoryStore,
    reader: InMemoryTrajectoryReader,
    backing: InMemoryStore,
) -> None:
    """One definition of an outcome, so a bar's count is the page behind it."""
    writer.record_investigation_trajectories(
        [
            _trajectory("run-1", f"chat-{index}", ingest_status=status)
            for index, status in enumerate(
                [
                    IngestStatus.INGESTED,
                    IngestStatus.PARTIAL,
                    IngestStatus.NOT_INGESTED,
                    IngestStatus.INGESTED,
                ]
            )
        ]
    )
    _seed_occurrences(
        backing,
        [
            _occurrence(
                "occ-1", investigation_id="run-1", trajectory_ids=["chat-0"]
            )
        ],
    )

    for row in reader.count_daily_outcomes():
        _, total = reader.list_investigation_trajectories(
            outcome=row.outcome, limit=100, offset=0
        )
        assert total == row.trajectories


# --- the list ---------------------------------------------------------------------- #


def test_the_page_is_newest_first_with_the_id_breaking_ties(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    """Ties are routine -- a page of ingestion stamps one moment on every row."""
    writer.record_investigation_trajectories(
        [
            _trajectory("run-1", "chat-b"),
            _trajectory("run-1", "chat-a"),
            _trajectory(
                "run-1", "chat-c", created_at=_NOW + dt.timedelta(hours=1)
            ),
        ]
    )

    page, total = reader.list_investigation_trajectories(limit=10, offset=0)

    assert [view.trajectory_id for view in page] == [
        "chat-c",
        "chat-a",
        "chat-b",
    ]
    assert total == 3


def test_the_total_counts_the_matches_rather_than_the_page(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    writer.record_investigation_trajectories(
        [_trajectory("run-1", f"chat-{index}") for index in range(5)]
    )

    page, total = reader.list_investigation_trajectories(limit=2, offset=1)

    assert len(page) == 2
    assert total == 5


def test_paging_neither_skips_nor_repeats_a_row(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    writer.record_investigation_trajectories(
        [_trajectory("run-1", f"chat-{index}") for index in range(5)]
    )

    first, _ = reader.list_investigation_trajectories(limit=2, offset=0)
    second, _ = reader.list_investigation_trajectories(limit=2, offset=2)
    third, _ = reader.list_investigation_trajectories(limit=2, offset=4)

    seen = [view.trajectory_id for view in (*first, *second, *third)]
    assert sorted(seen) == [f"chat-{index}" for index in range(5)]


def test_the_list_filters_by_run(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    writer.record_investigation_trajectories(
        [_trajectory("run-1", "chat-1"), _trajectory("run-2", "chat-2")]
    )

    page, total = reader.list_investigation_trajectories(
        investigation_id="run-2", limit=10, offset=0
    )

    assert [view.trajectory_id for view in page] == ["chat-2"]
    assert total == 1


def test_the_window_bounds_are_inclusive(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    writer.record_investigation_trajectories(
        [
            _trajectory(
                "run-1", "before", created_at=_NOW - dt.timedelta(hours=1)
            ),
            _trajectory("run-1", "edge", created_at=_NOW),
            _trajectory(
                "run-1", "after", created_at=_NOW + dt.timedelta(hours=1)
            ),
        ]
    )

    page, _ = reader.list_investigation_trajectories(
        window_start=_NOW.isoformat(),
        window_end=_NOW.isoformat(),
        limit=10,
        offset=0,
    )

    assert [view.trajectory_id for view in page] == ["edge"]


def test_an_unparseable_window_bound_is_no_bound(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    """A bound nothing can be compared against must not silently empty the list."""
    writer.record_investigation_trajectories([_trajectory("run-1", "chat-1")])

    page, _ = reader.list_investigation_trajectories(
        window_start="last tuesday", limit=10, offset=0
    )

    assert [view.trajectory_id for view in page] == ["chat-1"]


def test_a_naive_window_bound_is_read_as_utc(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    writer.record_investigation_trajectories([_trajectory("run-1", "chat-1")])

    page, _ = reader.list_investigation_trajectories(
        window_start="2026-06-01T00:00:00", limit=10, offset=0
    )

    assert [view.trajectory_id for view in page] == ["chat-1"]


def test_the_list_is_scoped_to_the_agent(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    writer.record_investigation_trajectories(
        [
            _trajectory("run-1", "chat-1"),
            _trajectory("run-1", "chat-2", observed_agent_key=_OTHER),
        ]
    )

    page, total = reader.list_investigation_trajectories(limit=10, offset=0)

    assert [view.trajectory_id for view in page] == ["chat-1"]
    assert total == 1


# --- resolving an occurrence's conversations ------------------------------------- #


def test_rows_come_back_keyed_by_the_pair_that_asked_for_them(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    """A trajectory id is only unique within a sweep, so the key is the pair."""
    writer.record_investigation_trajectories(
        [_trajectory("run-1", "chat-1"), _trajectory("run-2", "chat-1")]
    )

    found = reader.get_investigation_trajectories([("run-2", "chat-1")])

    assert list(found) == [("run-2", "chat-1")]
    assert found["run-2", "chat-1"].source_trace_ids == ["trace-chat-1"]


def test_a_pair_with_no_row_is_absent_rather_than_a_placeholder(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    """Allows callers to distinguish unrecorded trajectories from ones with empty trace IDs."""
    writer.record_investigation_trajectories(
        [_trajectory("run-1", "chat-1", source_trace_ids=[])]
    )

    found = reader.get_investigation_trajectories(
        [("run-1", "chat-1"), ("run-1", "gone")]
    )

    assert list(found) == [("run-1", "chat-1")]


def test_asking_for_nothing_reads_nothing(
    reader: InMemoryTrajectoryReader,
) -> None:
    assert reader.get_investigation_trajectories([]) == {}
    assert reader.get_trajectory_bundles([]) == {}


def test_another_agents_row_is_not_resolved(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    writer.record_investigation_trajectories(
        [_trajectory("run-1", "chat-1", observed_agent_key=_OTHER)]
    )

    assert reader.get_investigation_trajectories([("run-1", "chat-1")]) == {}


# --- the archived conversation ----------------------------------------------------- #


def test_a_conversation_resolves_to_the_copy_the_write_chose(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    """The join with the write half: one copy, and it is the better one."""
    writer.record_trajectory_bundles(
        [_build_bundle("chat-1", status=TrajectoryStatus.PARTIAL)]
    )
    writer.record_trajectory_bundles(
        [_build_bundle("chat-1", status=TrajectoryStatus.INGESTED)]
    )

    found = reader.get_trajectory_bundles(["chat-1"])

    assert list(found) == ["chat-1"]
    assert found["chat-1"].manifest.status is TrajectoryStatus.INGESTED


def test_an_unarchived_conversation_is_absent_rather_than_empty(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    """So a caller can tell "never archived" from "archived and empty"."""
    writer.record_trajectory_bundles([_build_bundle("chat-1")])

    found = reader.get_trajectory_bundles(["chat-1", "chat-2"])

    assert list(found) == ["chat-1"]


def test_an_archive_is_resolved_without_a_run(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    """Every sweep that sampled a session resolves to the same conversation."""
    writer.record_investigation_trajectories(
        [_trajectory("run-1", "chat-1"), _trajectory("run-2", "chat-1")]
    )
    writer.record_trajectory_bundles([_build_bundle("chat-1")])

    assert list(reader.get_trajectory_bundles(["chat-1"])) == ["chat-1"]


def test_another_agents_conversation_is_not_resolved(
    writer: InMemoryTrajectoryStore, reader: InMemoryTrajectoryReader
) -> None:
    writer.record_trajectory_bundles(
        [_build_bundle("chat-1", observed_agent_key=_OTHER)]
    )

    assert reader.get_trajectory_bundles(["chat-1"]) == {}
