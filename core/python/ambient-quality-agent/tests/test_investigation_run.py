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

"""Tests for the investigation run registry (data layer)."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from ambient_quality_agent.core.investigation import model as runs
from ambient_quality_agent.core.investigation.model import (
    InvestigationRecord,
    RunStatus,
)
from ambient_quality_agent.tools.investigations.models import (
    CustomOverrides,
    InvestigationCounters,
    InvestigationEvent,
    list_snapshot_columns,
)
from pydantic import ValidationError

_STATE: dict = {}
"""The registry reads state only to resolve the store, which the autouse
`investigation_store` fixture pins, so every call here can share one."""


# --------------------------------------------------------------------------- #
# Snapshots in, one record out                                                 #
# --------------------------------------------------------------------------- #


def test_create_run_writes_a_single_snapshot(investigation_store) -> None:
    record = _run(
        runs.create_run(_STATE, InvestigationRecord(observed_agent_name="a"))
    )

    (row,) = investigation_store.rows
    assert row["run_id"] == record.run_id
    assert row["observed_agent_name"] == "a"
    assert row["status"] == RunStatus.PENDING.value
    # Nothing has produced an output yet, so those columns stay out of the row.
    assert "summary" not in row and "finished_at" not in row


def test_get_run_roundtrip_and_missing() -> None:
    record = _run(runs.create_run(_STATE, InvestigationRecord()))
    fetched = _run(runs.get_run(_STATE, record.run_id))

    assert fetched is not None and fetched.run_id == record.run_id
    assert _run(runs.get_run(_STATE, "missing")) is None


def test_update_appends_a_whole_new_snapshot(investigation_store) -> None:
    record = _run(
        runs.create_run(_STATE, InvestigationRecord(observed_agent_name="a"))
    )

    updated = _run(
        runs.update_run(
            _STATE, record.run_id, status=RunStatus.DONE, summary={"k": "v"}
        )
    )

    assert len(investigation_store.rows) == 2
    latest = investigation_store.rows[-1]
    # The whole run, not just the change: the inputs are carried forward.
    assert latest["observed_agent_name"] == "a"
    assert latest["status"] == RunStatus.DONE.value
    assert updated is not None and updated.status == RunStatus.DONE
    # ...so the read is just the newest snapshot.
    stored = _run(runs.get_run(_STATE, record.run_id))
    assert stored is not None
    assert stored.observed_agent_name == "a"
    assert stored.status == RunStatus.DONE
    assert stored.summary == {"k": "v"}


def test_update_validates_the_values_it_is_given(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """`job_execution` passes the graph's counters through as a plain dict.
    Assigned rather than validated they stay one in a field typed
    `InvestigationCounters`, and a reader gets whichever shape it is handed."""
    record = _run(runs.create_run(_STATE, InvestigationRecord()))

    updated = _run(
        runs.update_run(
            _STATE,
            record.run_id,
            counters={"traces_scanned": 3, "insights_created": 1},
        )
    )

    assert updated is not None
    assert isinstance(updated.counters, InvestigationCounters)
    assert updated.counters.traces_scanned == 3
    stored = _run(runs.get_run(_STATE, record.run_id))
    assert stored is not None and stored.counters.insights_created == 1
    # In production the serializer warning is the only report of this.
    assert not [
        w
        for w in recwarn
        if "PydanticSerializationUnexpectedValue" in str(w.message)
    ]


def test_update_rejects_a_value_the_field_cannot_hold() -> None:
    """The other half of the guard below, which checks the name: a change that
    fits no field raises where it is made rather than at the write."""
    record = _run(runs.create_run(_STATE, InvestigationRecord()))

    with pytest.raises(ValidationError):
        _run(
            runs.update_run(
                _STATE, record.run_id, counters={"traces_scanned": "lots"}
            )
        )


def test_update_stamps_finished_on_terminal_status() -> None:
    record = _run(runs.create_run(_STATE, InvestigationRecord()))
    _run(runs.update_run(_STATE, record.run_id, status=RunStatus.DONE))

    stored = _run(runs.get_run(_STATE, record.run_id))
    assert stored is not None and stored.finished_at is not None


def test_update_missing_run_writes_nothing(investigation_store) -> None:
    assert _run(runs.update_run(_STATE, "nope", status=RunStatus.DONE)) is None
    assert not [r for r in investigation_store.rows if r["run_id"] == "nope"]


def test_update_rejects_a_field_the_table_cannot_hold() -> None:
    record = _run(runs.create_run(_STATE, InvestigationRecord()))
    with pytest.raises(ValueError, match="unknown investigation field"):
        _run(runs.update_run(_STATE, record.run_id, statuss=RunStatus.DONE))


def test_each_update_carries_the_previous_fields_forward() -> None:
    """A field set by one update survives the next, which never mentions it."""
    record = _run(runs.create_run(_STATE, InvestigationRecord()))
    _run(runs.update_run(_STATE, record.run_id, status=RunStatus.RUNNING))
    _run(runs.update_run(_STATE, record.run_id, job_name="jobs/1"))
    _run(
        runs.update_run(
            _STATE, record.run_id, status=RunStatus.FAILED, error="boom"
        )
    )

    stored = _run(runs.get_run(_STATE, record.run_id))
    assert stored is not None
    assert stored.status == RunStatus.FAILED
    assert stored.job_name == "jobs/1"
    assert stored.error == "boom"


# --------------------------------------------------------------------------- #
# Failing runs nothing ever came back to                                       #
# --------------------------------------------------------------------------- #


def test_a_run_left_pending_past_the_lease_is_failed(
    investigation_store,
) -> None:
    """Nothing revisits a run once its submission returned, so a job that died
    before writing `running` leaves a row that never settles."""
    record = _run(runs.create_run(_STATE, InvestigationRecord()))
    _age_run(investigation_store, record.run_id, minutes=45)

    failed = _run(runs.fail_stale_pending_runs(_STATE, lease_minutes=30))

    assert failed == [record.run_id]
    stored = _run(runs.get_run(_STATE, record.run_id))
    assert stored is not None and stored.status == RunStatus.FAILED
    assert "left pending" in (stored.error or "")
    # Terminal, so the run reads as over rather than still going.
    assert stored.finished_at is not None


def test_a_run_still_inside_the_lease_is_left_alone(
    investigation_store,
) -> None:
    """A submitted job can sit queued a while before it writes anything."""
    record = _run(runs.create_run(_STATE, InvestigationRecord()))
    _age_run(investigation_store, record.run_id, minutes=5)

    assert _run(runs.fail_stale_pending_runs(_STATE, lease_minutes=30)) == []
    stored = _run(runs.get_run(_STATE, record.run_id))
    assert stored is not None and stored.status == RunStatus.PENDING


def test_only_pending_runs_are_failed(investigation_store) -> None:
    """A run that reached `running` wrote a second snapshot, so something was
    alive to write it; one already finished is not this sweep's business."""
    running = _run(runs.create_run(_STATE, InvestigationRecord()))
    _run(runs.update_run(_STATE, running.run_id, status=RunStatus.RUNNING))
    done = _run(runs.create_run(_STATE, InvestigationRecord()))
    _run(runs.update_run(_STATE, done.run_id, status=RunStatus.DONE))
    for run_id in (running.run_id, done.run_id):
        _age_run(investigation_store, run_id, minutes=120)

    assert _run(runs.fail_stale_pending_runs(_STATE, lease_minutes=30)) == []


