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

"""Tests for `BigQueryTrajectoryReader` (offline, no BigQuery).

Same shape as `tests/test_insight_reader.py`: a mock client, assertions on the
SQL each read emits, the params it binds, and how it maps rows back. What the
join actually computes is not testable here -- `tests/test_trajectories_schema.py`
holds the SQL to the declared columns, and the outcome rules are asserted on the
mapping instead.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.tools.trajectories.bigquery_reader import (
    BigQueryTrajectoryReader,
)
from ambient_quality_agent.tools.trajectories.models import (
    IngestStatus,
    TrajectoryProcessingState,
)
from ambient_quality_agent.tools.trajectories.reader import (
    build_trace_console_url,
)

_PROJECT = "test-project"
_DATASET = "aqua_insights"
_AGENT = "root_agent"

_TRAJECTORIES_REF = f"{_PROJECT}.{_DATASET}.trajectories"
_OCCURRENCES_REF = f"{_PROJECT}.{_DATASET}.insight_occurrences"

_NOW = dt.datetime(2026, 6, 1, 9, 30, tzinfo=dt.UTC)


def _reader(client: mock.MagicMock) -> BigQueryTrajectoryReader:
    return BigQueryTrajectoryReader(
        client=client,
        project_id=_PROJECT,
        dataset=_DATASET,
        agent_name=_AGENT,
    )


def _params(call: Any) -> dict[str, Any]:
    """Extracts bound query parameter names and values from a mock query call.

    Args:
        call: Mock call object from client.query.

    Returns:
        Mapping of parameter names to their bound values.
    """
    job_config = call.kwargs["job_config"]
    return {
        p.name: getattr(p, "value", None) if hasattr(p, "value") else p.values
        for p in job_config.query_parameters
    }


def _sql(call: Any) -> str:
    return call.args[0]


def _query_returning(*result_sets: list[dict[str, Any]]) -> mock.MagicMock:
    """Creates a mock client returning specified row sets across query calls.

    Args:
        *result_sets: Row lists returned by consecutive query calls.

    Returns:
        Configured mock client instance.
    """
    client = mock.MagicMock()
    client.query.side_effect = [
        mock.MagicMock(**{"result.return_value": rows}) for rows in result_sets
    ]
    return client


def _trajectory_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "run_id": "run-1",
        "agent_name": _AGENT,
        "trajectory_id": "case-1",
        "created_at": _NOW,
        "source": "cloud_ops",
        "source_session_id": "session-1",
        "source_trace_ids": ["trace-1", "trace-2"],
        "ingest_status": "ingested",
        "outcome": "in_an_insight",
    }
    row.update(overrides)
    return row


# --- the per-day chart --------------------------------------------------------


def test_daily_outcomes_groups_by_day_and_outcome() -> None:
    client = _query_returning(
        [
            {
                "day": dt.date(2026, 6, 1),
                "outcome": "in_an_insight",
                "trajectories": 4,
            },
            {
                "day": dt.date(2026, 6, 1),
                "outcome": "no_insight",
                "trajectories": 11,
            },
        ]
    )

    days = _reader(client).count_daily_outcomes()

    sql = _sql(client.query.call_args_list[0])
    # A rejected or unjudged candidate writes an occurrence naming no insight,
    # and that row still lists every trajectory the cluster was built from.
    # Without this filter those trajectories count as evidence behind a tracked
    # defect, which is the opposite of the verdict the pass reached, and the
    # insights pane -- which resolves an owner through `insight_id` -- shows
    # nothing for them.
    assert "insight_id IS NOT NULL" in sql
    assert "GROUP BY day, outcome" in sql
    assert "ORDER BY day, outcome" in sql
    assert "DATE(t.created_at) AS day" in sql
    assert [(d.day, d.outcome, d.trajectories) for d in days] == [
        (dt.date(2026, 6, 1), TrajectoryProcessingState.IN_AN_INSIGHT, 4),
        (dt.date(2026, 6, 1), TrajectoryProcessingState.NO_INSIGHT, 11),
    ]


def test_daily_outcomes_scopes_to_the_agent_and_the_window() -> None:
    client = _query_returning([])

    _reader(client).count_daily_outcomes(
        window_start="2026-05-25T00:00:00Z", window_end="2026-06-01T00:00:00Z"
    )

    call = client.query.call_args_list[0]
    assert "t.agent_name = @agent_name" in _sql(call)
    # The partition column, so a windowed read prunes rather than scans.
    assert "t.created_at >= @window_start" in _sql(call)
    assert "t.created_at <= @window_end" in _sql(call)
    # A TIMESTAMP parameter is parsed by the client, so the bound value is the
    # instant rather than the string that was handed in.
    assert _params(call) == {
        "agent_name": _AGENT,
        "window_start": dt.datetime(2026, 5, 25, tzinfo=dt.UTC),
        "window_end": dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
    }


def test_daily_outcomes_without_a_window_has_no_date_filter() -> None:
    client = _query_returning([])

    _reader(client).count_daily_outcomes()

    call = client.query.call_args_list[0]
    assert "created_at >=" not in _sql(call)
    assert "created_at <=" not in _sql(call)
    assert set(_params(call)) == {"agent_name"}


def test_the_outcome_join_deduplicates_and_scopes_to_the_agent() -> None:
    """One trajectory can sit behind several occurrences of one run -- several
    defects found in the same conversation -- and each would otherwise count
    once per defect."""
    client = _query_returning([])

    _reader(client).count_daily_outcomes()

    sql = _sql(client.query.call_args_list[0])
    assert "SELECT DISTINCT run_id, trajectory_id" in sql
    assert (
        f"`{_OCCURRENCES_REF}`, UNNEST(trajectory_ids) AS trajectory_id" in sql
    )
    assert "USING (run_id, trajectory_id)" in sql
    # Neither table is read unscoped: the outer filter and the subquery bind the
    # same parameter.
    assert sql.count("agent_name = @agent_name") == 2


def test_a_left_join_is_what_keeps_the_uninsighted_trajectories() -> None:
    """An INNER JOIN would silently drop every bar but one -- the chart's whole
    subject is the trajectories no insight came out of."""
    client = _query_returning([])

    _reader(client).count_daily_outcomes()

    sql = _sql(client.query.call_args_list[0])
    assert "LEFT JOIN" in sql
    assert f"`{_TRAJECTORIES_REF}` AS t" in sql


def test_an_unusable_trajectory_outranks_the_insight_join() -> None:
    """`not_ingested` is tested before the join, because a trajectory nothing
    could be made of never reached an analysis -- reporting it as `no_insight`
    would file a broken sample under "evaluated and clean"."""
    client = _query_returning([])

    _reader(client).count_daily_outcomes()

    sql = _sql(client.query.call_args_list[0])
    not_ingested = sql.index("t.ingest_status = 'not_ingested'")
    joined = sql.index("o.trajectory_id IS NULL")
    assert not_ingested < joined


# --- the drill-down -----------------------------------------------------------


def test_list_trajectories_counts_then_pages() -> None:
    client = _query_returning([{"total": 42}], [_trajectory_row()])

    rows, total = _reader(client).list_trajectories(limit=30, offset=30)

    assert total == 42
    assert len(rows) == 1
    count_sql = _sql(client.query.call_args_list[0])
    assert "COUNT(*) AS total" in count_sql
    page_call = client.query.call_args_list[1]
    assert "ORDER BY t.created_at DESC, t.trajectory_id" in _sql(page_call)
    assert _params(page_call)["limit"] == 30
    assert _params(page_call)["offset"] == 30


def test_a_row_carries_the_ids_a_dot_is_drawn_from() -> None:
    client = _query_returning([{"total": 1}], [_trajectory_row()])

    rows, _ = _reader(client).list_trajectories(limit=30, offset=0)

    assert rows[0].trajectory_id == "case-1"
    assert rows[0].source_trace_ids == ["trace-1", "trace-2"]
    assert rows[0].ingest_status is IngestStatus.INGESTED
    assert rows[0].outcome is TrajectoryProcessingState.IN_AN_INSIGHT


def test_list_trajectories_filters_by_outcome_with_the_same_case() -> None:
    """Clicking one segment of one column has to return exactly what that
    segment counted, so the filter repeats the `CASE` rather than restating it."""
    client = _query_returning([{"total": 0}], [])

    _reader(client).list_trajectories(
        outcome=TrajectoryProcessingState.NOT_INGESTED, limit=30, offset=0
    )

    call = client.query.call_args_list[0]
    assert "END = @outcome" in _sql(call)
    assert _params(call)["outcome"] == "not_ingested"


def test_list_trajectories_filters_by_run() -> None:
    client = _query_returning([{"total": 0}], [])

    _reader(client).list_trajectories(run_id="run-7", limit=30, offset=0)

    call = client.query.call_args_list[0]
    assert "t.run_id = @run_id" in _sql(call)
    assert _params(call)["run_id"] == "run-7"


def test_the_page_names_its_columns() -> None:
    """A `SELECT *` would read a column the model does not have and go unnoticed
    until the mapping raised on a deployed dataset."""
    client = _query_returning([{"total": 0}], [])

    _reader(client).list_trajectories(limit=30, offset=0)

    assert "SELECT *" not in _sql(client.query.call_args_list[1])


# --- resolving an occurrence's trajectories -----------------------------------


def test_trajectories_for_keys_the_rows_by_run_and_trajectory() -> None:
    """The pair, because a trajectory id is only unique within a sweep and a
    page of occurrences spans as many runs as it has rows."""
    client = _query_returning(
        [
            _trajectory_row(),
            _trajectory_row(run_id="run-2", trajectory_id="case-2"),
        ]
    )

    found = _reader(client).get_trajectories(
        [("run-1", "case-1"), ("run-2", "case-2")]
    )

    assert sorted(found) == [("run-1", "case-1"), ("run-2", "case-2")]
    assert found["run-1", "case-1"].source_trace_ids == ["trace-1", "trace-2"]
    call = client.query.call_args_list[0]
    assert "t.run_id IN UNNEST(@run_ids)" in _sql(call)
    assert "t.trajectory_id IN UNNEST(@trajectory_ids)" in _sql(call)
    assert _params(call)["run_ids"] == ["run-1", "run-2"]
    assert _params(call)["trajectory_ids"] == ["case-1", "case-2"]


def test_trajectories_for_resolves_a_whole_page_in_one_query() -> None:
    """One query for the page, not one per occurrence."""
    client = _query_returning([_trajectory_row()])

    _reader(client).get_trajectories(
        [("run-1", "case-1"), ("run-2", "case-2"), ("run-3", "case-3")]
    )

    assert client.query.call_count == 1


def test_trajectories_for_drops_the_pairs_it_did_not_ask_for() -> None:
    """Binding the two id sets separately matches their cross product, so a row
    for a real run and a real trajectory that were never paired comes back and
    has to be discarded."""
    client = _query_returning(
        [
            _trajectory_row(),
            _trajectory_row(run_id="run-2", trajectory_id="case-1"),
        ]
    )

    found = _reader(client).get_trajectories(
        [("run-1", "case-1"), ("run-2", "case-2")]
    )

    assert list(found) == [("run-1", "case-1")]


def test_trajectories_for_asks_nothing_when_given_nothing() -> None:
    """An occurrence without an ids column has none, and a query over an
    empty array is a billed round trip that cannot match."""
    client = _query_returning([])

    assert _reader(client).get_trajectories([]) == {}
    client.query.assert_not_called()


def test_a_pair_with_no_row_is_absent_rather_than_a_placeholder() -> None:
    """Allows the caller to distinguish an unrecorded trajectory from one
    that was ingested without trace ids."""
    client = _query_returning([_trajectory_row()])

    found = _reader(client).get_trajectories(
        [("run-1", "case-1"), ("run-1", "case-gone")]
    )

    assert ("run-1", "case-gone") not in found


# --- the console link ---------------------------------------------------------


def test_the_console_link_opens_the_conversations_first_turn() -> None:
    """A session-scoped trajectory is several traces; the ingestion queries
    order them so the first is the opening turn."""
    url = build_trace_console_url(_PROJECT, ["trace-1", "trace-2"])

    assert url == (
        f"https://console.cloud.google.com/traces/list?project={_PROJECT}&tid=trace-1"
    )


@pytest.mark.parametrize(
    ("project", "trace_ids"),
    [
        # Every `big_query` trajectory: that source's schema carries no OTel ids.
        (_PROJECT, []),
        (_PROJECT, [""]),
        ("", ["trace-1"]),
    ],
)
def test_there_is_no_link_without_something_to_link_to(
    project: str, trace_ids: list[str]
) -> None:
    """An empty string rather than a URL that 404s: the absence is a property of
    the telemetry, and the caller renders no anchor."""
    assert build_trace_console_url(project, trace_ids) == ""


def test_the_link_is_derived_and_never_stored() -> None:
    """Stored ids, derived URLs -- the `_build_gcs_console_url` precedent -- so the
    link format can change without rewriting rows."""
    from ambient_quality_agent.tools.trajectories.models import Trajectory

    assert "console_url" not in Trajectory.model_fields
