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

"""Read-only query contract for insights, occurrences, and root causes.

Implemented by `bigquery_reader.BigQueryInsightReader` for deployments and
`standalone.InMemoryInsightReader` for standalone mode. Kept separate from
`store.InsightStore` so read paths cannot invoke write operations.

Both implementations enforce three invariants:
1. **Merged evidence rolls up to the target insight.** Occurrences, trajectory
   counts, and root-cause diagnoses of a merged duplicate are attributed to its
   target insight, and the duplicate itself reports zero occurrences.
2. **List queries hide dismissed and merged insights; direct lookups do not.**
   `list_insights` omits dismissed and merged insights so operator actions take
   effect immediately in list views, while `get_insight` still resolves them so
   existing links do not break.
3. **Figures about all traffic use estimates.** Investigations pick some
   conversations more often than others, so an insight's share of all
   conversations and its rank by impact count each reviewed conversation as
   ``1 / p`` conversations (`compute_estimated_share`, `compute_impact`).
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Protocol

from ambient_quality_agent.tools.investigations.models import CustomOverrides

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Iterable, Mapping, Sequence

    from ambient_quality_agent.tools.insights.models import (
        InsightOccurrence,
        InsightStatus,
        InsightView,
        RootCause,
    )
    from ambient_quality_agent.tools.investigations.models import (
        Investigation,
    )


class InsightOrder(enum.StrEnum):
    """Sort order for `InsightReader.list_insights` before pagination."""

    RECENT = "recent"
    """Most recently seen first (`updated_at` descending)."""

    IMPACT = "impact"
    """Most conversations of all traffic affected first (`compute_impact`
    descending)."""


def compute_impact(occurrences: Iterable[InsightOccurrence]) -> float:
    """Computes the figure `InsightOrder.IMPACT` ranks an insight by.

    Args:
        occurrences: The occurrences the insight owns.

    Returns:
        The sum of each occurrence's `estimated_trajectory_count`, or of its
        `trajectory_count` when it has no estimate.
    """
    return sum(
        o.trajectory_count
        if o.estimated_trajectory_count is None
        else o.estimated_trajectory_count
        for o in occurrences
    )


def is_share_investigation(investigation: Investigation) -> bool:
    """Checks whether an insight's share can be estimated from an investigation.

    An investigation that ran a selector reviewed the conversations it chose,
    not a sample of all traffic, and one recorded without estimates or
    without a counted window has no denominator. The selector is a custom
    investigation's own, or for any other the attachment's stored selector,
    as the investigation ran it.

    Args:
        investigation: The investigation's latest record.

    Returns:
        True when the investigation has estimates, counted the conversations
        in its window, has a window end, and ran no selector.
    """
    overrides = investigation.custom_overrides or CustomOverrides()
    selector_sql = (
        overrides.selector_sql
        if overrides.is_custom()
        else investigation.effective_config.get("investigation_selector_sql")
    )
    return (
        investigation.counters.has_estimates
        and investigation.counters.trajectories_scanned > 0
        and investigation.window_end is not None
        and not selector_sql
    )


def compute_estimated_share(
    occurrences: Iterable[InsightOccurrence],
    windows: Mapping[str, tuple[dt.datetime, int]],
    *,
    investigation_id: str | None = None,
) -> float | None:
    """Estimates the share of all conversations that show an insight.

    The numerator adds up the insight's estimated conversations, and the
    denominator the conversations each investigation's window held, which
    its estimates add up to (`_sweep.compute_trajectories_represented`). The
    investigations are all of the agent's from the first
    that saw the insight to the latest that saw it, by window end, including
    those in between that did not see it, so each counts in proportion to the
    traffic it covered. Both sums leave out investigations that
    `is_share_investigation` refuses, and occurrences without an estimate.
    Two occurrences of the insight in one investigation that share
    trajectories count those trajectories twice, so the share can overstate,
    and in the extreme exceed 1.

    Args:
        occurrences: The occurrences the insight owns.
        windows: For each investigation `is_share_investigation` accepts, its
            window end and `trajectories_scanned`, keyed by id.
        investigation_id: When set and not empty, the share in that
            investigation alone.

    Returns:
        The share, from 0 to 1 unless overlapping occurrences push it higher,
        or None when no accepted investigation saw the insight.
    """
    sighted = [
        o
        for o in occurrences
        if o.estimated_trajectory_count is not None
        and o.investigation_id in windows
        and investigation_id in (None, "", o.investigation_id)
    ]
    if not sighted:
        return None
    ends = [windows[o.investigation_id][0] for o in sighted]
    first, last = min(ends), max(ends)
    window_total = sum(
        scanned
        for key, (end, scanned) in windows.items()
        if first <= end <= last and investigation_id in (None, "", key)
    )
    if not window_total:
        return None
    return (
        sum(o.estimated_trajectory_count or 0.0 for o in sighted) / window_total
    )


class InsightReader(Protocol):
    """Read interface for one agent's insights, occurrences, and root causes."""

    def list_insights(
        self,
        *,
        statuses: Sequence[InsightStatus] | None = None,
        investigation_id: str | None = None,
        limit: int,
        offset: int,
        window_start: str | None = None,
        window_end: str | None = None,
        has_root_cause: bool | None = None,
        order_by: InsightOrder = InsightOrder.RECENT,
        day: str | None = None,
        query: str | None = None,
    ) -> tuple[list[InsightView], int]:
        """Lists one page of active insights and the total matching count.

        Excludes dismissed and merged insights. Time window bounds filter on
        ``updated_at`` (the most recent sighting) so recurring issues remain
        visible throughout the window. ``investigation_id`` filters by resolved
        owner insight so occurrences from merged duplicates still surface their
        target insight.

        When ``day`` (``YYYY-MM-DD`` in UTC) is set, filters to insights first
        created (``created_at``) or resolved (``resolved_at``) on that day,
        with ``statuses`` selecting which transition to include (``NEW`` or
        ``RESOLVED``).

        When ``query`` is set, keeps only insights whose label contains it or
        whose id starts with it, ignoring case. The label is the one the list
        shows: it holds verification's refined name when there is one.

        Args:
            statuses: Optional lifecycle statuses to filter on.
            investigation_id: Optional investigation ID to scope to.
            limit: Maximum insights to return.
            offset: Number of matching insights to skip.
            window_start: Optional ISO timestamp lower bound on `updated_at`.
            window_end: Optional ISO timestamp upper bound on `updated_at`.
            has_root_cause: Optional filter for diagnosed (True) or undiagnosed
                (False) insights.
            order_by: Sort order applied before pagination.
            day: Optional UTC date string (``YYYY-MM-DD``).
            query: Optional text to match against the label or id prefix.

        Returns:
            Tuple of ``(page, total_matching)``.
        """

    def count_affected_conversations(
        self,
        *,
        statuses: Sequence[InsightStatus] | None = None,
        investigation_id: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        has_root_cause: bool | None = None,
        day: str | None = None,
        query: str | None = None,
    ) -> int:
        """Counts distinct trajectories across all insights matching the filters.

        Applies the same filters as `list_insights` across the entire matching
        set (not just one page) and deduplicates trajectory IDs across
        occurrences and insights.

        Args:
            statuses: Optional lifecycle statuses to filter on.
            investigation_id: Optional investigation ID filter.
            window_start: Optional ISO timestamp lower bound on `updated_at`.
            window_end: Optional ISO timestamp upper bound on `updated_at`.
            has_root_cause: Optional boolean filter for root-cause presence.
            day: Optional UTC date string (``YYYY-MM-DD``).
            query: Optional text to match against the label or id prefix.

        Returns:
            Count of distinct trajectory IDs across all matching insights.
        """

    def get_insight(self, insight_id: str) -> InsightView | None:
        """Loads one insight with aggregated stats by ID, including dismissed or merged insights.

        Args:
            insight_id: Unique insight identifier.

        Returns:
            `InsightView` if found, or None otherwise.
        """

    def get_insight_with_occurrences(
        self,
        *,
        insight_id: str,
        investigation_id: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[InsightView, list[InsightOccurrence], int] | None:
        """Loads one insight and a paginated slice of its occurrences.

        Args:
            insight_id: Unique insight identifier.
            investigation_id: Optional investigation ID filter for occurrences.
            limit: Maximum occurrences to return.
            offset: Number of occurrences to skip.

        Returns:
            Tuple of ``(view, occurrences, total_occurrences)``, or None if the
            insight is not found.
        """

    def list_occurrences(
        self,
        *,
        insight_id: str | None = None,
        investigation_id: str | None = None,
        occurrence_id: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[InsightOccurrence], int]:
        """Lists one page of tracked occurrences ordered newest first.

        Resolves ``insight_id`` by owner so occurrences of merged duplicates are
        returned under their target insight. Excludes occurrences where
        ``insight_id`` is ``None`` (rejected or unjudged candidates).

        Args:
            insight_id: Insight ID to list occurrences for.
            investigation_id: Optional investigation ID filter.
            occurrence_id: Optional occurrence ID filter.
            limit: Maximum occurrences to return.
            offset: Number of occurrences to skip.

        Returns:
            Tuple of ``(occurrences, total_count)``.

        Raises:
            ValueError: If neither ``insight_id`` nor ``occurrence_id`` is
                provided.
        """

    def list_root_causes(
        self, insight_id: str, *, history: bool = False
    ) -> list[RootCause]:
        """Lists root-cause diagnoses for an insight, ordered newest first.

        Resolves ownership across merged insights so diagnoses written against a
        duplicate are returned for the target insight.

        Args:
            insight_id: Unique insight identifier.
            history: If True, returns all revisions; if False, returns only the
                latest revision per occurrence.

        Returns:
            List of `RootCause` records ordered newest first.
        """
