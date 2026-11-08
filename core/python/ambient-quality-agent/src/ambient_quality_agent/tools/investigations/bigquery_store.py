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

"""BigQuery persistence for investigations and their progress events.

Implements `store.InvestigationStore` over two append-only tables in
`Config.aqa_dataset`: ``investigations`` (one snapshot row per lifecycle
transition) and ``investigation_events`` (progress events emitted by workflow
nodes).

A store is bound to one agent's `observed_agent_key` (stored in the
``observed_agent_name`` column). List and counter aggregation queries filter by
that key, while `get`, `list_latest_stage_events`, `list_stale_pending`, and
`list_overdue_scheduled` query across all agents because investigation IDs are
globally unique.

Rows are written with load jobs rather than streaming inserts so they are
immediately visible to subsequent queries and DML.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import TYPE_CHECKING, Any

from ambient_quality_agent import clients
from ambient_quality_agent import config as config_module
from ambient_quality_agent.core.session_state import ADKStateLike
from ambient_quality_agent.tools.investigations.models import (
    EVENTS_FIELD,
    IN_FLIGHT_STATUSES,
    NON_AMBIENT_TRIGGER_TYPES,
    STAGE_EVENT_SOURCE,
    Investigation,
    InvestigationEvent,
    InvestigationStatus,
    counter_to_key,
    field_to_column,
    list_counter_names,
    list_event_fields,
    list_snapshot_columns,
    list_snapshot_fields,
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

# The config snapshot is only ever read back for a single investigation, by the durable
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
        name: Key the counter is stored under in the ``counters`` column.

    Returns:
        SQL expression string with COALESCE to default missing keys to 0.
    """
    return f"COALESCE(SAFE_CAST(JSON_VALUE(counters, '$.{name}') AS INT64), 0)"


