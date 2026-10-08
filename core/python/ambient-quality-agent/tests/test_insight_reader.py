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

"""Tests for `InsightReader` (offline, no BigQuery).

Patches ``bigquery.Client`` and asserts on the SQL each read emits, the params
it binds, and how it maps rows back to models. The real round-trip is covered by
the manual integration check, not here.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.tools.insights.bigquery_reader import (
    BigQueryInsightReader,
)
from ambient_quality_agent.tools.insights.models import (
    InsightStatus,
    OccurrenceState,
)
from ambient_quality_agent.tools.insights.reader import InsightOrder
from google.api_core.exceptions import NotFound

_PROJECT = "test-project"
_DATASET = "aqua_insights"
_AGENT = "root_agent"

_INSIGHTS_REF = f"{_PROJECT}.{_DATASET}.insights"
_OCCURRENCES_REF = f"{_PROJECT}.{_DATASET}.insight_occurrences"
_ROOT_CAUSES_REF = f"{_PROJECT}.{_DATASET}.insight_root_causes"

_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


def _reader(client: mock.MagicMock) -> BigQueryInsightReader:
    return BigQueryInsightReader(
        client=client,
        project_id=_PROJECT,
        dataset=_DATASET,
        agent_name=_AGENT,
    )


def _params(call: Any) -> dict[str, Any]:
    """Extracts bound parameter names and values from a query call.

    Args:
        call: Mock query call object.

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
    """Builds a mock client whose query calls yield successive result sets.

    Args:
        *result_sets: Sequence of row result lists to return from query calls.

    Returns:
        Mock BigQuery client.
    """
    client = mock.MagicMock()
    client.query.side_effect = [
        mock.MagicMock(**{"result.return_value": rows}) for rows in result_sets
    ]
    return client


def _summary_row(insight_id: str = "ins-1", **overrides: Any) -> dict[str, Any]:
    row = {
        "insight_id": insight_id,
        "agent_name": _AGENT,
        "label": "made no tool call",
        "status": "RECURRING",
        "created_at": _NOW,
        "updated_at": _NOW,
        # Still open, so undated; the resolved case has a test of its own.
        "resolved_at": None,
        # Unjudged, which is every row the list can return; the cases where one
        # of these is set have tests of their own.
        "dismissed_at": None,
        "merged_into_insight_id": None,
        "merged_at": None,
        "occurrence_count": 3,
        "trace_count": 7,
        "last_run_at": _NOW,
        "last_run_id": "run-9",
        "has_root_cause": False,
    }
    row.update(overrides)
    return row


# --- list_insights ------------------------------------------------------------


def test_list_insights_returns_summaries_and_total() -> None:
    # First query is the COUNT, second is the page.
    client = _query_returning([{"total": 5}], [_summary_row()])
    reader = _reader(client)

    summaries, total = reader.list_insights(
        statuses=None, run_id=None, limit=30, offset=0
    )

    assert total == 5
    assert [s.insight_id for s in summaries] == ["ins-1"]
    assert summaries[0].occurrence_count == 3
    assert summaries[0].trace_count == 7
    assert summaries[0].last_run_id == "run-9"
    # Page query aggregates occurrences and orders newest-updated first.
    page_sql = _sql(client.query.call_args_list[1])
    assert "COUNT(owned.occurrence_id) AS occurrence_count" in page_sql
    assert "ORDER BY i.updated_at DESC, i.insight_id" in page_sql
    assert _params(client.query.call_args_list[1])["limit"] == 30


def test_list_insights_ranks_by_impact_in_the_query() -> None:
    """The ranking is over every match, not over the rows that come back.

    Sorting the returned page instead would rank one window of the list, and
    the issues that belong at the top can sit on a page nobody asked for.
    """
    client = _query_returning([{"total": 5}], [_summary_row()])

    _reader(client).list_insights(
        limit=10, offset=0, order_by=InsightOrder.IMPACT
    )

    page_sql = _sql(client.query.call_args_list[1])
    assert "ORDER BY SUM(owned.trace_count) DESC, i.insight_id" in page_sql
    assert _params(client.query.call_args_list[1])["limit"] == 10


def test_a_resolved_insight_carries_its_resolution_date() -> None:
    """The resolution timestamp is read directly from the `resolved_at` column."""
    resolved_on = _NOW + dt.timedelta(days=14)
    client = _query_returning(
        [{"total": 1}],
        [_summary_row(status="RESOLVED", resolved_at=resolved_on)],
    )

    summaries, _ = reader_list(client)

    assert summaries[0].status is InsightStatus.RESOLVED
    assert summaries[0].resolved_at == resolved_on


