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

"""The investigation run registry, over its BigQuery table.

The investigation table shared by scheduling and execution:
1. `create_run` creates a new investigation record with the initial configuration.
2. `update_run` in execution records the updated state of the investigation.

Every function here is async: synchronous BigQuery calls are offloaded to
threads with `asyncio.to_thread` to avoid blocking the ADK event loop.

This module also hosts the read tools (`list_investigations` /
`get_investigation`) that surface the registry to the agent, and `format_run`,
the presentation view both return.

Concurrency: an investigation is written as a full object; last writer wins.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from ambient_quality_agent import providers
from ambient_quality_agent.core.session_state import ADKStateLike, LaunchContext
from ambient_quality_agent.tools.investigations.models import (
    TERMINAL_STATUSES,
    InvestigationCounters,
    InvestigationEvent,
    InvestigationRecord,
    RunStatus,
    list_snapshot_columns,
)
from ambient_quality_agent.tools.investigations.store import InvestigationStore

# Re-exported so callers keep importing the run vocabulary from the registry
# they use it with; the models live under `tools/` beside the store that
# persists them (as the insight models do).
__all__ = [
    "ADKStateLike",
    "InvestigationEvent",
    "InvestigationRecord",
    "RunStatus",
    "create_run",
    "fail_overdue_scheduled_runs",
    "fail_stale_pending_runs",
    "format_run",
    "get_investigation",
    "get_investigation_stats",
    "get_run",
    "list_investigations",
    "list_runs",
    "store_factory",
    "update_run",
]

_GCS_CONSOLE_BASE = "https://console.cloud.google.com/storage/browser/_details"
"""Base URL for the Cloud console object-details page (see `_build_gcs_console_url`)."""


store_factory: Callable[[ADKStateLike], InvestigationStore] = (
    providers.build_unbound_provider("store_factory")
)
"""Swappable seam; tests replace it with an in-memory store, and standalone mode
with `standalone.InMemoryInvestigationStore`.

