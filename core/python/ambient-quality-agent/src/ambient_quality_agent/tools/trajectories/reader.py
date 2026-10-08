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

"""What a reader of trajectories has to answer, apart from how they are kept.

`bigquery_reader.BigQueryTrajectoryReader` is the deployed implementation;
`standalone.InMemoryTrajectoryReader` is the other. The counterpart to
`store.TrajectoryStore`: that is ingestion's write path, this is what the
dashboard and the chat tools read back, and they are separate classes so a read
can never reach the write machinery.

Four reads:

* `count_daily_outcomes` -- the per-day chart, one count per (day, outcome).
* `list_trajectories` -- the dots behind a bar, one row per trajectory.
* `get_trajectories` -- the rows behind an insight's occurrence, and with them
  the source trace ids of every conversation it was found in.
* `get_archives` -- the archived conversations themselves, reassembled.

Two rules run through them, and they are what the implementations must agree on.

**An outcome is derived, never stored.** A trajectory is ``not_ingested`` when
ingestion made nothing of it, ``in_an_insight`` when an insight occurrence of
the *same run* names it, and ``no_insight`` otherwise. The order matters:
something no analysis could see must not be filed under "evaluated and clean".
An occurrence naming no insight is a candidate verification rejected or never
judged, and does not count as evidence. Deriving it is what keeps the
trajectories append-only -- a cleaned-up run cannot leave a stale flag behind.

**One conversation, one answer.** A conversation can have been sampled by many
sweeps and, on an append-only store, stored more than once;
`bigquery_reader.PAYLOAD_PREFERENCE` says which copy is the one. Whether an
implementation applies that when it writes or when it reads, `get_archives`
returns the same copy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ambient_quality_agent.tools.trajectories.models import (
        DailyOutcome,
        Trajectory,
        TrajectoryProcessingState,
        TrajectoryView,
    )
    from ambient_quality_agent.tools.trajectories.payloads import (
        TrajectoryArchive,
    )

_TRACE_CONSOLE_BASE = "https://console.cloud.google.com/traces/list"
"""Base URL for the Cloud Trace deep link (see `build_trace_console_url`)."""


def build_trace_console_url(project_id: str, trace_ids: Sequence[str]) -> str:
    """Constructs a Google Cloud Trace console URL for a trajectory.

    For multi-turn sessions, links to the first trace in chronological order,
    representing the opening turn.

    Args:
        project_id: GCP project identifier.
        trace_ids: Chronologically ordered trace IDs.

    Returns:
        Console URL string, or empty string if project_id or trace_ids are empty.
    """
    if not project_id or not trace_ids or not trace_ids[0]:
        return ""
    return f"{_TRACE_CONSOLE_BASE}?project={project_id}&tid={trace_ids[0]}"


class TrajectoryReader(Protocol):
    """One agent's sampled trajectories, as the dashboard reads them back."""

    def count_daily_outcomes(
        self, *, window_start: str | None = None, window_end: str | None = None
    ) -> list[DailyOutcome]:
        """Counts sampled trajectories per day and outcome within a time window.

        Args:
            window_start: Optional ISO timestamp lower bound on creation time.
            window_end: Optional ISO timestamp upper bound on creation time.

        Returns:
            List of DailyOutcome records ordered chronologically.
        """

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
        """Lists trajectories matching filters with pagination and total count.

        Args:
            run_id: Optional investigation run ID filter.
            outcome: Optional derived processing outcome filter.
            window_start: Optional ISO timestamp lower bound on creation time.
            window_end: Optional ISO timestamp upper bound on creation time.
            limit: Maximum records to return.
            offset: Number of records to skip.

        Returns:
            Tuple of (list of TrajectoryView records, total match count).
        """

    def get_trajectories(
        self, keys: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], Trajectory]:
        """Fetches stored trajectory records for (run_id, trajectory_id) pairs.

        Args:
            keys: Sequence of (run_id, trajectory_id) tuples.

        Returns:
            Mapping of (run_id, trajectory_id) to Trajectory records.
        """

    def get_archives(
        self, trajectory_ids: Sequence[str]
    ) -> dict[str, TrajectoryArchive]:
        """Fetches archived conversation manifests and turns for trajectory IDs.

        Args:
            trajectory_ids: Sequence of trajectory IDs to query.

        Returns:
            Mapping of trajectory ID to TrajectoryArchive objects.
        """
