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

"""Cloud Logging ingestion for the Ambient Quality Agent.

`CloudLoggingFetcher` reads agent telemetry from a BigQuery table populated
by a Cloud Logging sink that exports the
``gen_ai.client.inference.operation.details`` log (the shape produced by
agents deployed with agents-cli).
"""

from __future__ import annotations

import datetime as dt
import functools
import logging
from collections.abc import Callable, Container, Mapping
from typing import Any

from agentplatform._genai.types import EvalCase
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion import selector
from ambient_quality_agent.tools.ingestion.base import (
    BigQueryJobFetcher,
    CachingGcsReader,
    GcsReader,
)
from ambient_quality_agent.tools.ingestion.models import MappedRow
from ambient_quality_agent.tools.ingestion.trace_converter import (
    ATTR_AGENT_NAME,
    ATTR_INPUT_MESSAGES,
    ATTR_OUTPUT_MESSAGES,
    ATTR_SYSTEM_INSTRUCTIONS,
    ATTR_TOOL_DEFINITIONS,
    ConversionReport,
    MessageParseError,
    TraceAgentDataConverter,
)
from ambient_quality_agent.tools.ingestion.trace_models import Span, SpanStatus
from ambient_quality_agent.tools.trajectories.recorder import (
    NullTrajectoryRecorder,
    TrajectoryRecorder,
)

logger = logging.getLogger(__name__)

# The sink flattens each OTEL attribute into a ``labels`` STRUCT field with
# dots replaced by underscores. These are the identity and revision
# ``labels.*`` columns we read; the content columns are listed in
# `_REF_LABEL_TO_ATTR` and `_INLINE_LABEL_TO_ATTR`.
_LABEL_SESSION_ID = "gen_ai_conversation_id"
_LABEL_AGENT_NAME = "gen_ai_agent_name"
_LABEL_SERVICE_VERSION = "service_version"

# Sink column holding flattened OpenTelemetry attributes.
_LABELS_COLUMN = "labels"

# Fallback expression for unmaterialized label columns.
_ABSENT_LABEL = "CAST(NULL AS STRING)"

# Maps a sink ``labels.*_ref`` column to the OTEL attribute key the
# converter looks up for its GCS ref cascade. The synthesized span carries
# each GCS pointer under ``<content-key>_ref`` (the suffix the converter
# appends). Tool definitions use the dotted ``gen_ai.tool.definitions`` key
# -- the only tool key whose ref variant the converter resolves; the flat
# ``gen_ai.tool_definitions`` alias does not accept a ref.
_REF_LABEL_TO_ATTR = {
    "gen_ai_input_messages_ref": f"{ATTR_INPUT_MESSAGES}_ref",
    "gen_ai_output_messages_ref": f"{ATTR_OUTPUT_MESSAGES}_ref",
    "gen_ai_system_instructions_ref": f"{ATTR_SYSTEM_INSTRUCTIONS}_ref",
    "gen_ai_tool_definitions_ref": f"{ATTR_TOOL_DEFINITIONS}_ref",
}

# Maps a sink ``labels.*`` column holding inline content to the OTEL attribute
# key the converter falls back to when the matching ref is absent or
# unreadable. Cloud Logging truncates label values at 64 KiB, so an oversized
# inline payload is invalid JSON. When the converter falls back to a truncated
# label, including after an unreadable ref, it raises `MessageParseError` and
# the case is dropped as incomplete telemetry. Large or multimodal content
# needs the GCS upload hook.
_INLINE_LABEL_TO_ATTR = {
    "gen_ai_input_messages": ATTR_INPUT_MESSAGES,
    "gen_ai_output_messages": ATTR_OUTPUT_MESSAGES,
    "gen_ai_system_instructions": ATTR_SYSTEM_INSTRUCTIONS,
    "gen_ai_tool_definitions": ATTR_TOOL_DEFINITIONS,
}

_CONTENT_LABEL_TO_ATTR = {**_REF_LABEL_TO_ATTR, **_INLINE_LABEL_TO_ATTR}


def _build_label_expression(
    name: str, available: Container[str], alias: str = "s"
) -> str:
    """Renders a ``labels.<name>`` SQL reference based on available schema fields.

    Cloud Logging sinks create ``labels.*`` subfields only after log entries
    write to them. Querying a non-existent field causes a BigQuery 400 error
    rather than evaluating to NULL. Missing fields are compiled to typed NULL
    literals so queries execute safely across projections, joins, and filters.

    Args:
        name: Subfield name within the ``labels`` STRUCT.
        available: Subfield names present in the table's ``labels`` STRUCT.
        alias: Table alias prefix to qualify the reference with, or empty string.

    Returns:
        SQL expression of type STRING representing the column or typed NULL.
    """
    if name not in available:
        return _ABSENT_LABEL
    prefix = f"{alias}." if alias else ""
    return f"{prefix}{_LABELS_COLUMN}.{name}"


