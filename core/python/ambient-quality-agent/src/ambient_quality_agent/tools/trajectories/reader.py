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

"""Read-only query contract for investigation trajectories and trajectory bundles.

Implemented by `bigquery_reader.BigQueryTrajectoryReader` for deployments and
`standalone.InMemoryTrajectoryReader` for standalone mode. Kept separate from
`store.TrajectoryStore` so read paths cannot invoke write operations.

Both implementations enforce two rules:
1. **Processing outcomes are derived at read time.** A trajectory is
   ``not_ingested`` when `ingest_status` is ``NOT_INGESTED``, ``in_an_insight``
   when a tracked `InsightOccurrence` from the same investigation references it,
   and ``no_insight`` otherwise.
2. **Duplicate trajectory bundles resolve to a single preferred copy.** When a
   trajectory has been stored multiple times, the copy preferred by
   `store.compute_manifest_rank` (`bigquery_reader.PAYLOAD_PREFERENCE`) is
   returned.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ambient_quality_agent.tools.trajectories.bundles import (
        TrajectoryBundle,
    )
    from ambient_quality_agent.tools.trajectories.models import (
        DailyOutcome,
        InvestigationTrajectory,
        InvestigationTrajectoryView,
        TrajectoryProcessingState,
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
    """One agent's investigation trajectories and trajectory bundles, as the
    dashboard reads them back."""

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
        """Lists investigation trajectories matching filters, with a total count.

        Args:
            investigation_id: Optional investigation ID filter.
            outcome: Optional derived processing outcome filter.
            window_start: Optional ISO timestamp lower bound on creation time.
            window_end: Optional ISO timestamp upper bound on creation time.
            limit: Maximum records to return.
            offset: Number of records to skip.

        Returns:
            Tuple of (list of InvestigationTrajectoryView records, total match
            count).
        """

    def get_investigation_trajectories(
        self, keys: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], InvestigationTrajectory]:
        """Fetches investigation trajectories for (investigation_id, trajectory_id) pairs.

        Args:
            keys: Sequence of (investigation_id, trajectory_id) tuples.

        Returns:
            Mapping of (investigation_id, trajectory_id) to InvestigationTrajectory records.
        """

    def get_trajectory_bundles(
        self, trajectory_ids: Sequence[str]
    ) -> dict[str, TrajectoryBundle]:
        """Fetches the trajectory bundles of trajectory IDs.

        Args:
            trajectory_ids: Sequence of trajectory IDs to query.

        Returns:
            Mapping of trajectory ID to TrajectoryBundle objects.
        """