def test_a_zero_lease_turns_it_off(investigation_store) -> None:
    """Zero is the documented off switch; read as a lease it would instead fail
    every pending run the moment it was written."""
    record = _run(runs.create_run(_STATE, InvestigationRecord()))
    _age_run(investigation_store, record.run_id, minutes=1440)

    assert _run(runs.fail_stale_pending_runs(_STATE, lease_minutes=0)) == []
    stored = _run(runs.get_run(_STATE, record.run_id))
    assert stored is not None and stored.status == RunStatus.PENDING


def _create_scheduled_run(minutes_overdue: int) -> InvestigationRecord:
    """Creates a scheduled run whose trigger was due `minutes_overdue` ago.

    Args:
        minutes_overdue: Minutes since the due time; negative for not yet due.

    Returns:
        The stored scheduled record.
    """
    due = dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=minutes_overdue)
    return _run(
        runs.create_run(
            _STATE,
            InvestigationRecord(
                status=RunStatus.SCHEDULED, due_at=due.isoformat()
            ),
        )
    )


def test_a_scheduled_run_whose_trigger_never_fired_is_failed() -> None:
    """A deleted delayed task leaves the record waiting forever otherwise."""
    lost = _create_scheduled_run(minutes_overdue=120)
    waiting = _create_scheduled_run(minutes_overdue=-10)

    failed = _run(runs.fail_overdue_scheduled_runs(_STATE, grace_minutes=60))

    assert failed == [lost.run_id]
    stored = _run(runs.get_run(_STATE, lost.run_id))
    assert stored is not None and stored.status == RunStatus.FAILED
    assert "did not fire" in (stored.error or "")
    assert stored.finished_at is not None
    still = _run(runs.get_run(_STATE, waiting.run_id))
    assert still is not None and still.status == RunStatus.SCHEDULED


