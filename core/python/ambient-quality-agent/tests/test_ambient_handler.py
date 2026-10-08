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

"""Tests for the ambient trigger handler: `parse_ambient_trigger` (boundary
validation) and `dispatch_ambient_trigger` (dispatch of scheduled/update/task_fire
triggers)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from unittest import mock

import pytest
from ambient_quality_agent.core.ambient import handler
from ambient_quality_agent.core.investigation import model
from ambient_quality_agent.tools import cloudtasks
from ambient_quality_agent.tools.observed_agent_config import effective_config

from .conftest import CallbackContext, get_run, make_config

_RESOURCE = "projects/p/locations/l/reasoningEngines/1"


def _message(**payload: object) -> dict[str, object]:
    """Constructs a trigger payload dictionary shaped as passed by route handlers.

    Args:
        **payload: Key-value pairs for the trigger payload.

    Returns:
        A dictionary representing the trigger payload.
    """
    return payload


# --- validate_trigger_payload ---------------------------------------------- #


def test_parse_scheduled() -> None:
    # A scheduled body is static: only type + resource_name, with cadence defined
    # in config. It keys on its cadence-aligned window and parses with no dedup_id
    # to avoid over-tightening the parser.
    trigger = handler.validate_trigger_payload(
        _message(type="scheduled", resource_name=_RESOURCE)
    )
    assert trigger == handler.AmbientTrigger(
        type="scheduled", resource_name=_RESOURCE, dedup_id=None
    )


def test_parse_update() -> None:
    trigger = handler.validate_trigger_payload(
        _message(type="update", resource_name=_RESOURCE, dedup_id="op-1")
    )
    assert trigger == handler.AmbientTrigger(
        type="update", resource_name=_RESOURCE, dedup_id="op-1"
    )


def test_parse_task_fire_carries_dedup_id() -> None:
    trigger = handler.validate_trigger_payload(
        _message(type="task_fire", resource_name=_RESOURCE, dedup_id="d3")
    )
    assert trigger == handler.AmbientTrigger(
        type="task_fire", resource_name=_RESOURCE, dedup_id="d3"
    )


def test_parse_ignores_stale_cadence_and_delay_fields() -> None:
    # Verify that extra or legacy cadence_seconds and delay_seconds fields are ignored.
    trigger = handler.validate_trigger_payload(
        _message(
            type="scheduled",
            resource_name=_RESOURCE,
            cadence_seconds=1800,
            delay_seconds=60,
        )
    )
    assert trigger == handler.AmbientTrigger(
        type="scheduled", resource_name=_RESOURCE, dedup_id=None
    )


def test_parse_non_object_payload_raises() -> None:
    # A JSON scalar or array decodes fine but is not a trigger object.
    with pytest.raises(handler.AmbientTriggerParseError):
        handler.validate_trigger_payload(5)


def test_parse_unknown_type_raises() -> None:
    with pytest.raises(handler.AmbientTriggerParseError):
        handler.validate_trigger_payload(
            _message(type="bogus", resource_name=_RESOURCE, dedup_id="d1")
        )


def test_parse_scheduled_without_resource_name_is_accepted() -> None:
    # A deployment with `observed_engine_id` null watches no engine,
    # so Cloud Scheduler has no name to send and omits the key. Nothing reads it,
    # and rejecting the tick would take the schedule down with the update path.
    trigger = handler.validate_trigger_payload(
        _message(type="scheduled", dedup_id="d1")
    )
    assert trigger is not None
    assert trigger.resource_name is None


def test_parse_update_without_resource_name_raises() -> None:
    # The audit sink is the only source of an `update` and always supplies a
    # resourceName, so one arriving without it is malformed, not unwatched.
    with pytest.raises(handler.AmbientTriggerParseError):
        handler.validate_trigger_payload(
            _message(type="update", dedup_id="op-1")
        )


def test_parse_task_fire_without_resource_name_raises() -> None:
    # `task_fire` is that same `update` fired back, so it inherits the guarantee.
    with pytest.raises(handler.AmbientTriggerParseError):
        handler.validate_trigger_payload(
            _message(type="task_fire", dedup_id="d3")
        )


def test_parse_update_without_dedup_id_raises() -> None:
    # update/task_fire have no cadence window to key on, so a missing dedup_id
    # means nothing dedups -- no Cloud Tasks name, no idempotency marker -- and
    # every redelivery buys another paid investigation.
    with pytest.raises(handler.AmbientTriggerParseError):
        handler.validate_trigger_payload(
            _message(type="update", resource_name=_RESOURCE)
        )


def test_parse_task_fire_without_dedup_id_raises() -> None:
    # The second hop needs it just as much: at cadence 0 a task_fire derives no
    # window key either, so an unkeyed one runs unmarked on every redelivery.
    # A blank id coerces to None and trips the same check.
    with pytest.raises(handler.AmbientTriggerParseError):
        handler.validate_trigger_payload(
            _message(type="task_fire", resource_name=_RESOURCE, dedup_id="   ")
        )


def test_parse_blank_resource_name_coerced_to_none() -> None:
    # Same coercion as `dedup_id`: blank reads as absent, which on a `scheduled`
    # tick is allowed. On an `update` it trips the required check above.
    trigger = handler.validate_trigger_payload(
        _message(type="scheduled", resource_name="   ")
    )
    assert trigger is not None
    assert trigger.resource_name is None


def test_parse_blank_dedup_id_coerced_to_none() -> None:
    # Where a dedup_id is not required, a blank one is coerced to None (the
    # coercion that feeds the update/task_fire check), not a rejection.
    trigger = handler.validate_trigger_payload(
        _message(type="scheduled", resource_name=_RESOURCE, dedup_id="   ")
    )
    assert trigger is not None
    assert trigger.dedup_id is None


# --- dispatch_ambient_trigger ------------------------------------------------ #


@pytest.fixture
def mock_schedule(monkeypatch: pytest.MonkeyPatch) -> mock.AsyncMock:
    """Patch the shared `_launch_investigation` seam the handler calls."""
    fake = mock.AsyncMock(return_value={"run_id": "r1", "status": "pending"})
    monkeypatch.setattr(handler, "_launch_investigation", fake)
    return fake


@pytest.fixture
def mock_enqueue(monkeypatch: pytest.MonkeyPatch) -> mock.Mock:
    """Patch the Cloud Tasks seam the update path calls."""
    fake = mock.Mock()
    monkeypatch.setattr(cloudtasks, "enqueue_http_task", fake)
    return fake


def _use_config(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> None:
    """Pins the effective config the handler loads.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        **overrides: Configuration field overrides.
    """
    cfg = make_config(**overrides)
    monkeypatch.setattr(effective_config, "load", lambda state: cfg)


def test_handle_scheduled_schedules_without_a_key(
    monkeypatch: pytest.MonkeyPatch, mock_schedule: mock.AsyncMock
) -> None:
    _use_config(monkeypatch, observed_agent_name="agent-x")
    trigger = handler.AmbientTrigger(type="scheduled", resource_name=_RESOURCE)

    note = asyncio.run(
        handler.dispatch_ambient_trigger(CallbackContext(state={}), trigger)
    )

    mock_schedule.assert_awaited_once()
    kwargs = mock_schedule.call_args.kwargs
    assert kwargs["idempotency_key"] is None
    assert kwargs["ambient"] is True
    # A tick advances no scheduled record: none was made for it.
    assert kwargs["scheduled_run_id"] is None
    assert "r1" in note and "pending" in note
    assert "agent-x" in note


@pytest.mark.parametrize("trigger_type", ["scheduled", "update"])
def test_handle_with_no_agent_attached_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
    mock_schedule: mock.AsyncMock,
    mock_enqueue: mock.Mock,
    trigger_type: str,
) -> None:
    """A tick before anything is attached is a normal state, not a failure."""
    _use_config(monkeypatch, observed_agent_name="")
    trigger = handler.AmbientTrigger(
        type=trigger_type, resource_name=_RESOURCE, dedup_id="d1"
    )

    note = asyncio.run(
        handler.dispatch_ambient_trigger(CallbackContext(state={}), trigger)
    )

    mock_schedule.assert_not_awaited()
    mock_enqueue.assert_not_called()
    assert "agents-cli aqua attach" in note


def test_handle_task_fire_passes_its_dedup_id(
    monkeypatch: pytest.MonkeyPatch, mock_schedule: mock.AsyncMock
) -> None:
    _use_config(monkeypatch, observed_agent_name="agent-x")
    trigger = handler.AmbientTrigger(
        type="task_fire", resource_name=_RESOURCE, dedup_id="d3"
    )

    note = asyncio.run(
        handler.dispatch_ambient_trigger(CallbackContext(state={}), trigger)
    )

    mock_schedule.assert_awaited_once()
    kwargs = mock_schedule.call_args.kwargs
    assert kwargs["idempotency_key"] == "d3"
    assert kwargs["ambient"] is True
    # The same ID the update derived, so the fire advances that record.
    assert kwargs["scheduled_run_id"] == handler.compute_scheduled_run_id("d3")
    assert "r1" in note
    assert "agent-x" in note


def _update_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins a config with the update-path wiring fully populated.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    _use_config(
        monkeypatch,
        observed_agent_name="agent-x",
        project_id="p",
        location="us-east4",
        aqa_engine_id="99",
        agent_revision_trigger_delay_seconds=120,
        delay_task_queue="projects/p/locations/l/queues/updates",
        ambient_caller_sa="caller@p.iam.gserviceaccount.com",
    )


def test_handle_update_enqueues_delayed_trigger(
    monkeypatch: pytest.MonkeyPatch,
    mock_schedule: mock.AsyncMock,
    mock_enqueue: mock.Mock,
) -> None:
    # The task targets this engine's trigger route directly over OAuth (Cloud
    # Tasks supports an OAuth token with a custom body). The dedup_id is an
    # audit operation.id, a resource path, which is not a legal Cloud Tasks id.
    _update_config(monkeypatch)
    now = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    monkeypatch.setattr(handler, "_read_utc_clock", lambda: now)
    dedup_id = "projects/p/locations/us-central1/operations/7572401112376934400"
    trigger = handler.AmbientTrigger(
        type="update", resource_name=_RESOURCE, dedup_id=dedup_id
    )

    note = asyncio.run(
        handler.dispatch_ambient_trigger(CallbackContext(state={}), trigger)
    )

    mock_schedule.assert_not_called()
    mock_enqueue.assert_called_once()
    kwargs = mock_enqueue.call_args.kwargs
    assert kwargs["queue_path"] == "projects/p/locations/l/queues/updates"
    # The region appears in both the host prefix and the resource path, and the
    # passthrough hangs off `reasoningEngines/v1`, not `/v1`.
    assert kwargs["url"] == (
        "https://us-east4-aiplatform.googleapis.com/reasoningEngines/v1/"
        "projects/p/locations/us-east4/reasoningEngines/99/api/ambient/trigger"
    )
    assert kwargs["oauth_sa"] == "caller@p.iam.gserviceaccount.com"
    assert kwargs["headers"] == {"Content-Type": "application/json"}
    assert kwargs["schedule_time"] == now + dt.timedelta(seconds=120)
    # The dedup_id above with its `/`s substituted: a legal Cloud Tasks id (the
    # raw path is not), still readable back to the event it came from.
    assert kwargs["task_id"] == (
        "projects_p_locations_us-central1_operations_7572401112376934400"
    )
    assert re.fullmatch(r"[A-Za-z0-9_-]+", kwargs["task_id"])
    # The fired-back body carries the full unsanitized dedup_id (only the task
    # name is substituted), so the downstream GCS marker still hashes the original.
    body = json.loads(kwargs["body"].decode())
    assert handler.validate_trigger_payload(body) == handler.AmbientTrigger(
        type="task_fire", resource_name=_RESOURCE, dedup_id=dedup_id
    )
    assert dedup_id in note
    assert "agent-x" in note
    assert "120" in note


def test_handle_update_same_dedup_id_collapses(
    monkeypatch: pytest.MonkeyPatch,
    mock_schedule: mock.AsyncMock,
    mock_enqueue: mock.Mock,
) -> None:
    # Why sameness matters: Cloud Tasks dedups by task NAME, so a redelivered
    # audit event must map to the same id to collapse into one delayed fire.
    _update_config(monkeypatch)

    def _task_id_for(dedup_id: str) -> str:
        trigger = handler.AmbientTrigger(
            type="update", resource_name=_RESOURCE, dedup_id=dedup_id
        )
        asyncio.run(
            handler.dispatch_ambient_trigger(CallbackContext(state={}), trigger)
        )
        return mock_enqueue.call_args.kwargs["task_id"]

    first = _task_id_for("op-777")
    redelivery = _task_id_for("op-777")
    other = _task_id_for("op-888")

    assert first == redelivery
    assert other != first


def test_handle_update_skips_when_wiring_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    mock_schedule: mock.AsyncMock,
    mock_enqueue: mock.Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Without an engine id, no :streamQuery URL can be built. Partial
    # queue/caller-SA pairs fail validation at config load, so this tests the
    # engine-id-unset case.
    _use_config(
        monkeypatch,
        observed_agent_name="agent-x",
        aqa_engine_id="",  # missing -> nothing to target
        delay_task_queue="projects/p/locations/l/queues/updates",
        ambient_caller_sa="caller@p.iam.gserviceaccount.com",
    )
    trigger = handler.AmbientTrigger(
        type="update", resource_name=_RESOURCE, dedup_id="op-1"
    )

    with caplog.at_level("WARNING"):
        note = asyncio.run(
            handler.dispatch_ambient_trigger(CallbackContext(state={}), trigger)
        )

    mock_enqueue.assert_not_called()
    mock_schedule.assert_not_called()
    assert "no delayed task enqueued" in note
    assert "agent-x" in note
    assert any("no delayed task enqueued" in r.message for r in caplog.records)


def test_every_dispatch_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    mock_schedule: mock.AsyncMock,
    mock_enqueue: mock.Mock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An update waits for minutes before anything else shows; the log is
    where an operator checks that it arrived."""
    _update_config(monkeypatch)
    ctx = CallbackContext(state={})

    with caplog.at_level("INFO", logger=handler.__name__):
        for trigger_type in ("update", "task_fire", "scheduled"):
            asyncio.run(
                handler.dispatch_ambient_trigger(
                    ctx,
                    handler.AmbientTrigger(
                        type=trigger_type,
                        resource_name=_RESOURCE,
                        dedup_id="d1",
                    ),
                )
            )

    notes = [r.message for r in caplog.records if r.name == handler.__name__]
    assert any("Ambient update trigger" in n for n in notes)
    assert any("Ambient task_fire trigger" in n for n in notes)
    assert any("Ambient scheduled trigger" in n for n in notes)


