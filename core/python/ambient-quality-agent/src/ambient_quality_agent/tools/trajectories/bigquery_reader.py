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

"""The deployed `TrajectoryReader`: BigQuery, agent-scoped.

**"Clustered" is a join, never a stored flag.** Two of the four reads
(`count_daily_outcomes`, `list_trajectories`) derive the outcome by left-joining
the insight occurrences on
``(run_id, trajectory_id)``, so both tables stay append-only and nothing has to
be updated after correlation runs.

**A stored conversation is reconciled on the way out, not on the way in.**
`BigQueryTrajectoryStore.record_payloads` appends, because BigQuery has no cheap
``UPDATE``, so one conversation can have several copies; `PAYLOAD_PREFERENCE` is
where one of them is chosen. That constant is the contract's rule rather than
this implementation's: `standalone.InMemoryTrajectoryStore` applies it when it
writes and keeps one copy, and the two answer `get_archives` alike.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ambient_quality_agent.core.labels import build_request_labels
from ambient_quality_agent.tools.trajectories.models import (
    DailyOutcome,
    IngestStatus,
    PayloadStatus,
    Trajectory,
    TrajectoryPayload,
    TrajectoryPayloadTurn,
    TrajectoryProcessingState,
    TrajectoryView,
)
from ambient_quality_agent.tools.trajectories.payloads import TrajectoryArchive
from google.cloud import bigquery

if TYPE_CHECKING:
    from collections.abc import Sequence

_TRAJECTORIES_TABLE = "trajectories"
_OCCURRENCES_TABLE = "insight_occurrences"
_PAYLOADS_TABLE = "trajectory_payloads"
_TURNS_TABLE = "trajectory_payload_turns"

_OUTCOME_SQL = f"""CASE
    WHEN t.ingest_status = '{IngestStatus.NOT_INGESTED}'
      THEN '{TrajectoryProcessingState.NOT_INGESTED}'
    WHEN o.trajectory_id IS NULL
      THEN '{TrajectoryProcessingState.NO_INSIGHT}'
    ELSE '{TrajectoryProcessingState.IN_AN_INSIGHT}'
  END"""
"""The outcome, as SQL over ``t`` (a trajectory) and ``o`` (the joined
occurrence). Written once and used by both queries so a bar on the chart and a
dot in its drill-down can never disagree about what a trajectory was.

``not_ingested`` is checked first: a trajectory nothing could be made of never
reached an analysis, so it cannot be in an insight, and reporting it as
``no_insight`` would file a broken sample under "evaluated and clean"."""


PAYLOAD_PREFERENCE = "(status = 'ingested') DESC, created_at ASC"
"""How a reader picks between the stored copies of one conversation.

`BigQueryTrajectoryStore.record_payloads` appends and compares nothing, so
``(agent_name, trajectory_id)`` can hold more than one copy, distinguished by
``created_at`` -- which a manifest shares with its own turns, so choosing a copy
also chooses which turns belong to it.

As the ``ORDER BY`` of a ``QUALIFY ROW_NUMBER() OVER (...) = 1`` it says
**prefer a whole copy, then the earliest**:

* a `partial` copy from Monday and a whole one from Friday -> Friday's, so a
  later sweep repairs what an earlier one could not assemble;
* a whole copy from Monday and a `partial` one from Friday -> Monday's, which
  is the likelier direction: a conversation re-sampled later is reassembled
  from telemetry that has had longer to age out, so a plain "latest wins" would
  downgrade more often than it repaired;
* two of a kind -> the earlier, the one nearest the conversation.