def test_the_run_whose_trigger_is_firing_is_kept_however_late() -> None:
    """A trigger that fires after retries is late, not lost."""
    late = _create_scheduled_run(minutes_overdue=120)

    failed = _run(
        runs.fail_overdue_scheduled_runs(
            _STATE, grace_minutes=60, keep=late.run_id
        )
    )

    assert failed == []
    stored = _run(runs.get_run(_STATE, late.run_id))
    assert stored is not None and stored.status == RunStatus.SCHEDULED


def _age_run(store, run_id: str, *, minutes: int) -> None:
    """Backdates every snapshot of `run_id`, as if written that long ago.

    Args:
        store: Mock investigation store containing snapshots.
        run_id: Investigation run identifier to backdate.
        minutes: Number of minutes into the past to offset `updated_at`.
    """
    when = (
        dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=minutes)
    ).isoformat()
    for row in store.rows:
        if row["run_id"] == run_id:
            row["updated_at"] = when


def test_the_scheduler_and_the_job_both_leave_their_mark() -> None:
    """Each writer re-reads before it writes, so the run accumulates both.

    The two never overlap in the lifecycle: the scheduler records the job name,
    and the durable job writes everything after that.
    """
    record = _run(runs.create_run(_STATE, InvestigationRecord()))
    # Simulates the synchronous caller recording the dispatched job name.
    _run(runs.update_run(_STATE, record.run_id, job_name="jobs/1"))
    _run(  # durable job
        runs.update_run(
            _STATE, record.run_id, status=RunStatus.DONE, summary={"who": "job"}
        )
    )

    stored = _run(runs.get_run(_STATE, record.run_id))
    assert stored is not None
    assert stored.job_name == "jobs/1"
    assert stored.summary == {"who": "job"}


def test_input_fields_round_trip_through_the_table() -> None:
    record = _run(
        runs.create_run(
            _STATE,
            InvestigationRecord(
                observed_agent_name="watched",
                trigger_type="scheduled",
                window_start="2026-01-01T00:00:00+00:00",
                window_end="2026-01-01T01:00:00+00:00",
                budget_per_metric=25,
                metrics={"multi_turn": ["task_success"], "single_turn": []},
                dry_run=True,
                idempotency_key="trigger-1",
                effective_config={"data_lookback_window": 7},
            ),
        )
    )

    stored = _run(runs.get_run(_STATE, record.run_id))
    assert stored is not None
    assert stored.observed_agent_name == "watched"
    assert stored.trigger_type == "scheduled"
    assert stored.window_start == "2026-01-01T00:00:00+00:00"
    assert stored.window_end == "2026-01-01T01:00:00+00:00"
    assert stored.budget_per_metric == 25
    assert stored.metrics == {"multi_turn": ["task_success"], "single_turn": []}
    assert stored.dry_run is True
    assert stored.idempotency_key == "trigger-1"
    assert stored.effective_config == {"data_lookback_window": 7}


