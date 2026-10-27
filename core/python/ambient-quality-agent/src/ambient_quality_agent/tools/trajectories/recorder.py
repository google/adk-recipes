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

"""The seam ingestion writes trajectories through.

Injected into a fetcher the way a logger is: the fetcher calls `add` for every
row it attempts and `flush` at the end of each page, and knows nothing about
BigQuery, the sweep it is serving, or whether anyone is listening. Everything
the row needs but a fetcher cannot know -- which run, which agent, which
telemetry source, and when -- is bound here once, by whoever built the fetcher.

That is what keeps the ids out of the ingestion layer's return values. They are
needed to *write* a trajectory and nowhere else: no phase after ingestion reads
a session id or a trace id, and the ones that want trajectory ids get them off
the findings. So they go straight to the table and are read back from it, rather
than being ferried out through `Page` for one consumer to pick up.

`NullTrajectoryRecorder` is the default, so a fetcher built outside a sweep --
the offline quality harness, most tests -- needs no wiring and writes nothing.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from ambient_quality_agent.tools.trajectories.models import (
    IngestStatus,
    Trajectory,
)
from ambient_quality_agent.tools.trajectories.payloads import build_archive

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agentplatform._genai.types import EvalCase
    from ambient_quality_agent.tools.trajectories.payloads import (
        TrajectoryArchive,
    )
    from ambient_quality_agent.tools.trajectories.store import TrajectoryStore

logger = logging.getLogger(__name__)


class NullTrajectoryRecorder:
    """Accepts everything and records nothing.

    A null object rather than an optional, so no caller has to guard: ingestion
    always has somewhere to report to, and a fetcher used outside a sweep works
    unchanged.
    """

    def add(
        self,
        *,
        trajectory_id: str,
        session_id: str | None,
        trace_ids: Sequence[str],
        status: IngestStatus,
        case: EvalCase | None = None,
        agent_revision: str = "",
    ) -> None:
        """No-op handler for adding an attempted trajectory row."""

    def flush(self) -> None:
        """No-op handler for flushing buffered rows."""


class TrajectoryRecorder(NullTrajectoryRecorder):
    """Writes one agent's sampled trajectories for one run.

    Buffers a page's worth and writes them together, because that is the unit
    ingestion produces and one write per page is what the store is sized for.
    Nothing accumulates across pages.

    Both halves of a page go through this one seam -- the ids of every row
    ingestion attempted, and the conversations it built from them -- because
    ingestion has a single moment holding both, and a second seam would make a
    fetcher report the same row twice.
    """

    def __init__(
        self,
        *,
        store: TrajectoryStore,
        run_id: str,
        agent_name: str,
        source: str,
    ) -> None:
        """Bind the recorder to one sweep.

        Args:
            store: Where both the index rows and the conversations go.
            run_id: The sweep every row is attributed to, and the key its retry
                cleanup deletes by.
            agent_name: Observed agent, denormalized onto every row.
            source: Telemetry source the rows were ingested from.
        """
        self._store = store
        self._run_id = run_id
        self._agent_name = agent_name
        self._source = source
        self._pending: list[Trajectory] = []
        self._pending_archives: list[TrajectoryArchive] = []

    def add(
        self,
        *,
        trajectory_id: str,
        session_id: str | None,
        trace_ids: Sequence[str],
        status: IngestStatus,
        case: EvalCase | None = None,
        agent_revision: str = "",
    ) -> None:
        """Buffers an attempted trajectory and its assembled payload for writing.

        Args:
            trajectory_id: Identifier for the conversation trajectory.
            session_id: Optional parent session ID for single-turn trajectories.
            trace_ids: Sequence of Cloud Trace IDs for the trajectory.
            status: Ingestion status.
            case: Assembled EvalCase, or None if ingestion could not build a case.
            agent_revision: Deployment revision associated with the case.
        """
        recorded_at = dt.datetime.now(dt.UTC)
        self._pending.append(
            Trajectory(
                run_id=self._run_id,
                agent_name=self._agent_name,
                trajectory_id=trajectory_id,
                created_at=recorded_at,
                source=self._source or None,
                source_session_id=session_id,
                source_trace_ids=list(trace_ids),
                ingest_status=status,
            )
        )
        if case is None:
            return
        self._pending_archives.append(
            build_archive(
                case,
                agent_name=self._agent_name,
                trajectory_id=trajectory_id,
                created_at=recorded_at,
                partial=status is IngestStatus.PARTIAL,
                agent_revision=agent_revision,
            )
        )

    def flush(self) -> None:
        """Flushes buffered index rows and conversation payloads.

        Writes proceed in two steps:
        1. Write index rows (best-effort; failures are logged so sweep execution continues).
        2. Write conversation payloads (retried on failure; raises on final error to protect evidence).

        A failed index write does not stop the payload write, so it can leave payloads
        without index rows. Buffers are cleared regardless of write outcome.
        """
        pending, self._pending = self._pending, []
        archives, self._pending_archives = self._pending_archives, []
        if pending:
            try:
                self._store.record(pending)
            except Exception as exc:
                logger.warning(
                    "Could not record %d sampled trajectory(s) of run %s: %s",
                    len(pending),
                    self._run_id,
                    exc,
                )
        if archives:
            self._store.record_payloads(archives)

    def delete_run_trajectories(self) -> None:
        """Deletes index rows written by an earlier attempt of this run before retrying."""
        self._store.delete_run_trajectories(self._run_id)
