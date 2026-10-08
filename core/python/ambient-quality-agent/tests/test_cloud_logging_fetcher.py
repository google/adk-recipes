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

"""Tests for CloudLoggingFetcher."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest import mock

import pytest
from agentplatform._genai.types import EvalCase
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion.base import BaseFetcher
from ambient_quality_agent.tools.ingestion.cloud_logging_fetcher import (
    _INLINE_LABEL_TO_ATTR,
    _LABEL_AGENT_NAME,
    _LABEL_SERVICE_VERSION,
    _LABEL_SESSION_ID,
    _REF_LABEL_TO_ATTR,
    CloudLoggingFetcher,
    _rows_to_spans,
)
from ambient_quality_agent.tools.trajectories.models import IngestStatus
from google.cloud import bigquery

from .conftest import FakeTrajectoryRecorder

_PROJECT = "test-project"
_DATASET = "agent_telemetry"
_TABLE = "gen_ai_client_inference_operation_details"
_TABLE_REF = f"{_PROJECT}.{_DATASET}.{_TABLE}"
_LOCATION = "us-central1"

_START = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
_END = dt.datetime(2026, 1, 8, tzinfo=dt.UTC)

# GCS content the fake reader returns, keyed by ref URI. Shapes mirror the
# real agents-cli export: one JSONL message object per line.
_GCS: dict[str, str] = {
    "gs://b/turn1_inputs.jsonl": json.dumps(
        {
            "role": "user",
            "parts": [{"content": "hi", "type": "text"}],
            "index": 0,
        }
    ),
    "gs://b/turn1_outputs.jsonl": json.dumps(
        {
            "role": "assistant",
            "parts": [{"content": "hello", "type": "text"}],
            "finish_reason": "stop",
            "index": 0,
        }
    ),
    "gs://b/turn2_inputs.jsonl": json.dumps(
        {
            "role": "user",
            "parts": [{"content": "bye", "type": "text"}],
            "index": 0,
        }
    ),
    "gs://b/turn2_outputs.jsonl": json.dumps(
        {
            "role": "assistant",
            "parts": [{"content": "goodbye", "type": "text"}],
            "finish_reason": "stop",
            "index": 0,
        }
    ),
    "gs://b/tools.jsonl": json.dumps(
        {
            "name": "create_ticket",
            "description": "d",
            "type": "function",
            "index": 0,
        }
    ),
}


# Ref URI the fake reader fails on, standing in for a deleted or unreadable
# GCS object.
_UNREADABLE_REF = "gs://b/missing_inputs.jsonl"


def _fake_gcs_reader(uri: str) -> str:
    """Returns canned GCS text for `uri`, or raises `OSError` for `_UNREADABLE_REF`.

    Args:
        uri: ``gs://`` object URI to read.

    Returns:
        JSONL payload from `_GCS` for `uri`.

    Raises:
        OSError: When `uri` equals `_UNREADABLE_REF`.
    """
    if uri == _UNREADABLE_REF:
        raise OSError(f"cannot read {uri}")
    return _GCS[uri]


# Complete set of label fields read by the fetcher, derived from the module
# under test to keep schema definitions synchronized across full-schema tests.
_REF_LABELS = tuple(_REF_LABEL_TO_ATTR)
_INLINE_LABELS = tuple(_INLINE_LABEL_TO_ATTR)
_ALL_LABELS = (
    _LABEL_SESSION_ID,
    _LABEL_AGENT_NAME,
    _LABEL_SERVICE_VERSION,
    *_REF_LABELS,
    *_INLINE_LABELS,
)

# ``labels`` sub-fields of the default agents-cli sink schema
# (``deployment/terraform/shared/genai_logs_schema.json``). Of the inline
# content labels it declares only ``gen_ai_tool_definitions``.
_AGENTS_CLI_DEFAULT_LABELS = (
    "gen_ai_conversation_id",
    "gen_ai_agent_name",
    "gen_ai_input_messages_ref",
    "gen_ai_output_messages_ref",
    "gen_ai_system_instructions_ref",
    "gen_ai_usage_input_tokens",
    "gen_ai_usage_output_tokens",
    "gen_ai_response_finish_reasons",
    "gen_ai_tool_definitions",
    "gen_ai_tool_definitions_ref",
    "gcp_vertex_agent_invocation_id",
    "gcp_vertex_agent_event_id",
    "event_name",
)


def _set_table_labels(client: mock.MagicMock, labels: tuple[str, ...]) -> None:
    """Configures mock BigQuery table metadata with specified label subfields.

    Args:
        client: Mock BigQuery client to configure.
        labels: Field names to populate under the ``labels`` STRUCT.
    """
    schema = [
        bigquery.SchemaField("timestamp", "TIMESTAMP"),
        bigquery.SchemaField("trace", "STRING"),
        bigquery.SchemaField(
            "labels",
            "RECORD",
            fields=[bigquery.SchemaField(name, "STRING") for name in labels],
        ),
    ]
    client.get_table.return_value = mock.MagicMock(schema=schema)


def _log_row(
    *,
    trace: str = "t1",
    span_id: str = "s1",
    agent_name: str = "root_agent",
    input_ref: str | None = "gs://b/turn1_inputs.jsonl",
    output_ref: str | None = "gs://b/turn1_outputs.jsonl",
    tool_ref: str | None = None,
    input_messages: str | None = None,
    output_messages: str | None = None,
    system_instructions: str | None = None,
    tool_definitions: str | None = None,
) -> dict[str, Any]:
    """Constructs a sample inference log row matching `_build_row_struct` output.

    Args:
        trace: Trace identifier.
        span_id: Span identifier.
        agent_name: Name of the agent.
        input_ref: GCS URI reference for input messages.
        output_ref: GCS URI reference for output messages.
        tool_ref: GCS URI reference for tool definitions.
        input_messages: Inline input messages JSON.
        output_messages: Inline output messages JSON.
        system_instructions: Inline system instructions JSON.
        tool_definitions: Inline tool definitions JSON.

    Returns:
        Dictionary representing a log row.
    """
    return {
        "trace": trace,
        "span_id": span_id,
        "end_time": "2026-01-01T00:00:00Z",
        "agent_name": agent_name,
        "gen_ai_input_messages_ref": input_ref,
        "gen_ai_output_messages_ref": output_ref,
        "gen_ai_system_instructions_ref": None,
        "gen_ai_tool_definitions_ref": tool_ref,
        "gen_ai_input_messages": input_messages,
        "gen_ai_output_messages": output_messages,
        "gen_ai_system_instructions": system_instructions,
        "gen_ai_tool_definitions": tool_definitions,
    }


def _make_fetcher(
    client: mock.MagicMock,
    recorder: Any | None = None,
    selector_sql: str = "",
    labels: tuple[str, ...] | None = _ALL_LABELS,
) -> CloudLoggingFetcher:
    # None preserves the caller's pre-configured table mock on the client.
    if labels is not None:
        _set_table_labels(client, labels)
    with (
        mock.patch(
            "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
            return_value=client,
        ),
        mock.patch("ambient_quality_agent.tools.ingestion.base.storage"),
    ):
        fetcher = CloudLoggingFetcher(
            project_id=_PROJECT,
            dataset=_DATASET,
            table=_TABLE,
            location=_LOCATION,
            recorder=recorder,
            selector_sql=selector_sql,
        )
    # Swap the GCS-backed converter for one using the in-memory reader.
    from ambient_quality_agent.tools.ingestion.trace_converter import (
        TraceAgentDataConverter,
    )

    fetcher._converter = TraceAgentDataConverter(gcs_reader=_fake_gcs_reader)
    return fetcher


def _row(values: dict[str, Any]) -> mock.MagicMock:
    row = mock.MagicMock()
    row.items.return_value = values.items()
    return row


def _configure_paging(
    client: mock.MagicMock,
    *,
    page_rows: list[dict[str, Any]],
    next_page_token: str | None,
) -> None:
    query_job = mock.MagicMock(spec=bigquery.QueryJob)
    query_job.destination = "dest-table-ref"
    client.get_job.return_value = query_job

    row_iter = mock.MagicMock()
    row_iter.pages = iter([[_row(v) for v in page_rows]])
    row_iter.next_page_token = next_page_token
    client.list_rows.return_value = row_iter


def test_cloud_logging_fetcher_is_a_base_fetcher() -> None:
    assert issubclass(CloudLoggingFetcher, BaseFetcher)
    assert isinstance(_make_fetcher(mock.MagicMock()), BaseFetcher)


def test_table_ref_and_client_project() -> None:
    client = mock.MagicMock()
    with (
        mock.patch(
            "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
            return_value=client,
        ) as ctor,
        mock.patch("ambient_quality_agent.tools.ingestion.base.storage"),
    ):
        fetcher = CloudLoggingFetcher(
            project_id=_PROJECT,
            dataset=_DATASET,
            table=_TABLE,
            location=_LOCATION,
        )
    assert fetcher.table_ref == _TABLE_REF
    assert ctor.call_args.kwargs["project"] == _PROJECT


@pytest.mark.parametrize(
    ("metric_type", "expected_keyword"),
    [
        (MetricType.MULTI_TURN, "TargetSessions"),
        (MetricType.SINGLE_TURN, "TargetTraces"),
    ],
)
def test_submit_query_selects_sql_by_metric_type(
    metric_type: MetricType, expected_keyword: str
) -> None:
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-123"
    fetcher = _make_fetcher(client)

    job_id = fetcher.submit_query(
        "my-agent", metric_type, _START, _END, limit=50
    )

    assert job_id == "job-123"
    sql = client.query.call_args.args[0]
    assert expected_keyword in sql
    assert _TABLE_REF in sql
    # The log sink flattens the agent name into a labels.* column.
    assert "labels.gen_ai_agent_name" in sql


def test_submit_query_binds_parameters() -> None:
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client)

    fetcher.submit_query(
        "agent-a", MetricType.MULTI_TURN, _START, _END, limit=7
    )

    job_config = client.query.call_args.kwargs["job_config"]
    params = {p.name: p.value for p in job_config.query_parameters}
    assert params == {
        "agent_name": "agent-a",
        "window_start": _START,
        "window_end": _END,
        "limit": 7,
    }


def test_fetch_page_maps_multi_turn_session_row() -> None:
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "session_id": "sess-1",
                "traces": [
                    {
                        "trace_id": "t1",
                        "inference_rows": [_log_row(trace="t1", span_id="s1")],
                    },
                    {
                        "trace_id": "t2",
                        "inference_rows": [
                            _log_row(
                                trace="t2",
                                span_id="s2",
                                input_ref="gs://b/turn2_inputs.jsonl",
                                output_ref="gs://b/turn2_outputs.jsonl",
                            )
                        ],
                    },
                ],
            }
        ],
        next_page_token="tok-2",
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.MULTI_TURN)

    assert len(page.dataset.eval_cases) == 1
    case = page.dataset.eval_cases[0]
    assert case.eval_case_id == "sess-1"
    # Two traces -> two turns.
    assert [t.turn_index for t in case.agent_data.turns] == [0, 1]
    assert case.agent_data.turns[0].events[0].content.parts[0].text == "hi"
    assert case.agent_data.agents["root_agent"] is not None
    assert page.next_page_token == "tok-2"


def _build_inline_messages(*messages: tuple[str, str]) -> str:
    """Serializes OTel GenAI text messages as an inline label value.

    Args:
        *messages: Ordered ``(role, text)`` pairs.

    Returns:
        JSON-serialized message array.
    """
    return json.dumps(
        [
            {"role": role, "parts": [{"type": "text", "content": text}]}
            for role, text in messages
        ]
    )


def _build_instructed_log_row(
    trace: str,
    span_id: str,
    instruction: str,
    inputs: tuple[tuple[str, str], ...],
    reply: str,
) -> dict[str, Any]:
    """Builds an inference log row with inline content and instructions.

    Args:
        trace: Trace identifier.
        span_id: Span identifier.
        instruction: ``gen_ai_system_instructions`` text.
        inputs: Input messages as ``(role, text)`` pairs, history first.
        reply: Assistant text reply.

    Returns:
        Dictionary representing a log row.
    """
    return _log_row(
        trace=trace,
        span_id=span_id,
        input_ref=None,
        output_ref=None,
        input_messages=_build_inline_messages(*inputs),
        output_messages=_build_inline_messages(("assistant", reply)),
        system_instructions=instruction,
    )


def test_fetch_page_shows_a_changed_instruction_once_per_turn() -> None:
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "session_id": "sess-1",
                "traces": [
                    {
                        "trace_id": "t1",
                        "inference_rows": [
                            _build_instructed_log_row(
                                "t1",
                                "s1",
                                "Be brief.",
                                (("user", "hi"),),
                                "hello",
                            )
                        ],
                    },
                    {
                        "trace_id": "t2",
                        "inference_rows": [
                            _build_instructed_log_row(
                                "t2",
                                "s2",
                                "Be verbose.",
                                (("user", "slots?"),),
                                "Monday.",
                            ),
                            _build_instructed_log_row(
                                "t2",
                                "s3",
                                "Be verbose.",
                                (
                                    ("user", "slots?"),
                                    ("assistant", "Monday."),
                                    ("user", "later?"),
                                ),
                                "Friday.",
                            ),
                        ],
                    },
                ],
            }
        ],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.MULTI_TURN)

    agent_data = page.dataset.eval_cases[0].agent_data
    assert agent_data.agents["root_agent"].instruction == "Be brief."
    second_turn = agent_data.turns[1].events
    assert [event.author for event in second_turn].count("root_agent") == 2
    shown = [
        (event.author, event.content.parts[0].text, event.state_delta)
        for turn in agent_data.turns
        for event in turn.events
        if event.state_delta
    ]
    assert shown == [
        ("root_agent", "Monday.", {"system_instruction": "Be verbose."})
    ]


def test_fetch_page_maps_single_turn_trace_row() -> None:
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[{"trace_id": "trace-9", "inference_rows": [_log_row()]}],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    case = page.dataset.eval_cases[0]
    assert case.eval_case_id == "trace-9"
    assert len(case.agent_data.turns) == 1
    assert page.is_exhausted is True


def test_fetch_page_drops_case_with_no_turns() -> None:
    """A row whose refs resolve to no content is dropped."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {"trace_id": "good", "inference_rows": [_log_row(trace="good")]},
            {
                "trace_id": "empty",
                "inference_rows": [
                    _log_row(trace="empty", input_ref=None, output_ref=None)
                ],
            },
        ],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    assert [c.eval_case_id for c in page.dataset.eval_cases] == ["good"]