# --------------------------------------------------------------------------- #
# Events                                                                       #
# --------------------------------------------------------------------------- #


def test_events_are_rows_and_come_back_in_order(investigation_store) -> None:
    record = _run(runs.create_run(_STATE, InvestigationRecord()))
    investigation_store.append_event(record.run_id, "first\n", source="init")
    investigation_store.append_event(record.run_id, "second\n", source="fetch")

    stored = _run(runs.get_run(_STATE, record.run_id))
    assert stored is not None
    assert [e.text for e in stored.events] == ["first\n", "second\n"]
    assert [e.source for e in stored.events] == ["init", "fetch"]
    assert all(e.created_at for e in stored.events)


def test_events_are_readable_before_the_run_finishes(
    investigation_store,
) -> None:
    """A node's event is visible while the run is still `running`."""
    record = _run(runs.create_run(_STATE, InvestigationRecord()))
    _run(runs.update_run(_STATE, record.run_id, status=RunStatus.RUNNING))
    investigation_store.append_event(record.run_id, "halfway\n")

    view = _run(runs.get_investigation(record.run_id, _ctx()))
    assert view["status"] == RunStatus.RUNNING
    assert view["summary"]["events"] == ["halfway\n"]


def test_stored_summary_drops_events_that_are_already_rows(
    investigation_store,
) -> None:
    """The events live in rows; the summary column must not carry them twice."""
    record = _run(runs.create_run(_STATE, InvestigationRecord()))
    investigation_store.append_event(record.run_id, "from a node\n")
    _run(
        runs.update_run(
            _STATE,
            record.run_id,
            status=RunStatus.DONE,
            summary={"metrics_passed": 1, "events": ["from a node\n"]},
        )
    )

    assert "events" not in investigation_store.rows[-1]["summary"]
    view = _run(runs.get_investigation(record.run_id, _ctx()))
    assert view["summary"]["metrics_passed"] == 1
    assert view["summary"]["events"] == ["from a node\n"]
    assert view["events"][0]["text"] == "from a node\n"


# --------------------------------------------------------------------------- #
# Listing                                                                      #
# --------------------------------------------------------------------------- #


def test_list_runs_filters_by_status() -> None:
    a = _run(runs.create_run(_STATE, InvestigationRecord()))
    _run(runs.create_run(_STATE, InvestigationRecord()))
    _run(runs.update_run(_STATE, a.run_id, status=RunStatus.DONE))

    assert len(_run(runs.list_runs(_STATE))) == 2
    done = _run(runs.list_runs(_STATE, status=RunStatus.DONE))
    assert [r.run_id for r in done] == [a.run_id]


def test_list_runs_omits_events(investigation_store) -> None:
    record = _run(runs.create_run(_STATE, InvestigationRecord()))
    investigation_store.append_event(record.run_id, "noise\n")

    (listed,) = _run(runs.list_runs(_STATE))
    assert listed.events == []
    # The summary view a list produces therefore carries no `events` key.
    assert runs.format_run(listed)["summary"] is None


def test_list_investigations_reports_a_failure_instead_of_raising(
    monkeypatch,
) -> None:
    def boom(_state):
        raise RuntimeError("table missing")

    monkeypatch.setattr(runs, "store_factory", boom)
    result = _run(runs.list_investigations(_ctx()))
    assert result["runs"] == []
    assert "table missing" in result["error"]


