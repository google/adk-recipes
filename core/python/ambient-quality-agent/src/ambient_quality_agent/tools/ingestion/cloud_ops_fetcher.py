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
  (``gen_ai.conversation.id``), with the spans of the session's traces
  aggregated and ordered. Each trace becomes a conversation turn.
* ``MetricType.SINGLE_TURN`` — one row per trace (one turn), with that
  trace's spans aggregated.

Both attach spans to an evaluation case by ``trace_id``. Because execution-level
spans (such as ``call_llm`` and ``execute_tool``) omit ``gen_ai.conversation.id``,
session ids are first resolved to trace ids to retain tool execution telemetry.
Both read a selected session's or trace's spans only within
`JOIN_BACK_MARGIN_DAYS` of the window (see `build_join_back_window`). Weighted
sampling scores a session or trace on the spans it recorded in the window
(`_build_measures_sql`).

Span payloads are converted to `AgentData` for evaluation.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any

from agentplatform._genai.types import EvalCase
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion import sampling, selector
from ambient_quality_agent.tools.ingestion.base import (
    DEFAULT_JOB_TIMEOUT_MS,
    BigQueryJobFetcher,
    CachingGcsReader,
    GcsReader,
    build_join_back_window,
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
    LogEntryView,
    Span,
    SpanEvent,
    SpanStatus,
)
from ambient_quality_agent.tools.ingestion.trajectory_files import (
    TrajectoryFileStore,
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

_JOIN_BACK_WINDOW = build_join_back_window("s.start_time")


# Default target definitions for multi-turn and single-turn queries.
# The counting and sampling queries share the same targets CTE so both measure
# the exact same population.
def _build_multi_turn_targets(id_column: str) -> str:
    """Builds the multi-turn targets subquery projecting the session as `id_column`.

    Args:
        id_column: Column name the session ID is projected and grouped as.

    Returns:
        SQL subquery template containing a ``{table_ref}`` placeholder.
    """
    return f"""
  SELECT JSON_VALUE(attributes, '$."{ATTR_SESSION_ID}"') AS {id_column}
  FROM `{{table_ref}}`
  WHERE start_time BETWEEN @window_start AND @window_end
    AND JSON_VALUE(attributes, '$."{ATTR_AGENT_NAME}"') = @agent_name
    AND JSON_VALUE(attributes, '$."{ATTR_SESSION_ID}"') IS NOT NULL
  GROUP BY {id_column}
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized


_MULTI_TURN_TARGETS = _build_multi_turn_targets("session_id")

_SINGLE_TURN_TARGETS = f"""
  SELECT trace_id
  FROM `{{table_ref}}`
  WHERE start_time BETWEEN @window_start AND @window_end
    AND JSON_VALUE(attributes, '$."{ATTR_AGENT_NAME}"') = @agent_name
  GROUP BY trace_id
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized

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


_ATTR_OPERATION_NAME = "gen_ai.operation.name"
_ATTR_FINISH_REASONS = "gen_ai.response.finish_reasons"
_ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"
_ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
_ATTR_TOOL_NAME = "gen_ai.tool.name"


def _build_attribute_sql(key: str) -> str:
    """Builds a span attribute read as a string, from the spans table ``s``.

    Args:
        key: OpenTelemetry attribute key.

    Returns:
        A STRING expression, NULL where the span lacks the attribute.
    """
    return f"""JSON_VALUE(s.attributes, '$."{key}"')"""


def _build_token_sql(key: str) -> str:
    """Builds a span's token count attribute read as an integer.

    Args:
        key: OpenTelemetry attribute key of the count.

    Returns:
        An INT64 expression, NULL where the span lacks the attribute.
    """
    return f"SAFE_CAST({_build_attribute_sql(key)} AS INT64)"


_FINISH_REASON_TEXT_SQL = (
    f"NULLIF({_build_attribute_sql(_ATTR_FINISH_REASONS)}, '')"
)

# The span's finish reasons. OpenTelemetry defines the attribute as a string
# array; the second read accepts an exporter that wrote the array as JSON text,
# and the third one that wrote a single reason as a plain string. An empty
# string records no reason.
_FINISH_REASONS_SQL = f"""COALESCE(
          JSON_VALUE_ARRAY(s.attributes, '$."{_ATTR_FINISH_REASONS}"'),
          JSON_VALUE_ARRAY({_FINISH_REASON_TEXT_SQL}),
          IF({_FINISH_REASON_TEXT_SQL} IS NULL, NULL, [{_FINISH_REASON_TEXT_SQL}])
        )"""


def _build_trace_measures_sql(table_ref: str) -> str:
    """Builds the risk measures of every trace in the window, one turn each.

    Reads every span, not only the agent's: execution spans such as
    ``execute_tool`` may carry neither the agent name nor the session id, so
    they are tied to a trajectory by ``trace_id`` alone.

    Args:
        table_ref: Fully-qualified spans table.

    Returns:
        SQL subquery projecting ``trace_id``, `sampling.MEASURE_COLUMNS` with
        ``turn_ms`` for ``slowest_turn_ms``, and one row per trace.
    """
    operation = _build_attribute_sql(_ATTR_OPERATION_NAME)
    input_tokens = _build_token_sql(_ATTR_INPUT_TOKENS)
    output_tokens = _build_token_sql(_ATTR_OUTPUT_TOKENS)
    # ADK fails an `execute_tool` span both when the tool raises, which also
    # records an `exception` event, and when the tool's result reports a
    # failure, which records none.
    tool_reported_error = f"""COALESCE(
          {operation} = 'execute_tool'
            AND s.status.code = 2
            AND NOT EXISTS(
              SELECT 1 FROM UNNEST(s.events) AS event
              WHERE event.name = 'exception'
            ),
          FALSE
        )"""  # noqa: S608 - trusted identifiers and fixed SQL
    return f"""
    SELECT
      spans.trace_id,
      spans.has_error,
      spans.has_abnormal_finish,
      spans.has_tool_reported_error,
      tools.busy_turn_calls,
      spans.total_tokens,
      spans.turn_ms
    FROM (
      SELECT
        s.trace_id,
        -- Only GenAI spans count: other instrumentation also sets an error
        -- status, such as an HTTP client span for a request that was retried
        -- successfully.
        LOGICAL_OR(
          {operation} IS NOT NULL
            AND s.status.code = 2
            AND NOT {tool_reported_error}
        ) AS has_error,
        LOGICAL_OR({tool_reported_error}) AS has_tool_reported_error,
        LOGICAL_OR(EXISTS(
          SELECT 1 FROM UNNEST({_FINISH_REASONS_SQL}) AS reason
          WHERE {sampling.build_abnormal_finish_sql("reason")}
        )) AS has_abnormal_finish,
        SUM(IF(
          {operation} = 'generate_content'
            AND ({input_tokens} IS NOT NULL OR {output_tokens} IS NOT NULL),
          COALESCE({input_tokens}, 0) + COALESCE({output_tokens}, 0),
          NULL
        )) AS total_tokens,
        TIMESTAMP_DIFF(MAX(s.end_time), MIN(s.start_time), MILLISECOND) AS turn_ms
      FROM `{table_ref}` AS s
      WHERE s.start_time BETWEEN @window_start AND @window_end
      GROUP BY s.trace_id
    ) AS spans
    LEFT JOIN (
      SELECT trace_id, MAX(calls) AS busy_turn_calls
      FROM (
        SELECT s.trace_id, {_build_attribute_sql(_ATTR_TOOL_NAME)} AS tool, COUNT(*) AS calls
        FROM `{table_ref}` AS s
        WHERE s.start_time BETWEEN @window_start AND @window_end
          AND {operation} = 'execute_tool'
          -- ADK's hand-off to another agent routes the turn rather than doing
          -- its work.
          AND {_build_attribute_sql(_ATTR_TOOL_NAME)} IS DISTINCT FROM 'transfer_to_agent'
        GROUP BY s.trace_id, tool
      )
      GROUP BY trace_id
    ) AS tools USING (trace_id)
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized


def _build_measures_sql(metric_type: MetricType, table_ref: str) -> str:
    """Builds the risk measures of each session or trace in the window.

    A turn is a trace. The properties, per `sampling.MEASURE_COLUMNS`:

    - ``has_error``: a GenAI span (one with ``gen_ai.operation.name``) whose
      status code is 2, an error, other than one ``has_tool_reported_error``
      counts.
    - ``has_abnormal_finish``: ``gen_ai.response.finish_reasons`` holds a value
      outside `sampling.NORMAL_FINISH_REASONS`, such as ``length``,
      ``safety`` or ``error``, which ADK writes for Gemini's ``OTHER``. The
      span's status is not an error.
    - ``has_tool_reported_error``: an ``execute_tool`` span whose status code
      is 2 without an ``exception`` event, which ADK 2.13 writes when the
      tool's result reports a failure rather than the tool raising one (a
      function tool flags a non-empty ``error``, an MCP tool ``isError``), and
      for a call to a tool the agent does not have.
    - ``busy_turn_calls``: the most ``execute_tool`` spans for one
      ``gen_ai.tool.name`` other than ``transfer_to_agent`` in one trace.
    - ``total_tokens``: ``gen_ai.usage.input_tokens`` plus
      ``gen_ai.usage.output_tokens`` over the ``generate_content`` spans.
    - ``slowest_turn_ms``: the longest trace, from its first span's start in
      the window to its last span's end.

    Args:
        metric_type: The unit measured: sessions for ``MULTI_TURN``, traces
            for ``SINGLE_TURN``.
        table_ref: Fully-qualified spans table.

    Returns:
        SQL subquery projecting the unit's id column and
        `sampling.MEASURE_COLUMNS`.
    """
    traces = _build_trace_measures_sql(table_ref)
    if metric_type is MetricType.SINGLE_TURN:
        return f"""
  SELECT
    trace_id, has_error, has_abnormal_finish, has_tool_reported_error, busy_turn_calls, total_tokens,
    turn_ms AS slowest_turn_ms
  FROM ({traces})
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
    return f"""
  SELECT
    sessions.session_id,
    LOGICAL_OR(traces.has_error) AS has_error,
    LOGICAL_OR(traces.has_abnormal_finish) AS has_abnormal_finish,
    LOGICAL_OR(traces.has_tool_reported_error) AS has_tool_reported_error,
    MAX(traces.busy_turn_calls) AS busy_turn_calls,
    SUM(traces.total_tokens) AS total_tokens,
    MAX(traces.turn_ms) AS slowest_turn_ms
  FROM (
    SELECT JSON_VALUE(attributes, '$."{ATTR_SESSION_ID}"') AS session_id, trace_id
    FROM `{table_ref}`
    WHERE start_time BETWEEN @window_start AND @window_end
      AND JSON_VALUE(attributes, '$."{ATTR_AGENT_NAME}"') = @agent_name
      AND JSON_VALUE(attributes, '$."{ATTR_SESSION_ID}"') IS NOT NULL
    GROUP BY session_id, trace_id
  ) AS sessions
  JOIN ({traces}) AS traces USING (trace_id)
  GROUP BY sessions.session_id
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized


def _build_multi_turn_sql(target_ctes: str, table_ref: str) -> str:
    """Build the session-level ingestion query over the sampled sessions.

    Maps sampled sessions to trace IDs to aggregate all child spans per trace.
    A row's ``log_windows`` group its traces by the hour they start in, so the
    logs of a session spread over days are read one active hour at a time.

    Args:
        target_ctes: Common table expressions ending in ``TargetSessions``,
            which projects ``session_id`` and ``sampling``.
        table_ref: Fully-qualified table containing Cloud Trace spans.

    Returns:
        Complete multi-turn SQL query.
    """
    return f"""
