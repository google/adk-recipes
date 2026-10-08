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

"""Tests for CloudOpsFetcher."""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import json
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion.base import BaseFetcher
from ambient_quality_agent.tools.ingestion.cloud_ops_fetcher import (
    CloudOpsFetcher,
    _decode_spans,
)
from ambient_quality_agent.tools.trajectories.models import IngestStatus
from google.cloud import bigquery

from .conftest import FakeTrajectoryRecorder

_PROJECT = "test-project"
_DATASET = "trace_spans_linked"
_TABLE = "_AllSpans"
_TABLE_REF = f"{_PROJECT}.{_DATASET}.{_TABLE}"
_LOCATION = "us-central1"

_START = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
_END = dt.datetime(2026, 1, 8, tzinfo=dt.UTC)


def _span_struct(
    span_id: str,
    parent_span_id: str,
    name: str,
    attributes: dict[str, Any],
    *,
    status_code: int | None = None,
    status_message: str | None = None,
) -> dict[str, Any]:
    """Constructs a span dictionary as projected by BigQuery queries.

    Args:
        span_id: OpenTelemetry span id.
        parent_span_id: Parent span id.
        name: Span name.
        attributes: Span attributes dictionary to be serialized as JSON.
        status_code: Optional span status code.
        status_message: Optional status description.

    Returns:
        Dictionary matching the BigQuery span record structure.
    """
    return {
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "name": name,
        "attributes": json.dumps(attributes),
        "status_code": status_code,
        "status_message": status_message,
    }


def _turn_spans(user: str = "hi", model: str = "hello") -> list[dict[str, Any]]:
    return [
        _span_struct(
            "agent-1",
            "root",
            "invoke_agent",
            {"gen_ai.agent.name": "car_service_agent"},
        ),
        _span_struct(
            "llm-1",
            "agent-1",
            "call_llm",
            {
                "gcp.vertex.agent.llm_request": json.dumps(
                    {"contents": [{"role": "user", "parts": [{"text": user}]}]}
                ),
                "gcp.vertex.agent.llm_response": json.dumps(
                    {"content": {"role": "model", "parts": [{"text": model}]}}
                ),
            },
        ),
    ]


def _make_fetcher(
    client: mock.MagicMock,
    log_fetcher: Any | None = None,
    recorder: Any | None = None,
    selector_sql: str = "",
) -> CloudOpsFetcher:
    # The fetcher builds its own LogFetcher; patch the class so it uses the
    # fake instead of hitting Cloud Logging.
    with (
        mock.patch(
            "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
            return_value=client,
        ),
        mock.patch("ambient_quality_agent.tools.ingestion.base.storage"),
        mock.patch(
            "ambient_quality_agent.tools.ingestion.cloud_ops_fetcher.LogFetcher",
            return_value=log_fetcher or _FakeLogFetcher(),
        ),
    ):
        return CloudOpsFetcher(
            project_id=_PROJECT,
            dataset=_DATASET,
            table=_TABLE,
            location=_LOCATION,
            recorder=recorder,
            selector_sql=selector_sql,
        )


class _FakeLogFetcher:
    """A LogFetcher stand-in returning canned per-span log entries."""

    def __init__(self, entries: dict[str, list[Any]] | None = None) -> None:
        self._entries = entries or {}
        self.requested: list[str] = []
        self.window: tuple[Any, Any] = (None, None)

    def fetch_entries_by_span(
        self, trace_ids: Any, *, start: Any = None, end: Any = None
    ) -> dict[str, list[Any]]:
        self.requested = list(trace_ids)
        self.window = (start, end)
        return self._entries


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
    row_iter.pages = iter([[_row(row_values) for row_values in page_rows]])
    row_iter.next_page_token = next_page_token
    client.list_rows.return_value = row_iter


def test_cloud_ops_fetcher_is_a_base_fetcher() -> None:
    assert issubclass(CloudOpsFetcher, BaseFetcher)
    assert isinstance(_make_fetcher(mock.MagicMock()), BaseFetcher)


def test_table_ref_built_from_parts_and_project_used_for_client() -> None:
    client = mock.MagicMock()
    with (
        mock.patch(
            "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
            return_value=client,
        ) as ctor,
        mock.patch("ambient_quality_agent.tools.ingestion.base.storage"),
        mock.patch(
            "ambient_quality_agent.tools.ingestion.cloud_ops_fetcher.LogFetcher",
            return_value=_FakeLogFetcher(),
        ),
    ):
        fetcher = CloudOpsFetcher(
            project_id=_PROJECT,
            dataset=_DATASET,
            table=_TABLE,
            location=_LOCATION,
        )

    # The fully-qualified ref is assembled from the project/dataset/table parts.
    assert fetcher.table_ref == _TABLE_REF
    # The BigQuery client is created in the configured project.
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
    assert "gen_ai.agent.name" in sql