def _build_window_conditions(
    window_start: str | None, window_end: str | None
) -> tuple[list[str], list[Any]]:
    """Builds SQL WHERE conditions and query parameters for telemetry window filtering.

    Filters on `window_end` so investigations are grouped by the telemetry
    period they inspected.

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
    """Appends and reads one agent's investigations and their events."""

    def __init__(
        self,
        *,
        client: bigquery.Client,
        project_id: str,
        dataset: str,
        observed_agent_key: str = "",
    ) -> None:
        """Bind the store to the investigation tables and one agent.

        Args:
            client: BigQuery client; its location fixes the job region.
            project_id: GCP project owning the dataset, for table references.
            dataset: BigQuery dataset holding the tables (`Config.aqa_dataset`).
            observed_agent_key: Key of the agent whose investigations the investigation list and counter
                totals cover. Empty for a deployment with no agent, whose
                list and totals are not scoped.
        """
        self._client = client
        self._project_id = project_id
        self._dataset = dataset
        self._observed_agent_key = observed_agent_key

    @classmethod
    def from_config(cls, config: Config) -> BigQueryInvestigationStore:
        """Initializes a store instance from application configuration.

        Args:
            config: Application configuration.

        Returns:
            Configured BigQueryInvestigationStore instance.
        """
        return cls(
            client=clients.create_bigquery_client(
                config.project_id, config.aqa_dataset_location
            ),
            project_id=config.project_id,
            dataset=config.aqa_dataset,
            observed_agent_key=config.resolve_agent_key(),
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
            client=clients.create_bigquery_client(
                state["project_id"],
                state.get("aqa_dataset_location", "us-central1"),
            ),
            project_id=state["project_id"],
            dataset=state.get("aqa_dataset", "aqua_insights"),
            observed_agent_key=config_module.extract_agent_key(state),
        )

    # --- Writes ------------------------------------------------------------ #

    def append(self, record: Investigation) -> Investigation:
        """Appends record as the latest snapshot for the investigation.

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

    def append_event(
        self, investigation_id: str, text: str, *, source: str = ""
    ) -> None:
        """Appends a progress event to the events table.

        Args:
            investigation_id: Investigation identifier.
            text: Markdown content emitted by the node.
            source: Identifier of the emitting node or component.
        """
        event = InvestigationEvent(
            investigation_id=investigation_id, text=text, source=source or None
        )
        self._insert_rows(EVENTS_TABLE, [_record_to_row(event)])

    # --- Reads --------------------------------------------------------------#

    def get(
        self, investigation_id: str, *, include_events: bool = True
    ) -> Investigation | None:
        """Loads the latest snapshot of an investigation by ID.

        Args:
            investigation_id: Investigation identifier.
            include_events: Whether to load associated progress events.

        Returns:
            The investigation record snapshot, or None if not found.
        """
        params = [
            bigquery.ScalarQueryParameter(
                "investigation_id", "STRING", investigation_id
            )
        ]
        rows = self._run(
            f"SELECT {_build_select_list(list_snapshot_fields())} "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            f"FROM `{self._build_table_ref(INVESTIGATIONS_TABLE)}` "
            "WHERE run_id = @investigation_id ORDER BY updated_at DESC LIMIT 1",
            params,
        )
        if not rows:
            return None
        record = Investigation.model_validate(_drop_null_columns(rows[0]))
        if include_events:
            record = record.model_copy(
                update={"events": self._list_events(investigation_id)}
            )
        return record

    def list_recent(
        self,
        *,
        status: InvestigationStatus | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> list[Investigation]:
        """Queries the most recent investigations within an optional window.

        Args:
            status: Optional investigation status filter.
            limit: Maximum number of investigations to return.
            window_start: Optional ISO timestamp lower bound for telemetry window end.
            window_end: Optional ISO timestamp upper bound for telemetry window end.

        Returns:
            List of recent investigation records ordered chronologically.
        """
        fields = [f for f in list_snapshot_fields() if f not in _LIST_SKIPS]
        agent, params = self._build_agent_filter()
        params.append(bigquery.ScalarQueryParameter("limit", "INT64", limit))
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
  SELECT {_build_select_list(fields)}
  FROM `{self._build_table_ref(INVESTIGATIONS_TABLE)}`{agent}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY updated_at DESC) = 1)
{where}
ORDER BY created_at DESC
LIMIT @limit
""",  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            params,
        )
        return [
            Investigation.model_validate(_drop_null_columns(row))
            for row in reversed(rows)
        ]

    def list_latest_stage_events(
        self, investigation_ids: Sequence[str]
    ) -> list[InvestigationEvent]:
        """Queries the newest stage event of each named investigation.

        Args:
            investigation_ids: Investigations to look up; empty costs no
                query.

        Returns:
            One event per named investigation that wrote a stage event.
        """
        if not investigation_ids:
            return []
        rows = self._run(
            f"SELECT {_build_select_list(list_event_fields())} "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            f"FROM `{self._build_table_ref(EVENTS_TABLE)}` "
            "WHERE run_id IN UNNEST(@investigation_ids) AND source = @source "
            "QUALIFY ROW_NUMBER() "
            "OVER (PARTITION BY run_id ORDER BY created_at DESC) = 1",
            [
                bigquery.ArrayQueryParameter(
                    "investigation_ids", "STRING", list(investigation_ids)
                ),
                bigquery.ScalarQueryParameter(
                    "source", "STRING", STAGE_EVENT_SOURCE
                ),
            ],
        )
        return [
            InvestigationEvent.model_validate(_drop_null_columns(row))
            for row in rows
        ]

    def sum_counters(
        self, *, window_start: str | None = None, window_end: str | None = None
    ) -> tuple[dict[str, int], dict[str, str | None]]:
        """Aggregates all counters across investigations in BigQuery.

        Args:
            window_start: Optional ISO timestamp lower bound for telemetry window end.
            window_end: Optional ISO timestamp upper bound for telemetry window end.

        Returns:
            Tuple of (counter name to total mapping, covered window bounds dict).
        """
        sums = ",\n  ".join(
            f"SUM({_build_counter_sql(counter_to_key(name))}) AS {name}"
            for name in list_counter_names()
        )
        conditions, params = _build_window_conditions(window_start, window_end)
        where = f"\nWHERE {' AND '.join(conditions)}" if conditions else ""
        agent, agent_params = self._build_agent_filter()
        params += agent_params
        rows = self._run(
            f"""
SELECT
  COUNT(*) AS investigations,
  MIN(window_end) AS covered_start,
  MAX(window_end) AS covered_end,
  {sums}
FROM (
  SELECT run_id, window_end, {_UNWRAPPED_COUNTERS} AS counters
  FROM `{self._build_table_ref(INVESTIGATIONS_TABLE)}`{agent}
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
        """Queries IDs of investigations remaining in pending status beyond the lease duration.

        Args:
            lease_minutes: Number of minutes after update before an
                investigation is considered stale.

        Returns:
            List of stale pending investigation IDs.
        """
        deadline = (
            dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=lease_minutes)
        ).isoformat()
        params = [
            bigquery.ScalarQueryParameter(
                "status", "STRING", InvestigationStatus.PENDING.value
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
        """Queries IDs of scheduled investigations whose trigger is overdue beyond the grace.

        Args:
            grace_minutes: Minutes past `due_at` a scheduled investigation may wait for its
                trigger before it is considered lost.

        Returns:
            List of overdue scheduled investigation IDs.
        """
        deadline = dt.datetime.now(tz=dt.UTC) - dt.timedelta(
            minutes=grace_minutes
        )
        params = [
            bigquery.ScalarQueryParameter(
                "status", "STRING", InvestigationStatus.SCHEDULED.value
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
        self, observed_agent_key: str
    ) -> str | None:
        """Returns the latest telemetry window end timestamp for finished ambient investigations.

        Excludes dry runs and non-ambient triggers (`NON_AMBIENT_TRIGGER_TYPES`) because
        ad-hoc or custom sweeps use custom windows that would misalign the ambient
        telemetry watermark.

        Args:
            observed_agent_key: The agent's key, which investigations record in
                `observed_agent_key`.

        Returns:
            ISO 8601 timestamp of the latest completed window end, or None if no
            completed ambient investigation exists.

        TODO: an investigation stopped by its evaluation budget still ends ``done``, so the
        point advances past unevaluated telemetry.
        """
        params = [
            bigquery.ScalarQueryParameter(
                "status", "STRING", InvestigationStatus.DONE.value
            ),
            bigquery.ScalarQueryParameter(
                "agent", "STRING", observed_agent_key
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

    def count_in_flight(self, observed_agent_key: str) -> int:
        """Counts active in-flight investigations for an agent.

        Because durable investigations execute in separate processes, active
        counts are queried from the store rather than tracked in memory.
        Investigations untouched for longer than `IN_FLIGHT_HORIZON_HOURS` are
        excluded so aborted or crashed processes do not permanently consume
        concurrency capacity.

        Args:
            observed_agent_key: The agent's key, which investigations record in
                `observed_agent_key`.

        Returns:
            Number of pending or running investigations updated within the
            horizon.
        """
        since = (
            dt.datetime.now(tz=dt.UTC)
            - dt.timedelta(hours=IN_FLIGHT_HORIZON_HOURS)
        ).isoformat()
        params = [
            bigquery.ScalarQueryParameter(
                "agent", "STRING", observed_agent_key
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
        window rather than a paginated subset. Days with no investigations are omitted.
        Includes an `unmeasured` count for investigations where all counters are zero.

        Args:
            window_start: Optional ISO timestamp lower bound for telemetry window end.
            window_end: Optional ISO timestamp upper bound for telemetry window end.

        Returns:
            List of daily summary dictionaries ordered chronologically.
        """
        names = list_counter_names()
        sums = ",\n  ".join(
            f"SUM({_build_counter_sql(counter_to_key(n))}) AS {n}"
            for n in names
        )
        # "Measured nothing" means every counter is zero, rather than a NULL column.
        measured = " + ".join(
            _build_counter_sql(counter_to_key(n)) for n in names
        )
        conditions, params = _build_window_conditions(window_start, window_end)
        # Exclude investigations without a `window_end` so they are not bucketed
        # under an arbitrary day.
        conditions.insert(0, "window_end IS NOT NULL")
        agent, agent_params = self._build_agent_filter()
        params += agent_params
        rows = self._run(
            f"""
SELECT
  FORMAT_DATE('%Y-%m-%d', DATE(window_end)) AS day,
  COUNT(*) AS investigations,
  SUM(CASE WHEN ({measured}) = 0 THEN 1 ELSE 0 END) AS unmeasured,
  {sums}
FROM (
  SELECT run_id, window_end, {_UNWRAPPED_COUNTERS} AS counters
  FROM `{self._build_table_ref(INVESTIGATIONS_TABLE)}`{agent}
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

    def _list_events(self, investigation_id: str) -> list[InvestigationEvent]:
        """Queries all progress events for an investigation, ordered by creation time.

        Args:
            investigation_id: Investigation identifier.

        Returns:
            List of InvestigationEvent objects sorted oldest first.
        """
        rows = self._run(
            f"SELECT {_build_select_list(list_event_fields())} "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            f"FROM `{self._build_table_ref(EVENTS_TABLE)}` "
            "WHERE run_id = @investigation_id ORDER BY created_at",
            [
                bigquery.ScalarQueryParameter(
                    "investigation_id", "STRING", investigation_id
                )
            ],
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

    def _build_agent_filter(self) -> tuple[str, list[Any]]:
        """Builds the WHERE clause that scopes queries to the store's agent.

        Returns:
            Tuple of (SQL fragment with a leading space, or empty when the
            store has no agent; its query parameters).
        """
        if not self._observed_agent_key:
            return "", []
        return " WHERE observed_agent_name = @agent", [
            bigquery.ScalarQueryParameter(
                "agent", "STRING", self._observed_agent_key
            )
        ]

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


def _build_select_list(fields: Sequence[str]) -> str:
    """Builds the SELECT list that reads model fields from their columns.

    A column whose name differs from its field is selected under the field's
    name, so a row validates straight into the model.

    Args:
        fields: Sequence of model field names.

    Returns:
        Formatted comma-separated select list.
    """
    return ", ".join(
        f if field_to_column(f) == f else f"{field_to_column(f)} AS {f}"
        for f in fields
    )


def _record_to_row(
    record: Investigation | InvestigationEvent,
    *,
    exclude: set[str] | None = None,
) -> dict[str, Any]:
    """Converts a model instance into a dictionary for BigQuery JSON loading.

    Args:
        record: Investigation or InvestigationEvent instance.
        exclude: Optional set of field names to omit.

    Returns:
        Dictionary formatted for BigQuery JSON insertion.
    """
    if isinstance(record, Investigation) and record.summary is not None:
        # The events are rows of their own; carrying them here too would repeat
        # an investigation's largest payload in every snapshot after the one that ran it.
        record = record.model_copy(
            update={
                "summary": {
                    k: v for k, v in record.summary.items() if k != "events"
                }
            }
        )
    row = record.model_dump(
        mode="json", exclude=exclude, exclude_none=True, by_alias=True
    )
    return {
        field_to_column(field): json.dumps(value, ensure_ascii=False)
        if isinstance(value, dict | list)
        else value
        for field, value in row.items()
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