`store.compute_payload_rank` is the same rule in Python, for the implementation that
applies it when it writes. Two spellings because SQL cannot call the function
and Python cannot be an ``ORDER BY`` -- but one rule, for the reason
`_OUTCOME_SQL` is written once: a second rule, picking differently, would
disagree with this one about what a conversation was, and neither would be wrong
on its own terms.
"""


class BigQueryTrajectoryReader:
    """Reads one agent's sampled trajectories back out of BigQuery."""

    def __init__(
        self,
        *,
        client: bigquery.Client,
        project_id: str,
        dataset: str,
        agent_name: str,
    ) -> None:
        """Bind the reader to one agent's dataset.

        Args:
            client: Shared BigQuery client (its location fixes the job region).
            project_id: GCP project owning the dataset, for table references.
            dataset: BigQuery dataset holding the trajectories table; the same
                one the insight tables live in, which is what lets the outcome
                be a join rather than a second round trip.
            agent_name: The observed agent this reader is scoped to; every
                statement filters on it.
        """
        self._client = client
        self._project_id = project_id
        self._dataset = dataset
        self._agent_name = agent_name

    def count_daily_outcomes(
        self, *, window_start: str | None = None, window_end: str | None = None
    ) -> list[DailyOutcome]:
        """Per-day counts of what became of the trajectories a sweep sampled.

        The chart behind §1's first question: how much of the window was
        actually looked at, and how much of it turned into a defect. Grouped in
        UTC, on `Trajectory.created_at`, which is the table's partition column
        -- so a windowed read prunes partitions instead of scanning the history.

        Args:
            window_start: ISO instant; keep only trajectories recorded at or
                after it. ``None`` for no lower bound.
            window_end: ISO instant; the matching upper bound.

        Returns one entry per ``(day, outcome)`` that has any trajectory at all,
        oldest day first. A day with no trajectories is absent rather than
        being three zeroes -- the caller draws the buckets it wants.
        """
        where, params = self._build_filters(
            window_start=window_start, window_end=window_end
        )
        sql = f"""
SELECT
  DATE(t.created_at) AS day,
  {_OUTCOME_SQL} AS outcome,
  COUNT(*) AS trajectories
FROM {self._build_trajectories_join()}
WHERE {where}
GROUP BY day, outcome
ORDER BY day, outcome
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
        return [
            DailyOutcome(
                day=row["day"],
                outcome=TrajectoryProcessingState(row["outcome"]),
                trajectories=int(row["trajectories"] or 0),
            )
            for row in self._run(sql, params)
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
        """Queries sampled trajectories with pagination and derived outcomes.

        Args:
            run_id: Optional investigation run ID filter.
            outcome: Optional derived outcome state filter.
            window_start: Optional ISO timestamp lower bound on creation time.
            window_end: Optional ISO timestamp upper bound on creation time.
            limit: Maximum number of records to return.
            offset: Number of matching records to skip.

        Returns:
            Tuple of (list of TrajectoryView records, total matching count).
        """
        where, params = self._build_filters(
            run_id=run_id,
            outcome=outcome,
            window_start=window_start,
            window_end=window_end,
        )
        count_rows = list(
            self._run(
                f"SELECT COUNT(*) AS total FROM {self._build_trajectories_join()} "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
                f"WHERE {where}",
                params,
            )
        )
        total = int(count_rows[0]["total"]) if count_rows else 0
        page_params = [
            *params,
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
            bigquery.ScalarQueryParameter("offset", "INT64", offset),
        ]
        sql = f"""
SELECT
  t.run_id, t.agent_name, t.trajectory_id, t.created_at, t.source,
  t.source_session_id, t.source_trace_ids, t.ingest_status,
  {_OUTCOME_SQL} AS outcome
