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

"""Tests for the orchestrator insight tools.

Fakes go in behind the `reader_factory` and `store_factory` seams, so these
exercise the tools' argument handling, pagination-token round-tripping and JSON
shaping without any BigQuery. The SQL itself is covered by
`test_insight_reader.py` and `test_insight_store.py`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest
from ambient_quality_agent.tools.insights.findings import Finding
from ambient_quality_agent.tools.insights.models import (
    InsightOccurrence,
    InsightStatus,
    InsightView,
    OccurrenceState,
    ProposedEdit,
    RootCause,
    RubricExample,
)
from ambient_quality_agent.tools.insights.reader import InsightOrder
from ambient_quality_agent.tools.orchestrator import (
    insight_tools,
    paging,
    trajectory_tools,
)
from ambient_quality_agent.tools.trajectories.models import (
    IngestStatus,
    Trajectory,
)
from google.api_core.exceptions import NotFound

from .conftest import StateContext

_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


def _call_list(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Executes the async `list_insights` tool synchronously for testing.

    Args:
        *args: Positional arguments for `list_insights`.
        **kwargs: Keyword arguments for `list_insights`.

    Returns:
        Dictionary result from `list_insights`.
    """
    return asyncio.run(insight_tools.list_insights(*args, **kwargs))


def _call_get(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Executes the async `get_insight` tool synchronously for testing.

    Args:
        *args: Positional arguments for `get_insight`.
        **kwargs: Keyword arguments for `get_insight`.

    Returns:
        Dictionary result from `get_insight`.
    """
    return asyncio.run(insight_tools.get_insight(*args, **kwargs))


def _summary(insight_id: str = "ins-1", count: int = 3) -> InsightView:
    return InsightView(
        insight_id=insight_id,
        agent_name="agent",
        label="made no tool call",
        status=InsightStatus.RECURRING,
        created_at=_NOW,
        updated_at=_NOW,
        occurrence_count=count,
        last_run_id="run-9",
        last_run_at=_NOW,
    )


def _occurrence(
    run_id: str = "run-9",
    with_trace: bool = True,
    trajectory_ids: list[str] | None = None,
) -> InsightOccurrence:
    return InsightOccurrence(
        occurrence_id=f"occ-{run_id}",
        insight_id="ins-1",
        occurrence_state=OccurrenceState.TRACKED,
        run_id=run_id,
        created_at=_NOW,
        agent_name="agent",
        label="made no tool call",
        item_count=1,
        trajectory_ids=trajectory_ids if trajectory_ids is not None else [],
        rubrics=[
            RubricExample(
                rubric=Finding(
                    expected_behavior="call create_ticket",
                    actual_behavior="no tool call was made",
                    session_id="case-1",
                ),
                eval_case_id="case-1",
                trace=[{"role": "user"}] if with_trace else [],
            )
        ],
    )


class _FakeReader:
    """Records read-method calls and returns canned pages."""

    def __init__(
        self,
        *,
        summaries: list[InsightView] | None = None,
        total: int = 0,
        insight: InsightView | None = None,
        occurrences: list[InsightOccurrence] | None = None,
        occ_total: int = 0,
        root_causes: list[RootCause] | None = None,
        conversations: int = 0,
    ) -> None:
        self._summaries = summaries or []
        self._total = total
        self._insight = insight
        self._occurrences = occurrences or []
        self._occ_total = occ_total
        self._root_causes = root_causes or []
        self._conversations = conversations
        self.list_calls: list[dict[str, Any]] = []
        self.conversation_calls: list[dict[str, Any]] = []
        self.occ_calls: list[dict[str, Any]] = []
        self.root_cause_calls: list[dict[str, Any]] = []

    def list_insights(
        self,
        *,
        statuses,
        run_id,
        limit,
        offset,
        window_start=None,
        window_end=None,
        has_root_cause=None,
        order_by=InsightOrder.RECENT,
        day=None,
    ):
        self.list_calls.append(
            {
                "day": day,
                "statuses": statuses,
                "run_id": run_id,
                "limit": limit,
                "offset": offset,
                "window_start": window_start,
                "window_end": window_end,
                "has_root_cause": has_root_cause,
                "order_by": order_by,
            }
        )
        return self._summaries, self._total

    def count_affected_conversations(
        self,
        *,
        statuses,
        run_id,
        window_start=None,
        window_end=None,
        has_root_cause=None,
        day=None,
    ):
        self.conversation_calls.append(
            {
                "day": day,
                "statuses": statuses,
                "run_id": run_id,
                "window_start": window_start,
                "window_end": window_end,
                "has_root_cause": has_root_cause,
            }
        )
        return self._conversations

    def get_insight(self, insight_id):
        return self._insight

    def list_root_causes(self, insight_id, *, history=False):
        self.root_cause_calls.append(
            {"insight_id": insight_id, "history": history}
        )
        return [r for r in self._root_causes if r.insight_id == insight_id]

    def list_occurrences(self, *, insight_id, run_id, limit, offset):
        self.occ_calls.append(
            {
                "insight_id": insight_id,
                "run_id": run_id,
                "limit": limit,
                "offset": offset,
            }
        )
        return self._occurrences, self._occ_total

    def get_insight_with_occurrences(
        self, *, insight_id, run_id, limit, offset
    ):
        # Mirror the real reader: compose the summary + occurrence reads, so the
        # tool tests still observe the recorded occurrence call.
        view = self.get_insight(insight_id)
        if view is None:
            return None
        occurrences, total = self.list_occurrences(
            insight_id=insight_id, run_id=run_id, limit=limit, offset=offset
        )
        return view, occurrences, total


@pytest.fixture
def use_reader(monkeypatch: pytest.MonkeyPatch):
    """Return a setter that installs a fake reader behind the tool factory."""

    def _install(reader: _FakeReader) -> _FakeReader:
        monkeypatch.setattr(
            insight_tools, "reader_factory", lambda state: reader
        )
        return reader

    return _install


# --- page token codec ---------------------------------------------------------


def test_page_token_round_trip() -> None:
    assert paging.decode_page_token(paging.encode_page_token(60)) == 60
    assert paging.decode_page_token("") == 0
    assert paging.decode_page_token("not-a-token") == 0


# --- list_insights ------------------------------------------------------------


def test_list_insights_returns_page_and_next_token(use_reader) -> None:
    # 30 returned out of 70 total -> there is a next page.
    reader = use_reader(
        _FakeReader(
            summaries=[_summary(f"ins-{i}") for i in range(30)], total=70
        )
    )

    out = _call_list(StateContext())

    assert len(out["insights"]) == 30
    assert out["total"] == 70
    assert out["next_page_token"] is not None
    assert reader.list_calls[0]["limit"] == insight_tools.INSIGHTS_PAGE_SIZE
    assert reader.list_calls[0]["offset"] == 0
    # Results are JSON-safe dicts, not models.
    assert out["insights"][0]["status"] == "RECURRING"


def test_list_insights_last_page_has_no_next_token(use_reader) -> None:
    use_reader(_FakeReader(summaries=[_summary()], total=1))

    out = _call_list(StateContext())

    assert out["next_page_token"] is None


def test_list_insights_counts_conversations_over_the_whole_set(
    use_reader,
) -> None:
    """The page is capped; the count is not. A caller that summed what it was
    handed would state a fraction of the deployment as the total."""
    use_reader(
        _FakeReader(
            summaries=[_summary(f"ins-{i}") for i in range(30)],
            total=70,
            conversations=41,
        )
    )

    out = _call_list(StateContext())

    assert out["conversations"] == 41


def test_list_insights_counts_conversations_under_the_page_filters(
    use_reader,
) -> None:
    """Two reads, one question. A count over a different set than the list
    describes would be a figure about something the reader cannot see."""
    reader = use_reader(_FakeReader(summaries=[_summary()], total=1))

    _call_list(
        StateContext(),
        status="NEW",
        run_id="run-7",
        has_root_cause="true",
        window_start="2026-06-01T00:00:00Z",
    )

    listed = dict(reader.list_calls[0])
    # Paging and ranking shape the page, not the set the count covers.
    del listed["limit"], listed["offset"], listed["order_by"]
    assert reader.conversation_calls[0] == listed


def test_list_insights_forwards_a_day_to_the_list_and_its_header(
    use_reader,
) -> None:
    reader = use_reader(_FakeReader(summaries=[], total=0))

    out = _call_list(StateContext(), status="new", day="2026-09-20")

    assert "error" not in out
    assert reader.list_calls[0]["day"] == "2026-09-20"
    assert reader.list_calls[0]["statuses"] == [InsightStatus.NEW]
    assert reader.conversation_calls[0]["day"] == "2026-09-20"


@pytest.mark.parametrize(
    "day", ["2026-02-30", "20260920", "2026-9-20", "yesterday"]
)
def test_list_insights_refuses_a_day_that_is_not_one(
    use_reader, day: str
) -> None:
    """Refused rather than dropped: a caller that asked for one day and got
    every insight would read the whole list as that day's."""
    reader = use_reader(_FakeReader())

    out = _call_list(StateContext(), day=day)

    assert "YYYY-MM-DD" in out["error"]
    assert reader.list_calls == []


def test_list_insights_refuses_recurring_on_a_day(use_reader) -> None:
    """No day is stamped with a recurrence, so the answer would always be an
    empty list that reads as "nothing recurred"."""
    reader = use_reader(_FakeReader())

    out = _call_list(StateContext(), status="RECURRING", day="2026-09-20")

    assert "RECURRING" in out["error"]
    assert reader.list_calls == []


def test_list_insights_page_token_advances_offset(use_reader) -> None:
    reader = use_reader(_FakeReader(summaries=[_summary()], total=100))
    token = paging.encode_page_token(30)

    _call_list(StateContext(), page_token=token)

    assert reader.list_calls[0]["offset"] == 30


def test_list_insights_status_filter_parsed(use_reader) -> None:
    reader = use_reader(_FakeReader(summaries=[], total=0))

    _call_list(StateContext(), status="recurring")

    assert reader.list_calls[0]["statuses"] == [InsightStatus.RECURRING]


def test_list_insights_invalid_status_returns_error(use_reader) -> None:
    use_reader(_FakeReader())

    out = _call_list(StateContext(), status="bogus")

    assert "error" in out


def test_list_insights_all_status_is_no_filter(use_reader) -> None:
    reader = use_reader(_FakeReader(summaries=[], total=0))

    _call_list(StateContext(), status="all")

    assert reader.list_calls[0]["statuses"] is None


def test_list_insights_defaults_to_the_recent_order(use_reader) -> None:
    reader = use_reader(_FakeReader(summaries=[], total=0))

    _call_list(StateContext())

    assert reader.list_calls[0]["order_by"] is InsightOrder.RECENT


def test_list_insights_order_reaches_the_reader(use_reader) -> None:
    reader = use_reader(_FakeReader(summaries=[], total=0))

    _call_list(StateContext(), order_by="impact")

    assert reader.list_calls[0]["order_by"] is InsightOrder.IMPACT


def test_list_insights_invalid_order_returns_error(use_reader) -> None:
    # Not a silent fallback to the default: the caller asked for a ranking, and
    # reading another one as "the top" is the mistake worth refusing.
    reader = use_reader(_FakeReader(summaries=[], total=0))

    out = _call_list(StateContext(), order_by="worst")

    assert "invalid order_by" in out["error"]
    assert reader.list_calls == []


def test_list_insights_page_size_reaches_the_reader(use_reader) -> None:
    reader = use_reader(_FakeReader(summaries=[], total=0))

    _call_list(StateContext(), page_size=10)

    assert reader.list_calls[0]["limit"] == 10


def test_list_insights_page_size_is_capped(use_reader) -> None:
    reader = use_reader(_FakeReader(summaries=[], total=0))

    _call_list(StateContext(), page_size=5_000)

    assert reader.list_calls[0]["limit"] == insight_tools.MAX_INSIGHTS_PAGE_SIZE


def test_list_insights_unset_page_size_takes_the_default(use_reader) -> None:
    reader = use_reader(_FakeReader(summaries=[], total=0))

    _call_list(StateContext(), page_size=0)
    _call_list(StateContext(), page_size=-1)

    assert [call["limit"] for call in reader.list_calls] == [
        insight_tools.INSIGHTS_PAGE_SIZE,
        insight_tools.INSIGHTS_PAGE_SIZE,
    ]


def test_list_insights_offers_no_page_after_an_exact_last_one(
    use_reader,
) -> None:
    """A final page that is full still ends the listing.

    Thirty matches at ten a page: the third page fills itself, and the token is
    what says there is no fourth. An off-by-one here is a page of nothing that
    a client is invited to fetch.
    """
    use_reader(
        _FakeReader(
            summaries=[_summary(f"ins-{i}") for i in range(10)], total=30
        )
    )

    out = _call_list(
        StateContext(), page_size=10, page_token=paging.encode_page_token(20)
    )

    assert len(out["insights"]) == 10
    assert out["next_page_token"] is None


def test_list_insights_next_token_follows_the_page_size(use_reader) -> None:
    # The token is an offset, so a short page has to advance by what it held
    # rather than by the default -- otherwise paging skips rows.
    reader = use_reader(
        _FakeReader(
            summaries=[_summary(f"ins-{i}") for i in range(10)], total=70
        )
    )

    out = _call_list(StateContext(), page_size=10)
    _call_list(StateContext(), page_size=10, page_token=out["next_page_token"])

    assert reader.list_calls[1]["offset"] == 10


# --- get_insight --------------------------------------------------------------


def test_get_insight_requires_id(use_reader) -> None:
    use_reader(_FakeReader())
    assert "error" in _call_get(StateContext(), insight_id="")


def test_get_insight_unknown_id_errors(use_reader) -> None:
    use_reader(_FakeReader(insight=None))

    out = _call_get(StateContext(), insight_id="missing")

    assert "error" in out


def test_get_insight_default_returns_full_history(use_reader) -> None:
    reader = use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence(f"run-{i}") for i in range(30)],
            occ_total=90,
        )
    )

    out = _call_get(StateContext(), insight_id="ins-1")

    assert out["insight"]["insight_id"] == "ins-1"
    # Default view pages the whole occurrence history (no run_id filter).
    assert len(out["occurrences"]) == 30
    assert out["next_page_token"] is not None
    assert reader.occ_calls[0]["limit"] == insight_tools.OCCURRENCES_PAGE_SIZE
    assert reader.occ_calls[0]["run_id"] is None


