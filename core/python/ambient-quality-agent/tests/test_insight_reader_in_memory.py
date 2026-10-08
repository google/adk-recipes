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

"""`InMemoryInsightReader` against the semantics `InsightReader` writes down.

Read alongside `test_insight_reader`, which covers the SQL the BigQuery reader
emits for the same contract. The two cannot share a body -- there is no faithful
row-level twin of that reader to run one against -- so what holds them together
is that both are written from the contract's docstrings.

The merge tests are the ones that matter most, because merging is where the two
implementations deliberately hold different data: BigQuery resolves an
indirection on read, this store moved the records when the merge happened. Every
test here asserting what a merged insight and its target answer is asserting the
half of that agreement this side owes.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from ambient_quality_agent.standalone.insight_reader import (
    InMemoryInsightReader,
)
from ambient_quality_agent.standalone.insights import InMemoryInsightStore
from ambient_quality_agent.standalone.store import InMemoryStore, Insights
from ambient_quality_agent.tools.insights.models import (
    Insight,
    InsightOccurrence,
    InsightStatus,
    OccurrenceState,
    RootCause,
)
from ambient_quality_agent.tools.insights.reader import InsightOrder

_AGENT = "watched-agent"
_OTHER = "somebody-else"
_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


@pytest.fixture
def backing() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def reader(backing: InMemoryStore) -> InMemoryInsightReader:
    return InMemoryInsightReader(backing, agent_name=_AGENT)


@pytest.fixture
def writer(backing: InMemoryStore) -> InMemoryInsightStore:
    """The write side, for the tests that merge before reading."""
    return InMemoryInsightStore(backing, agent_name=_AGENT)


def _insight(
    insight_id: str, label: str = "an issue", **fields: Any
) -> Insight:
    defaults: dict[str, Any] = {
        "agent_name": _AGENT,
        "status": InsightStatus.NEW,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return Insight(insight_id=insight_id, label=label, **{**defaults, **fields})


def _occurrence(
    occurrence_id: str, insight_id: str | None, **fields: Any
) -> InsightOccurrence:
    defaults: dict[str, Any] = {
        "agent_name": _AGENT,
        "run_id": "run-1",
        "label": "an issue",
        "occurrence_state": OccurrenceState.TRACKED,
        "created_at": _NOW,
        "trace_count": 1,
    }
    return InsightOccurrence(
        occurrence_id=occurrence_id,
        insight_id=insight_id,
        **{**defaults, **fields},
    )


def _root_cause(
    root_cause_id: str, insight_id: str, **fields: Any
) -> RootCause:
    defaults: dict[str, Any] = {
        "occurrence_id": "o1",
        "agent_revision": "rev-1",
        "summary": "because of a thing",
        "created_at": _NOW,
    }
    return RootCause(
        root_cause_id=root_cause_id,
        insight_id=insight_id,
        **{**defaults, **fields},
    )


def _seed(
    backing: InMemoryStore,
    insights: list[Insight],
    occurrences: list[InsightOccurrence] | None = None,
    root_causes: list[RootCause] | None = None,
) -> None:
    backing.update_insights(
        lambda current: Insights(
            {i.insight_id: i for i in insights},
            list(occurrences or []),
            current.root_causes,
        )
    )
    for record in root_causes or []:
        backing.append_root_cause(record)


def _ids(page: list[Any]) -> list[str]:
    return [view.insight_id for view in page]


# --- what the list hides ---------------------------------------------------- #


def test_the_list_hides_a_dismissed_insight(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    """The Dismiss button only reads as working if the row stops coming back,
    and the client re-reads this list rather than hiding anything itself."""
    _seed(backing, [_insight("kept"), _insight("hidden", dismissed_at=_NOW)])

    page, total = reader.list_insights(limit=10, offset=0)

    assert _ids(page) == ["kept"]
    assert total == 1


def test_the_list_hides_a_merged_insight(
    reader: InMemoryInsightReader,
    writer: InMemoryInsightStore,
    backing: InMemoryStore,
) -> None:
    _seed(backing, [_insight("survivor"), _insight("absorbed")])
    writer.merge_insight(insight_id="absorbed", target_insight_id="survivor")

    page, total = reader.list_insights(limit=10, offset=0)

    assert _ids(page) == ["survivor"]
    assert total == 1


def test_another_agents_insight_is_never_listed(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    _seed(backing, [_insight("mine"), _insight("theirs", agent_name=_OTHER)])

    page, _ = reader.list_insights(limit=10, offset=0)

    assert _ids(page) == ["mine"]


# --- what a direct read still answers --------------------------------------- #


def test_a_dismissed_insight_still_resolves_directly(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    """A bookmarked link must not start 404-ing the moment somebody tidies the
    list."""
    _seed(backing, [_insight("hidden", dismissed_at=_NOW)])

    assert reader.get_insight("hidden") is not None


def test_a_merged_insight_resolves_and_reads_as_zero_sightings(
    reader: InMemoryInsightReader,
    writer: InMemoryInsightStore,
    backing: InMemoryStore,
) -> None:
    """Its evidence counts for the target now, which is what the zero says."""
    _seed(
        backing,
        [_insight("survivor"), _insight("absorbed")],
        [_occurrence("o1", "absorbed")],
    )
    writer.merge_insight(insight_id="absorbed", target_insight_id="survivor")

    view = reader.get_insight("absorbed")

    assert view is not None
    assert view.occurrence_count == 0
    assert view.merged_into_insight_id == "survivor"


def test_an_unknown_insight_is_none(reader: InMemoryInsightReader) -> None:
    assert reader.get_insight("no-such-insight") is None


def test_another_agents_insight_does_not_resolve(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    _seed(backing, [_insight("theirs", agent_name=_OTHER)])

    assert reader.get_insight("theirs") is None


# --- the stats every read leads with ----------------------------------------- #


def test_the_view_counts_sightings_and_conversations(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    _seed(
        backing,
        [_insight("i1")],
        [
            _occurrence("o1", "i1", trace_count=2, trajectory_ids=["t1", "t2"]),
            _occurrence(
                "o2", "i1", trace_count=3, trajectory_ids=["t2", "t3", "t4"]
            ),
        ],
    )

    view = reader.get_insight("i1")

    assert view is not None
    assert view.occurrence_count == 2
    assert view.trace_count == 5, "summed over sightings, not deduplicated"


def test_the_view_names_the_most_recent_sweep(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    later = _NOW + dt.timedelta(days=2)
    _seed(
        backing,
        [_insight("i1")],
        [
            _occurrence("old", "i1", run_id="run-old"),
            _occurrence("new", "i1", run_id="run-new", created_at=later),
        ],
    )

    view = reader.get_insight("i1")

    assert view is not None
    assert view.last_run_id == "run-new"
    assert view.last_run_at == later


def test_an_insight_nothing_has_seen_reads_as_empty(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    _seed(backing, [_insight("i1")])

    view = reader.get_insight("i1")

    assert view is not None
    assert (view.occurrence_count, view.trace_count) == (0, 0)
    assert view.last_run_id is None and view.last_run_at is None


def test_a_merge_moves_the_counts_onto_the_survivor(
    reader: InMemoryInsightReader,
    writer: InMemoryInsightStore,
    backing: InMemoryStore,
) -> None:
    """The agreement with BigQuery, from this side: however the evidence got
    there, the target answers for both afterwards."""
    _seed(
        backing,
        [_insight("survivor"), _insight("absorbed")],
        [
            _occurrence("o1", "survivor", trace_count=1, trajectory_ids=["t1"]),
            _occurrence("o2", "absorbed", trace_count=4, trajectory_ids=["t2"]),
        ],
    )
    writer.merge_insight(insight_id="absorbed", target_insight_id="survivor")

    view = reader.get_insight("survivor")

    assert view is not None
    assert view.occurrence_count == 2
    assert view.trace_count == 5


# --- ordering and paging ------------------------------------------------------ #


def test_recent_orders_by_last_seen_with_the_id_breaking_ties(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    """The tie-break is what stops a row being skipped or repeated between
    pages, and ties are routine because one sweep stamps one timestamp."""
    later = _NOW + dt.timedelta(days=1)
    _seed(
        backing,
        [
            _insight("b", updated_at=_NOW),
            _insight("a", updated_at=_NOW),
            _insight("z", updated_at=later),
        ],
    )

    page, _ = reader.list_insights(limit=10, offset=0)

    assert _ids(page) == ["z", "a", "b"]


def test_impact_orders_by_conversations_affected(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    _seed(
        backing,
        [_insight("small"), _insight("big"), _insight("none")],
        [
            _occurrence("o1", "small", trace_count=1),
            _occurrence("o2", "big", trace_count=9),
        ],
    )

    page, _ = reader.list_insights(
        limit=10, offset=0, order_by=InsightOrder.IMPACT
    )

    assert _ids(page) == ["big", "small", "none"], "unseen insights rank last"


def test_paging_walks_the_ranking_without_repeating_a_row(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    _seed(backing, [_insight(f"i{n}") for n in range(5)])

    first, total = reader.list_insights(limit=2, offset=0)
    second, _ = reader.list_insights(limit=2, offset=2)
    third, _ = reader.list_insights(limit=2, offset=4)

    assert total == 5
    assert len(_ids(first) + _ids(second) + _ids(third)) == 5
    assert len(set(_ids(first) + _ids(second) + _ids(third))) == 5


def test_the_total_describes_the_whole_match_not_the_page(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    _seed(backing, [_insight(f"i{n}") for n in range(5)])

    page, total = reader.list_insights(limit=2, offset=0)

    assert len(page) == 2
    assert total == 5


# --- the list's filters -------------------------------------------------------- #


def test_the_status_filter_selects_lifecycle_states(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    _seed(
        backing,
        [
            _insight("new"),
            _insight("recurring", status=InsightStatus.RECURRING),
        ],
    )

    page, _ = reader.list_insights(
        limit=10, offset=0, statuses=[InsightStatus.RECURRING]
    )

    assert _ids(page) == ["recurring"]


def test_the_window_filters_on_the_last_sighting(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    """ "Insights in the last week" means the issues live that week. On
    `created_at` it would hide a defect that has recurred daily for a month."""
    _seed(
        backing,
        [
            _insight(
                "live",
                created_at=_NOW - dt.timedelta(days=90),
                updated_at=_NOW,
            ),
            _insight(
                "gone",
                created_at=_NOW - dt.timedelta(days=90),
                updated_at=_NOW - dt.timedelta(days=60),
            ),
        ],
    )

    page, _ = reader.list_insights(
        limit=10,
        offset=0,
        window_start=(_NOW - dt.timedelta(days=7)).isoformat(),
    )

    assert _ids(page) == ["live"]


def test_the_day_filter_keeps_what_was_found_or_resolved_that_day(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    """The rows behind the per-day chart's two series, and no others. A UTC
    day, as the chart's is: 01:00 at +02:00 is still the day before."""
    day = _NOW.date()
    earlier = _NOW - dt.timedelta(days=30)
    plus_two = dt.timezone(dt.timedelta(hours=2))
    _seed(
        backing,
        [
            # Found that day and seen again since: the chart's new one still.
            _insight("found", status=InsightStatus.RECURRING, updated_at=_NOW),
            _insight(
                "found-late",
                created_at=dt.datetime(2026, 6, 2, 1, 0, tzinfo=plus_two),
            ),
            _insight(
                "resolved",
                status=InsightStatus.RESOLVED,
                created_at=earlier,
                updated_at=earlier,
                resolved_at=_NOW,
            ),
            # Only seen that day: neither of the changes the chart counts.
            _insight(
                "seen", status=InsightStatus.RECURRING, created_at=earlier
            ),
            _insight("found-before", created_at=_NOW - dt.timedelta(days=1)),
        ],
    )

    every, total = reader.list_insights(limit=10, offset=0, day=day.isoformat())
    new, _ = reader.list_insights(
        limit=10, offset=0, statuses=[InsightStatus.NEW], day=day.isoformat()
    )
    resolved, _ = reader.list_insights(
        limit=10,
        offset=0,
        statuses=[InsightStatus.RESOLVED],
        day=day.isoformat(),
    )
    recurring, _ = reader.list_insights(
        limit=10,
        offset=0,
        statuses=[InsightStatus.RECURRING],
        day=day.isoformat(),
    )

    assert sorted(_ids(every)) == ["found", "found-late", "resolved"]
    assert total == 3
    assert sorted(_ids(new)) == ["found", "found-late"]
    assert _ids(resolved) == ["resolved"]
    # No change of that kind is dated to a day.
    assert recurring == []