def test_rows_to_spans_maps_refs_to_converter_attribute_keys() -> None:
    spans = _rows_to_spans([_log_row(tool_ref="gs://b/tools.jsonl")])

    assert len(spans) == 1
    attrs = spans[0].attributes
    assert spans[0].span_id == "s1"
    assert attrs["gen_ai.agent.name"] == "root_agent"
    assert attrs["gen_ai.input.messages_ref"] == "gs://b/turn1_inputs.jsonl"
    assert attrs["gen_ai.output.messages_ref"] == "gs://b/turn1_outputs.jsonl"
    # The dotted tool key is required: only its _ref variant is resolved by
    # the converter (the flat gen_ai.tool_definitions alias is not).
    assert attrs["gen_ai.tool.definitions_ref"] == "gs://b/tools.jsonl"
    assert "gen_ai.system_instructions_ref" not in attrs


def test_rows_to_spans_omits_absent_refs() -> None:
    spans = _rows_to_spans([_log_row(input_ref=None, tool_ref=None)])

    attrs = spans[0].attributes
    assert "gen_ai.input.messages_ref" not in attrs
    assert "gen_ai.tool.definitions_ref" not in attrs
    assert attrs["gen_ai.output.messages_ref"] == "gs://b/turn1_outputs.jsonl"