FROM {self._build_trajectories_join()}
WHERE {where}
ORDER BY t.created_at DESC, t.trajectory_id
LIMIT @limit OFFSET @offset
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
        return [_row_to_view(row) for row in self._run(sql, page_params)], total

    def get_trajectories(
        self, keys: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], Trajectory]:
        """Queries stored trajectory records for (run_id, trajectory_id) pairs.

        Args:
            keys: Sequence of (run_id, trajectory_id) tuples to query.

        Returns:
            Mapping of (run_id, trajectory_id) pairs to Trajectory records.
        """
        if not keys:
            return {}
        wanted = set(keys)
        sql = f"""
SELECT t.run_id, t.agent_name, t.trajectory_id, t.created_at, t.source,
       t.source_session_id, t.source_trace_ids, t.ingest_status
FROM `{self._build_table_ref(_TRAJECTORIES_TABLE)}` AS t
WHERE t.agent_name = @agent_name
  AND t.run_id IN UNNEST(@run_ids)
  AND t.trajectory_id IN UNNEST(@trajectory_ids)
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
        params = [
            bigquery.ScalarQueryParameter(
                "agent_name", "STRING", self._agent_name
            ),
            bigquery.ArrayQueryParameter(
                "run_ids", "STRING", sorted({run_id for run_id, _ in wanted})
            ),
            bigquery.ArrayQueryParameter(
                "trajectory_ids",
                "STRING",
                sorted({trajectory_id for _, trajectory_id in wanted}),
            ),
        ]
        found: dict[tuple[str, str], Trajectory] = {}
        for row in self._run(sql, params):
            record = _row_to_trajectory(row)
            key = (record.run_id, record.trajectory_id)
            if key in wanted:
                found[key] = record
        return found

    def get_archives(
        self, trajectory_ids: Sequence[str]
    ) -> dict[str, TrajectoryArchive]:
        """Queries archived conversation manifests and turns by trajectory ID.

        Args:
            trajectory_ids: Sequence of trajectory IDs to query.

        Returns:
            Mapping of trajectory ID to reassembled TrajectoryArchive instances.
        """
        if not trajectory_ids:
            return {}
        sql = f"""
WITH chosen AS (
  SELECT agent_name, trajectory_id, created_at, status, turn_count, agents,
         agent_revision
  FROM `{self._build_table_ref(_PAYLOADS_TABLE)}`
  WHERE agent_name = @agent_name
    AND trajectory_id IN UNNEST(@trajectory_ids)
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY agent_name, trajectory_id
    ORDER BY {PAYLOAD_PREFERENCE}
  ) = 1
),
picked_turns AS (
  SELECT agent_name, trajectory_id, created_at, turn_index, turn
  FROM `{self._build_table_ref(_TURNS_TABLE)}`
  WHERE agent_name = @agent_name
    AND trajectory_id IN UNNEST(@trajectory_ids)
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY agent_name, trajectory_id, created_at, turn_index
    ORDER BY turn_index
  ) = 1
)
SELECT c.agent_name, c.trajectory_id, c.created_at, c.status, c.turn_count,
       c.agents, c.agent_revision, t.turn_index, t.turn
FROM chosen AS c
LEFT JOIN picked_turns AS t
  USING (agent_name, trajectory_id, created_at)
