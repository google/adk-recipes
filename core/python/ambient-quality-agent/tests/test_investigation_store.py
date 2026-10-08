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

"""Tests for `BigQueryInvestigationStore`'s plumbing (offline, no BigQuery).

Asserts on the rows it loads and the SQL it emits. The lifecycle driving it --
who may create a run, what a status transition implies -- belongs to the
registry and is covered by `test_investigation_run`, which drives the same store
over an in-memory row list.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest import mock

from ambient_quality_agent.tools.investigations.bigquery_store import (
    EVENTS_TABLE,
    INVESTIGATIONS_TABLE,
    BigQueryInvestigationStore,
)
from ambient_quality_agent.tools.investigations.models import (
    NON_AMBIENT_TRIGGER_TYPES,
    InvestigationCounters,
    InvestigationRecord,
    RunStatus,
    list_snapshot_columns,
)
from ambient_quality_agent.tools.investigations.store import (
    IN_FLIGHT_HORIZON_HOURS,
)

_PROJECT = "test-project"
_DATASET = "aqua_insights"
_TABLE_REF = f"{_PROJECT}.{_DATASET}.{INVESTIGATIONS_TABLE}"
_EVENTS_REF = f"{_PROJECT}.{_DATASET}.{EVENTS_TABLE}"


def _store(client: mock.MagicMock) -> BigQueryInvestigationStore:
    return BigQueryInvestigationStore(
        client=client, project_id=_PROJECT, dataset=_DATASET
    )


def _loaded_rows(client: mock.MagicMock) -> list[dict[str, Any]]:
    """Collects all row dictionaries passed to load_table_from_json calls.

    Args:
        client: Mock BigQuery client.

    Returns:
        List of loaded row dictionaries in call order.
    """
    return [
        row
        for call in client.load_table_from_json.call_args_list
        for row in call.args[0]
    ]


def _loaded_table(client: mock.MagicMock) -> str:
    """Extracts destination table reference from the most recent load job.

    Args:
        client: Mock BigQuery client.

    Returns:
        Destination table reference string.
    """
    return client.load_table_from_json.call_args.args[1]


def _query_call(client: mock.MagicMock) -> tuple[str, dict[str, Any]]:
    """Extract SQL query string and bound parameters from the last client.query call.

    Args:
        client: Mock BigQuery client.

    Returns:
        Tuple of (sql_query, parameters_dict).
    """
    call = client.query.call_args
    params = {
        p.name: list(p.values) if hasattr(p, "values") else p.value
        for p in call.kwargs["job_config"].query_parameters
    }
    return call.args[0], params


def _row(**overrides: Any) -> dict[str, Any]:
    """Builds a mock BigQuery row with TIMESTAMP fields converted to datetimes.

    Args:
        **overrides: Field values to override defaults.

    Returns:
        Row dictionary matching BigQuery client output format.
    """
    row = {
        "run_id": "r1",
        "created_at": dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        "updated_at": dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
    }
    row.update(overrides)
    return row


def test_append_writes_the_whole_record_as_one_row() -> None:
    client = mock.MagicMock()
    stored = _store(client).append(
        InvestigationRecord(
            run_id="r1",
            observed_agent_name="watched",
            metrics={"multi_turn": ["task_success"]},
            budget_per_metric=10,
            status=RunStatus.RUNNING,
        )
    )

    assert _loaded_table(client) == _TABLE_REF
    (row,) = _loaded_rows(client)
    assert row["run_id"] == "r1"
    assert row["observed_agent_name"] == "watched"
    assert row["budget_per_metric"] == 10
    assert row["status"] == "running"
    # JSON columns go in as JSON text, the convention the insight tables use.
    assert json.loads(row["metrics"]) == {"multi_turn": ["task_success"]}
    # Restamped, which is what makes this snapshot the newest of its run.
    assert row["updated_at"] == stored.updated_at
    # Appending reads nothing: the caller brought the whole record.
    client.query.assert_not_called()


def test_append_keeps_the_events_out_of_the_snapshot() -> None:
    client = mock.MagicMock()
    _store(client).append(InvestigationRecord(run_id="r1"))

    (row,) = _loaded_rows(client)
    assert "events" not in row


def test_append_drops_the_events_from_the_stored_summary() -> None:
    client = mock.MagicMock()
    _store(client).append(
        InvestigationRecord(
            run_id="r1", summary={"metrics_passed": 1, "events": ["a\n"]}
        )
    )

    (row,) = _loaded_rows(client)
    assert json.loads(row["summary"]) == {"metrics_passed": 1}


def test_append_event_writes_one_row_to_the_events_table() -> None:
    client = mock.MagicMock()
    _store(client).append_event("r1", "**step**\n", source="init")

    assert _loaded_table(client) == _EVENTS_REF
    (row,) = _loaded_rows(client)
    assert row["run_id"] == "r1"
    assert row["text"] == "**step**\n"
    assert row["source"] == "init"
    assert row["created_at"]
    # An event is the event: no run columns ride along with it.
    assert "status" not in row and "observed_agent_name" not in row
    # And it reads nothing first, which is what makes it cheap in a node.
    client.query.assert_not_called()


def test_get_asks_bigquery_for_the_newest_snapshot_and_the_events() -> None:
    client = mock.MagicMock()
    client.query.return_value.result.side_effect = [
        [_row(status="running")],
        [
            {
                "run_id": "r1",
                "created_at": dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
                "text": "step\n",
                "source": "init",
            }
        ],
    ]

    record = _store(client).get("r1")

    snapshot_sql = client.query.call_args_list[0].args[0]
    events_sql = client.query.call_args_list[1].args[0]
    assert f"FROM `{_TABLE_REF}`" in snapshot_sql
    assert "ORDER BY updated_at DESC LIMIT 1" in snapshot_sql
    assert "SELECT *" not in snapshot_sql
    assert f"FROM `{_EVENTS_REF}`" in events_sql
    assert "ORDER BY created_at" in events_sql
    assert record is not None
    assert record.status == RunStatus.RUNNING
    assert [e.text for e in record.events] == ["step\n"]


def test_get_without_events_does_not_query_for_them() -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [_row(status="running")]

    record = _store(client).get("r1", include_events=False)

    assert client.query.call_count == 1
    assert record is not None and record.events == []


def test_get_on_an_unknown_run_is_none() -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []
    assert _store(client).get("nope") is None


def test_list_recent_takes_one_snapshot_per_run_and_skips_the_config() -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [_row(status="done")]

    _store(client).list_recent(limit=7)

    sql, params = _query_call(client)
    assert (
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY updated_at DESC) = 1"
        in sql
    )
    assert "ORDER BY created_at DESC" in sql
    assert params == {"limit": 7}
    # The heavy column stays out of the list read, and so does the other table.
    assert "effective_config" not in sql
    assert _EVENTS_REF not in sql


def test_list_recent_filters_status_after_picking_the_newest_snapshot() -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []

    _store(client).list_recent(status=RunStatus.DONE)

    sql, params = _query_call(client)
    # The filter sits outside the QUALIFY subquery, so a run that has moved on
    # is absent rather than matched on a superseded snapshot.
    assert sql.index("QUALIFY") < sql.index("status = @status")
    assert params["status"] == "done"


def test_list_recent_returns_oldest_first() -> None:
    client = mock.MagicMock()
    # BigQuery orders newest first (the LIMIT needs it); the store flips them.
    client.query.return_value.result.return_value = [
        _row(run_id="new", created_at=dt.datetime(2026, 6, 2, tzinfo=dt.UTC)),
        _row(run_id="old", created_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC)),
    ]

    records = _store(client).list_recent()

    assert [r.run_id for r in records] == ["old", "new"]


# --- counters ------------------------------------------------------------------


def test_append_writes_every_counter_as_json() -> None:
    client = mock.MagicMock()
    _store(client).append(
        InvestigationRecord(run_id="r1", counters={"traces_scanned": 12})
    )

    (row,) = _loaded_rows(client)
    stored = json.loads(row["counters"])
    assert stored["traces_scanned"] == 12
    # Every counter is written, not just the one that was set, so the totals
    # query finds the same keys on every run.
    assert stored["clusters_created"] == 0


def test_counters_read_back_from_the_stored_json() -> None:
    record = InvestigationRecord.model_validate(
        {"run_id": "r1", "counters": '{"traces_ingested": 4}'}
    )
    assert record.counters.traces_ingested == 4


def test_format_run_survives_counters_copied_on_as_a_plain_dict() -> None:
    """`model_copy(update=...)` does not coerce, and the alert logger copies the
    very changes `execute_investigation` writes -- a plain ``counters`` dict --
    onto the record before formatting it.

    Reaching for `.model_dump()` on that dict raised inside the one caller that
    wraps this in a try/except, so a finished run logged "could not log run for
    alerting" and its email was never sent. Silent by construction: the guard
    that keeps a logging fault from failing the run also hid this.
    """
    from ambient_quality_agent.core.investigation.model import format_run

    record = InvestigationRecord(run_id="r1").model_copy(
        update={"counters": {"insights_created": 1}}
    )

    view = format_run(record)

    assert view["counters"]["insights_created"] == 1
    assert view["counters"]["traces_scanned"] == 0


def test_a_run_recorded_without_counters_reads_as_zero() -> None:
    """A snapshot predating the column, or one whose payload will not parse,
    counted nothing -- which is a zeroed model, not a validation error."""
    without = InvestigationRecord.model_validate({"run_id": "r1"})
    unparseable = InvestigationRecord.model_validate(
        {"run_id": "r1", "counters": "{not json"}
    )
    assert without.counters.traces_scanned == 0
    assert unparseable.counters.traces_scanned == 0


def test_totals_sums_every_counter_over_one_snapshot_per_run() -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [
        {"investigations": 2, "traces_scanned": 30, "traces_ingested": 9}
    ]

    totals, _ = _store(client).sum_counters()

    sql, params = _query_call(client)
    assert params == {}
    # One row per run before summing, or a run updated five times would count
    # its counters five times over.
    assert (
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY updated_at DESC) = 1"
        in sql
    )
    assert (
        "SUM(COALESCE(SAFE_CAST(JSON_VALUE(counters, '$.traces_scanned')" in sql
    )
    # Every declared counter is summed, so adding one needs no change here.
    for name in InvestigationCounters.model_fields:
        assert f"AS {name}" in sql
    assert totals == {
        "investigations": 2,
        "traces_scanned": 30,
        "traces_ingested": 9,
    }


def test_totals_unwraps_a_counters_column_stored_as_json_text() -> None:
    """`_record_to_row` writes JSON columns as JSON *text*, so the column holds a JSON
    string and not an object -- and reading a field straight out of it returns
    NULL rather than failing, which sums to a plausible-looking zero.

    Measured against a deployed table on 2026-08-19: `JSON_TYPE(counters)` was
    `string`, `JSON_VALUE(counters, '$.traces_scanned')` was NULL, and the
    unwrapped read was `1`, while the run's own record showed the right counts
    all along -- the Python read path decodes the double encoding and this
    query is the only thing that reads a JSON column in SQL.
    """
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []

    _store(client).sum_counters()

    sql, _ = _query_call(client)
    # One unwrap in the subquery, so the sums above can stay simple. The
    # COALESCE keeps it working on a column that holds a real object.
    assert (
        "COALESCE(SAFE.PARSE_JSON(JSON_VALUE(counters)), counters) AS counters"
        in sql
    )


def test_totals_on_an_empty_table_is_zero_everywhere() -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []

    totals, _ = _store(client).sum_counters()

    assert totals["investigations"] == 0
    assert set(totals) == {
        "investigations",
        *InvestigationCounters.model_fields,
    }


def test_the_mark_excludes_every_run_that_covered_nothing() -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [
        {"mark": dt.datetime(2026, 6, 1, 4, tzinfo=dt.UTC)}
    ]

    mark = _store(client).get_last_finished_window_end("watched")

    sql, params = _query_call(client)
    # Filter outside the QUALIFY subquery to evaluate each run on its latest snapshot.
    assert sql.index("QUALIFY") < sql.index("WHERE status = @status")
    for clause in (
        "NOT dry_run",
        "trigger_type NOT IN UNNEST(@non_ambient)",
        "observed_agent_name = @agent",
    ):
        assert clause in sql
    assert params == {
        "status": "done",
        "agent": "watched",
        "non_ambient": ["custom", "manual"],
    }
    assert mark == "2026-06-01T04:00:00+00:00"


def test_the_mark_ignores_the_windows_a_person_named() -> None:
    """Verify get_last_finished_window_end ignores non-ambient trigger types."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"mark": None}]

    _store(client).get_last_finished_window_end("watched")

    _, params = _query_call(client)
    assert set(params["non_ambient"]) == set(NON_AMBIENT_TRIGGER_TYPES)


