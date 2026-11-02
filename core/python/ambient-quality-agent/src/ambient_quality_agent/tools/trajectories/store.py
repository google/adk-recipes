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

"""What a store of trajectories has to answer, apart from how it keeps them.

`bigquery_store.BigQueryTrajectoryStore` is the deployed implementation;
`standalone.InMemoryTrajectoryStore` is the other. This is the write path
ingestion takes, through `TrajectoryRecorder`; `reader.TrajectoryReader` is the
read path the dashboard and the chat tools take, and the two are separate
classes for the reason the insight pair is -- so nothing serving a dashboard can
delete a run's rows.

Two grains, and the whole contract turns on keeping them apart.

**An index row belongs to a sweep.** It is keyed ``(run_id, trajectory_id)``,
and `delete_run_trajectories` is what keeps that key true when the durable job is retried
from the start.

**A conversation belongs to nobody's run.** It is keyed
``(agent_name, trajectory_id)``, stored once however many sweeps sampled it, and
`delete_run_trajectories` must never touch one: deleting a copy this run wrote would destroy
evidence a different run's insight points at.

Where the implementations part is what `record_payloads` does with a
conversation that is already stored, and they part only in how the rows are
kept. BigQuery appends a second copy and chooses between them on every read,
because there is no cheap ``UPDATE`` there; an implementation that can change a
record applies the same preference at write time and keeps one (D19). Both
answer `TrajectoryReader.get_archives` with the same copy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ambient_quality_agent.tools.trajectories.models import PayloadStatus

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Sequence

    from ambient_quality_agent.tools.trajectories.models import (
        Trajectory,
        TrajectoryPayload,
    )
    from ambient_quality_agent.tools.trajectories.payloads import (
        TrajectoryArchive,
    )


def compute_payload_rank(
    payload: TrajectoryPayload,
) -> tuple[bool, dt.datetime]:
    """Computes a sort rank for choosing between stored copies of a conversation.

    Prefers fully ingested copies over partial or truncated ones, breaking ties by
    earliest creation timestamp.

    Args:
        payload: Trajectory payload to rank.

    Returns:
        A tuple of (is_incomplete, created_at) suitable for min() comparison.
    """
    return (payload.status is not PayloadStatus.INGESTED, payload.created_at)


class TrajectoryStore(Protocol):
    """One agent's sampled trajectories, and the conversations behind them."""

    def delete_run_trajectories(self, run_id: str) -> None:
        """Deletes index rows from a prior attempt of a run.

        Removes rows from the trajectories index table to ensure uniqueness across
        retried runs. Never deletes payloads, as archived conversations may be shared
        across sweeps.

        Args:
            run_id: Investigation run identifier to clean up.
        """

    def record(self, trajectories: Sequence[Trajectory]) -> None:
        """Appends a batch of trajectory index rows.

        Args:
            trajectories: Sequence of trajectory index records to store.
        """

    def record_payloads(self, archives: Sequence[TrajectoryArchive]) -> None:
        """Stores conversation payloads and turns.

        When a conversation is stored more than once, the copy that
        `compute_payload_rank` prefers (intact copies, then earliest creation
        time) is the one kept at write time or chosen at read time.

        Args:
            archives: Sequence of trajectory archives containing manifests and turns.

        Raises:
            Exception: If storage fails, after any retries the store makes.
        """
