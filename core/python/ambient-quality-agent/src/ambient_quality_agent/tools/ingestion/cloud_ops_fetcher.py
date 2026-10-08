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

"""Cloud Trace ingestion for the Ambient Quality Agent.

`CloudOpsFetcher` reads agent telemetry from a BigQuery linked dataset
that mirrors the Cloud Trace ``_AllSpans`` view (OTLP spans exported to
BigQuery).

The two query shapes mirror the two evaluation scopes in Gemini platform:

* ``MetricType.MULTI_TURN`` — one row per session
  (``gen_ai.conversation.id``), with every span across the session's
  traces aggregated and ordered. Each trace becomes a conversation turn.
* ``MetricType.SINGLE_TURN`` — one row per trace (one turn), with that
  trace's spans aggregated.

Both attach spans to an evaluation case by ``trace_id``. Because execution-level
spans (such as ``call_llm`` and ``execute_tool``) omit ``gen_ai.conversation.id``,
session ids are first resolved to trace ids to retain tool execution telemetry.

Span payloads are converted to `AgentData` for evaluation.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from agentplatform._genai.types import EvalCase
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion import selector
from ambient_quality_agent.tools.ingestion.base import (
    BigQueryJobFetcher,
    CachingGcsReader,
    GcsReader,
)
from ambient_quality_agent.tools.ingestion.log_fetcher import LogFetcher
from ambient_quality_agent.tools.ingestion.models import MappedRow
from ambient_quality_agent.tools.ingestion.trace_converter import (
    ATTR_AGENT_NAME,
    ATTR_INPUT_MESSAGES,
    ATTR_LLM_REQUEST,
    ATTR_LLM_RESPONSE,
    ATTR_OUTPUT_MESSAGES,
    ATTR_SESSION_ID,
    ATTR_SYSTEM_INSTRUCTIONS,
    ATTR_TOOL_DEFINITIONS,
    ConversionReport,
    MessageParseError,
    TraceAgentDataConverter,
)
from ambient_quality_agent.tools.ingestion.trace_models import (
    Span,
    SpanEvent,
    SpanStatus,
)
from ambient_quality_agent.tools.trajectories.recorder import (
    NullTrajectoryRecorder,
    TrajectoryRecorder,
)

logger = logging.getLogger(__name__)

# Removes duplicate LLM request and response payloads from intermediate spans
# (such as ``call_llm``). Child ``generate_content`` spans provide conversation
# messages; intermediate spans are needed only to resolve agent ownership for
# tool executions.
_SPAN_ATTRIBUTES = f"""
      IF(
        JSON_VALUE(s.attributes, '$."{ATTR_SESSION_ID}"') IS NULL,
        TO_JSON_STRING(
          JSON_REMOVE(
            s.attributes,
            '$."{ATTR_LLM_REQUEST}"',
            '$."{ATTR_LLM_RESPONSE}"'
          )
        ),
        TO_JSON_STRING(s.attributes)
      )
"""

# Span fields selected from the linked dataset, aggregated per trace.
# ``status`` and ``events`` carry tool-error signals (OTEL span status and
# ``exception`` events) that the converter promotes into the eval data.
_SPAN_STRUCT = f"""
    STRUCT(
      s.span_id AS span_id,
      s.parent_span_id AS parent_span_id,
      s.name AS name,
      FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%E*SZ', s.end_time, 'UTC') AS end_time,
      {_SPAN_ATTRIBUTES} AS attributes,
      s.status.code AS status_code,
      s.status.message AS status_message,
      TO_JSON_STRING(s.events) AS events
    )