def test_get_insight_run_id_filters_occurrence(use_reader) -> None:
    reader = use_reader(
        _FakeReader(
            insight=_summary(), occurrences=[_occurrence("run-3")], occ_total=1
        )
    )

    out = _call_get(StateContext(), insight_id="ins-1", run_id="run-3")

    assert reader.occ_calls[0]["run_id"] == "run-3"
    assert out["next_page_token"] is None


def test_get_insight_can_strip_traces(use_reader) -> None:
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence(with_trace=True)],
            occ_total=1,
        )
    )

    out = _call_get(StateContext(), insight_id="ins-1", include_traces=False)

    assert out["occurrences"][0]["rubrics"][0]["trace"] == []


def test_get_insight_keeps_traces_by_default(use_reader) -> None:
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence(with_trace=True)],
            occ_total=1,
        )
    )

    out = _call_get(StateContext(), insight_id="ins-1")

    assert out["occurrences"][0]["rubrics"][0]["trace"] == [{"role": "user"}]


# --- resolving an occurrence's trajectories -----------------------------------


class _FakeTrajectoryReader:
    """Returns canned trajectory rows for the pairs it is asked about."""

    def __init__(
        self, rows: dict[tuple[str, str], Trajectory] | None = None
    ) -> None:
        self._rows = rows or {}
        self.calls: list[list[tuple[str, str]]] = []

    def get_trajectories(self, keys):
        self.calls.append(list(keys))
        return {key: row for key, row in self._rows.items() if key in set(keys)}


