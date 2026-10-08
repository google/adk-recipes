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

"""Executing an investigation: the durable background half.

Wiring::

    orchestrator._run_investigation_action()   job_scheduling._execute_on_this_thread()
                    │ run self-query                        │ sync_investigation
                    └──────────────────┬────────────────────┘
                                       ▼
    model.get_run(),   ◄──────── execute_investigation()
    model.update_run()                 │
            ▲                          ▼
            └─────────────── _claim_marker_and_run() ──► gcs.claim_once() ──► GCS
                                       │
                                       ▼
                           run_investigation_graph()
                               ├──► job_scheduling.derive_window_and_budget()
                               ├──► Runner ──► investigation_workflow
                               ├──► _summarize(), _extract_counters()
                               └──► telemetry.setup.flush_spans()

    execute_investigation(), job_scheduling._dispatch()
        └──► _log_run_finished() ──► _should_notify()
                    └──► Cloud Logging (alert policy)

Executes inside the durable LRO invocation triggered by the `RUN_KEYWORD`
self-query (submitted by `job_scheduling`):
1. Claim idempotency marker (skips as duplicate if already present).
2. Transition the run status to `RUNNING`.
3. Run `investigation_workflow` via `run_investigation_graph` using the submit-time config snapshot.
4. Record terminal `DONE` or `FAILED` status and summary back to the run registry.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
from collections.abc import Callable
from typing import Any

from ambient_quality_agent.config import Config
from ambient_quality_agent.core import _logging
from ambient_quality_agent.core import _memory as memory
from ambient_quality_agent.core.investigation import job_scheduling, model
from ambient_quality_agent.core.investigation.model import RunStatus
from ambient_quality_agent.core.session_state import LaunchContext
from ambient_quality_agent.core.state import COUNTERS_KEY
from ambient_quality_agent.core.workflow import investigation_workflow
from ambient_quality_agent.telemetry import setup as telemetry_setup
from ambient_quality_agent.tools import gcs
from ambient_quality_agent.tools.investigations.models import (
    CUSTOM_TRIGGER_TYPE,
    CustomOverrides,
    InvestigationCounters,
)
from ambient_quality_agent.tools.observed_agent_config import effective_config
from google.adk import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

logger = logging.getLogger(__name__)

_DRY_RUN_CAP = 2
"""Tiny evaluation cap used for dry-run probes."""

_RUN_APP_NAME = "aqa-investigation"
_RUN_USER_ID = "orchestrator"


def _read_task_attempt() -> int:
    """Read Cloud Run Job task retry counter.

    Cloud Run Jobs set `CLOUD_RUN_TASK_ATTEMPT` to "0" on the first attempt and
    increment it on each automatic retry. Missing, empty, or non-integer values
    default to 0 to treat unknown environments as initial attempts.

    Returns:
        Task retry attempt count (0 outside Cloud Run or on initial attempt).
    """
    try:
        return int(os.environ.get("CLOUD_RUN_TASK_ATTEMPT", "0") or 0)
    except ValueError:
        return 0


async def execute_investigation(
    run_id: str, context: LaunchContext
) -> dict[str, Any]:
    """Execute a scheduled investigation run and record its terminal status.

    Executes the following sequence:
    1. Fetch the stored investigation record; fail if unknown.
    2. Claim the idempotency marker to prevent duplicate execution.
    3. Transition the run status to `RUNNING`.
    4. Execute the workflow graph with the submit-time configuration snapshot.
    5. Record the final status (`DONE`, `FAILED`, or `SKIPPED`) and summary.

    Args:
        run_id: Unique identifier of the investigation run.
        context: Launch context containing workflow state.

    Returns:
        Dictionary reporting execution status ('done', 'failed', or 'skipped')
        and associated run details.
    """
    state = context.state
    record = await model.get_run(state, run_id)
    if record is None:
        # Fail immediately and log because an unknown run cannot be tracked further.
        error = f"unknown run {run_id!r}"
        changes: dict[str, Any] = {"status": RunStatus.FAILED, "error": error}
        placeholder = model.InvestigationRecord(
            run_id=run_id,
            observed_agent_name=effective_config.load(
                state
            ).observed_agent_name,
        )
        _log_run_finished(placeholder, changes)
        return {"status": "failed", "error": error}

    logged = False

    def log_result_for_alerting(changes: dict[str, Any]) -> None:
        nonlocal logged
        if logged:
            return
        logged = True
        _log_run_finished(record, changes)

    try:
        return await _claim_marker_and_run(
            state, run_id, record, log_result_for_alerting
        )
    except BaseException as exc:
        # Graph-external failure handler: ensure cancellations or unexpected crashes
        # record a terminal FAILED state rather than leaving the run abandoned.
        changes = {
            "status": RunStatus.FAILED,
            "error": f"{type(exc).__name__}: {exc}",
        }
        log_result_for_alerting(changes)
        try:
            await model.update_run(state, run_id, **changes)
        except Exception:
            logger.exception("could not record the failure of run %s", run_id)
        raise


async def _claim_marker_and_run(
    state: Any,
    run_id: str,
    record: model.InvestigationRecord,
    log_result_for_alerting: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Claim the dedup marker, run the graph, and record the terminal run state.

    Args:
        state: Workflow state storing run records.
        run_id: Unique identifier of the investigation run.
        record: Stored investigation record.
        log_result_for_alerting: Callback to log terminal run result for alerting.

    Returns:
        Dictionary reporting workflow execution result.
    """
    # Atomically claim a GCS marker for the trigger dedup key to ensure
    # redelivered triggers exit without re-running the graph.
    key = record.idempotency_key
    bucket = record.effective_config.get("jobs_gcs_bucket") or ""
    if key and bucket:
        # Hash the key to produce a bounded, filesystem-safe object name (keys contain slashes/colons).
        digest = hashlib.sha256(key.encode()).hexdigest()
        object_name = f"aqa-idempotency-keys/{digest}.json"
        if not gcs.claim_once(bucket, object_name):
            # An existing marker on attempt 0 indicates a duplicate delivery (skip).
            # On attempt > 0, the marker was left by a crashed attempt and must re-run to avoid dropping the window.
            attempt = _read_task_attempt()
            if attempt == 0:
                skipped: dict[str, Any] = {
                    "status": RunStatus.SKIPPED,
                    "error": "duplicate trigger; skipped",
                }
                await model.update_run(state, run_id, **skipped)
                log_result_for_alerting(skipped)
                return {"status": "skipped", "reason": "duplicate"}
            logger.info(
                "crash-retry attempt %d for run %s; re-running despite existing marker",
                attempt,
                run_id,
            )

    await model.update_run(state, run_id, status=RunStatus.RUNNING)
    result = await run_investigation_graph(
        record.effective_config,
        run_id=record.run_id,
        window_start=record.window_start,
        window_end=record.window_end,
        budget_per_metric=record.budget_per_metric,
        dry_run=record.dry_run,
        trigger_type=record.trigger_type,
        custom_overrides=record.custom_overrides,
    )

    if result.get("status") == "done":
        changes = {
            "status": RunStatus.DONE,
            "summary": result.get("summary") or {},
            "counters": result.get("counters") or {},
        }
    else:
        changes = {
            "status": RunStatus.FAILED,
            "error": result.get("error", "unknown error"),
        }
    # The marker persists on completion and failure so subsequent deliveries deduplicate;
    # only container crash-retries re-execute.
    await model.update_run(state, run_id, **changes)
    log_result_for_alerting(changes)
    return result


