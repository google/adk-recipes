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

"""Adapts `InMemoryStore` insights to implement the `InsightReader` contract.

Exposes in-memory insights to dashboard routes and chat tools through the same
interface as `BigQueryInsightReader`.

## Reconciled Ownership on Write

When insights merge, `InMemoryInsightStore.merge_insight` reassigns occurrence and
diagnosis records directly to the surviving insight. Consequently, each
occurrence's `insight_id` directly identifies its active owner, eliminating the
need for join-time ownership coalescing during reads.

## Behavioral Parity

Filtering, ordering, and pagination rules match the production contract:
1. Dismissed and merged insights are omitted from listings but remain accessible
   via direct lookups.
2. Window filters evaluate the most recent sighting timestamp.
3. Total counts represent the entire matching dataset rather than the current page.
4. Conversations are deduplicated across multiple sweeps.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from ambient_quality_agent.tools.insights.models import (
    InsightStatus,
    InsightView,
)
from ambient_quality_agent.tools.insights.reader import InsightOrder

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ambient_quality_agent.standalone.store import InMemoryStore
    from ambient_quality_agent.tools.insights.models import (
        Insight,
        InsightOccurrence,
        RootCause,
    )

logger = logging.getLogger(__name__)


class InMemoryInsightReader:
    """One agent's insights, read out of the in-process `InMemoryStore`."""

    def __init__(self, store: InMemoryStore, *, agent_name: str) -> None:
        self._store = store
        self._agent_name = agent_name

    # --- The list, and the header above it ----------------------------------- #

    def list_insights(
        self,
        *,
        statuses: Sequence[InsightStatus] | None = None,
        run_id: str | None = None,
        limit: int,
        offset: int,
        window_start: str | None = None,
        window_end: str | None = None,
        has_root_cause: bool | None = None,
        order_by: InsightOrder = InsightOrder.RECENT,
        day: str | None = None,
    ) -> tuple[list[InsightView], int]:
        """Returns a paginated list of insights and the total match count.

        Args:
            statuses: Optional sequence of insight statuses to filter by.
            run_id: Optional investigation run ID to filter occurrences by.
            limit: Maximum number of insight views to return.
            offset: Number of matching insights to skip.
            window_start: Optional ISO timestamp lower bound for last sighting.
            window_end: Optional ISO timestamp upper bound for last sighting.
            has_root_cause: Optional boolean filter for whether diagnoses exist.
            order_by: Sorting criterion (recency or conversation impact).
            day: Optional ISO date string (YYYY-MM-DD) for status change date filter.

        Returns:
            A tuple of (paginated list of InsightView records, total match count).
        """
        matching = self._list_matching_insights(
            statuses, run_id, window_start, window_end, has_root_cause, day=day
        )
        views = [self._build_insight_view(insight) for insight in matching]
        views.sort(key=_ORDER_KEYS[order_by])
        return views[offset : offset + limit], len(views)

    def count_affected_conversations(
        self,
        *,
        statuses: Sequence[InsightStatus] | None = None,
        run_id: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        has_root_cause: bool | None = None,
        day: str | None = None,
    ) -> int:
        """Counts unique conversations across insights matching the given filters.

        Shares filtering logic with `list_insights` so summary counts align with
        listed rows.

        Args:
            statuses: Optional sequence of insight statuses to filter by.
            run_id: Optional investigation run ID to filter occurrences by.
            window_start: Optional ISO timestamp lower bound for last sighting.
            window_end: Optional ISO timestamp upper bound for last sighting.
            has_root_cause: Optional boolean filter for whether diagnoses exist.
            day: Optional ISO date string (YYYY-MM-DD) for status change date filter.

        Returns:
            Number of distinct conversation trajectory IDs across matching insights.
        """
        conversations: set[str] = set()
        for insight in self._list_matching_insights(
            statuses, run_id, window_start, window_end, has_root_cause, day=day
        ):
            for occurrence in self._list_owned_occurrences(insight.insight_id):
                conversations.update(occurrence.trajectory_ids)
        return len(conversations)

    # --- One insight ---------------------------------------------------------- #

    def get_insight(self, insight_id: str) -> InsightView | None:
        """Retrieves an insight with aggregate statistics by ID.

        Does not filter out dismissed or merged insights, allowing direct lookups
        and bookmark links to resolve.

        Args:
            insight_id: Unique identifier of the insight.

        Returns:
            The `InsightView` if found and owned by this agent, or None.
        """
        insight = self._store.insights.get(insight_id)
        if insight is None or not self._is_mine(insight):
            return None
        return self._build_insight_view(insight)

    def get_insight_with_occurrences(
        self,
        *,
        insight_id: str,
        run_id: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[InsightView, list[InsightOccurrence], int] | None:
        """Retrieves an insight along with a paginated page of its occurrence evidence.

        Args:
            insight_id: Unique identifier of the insight.
            run_id: Optional run ID to filter occurrences by.
            limit: Maximum number of occurrences to return.
            offset: Number of occurrences to skip.

        Returns:
            A tuple of (insight view, occurrences list, total occurrences count),
            or None if the insight does not exist or belongs to another agent.
        """
        view = self.get_insight(insight_id)
        if view is None:
            return None
        occurrences, total = self.list_occurrences(
            insight_id=insight_id, run_id=run_id, limit=limit, offset=offset
        )
        return view, occurrences, total

    def list_occurrences(
        self,
        *,
        insight_id: str | None = None,
        run_id: str | None = None,
        occurrence_id: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[InsightOccurrence], int]:
        """Returns a paginated list of occurrences, newest first.

        Args:
            insight_id: Optional insight ID to filter occurrences by.
            run_id: Optional investigation run ID to filter occurrences by.
            occurrence_id: Optional occurrence ID to fetch a specific occurrence.
            limit: Maximum number of occurrences to return.
            offset: Number of matching occurrences to skip.

        Returns:
            A tuple of (list of InsightOccurrence records, total matching count).

        Raises:
            ValueError: If neither insight_id nor occurrence_id is provided.
        """
        if not insight_id and not occurrence_id:
            raise ValueError(
                "list_occurrences needs an insight_id or an occurrence_id"
            )
        matching = [
            occurrence
            for occurrence in self._store.occurrences
            if occurrence.agent_name == self._agent_name
            # Exclude unconfirmed or rejected candidate occurrences that lack an insight ID.
            and occurrence.insight_id is not None
            and (insight_id is None or occurrence.insight_id == insight_id)
            and (run_id is None or occurrence.run_id == run_id)
            and (
                occurrence_id is None
                or occurrence.occurrence_id == occurrence_id
            )
        ]
        matching.sort(
            key=lambda o: (o.created_at, o.occurrence_id), reverse=True
        )
        return matching[offset : offset + limit], len(matching)

    def list_root_causes(
        self, insight_id: str, *, history: bool = False
    ) -> list[RootCause]:
        """Returns diagnoses for an insight, sorted newest first.

        Args:
            insight_id: Unique identifier of the insight.
            history: If True, returns all diagnosis revisions; if False, returns
                only the latest diagnosis for each occurrence.

        Returns:
            List of `RootCause` records.
        """
        records = [
            record
            for record in self._store.root_causes
            if record.insight_id == insight_id
        ]
        records.sort(
            key=lambda r: (r.created_at, r.root_cause_id), reverse=True
        )
        if history:
            return records
        # Select only the latest revision per occurrence from the already-sorted list.
        seen: set[str] = set()
        current = []
        for record in records:
            if record.occurrence_id in seen:
                continue
            seen.add(record.occurrence_id)
            current.append(record)
        return current

    # --- Shared rules ---------------------------------------------------------- #

    def _is_mine(self, insight: Insight) -> bool:
        return insight.agent_name == self._agent_name

    def _list_matching_insights(
        self,
        statuses: Sequence[InsightStatus] | None,
        run_id: str | None,
        window_start: str | None,
        window_end: str | None,
        has_root_cause: bool | None,
        *,
        day: str | None = None,
    ) -> list[Insight]:
        """Filters insights by lifecycle status, run, sighting window, and diagnoses.

        Dismissed and merged insights are excluded so user actions immediately
        remove rows from listing views.

        Args:
            statuses: Optional sequence of insight statuses to filter by.
            run_id: Optional run ID to filter occurrences by.
            window_start: Optional ISO timestamp lower bound for last sighting.
            window_end: Optional ISO timestamp upper bound for last sighting.
            has_root_cause: Optional boolean filter for diagnosis presence.
            day: Optional ISO date string (YYYY-MM-DD) for status change date filter.

        Returns:
            List of matching `Insight` records.
        """
        start = _parse_instant(window_start)
        end = _parse_instant(window_end)
        changed_on = dt.date.fromisoformat(day) if day else None
        wanted = set(statuses) if statuses else None
        kept = []
        for insight in self._store.insights.values():
            if not self._is_mine(insight):
                continue
            if insight.dismissed_at is not None:
                continue
            if insight.merged_into_insight_id is not None:
                continue
            if changed_on is not None:
                # When filtering by day, statuses represent state transitions rather than current states.
                if not _has_changed_on(insight, changed_on, wanted):
                    continue
            elif wanted is not None and insight.status not in wanted:
                continue
            # Filter by the most recent sighting timestamp to match active issues in the window.
            if start is not None and insight.updated_at < start:
                continue
            if end is not None and insight.updated_at > end:
                continue
            if run_id is not None and not any(
                o.run_id == run_id
                for o in self._list_owned_occurrences(insight.insight_id)
            ):
                continue
            if has_root_cause is not None and (
                bool(self.list_root_causes(insight.insight_id))
                != has_root_cause
            ):
                continue
            kept.append(insight)
        return kept

    def _list_owned_occurrences(
        self, insight_id: str
    ) -> list[InsightOccurrence]:
        """Returns all occurrences belonging to an insight.

        Occurrences directly reference their active insight ID following merges.

        Args:
            insight_id: Unique identifier of the insight.

        Returns:
            List of `InsightOccurrence` records belonging to this insight and agent.
        """
        return [
            occurrence
            for occurrence in self._store.occurrences
            if occurrence.agent_name == self._agent_name
            and occurrence.insight_id == insight_id
        ]

    def _build_insight_view(self, insight: Insight) -> InsightView:
        """Assembles an `InsightView` with aggregated stats for an insight.

        Args:
            insight: Base insight record.

        Returns:
            `InsightView` combining insight attributes with occurrence and trace counts.
        """
        owned = self._list_owned_occurrences(insight.insight_id)
        newest = max(
            owned, key=lambda o: (o.created_at, o.occurrence_id), default=None
        )
        return InsightView(
            **insight.model_dump(),
            occurrence_count=len(owned),
            trace_count=sum(o.trace_count for o in owned),
            last_run_id=newest.run_id if newest else None,
            last_run_at=newest.created_at if newest else None,
            has_root_cause=bool(self.list_root_causes(insight.insight_id)),
        )


def _has_changed_on(
    insight: Insight, day: dt.date, wanted: set[InsightStatus] | None
) -> bool:
    """Checks whether an insight was created (NEW) or resolved (RESOLVED) on a UTC day.

    Args:
        insight: Insight record to check.
        day: Target UTC calendar day.
        wanted: Optional set of statuses to restrict the check to.

    Returns:
        True if the insight transitioned to a wanted status on the specified day.
    """

    def is_on_day(moment: dt.datetime | None) -> bool:
        return moment is not None and moment.astimezone(dt.UTC).date() == day

    # Evaluated against the insight created_at timestamp in UTC, whereas the
    # dashboard home chart groups by the creating investigation's window_end date.
    found = is_on_day(insight.created_at) and (
        wanted is None or InsightStatus.NEW in wanted
    )
    resolved = is_on_day(insight.resolved_at) and (
        wanted is None or InsightStatus.RESOLVED in wanted
    )
    return found or resolved


def _compute_recency_key(view: InsightView) -> tuple[object, ...]:
    """Sort key for ordering insights by recency descending.

    Breaks timestamp ties using `insight_id` to guarantee stable pagination.

    Args:
        view: Insight view to compute the sort key for.

    Returns:
        Tuple representing the sort key.
    """
    return (-view.updated_at.timestamp(), view.insight_id)


def _compute_impact_key(view: InsightView) -> tuple[object, ...]:
    """Sort key for ordering insights by conversation count descending.

    Args:
        view: Insight view to compute the sort key for.

    Returns:
        Tuple representing the sort key.
    """
    return (-view.trace_count, view.insight_id)


_ORDER_KEYS = {
    InsightOrder.RECENT: _compute_recency_key,
    InsightOrder.IMPACT: _compute_impact_key,
}


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
        logger.warning("insights: unparseable window bound %r", stamp)
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)