@pytest.fixture
def use_trajectories(monkeypatch: pytest.MonkeyPatch):
    """Install a fake behind the trajectory tools' reader seam.

    `get_insight` reaches it through that module rather than through a seam of
    its own, so this is the one place to swap.
    """

    def _install(reader: _FakeTrajectoryReader) -> _FakeTrajectoryReader:
        monkeypatch.setattr(
            trajectory_tools, "reader_factory", lambda state: reader
        )
        return reader

    return _install


def _trajectory(
    trajectory_id: str, trace_ids: list[str] | None = None
) -> Trajectory:
    return Trajectory(
        run_id="run-9",
        agent_name="agent",
        trajectory_id=trajectory_id,
        created_at=_NOW,
        source="cloud_ops",
        source_trace_ids=["trace-1"] if trace_ids is None else trace_ids,
        ingest_status=IngestStatus.INGESTED,
    )


def test_an_occurrence_names_its_conversations_and_links_to_them(
    use_reader, use_trajectories
) -> None:
    """The payoff of the ids column: an insight resolves to the conversations it
    was found in, each openable in the console."""
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence(trajectory_ids=["case-1"])],
            occ_total=1,
        )
    )
    use_trajectories(
        _FakeTrajectoryReader({("run-9", "case-1"): _trajectory("case-1")})
    )

    out = _call_get(StateContext(), insight_id="ins-1")

    trajectories = out["occurrences"][0]["trajectories"]
    assert [t["trajectory_id"] for t in trajectories] == ["case-1"]
    assert trajectories[0]["console_url"].endswith("&tid=trace-1")