def test_an_open_insight_has_no_resolution_date() -> None:
    client = _query_returning([{"total": 1}], [_summary_row()])

    summaries, _ = reader_list(client)

    assert summaries[0].resolved_at is None


def reader_list(client: Any) -> tuple[list[Any], int]:
    """Invokes `list_insights` using test default parameters.

    Args:
        client: Mock BigQuery client.

    Returns:
        Tuple of (insight_summaries, total_count).
    """
    return _reader(client).list_insights(
        statuses=None, run_id=None, limit=30, offset=0
    )


def test_list_insights_status_filter_binds_array() -> None:
    client = _query_returning([{"total": 0}], [])
    reader = _reader(client)

    reader.list_insights(
        statuses=[InsightStatus.NEW, InsightStatus.RECURRING],
        limit=30,
        offset=0,
    )

    count_sql = _sql(client.query.call_args_list[0])
    assert "i.status IN UNNEST(@statuses)" in count_sql
    assert _params(client.query.call_args_list[0])["statuses"] == [
        "NEW",
        "RECURRING",
    ]


def test_list_insights_run_id_filter_uses_exists_subquery() -> None:
    client = _query_returning([{"total": 0}], [])
    reader = _reader(client)

    reader.list_insights(run_id="run-7", limit=30, offset=0)

    count_sql = _sql(client.query.call_args_list[0])
    assert "EXISTS (SELECT 1 FROM" in count_sql
    assert _params(client.query.call_args_list[0])["run_id"] == "run-7"


def test_list_insights_period_filters_on_last_seen() -> None:
    """On `updated_at`, not `created_at`: "insights in the last week" means the
    issues live that week. Filtering on first sighting would hide a defect that
    has recurred daily for a month behind the week it was discovered."""
    client = _query_returning([{"total": 0}], [])
    reader = _reader(client)

    reader.list_insights(
        limit=30,
        offset=0,
        window_start="2026-08-01T00:00:00+00:00",
        window_end="2026-08-08T00:00:00+00:00",
    )

    count_sql = _sql(client.query.call_args_list[0])
    params = _params(client.query.call_args_list[0])
    assert "i.updated_at >= @window_start" in count_sql
    assert "i.updated_at <= @window_end" in count_sql
    assert "i.created_at" not in count_sql
    # TIMESTAMP-bound, so the comparison is on instants rather than text.
    assert params["window_start"] == dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    assert params["window_end"] == dt.datetime(2026, 8, 8, tzinfo=dt.UTC)


def test_list_insights_period_applies_to_the_count_as_well() -> None:
    """The total is what paging is driven from, so a filtered page under an
    unfiltered total would page past the end of its own result."""
    client = _query_returning([{"total": 0}], [])
    reader = _reader(client)

    reader.list_insights(
        limit=30, offset=0, window_start="2026-08-01T00:00:00+00:00"
    )

    count_sql = _sql(client.query.call_args_list[0])
    page_sql = _sql(client.query.call_args_list[1])
    assert "i.updated_at >= @window_start" in count_sql
    assert "i.updated_at >= @window_start" in page_sql


def test_list_insights_without_a_period_has_no_window_filter() -> None:
    client = _query_returning([{"total": 0}], [])
    reader = _reader(client)

    reader.list_insights(limit=30, offset=0)

    count_sql = _sql(client.query.call_args_list[0])
    assert "@window_start" not in count_sql
    assert "@window_end" not in count_sql


def test_list_insights_day_keeps_what_was_found_or_resolved_that_day() -> None:
    """The per-day chart counts two changes, so a day's list is the rows behind
    them: first found that day, or resolved that day. Not every insight merely
    seen that day, which on a busy deployment is all of the open ones."""
    client = _query_returning([{"total": 0}], [])

    _reader(client).list_insights(limit=10, offset=0, day="2026-09-20")

    for call in client.query.call_args_list:
        sql = _sql(call)
        assert (
            "(DATE(i.created_at) = @day OR DATE(i.resolved_at) = @day)" in sql
        ), "the count and the page have to describe one set"
        assert _params(call)["day"] == dt.date(2026, 9, 20)
    assert "@window_start" not in _sql(client.query.call_args_list[0])


@pytest.mark.parametrize(
    ("status", "change"),
    [
        (InsightStatus.NEW, "DATE(i.created_at) = @day"),
        (InsightStatus.RESOLVED, "DATE(i.resolved_at) = @day"),
    ],
)
def test_list_insights_day_reads_a_status_as_the_change_it_names(
    status: InsightStatus, change: str
) -> None:
    """With a day, NEW is "first found that day" whatever the insight is now: an
    insight found on Monday and seen again on Tuesday is RECURRING, and the
    chart still counts it as Monday's new one."""
    client = _query_returning([{"total": 0}], [])

    _reader(client).list_insights(
        limit=10, offset=0, statuses=[status], day="2026-09-20"
    )

    count_sql = _sql(client.query.call_args_list[0])
    assert f"({change})" in count_sql
    assert "@statuses" not in count_sql


