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

"""Scheduling an investigation: the synchronous user-turn half.

Wiring::

    chat_agent.start_investigation()          chat_agent.start_custom_investigation()
    orchestrator.build_orchestrator() tool    ambient.handler.dispatch_ambient_trigger()
    command_routes.schedule_investigation()           │
                │                                     │
                ▼                                     ▼
    schedule_investigation() ───────────────► _launch_investigation()
                                                  ├──► derive_window_and_budget()
                                                  ├──► locking.InvestigationLock
                                                  │      acquire(), release()
                                                  ├──► model.create_run(),
                                                  │      get_run(), update_run(),
                                                  │      fail_stale_pending_runs(),
                                                  │      fail_overdue_scheduled_runs()
                                                  ▼
                                              _dispatch()
                             sync_investigation   │   durable
                   ┌──────────────────────────────┴───────────────┐
                   ▼                                              ▼
       _run_sweep_on_a_thread()                        submit_investigation()
                   │ worker thread                                │ submits query job
                   ▼                                              ▼
       job_execution.execute_investigation()           Agent Runtime (run self-query)

Orchestrates investigation launch:
1. `_launch_investigation` resolves configuration, derives an evaluation window
   and budget anchored to submit time, and records a `PENDING` run -- under the
   run ID of the `SCHEDULED` record an observed-agent update left, when the
   ambient handler names one; `_dispatch` then takes one of the two branches
   below.
2. Durable execution (`submit_investigation`): submits a durable self-query LRO
   job that runs asynchronously in `job_execution`.
3. Synchronous execution (`sync_investigation`): runs `job_execution` on a worker
   thread, waits for completion, and holds an exclusive lock on the agent for
   the sweep's duration (see `locking`).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Any

import agentplatform
from ambient_quality_agent import config as config_module
from ambient_quality_agent.config import Config
from ambient_quality_agent.core.investigation import locking, model
from ambient_quality_agent.core.investigation.model import (
    InvestigationRecord,
    RunStatus,
)
from ambient_quality_agent.core.protocol import RUN_KEYWORD
from ambient_quality_agent.core.session_state import (
    ADKStateLike,
    DetachedContext,
    LaunchContext,
)
from ambient_quality_agent.tools.investigations.models import (
    CUSTOM_TRIGGER_TYPE,
    MANUAL_TRIGGER_TYPE,
    CustomOverrides,
)
from ambient_quality_agent.tools.observed_agent_config import effective_config

logger = logging.getLogger(__name__)

DEDUP_GRAIN_SECONDS = 300
"""Time bucket window in seconds for deduplicating repeated trigger invocations."""

NO_AGENT_ATTACHED = (
    "this deployment names no agent and none is attached, so there is nothing "
    "to investigate; attach one with `agents-cli aqua attach`"
)
"""Refusal for a deployment deployed without an observed agent, before an attach."""

DRY_RUN_WITH_OVERRIDES = (
    "a dry run re-derives its own window and budget, which would discard the "
    "custom investigation's; ask for one or the other"
)
"""Error message used when both dry_run and custom overrides are requested.

Shared between submit-time scheduling and execution-time validation.
"""

MAX_RUNS_IN_FLIGHT = 3
"""Maximum active runs allowed per agent before scheduling new custom runs is throttled.

Acts as a best-effort throttle against queue buildup rather than a strict
concurrency guarantee. Because active run counts are checked without distributed
locks prior to inserting records, concurrent scheduling requests may briefly
exceed this threshold.
"""

SCHEDULED_RUN_GRACE_MINUTES = 60
"""Minutes past its due time a `SCHEDULED` run may wait for its delayed trigger.