def test_the_conversations_keep_the_order_the_occurrence_stored(
    use_reader, use_trajectories
) -> None:
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence(trajectory_ids=["case-2", "case-1"])],
            occ_total=1,
        )
    )
    use_trajectories(
        _FakeTrajectoryReader(
            {
                ("run-9", "case-1"): _trajectory("case-1"),
                ("run-9", "case-2"): _trajectory("case-2"),
            }
        )
    )

    out = _call_get(StateContext(), insight_id="ins-1")

    assert [
        t["trajectory_id"] for t in out["occurrences"][0]["trajectories"]
    ] == [
        "case-2",
        "case-1",
    ]


def test_an_id_the_store_never_saw_is_left_out(
    use_reader, use_trajectories
) -> None:
    """The trajectory store is younger than the insight tables, so an older
    sweep's occurrence names conversations that were never recorded."""
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence(trajectory_ids=["case-1", "case-old"])],
            occ_total=1,
        )
    )
    use_trajectories(
        _FakeTrajectoryReader({("run-9", "case-1"): _trajectory("case-1")})
    )

    out = _call_get(StateContext(), insight_id="ins-1")

    assert [
        t["trajectory_id"] for t in out["occurrences"][0]["trajectories"]
    ] == ["case-1"]


def test_the_page_is_resolved_in_one_read(use_reader, use_trajectories) -> None:
    """Not one query per occurrence: a page is thirty of them."""
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[
                _occurrence(run_id="run-9", trajectory_ids=["case-1"]),
                _occurrence(run_id="run-8", trajectory_ids=["case-2"]),
            ],
            occ_total=2,
        )
    )
    reader = use_trajectories(_FakeTrajectoryReader())

    _call_get(StateContext(), insight_id="ins-1")

    assert len(reader.calls) == 1
    assert reader.calls[0] == [("run-9", "case-1"), ("run-8", "case-2")]