def test_tool_definitions_resolve_via_ref() -> None:
    """A tool_definitions_ref populates the agent's tools (dotted-key path)."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "trace_id": "t",
                "inference_rows": [_log_row(tool_ref="gs://b/tools.jsonl")],
            }
        ],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    agent = page.dataset.eval_cases[0].agent_data.agents["root_agent"]
    decls = agent.tools[0].function_declarations
    assert decls[0].name == "create_ticket"


def test_caching_reader_caches_tool_and_system_refs_but_not_messages() -> None:
    from ambient_quality_agent.tools.ingestion.base import CachingGcsReader

    calls: list[str] = []

    def underlying(uri: str) -> str:
        calls.append(uri)
        return f"content-of-{uri}"

    reader = CachingGcsReader(reader=underlying)
    tool_ref = "gs://b/completions/HASH_tool.definitions.jsonl"
    sys_ref = "gs://b/completions/HASH_system_instruction.jsonl"
    in_ref = "gs://b/completions/UUID_inputs.jsonl"

    # Cacheable refs: fetched from source once, then served from cache.
    assert reader(tool_ref) == f"content-of-{tool_ref}"
    assert reader(tool_ref) == f"content-of-{tool_ref}"
    assert reader(sys_ref) == f"content-of-{sys_ref}"
    assert reader(sys_ref) == f"content-of-{sys_ref}"
    # Message refs are unique per call and must always pass through.
    assert reader(in_ref) == f"content-of-{in_ref}"
    assert reader(in_ref) == f"content-of-{in_ref}"

    assert calls == [tool_ref, sys_ref, in_ref, in_ref]


def test_gcs_client_reader_uses_injected_client_for_every_read() -> None:
    """An injected client is reused for all reads (no new client created)."""
    from ambient_quality_agent.tools.ingestion import base

    client = mock.MagicMock()
    client.bucket.return_value.blob.return_value.download_as_text.return_value = ""

    with mock.patch.object(base.storage, "Client") as client_cls:
        reader = base.GcsClientReader(client=client)
        reader("gs://b/o")
        reader("gs://b/o2")

    # The injected client was used; no default client was constructed.
    assert client.bucket.call_count == 2
    client_cls.assert_not_called()


def test_gcs_client_reader_defaults_to_a_new_client() -> None:
    """Without injection, one default storage client is created."""
    from ambient_quality_agent.tools.ingestion import base

    with mock.patch.object(base.storage, "Client") as client_cls:
        base.GcsClientReader()
        assert client_cls.call_count == 1


# --- counting -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("metric_type", "unit_column"),
    [
        (MetricType.MULTI_TURN, "session_id"),
        (MetricType.SINGLE_TURN, "trace_id"),
    ],
)
def test_count_scanned_is_the_sampling_query_without_the_sampling(
    metric_type: MetricType, unit_column: str
) -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"scanned": 3}]
    fetcher = _make_fetcher(client)

    scanned = fetcher.count_scanned("agent-a", metric_type, _START, _END)

    sql = client.query.call_args.args[0]
    assert "COUNT(*) AS scanned" in sql
    assert f"GROUP BY {unit_column}" in sql
    assert "labels.gen_ai_agent_name = @agent_name" in sql
    assert "ORDER BY RAND()" not in sql and "LIMIT" not in sql
    assert scanned == 3 and fetcher.counts.scanned == 3


# --- deployment revision ------------------------------------------------------


def _sql_words(sql: str) -> str:
    """Collapses statement line breaks and whitespace.

    Args:
        sql: SQL statement string.

    Returns:
        Single-line SQL string with normalized spaces.
    """
    return " ".join(sql.split())


@pytest.mark.parametrize(
    "metric_type", [MetricType.MULTI_TURN, MetricType.SINGLE_TURN]
)
def test_submit_query_projects_the_deployment_revision(
    metric_type: MetricType,
) -> None:
    """Both scopes read the sink's flattened ``service.version`` label. Naming
    a column the sink does not have fails the query at runtime, so the exact
    name is pinned here."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client)

    fetcher.submit_query("agent-a", metric_type, _START, _END, limit=5)

    sql = _sql_words(client.query.call_args.args[0])
    assert "s.labels.service_version" in sql
    assert "AS agent_revision" in sql