def test_submit_query_binds_parameters() -> None:
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client)

    fetcher.submit_query(
        "agent-a", MetricType.MULTI_TURN, _START, _END, limit=7
    )

    job_config = client.query.call_args.kwargs["job_config"]
    params = {param.name: param.value for param in job_config.query_parameters}
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
                    {"trace_id": "t1", "spans": _turn_spans()},
                    {"trace_id": "t2", "spans": _turn_spans("bye", "goodbye")},
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
    assert page.next_page_token == "tok-2"
    assert page.is_exhausted is False


def _build_instructed_turn_spans(
    trace_id: str, instruction: str, exchanges: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Builds one trace's spans with one ``call_llm`` span per exchange.

    Each request replays the trace's earlier exchanges as history, matching ADK.

    Args:
        trace_id: Prefix keeping span IDs unique per trace.
        instruction: System instruction set on every request.
        exchanges: Ordered ``(user_text, model_reply)`` pairs.

    Returns:
        Span dictionaries for the trace.
    """
    agent_span_id = f"{trace_id}-agent"
    spans = [
        _span_struct(
            agent_span_id,
            "root",
            "invoke_agent",
            {"gen_ai.agent.name": "car_service_agent"},
        )
    ]
    history: list[dict[str, Any]] = []
    for index, (user, model) in enumerate(exchanges):
        history.append({"role": "user", "parts": [{"text": user}]})
        reply = {"role": "model", "parts": [{"text": model}]}
        spans.append(
            _span_struct(
                f"{trace_id}-llm-{index}",
                agent_span_id,
                "call_llm",
                {
                    "gcp.vertex.agent.llm_request": json.dumps(
                        {
                            "config": {"system_instruction": instruction},
                            "contents": history,
                        }
                    ),
                    "gcp.vertex.agent.llm_response": json.dumps(
                        {"content": reply}
                    ),
                },
            )
        )
        history.append(reply)
    return spans


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
                        "spans": _build_instructed_turn_spans(
                            "t1", "Be brief.", [("hi", "hello")]
                        ),
                    },
                    {
                        "trace_id": "t2",
                        "spans": _build_instructed_turn_spans(
                            "t2",
                            "Be verbose.",
                            [("slots?", "Monday."), ("later?", "Friday.")],
                        ),
                    },
                ],
            }
        ],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.MULTI_TURN)

    agent_data = page.dataset.eval_cases[0].agent_data
    assert agent_data.agents["car_service_agent"].instruction == "Be brief."
    second_turn = agent_data.turns[1].events
    assert [event.author for event in second_turn].count(
        "car_service_agent"
    ) == 2
    shown = [
        (event.author, event.content.parts[0].text, event.state_delta)
        for turn in agent_data.turns
        for event in turn.events
        if event.state_delta
    ]
    assert shown == [
        (
            "car_service_agent",
            "Monday.",
            {"system_instruction": "Be verbose."},
        )
    ]


def test_fetch_page_maps_single_turn_trace_row() -> None:
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[{"trace_id": "trace-9", "spans": _turn_spans()}],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    case = page.dataset.eval_cases[0]
    assert case.eval_case_id == "trace-9"
    assert len(case.agent_data.turns) == 1
    assert case.agent_data.agents["car_service_agent"] is not None
    assert page.is_exhausted is True


def _metadata_only_spans() -> list[dict[str, Any]]:
    """Constructs test spans carrying metadata without inline message content.

    Returns:
        List of span dictionaries.
    """
    return [
        _span_struct(
            "agent-1",
            "root",
            "invoke_agent",
            {"gen_ai.agent.name": "car_service_agent"},
        ),
        _span_struct(
            "llm-1", "agent-1", "call_llm", {"gen_ai.usage.input_tokens": 10}
        ),
    ]


@dataclasses.dataclass
class _LogEntry:
    labels: dict[str, Any]
    json_payload: dict[str, Any]


def _log_entry(event_name: str, content: Any) -> _LogEntry:
    return _LogEntry(
        labels={"event.name": event_name}, json_payload={"content": content}
    )


def test_fetch_page_merges_message_content_from_logs() -> None:
    """When spans carry only metadata, log content fills the turn."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[{"trace_id": "trace-9", "spans": _metadata_only_spans()}],
        next_page_token=None,
    )
    log_fetcher = _FakeLogFetcher(
        {
            "llm-1": [
                _log_entry(
                    "gen_ai.user.message",
                    {"role": "user", "parts": [{"text": "from logs"}]},
                ),
                _log_entry(
                    "gen_ai.choice",
                    {"role": "model", "parts": [{"text": "logged reply"}]},
                ),
            ]
        }
    )
    fetcher = _make_fetcher(client, log_fetcher=log_fetcher)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    # The trace id was forwarded to the log fetcher.
    assert log_fetcher.requested == ["trace-9"]
    events = page.dataset.eval_cases[0].agent_data.turns[0].events
    texts = [e.content.parts[0].text for e in events]
    assert texts == ["from logs", "logged reply"]
    assert events[0].author == "user"
    assert events[1].author == "car_service_agent"


