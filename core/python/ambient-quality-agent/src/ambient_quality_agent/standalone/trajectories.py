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

"""In-memory trajectory storage and reader implementations for standalone execution.

Combines write and read implementations:
- `InMemoryTrajectoryStore`: Implements `TrajectoryStore` for ingestion.
- `InMemoryTrajectoryReader`: Implements `TrajectoryReader` for dashboard and chat queries.

## Manifest Reconciliation and Storage

Trajectory bundles reconcile manifest versions during write operations:
1. Incoming trajectory bundles are compared against existing stored copies
   using `compute_manifest_rank`, storing only the higher-ranking manifest.
2. Manifests and steps are stored together within unified `TrajectoryBundle`
   objects, preventing partial or mismatched state.

## Dynamic Outcome Computation

Trajectory outcomes (`TrajectoryProcessingState`) are derived dynamically on read
rather than stored at ingestion time. Because outcome classification depends on
insight occurrences produced during correlation (after ingestion completes),
deriving outcomes dynamically ensures state consistency without mutating prior
investigation trajectories.
"""

from __future__ import annotations

import collections
import datetime as dt
import logging
from typing import TYPE_CHECKING

from ambient_quality_agent.tools.trajectories.models import (
    DailyOutcome,
    IngestStatus,
    InvestigationTrajectoryView,
    TrajectoryProcessingState,
)
from ambient_quality_agent.tools.trajectories.store import compute_manifest_rank

if TYPE_CHECKING:
    from collections.abc import Container, Sequence

    from ambient_quality_agent.standalone.store import InMemoryStore
    from ambient_quality_agent.tools.trajectories.bundles import (
        TrajectoryBundle,
    )
    from ambient_quality_agent.tools.trajectories.models import (
        InvestigationTrajectory,
        TrajectoryManifest,
    )

logger = logging.getLogger(__name__)


class InMemoryTrajectoryStore:
    """One agent's investigation trajectories and trajectory bundles, over the
    in-process `InMemoryStore`."""

    def __init__(
        self, store: InMemoryStore, *, observed_agent_key: str
    ) -> None:
        """Initializes the trajectory store scoped to an observed agent.

        Args:
            store: Backing in-memory store.
            observed_agent_key: Key of the observed agent scoping cleanups and storage.
        """
        self._store = store
        self._observed_agent_key = observed_agent_key

    def delete_investigation_trajectories(self, investigation_id: str) -> None:
        """Deletes the investigation trajectories one investigation wrote.

        Args:
            investigation_id: Investigation identifier to clean up.
        """
        self._store.delete_investigation_trajectories(
            investigation_id, self._observed_agent_key
        )

    def record_investigation_trajectories(
        self, investigation_trajectories: Sequence[InvestigationTrajectory]
    ) -> None:
        """Stores a batch of investigation trajectories.

        Args:
            investigation_trajectories: Investigation trajectories to store.
        """
        self._store.save_investigation_trajectories(investigation_trajectories)

    def record_trajectory_bundles(
        self, bundles: Sequence[TrajectoryBundle]
    ) -> None:
        """Stores trajectory bundles, keeping the higher-ranking copy of duplicates.

        Args:
            bundles: Trajectory bundles to persist.
        """
        for bundle in bundles:
            self._store.save_trajectory_bundle(bundle, better=_is_better)