"""

# The deployment revision that emitted a span: Agent Runtime records it as the
# OTLP resource attribute ``service.version``, a small integer as a string.
# A revision number only identifies a deployment once the deployment itself is
# fixed (``cloud.resource_id``); these queries are already scoped to one agent
# by ``gen_ai.agent.name``, so that column is not projected.
_SPAN_REVISION = """JSON_VALUE(s.resource.attributes, '$."service.version"')"""

# The revision a group of spans ended on. A session that spans a redeploy takes
# the later one -- the configuration in force when it finished. ``IGNORE NULLS``
# keeps a session attributed when only some of its spans carry the attribute,
# and is required because `ARRAY_AGG` rejects null inputs.
_LATEST_SPAN_REVISION = (
    f"ARRAY_AGG({_SPAN_REVISION} IGNORE NULLS ORDER BY s.start_time DESC LIMIT 1)"
    "[SAFE_OFFSET(0)]"
)

# Default target definitions for multi-turn and single-turn queries.
# The counting and sampling queries share the same targets CTE so both measure
# the exact same population.
_MULTI_TURN_TARGETS = f"""
  SELECT JSON_VALUE(attributes, '$."{ATTR_SESSION_ID}"') AS session_id
  FROM `{{table_ref}}`
  WHERE start_time BETWEEN @window_start AND @window_end
    AND JSON_VALUE(attributes, '$."{ATTR_AGENT_NAME}"') = @agent_name
    AND JSON_VALUE(attributes, '$."{ATTR_SESSION_ID}"') IS NOT NULL
  GROUP BY session_id
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized

_SINGLE_TURN_TARGETS = f"""
  SELECT trace_id
  FROM `{{table_ref}}`
  WHERE start_time BETWEEN @window_start AND @window_end
    AND JSON_VALUE(attributes, '$."{ATTR_AGENT_NAME}"') = @agent_name
  GROUP BY trace_id
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized

_SINGLE_TURN_COUNT_SQL = (
    f"SELECT COUNT(*) AS scanned FROM ({_SINGLE_TURN_TARGETS})"  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
)

# The two targets above without their agent filter, counted per agent: what
# `count_by_agent` reports when the configured name matched nothing.
_MULTI_TURN_COUNT_BY_AGENT_SQL = f"""
  SELECT
    JSON_VALUE(attributes, '$."{ATTR_AGENT_NAME}"') AS agent_name,
    COUNT(DISTINCT JSON_VALUE(attributes, '$."{ATTR_SESSION_ID}"')) AS scanned
  FROM `{{table_ref}}`
  WHERE start_time BETWEEN @window_start AND @window_end
    AND JSON_VALUE(attributes, '$."{ATTR_AGENT_NAME}"') IS NOT NULL
    AND JSON_VALUE(attributes, '$."{ATTR_SESSION_ID}"') IS NOT NULL
  GROUP BY agent_name
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized

_SINGLE_TURN_COUNT_BY_AGENT_SQL = f"""
  SELECT
    JSON_VALUE(attributes, '$."{ATTR_AGENT_NAME}"') AS agent_name,
    COUNT(DISTINCT trace_id) AS scanned
  FROM `{{table_ref}}`
  WHERE start_time BETWEEN @window_start AND @window_end
    AND JSON_VALUE(attributes, '$."{ATTR_AGENT_NAME}"') IS NOT NULL
  GROUP BY agent_name
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized


# The attribute keys, on a span or on its log entries' labels, under which the
# converter finds a ``gs://`` pointer to offloaded content.
_PAYLOAD_REF_KEYS = tuple(
    f"{key}_ref"
    for key in (
        ATTR_INPUT_MESSAGES,
        ATTR_OUTPUT_MESSAGES,
        ATTR_SYSTEM_INSTRUCTIONS,
        ATTR_TOOL_DEFINITIONS,
    )
)

_SPAN_PAYLOAD_REF = "COALESCE({})".format(
    ", ".join(
        f"""JSON_VALUE(attributes, '$."{key}"')""" for key in _PAYLOAD_REF_KEYS
    )
)

# One payload reference among the spans of the agent's traces. The agent name is
# on the agent's own spans, not necessarily on the model-call spans that carry
# the reference, so the traces are selected first, as ingestion does.
_PAYLOAD_REF_SQL = f"""
SELECT {_SPAN_PAYLOAD_REF} AS ref
FROM `{{table_ref}}`
WHERE start_time BETWEEN @window_start AND @window_end
  AND trace_id IN ({_SINGLE_TURN_TARGETS})
  AND {_SPAN_PAYLOAD_REF} IS NOT NULL