def test_the_links_can_be_had_without_the_conversations(
    use_reader, use_trajectories
) -> None:
    """What the dashboard asks for: a way out to each conversation, and not the
    conversation itself. The two are different costs -- a rubric's trace is a
    heavy payload already on the row, this is one extra query for a few ids --
    so a reader has to be able to ask for one without the other."""
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence(trajectory_ids=["case-1"])],
            occ_total=1,
        )
    )
    use_trajectories(
        _FakeTrajectoryReader({("run-9", "case-1"): _trajectory("case-1")})
    )

    out = _call_get(
        StateContext(),
        insight_id="ins-1",
        include_traces=False,
        include_trajectories=True,
    )

    occurrence = out["occurrences"][0]
    assert occurrence["trajectories"][0]["console_url"].endswith("&tid=trace-1")
    assert all(rubric["trace"] == [] for rubric in occurrence["rubrics"])


def test_the_conversations_can_be_had_without_the_links(
    use_reader, use_trajectories
) -> None:
    """The other way round, so the flag is a real axis rather than a rename of
    the first: the extra query is skipped and the rubric evidence is not."""
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence(trajectory_ids=["case-1"])],
            occ_total=1,
        )
    )
    reader = use_trajectories(_FakeTrajectoryReader())

    out = _call_get(
        StateContext(),
        insight_id="ins-1",
        include_traces=True,
        include_trajectories=False,
    )

    assert reader.calls == []
    assert out["occurrences"][0]["trajectories"] == []


