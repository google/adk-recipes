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

"""The investigation registry, over its BigQuery table.

The investigation table shared by scheduling and execution:
1. `create_investigation` creates a new investigation record with the initial configuration.
2. `update_investigation` in execution records the updated state of the investigation.

Every function here is async: synchronous BigQuery calls are offloaded to
threads with `asyncio.to_thread` to avoid blocking the ADK event loop.

This module also hosts the read tools (`list_investigations` /
`get_investigation`) that surface the registry to the agent, and `format_investigation`,
the presentation view both return.

Concurrency: an investigation is written as a full object; last writer wins.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from ambient_quality_agent import providers
from ambient_quality_agent.core.session_state import ADKStateLike, LaunchContext
from ambient_quality_agent.tools.investigations.models import (
    IN_FLIGHT_STATUSES,
    STAGE_EVENT_SOURCE,
    TERMINAL_STATUSES,
    Investigation,
    InvestigationCounters,
    InvestigationEvent,
    InvestigationStatus,
    StageProgress,
    list_snapshot_fields,
)
from ambient_quality_agent.tools.investigations.store import InvestigationStore
from pydantic import ValidationError

logger = logging.getLogger(__name__)

# Re-exported so callers keep importing the investigation vocabulary from the registry
# they use it with; the models live under `tools/` beside the store that
# persists them (as the insight models do).
__all__ = [
    "ADKStateLike",
    "Investigation",
    "InvestigationEvent",
    "InvestigationStatus",
    "create_investigation",
    "fail_overdue_scheduled_investigations",
    "fail_stale_pending_investigations",
    "format_investigation",
    "get_investigation",
    "get_investigation_stats",
    "get_stored_investigation",
    "list_investigations",
    "list_stored_investigations",
    "store_factory",
    "update_investigation",
]

_GCS_CONSOLE_BASE = "https://console.cloud.google.com/storage/browser/_details"
"""Base URL for the Cloud console object-details page (see `_build_gcs_console_url`)."""


store_factory: Callable[[ADKStateLike], InvestigationStore] = (
    providers.build_unbound_provider("store_factory")
)
"""Swappable seam; tests replace it with an in-memory store, and standalone mode
with `standalone.InMemoryInvestigationStore`.