@pytest.mark.parametrize(
    "metric_type", [MetricType.MULTI_TURN, MetricType.SINGLE_TURN]
)
def test_row_revision_is_taken_from_the_last_row(
    metric_type: MetricType,
) -> None:
    """A redeploy mid-trace leaves two revisions on one trace's rows; the
    latest one is the configuration the trace finished under."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client)

    fetcher.submit_query("agent-a", metric_type, _START, _END, limit=5)

    sql = _sql_words(client.query.call_args.args[0])
    assert (
        "ARRAY_AGG(s.labels.service_version IGNORE NULLS ORDER BY s.timestamp "
        "DESC LIMIT 1)[SAFE_OFFSET(0)] AS agent_revision" in sql
    )


def test_multi_turn_query_takes_the_session_s_latest_revision() -> None:
    """A session spanning a redeploy is attributed to its last trace's revision.

    BigQuery performs this aggregation, so the guarantee lives in the SQL; the
    fake row iterator cannot reproduce it.
    """
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client)

    fetcher.submit_query(
        "agent-a", MetricType.MULTI_TURN, _START, _END, limit=5
    )

    sql = _sql_words(client.query.call_args.args[0])
    assert (
        "ARRAY_AGG( agent_revision IGNORE NULLS ORDER BY trace_start DESC LIMIT 1 "
        ")[SAFE_OFFSET(0)] AS agent_revision" in sql
    )


@pytest.mark.parametrize(
    ("metric_type", "unit_column"),
    [
        (MetricType.MULTI_TURN, "session_id"),
        (MetricType.SINGLE_TURN, "trace_id"),
    ],
)
def test_counting_query_ignores_the_revision(
    metric_type: MetricType, unit_column: str
) -> None:
    """The revision is a projection, not a filter: it must not narrow the
    population the sampling query is counted against."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"scanned": 3}]
    fetcher = _make_fetcher(client)

    fetcher.count_scanned("agent-a", metric_type, _START, _END)

    sql = _sql_words(client.query.call_args.args[0])
    assert "service_version" not in sql
    assert f"GROUP BY {unit_column}" in sql


@pytest.mark.parametrize(
    ("metric_type", "page_row", "expected"),
    [
        (
            MetricType.MULTI_TURN,
            {
                "session_id": "sess-1",
                "agent_revision": "11",
                "traces": [{"trace_id": "t1", "inference_rows": [_log_row()]}],
            },
            {"sess-1": "11"},
        ),
        (
            MetricType.SINGLE_TURN,
            {
                "trace_id": "trace-9",
                "agent_revision": "11",
                "inference_rows": [_log_row()],
            },
            {"trace-9": "11"},
        ),
    ],
)
def test_fetch_page_maps_the_revision_by_eval_case_id(
    metric_type: MetricType, page_row: dict[str, Any], expected: dict[str, str]
) -> None:
    client = mock.MagicMock()
    _configure_paging(client, page_rows=[page_row], next_page_token=None)
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", metric_type)

    assert page.revisions == expected


def test_fetch_page_leaves_a_row_without_a_revision_unattributed() -> None:
    """Rows carrying no ``service_version`` map to the single unnamed revision,
    not an unknown one."""
    row = {
        "trace_id": "trace-9",
        "agent_revision": None,
        "inference_rows": [_log_row()],
    }
    client = mock.MagicMock()
    _configure_paging(client, page_rows=[row], next_page_token=None)
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    assert [c.eval_case_id for c in page.dataset.eval_cases] == ["trace-9"]
    assert page.revisions == {}
    assert fetcher._map_row(row, MetricType.SINGLE_TURN).agent_revision == ""


# --- what ingestion reports to the recorder -----------------------------------