_ALERT_COUNTS = (
    "insights_created",
    "insights_recurring",
    "traces_evaluated",
    "traces_eval_failed",
    "traces_eval_errored",
)
"""Counters extracted from investigation results for alert email bodies."""


def _truncate_error(error: object) -> str:
    """Shorten error text to 200 characters for email notifications.

    Returns empty string rather than None because Cloud Logging serializes
    JSON null as literal `NULL_VALUE`, which would appear in email alerts.

    Args:
        error: Error object or message to format.

    Returns:
        Truncated error string capped at 200 characters, or empty string if None.
    """
    if error is None:
        return ""
    text = str(error)
    return text if len(text) <= 200 else text[:200] + "…"


def _should_notify(
    status: object, counters: dict[str, Any], trigger_type: str = ""
) -> tuple[bool, str]:
    """Determine whether a completed run requires an email notification.

    Notifies on evaluation failures or API errors. Custom investigations are excluded
    from email notifications because they are interactively triggered by the user.

    In Python rather than the policy's filter, so it can be tested.

    Args:
        status: Terminal status of the investigation run.
        counters: Metric counters recorded during the run.
        trigger_type: Trigger identifier (e.g. 'custom', 'scheduled').

    Returns:
        Tuple of (should_notify, notification_reason).
    """
    if trigger_type == CUSTOM_TRIGGER_TYPE:
        return False, ""
    if status == RunStatus.SKIPPED:
        return False, ""
    if status != RunStatus.DONE:
        return True, "the investigation failed"
    new = counters.get("insights_created") or 0
    recurring = counters.get("insights_recurring") or 0
    errored = counters.get("traces_eval_errored") or 0
    if new:
        return True, f"{new} newly found problem(s)"
    if errored:
        return True, f"{errored} trace(s) the evaluator could not judge"
    if (counters.get("traces_eval_failed") or 0) and not recurring:
        # Unreported failure guard: send alert if failures were not grouped into insights.
        return True, "failed traces that could not be grouped"
    # Known recurring issues are omitted to avoid repetitive alert emails on every sweep.
    return False, ""