LIMIT 1
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized


def _build_scanned_count_sql(targets: str) -> str:
    """Build a query calculating the total count of matching target records.

    Args:
        targets: Target selection subquery SQL fragment.

    Returns:
        SQL query returning the total record count as ``scanned``.
    """
    return f"SELECT COUNT(*) AS scanned FROM ({targets})"  # noqa: S608 - may embed agent selector SQL, gated by precheck_selector's table guard and dry-run allowlist


def _build_multi_turn_sql(targets: str, table_ref: str) -> str:
    """Build the session-level ingestion query over ``targets``.

    Maps sampled sessions to trace IDs to aggregate all child spans per trace.
    Random sampling and limit application occur inside the CTE to ensure
    unbiased sampling when matches exceed the evaluation budget.

    Args:
        targets: Targets CTE SQL fragment projecting ``session_id``.
        table_ref: Fully-qualified table containing Cloud Trace spans.

    Returns:
        Complete multi-turn SQL query.
    """
    return f"""
WITH TargetSessions AS (
{targets}
  ORDER BY RAND()
  LIMIT @limit
),
TargetTraces AS (
  SELECT s.trace_id AS trace_id, t.session_id AS session_id
  FROM `{table_ref}` AS s
  JOIN TargetSessions AS t
    ON JSON_VALUE(s.attributes, '$."{ATTR_SESSION_ID}"') = t.session_id
  GROUP BY trace_id, session_id
)
SELECT
  session_id,
  ARRAY_AGG(
    STRUCT(trace_id, spans)
    ORDER BY trace_start ASC
  ) AS traces,
  ARRAY_AGG(
    agent_revision IGNORE NULLS ORDER BY trace_start DESC LIMIT 1
  )[SAFE_OFFSET(0)] AS agent_revision
FROM (
  SELECT
    t.session_id AS session_id,
    s.trace_id AS trace_id,
    MIN(s.start_time) AS trace_start,
    {_LATEST_SPAN_REVISION} AS agent_revision,
    ARRAY_AGG({_SPAN_STRUCT} ORDER BY s.start_time ASC) AS spans
  FROM `{table_ref}` AS s
  JOIN TargetTraces AS t USING (trace_id)
  GROUP BY s.trace_id, t.session_id
)
GROUP BY session_id
"""  # noqa: S608 - may embed agent selector SQL, gated by precheck_selector's table guard and dry-run allowlist