def test_a_launch_that_starts_nothing_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, mock_schedule: mock.AsyncMock
) -> None:
    _use_config(monkeypatch, observed_agent_name="agent-x")
    mock_schedule.return_value = {"error": "busy"}

    note = asyncio.run(
        handler.dispatch_ambient_trigger(
            CallbackContext(state={}),
            handler.AmbientTrigger(type="scheduled", resource_name=_RESOURCE),
        )
    )

    assert "no investigation started" in note and "busy" in note


# --- the scheduled record an update leaves --------------------------------- #


def _dispatch_update(dedup_id: str = "op-1") -> str:
    """Dispatches an `update` trigger with an empty state.

    Args:
        dedup_id: The audit event's operation id.

    Returns:
        The dispatch note.
    """
    return asyncio.run(
        handler.dispatch_ambient_trigger(
            CallbackContext(state={}),
            handler.AmbientTrigger(
                type="update", resource_name=_RESOURCE, dedup_id=dedup_id
            ),
        )
    )


def test_an_update_records_a_scheduled_run_due_with_its_task(
    monkeypatch: pytest.MonkeyPatch, mock_enqueue: mock.Mock
) -> None:
    """The wait shows in the run list as soon as the update arrives."""
    _update_config(monkeypatch)
    now = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    monkeypatch.setattr(handler, "_read_utc_clock", lambda: now)

    note = _dispatch_update("op-1")

    run_id = handler.compute_scheduled_run_id("op-1")
    record = get_run({}, run_id)
    assert record is not None
    assert record.status == "scheduled"
    assert record.observed_agent_name == "agent-x"
    assert record.trigger_type == "task_fire"
    assert record.idempotency_key == "op-1"
    assert (
        dt.datetime.fromisoformat(record.due_at)
        == mock_enqueue.call_args.kwargs["schedule_time"]
    )
    # The window is left to the fire, so it holds the new revision's traces.
    assert record.window_start is None and record.window_end is None
    assert run_id in note


