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

"""Tests for the investigation registry (data layer)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from unittest import mock

import pytest
from ambient_quality_agent.core.investigation import model as runs
from ambient_quality_agent.core.investigation.model import (
    Investigation,
    InvestigationStatus,
)
from ambient_quality_agent.tools.investigations.models import (
    STAGE_EVENT_SOURCE,
    CustomOverrides,
    InvestigationCounters,
    InvestigationEvent,
    StageProgress,
    field_to_column,
    list_snapshot_columns,
)
from pydantic import ValidationError

_STATE: dict = {}
"""The registry reads state only to resolve the store, which the autouse
`investigation_store` fixture pins, so every call here can share one."""


# --------------------------------------------------------------------------- #
# Snapshots in, one record out                                                 #
# --------------------------------------------------------------------------- #


def test_create_investigation_writes_a_single_snapshot(
    investigation_store,
) -> None:
    record = _run(
        runs.create_investigation(_STATE, Investigation(observed_agent_key="a"))
    )

    (row,) = investigation_store.rows
    assert row["run_id"] == record.investigation_id
    assert row["observed_agent_name"] == "a"
    assert row["status"] == InvestigationStatus.PENDING.value
    # Nothing has produced an output yet, so those columns stay out of the row.
    assert "summary" not in row and "finished_at" not in row


def test_get_stored_investigation_roundtrip_and_missing() -> None:
    record = _run(runs.create_investigation(_STATE, Investigation()))
    fetched = _run(
        runs.get_stored_investigation(_STATE, record.investigation_id)
    )

    assert (
        fetched is not None
        and fetched.investigation_id == record.investigation_id
    )
    assert _run(runs.get_stored_investigation(_STATE, "missing")) is None


def test_update_appends_a_whole_new_snapshot(investigation_store) -> None:
    record = _run(
        runs.create_investigation(_STATE, Investigation(observed_agent_key="a"))
    )

    updated = _run(
        runs.update_investigation(
            _STATE,
            record.investigation_id,
            status=InvestigationStatus.DONE,
            summary={"k": "v"},
        )
    )

    assert len(investigation_store.rows) == 2
    latest = investigation_store.rows[-1]
    # The whole investigation, not just the change: the inputs are carried forward.
    assert latest["observed_agent_name"] == "a"
    assert latest["status"] == InvestigationStatus.DONE.value
    assert updated is not None and updated.status == InvestigationStatus.DONE
    # ...so the read is just the newest snapshot.
    stored = _run(
        runs.get_stored_investigation(_STATE, record.investigation_id)
    )
    assert stored is not None
    assert stored.observed_agent_key == "a"
    assert stored.status == InvestigationStatus.DONE
    assert stored.summary == {"k": "v"}


def test_update_validates_the_values_it_is_given(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """`job_execution` passes the graph's counters through as a plain dict.
    Assigned rather than validated they stay one in a field typed
    `InvestigationCounters`, and a reader gets whichever shape it is handed."""
    record = _run(runs.create_investigation(_STATE, Investigation()))

    updated = _run(
        runs.update_investigation(
            _STATE,
            record.investigation_id,
            counters={"trajectories_scanned": 3, "insights_created": 1},
        )
    )

    assert updated is not None
    assert isinstance(updated.counters, InvestigationCounters)
    assert updated.counters.trajectories_scanned == 3
    stored = _run(
        runs.get_stored_investigation(_STATE, record.investigation_id)
    )
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
    record = _run(runs.create_investigation(_STATE, Investigation()))

    with pytest.raises(ValidationError):
        _run(
            runs.update_investigation(
                _STATE,
                record.investigation_id,
                counters={"trajectories_scanned": "lots"},
            )
        )


def test_update_stamps_finished_on_terminal_status() -> None:
    record = _run(runs.create_investigation(_STATE, Investigation()))
    _run(
        runs.update_investigation(
            _STATE, record.investigation_id, status=InvestigationStatus.DONE
        )
    )

    stored = _run(
        runs.get_stored_investigation(_STATE, record.investigation_id)
    )
    assert stored is not None and stored.finished_at is not None


def test_update_missing_run_writes_nothing(investigation_store) -> None:
    assert (
        _run(
            runs.update_investigation(
                _STATE, "nope", status=InvestigationStatus.DONE
            )
        )
        is None
    )
    assert not [r for r in investigation_store.rows if r["run_id"] == "nope"]


def test_update_rejects_a_field_the_table_cannot_hold() -> None:
    record = _run(runs.create_investigation(_STATE, Investigation()))
    with pytest.raises(ValueError, match="unknown investigation field"):
        _run(
            runs.update_investigation(
                _STATE,
                record.investigation_id,
                statuss=InvestigationStatus.DONE,
            )
        )


def test_each_update_carries_the_previous_fields_forward() -> None:
    """A field set by one update survives the next, which never mentions it."""
    record = _run(runs.create_investigation(_STATE, Investigation()))
    _run(
        runs.update_investigation(
            _STATE, record.investigation_id, status=InvestigationStatus.RUNNING
        )
    )
    _run(
        runs.update_investigation(
            _STATE, record.investigation_id, job_name="jobs/1"
        )
    )
    _run(
        runs.update_investigation(
            _STATE,
            record.investigation_id,
            status=InvestigationStatus.FAILED,
            error="boom",
        )
    )

    stored = _run(
        runs.get_stored_investigation(_STATE, record.investigation_id)
    )
    assert stored is not None
    assert stored.status == InvestigationStatus.FAILED
    assert stored.job_name == "jobs/1"
    assert stored.error == "boom"


# --------------------------------------------------------------------------- #
# Failing runs nothing ever came back to                                       #
# --------------------------------------------------------------------------- #


def test_a_run_left_pending_past_the_lease_is_failed(
    investigation_store,
) -> None:
    """Nothing revisits an investigation once its submission returned, so a job that died
    before writing `running` leaves a row that never settles."""
    record = _run(runs.create_investigation(_STATE, Investigation()))
    _age_run(investigation_store, record.investigation_id, minutes=45)

    failed = _run(
        runs.fail_stale_pending_investigations(_STATE, lease_minutes=30)
    )

    assert failed == [record.investigation_id]
    stored = _run(
        runs.get_stored_investigation(_STATE, record.investigation_id)
    )
    assert stored is not None and stored.status == InvestigationStatus.FAILED
    assert "left pending" in (stored.error or "")
    # Terminal, so the investigation reads as over rather than still going.
    assert stored.finished_at is not None


def test_a_run_still_inside_the_lease_is_left_alone(
    investigation_store,
) -> None:
    """A submitted job can sit queued a while before it writes anything."""
    record = _run(runs.create_investigation(_STATE, Investigation()))
    _age_run(investigation_store, record.investigation_id, minutes=5)

    assert (
        _run(runs.fail_stale_pending_investigations(_STATE, lease_minutes=30))
        == []
    )
    stored = _run(
        runs.get_stored_investigation(_STATE, record.investigation_id)
    )
    assert stored is not None and stored.status == InvestigationStatus.PENDING


def test_only_pending_runs_are_failed(investigation_store) -> None:
    """An investigation that reached `running` wrote a second snapshot, so something was
    alive to write it; one already finished is not this sweep's business."""
    running = _run(runs.create_investigation(_STATE, Investigation()))
    _run(
        runs.update_investigation(
            _STATE, running.investigation_id, status=InvestigationStatus.RUNNING
        )
    )
    done = _run(runs.create_investigation(_STATE, Investigation()))
    _run(
        runs.update_investigation(
            _STATE, done.investigation_id, status=InvestigationStatus.DONE
        )
    )
    for investigation_id in (running.investigation_id, done.investigation_id):
        _age_run(investigation_store, investigation_id, minutes=120)

    assert (
        _run(runs.fail_stale_pending_investigations(_STATE, lease_minutes=30))
        == []
    )


