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

"""What a store of investigation runs has to answer, apart from how it keeps them.

`bigquery_store.BigQueryInvestigationStore` is one implementation;
`standalone.InMemoryInvestigationStore` is another. Naming the contract
separately is what lets a caller be typed to the question rather than to the
answer, and what makes the difference between the two reviewable: they are free
to keep a run quite differently as long as they answer these the same way.

Two writes and eight reads. The reads are where they diverge most. BigQuery has
no cheap ``UPDATE``, so a run there is a history of whole snapshots and every
read reconstructs the current one; a store that can simply change the record it
holds reconstructs nothing. That is a difference in storage and not in
behaviour, and this is the line it must not cross.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ambient_quality_agent.tools.investigations.models import (
        InvestigationRecord,
        RunStatus,
    )

DEFAULT_LIST_LIMIT = 50
"""Runs one `list_recent` page holds."""

IN_FLIGHT_HORIZON_HOURS = 24
"""Maximum age in hours for an unfinished run to be counted by `count_in_flight`.

Runs untouched longer than this threshold are considered abandoned so crashed
processes do not permanently block concurrency limits.
"""


class InvestigationStore(Protocol):
    """One deployment's investigations, and the events they emitted."""

    def append(self, record: InvestigationRecord) -> InvestigationRecord:
        """Persists a new snapshot of an investigation run.

        Restamps updated_at on the record. Does not persist events, which must be
        written using append_event.

        Args:
            record: Investigation record state to store.

        Returns:
            The stored investigation record snapshot.
        """

    def append_event(self, run_id: str, text: str, *, source: str = "") -> None:
        """Appends a progress event to a run.

        Args:
            run_id: Investigation run identifier.
            text: Markdown text of the event.
            source: Emitting workflow node or component identifier.
        """

    def get(
        self, run_id: str, *, include_events: bool = True
    ) -> InvestigationRecord | None:
        """Loads an investigation run by ID.

        Args:
            run_id: Investigation run identifier.
            include_events: Whether to load associated progress events.

        Returns:
            The investigation record snapshot, or None if not found.
        """

    def list_recent(
        self,
        *,
        status: RunStatus | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> list[InvestigationRecord]:
        """Queries recent investigation runs ordered chronologically without events.

        Args:
            status: Optional run status filter.
            limit: Maximum number of runs to return.
            window_start: Optional ISO timestamp lower bound on telemetry window end.
            window_end: Optional ISO timestamp upper bound on telemetry window end.

        Returns:
            List of investigation records sorted oldest first.
        """

    def sum_counters(
        self, *, window_start: str | None = None, window_end: str | None = None
    ) -> tuple[dict[str, int], dict[str, str | None]]:
        """Aggregates counters across investigation runs within a time window.

        Args:
            window_start: Optional ISO timestamp lower bound on telemetry window end.
            window_end: Optional ISO timestamp upper bound on telemetry window end.

        Returns:
            Tuple of (mapping of counter name to sum, dictionary of covered window bounds).
        """

    def sum_counters_by_day(
        self, *, window_start: str | None = None, window_end: str | None = None
    ) -> list[dict[str, int | str]]:
        """Aggregates counters grouped by calendar day of telemetry window end.

        Args:
            window_start: Optional ISO timestamp lower bound on telemetry window end.
            window_end: Optional ISO timestamp upper bound on telemetry window end.

        Returns:
            List of daily summary dictionaries ordered chronologically.
        """

    def get_last_finished_window_end(
        self, observed_agent_name: str
    ) -> str | None:
        """Returns the latest telemetry window end timestamp for finished ambient runs.

        Excludes dry runs and non-ambient triggers (`NON_AMBIENT_TRIGGER_TYPES`) to
        prevent custom or manual sweeps from advancing or resetting the ambient
        telemetry watermark.

        Args:
            observed_agent_name: Name of the observed agent.

        Returns:
            ISO 8601 timestamp of the latest completed window end, or None if no
            completed ambient run exists.
        """

    def count_in_flight(self, observed_agent_name: str) -> int:
        """Counts currently active investigation runs for an agent.

        Counts runs whose latest status is pending or running and updated within
        `IN_FLIGHT_HORIZON_HOURS`. The time horizon ensures abandoned or crashed
        processes do not consume concurrency capacity indefinitely. A scheduled
        run is not counted: nothing runs for it until its trigger fires.

        Args:
            observed_agent_name: Name of the observed agent.

        Returns:
            Number of in-flight investigations.
        """

    def list_stale_pending(self, *, lease_minutes: int) -> list[str]:
        """Identifies run IDs remaining in pending status beyond the lease duration.

        Args:
            lease_minutes: Number of minutes after creation before a pending run is considered stale.

        Returns:
            List of stale run IDs.
        """

    def list_overdue_scheduled(self, *, grace_minutes: int) -> list[str]:
        """Identifies scheduled run IDs whose trigger is overdue by more than the grace.

        Args:
            grace_minutes: Minutes past `due_at` a scheduled run may wait for its
                trigger before it is considered lost.

        Returns:
            List of overdue scheduled run IDs.
        """