def test_fetch_page_maps_attachments_in_log_label_messages() -> None:
    """A referenced file stays native; inline bytes become a placeholder."""
    image = b"\x89PNG" + bytes(28)
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[{"trace_id": "trace-9", "spans": _metadata_only_spans()}],
        next_page_token=None,
    )
    messages = [
        {
            "role": "user",
            "parts": [
                {
                    "type": "uri",
                    "uri": "gs://bucket/doc.pdf",
                    "mime_type": "application/pdf",
                    "modality": "application",
                },
                {
                    "type": "blob",
                    "content": base64.b64encode(image).decode(),
                    "mime_type": "image/png",
                    "modality": "image",
                },
            ],
        }
    ]
    log_fetcher = _FakeLogFetcher(
        {
            "llm-1": [
                _LogEntry(
                    labels={"gen_ai.input.messages": json.dumps(messages)},
                    json_payload={},
                )
            ]
        }
    )
    fetcher = _make_fetcher(client, log_fetcher=log_fetcher)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    parts = (
        page.dataset.eval_cases[0].agent_data.turns[0].events[0].content.parts
    )
    assert parts[0].file_data is not None
    assert parts[0].file_data.file_uri == "gs://bucket/doc.pdf"
    digest = hashlib.sha256(image).hexdigest()[:8]
    assert parts[1].text == (
        f"<attachment: image/png, {len(image)} bytes, sha256:{digest}>"
    )


def test_fetch_page_joins_a_structured_system_message_from_logs() -> None:
    """A Content-shaped system message becomes the joined instruction text."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[{"trace_id": "trace-9", "spans": _metadata_only_spans()}],
        next_page_token=None,
    )
    log_fetcher = _FakeLogFetcher(
        {
            "llm-1": [
                _log_entry(
                    "gen_ai.system.message",
                    {
                        "role": "system",
                        "parts": [
                            {"text": "Be helpful."},
                            {"text": "Be brief."},
                        ],
                    },
                ),
                _log_entry(
                    "gen_ai.user.message",
                    {"role": "user", "parts": [{"text": "hi"}]},
                ),
            ]
        }
    )
    fetcher = _make_fetcher(client, log_fetcher=log_fetcher)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    agent = page.dataset.eval_cases[0].agent_data.agents["car_service_agent"]
    assert agent.instruction == "Be helpful.\n\nBe brief."


def _truncated_spans() -> list[dict[str, Any]]:
    """Constructs test spans containing truncated, unparseable message payloads.

    Returns:
        List of span dictionaries.
    """
    return [
        _span_struct(
            "agent-1",
            "root",
            "invoke_agent",
            {"gen_ai.agent.name": "car_service_agent"},
        ),
        _span_struct(
            "llm-1",
            "agent-1",
            "call_llm",
            {
                "gen_ai.input.messages": '[{"role":"user","parts":[{"content":"hel'
            },
        ),
    ]


def test_fetch_page_drops_unparseable_case() -> None:
    """A case with truncated telemetry is dropped from the dataset."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "session_id": "good",
                "traces": [{"trace_id": "t1", "spans": _turn_spans()}],
            },
            {
                "session_id": "bad",
                "traces": [{"trace_id": "t2", "spans": _truncated_spans()}],
            },
        ],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.MULTI_TURN)

    assert [c.eval_case_id for c in page.dataset.eval_cases] == ["good"]


def test_fetch_page_drops_case_with_no_turns() -> None:
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "session_id": "good",
                "traces": [{"trace_id": "t1", "spans": _turn_spans()}],
            },
            {
                "session_id": "empty",
                "traces": [{"trace_id": "t2", "spans": _metadata_only_spans()}],
            },
        ],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client, log_fetcher=_FakeLogFetcher())

    page = fetcher.fetch_page("job-1", MetricType.MULTI_TURN)

    assert [c.eval_case_id for c in page.dataset.eval_cases] == ["good"]


def test_fetch_page_passes_page_token() -> None:
    client = mock.MagicMock()
    _configure_paging(client, page_rows=[], next_page_token=None)
    fetcher = _make_fetcher(client)

    fetcher.fetch_page("job-1", MetricType.MULTI_TURN, page_token="resume-here")

    assert client.list_rows.call_args.kwargs["page_token"] == "resume-here"


def test_fetch_page_empty_destination_returns_no_cases() -> None:
    client = mock.MagicMock()
    _configure_paging(client, page_rows=[], next_page_token=None)
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    assert page.dataset.eval_cases == []
    assert page.is_exhausted is True


def test_fetch_page_raises_without_destination() -> None:
    client = mock.MagicMock()
    query_job = mock.MagicMock(spec=bigquery.QueryJob)
    query_job.destination = None
    client.get_job.return_value = query_job
    fetcher = _make_fetcher(client)

    with pytest.raises(ValueError, match="no destination"):
        fetcher.fetch_page("job-1", MetricType.MULTI_TURN)


def test_decode_spans_parses_attributes_json() -> None:
    decoded = _decode_spans(
        [_span_struct("s1", "p1", "call_llm", {"gen_ai.agent.name": "a"})]
    )
    assert decoded[0].span_id == "s1"
    assert decoded[0].attributes == {"gen_ai.agent.name": "a"}


