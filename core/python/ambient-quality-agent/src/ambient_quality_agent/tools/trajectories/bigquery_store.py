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

"""The deployed `TrajectoryStore`: BigQuery, agent-scoped.

Three tables, one store, as `BigQueryInsightStore` covers `insights` with its
occurrences and `BigQueryInvestigationStore` covers `investigations` with its
events:
the index of what was sampled (`trajectories`) and the archived conversation
behind it (`trajectory_payloads` with its turns).

**Every write is a plain append**, and the difference between the two is what
becomes of an older row. An index row belongs to the run that wrote it, so a
retry of that run erases the previous attempt's. A payload belongs to the
conversation, and a sweep that samples one a second time simply stores a second
copy: nothing is read back, compared or replaced at write time, so no write can
destroy a conversation somebody else stored.

Which copy to believe is `bigquery_reader.PAYLOAD_PREFERENCE`, decided where the
payload is read. Keeping it there is the `_build_gcs_console_url` precedent
(`core/investigation/model.py`) applied to a rule instead of a URL: the stored
rows carry the facts, the reader derives the answer, and the answer can change
without rewriting anything.

Appending and choosing later is BigQuery's answer, not the contract's.
`standalone.InMemoryTrajectoryStore` can change a record, so it applies the same
preference at write time and keeps one copy (D19). Both hand back the copy the
rule names.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.core.labels import build_request_labels
from google.cloud import bigquery

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ambient_quality_agent.tools.trajectories.models import (
        Trajectory,
        TrajectoryPayload,
        TrajectoryPayloadTurn,
    )
    from ambient_quality_agent.tools.trajectories.payloads import (
        TrajectoryArchive,
    )

logger = logging.getLogger(__name__)

_TRAJECTORIES_TABLE = "trajectories"
PAYLOADS_TABLE = "trajectory_payloads"
TURNS_TABLE = "trajectory_payload_turns"

_WRITE_ATTEMPTS = 3
"""Times a payload load is tried before the sweep is failed.