WITH {target_ctes},
TargetTraces AS (
  SELECT
    s.trace_id AS trace_id,
    t.session_id AS session_id,
    ANY_VALUE(t.{sampling.SAMPLING_COLUMN}) AS {sampling.SAMPLING_COLUMN}
  FROM `{table_ref}` AS s
  JOIN TargetSessions AS t
    ON JSON_VALUE(s.attributes, '$."{ATTR_SESSION_ID}"') = t.session_id
  WHERE {_JOIN_BACK_WINDOW}
  GROUP BY trace_id, session_id
),
Sessions AS (
  SELECT
    session_id,
    ARRAY_AGG(
      STRUCT(trace_id, trace_start, trace_end, spans)
      ORDER BY trace_start ASC
    ) AS traces,
    ARRAY_AGG(
      agent_revision IGNORE NULLS ORDER BY trace_start DESC LIMIT 1
    )[SAFE_OFFSET(0)] AS agent_revision,
    ANY_VALUE({sampling.SAMPLING_COLUMN}) AS {sampling.SAMPLING_COLUMN}
  FROM (
    SELECT
      t.session_id AS session_id,
      ANY_VALUE(t.{sampling.SAMPLING_COLUMN}) AS {sampling.SAMPLING_COLUMN},
      s.trace_id AS trace_id,
      MIN(s.start_time) AS trace_start,
      MAX(s.end_time) AS trace_end,
      {_LATEST_SPAN_REVISION} AS agent_revision,
      ARRAY_AGG({_SPAN_STRUCT} ORDER BY s.start_time ASC) AS spans
    FROM `{table_ref}` AS s
    JOIN TargetTraces AS t USING (trace_id)
    WHERE {_JOIN_BACK_WINDOW}
    GROUP BY s.trace_id, t.session_id
  )
  GROUP BY session_id
)
SELECT
  *,
  ARRAY(
    SELECT AS STRUCT
      ARRAY_AGG(trace.trace_id ORDER BY trace.trace_start) AS trace_ids,
      MIN(trace.trace_start) AS start_time,
      MAX(trace.trace_end) AS end_time
    FROM UNNEST(traces) AS trace
    GROUP BY TIMESTAMP_TRUNC(trace.trace_start, HOUR)
    ORDER BY start_time
  ) AS log_windows