# Samples traces directly up to the evaluation budget for single-turn evaluation.
_SINGLE_TURN_SQL = f"""
WITH TargetTraces AS (
{_SINGLE_TURN_TARGETS}
  ORDER BY RAND()
  LIMIT @limit
)
SELECT
  s.trace_id AS trace_id,
  JSON_VALUE(ANY_VALUE(s.attributes), '$."{ATTR_SESSION_ID}"') AS session_id,
  {_LATEST_SPAN_REVISION} AS agent_revision,
  ARRAY_AGG({_SPAN_STRUCT} ORDER BY s.start_time ASC) AS spans
FROM `{{table_ref}}` AS s
JOIN TargetTraces AS t USING (trace_id)
GROUP BY s.trace_id
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized


class CloudOpsFetcher(BigQueryJobFetcher):
    """Fetches and shapes agent telemetry from a Cloud Trace linked dataset.

    Inherits all BigQuery job/paging plumbing from `BigQueryJobFetcher`;
    only the SQL templates and the row mapping are specific to the OTLP
    ``_AllSpans`` schema. Unlike the analytics path, a row whose telemetry
    fails to parse is dropped (see `_map_page`).
    """

    TELEMETRY_SOURCE = selector.CLOUD_OPS_SOURCE
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
            dataset: BigQuery linked dataset (e.g.
                ``trace_spans_linked``).
            table: BigQuery table/view holding the OTLP spans (e.g.
                ``_AllSpans``).
            location: BigQuery location/region of the job and dataset.
            observed_project_id: Google Cloud project of the observed agent,
                hosting the linked dataset, when it is not `project_id`.
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
        # Message content is logged where the spans are, in the agent's project.
        self._log_fetcher = LogFetcher(observed_project_id or project_id)

    def _build_ingestion_sql(self, metric_type: MetricType) -> str:
        if metric_type is MetricType.MULTI_TURN:
            return _build_multi_turn_sql(
                self._resolve_multi_turn_targets(), self.table_ref
            )
        return self._substitute_table_ref(_SINGLE_TURN_SQL)

    def _build_count_sql(self, metric_type: MetricType) -> str:
        if metric_type is MetricType.MULTI_TURN:
            return _build_scanned_count_sql(self._resolve_multi_turn_targets())
        return self._substitute_table_ref(_SINGLE_TURN_COUNT_SQL)

    def _build_count_by_agent_sql(self, metric_type: MetricType) -> str:
        if metric_type is MetricType.MULTI_TURN:
            return self._substitute_table_ref(_MULTI_TURN_COUNT_BY_AGENT_SQL)
        return self._substitute_table_ref(_SINGLE_TURN_COUNT_BY_AGENT_SQL)

    def get_payload_ref(
        self, agent_name: str, start: dt.datetime, end: dt.datetime
    ) -> str | None:
        """Returns one payload reference, from a span or from a log entry.

        The two places the converter looks, in its order: the spans' attributes,
        then the labels of the log entries correlated with them.

        Args:
            agent_name: Watched agent name.
            start: Window start timestamp.
            end: Window end timestamp.

        Returns:
            A ``gs://`` URI, or None when neither holds one in the window.
        """
        ref = self._select_payload_ref(_PAYLOAD_REF_SQL, agent_name, start, end)
        if ref is not None:
            return ref
        return self._log_fetcher.get_payload_ref(
            agent_name, _PAYLOAD_REF_KEYS, start=start, end=end
        )

    def _resolve_multi_turn_targets(self) -> str:
        """Resolve the target session IDs for multi-turn ingestion.

        Returns:
            SQL fragment producing distinct ``session_id`` values.
        """
        return self._resolve_targets(_MULTI_TURN_TARGETS, "session_id")

    def _map_row(
        self, row: Mapping[str, Any], metric_type: MetricType
    ) -> MappedRow:
        """Build one `EvalCase` from a query row for ``metric_type``.

        Returns a case-less `MappedRow` (with a logged warning) when the case
        has no usable telemetry, dropping it from evaluation. This occurs when
        message payloads cannot be parsed (e.g. due to truncation) or when spans
        contain no conversation content. Partial cases—such as unreadable GCS
        payloads or traces yielding no turns—are kept and marked partial.
        A row whose spans omit ``service.version`` defaults to an empty revision,
        representing the deployment's single unnamed revision.

        Every outcome preserves its IDs, including dropped cases, to allow
        downstream stores to track the conversation.

        Args:
            row: BigQuery query result row mapping.
            metric_type: Evaluation metric scope (single-turn or multi-turn).

        Returns:
            Mapped row carrying the evaluation case and metadata.
        """
        if metric_type is MetricType.MULTI_TURN:
            eval_case_id = row.get("session_id")
            session_id = eval_case_id
            traces = [
                _decode_spans(trace.get("spans"))
                for trace in row.get("traces") or []
            ]
            trace_ids = [
                trace.get("trace_id")
                for trace in row.get("traces") or []
                if trace.get("trace_id")
            ]
        else:
            eval_case_id = row.get("trace_id")
            session_id = row.get("session_id")
            traces = [_decode_spans(row.get("spans"))]
            trace_ids = [row["trace_id"]] if row.get("trace_id") else []

        # Preserves trace start order so the first ID corresponds to the opening turn used in console links.
        trajectory_id = str(eval_case_id or "")
        source_trace_ids = tuple(str(trace_id) for trace_id in trace_ids)

        start, end = _compute_span_time_window(traces)
        log_entries_by_span = self._log_fetcher.fetch_entries_by_span(
            trace_ids, start=start, end=end
        )
        report = ConversionReport()
        try:
            agent_data = self._converter.convert(
                traces, log_entries_by_span, report
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


def _compute_span_time_window(
    traces: Sequence[Sequence[Span]],
) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Return the earliest/latest span ``end_time`` across ``traces``.

    Bounds the Cloud Logging query to the sampled cases' time range.
    Only ``end_time`` is available on spans, so it stands in for both ends
    of the window; the fetcher's buffer absorbs start/end skew.

    Args:
        traces: Sequence of traces containing spans.

    Returns:
        Tuple of (earliest, latest) datetimes, or (None, None) if no spans
        contain parseable timestamps.
    """
    times = [
        parsed
        for trace in traces
        for span in trace
        if (parsed := _parse_span_end_time(span.end_time)) is not None
    ]
    if not times:
        return None, None
    return min(times), max(times)