An index row is best-effort, because losing one costs a dot on a chart. Losing
a payload costs the evidence an insight points at, so this one retries and then
raises. Safe to retry because a load job commits every row or none: a load that
failed wrote nothing to duplicate.
"""

_RETRY_BACKOFF_SECONDS = 2.0


class BigQueryTrajectoryStore:
    """Writes one agent's sampled trajectories, and the conversations behind
    them, to BigQuery.

    **`delete_run_trajectories` covers the index and must never touch the payloads.** An
    index row belongs to the run that wrote it, so a retry erases the previous
    attempt's; a payload is referenced by every sweep that sampled the
    conversation, and deleting one run's would destroy a copy still in use --
    including one this run did not write. Nothing needs cleaning up there: an
    attempt that failed leaves rows a reader never reaches, and they expire
    with their partition.
    """

    def __init__(
        self,
        *,
        client: bigquery.Client,
        project_id: str,
        dataset: str,
        agent_name: str,
    ) -> None:
        """Bind the store to one agent's dataset.

        Args:
            client: Shared BigQuery client, created once by the caller and
                reused across fetchers/stores (its location fixes the job
                region).
            project_id: GCP project owning the dataset, for table references.
            dataset: BigQuery dataset holding the trajectory tables; the
                same one the insight tables live in.
            agent_name: The observed agent this store is scoped to; every
                statement filters on it.
        """
        self._client = client
        self._project_id = project_id
        self._dataset = dataset
        self._agent_name = agent_name

    def _build_table_ref(self, table: str) -> str:
        """Fully-qualified ``project.dataset.table`` reference."""
        return f"{self._project_id}.{self._dataset}.{table}"

    def delete_run_trajectories(self, run_id: str) -> None:
        """Erases index rows for an investigation run to prepare for a clean retry.

        Deletes rows from the trajectories index table. Never touches payloads,
        which are shared across runs and referenced by existing insights.

        Args:
            run_id: Investigation run identifier to clean up.
        """
        self._run(
            f"DELETE FROM `{self._build_table_ref(_TRAJECTORIES_TABLE)}` "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            "WHERE run_id = @run_id AND agent_name = @agent_name",
            [
                bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
                bigquery.ScalarQueryParameter(
                    "agent_name", "STRING", self._agent_name
                ),
            ],
        )

    def record(self, trajectories: Sequence[Trajectory]) -> None:
        """Appends a batch of trajectory index rows to BigQuery.

        Args:
            trajectories: Sequence of Trajectory models to write.
        """
        if not trajectories:
            return
        rows = [
            {
                "run_id": t.run_id,
                "agent_name": t.agent_name,
                "trajectory_id": t.trajectory_id,
                "created_at": t.created_at.isoformat(),
                "source": t.source,
                "source_session_id": t.source_session_id,
                "source_trace_ids": list(t.source_trace_ids),
                "ingest_status": t.ingest_status.value,
            }
            for t in trajectories
        ]
        self._insert_rows(_TRAJECTORIES_TABLE, rows)

    def record_payloads(self, archives: Sequence[TrajectoryArchive]) -> None:
        """Appends conversation manifests and turns using load jobs.

        Writes turns before manifests so partially written batches leave turns
        unreferenced rather than manifests missing turns. Retries transient errors.

        Args:
            archives: Sequence of TrajectoryArchive objects to persist.

        Raises:
            Exception: If load jobs fail after all retry attempts.
        """
        if not archives:
            return
        self._insert_rows_with_retry(
            TURNS_TABLE,
            [
                _turn_to_row(turn)
                for archive in archives
                for turn in archive.turns
            ],
        )
        self._insert_rows_with_retry(
            PAYLOADS_TABLE,
            [_payload_to_row(archive.payload) for archive in archives],
        )

    def _insert_rows_with_retry(
        self, table: str, rows: list[dict[str, Any]]
    ) -> None:
        """Executes a table load job with linearly increasing backoff on failure.

        Args:
            table: Target BigQuery table name.
            rows: Row dictionaries to load.

        Raises:
            Exception: If loading fails after maximum retry attempts.
        """
        for attempt in range(1, _WRITE_ATTEMPTS + 1):
            try:
                self._insert_rows(table, rows)
            except Exception as exc:
                if attempt == _WRITE_ATTEMPTS:
                    raise
                logger.warning(
                    "Could not load %d row(s) into %s, attempt %d of %d: %s",
                    len(rows),
                    table,
                    attempt,
                    _WRITE_ATTEMPTS,
                    exc,
                )
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
            else:
                return

    def _run(
        self, sql: str, params: list[Any] | None = None
    ) -> bigquery.table.RowIterator:
        """Executes a SQL query against BigQuery.

        Args:
            sql: SQL statement to execute.
            params: Optional query parameters.

        Returns:
            Query job RowIterator.
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=params or [], labels=build_request_labels()
        )
        return self._client.query(sql, job_config=job_config).result()

    def _insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        """Loads row dictionaries into a table via a WRITE_APPEND load job.

        Args:
            table: Target table name.
            rows: Row dictionaries to append.
        """
        if not rows:
            return
        errors = self._client.load_table_from_json(
            rows,
            self._build_table_ref(table),
            job_config=bigquery.LoadJobConfig(
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
                autodetect=False,
            ),
        ).result()
        del errors  # result() raises on failure; nothing to inspect on success.


def _payload_to_row(payload: TrajectoryPayload) -> dict[str, Any]:
    """Converts a TrajectoryPayload model into a BigQuery row dictionary.

    Args:
        payload: Manifest model to convert.

    Returns:
        Row dictionary formatted for BigQuery JSON loading.
    """
    return {
        "agent_name": payload.agent_name,
        "trajectory_id": payload.trajectory_id,
        "created_at": payload.created_at.isoformat(),
        "status": payload.status.value,
        "turn_count": payload.turn_count,
        "agents": payload.agents,
        "agent_revision": payload.agent_revision,
    }


def _turn_to_row(turn: TrajectoryPayloadTurn) -> dict[str, Any]:
    """Converts a TrajectoryPayloadTurn model into a BigQuery row dictionary.

    Args:
        turn: Turn model to convert.

    Returns:
        Row dictionary formatted for BigQuery JSON loading.
    """
    return {
        "agent_name": turn.agent_name,
        "trajectory_id": turn.trajectory_id,
        "turn_index": turn.turn_index,
        "created_at": turn.created_at.isoformat(),
        "turn": turn.turn,
    }