def test_list_insights_day_and_run_id_both_narrow_the_list() -> None:
    """A day and a run are two filters, ANDed: the day does not replace the run."""
    client = _query_returning([{"total": 0}], [])

    _reader(client).list_insights(
        limit=10, offset=0, run_id="run-7", day="2026-09-20"
    )

    count_sql = _sql(client.query.call_args_list[0])
    assert (
        "(DATE(i.created_at) = @day OR DATE(i.resolved_at) = @day) AND EXISTS"
        in (count_sql)
    )
    assert "o.run_id = @run_id" in count_sql
    params = _params(client.query.call_args_list[0])
    assert params["run_id"] == "run-7"
    assert params["day"] == dt.date(2026, 9, 20)


def test_list_insights_day_with_only_recurring_matches_nothing() -> None:
    client = _query_returning([{"total": 0}], [])

    _reader(client).list_insights(
        limit=10, offset=0, statuses=[InsightStatus.RECURRING], day="2026-09-20"
    )

    assert "(FALSE)" in _sql(client.query.call_args_list[0])


def test_affected_conversations_takes_the_day_filter() -> None:
    client = _query_returning([{"conversations": 0}])

    _reader(client).count_affected_conversations(day="2026-09-20")

    assert "DATE(i.created_at) = @day" in _sql(client.query.call_args)
    assert _params(client.query.call_args)["day"] == dt.date(2026, 9, 20)


# --- count_affected_conversations ---------------------------------------------------


def _conversations(n: int) -> list[dict[str, Any]]:
    return [{"conversations": n}]


def test_affected_conversations_deduplicates_across_sweeps_and_insights() -> (
    None
):
    """The union is the whole point: summing `trace_count` instead counts a
    conversation once per sweep that saw it and once per insight it matches."""
    client = _query_returning(_conversations(5))

    assert _reader(client).count_affected_conversations() == 5

    sql = _sql(client.query.call_args)
    assert "COUNT(DISTINCT trajectory_id)" in sql
    assert "CROSS JOIN UNNEST(owned.trajectory_ids)" in sql


def test_affected_conversations_covers_the_whole_filtered_set() -> None:
    """No LIMIT and no OFFSET: one page is exactly what this must not total."""
    client = _query_returning(_conversations(5))

    _reader(client).count_affected_conversations()

    sql = _sql(client.query.call_args)
    assert "LIMIT" not in sql
    assert "OFFSET" not in sql


def test_affected_conversations_rolls_up_through_the_merge_pointer() -> None:
    """Deduplicated insights fold into their target, as in every other
    aggregating read, so one conversation is not counted under each of two."""
    client = _query_returning(_conversations(5))

    _reader(client).count_affected_conversations()

    assert (
        "COALESCE(m.merged_into_insight_id, o.insight_id) AS owner_id"
        in _sql(client.query.call_args)
    )


def test_affected_conversations_takes_the_list_filters() -> None:
    client = _query_returning(_conversations(2))

    _reader(client).count_affected_conversations(
        statuses=[InsightStatus.NEW],
        run_id="run-3",
        window_start="2026-06-01T00:00:00+00:00",
    )

    params = _params(client.query.call_args)
    assert params["statuses"] == ["NEW"]
    assert params["run_id"] == "run-3"
    assert params["window_start"] == dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
    assert "i.updated_at >= @window_start" in _sql(client.query.call_args)


def test_affected_conversations_of_an_empty_deployment_is_zero() -> None:
    """COUNT over no rows is 0, so nothing matched reads as none affected."""
    assert (
        _reader(
            _query_returning(_conversations(0))
        ).count_affected_conversations()
        == 0
    )


def test_affected_conversations_refuses_a_root_cause_filter_without_the_table() -> (
    None
):
    client = _query_returning(_conversations(0))
    client.get_table.side_effect = NotFound("insight_root_causes")

    with pytest.raises(NotFound):
        _reader(client).count_affected_conversations(has_root_cause=True)


# --- get_insight --------------------------------------------------------------


def test_get_insight_returns_summary() -> None:
    client = _query_returning([_summary_row("ins-42")])
    reader = _reader(client)

    summary = reader.get_insight("ins-42")

    assert summary is not None
    assert summary.insight_id == "ins-42"
    assert _params(client.query.call_args)["insight_id"] == "ins-42"