def test_unset_the_links_follow_the_conversations(
    use_reader, use_trajectories
) -> None:
    """The default a harness wants -- the conversations and their provenance
    together, or neither -- so no existing caller changes behaviour."""
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence(trajectory_ids=["case-1"])],
            occ_total=1,
        )
    )
    use_trajectories(
        _FakeTrajectoryReader({("run-9", "case-1"): _trajectory("case-1")})
    )

    out = _call_get(StateContext(), insight_id="ins-1", include_traces=True)

    assert out["occurrences"][0]["trajectories"] != []


def test_stripping_traces_also_skips_the_extra_read(
    use_reader, use_trajectories
) -> None:
    """`include_traces=False` is the cheap scan; a second query would undo that.

    Still true when nothing asks otherwise: `include_trajectories` is what a
    caller that wants the links without the conversations sets, and unset
    leaves this exactly as it was."""
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence(trajectory_ids=["case-1"])],
            occ_total=1,
        )
    )
    reader = use_trajectories(_FakeTrajectoryReader())

    out = _call_get(StateContext(), insight_id="ins-1", include_traces=False)

    assert reader.calls == []
    assert out["occurrences"][0]["trajectories"] == []


def test_an_occurrence_with_no_ids_asks_for_nothing(
    use_reader, use_trajectories
) -> None:
    """Every occurrence written before the ids column."""
    use_reader(
        _FakeReader(
            insight=_summary(), occurrences=[_occurrence()], occ_total=1
        )
    )
    reader = use_trajectories(_FakeTrajectoryReader())

    out = _call_get(StateContext(), insight_id="ins-1")

    assert reader.calls == []
    assert out["occurrences"][0]["trajectories"] == []


def test_a_failed_resolution_does_not_cost_the_caller_its_evidence(
    use_reader, use_trajectories
) -> None:
    """A dataset whose deploy has not caught up has no trajectories table, and
    the rubrics are still what a harness came for."""

    class _Boom(_FakeTrajectoryReader):
        def get_trajectories(self, keys):
            raise RuntimeError("table not found")

    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence(trajectory_ids=["case-1"])],
            occ_total=1,
        )
    )
    use_trajectories(_Boom())

    out = _call_get(StateContext(), insight_id="ins-1")

    assert "error" not in out
    assert out["occurrences"][0]["trajectories"] == []
    assert out["occurrences"][0]["rubrics"][0]["trace"] == [{"role": "user"}]


# --- root causes --------------------------------------------------------------


def _root_cause(
    occurrence_id: str = "occ-run-9", **overrides: Any
) -> RootCause:
    record = {
        "root_cause_id": f"rc-{occurrence_id}",
        "insight_id": "ins-1",
        "occurrence_id": occurrence_id,
        "agent_revision": "agent-00042-abc",
        "summary": "The instruction states the step as a preference.",
        "edits": [
            ProposedEdit(
                path="app/agent.py",
                start_line=42,
                end_line=44,
                before="old\n",
                after="new\n",
                rationale="States it as a constraint.",
            )
        ],
        "created_at": _NOW,
    }
    record.update(overrides)
    return RootCause(**record)


def test_the_root_cause_filter_reaches_the_reader(use_reader) -> None:
    reader = use_reader(_FakeReader())

    _call_list(StateContext({}), has_root_cause="true")

    assert reader.list_calls[0]["has_root_cause"] is True


def test_the_negative_root_cause_filter_reaches_the_reader(use_reader) -> None:
    """Verifies passing 'false' for has_root_cause filters for undiagnosed insights."""
    reader = use_reader(_FakeReader())

    _call_list(StateContext({}), has_root_cause="false")

    assert reader.list_calls[0]["has_root_cause"] is False


def test_an_unset_root_cause_filter_is_no_filter(use_reader) -> None:
    reader = use_reader(_FakeReader())

    _call_list(StateContext({}))

    assert reader.list_calls[0]["has_root_cause"] is None


