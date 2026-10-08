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

"""Unit tests verifying `GcsSpanExporter` serialization and provider attachment.

Tests validate emitted span format against `all_spans.schema.json` and ensure
exported traces can be decoded and converted into conversation turns.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import pathlib
from typing import Any

import pytest
from ambient_quality_agent.telemetry import setup as telemetry_setup
from ambient_quality_agent.telemetry.gcs_span_exporter import GcsSpanExporter
from ambient_quality_agent.tools.ingestion.cloud_ops_fetcher import (
    _decode_spans,
)
from ambient_quality_agent.tools.ingestion.trace_converter import (
    TraceAgentDataConverter,
)
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.trace import SpanContext, TraceFlags
from opentelemetry.trace.status import Status, StatusCode

_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "ambient_quality_cli"
    / "all_spans.schema.json"
)

_TRACE_ID = 0x0123456789ABCDEF0123456789ABCDEF
_RESOURCE_ATTRIBUTES = {
    "service.name": "ambient_quality_agent",
    "service.version": "42",
}

# 2026-03-01T07:59:59.500000Z, in epoch nanoseconds -- an hour boundary is one
# second away, which is what the partitioning test needs.
_START_NS = 1_772_351_999_500_000_000
_END_NS = _START_NS + 1_500_000_000


class _FakeBlob:
    """Records one upload, or raises when the fake was seeded to fail."""

    def __init__(self, client: _FakeGcsClient, name: str) -> None:
        self._client = client
        self._name = name

    def upload_from_string(
        self, data: bytes, content_type: str | None = None
    ) -> None:
        if self._client.fail:
            raise RuntimeError("upload failed")
        self._client.uploads.append((self._name, data, content_type))


class _FakeBucket:
    def __init__(self, client: _FakeGcsClient, name: str) -> None:
        self._client = client
        self.name = name

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self._client, name)


class _FakeGcsClient:
    """In-memory `storage.Client` stand-in recording the objects written."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.uploads: list[tuple[str, bytes, str | None]] = []
        self.buckets: list[str] = []

    def bucket(self, name: str) -> _FakeBucket:
        self.buckets.append(name)
        return _FakeBucket(self, name)