def _build_row_struct(labels: Container[str]) -> str:
    """Builds a BigQuery STRUCT expression projecting required inference fields.

    Fields are unpacked individually to preserve typed access in BigQuery.
    ``trace`` groups calls within a turn, and ``timestamp`` provides ordering.
    Both the GCS ref and the inline content label of each content key are
    projected, so the converter can fall back to inline content when a ref is
    absent or unreadable.

    Args:
        labels: Subfield names present in the table's ``labels`` STRUCT.

    Returns:
        SQL STRUCT expression with field aliases expected by `_rows_to_spans`.
    """
    content = "".join(
        f",\n      {_build_label_expression(name, labels)} AS {name}"
        for name in _CONTENT_LABEL_TO_ATTR
    )
    return f"""
    STRUCT(
      s.trace AS trace,
      s.spanId AS span_id,
      FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*SZ', s.timestamp, 'UTC') AS end_time,
      {_build_label_expression(_LABEL_AGENT_NAME, labels)} AS agent_name{content}
    )
"""


def _build_latest_row_revision(labels: Container[str]) -> str:
    """Builds a SQL expression for the latest deployment revision in a row group.

    Agent Runtime records deployment revisions in the ``service.version`` label.
    Traces spanning redeployments use the latest timestamped revision.
    ``IGNORE NULLS`` maintains attribution when only a subset of rows carries
    the label.

    Args:
        labels: Subfield names present in the table's ``labels`` STRUCT.

    Returns:
        SQL STRING expression yielding the latest revision, or NULL if unset.
    """
    return (
        f"ARRAY_AGG({_build_label_expression(_LABEL_SERVICE_VERSION, labels)} IGNORE NULLS "
        "ORDER BY s.timestamp DESC LIMIT 1)[SAFE_OFFSET(0)]"
    )


def _build_multi_turn_targets(labels: Container[str]) -> str:
    """Builds the subquery selecting distinct session IDs within the time window.

    Shared between sampling and count queries to ensure identical target populations.

    Args:
        labels: Subfield names present in the table's ``labels`` STRUCT.

    Returns:
        SQL subquery template containing a ``{table_ref}`` placeholder.
    """
    session_id = _build_label_expression(_LABEL_SESSION_ID, labels, alias="")
    return f"""
  SELECT {session_id} AS session_id
  FROM `{{table_ref}}`
  WHERE timestamp BETWEEN @window_start AND @window_end
    AND {_build_label_expression(_LABEL_AGENT_NAME, labels, alias="")} = @agent_name
    AND {session_id} IS NOT NULL
  GROUP BY session_id
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized


def _build_single_turn_targets(labels: Container[str]) -> str:
    """Builds the subquery selecting distinct trace IDs within the time window.

    Shared between sampling and count queries to ensure identical target populations.

    Args:
        labels: Subfield names present in the table's ``labels`` STRUCT.

    Returns:
        SQL subquery template containing a ``{table_ref}`` placeholder.
    """
    return f"""
  SELECT trace AS trace_id
  FROM `{{table_ref}}`
  WHERE timestamp BETWEEN @window_start AND @window_end
    AND {_build_label_expression(_LABEL_AGENT_NAME, labels, alias="")} = @agent_name
    AND trace IS NOT NULL
  GROUP BY trace_id
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized


def _build_count_by_agent_sql(
    labels: Container[str], metric_type: MetricType
) -> str:
    """Builds the per-agent count over the targets population, unfiltered by agent.

    Mirrors `_build_multi_turn_targets` and `_build_single_turn_targets`
    without their agent filter. A table with no agent-name label yields no rows,
    since every name reads as NULL.

    Args:
        labels: Subfield names present in the table's ``labels`` STRUCT.
        metric_type: Ingestion scope, which picks sessions or traces.

    Returns:
        SQL subquery template containing a ``{table_ref}`` placeholder.
    """
    agent_name = _build_label_expression(_LABEL_AGENT_NAME, labels, alias="")
    if metric_type is MetricType.MULTI_TURN:
        unit = _build_label_expression(_LABEL_SESSION_ID, labels, alias="")
    else:
        unit = "trace"
    return f"""
  SELECT {agent_name} AS agent_name, COUNT(DISTINCT {unit}) AS scanned
  FROM `{{table_ref}}`
  WHERE timestamp BETWEEN @window_start AND @window_end
    AND {agent_name} IS NOT NULL
    AND {unit} IS NOT NULL
  GROUP BY agent_name
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized


def _build_scanned_count_sql(targets: str) -> str:
    """Builds a query counting the total records selected by a targets subquery.

    Args:
        targets: Target selection subquery SQL fragment without placeholders.

    Returns:
        SQL query returning the total record count as ``scanned``.
    """
    return f"SELECT COUNT(*) AS scanned FROM ({targets})"  # noqa: S608 - may embed agent selector SQL, gated by precheck_selector's table guard and dry-run allowlist


def _build_payload_ref_sql(labels: Container[str]) -> str | None:
    """Builds the query returning one payload reference among the agent's rows.

    Args:
        labels: Subfield names present in the table's ``labels`` STRUCT.

    Returns:
        SQL query template containing a ``{table_ref}`` placeholder and
        returning at most one ``ref``, or None when the table has no reference
        column to read.
    """
    present = [name for name in _REF_LABEL_TO_ATTR if name in labels]
    if not present:
        return None
    ref = (
        f"COALESCE({', '.join(f'{_LABELS_COLUMN}.{name}' for name in present)})"
    )
    return f"""
SELECT {ref} AS ref
FROM `{{table_ref}}`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND {_build_label_expression(_LABEL_AGENT_NAME, labels, alias="")} = @agent_name
  AND {ref} IS NOT NULL
LIMIT 1
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized


def _build_multi_turn_sql(
    labels: Container[str], targets: str, table_ref: str
) -> str:
    """Builds a multi-turn ingestion query to sample sessions and group rows by trace.

    Interpolates `table_ref` directly because `targets` may contain custom SQL
    with braces (e.g. regex quantifiers or JSON literals) that `str.format`
    would misinterpret as template placeholders.

    Args:
        labels: Column names present in the table's ``labels`` STRUCT.
        targets: Resolved CTE subquery projecting ``session_id``.
        table_ref: Fully-qualified BigQuery table reference (``project.dataset.table``).

    Returns:
        Executable SQL query.
    """
    return f"""
WITH TargetSessions AS (
{targets}
  ORDER BY RAND()
  LIMIT @limit
)
SELECT
  session_id,
  ARRAY_AGG(
    STRUCT(trace_id, inference_rows)
    ORDER BY trace_start ASC
  ) AS traces,
  ARRAY_AGG(
    agent_revision IGNORE NULLS ORDER BY trace_start DESC LIMIT 1
  )[SAFE_OFFSET(0)] AS agent_revision
FROM (
  SELECT
    ANY_VALUE({_build_label_expression(_LABEL_SESSION_ID, labels)}) AS session_id,
    s.trace AS trace_id,
    MIN(s.timestamp) AS trace_start,
    {_build_latest_row_revision(labels)} AS agent_revision,
    ARRAY_AGG({_build_row_struct(labels)} ORDER BY s.timestamp ASC) AS inference_rows
  FROM `{table_ref}` AS s
  JOIN TargetSessions AS t
    ON {_build_label_expression(_LABEL_SESSION_ID, labels)} = t.session_id
  GROUP BY s.trace
)
GROUP BY session_id
"""  # noqa: S608 - may embed agent selector SQL, gated by precheck_selector's table guard and dry-run allowlist


