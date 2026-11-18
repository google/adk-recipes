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

"""BigQuery implementation of `TrajectoryStore`, scoped to one observed agent.

Writes across three tables in `Config.aqa_dataset`:
* ``trajectories`` (`_INVESTIGATION_TRAJECTORIES_TABLE`) for
  `InvestigationTrajectory` rows, keyed by ``(investigation_id, trajectory_id)``.
  Retry cleanup (`delete_investigation_trajectories`) deletes a prior attempt's
  rows for the investigation.
* ``trajectory_payloads`` (`MANIFESTS_TABLE`) and ``trajectory_payload_turns``
  (`STEPS_TABLE`) for `TrajectoryBundle` manifests and steps, keyed by
  ``(observed_agent_key, trajectory_id)`` across investigations.

When a trajectory is sampled by multiple investigations, each appends its own
bundle copy; `bigquery_reader.PAYLOAD_PREFERENCE` selects the preferred copy at
read time. `standalone.InMemoryTrajectoryStore` applies the same rule at write
time (`store.compute_manifest_rank`).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.core.labels import build_request_labels
from google.cloud import bigquery

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ambient_quality_agent.tools.trajectories.bundles import (
        TrajectoryBundle,
    )
    from ambient_quality_agent.tools.trajectories.models import (
        InvestigationTrajectory,
        TrajectoryManifest,
        TrajectoryStep,
    )

logger = logging.getLogger(__name__)

_INVESTIGATION_TRAJECTORIES_TABLE = "trajectories"
MANIFESTS_TABLE = "trajectory_payloads"
STEPS_TABLE = "trajectory_payload_turns"

_WRITE_ATTEMPTS = 3
"""Times a trajectory bundle load is tried before the sweep is failed.

An investigation trajectory is best-effort, because losing one costs a dot on a
chart. Losing a trajectory bundle costs the evidence an insight points at, so
this one retries and then raises. Safe to retry because a load job commits every row or none: a load that
failed wrote nothing to duplicate.
"""

_RETRY_BACKOFF_SECONDS = 2.0


class BigQueryTrajectoryStore:
    """Writes one agent's investigation trajectories, and the trajectory
    bundles behind them, to BigQuery.

    **`delete_investigation_trajectories` must never touch the trajectory
    bundles.** An investigation trajectory belongs to the investigation that wrote it, so
    a retry erases the previous attempt's; a bundle is referenced by every
    sweep that sampled the trajectory, and deleting one investigation's would destroy a
    copy still in use -- including one this investigation did not write. Nothing needs cleaning up there: an
    attempt that failed leaves rows a reader never reaches, and they expire
    with their partition.
    """

    def __init__(
        self,
        *,
        client: bigquery.Client,
        project_id: str,
        dataset: str,
        observed_agent_key: str,
    ) -> None:
        """Bind the store to one agent's dataset.

        Args:
            client: Shared BigQuery client, created once by the caller and
                reused across fetchers/stores (its location fixes the job
                region).
            project_id: GCP project owning the dataset, for table references.
            dataset: BigQuery dataset holding the trajectory tables; the
                same one the insight tables live in.
            observed_agent_key: The observed agent this store is scoped to; every
                statement filters on it.
        """
        self._client = client
        self._project_id = project_id
        self._dataset = dataset
        self._observed_agent_key = observed_agent_key

    def _build_table_ref(self, table: str) -> str:
        """Fully-qualified ``project.dataset.table`` reference."""
        return f"{self._project_id}.{self._dataset}.{table}"

    def delete_investigation_trajectories(self, investigation_id: str) -> None:
        """Erases the investigation trajectories one investigation wrote.

        Never touches trajectory bundles, which are shared across
        investigations and referenced by existing insights.

        Args:
            investigation_id: Investigation identifier to clean up.
        """
        self._run(
            f"DELETE FROM `{self._build_table_ref(_INVESTIGATION_TRAJECTORIES_TABLE)}` "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            "WHERE run_id = @investigation_id AND agent_name = @agent_name",
            [
                bigquery.ScalarQueryParameter(
                    "investigation_id", "STRING", investigation_id
                ),
                bigquery.ScalarQueryParameter(
                    "agent_name", "STRING", self._observed_agent_key
                ),
            ],
        )

    def record_investigation_trajectories(
        self, investigation_trajectories: Sequence[InvestigationTrajectory]
    ) -> None:
        """Appends a batch of investigation trajectories to BigQuery.

        Args:
            investigation_trajectories: Investigation trajectories to write.
        """
        if not investigation_trajectories:
            return
        rows = [
            {
                "run_id": t.investigation_id,
                "agent_name": t.observed_agent_key,
                "trajectory_id": t.trajectory_id,
                "created_at": t.created_at.isoformat(),
                "source": t.ingestion_source,
                "source_session_id": t.source_session_id,
                "source_trace_ids": list(t.source_trace_ids),
                "ingest_status": t.ingest_status.value,
            }
            for t in investigation_trajectories
        ]
        self._insert_rows(_INVESTIGATION_TRAJECTORIES_TABLE, rows)

    def record_trajectory_bundles(
        self, bundles: Sequence[TrajectoryBundle]
    ) -> None:
        """Appends trajectory manifests and steps using load jobs.

        Writes steps before manifests so partially written batches leave steps
        unreferenced rather than manifests missing steps. Retries transient
        errors.

        Args:
            bundles: Trajectory bundles to persist.

        Raises:
            Exception: If load jobs fail after all retry attempts.
        """
        if not bundles:
            return
        self._insert_rows_with_retry(
            STEPS_TABLE,
            [_step_to_row(step) for bundle in bundles for step in bundle.steps],
        )
        self._insert_rows_with_retry(
            MANIFESTS_TABLE,
            [_manifest_to_row(bundle.manifest) for bundle in bundles],
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


def _manifest_to_row(manifest: TrajectoryManifest) -> dict[str, Any]:
    """Converts a TrajectoryManifest model into a BigQuery row dictionary.

    Args:
        manifest: Manifest model to convert.

    Returns:
        Row dictionary formatted for BigQuery JSON loading.
    """
    return {
        "agent_name": manifest.observed_agent_key,
        "trajectory_id": manifest.trajectory_id,
        "created_at": manifest.created_at.isoformat(),
        "status": manifest.status.value,
        "turn_count": manifest.step_count,
        "agents": manifest.agents,
        "agent_revision": manifest.agent_revision,
    }


def _step_to_row(step: TrajectoryStep) -> dict[str, Any]:
    """Converts a TrajectoryStep model into a BigQuery row dictionary.

    Args:
        step: Step model to convert.

    Returns:
        Row dictionary formatted for BigQuery JSON loading.
    """
    return {
        "agent_name": step.observed_agent_key,
        "trajectory_id": step.trajectory_id,
        "turn_index": step.step_index,
        "created_at": step.created_at.isoformat(),
        "turn": step.step,
    }
