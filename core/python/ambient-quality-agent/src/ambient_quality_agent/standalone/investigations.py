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

"""Adapts `InMemoryStore` runs to implement the `InvestigationStore` contract.

Provides in-memory storage for the investigation scheduler, workflow event
writer, chat tools, and dashboard.

Each run is stored as a single mutable record representing its current state.
Filtering, ordering, and windowing rules match the production contract:
1. Filter telemetry windows based on window closure time (`window_end`) rather
   than run execution time.
2. Return recent run lists capped and sorted oldest first.
3. Exclude inactive days from daily aggregation rather than outputting zeros.
4. Exclude dry runs and manual triggers when calculating the ambient watermark.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from ambient_quality_agent.tools.investigations.models import (
    IN_FLIGHT_STATUSES,
    NON_AMBIENT_TRIGGER_TYPES,
    InvestigationCounters,
    InvestigationEvent,
    RunStatus,
    list_counter_names,
)
from ambient_quality_agent.tools.investigations.store import (
    DEFAULT_LIST_LIMIT,
    IN_FLIGHT_HORIZON_HOURS,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ambient_quality_agent.standalone.store import InMemoryStore
    from ambient_quality_agent.tools.investigations.models import (
        InvestigationRecord,
    )

logger = logging.getLogger(__name__)


def _format_utc_now_iso() -> str:
    return dt.datetime.now(tz=dt.UTC).isoformat()


class InMemoryInvestigationStore:
    """One deployment's runs, over the in-process `InMemoryStore`."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    # --- Writes ------------------------------------------------------------- #

    def append(self, record: InvestigationRecord) -> InvestigationRecord:
        """Saves a run record as the current state and returns the stored copy.

        Updates `updated_at` to the current timestamp and clears inline events;
        events are managed separately via `append_event`.

        Args:
            record: Investigation run state to store.

        Returns:
            The persisted `InvestigationRecord` with updated timestamp and empty events.
        """
        stored = record.model_copy(
            update={"updated_at": _format_utc_now_iso(), "events": []}
        )
        self._store.save_run(stored)
        return stored

    def append_event(self, run_id: str, text: str, *, source: str = "") -> None:
        """Appends a progress event to an investigation run.

        Args:
            run_id: Investigation run ID to attach the event to.
            text: Event message text.
            source: Component or node identifier that generated the event.
        """
        self._store.append_run_event(
            run_id,
            InvestigationEvent(run_id=run_id, text=text, source=source or None),
        )

    # --- Reads -------------------------------------------------------------- #

    def get(
        self, run_id: str, *, include_events: bool = True
    ) -> InvestigationRecord | None:
        """Retrieves a run by ID.

        Args:
            run_id: Investigation run identifier.
            include_events: Whether to populate the run's event history.

        Returns:
            The `InvestigationRecord` if found, or None.
        """
        record = self._store.runs.get(run_id)
        if record is None:
            return None
        if not include_events:
            return record
        return record.model_copy(
            update={"events": list(self._store.list_run_events(run_id))}
        )

    def list_recent(
        self,
        *,
        status: RunStatus | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> list[InvestigationRecord]:
        """Lists the most recently created runs up to limit, ordered oldest first.

        Selects the most recent runs by creation time and returns them in ascending
        chronological order without event details.

        Args:
            status: Optional run status to filter by.
            limit: Maximum number of recent runs to return.
            window_start: Optional ISO timestamp lower bound for `window_end`.
            window_end: Optional ISO timestamp upper bound for `window_end`.

        Returns:
            List of `InvestigationRecord` objects matching the criteria.
        """
        kept = [
            record
            for record in self._list_runs_in_window(
                self._store.runs.values(), window_start, window_end
            )
            if status is None or record.status == status
        ]
        kept.sort(key=lambda r: r.created_at, reverse=True)
        return list(reversed(kept[:limit]))

    def sum_counters(
        self, *, window_start: str | None = None, window_end: str | None = None
    ) -> tuple[dict[str, int], dict[str, str | None]]:
        """Sums telemetry counters across matching runs and computes their covered time span.

        Args:
            window_start: Optional ISO timestamp lower bound for `window_end`.
            window_end: Optional ISO timestamp upper bound for `window_end`.

        Returns:
            A tuple of:
            - Dict mapping counter names and 'investigations' to aggregated counts.
            - Dict with 'start' and 'end' bounds of the covered telemetry windows.
        """
        kept = list(
            self._list_runs_in_window(
                self._store.runs.values(), window_start, window_end
            )
        )
        totals = dict.fromkeys(list_counter_names(), 0)
        for record in kept:
            for name, value in _extract_counters(record).items():
                totals[name] += value
        windows = sorted(r.window_end for r in kept if r.window_end)
        covered = {
            "start": windows[0] if windows else None,
            "end": windows[-1] if windows else None,
        }
        return {"investigations": len(kept), **totals}, covered

    def sum_counters_by_day(
        self, *, window_start: str | None = None, window_end: str | None = None
    ) -> list[dict[str, int | str]]:
        """Aggregates telemetry counters by UTC calendar day of the telemetry window.

        Runs lacking a `window_end` timestamp are excluded from daily buckets.

        Args:
            window_start: Optional ISO timestamp lower bound for `window_end`.
            window_end: Optional ISO timestamp upper bound for `window_end`.

        Returns:
            List of daily metric dictionaries sorted chronologically by day.
        """
        names = list_counter_names()
        buckets: dict[str, dict[str, int | str]] = {}
        for record in self._list_runs_in_window(
            self._store.runs.values(), window_start, window_end
        ):
            day = _compute_utc_day(record.window_end)
            if day is None:
                continue
            bucket = buckets.setdefault(
                day,
                {
                    "day": day,
                    "investigations": 0,
                    "unmeasured": 0,
                    **dict.fromkeys(names, 0),
                },
            )
            counters = _extract_counters(record)
            bucket["investigations"] = int(bucket["investigations"]) + 1
            if not sum(counters.values()):
                bucket["unmeasured"] = int(bucket["unmeasured"]) + 1
            for name, value in counters.items():
                bucket[name] = int(bucket[name]) + value
        return [buckets[day] for day in sorted(buckets)]

    def get_last_finished_window_end(
        self, observed_agent_name: str
    ) -> str | None:
        """Returns the latest telemetry window end timestamp for finished ambient runs.

        Excludes dry runs and non-ambient triggers (`NON_AMBIENT_TRIGGER_TYPES`) so
        custom or manual sweeps do not advance or reset the ambient watermark.

        Args:
            observed_agent_name: Name of the observed agent.

        Returns:
            ISO 8601 timestamp of the latest completed window end, or None if no
            completed ambient run exists.
        """
        ends = [
            record.window_end
            for record in self._store.runs.values()
            if record.observed_agent_name == observed_agent_name
            and record.status == RunStatus.DONE
            and record.trigger_type not in NON_AMBIENT_TRIGGER_TYPES
            and not record.dry_run
            and record.window_end
        ]
        # UTC ISO timestamps sort lexicographically in chronological order.
        return max(ends) if ends else None

    def count_in_flight(self, observed_agent_name: str) -> int:
        """Counts active in-flight investigation runs for an agent.

        Runs untouched for longer than `IN_FLIGHT_HORIZON_HOURS` are excluded
        so abandoned or crashed executions do not permanently block concurrency.

        Args:
            observed_agent_name: Name of the observed agent.

        Returns:
            Number of pending or running runs updated within the horizon.
        """
        since = dt.datetime.now(tz=dt.UTC) - dt.timedelta(
            hours=IN_FLIGHT_HORIZON_HOURS
        )
        return sum(
            1
            for record in self._store.runs.values()
            if record.observed_agent_name == observed_agent_name
            and record.status in IN_FLIGHT_STATUSES
            and (stamped := _parse_instant(record.updated_at)) is not None
            and stamped >= since
        )

    def list_stale_pending(self, *, lease_minutes: int) -> list[str]:
        """Lists run IDs remaining in pending status beyond lease expiration.

        Args:
            lease_minutes: Maximum duration in minutes a run may remain pending before being considered stale.

        Returns:
            List of stale pending run IDs.
        """
        deadline = dt.datetime.now(tz=dt.UTC) - dt.timedelta(
            minutes=lease_minutes
        )
        return [
            record.run_id
            for record in self._store.runs.values()
            if record.status == RunStatus.PENDING
            and (stamped := _parse_instant(record.updated_at)) is not None
            and stamped < deadline
        ]

    def list_overdue_scheduled(self, *, grace_minutes: int) -> list[str]:
        """Lists scheduled run IDs whose trigger is overdue beyond the grace.

        Args:
            grace_minutes: Minutes past `due_at` a scheduled run may wait for its
                trigger before it is considered lost.

        Returns:
            List of overdue scheduled run IDs.
        """
        deadline = dt.datetime.now(tz=dt.UTC) - dt.timedelta(
            minutes=grace_minutes
        )
        return [
            record.run_id
            for record in self._store.runs.values()
            if record.status == RunStatus.SCHEDULED
            and (due := _parse_instant(record.due_at)) is not None
            and due < deadline
        ]

    # --- Shared rules -------------------------------------------------------- #

    @staticmethod
    def _list_runs_in_window(
        records: Iterable[InvestigationRecord],
        window_start: str | None,
        window_end: str | None,
    ) -> list[InvestigationRecord]:
        """Filters runs whose telemetry window closed within the specified bounds.

        Evaluates against `window_end` (the period covered by the sweep). Runs
        without a `window_end` are excluded when bounds are provided.

        Args:
            records: Iterable of investigation records to filter.
            window_start: Optional ISO timestamp lower bound.
            window_end: Optional ISO timestamp upper bound.

        Returns:
            List of matching `InvestigationRecord` objects.
        """
        if window_start is None and window_end is None:
            return list(records)
        kept = []
        for record in records:
            closed = _parse_instant(record.window_end)
            if closed is None:
                continue
            if window_start is not None and closed < _require(
                _parse_instant(window_start)
            ):
                continue
            if window_end is not None and closed > _require(
                _parse_instant(window_end)
            ):
                continue
            kept.append(record)
        return kept


def _extract_counters(record: InvestigationRecord) -> dict[str, int]:
    """Extracts a complete mapping of counter names to values, including zeros.

    Args:
        record: Investigation record containing counter data.

    Returns:
        Mapping of all defined counter names to integer counts.
    """
    return InvestigationCounters.model_validate(record.counters).model_dump()


def _parse_instant(stamp: str | None) -> dt.datetime | None:
    """Parses an ISO timestamp string into an offset-aware UTC datetime.

    Args:
        stamp: ISO timestamp string or None.

    Returns:
        Offset-aware UTC datetime, or None if unparseable or empty.
    """
    if not stamp:
        return None
    try:
        moment = dt.datetime.fromisoformat(stamp)
    except ValueError:
        logger.warning("investigations: unparseable window bound %r", stamp)
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)


def _require(moment: dt.datetime | None) -> dt.datetime:
    """Validates that a parsed timestamp is not None.

    Args:
        moment: Parsed datetime object or None.

    Returns:
        The validated datetime object.

    Raises:
        ValueError: If moment is None.
    """
    if moment is None:
        raise ValueError("window bounds must be ISO instants")
    return moment


def _compute_utc_day(stamp: str | None) -> str | None:
    """Extracts the UTC calendar day from an ISO timestamp formatted as YYYY-MM-DD.

    Args:
        stamp: ISO timestamp string.

    Returns:
        ISO date string (YYYY-MM-DD) in UTC, or None if the timestamp is unparseable.
    """
    moment = _parse_instant(stamp)
    if moment is None:
        return None
    return moment.astimezone(dt.UTC).date().isoformat()