def test_the_day_filter_skips_a_resolved_row_with_no_resolution_stamp(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    """Rows resolved before ``resolved_at`` existed carry no day to file them
    under, so no day's resolved list shows them."""
    earlier = _NOW - dt.timedelta(days=30)
    _seed(
        backing,
        [
            _insight(
                "unstamped",
                status=InsightStatus.RESOLVED,
                created_at=earlier,
                updated_at=_NOW,
                resolved_at=None,
            )
        ],
    )

    page, total = reader.list_insights(
        limit=10,
        offset=0,
        statuses=[InsightStatus.RESOLVED],
        day=_NOW.date().isoformat(),
    )

    assert page == []
    assert total == 0


def test_the_day_and_run_filters_both_narrow_the_list(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    earlier = _NOW - dt.timedelta(days=30)
    _seed(
        backing,
        [
            _insight("both"),
            _insight("day-only"),
            _insight("run-only", created_at=earlier, updated_at=earlier),
        ],
        [
            _occurrence("o1", "both", run_id="the-sweep"),
            _occurrence("o2", "run-only", run_id="the-sweep"),
        ],
    )

    page, total = reader.list_insights(
        limit=10, offset=0, run_id="the-sweep", day=_NOW.date().isoformat()
    )

    assert _ids(page) == ["both"]
    assert total == 1


def test_the_run_filter_selects_by_owner(
    reader: InMemoryInsightReader,
    writer: InMemoryInsightStore,
    backing: InMemoryStore,
) -> None:
    """A sweep that found a defect under a label an operator has since called a
    duplicate still found it, and the target is what now answers for it."""
    _seed(
        backing,
        [_insight("survivor"), _insight("absorbed")],
        [_occurrence("o1", "absorbed", run_id="the-sweep")],
    )
    writer.merge_insight(insight_id="absorbed", target_insight_id="survivor")

    page, _ = reader.list_insights(limit=10, offset=0, run_id="the-sweep")

    assert _ids(page) == ["survivor"]


def test_the_root_cause_filter_splits_diagnosed_from_not(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    _seed(
        backing,
        [_insight("diagnosed"), _insight("undiagnosed")],
        [],
        [_root_cause("rc1", "diagnosed")],
    )

    with_rc, _ = reader.list_insights(limit=10, offset=0, has_root_cause=True)
    without_rc, _ = reader.list_insights(
        limit=10, offset=0, has_root_cause=False
    )

    assert _ids(with_rc) == ["diagnosed"]
    assert _ids(without_rc) == ["undiagnosed"]


# --- the header above the list -------------------------------------------------- #


def test_conversations_are_counted_once_however_many_sweeps_saw_them(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    """`trace_count` counts a conversation again per sweep and per insight; this
    is the number that does not."""
    _seed(
        backing,
        [_insight("i1"), _insight("i2")],
        [
            _occurrence("o1", "i1", trajectory_ids=["t1", "t2"]),
            _occurrence("o2", "i1", trajectory_ids=["t1", "t2"]),
            _occurrence("o3", "i2", trajectory_ids=["t2", "t3"]),
        ],
    )

    assert reader.count_affected_conversations() == 3


def test_the_header_and_the_list_describe_one_set(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    """One predicate, shared. A header that counted the hidden rows would state
    a total the list beneath it cannot account for."""
    _seed(
        backing,
        [_insight("kept"), _insight("hidden", dismissed_at=_NOW)],
        [
            _occurrence("o1", "kept", trajectory_ids=["t1"]),
            _occurrence("o2", "hidden", trajectory_ids=["t2"]),
        ],
    )

    assert reader.count_affected_conversations() == 1


# --- the evidence ---------------------------------------------------------------- #


def test_occurrences_come_back_newest_first(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    _seed(
        backing,
        [_insight("i1")],
        [
            _occurrence("old", "i1"),
            _occurrence("new", "i1", created_at=_NOW + dt.timedelta(days=1)),
        ],
    )

    page, total = reader.list_occurrences(insight_id="i1", limit=10, offset=0)

    assert [o.occurrence_id for o in page] == ["new", "old"]
    assert total == 2


def test_an_occurrence_naming_no_insight_is_never_returned(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    """It records a candidate verification rejected or never judged: evidence
    about the sweep, not a sighting of a tracked issue."""
    _seed(
        backing,
        [_insight("i1")],
        [
            _occurrence(
                "rejected", None, occurrence_state=OccurrenceState.REJECTED
            )
        ],
    )

    page, total = reader.list_occurrences(
        occurrence_id="rejected", limit=10, offset=0
    )

    assert page == [] and total == 0


def test_a_merged_insight_shows_no_evidence_of_its_own(
    reader: InMemoryInsightReader,
    writer: InMemoryInsightStore,
    backing: InMemoryStore,
) -> None:
    """Its header says zero sightings; listing some would contradict that."""
    _seed(
        backing,
        [_insight("survivor"), _insight("absorbed")],
        [_occurrence("o1", "absorbed")],
    )
    writer.merge_insight(insight_id="absorbed", target_insight_id="survivor")

    absorbed, _ = reader.list_occurrences(
        insight_id="absorbed", limit=10, offset=0
    )
    survivor, _ = reader.list_occurrences(
        insight_id="survivor", limit=10, offset=0
    )

    assert absorbed == []
    assert [o.occurrence_id for o in survivor] == ["o1"]


def test_listing_occurrences_needs_something_to_bound_it(
    reader: InMemoryInsightReader,
) -> None:
    with pytest.raises(ValueError, match="insight_id or an occurrence_id"):
        reader.list_occurrences(limit=10, offset=0)


def test_the_detail_read_pairs_the_view_with_its_evidence(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    _seed(backing, [_insight("i1")], [_occurrence("o1", "i1")])

    result = reader.get_insight_with_occurrences(
        insight_id="i1", limit=10, offset=0
    )

    assert result is not None
    view, occurrences, total = result
    assert view.insight_id == "i1"
    assert [o.occurrence_id for o in occurrences] == ["o1"]
    assert total == 1


def test_the_detail_read_of_an_unknown_insight_is_none(
    reader: InMemoryInsightReader,
) -> None:
    assert (
        reader.get_insight_with_occurrences(
            insight_id="nope", limit=10, offset=0
        )
        is None
    )


# --- diagnoses --------------------------------------------------------------------- #


def test_only_the_current_diagnosis_per_occurrence_comes_back(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    _seed(
        backing,
        [_insight("i1")],
        [],
        [
            _root_cause("first", "i1", occurrence_id="o1"),
            _root_cause(
                "second",
                "i1",
                occurrence_id="o1",
                created_at=_NOW + dt.timedelta(1),
            ),
        ],
    )

    assert [r.root_cause_id for r in reader.list_root_causes("i1")] == [
        "second"
    ]


def test_history_returns_every_revision(
    reader: InMemoryInsightReader, backing: InMemoryStore
) -> None:
    _seed(
        backing,
        [_insight("i1")],
        [],
        [
            _root_cause("first", "i1", occurrence_id="o1"),
            _root_cause(
                "second",
                "i1",
                occurrence_id="o1",
                created_at=_NOW + dt.timedelta(1),
            ),
        ],
    )

    records = reader.list_root_causes("i1", history=True)

    assert [r.root_cause_id for r in records] == ["second", "first"]


def test_a_merge_moves_the_diagnoses_too(
    reader: InMemoryInsightReader,
    writer: InMemoryInsightStore,
    backing: InMemoryStore,
) -> None:
    """A diagnosis of the duplicate is a diagnosis of the target. Leaving it
    behind would make the counts say one thing and the badge another."""
    _seed(
        backing,
        [_insight("survivor"), _insight("absorbed")],
        [_occurrence("o1", "absorbed")],
        [_root_cause("rc1", "absorbed")],
    )
    writer.merge_insight(insight_id="absorbed", target_insight_id="survivor")

    assert [r.root_cause_id for r in reader.list_root_causes("survivor")] == [
        "rc1"
    ]
    assert reader.list_root_causes("absorbed") == []


def test_the_diagnosed_badge_follows_the_merge(
    reader: InMemoryInsightReader,
    writer: InMemoryInsightStore,
    backing: InMemoryStore,
) -> None:
    _seed(
        backing,
        [_insight("survivor"), _insight("absorbed")],
        [],
        [_root_cause("rc1", "absorbed")],
    )
    writer.merge_insight(insight_id="absorbed", target_insight_id="survivor")

    view = reader.get_insight("survivor")

    assert view is not None and view.has_root_cause