def _build_single_turn_sql(labels: Container[str]) -> str:
    """Builds the single-turn query to sample individual traces as evaluation cases.

    Args:
        labels: Subfield names present in the table's ``labels`` STRUCT.

    Returns:
        SQL query template containing a ``{table_ref}`` placeholder.
    """
    return f"""
WITH TargetTraces AS (
{_build_single_turn_targets(labels)}
  ORDER BY RAND()
  LIMIT @limit
)
SELECT
  s.trace AS trace_id,
  ANY_VALUE({_build_label_expression(_LABEL_SESSION_ID, labels)}) AS session_id,
  {_build_latest_row_revision(labels)} AS agent_revision,
  ARRAY_AGG({_build_row_struct(labels)} ORDER BY s.timestamp ASC) AS inference_rows
FROM `{{table_ref}}` AS s
JOIN TargetTraces AS t ON s.trace = t.trace_id
GROUP BY s.trace
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized


class CloudLoggingFetcher(BigQueryJobFetcher):
    """Fetches and shapes agent telemetry from a Cloud Logging export table."""

    TELEMETRY_SOURCE = selector.CLOUD_LOGGING_SOURCE
    FOLLOWS_PAYLOAD_REFS = True

    def __init__(
        self,
        *,
        project_id: str,
        dataset: str,
        table: str,
        location: str,
        observed_project_id: str = "",
        gcs_reader: GcsReader | None = None,
        memory_guard: Callable[[], bool] | None = None,
        recorder: TrajectoryRecorder | NullTrajectoryRecorder | None = None,
        selector_sql: str = "",
        selector_ai_model: str = "",
        job_timeout_ms: int | None = None,
    ) -> None:
        """Initialize the fetcher.

        Args:
            project_id: Google Cloud project the query jobs run in.
            dataset: BigQuery dataset holding the sink table (e.g.
                ``<agent>_telemetry``).
            table: BigQuery table with the exported inference log rows
                (e.g. ``gen_ai_client_inference_operation_details``).
            location: BigQuery location/region of the job and dataset.
            observed_project_id: Google Cloud project of the observed agent,
                hosting the export dataset, when it is not `project_id`.
            gcs_reader: Resolves ``gs://`` message/tool refs.
            memory_guard: Run-shared memory guard (see `BaseFetcher`).
            recorder: Where to report sampled trajectories (see `BaseFetcher`).
            selector_sql: Agent-authored query selecting specific sessions to
                review (see `BigQueryJobFetcher`).
            selector_ai_model: Model endpoint for selector `AI.IF` calls
                (see `BigQueryJobFetcher`).
            job_timeout_ms: Server-side execution timeout in milliseconds for
                BigQuery query jobs (see `BigQueryJobFetcher`).
        """
        super().__init__(
            project_id=project_id,
            dataset=dataset,
            table=table,
            location=location,
            observed_project_id=observed_project_id,
            memory_guard=memory_guard,
            recorder=recorder,
            selector_sql=selector_sql,
            selector_ai_model=selector_ai_model,
            job_timeout_ms=job_timeout_ms,
        )
        self._converter = TraceAgentDataConverter(
            gcs_reader=gcs_reader or CachingGcsReader()
        )

    @functools.cached_property
    def _available_labels(self) -> frozenset[str]:
        """Discovers subfields present in the table's ``labels`` STRUCT.

        Cached per fetcher instance because the sink table schema remains static
        during a run, avoiding redundant metadata RPCs. `functools.cached_property`
        is not thread-safe, so this relies on `run_sweep` building one fetcher per
        sweep via `fetcher_factory`: an instance is never shared across the threads
        its nodes run on. Pooling or sharing fetchers would allow concurrent
        threads to each compute the value.

        Returns:
            Set of field names present under the ``labels`` STRUCT column.
        """
        table = self.load_table()
        available = frozenset(
            sub_field.name
            for field in table.schema
            if field.name == _LABELS_COLUMN
            for sub_field in field.fields
        )
        # A missing labels struct usually means the wrong table, but a fresh
        # deployment whose sink table holds no labelled entries yet looks the
        # same, so warn and read every label as NULL instead of failing the run.
        if not available:
            logger.warning(
                "Table %s exposes no %r sub-fields; every label reads as NULL.",
                self.table_ref,
                _LABELS_COLUMN,
            )
        return available

    def _build_ingestion_sql(self, metric_type: MetricType) -> str:
        labels = self._available_labels
        if metric_type is MetricType.MULTI_TURN:
            return _build_multi_turn_sql(
                labels, self._resolve_multi_turn_targets(), self.table_ref
            )
        return self._substitute_table_ref(_build_single_turn_sql(labels))

    def _build_count_sql(self, metric_type: MetricType) -> str:
        labels = self._available_labels
        if metric_type is MetricType.MULTI_TURN:
            return _build_scanned_count_sql(self._resolve_multi_turn_targets())
        return self._substitute_table_ref(
            _build_scanned_count_sql(_build_single_turn_targets(labels))
        )

    def _build_count_by_agent_sql(self, metric_type: MetricType) -> str:
        return self._substitute_table_ref(
            _build_count_by_agent_sql(self._available_labels, metric_type)
        )

    def get_payload_ref(
        self, agent_name: str, start: dt.datetime, end: dt.datetime
    ) -> str | None:
        """Returns one ``labels.*_ref`` value from the agent's rows in a window.

        Args:
            agent_name: Watched agent name.
            start: Window start timestamp.
            end: Window end timestamp.

        Returns:
            A ``gs://`` URI, or None when no row in the window carries one.
        """
        template = _build_payload_ref_sql(self._available_labels)
        if template is None:
            return None
        return self._select_payload_ref(template, agent_name, start, end)

    def _resolve_multi_turn_targets(self) -> str:
        """Resolves the target session population for multi-turn ingestion.

        Returns:
            SQL fragment projecting distinct ``session_id`` values matching the
            configured selector or the full time window.
        """
        return self._resolve_targets(
            _build_multi_turn_targets(self._available_labels), "session_id"
        )

    def _map_row(
        self, row: Mapping[str, Any], metric_type: MetricType
    ) -> MappedRow:
        """Build one `EvalCase` from a query row.

        Drops the case (returning a case-less `MappedRow`) when telemetry cannot
        be parsed, and marks it partial when telemetry was incomplete. Like
        `CloudOpsFetcher._map_row`, it preserves IDs across all outcomes.

        A row whose log entries omit ``labels.service_version`` defaults to an
        empty revision, representing the deployment's single unnamed revision.

        Args:
            row: Query result row mapping.
            metric_type: Evaluation metric scope (single-turn or multi-turn).

        Returns:
            Mapped row carrying the evaluation case and metadata.
        """
        if metric_type is MetricType.MULTI_TURN:
            eval_case_id = row.get("session_id")
            session_id = eval_case_id
            traces = [
                _rows_to_spans(trace.get("inference_rows"))
                for trace in row.get("traces") or []
            ]
            # Preserves chronological trace order so the first ID corresponds to the opening turn.
            trace_ids = [
                trace.get("trace_id")
                for trace in row.get("traces") or []
                if trace.get("trace_id")
            ]
        else:
            eval_case_id = row.get("trace_id")
            session_id = row.get("session_id")
            traces = [_rows_to_spans(row.get("inference_rows"))]
            trace_ids = [row["trace_id"]] if row.get("trace_id") else []

        trajectory_id = str(eval_case_id or "")
        source_trace_ids = tuple(str(trace_id) for trace_id in trace_ids)

        report = ConversionReport()
        try:
            agent_data = self._converter.convert(
                traces, log_entries_by_span=None, report=report
            )
        except MessageParseError as exc:
            logger.warning("Dropping case %s: %s", eval_case_id, exc)
            return MappedRow(
                trajectory_id=trajectory_id,
                session_id=session_id,
                trace_ids=source_trace_ids,
            )
        if not agent_data.turns:
            logger.warning(
                "Dropping case %s: no conversation turns.", eval_case_id
            )
            return MappedRow(
                trajectory_id=trajectory_id,
                session_id=session_id,
                trace_ids=source_trace_ids,
            )
        if report.degraded:
            logger.warning(
                "Case %s ingested partially: %d unresolved ref(s), %d trace(s) "
                "without a turn.",
                eval_case_id,
                report.unresolved_refs,
                report.dropped_traces,
            )
        return MappedRow(
            case=EvalCase(eval_case_id=eval_case_id, agent_data=agent_data),
            partial=report.degraded,
            agent_revision=str(row.get("agent_revision") or ""),
            trajectory_id=trajectory_id,
            session_id=session_id,
            trace_ids=source_trace_ids,
        )


def _rows_to_spans(rows: Any) -> list[Span]:
    """Synthesizes one `Span` per inference log row for the converter.

    Each span carries the agent name plus every non-empty ref and inline
    content label under the attribute key the converter resolves.

    Args:
        rows: ``inference_rows`` array from a query row, shaped by
            `_build_row_struct`; non-mapping items are skipped.

    Returns:
        Spans in row order.
    """
    spans: list[Span] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        attributes: dict[str, Any] = {}
        agent_name = row.get("agent_name")
        if agent_name:
            attributes[ATTR_AGENT_NAME] = agent_name
        for label, attr_key in _CONTENT_LABEL_TO_ATTR.items():
            value = row.get(label)
            if value:
                attributes[attr_key] = value
        spans.append(
            Span(
                span_id=str(row.get("span_id") or ""),
                parent_span_id=None,
                name=None,
                end_time=row.get("end_time"),
                attributes=attributes,
                status=SpanStatus(code=None, message=None),
                events=[],
            )
        )
    return spans