def test_get_insight_missing_returns_none() -> None:
    client = _query_returning([])
    reader = _reader(client)

    assert reader.get_insight("nope") is None


# --- get_insight_with_occurrences -------------------------------------------------------


def test_get_insight_with_occurrences_merges_summary_and_occurrences() -> None:
    # 1) summary aggregation, 2) occurrence count, 3) occurrence page.
    client = _query_returning(
        [_summary_row("ins-1")],
        [{"total": 2}],
        [_occurrence_row(_RUBRIC_JSON)],
    )
    reader = _reader(client)

    detail = reader.get_insight_with_occurrences(
        insight_id="ins-1", limit=30, offset=0
    )

    assert detail is not None
    view, occurrences, total = detail
    assert view.insight_id == "ins-1"
    assert total == 2
    assert (
        occurrences[0].rubrics[0].rubric.expected_behavior
        == "call create_ticket"
    )


def test_get_insight_with_occurrences_missing_returns_none() -> None:
    # Unknown insight -> the summary query returns nothing, and no occurrence
    # query is issued.
    client = _query_returning([])
    reader = _reader(client)

    assert (
        reader.get_insight_with_occurrences(
            insight_id="nope", limit=30, offset=0
        )
        is None
    )
    assert client.query.call_count == 1


# --- list_occurrences ---------------------------------------------------------


def _occurrence_row(rubrics: Any, **overrides: Any) -> dict[str, Any]:
    row = {
        "occurrence_id": "occ-1",
        "insight_id": "ins-1",
        "run_id": "run-1",
        "created_at": _NOW,
        "agent_name": _AGENT,
        "agent_revision": None,
        "label": "made no tool call",
        "item_count": 2,
        "trace_count": 2,
        "trajectory_ids": ["case-1", "case-2"],
        "analyses": None,
        "rubrics": rubrics,
    }
    row.update(overrides)
    return row


_RUBRIC_JSON = [
    {
        "rubric": {
            "rubric_id": "r-1",
            "expected_behavior": "call create_ticket",
            "actual_behavior": "no tool call was made",
        },
        "eval_case_id": "case-1",
        "trace": [{"role": "user"}],
    }
]


def test_list_occurrences_rehydrates_rubrics_from_json_string() -> None:
    client = _query_returning(
        [{"total": 1}], [_occurrence_row(json.dumps(_RUBRIC_JSON))]
    )
    reader = _reader(client)

    occurrences, total = reader.list_occurrences(
        insight_id="ins-1", limit=1, offset=0
    )

    assert total == 1
    assert (
        occurrences[0].rubrics[0].rubric.expected_behavior
        == "call create_ticket"
    )
    assert occurrences[0].rubrics[0].eval_case_id == "case-1"


def test_list_occurrences_names_every_trajectory_behind_the_sighting() -> None:
    """The ids, not just their number: they are what a reader resolves against
    the `trajectories` table, and there are more of them than sampled rubrics."""
    client = _query_returning(
        [{"total": 1}], [_occurrence_row(json.dumps(_RUBRIC_JSON))]
    )
    reader = _reader(client)

    occurrences, _ = reader.list_occurrences(
        insight_id="ins-1", limit=1, offset=0
    )

    assert occurrences[0].trajectory_ids == ["case-1", "case-2"]


def test_list_occurrences_reads_an_occurrence_written_before_the_column() -> (
    None
):
    """There is no backfill, so history has the count and no ids. That reads as
    an empty list rather than failing the page it appears on."""
    client = _query_returning(
        [{"total": 1}],
        [_occurrence_row(json.dumps(_RUBRIC_JSON), trajectory_ids=None)],
    )
    reader = _reader(client)

    occurrences, _ = reader.list_occurrences(
        insight_id="ins-1", limit=1, offset=0
    )

    assert occurrences[0].trajectory_ids == []
    assert occurrences[0].trace_count == 2


def test_list_occurrences_rehydrates_rubrics_from_parsed_value() -> None:
    # Newer BigQuery clients hand back a JSON column already parsed, not as a str.
    client = _query_returning([{"total": 1}], [_occurrence_row(_RUBRIC_JSON)])
    reader = _reader(client)

    occurrences, _ = reader.list_occurrences(
        insight_id="ins-1", limit=1, offset=0
    )

    assert (
        occurrences[0].rubrics[0].rubric.expected_behavior
        == "call create_ticket"
    )


def test_a_row_written_before_the_state_column_reads_as_a_sighting() -> None:
    """The column is NULLABLE for the rows already in the table, and every row
    this path can return names an insight -- the query matches ``insight_id``
    against a real id -- so a sighting is what such a row was."""
    client = _query_returning(
        [{"total": 1}], [_occurrence_row(_RUBRIC_JSON, occurrence_state=None)]
    )
    reader = _reader(client)

    occurrences, _ = reader.list_occurrences(
        insight_id="ins-1", limit=1, offset=0
    )

    assert occurrences[0].occurrence_state is OccurrenceState.TRACKED