# --------------------------------------------------------------------------- #
# Decoding a BigQuery row into a record                                        #
# --------------------------------------------------------------------------- #


def test_a_row_is_the_records_own_fields() -> None:
    """No column table maps the two: the model's fields are the columns."""
    assert "events" not in list_snapshot_columns()
    assert set(list_snapshot_columns()) == set(
        InvestigationRecord.model_fields
    ) - {"events"}


def test_timestamps_arrive_as_datetimes_and_are_held_as_text() -> None:
    record = InvestigationRecord.model_validate(
        {
            "run_id": "r1",
            "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            "updated_at": dt.datetime(2026, 1, 1, 0, 5, tzinfo=dt.UTC),
            "finished_at": dt.datetime(2026, 1, 1, 0, 9, tzinfo=dt.UTC),
        }
    )
    assert record.created_at == "2026-01-01T00:00:00+00:00"
    assert record.updated_at == "2026-01-01T00:05:00+00:00"
    assert record.finished_at == "2026-01-01T00:09:00+00:00"


def test_a_json_column_is_decoded_whether_it_arrives_parsed_or_as_text() -> (
    None
):
    as_text = InvestigationRecord.model_validate(
        {"summary": '{"metrics_passed": 3}'}
    )
    as_object = InvestigationRecord.model_validate(
        {"summary": {"metrics_passed": 3}}
    )
    assert as_text.summary == as_object.summary == {"metrics_passed": 3}


def test_a_double_encoded_json_column_is_unwrapped() -> None:
    """Text that was itself JSON-encoded comes back quoted."""
    record = InvestigationRecord.model_validate(
        {"summary": '"{\\"metrics_passed\\": 3}"'}
    )
    assert record.summary == {"metrics_passed": 3}


def test_a_malformed_json_column_leaves_the_field_at_its_default() -> None:
    assert (
        InvestigationRecord.model_validate({"summary": "{not json"}).summary
        is None
    )


def test_an_event_row_decodes_into_an_event() -> None:
    event = InvestigationEvent.model_validate(
        {
            "run_id": "r1",
            "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            "text": "step\n",
            "source": "init",
        }
    )
    assert event.created_at == "2026-01-01T00:00:00+00:00"
    assert event.text == "step\n"
    assert event.source == "init"


# --------------------------------------------------------------------------- #
# format_run (presentation)                                                    #
# --------------------------------------------------------------------------- #


def test_format_run_computes_elapsed() -> None:
    record = InvestigationRecord(
        created_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:05+00:00",
        status=RunStatus.DONE,
    )
    formatted = runs.format_run(record)
    assert formatted["elapsed_seconds"] == pytest.approx(5.0)
    assert formatted["status"] == RunStatus.DONE


def test_format_run_shows_what_a_custom_run_was_told_to_do() -> None:
    """Verify format_run includes custom overrides in the formatted output."""
    overrides = CustomOverrides(
        selector_sql="SELECT session_id FROM t", session_review_focus="refunds"
    )
    record = InvestigationRecord(
        status=RunStatus.DONE, custom_overrides=overrides
    )

    assert runs.format_run(record)["custom_overrides"] == overrides.model_dump()


def test_format_run_leaves_an_ambient_sweep_without_overrides() -> None:
    assert runs.format_run(InvestigationRecord())["custom_overrides"] is None


# --- report console URL (derived at presentation time) ---------------- #


def test_format_run_derives_console_url_from_report_uri() -> None:
    # The stored summary carries only the raw gs:// URI; format_run derives the
    # browsable Cloud console link at read time.
    record = InvestigationRecord(
        status=RunStatus.DONE,
        summary={
            "report_gcs_uri": "gs://my-bucket/aqa-reports/test-agent-x.md"
        },
    )
    summary = runs.format_run(record)["summary"]
    assert (
        summary["report_gcs_uri"]
        == "gs://my-bucket/aqa-reports/test-agent-x.md"
    )
    assert summary["report_console_url"] == (
        "https://console.cloud.google.com/storage/browser/_details/"
        "my-bucket/aqa-reports/test-agent-x.md"
    )


