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

## Payload Reconciliation and Storage

Conversation archives reconcile payload versions during write operations:
1. Incoming conversation archives are compared against existing stored copies
   using `compute_payload_rank`, storing only the higher-ranking payload.
2. Manifests and turns are stored together within unified `TrajectoryArchive`
   objects, preventing partial or mismatched state.

## Dynamic Outcome Computation

Trajectory outcomes (`TrajectoryProcessingState`) are derived dynamically on read
rather than stored at ingestion time. Because outcome classification depends on
insight occurrences produced during correlation (after ingestion completes),
deriving outcomes dynamically ensures state consistency without mutating prior
ingestion rows.
"""

from __future__ import annotations

import collections
import datetime as dt
import logging
from typing import TYPE_CHECKING

from ambient_quality_agent.tools.trajectories.models import (
    DailyOutcome,
    IngestStatus,
    TrajectoryProcessingState,
    TrajectoryView,
)
from ambient_quality_agent.tools.trajectories.store import compute_payload_rank

if TYPE_CHECKING:
    from collections.abc import Container, Sequence

    from ambient_quality_agent.standalone.store import InMemoryStore
    from ambient_quality_agent.tools.trajectories.models import (
        Trajectory,
        TrajectoryPayload,
    )
    from ambient_quality_agent.tools.trajectories.payloads import (
        TrajectoryArchive,
    )

logger = logging.getLogger(__name__)


class InMemoryTrajectoryStore:
    """One agent's sampled trajectories, over the in-process `InMemoryStore`."""

    def __init__(self, store: InMemoryStore, *, agent_name: str) -> None:
        """Initializes the trajectory store scoped to an observed agent.

        Args:
            store: Backing in-memory store.
            agent_name: Name of the observed agent scoping cleanups and storage.
        """
        self._store = store
        self._agent_name = agent_name

    def delete_run_trajectories(self, run_id: str) -> None:
        """Deletes trajectory index rows for a run attempt to prepare for retries.

        Args:
            run_id: Investigation run identifier to clean up.
        """
        self._store.delete_run_trajectories(run_id, self._agent_name)

    def record(self, trajectories: Sequence[Trajectory]) -> None:
        """Stores a batch of trajectory index records.

        Args:
            trajectories: Sequence of trajectory records to store.
        """
        self._store.save_trajectories(trajectories)

    def record_payloads(self, archives: Sequence[TrajectoryArchive]) -> None:
        """Stores conversation payloads, retaining the higher-ranking copy of duplicates.

        Args:
            archives: Sequence of conversation archives to persist.
        """
        for archive in archives:
            self._store.save_archive(archive, better=_is_better)


