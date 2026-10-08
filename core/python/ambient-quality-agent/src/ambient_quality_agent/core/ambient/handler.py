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

"""Ambient trigger execution logic.

Wiring::

        Cloud Scheduler ── scheduled ─────────┐
    ┌─► Cloud Tasks ────── task_fire, delayed ┴─► routes.receive_trigger() ──────────┐
    │   Pub/Sub ────────── audit log entry ─────► routes.receive_observed_update() ──┤
    │                                                                                │
    │   AmbientTrigger ◄── validate_trigger_payload() ◄──────────────────────────────┤
    │                                                                                │
    │   dispatch_ambient_trigger() ◄─────────────────────────────────────────────────┘
    │     ├──► effective_config.load()
    │     ├─ scheduled, task_fire ──► _launch_investigation()
    │     └─ update ──► _schedule_delayed_update() ──► _build_engine_name()
    │                     ├──► _record_scheduled_run() ──► model.create_run()
    │                     ├──► _fail_unenqueued_run() ──► model.update_run()
    │                     │ cloudtasks.enqueue_http_task()
    └─────────────────────┘

Ambient triggers start investigations automatically from scheduled cron ticks
or observed agent updates. An update is deferred: it enqueues a delayed Cloud
Task that fires back as a `task_fire` once the new revision's traces have had
time to accumulate. While it waits, a `SCHEDULED` run record carrying the due
time stands for it, so the wait shows in the run list; the `task_fire` then
advances that same record (`_launch_investigation`'s `scheduled_run_id`). Every
dispatch is logged with its outcome.

Main rules: a `scheduled` trigger has no dedup id, so its key is computed from a
timestamp -- `X-CloudScheduler-ScheduleTime`, or this container's clock -- which
every attempt at one fire repeats. It also checks everything since the run that
finished last. `update` and `task_fire` carry a
`dedup_id`, the audit event's id, which names the delayed Cloud Task so repeated
deliveries become one fire, and derives the scheduled record's run ID so they
become one record. The update delay comes from the settings, not from the
payload. The window is derived when the task fires, not when the update
arrives, so that it holds the new revision's traces.

`core.ambient.routes` gives each source an HTTP route and calls in here with the
payload it carried. That is the only way in.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import re
from typing import cast

from ambient_quality_agent.config import Config
from ambient_quality_agent.core.investigation import model
from ambient_quality_agent.core.investigation.job_scheduling import (
    NO_AGENT_ATTACHED,
    _build_engine_name,
    _launch_investigation,
)
from ambient_quality_agent.core.session_state import ADKStateLike, LaunchContext
from ambient_quality_agent.tools import cloudtasks
from ambient_quality_agent.tools.investigations.models import (
    InvestigationRecord,
    RunStatus,
)
from ambient_quality_agent.tools.observed_agent_config import effective_config

logger = logging.getLogger(__name__)


def _read_utc_clock() -> dt.datetime:
    """Returns current UTC time.

    Used as a seam that tests can pin to ensure schedule_time is deterministic.

    Returns:
        Current datetime in UTC timezone.
    """
    return dt.datetime.now(tz=dt.UTC)


_VALID_TYPES = frozenset({"scheduled", "task_fire", "update"})

_DEDUP_REQUIRED_TYPES = frozenset({"task_fire", "update"})

# Only the audit-sink path can be held to carrying an engine name: `update` comes
# from that sink, and `task_fire` is the same trigger fired back. A `scheduled`
# tick comes from Cloud Scheduler, which has no engine name to send when the
# deployment watches no engine (`observed_engine_id` null).
_RESOURCE_NAME_REQUIRED_TYPES = frozenset({"task_fire", "update"})


@dataclasses.dataclass(frozen=True)
class AmbientTrigger:
    """A parsed, validated ambient trigger payload.

    `dedup_id` is the delivery's stable audit id.
    `resource_name` is carried for traceability and read by nothing, which is
    what lets a `scheduled` tick arrive without one.
    """

    type: str
    resource_name: str | None = None
    dedup_id: str | None = None


class AmbientTriggerParseError(Exception):
    """A trigger payload was present but malformed."""


def validate_trigger_payload(payload: object) -> AmbientTrigger:
    """Validates a decoded trigger payload into an `AmbientTrigger`.

    `update` and `task_fire` require a `dedup_id` (the audit LogEntry's
    top-level `operation.id`) because they derive no key from the clock:
    it names the delayed Cloud Task and becomes the investigation's idempotency
    marker. A `scheduled` tick keys on the clock instead.

    Args:
        payload: Decoded JSON payload object.

    Returns:
        Validated AmbientTrigger dataclass instance.

    Raises:
        AmbientTriggerParseError: If the payload is not a dictionary, carries an
            unknown trigger type, or omits a required resource_name or dedup_id.
    """
    if not isinstance(payload, dict):
        raise AmbientTriggerParseError(
            f"payload is not a JSON object: {payload!r}"
        )

    trigger_type = payload.get("type")
    if trigger_type not in _VALID_TYPES:
        raise AmbientTriggerParseError(
            f"unknown type {trigger_type!r} (expected one of {sorted(_VALID_TYPES)})"
        )

    resource_name = payload.get("resource_name")
    resource_name = resource_name if _is_nonempty_str(resource_name) else None
    if resource_name is None and trigger_type in _RESOURCE_NAME_REQUIRED_TYPES:
        raise AmbientTriggerParseError(
            f"missing required 'resource_name' for a {trigger_type!r} trigger "
            f"(the audit sink always supplies one): {payload!r}"
        )

    dedup_id = payload.get("dedup_id")
    dedup_id = dedup_id if _is_nonempty_str(dedup_id) else None
    if dedup_id is None and trigger_type in _DEDUP_REQUIRED_TYPES:
        raise AmbientTriggerParseError(
            f"missing required 'dedup_id' for a {trigger_type!r} trigger (the "
            "ingress must inject the audit LogEntry's top-level operation.id): "
            f"{payload!r}"
        )

    return AmbientTrigger(
        type=trigger_type,
        resource_name=resource_name,
        dedup_id=dedup_id,
    )


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


async def dispatch_ambient_trigger(
    context: LaunchContext,
    trigger: AmbientTrigger,
    *,
    anchor: dt.datetime | None = None,
) -> str:
    """Dispatches a parsed ambient trigger and logs what it did.

    `scheduled` and `task_fire` triggers schedule an investigation directly.
    `update` triggers defer execution by enqueuing a delayed Cloud Task that
    fires back as a `task_fire`.

    Args:
        context: Execution launch context holding session state.
        trigger: Validated trigger event to dispatch.
        anchor: Optional intended firing instant used to derive dedup keys.

    Returns:
        Human-readable summary message describing the dispatch action.
    """
    config = effective_config.load(context.state)
    if not config.observed_agent_name:
        # An ambient trigger in a deployment that nothing has been attached to yet.
        note = f"Ambient {trigger.type} trigger: {NO_AGENT_ATTACHED}."
        logger.info("%s", note)
        return note

    if trigger.type == "update":
        return await _schedule_delayed_update(context, config, trigger)

    result = await _launch_investigation(
        context,
        idempotency_key=trigger.dedup_id,
        ambient=True,
        trigger_type=trigger.type,
        anchor=anchor,
        scheduled_run_id=compute_scheduled_run_id(trigger.dedup_id)
        if trigger.type == "task_fire" and trigger.dedup_id
        else None,
    )
    if "run_id" in result:
        note = (
            f"Ambient {trigger.type} trigger for {config.observed_agent_name}: "
            f"scheduled investigation run {result['run_id']} "
            f"(status {result['status']})."
        )
    else:
        note = (
            f"Ambient {trigger.type} trigger for {config.observed_agent_name}: "
            f"no investigation started: {result.get('error')}."
        )
    logger.info("%s", note)
    return note


def compute_scheduled_run_id(dedup_id: str) -> str:
    """Derives the run ID of the scheduled record an update leaves.

    Both hops compute it from the audit event's id: the `update` to create the
    record, and its `task_fire` to find it again. A redelivered event therefore
    lands on the record the first delivery made.

    Args:
        dedup_id: The audit LogEntry's `operation.id`.

    Returns:
        An eight-hex-digit run ID, the length of a generated one.
    """
    return hashlib.sha256(dedup_id.encode()).hexdigest()[:8]


async def _schedule_delayed_update(
    context: LaunchContext, config: Config, trigger: AmbientTrigger
) -> str:
    """Records a scheduled run and enqueues the delayed Cloud Task that starts it.

    1. Records a `SCHEDULED` run due when the task is (`_record_scheduled_run`).
    2. Enqueues a Cloud Task delayed by `agent_revision_trigger_delay_seconds`,
       so traces from the new revision can accumulate. The task POSTs a
       `task_fire` to this engine's `/ambient/trigger` route using
       `ambient_caller_sa` credentials.
    3. If the enqueue fails, fails the record it just wrote and re-raises, so
       Pub/Sub redelivers the update and the next attempt schedules it again.

    Args:
        context: Execution launch context holding session state.
        config: Effective deployment configuration.
        trigger: The incoming update trigger event.

    Returns:
        Summary message indicating whether the delayed task was enqueued or
        skipped due to incomplete wiring.

    Raises:
        Exception: Whatever the Cloud Tasks enqueue raised.
    """
    if not (
        config.delay_task_queue
        and config.ambient_caller_sa
        and config.aqa_engine_id
    ):
        # Raising an exception turns a well-formed request into an HTTP 500 error.
        # Incomplete deployment configuration is an operator problem, not a caller
        # problem.
        note = (
            f"Ambient update trigger for {config.observed_agent_name} but the "
            "update-path wiring is incomplete (need AQA_DELAY_TASK_QUEUE, "
            "AQA_AMBIENT_CALLER_SA, and the reasoning-engine id); no delayed task "
            "enqueued."
        )
        logger.warning(note)
        return note

    # Non-None here: `validate_trigger_payload` requires a `dedup_id` on `update`.
    dedup_id = cast(str, trigger.dedup_id)
    # A Cloud Tasks id allows only `[A-Za-z0-9_-]` and `dedup_id` is the audit
    # `operation.id`, a resource path: substituting keeps a task traceable to its event.
    task_id = re.sub(r"[^A-Za-z0-9_-]", "_", dedup_id)
    delay = config.agent_revision_trigger_delay_seconds
    schedule_time = _read_utc_clock() + dt.timedelta(seconds=delay)

    # Serialize the dataclass so the wire shape stays in sync with `AmbientTrigger`.
    fire_back = dataclasses.replace(trigger, type="task_fire")
    engine_name = _build_engine_name(
        config.project_id, config.location, config.aqa_engine_id
    )
    # Custom container routes require the `reasoningEngines/v1` URL prefix.
    # The standard `/v1` prefix returns 404.
    url = (
        f"https://{config.location}-aiplatform.googleapis.com/reasoningEngines/v1/"
        f"{engine_name}/api/ambient/trigger"
    )
    body = json.dumps(dataclasses.asdict(fire_back)).encode()
    run_id = compute_scheduled_run_id(dedup_id)
    recorded = await _record_scheduled_run(
        context.state,
        InvestigationRecord(
            run_id=run_id,
            status=RunStatus.SCHEDULED,
            observed_agent_name=config.observed_agent_name,
            trigger_type=fire_back.type,
            idempotency_key=dedup_id,
            due_at=schedule_time.isoformat(),
        ),
    )

    try:
        cloudtasks.enqueue_http_task(
            queue_path=config.delay_task_queue,
            url=url,
            body=body,
            schedule_time=schedule_time,
            oauth_sa=config.ambient_caller_sa,
            task_id=task_id,
            headers={"Content-Type": "application/json"},
        )
    except Exception as exc:
        logger.exception(
            "Ambient update trigger for %s: could not enqueue the delayed "
            "trigger (dedup_id %s).",
            config.observed_agent_name,
            dedup_id,
        )
        if recorded:
            await _fail_unenqueued_run(context.state, run_id, exc)
        raise
    note = (
        f"Ambient update trigger for {config.observed_agent_name}: enqueued delayed "
        f"trigger (dedup_id {dedup_id}, delay {delay}s, scheduled "
        f"{schedule_time.isoformat()}) for investigation run {run_id}."
    )
    logger.info("%s", note)
    return note


async def _record_scheduled_run(
    state: ADKStateLike, record: InvestigationRecord
) -> bool:
    """Writes the scheduled record unless an earlier delivery already made it.

    A record already present stays as it is: a redelivered update must neither
    move its due time nor reset a run that has started or finished. One that
    failed before it started (it has no window yet) is written again: a failed
    enqueue is what makes Pub/Sub redeliver, and this delivery retries it.

    A write that raises is logged rather than raised. The record only makes the
    wait visible; the investigation still runs when the task fires.

    Args:
        state: State dictionary to access the investigation store.
        record: The `SCHEDULED` record to write.

    Returns:
        True if this call wrote the record.
    """
    try:
        existing = await asyncio.to_thread(
            lambda: model.store_factory(state).get(
                record.run_id, include_events=False
            )
        )
        if existing is not None and not (
            existing.status == RunStatus.FAILED and existing.window_end is None
        ):
            logger.info(
                "Investigation run %s is already recorded as %s; leaving it.",
                record.run_id,
                existing.status,
            )
            return False
        await model.create_run(state, record)
    except Exception:
        logger.exception(
            "could not record scheduled investigation run %s", record.run_id
        )
        return False
    return True


async def _fail_unenqueued_run(
    state: ADKStateLike, run_id: str, exc: Exception
) -> None:
    """Fails a scheduled record whose delayed trigger could not be enqueued.

    Args:
        state: State dictionary to access the investigation store.
        run_id: ID of the scheduled record.
        exc: The error the enqueue raised.
    """
    try:
        await model.update_run(
            state,
            run_id,
            status=RunStatus.FAILED,
            error=(
                "could not enqueue the delayed trigger: "
                f"{type(exc).__name__}: {exc}"
            ),
        )
    except Exception:
        logger.exception("could not fail investigation run %s", run_id)
