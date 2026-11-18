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

"""In-memory backing store for standalone AQuA execution.

Provides unified in-process storage across all persisted AQuA entities, shared
between background investigation sweeps and foreground API handlers. Entity
adapters expose this store through standard storage contracts.

## Concurrency Model

Investigation sweeps execute on background threads while API routes serve
concurrently. Safe concurrent access is achieved through:
1. **Single write lock (`_write`)**: Mutations acquire `_write` for the duration
   of read-modify-write operations, creating new collection instances.
2. **Lock-free reads**: Mutated collections are rebound atomically under the GIL.
   Readers observe either complete pre-mutation or post-mutation states without
   locking.

## Storage Design

Storage structures are optimized for in-memory read performance:
1. Investigations are stored as single mutable records representing latest
   state.
2. Merged insight evidence is directly reassigned to surviving insights, allowing
   reads to query ownership directly without runtime coalescing.
3. Trajectory bundles keep the highest-ranking manifest directly upon write.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, NamedTuple

from ambient_quality_agent.tools.insights.models import (
    Insight,
    InsightOccurrence,
    RootCause,
)

if TYPE_CHECKING:
    from ambient_quality_agent.tools.investigations.models import (
        Investigation,
        InvestigationEvent,
    )
    from ambient_quality_agent.tools.trajectories.bundles import (
        TrajectoryBundle,
    )
    from ambient_quality_agent.tools.trajectories.models import (
        InvestigationTrajectory,
        TrajectoryManifest,
    )


class Insights(NamedTuple):
    """Snapshot containing insights, occurrences, and root cause diagnoses.

    Passed and returned as a single unit so multi-entity mutations apply atomically.
    """

    insights: Mapping[str, Insight]
    insight_occurrences: Sequence[InsightOccurrence]
    root_causes: Sequence[RootCause]


class InMemoryStore:
    """In-memory collections for a standalone session, initialized with no prior state."""

    def __init__(self) -> None:
        self._write = threading.Lock()
        self._investigations: Mapping[str, Investigation] = {}
        self._investigation_events: Mapping[
            str, tuple[InvestigationEvent, ...]
        ] = {}
        self._insights: Mapping[str, Insight] = {}
        self._insight_occurrences: tuple[InsightOccurrence, ...] = ()
        self._root_causes: tuple[RootCause, ...] = ()
        self._investigation_trajectories: Mapping[
            tuple[str, str], InvestigationTrajectory
        ] = {}
        self._trajectory_bundles: Mapping[str, TrajectoryBundle] = {}

    # --- Investigations ----------------------------------------------------- #

    @property
    def investigations(self) -> Mapping[str, Investigation]:
        """All investigations by ID, each holding its latest state."""
        return self._investigations

    def list_investigation_events(
        self, investigation_id: str
    ) -> tuple[InvestigationEvent, ...]:
        """Returns the progress events of an investigation in chronological order.

        Args:
            investigation_id: Investigation identifier.

        Returns:
            Tuple of `InvestigationEvent` records emitted for the investigation.
        """
        return self._investigation_events.get(investigation_id, ())

    def save_investigation(self, record: Investigation) -> None:
        """Stores an investigation, replacing any existing one with the same ID.

        Args:
            record: Investigation to store.
        """
        with self._write:
            self._investigations = {
                **self._investigations,
                record.investigation_id: record,
            }

    def append_investigation_event(
        self, investigation_id: str, event: InvestigationEvent
    ) -> None:
        """Appends an event to an investigation's event sequence.

        Events are kept separate from investigations so full-record replacements
        do not overwrite intermediate events emitted by workflow nodes.

        Args:
            investigation_id: Investigation identifier.
            event: Event record to append.
        """
        with self._write:
            existing = self._investigation_events.get(investigation_id, ())
            self._investigation_events = {
                **self._investigation_events,
                investigation_id: (*existing, event),
            }

    # --- Insights ----------------------------------------------------------- #

    @property
    def insights(self) -> Mapping[str, Insight]:
        """All insights by ID, each holding its current state."""
        return self._insights

    @property
    def insight_occurrences(self) -> tuple[InsightOccurrence, ...]:
        """All insight occurrences in recording order.

        Stored as a flat sequence because occurrences move between insights upon
        merging, and each occurrence references its active insight ID directly.
        """
        return self._insight_occurrences

    @property
    def root_causes(self) -> tuple[RootCause, ...]:
        """All diagnoses in append order.

        Preserved alongside occurrences so diagnoses follow insight merges.
        Revisions accumulate so the latest entry for an occurrence represents its
        current state.
        """
        return self._root_causes

    def append_root_cause(self, record: RootCause) -> None:
        """Appends a root cause diagnosis record.

        Args:
            record: Root cause record to append.
        """
        with self._write:
            self._root_causes = (*self._root_causes, record)

    def update_insights(self, change: Callable[[Insights], Insights]) -> None:
        """Applies an atomic mutation across insights, occurrences, and diagnoses.

        Executes under the write lock so multi-entity mutations (such as merges)
        never expose partial state transitions to concurrent readers.

        Args:
            change: Callable that receives the current `Insights` and returns an
                updated `Insights` instance. Must not block.
        """
        with self._write:
            insights, insight_occurrences, root_causes = change(
                Insights(
                    self._insights,
                    self._insight_occurrences,
                    self._root_causes,
                )
            )
            self._insights = dict(insights)
            self._insight_occurrences = tuple(insight_occurrences)
            self._root_causes = tuple(root_causes)

    # --- Trajectories ------------------------------------------------------- #

    @property
    def investigation_trajectories(
        self,
    ) -> Mapping[tuple[str, str], InvestigationTrajectory]:
        """Investigation trajectories keyed by (investigation_id, trajectory_id).

        Keyed by pair because trajectory IDs are unique only within an
        investigation.
        """
        return self._investigation_trajectories

    @property
    def trajectory_bundles(self) -> Mapping[str, TrajectoryBundle]:
        """Trajectory bundles keyed by trajectory ID.

        Stores a single preferred copy per trajectory ID across all
        investigations.
        """
        return self._trajectory_bundles

    def save_investigation_trajectories(
        self, investigation_trajectories: Sequence[InvestigationTrajectory]
    ) -> None:
        """Stores investigation trajectories, replacing any with matching keys.

        Args:
            investigation_trajectories: Investigation trajectories to store.
        """
        if not investigation_trajectories:
            return
        with self._write:
            self._investigation_trajectories = {
                **self._investigation_trajectories,
                **{
                    (t.investigation_id, t.trajectory_id): t
                    for t in investigation_trajectories
                },
            }

    def delete_investigation_trajectories(
        self, investigation_id: str, observed_agent_key: str
    ) -> None:
        """Removes the investigation trajectories of one investigation and agent.

        Preserves trajectory bundles because they may serve as evidence for
        other investigations.

        Args:
            investigation_id: Investigation identifier to clean up.
            observed_agent_key: Key of the observed agent scoping the removal.
        """
        with self._write:
            self._investigation_trajectories = {
                key: trajectory
                for key, trajectory in self._investigation_trajectories.items()
                if not (
                    key[0] == investigation_id
                    and trajectory.observed_agent_key == observed_agent_key
                )
            }

    def save_trajectory_bundle(
        self,
        bundle: TrajectoryBundle,
        *,
        better: Callable[[TrajectoryManifest, TrajectoryManifest], bool],
    ) -> None:
        """Saves a trajectory bundle if preferred over the stored one.

        Evaluates manifest preference under the write lock to prevent race
        conditions between concurrent investigations.

        Args:
            bundle: Arriving trajectory bundle.
            better: Callable returning True if the arriving manifest should
                replace the currently stored manifest.
        """
        key = bundle.manifest.trajectory_id
        with self._write:
            stored = self._trajectory_bundles.get(key)
            if stored is not None and not better(
                bundle.manifest, stored.manifest
            ):
                return
            self._trajectory_bundles = {**self._trajectory_bundles, key: bundle}