def test_decode_spans_tolerates_bad_attributes() -> None:
    decoded = _decode_spans(
        [
            {
                "span_id": "s1",
                "parent_span_id": "p",
                "name": "x",
                "attributes": "{bad",
            }
        ]
    )
    assert decoded[0].attributes == {}


def test_decode_spans_tolerates_null_attributes() -> None:
    """Ensure a BigQuery NULL attributes column (rendered as string "null") decodes to an empty dict."""
    decoded = _decode_spans(
        [
            {
                "span_id": "s1",
                "parent_span_id": "p",
                "name": "x",
                "attributes": "null",
            }
        ]
    )
    assert decoded[0].attributes == {}


def test_decode_spans_tolerates_null_events() -> None:
    decoded = _decode_spans(
        [
            {
                "span_id": "s1",
                "parent_span_id": "p",
                "name": "x",
                "events": "null",
            }
        ]
    )
    assert decoded[0].events == []


def test_decode_spans_tolerates_wrong_shaped_attributes() -> None:
    """Ensure an unexpected JSON structure (array instead of object) falls back to the default dict."""
    decoded = _decode_spans(
        [
            {
                "span_id": "s1",
                "parent_span_id": "p",
                "name": "x",
                "attributes": "[1,2,3]",
            }
        ]
    )
    assert decoded[0].attributes == {}


def test_fetch_page_keeps_a_case_with_a_null_attributes_span() -> None:
    """Ensure an infrastructure span with NULL attributes does not drop the evaluation case."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "trace_id": "trace-9",
                "spans": [
                    *_turn_spans(),
                    {
                        "span_id": "infra-1",
                        "parent_span_id": "root",
                        "name": "dequeue_event",
                        "attributes": "null",
                        "events": "null",
                    },
                ],
            }
        ],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    assert [c.eval_case_id for c in page.dataset.eval_cases] == ["trace-9"]


# --- counting and ingestion tallies -------------------------------------------


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
    client.query.return_value.result.return_value = [{"scanned": 11}]
    fetcher = _make_fetcher(client)

    scanned = fetcher.count_scanned("agent-a", metric_type, _START, _END)

    sql = client.query.call_args.args[0]
    assert "COUNT(*) AS scanned" in sql
    assert f"GROUP BY {unit_column}" in sql
    assert "gen_ai.agent.name" in sql
    assert "ORDER BY RAND()" not in sql and "LIMIT" not in sql
    assert scanned == 11 and fetcher.counts.scanned == 11


@pytest.mark.parametrize(
    ("metric_type", "unit"),
    [
        (
            MetricType.MULTI_TURN,
            "COUNT(DISTINCT JSON_VALUE(attributes, '$.\"gen_ai.conversation.id\"'))",
        ),
        (MetricType.SINGLE_TURN, "COUNT(DISTINCT trace_id)"),
    ],
)
def test_count_by_agent_counts_the_same_unit_for_every_agent(
    metric_type: MetricType, unit: str
) -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [
        {"agent_name": "a", "scanned": 4}
    ]
    fetcher = _make_fetcher(client)

    counts = fetcher.count_by_agent(metric_type, _START, _END)

    sql = client.query.call_args.args[0]
    assert f"{unit} AS scanned" in sql
    assert (
        "JSON_VALUE(attributes, '$.\"gen_ai.agent.name\"') AS agent_name" in sql
    )
    assert "GROUP BY agent_name" in sql
    assert "@agent_name" not in sql
    params = {
        p.name
        for p in client.query.call_args.kwargs["job_config"].query_parameters
    }
    assert params == {"window_start", "window_end"}
    assert counts == {"a": 4}
    assert fetcher.counts.scanned == 0


def test_map_page_counts_a_whole_case_as_ingested() -> None:
    fetcher = _make_fetcher(mock.MagicMock())

    fetcher._map_page(
        [{"trace_id": "t1", "spans": _turn_spans()}], MetricType.SINGLE_TURN
    )

    assert dataclasses.astuple(fetcher.counts) == (0, 1, 0, 0)


def test_map_page_counts_a_case_built_from_some_of_its_traces_as_partial() -> (
    None
):
    """A session whose second trace yields no turn is evaluated on half its
    telemetry -- worth telling apart from one that arrived whole."""
    fetcher = _make_fetcher(mock.MagicMock())

    cases, _ = fetcher._map_page(
        [
            {
                "session_id": "sess-1",
                "traces": [
                    {"trace_id": "t1", "spans": _turn_spans()},
                    {"trace_id": "t2", "spans": []},
                ],
            }
        ],
        MetricType.MULTI_TURN,
    )

    # Still evaluated, and counted apart from the intact ones.
    assert len(cases) == 1
    assert dataclasses.astuple(fetcher.counts) == (0, 0, 1, 0)


def test_map_page_counts_a_dropped_row_as_failed() -> None:
    """A row with no conversation content at all yields no case."""
    fetcher = _make_fetcher(mock.MagicMock())

    cases, _ = fetcher._map_page(
        [{"trace_id": "t1", "spans": _metadata_only_spans()}],
        MetricType.SINGLE_TURN,
    )

    assert cases == []
    assert dataclasses.astuple(fetcher.counts) == (0, 0, 0, 1)


def test_an_unreadable_gcs_payload_makes_the_case_partial() -> None:
    """The payload was offloaded because it was too large to inline, so losing
    it loses conversation -- the case is kept, and marked."""
    client = mock.MagicMock()
    fetcher = _make_fetcher(client)

    def failing_reader(_uri: str) -> str:
        raise RuntimeError("403")

    fetcher._converter._gcs_reader = failing_reader
    spans = [
        *_turn_spans(),
        _span_struct(
            "llm-2",
            "agent-1",
            "call_llm",
            {"gen_ai.input.messages_ref": "gs://bucket/lost.json"},
        ),
    ]

    cases, _ = fetcher._map_page(
        [{"trace_id": "t1", "spans": spans}], MetricType.SINGLE_TURN
    )

    assert len(cases) == 1
    assert fetcher.counts.partial == 1
    assert fetcher.counts.ingested == 0


def test_an_unreadable_gcs_payload_logs_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A denied read and a missing object need different fixes."""
    fetcher = _make_fetcher(mock.MagicMock())

    def read_denied(_uri: str) -> str:
        """Refuses every read, as a missing bucket grant does.

        Args:
            _uri: Ignored payload URI.

        Returns:
            Nothing; it always raises.

        Raises:
            RuntimeError: Always.
        """
        raise RuntimeError("403 storage.objects.get denied")

    fetcher._converter._gcs_reader = read_denied
    spans = [
        *_turn_spans(),
        _span_struct(
            "llm-2",
            "agent-1",
            "call_llm",
            {"gen_ai.input.messages_ref": "gs://bucket/lost.json"},
        ),
    ]

    with caplog.at_level("WARNING"):
        fetcher._map_page(
            [{"trace_id": "t1", "spans": spans}], MetricType.SINGLE_TURN
        )

    assert (
        "Failed to resolve trace attribute ref gs://bucket/lost.json: "
        "RuntimeError: 403 storage.objects.get denied" in caplog.text
    )


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
    """Both scopes read ``service.version`` off the span's resource attributes."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client)

    fetcher.submit_query("agent-a", metric_type, _START, _END, limit=5)

    sql = _sql_words(client.query.call_args.args[0])
    assert """JSON_VALUE(s.resource.attributes, '$."service.version"')""" in sql
    assert "AS agent_revision" in sql


@pytest.mark.parametrize(
    "metric_type", [MetricType.MULTI_TURN, MetricType.SINGLE_TURN]
)
def test_span_revision_is_taken_from_the_last_span(
    metric_type: MetricType,
) -> None:
    """A redeploy mid-trace leaves two revisions on one trace's spans; the
    latest one is the configuration the trace finished under."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client)

    fetcher.submit_query("agent-a", metric_type, _START, _END, limit=5)

    sql = _sql_words(client.query.call_args.args[0])
    assert (
        """ARRAY_AGG(JSON_VALUE(s.resource.attributes, '$."service.version"') """
        "IGNORE NULLS ORDER BY s.start_time DESC LIMIT 1)[SAFE_OFFSET(0)] "
        "AS agent_revision" in sql
    )