Typed to `InvestigationStore`, the contract, rather than to the BigQuery class
that implements it -- so a store that keeps an investigation some other way is installable
here without inheriting machinery it will never call."""


async def create_investigation(
    state: ADKStateLike, record: Investigation
) -> Investigation:
    """Persists a new investigation with its input parameters and returns it.

    Args:
        state: State dictionary to access the investigation store.
        record: Investigation record to insert.

    Returns:
        The inserted `Investigation`.
    """
    await asyncio.to_thread(lambda: store_factory(state).append(record))
    return record


async def get_stored_investigation(
    state: ADKStateLike, investigation_id: str
) -> Investigation | None:
    """Returns one investigation with its events, or `None` if not found.

    Args:
        state: State dictionary to access the investigation store.
        investigation_id: Unique identifier of the investigation.

    Returns:
        The matching `Investigation`, or None if not found.
    """
    return await asyncio.to_thread(
        lambda: store_factory(state).get(investigation_id)
    )


async def list_stored_investigations(
    state: ADKStateLike,
    status: InvestigationStatus | None = None,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
) -> list[Investigation]:
    """Returns recent investigations ordered oldest first, excluding events.

    Args:
        state: State dictionary to access the investigation store.
        status: Optional filter by investigation status.
        window_start: Optional ISO instant filtering investigations whose window
            closed at or after it.
        window_end: Optional ISO instant filtering investigations whose window
            closed at or before it.

    Returns:
        List of recent `Investigation` records matching the criteria.
    """
    return await asyncio.to_thread(
        lambda: store_factory(state).list_recent(
            status=status, window_start=window_start, window_end=window_end
        )
    )


async def get_last_finished_window_end(
    state: ADKStateLike, agent_key: str
) -> str | None:
    """Returns the window end timestamp of the most recent finished ambient investigation.

    Args:
        state: State dictionary to access the investigation store.
        agent_key: Key the agent's investigations are recorded under.

    Returns:
        ISO-8601 window end timestamp string, or None if no completed ambient
        investigations exist.
    """
    return await asyncio.to_thread(
        lambda: store_factory(state).get_last_finished_window_end(agent_key)
    )


async def fail_stale_pending_investigations(
    state: ADKStateLike, *, lease_minutes: int
) -> list[str]:
    """Marks investigations left `pending` longer than `lease_minutes` as failed.

    Args:
        state: State dictionary to access the investigation store.
        lease_minutes: Maximum minutes an investigation may stay pending before
            failing.

    Returns:
        List of investigation IDs transitioned to `FAILED`.
    """
    if lease_minutes <= 0:
        return []
    stale_ids = await asyncio.to_thread(
        lambda: store_factory(state).list_stale_pending(
            lease_minutes=lease_minutes
        )
    )
    failed: list[str] = []
    for investigation_id in stale_ids:
        updated = await update_investigation(
            state,
            investigation_id,
            status=InvestigationStatus.FAILED,
            error=(
                f"left pending for over {lease_minutes} minute(s) with no update "
                "(a crashed job or a lost long-running query)"
            ),
        )
        if updated is not None:
            failed.append(investigation_id)
    return failed


async def fail_overdue_scheduled_investigations(
    state: ADKStateLike, *, grace_minutes: int, keep: str | None = None
) -> list[str]:
    """Marks scheduled investigations whose trigger is overdue by over `grace_minutes` as failed.

    A scheduled investigation waits for a delayed Cloud Task. If that task was
    deleted or never fired, nothing else would transition the investigation out
    of `SCHEDULED`.

    Args:
        state: State dictionary to access the investigation store.
        grace_minutes: Minutes past `due_at` a scheduled investigation may wait.
        keep: Investigation ID to leave alone (the one whose trigger is firing
            right now, however late).

    Returns:
        List of investigation IDs transitioned to `FAILED`.
    """
    overdue_ids = await asyncio.to_thread(
        lambda: store_factory(state).list_overdue_scheduled(
            grace_minutes=grace_minutes
        )
    )
    failed: list[str] = []
    for investigation_id in overdue_ids:
        if investigation_id == keep:
            continue
        updated = await update_investigation(
            state,
            investigation_id,
            status=InvestigationStatus.FAILED,
            error=(
                f"its delayed trigger did not fire within {grace_minutes} "
                "minute(s) of the due time (the Cloud Task was deleted or "
                "never dispatched)"
            ),
        )
        if updated is not None:
            failed.append(investigation_id)
    return failed


async def update_investigation(
    state: ADKStateLike, investigation_id: str, **changes: Any
) -> Investigation | None:
    """Applies updates to an existing investigation and returns the updated record.

    Reads the investigation first; updating a non-existent investigation is a
    no-op returning None. When status changes to a terminal state, sets
    `finished_at` automatically unless explicitly provided.

    Args:
        state: State dictionary to access the investigation store.
        investigation_id: Unique identifier of the investigation.
        **changes: Keyword arguments matching `Investigation` field names.

    Returns:
        Updated `Investigation`, or None if `investigation_id` was not found.

    Raises:
        ValueError: If a change key does not match a known snapshot field.
        pydantic.ValidationError: If an update value fails schema validation.
    """
    unknown = set(changes) - set(list_snapshot_fields())
    if unknown:
        raise ValueError(
            f"cannot persist unknown investigation field(s): {sorted(unknown)}"
        )
    return await asyncio.to_thread(
        lambda: _apply_and_append(state, investigation_id, changes)
    )


def _apply_and_append(
    state: ADKStateLike, investigation_id: str, changes: dict[str, Any]
) -> Investigation | None:
    """Reads an investigation, applies updates, and appends the newest snapshot.

    Args:
        state: State dictionary to access the investigation store.
        investigation_id: Unique identifier of the investigation.
        changes: Dictionary of validated field updates.

    Returns:
        Updated `Investigation`, or None if the investigation was not found.
    """
    store = store_factory(state)
    # Events are not fetched: they are rows of their own and are never rewritten
    # by a snapshot, so carrying them through the update would be dead weight.
    record = store.get(investigation_id, include_events=False)
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
    updated = Investigation.model_validate({**record.model_dump(), **changes})
    store.append(updated)
    return updated


def format_investigation(
    record: Investigation, stage_event: InvestigationEvent | None = None
) -> dict[str, Any]:
    """Build a compact, chat-friendly dictionary view of an investigation record.

    Derives presentation fields such as `report_console_url`, lifts outcome tallies
    to the top level for dashboards, and populates zero-default counter maps.

    Stage events say how far a running investigation got, and only the newest
    says anything: `events` and the summary leave them out, and `progress`
    carries the newest for as long as the investigation is in flight.

    Args:
        record: Source investigation record.
        stage_event: The investigation's newest stage event, for a record read
            without its events; a record read with them supplies its own.

    Returns:
        Serialized dictionary representation of the investigation record.
    """
    events = [e for e in record.events if e.source != STAGE_EVENT_SOURCE]
    stages = [e for e in record.events if e.source == STAGE_EVENT_SOURCE]
    if stage_event is None and stages:
        stage_event = stages[-1]
    elapsed: float | None = None
    if record.finished_at is not None:
        elapsed = (
            dt.datetime.fromisoformat(record.finished_at)
            - dt.datetime.fromisoformat(record.created_at)
        ).total_seconds()
    summary = record.summary or {}
    return {
        "investigation_id": record.investigation_id,
        "status": record.status,
        "observed_agent_key": record.observed_agent_key,
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
        "events": [e.model_dump(mode="json") for e in events],
        "progress": _format_progress(stage_event, record.investigation_id)
        if stage_event is not None and record.status in IN_FLIGHT_STATUSES
        else None,
        "summary": _format_summary(record, events),
        "error": record.error,
        "idempotency_key": record.idempotency_key,
    }


def _format_progress(
    stage_event: InvestigationEvent, investigation_id: str
) -> dict[str, Any] | None:
    """Reads how far an investigation got off its newest stage event.

    Args:
        stage_event: The stage event, whose text is a `StageProgress` in JSON.
        investigation_id: The investigation it belongs to, for the log: a
            copy nested in a record carries none of its own.

    Returns:
        The progress's fields, those it has, and the time it was written as
        ``updated_at``; None for an event whose text is not a progress record.
    """
    try:
        progress = StageProgress.model_validate_json(stage_event.text)
    except ValidationError:
        logger.warning(
            "investigations: skipped a stage event of %s that is not a "
            "progress record",
            investigation_id,
        )
        return None
    return {
        **progress.model_dump(exclude_none=True),
        "updated_at": stage_event.created_at,
    }


def _format_summary(
    record: Investigation, events: list[InvestigationEvent]
) -> dict[str, Any] | None:
    """Format stored summary with progress events and console URL.

    Args:
        record: Target investigation record.
        events: The record's events, less its stage events.

    Returns:
        Summary dictionary with reattached events and console URL, or None if empty.
    """
    summary = record.summary
    texts = [e.text for e in events]
    if not summary:
        return {"events": texts} if texts else summary
    view = dict(summary)
    if texts:
        view["events"] = texts
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
    """Returns recent investigations with their summaries and without events.

    The page limit applies after window filtering, so a wide period returns its
    most recent page; use `get_investigation_stats` to aggregate across an
    entire period.

    An investigation still pending or running carries `progress` once it has
    reported a stage, and null before: the stage it is in and its counts so
    far, such as
    ``{"stage": "Session review", "evaluated": 45, "total": 205,
    "findings": 12, "updated_at": ...}``.

    Args:
        tool_context: Launch context providing access to execution state.
        window_start: ISO instant lower bound on `window_end`; empty for no
            lower bound.
        window_end: ISO instant upper bound on `window_end`; empty for no upper
            bound.

    Returns:
        Dictionary with ``investigations`` list, or ``error`` if the read fails.
    """
    try:
        records = await list_stored_investigations(
            tool_context.state,
            window_start=window_start or None,
            window_end=window_end or None,
        )
    except Exception as exc:
        return {
            "investigations": [],
            "error": f"failed to list investigations: {exc}",
        }
    in_flight = [
        r.investigation_id for r in records if r.status in IN_FLIGHT_STATUSES
    ]
    try:
        stages = (
            await asyncio.to_thread(
                lambda: store_factory(
                    tool_context.state
                ).list_latest_stage_events(in_flight)
            )
            if in_flight
            else []
        )
    except Exception:
        # A row reads well without its stage, so losing them must not lose the
        # list.
        logger.warning(
            "investigations: could not read the stage events", exc_info=True
        )
        stages = []
    by_id = {e.investigation_id: e for e in stages}
    return {
        "investigations": [
            format_investigation(r, by_id.get(r.investigation_id))
            for r in records
        ]
    }


async def get_investigation_stats(
    tool_context: LaunchContext,
    *,
    window_start: str = "",
    window_end: str = "",
) -> dict[str, Any]:
    """Returns counter totals summed across all matching investigations.

    Unlike `list_investigations`, sums across all matching rows in the store
    rather than a single page. ``window`` reports both the requested bounds
    (``start``, ``end``) and the ``window_end`` span actually covered by the
    counted investigations (``covered_start``, ``covered_end``).

    Args:
        tool_context: Launch context providing access to execution state.
        window_start: ISO instant lower bound on `window_end`; empty for no
            lower bound.
        window_end: ISO instant upper bound on `window_end`; empty for no upper
            bound.

    Returns:
        Dictionary with ``stats`` (one entry per `InvestigationCounters` field
        plus ``investigations``) and ``window``.
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
    """Returns daily aggregated counter totals for a telemetry window.

    Aggregates across all matching investigations without pagination limits.
    Days with no investigations are omitted so callers can distinguish inactive
    days from days with zero findings; each returned day includes an
    `unmeasured` count of investigations that recorded all-zero counters.

    Args:
        tool_context: Launch context providing access to execution state.
        window_start: Optional ISO instant lower bound on `window_end`.
        window_end: Optional ISO instant upper bound on `window_end`.

    Returns:
        Dictionary containing ``days`` list of daily aggregated counter dicts.
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
    investigation_id: str, tool_context: LaunchContext
) -> dict[str, Any]:
    """Returns the formatted investigation record and progress events for `investigation_id`.

    While the investigation is pending or running, `progress` says how far it
    got, once it has reported a stage.

    Args:
        investigation_id: Unique identifier of the investigation.
        tool_context: Launch context providing access to execution state.

    Returns:
        Formatted investigation dictionary, or an ``{"error": ...}`` dictionary
        if `investigation_id` is empty, not found, or the read fails.
    """
    if not investigation_id:
        return {"error": "investigation_id is required"}
    try:
        record = await get_stored_investigation(
            tool_context.state, investigation_id
        )
    except Exception as exc:
        return {
            "error": f"failed to get investigation {investigation_id!r}: {exc}"
        }
    if record is None:
        return {
            "error": f"No investigation found with id {investigation_id!r}."
        }
    return format_investigation(record)
