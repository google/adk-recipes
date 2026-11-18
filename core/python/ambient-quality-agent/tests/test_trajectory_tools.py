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

"""Tests for the trajectory read tools.

A fake reader behind the module's factory seam, the way
`tests/test_insight_tools.py` fakes the insight reader: what is under test is
argument marshalling, pagination and the link derivation, not SQL.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest
from ambient_quality_agent.tools.orchestrator import trajectory_tools
from ambient_quality_agent.tools.trajectories.bundles import (
    NOT_ARCHIVED,
    TrajectoryBundle,
)
from ambient_quality_agent.tools.trajectories.models import (
    DailyOutcome,
    IngestStatus,
    InvestigationTrajectory,
    InvestigationTrajectoryView,
    TrajectoryManifest,
    TrajectoryProcessingState,
    TrajectoryStatus,
    TrajectoryStep,
)

from .conftest import StateContext

_NOW = dt.datetime(2026, 6, 1, 9, 30, tzinfo=dt.UTC)


def _call_outcomes(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(
        trajectory_tools.count_investigation_trajectory_outcomes(
            *args, **kwargs
        )
    )


def _call_list(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(
        trajectory_tools.list_investigation_trajectories(*args, **kwargs)
    )


def _call_get_trajectory_bundle(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(trajectory_tools.get_trajectory_bundle(*args, **kwargs))


def _build_bundle(
    trajectory_id: str, turns: list[dict[str, Any]]
) -> TrajectoryBundle:
    """Builds a mock stored conversation as returned by get_trajectory_bundles.

    Args:
        trajectory_id: Trajectory identifier string.
        turns: List of turn event dictionaries.

    Returns:
        Configured TrajectoryBundle instance.
    """
    return TrajectoryBundle(
        manifest=TrajectoryManifest(
            observed_agent_key="observed",
            trajectory_id=trajectory_id,
            created_at=_NOW,
            status=TrajectoryStatus.INGESTED,
            step_count=len(turns),
            agents={"observed": {"instruction": "be helpful"}},
        ),
        steps=tuple(
            TrajectoryStep(
                observed_agent_key="observed",
                trajectory_id=trajectory_id,
                step_index=index,
                created_at=_NOW,
                step=turn,
            )
            for index, turn in enumerate(turns)
        ),
    )


def _view(
    trajectory_id: str = "case-1", **overrides: Any
) -> InvestigationTrajectoryView:
    fields: dict[str, Any] = {
        "investigation_id": "run-1",
        "observed_agent_key": "test-observed-agent",
        "trajectory_id": trajectory_id,
        "created_at": _NOW,
        "source": "cloud_ops",
        "source_session_id": "session-1",
        "source_trace_ids": ["trace-1", "trace-2"],
        "ingest_status": IngestStatus.INGESTED,
        "outcome": TrajectoryProcessingState.IN_AN_INSIGHT,
    }
    fields.update(overrides)
    return InvestigationTrajectoryView(**fields)


class _FakeReader:
    def __init__(
        self,
        *,
        days: list[DailyOutcome] | None = None,
        rows: list[InvestigationTrajectoryView] | None = None,
        total: int = 0,
        bundles: dict[str, TrajectoryBundle] | None = None,
        pairs: dict[tuple[str, str], InvestigationTrajectory] | None = None,
    ) -> None:
        self._days = days or []
        self._rows = rows or []
        self._total = total
        self._bundles = bundles or {}
        self._pairs = pairs or {}
        self.daily_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.payload_calls: list[list[str]] = []
        self.pair_calls: list[list[tuple[str, str]]] = []

    def count_daily_outcomes(self, *, window_start=None, window_end=None):
        self.daily_calls.append(
            {"window_start": window_start, "window_end": window_end}
        )
        return self._days

    def list_investigation_trajectories(
        self,
        *,
        investigation_id,
        outcome,
        window_start,
        window_end,
        limit,
        offset,
    ):
        self.list_calls.append(
            {
                "investigation_id": investigation_id,
                "outcome": outcome,
                "window_start": window_start,
                "window_end": window_end,
                "limit": limit,
                "offset": offset,
            }
        )
        return self._rows, self._total

    def get_trajectory_bundles(self, trajectory_ids):
        self.payload_calls.append(list(trajectory_ids))
        return {k: v for k, v in self._bundles.items() if k in trajectory_ids}

    def get_investigation_trajectories(self, keys):
        self.pair_calls.append(list(keys))
        return {k: v for k, v in self._pairs.items() if k in keys}


@pytest.fixture
def use_reader(monkeypatch: pytest.MonkeyPatch):
    """Return a setter that installs a fake reader behind the tool factory."""

    def _install(reader: _FakeReader) -> _FakeReader:
        monkeypatch.setattr(
            trajectory_tools, "reader_factory", lambda state: reader
        )
        return reader

    return _install


# --- count_investigation_trajectory_outcomes ---------------------------------


def test_outcomes_returns_a_day_per_bucket(use_reader) -> None:
    use_reader(
        _FakeReader(
            days=[
                DailyOutcome(
                    day=dt.date(2026, 6, 1),
                    outcome=TrajectoryProcessingState.NOT_INGESTED,
                    trajectories=2,
                )
            ]
        )
    )

    result = _call_outcomes(StateContext())

    assert result == {
        "days": [
            {"day": "2026-06-01", "outcome": "not_ingested", "trajectories": 2}
        ]
    }


def test_outcomes_passes_the_window_through(use_reader) -> None:
    reader = use_reader(_FakeReader())

    _call_outcomes(
        StateContext(),
        window_start="2026-05-25T00:00:00Z",
        window_end="2026-06-01T00:00:00Z",
    )

    assert reader.daily_calls == [
        {
            "window_start": "2026-05-25T00:00:00Z",
            "window_end": "2026-06-01T00:00:00Z",
        }
    ]


def test_an_absent_window_reaches_the_reader_as_none(use_reader) -> None:
    """The route sends ``""`` for an argument the caller omitted, and an empty
    string is not a bound that BigQuery can compare against."""
    reader = use_reader(_FakeReader())

    _call_outcomes(StateContext(), window_start="", window_end="")

    assert reader.daily_calls == [{"window_start": None, "window_end": None}]


def test_outcomes_reports_a_failed_read_rather_than_raising(use_reader) -> None:
    class _Boom(_FakeReader):
        def count_daily_outcomes(self, **_: Any):
            raise RuntimeError("dataset not found")

    use_reader(_Boom())

    result = _call_outcomes(StateContext())

    assert "dataset not found" in result["error"]


# --- list_investigation_trajectories -----------------------------------------


def test_list_returns_a_page_and_its_next_token(use_reader) -> None:
    use_reader(_FakeReader(rows=[_view()], total=250))

    result = _call_list(StateContext())

    assert result["total"] == 250
    assert result["next_page_token"]
    assert result["investigation_trajectories"][0]["trajectory_id"] == "case-1"


def test_the_last_page_has_no_next_token(use_reader) -> None:
    use_reader(_FakeReader(rows=[_view()], total=1))

    assert _call_list(StateContext())["next_page_token"] is None


def test_a_page_token_becomes_an_offset(use_reader) -> None:
    from ambient_quality_agent.tools.orchestrator import paging

    reader = use_reader(_FakeReader(rows=[], total=0))

    _call_list(StateContext(), page_token=paging.encode_page_token(100))

    assert reader.list_calls[0]["offset"] == 100
    assert (
        reader.list_calls[0]["limit"] == trajectory_tools.TRAJECTORIES_PAGE_SIZE
    )


def test_a_dot_carries_the_link_to_its_conversation(use_reader) -> None:
    use_reader(_FakeReader(rows=[_view()], total=1))

    trajectory = _call_list(StateContext())["investigation_trajectories"][0]

    assert trajectory["console_url"].endswith("&tid=trace-1")
    assert trajectory["source_trace_ids"] == ["trace-1", "trace-2"]


def test_a_source_with_no_trace_ids_gets_no_link(use_reader) -> None:
    """Every `big_query` trajectory: the absence is a property of the telemetry,
    and the caller renders no anchor rather than a URL that cannot resolve."""
    use_reader(
        _FakeReader(rows=[_view(source="big_query", source_trace_ids=[])])
    )

    assert (
        _call_list(StateContext())["investigation_trajectories"][0][
            "console_url"
        ]
        == ""
    )


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("not_ingested", TrajectoryProcessingState.NOT_INGESTED),
        ("IN_AN_INSIGHT", TrajectoryProcessingState.IN_AN_INSIGHT),
        ("", None),
        ("all", None),
    ],
)
def test_the_outcome_filter_is_parsed_case_insensitively(
    use_reader, given: str, expected: TrajectoryProcessingState | None
) -> None:
    reader = use_reader(_FakeReader())

    _call_list(StateContext(), outcome=given)

    assert reader.list_calls[0]["outcome"] == expected


def test_an_unknown_outcome_is_rejected_before_the_read(use_reader) -> None:
    """A typo has to name itself, rather than silently returning every dot as
    though no filter had been asked for."""
    reader = use_reader(_FakeReader())

    result = _call_list(StateContext(), outcome="passed")

    assert "invalid outcome" in result["error"]
    assert "not_ingested" in result["error"]
    assert reader.list_calls == []


def test_list_reports_a_failed_read_rather_than_raising(use_reader) -> None:
    class _Boom(_FakeReader):
        def list_investigation_trajectories(self, **_: Any):
            raise RuntimeError("table not found")

    use_reader(_Boom())

    assert "table not found" in _call_list(StateContext())["error"]


# --- get_trajectory_bundle ---------------------------------------------------

_TURN = {
    "turn_index": 0,
    "turn_id": "t0",
    "events": [
        {
            "author": "user",
            "content": {"role": "user", "parts": [{"text": "hi"}]},
        }
    ],
}


def test_a_case_comes_back_as_its_turns(use_reader) -> None:
    use_reader(
        _FakeReader(bundles={"case-1": _build_bundle("case-1", [_TURN])})
    )

    case = _call_get_trajectory_bundle(StateContext(), trajectory_id="case-1")[
        "trajectory_bundle"
    ]

    assert case["status"] == TrajectoryStatus.INGESTED.value
    assert case["step_count"] == 1
    assert case["steps"] == [_TURN]


def test_the_turns_are_the_stored_shape_not_a_flattening(use_reader) -> None:
    """The nesting is the point: the dashboard reads the events in order, and
    a turn's tool call sits between the text before it and the text after."""
    use_reader(
        _FakeReader(bundles={"case-1": _build_bundle("case-1", [_TURN])})
    )

    turn = _call_get_trajectory_bundle(StateContext(), trajectory_id="case-1")[
        "trajectory_bundle"
    ]["steps"][0]

    assert turn["events"][0]["content"]["parts"] == [{"text": "hi"}]


