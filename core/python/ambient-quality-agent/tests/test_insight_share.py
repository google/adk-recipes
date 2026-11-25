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

"""Tests for the estimates an insight reports for all traffic.

`reader.compute_estimated_share` and `reader.compute_impact` are the rule; the
in-memory reader applies them over the store, and the BigQuery reader spells
the same rule in SQL.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.core.nodes.insight_correlation import (
    compute_estimated_trajectory_count,
)
from ambient_quality_agent.standalone.insight_reader import (
    InMemoryInsightReader,
)
from ambient_quality_agent.standalone.store import InMemoryStore, Insights
from ambient_quality_agent.tools.insights.bigquery_reader import (
    BigQueryInsightReader,
)
from ambient_quality_agent.tools.insights.models import (
    Insight,
    InsightOccurrence,
    InsightStatus,
    OccurrenceState,
)
from ambient_quality_agent.tools.insights.reader import (
    InsightOrder,
    compute_estimated_share,
    compute_impact,
    is_share_investigation,
)
from ambient_quality_agent.tools.investigations.models import (
    CustomOverrides,
    Investigation,
    InvestigationCounters,
)

_AGENT = "root_agent"
_DAY = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


def _build_occurrence(
    investigation_id: str,
    *,
    insight_id: str = "ins-1",
    estimated: float | None,
    trajectories: int = 1,
) -> InsightOccurrence:
    """Builds an occurrence of one insight in one investigation.

    Args:
        investigation_id: The investigation it was seen in.
        insight_id: The insight it belongs to.
        estimated: Its `estimated_trajectory_count`.
        trajectories: Its reviewed `trajectory_count`.

    Returns:
        The occurrence.
    """
    return InsightOccurrence(
        occurrence_id=f"occ-{insight_id}-{investigation_id}",
        insight_id=insight_id,
        occurrence_state=OccurrenceState.TRACKED,
        investigation_id=investigation_id,
        created_at=_DAY,
        observed_agent_key=_AGENT,
        label="a defect",
        trajectory_count=trajectories,
        estimated_trajectory_count=estimated,
    )


def _build_investigation(
    investigation_id: str,
    day: int,
    *,
    scanned: int = 1000,
    estimated: bool = True,
    selector_sql: str = "",
) -> Investigation:
    """Builds a finished investigation of the agent.

    Args:
        investigation_id: Its id.
        day: Days after `_DAY` its window ends.
        scanned: Its `trajectories_scanned`.
        estimated: Whether it recorded estimates.
        selector_sql: The selector it ran; empty for none.

    Returns:
        The investigation.
    """
    return Investigation(
        investigation_id=investigation_id,
        observed_agent_key=_AGENT,
        window_end=(_DAY + dt.timedelta(days=day)).isoformat(),
        custom_overrides=CustomOverrides(selector_sql=selector_sql)
        if selector_sql
        else None,
        counters=InvestigationCounters(
            trajectories_scanned=scanned,
            trajectories_evaluated=100,
            estimated_trajectories_eval_passed=scanned if estimated else 0,
        ),
    )


def _build_windows(*investigations: Investigation) -> dict[str, Any]:
    """Builds the windows `compute_estimated_share` reads.

    Args:
        *investigations: The agent's investigations.

    Returns:
        The window end and `trajectories_scanned` of each investigation
        `is_share_investigation` accepts, keyed by id.
    """
    return {
        i.investigation_id: (
            dt.datetime.fromisoformat(str(i.window_end)),
            i.counters.trajectories_scanned,
        )
        for i in investigations
        if is_share_investigation(i)
    }


def test_the_share_in_one_investigation_is_its_estimate_over_its_traffic() -> (
    None
):
    share = compute_estimated_share(
        [_build_occurrence("run-1", estimated=50.0)],
        _build_windows(_build_investigation("run-1", 0)),
    )

    assert share == pytest.approx(0.05)


def test_investigations_between_two_sightings_count_though_they_saw_nothing() -> (
    None
):
    """The share would otherwise overstate every defect that is not seen in
    every investigation."""
    windows = _build_windows(
        _build_investigation("run-0", 0, scanned=5000),
        _build_investigation("run-1", 1, scanned=1000),
        _build_investigation("run-2", 2, scanned=2000),
        _build_investigation("run-3", 3, scanned=1000),
        _build_investigation("run-4", 4, scanned=7000),
    )
    occurrences = [
        _build_occurrence("run-1", estimated=30.0),
        _build_occurrence("run-3", estimated=10.0),
    ]

    share = compute_estimated_share(occurrences, windows)

    # run-0 and run-4 lie outside the first and latest sightings.
    assert share == pytest.approx(40 / 4000)


def test_a_list_filtered_to_one_investigation_shows_its_share_alone() -> None:
    windows = _build_windows(
        _build_investigation("run-1", 1, scanned=1000),
        _build_investigation("run-2", 2, scanned=2000),
    )
    occurrences = [
        _build_occurrence("run-1", estimated=30.0),
        _build_occurrence("run-2", estimated=10.0),
    ]

    assert compute_estimated_share(
        occurrences, windows, investigation_id="run-2"
    ) == pytest.approx(10 / 2000)


def test_an_empty_investigation_filter_is_no_filter() -> None:
    """As the BigQuery reader and the mock server read it."""
    windows = _build_windows(
        _build_investigation("run-1", 1, scanned=1000),
        _build_investigation("run-2", 2, scanned=2000),
    )
    occurrences = [
        _build_occurrence("run-1", estimated=30.0),
        _build_occurrence("run-2", estimated=10.0),
    ]

    assert compute_estimated_share(
        occurrences, windows, investigation_id=""
    ) == compute_estimated_share(occurrences, windows)


def test_a_selector_run_is_left_out_of_both_sums() -> None:
    """Its conversations were chosen, not sampled from all traffic."""
    windows = _build_windows(
        _build_investigation("run-1", 1),
        _build_investigation("custom", 2, selector_sql="SELECT 1"),
        _build_investigation("run-3", 3),
    )
    occurrences = [
        _build_occurrence("run-1", estimated=10.0),
        _build_occurrence("custom", estimated=500.0),
        _build_occurrence("run-3", estimated=10.0),
    ]

    assert compute_estimated_share(occurrences, windows) == pytest.approx(
        20 / 2000
    )


def test_a_run_of_the_stored_selector_is_left_out_too() -> None:
    """A scheduled run with the attachment's selector reviewed a chosen slice;
    a custom run without a selector of its own ignores the stored one."""
    stored = _build_investigation("stored", 1)
    stored.effective_config["investigation_selector_sql"] = "SELECT 1"
    focused = _build_investigation("focused", 2)
    focused.effective_config["investigation_selector_sql"] = "SELECT 1"
    focused.custom_overrides = CustomOverrides(review_focus="refunds")

    assert not is_share_investigation(stored)
    assert is_share_investigation(focused)


def test_records_without_estimates_are_left_out_and_may_leave_no_share() -> (
    None
):
    windows = _build_windows(
        _build_investigation("old", 0, estimated=False),
        _build_investigation("run-1", 1),
    )

    assert "old" not in windows
    assert (
        compute_estimated_share(
            [
                _build_occurrence("old", estimated=5.0),
                _build_occurrence("run-1", estimated=None),
            ],
            windows,
        )
        is None
    )


def test_impact_counts_an_occurrence_without_an_estimate_as_reviewed() -> None:
    assert compute_impact(
        [
            _build_occurrence("run-1", estimated=40.5),
            _build_occurrence("run-2", estimated=None, trajectories=3),
        ]
    ) == pytest.approx(43.5)


def test_a_cluster_is_estimated_from_what_its_trajectories_stand_for() -> None:
    represented = {"a": 2.0, "b": 10.0}

    assert compute_estimated_trajectory_count(
        ["a", "b"], represented
    ) == pytest.approx(12)
    assert compute_estimated_trajectory_count(["a", "c"], represented) is None
    assert compute_estimated_trajectory_count([], represented) is None


def test_the_in_memory_reader_ranks_by_estimates_and_reports_the_share() -> (
    None
):
    """Two reviewed conversations that stand for few outrank one that stands
    for many only by raw count."""
    store = InMemoryStore()
    store.save_investigation(_build_investigation("run-1", 0))

    def build_insight(insight_id: str) -> Insight:
        """Builds a new insight labeled with its id.

        Args:
            insight_id: Its id.

        Returns:
            The insight.
        """
        return Insight(
            insight_id=insight_id,
            observed_agent_key=_AGENT,
            label=insight_id,
            status=InsightStatus.NEW,
            created_at=_DAY,
            updated_at=_DAY,
        )

    store.update_insights(
        lambda current: Insights(
            insights={
                "rare": build_insight("rare"),
                "wide": build_insight("wide"),
            },
            insight_occurrences=[
                _build_occurrence(
                    "run-1", insight_id="rare", estimated=4.0, trajectories=2
                ),
                _build_occurrence(
                    "run-1", insight_id="wide", estimated=90.0, trajectories=1
                ),
            ],
            root_causes=current.root_causes,
        )
    )
    reader = InMemoryInsightReader(store, observed_agent_key=_AGENT)

    views, _ = reader.list_insights(
        limit=10, offset=0, order_by=InsightOrder.IMPACT
    )

    assert [v.insight_id for v in views] == ["wide", "rare"]
    assert views[0].estimated_trajectory_count == pytest.approx(90.0)
    assert views[0].impact == pytest.approx(90.0)
    assert views[1].impact == pytest.approx(4.0)
    assert views[0].estimated_share == pytest.approx(0.09)


def test_the_bigquery_share_is_per_investigation_only_when_filtered() -> None:
    client = mock.MagicMock()
    client.query.side_effect = [
        mock.MagicMock(**{"result.return_value": rows})
        for rows in ([{"total": 0}], [], [{"total": 0}], [], [{"total": 0}], [])
    ]
    reader = BigQueryInsightReader(
        client=client,
        project_id="p",
        dataset="d",
        observed_agent_key=_AGENT,
    )

    reader.list_insights(limit=10, offset=0)
    unfiltered = client.query.call_args.args[0]
    reader.list_insights(limit=10, offset=0, investigation_id="run-1")
    filtered = client.query.call_args.args[0]
    reader.list_insights(limit=10, offset=0, order_by=InsightOrder.IMPACT)
    reader_sql_impact = client.query.call_args.args[0]

    assert "AND runs.run_id = @investigation_id" not in unfiltered
    assert filtered.count("AND runs.run_id = @investigation_id") == 2
    # The rule of `is_share_investigation`, in SQL.
    assert "latest_runs.stored_selector" in unfiltered
    assert "latest_runs.custom_selector != ''" in unfiltered
    assert "0 < latest_runs.estimated_judged" in unfiltered
    assert "ORDER BY impact DESC, i.insight_id" in reader_sql_impact
    assert "SAFE_DIVIDE(sighted.estimated, SUM(runs.scanned))" in unfiltered
    assert "'$.traces_scanned'" in unfiltered
