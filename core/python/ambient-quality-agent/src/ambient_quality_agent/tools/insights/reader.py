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

"""What a reader of insights has to answer, apart from how it keeps them.

`bigquery_reader.BigQueryInsightReader` is the deployed implementation;
`standalone.InMemoryInsightReader` is the other. The counterpart to
`store.InsightStore`: that is the sweep's write path and the two operator
actions, this is what the dashboard and the chat tools read back. Deliberately
separate, so a read can never reach the write machinery.

Two rules run through every operation here and are the whole of what the two
implementations must agree on.

**Evidence counts for the insight that owns it.** Merging is a claim that two
insights are one defect, so afterwards the target answers for both: its
sightings, its conversations, its diagnoses. How a store arrives there is its
own business -- BigQuery resolves an indirection on every read, an in-memory
implementation moves them during merge -- but the answers are the same.

**The list hides what an operator hid; a direct read does not.** Dismissed and
merged insights leave the list unconditionally, because the dashboard's buttons
only read as working if the row stops coming back. `get_insight` still resolves
them, because a bookmark should not start 404-ing the moment somebody tidies up.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ambient_quality_agent.tools.insights.models import (
        InsightOccurrence,
        InsightStatus,
        InsightView,
        RootCause,
    )


class InsightOrder(enum.StrEnum):
    """How `InsightReader.list_insights` ranks the insights before it pages them.

    Ordering belongs to the read because the caller receives one page. A client
    that sorts what arrived ranks a window of the list, and the rows that would
    have led the ranking sit on a page it never asked for.
    """

    RECENT = "recent"
    """Last seen first, on ``updated_at``."""

    IMPACT = "impact"
    """Most conversations affected first: `InsightView.trace_count`, summed over
    the insight's sightings, which is what the dashboard ranks by."""


class InsightReader(Protocol):
    """One agent's insights, as the dashboard and the chat tools read them."""

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
        """One page of this agent's insights, and how many matched in all.

        Dismissed and merged insights are left out whatever the filters say.

        The window filters on ``updated_at``, the *last* sighting: "insights in
        the last week" means the issues live that week, which is what a reader
        scanning the list is asking. On ``created_at`` it would answer "issues
        first found that week" and hide a defect that has recurred daily for a
        month.

        ``run_id`` selects by owner, as the counts do -- a sweep that found a
        defect under a label an operator has since called a duplicate still
        found it, and filtering the raw ids would list neither insight.

        ``day`` (``YYYY-MM-DD``, a UTC day) keeps the insights that changed on
        it: first found that day (``created_at``), or resolved that day
        (``resolved_at``). Those are the two per-day series the dashboard
        charts, so a day's list holds the rows behind its column and not every
        insight merely seen that day. With a day, ``statuses`` name which of the
        changes count rather than the current status: NEW keeps those found
        that day even if they have recurred since, RESOLVED keeps those resolved
        that day, and RECURRING, a change no day is stamped with, keeps nothing.

        Args:
            statuses: Optional lifecycle statuses to filter on.
            run_id: Optional sweep run ID to scope to.
            limit: Maximum insights to return.
            offset: Number of matching insights to skip.
            window_start: Optional ISO timestamp lower bound on updated_at.
            window_end: Optional ISO timestamp upper bound on updated_at.
            has_root_cause: Optional filter for diagnosed (True) or undiagnosed
                (False) insights.
            order_by: Sort order for results before pagination.
            day: Optional UTC day string (YYYY-MM-DD).

        Returns:
            ``(page, total matching)``. The total is of the whole matching set,
            not of the page.
        """

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
        """Distinct conversations behind every insight the same filters match.

        Over the whole matching set rather than one page: a header that sums a
        page states a fraction of the deployment as its total. It shares
        `list_insights`' filters exactly, so a header and the list beneath it
        cannot describe different sets.

        *Distinct* is the point. `InsightView.trace_count` counts a conversation
        again for every sweep that saw it and every insight it matches; this
        counts it once.

        Args:
            statuses: Optional lifecycle statuses to filter on.
            run_id: Optional sweep run ID.
            window_start: Optional ISO timestamp lower bound on updated_at.
            window_end: Optional ISO timestamp upper bound on updated_at.
            has_root_cause: Optional boolean filter for root-cause presence.
            day: Optional UTC day string (YYYY-MM-DD).

        Returns:
            Total count of distinct conversations across all matching insights.
        """

    def get_insight(self, insight_id: str) -> InsightView | None:
        """One insight with its stats, or ``None`` if there is no such insight.

        The same aggregation `list_insights` does, narrowed to one id, so a
        detail view leads with the summary the list showed.

        Without the list's hiding filter: a dismissed or merged insight still
        resolves, because a bookmarked link should not start returning 404 the
        moment somebody tidies the list. A merged one reads as zero sightings --
        its evidence counts for the target now.

        Args:
            insight_id: Unique insight identifier.

        Returns:
            InsightView if found, None otherwise.
        """

    def get_insight_with_occurrences(
        self,
        *,
        insight_id: str,
        run_id: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[InsightView, list[InsightOccurrence], int] | None:
        """One insight and a page of its evidence, or ``None`` if unknown.

        Args:
            insight_id: Unique insight identifier.
            run_id: Optional sweep ID filter for occurrences.
            limit: Maximum occurrences to return.
            offset: Number of occurrences to skip.

        Returns:
            Tuple of ``(view, occurrences, total occurrences)``, or None if
            unknown.
        """

    def list_occurrences(
        self,
        *,
        insight_id: str | None = None,
        run_id: str | None = None,
        occurrence_id: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[InsightOccurrence], int]:
        """One page of an insight's sightings, newest first.

        ``insight_id`` selects by owner, the same resolution `get_insight`
        aggregates over, so a detail page shows the evidence its header counts.
        A merged insight therefore shows none: it has none of its own any more,
        which is what its zero says.

        An occurrence naming no insight is never returned. Such a row records a
        candidate the verification pass rejected or never judged -- evidence
        about the sweep, not a sighting of a tracked issue.

        Args:
            insight_id: Insight ID to list occurrences for.
            run_id: Optional sweep run ID filter.
            occurrence_id: Optional sighting ID filter.
            limit: Maximum occurrences to return.
            offset: Number of occurrences to skip.

        Returns:
            Tuple of (occurrences_list, total_count).

        Raises:
            ValueError: if neither ``insight_id`` nor ``occurrence_id`` bounds
                the read.
        """

    def list_root_causes(
        self, insight_id: str, *, history: bool = False
    ) -> list[RootCause]:
        """One insight's diagnoses, newest first.

        Resolved by owner, like the sightings: a diagnosis of a merged insight
        is a diagnosis of the target that absorbed it, and a merged insight
        answers with none of its own. Each record still carries the
        ``insight_id`` it was written against.

        ``history`` returns every revision rather than only the current one per
        occurrence.

        Args:
            insight_id: Unique insight identifier.
            history: If True, returns all revisions rather than only the latest
                per occurrence.

        Returns:
            List of RootCause records ordered newest first.
        """