def test_a_conversation_nobody_archived_says_so(use_reader) -> None:
    """Absent rather than empty, because "never stored" and "stored and empty"
    lead a reader to opposite conclusions."""
    use_reader(_FakeReader(bundles={}))

    case = _call_get_trajectory_bundle(
        StateContext(), trajectory_id="case-gone"
    )["trajectory_bundle"]

    assert case["status"] == NOT_ARCHIVED
    assert case["steps"] == []


def test_a_case_is_read_by_id_alone(use_reader) -> None:
    """No run in the key: a payload records the conversation, not one sweep's
    ingestion of it."""
    reader = use_reader(
        _FakeReader(bundles={"case-1": _build_bundle("case-1", [])})
    )

    _call_get_trajectory_bundle(
        StateContext(), trajectory_id="case-1", investigation_id="run-9"
    )

    assert reader.payload_calls == [["case-1"]]


def test_a_missing_id_is_rejected_rather_than_read(use_reader) -> None:
    reader = use_reader(_FakeReader())

    assert (
        "trajectory_id"
        in _call_get_trajectory_bundle(StateContext(), trajectory_id="")[
            "error"
        ]
    )
    assert reader.payload_calls == []


def test_the_run_resolves_the_trace_link_beside_the_conversation(
    use_reader,
) -> None:
    pair = InvestigationTrajectory(
        investigation_id="run-9",
        observed_agent_key="observed",
        trajectory_id="case-1",
        created_at=_NOW,
        ingestion_source="cloud_logging",
        source_trace_ids=["t1"],
        ingest_status=IngestStatus.INGESTED,
    )
    use_reader(
        _FakeReader(
            bundles={"case-1": _build_bundle("case-1", [_TURN])},
            pairs={("run-9", "case-1"): pair},
        )
    )

    case = _call_get_trajectory_bundle(
        StateContext(), trajectory_id="case-1", investigation_id="run-9"
    )["trajectory_bundle"]

    assert case["trajectory"]["source_trace_ids"] == ["t1"]
    assert "console_url" in case["trajectory"]


def test_the_conversation_survives_an_unresolvable_run(use_reader) -> None:
    """The index row supplies a link, not the conversation. A `(run, id)` pair
    that never existed -- a hand-edited URL -- still replays."""
    use_reader(
        _FakeReader(
            bundles={"case-1": _build_bundle("case-1", [_TURN])}, pairs={}
        )
    )

    case = _call_get_trajectory_bundle(
        StateContext(), trajectory_id="case-1", investigation_id="nope"
    )["trajectory_bundle"]

    assert case["steps"] == [_TURN]
    assert "trajectory" not in case


def test_a_failed_read_is_reported_rather_than_raised(use_reader) -> None:
    class _Boom(_FakeReader):
        def get_trajectory_bundles(self, trajectory_ids):
            raise RuntimeError("table not found")

    use_reader(_Boom())

    assert (
        "table not found"
        in _call_get_trajectory_bundle(StateContext(), trajectory_id="c")[
            "error"
        ]
    )