def _parse_span_end_time(end_time: str | None) -> dt.datetime | None:
    """Parse a span's ISO-8601 ``end_time`` into a UTC datetime.

    Args:
        end_time: ISO-8601 formatted timestamp string.

    Returns:
        Parsed datetime object, or None if input is empty or invalid.
    """
    if not end_time:
        return None
    try:
        return dt.datetime.fromisoformat(end_time.replace("Z", "+00:00"))
    except ValueError:
        return None


def _decode_spans(spans: Any) -> list[Span]:
    """Decode a trace's span structs into typed `Span` objects.

    Args:
        spans: Raw span records from BigQuery query result.

    Returns:
        List of decoded `Span` instances.
    """
    decoded: list[Span] = []
    for span in spans or []:
        if not isinstance(span, Mapping):
            continue
        events = [
            SpanEvent(
                name=event.get("name"),
                attributes=event.get("attributes")
                if isinstance(event.get("attributes"), dict)
                else {},
            )
            for event in _decode_json_field(span.get("events"), [])
            if isinstance(event, Mapping)
        ]
        decoded.append(
            Span(
                span_id=str(span.get("span_id") or ""),
                parent_span_id=span.get("parent_span_id"),
                name=span.get("name"),
                end_time=span.get("end_time"),
                attributes=_decode_json_field(span.get("attributes"), {}),
                status=SpanStatus(
                    code=span.get("status_code"),
                    message=span.get("status_message"),
                ),
                events=events,
            )
        )
    return decoded


def _decode_json_field(raw: Any, default: Any) -> Any:
    """Parse a BigQuery JSON string column value into the type of ``default``.

    Guarantees a return value matching ``type(default)``. A SQL NULL serialized
    with ``TO_JSON_STRING`` produces the string literal ``"null"``, which parses
    as Python ``None`` rather than the expected container. Unparseable strings or
    mismatched JSON shapes safely fall back to ``default``.

    Args:
        raw: Raw JSON string from BigQuery.
        default: Fallback object whose type the return value must match.

    Returns:
        Parsed JSON object if matching type(default), otherwise default.
    """
    if not isinstance(raw, str) or not raw.strip():
        return default
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse span JSON field.")
        return default
    if not isinstance(parsed, type(default)):
        logger.warning(
            "Ignoring span JSON field of unusable type %s.",
            type(parsed).__name__,
        )
        return default
    return parsed