def test_an_unparseable_root_cause_filter_is_reported(use_reader) -> None:
    reader = use_reader(_FakeReader())

    result = _call_list(StateContext({}), has_root_cause="maybe")

    assert "invalid has_root_cause" in result["error"]
    assert reader.list_calls == []


def test_a_filter_against_a_missing_table_reports_the_error(use_reader) -> None:
    """Verifies NotFound from reader is propagated in the result error field."""

    class _NoTable(_FakeReader):
        def list_insights(self, **kwargs):
            raise NotFound("insight_root_causes does not exist")

    use_reader(_NoTable())

    result = _call_list(StateContext({}), has_root_cause="true")

    assert "insight_root_causes" in result["error"]


def test_get_insight_carries_the_root_causes_of_the_insight(use_reader) -> None:
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence()],
            occ_total=1,
            root_causes=[_root_cause()],
        )
    )

    result = _call_get(StateContext({}), insight_id="ins-1")

    assert [r["root_cause_id"] for r in result["root_causes"]] == [
        "rc-occ-run-9"
    ]


def test_a_root_cause_is_injected_into_the_sighting_it_names(
    use_reader,
) -> None:
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[
                _occurrence(run_id="run-9"),
                _occurrence(run_id="run-8"),
            ],
            occ_total=2,
            root_causes=[_root_cause(occurrence_id="occ-run-9")],
        )
    )

    result = _call_get(StateContext({}), insight_id="ins-1")

    diagnosed, undiagnosed = result["occurrences"]
    assert [r["root_cause_id"] for r in diagnosed["root_causes"]] == [
        "rc-occ-run-9"
    ]
    assert undiagnosed["root_causes"] == []


def test_a_record_whose_sighting_is_gone_still_surfaces(use_reader) -> None:
    """Verifies root causes whose occurrence IDs are missing from the page appear in the top-level list."""
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence(run_id="run-9")],
            occ_total=1,
            root_causes=[_root_cause(occurrence_id="occ-retired")],
        )
    )

    result = _call_get(StateContext({}), insight_id="ins-1")

    assert result["occurrences"][0]["root_causes"] == []
    assert [r["occurrence_id"] for r in result["root_causes"]] == [
        "occ-retired"
    ]


def test_dropping_the_edits_keeps_the_summary_and_the_count(use_reader) -> None:
    """Verifies include_edits=False strips before/after code blocks while preserving metadata."""
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence()],
            occ_total=1,
            root_causes=[_root_cause()],
        )
    )

    result = _call_get(
        StateContext({}), insight_id="ins-1", include_edits=False
    )

    record = result["root_causes"][0]
    assert record["summary"]
    assert record["edit_count"] == 1
    edit = record["edits"][0]
    assert "before" not in edit
    assert "after" not in edit
    # Edit path and line ranges are retained when code bodies are omitted.
    assert edit["path"] == "app/agent.py"
    assert (edit["start_line"], edit["end_line"]) == (42, 44)


def test_the_edits_are_carried_whole_by_default(use_reader) -> None:
    use_reader(
        _FakeReader(
            insight=_summary(),
            occurrences=[_occurrence()],
            occ_total=1,
            root_causes=[_root_cause()],
        )
    )

    result = _call_get(StateContext({}), insight_id="ins-1")

    edit = result["root_causes"][0]["edits"][0]
    assert edit["before"] == "old\n"
    assert edit["after"] == "new\n"


def test_a_failed_root_cause_read_does_not_cost_the_evidence(
    use_reader,
) -> None:
    """Verifies root cause read failures do not prevent returning occurrence rubrics."""

    class _Broken(_FakeReader):
        def list_root_causes(self, insight_id, *, history=False):
            raise RuntimeError("bigquery is unhappy")

    use_reader(
        _Broken(insight=_summary(), occurrences=[_occurrence()], occ_total=1)
    )

    result = _call_get(StateContext({}), insight_id="ins-1")

    assert result["root_causes"] == []
    assert result["occurrences"][0]["rubrics"]