FROM Sessions
"""  # noqa: S608 - may embed agent selector SQL, gated by precheck_selector's table guard and dry-run allowlist


def _build_single_turn_sql(target_ctes: str, table_ref: str) -> str:
    """Build the trace-level ingestion query over the sampled traces.

    A row's one log window is its trace's own time range.

    Args:
        target_ctes: Common table expressions ending in ``TargetTraces``,
            which projects ``trace_id`` and ``sampling``.
        table_ref: Fully-qualified table containing Cloud Trace spans.

    Returns:
        Complete single-turn SQL query.
    """
    return f"""
WITH {target_ctes}
SELECT
  s.trace_id AS trace_id,
  ANY_VALUE(t.{sampling.SAMPLING_COLUMN}) AS {sampling.SAMPLING_COLUMN},
  JSON_VALUE(ANY_VALUE(s.attributes), '$."{ATTR_SESSION_ID}"') AS session_id,
  {_LATEST_SPAN_REVISION} AS agent_revision,
  ARRAY_AGG({_SPAN_STRUCT} ORDER BY s.start_time ASC) AS spans,
  [STRUCT(
    [s.trace_id] AS trace_ids,
    MIN(s.start_time) AS start_time,
    MAX(s.end_time) AS end_time
  )] AS log_windows