def test_a_zero_lease_turns_it_off(investigation_store) -> None:
    """Zero is the documented off switch; read as a lease it would instead fail
    every pending run the moment it was written."""
    record = _run(runs.create_investigation(_STATE, Investigation()))
    _age_run(investigation_store, record.investigation_id, minutes=1440)

    assert (
        _run(runs.fail_stale_pending_investigations(_STATE, lease_minutes=0))
        == []
    )
    stored = _run(
        runs.get_stored_investigation(_STATE, record.investigation_id)
    )
    assert stored is not None and stored.status == InvestigationStatus.PENDING


def _create_scheduled_run(minutes_overdue: int) -> Investigation:
    """Creates a scheduled investigation whose trigger was due `minutes_overdue` ago.

    Args:
        minutes_overdue: Minutes since the due time; negative for not yet due.

    Returns:
        The stored scheduled record.
    """
    due = dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=minutes_overdue)
    return _run(
        runs.create_investigation(
            _STATE,
            Investigation(
                status=InvestigationStatus.SCHEDULED, due_at=due.isoformat()
            ),
        )
    )


def test_a_scheduled_run_whose_trigger_never_fired_is_failed() -> None:
    """A deleted delayed task leaves the record waiting forever otherwise."""
    lost = _create_scheduled_run(minutes_overdue=120)
    waiting = _create_scheduled_run(minutes_overdue=-10)

    failed = _run(
        runs.fail_overdue_scheduled_investigations(_STATE, grace_minutes=60)
    )

    assert failed == [lost.investigation_id]
    stored = _run(runs.get_stored_investigation(_STATE, lost.investigation_id))
    assert stored is not None and stored.status == InvestigationStatus.FAILED
    assert "did not fire" in (stored.error or "")
    assert stored.finished_at is not None
    still = _run(
        runs.get_stored_investigation(_STATE, waiting.investigation_id)
    )
    assert still is not None and still.status == InvestigationStatus.SCHEDULED