Typed to `InvestigationStore`, the contract, rather than to the BigQuery class
that implements it -- so a store that keeps a run some other way is installable
here without inheriting machinery it will never call."""


async def create_run(
    state: ADKStateLike, record: InvestigationRecord
) -> InvestigationRecord:
    """Record a new run with its input parameters and return it.

    Args:
        state: State dictionary to access the investigation store.
        record: Investigation record to insert.

    Returns:
        The inserted `InvestigationRecord`.
    """
    await asyncio.to_thread(lambda: store_factory(state).append(record))
    return record


async def get_run(
    state: ADKStateLike, run_id: str
) -> InvestigationRecord | None:
    """Return one run with its events, or `None` if the run is not found.

    Args:
        state: State dictionary to access the investigation store.
        run_id: Unique identifier of the run.

    Returns:
        The matching `InvestigationRecord`, or None if not found.
    """
    return await asyncio.to_thread(lambda: store_factory(state).get(run_id))


async def list_runs(
    state: ADKStateLike,
    status: RunStatus | None = None,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
) -> list[InvestigationRecord]:
    """Return recent runs, ordered newest last, excluding event details.

    Args:
        state: State dictionary to access the investigation store.
        status: Optional filter by run status.
        window_start: Optional ISO instant filtering runs whose window closed at or after it.
        window_end: Optional ISO instant filtering runs whose window closed at or before it.

    Returns:
        List of recent `InvestigationRecord` summaries matching the criteria.
    """
    return await asyncio.to_thread(
        lambda: store_factory(state).list_recent(
            status=status, window_start=window_start, window_end=window_end
        )
    )


async def get_last_finished_window_end(
    state: ADKStateLike, observed_agent_name: str
) -> str | None:
    """Return window end timestamp of the most recent finished ambient run.

    Args:
        state: State dictionary to access the investigation store.
        observed_agent_name: Name of the observed target agent.

    Returns:
        ISO-8601 window end timestamp string, or None if no completed runs exist.
    """
    return await asyncio.to_thread(
        lambda: store_factory(state).get_last_finished_window_end(
            observed_agent_name
        )
    )


async def fail_stale_pending_runs(
    state: ADKStateLike, *, lease_minutes: int
) -> list[str]:
    """Mark runs left `pending` longer than `lease_minutes` as failed.

    Args:
        state: State dictionary to access the investigation store.
        lease_minutes: Maximum minutes a run may stay pending before failing.

    Returns:
        List of run IDs transitioned to `FAILED`.
    """
    if lease_minutes <= 0:
        return []
    stale_ids = await asyncio.to_thread(
        lambda: store_factory(state).list_stale_pending(
            lease_minutes=lease_minutes
        )
    )
    failed: list[str] = []
    for run_id in stale_ids:
        updated = await update_run(
            state,
            run_id,
            status=RunStatus.FAILED,
            error=(
                f"left pending for over {lease_minutes} minute(s) with no update "
                "(a crashed job or a lost long-running query)"
            ),
        )
        if updated is not None:
            failed.append(run_id)
    return failed


async def fail_overdue_scheduled_runs(
    state: ADKStateLike, *, grace_minutes: int, keep: str | None = None
) -> list[str]:
    """Mark scheduled runs whose trigger is overdue by over `grace_minutes` as failed.

    A scheduled run waits for a delayed Cloud Task. If that task was deleted, or
    never fired, nothing else would ever move the run on.

    Args:
        state: State dictionary to access the investigation store.
        grace_minutes: Minutes past `due_at` a scheduled run may wait.
        keep: Run ID to leave alone: the one whose trigger is firing right now,
            however late.

    Returns:
        List of run IDs transitioned to `FAILED`.
    """
    overdue_ids = await asyncio.to_thread(
        lambda: store_factory(state).list_overdue_scheduled(
            grace_minutes=grace_minutes
        )
    )
    failed: list[str] = []
    for run_id in overdue_ids:
        if run_id == keep:
            continue
        updated = await update_run(
            state,
            run_id,
            status=RunStatus.FAILED,
            error=(
                f"its delayed trigger did not fire within {grace_minutes} "
                "minute(s) of the due time (the Cloud Task was deleted or "
                "never dispatched)"
            ),
        )
        if updated is not None:
            failed.append(run_id)
    return failed


async def update_run(
    state: ADKStateLike, run_id: str, **changes: Any
) -> InvestigationRecord | None:
    """Apply updates to an existing run record and return the updated record.

    Reads the run first; updating a non-existent run is a no-op returning None.
    When status changes to a terminal state, sets `finished_at` automatically
    unless explicitly provided.

    Args:
        state: State dictionary to access the investigation store.
        run_id: Unique identifier of the run.
        **changes: Keyword arguments matching `InvestigationRecord` field names.

    Returns:
        Updated `InvestigationRecord`, or None if run_id was not found.

    Raises:
        ValueError: If a change key does not match a known snapshot column.
        pydantic.ValidationError: If an update value fails schema validation.
    """
    unknown = set(changes) - set(list_snapshot_columns())
    if unknown:
        raise ValueError(
            f"cannot persist unknown investigation field(s): {sorted(unknown)}"
        )
    return await asyncio.to_thread(
        lambda: _apply_and_append(state, run_id, changes)
    )


def _apply_and_append(
    state: ADKStateLike, run_id: str, changes: dict[str, Any]
) -> InvestigationRecord | None:
    """Read run, apply updates, and store newest snapshot synchronously.

    Args:
        state: State dictionary to access the investigation store.
        run_id: Unique identifier of the run.
        changes: Dictionary of validated field updates.

    Returns:
        Updated `InvestigationRecord`, or None if the run was not found.
    """
    store = store_factory(state)
    # Events are not fetched: they are rows of their own and are never rewritten
    # by a snapshot, so carrying them through the update would be dead weight.
    record = store.get(run_id, include_events=False)
    if record is None:
        return None
    if (
        changes.get("status") in TERMINAL_STATUSES
        and "finished_at" not in changes
    ):
        changes = {
            **changes,
            "finished_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        }
    # Revalidated, not `model_copy(update=...)`: that assigns a caller's plain
    # dict into a typed field, and pydantic only warns, at write time.
    updated = InvestigationRecord.model_validate(
        {**record.model_dump(), **changes}
    )
    store.append(updated)
    return updated


def format_run(record: InvestigationRecord) -> dict[str, Any]:
    """Build a compact, chat-friendly dictionary view of an investigation record.

    Derives presentation fields such as `report_console_url`, lifts outcome tallies
    to the top level for dashboards, and populates zero-default counter maps.

    Args:
        record: Source investigation record.

    Returns:
        Serialized dictionary representation of the run record.
    """
    elapsed: float | None = None
    if record.finished_at is not None:
        elapsed = (
            dt.datetime.fromisoformat(record.finished_at)
            - dt.datetime.fromisoformat(record.created_at)
        ).total_seconds()
    summary = record.summary or {}
    return {
        "run_id": record.run_id,
        "status": record.status,
        "observed_agent_name": record.observed_agent_name,
        "trigger_type": record.trigger_type,
        "window_start": record.window_start,
        "window_end": record.window_end,
        "due_at": record.due_at,
        "budget_per_metric": record.budget_per_metric,
        "custom_overrides": record.custom_overrides.model_dump()
        if record.custom_overrides
        else None,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "finished_at": record.finished_at,
        "elapsed_seconds": elapsed,
        "rca_status": summary.get("rca_status"),
        "metrics_passed": summary.get("metrics_passed"),
        "metrics_failed": summary.get("metrics_failed"),
        "metrics_errored": summary.get("metrics_errored"),
        "counters": InvestigationCounters.model_validate(
            record.counters
        ).model_dump(),
        "events": [e.model_dump(mode="json") for e in record.events],
        "summary": _format_summary(record),
        "error": record.error,
        "idempotency_key": record.idempotency_key,
    }


def _format_summary(record: InvestigationRecord) -> dict[str, Any] | None:
    """Format stored summary with progress events and console URL.

    Args:
        record: Target investigation record.

    Returns:
        Summary dictionary with reattached events and console URL, or None if empty.
    """
    summary = record.summary
    events = [e.text for e in record.events]
    if not summary:
        return {"events": events} if events else summary
    view = dict(summary)
    if events:
        view["events"] = events
    if console_url := _build_gcs_console_url(
        summary.get("report_gcs_uri") or ""
    ):
        view["report_console_url"] = console_url
    return view


def _build_gcs_console_url(gcs_uri: str) -> str:
    """Convert a `gs://bucket/object` URI to a Cloud console browser URL.

    Args:
        gcs_uri: Google Cloud Storage URI string.

    Returns:
        Google Cloud console object details URL, or empty string if invalid.
    """
    parsed = urlparse(gcs_uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        return ""
    object_path = parsed.path.lstrip("/")
    return f"{_GCS_CONSOLE_BASE}/{parsed.netloc}/{object_path}"


# --------------------------------------------------------------------------- #
# Chat-facing read tools                                                       #
# --------------------------------------------------------------------------- #


async def list_investigations(
    tool_context: LaunchContext,
    *,
    window_start: str = "",
    window_end: str = "",
) -> dict[str, Any]:
    """Return the recent investigation runs, with their summary but no events.

    Args:
        window_start: ISO instant. Keep only runs whose telemetry window closed
            at or after it; empty for no lower bound.
        window_end: ISO instant. Keep only runs whose window closed at or before
            it; empty for no upper bound.

    The page limit applies after the window, so a wide period returns its most
    recent page rather than all of it -- see `get_investigation_stats` for
    figures that cover a period whole.
    """
    try:
        records = await list_runs(
            tool_context.state,
            window_start=window_start or None,
            window_end=window_end or None,
        )
    except Exception as exc:
        return {"runs": [], "error": f"failed to list investigations: {exc}"}
    return {"runs": [format_run(r) for r in records]}


async def get_investigation_stats(
    tool_context: LaunchContext,
    *,
    window_start: str = "",
    window_end: str = "",
) -> dict[str, Any]:
    """Return the deployment's totals: runs' counters, summed.

    Args:
        window_start: ISO instant. Keep only runs whose telemetry window closed
            at or after it; empty for no lower bound.
        window_end: ISO instant. Keep only runs whose window closed at or before
            it; empty for no upper bound.

    Returns ``{"stats": {"investigations": N, "traces_scanned": N, ...},
    "window": {"start": ..., "end": ..., "covered_start": ..., "covered_end": ...}}``
    -- one figure per `InvestigationCounters` field, plus how many runs
    produced them. Unlike `list_investigations` this reads the whole table rather
    than a page of it, so the totals never quietly describe the fifty most
    recent runs; with a window that is also the only way to total a period
    truthfully, for the same reason.

    ``window`` reports both the bounds that were *asked for* and the span the
    counted runs actually *cover*. The two rarely match -- a week's window over
    a three-day-old deployment covers three days -- and a caller that labels its
    figures with the request rather than the coverage claims a quiet fortnight
    where there was only an empty one.
    """
    window: dict[str, Any] = {
        "start": window_start or None,
        "end": window_end or None,
        "covered_start": None,
        "covered_end": None,
    }
    try:
        stats, covered = await asyncio.to_thread(
            lambda: store_factory(tool_context.state).sum_counters(
                window_start=window_start or None, window_end=window_end or None
            )
        )
    except Exception as exc:
        return {
            "stats": {},
            "window": window,
            "error": f"failed to total the investigations: {exc}",
        }
    window["covered_start"] = covered.get("start")
    window["covered_end"] = covered.get("end")
    return {"stats": stats, "window": window}


async def get_daily_trends(
    tool_context: LaunchContext,
    *,
    window_start: str = "",
    window_end: str = "",
) -> dict[str, Any]:
    """Return daily aggregated counter metrics for a specified period.

    Aggregated in BigQuery to prevent pagination distortion. Omits days with no
    sweeps: a caller drawing a fixed range knows which days it means to show,
    and a zero row could not be told apart from a day nothing ran. Each day
    reports its `unmeasured` count separately.

    Args:
        tool_context: Launch context providing access to execution state.
        window_start: Optional ISO instant lower bound for closed telemetry windows.
        window_end: Optional ISO instant upper bound for closed telemetry windows.

    Returns:
        Dictionary containing 'days' list of daily aggregated metric records.
    """
    try:
        days = await asyncio.to_thread(
            lambda: store_factory(tool_context.state).sum_counters_by_day(
                window_start=window_start or None, window_end=window_end or None
            )
        )
    except Exception as exc:
        return {"days": [], "error": f"failed to read the daily trends: {exc}"}
    return {"days": days}


async def get_investigation(
    run_id: str, tool_context: LaunchContext
) -> dict[str, Any]:
    """Returns the summary and node events for an investigation run.

    Each event includes the time it was emitted. Returns an error dictionary if
    `run_id` is empty, no run has that id, or the read fails.
    """
    if not run_id:
        return {"error": "run_id is required"}
    try:
        record = await get_run(tool_context.state, run_id)
    except Exception as exc:
        return {"error": f"failed to get investigation {run_id!r}: {exc}"}
    if record is None:
        return {"error": f"No run found with id {run_id!r}."}
    return format_run(record)
