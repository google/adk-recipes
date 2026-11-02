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
    │     ├─ scheduled ──► schedule.compute_tick(), then _dispatch_tick()
    │     │                  ├──► effective_config.list_agent_keys()
    │     │                  └─ each agent ──► _dispatch_agent_tick()
    │     │                       ├──► effective_config.load()
    │     │                       ├──► _resolve_unscheduled_note()
    │     │                       │      ├──► effective_config.load_selected_agent_record()
    │     │                       │      ├──► schedule.resolve_investigation_schedule()
    │     │                       │      └──► schedule.is_schedule_due()
    │     │                       └─ when due ──► _launch_agent_investigation()
    │     ├─ update, task_fire ──► _resolve_trigger_agent_key()
    │     │                          ├─ task_fire ──► model.store_factory().get()
    │     │                          └──► effective_config.resolve_engine_agent_key()
    │     │                        then effective_config.load_fresh_agent_config()
    │     ├─ task_fire ──► _launch_agent_investigation() ──► _launch_investigation()
    │     └─ update ──► _schedule_delayed_update() ──► _build_engine_name()
    │                     ├──► _record_scheduled_run() ──► model.create_run()
    │                     ├──► _fail_unenqueued_run() ──► model.update_run()
    │                     │ cloudtasks.enqueue_http_task()
    └─────────────────────┘