def test_list_occurrences_rehydrates_analyses() -> None:
    """A row written before any analysis ran carries no entry, not an empty one."""
    client = _query_returning([{"total": 1}], [_occurrence_row(_RUBRIC_JSON)])
    reader = _reader(client)
    occurrences, _ = reader.list_occurrences(
        insight_id="ins-1", limit=1, offset=0
    )
    assert occurrences[0].analyses == {}

    client = _query_returning(
        [{"total": 1}],
        [
            _occurrence_row(
                _RUBRIC_JSON,
                analyses={
                    "verification": {
                        "model": "gemini-3.7-flash",
                        "valid": True,
                        "explanation": "Agent omitted the required ticket ID.",
                        "refined_label": "called create_ticket without an id",
                    }
                },
            )
        ],
    )
    reader = _reader(client)
    occurrences, _ = reader.list_occurrences(
        insight_id="ins-1", limit=1, offset=0
    )
    result = occurrences[0].analyses["verification"]
    assert result.model == "gemini-3.7-flash"
    assert result.valid is True
    assert result.explanation == "Agent omitted the required ticket ID."
    assert result.refined_label == "called create_ticket without an id"
    # The sweep's own label is left as the sweep recorded it.
    assert occurrences[0].label == "made no tool call"


def test_list_occurrences_skips_a_malformed_analysis() -> None:
    """One unparseable result is dropped; the occurrence and its siblings survive."""
    client = _query_returning(
        [{"total": 1}],
        [
            _occurrence_row(
                _RUBRIC_JSON,
                analyses={
                    "broken": ["not", "a", "result"],
                    "verification": {"model": "m"},
                },
            )
        ],
    )
    reader = _reader(client)
    occurrences, _ = reader.list_occurrences(
        insight_id="ins-1", limit=1, offset=0
    )
    assert set(occurrences[0].analyses) == {"verification"}


def test_list_occurrences_skips_malformed_rubric_entry() -> None:
    client = _query_returning(
        [{"total": 1}],
        [_occurrence_row([{"not": "a rubric example"}, _RUBRIC_JSON[0]])],
    )
    reader = _reader(client)

    occurrences, _ = reader.list_occurrences(
        insight_id="ins-1", limit=1, offset=0
    )

    # The junk entry is dropped; the valid one survives.
    assert [e.rubric.expected_behavior for e in occurrences[0].rubrics] == [
        "call create_ticket"
    ]


def test_list_occurrences_run_id_filter_binds() -> None:
    client = _query_returning([{"total": 0}], [])
    reader = _reader(client)

    reader.list_occurrences(
        insight_id="ins-1", run_id="run-3", limit=1, offset=0
    )

    page_sql = _sql(client.query.call_args_list[1])
    assert "o.run_id = @run_id" in page_sql
    assert _params(client.query.call_args_list[1])["run_id"] == "run-3"


def test_list_occurrences_occurrence_id_filter_binds() -> None:
    client = _query_returning([{"total": 0}], [])
    reader = _reader(client)

    reader.list_occurrences(
        insight_id="ins-1", occurrence_id="occ-3", limit=1, offset=0
    )

    page_sql = _sql(client.query.call_args_list[1])
    assert "o.occurrence_id = @occurrence_id" in page_sql
    assert _params(client.query.call_args_list[1])["occurrence_id"] == "occ-3"


def test_list_occurrences_occurrence_id_alone_binds_without_an_insight() -> (
    None
):
    """Verifies a globally unique occurrence_id resolves under agent scope alone."""
    client = _query_returning([{"total": 0}], [])
    reader = _reader(client)

    reader.list_occurrences(occurrence_id="occ-3", limit=1, offset=0)

    page_call = client.query.call_args_list[1]
    assert "o.occurrence_id = @occurrence_id" in _sql(page_call)
    assert "@insight_id" not in _sql(page_call)
    # Preserves agent tenancy filtering when insight_id is omitted.
    assert _params(page_call) == {
        "agent_name": _AGENT,
        "occurrence_id": "occ-3",
        "limit": 1,
        "offset": 0,
    }


def test_list_occurrences_without_any_id_is_rejected() -> None:
    """Verifies querying without an insight_id or occurrence_id raises ValueError."""
    client = _query_returning([{"total": 0}], [])
    reader = _reader(client)

    with pytest.raises(ValueError, match="insight_id or an occurrence_id"):
        reader.list_occurrences(limit=1, offset=0)

    client.query.assert_not_called()