def test_a_multi_turn_page_reports_its_traces_in_conversation_order() -> None:
    """The query aggregates a session's traces by start time, so the first id
    is its opening turn."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "session_id": "sess-1",
                "traces": [
                    {
                        "trace_id": "first",
                        "inference_rows": [_log_row(trace="first")],
                    },
                    {
                        "trace_id": "second",
                        "inference_rows": [_log_row(trace="second")],
                    },
                ],
            }
        ],
        next_page_token=None,
    )
    recorder = FakeTrajectoryRecorder()

    _make_fetcher(client, recorder=recorder).fetch_page(
        "job-1", MetricType.MULTI_TURN
    )

    assert [t.trajectory_id for t in recorder.records] == ["sess-1"]
    assert recorder.records[0].trace_ids == ("first", "second")
    assert recorder.records[0].session_id == "sess-1"
    # Both traces carry the same spans, so session-wide dedup leaves the second
    # without a turn -- partial, a state a `dropped` bool could not express.
    assert recorder.records[0].ingest_status is IngestStatus.PARTIAL


def test_a_single_turn_page_reports_the_session_the_turn_belongs_to() -> None:
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "trace_id": "trace-9",
                "session_id": "sess-1",
                "inference_rows": [_log_row()],
            }
        ],
        next_page_token=None,
    )
    recorder = FakeTrajectoryRecorder()

    _make_fetcher(client, recorder=recorder).fetch_page(
        "job-1", MetricType.SINGLE_TURN
    )

    assert recorder.records[0].trajectory_id == "trace-9"
    assert recorder.records[0].trace_ids == ("trace-9",)
    assert recorder.records[0].session_id == "sess-1"


def test_a_dropped_row_is_reported_beside_the_mapped_ones() -> None:
    """The page yields one case and two trajectories: the run sampled both, and
    only the account that includes the second explains the size of the sample.
    """
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {"trace_id": "good", "inference_rows": [_log_row(trace="good")]},
            {
                "trace_id": "empty",
                "inference_rows": [
                    _log_row(trace="empty", input_ref=None, output_ref=None)
                ],
            },
        ],
        next_page_token=None,
    )
    recorder = FakeTrajectoryRecorder()

    page = _make_fetcher(client, recorder=recorder).fetch_page(
        "job-1", MetricType.SINGLE_TURN
    )

    assert [c.eval_case_id for c in page.dataset.eval_cases] == ["good"]
    assert [(t.trajectory_id, t.ingest_status) for t in recorder.records] == [
        ("good", IngestStatus.INGESTED),
        ("empty", IngestStatus.NOT_INGESTED),
    ]


# --- the selector: which sessions the sweep reviews ------------------------ #

# SQL selector containing braces (regex quantifiers, JSON literals, struct
# literals) to verify format-safe template handling.
_BRACE_SELECTOR = """
SELECT labels.gen_ai_conversation_id AS target_id
FROM `test-project.agent_telemetry.gen_ai_client_inference_operation_details`
WHERE REGEXP_CONTAINS(labels.gen_ai_agent_name, r'\\d{3}')
  AND TO_JSON_STRING(labels) = '{"tool":"refund"}'
  AND STRUCT(1 AS a) IS NOT NULL
"""

# Expected targets CTE SQL fragment for window-scoped queries.
_WINDOW_TARGETS = f"""
  SELECT labels.gen_ai_conversation_id AS session_id
  FROM `{_TABLE_REF}`
  WHERE timestamp BETWEEN @window_start AND @window_end
    AND labels.gen_ai_agent_name = @agent_name
    AND labels.gen_ai_conversation_id IS NOT NULL
  GROUP BY session_id
"""  # noqa: S608 - trusted test constants


def _targets_of(sql: str) -> str:
    """Extract the TargetSessions CTE body from a full query string.

    Args:
        sql: Full SQL query text.

    Returns:
        Extracted targets CTE SQL fragment.
    """
    return sql.split("WITH TargetSessions AS (\n", 1)[1].split(
        "\n  ORDER BY RAND()", 1
    )[0]


def _below_the_cte(sql: str) -> str:
    """Extract query clauses below the TargetSessions CTE definition.

    Args:
        sql: Full SQL query text.

    Returns:
        SQL fragment following the target sampling clause.
    """
    return sql.split("\n  ORDER BY RAND()", 1)[1]


def test_a_run_without_a_selector_queries_exactly_what_it_queried_before() -> (
    None
):
    """Unscoped multi-turn queries default to sampling the entire time window."""
    fetcher = _make_fetcher(mock.MagicMock())

    assert (
        _targets_of(fetcher._build_ingestion_sql(MetricType.MULTI_TURN))
        == _WINDOW_TARGETS
    )
    assert (
        fetcher._build_count_sql(MetricType.MULTI_TURN)
        == f"SELECT COUNT(*) AS scanned FROM ({_WINDOW_TARGETS})"  # noqa: S608 - trusted test constants
    )


def test_a_selector_replaces_the_targets_and_nothing_below_them() -> None:
    """Custom selectors override target CTE definitions while preserving outer joins."""
    fetcher = _make_fetcher(
        mock.MagicMock(), selector_sql="SELECT s AS target_id"
    )
    default = _make_fetcher(mock.MagicMock())

    sql = fetcher._build_ingestion_sql(MetricType.MULTI_TURN)

    assert _targets_of(sql) == (
        "SELECT DISTINCT target_id AS session_id FROM (\nSELECT s AS target_id\n)"
    )
    assert _below_the_cte(sql) == _below_the_cte(
        default._build_ingestion_sql(MetricType.MULTI_TURN)
    )


def test_the_selected_sessions_are_still_sampled_to_the_budget() -> None:
    """Targeted sessions are sampled with random ordering up to the query limit."""
    fetcher = _make_fetcher(
        mock.MagicMock(), selector_sql="SELECT s AS target_id"
    )

    assert "ORDER BY RAND()\n  LIMIT @limit" in fetcher._build_ingestion_sql(
        MetricType.MULTI_TURN
    )


def test_the_count_and_the_sample_ask_about_the_selected_population() -> None:
    """Count and sampling queries evaluate the identical targets CTE definition."""
    fetcher = _make_fetcher(
        mock.MagicMock(), selector_sql="SELECT s AS target_id"
    )

    targets = _targets_of(fetcher._build_ingestion_sql(MetricType.MULTI_TURN))

    assert fetcher._build_count_sql(MetricType.MULTI_TURN) == (
        f"SELECT COUNT(*) AS scanned FROM ({targets})"  # noqa: S608 - trusted test constants
    )


def test_a_selector_holding_braces_reaches_bigquery_intact() -> None:
    """SQL containing braces is passed to BigQuery without formatting errors."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"scanned": 1}]
    fetcher = _make_fetcher(client, selector_sql=_BRACE_SELECTOR)

    fetcher.submit_query(
        "agent-a", MetricType.MULTI_TURN, _START, _END, limit=5
    )
    sampled = client.query.call_args.args[0]
    fetcher.count_scanned("agent-a", MetricType.MULTI_TURN, _START, _END)
    counted = client.query.call_args.args[0]

    assert _BRACE_SELECTOR in sampled
    assert _BRACE_SELECTOR in counted
    assert _TABLE_REF in sampled


