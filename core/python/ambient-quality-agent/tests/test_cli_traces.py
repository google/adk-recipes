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

"""Hermetic unit and integration tests for `agents-cli aqua dump-traces`.

Uses `GcsSpanExporter` to generate realistic test fixtures and validates
in-memory GCS mocking, range filtering, zip archive creation, and CLI execution.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import pathlib
import zipfile
from typing import Any

import pytest
from ambient_quality_agent.telemetry import gcs_span_exporter
from ambient_quality_agent.telemetry.gcs_span_exporter import GcsSpanExporter
from ambient_quality_cli import aqua_cli, traces
from ambient_quality_cli import gcs as gcs_lib
from ambient_quality_shared.telemetry import SPAN_OBJECT_PREFIX
from ambient_quality_shared.terraform_state import (
    DEPLOYMENT_KEY,
    STATE_DIR_NAME,
)
from click.testing import CliRunner
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanContext, TraceFlags
from opentelemetry.trace.status import Status, StatusCode

_BUCKET = "observed-agent-aqua-jobs"
_TRACE_ID = 0x0123456789ABCDEF0123456789ABCDEF

_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "ambient_quality_cli"
    / "all_spans.schema.json"
)


# --------------------------------------------------------------------------- #
# Fixtures: real exporter output, and a bucket of it                           #
# --------------------------------------------------------------------------- #
class _FakeBlob:
    def __init__(self, objects: dict[str, bytes], name: str) -> None:
        self._objects = objects
        self._name = name

    def upload_from_string(
        self, data: bytes, content_type: str | None = None
    ) -> None:
        self._objects[self._name] = data


class _FakeBucket:
    def __init__(self, objects: dict[str, bytes], name: str) -> None:
        self._objects = objects
        self.name = name

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self._objects, name)


class _FakeGcsClient:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self._objects, name)


def _span(
    *,
    agent: str,
    end: dt.datetime,
    span_id: int,
    content: bool = False,
    empty_content: bool = False,
) -> ReadableSpan:
    """Creates a mock OpenTelemetry ReadableSpan ending at `end`.

    Args:
        agent: Value for the `gen_ai.agent.name` attribute.
        end: Span end timestamp.
        span_id: OpenTelemetry span id.
        content: Whether to attach populated conversation content attributes,
            simulating enabled content capture.
        empty_content: Whether to attach empty container placeholders emitted
            when content capture is disabled.

    Returns:
        Configured ReadableSpan instance.
    """
    end_ns = int(end.timestamp() * 1_000_000_000)
    attributes: dict[str, Any] = {"gen_ai.agent.name": agent}
    if content:
        attributes["gen_ai.input.messages"] = json.dumps(
            [{"role": "user", "parts": [{"type": "text", "content": "hello"}]}]
        )
    if empty_content:
        attributes["gcp.vertex.agent.llm_request"] = "{}"
        attributes["gcp.vertex.agent.llm_response"] = "{}"
        attributes["gen_ai.input.messages"] = "[]"
    return ReadableSpan(
        name=f"invoke_agent {agent}",
        context=SpanContext(
            trace_id=_TRACE_ID,
            span_id=span_id,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        ),
        parent=None,
        attributes=attributes,
        events=(),
        status=Status(StatusCode.UNSET),
        start_time=end_ns - 1_000_000_000,
        end_time=end_ns,
    )


def _export(
    objects: dict[str, bytes],
    *,
    written_at: dt.datetime,
    spans: list[ReadableSpan],
    monkeypatch: pytest.MonkeyPatch,
    version: str = "rev-1",
) -> None:
    """Exports a batch of test spans into an in-memory bucket mapping.

    Args:
        objects: In-memory object mapping of bucket contents.
        written_at: Timestamp when spans are exported.
        spans: List of ReadableSpan instances to export.
        monkeypatch: Pytest monkeypatch fixture.
        version: Service version attribute for the exporter.
    """
    monkeypatch.setattr(
        gcs_span_exporter, "_read_utc_clock", lambda: written_at
    )
    GcsSpanExporter(
        bucket=_BUCKET,
        prefix=SPAN_OBJECT_PREFIX,
        resource_attributes={
            "service.name": "ambient_quality_agent",
            "service.version": version,
        },
        client_factory=lambda: _FakeGcsClient(objects),
    ).export(spans)


@pytest.fixture
def bucket_objects(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """Generates three batches of exported test spans spanning a UTC day boundary."""
    objects: dict[str, bytes] = {}
    _export(
        objects,
        written_at=dt.datetime(2026, 3, 1, 23, 10, tzinfo=dt.UTC),
        spans=[
            _span(
                agent="aqa_orchestrator",
                end=dt.datetime(2026, 3, 1, 23, 5, tzinfo=dt.UTC),
                span_id=0x01,
                empty_content=True,
            )
        ],
        monkeypatch=monkeypatch,
    )
    _export(
        objects,
        written_at=dt.datetime(2026, 3, 2, 0, 5, tzinfo=dt.UTC),
        spans=[
            # Ended before midnight, partitioned in the next day's initial hour.
            _span(
                agent="aqa_chat",
                end=dt.datetime(2026, 3, 1, 23, 58, tzinfo=dt.UTC),
                span_id=0x02,
                content=True,
            ),
            _span(
                agent="aqa_orchestrator",
                end=dt.datetime(2026, 3, 2, 0, 3, tzinfo=dt.UTC),
                span_id=0x03,
                content=True,
            ),
        ],
        monkeypatch=monkeypatch,
    )
    _export(
        objects,
        written_at=dt.datetime(2026, 3, 2, 6, 0, tzinfo=dt.UTC),
        spans=[
            _span(
                agent="aqa_metrics",
                end=dt.datetime(2026, 3, 2, 5, 59, tzinfo=dt.UTC),
                span_id=0x04,
                empty_content=True,
            )
        ],
        monkeypatch=monkeypatch,
        version="rev-2",
    )
    return objects


@pytest.fixture
def fake_gcs(monkeypatch: pytest.MonkeyPatch, bucket_objects: dict[str, bytes]):
    """Mocks `gcs.list_objects` and `gcs.stream` against `bucket_objects`."""

    def list_objects(bucket, token, prefix, remediation):
        assert bucket == _BUCKET
        return [
            {"name": name}
            for name in sorted(bucket_objects)
            if name.startswith(prefix)
        ]

    @contextlib.contextmanager
    def stream(bucket, token, name, remediation):
        payload = bucket_objects[name]
        # Stream byte by byte to test chunked decompression and line framing.
        yield (payload[i : i + 1] for i in range(len(payload)))

    monkeypatch.setattr(traces.gcs, "list_objects", list_objects)
    monkeypatch.setattr(traces.gcs, "stream", stream)
    return bucket_objects


def _state_root(
    tmp_path: pathlib.Path, outputs: dict[str, Any] | None
) -> pathlib.Path:
    """Creates a `.aqua/single-project.tfstate` fixture with the given outputs.

    Args:
        tmp_path: Temporary directory path.
        outputs: Optional dictionary of outputs to write into state.

    Returns:
        Path to the `.aqua` state directory.
    """
    root = tmp_path / STATE_DIR_NAME
    root.mkdir(parents=True, exist_ok=True)
    if outputs is not None:
        (root / f"{DEPLOYMENT_KEY}.tfstate").write_text(
            json.dumps(
                {
                    "version": 4,
                    "outputs": {k: {"value": v} for k, v in outputs.items()},
                }
            ),
            encoding="utf-8",
        )
    return root


_BUCKETS_OUTPUT = {
    "buckets": {
        "package": "p",
        "jobs": _BUCKET,
        "source": "s",
        "metrics": "m",
    },
    "ui_service_name": "aqa-ui",
}


# --------------------------------------------------------------------------- #
# Resolving the bucket from Terraform state                                    #
# --------------------------------------------------------------------------- #
def test_the_traces_bucket_is_the_jobs_bucket_terraform_recorded(
    tmp_path,
) -> None:
    # Verify bucket resolution functions offline without active deployment connection.
    assert (
        traces.resolve_traces_bucket(_state_root(tmp_path, _BUCKETS_OUTPUT))
        == _BUCKET
    )


def test_a_missing_state_file_names_the_directory_it_looked_in(
    tmp_path,
) -> None:
    with pytest.raises(traces.DumpError) as caught:
        traces.resolve_traces_bucket(_state_root(tmp_path, None))

    message = str(caught.value)
    assert f"{STATE_DIR_NAME}/" in message
    assert f"{DEPLOYMENT_KEY}.tfstate" in message


def test_state_without_a_buckets_output_says_so(tmp_path) -> None:
    with pytest.raises(traces.DumpError, match="no `buckets` output"):
        traces.resolve_traces_bucket(
            _state_root(tmp_path, {"ui_service_name": "aqa-ui"})
        )


# --------------------------------------------------------------------------- #
# The range and the prefixes it covers                                         #
# --------------------------------------------------------------------------- #
_NOW = dt.datetime(2026, 3, 2, 12, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7d", dt.datetime(2026, 2, 23, 12, 0, tzinfo=dt.UTC)),
        ("36h", dt.datetime(2026, 3, 1, 0, 0, tzinfo=dt.UTC)),
        ("90m", dt.datetime(2026, 3, 2, 10, 30, tzinfo=dt.UTC)),
        ("2026-09-01", dt.datetime(2026, 9, 1, 0, 0, tzinfo=dt.UTC)),
        ("2026-09-01T12:30:00", dt.datetime(2026, 9, 1, 12, 30, tzinfo=dt.UTC)),
        (
            "2026-09-01T12:30:00Z",
            dt.datetime(2026, 9, 1, 12, 30, tzinfo=dt.UTC),
        ),
        # Explicit timezone offsets are converted to UTC.
        (
            "2026-09-01T12:30:00+09:00",
            dt.datetime(2026, 9, 1, 3, 30, tzinfo=dt.UTC),
        ),
    ],
)
def test_parse_moment_reads_durations_and_iso_as_utc(text, expected) -> None:
    assert traces.parse_moment(text, now=_NOW) == expected


def test_parse_moment_rejects_anything_else() -> None:
    with pytest.raises(traces.DumpError, match="neither a duration"):
        traces.parse_moment("last tuesday", now=_NOW)


def test_hour_prefixes_enumerate_utc_hours_across_a_day_boundary() -> None:
    prefixes = traces.build_hour_prefixes(
        traces.TimeRange(
            dt.datetime(2026, 3, 1, 22, 40, tzinfo=dt.UTC),
            dt.datetime(2026, 3, 2, 1, 10, tzinfo=dt.UTC),
        )
    )

    assert prefixes == [
        f"{SPAN_OBJECT_PREFIX}/dt=2026-03-01/22/",
        f"{SPAN_OBJECT_PREFIX}/dt=2026-03-01/23/",
        f"{SPAN_OBJECT_PREFIX}/dt=2026-03-02/00/",
        f"{SPAN_OBJECT_PREFIX}/dt=2026-03-02/01/",
    ]


def test_widening_adds_an_hour_at_each_edge() -> None:
    # Prefix listing widens by one hour to capture spans written in subsequent partitions.
    requested = traces.TimeRange(
        dt.datetime(2026, 3, 2, 3, 0, tzinfo=dt.UTC),
        dt.datetime(2026, 3, 2, 4, 0, tzinfo=dt.UTC),
    )

    assert traces.build_hour_prefixes(requested.widen()) == [
        f"{SPAN_OBJECT_PREFIX}/dt=2026-03-02/02/",
        f"{SPAN_OBJECT_PREFIX}/dt=2026-03-02/03/",
        f"{SPAN_OBJECT_PREFIX}/dt=2026-03-02/04/",
        f"{SPAN_OBJECT_PREFIX}/dt=2026-03-02/05/",
    ]


def test_listing_takes_only_the_objects_in_the_widened_hours(fake_gcs) -> None:
    listed = traces.list_span_objects(
        _BUCKET,
        "token",
        traces.TimeRange(
            dt.datetime(2026, 3, 1, 23, 30, tzinfo=dt.UTC),
            dt.datetime(2026, 3, 2, 0, 30, tzinfo=dt.UTC),
        ).widen(),
    )

    hours = {name.split("/dt=")[1].rsplit("/", 1)[0] for name in listed}
    # Includes partitions from 22:00 (day 1) through 01:00 (day 2); excludes 06:00.
    assert hours == {"2026-03-01/23", "2026-03-02/00"}


# --------------------------------------------------------------------------- #
# The archive                                                                  #
# --------------------------------------------------------------------------- #
def _dump(
    tmp_path: pathlib.Path,
    time_range: traces.TimeRange,
    *,
    extra: dict[str, Any] | None = None,
    location: str = "",
) -> tuple[pathlib.Path, traces.DumpResult]:
    output = tmp_path / "dump.zip"
    result = traces.write_dump(
        output=output,
        bucket=_BUCKET,
        token="token",
        time_range=time_range,
        object_names=traces.list_span_objects(
            _BUCKET, "token", time_range.widen()
        ),
        manifest_extra=lambda _: extra or {},
        progress=lambda _: None,
        location=location,
    )
    return output, result


_WHOLE_RANGE = traces.TimeRange(
    dt.datetime(2026, 3, 1, 0, 0, tzinfo=dt.UTC),
    dt.datetime(2026, 3, 2, 23, 0, tzinfo=dt.UTC),
)


def test_the_archive_carries_spans_schema_manifest_and_readme(
    tmp_path, fake_gcs
) -> None:
    output, _ = _dump(tmp_path, _WHOLE_RANGE)

    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert traces.SCHEMA_ENTRY in names
        assert traces.MANIFEST_ENTRY in names
        assert traces.README_ENTRY in names
        # Verify the schema file matches the canonical schema expected by BigQuery load.
        assert archive.read(traces.SCHEMA_ENTRY) == _SCHEMA_PATH.read_bytes()
        readme = archive.read(traces.README_ENTRY).decode()
    assert "bq load" in readme
    assert "TELEMETRY_INGESTION_SOURCE=cloud_ops" in readme
    assert "AQA_TELEMETRY_TABLE=all_spans" in readme


def test_every_row_lands_in_one_span_entry(tmp_path, fake_gcs) -> None:
    # `bq load` requires a single local file rather than glob patterns.
    output, result = _dump(tmp_path, _WHOLE_RANGE)

    with zipfile.ZipFile(output) as archive:
        span_entries = [n for n in archive.namelist() if n.startswith("spans/")]
        rows = archive.read(traces.SPANS_ENTRY).decode().splitlines()

    assert span_entries == [traces.SPANS_ENTRY]
    assert len(rows) == result.spans == 4
    assert all(json.loads(row)["end_time"] for row in rows)


def test_the_readme_loads_the_entry_the_archive_actually_holds(
    tmp_path, fake_gcs
) -> None:
    # The load command has to work as written: right file, matching locations.
    output, _ = _dump(tmp_path, _WHOLE_RANGE, location="europe-west4")

    with zipfile.ZipFile(output) as archive:
        readme = archive.read(traces.README_ENTRY).decode()

    assert traces.SPANS_ENTRY in readme
    assert "spans/*.jsonl" not in readme
    assert "LOCATION=europe-west4" in readme
    assert readme.count('--location="${LOCATION}"') == 2


def test_the_readme_falls_back_to_a_default_location(
    tmp_path, fake_gcs
) -> None:
    output, _ = _dump(tmp_path, _WHOLE_RANGE)

    with zipfile.ZipFile(output) as archive:
        readme = archive.read(traces.README_ENTRY).decode()

    assert f"LOCATION={traces.DEFAULT_DATASET_LOCATION}" in readme


def test_every_span_is_counted_against_the_surface_that_emitted_it(
    tmp_path, fake_gcs
) -> None:
    _, result = _dump(tmp_path, _WHOLE_RANGE)

    assert result.spans == 4
    assert result.spans_by_agent == {
        "aqa_chat": 1,
        "aqa_metrics": 1,
        "aqa_orchestrator": 2,
    }
    assert sorted(result.agent_versions) == ["rev-1", "rev-2"]


def test_rows_outside_the_exact_range_are_dropped(tmp_path, fake_gcs) -> None:
    # Row filter excludes spans ending outside the requested window despite widened listing.
    output, result = _dump(
        tmp_path,
        traces.TimeRange(
            dt.datetime(2026, 3, 2, 0, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 3, 2, 1, 0, tzinfo=dt.UTC),
        ),
    )

    assert result.spans_by_agent == {"aqa_orchestrator": 1}
    assert _rows(output)[0]["end_time"].startswith("2026-03-02T00:03")


def test_a_row_exactly_on_a_bound_is_kept(tmp_path, fake_gcs) -> None:
    # Range boundaries are inclusive on both ends.
    _, result = _dump(
        tmp_path,
        traces.TimeRange(
            dt.datetime(2026, 3, 1, 23, 5, tzinfo=dt.UTC),
            dt.datetime(2026, 3, 1, 23, 58, tzinfo=dt.UTC),
        ),
    )

    assert result.spans_by_agent == {"aqa_chat": 1, "aqa_orchestrator": 1}


def test_a_dump_that_filters_everything_out_leaves_no_span_entry(
    tmp_path, fake_gcs
) -> None:
    output, result = _dump(
        tmp_path,
        traces.TimeRange(
            dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 1, 2, 0, 0, tzinfo=dt.UTC),
        ),
    )

    assert result.spans == 0
    with zipfile.ZipFile(output) as archive:
        assert [n for n in archive.namelist() if n.startswith("spans/")] == []


def test_a_failed_read_leaves_no_archive_at_the_destination(
    tmp_path, fake_gcs, monkeypatch
) -> None:
    # Incomplete archives must not be left on disk if an error occurs during processing.
    @contextlib.contextmanager
    def refuse(*_args, **_kwargs):
        raise gcs_lib.GcsError("could not reach gs://observed-agent-aqua-jobs")
        yield  # pragma: no cover -- makes this a generator

    monkeypatch.setattr(traces.gcs, "stream", refuse)

    with pytest.raises(gcs_lib.GcsError):
        _dump(tmp_path, _WHOLE_RANGE)

    assert list(tmp_path.iterdir()) == []


def test_a_corrupt_object_is_reported_as_a_dump_failure(
    tmp_path, fake_gcs, monkeypatch
) -> None:
    # Corrupt gzip payloads raise DumpError instead of uncaught zlib exceptions.
    @contextlib.contextmanager
    def not_gzip(*_args, **_kwargs):
        yield iter([b'{"trace_id": "plain json, never compressed"}'])

    monkeypatch.setattr(traces.gcs, "stream", not_gzip)

    with pytest.raises(traces.DumpError, match="not readable gzip"):
        _dump(tmp_path, _WHOLE_RANGE)


def test_a_missing_schema_fails_before_anything_is_downloaded(
    tmp_path, fake_gcs, monkeypatch
) -> None:
    monkeypatch.setattr(
        traces.gcs, "stream", lambda *a, **k: pytest.fail("read the bucket")
    )

    with pytest.raises(traces.DumpError, match="`bq load` schema"):
        traces.write_dump(
            output=tmp_path / "dump.zip",
            bucket=_BUCKET,
            token="token",
            time_range=_WHOLE_RANGE,
            object_names=["aqa-telemetry/spans/dt=2026-03-02/00/x.jsonl.gz"],
            manifest_extra=lambda _: {},
            progress=lambda _: None,
            schema_path=tmp_path / "absent.json",
        )


def test_the_manifest_records_the_requested_and_the_actual_range(
    tmp_path, fake_gcs
) -> None:
    output, _ = _dump(tmp_path, _WHOLE_RANGE, extra={"cli_version": "0.1.0"})

    manifest = _manifest(output)
    assert manifest["range"]["requested"] == _WHOLE_RANGE.to_json()
    assert manifest["range"]["actual"]["first_end_time"].startswith(
        "2026-03-01T23:05"
    )
    assert manifest["range"]["actual"]["last_end_time"].startswith(
        "2026-03-02T05:59"
    )
    assert manifest["source"] == {
        "bucket": _BUCKET,
        "prefix": SPAN_OBJECT_PREFIX,
    }
    assert manifest["counts"]["spans"] == 4
    assert manifest["cli_version"] == "0.1.0"


def _rows(output: pathlib.Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(output) as archive:
        return [
            json.loads(line)
            for name in archive.namelist()
            if name.startswith("spans/")
            for line in archive.read(name).decode().splitlines()
        ]


def _manifest(output: pathlib.Path) -> dict[str, Any]:
    with zipfile.ZipFile(output) as archive:
        return json.loads(archive.read(traces.MANIFEST_ENTRY))


# --------------------------------------------------------------------------- #
# The command                                                                  #
# --------------------------------------------------------------------------- #
@pytest.fixture
def project(tmp_path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """Sets up a temporary working directory with Terraform state fixtures."""
    _state_root(tmp_path, _BUCKETS_OUTPUT)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aqua_cli, "get_adc_token", lambda: "token")
    return tmp_path


class _UnreachableClient:
    async def post_command(self, path, payload=None):
        raise RuntimeError("connection refused")


class _ConfigClient:
    """Answers the config route the way the agent does: wrapped in `config`."""

    def __init__(self, capture_mode: str | None = None) -> None:
        self._capture_mode = capture_mode

    async def post_command(self, path, payload=None):
        config = {"observed_agent_name": "demo", "data_lookback_window": 7}
        if self._capture_mode is not None:
            config["content_capture_mode"] = self._capture_mode
        return {"config": config}


def _invoke(runner: CliRunner, *args: str):
    return runner.invoke(aqua_cli.cli, ["dump-traces", *args])


def test_the_command_writes_an_archive_and_reports_it_as_json(
    project, fake_gcs, monkeypatch
) -> None:
    monkeypatch.setattr(
        aqua_cli, "_build_client", lambda *a, **k: _UnreachableClient()
    )

    result = _invoke(
        CliRunner(),
        "--since",
        "2026-03-01T00:00:00",
        "--until",
        "2026-03-02T23:00:00",
        "--output",
        "dump.zip",
    )

    assert result.exit_code == aqua_cli.EXIT_OK, result.output
    payload = json.loads(result.stdout)
    assert payload["spans"] == 4
    assert payload["spans_by_agent"]["aqa_orchestrator"] == 2
    assert (project / "dump.zip").is_file()


def test_an_unreachable_deployment_is_recorded_not_fatal(
    project, fake_gcs, monkeypatch
) -> None:
    # Unreachable deployments record errors in the manifest without aborting the trace dump.
    monkeypatch.setattr(
        aqua_cli, "_build_client", lambda *a, **k: _UnreachableClient()
    )

    result = _invoke(
        CliRunner(), "--since", "2026-03-01T00:00:00", "--output", "dump.zip"
    )

    assert result.exit_code == aqua_cli.EXIT_OK, result.output
    manifest = _manifest(project / "dump.zip")
    assert manifest["config"] is None
    assert "connection refused" in manifest["config_error"]


def test_an_empty_range_still_writes_a_valid_archive(project, fake_gcs) -> None:
    result = _invoke(
        CliRunner(),
        "--since",
        "2026-01-01T00:00:00",
        "--until",
        "2026-01-02T00:00:00",
        "--output",
        "dump.zip",
    )

    assert result.exit_code == aqua_cli.EXIT_OK, result.output
    assert "No exported spans in that range" in result.stderr
    manifest = _manifest(project / "dump.zip")
    assert manifest["counts"] == {
        "objects_listed": 0,
        "objects_read": 0,
        "spans": 0,
        "spans_by_agent": {},
    }


def test_the_privacy_warning_is_printed_for_every_dump_that_has_spans(
    project, fake_gcs, monkeypatch
) -> None:
    monkeypatch.setattr(
        aqua_cli, "_build_client", lambda *a, **k: _UnreachableClient()
    )

    result = _invoke(
        CliRunner(),
        "--since",
        "2026-03-01T00:00:00",
        "--until",
        "2026-03-02T23:00:00",
        "--output",
        "dump.zip",
    )

    assert "PRIVACY" in result.stderr
    assert "trace_readers" in result.stderr


@pytest.mark.parametrize("mode", ["NO_CONTENT", ""])
def test_a_deployment_with_capture_off_is_called_out_after_the_warning(
    project, fake_gcs, monkeypatch, mode
) -> None:
    # An unset capture variable defaults to NO_CONTENT; config metadata determines
    # whether turns were recorded.
    monkeypatch.setattr(
        aqua_cli,
        "_build_client",
        lambda *a, **k: _ConfigClient(capture_mode=mode),
    )

    result = _invoke(
        CliRunner(), "--since", "2026-03-01T00:00:00", "--output", "dump.zip"
    )

    assert result.exit_code == aqua_cli.EXIT_OK, result.output
    assert "PRIVACY" in result.stderr
    assert "not capturing conversation content" in result.stderr
    assert "replay without turns" in result.stderr
    assert (mode or "unset") in result.stderr


def test_event_only_capture_is_called_out_as_not_reaching_the_spans(
    project, fake_gcs, monkeypatch
) -> None:
    # EVENT_ONLY writes turns to log records rather than span attributes, leaving
    # span archives empty.
    monkeypatch.setattr(
        aqua_cli,
        "_build_client",
        lambda *a, **k: _ConfigClient(capture_mode="EVENT_ONLY"),
    )

    result = _invoke(
        CliRunner(), "--since", "2026-03-01T00:00:00", "--output", "dump.zip"
    )

    assert result.exit_code == aqua_cli.EXIT_OK, result.output
    assert "EVENT_ONLY" in result.stderr
    assert "log records rather than spans" in result.stderr
    assert "SPAN_ONLY or SPAN_AND_EVENT" in result.stderr


@pytest.mark.parametrize("mode", ["SPAN_ONLY", "SPAN_AND_EVENT"])
def test_a_deployment_with_capture_on_gets_no_extra_notice(
    project, fake_gcs, monkeypatch, mode
) -> None:
    monkeypatch.setattr(
        aqua_cli,
        "_build_client",
        lambda *a, **k: _ConfigClient(capture_mode=mode),
    )

    result = _invoke(
        CliRunner(), "--since", "2026-03-01T00:00:00", "--output", "dump.zip"
    )

    assert result.exit_code == aqua_cli.EXIT_OK, result.output
    assert "PRIVACY" in result.stderr
    assert "replay without turns" not in result.stderr


def test_an_unreadable_config_says_nothing_about_capture(
    project, fake_gcs, monkeypatch
) -> None:
    # The manifest already records that the deployment was unreachable, so the
    # dump must not guess at a setting it never read.
    monkeypatch.setattr(
        aqua_cli, "_build_client", lambda *a, **k: _UnreachableClient()
    )

    result = _invoke(
        CliRunner(), "--since", "2026-03-01T00:00:00", "--output", "dump.zip"
    )

    assert result.exit_code == aqua_cli.EXIT_OK, result.output
    assert "replay without turns" not in result.stderr


def test_a_missing_state_file_fails_before_any_bucket_call(
    project, monkeypatch
) -> None:
    (project / STATE_DIR_NAME / f"{DEPLOYMENT_KEY}.tfstate").unlink()

    result = _invoke(CliRunner(), "--output", "dump.zip")

    assert result.exit_code == aqua_cli.EXIT_ERROR
    assert STATE_DIR_NAME in result.stderr
    assert not (project / "dump.zip").exists()


def test_an_inverted_range_is_refused(project) -> None:
    result = _invoke(
        CliRunner(),
        "--since",
        "2026-03-02T00:00:00",
        "--until",
        "2026-03-01T00:00:00",
    )

    assert result.exit_code == aqua_cli.EXIT_ERROR
    assert "is after --until" in result.stderr


# --------------------------------------------------------------------------- #
# Shared bucket access: one implementation, two remediations                   #
# --------------------------------------------------------------------------- #
class _Response:
    def __init__(self, status: int) -> None:
        self.status_code = status
        self.text = "{}"

    def json(self) -> dict:
        return {}


def test_each_caller_gets_its_own_remediation() -> None:
    # Verifies each caller provides its own context-specific remediation for GCS errors.
    with pytest.raises(gcs_lib.GcsError, match="trace_readers"):
        gcs_lib.raise_for_status(_Response(403), _BUCKET, traces.REMEDIATION)

    from ambient_quality_cli import metrics as metrics_lib

    with pytest.raises(metrics_lib.ValidationError, match="metrics_writers"):
        metrics_lib._raise_for_gcs(_Response(403), _BUCKET)


def test_the_manifest_records_the_config_without_nesting_it_twice(
    project, fake_gcs, monkeypatch
) -> None:
    # Unpack route response `{"config": {...}}` so settings are stored at top-level under `config`.
    monkeypatch.setattr(
        aqua_cli, "_build_client", lambda *a, **k: _ConfigClient()
    )

    result = _invoke(
        CliRunner(), "--since", "2026-03-01T00:00:00", "--output", "dump.zip"
    )

    assert result.exit_code == aqua_cli.EXIT_OK, result.output
    manifest = _manifest(project / "dump.zip")
    assert manifest["config"] == {
        "observed_agent_name": "demo",
        "data_lookback_window": 7,
    }
    assert "config_error" not in manifest


def test_the_config_query_is_announced_before_the_span_progress(
    project, fake_gcs, monkeypatch
) -> None:
    # Query configuration before listing spans so progress output remains uninterrupted.
    monkeypatch.setattr(
        aqua_cli, "_build_client", lambda *a, **k: _ConfigClient()
    )

    result = _invoke(
        CliRunner(),
        "--since",
        "2026-03-01T00:00:00",
        "--until",
        "2026-03-02T23:00:00",
        "--output",
        "dump.zip",
    )

    stderr = result.stderr
    assert "Collecting the effective config for the manifest" in stderr
    assert stderr.index("effective config") < stderr.index("object(s) to read")
    assert stderr.index("object(s) to read") < stderr.index("[1/3]")


def test_the_manifest_says_what_the_agent_versions_actually_are(
    tmp_path, fake_gcs
) -> None:
    # `service.version` reads like an Agent Runtime revision to whoever replays
    # the dump, and is not one.
    output, _ = _dump(tmp_path, _WHOLE_RANGE)

    manifest = _manifest(output)
    assert manifest["agent_versions"] == ["rev-1", "rev-2"]
    assert "not an Agent Runtime deployment" in manifest["agent_versions_note"]


def test_the_engine_location_is_read_off_the_resource_name() -> None:
    assert (
        aqua_cli._extract_engine_location(
            "projects/p/locations/europe-west4/reasoningEngines/1"
        )
        == "europe-west4"
    )
    assert aqua_cli._extract_engine_location("not-a-resource-name") == ""