FROM `{table_ref}` AS s
JOIN TargetTraces AS t USING (trace_id)
WHERE {_JOIN_BACK_WINDOW}
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
    SELECTOR_TIME_COLUMN = "start_time"
    STARTER_SELECTOR = _build_multi_turn_targets(selector.TARGET_COLUMN)
    SCHEMA_FILE = "all_spans.schema.json"
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
        trajectory_file_store: TrajectoryFileStore | None = None,
        selector_sql: str = "",
        selector_ai_model: str = "",
        job_timeout_ms: int = DEFAULT_JOB_TIMEOUT_MS,
        sampling_method: sampling.SamplingMethod = sampling.SamplingMethod.UNIFORM,
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
            trajectory_file_store: Store for files attached to sampled
                conversations (see `BigQueryJobFetcher`).
            selector_sql: Agent-authored query selecting specific sessions to
                review (see `BigQueryJobFetcher`).
            selector_ai_model: Model endpoint for selector `AI.IF` calls
                (see `BigQueryJobFetcher`).
            job_timeout_ms: Server-side execution timeout in milliseconds for
                BigQuery query jobs (see `BigQueryJobFetcher`).
            sampling_method: How trajectories are picked from the candidates
                (see `BigQueryJobFetcher`).
        """
        super().__init__(
            project_id=project_id,
            dataset=dataset,
            table=table,
            location=location,
            observed_project_id=observed_project_id,
            memory_guard=memory_guard,
            recorder=recorder,
            trajectory_file_store=trajectory_file_store,
            selector_sql=selector_sql,
            selector_ai_model=selector_ai_model,
            job_timeout_ms=job_timeout_ms,
            sampling_method=sampling_method,
        )
        self._converter = TraceAgentDataConverter(
            gcs_reader=gcs_reader or CachingGcsReader()
        )
        # Message content is logged where the spans are, in the agent's project.
        self._log_fetcher = LogFetcher(observed_project_id or project_id)

    def _resolve_candidates(self, metric_type: MetricType) -> tuple[str, str]:
        if metric_type is MetricType.MULTI_TURN:
            return self._resolve_multi_turn_targets(), "session_id"
        return self._substitute_table_ref(_SINGLE_TURN_TARGETS), "trace_id"

    def _build_ingestion_sql(self, metric_type: MetricType) -> str:
        if metric_type is MetricType.MULTI_TURN:
            return _build_multi_turn_sql(
                self._build_target_ctes(metric_type, "TargetSessions"),
                self.table_ref,
            )
        return _build_single_turn_sql(
            self._build_target_ctes(metric_type, "TargetTraces"),
            self.table_ref,
        )

    def _build_measures_sql(self, metric_type: MetricType) -> str:
        return _build_measures_sql(metric_type, self.table_ref)

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

        log_entries_by_span: dict[str, list[LogEntryView]] = {}
        for window in row.get("log_windows") or []:
            # Each trace is in one window, so no span's entries are replaced.
            log_entries_by_span.update(
                self._log_fetcher.fetch_entries_by_span(
                    window["trace_ids"],
                    start=window["start_time"],
                    end=window["end_time"],
                )
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