def _log_run_finished(
    record: model.InvestigationRecord, changes: dict[str, Any]
) -> None:
    """Write one structured log entry when a run finishes.

    Selected by alert policies to send email notifications. Never raises so
    logging errors do not mask successful run completion.

    Args:
        record: Target investigation record.
        changes: Terminal run updates containing status, summary, and counters.
    """
    try:
        # Use a copy because callers may log before database writes complete or lack stored records.
        view = model.format_run(record.model_copy(update=changes))
        summary = view.get("summary") or {}
        counters = view.get("counters") or {}
        # Default missing counts to zero because Cloud Logging extracts JSON null as literal 'NULL_VALUE'.
        counts = {name: counters.get(name) or 0 for name in _ALERT_COUNTS}
        notify, reason = _should_notify(
            view.get("status"), counters, str(view.get("trigger_type") or "")
        )
        logger.info(
            json.dumps(
                {
                    "event": "aqa_investigation_finished",
                    "run_id": view.get("run_id"),
                    "agent": view.get("observed_agent_name"),
                    "status": view.get("status"),
                    "error": _truncate_error(view.get("error")),
                    "window_start": view.get("window_start"),
                    "window_end": view.get("window_end"),
                    "should_notify": notify,
                    "notify_reason": reason,
                    **counts,
                    "pages": (summary.get("multi_turn_pages_evaluated") or 0)
                    + (summary.get("single_turn_pages_evaluated") or 0),
                },
                default=str,
            )
        )
    except Exception:
        logger.exception(
            "could not log run %s for alerting", getattr(record, "run_id", "?")
        )