def test_counting_the_runs_in_flight_counts_only_pending_and_running() -> None:
    """Finished runs are over, and a scheduled one has not been handed work."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"in_flight": 2}]

    assert _store(client).count_in_flight("watched") == 2

    sql, params = _query_call(client)
    # Deduplicate by run_id using QUALIFY to evaluate only the latest snapshot per run.
    assert sql.index("QUALIFY") < sql.index(
        "WHERE observed_agent_name = @agent"
    )
    assert "status IN UNNEST(@in_flight)" in sql
    assert set(params["in_flight"]) == {"pending", "running"}
    assert params["agent"] == "watched"


def test_overdue_scheduled_runs_are_read_off_the_newest_snapshot() -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"run_id": "late"}]

    assert _store(client).list_overdue_scheduled(grace_minutes=60) == ["late"]

    sql, params = _query_call(client)
    # A run that left `scheduled` must not match on its older snapshot.
    assert sql.index("QUALIFY") < sql.index("WHERE status = @status")
    assert "due_at < @deadline" in sql
    assert params["status"] == RunStatus.SCHEDULED.value
    age = dt.datetime.now(tz=dt.UTC) - params["deadline"]
    assert abs(age - dt.timedelta(minutes=60)) < dt.timedelta(seconds=5)


def test_a_run_stuck_running_stops_counting_after_a_day() -> None:
    """Verify abandoned in-flight runs older than IN_FLIGHT_HORIZON_HOURS are excluded from count."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"in_flight": 0}]

    _store(client).count_in_flight("watched")

    sql, params = _query_call(client)
    assert "updated_at >= @since" in sql
    # BigQuery TIMESTAMP query parameters store datetime objects.
    age = dt.datetime.now(tz=dt.UTC) - params["since"]
    assert abs(
        age - dt.timedelta(hours=IN_FLIGHT_HORIZON_HOURS)
    ) < dt.timedelta(seconds=5)