def _span(
    *,
    name: str = "invocation",
    span_id: int = 0x1122334455667788,
    parent_span_id: int | None = None,
    attributes: dict[str, Any] | None = None,
    status: Status | None = None,
    events: tuple[Event, ...] = (),
    start_time: int = _START_NS,
    end_time: int = _END_NS,
) -> ReadableSpan:
    """Builds a `ReadableSpan` for testing.

    Args:
        name: Span operation name.
        span_id: Identifier for the span.
        parent_span_id: Optional parent span identifier.
        attributes: Span attributes dictionary.
        status: Span execution status.
        events: Span events sequence.
        start_time: Start timestamp in epoch nanoseconds.
        end_time: End timestamp in epoch nanoseconds.

    Returns:
        Configured ReadableSpan instance.
    """
    context = SpanContext(
        trace_id=_TRACE_ID,
        span_id=span_id,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    parent = (
        SpanContext(
            trace_id=_TRACE_ID,
            span_id=parent_span_id,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        if parent_span_id is not None
        else None
    )
    return ReadableSpan(
        name=name,
        context=context,
        parent=parent,
        attributes=attributes or {},
        events=events,
        status=status or Status(StatusCode.UNSET),
        start_time=start_time,
        end_time=end_time,
    )


def _exporter(client: _FakeGcsClient, *, prefix: str = "aqa-telemetry/spans"):
    return GcsSpanExporter(
        bucket="traces-bucket",
        prefix=prefix,
        resource_attributes=_RESOURCE_ATTRIBUTES,
        client_factory=lambda: client,
    )


def _exported_rows(client: _FakeGcsClient) -> list[dict[str, Any]]:
    """Decodes the newline-delimited rows of the single object written.

    Args:
        client: Fake GCS client capturing uploads.

    Returns:
        List of decoded row dictionaries from the gzipped upload.
    """
    assert len(client.uploads) == 1
    _, payload, _ = client.uploads[0]
    lines = gzip.decompress(payload).decode("utf-8").splitlines()
    return [json.loads(line) for line in lines]


def test_exported_row_matches_the_all_spans_schema() -> None:
    # The emitted row and the schema the dump is loaded with are one contract:
    # every column is present, and no key exists that the schema has no column
    # for.
    client = _FakeGcsClient()
    span = _span(
        attributes={
            "gen_ai.response.finish_reasons": ("STOP",),
            "gen_ai.input.messages": '[{"role": "user"}]',
            "gen_ai.usage.input_tokens": 12,
        },
        status=Status(StatusCode.ERROR, "boom"),
        events=(
            Event(name="exception", timestamp=_END_NS, attributes={"a": "b"}),
        ),
    )

    assert _exporter(client).export([span]) is SpanExportResult.SUCCESS

    (row,) = _exported_rows(client)
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert set(row) == {field["name"] for field in schema}
    assert row["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert row["span_id"] == "1122334455667788"
    assert row["parent_span_id"] is None
    assert row["name"] == "invocation"
    assert row["start_time"] == "2026-03-01T07:59:59.500000Z"
    assert row["end_time"] == "2026-03-01T08:00:01.000000Z"
    assert row["status"] == {"code": 2, "message": "boom"}
    assert row["events"] == [
        {
            "time": "2026-03-01T08:00:01.000000Z",
            "name": "exception",
            "attributes": {"a": "b"},
        }
    ]
    assert row["resource"] == {"attributes": _RESOURCE_ATTRIBUTES}
    # A sequence-valued attribute becomes a JSON array; an already-JSON-encoded
    # content attribute stays the string it arrived as, so a reader parses it
    # exactly once.
    assert row["attributes"] == {
        "gen_ai.response.finish_reasons": ["STOP"],
        "gen_ai.input.messages": '[{"role": "user"}]',
        "gen_ai.usage.input_tokens": 12,
    }


def test_child_span_carries_its_parent_id_in_hex() -> None:
    client = _FakeGcsClient()

    _exporter(client).export([_span(span_id=0xAB, parent_span_id=0xCD)])

    (row,) = _exported_rows(client)
    assert row["span_id"] == "00000000000000ab"
    assert row["parent_span_id"] == "00000000000000cd"


def test_object_name_partitions_by_utc_date_and_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The partition follows the wall clock in UTC, not the local zone, and rolls
    # at the hour: 23:59 and 00:01 fall on different dates.
    client = _FakeGcsClient()
    exporter = _exporter(client)

    for moment in (
        dt.datetime(2026, 3, 1, 23, 59, 59, tzinfo=dt.UTC),
        dt.datetime(2026, 3, 2, 0, 0, 1, tzinfo=dt.UTC),
    ):
        monkeypatch.setattr(
            "ambient_quality_agent.telemetry.gcs_span_exporter._read_utc_clock",
            lambda moment=moment: moment,
        )
        exporter.export([_span()])

    names = [name for name, _, _ in client.uploads]
    assert names[0].startswith("aqa-telemetry/spans/dt=2026-03-01/23/")
    assert names[1].startswith("aqa-telemetry/spans/dt=2026-03-02/00/")
    # Every object is write-once, so two batches never collide.
    assert all(name.endswith(".jsonl.gz") for name in names)
    assert names[0] != names[1]


def test_object_name_keeps_a_local_zone_out_of_the_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A reader lists `dt=`/`HH` prefixes in UTC, so a naive or non-UTC clock
    # would file a batch under the wrong hour.
    client = _FakeGcsClient()
    tokyo = dt.timezone(dt.timedelta(hours=9))
    monkeypatch.setattr(
        "ambient_quality_agent.telemetry.gcs_span_exporter._read_utc_clock",
        lambda: dt.datetime(2026, 3, 2, 8, 30, tzinfo=tokyo),
    )

    _exporter(client).export([_span()])

    assert client.uploads[0][0].startswith(
        "aqa-telemetry/spans/dt=2026-03-01/23/"
    )


def test_export_reports_failure_when_the_write_raises() -> None:
    # Telemetry must not be able to fail the work it describes: a broken write
    # is a FAILURE result and a log line, never an exception.
    client = _FakeGcsClient(fail=True)

    assert _exporter(client).export([_span()]) is SpanExportResult.FAILURE


def test_an_unserializable_span_does_not_cost_the_batch() -> None:
    client = _FakeGcsClient()
    unserializable = _span(name="bad", attributes={"weird": {"a", "b"}})

    result = _exporter(client).export([unserializable, _span(name="good")])

    assert result is SpanExportResult.SUCCESS
    assert [row["name"] for row in _exported_rows(client)] == ["good"]


def test_a_non_finite_attribute_does_not_cost_the_batch() -> None:
    # NaN and Infinity serialize to bare tokens no JSON parser accepts, so
    # emitting them would break the reader's `bq load` rather than this export.
    client = _FakeGcsClient()
    non_finite = _span(name="bad", attributes={"score": float("nan")})

    result = _exporter(client).export([non_finite, _span(name="good")])

    assert result is SpanExportResult.SUCCESS
    assert [row["name"] for row in _exported_rows(client)] == ["good"]


def test_a_span_that_raises_while_serializing_does_not_cost_the_batch() -> None:
    # Telemetry may never fail the work it observes, so a degenerate span has to
    # be dropped whatever reading it raises -- not only the JSON encoder's
    # TypeError. A span with no status reaches `.status_code` on None.
    client = _FakeGcsClient()
    statusless = _span(name="bad")
    object.__setattr__(statusless, "_status", None)

    result = _exporter(client).export([statusless, _span(name="good")])

    assert result is SpanExportResult.SUCCESS
    assert [row["name"] for row in _exported_rows(client)] == ["good"]


def test_flush_and_shutdown_survive_a_provider_that_cannot_do_either() -> None:
    # A flush at the end of a run is a courtesy to the dump, never a reason for
    # the run to fail, so a provider without these methods is not an error.
    telemetry_setup.flush_spans()
    telemetry_setup.shutdown_spans()


def test_exporter_lifecycle_calls_are_harmless() -> None:
    client = _FakeGcsClient()
    exporter = _exporter(client)

    # Nothing is buffered in the exporter itself, so a flush has nothing to do.
    assert exporter.force_flush() is True
    exporter.shutdown()
    assert exporter.export([_span()]) is SpanExportResult.SUCCESS


# --- round trip: exported rows back through ingestion -------------------------

_INPUT_MESSAGES = json.dumps(
    [
        {
            "role": "user",
            "parts": [{"type": "text", "content": "why did it fail?"}],
        }
    ]
)
_OUTPUT_MESSAGES = json.dumps(
    [
        {
            "role": "assistant",
            "parts": [{"type": "text", "content": "the quota ran out"}],
        }
    ]
)


def _orchestrator_turn_spans() -> list[ReadableSpan]:
    """Constructs spans mirroring the ADK orchestrator hierarchy:
    1. invocation
    2. invoke_agent
    3. call_llm
    4. generate_content

    Mirrors the span tree ADK emits with content environment variables enabled,
    placing message payloads on the ``generate_content`` span.

    Returns:
        List of ReadableSpan instances representing the turn hierarchy.
    """
    return [
        _span(name="invocation", span_id=0x01),
        _span(
            name="invoke_agent aqa_orchestrator",
            span_id=0x02,
            parent_span_id=0x01,
            attributes={
                "gen_ai.agent.name": "aqa_orchestrator",
                "gen_ai.conversation.id": "sess-1",
            },
        ),
        _span(
            name="call_llm",
            span_id=0x03,
            parent_span_id=0x02,
            attributes={"gcp.vertex.agent.session_id": "sess-1"},
        ),
        _span(
            name="generate_content gemini-3.5-flash",
            span_id=0x04,
            parent_span_id=0x03,
            attributes={
                "gen_ai.agent.name": "aqa_orchestrator",
                "gen_ai.conversation.id": "sess-1",
                "gen_ai.input.messages": _INPUT_MESSAGES,
                "gen_ai.output.messages": _OUTPUT_MESSAGES,
                "gen_ai.response.finish_reasons": ("STOP",),
            },
        ),
    ]


def _as_query_span_struct(row: dict[str, Any]) -> dict[str, Any]:
    """Project an exported row the way `CloudOpsFetcher`'s SQL projects a table row.

    The query flattens ``status`` and renders the JSON columns with
    ``TO_JSON_STRING``, so this is what the decode path actually receives.

    Args:
        row: Exported span row dictionary.

    Returns:
        Projected row dictionary with flattened status and stringified JSON columns.
    """
    return {
        "span_id": row["span_id"],
        "parent_span_id": row["parent_span_id"],
        "name": row["name"],
        "end_time": row["end_time"],
        "attributes": json.dumps(row["attributes"]),
        "status_code": row["status"]["code"],
        "status_message": row["status"]["message"],
        "events": json.dumps(row["events"]),
    }


def test_exported_spans_replay_as_conversation_turns() -> None:
    # The feature's end-to-end claim: what the exporter writes, loaded under the
    # shipped schema and read by the existing ingestion path, is still a
    # conversation with its content intact.
    client = _FakeGcsClient()
    _exporter(client).export(_orchestrator_turn_spans())

    rows = _exported_rows(client)
    spans = _decode_spans([_as_query_span_struct(row) for row in rows])
    agent_data = TraceAgentDataConverter().convert([spans])

    assert len(agent_data.turns) == 1
    texts = [
        part.text
        for event in agent_data.turns[0].events
        for part in event.content.parts
        if part.text
    ]
    assert "why did it fail?" in texts
    assert "the quota ran out" in texts


# --- attaching to the tracer provider ----------------------------------------


class _RecordingProvider:
    """A tracer provider that is not the SDK's, and records any attach attempt."""

    def __init__(self) -> None:
        self.processors: list[Any] = []

    def add_span_processor(self, processor: Any) -> None:
        self.processors.append(processor)


def test_attach_is_skipped_without_a_traces_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.delenv("AQA_JOBS_GCS_BUCKET", raising=False)
    monkeypatch.delenv("AQA_TRACES_GCS_BUCKET", raising=False)
    provider = TracerProvider()

    assert telemetry_setup.attach_gcs_exporter(provider) is False


def test_attach_is_skipped_for_a_foreign_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With OTEL_EXPORTER_OTLP_* set, the runtime installs a provider of its own;
    # attaching to it is not this code's business even though it would duck-type.
    monkeypatch.setenv("AQA_TRACES_GCS_BUCKET", "traces-bucket")
    provider = _RecordingProvider()

    assert telemetry_setup.attach_gcs_exporter(provider) is False
    assert provider.processors == []


def test_attach_adds_a_gcs_exporter_to_an_sdk_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    monkeypatch.setenv("AQA_TRACES_GCS_BUCKET", "traces-bucket")
    provider = TracerProvider()
    # Captures constructor arguments because BatchSpanProcessor exposes
    # schedule delay only on a private attribute.
    delays: list[float | None] = []

    class _RecordingBatchSpanProcessor(BatchSpanProcessor):
        def __init__(self, exporter, **kwargs):  # type: ignore[no-untyped-def]
            delays.append(kwargs.get("schedule_delay_millis"))
            super().__init__(exporter, **kwargs)

    monkeypatch.setattr(
        telemetry_setup, "BatchSpanProcessor", _RecordingBatchSpanProcessor
    )

    assert telemetry_setup.attach_gcs_exporter(provider) is True

    processors = provider._active_span_processor._span_processors
    attached = [p for p in processors if isinstance(p, BatchSpanProcessor)]
    assert len(attached) == 1
    assert isinstance(attached[0].span_exporter, GcsSpanExporter)
    # A 30-second window limits object creation (~720/hour at default intervals)
    # to preserve downstream prefix listing performance.
    assert delays == [30_000]
    provider.shutdown()


def test_attach_defaults_the_traces_bucket_to_the_jobs_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Terraform sets no traces bucket on purpose: adding a key to the engine
    # environment recreates the engine.
    from ambient_quality_agent import config as config_module

    monkeypatch.delenv("AQA_TRACES_GCS_BUCKET", raising=False)
    monkeypatch.setenv("AQA_JOBS_GCS_BUCKET", "jobs-bucket")

    assert config_module.load().traces_gcs_bucket == "jobs-bucket"


# --------------------------------------------------------------------------- #
# The version stamped on every exported span                                   #
# --------------------------------------------------------------------------- #
def test_the_deploy_time_version_is_used_when_it_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_VERSION", "3")

    assert telemetry_setup._resolve_service_version() == "3"


@pytest.mark.parametrize("stamped", ["", "0.0.0"])
def test_an_unresolved_version_falls_back_to_the_package_version(
    monkeypatch: pytest.MonkeyPatch, stamped: str
) -> None:
    # agents-cli cannot read this project's `dynamic` version and stamps 0.0.0,
    # which says nothing at all about the code that is running.
    import ambient_quality_agent

    monkeypatch.setenv("AGENT_VERSION", stamped)

    assert (
        telemetry_setup._resolve_service_version()
        == ambient_quality_agent.__version__
    )