ORDER BY c.trajectory_id, t.turn_index
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
        params = [
            bigquery.ScalarQueryParameter(
                "agent_name", "STRING", self._agent_name
            ),
            bigquery.ArrayQueryParameter(
                "trajectory_ids", "STRING", sorted(set(trajectory_ids))
            ),
        ]
        manifests: dict[str, TrajectoryPayload] = {}
        turns: dict[str, list[TrajectoryPayloadTurn]] = {}
        for row in self._run(sql, params):
            trajectory_id = row["trajectory_id"]
            if trajectory_id not in manifests:
                manifests[trajectory_id] = _row_to_payload(row)
                turns[trajectory_id] = []
            # A LEFT JOIN, so a manifest whose turns are gone still arrives --
            # with one row whose turn columns are null.
            if row["turn_index"] is not None:
                turns[trajectory_id].append(_row_to_turn(row))
        return {
            trajectory_id: TrajectoryArchive(
                payload=payload, turns=tuple(turns[trajectory_id])
            )
            for trajectory_id, payload in manifests.items()
        }

    def _build_trajectories_join(self) -> str:
        """Constructs SQL FROM clause joining trajectories with insight occurrences.

        Returns:
            SQL join fragment string.
        """
        return f"""`{self._build_table_ref(_TRAJECTORIES_TABLE)}` AS t
LEFT JOIN (
  SELECT DISTINCT run_id, trajectory_id
  FROM `{self._build_table_ref(_OCCURRENCES_TABLE)}`, UNNEST(trajectory_ids) AS trajectory_id
  WHERE agent_name = @agent_name AND insight_id IS NOT NULL
) AS o USING (run_id, trajectory_id)"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized

    def _build_filters(
        self,
        *,
        run_id: str | None = None,
        outcome: TrajectoryProcessingState | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Constructs WHERE clause SQL conditions and query parameters.

        Args:
            run_id: Optional run ID filter.
            outcome: Optional outcome filter.
            window_start: Optional lower bound timestamp.
            window_end: Optional upper bound timestamp.

        Returns:
            Tuple of (AND-joined SQL condition string, list of query parameters).
        """
        clauses = ["t.agent_name = @agent_name"]
        params: list[Any] = [
            bigquery.ScalarQueryParameter(
                "agent_name", "STRING", self._agent_name
            )
        ]
        if run_id:
            clauses.append("t.run_id = @run_id")
            params.append(
                bigquery.ScalarQueryParameter("run_id", "STRING", run_id)
            )
        if outcome:
            clauses.append(f"{_OUTCOME_SQL} = @outcome")
            params.append(
                bigquery.ScalarQueryParameter(
                    "outcome", "STRING", outcome.value
                )
            )
        if window_start:
            clauses.append("t.created_at >= @window_start")
            params.append(
                bigquery.ScalarQueryParameter(
                    "window_start", "TIMESTAMP", window_start
                )
            )
        if window_end:
            clauses.append("t.created_at <= @window_end")
            params.append(
                bigquery.ScalarQueryParameter(
                    "window_end", "TIMESTAMP", window_end
                )
            )
        return " AND ".join(clauses), params

    def _build_table_ref(self, table: str) -> str:
        """Constructs a fully qualified `project.dataset.table` reference.

        Args:
            table: Table name.

        Returns:
            Fully-qualified table reference string.
        """
        return f"{self._project_id}.{self._dataset}.{table}"

    def _run(self, sql: str, params: list[Any]) -> bigquery.table.RowIterator:
        """Executes a parameterized BigQuery SQL query.

        Args:
            sql: Query SQL statement.
            params: Sequence of query parameters.

        Returns:
            Query job RowIterator.
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=params, labels=build_request_labels()
        )
        return self._client.query(sql, job_config=job_config).result()


def _row_to_trajectory(row: Any) -> Trajectory:
    """Converts a BigQuery row into a Trajectory model.

    Args:
        row: Row mapping from BigQuery.

    Returns:
        Validated Trajectory instance.
    """
    return Trajectory(
        run_id=row["run_id"],
        agent_name=row["agent_name"],
        trajectory_id=row["trajectory_id"],
        created_at=row["created_at"],
        source=row["source"],
        source_session_id=row["source_session_id"],
        # BigQuery returns a REPEATED column as an empty array, never null; the
        # guard is for a row a caller built by hand.
        source_trace_ids=list(row["source_trace_ids"] or []),
        ingest_status=IngestStatus(row["ingest_status"]),
    )


def _row_to_view(row: Any) -> TrajectoryView:
    """Converts a joined BigQuery row into a TrajectoryView model.

    Args:
        row: Joined BigQuery row containing trajectory and outcome columns.

    Returns:
        TrajectoryView instance.
    """
    return TrajectoryView(
        **_row_to_trajectory(row).model_dump(),
        outcome=TrajectoryProcessingState(row["outcome"]),
    )


def _row_to_payload(row: Any) -> TrajectoryPayload:
    """Converts BigQuery manifest columns into a TrajectoryPayload model.

    Args:
        row: Row mapping containing payload columns.

    Returns:
        TrajectoryPayload instance.
    """
    return TrajectoryPayload(
        agent_name=row["agent_name"],
        trajectory_id=row["trajectory_id"],
        created_at=row["created_at"],
        status=PayloadStatus(row["status"]),
        turn_count=row["turn_count"],
        agents=row["agents"],
        # Absent on an unversioned source that reports no named revision.
        agent_revision=row.get("agent_revision"),
    )


def _row_to_turn(row: Any) -> TrajectoryPayloadTurn:
    """Converts BigQuery turn columns into a TrajectoryPayloadTurn model.

    Args:
        row: Row mapping containing turn columns.

    Returns:
        TrajectoryPayloadTurn instance.
    """
    return TrajectoryPayloadTurn(
        agent_name=row["agent_name"],
        trajectory_id=row["trajectory_id"],
        turn_index=row["turn_index"],
        created_at=row["created_at"],
        turn=row["turn"],
    )