def test_nothing_in_flight_counts_as_zero() -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"in_flight": None}]

    assert _store(client).count_in_flight("watched") == 0


def test_the_mark_is_none_when_no_run_has_finished() -> None:
    client = mock.MagicMock()
    # An aggregate query over empty results returns a single NULL row.
    client.query.return_value.result.return_value = [{"mark": None}]

    assert _store(client).get_last_finished_window_end("watched") is None


def test_the_mark_query_names_only_real_columns() -> None:
    # The query spells its columns out, so a rename in the schema has to fail
    # here rather than in a deployed query nothing runs until the next trigger.
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"mark": None}]

    _store(client).get_last_finished_window_end("watched")

    sql, _ = _query_call(client)
    for column in (
        "window_end",
        "status",
        "dry_run",
        "trigger_type",
        "observed_agent_name",
    ):
        assert column in list_snapshot_columns(), column
        assert column in sql, column


def test_totals_can_be_scoped_to_a_period() -> None:
    """Scoping in SQL is the whole point: summed off `list_recent` a period
    would stop at the fifty most recent sweeps, so on a busy deployment "the
    last week" would quietly be the last few hours."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"investigations": 3}]

    _store(client).sum_counters(
        window_start="2026-08-01T00:00:00+00:00",
        window_end="2026-08-08T00:00:00+00:00",
    )

    sql, params = _query_call(client)
    assert "window_end >= @window_start" in sql
    assert "window_end <= @window_end" in sql
    # Bound as TIMESTAMP, so the client parses the ISO text into an instant --
    # which is what makes the comparison a time comparison and not a string one.
    assert params == {
        "window_start": dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        "window_end": dt.datetime(2026, 8, 8, tzinfo=dt.UTC),
    }
    # Applied outside the QUALIFY, so the period selects runs by their window
    # instead of letting a superseded snapshot stand in for a run that has since
    # moved out of it.
    assert sql.index("WHERE window_end >=") > sql.index("QUALIFY ROW_NUMBER()")


def test_totals_report_the_span_the_counted_runs_cover() -> None:
    """Not the period that was asked for: a week's window over a three-day-old
    deployment covers three days, and figures labelled with the request would
    claim a quiet week where there was only an empty one."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [
        {
            "investigations": 2,
            "covered_start": dt.datetime(2026, 8, 6, tzinfo=dt.UTC),
            "covered_end": dt.datetime(2026, 8, 7, tzinfo=dt.UTC),
            "traces_scanned": 30,
        }
    ]

    totals, covered = _store(client).sum_counters(
        window_start="2026-08-01T00:00:00+00:00"
    )

    assert covered == {
        "start": "2026-08-06T00:00:00+00:00",
        "end": "2026-08-07T00:00:00+00:00",
    }
    # They are aggregates, not counters, so they stay out of the summed mapping.
    assert "covered_start" not in totals
    assert totals["traces_scanned"] == 30