def test_the_single_turn_query_has_no_selector_path() -> None:
    """Single-turn queries ignore custom selectors and target individual traces."""
    fetcher = _make_fetcher(
        mock.MagicMock(), selector_sql="SELECT s AS target_id"
    )
    default = _make_fetcher(mock.MagicMock())

    assert fetcher._build_ingestion_sql(
        MetricType.SINGLE_TURN
    ) == default._build_ingestion_sql(MetricType.SINGLE_TURN)
    assert fetcher._build_count_sql(
        MetricType.SINGLE_TURN
    ) == default._build_count_sql(MetricType.SINGLE_TURN)


# --- Tests for missing table labels -------------------------------------------


def _without(*names: str) -> tuple[str, ...]:
    """Returns the full label set excluding the specified names.

    Args:
        *names: Label field names to exclude.

    Returns:
        Tuple of label field names with excluded items removed.
    """
    return tuple(label for label in _ALL_LABELS if label not in names)


@pytest.mark.parametrize(
    "metric_type", [MetricType.MULTI_TURN, MetricType.SINGLE_TURN]
)
def test_a_table_without_service_version_reads_the_revision_as_null(
    metric_type: MetricType,
) -> None:
    """Verifies that missing ``service_version`` evaluates revision as NULL without query failure."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client, labels=_without("service_version"))

    fetcher.submit_query("agent-a", metric_type, _START, _END, limit=5)

    sql = _sql_words(client.query.call_args.args[0])
    assert "service_version" not in sql
    assert (
        "ARRAY_AGG(CAST(NULL AS STRING) IGNORE NULLS ORDER BY s.timestamp "
        "DESC LIMIT 1)[SAFE_OFFSET(0)] AS agent_revision" in sql
    )


def test_a_table_without_service_version_ingests_cases_with_an_empty_revision() -> (
    None
):
    """The revision NULL the guarded SQL projects comes back as a ``None`` row
    value, which reads as the deployment's single unnamed revision."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client, labels=_without(_LABEL_SERVICE_VERSION))

    fetcher.submit_query(
        "agent-a", MetricType.SINGLE_TURN, _START, _END, limit=5
    )

    sql = _sql_words(client.query.call_args.args[0])
    assert "ARRAY_AGG(CAST(NULL AS STRING) IGNORE NULLS" in sql

    _configure_paging(
        client,
        page_rows=[
            {
                "trace_id": "trace-9",
                "agent_revision": None,
                "inference_rows": [_log_row()],
            }
        ],
        next_page_token=None,
    )
    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    assert [c.eval_case_id for c in page.dataset.eval_cases] == ["trace-9"]
    assert page.revisions == {}


@pytest.mark.parametrize(
    "metric_type", [MetricType.MULTI_TURN, MetricType.SINGLE_TURN]
)
def test_counting_a_table_without_the_agent_name_label_filters_on_null(
    metric_type: MetricType,
) -> None:
    """Both count subqueries filter on the agent name, so a missing label has
    to compile to a typed NULL for the query to run at all."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"scanned": 2}]
    fetcher = _make_fetcher(client, labels=_without(_LABEL_AGENT_NAME))

    assert fetcher.count_scanned("agent-a", metric_type, _START, _END) == 2

    sql = _sql_words(client.query.call_args.args[0])
    assert f"labels.{_LABEL_AGENT_NAME}" not in sql
    assert "CAST(NULL AS STRING) = @agent_name" in sql


@pytest.mark.parametrize(
    ("metric_type", "unit"),
    [
        (MetricType.MULTI_TURN, f"labels.{_LABEL_SESSION_ID}"),
        (MetricType.SINGLE_TURN, "trace"),
    ],
)
def test_count_by_agent_counts_the_same_unit_for_every_agent(
    metric_type: MetricType, unit: str
) -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [
        {"agent_name": "it_support_agent", "scanned": 5}
    ]
    fetcher = _make_fetcher(client)

    counts = fetcher.count_by_agent(metric_type, _START, _END)

    sql = _sql_words(client.query.call_args.args[0])
    assert (
        f"labels.{_LABEL_AGENT_NAME} AS agent_name, COUNT(DISTINCT {unit}) AS scanned"
        in sql
    )
    assert "GROUP BY agent_name" in sql
    assert "@agent_name" not in sql
    params = {
        p.name
        for p in client.query.call_args.kwargs["job_config"].query_parameters
    }
    assert params == {"window_start", "window_end"}
    assert counts == {"it_support_agent": 5}


def test_count_by_agent_on_a_table_without_the_agent_name_label_still_runs() -> (
    None
):
    """Every name reads as a typed NULL there, so the query compiles and
    returns nothing rather than failing."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []
    fetcher = _make_fetcher(client, labels=_without(_LABEL_AGENT_NAME))

    assert fetcher.count_by_agent(MetricType.MULTI_TURN, _START, _END) == {}

    sql = _sql_words(client.query.call_args.args[0])
    assert "CAST(NULL AS STRING) AS agent_name" in sql
    assert "CAST(NULL AS STRING) IS NOT NULL" in sql