class InMemoryTrajectoryReader:
    """One agent's investigation trajectories and trajectory bundles, read out
    of the `InMemoryStore`."""

    def __init__(
        self, store: InMemoryStore, *, observed_agent_key: str
    ) -> None:
        self._store = store
        self._observed_agent_key = observed_agent_key

    def count_daily_outcomes(
        self, *, window_start: str | None = None, window_end: str | None = None
    ) -> list[DailyOutcome]:
        """Counts trajectory processing outcomes grouped by UTC calendar day.

        Args:
            window_start: Optional ISO timestamp lower bound for creation time.
            window_end: Optional ISO timestamp upper bound for creation time.

        Returns:
            List of `DailyOutcome` instances sorted by day and outcome. Days without
            trajectories are omitted.
        """
        counts: collections.Counter[
            tuple[dt.date, TrajectoryProcessingState]
        ] = collections.Counter()
        evidence = self._compute_evidence_keys()
        for trajectory in self._list_matching_investigation_trajectories(
            window_start=window_start, window_end=window_end
        ):
            day = trajectory.created_at.astimezone(dt.UTC).date()
            counts[day, _compute_outcome(trajectory, evidence)] += 1
        # Days without trajectories are omitted so callers can construct custom buckets.
        return [
            DailyOutcome(day=day, outcome=outcome, trajectories=total)
            for (day, outcome), total in sorted(counts.items())
        ]

    def list_investigation_trajectories(
        self,
        *,
        investigation_id: str | None = None,
        outcome: TrajectoryProcessingState | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[InvestigationTrajectoryView], int]:
        """Returns a paginated list of trajectories and total match count.

        Args:
            investigation_id: Optional investigation ID filter.
            outcome: Optional trajectory processing state filter.
            window_start: Optional ISO timestamp lower bound for creation time.
            window_end: Optional ISO timestamp upper bound for creation time.
            limit: Maximum number of trajectories to return.
            offset: Number of matching trajectories to skip.

        Returns:
            A tuple of (paginated trajectory views, total matching count).
        """
        evidence = self._compute_evidence_keys()
        views = []
        for trajectory in self._list_matching_investigation_trajectories(
            investigation_id=investigation_id,
            window_start=window_start,
            window_end=window_end,
        ):
            # Uses the same outcome derivation as count_daily_outcomes to ensure consistent drill-down views.
            derived = _compute_outcome(trajectory, evidence)
            if outcome is not None and derived != outcome:
                continue
            views.append(
                InvestigationTrajectoryView(
                    **trajectory.model_dump(), outcome=derived
                )
            )
        views.sort(key=lambda v: (-v.created_at.timestamp(), v.trajectory_id))
        return views[offset : offset + limit], len(views)

    def get_investigation_trajectories(
        self, keys: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], InvestigationTrajectory]:
        """Retrieves stored trajectory rows by (investigation_id, trajectory_id) pairs.

        Args:
            keys: Sequence of (investigation_id, trajectory_id) tuples to fetch.

        Returns:
            Dictionary mapping found keys to their `InvestigationTrajectory` records.
        """
        found = {}
        for key in set(keys):
            trajectory = self._store.investigation_trajectories.get(key)
            if (
                trajectory is not None
                and trajectory.observed_agent_key == self._observed_agent_key
            ):
                found[key] = trajectory
        return found

    def get_trajectory_bundles(
        self, trajectory_ids: Sequence[str]
    ) -> dict[str, TrajectoryBundle]:
        """Retrieves trajectory bundles by trajectory ID.

        Args:
            trajectory_ids: Sequence of trajectory IDs to fetch.

        Returns:
            Dictionary mapping trajectory IDs to their `TrajectoryBundle` records.
        """
        stored = self._store.trajectory_bundles
        found = {}
        for trajectory_id in set(trajectory_ids):
            bundle = stored.get(trajectory_id)
            if (
                bundle is not None
                and bundle.manifest.observed_agent_key
                == self._observed_agent_key
            ):
                found[trajectory_id] = bundle
        return found

    def _list_matching_investigation_trajectories(
        self,
        *,
        investigation_id: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> list[InvestigationTrajectory]:
        """Lists trajectory records for this agent within the given window.

        Args:
            investigation_id: Optional investigation ID filter.
            window_start: Optional ISO timestamp lower bound.
            window_end: Optional ISO timestamp upper bound.

        Returns:
            Unordered list of matching `InvestigationTrajectory` objects.
        """
        start = _parse_instant(window_start)
        end = _parse_instant(window_end)
        kept = []
        for trajectory in self._store.investigation_trajectories.values():
            if trajectory.observed_agent_key != self._observed_agent_key:
                continue
            if (
                investigation_id is not None
                and trajectory.investigation_id != investigation_id
            ):
                continue
            if start is not None and trajectory.created_at < start:
                continue
            if end is not None and trajectory.created_at > end:
                continue
            kept.append(trajectory)
        return kept

    def _compute_evidence_keys(self) -> set[tuple[str, str]]:
        """Collects (investigation_id, trajectory_id) pairs referenced by confirmed insights.

        Occurrences without an insight ID represent unconfirmed or rejected
        candidates and are excluded so unverified samples are not counted as insight evidence.

        Returns:
            Set of (investigation_id, trajectory_id) tuples tied to confirmed insights.
        """
        return {
            (occurrence.investigation_id, trajectory_id)
            for occurrence in self._store.insight_occurrences
            if occurrence.observed_agent_key == self._observed_agent_key
            and occurrence.insight_id is not None
            for trajectory_id in occurrence.trajectory_ids
        }


def _compute_outcome(
    trajectory: InvestigationTrajectory, evidence: Container[tuple[str, str]]
) -> TrajectoryProcessingState:
    """Derives the processing outcome for an investigation trajectory.

    Evaluates state in the following order:
    1. Returns `NOT_INGESTED` if ingestion failed, preventing unanalyzed
       trajectories from appearing as evaluated.
    2. Returns `IN_AN_INSIGHT` if the trajectory is referenced by a confirmed
       insight occurrence.
    3. Returns `NO_INSIGHT` if ingestion succeeded without linking to an
       insight.

    Args:
        trajectory: The investigation trajectory record to evaluate.
        evidence: Container of ``(investigation_id, trajectory_id)`` tuples
            present in confirmed insights.

    Returns:
        The derived `TrajectoryProcessingState`.
    """
    if trajectory.ingest_status is IngestStatus.NOT_INGESTED:
        return TrajectoryProcessingState.NOT_INGESTED
    if (trajectory.investigation_id, trajectory.trajectory_id) in evidence:
        return TrajectoryProcessingState.IN_AN_INSIGHT
    return TrajectoryProcessingState.NO_INSIGHT


def _is_better(
    arriving: TrajectoryManifest, stored: TrajectoryManifest
) -> bool:
    """Determines whether an arriving manifest should replace a stored copy.

    Compares ranks using `compute_manifest_rank` where lower rank indicates higher
    preference. Equal ranks return False to avoid redundant writes.

    Args:
        arriving: Incoming trajectory manifest.
        stored: Currently stored trajectory manifest.

    Returns:
        True if the arriving manifest has a strictly lower rank, False otherwise.
    """
    return compute_manifest_rank(arriving) < compute_manifest_rank(stored)


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
        logger.warning("trajectories: unparseable window bound %r", stamp)
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.UTC)