def test_totals_over_nothing_cover_nothing() -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [
        {"investigations": 0, "covered_start": None, "covered_end": None}
    ]

    totals, covered = _store(client).sum_counters()

    assert totals["investigations"] == 0
    assert covered == {"start": None, "end": None}


def test_totals_takes_either_bound_on_its_own() -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []

    _store(client).sum_counters(window_start="2026-08-01T00:00:00+00:00")

    sql, params = _query_call(client)
    assert "window_end >= @window_start" in sql
    assert "@window_end" not in sql
    assert set(params) == {"window_start"}

    _store(client).sum_counters(window_end="2026-08-08T00:00:00+00:00")

    sql, params = _query_call(client)
    assert "window_end <= @window_end" in sql
    assert "@window_start" not in sql
    assert set(params) == {"window_end"}


def test_unscoped_totals_filter_on_nothing() -> None:
    """No period means every run, the ones that never derived a window
    included -- a WHERE here would silently drop them."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []

    _store(client).sum_counters()

    sql, params = _query_call(client)
    assert "WHERE" not in sql
    assert params == {}


def test_list_recent_can_be_scoped_to_a_period() -> None:
    """The list and the totals answer for the same span, so a dashboard with one
    period control cannot show rows from a wider window than its figures."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []

    _store(client).list_recent(
        window_start="2026-08-01T00:00:00+00:00",
        window_end="2026-08-08T00:00:00+00:00",
    )

    sql, params = _query_call(client)
    assert "window_end >= @window_start" in sql
    assert "window_end <= @window_end" in sql
    assert params["window_start"] == dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    assert params["window_end"] == dt.datetime(2026, 8, 8, tzinfo=dt.UTC)
    # Outside the QUALIFY for the same reason `totals` puts it there: filtering
    # inside would let a superseded snapshot represent a run whose current one
    # has moved out of the period.
    assert sql.index("WHERE") > sql.index("QUALIFY ROW_NUMBER()")