def test_list_occurrences_orders_latest_first() -> None:
    client = _query_returning([{"total": 0}], [])
    reader = _reader(client)

    reader.list_occurrences(insight_id="ins-1", limit=30, offset=0)

    # Latest to oldest, with a deterministic tiebreaker for stable pagination.
    assert "ORDER BY o.created_at DESC, o.occurrence_id" in _sql(
        client.query.call_args_list[1]
    )


# --- root causes --------------------------------------------------------------


def _missing_root_cause_table(client: mock.MagicMock) -> mock.MagicMock:
    """Configures ``get_table`` on the mock client to simulate a missing root-cause table.

    Args:
        client: Mock BigQuery client to configure.

    Returns:
        Configured mock client raising NotFound on `get_table`.
    """
    client.get_table.side_effect = NotFound("no such table")
    return client


def _root_cause_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "root_cause_id": "rc-1",
        "insight_id": "ins-1",
        "occurrence_id": "occ-1",
        "agent_revision": "agent-00042-abc",
        "summary": "The instruction states the step as a preference.",
        "edits": json.dumps(
            [
                {
                    "path": "app/agent.py",
                    "start_line": 42,
                    "end_line": 44,
                    "before": "old\n",
                    "after": "new\n",
                    "rationale": "States it as a constraint.",
                }
            ]
        ),
        "created_at": _NOW,
    }
    row.update(overrides)
    return row


def test_the_marker_is_a_correlated_subquery_on_the_insight_id() -> None:
    """Verifies ``has_root_cause`` correlates on ``insight_id`` without altering occurrence aggregations."""
    client = _query_returning([{"total": 1}], [_summary_row()])

    reader_list(client)

    page_sql = _sql(client.query.call_args_list[1])
    assert f"`{_ROOT_CAUSES_REF}` rc" in page_sql
    assert (
        "WHERE COALESCE(rm.merged_into_insight_id, rc.insight_id) = i.insight_id)"
        " AS has_root_cause"
    ) in page_sql
    # The marker leaves the aggregations alone: they still count occurrences and
    # sum traces, off the `owned` CTE that resolves each sighting to the insight
    # it counts for.
    assert "COUNT(owned.occurrence_id) AS occurrence_count" in page_sql
    assert "SUM(owned.trace_count) AS trace_count" in page_sql
    assert (
        "GROUP BY i.insight_id, i.agent_name, i.label, i.status, i.created_at"
        in (page_sql)
    )


def test_the_detail_query_carries_the_marker_too() -> None:
    """Verifies get_insight includes the has_root_cause column in its query."""
    client = _query_returning([_summary_row("ins-42")])

    _reader(client).get_insight("ins-42")

    assert "AS has_root_cause" in _sql(client.query.call_args)


def test_a_diagnosed_insight_reads_back_marked() -> None:
    client = _query_returning(
        [{"total": 1}], [_summary_row(has_root_cause=True)]
    )

    summaries, _ = reader_list(client)

    assert summaries[0].has_root_cause is True


def test_the_root_cause_filter_applies_to_the_count_and_the_page() -> None:
    """Verifies has_root_cause filter is included in both count and pagination queries."""
    client = _query_returning([{"total": 0}], [])

    _reader(client).list_insights(limit=30, offset=0, has_root_cause=True)

    count_sql = _sql(client.query.call_args_list[0])
    page_sql = _sql(client.query.call_args_list[1])
    clause = f"EXISTS (SELECT 1 FROM `{_ROOT_CAUSES_REF}` rc"  # noqa: S608 - trusted test constants
    assert clause in count_sql
    assert clause in page_sql


def test_the_negative_filter_asks_for_the_absence_of_a_record() -> None:
    client = _query_returning([{"total": 0}], [])

    _reader(client).list_insights(limit=30, offset=0, has_root_cause=False)

    assert "NOT EXISTS (SELECT 1 FROM" in _sql(client.query.call_args_list[0])


def test_no_root_cause_filter_leaves_the_where_clause_alone() -> None:
    client = _query_returning([{"total": 0}], [])

    _reader(client).list_insights(limit=30, offset=0)

    assert "EXISTS" not in _sql(client.query.call_args_list[0])


def test_root_causes_for_resolves_the_newest_record_per_occurrence() -> None:
    client = _query_returning([_root_cause_row()])

    records = _reader(client).list_root_causes("ins-1")

    assert [r.root_cause_id for r in records] == ["rc-1"]
    sql = _sql(client.query.call_args)
    assert "QUALIFY ROW_NUMBER() OVER (" in sql
    # root_cause_id breaks timestamp ties to guarantee deterministic selection.
    assert (
        "PARTITION BY rc.occurrence_id ORDER BY rc.created_at DESC, rc.root_cause_id"
        in (sql)
    )
    assert _params(client.query.call_args)["insight_id"] == "ins-1"