@pytest.mark.parametrize(
    "metric_type", [MetricType.MULTI_TURN, MetricType.SINGLE_TURN]
)
def test_a_table_without_ref_labels_projects_nulls_under_the_same_aliases(
    metric_type: MetricType,
) -> None:
    """Verifies that missing reference labels project NULL while retaining expected aliases."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client, labels=_without(*_REF_LABELS))

    fetcher.submit_query("agent-a", metric_type, _START, _END, limit=5)

    sql = _sql_words(client.query.call_args.args[0])
    for label in _REF_LABELS:
        assert f"s.labels.{label}" not in sql
        assert f"CAST(NULL AS STRING) AS {label}" in sql


def test_a_table_without_the_agent_name_label_matches_no_rows() -> None:
    """Verifies that filtering on a missing agent name label evaluates safely to NULL and yields no rows."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client, labels=_without("gen_ai_agent_name"))

    fetcher.submit_query(
        "agent-a", MetricType.SINGLE_TURN, _START, _END, limit=5
    )

    sql = _sql_words(client.query.call_args.args[0])
    assert "labels.gen_ai_agent_name" not in sql
    assert "CAST(NULL AS STRING) = @agent_name" in sql


def test_a_table_without_a_labels_column_reads_every_label_as_null(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verifies that a table without a labels column logs a warning and evaluates all labels as NULL."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    client.get_table.return_value = mock.MagicMock(
        schema=[bigquery.SchemaField("trace", "STRING")]
    )
    fetcher = _make_fetcher(client, labels=None)

    with caplog.at_level("WARNING"):
        fetcher.submit_query(
            "agent-a", MetricType.MULTI_TURN, _START, _END, limit=5
        )

    sql = _sql_words(client.query.call_args.args[0])
    assert "labels." not in sql
    assert "every label reads as NULL" in caplog.text


@pytest.mark.parametrize(
    "metric_type", [MetricType.MULTI_TURN, MetricType.SINGLE_TURN]
)
def test_a_table_with_every_label_keeps_typed_dotted_access(
    metric_type: MetricType,
) -> None:
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client)

    fetcher.submit_query("agent-a", metric_type, _START, _END, limit=5)

    sql = _sql_words(client.query.call_args.args[0])
    assert "CAST(NULL AS STRING)" not in sql
    assert "s.labels.gen_ai_conversation_id" in sql
    assert "s.labels.service_version" in sql
    for label in (*_REF_LABELS, *_INLINE_LABELS):
        assert f"s.labels.{label} AS {label}" in sql


def test_the_table_schema_is_read_once_per_fetcher() -> None:
    """Verifies that table metadata is fetched once and reused across subsequent queries."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    client.query.return_value.result.return_value = [{"scanned": 1}]
    fetcher = _make_fetcher(client)

    fetcher.submit_query(
        "agent-a", MetricType.MULTI_TURN, _START, _END, limit=5
    )
    fetcher.submit_query(
        "agent-a", MetricType.SINGLE_TURN, _START, _END, limit=5
    )
    fetcher.count_scanned("agent-a", MetricType.SINGLE_TURN, _START, _END)

    assert client.get_table.call_count == 1


# --- get_payload_ref ---------------------------------------------------------- #


def test_get_payload_ref_reads_one_ref_among_the_agents_rows() -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"ref": "gs://b/in.jsonl"}]
    fetcher = _make_fetcher(client)

    ref = fetcher.get_payload_ref("agent-a", _START, _END)

    assert ref == "gs://b/in.jsonl"
    sql = client.query.call_args.args[0]
    assert f"FROM `{_TABLE_REF}`" in sql
    assert "labels.gen_ai_agent_name = @agent_name" in sql
    assert "labels.gen_ai_input_messages_ref" in sql
    params = {
        p.name: p.value
        for p in client.query.call_args.kwargs["job_config"].query_parameters
    }
    assert params == {
        "agent_name": "agent-a",
        "window_start": _START,
        "window_end": _END,
    }


def test_get_payload_ref_reads_only_the_ref_columns_the_table_has() -> None:
    """The sink creates a label column only once an entry writes it; naming an
    absent one is a 400, not a NULL."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []
    fetcher = _make_fetcher(
        client, labels=(_LABEL_AGENT_NAME, "gen_ai_output_messages_ref")
    )

    assert fetcher.get_payload_ref("agent-a", _START, _END) is None
    sql = client.query.call_args.args[0]
    assert "labels.gen_ai_output_messages_ref" in sql
    assert "gen_ai_input_messages_ref" not in sql


def test_get_payload_ref_without_ref_columns_runs_no_query() -> None:
    client = mock.MagicMock()
    fetcher = _make_fetcher(client, labels=(_LABEL_AGENT_NAME,))

    assert fetcher.get_payload_ref("agent-a", _START, _END) is None
    client.query.assert_not_called()


# --- inline content labels ----------------------------------------------------

# Inline label values as OTel instrumentation writes them when content is
# captured without the GCS upload hook: a JSON list per attribute.
_INLINE_INPUTS = json.dumps(
    [{"role": "user", "parts": [{"type": "text", "content": "inline hi"}]}]
)
_INLINE_OUTPUTS = json.dumps(
    [
        {
            "role": "assistant",
            "parts": [{"type": "text", "content": "inline hello"}],
            "finish_reason": "stop",
        }
    ]
)
_INLINE_SYSTEM = json.dumps([{"type": "text", "content": "be brief"}])
_INLINE_TOOLS = json.dumps(
    [{"name": "create_ticket", "description": "d", "type": "function"}]
)


def _list_event_texts(case: EvalCase) -> list[str]:
    """Collects the text of every event part of a case's first turn.

    Args:
        case: Mapped `EvalCase`.

    Returns:
        Event part texts in order.
    """
    return [
        part.text
        for event in case.agent_data.turns[0].events
        for part in event.content.parts
        if part.text
    ]


@pytest.mark.parametrize(
    "metric_type", [MetricType.MULTI_TURN, MetricType.SINGLE_TURN]
)
def test_a_table_without_inline_labels_projects_nulls_under_the_same_aliases(
    metric_type: MetricType,
) -> None:
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client, labels=_without(*_INLINE_LABELS))

    fetcher.submit_query("agent-a", metric_type, _START, _END, limit=5)

    sql = _sql_words(client.query.call_args.args[0])
    for label in _INLINE_LABELS:
        assert f"s.labels.{label} " not in sql
        assert f"CAST(NULL AS STRING) AS {label}" in sql
    for label in _REF_LABELS:
        assert f"s.labels.{label} AS {label}" in sql