Ambient triggers start investigations automatically from scheduler ticks or
observed agent updates. Each investigation is for one attached agent, and runs
under a state that selects it (`effective_config.AGENT_KEY_STATE`). Cloud
Scheduler ticks every `schedule.TICK_PERIOD` for the whole instance; a tick
starts an investigation for each agent whose cron fell due since the tick
before it, so one tick can start several. A call off the 15-minute grid counts
as the tick before it, so it adds no investigation the grid does not. An
update names the observed engine, and is for the agent whose attachment
records that engine (`effective_config.resolve_engine_agent_key`). An update is
deferred: it enqueues a delayed Cloud Task that fires back as a `task_fire`
once the new revision's traces have had time to accumulate. While it waits, a
`SCHEDULED` run record carrying the due time stands for it, so the wait shows
in the run list; the `task_fire` then advances that same record
(`_launch_investigation`'s `scheduled_run_id`), and is for the agent that
record was written for. Every dispatch is logged with its outcome.

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
from ambient_quality_agent.core.session_state import (
    ADKStateLike,
    DetachedContext,
    LaunchContext,
)
from ambient_quality_agent.tools import cloudtasks
from ambient_quality_agent.tools.investigations.models import (
    InvestigationRecord,
    RunStatus,
)
from ambient_quality_agent.tools.observed_agent_config import (
    effective_config,
    schedule,
)
from ambient_quality_shared import engine_agent

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
# tick comes from Cloud Scheduler, and is for every agent rather than one
# engine.
_RESOURCE_NAME_REQUIRED_TYPES = frozenset({"task_fire", "update"})


@dataclasses.dataclass(frozen=True)
class AmbientTrigger:
    """A parsed, validated ambient trigger payload.

    `dedup_id` is the delivery's stable audit id.
    `resource_name` names the observed engine. An `update`, and a `task_fire`
    whose scheduled record is gone, find their agent by it; a `scheduled`
    tick, which is for every agent, may arrive without one.
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

    1. A `scheduled` trigger is for every attached agent: each whose cron
       fell due in the tick period ending at `anchor` gets an investigation
       (`_dispatch_tick`).
    2. An `update` or `task_fire` is for one agent
       (`_resolve_trigger_agent_key`). One that matches no agent, or whose
       agent is not attached, starts nothing.
    3. A `task_fire` always schedules an investigation. An `update` defers it
       by enqueuing a delayed Cloud Task that fires back as a `task_fire`.

    A trigger that starts nothing returns a note rather than raising, so that
    Cloud Scheduler, Cloud Tasks and Pub/Sub do not deliver it again.

    Args:
        context: Execution launch context holding session state.
        trigger: Validated trigger event to dispatch.
        anchor: Optional intended firing instant, which dedup keys are derived
            from, and for a `scheduled` trigger, whose tick the schedule is
            checked at; this container's clock if None.

    Returns:
        Human-readable summary message describing the dispatch action.

    Raises:
        Exception: For an `update` or `task_fire`, whatever reading the run
            store or the agent's attachment, enqueuing the delayed trigger, or
            launching the investigation raised; for a `scheduled` trigger,
            whatever an agent's launch raised. Either is delivered again. A
            tick that cannot list the agents or read one's attachment is
            answered with a note, as the next tick checks again.
    """
    if trigger.type == "scheduled":
        # One instant for every agent's check and dedup key, so a retry
        # repeats both.
        return await _dispatch_tick(
            context, trigger, schedule.compute_tick(anchor or _read_utc_clock())
        )

    agent_key = await _resolve_trigger_agent_key(context.state, trigger)
    if agent_key is None:
        note = (
            f"Ambient {trigger.type} trigger for engine {trigger.resource_name}: "
            "no attached agent observes it; no investigation scheduled. Attach "
            "it with `agents-cli aqua attach`."
        )
        logger.warning("%s", note)
        return note
    agent_context = _build_agent_context(context, agent_key)
    try:
        config = await asyncio.to_thread(
            effective_config.load_fresh_agent_config, agent_key
        )
    except effective_config.UnknownAgentError as exc:
        note = (
            f"Ambient {trigger.type} trigger for {agent_key}: {exc} No "
            "investigation started."
        )
        logger.warning("%s", note)
        return note

    if trigger.type == "update":
        return await _schedule_delayed_update(agent_context, config, trigger)
    return await _launch_agent_investigation(
        agent_context, config, trigger, anchor=anchor
    )


async def _dispatch_tick(
    context: LaunchContext, trigger: AmbientTrigger, tick: dt.datetime
) -> str:
    """Starts an investigation for each agent whose schedule fell due at `tick`.

    Every agent is checked even when an earlier one fails. A launch that
    raised is raised again once all are checked, so that Cloud Scheduler
    retries the tick; the agents whose investigation already started are
    deduplicated on the retry by their idempotency key, which `tick` derives.

    Args:
        context: Execution launch context holding session state.
        trigger: The `scheduled` trigger.
        tick: The tick instant, on the `schedule.TICK_PERIOD` grid.

    Returns:
        One note per agent, one per line; or a note that no agent is attached
        or that the attached agents cannot be listed.

    Raises:
        Exception: The first exception an agent's launch raised.
    """
    try:
        agent_keys = await asyncio.to_thread(effective_config.list_agent_keys)
    except Exception as exc:
        logger.warning(
            "Ambient scheduled trigger: cannot list the attached agents",
            exc_info=True,
        )
        return (
            "Ambient scheduled trigger: cannot list the attached agents "
            f"({type(exc).__name__}: {exc}); no investigation started."
        )
    if not agent_keys:
        note = f"Ambient {trigger.type} trigger: {NO_AGENT_ATTACHED}."
        logger.info("%s", note)
        return note
    notes: list[str] = []
    failures: list[Exception] = []
    for agent_key in agent_keys:
        try:
            notes.append(
                await _dispatch_agent_tick(
                    _build_agent_context(context, agent_key), trigger, tick
                )
            )
        except Exception as exc:
            logger.exception(
                "Ambient scheduled trigger for %s: could not start its "
                "investigation.",
                agent_key,
            )
            notes.append(
                f"Ambient scheduled trigger for {agent_key}: could not start "
                f"its investigation ({type(exc).__name__}: {exc})."
            )
            failures.append(exc)
    if failures:
        raise failures[0]
    return "\n".join(notes) or (
        f"Ambient scheduled trigger: {NO_AGENT_ATTACHED}."
    )


async def _dispatch_agent_tick(
    context: LaunchContext, trigger: AmbientTrigger, tick: dt.datetime
) -> str:
    """Starts an investigation for one agent if its schedule fell due at `tick`.

    Args:
        context: Launch context whose state selects the agent.
        trigger: The `scheduled` trigger.
        tick: The tick instant, on the `schedule.TICK_PERIOD` grid.

    Returns:
        The note logged for the agent.
    """
    try:
        config = await asyncio.to_thread(effective_config.load, context.state)
    except effective_config.UnknownAgentError as exc:
        # Detached since the listing, or an attachment that cannot be read or
        # is not valid; the next tick checks it again.
        note = f"Ambient scheduled trigger: {exc} No investigation started."
        logger.warning("%s", note)
        return note
    if note := await _resolve_unscheduled_note(context.state, config, tick):
        logger.info("%s", note)
        return note
    return await _launch_agent_investigation(
        context, config, trigger, anchor=tick
    )


async def _launch_agent_investigation(
    context: LaunchContext,
    config: Config,
    trigger: AmbientTrigger,
    *,
    anchor: dt.datetime | None,
) -> str:
    """Launches the investigation a `scheduled` or `task_fire` trigger starts.

    Args:
        context: Launch context whose state selects the agent.
        config: The agent's effective configuration.
        trigger: The trigger.
        anchor: The instant dedup keys are derived from; the clock if None.

    Returns:
        The note logged for the launch.
    """
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
            f"Ambient {trigger.type} trigger for {config.resolve_agent_key()}: "
            f"scheduled investigation run {result['run_id']} "
            f"(status {result['status']})."
        )
    else:
        note = (
            f"Ambient {trigger.type} trigger for {config.resolve_agent_key()}: "
            f"no investigation started: {result.get('error')}."
        )
    logger.info("%s", note)
    return note


