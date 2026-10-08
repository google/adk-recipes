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

"""Adapts `InMemoryStore` insights to implement the `InsightStore` contract.

Exposes in-memory insight storage to the correlation node and operator actions
through the same interface as `BigQueryInsightStore`.

The candidate matcher defaults to `matching.match_candidates` and is injectable
to allow offline evaluation with deterministic judges. This class controls which
labels are offered for matching and their presentation order.

## In-Memory Merge Semantics

When merging insights (`merge_insight`), occurrences and diagnoses are reassigned
directly to the surviving insight, leaving a tombstone record for the absorbed
insight. This avoids runtime indirection on reads and ensures that occurrences
always directly reference their active owner.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from ambient_quality_agent.standalone.store import Insights
from ambient_quality_agent.tools.insights import matching
from ambient_quality_agent.tools.insights.models import Insight, InsightStatus

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ambient_quality_agent.standalone.store import InMemoryStore
    from ambient_quality_agent.tools.insights.clustering import Cluster
    from ambient_quality_agent.tools.insights.models import InsightOccurrence

logger = logging.getLogger(__name__)


def _read_utc_clock() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class InMemoryInsightStore:
    """One agent's insights, over the in-process `InMemoryStore`."""

    def __init__(
        self,
        store: InMemoryStore,
        *,
        agent_name: str,
        matcher: matching.Matcher | None = None,
    ) -> None:
        """Initializes the insight store scoped to one observed agent.

        Args:
            store: The in-memory backing store for insights and occurrences.
            agent_name: Name of the observed agent to isolate storage queries.
            matcher: Matcher callable to detect duplicate issues. Defaults to
                `matching.match_candidates`.
        """
        self._store = store
        self._agent_name = agent_name
        self._matcher = matcher or matching.match_candidates

    # --- The sweep's write path ---------------------------------------------- #

    def delete_failed_insights(self, run_id: str) -> None:
        """Deletes occurrences and orphaned insights from a previous run attempt.

        Args:
            run_id: Investigation run identifier to clean up.
        """

        def change(current: Insights) -> Insights:
            kept = [
                o
                for o in current.occurrences
                if not (o.run_id == run_id and o.agent_name == self._agent_name)
            ]
            # Evaluate referenced insights against remaining occurrences to prune newly orphaned insights.
            referenced = {o.insight_id for o in kept}
            surviving = {
                insight_id: insight
                for insight_id, insight in current.insights.items()
                if insight.agent_name != self._agent_name
                or insight_id in referenced
            }
            return Insights(surviving, kept, current.root_causes)

        self._store.update_insights(change)

    def find_existing_insights(
        self, candidates: Sequence[Cluster]
    ) -> dict[int, str | None]:
        """Matches candidate clusters against open insights.

        Args:
            candidates: Sequence of clustered candidate issues.

        Returns:
            Mapping of candidate cluster indices to matching insight IDs, or None
            if no match is found.
        """
        if not candidates:
            return {}
        open_insights = self._list_open_insights()
        if not open_insights:
            return dict.fromkeys(range(len(candidates)))
        hits = self._matcher(
            [c.label for c in candidates], [i.label for i in open_insights]
        )
        return {
            index: (open_insights[hit].insight_id if hit is not None else None)
            for index, hit in hits.items()
        }

    def save_investigation_result(
        self,
        new_insights: Sequence[Insight],
        seen_insight_ids: Sequence[str],
        recurring_insight_ids: Sequence[str],
        occurrences: Sequence[InsightOccurrence],
        now: dt.datetime,
        *,
        relabelled: Mapping[str, str] | None = None,
    ) -> None:
        """Writes sweep correlation results including new insights and occurrences.

        Args:
            new_insights: Newly minted insights to add.
            seen_insight_ids: IDs of existing insights confirmed in this sweep.
            recurring_insight_ids: IDs of insights flagged as recurring.
            occurrences: New occurrence evidence records to append.
            now: Current timestamp applied to updated insights.
            relabelled: Optional mapping of insight IDs to updated labels.
        """
        seen = set(seen_insight_ids)
        recurring = set(recurring_insight_ids)
        renames = dict(relabelled or {})

        def change(current: Insights) -> Insights:
            updated = dict(current.insights)
            for insight in new_insights:
                updated[insight.insight_id] = insight
            # Atomically update timestamps, labels, and recurring status for seen insights.
            for insight_id in seen:
                insight = updated.get(insight_id)
                if insight is None or insight.agent_name != self._agent_name:
                    continue
                fields: dict[str, object] = {"updated_at": now}
                if insight_id in renames:
                    fields["label"] = renames[insight_id]
                if insight_id in recurring:
                    fields["status"] = InsightStatus.RECURRING
                updated[insight_id] = insight.model_copy(update=fields)
            return Insights(
                updated,
                [*current.occurrences, *occurrences],
                current.root_causes,
            )

        self._store.update_insights(change)

    def resolve_stale_insights(
        self, now: dt.datetime, window_days: int
    ) -> None:
        """Marks active insights as resolved if not updated within the specified window.

        Args:
            now: Current timestamp used as the resolution time.
            window_days: Inactivity threshold in days after which insights are resolved.
        """
        if window_days <= 0:
            return
        cutoff = now - dt.timedelta(days=window_days)

        def change(current: Insights) -> Insights:
            resolved = {
                insight_id: (
                    insight.model_copy(
                        update={
                            "status": InsightStatus.RESOLVED,
                            "resolved_at": now,
                        }
                    )
                    if self._is_mine(insight)
                    and insight.status is not InsightStatus.RESOLVED
                    and insight.updated_at < cutoff
                    else insight
                )
                for insight_id, insight in current.insights.items()
            }
            return Insights(resolved, current.occurrences, current.root_causes)

        self._store.update_insights(change)

    # --- What an operator can do --------------------------------------------- #

    def dismiss_insight(self, insight_id: str) -> bool:
        """Marks an insight as dismissed.

        Args:
            insight_id: Unique identifier of the insight to dismiss.

        Returns:
            True if the insight exists and belongs to this agent, False otherwise.
        """
        found = False

        def change(current: Insights) -> Insights:
            nonlocal found
            insight = current.insights.get(insight_id)
            if insight is None or not self._is_mine(insight):
                return current
            found = True
            if insight.dismissed_at is not None:
                # Preserve original dismissal timestamp on duplicate dismiss requests.
                return current
            hidden = {
                **current.insights,
                insight_id: insight.model_copy(
                    update={"dismissed_at": _read_utc_clock()}
                ),
            }
            return Insights(hidden, current.occurrences, current.root_causes)

        self._store.update_insights(change)
        return found

    def merge_insight(self, *, insight_id: str, target_insight_id: str) -> bool:
        """Merges a source insight into a target insight.

        Performs the following steps:
        1. Validates that both insights exist, belong to this agent, and are not already merged.
        2. Sets tombstone markers on the source insight and any previously absorbed insights.
        3. Expands the target insight's time window to encompass the source insight.
        4. Reassigns all occurrences from absorbed insights to the target insight.
        5. Reassigns all root cause diagnoses from absorbed insights to the target insight.

        Args:
            insight_id: Identifier of the source insight being absorbed.
            target_insight_id: Identifier of the surviving target insight.

        Returns:
            True if the merge succeeded, False if either insight is invalid or already merged.
        """
        merged = False

        def change(current: Insights) -> Insights:
            nonlocal merged
            source = current.insights.get(insight_id)
            target = current.insights.get(target_insight_id)
            if (
                source is None
                or target is None
                or not self._is_mine(source)
                or not self._is_mine(target)
                or target.merged_into_insight_id is not None
                or insight_id == target_insight_id
            ):
                return current
            merged = True
            now = _read_utc_clock()
            # Flatten transitive merges so all predecessor insights point directly to the new target.
            absorbed = {insight_id} | {
                other_id
                for other_id, other in current.insights.items()
                if other.merged_into_insight_id == insight_id
            }
            updated = {
                other_id: (
                    other.model_copy(
                        update={
                            "merged_into_insight_id": target_insight_id,
                            "merged_at": now,
                        }
                    )
                    if other_id in absorbed
                    else other
                )
                for other_id, other in current.insights.items()
            }
            updated[target_insight_id] = target.model_copy(
                update={
                    "created_at": min(target.created_at, source.created_at),
                    "updated_at": max(target.updated_at, source.updated_at),
                }
            )
            moved = [
                o.model_copy(update={"insight_id": target_insight_id})
                if o.insight_id in absorbed
                else o
                for o in current.occurrences
            ]
            # Reassign root cause diagnoses to the target insight to keep diagnosis counts consistent.
            diagnoses = [
                r.model_copy(update={"insight_id": target_insight_id})
                if r.insight_id in absorbed
                else r
                for r in current.root_causes
            ]
            return Insights(updated, moved, diagnoses)

        self._store.update_insights(change)
        return merged

    # --- Shared rules --------------------------------------------------------- #

    def _is_mine(self, insight: Insight) -> bool:
        return insight.agent_name == self._agent_name

    def _list_open_insights(self) -> list[Insight]:
        """Returns open insights eligible for duplicate matching, newest first.

        Excludes resolved and merged insights while retaining dismissed ones.
        Ties in `updated_at` are broken by `insight_id` for deterministic batching.

        Returns:
            List of open `Insight` objects ordered by `updated_at` descending, then `insight_id`.
        """
        mine = [
            insight
            for insight in self._store.insights.values()
            if self._is_mine(insight)
            and insight.status is not InsightStatus.RESOLVED
            and insight.merged_into_insight_id is None
        ]
        return sorted(
            sorted(mine, key=lambda i: i.insight_id),
            key=lambda i: i.updated_at,
            reverse=True,
        )
