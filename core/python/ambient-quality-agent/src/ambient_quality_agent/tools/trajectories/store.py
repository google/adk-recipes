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

"""Storage contract for writing investigation trajectories and trajectory bundles.

Implemented by `bigquery_store.BigQueryTrajectoryStore` for deployments and
`standalone.InMemoryTrajectoryStore` for standalone mode. Kept separate from
`reader.TrajectoryReader` so read-only callers cannot delete or write rows.

Two distinct grains are stored:
1. **Investigation trajectories** are keyed by ``(investigation_id, trajectory_id)``.
   `delete_investigation_trajectories` removes a prior attempt's rows when an
   investigation retries.
2. **Trajectory bundles** are keyed by ``(observed_agent_key, trajectory_id)``
   and shared across investigations. Retry cleanup never deletes trajectory
   bundles because other investigations' insights may reference them.

When a trajectory is stored more than once, `compute_manifest_rank` defines
which copy wins (preferring `TrajectoryStatus.INGESTED`, then earliest
`created_at`). BigQuery appends every copy and applies this rank on read;
`InMemoryTrajectoryStore` applies it on write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ambient_quality_agent.tools.trajectories.models import TrajectoryStatus

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Sequence

    from ambient_quality_agent.tools.trajectories.bundles import (
        TrajectoryBundle,
    )
    from ambient_quality_agent.tools.trajectories.models import (
        InvestigationTrajectory,
        TrajectoryManifest,
    )


def compute_manifest_rank(
    manifest: TrajectoryManifest,
) -> tuple[bool, dt.datetime]:
    """Computes a sort rank for choosing between stored copies of a trajectory.

    Prefers fully ingested copies over partial or truncated ones, breaking ties by
    earliest creation timestamp.

    Args:
        manifest: Trajectory manifest to rank.

    Returns:
        A tuple of (is_incomplete, created_at) suitable for min() comparison.
    """
    return (
        manifest.status is not TrajectoryStatus.INGESTED,
        manifest.created_at,
    )


class TrajectoryStore(Protocol):
    """One agent's investigation trajectories, and the trajectory bundles
    behind them."""

    def delete_investigation_trajectories(self, investigation_id: str) -> None:
        """Deletes investigation trajectories from a prior attempt.

        Keeps ``(investigation_id, trajectory_id)`` unique across retried investigations.
        Never deletes trajectory bundles, as they may be shared across
        investigations.

        Args:
            investigation_id: Investigation identifier to clean up.
        """

    def record_investigation_trajectories(
        self, investigation_trajectories: Sequence[InvestigationTrajectory]
    ) -> None:
        """Appends a batch of investigation trajectories.

        Args:
            investigation_trajectories: Investigation trajectories to store.
        """

    def record_trajectory_bundles(
        self, bundles: Sequence[TrajectoryBundle]
    ) -> None:
        """Stores trajectory bundles: each manifest and its steps.

        When a trajectory is stored more than once, the copy that
        `compute_manifest_rank` prefers (intact copies, then earliest creation
        time) is the one kept at write time or chosen at read time.

        Args:
            bundles: Trajectory bundles to store.

        Raises:
            Exception: If storage fails, after any retries the store makes.
        """
