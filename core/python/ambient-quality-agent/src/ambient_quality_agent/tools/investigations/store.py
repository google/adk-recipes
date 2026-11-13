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

"""Storage contract for investigations and their progress events.

Implemented by `bigquery_store.BigQueryInvestigationStore` for deployments and
`standalone.InMemoryInvestigationStore` for standalone mode.

In BigQuery, an investigation is stored as an append-only series of snapshots
and reconstructed on read; in-memory storage replaces the record directly. Both
implementations satisfy the same behavioral contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ambient_quality_agent.tools.investigations.models import (
        Investigation,
        InvestigationEvent,
        InvestigationStatus,
    )

DEFAULT_LIST_LIMIT = 50
"""Maximum investigations returned by one `list_recent` query."""

IN_FLIGHT_HORIZON_HOURS = 24
"""Maximum age in hours for an unfinished investigation to be counted by `count_in_flight`.

Investigations untouched longer than this threshold are treated as abandoned so
crashed processes do not permanently block concurrency limits.
"""


class InvestigationStore(Protocol):
    """Storage interface for one agent's investigations and progress events.

    A store is bound to one agent's key when constructed. `list_recent`,
    `sum_counters`, and `sum_counters_by_day` scope to investigations matching
    that `observed_agent_key` (or all investigations when bound to an empty
    key). `get`, `list_latest_stage_events`, `list_stale_pending`, and
    `list_overdue_scheduled` query across all agents because investigation IDs
    are globally unique and share one housekeeping pass.
    """

    def append(self, record: Investigation) -> Investigation:
        """Persists a new snapshot of an investigation.

        Restamps `updated_at` on the record. Does not persist `events`, which
        are written separately via `append_event`.

        Args:
            record: Investigation state to store.

        Returns:
            The stored investigation snapshot.
        """

    def append_event(
        self, investigation_id: str, text: str, *, source: str = ""
    ) -> None:
        """Appends a progress event to an investigation.

        Args:
            investigation_id: Investigation identifier.
            text: Markdown text of the event.
            source: Emitting workflow node or component identifier.
        """

    def get(
        self, investigation_id: str, *, include_events: bool = True
    ) -> Investigation | None:
        """Loads an investigation by ID.

        Args:
            investigation_id: Investigation identifier.
            include_events: Whether to load associated progress events.

        Returns:
            The investigation snapshot, or None if not found.
        """

    def list_recent(
        self,
        *,
        status: InvestigationStatus | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> list[Investigation]:
        """Lists the most recent investigations ordered oldest first, without events.

        Args:
            status: Optional investigation status filter.
            limit: Maximum number of investigations to return.
            window_start: Optional ISO timestamp lower bound on `window_end`.
            window_end: Optional ISO timestamp upper bound on `window_end`.

        Returns:
            List of `Investigation` records sorted oldest first.
        """

    def list_latest_stage_events(
        self, investigation_ids: Sequence[str]
    ) -> list[InvestigationEvent]:
        """Finds the newest stage event of each named investigation.

        A stage event (`STAGE_EVENT_SOURCE`) says how far a running
        investigation got. `list_recent` reads no events, so the list asks
        for these separately for the investigations still going.

        Args:
            investigation_ids: Investigations to look up; may be empty.

        Returns:
            One event per named investigation that wrote a stage event, in no
            particular order.
        """

    def sum_counters(
        self, *, window_start: str | None = None, window_end: str | None = None
    ) -> tuple[dict[str, int], dict[str, str | None]]:
        """Sums counters across investigations within a telemetry window.

        Args:
            window_start: Optional ISO timestamp lower bound on `window_end`.
            window_end: Optional ISO timestamp upper bound on `window_end`.

        Returns:
            Tuple of `(counter_totals, covered_window_bounds)`.
        """

    def sum_counters_by_day(
        self, *, window_start: str | None = None, window_end: str | None = None
    ) -> list[dict[str, int | str]]:
        """Sums counters grouped by UTC calendar day of `window_end`.

        Args:
            window_start: Optional ISO timestamp lower bound on `window_end`.
            window_end: Optional ISO timestamp upper bound on `window_end`.

        Returns:
            List of daily counter summary dicts ordered chronologically.
        """

    def get_last_finished_window_end(
        self, observed_agent_key: str
    ) -> str | None:
        """Returns the latest `window_end` among completed ambient investigations.

        Excludes dry runs and non-ambient triggers (`NON_AMBIENT_TRIGGER_TYPES`)
        so manual or custom investigations do not move the ambient watermark.

        Args:
            observed_agent_key: Key of the observed agent.

        Returns:
            ISO 8601 timestamp of the latest completed `window_end`, or None if
            no completed ambient investigation exists.
        """

    def count_in_flight(self, observed_agent_key: str) -> int:
        """Counts active in-flight investigations for an agent.

        Counts investigations whose latest status is ``PENDING`` or ``RUNNING``
        and updated within `IN_FLIGHT_HORIZON_HOURS`. Excludes ``SCHEDULED``
        investigations because nothing runs until their delayed trigger fires.

        Args:
            observed_agent_key: Key of the observed agent.

        Returns:
            Number of in-flight investigations.
        """

    def list_stale_pending(self, *, lease_minutes: int) -> list[str]:
        """Lists investigation IDs left in ``PENDING`` status beyond `lease_minutes`.

        Args:
            lease_minutes: Minutes after `updated_at` before a pending
                investigation is considered stale.

        Returns:
            List of stale pending investigation IDs.
        """

    def list_overdue_scheduled(self, *, grace_minutes: int) -> list[str]:
        """Lists ``SCHEDULED`` investigation IDs whose `due_at` passed by more than `grace_minutes`.

        Args:
            grace_minutes: Minutes past `due_at` a scheduled investigation may
                wait for its delayed trigger before it is considered lost.

        Returns:
            List of overdue scheduled investigation IDs.
        """