async def run_investigation_graph(
    effective_config: dict[str, Any],
    *,
    run_id: str = "",
    window_start: str | None = None,
    window_end: str | None = None,
    budget_per_metric: int = 0,
    dry_run: bool = False,
    trigger_type: str = "",
    custom_overrides: CustomOverrides | None = None,
) -> dict[str, Any]:
    """Execute the investigation workflow graph and summarize results.

    Seeds execution configuration, time window, budget, trigger type, and custom
    overrides into workflow state, executes the workflow graph, and returns summary
    and counter outputs. `trigger_type` and `custom_overrides` are read off the
    persisted record by the caller: the durable dispatch carries only a run id
    across the boundary.

    Args:
        effective_config: Serialized configuration dictionary for this run.
        run_id: Unique identifier for the investigation run.
        window_start: Telemetry evaluation window start timestamp.
        window_end: Telemetry evaluation window end timestamp.
        budget_per_metric: Maximum evaluation budget allocated per metric.
        dry_run: Whether this is a lightweight verification run.
        trigger_type: Origin of the run (e.g. 'custom', 'scheduled', 'manual').
        custom_overrides: Custom conversation selector and review focus overrides.

    Returns:
        Dictionary with status ('done' or 'failed'), along with summary and counters
        on success or error message on failure. The counters are stored in a
        column of their own rather than inside the summary, so they can be summed
        across runs.
    """
    overrides = custom_overrides or CustomOverrides()
    try:
        if dry_run and overrides.is_custom():
            raise ValueError(job_scheduling.DRY_RUN_WITH_OVERRIDES)
        fields = dict(effective_config)
        if dry_run:
            fields["data_evaluation_cap"] = _DRY_RUN_CAP
        config = Config(**fields)

        if dry_run:
            # Re-derive window and budget because data_evaluation_cap was lowered for dry-run mode.
            window_start, window_end, budget_per_metric = (
                job_scheduling.derive_window_and_budget(config)
            )

        runner = Runner(
            app_name=_RUN_APP_NAME,
            node=investigation_workflow,
            session_service=InMemorySessionService(),
        )
        # Seed resolved inputs directly into state so init does not re-derive them.
        # Run ID is preserved across retries for idempotent persistence.
        seed_state = {
            **dataclasses.asdict(config),
            "run_id": run_id,
            "window_start": window_start,
            "window_end": window_end,
            "budget_per_metric": budget_per_metric,
            "trigger_type": trigger_type,
            "selector_sql": overrides.selector_sql,
            "session_review_focus": overrides.session_review_focus,
            "memory_baseline_mb": memory.read_cgroup_current_mb(),
        }
        session = await runner.session_service.create_session(
            app_name=_RUN_APP_NAME,
            user_id=_RUN_USER_ID,
            state=seed_state,
        )
        # Tag graph execution logs with run_id to correlate container log entries.
        _logging.install_run_id_logging()
        with _logging.tag_logs_with_run_id(run_id):
            async for _ in runner.run_async(
                user_id=_RUN_USER_ID,
                session_id=session.id,
                new_message=genai_types.Content(
                    role="user", parts=[genai_types.Part(text="run")]
                ),
            ):
                pass

        final = await runner.session_service.get_session(
            app_name=_RUN_APP_NAME, user_id=_RUN_USER_ID, session_id=session.id
        )
        final_state = dict(final.state) if final is not None else {}
        summary = _summarize(final_state, config)
        return {
            "status": "done",
            "summary": summary,
            "counters": _extract_counters(final_state),
        }
    except Exception as exc:
        logger.exception("Investigation run failed.")
        return {"status": "failed", "error": str(exc)}
    finally:
        # Flush telemetry spans immediately to prevent data loss before container shutdown.
        telemetry_setup.flush_spans()


def _extract_counters(state: dict[str, Any]) -> dict[str, int]:
    """Extract terminal stage counters from state as a complete dictionary.

    Validating against `InvestigationCounters` guarantees all expected keys are
    present with zero defaults, allowing aggregate queries to sum all metrics.

    Args:
        state: Terminal workflow state dictionary.

    Returns:
        Mapping of counter names to integer counts.
    """
    counters = state.get(COUNTERS_KEY) or {}
    return InvestigationCounters.model_validate(counters).model_dump()


def _summarize(state: dict[str, Any], config: Config) -> dict[str, Any]:
    """Build a compact summary from terminal workflow state.

    Collects execution progress events, evaluation outcome counts, and per-metric
    statistics into a single summary dictionary for status reporting.

    Args:
        state: Terminal workflow state dictionary.
        config: Effective agent configuration.

    Returns:
        Dictionary containing run window bounds, evaluated counts, metric
        summaries, and the goal the session review ran with
        (`WorkflowState.goal`), so runs can be compared by goal.
    """
    counts = state.get("eval_outcome_counts") or {}
    return {
        "observed_agent_name": config.observed_agent_name,
        "window_start": state.get("window_start"),
        "window_end": state.get("window_end"),
        "budget_per_metric": state.get("budget_per_metric"),
        "multi_turn_metrics": list(config.multi_turn_metrics),
        "single_turn_metrics": list(config.single_turn_metrics),
        "multi_turn_pages_evaluated": state.get(
            "multi_turn_pages_evaluated", 0
        ),
        "single_turn_pages_evaluated": state.get(
            "single_turn_pages_evaluated", 0
        ),
        "metrics_by_name": state.get("metrics_by_name") or {},
        "metrics_detail": state.get("metrics_detail") or {},
        "metrics_passed": counts.get("passed", 0),
        "metrics_failed": counts.get("failed", 0),
        "metrics_errored": counts.get("errored", 0),
        "goal": state.get("goal") or {},
        "events": list(state.get("progress_log") or []),
    }