def test_multi_turn_query_takes_the_session_s_latest_revision() -> None:
    """A session spanning a redeploy is attributed to its last trace's revision."""
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
    assert "service.version" not in sql
    assert f"GROUP BY {unit_column}" in sql


def test_fetch_page_maps_the_revision_by_eval_case_id() -> None:
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "session_id": "sess-1",
                "agent_revision": "4",
                "traces": [{"trace_id": "t1", "spans": _turn_spans()}],
            }
        ],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.MULTI_TURN)

    assert page.revisions == {"sess-1": "4"}


def test_fetch_page_leaves_a_row_without_a_revision_unattributed() -> None:
    """Spans carrying no ``service.version`` map to the single unnamed
    revision, not an unknown one."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[{"trace_id": "trace-9", "spans": _turn_spans()}],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.SINGLE_TURN)

    assert [c.eval_case_id for c in page.dataset.eval_cases] == ["trace-9"]
    assert page.revisions == {}


# --- attaching spans by trace id ----------------------------------------------

_STATUS_ERROR = 2


# Conversation turn data in two telemetry formats: OpenTelemetry messages on
# ``generate_content`` spans and Gemini API-format payload attributes on ``call_llm``
# spans. Because tool-call serialization structures differ between formats,
# ingesting both unpruned would duplicate conversation events.
_OTEL_INPUT = json.dumps(
    [
        {
            "role": "user",
            "parts": [{"type": "text", "content": "file my expense"}],
        }
    ]
)
_OTEL_OUTPUT = json.dumps(
    [
        {
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_call",
                    "id": "adk-1",
                    "name": "file_expense",
                    "arguments": {"amount": 42},
                }
            ],
        }
    ]
)
_NATIVE_LLM_REQUEST = json.dumps(
    {"contents": [{"role": "user", "parts": [{"text": "file my expense"}]}]}
)
_NATIVE_LLM_RESPONSE = json.dumps(
    {
        "content": {
            "role": "model",
            "parts": [
                {
                    "function_call": {
                        "id": "adk-1",
                        "name": "file_expense",
                        "args": {"amount": 42},
                    }
                }
            ],
        }
    }
)


def _failing_tool_spans(
    *, with_call_llm: bool = True, inline_conversation: bool = False
) -> list[dict[str, Any]]:
    """Build a synthetic span tree representing a failed tool execution.

    Agent and workflow spans contain session ids, whereas intermediate
    ``call_llm`` and leaf ``execute_tool`` spans rely on trace-level joining
    to reach the converter.

    Args:
        with_call_llm: Whether to include the intermediate ``call_llm`` span linking
            the tool to the agent.
        inline_conversation: Whether ``call_llm`` contains unpruned Gemini API-format
            request and response payloads.

    Returns:
        List of span dictionaries representing a tool failure.
    """
    spans = [
        _span_struct(
            "wf-1",
            "",
            "invoke_workflow travel_desk_sr1",
            {"gen_ai.conversation.id": "sess-1"},
            status_code=_STATUS_ERROR,
            status_message="ValueError: budget exceeded",
        ),
        _span_struct(
            "agent-1",
            "wf-1",
            "invoke_agent expense_agent",
            {
                "gen_ai.conversation.id": "sess-1",
                "gen_ai.agent.name": "expense_agent",
            },
            status_code=_STATUS_ERROR,
            status_message="ValueError: budget exceeded",
        ),
    ]
    if with_call_llm:
        payload = (
            {
                "gcp.vertex.agent.llm_request": _NATIVE_LLM_REQUEST,
                "gcp.vertex.agent.llm_response": _NATIVE_LLM_RESPONSE,
            }
            if inline_conversation
            else {}
        )
        spans.append(_span_struct("llm-1", "agent-1", "call_llm", payload))
    spans += [
        _span_struct(
            "gen-1",
            "llm-1" if with_call_llm else "agent-1",
            "generate_content gemini-2.5-flash",
            {
                "gen_ai.conversation.id": "sess-1",
                "gen_ai.input.messages": _OTEL_INPUT,
                "gen_ai.output.messages": _OTEL_OUTPUT,
            },
        ),
        _span_struct(
            "tool-1",
            "llm-1" if with_call_llm else "agent-1",
            "execute_tool file_expense",
            {"gen_ai.tool.name": "file_expense"},
            status_code=_STATUS_ERROR,
            status_message="budget exceeded",
        ),
    ]
    return spans


def test_multi_turn_query_attaches_spans_by_trace_id() -> None:
    """Ensure multi-turn queries map sampled sessions to traces before joining spans,
    preserving spans that lack conversation ids."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client)

    fetcher.submit_query(
        "agent-a", MetricType.MULTI_TURN, _START, _END, limit=5
    )

    sql = _sql_words(client.query.call_args.args[0])
    assert (
        "TargetTraces AS ( SELECT s.trace_id AS trace_id, t.session_id" in sql
    )
    assert "JOIN TargetTraces AS t USING (trace_id)" in sql
    # Sourced from TargetTraces to guarantee a non-null session id when grouped
    # child spans omit conversation attributes.
    assert "t.session_id AS session_id" in sql
    assert "ANY_VALUE" not in sql