def test_get_insight_builds_one_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifies get_insight reuses a single reader instance across queries."""
    built: list[_FakeReader] = []
    reader = _FakeReader(
        insight=_summary(), occurrences=[_occurrence()], occ_total=1
    )

    def _factory(state):
        built.append(reader)
        return reader

    monkeypatch.setattr(insight_tools, "reader_factory", _factory)

    _call_get(StateContext({}), insight_id="ins-1", include_trajectories=False)

    assert len(built) == 1


# --- the two operator judgements ----------------------------------------------


class _FakeStore:
    """Records what the two write tools ask for, and answers what it is told to."""

    def __init__(self, *, dismissed: bool = True, merged: bool = True) -> None:
        self._dismissed = dismissed
        self._merged = merged
        self.calls: list[tuple[str, ...]] = []

    def dismiss_insight(self, insight_id: str) -> bool:
        self.calls.append(("dismiss", insight_id))
        return self._dismissed

    def merge_insight(self, *, insight_id: str, target_insight_id: str) -> bool:
        self.calls.append(("merge", insight_id, target_insight_id))
        return self._merged


@pytest.fixture
def use_store(monkeypatch: pytest.MonkeyPatch):
    """Return a setter that installs a fake store behind the tool factory."""

    def _install(store: _FakeStore) -> _FakeStore:
        monkeypatch.setattr(insight_tools, "store_factory", lambda state: store)
        return store

    return _install


def _call_dismiss(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(insight_tools.dismiss_insight(*args, **kwargs))


def _call_merge(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(insight_tools.merge_insight(*args, **kwargs))


def test_dismiss_reaches_the_store_and_reports_the_one_key_the_client_reads(
    use_store,
) -> None:
    store = use_store(_FakeStore())

    out = _call_dismiss(StateContext(), insight_id="ins-1")

    assert out == {"dismissed": True}
    assert store.calls == [("dismiss", "ins-1")]


def test_dismissing_an_unknown_insight_is_an_error_not_a_silent_success(
    use_store,
) -> None:
    use_store(_FakeStore(dismissed=False))

    out = _call_dismiss(StateContext(), insight_id="nope")

    assert "nope" in out["error"]


def test_dismiss_without_an_insight_id_never_reaches_the_store(
    use_store,
) -> None:
    store = use_store(_FakeStore())

    assert "insight_id" in _call_dismiss(StateContext())["error"]
    assert store.calls == []


def test_merge_reaches_the_store_with_both_ids(use_store) -> None:
    store = use_store(_FakeStore())

    out = _call_merge(
        StateContext(), insight_id="ins-1", target_insight_id="ins-2"
    )

    assert out == {"merged": True}
    assert store.calls == [("merge", "ins-1", "ins-2")]


def test_merging_an_insight_into_itself_is_refused_before_the_write(
    use_store,
) -> None:
    """The dashboard excludes the target from the ids it fans out, but these
    arrive over HTTP. Left to the UPDATE it would point a row at itself, which
    hides it from the list with nothing to say where it went."""
    store = use_store(_FakeStore())

    out = _call_merge(
        StateContext(), insight_id="ins-1", target_insight_id="ins-1"
    )

    assert "cannot be a duplicate of itself" in out["error"]
    assert store.calls == []


def test_a_refused_merge_names_both_insights(use_store) -> None:
    use_store(_FakeStore(merged=False))

    out = _call_merge(
        StateContext(), insight_id="ins-1", target_insight_id="ins-2"
    )

    assert "ins-1" in out["error"]
    assert "ins-2" in out["error"]


def test_merge_without_a_target_never_reaches_the_store(use_store) -> None:
    store = use_store(_FakeStore())

    assert (
        "target_insight_id"
        in _call_merge(StateContext(), insight_id="ins-1")["error"]
    )
    assert store.calls == []


def test_a_write_that_raises_comes_back_as_an_error_payload(use_store) -> None:
    """Same shape as the reads: the dashboard renders `error`, and an exception
    crossing the route boundary would be a 500 it has nothing to say about."""

    class _Boom(_FakeStore):
        def dismiss_insight(self, insight_id: str) -> bool:
            raise RuntimeError("table not found")

    use_store(_Boom())

    out = _call_dismiss(StateContext(), insight_id="ins-1")

    assert "table not found" in out["error"]