def test_the_run_whose_trigger_is_firing_is_kept_however_late() -> None:
    """A trigger that fires after retries is late, not lost."""
    late = _create_scheduled_run(minutes_overdue=120)

    failed = _run(
        runs.fail_overdue_scheduled_investigations(
            _STATE, grace_minutes=60, keep=late.investigation_id
        )
    )

    assert failed == []
    stored = _run(runs.get_stored_investigation(_STATE, late.investigation_id))
    assert stored is not None and stored.status == InvestigationStatus.SCHEDULED


def _age_run(store, investigation_id: str, *, minutes: int) -> None:
    """Backdates every snapshot of `investigation_id`, as if written that long ago.

    Args:
        store: Mock investigation store containing snapshots.
        investigation_id: Investigation identifier to backdate.
        minutes: Number of minutes into the past to offset `updated_at`.
    """
    when = (
        dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=minutes)
    ).isoformat()
    for row in store.rows:
        if row["run_id"] == investigation_id:
            row["updated_at"] = when


def test_the_scheduler_and_the_job_both_leave_their_mark() -> None:
    """Each writer re-reads before it writes, so the investigation accumulates both.

    The two never overlap in the lifecycle: the scheduler records the job name,
    and the durable job writes everything after that.
    """
    record = _run(runs.create_investigation(_STATE, Investigation()))
    # Simulates the synchronous caller recording the dispatched job name.
    _run(
        runs.update_investigation(
            _STATE, record.investigation_id, job_name="jobs/1"
        )
    )
    _run(  # durable job
        runs.update_investigation(
            _STATE,
            record.investigation_id,
            status=InvestigationStatus.DONE,
            summary={"who": "job"},
        )
    )

    stored = _run(
        runs.get_stored_investigation(_STATE, record.investigation_id)
    )
    assert stored is not None
    assert stored.job_name == "jobs/1"
    assert stored.summary == {"who": "job"}