Cloud Tasks dispatches a task at its schedule time; only a dispatch that keeps
failing is retried later than this. Past it, the next submission treats the
trigger as lost (its task deleted or never dispatched) and fails the run, so the
record does not wait forever. A trigger that still fires after that starts a run
under an ID of its own.
"""

# `RUN_KEYWORD` (defined in `core.protocol`) prefixes the self-query message that
# `submit_investigation` sends, so the orchestrator routes it to `job_execution`.

# AdkApp operation invoked by the durable query job. Streaming is required because
# the investigation graph emits newline-delimited event streams saved to GCS.
#
# Handled via the Agent Runtime contract: an `async_stream` request routes to
# /api/stream_reasoning_engine with `class_method` and `input`.
_JOB_CLASS_METHOD = "async_stream_query"

_AMBIENT_USER_ID = "ambient"


def _read_utc_clock() -> dt.datetime:
    """Read current UTC datetime (isolated as a seam for deterministic tests).

    Returns:
        Current UTC datetime object.
    """
    return dt.datetime.now(tz=dt.UTC)


def iso_instant_to_utc(value: str, *, label: str) -> str:
    """Parse an ISO-8601 timestamp with explicit timezone offset and convert to UTC.

    Explicit offsets prevent ambiguous time conversions when querying BigQuery TIMESTAMP fields.

    Args:
        value: ISO-8601 formatted timestamp with timezone offset (e.g. '2026-09-15T09:00:00+02:00').
        label: Field name included in error messages.

    Returns:
        ISO-8601 formatted timestamp string in UTC.

    Raises:
        ValueError: If value cannot be parsed as ISO-8601 or lacks a timezone offset.
    """
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{label} must be an ISO-8601 instant with a UTC offset "
            f"(e.g. 2026-09-15T09:00:00+02:00); got {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"{label} must name a UTC offset (e.g. 2026-09-15T09:00:00+02:00 or "
            f"2026-09-15T07:00:00Z); got {value!r}, which has no time zone"
        )
    return parsed.astimezone(dt.UTC).isoformat()


def derive_window_and_budget(
    config: Config, *, since: str | None = None
) -> tuple[str, str, int]:
    """Calculate the telemetry evaluation window and per-metric budget.

    Derives the fixed evaluation window and budget prior to execution:
    1. If `since` is provided, starts from the end of the previous run; otherwise
       defaults to `data_lookback_window` days prior to now.
    2. Bounds `window_start` to at most `data_lookback_window` days in the past.
    3. Calculates per-metric budget: in `session_review` mode, allocates the full
       cap; in `eval_service` mode, divides cap evenly across active metrics.

    Args:
        config: Agent configuration.
        since: Optional ISO-8601 timestamp of previous successful run's window end.

    Returns:
        Tuple of (window_start_iso, window_end_iso, budget_per_metric).
    """
    cap = config.data_evaluation_cap
    if config.quality_analysis_mode == "session_review":
        budget = max(0, cap)
    else:
        budget = _compute_eval_service_budget(config, cap)

    now = _read_utc_clock()
    window_end = now
    floor = window_end - dt.timedelta(days=config.data_lookback_window)
    window_start = dt.datetime.fromisoformat(since) if since else floor
    if window_start < floor:
        logger.warning(
            "Last window ended %s, beyond the %d-day lookback: starting at %s "
            "and skipping the %s before it.",
            window_start.isoformat(),
            config.data_lookback_window,
            floor.isoformat(),
            floor - window_start,
        )
        window_start = floor
    return window_start.isoformat(), window_end.isoformat(), budget


def _compute_eval_service_budget(config: Config, cap: int) -> int:
    """Divide the evaluation cap evenly across configured metrics (minimum 1 each).

    Args:
        config: Agent configuration containing metric lists.
        cap: Total evaluation budget cap.

    Returns:
        Integer budget allocated per metric, or 0 if no metrics are configured.
    """
    total_metrics = len(config.multi_turn_metrics) + len(
        config.single_turn_metrics
    )
    if total_metrics == 0:
        logger.warning("No metrics to evaluate.")
        return 0
    return max(1, cap // total_metrics) if cap > 0 else 0


async def schedule_investigation(
    tool_context: LaunchContext, dry_run: bool = False
) -> dict[str, Any]:
    """Schedules an investigation and returns immediately (or waits for it if sync_investigation is enabled).

    Delegates to `_launch_investigation` with no ambient parameters, so an ad-hoc
    run keeps the day-based lookback window and carries no idempotency key --
    identical to the behavior before the ambient seam existed.
    """
    return await _launch_investigation(tool_context, dry_run=dry_run)


async def _launch_investigation(
    context: LaunchContext,
    *,
    idempotency_key: str | None = None,
    ambient: bool = False,
    dry_run: bool = False,
    trigger_type: str = MANUAL_TRIGGER_TYPE,
    anchor: dt.datetime | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    custom_overrides: CustomOverrides | None = None,
    scheduled_run_id: str | None = None,
) -> dict[str, Any]:
    """Schedule one investigation; the seam shared by the tools and the handler.

    Records a `pending` run, then either runs the investigation on a worker
    thread and waits for it (if `config.sync_investigation` is enabled) or
    submits a durable long-running query job against this engine. Returns the
    formatted run.

    Called by the `schedule_investigation` tool, by the ambient handler, and by
    `chat_agent.start_custom_investigation`. The handler passes `ambient`, which
    starts the window at the last finished run and, when no `idempotency_key` is
    given, derives a dedup key from the clock, and its own `trigger_type`. The
    tool passes neither, so it keeps the day-based window and no dedup.

    `anchor` is the instant the trigger was meant to fire, and replaces the
    clock in that key, so every attempt at one fire derives the same key.

    `scheduled_run_id` names the `SCHEDULED` record an observed-agent update
    left for this trigger. While that record is still scheduled, the run is
    recorded under its ID, so one redeploy reads as one run from arrival to
    result. Once it has moved on -- a redelivered trigger after the run began --
    the run gets an ID of its own, as any other duplicate does.

    Args:
        context: Launch context containing workflow state and session info.
        idempotency_key: Dedup key stored on the run, or derived from clock for ambient runs.
        ambient: Whether this is a scheduled ambient run that tracks previous window end.
        dry_run: Whether to run a minimal test probe. Incompatible with custom overrides.
        trigger_type: Identifier for what triggered the run (e.g. manual, custom, scheduled).
        anchor: Target trigger execution timestamp used for dedup key calculation.
        window_start: Explicit start timestamp (ISO-8601 with offset). Must be paired with window_end.
        window_end: Explicit end timestamp (ISO-8601 with offset). Must be paired with window_start.
        custom_overrides: Optional query selector SQL and reviewer focus overrides.
        scheduled_run_id: ID of the `SCHEDULED` record this trigger advances.

    Returns:
        Formatted investigation run dictionary, or an error payload if the run is
        refused, including when the deployment has no agent to investigate.

    Raises:
        ValueError: If window bounds are incomplete/invalid, or if dry_run is combined with custom overrides.
    """
    state = context.state
    config = effective_config.load(state)
    if not config.observed_agent_name:
        return {"error": NO_AGENT_ATTACHED}
    is_custom = custom_overrides is not None and custom_overrides.is_custom()
    if is_custom and dry_run:
        raise ValueError(DRY_RUN_WITH_OVERRIDES)
    explicit_window = _parse_explicit_window(window_start, window_end)
    if is_custom and trigger_type == MANUAL_TRIGGER_TYPE:
        # Custom runs are marked with CUSTOM_TRIGGER_TYPE to exclude them from
        # ambient watermarks and completion email notifications.
        trigger_type = CUSTOM_TRIGGER_TYPE
    scheduled = (
        await _get_scheduled_run(state, scheduled_run_id)
        if scheduled_run_id
        else None
    )
    try:
        await model.fail_stale_pending_runs(
            state, lease_minutes=config.pending_run_lease_minutes
        )
    except Exception:
        # Housekeeping errors on prior pending runs should not abort the current run.
        logger.exception("could not fail the stale pending runs")
    try:
        await model.fail_overdue_scheduled_runs(
            state,
            grace_minutes=SCHEDULED_RUN_GRACE_MINUTES,
            keep=scheduled_run_id,
        )
    except Exception:
        logger.exception("could not fail the overdue scheduled runs")
    since = (
        await model.get_last_finished_window_end(
            state, config.observed_agent_name
        )
        if ambient
        else None
    )
    derived_start, derived_end, budget = derive_window_and_budget(
        config, since=since
    )
    # Explicit window bounds override derived ranges while preserving per-run budgets.
    window_start, window_end = explicit_window or (derived_start, derived_end)

    # Scheduled triggers generate a dedup key based on anchor or current time bucket.
    # Keying on time buckets avoids deadlocks if previous runs failed.
    if idempotency_key is None and ambient:
        bucket = (
            int((anchor or _read_utc_clock()).timestamp())
            // DEDUP_GRAIN_SECONDS
        )
        idempotency_key = f"{config.observed_agent_name}:{bucket}"

    # Instantiate record before persistence so concurrency checks can inspect its parameters.
    record = InvestigationRecord(
        status=RunStatus.PENDING,
        observed_agent_name=config.observed_agent_name,
        trigger_type=trigger_type,
        metrics={
            "multi_turn": list(config.multi_turn_metrics),
            "single_turn": list(config.single_turn_metrics),
        },
        window_start=window_start,
        window_end=window_end,
        budget_per_metric=budget,
        dry_run=dry_run,
        custom_overrides=custom_overrides,
        effective_config=effective_config.serialize_config(config),
        idempotency_key=idempotency_key,
    )
    if scheduled is not None:
        record = record.model_copy(
            update={"run_id": scheduled.run_id, "due_at": scheduled.due_at}
        )

    if is_custom and not config.sync_investigation:
        # Check active run limits in store because durable jobs run in separate processes.
        if refusal := await _refuse_if_too_busy(
            state, config.observed_agent_name
        ):
            return refusal

    lock = locking.lock_factory(config)
    if holder := lock.acquire(config.observed_agent_name, record.run_id):
        logger.info(
            "A sweep of %r is already running as %s; %s.",
            config.observed_agent_name,
            holder,
            "refusing this custom investigation"
            if is_custom
            else "returning it rather than starting a second",
        )
        if is_custom:
            # Prevent returning an existing sweep that does not match the requested overrides.
            return {
                "error": (
                    f"a sweep of {config.observed_agent_name!r} is already "
                    f"running as {holder}; your investigation was not started"
                ),
                "running_run_id": holder,
            }
        if scheduled is not None:
            # Left scheduled, the record would wait for a trigger that has
            # already come and gone.
            await model.update_run(
                state,
                scheduled.run_id,
                status=RunStatus.SKIPPED,
                error=f"a sweep was already running as {holder}; skipped",
            )
        return await _get_running_run_view(
            state, config.observed_agent_name, holder
        )

    updated: InvestigationRecord | None = None
    try:
        await model.create_run(state, record)
        changes = await _dispatch(record, context, config)
        updated = await model.get_run(state, record.run_id)
        if changes:
            updated = await model.update_run(state, record.run_id, **changes)
    finally:
        # Release the lock only after terminal state write completes.
        lock.release(config.observed_agent_name, record.run_id)
    return model.format_run(updated or record)


async def _get_scheduled_run(
    state: ADKStateLike, run_id: str
) -> InvestigationRecord | None:
    """Return the run named `run_id` if it is still waiting for its trigger.

    Args:
        state: State dictionary to access the investigation store.
        run_id: ID of the scheduled record an observed-agent update left.

    Returns:
        The `SCHEDULED` record, or None if it is absent or has moved on.
    """
    record = await asyncio.to_thread(
        lambda: model.store_factory(state).get(run_id, include_events=False)
    )
    if record is None or record.status != RunStatus.SCHEDULED:
        return None
    return record


def _parse_explicit_window(
    window_start: str | None, window_end: str | None
) -> tuple[str, str] | None:
    """Validate and convert explicit window start and end timestamps to UTC.

    Requires both boundaries to be provided together to avoid accidental hybrid windows.

    Args:
        window_start: Optional ISO-8601 start timestamp string with offset.
        window_end: Optional ISO-8601 end timestamp string with offset.

    Returns:
        Tuple of (UTC start, UTC end) strings, or None if neither boundary was provided.

    Raises:
        ValueError: If only one bound is provided, timezone offset is missing,
            or start timestamp does not precede end timestamp.
    """
    if window_start is None and window_end is None:
        return None
    if not window_start or not window_end:
        raise ValueError(
            "an explicit window needs both window_start and window_end; "
            f"got start={window_start!r}, end={window_end!r}"
        )
    start = iso_instant_to_utc(window_start, label="window_start")
    end = iso_instant_to_utc(window_end, label="window_end")
    if start >= end:
        # Non-positive time ranges contain no telemetry and are rejected.
        raise ValueError(
            f"window_start {start} must fall before window_end {end}"
        )
    return start, end


async def _refuse_if_too_busy(
    state: ADKStateLike, agent_name: str
) -> dict[str, Any] | None:
    """Check if active runs exceed MAX_RUNS_IN_FLIGHT and return refusal response if so.

    Fails open if querying in-flight count fails so database transient errors do not block execution.

    Args:
        state: State dictionary used to resolve the investigation store.
        agent_name: Name of the observed agent to check concurrency for.

    Returns:
        Refusal error dictionary if limit is reached, or None to allow execution.
    """
    try:
        in_flight = await asyncio.to_thread(
            lambda: model.store_factory(state).count_in_flight(agent_name)
        )
    except Exception:
        logger.exception(
            "could not count the runs in flight for %r", agent_name
        )
        return None
    if in_flight < MAX_RUNS_IN_FLIGHT:
        return None
    return {
        "error": (
            f"{in_flight} investigation(s) of {agent_name!r} are already in "
            f"flight, at or above the {MAX_RUNS_IN_FLIGHT} this deployment "
            "aims to keep; your investigation was not started. Wait for one to "
            "finish."
        ),
        "runs_in_flight": in_flight,
    }


async def _dispatch(
    record: InvestigationRecord, context: LaunchContext, config: Config
) -> dict[str, Any]:
    """Dispatch the run's workload and return state modifications.

    Routes execution based on configuration:
    1. Synchronous sweep: runs inline on a worker thread until completion.
    2. Durable sweep: submits a long-running platform query job and returns its job name.

    Args:
        record: Stored investigation run record.
        context: Launch context containing state and session info.
        config: Effective agent configuration.

    Returns:
        Dictionary of updates to apply to the investigation record.
    """
    try:
        if config.sync_investigation:
            await _run_sweep_on_a_thread(record.run_id, context)
            return {}
        result = submit_investigation(record.run_id, context)
        return {"job_name": result.get("job_name")}
    except Exception as exc:
        logger.exception(
            "Failed to run/submit investigation run_id=%s", record.run_id
        )
        changes = {
            "status": RunStatus.FAILED,
            "error": f"failed: {type(exc).__name__}: {exc}",
        }
        # Asynchronous submission failures are logged here; synchronous failures log within execution.
        if not config.sync_investigation:
            from ambient_quality_agent.core.investigation import job_execution

            job_execution._log_run_finished(record, changes)
        return changes


async def _run_sweep_on_a_thread(run_id: str, context: LaunchContext) -> None:
    """Execute investigation workflow to completion on a dedicated worker thread.

    Workflow nodes run blocking functions that would otherwise block the asyncio
    event loop and stall HTTP endpoints. Delegating to a worker thread isolates
    execution while awaiting completion.

    Args:
        run_id: Identifier of the investigation run.
        context: Launch context containing workflow state.
    """
    logger.info("Running investigation %s on a thread of its own...", run_id)
    await asyncio.to_thread(
        _execute_on_this_thread, run_id, DetachedContext.from_context(context)
    )


def _execute_on_this_thread(run_id: str, context: LaunchContext) -> None:
    """Run `execute_investigation` synchronously in an isolated thread event loop.

    Args:
        run_id: Identifier of the investigation run.
        context: Launch context containing workflow state.
    """
    from ambient_quality_agent.core.investigation import job_execution

    asyncio.run(job_execution.execute_investigation(run_id, context))


async def _get_running_run_view(
    state: ADKStateLike, agent_name: str, run_id: str
) -> dict[str, Any]:
    """Retrieve formatted status for an active run when a new launch is refused.

    Args:
        state: State dictionary to access the investigation store.
        agent_name: Name of the observed agent.
        run_id: Identifier of the currently executing run.

    Returns:
        Formatted dictionary representing the active run.
    """
    record = await model.get_run(state, run_id)
    if record is None:
        # Active holder may be claimed before initial record write completes; construct placeholder view.
        record = InvestigationRecord(
            run_id=run_id,
            status=RunStatus.RUNNING,
            observed_agent_name=agent_name,
        )
    return model.format_run(record)


def submit_investigation(run_id: str, context: LaunchContext) -> dict[str, Any]:
    """Submit durable self-query LRO job and return its platform job name.

    Submits a platform-managed query job (`run_query_job`) against this same
    Agent Runtime agent containing `__RUN_INVESTIGATION__ <run_id>`. The platform
    job executes independently of the active user session.

    Args:
        run_id: Unique identifier of the investigation run.
        context: Launch context providing active session details.

    Returns:
        Dictionary containing `job_name` of the submitted query job.
    """
    cfg = config_module.load()
    session = context.session
    job_input: dict[str, Any] = {"message": f"{RUN_KEYWORD} {run_id}"}
    if session is None:
        engine_id = cfg.aqa_engine_id
        job_input["user_id"] = _AMBIENT_USER_ID
    else:
        engine_id = cfg.aqa_engine_id or session.app_name
        job_input["user_id"] = session.user_id
        job_input["session_id"] = session.id
    job_config: dict[str, Any] = {
        "query": json.dumps(
            {
                # Required: container routes streaming requests to /api/stream_reasoning_engine
                # based on class_method.
                "class_method": _JOB_CLASS_METHOD,
                "input": job_input,
            }
        )
    }
    if cfg.jobs_gcs_bucket:
        job_config["output_gcs_uri"] = (
            f"gs://{cfg.jobs_gcs_bucket}/aqa-jobs/{run_id}.json"
        )

    client = agentplatform.Client(project=cfg.project_id, location=cfg.location)
    result = client.agent_engines.run_query_job(
        name=_build_engine_name(cfg.project_id, cfg.location, engine_id),
        config=job_config,
    )
    job_name = getattr(result, "job_name", None)
    logger.info(
        "Submitted durable investigation run_id=%s job=%s", run_id, job_name
    )
    return {"job_name": job_name}


def _build_engine_name(project: str, location: str, engine_id: str) -> str:
    return (
        f"projects/{project}/locations/{location}/reasoningEngines/{engine_id}"
    )