def test_root_causes_for_reads_one_insight_in_one_query() -> None:
    """Verifies reading all root causes for an insight executes in a single query."""
    client = _query_returning(
        [_root_cause_row(), _root_cause_row(occurrence_id="occ-2")]
    )

    records = _reader(client).list_root_causes("ins-1")

    assert client.query.call_count == 1
    assert {r.occurrence_id for r in records} == {"occ-1", "occ-2"}


def test_root_cause_history_returns_every_revision() -> None:
    client = _query_returning(
        [_root_cause_row(), _root_cause_row(root_cause_id="rc-2")]
    )

    records = _reader(client).list_root_causes("ins-1", history=True)

    assert [r.root_cause_id for r in records] == ["rc-1", "rc-2"]
    sql = _sql(client.query.call_args)
    assert "QUALIFY" not in sql
    assert "ORDER BY rc.created_at DESC, rc.root_cause_id" in sql


# --- a diagnosis follows a deduplication ---------------------------------------
# A target whose counts absorb a duplicate's sightings absorbs its diagnoses with
# them: the three reads below are the three places `has_root_cause` and the
# records themselves are decided, and a fold applied to two of them would leave a
# card claiming a diagnosis it cannot show, or showing one it denies having.


def test_the_marker_credits_a_duplicates_diagnosis_to_its_target() -> None:
    client = _query_returning([{"total": 1}], [_summary_row()])

    reader_list(client)

    page_sql = _sql(client.query.call_args_list[1])
    assert (
        f"LEFT JOIN `{_INSIGHTS_REF}` rm ON rm.insight_id = rc.insight_id"
        in page_sql
    )
    assert (
        "WHERE COALESCE(rm.merged_into_insight_id, rc.insight_id) = i.insight_id"
        in page_sql
    )


def test_the_root_cause_filter_resolves_the_owner_too() -> None:
    """Or the list would show a diagnosed badge on a row that "diagnosed only"
    filters away."""
    client = _query_returning([{"total": 0}], [])

    _reader(client).list_insights(limit=30, offset=0, has_root_cause=True)

    where = _sql(client.query.call_args_list[0])
    assert (
        f"EXISTS (SELECT 1 FROM `{_ROOT_CAUSES_REF}` rc "  # noqa: S608 - trusted test constants
        f"LEFT JOIN `{_INSIGHTS_REF}` rm ON rm.insight_id = rc.insight_id "
        "WHERE COALESCE(rm.merged_into_insight_id, rc.insight_id) = i.insight_id)"
    ) in where


def test_root_causes_for_reads_by_owner_not_by_written_against() -> None:
    """So the detail pane lists what the header counted. The record keeps the
    ``insight_id`` it was written against -- only the lookup folds."""
    client = _query_returning([_root_cause_row()])

    records = _reader(client).list_root_causes("ins-1")

    sql = _sql(client.query.call_args)
    assert (
        f"LEFT JOIN `{_INSIGHTS_REF}` rm ON rm.insight_id = rc.insight_id"
        in sql
    )
    assert (
        "WHERE COALESCE(rm.merged_into_insight_id, rc.insight_id) = @insight_id"
        in (sql)
    )
    # The fold is on the lookup; the row still says which insight it diagnosed.
    assert records[0].insight_id == "ins-1"


def test_a_record_written_without_a_revision_still_reads() -> None:
    """Verifies records with null agent_revision deserialize as empty strings."""
    client = _query_returning([_root_cause_row(agent_revision=None)])

    records = _reader(client).list_root_causes("ins-1")

    assert records[0].agent_revision == ""


def test_a_record_survives_a_malformed_edits_payload() -> None:
    """Verifies unparseable edits are skipped while preserving root-cause summary."""
    client = _query_returning([_root_cause_row(edits="{not json")])

    records = _reader(client).list_root_causes("ins-1")

    assert records[0].edits == []
    assert records[0].summary


def test_a_malformed_edit_is_skipped_and_its_siblings_survive() -> None:
    client = _query_returning(
        [
            _root_cause_row(
                edits=[
                    {"not": "an edit"},
                    {
                        "path": "app/agent.py",
                        "start_line": 1,
                        "end_line": 2,
                        "after": "x",
                    },
                ]
            )
        ]
    )

    records = _reader(client).list_root_causes("ins-1")

    assert [e.path for e in records[0].edits] == ["app/agent.py"]