def test_format_run_does_not_mutate_stored_summary() -> None:
    # Deriving the console link must not leak back into the stored record.
    stored = {"report_gcs_uri": "gs://my-bucket/aqa-reports/test-agent-x.md"}
    record = InvestigationRecord(status=RunStatus.DONE, summary=stored)
    runs.format_run(record)
    assert "report_console_url" not in stored
    assert record.summary is not None
    assert "report_console_url" not in record.summary


def test_format_run_summary_without_report_uri_is_unchanged() -> None:
    record = InvestigationRecord(
        status=RunStatus.DONE, summary={"report_summary": "- x\n"}
    )
    summary = runs.format_run(record)["summary"]
    assert "report_console_url" not in summary


def test_format_run_handles_missing_summary() -> None:
    assert (
        runs.format_run(InvestigationRecord(status=RunStatus.DONE))["summary"]
        is None
    )


def test_format_run_carries_the_due_time_of_a_scheduled_run() -> None:
    record = InvestigationRecord(
        status=RunStatus.SCHEDULED, due_at="2026-01-01T00:15:00+00:00"
    )

    view = runs.format_run(record)

    assert view["status"] == "scheduled"
    assert view["due_at"] == "2026-01-01T00:15:00+00:00"
    assert runs.format_run(InvestigationRecord())["due_at"] is None


def test_format_run_carries_idempotency_key() -> None:
    assert (
        runs.format_run(InvestigationRecord(idempotency_key="k-1"))[
            "idempotency_key"
        ]
        == "k-1"
    )
    assert runs.format_run(InvestigationRecord())["idempotency_key"] is None


# --- list-friendly outcome fields (lifted from the stored summary) ---- #


def test_format_run_lifts_outcome_fields_from_summary() -> None:
    record = InvestigationRecord(
        status=RunStatus.DONE,
        summary={
            "rca_status": "executed",
            "metrics_passed": 4,
            "metrics_failed": 1,
            "metrics_errored": 2,
        },
    )
    formatted = runs.format_run(record)
    assert formatted["rca_status"] == "executed"
    assert formatted["metrics_passed"] == 4
    assert formatted["metrics_failed"] == 1
    assert formatted["metrics_errored"] == 2


def test_format_run_outcome_fields_none_without_summary() -> None:
    # A pending/running run (or a legacy record) has no summary, so the outcome
    # fields surface as None and the dashboard renders a dash for them.
    formatted = runs.format_run(InvestigationRecord(status=RunStatus.PENDING))
    assert formatted["rca_status"] is None
    assert formatted["metrics_passed"] is None
    assert formatted["metrics_failed"] is None
    assert formatted["metrics_errored"] is None


def test_gcs_console_url_matches_console_details_form() -> None:
    assert runs._build_gcs_console_url(
        "gs://my-project-aqua-jobs/aqa-jobs/54344ef6.json"
    ) == (
        "https://console.cloud.google.com/storage/browser/_details/"
        "my-project-aqua-jobs/aqa-jobs/54344ef6.json"
    )


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "not-a-uri",
        "s3://bucket/key",
        "gs://",
        "https://storage.googleapis.com/b/o",
    ],
)
def test_gcs_console_url_rejects_non_gcs(uri: str) -> None:
    assert runs._build_gcs_console_url(uri) == ""


def _run(coro):
    """Drives a coroutine to completion for test assertions.

    Args:
        coro: Coroutine to execute.

    Returns:
        Result value from the completed coroutine.
    """
    return asyncio.run(coro)


def _ctx():
    """Builds a tool context stand-in providing access to state for testing.

    Returns:
        ToolContext initialized with module state.
    """
    from tests.conftest import ToolContext

    return ToolContext(_STATE)