async def _resolve_trigger_agent_key(
    state: ADKStateLike, trigger: AmbientTrigger
) -> str | None:
    """Resolves the agent an `update` or `task_fire` trigger is for.

    1. A `task_fire` is for the agent of the `SCHEDULED` record its `update`
       left. Run ids are unique across agents, so the record is found by its
       id alone.
    2. Otherwise, and for an `update`, the agent whose attachment records the
       engine `resource_name` names (`effective_config.resolve_engine_agent_key`).

    Args:
        state: Session state, which reaches the run store.
        trigger: The `update` or `task_fire` trigger.

    Returns:
        The agent's key, or `None` when no agent can be told to be the
        trigger's.

    Raises:
        Exception: Whatever reading the run store or the attachments raised.
    """
    # Non-None here: `validate_trigger_payload` requires both on these types.
    resource_name = cast(str, trigger.resource_name)
    dedup_id = cast(str, trigger.dedup_id)
    if trigger.type == "task_fire":
        run_id = compute_scheduled_run_id(dedup_id)
        record = await asyncio.to_thread(
            lambda: model.store_factory(state).get(run_id, include_events=False)
        )
        if record is not None and record.observed_agent_name:
            return record.observed_agent_name
    return await asyncio.to_thread(
        effective_config.resolve_engine_agent_key,
        engine_agent.extract_engine_id(resource_name),
    )


def _build_agent_context(
    context: LaunchContext, agent_key: str
) -> DetachedContext:
    """Builds a launch context that selects one agent.

    A copy, because one tick launches for several agents from one context.

    Args:
        context: The trigger's launch context.
        agent_key: Key of the agent to select.

    Returns:
        A context whose state is a copy of `context`'s, selecting `agent_key`.
    """
    detached = DetachedContext.from_context(context)
    detached.state[effective_config.AGENT_KEY_STATE] = agent_key
    return detached


async def _resolve_unscheduled_note(
    state: ADKStateLike, config: Config, tick: dt.datetime
) -> str:
    """Resolves why a scheduler tick starts no investigation, if it starts none.

    Args:
        state: Session state, which selects the agent whose schedule is
            checked.
        config: Effective deployment configuration.
        tick: The instant the tick was scheduled for.

    Returns:
        A note saying the attachment or its schedule is unreadable, or the
        schedule is paused or not due; empty when it fell due in the tick
        period ending at `tick`.
    """
    prefix = f"Ambient scheduled trigger for {config.resolve_agent_key()}"
    try:
        record = await asyncio.to_thread(
            effective_config.load_selected_agent_record, state
        )
    except Exception as exc:
        # Read as no attachment, the agent would run on the default schedule,
        # whatever its own says, paused included.
        logger.warning("%s: cannot read its attachment", prefix, exc_info=True)
        return (
            f"{prefix}: cannot read its attachment ({type(exc).__name__}: "
            f"{exc}); no investigation started."
        )
    cron = schedule.resolve_investigation_schedule(record)
    if cron is None:
        return f"{prefix}: its schedule is paused; no investigation started."
    try:
        due = schedule.is_schedule_due(cron, tick)
    except ValueError as exc:
        # A schedule the engine cannot read would otherwise be silent until
        # someone notices that nothing runs.
        logger.warning("%s: cannot read its schedule: %s", prefix, exc)
        return (
            f"{prefix}: cannot read its schedule ({exc}); no investigation "
            "started."
        )
    if not due:
        return (
            f"{prefix}: schedule {cron!r} is not due in the tick ending "
            f"{tick.isoformat()}."
        )
    return ""


def compute_scheduled_run_id(dedup_id: str) -> str:
    """Derives the run ID of the scheduled record an update leaves.

    Both hops compute it from the audit event's id: the `update` to create the
    record, and its `task_fire` to find it again. A redelivered event therefore
    lands on the record the first delivery made.

    Args:
        dedup_id: The audit LogEntry's `operation.id`.

    Returns:
        A 32-hex-digit run ID, the length of a generated one, so that it is
        unique across agents too.
    """
    return hashlib.sha256(dedup_id.encode()).hexdigest()[:32]


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
        context: Launch context whose state selects the agent.
        config: The agent's effective configuration.
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
            f"Ambient update trigger for {config.resolve_agent_key()} but the "
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
            observed_agent_name=config.resolve_agent_key(),
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
            config.resolve_agent_key(),
            dedup_id,
        )
        if recorded:
            await _fail_unenqueued_run(context.state, run_id, exc)
        raise
    note = (
        f"Ambient update trigger for {config.resolve_agent_key()}: enqueued delayed "
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