class InMemoryTrajectoryReader:
    """One agent's sampled trajectories, read out of the `InMemoryStore`."""

    def __init__(self, store: InMemoryStore, *, agent_name: str) -> None:
        self._store = store
        self._agent_name = agent_name

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
        for trajectory in self._list_matching_trajectories(
            window_start=window_start, window_end=window_end
        ):
            day = trajectory.created_at.astimezone(dt.UTC).date()
            counts[day, _compute_outcome(trajectory, evidence)] += 1
        # Days without trajectories are omitted so callers can construct custom buckets.
        return [
            DailyOutcome(day=day, outcome=outcome, trajectories=total)
            for (day, outcome), total in sorted(counts.items())
        ]

    def list_trajectories(
        self,
        *,
        run_id: str | None = None,
        outcome: TrajectoryProcessingState | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[TrajectoryView], int]:
        """Returns a paginated list of trajectories and total match count.

        Args:
            run_id: Optional investigation run ID filter.
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
        for trajectory in self._list_matching_trajectories(
            run_id=run_id, window_start=window_start, window_end=window_end
        ):
            # Uses the same outcome derivation as count_daily_outcomes to ensure consistent drill-down views.
            derived = _compute_outcome(trajectory, evidence)
            if outcome is not None and derived != outcome:
                continue
            views.append(
                TrajectoryView(**trajectory.model_dump(), outcome=derived)
            )
        views.sort(key=lambda v: (-v.created_at.timestamp(), v.trajectory_id))
        return views[offset : offset + limit], len(views)

    def get_trajectories(
        self, keys: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], Trajectory]:
        """Retrieves stored trajectory rows by (run_id, trajectory_id) pairs.

        Args:
            keys: Sequence of (run_id, trajectory_id) tuples to fetch.

        Returns:
            Dictionary mapping found keys to their `Trajectory` records.
        """
        found = {}
        for key in set(keys):
            trajectory = self._store.trajectories.get(key)
            if (
                trajectory is not None
                and trajectory.agent_name == self._agent_name
            ):
                found[key] = trajectory
        return found

    def get_archives(
        self, trajectory_ids: Sequence[str]
    ) -> dict[str, TrajectoryArchive]:
        """Retrieves archived conversations by trajectory ID.

        Args:
            trajectory_ids: Sequence of trajectory IDs to fetch.

        Returns:
            Dictionary mapping trajectory IDs to their `TrajectoryArchive` records.
        """
        archives = self._store.archives
        found = {}
        for trajectory_id in set(trajectory_ids):
            archive = archives.get(trajectory_id)
            if (
                archive is not None
                and archive.payload.agent_name == self._agent_name
            ):
                found[trajectory_id] = archive
        return found

    def _list_matching_trajectories(
        self,
        *,
        run_id: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> list[Trajectory]:
        """Lists trajectory records for this agent within the given window.

        Args:
            run_id: Optional investigation run ID filter.
            window_start: Optional ISO timestamp lower bound.
            window_end: Optional ISO timestamp upper bound.

        Returns:
            Unordered list of matching `Trajectory` objects.
        """
        start = _parse_instant(window_start)
        end = _parse_instant(window_end)
        kept = []
        for trajectory in self._store.trajectories.values():
            if trajectory.agent_name != self._agent_name:
                continue
            if run_id is not None and trajectory.run_id != run_id:
                continue
            if start is not None and trajectory.created_at < start:
                continue
            if end is not None and trajectory.created_at > end:
                continue
            kept.append(trajectory)
        return kept

    def _compute_evidence_keys(self) -> set[tuple[str, str]]:
        """Collects (run_id, trajectory_id) pairs referenced by confirmed insights.

        Occurrences without an insight ID represent unconfirmed or rejected
        candidates and are excluded so unverified samples are not counted as insight evidence.

        Returns:
            Set of (run_id, trajectory_id) tuples tied to confirmed insights.
        """
        return {
            (occurrence.run_id, trajectory_id)
            for occurrence in self._store.occurrences
            if occurrence.agent_name == self._agent_name
            and occurrence.insight_id is not None
            for trajectory_id in occurrence.trajectory_ids
        }


def _compute_outcome(
    trajectory: Trajectory, evidence: Container[tuple[str, str]]
) -> TrajectoryProcessingState:
    """Derives the processing outcome for a trajectory.

    Evaluates state in the following order:
    1. Returns NOT_INGESTED if ingestion failed, preventing unanalyzed traces
       from appearing as evaluated.
    2. Returns IN_AN_INSIGHT if the trajectory is referenced by confirmed insight evidence.
    3. Returns NO_INSIGHT if ingestion succeeded without linking to an insight.

    Args:
        trajectory: The trajectory record to evaluate.
        evidence: Container of (run_id, trajectory_id) tuples present in confirmed insights.

    Returns:
        The derived `TrajectoryProcessingState`.
    """
    if trajectory.ingest_status is IngestStatus.NOT_INGESTED:
        return TrajectoryProcessingState.NOT_INGESTED
    if (trajectory.run_id, trajectory.trajectory_id) in evidence:
        return TrajectoryProcessingState.IN_AN_INSIGHT
    return TrajectoryProcessingState.NO_INSIGHT


def _is_better(arriving: TrajectoryPayload, stored: TrajectoryPayload) -> bool:
    """Determines whether an arriving conversation payload should replace a stored copy.

    Compares ranks using `compute_payload_rank` where lower rank indicates higher
    preference. Equal ranks return False to avoid redundant writes.

    Args:
        arriving: Incoming trajectory payload.
        stored: Currently stored trajectory payload.

    Returns:
        True if the arriving payload has a strictly lower rank, False otherwise.
    """
    return compute_payload_rank(arriving) < compute_payload_rank(stored)


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
