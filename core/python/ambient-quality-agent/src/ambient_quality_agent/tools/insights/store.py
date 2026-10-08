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

"""What a store of insights has to answer, apart from how it keeps them.

`bigquery_store.BigQueryInsightStore` is the deployed implementation;
`standalone.InMemoryInsightStore` is the other. This is the write path a sweep
takes plus the two actions an operator can take; `reader.InsightReader` is the
read path the dashboard and the chat tools take, and the two are deliberately
separate classes so a read can never reach the write machinery.

Six operations, and one of them -- `merge_insight` -- is the only place in AQuA
where the two implementations are meant to end up holding *different data* for
the same history. BigQuery leaves the absorbed insight's occurrences where they
are and points the insight at its survivor, because re-pointing many rows is
expensive there; the read folds the pointer away. In-memory implementations
can move occurrences directly during the merge; both approaches yield identical
read results. Both must answer every read the same way afterwards, and neither
may leave an occurrence reachable from two insights.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ambient_quality_agent.tools.insights.clustering import Cluster
    from ambient_quality_agent.tools.insights.models import (
        Insight,
        InsightOccurrence,
    )


class InsightWriter(Protocol):
    """What a sweep needs: the four calls `insight_correlation` makes.

    Narrower than `InsightStore` because the graph never curates, and the
    narrowing is load bearing rather than tidiness. `insight_store_factory` is
    typed to this, so a stand-in installed there -- the harness's capturing
    store, a test double -- answers four calls and no more. Typed to the whole
    store, such a stand-in would have to inherit one to stay assignable, and
    would carry an implementation of the other two that it never meant to have
    and that nothing would exercise.
    """

    def delete_failed_insights(self, run_id: str) -> None:
        """Erase a previous attempt of ``run_id`` so a retry starts clean.

        Drops this agent's occurrences for the run, then the insights that
        deletion left with no occurrence at all. Order matters: the orphan check
        reads the occurrence set as it stands *after* the first step, or it
        would spare an insight held up only by the rows being removed.

        Args:
            run_id: Identifier of the run being cleared.
        """

    def find_existing_insights(
        self, candidates: Sequence[Cluster]
    ) -> dict[int, str | None]:
        """Map each candidate to the open insight the judge deems the same.

        Candidates are keyed by position, because labels are free text and could
        collide. What is offered to the judge:

        - Resolved insights are skipped, so a recurrence after resolution mints
          a new insight instead of reopening the old one.
        - A merged insight is skipped, so the next sweep attaches the recurrence
          to the target an operator chose rather than the row they hid.
        - A dismissed one is deliberately still matched. It stays hidden either
          way, and skipping it would mint a fresh insight every sweep for
          exactly the defect somebody asked not to see.

        Ordered newest-updated first, ``insight_id`` breaking ties -- a sweep
        stamps one timestamp across everything it touches, so ties are routine,
        and the order decides which batch of labels the judge answers from.

        Args:
            candidates: Sequence of candidate clusters to match.

        Returns:
            ``{candidate index: matched insight id or None}``. An absent key and
            a ``None`` both mean no match.
        """

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
        """Write a sweep's correlation result: mint, append, date, mark.

        A sweep establishes two separate facts about an insight it already had.
        ``seen_insight_ids`` is every insight a candidate matched, and dating
        those to ``now`` is what leaves `resolve_stale_insights` measuring from
        the last sighting. ``recurring_insight_ids`` is the subset a verdict
        confirmed, and only those change status, because an absent verdict is
        not a recurrence. The second set is contained in the first.

        ``relabelled`` renames an insight to the wording verification settled
        on, so the label a reader sees is the label the next sweep matches
        against.

        Both facts about a seen insight have to land together: no reader may
        observe one dated to this sweep while its status still says it was never
        seen again.

        Args:
            new_insights: Newly minted insights to insert.
            seen_insight_ids: Insight IDs matched by current candidates.
            recurring_insight_ids: Subset of seen insights confirmed by verdict.
            occurrences: Occurrences to persist for this run.
            now: Sighting timestamp.
            relabelled: Optional mapping of insight IDs to replacement labels.
        """

    def resolve_stale_insights(
        self, now: dt.datetime, window_days: int
    ) -> None:
        """Resolve this agent's insights unseen for ``window_days``.

        ``0`` disables it -- the documented "auto-resolve off" setting, which
        must touch nothing rather than resolve everything against a zero-day
        cutoff.

        Stamps ``resolved_at`` as well as the status, because when an issue went
        away is a fact nothing else records, and it cannot go in ``updated_at``:
        that is the last sighting, and it is what this very cutoff reads.
        Already-resolved insights are left alone, so a later sweep does not
        restamp a date it already wrote.

        Args:
            now: Timestamp marking resolution.
            window_days: Days of inactivity before an insight is resolved (0
                disables resolution).
        """


class InsightStore(InsightWriter, Protocol):
    """A complete store: everything a sweep writes, plus what an operator does.

    `insight_tools` is typed to this, because dismissing and merging are the two
    things the dashboard can do to an insight that no sweep does.
    """

    def dismiss_insight(self, insight_id: str) -> bool:
        """Hide one insight from the list; return whether it exists.

        Idempotent, and deliberately so: the dashboard's button is one click
        with no undo, and a double click must not read as a failure. Dismissing
        twice keeps the first judgement's time, so the caller can still tell
        "already dismissed" from "no such insight".

        Args:
            insight_id: Unique insight identifier.

        Returns:
            True if the insight existed and was dismissed, False otherwise.
        """

    def merge_insight(self, *, insight_id: str, target_insight_id: str) -> bool:
        """Record ``insight_id`` as a duplicate of ``target_insight_id``.

        Neither insight is better than the other; which is which is decided by
        the order an operator clicked. Afterwards the target answers for both:
        its occurrences, its root causes, and a span covering the union of what
        the two had seen.

        Chains flatten as they are written. Merging A into B and then B into C
        must leave A answering C, not B -- anything already pointing at the
        source moves with it.

        Refuses, returning ``False``, when the source does not exist, when the
        target does not exist, or when the target is itself already merged into
        something. The check and the move are one step: the dashboard fires
        several of these concurrently at one target, so a separate check could
        be true when it ran and false when the write landed.

        Not undoable, on either implementation.

        Args:
            insight_id: Duplicate insight identifier to merge away.
            target_insight_id: Target canonical insight identifier.

        Returns:
            True if the merge succeeded, False if either insight is unknown
            or the target is already merged.
        """