def test_a_dataset_without_the_table_still_lists_insights() -> None:
    """Verifies list_insights falls back to FALSE for has_root_cause when the table is absent."""
    client = _missing_root_cause_table(
        _query_returning([{"total": 1}], [_summary_row()])
    )

    summaries, total = reader_list(client)

    assert total == 1
    assert summaries[0].has_root_cause is False
    page_sql = _sql(client.query.call_args_list[1])
    assert "FALSE AS has_root_cause" in page_sql
    assert _ROOT_CAUSES_REF not in page_sql


def test_a_dataset_without_the_table_is_probed_once() -> None:
    """Verifies table presence probe result is cached on the reader."""
    client = _missing_root_cause_table(
        _query_returning([{"total": 0}], [], [_summary_row()])
    )
    reader = _reader(client)

    reader.list_insights(limit=30, offset=0)
    reader.get_insight("ins-1")

    assert client.get_table.call_count == 1


def test_a_dataset_without_the_table_reports_no_root_causes() -> None:
    client = _missing_root_cause_table(_query_returning([]))

    assert _reader(client).list_root_causes("ins-1") == []
    client.query.assert_not_called()


def test_an_explicit_root_cause_filter_against_a_missing_table_is_an_error() -> (
    None
):
    """Verifies filtering on has_root_cause raises NotFound if the table is missing."""
    client = _missing_root_cause_table(_query_returning([{"total": 0}], []))

    with pytest.raises(NotFound, match="insight_root_causes"):
        _reader(client).list_insights(limit=30, offset=0, has_root_cause=True)

    client.query.assert_not_called()


# --- the two operator judgements ----------------------------------------------


def test_the_list_hides_a_dismissed_or_deduplicated_insight() -> None:
    """Unconditionally, and on both the page and the count: the dashboard hides
    nothing client-side, so a row that keeps coming back is a button that reads
    as broken."""
    client = _query_returning([{"total": 0}], [])
    reader = _reader(client)

    reader.list_insights(limit=30, offset=0)

    for call in client.query.call_args_list:
        assert "i.dismissed_at IS NULL" in _sql(call)
        assert "i.merged_into_insight_id IS NULL" in _sql(call)


def test_get_insight_keeps_serving_one_that_the_list_hides() -> None:
    """A bookmarked link should not start 404ing because somebody tidied the
    list."""
    client = _query_returning(
        [
            _summary_row(
                "ins-1",
                dismissed_at=_NOW,
                merged_into_insight_id="ins-2",
                merged_at=_NOW,
            )
        ]
    )
    reader = _reader(client)

    summary = reader.get_insight("ins-1")

    assert summary is not None
    assert summary.dismissed_at == _NOW
    assert summary.merged_into_insight_id == "ins-2"
    assert summary.merged_at == _NOW
    sql = _sql(client.query.call_args)
    assert "dismissed_at IS NULL" not in sql
    assert "merged_into_insight_id IS NULL" not in sql


def test_the_counts_are_aggregated_over_the_sightings_an_insight_owns() -> None:
    """Deduplication moves no occurrence, so the fold happens here. One COALESCE
    is enough only because the write flattens chains -- the two are one design."""
    client = _query_returning([{"total": 0}], [])
    reader = _reader(client)

    reader.list_insights(limit=30, offset=0)

    page_sql = _sql(client.query.call_args_list[1])
    assert (
        "COALESCE(m.merged_into_insight_id, o.insight_id) AS owner_id"
        in page_sql
    )
    assert "LEFT JOIN owned\n  ON owned.owner_id = i.insight_id" in page_sql


def test_occurrences_are_selected_by_owner_not_by_the_row_they_were_written_on() -> (
    None
):
    """So a target's detail pane shows the evidence its counts claim, and a
    duplicate shows none rather than listing sightings its own header counts as
    zero."""
    client = _query_returning([{"total": 0}], [])
    reader = _reader(client)

    reader.list_occurrences(insight_id="ins-1", limit=30, offset=0)

    for call in client.query.call_args_list:
        assert (
            "COALESCE(m.merged_into_insight_id, o.insight_id) = @insight_id"
            in _sql(call)
        )


def test_the_run_filter_resolves_ownership_too() -> None:
    """Otherwise a sweep's own view lists neither insight: the duplicate is
    hidden, and the target has no occurrence carrying that run id."""
    client = _query_returning([{"total": 0}], [])
    reader = _reader(client)

    reader.list_insights(run_id="run-7", limit=30, offset=0)

    count_sql = _sql(client.query.call_args_list[0])
    assert "EXISTS (SELECT 1 FROM" in count_sql
    assert (
        "WHERE COALESCE(m.merged_into_insight_id, o.insight_id) = i.insight_id"
        in count_sql
    )