def test_list_recent_applies_the_period_before_the_limit() -> None:
    """A period wide enough to hold more than one page yields its most recent
    page. Ordering LIMIT before the window would instead page the whole table
    and then filter, so a busy deployment could return nothing for a period it
    has plenty of runs in."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []

    _store(client).list_recent(window_start="2026-08-01T00:00:00+00:00")

    sql, _ = _query_call(client)
    assert sql.index("WHERE") < sql.index("LIMIT @limit")


def test_list_recent_combines_a_status_with_a_period() -> None:
    """Verifies that status and window filters are combined in a single WHERE clause."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []

    _store(client).list_recent(
        status=RunStatus.DONE, window_start="2026-08-01T00:00:00+00:00"
    )

    sql, params = _query_call(client)
    assert "status = @status" in sql
    assert "window_end >= @window_start" in sql
    assert params["status"] == RunStatus.DONE.value
    assert params["window_start"] == dt.datetime(2026, 8, 1, tzinfo=dt.UTC)


def test_list_recent_without_a_period_has_no_window_filter() -> None:
    """No period means every run, including those that never derived a window
    -- which a bounded comparison would silently drop."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []

    _store(client).list_recent()

    sql, params = _query_call(client)
    # `window_end` is a selected column, so the absence worth asserting is the
    # comparison against it, and the WHERE that would carry it.
    assert "window_end >=" not in sql
    assert "window_end <=" not in sql
    assert "WHERE" not in sql
    assert set(params) == {"limit"}


def test_counters_by_day_groups_by_the_window_day() -> None:
    """Aggregated in SQL so a period longer than the list's cap still charts all
    of itself -- the same argument `totals` makes, one bucket at a time."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [
        {
            "day": "2026-08-20",
            "investigations": 4,
            "unmeasured": 1,
            **dict.fromkeys(InvestigationCounters.model_fields, 7),
        }
    ]

    days = _store(client).sum_counters_by_day(
        window_start="2026-08-01T00:00:00+00:00"
    )

    sql, params = _query_call(client)
    assert "GROUP BY day" in sql
    assert "DATE(window_end)" in sql
    assert "window_end >= @window_start" in sql
    assert params["window_start"] == dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    assert days == [
        {
            "day": "2026-08-20",
            "investigations": 4,
            "unmeasured": 1,
            **dict.fromkeys(InvestigationCounters.model_fields, 7),
        }
    ]


def test_counters_by_day_excludes_runs_with_no_window() -> None:
    """A run that never derived a window cannot be filed under a day, so it is
    out of the read rather than bucketed under a guess."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []

    _store(client).sum_counters_by_day()

    sql, _ = _query_call(client)
    assert "window_end IS NOT NULL" in sql


def test_counters_by_day_counts_zeroed_runs_as_unmeasured() -> None:
    """A failed, skipped or running sweep measured nothing; counting its zeros
    as data would draw an outage as a quiet day."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []

    _store(client).sum_counters_by_day()

    sql, _ = _query_call(client)
    assert "unmeasured" in sql
    assert "CASE WHEN (" in sql
    # Every counter participates in the "measured nothing" test, so a run that
    # recorded only a counter added later still counts as measured.
    for name in InvestigationCounters.model_fields:
        assert f"'$.{name}'" in sql