@pytest.mark.parametrize(
    "metric_type", [MetricType.MULTI_TURN, MetricType.SINGLE_TURN]
)
def test_query_prunes_the_inline_conversation_from_spans_without_a_session(
    metric_type: MetricType,
) -> None:
    """Ensure queries prune duplicate LLM payloads from spans lacking session ids."""
    client = mock.MagicMock()
    client.query.return_value.job_id = "job-1"
    fetcher = _make_fetcher(client)

    fetcher.submit_query("agent-a", metric_type, _START, _END, limit=5)

    sql = _sql_words(client.query.call_args.args[0])
    assert (
        """IF( JSON_VALUE(s.attributes, '$."gen_ai.conversation.id"') IS NULL, """
        """TO_JSON_STRING( JSON_REMOVE( s.attributes, """
        """'$."gcp.vertex.agent.llm_request"', """
        """'$."gcp.vertex.agent.llm_response"' ) ), """
        "TO_JSON_STRING(s.attributes) ) AS attributes" in sql
    )


def test_spans_without_a_conversation_id_still_map_to_their_session() -> None:
    """Ensure spans lacking conversation ids inherit the session id resolved by trace mapping."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "session_id": "sess-1",
                "traces": [{"trace_id": "t1", "spans": _failing_tool_spans()}],
            }
        ],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.MULTI_TURN)

    assert [c.eval_case_id for c in page.dataset.eval_cases] == ["sess-1"]


def test_a_failed_tool_is_named_and_attributed_to_the_agent_that_ran_it() -> (
    None
):
    """Ensure tool execution failures are attributed to the owning agent via the span hierarchy."""
    client = mock.MagicMock()
    _configure_paging(
        client,
        page_rows=[
            {
                "session_id": "sess-1",
                "traces": [{"trace_id": "t1", "spans": _failing_tool_spans()}],
            }
        ],
        next_page_token=None,
    )
    fetcher = _make_fetcher(client)

    page = fetcher.fetch_page("job-1", MetricType.MULTI_TURN)

    events = page.dataset.eval_cases[0].agent_data.turns[0].events
    errors = [
        (e.author, part.function_response)
        for e in events
        for part in e.content.parts
        if part.function_response is not None
    ]
    assert errors == [
        ("expense_agent", mock.ANY),
    ]
    assert errors[0][1].name == "file_expense"
    assert errors[0][1].response == {"error": "budget exceeded"}


def test_a_pruned_call_llm_span_adds_no_conversation_events() -> None:
    """Ensure pruned intermediate ``call_llm`` spans do not emit duplicate conversation events."""
    client = mock.MagicMock()
    fetcher = _make_fetcher(client)

    def event_count(spans: list[dict[str, Any]]) -> int:
        cases, _ = fetcher._map_page(
            [
                {
                    "session_id": "sess-1",
                    "traces": [{"trace_id": "t1", "spans": spans}],
                }
            ],
            MetricType.MULTI_TURN,
        )
        return sum(len(turn.events) for turn in cases[0].agent_data.turns)

    baseline = event_count(_failing_tool_spans(with_call_llm=False))
    assert event_count(_failing_tool_spans(with_call_llm=True)) == baseline
    # Retaining unpruned payload attributes introduces format-mismatched duplicate events.
    assert (
        event_count(
            _failing_tool_spans(with_call_llm=True, inline_conversation=True)
        )
        > baseline
    )


# --- what ingestion reports to the recorder -----------------------------------


def _recorded(
    fetcher: CloudOpsFetcher, rows: list[dict[str, Any]], metric_type: Any
):
    """Maps a page of rows through the fetcher and returns recorded records.

    Args:
        fetcher: The CloudOpsFetcher instance.
        rows: List of row dictionaries.
        metric_type: MetricType (single-turn or multi-turn).

    Returns:
        List of recorded trajectory records.
    """
    fetcher._map_page(rows, metric_type)
    fetcher._recorder.flush()
    return fetcher._recorder.records


def test_a_multi_turn_trajectory_reports_its_traces_in_conversation_order() -> (
    None
):
    """The query aggregates a session's traces by start time, so the first id
    is its opening turn -- which is the one a console link uses."""
    recorder = FakeTrajectoryRecorder()
    fetcher = _make_fetcher(mock.MagicMock(), recorder=recorder)

    recorded = _recorded(
        fetcher,
        [
            {
                "session_id": "sess-1",
                "traces": [
                    {"trace_id": "first", "spans": _turn_spans()},
                    {"trace_id": "second", "spans": _turn_spans()},
                ],
            }
        ],
        MetricType.MULTI_TURN,
    )

    assert [t.trajectory_id for t in recorded] == ["sess-1"]
    assert recorded[0].trace_ids == ("first", "second")
    assert recorded[0].session_id == "sess-1"
    # Both traces carry the same spans, so session-wide dedup leaves the second
    # without a turn -- partial, a state a `dropped` bool could not express.
    assert recorded[0].ingest_status is IngestStatus.PARTIAL


def test_a_single_turn_trajectory_reports_the_session_it_belongs_to() -> None:
    """One turn's own trace id, plus the conversation around it, so a reader
    can walk from the turn to the whole session."""
    recorder = FakeTrajectoryRecorder()
    fetcher = _make_fetcher(mock.MagicMock(), recorder=recorder)

    recorded = _recorded(
        fetcher,
        [{"trace_id": "t1", "session_id": "sess-1", "spans": _turn_spans()}],
        MetricType.SINGLE_TURN,
    )

    assert recorded[0].trajectory_id == "t1"
    assert recorded[0].trace_ids == ("t1",)
    assert recorded[0].session_id == "sess-1"
    assert recorded[0].ingest_status is IngestStatus.INGESTED


def test_a_row_with_no_conversation_content_is_still_reported() -> None:
    """The most valuable link on the page: a sampled trajectory the run could
    not use is the one case where "what does this look like?" has no other
    answer."""
    recorder = FakeTrajectoryRecorder()
    fetcher = _make_fetcher(mock.MagicMock(), recorder=recorder)

    recorded = _recorded(
        fetcher,
        [{"trace_id": "t1", "spans": _metadata_only_spans()}],
        MetricType.SINGLE_TURN,
    )

    assert [
        (t.trajectory_id, t.ingest_status, t.trace_ids) for t in recorded
    ] == [("t1", IngestStatus.NOT_INGESTED, ("t1",))]


def test_a_row_whose_payload_cannot_be_parsed_is_still_reported() -> None:
    """The other drop path, which the counters distinguish from the one above
    only as a tally."""
    recorder = FakeTrajectoryRecorder()
    fetcher = _make_fetcher(mock.MagicMock(), recorder=recorder)
    spans = [
        _span_struct(
            "llm-1",
            "agent-1",
            "call_llm",
            {"gcp.vertex.agent.llm_request": "{not json"},
        )
    ]

    recorded = _recorded(
        fetcher, [{"trace_id": "t1", "spans": spans}], MetricType.SINGLE_TURN
    )

    assert [(t.trajectory_id, t.ingest_status) for t in recorded] == [
        ("t1", IngestStatus.NOT_INGESTED)
    ]


def test_nothing_is_written_before_the_page_is_done() -> None:
    """One write per page: the unit ingestion produces, and what the store is
    sized for."""
    recorder = FakeTrajectoryRecorder()
    fetcher = _make_fetcher(mock.MagicMock(), recorder=recorder)

    fetcher._map_page(
        [{"trace_id": "t1", "spans": _turn_spans()}], MetricType.SINGLE_TURN
    )

    assert recorder.records == []
    fetcher._recorder.flush()
    assert len(recorder.records) == 1


# --- the selector: which sessions the sweep reviews ------------------------ #

# SQL selector containing braces (regex quantifiers, JSON literals, struct
# literals) to verify format-safe template handling.
_BRACE_SELECTOR = """
SELECT JSON_VALUE(attributes, '$."gen_ai.conversation.id"') AS target_id
FROM `test-project.trace_spans_linked._AllSpans`
WHERE REGEXP_CONTAINS(name, r'\\d{3}')
  AND TO_JSON_STRING(attributes) = '{"tool":"refund"}'
  AND STRUCT(1 AS a) IS NOT NULL
