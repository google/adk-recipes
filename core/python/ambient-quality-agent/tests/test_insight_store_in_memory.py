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

"""`InMemoryInsightStore` against the semantics `InsightStore` writes down.

Not a conformance suite, unlike `test_investigation_store_contract`, and the
reason is worth stating. That one runs one body against both implementations
because `FakeInvestigationStore` is a faithful row-level twin of the BigQuery
store. There is no such twin for insights: `conftest.FakeInsightStore` records
calls, and building a real one would mean re-implementing this store's logic a
second time to check it against itself.

So the specification these are read against is the contract's docstrings, and
what holds the two implementations together is that both are written from it.
The cross-check that does exist arrives with the reader: whatever `merge_insight`
did to the data, both must answer every read the same way afterwards.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Sequence
from typing import Any

import pytest
from ambient_quality_agent.standalone.insights import InMemoryInsightStore
from ambient_quality_agent.standalone.store import InMemoryStore, Insights
from ambient_quality_agent.tools.insights.clustering import Cluster
from ambient_quality_agent.tools.insights.models import (
    Insight,
    InsightOccurrence,
    InsightStatus,
    OccurrenceState,
)

_AGENT = "watched-agent"
_OTHER = "somebody-else"
_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


def _never_matches(
    candidates: Sequence[str], existing: Sequence[str]
) -> dict[int, int | None]:
    """Mock matcher that returns no matches for candidates.

    Args:
        candidates: Candidate issue labels to match.
        existing: Existing insight labels to compare against.

    Returns:
        Mapping of candidate indices to None.
    """
    del existing
    return dict.fromkeys(range(len(candidates)))


@pytest.fixture
def backing() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def store(backing: InMemoryStore) -> InMemoryInsightStore:
    """The store under test, with a judge that never matches anything.

    Matching has its own tests in `test_insight_matching`; what these cover is
    which labels the judge is *offered*, which is this store's decision.
    """
    return InMemoryInsightStore(
        backing, agent_name=_AGENT, matcher=_never_matches
    )


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
    occurrence_id: str, insight_id: str, **fields: Any
) -> InsightOccurrence:
    defaults: dict[str, Any] = {
        "agent_name": _AGENT,
        "run_id": "run-1",
        "occurrence_state": OccurrenceState.TRACKED,
        "label": "an issue",
        "created_at": _NOW,
    }
    return InsightOccurrence(
        occurrence_id=occurrence_id,
        insight_id=insight_id,
        **{**defaults, **fields},
    )


def _seed(
    backing: InMemoryStore,
    insights: list[Insight],
    occurrences: list[InsightOccurrence] | None = None,
) -> None:
    backing.update_insights(
        lambda current: Insights(
            {i.insight_id: i for i in insights},
            list(occurrences or []),
            current.root_causes,
        )
    )


# --- what a retry erases ---------------------------------------------------- #


def test_a_retry_drops_the_previous_attempts_occurrences(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    _seed(
        backing,
        [_insight("i1")],
        [
            _occurrence("o1", "i1", run_id="run-1"),
            _occurrence("o2", "i1", run_id="run-2"),
        ],
    )

    store.delete_failed_insights("run-1")

    assert [o.occurrence_id for o in backing.occurrences] == ["o2"]


def test_an_insight_the_cleanup_orphaned_goes_with_it(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """The orphan check reads the occurrence set as it stands *after* the
    removal. Read before, it would spare an insight held up only by the rows
    that just went."""
    _seed(backing, [_insight("i1")], [_occurrence("o1", "i1", run_id="run-1")])

    store.delete_failed_insights("run-1")

    assert backing.insights == {}


def test_an_insight_another_run_still_holds_up_survives(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    _seed(
        backing,
        [_insight("i1")],
        [
            _occurrence("o1", "i1", run_id="run-1"),
            _occurrence("o2", "i1", run_id="run-2"),
        ],
    )

    store.delete_failed_insights("run-1")

    assert set(backing.insights) == {"i1"}


def test_another_agents_records_are_never_touched(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    _seed(
        backing,
        [_insight("theirs", agent_name=_OTHER)],
        [_occurrence("o1", "theirs", agent_name=_OTHER, run_id="run-1")],
    )

    store.delete_failed_insights("run-1")

    assert set(backing.insights) == {"theirs"}
    assert len(backing.occurrences) == 1


# --- which labels the judge is offered -------------------------------------- #


def _offered(store: InMemoryInsightStore) -> list[str]:
    """Captures the candidate labels offered to the matcher in evaluation order.

    Args:
        store: In-memory insight store instance.

    Returns:
        List of existing insight labels presented to the matcher.
    """
    seen: list[str] = []

    def record(
        candidates: Sequence[str], existing: Sequence[str]
    ) -> dict[int, int | None]:
        seen.extend(existing)
        return dict.fromkeys(range(len(candidates)))

    store._matcher = record
    store.find_existing_insights([Cluster(label="a candidate")])
    return seen


def test_a_resolved_insight_is_not_offered(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """A recurrence after resolution mints a new insight rather than reopening
    the old one."""
    _seed(
        backing,
        [_insight("i1", "resolved issue", status=InsightStatus.RESOLVED)],
    )

    assert "resolved issue" not in _offered(store)


def test_a_merged_insight_is_not_offered(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """The next sweep must attach the recurrence to the target an operator
    chose, not to the row they hid."""
    _seed(
        backing,
        [
            _insight("survivor", "the surviving issue"),
            _insight(
                "absorbed",
                "the absorbed issue",
                merged_into_insight_id="survivor",
            ),
        ],
    )

    offered = _offered(store)

    assert "the surviving issue" in offered
    assert "the absorbed issue" not in offered


def test_a_dismissed_insight_is_still_offered(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """It stays hidden either way, and skipping it would mint a fresh insight
    every sweep for exactly the defect somebody asked not to see."""
    _seed(backing, [_insight("i1", "dismissed issue", dismissed_at=_NOW)])

    assert "dismissed issue" in _offered(store)


def test_another_agents_insight_is_not_offered(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    _seed(backing, [_insight("theirs", "their issue", agent_name=_OTHER)])

    assert "their issue" not in _offered(store)


def test_labels_are_offered_newest_first_with_the_id_breaking_ties(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """A sweep stamps one timestamp across everything it touches, so ties are
    routine, and the order decides which batch the judge answers from."""
    later = _NOW + dt.timedelta(days=1)
    _seed(
        backing,
        [
            _insight("i3", "tied, higher id"),
            _insight("i1", "tied, lower id"),
            _insight("i9", "newest", updated_at=later),
        ],
    )

    assert _offered(store) == ["newest", "tied, lower id", "tied, higher id"]


def test_no_candidates_asks_nothing(store: InMemoryInsightStore) -> None:
    assert store.find_existing_insights([]) == {}


def test_an_empty_store_answers_none_for_every_candidate(
    store: InMemoryInsightStore,
) -> None:
    result = store.find_existing_insights(
        [Cluster(label="a"), Cluster(label="b")]
    )

    assert result == {0: None, 1: None}


def test_a_match_comes_back_as_the_insight_id(
    backing: InMemoryStore,
) -> None:
    _seed(backing, [_insight("i1", "the known issue")])
    store = InMemoryInsightStore(
        backing, agent_name=_AGENT, matcher=lambda candidates, existing: {0: 0}
    )

    assert store.find_existing_insights([Cluster(label="a candidate")]) == {
        0: "i1"
    }


# --- what a sweep records --------------------------------------------------- #


def test_a_new_insight_and_its_occurrence_are_both_stored(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    store.save_investigation_result(
        [_insight("fresh")], [], [], [_occurrence("o1", "fresh")], _NOW
    )

    assert set(backing.insights) == {"fresh"}
    assert [o.occurrence_id for o in backing.occurrences] == ["o1"]


def test_a_seen_insight_is_dated_to_this_sweep(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """Dating it is what leaves `resolve_stale_insights` measuring from the last
    sighting rather than the first."""
    _seed(backing, [_insight("i1")])
    later = _NOW + dt.timedelta(days=3)

    store.save_investigation_result([], ["i1"], [], [], later)

    assert backing.insights["i1"].updated_at == later


def test_only_a_confirmed_sighting_changes_the_status(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """An absent verdict is not a recurrence: seen dates it, recurring marks it,
    and the second set is contained in the first."""
    _seed(backing, [_insight("dated"), _insight("marked")])
    later = _NOW + dt.timedelta(days=3)

    store.save_investigation_result(
        [], ["dated", "marked"], ["marked"], [], later
    )

    assert backing.insights["dated"].status is InsightStatus.NEW
    assert backing.insights["marked"].status is InsightStatus.RECURRING
    assert backing.insights["dated"].updated_at == later


def test_a_relabelled_insight_takes_the_wording_verification_settled_on(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """The label a reader sees has to be the label the next sweep matches
    against, or the two disagree about what the insight is."""
    _seed(backing, [_insight("i1", "the raw clustering label")])

    store.save_investigation_result(
        [], ["i1"], [], [], _NOW, relabelled={"i1": "the refined label"}
    )

    assert backing.insights["i1"].label == "the refined label"


def test_a_seen_id_that_belongs_to_another_agent_is_ignored(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    _seed(backing, [_insight("theirs", agent_name=_OTHER)])
    later = _NOW + dt.timedelta(days=3)

    store.save_investigation_result([], ["theirs"], ["theirs"], [], later)

    assert backing.insights["theirs"].updated_at == _NOW
    assert backing.insights["theirs"].status is InsightStatus.NEW


# --- auto-resolution --------------------------------------------------------- #


def test_an_insight_unseen_past_the_window_resolves(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    _seed(backing, [_insight("stale")])

    store.resolve_stale_insights(_NOW + dt.timedelta(days=20), 14)

    assert backing.insights["stale"].status is InsightStatus.RESOLVED


def test_resolving_stamps_its_own_date_rather_than_the_sighting(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """`updated_at` is the last sighting and is what the cutoff reads, so the
    resolution time cannot go there without moving the measurement."""
    _seed(backing, [_insight("stale")])
    now = _NOW + dt.timedelta(days=20)

    store.resolve_stale_insights(now, 14)

    assert backing.insights["stale"].resolved_at == now
    assert backing.insights["stale"].updated_at == _NOW


def test_an_insight_seen_inside_the_window_is_left_alone(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    _seed(backing, [_insight("recent")])

    store.resolve_stale_insights(_NOW + dt.timedelta(days=3), 14)

    assert backing.insights["recent"].status is InsightStatus.NEW


def test_a_zero_window_resolves_nothing(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """The documented "auto-resolve off" setting. Taken literally as a zero-day
    cutoff it would resolve the entire store."""
    _seed(
        backing, [_insight("ancient", updated_at=_NOW - dt.timedelta(days=900))]
    )

    store.resolve_stale_insights(_NOW, 0)

    assert backing.insights["ancient"].status is InsightStatus.NEW


def test_an_already_resolved_insight_keeps_its_first_resolution_date(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    first = _NOW + dt.timedelta(days=20)
    _seed(
        backing,
        [_insight("done", status=InsightStatus.RESOLVED, resolved_at=first)],
    )

    store.resolve_stale_insights(first + dt.timedelta(days=5), 14)

    assert backing.insights["done"].resolved_at == first


# --- dismissing -------------------------------------------------------------- #


def test_dismissing_hides_it_and_reports_that_it_exists(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    _seed(backing, [_insight("i1")])

    assert store.dismiss_insight("i1") is True
    assert backing.insights["i1"].dismissed_at is not None


def test_dismissing_twice_keeps_the_first_judgements_time(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """The button has no undo, so a double click must read as success rather
    than as a second, later decision."""
    _seed(backing, [_insight("i1")])
    store.dismiss_insight("i1")
    first = backing.insights["i1"].dismissed_at

    assert store.dismiss_insight("i1") is True
    assert backing.insights["i1"].dismissed_at == first


def test_dismissing_something_that_does_not_exist_says_so(
    store: InMemoryInsightStore,
) -> None:
    assert store.dismiss_insight("no-such-insight") is False


def test_another_agents_insight_cannot_be_dismissed(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    _seed(backing, [_insight("theirs", agent_name=_OTHER)])

    assert store.dismiss_insight("theirs") is False
    assert backing.insights["theirs"].dismissed_at is None


# --- merging ----------------------------------------------------------------- #


def test_a_merge_moves_the_occurrences_onto_the_survivor(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """Occurrence records are directly moved to the survivor rather than resolving
    a pointer on read."""
    _seed(
        backing,
        [_insight("survivor"), _insight("absorbed")],
        [_occurrence("o1", "survivor"), _occurrence("o2", "absorbed")],
    )

    assert store.merge_insight(
        insight_id="absorbed", target_insight_id="survivor"
    )

    assert {o.insight_id for o in backing.occurrences} == {"survivor"}


def test_a_merge_leaves_a_tombstone_rather_than_a_hole(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """A bookmark on the absorbed id still resolves instead of 404-ing."""
    _seed(backing, [_insight("survivor"), _insight("absorbed")])

    store.merge_insight(insight_id="absorbed", target_insight_id="survivor")

    tombstone = backing.insights["absorbed"]
    assert tombstone.merged_into_insight_id == "survivor"
    assert tombstone.merged_at is not None


def test_the_survivors_span_covers_both(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    early, late = _NOW - dt.timedelta(days=10), _NOW + dt.timedelta(days=10)
    _seed(
        backing,
        [
            _insight("survivor", created_at=_NOW, updated_at=_NOW),
            _insight("absorbed", created_at=early, updated_at=late),
        ],
    )

    store.merge_insight(insight_id="absorbed", target_insight_id="survivor")

    assert backing.insights["survivor"].created_at == early
    assert backing.insights["survivor"].updated_at == late


def test_a_chain_flattens_so_the_first_answers_the_last(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """A into B then B into C must leave A pointing at C. Pointing at B would
    make a bookmark on A resolve to another tombstone."""
    _seed(
        backing,
        [_insight("a"), _insight("b"), _insight("c")],
        [_occurrence("oa", "a"), _occurrence("ob", "b")],
    )

    store.merge_insight(insight_id="a", target_insight_id="b")
    store.merge_insight(insight_id="b", target_insight_id="c")

    assert backing.insights["a"].merged_into_insight_id == "c"
    assert backing.insights["b"].merged_into_insight_id == "c"
    assert {o.insight_id for o in backing.occurrences} == {"c"}


@pytest.mark.parametrize(
    ("label", "source", "target"),
    [
        ("an unknown source", "nope", "survivor"),
        ("an unknown target", "absorbed", "nope"),
        ("itself", "absorbed", "absorbed"),
    ],
)
def test_a_merge_that_cannot_be_made_is_refused(
    store: InMemoryInsightStore,
    backing: InMemoryStore,
    label: str,
    source: str,
    target: str,
) -> None:
    _seed(backing, [_insight("survivor"), _insight("absorbed")])

    assert (
        store.merge_insight(insight_id=source, target_insight_id=target)
        is False
    ), label
    assert backing.insights["absorbed"].merged_into_insight_id is None


def test_merging_into_a_tombstone_is_refused(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    """Allowing it would build the chain depth the flattening exists to avoid."""
    _seed(backing, [_insight("a"), _insight("b"), _insight("c")])
    store.merge_insight(insight_id="b", target_insight_id="c")

    assert store.merge_insight(insight_id="a", target_insight_id="b") is False


def test_another_agents_insight_cannot_be_merged(
    store: InMemoryInsightStore, backing: InMemoryStore
) -> None:
    _seed(
        backing, [_insight("survivor"), _insight("theirs", agent_name=_OTHER)]
    )

    assert (
        store.merge_insight(insight_id="theirs", target_insight_id="survivor")
        is False
    )


# --- what the store itself has to promise ------------------------------------ #


def test_a_reader_never_sees_a_half_applied_write(
    backing: InMemoryStore,
) -> None:
    """Insights and occurrences move together. Rebound one at a time, a reader
    could catch an occurrence whose insight does not exist yet."""
    store = InMemoryInsightStore(backing, agent_name=_AGENT)
    _seed(backing, [_insight("i1")], [_occurrence("o1", "i1")])

    before_insights, before_occurrences = backing.insights, backing.occurrences
    store.save_investigation_result(
        [_insight("i2")], [], [], [_occurrence("o2", "i2")], _NOW
    )

    assert set(before_insights) == {"i1"}
    assert [o.occurrence_id for o in before_occurrences] == ["o1"]
    assert set(backing.insights) == {"i1", "i2"}


def test_concurrent_writers_lose_nothing(backing: InMemoryStore) -> None:
    store = InMemoryInsightStore(backing, agent_name=_AGENT)
    start = threading.Barrier(8)

    def write(n: int) -> None:
        start.wait(timeout=5)
        store.save_investigation_result(
            [_insight(f"i{n}")], [], [], [_occurrence(f"o{n}", f"i{n}")], _NOW
        )

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(backing.insights) == 8
    assert len(backing.occurrences) == 8


def test_the_store_starts_empty(backing: InMemoryStore) -> None:
    assert backing.insights == {}
    assert backing.occurrences == ()