def test_rows_to_spans_maps_inline_labels_to_converter_attribute_keys() -> None:
    spans = _rows_to_spans(
        [
            _log_row(
                input_ref=None,
                output_ref=None,
                input_messages=_INLINE_INPUTS,
                output_messages=_INLINE_OUTPUTS,
                system_instructions=_INLINE_SYSTEM,
                tool_definitions=_INLINE_TOOLS,
            )
        ]
    )

    attrs = spans[0].attributes
    assert attrs["gen_ai.input.messages"] == _INLINE_INPUTS
    assert attrs["gen_ai.output.messages"] == _INLINE_OUTPUTS
    assert attrs["gen_ai.system_instructions"] == _INLINE_SYSTEM
    assert attrs["gen_ai.tool.definitions"] == _INLINE_TOOLS
    assert not any(key.endswith("_ref") for key in attrs)


def test_rows_to_spans_omits_empty_inline_labels() -> None:
    spans = _rows_to_spans(
        [_log_row(input_messages="", output_messages=_INLINE_OUTPUTS)]
    )

    attrs = spans[0].attributes
    assert "gen_ai.input.messages" not in attrs
    assert "gen_ai.system_instructions" not in attrs
    assert "gen_ai.tool.definitions" not in attrs
    assert attrs["gen_ai.output.messages"] == _INLINE_OUTPUTS


def test_fetch_page_reads_inline_content_when_a_row_has_no_refs() -> None:
    client = mock.MagicMock()
    row = {
        "trace_id": "t",
        "inference_rows": [
            _log_row(
                input_ref=None,
                output_ref=None,
                input_messages=_INLINE_INPUTS,
                output_messages=_INLINE_OUTPUTS,
            )
        ],
    }
    _configure_paging(client, page_rows=[row], next_page_token=None)
    recorder = FakeTrajectoryRecorder()

    page = _make_fetcher(client, recorder=recorder).fetch_page(
        "job-1", MetricType.SINGLE_TURN
    )

    assert _list_event_texts(page.dataset.eval_cases[0]) == [
        "inline hi",
        "inline hello",
    ]
    assert recorder.records[0].ingest_status is IngestStatus.INGESTED


def test_fetch_page_prefers_a_readable_ref_over_inline_content() -> None:
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "trace_id": "t",
                "inference_rows": [
                    _log_row(
                        input_messages=_INLINE_INPUTS,
                        output_messages=_INLINE_OUTPUTS,
                    )
                ],
            }
        ],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    assert _list_event_texts(page.dataset.eval_cases[0]) == ["hi", "hello"]


def test_an_unreadable_ref_falls_back_to_inline_content_and_is_partial() -> (
    None
):
    """The inline fallback keeps the case, but the offloaded payload was lost,
    so the case is still reported as partially ingested."""
    client = mock.MagicMock()
    row = {
        "trace_id": "t",
        "inference_rows": [
            _log_row(
                input_ref=_UNREADABLE_REF,
                input_messages=_INLINE_INPUTS,
            )
        ],
    }
    _configure_paging(client, page_rows=[row], next_page_token=None)
    recorder = FakeTrajectoryRecorder()
    fetcher = _make_fetcher(client, recorder=recorder)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    assert _list_event_texts(page.dataset.eval_cases[0]) == [
        "inline hi",
        "hello",
    ]
    assert recorder.records[0].ingest_status is IngestStatus.PARTIAL


@pytest.mark.parametrize(
    "metric_type", [MetricType.MULTI_TURN, MetricType.SINGLE_TURN]
)
def test_the_agents_cli_default_schema_reads_inline_tool_definitions(
    metric_type: MetricType,
) -> None:
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client, labels=_AGENTS_CLI_DEFAULT_LABELS)

    fetcher.submit_query("agent-a", metric_type, _START, _END, limit=5)

    sql = _sql_words(client.query.call_args.args[0])
    assert "s.labels.gen_ai_tool_definitions AS gen_ai_tool_definitions" in sql
    for label in (
        "gen_ai_input_messages",
        "gen_ai_output_messages",
        "gen_ai_system_instructions",
    ):
        assert f"CAST(NULL AS STRING) AS {label}" in sql


def test_fetch_page_populates_tools_from_inline_tool_definitions() -> None:
    """The agents-cli default schema declares only this inline content label,
    beside the message refs."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "trace_id": "t",
                "inference_rows": [_log_row(tool_definitions=_INLINE_TOOLS)],
            }
        ],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    agent = page.dataset.eval_cases[0].agent_data.agents["root_agent"]
    decls = agent.tools[0].function_declarations
    assert [decl.name for decl in decls] == ["create_ticket"]


def test_fetch_page_populates_the_instruction_from_inline_system_instructions() -> (
    None
):
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "trace_id": "t",
                "inference_rows": [
                    _log_row(system_instructions=_INLINE_SYSTEM)
                ],
            }
        ],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    agent = page.dataset.eval_cases[0].agent_data.agents["root_agent"]
    assert agent.instruction == "be brief"


def test_a_truncated_inline_tool_definitions_label_drops_the_case() -> None:
    """Cloud Logging truncates label values at 64 KiB, leaving invalid JSON
    that the converter refuses to guess at."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "trace_id": "t",
                "inference_rows": [
                    _log_row(tool_definitions=_INLINE_TOOLS[:20] + "...")
                ],
            }
        ],
        next_page_token=None,
    )
    recorder = FakeTrajectoryRecorder()

    page = _make_fetcher(client, recorder=recorder).fetch_page(
        "job-1", MetricType.SINGLE_TURN
    )

    assert page.dataset.eval_cases == []
    assert recorder.records[0].ingest_status is IngestStatus.NOT_INGESTED


@pytest.mark.parametrize(
    "metric_type", [MetricType.MULTI_TURN, MetricType.SINGLE_TURN]
)
def test_counting_query_ignores_the_inline_labels(
    metric_type: MetricType,
) -> None:
    """Inline content is a projection of the sampling query; the count only
    sizes the target population."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"scanned": 3}]
    fetcher = _make_fetcher(client)

    fetcher.count_scanned("agent-a", metric_type, _START, _END)

    sql = client.query.call_args.args[0]
    for label in _INLINE_LABELS:
        assert label not in sql
