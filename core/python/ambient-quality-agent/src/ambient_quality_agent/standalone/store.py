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
1. Runs are stored as single mutable records representing latest state.
2. Merged insight evidence is directly reassigned to surviving insights, allowing
   reads to query ownership directly without runtime coalescing.
3. Conversation archives keep the highest-ranking payload directly upon write.
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
        InvestigationEvent,
        InvestigationRecord,
    )
    from ambient_quality_agent.tools.trajectories.models import (
        Trajectory,
        TrajectoryPayload,
    )
    from ambient_quality_agent.tools.trajectories.payloads import (
        TrajectoryArchive,
    )


class Insights(NamedTuple):
    """Snapshot containing insights, occurrences, and root cause diagnoses.

    Passed and returned as a single unit so multi-entity mutations apply atomically.
    """

    insights: Mapping[str, Insight]
    occurrences: Sequence[InsightOccurrence]
    root_causes: Sequence[RootCause]


class InMemoryStore:
    """In-memory collections for a standalone session, initialized with no prior state."""

    def __init__(self) -> None:
        self._write = threading.Lock()
        self._runs: Mapping[str, InvestigationRecord] = {}
        self._run_events: Mapping[str, tuple[InvestigationEvent, ...]] = {}
        self._insights: Mapping[str, Insight] = {}
        self._occurrences: tuple[InsightOccurrence, ...] = ()
        self._root_causes: tuple[RootCause, ...] = ()
        self._trajectories: Mapping[tuple[str, str], Trajectory] = {}
        self._archives: Mapping[str, TrajectoryArchive] = {}

    # --- Runs --------------------------------------------------------------- #

    @property
    def runs(self) -> Mapping[str, InvestigationRecord]:
        """All investigation runs by ID, each holding its latest state."""
        return self._runs

    def list_run_events(self, run_id: str) -> tuple[InvestigationEvent, ...]:
        """Returns all progress events recorded for a run in chronological order.

        Args:
            run_id: Investigation run identifier.

        Returns:
            Tuple of `InvestigationEvent` records emitted for the run.
        """
        return self._run_events.get(run_id, ())

    def save_run(self, record: InvestigationRecord) -> None:
        """Stores a run record, replacing any existing record with the same ID.

        Args:
            record: Investigation run record to store.
        """
        with self._write:
            self._runs = {**self._runs, record.run_id: record}

    def append_run_event(self, run_id: str, event: InvestigationEvent) -> None:
        """Appends an event to a run's event sequence.

        Events are kept separate from run records so full-record replacements
        do not overwrite intermediate events emitted by workflow nodes.

        Args:
            run_id: Investigation run identifier.
            event: Event record to append.
        """
        with self._write:
            existing = self._run_events.get(run_id, ())
            self._run_events = {**self._run_events, run_id: (*existing, event)}

    # --- Insights ----------------------------------------------------------- #

    @property
    def insights(self) -> Mapping[str, Insight]:
        """All insights by ID, each holding its current state."""
        return self._insights

    @property
    def occurrences(self) -> tuple[InsightOccurrence, ...]:
        """All occurrences in recording order.

        Stored as a flat sequence because occurrences move between insights upon
        merging, and each occurrence references its active insight ID directly.
        """
        return self._occurrences

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
            insights, occurrences, root_causes = change(
                Insights(self._insights, self._occurrences, self._root_causes)
            )
            self._insights = dict(insights)
            self._occurrences = tuple(occurrences)
            self._root_causes = tuple(root_causes)

    # --- Trajectories --------------------------------------------------------- #

    @property
    def trajectories(self) -> Mapping[tuple[str, str], Trajectory]:
        """Trajectory index rows keyed by (run_id, trajectory_id).

        Keyed by pair because trajectory IDs are unique only within an
        investigation run.
        """
        return self._trajectories

    @property
    def archives(self) -> Mapping[str, TrajectoryArchive]:
        """Archived conversations keyed by trajectory ID.

        Stores a single preferred payload version per trajectory ID across all
        sweeps.
        """
        return self._archives

    def save_trajectories(self, trajectories: Sequence[Trajectory]) -> None:
        """Stores trajectory index records, replacing existing records with matching keys.

        Args:
            trajectories: Sequence of trajectory index records to store.
        """
        if not trajectories:
            return
        with self._write:
            self._trajectories = {
                **self._trajectories,
                **{(t.run_id, t.trajectory_id): t for t in trajectories},
            }

    def delete_run_trajectories(self, run_id: str, agent_name: str) -> None:
        """Removes trajectory index rows for a specific run and agent.

        Preserves conversation archives because they may serve as evidence for
        other investigation runs.

        Args:
            run_id: Investigation run identifier to clean up.
            agent_name: Name of the observed agent scoping the removal.
        """
        with self._write:
            self._trajectories = {
                key: trajectory
                for key, trajectory in self._trajectories.items()
                if not (
                    key[0] == run_id and trajectory.agent_name == agent_name
                )
            }

    def save_archive(
        self,
        archive: TrajectoryArchive,
        *,
        better: Callable[[TrajectoryPayload, TrajectoryPayload], bool],
    ) -> None:
        """Saves a conversation archive if preferred over the currently stored version.

        Evaluates payload preference under the write lock to prevent race conditions
        between concurrent sweeps.

        Args:
            archive: Arriving conversation archive.
            better: Callable returning True if the arriving payload should replace
                the currently stored payload.
        """
        key = archive.payload.trajectory_id
        with self._write:
            stored = self._archives.get(key)
            if stored is not None and not better(
                archive.payload, stored.payload
            ):
                return
            self._archives = {**self._archives, key: archive}
