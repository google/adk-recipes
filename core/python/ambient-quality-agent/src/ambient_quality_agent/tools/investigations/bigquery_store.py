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

"""BigQuery persistence for investigation runs.

The deployed implementation of `store.InvestigationStore`. That module is where
the behaviour a store owes its callers is written down; this is how BigQuery
answers for it.

Two append-only tables in the dataset the agent owns for its own tables
(`Config.aqa_dataset` -- the same one the insight tables live in):
`investigations`, holding a snapshot of a run per lifecycle step, and
`investigation_events`, holding the progress events its workflow nodes emitted.

Five operations, and no lifecycle knowledge behind any of them: append a
snapshot, append an event, read one run, list the recent ones, and total every
run's counters. What a run's lifecycle *is* -- who may create one, what a status
transition implies -- belongs to `core.investigation.model`, which drives this.

Nothing here updates or deletes a row. A run changes by gaining a newer
snapshot, and a crashed run that retries simply appends more; every read takes
the newest. Rows are written with load jobs rather than the streaming API,
so a row is immediately queryable by the invocation that follows.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.core.session_state import ADKStateLike
from ambient_quality_agent.tools.investigations.models import (
    EVENTS_FIELD,
    IN_FLIGHT_STATUSES,
    NON_AMBIENT_TRIGGER_TYPES,
    InvestigationEvent,
    InvestigationRecord,
    RunStatus,
    list_counter_names,
    list_event_columns,
    list_snapshot_columns,
)
from ambient_quality_agent.tools.investigations.store import (
    DEFAULT_LIST_LIMIT,
    IN_FLIGHT_HORIZON_HOURS,
)
from google.cloud import bigquery

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ambient_quality_agent.config import Config

logger = logging.getLogger(__name__)

INVESTIGATIONS_TABLE = "investigations"
EVENTS_TABLE = "investigation_events"

_SCHEMA_CHECK_TIMEOUT_SECONDS = 10
"""Cap on the boot-time schema read: a wedged metadata call must not hold up
the first request over a check that only logs."""

# The config snapshot is only ever read back for a single run, by the durable
# job that runs the graph from it, so the list leaves it in the table.
_LIST_SKIPS = frozenset({"effective_config"})

# `_record_to_row` hands BigQuery structured columns as JSON text, so a column
# may hold a JSON string rather than an object. Unwrap one level before reading:
# `JSON_VALUE` with no path extracts string contents and yields NULL for an object,
# allowing COALESCE to handle both string-encoded and native JSON values.
#
# Follows the same decoding logic as `models._to_json_obj` ("decode until it
# stops being a string").
_UNWRAPPED_COUNTERS = (
    "COALESCE(SAFE.PARSE_JSON(JSON_VALUE(counters)), counters)"
)


def _build_counter_sql(name: str) -> str:
    """Generates SQL expression to extract a counter as INT64 from JSON.

    Args:
        name: Counter field name.

    Returns:
        SQL expression string with COALESCE to default missing keys to 0.
    """
    return f"COALESCE(SAFE_CAST(JSON_VALUE(counters, '$.{name}') AS INT64), 0)"


def _build_window_conditions(
    window_start: str | None, window_end: str | None
) -> tuple[list[str], list[Any]]:
    """Builds SQL WHERE conditions and query parameters for telemetry window filtering.

    Filters on `window_end` so runs correspond to inspected telemetry data.

    Args:
        window_start: Optional ISO timestamp lower bound.
        window_end: Optional ISO timestamp upper bound.

    Returns:
        Tuple of (SQL condition strings list, query parameter objects list).
    """
    conditions: list[str] = []
    params: list[Any] = []
    if window_start:
        conditions.append("window_end >= @window_start")
        params.append(
            bigquery.ScalarQueryParameter(
                "window_start", "TIMESTAMP", window_start
            )
        )
    if window_end:
        conditions.append("window_end <= @window_end")
        params.append(
            bigquery.ScalarQueryParameter("window_end", "TIMESTAMP", window_end)
        )
    return conditions, params


class BigQueryInvestigationStore:
    """Appends and reads one deployment's investigations and their events."""

    def __init__(
        self,
        *,
        client: bigquery.Client,
        project_id: str,
        dataset: str,
    ) -> None:
        """Bind the store to the investigation tables.

        Args:
            client: BigQuery client; its location fixes the job region.
            project_id: GCP project owning the dataset, for table references.
            dataset: BigQuery dataset holding the tables (`Config.aqa_dataset`).
        """
        self._client = client
        self._project_id = project_id
        self._dataset = dataset

    @classmethod
    def from_config(cls, config: Config) -> BigQueryInvestigationStore:
        """Initializes a store instance from application configuration.

        Args:
            config: Application configuration.

        Returns:
            Configured BigQueryInvestigationStore instance.
        """
        return cls(
            client=bigquery.Client(
                project=config.project_id, location=config.aqa_dataset_location
            ),
            project_id=config.project_id,
            dataset=config.aqa_dataset,
        )

    @classmethod
    def from_workflow_state(
        cls, state: ADKStateLike
    ) -> BigQueryInvestigationStore:
        """Initializes a store instance from workflow execution state.

        Args:
            state: Workflow state mapping.

        Returns:
            Configured BigQueryInvestigationStore instance.
        """
        return cls(
            client=bigquery.Client(
                project=state["project_id"],
                location=state.get("aqa_dataset_location", "us-central1"),
            ),
            project_id=state["project_id"],
            dataset=state.get("aqa_dataset", "aqua_insights"),
        )

    # --- Writes ------------------------------------------------------------ #

    def append(self, record: InvestigationRecord) -> InvestigationRecord:
        """Appends record as the latest snapshot for the investigation run.

        Restamps updated_at. Excludes events, which are written to the events table.

        Args:
            record: Investigation record state to store.

        Returns:
            The stored record snapshot.
        """
        stored = record.model_copy(update={"updated_at": _format_utc_now_iso()})
        self._insert_rows(
            INVESTIGATIONS_TABLE,
            [_record_to_row(stored, exclude={EVENTS_FIELD})],
        )
        return stored

    def append_event(self, run_id: str, text: str, *, source: str = "") -> None:
        """Appends a progress event to the events table.

        Args:
            run_id: Investigation run identifier.
            text: Markdown content emitted by the node.
            source: Identifier of the emitting node or component.
        """
        event = InvestigationEvent(
            run_id=run_id, text=text, source=source or None
        )
        self._insert_rows(EVENTS_TABLE, [_record_to_row(event)])

    # --- Reads --------------------------------------------------------------#

    def get(
        self, run_id: str, *, include_events: bool = True
    ) -> InvestigationRecord | None:
        """Loads the latest snapshot of an investigation run by ID.

        Args:
            run_id: Investigation run identifier.
            include_events: Whether to load associated progress events.

        Returns:
            The investigation record snapshot, or None if not found.
        """
        params = [bigquery.ScalarQueryParameter("run_id", "STRING", run_id)]
        rows = self._run(
            f"SELECT {_build_select_list(list_snapshot_columns())} "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            f"FROM `{self._build_table_ref(INVESTIGATIONS_TABLE)}` "
            "WHERE run_id = @run_id ORDER BY updated_at DESC LIMIT 1",
            params,
        )
        if not rows:
            return None
        record = InvestigationRecord.model_validate(_drop_null_columns(rows[0]))
        if include_events:
            record = record.model_copy(
                update={"events": self._list_events(run_id)}
            )
        return record

    def list_recent(
        self,
        *,
        status: RunStatus | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> list[InvestigationRecord]:
        """Queries the most recent investigation runs within an optional window.

        Args:
            status: Optional run status filter.
            limit: Maximum number of runs to return.
            window_start: Optional ISO timestamp lower bound for telemetry window end.
            window_end: Optional ISO timestamp upper bound for telemetry window end.

        Returns:
            List of recent investigation records ordered chronologically.
        """
        columns = [c for c in list_snapshot_columns() if c not in _LIST_SKIPS]
        params: list[Any] = [
            bigquery.ScalarQueryParameter("limit", "INT64", limit)
        ]
        conditions: list[str] = []
        if status is not None:
            conditions.append("status = @status")
            params.append(
                bigquery.ScalarQueryParameter("status", "STRING", status.value)
            )
        window_conditions, window_params = _build_window_conditions(
            window_start, window_end
        )
        conditions += window_conditions
        params += window_params
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._run(
            f"""
SELECT * FROM (
  SELECT {_build_select_list(columns)}
  FROM `{self._build_table_ref(INVESTIGATIONS_TABLE)}`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY updated_at DESC) = 1)
{where}
ORDER BY created_at DESC
LIMIT @limit
""",  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            params,
        )
        return [
            InvestigationRecord.model_validate(_drop_null_columns(row))
            for row in reversed(rows)
        ]

    def sum_counters(
        self, *, window_start: str | None = None, window_end: str | None = None
    ) -> tuple[dict[str, int], dict[str, str | None]]:
        """Aggregates all counters across investigation runs in BigQuery.

        Args:
            window_start: Optional ISO timestamp lower bound for telemetry window end.
            window_end: Optional ISO timestamp upper bound for telemetry window end.

        Returns:
            Tuple of (counter name to total mapping, covered window bounds dict).
        """
        sums = ",\n  ".join(
            f"SUM({_build_counter_sql(name)}) AS {name}"
            for name in list_counter_names()
        )
        conditions, params = _build_window_conditions(window_start, window_end)
        where = f"\nWHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._run(
            f"""
SELECT
  COUNT(*) AS investigations,
  MIN(window_end) AS covered_start,
  MAX(window_end) AS covered_end,
  {sums}
FROM (
  SELECT run_id, window_end, {_UNWRAPPED_COUNTERS} AS counters
  FROM `{self._build_table_ref(INVESTIGATIONS_TABLE)}`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY updated_at DESC) = 1){where}
""",  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            params,
        )
        if not rows:
            zeros = {
                "investigations": 0,
                **dict.fromkeys(list_counter_names(), 0),
            }
            return zeros, {"start": None, "end": None}
        row = dict(rows[0])
        covered = {
            "start": _to_iso(row.pop("covered_start", None)),
            "end": _to_iso(row.pop("covered_end", None)),
        }
        return {name: int(value or 0) for name, value in row.items()}, covered

    def list_stale_pending(self, *, lease_minutes: int) -> list[str]:
        """Queries IDs of runs remaining in pending status beyond the lease duration.

        Args:
            lease_minutes: Number of minutes after creation before a run is considered stale.

        Returns:
            List of stale run IDs.
        """
        deadline = (
            dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=lease_minutes)
        ).isoformat()
        params = [
            bigquery.ScalarQueryParameter(
                "status", "STRING", RunStatus.PENDING.value
            ),
            bigquery.ScalarQueryParameter("deadline", "TIMESTAMP", deadline),
        ]
        rows = self._run(
            f"""
SELECT run_id FROM (
  SELECT run_id, updated_at, status
  FROM `{self._build_table_ref(INVESTIGATIONS_TABLE)}`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY updated_at DESC) = 1)
WHERE status = @status AND updated_at < @deadline
""",  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            params,
        )
        return [row["run_id"] for row in rows]

    def list_overdue_scheduled(self, *, grace_minutes: int) -> list[str]:
        """Queries IDs of scheduled runs whose trigger is overdue beyond the grace.

        Args:
            grace_minutes: Minutes past `due_at` a scheduled run may wait for its
                trigger before it is considered lost.

        Returns:
            List of overdue scheduled run IDs.
        """
        deadline = dt.datetime.now(tz=dt.UTC) - dt.timedelta(
            minutes=grace_minutes
        )
        params = [
            bigquery.ScalarQueryParameter(
                "status", "STRING", RunStatus.SCHEDULED.value
            ),
            bigquery.ScalarQueryParameter("deadline", "TIMESTAMP", deadline),
        ]
        rows = self._run(
            f"""
SELECT run_id FROM (
  SELECT run_id, updated_at, status, due_at
  FROM `{self._build_table_ref(INVESTIGATIONS_TABLE)}`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY updated_at DESC) = 1)
WHERE status = @status AND due_at < @deadline
""",  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            params,
        )
        return [row["run_id"] for row in rows]

    def get_last_finished_window_end(
        self, observed_agent_name: str
    ) -> str | None:
        """Returns the latest telemetry window end timestamp for finished ambient runs.

        Excludes dry runs and non-ambient triggers (`NON_AMBIENT_TRIGGER_TYPES`) because
        ad-hoc or custom sweeps use custom windows that would misalign the ambient
        telemetry watermark.

        Args:
            observed_agent_name: Name of the observed agent.

        Returns:
            ISO 8601 timestamp of the latest completed window end, or None if no
            completed ambient run exists.

        TODO: a run stopped by its evaluation budget still ends ``done``, so the
        point advances past unevaluated telemetry.
        """
        params = [
            bigquery.ScalarQueryParameter(
                "status", "STRING", RunStatus.DONE.value
            ),
            bigquery.ScalarQueryParameter(
                "agent", "STRING", observed_agent_name
            ),
            bigquery.ArrayQueryParameter(
                "non_ambient", "STRING", sorted(NON_AMBIENT_TRIGGER_TYPES)
            ),
        ]
        rows = self._run(
            f"""
SELECT MAX(window_end) AS mark FROM (
  SELECT run_id, updated_at, window_end, status, dry_run, trigger_type, observed_agent_name
  FROM `{self._build_table_ref(INVESTIGATIONS_TABLE)}`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY updated_at DESC) = 1)
WHERE status = @status
  AND observed_agent_name = @agent
  AND trigger_type NOT IN UNNEST(@non_ambient)
  AND NOT dry_run
""",  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            params,
        )
        mark = rows[0]["mark"] if rows else None
        return mark.isoformat() if isinstance(mark, dt.datetime) else mark

    def count_in_flight(self, observed_agent_name: str) -> int:
        """Counts active in-flight investigation runs for an agent.

        Because durable runs execute in external processes, active counts are
        queried from the store rather than tracked in memory. Runs untouched for
        longer than `IN_FLIGHT_HORIZON_HOURS` are excluded so aborted or crashed
        processes do not permanently consume concurrency capacity.

        Args:
            observed_agent_name: Name of the observed agent.

        Returns:
            Number of pending or running runs updated within the horizon.
        """
        since = (
            dt.datetime.now(tz=dt.UTC)
            - dt.timedelta(hours=IN_FLIGHT_HORIZON_HOURS)
        ).isoformat()
        params = [
            bigquery.ScalarQueryParameter(
                "agent", "STRING", observed_agent_name
            ),
            bigquery.ArrayQueryParameter(
                "in_flight",
                "STRING",
                sorted(s.value for s in IN_FLIGHT_STATUSES),
            ),
            bigquery.ScalarQueryParameter("since", "TIMESTAMP", since),
        ]
        rows = self._run(
            f"""
SELECT COUNT(*) AS in_flight FROM (
  SELECT run_id, updated_at, status, observed_agent_name
  FROM `{self._build_table_ref(INVESTIGATIONS_TABLE)}`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY updated_at DESC) = 1)
WHERE observed_agent_name = @agent
  AND status IN UNNEST(@in_flight)
  AND updated_at >= @since
""",  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            params,
        )
        return int(rows[0]["in_flight"] or 0) if rows else 0

    def sum_counters_by_day(
        self, *, window_start: str | None = None, window_end: str | None = None
    ) -> list[dict[str, int | str]]:
        """Aggregates counter totals grouped by calendar day of telemetry window end.

        Aggregates in SQL over the specified time range so the results reflect the full
        window rather than a paginated subset. Days with no runs are omitted.
        Includes an `unmeasured` count for runs where all counters are zero.

        Args:
            window_start: Optional ISO timestamp lower bound for telemetry window end.
            window_end: Optional ISO timestamp upper bound for telemetry window end.

        Returns:
            List of daily summary dictionaries ordered chronologically.
        """
        names = list_counter_names()
        sums = ",\n  ".join(
            f"SUM({_build_counter_sql(n)}) AS {n}" for n in names
        )
        # "Measured nothing" means every counter is zero, rather than a NULL column.
        measured = " + ".join(_build_counter_sql(n) for n in names)
        conditions, params = _build_window_conditions(window_start, window_end)
        # A run with no window cannot be filed under a day at all, so it is out
        # of this read entirely rather than bucketed under a guess.
        conditions.insert(0, "window_end IS NOT NULL")
        rows = self._run(
            f"""
SELECT
  FORMAT_DATE('%Y-%m-%d', DATE(window_end)) AS day,
  COUNT(*) AS investigations,
  SUM(CASE WHEN ({measured}) = 0 THEN 1 ELSE 0 END) AS unmeasured,
  {sums}
FROM (
  SELECT run_id, window_end, {_UNWRAPPED_COUNTERS} AS counters
  FROM `{self._build_table_ref(INVESTIGATIONS_TABLE)}`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY updated_at DESC) = 1)
WHERE {" AND ".join(conditions)}
GROUP BY day
ORDER BY day
""",  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            params,
        )
        return [
            {
                "day": str(row["day"]),
                **{
                    name: int(row[name] or 0)
                    for name in ("investigations", "unmeasured", *names)
                },
            }
            for row in rows
        ]

    def _list_events(self, run_id: str) -> list[InvestigationEvent]:
        """Queries all progress events for a run, ordered by creation time.

        Args:
            run_id: Investigation run identifier.

        Returns:
            List of InvestigationEvent objects sorted oldest first.
        """
        rows = self._run(
            f"SELECT {_build_select_list(list_event_columns())} "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            f"FROM `{self._build_table_ref(EVENTS_TABLE)}` "
            "WHERE run_id = @run_id ORDER BY created_at",
            [bigquery.ScalarQueryParameter("run_id", "STRING", run_id)],
        )
        return [
            InvestigationEvent.model_validate(_drop_null_columns(row))
            for row in rows
        ]

    def list_missing_columns(self) -> list[str] | None:
        """Identifies expected model columns missing from the live BigQuery schema.

        Returns:
            List of missing column names, or None if the table schema could not be retrieved.
        """
        try:
            table = self._client.get_table(
                self._build_table_ref(INVESTIGATIONS_TABLE),
                timeout=_SCHEMA_CHECK_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning(
                "could not read the investigations table's schema; skipping the check",
                exc_info=True,
            )
            return None
        have = {field.name for field in table.schema}
        return [name for name in list_snapshot_columns() if name not in have]

    # --- Plumbing ---------------------------------------------------------- #

    def _build_table_ref(self, table: str) -> str:
        """Constructs a fully qualified `project.dataset.table` reference.

        Args:
            table: Table name.

        Returns:
            Fully-qualified table reference string.
        """
        return f"{self._project_id}.{self._dataset}.{table}"

    def _run(self, sql: str, params: list[Any]) -> Sequence[Any]:
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        return list(self._client.query(sql, job_config=job_config).result())

    def _insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        """Loads row dictionaries into a table via a WRITE_APPEND load job.

        Args:
            table: Target table name.
            rows: Row dictionaries to append.
        """
        self._client.load_table_from_json(
            rows,
            self._build_table_ref(table),
            job_config=bigquery.LoadJobConfig(
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
                autodetect=False,
            ),
        ).result()


def _build_select_list(columns: Sequence[str]) -> str:
    """Builds a comma-separated column list for SQL SELECT statements.

    Args:
        columns: Sequence of column names.

    Returns:
        Formatted comma-separated column string.
    """
    return ", ".join(columns)


def _record_to_row(
    record: InvestigationRecord | InvestigationEvent,
    *,
    exclude: set[str] | None = None,
) -> dict[str, Any]:
    """Converts a model instance into a dictionary for BigQuery JSON loading.

    Args:
        record: InvestigationRecord or InvestigationEvent instance.
        exclude: Optional set of field names to omit.

    Returns:
        Dictionary formatted for BigQuery JSON insertion.
    """
    if isinstance(record, InvestigationRecord) and record.summary is not None:
        # The events are rows of their own; carrying them here too would repeat
        # a run's largest payload in every snapshot after the one that ran it.
        record = record.model_copy(
            update={
                "summary": {
                    k: v for k, v in record.summary.items() if k != "events"
                }
            }
        )
    row = record.model_dump(mode="json", exclude=exclude, exclude_none=True)
    return {
        column: json.dumps(value, ensure_ascii=False)
        if isinstance(value, dict | list)
        else value
        for column, value in row.items()
    }


def _drop_null_columns(row: Any) -> dict[str, Any]:
    """Strips NULL values from a BigQuery row before model validation.

    Args:
        row: Row mapping from BigQuery.

    Returns:
        Dictionary containing non-null column values.
    """
    return {
        column: value
        for column, value in dict(row).items()
        if value is not None
    }


def _to_iso(value: Any) -> str | None:
    """Converts a timestamp or datetime to an ISO 8601 string.

    Args:
        value: Datetime instance, string, or None.

    Returns:
        ISO 8601 string or None.
    """
    if value is None:
        return None
    return value.isoformat() if isinstance(value, dt.datetime) else str(value)


def _format_utc_now_iso() -> str:
    return dt.datetime.now(tz=dt.UTC).isoformat()