def test_a_redelivered_update_keeps_the_first_due_time(
    monkeypatch: pytest.MonkeyPatch, mock_enqueue: mock.Mock
) -> None:
    """Cloud Tasks keeps the first task under the shared name, so the record
    keeps the first delivery's due time too."""
    _update_config(monkeypatch)
    first = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    monkeypatch.setattr(handler, "_read_utc_clock", lambda: first)
    _dispatch_update("op-1")
    monkeypatch.setattr(
        handler, "_read_utc_clock", lambda: first + dt.timedelta(minutes=3)
    )

    _dispatch_update("op-1")

    record = get_run({}, handler.compute_scheduled_run_id("op-1"))
    assert dt.datetime.fromisoformat(record.due_at) == first + dt.timedelta(
        seconds=120
    )
    assert mock_enqueue.call_count == 2


def test_a_redelivered_update_leaves_a_started_run_alone(
    monkeypatch: pytest.MonkeyPatch, mock_enqueue: mock.Mock
) -> None:
    _update_config(monkeypatch)
    _dispatch_update("op-1")
    run_id = handler.compute_scheduled_run_id("op-1")
    asyncio.run(model.update_run({}, run_id, status="running"))

    _dispatch_update("op-1")

    assert get_run({}, run_id).status == "running"


def test_a_redelivered_update_leaves_a_run_that_failed_while_running(
    monkeypatch: pytest.MonkeyPatch, mock_enqueue: mock.Mock
) -> None:
    """Its window says it started; only a failure before that is retried."""
    _update_config(monkeypatch)
    _dispatch_update("op-1")
    run_id = handler.compute_scheduled_run_id("op-1")
    asyncio.run(
        model.update_run(
            {},
            run_id,
            status="failed",
            window_start="2026-01-01T00:00:00+00:00",
            window_end="2026-01-02T00:00:00+00:00",
        )
    )

    _dispatch_update("op-1")

    assert get_run({}, run_id).status == "failed"