def test_input_fields_round_trip_through_the_table() -> None:
    record = _run(
        runs.create_investigation(
            _STATE,
            Investigation(
                observed_agent_key="watched",
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

    stored = _run(
        runs.get_stored_investigation(_STATE, record.investigation_id)
    )
    assert stored is not None
    assert stored.observed_agent_key == "watched"
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
    record = _run(runs.create_investigation(_STATE, Investigation()))
    investigation_store.append_event(
        record.investigation_id, "first\n", source="init"
    )
    investigation_store.append_event(
        record.investigation_id, "second\n", source="fetch"
    )

    stored = _run(
        runs.get_stored_investigation(_STATE, record.investigation_id)
    )
    assert stored is not None
    assert [e.text for e in stored.events] == ["first\n", "second\n"]
    assert [e.source for e in stored.events] == ["init", "fetch"]
    assert all(e.created_at for e in stored.events)


def test_events_are_readable_before_the_run_finishes(
    investigation_store,
) -> None:
    """A node's event is visible while the investigation is still `running`."""
    record = _run(runs.create_investigation(_STATE, Investigation()))
    _run(
        runs.update_investigation(
            _STATE, record.investigation_id, status=InvestigationStatus.RUNNING
        )
    )
    investigation_store.append_event(record.investigation_id, "halfway\n")

    view = _run(runs.get_investigation(record.investigation_id, _ctx()))
    assert view["status"] == InvestigationStatus.RUNNING
    assert view["summary"]["events"] == ["halfway\n"]


def test_the_events_and_summary_leave_out_stage_progress(
    investigation_store,
) -> None:
    """Stage rows say how far a running sweep got, page by page. The events
    and the summary the chat reads hold what the nodes reported, which a
    page-by-page count would bury; `progress` carries the newest."""
    record = _run(runs.create_investigation(_STATE, Investigation()))
    investigation_store.append_event(
        record.investigation_id, "started\n", source="init"
    )
    investigation_store.append_event(
        record.investigation_id,
        _build_progress_row(stage="Session review", evaluated=2, total=7),
        source=STAGE_EVENT_SOURCE,
    )

    view = _run(runs.get_investigation(record.investigation_id, _ctx()))
    assert view["summary"]["events"] == ["started\n"]
    assert [e["source"] for e in view["events"]] == ["init"]


def test_stored_summary_drops_events_that_are_already_rows(
    investigation_store,
) -> None:
    """The events live in rows; the summary column must not carry them twice."""
    record = _run(runs.create_investigation(_STATE, Investigation()))
    investigation_store.append_event(record.investigation_id, "from a node\n")
    _run(
        runs.update_investigation(
            _STATE,
            record.investigation_id,
            status=InvestigationStatus.DONE,
            summary={"metrics_passed": 1, "events": ["from a node\n"]},
        )
    )

    assert "events" not in investigation_store.rows[-1]["summary"]
    view = _run(runs.get_investigation(record.investigation_id, _ctx()))
    assert view["summary"]["metrics_passed"] == 1
    assert view["summary"]["events"] == ["from a node\n"]
    assert view["events"][0]["text"] == "from a node\n"


# --------------------------------------------------------------------------- #
# Listing                                                                      #
# --------------------------------------------------------------------------- #


def test_list_stored_investigations_filters_by_status() -> None:
    a = _run(runs.create_investigation(_STATE, Investigation()))
    _run(runs.create_investigation(_STATE, Investigation()))
    _run(
        runs.update_investigation(
            _STATE, a.investigation_id, status=InvestigationStatus.DONE
        )
    )

    assert len(_run(runs.list_stored_investigations(_STATE))) == 2
    done = _run(
        runs.list_stored_investigations(_STATE, status=InvestigationStatus.DONE)
    )
    assert [r.investigation_id for r in done] == [a.investigation_id]


def test_list_stored_investigations_omits_events(investigation_store) -> None:
    record = _run(runs.create_investigation(_STATE, Investigation()))
    investigation_store.append_event(record.investigation_id, "noise\n")

    (listed,) = _run(runs.list_stored_investigations(_STATE))
    assert listed.events == []
    # The summary view a list produces therefore carries no `events` key.
    assert runs.format_investigation(listed)["summary"] is None


def test_list_investigations_reports_a_failure_instead_of_raising(
    monkeypatch,
) -> None:
    def boom(_state):
        raise RuntimeError("table missing")

    monkeypatch.setattr(runs, "store_factory", boom)
    result = _run(runs.list_investigations(_ctx()))
    assert result["investigations"] == []
    assert "table missing" in result["error"]


def test_the_list_says_how_far_each_running_investigation_got(
    investigation_store,
) -> None:
    """The list reads no events, except the newest stage row of each
    investigation still going: the one line that says how far it got."""
    running = _run(runs.create_investigation(_STATE, Investigation()))
    done = _run(runs.create_investigation(_STATE, Investigation()))
    for record in (running, done):
        for evaluated in (0, 2):
            investigation_store.append_event(
                record.investigation_id,
                _build_progress_row(
                    stage="Session review", evaluated=evaluated, total=7
                ),
                source=STAGE_EVENT_SOURCE,
            )
    _run(
        runs.update_investigation(
            _STATE, running.investigation_id, status=InvestigationStatus.RUNNING
        )
    )
    _run(
        runs.update_investigation(
            _STATE, done.investigation_id, status=InvestigationStatus.DONE
        )
    )

    listed = {
        item["investigation_id"]: item
        for item in _run(runs.list_investigations(_ctx()))["investigations"]
    }

    newest = investigation_store.event_rows[1]
    assert listed[running.investigation_id]["progress"] == {
        "stage": "Session review",
        "evaluated": 2,
        "total": 7,
        "updated_at": newest["created_at"],
    }
    assert listed[running.investigation_id]["events"] == []
    # A settled investigation's last stage row describes a moment long gone.
    assert listed[done.investigation_id]["progress"] is None


def test_a_failed_stage_read_still_lists_the_investigations(
    investigation_store, monkeypatch
) -> None:
    """The stage is a detail of a row; losing it must not lose the table."""
    record = _run(runs.create_investigation(_STATE, Investigation()))

    def boom(_investigation_ids):
        raise RuntimeError("events table missing")

    monkeypatch.setattr(investigation_store, "list_latest_stage_events", boom)
    result = _run(runs.list_investigations(_ctx()))

    assert "error" not in result
    (item,) = result["investigations"]
    assert item["investigation_id"] == record.investigation_id
    assert item["progress"] is None


def test_the_detail_says_how_far_a_running_investigation_got(
    investigation_store,
) -> None:
    record = _run(runs.create_investigation(_STATE, Investigation()))
    _run(
        runs.update_investigation(
            _STATE, record.investigation_id, status=InvestigationStatus.RUNNING
        )
    )
    investigation_store.append_event(
        record.investigation_id,
        _build_progress_row(stage="Clustering", findings=3),
        source=STAGE_EVENT_SOURCE,
    )
    investigation_store.append_event(
        record.investigation_id, "review finished\n", source="review"
    )

    view = _run(runs.get_investigation(record.investigation_id, _ctx()))
    assert view["progress"]["stage"] == "Clustering"
    assert view["progress"]["findings"] == 3

    _run(
        runs.update_investigation(
            _STATE, record.investigation_id, status=InvestigationStatus.DONE
        )
    )
    view = _run(runs.get_investigation(record.investigation_id, _ctx()))
    assert view["progress"] is None


def test_a_stage_row_that_holds_no_progress_draws_none(
    investigation_store, caplog
) -> None:
    """The events table holds whatever text was written. A stage row that is
    not a progress record says nothing the views can draw, and must not fail
    them."""
    record = _run(runs.create_investigation(_STATE, Investigation()))
    investigation_store.append_event(
        record.investigation_id,
        "Session review: 2/7",
        source=STAGE_EVENT_SOURCE,
    )

    with caplog.at_level(logging.WARNING):
        view = _run(runs.get_investigation(record.investigation_id, _ctx()))
    assert view["progress"] is None
    assert (
        f"stage event of {record.investigation_id} that is not a progress "
        "record" in caplog.text
    )


def test_a_malformed_stage_row_is_logged_under_its_investigation(
    caplog,
) -> None:
    """The copies of events nested in a record carry no investigation id, so
    the warning names the record's."""
    record = Investigation(
        investigation_id="investigation-7",
        status=InvestigationStatus.RUNNING,
        events=[InvestigationEvent(text="2/7", source=STAGE_EVENT_SOURCE)],
    )

    with caplog.at_level(logging.WARNING):
        view = runs.format_investigation(record)

    assert view["progress"] is None
    assert "stage event of investigation-7 that is not" in caplog.text


def test_listing_settled_investigations_reads_no_stage(
    investigation_store, monkeypatch
) -> None:
    """Only a pending or running investigation shows its stage, so a list
    with none in flight costs no events-table query."""
    record = _run(runs.create_investigation(_STATE, Investigation()))
    _run(
        runs.update_investigation(
            _STATE, record.investigation_id, status=InvestigationStatus.DONE
        )
    )

    def boom(_investigation_ids):
        raise AssertionError("read the stages of a list with none in flight")

    monkeypatch.setattr(investigation_store, "list_latest_stage_events", boom)
    with mock.patch.object(runs.logger, "warning") as warning:
        result = _run(runs.list_investigations(_ctx()))

    warning.assert_not_called()
    assert result["investigations"][0]["progress"] is None


# --------------------------------------------------------------------------- #
# Decoding a BigQuery row into a record                                        #
# --------------------------------------------------------------------------- #


def test_a_row_is_the_records_own_fields() -> None:
    """The model's fields are the columns, under the names `field_to_column` gives."""
    assert "events" not in list_snapshot_columns()
    assert set(list_snapshot_columns()) == {
        field_to_column(field) for field in Investigation.model_fields
    } - {"events"}


def test_timestamps_arrive_as_datetimes_and_are_held_as_text() -> None:
    record = Investigation.model_validate(
        {
            "investigation_id": "r1",
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
    as_text = Investigation.model_validate({"summary": '{"metrics_passed": 3}'})
    as_object = Investigation.model_validate({"summary": {"metrics_passed": 3}})
    assert as_text.summary == as_object.summary == {"metrics_passed": 3}


def test_a_double_encoded_json_column_is_unwrapped() -> None:
    """Text that was itself JSON-encoded comes back quoted."""
    record = Investigation.model_validate(
        {"summary": '"{\\"metrics_passed\\": 3}"'}
    )
    assert record.summary == {"metrics_passed": 3}


def test_a_malformed_json_column_leaves_the_field_at_its_default() -> None:
    assert (
        Investigation.model_validate({"summary": "{not json"}).summary is None
    )


def test_an_event_row_decodes_into_an_event() -> None:
    event = InvestigationEvent.model_validate(
        {
            "investigation_id": "r1",
            "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            "text": "step\n",
            "source": "init",
        }
    )
    assert event.created_at == "2026-01-01T00:00:00+00:00"
    assert event.text == "step\n"
    assert event.source == "init"


# --------------------------------------------------------------------------- #
# format_investigation (presentation)                                                    #
# --------------------------------------------------------------------------- #


def test_format_investigation_computes_elapsed() -> None:
    record = Investigation(
        created_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:05+00:00",
        status=InvestigationStatus.DONE,
    )
    formatted = runs.format_investigation(record)
    assert formatted["elapsed_seconds"] == pytest.approx(5.0)
    assert formatted["status"] == InvestigationStatus.DONE


def test_format_investigation_shows_what_a_custom_run_was_told_to_do() -> None:
    """Verify format_investigation includes custom overrides in the formatted output."""
    overrides = CustomOverrides(
        selector_sql="SELECT session_id FROM t", review_focus="refunds"
    )
    record = Investigation(
        status=InvestigationStatus.DONE, custom_overrides=overrides
    )

    assert (
        runs.format_investigation(record)["custom_overrides"]
        == overrides.model_dump()
    )


def test_format_investigation_leaves_an_ambient_sweep_without_overrides() -> (
    None
):
    assert (
        runs.format_investigation(Investigation())["custom_overrides"] is None
    )


# --- report console URL (derived at presentation time) ---------------- #


def test_format_investigation_derives_console_url_from_report_uri() -> None:
    # The stored summary carries only the raw gs:// URI; format_investigation derives the
    # browsable Cloud console link at read time.
    record = Investigation(
        status=InvestigationStatus.DONE,
        summary={
            "report_gcs_uri": "gs://my-bucket/aqa-reports/test-agent-x.md"
        },
    )
    summary = runs.format_investigation(record)["summary"]
    assert (
        summary["report_gcs_uri"]
        == "gs://my-bucket/aqa-reports/test-agent-x.md"
    )
    assert summary["report_console_url"] == (
        "https://console.cloud.google.com/storage/browser/_details/"
        "my-bucket/aqa-reports/test-agent-x.md"
    )


def test_format_investigation_does_not_mutate_stored_summary() -> None:
    # Deriving the console link must not leak back into the stored record.
    stored = {"report_gcs_uri": "gs://my-bucket/aqa-reports/test-agent-x.md"}
    record = Investigation(status=InvestigationStatus.DONE, summary=stored)
    runs.format_investigation(record)
    assert "report_console_url" not in stored
    assert record.summary is not None
    assert "report_console_url" not in record.summary


def test_format_investigation_summary_without_report_uri_is_unchanged() -> None:
    record = Investigation(
        status=InvestigationStatus.DONE, summary={"report_summary": "- x\n"}
    )
    summary = runs.format_investigation(record)["summary"]
    assert "report_console_url" not in summary


def test_format_investigation_handles_missing_summary() -> None:
    assert (
        runs.format_investigation(
            Investigation(status=InvestigationStatus.DONE)
        )["summary"]
        is None
    )


def test_format_investigation_carries_the_due_time_of_a_scheduled_run() -> None:
    record = Investigation(
        status=InvestigationStatus.SCHEDULED, due_at="2026-01-01T00:15:00+00:00"
    )

    view = runs.format_investigation(record)

    assert view["status"] == "scheduled"
    assert view["due_at"] == "2026-01-01T00:15:00+00:00"
    assert runs.format_investigation(Investigation())["due_at"] is None


def test_format_investigation_carries_idempotency_key() -> None:
    assert (
        runs.format_investigation(Investigation(idempotency_key="k-1"))[
            "idempotency_key"
        ]
        == "k-1"
    )
    assert runs.format_investigation(Investigation())["idempotency_key"] is None


# --- list-friendly outcome fields (lifted from the stored summary) ---- #


def test_format_investigation_lifts_outcome_fields_from_summary() -> None:
    record = Investigation(
        status=InvestigationStatus.DONE,
        summary={
            "rca_status": "executed",
            "metrics_passed": 4,
            "metrics_failed": 1,
            "metrics_errored": 2,
        },
    )
    formatted = runs.format_investigation(record)
    assert formatted["rca_status"] == "executed"
    assert formatted["metrics_passed"] == 4
    assert formatted["metrics_failed"] == 1
    assert formatted["metrics_errored"] == 2


def test_format_investigation_outcome_fields_none_without_summary() -> None:
    # A pending/running run (or a legacy record) has no summary, so the outcome
    # fields surface as None and the dashboard renders a dash for them.
    formatted = runs.format_investigation(
        Investigation(status=InvestigationStatus.PENDING)
    )
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


def _build_progress_row(**fields) -> str:
    """The text of a stage row, as the pipeline writes it.

    Args:
        **fields: `StageProgress` fields.

    Returns:
        The progress record as JSON.
    """
    return StageProgress(**fields).model_dump_json(exclude_none=True)


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