"""

# Expected targets CTE SQL fragment for window-scoped queries.
_WINDOW_TARGETS = f"""
  SELECT JSON_VALUE(attributes, '$."gen_ai.conversation.id"') AS session_id
  FROM `{_TABLE_REF}`
  WHERE start_time BETWEEN @window_start AND @window_end
    AND JSON_VALUE(attributes, '$."gen_ai.agent.name"') = @agent_name
    AND JSON_VALUE(attributes, '$."gen_ai.conversation.id"') IS NOT NULL
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


# --- get_payload_ref ---------------------------------------------------------- #


class _RefLogFetcher(_FakeLogFetcher):
    """Answers `get_payload_ref` with a scripted reference."""

    def __init__(self, ref: str | None) -> None:
        super().__init__()
        self._ref = ref
        self.ref_calls: list[tuple[Any, ...]] = []

    def get_payload_ref(self, agent_name, ref_keys, *, start, end):
        self.ref_calls.append((agent_name, tuple(ref_keys), start, end))
        return self._ref


def test_get_payload_ref_reads_the_spans_of_the_agents_traces_first() -> None:
    """The reference sits on the model-call span, which need not carry the
    agent name, so the agent's traces are selected first."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"ref": "gs://b/in.jsonl"}]
    logs = _RefLogFetcher("gs://b/from-logs.jsonl")
    fetcher = _make_fetcher(client, log_fetcher=logs)

    assert fetcher.get_payload_ref("agent-a", _START, _END) == "gs://b/in.jsonl"
    sql = client.query.call_args.args[0]
    assert f"FROM `{_TABLE_REF}`" in sql
    assert "trace_id IN (" in sql
    assert """JSON_VALUE(attributes, '$."gen_ai.input.messages_ref"')""" in sql
    assert logs.ref_calls == []


def test_get_payload_ref_falls_back_to_the_log_entries() -> None:
    """The converter's second place to look, so the check's too."""
    client = mock.MagicMock()
    client.query.return_value.result.return_value = []
    logs = _RefLogFetcher("gs://b/from-logs.jsonl")
    fetcher = _make_fetcher(client, log_fetcher=logs)

    assert (
        fetcher.get_payload_ref("agent-a", _START, _END)
        == "gs://b/from-logs.jsonl"
    )
    [(agent, keys, start, end)] = logs.ref_calls
    assert agent == "agent-a"
    assert keys[0] == "gen_ai.input.messages_ref"
    assert (start, end) == (_START, _END)


def test_the_observed_project_owns_the_table_and_the_logs_and_jobs_stay_home() -> (
    None
):
    with (
        mock.patch(
            "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
            return_value=mock.MagicMock(),
        ) as ctor,
        mock.patch("ambient_quality_agent.tools.ingestion.base.storage"),
        mock.patch(
            "ambient_quality_agent.tools.ingestion.cloud_ops_fetcher.LogFetcher",
            return_value=_FakeLogFetcher(),
        ) as log_fetcher,
    ):
        fetcher = CloudOpsFetcher(
            project_id=_PROJECT,
            dataset=_DATASET,
            table=_TABLE,
            location=_LOCATION,
            observed_project_id="agent-project",
        )

    assert fetcher.table_ref == f"agent-project.{_DATASET}.{_TABLE}"
    assert ctor.call_args.kwargs["project"] == _PROJECT
    log_fetcher.assert_called_once_with("agent-project")