def test_a_failed_enqueue_fails_the_record_and_is_retried(
    monkeypatch: pytest.MonkeyPatch, mock_enqueue: mock.Mock
) -> None:
    """Raising makes Pub/Sub redeliver; the failed record says why meanwhile,
    and the redelivery that gets through schedules it again."""
    _update_config(monkeypatch)
    run_id = handler.compute_scheduled_run_id("op-1")
    mock_enqueue.side_effect = RuntimeError("permission denied")

    with pytest.raises(RuntimeError):
        _dispatch_update("op-1")

    record = get_run({}, run_id)
    assert record.status == "failed"
    assert "could not enqueue" in record.error
    assert "permission denied" in record.error

    mock_enqueue.side_effect = None
    _dispatch_update("op-1")

    assert get_run({}, run_id).status == "scheduled"


def test_an_update_still_enqueues_when_the_record_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch,
    mock_enqueue: mock.Mock,
    investigation_store: object,
) -> None:
    """The record makes the wait visible; the investigation does not need it."""
    _update_config(monkeypatch)

    def refuse(record: object) -> None:
        raise RuntimeError("bigquery is down")

    monkeypatch.setattr(investigation_store, "append", refuse)

    _dispatch_update("op-1")

    mock_enqueue.assert_called_once()


def test_an_update_without_wiring_records_nothing(
    monkeypatch: pytest.MonkeyPatch, mock_enqueue: mock.Mock
) -> None:
    """No task will fire, so a scheduled record would only wait to fail."""
    _use_config(monkeypatch, observed_agent_name="agent-x", aqa_engine_id="")

    _dispatch_update("op-1")

    assert get_run({}, handler.compute_scheduled_run_id("op-1")) is None


def test_the_scheduled_run_id_is_stable_and_shaped_like_a_generated_one() -> (
    None
):
    first = handler.compute_scheduled_run_id("projects/1/operations/2")

    assert first == handler.compute_scheduled_run_id("projects/1/operations/2")
    assert first != handler.compute_scheduled_run_id("projects/1/operations/3")
    assert re.fullmatch(r"[0-9a-f]{8}", first)
