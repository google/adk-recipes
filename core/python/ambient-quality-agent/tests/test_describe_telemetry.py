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

"""Tests for the `describe_telemetry` chat tool and schema reader.

Replaces `effective_config.load` with a mutable stub to simulate runtime
`agents-cli aqua attach` updates, and uses a fake BigQuery client to record
metadata reads.
"""

from __future__ import annotations

import asyncio
import logging
import types
from typing import Any

import pytest
from ambient_quality_agent.config import (
    ALLOWED_TELEMETRY_INGESTION_SOURCES,
    DEFAULT_TELEMETRY_TABLES,
    Config,
)
from ambient_quality_agent.core import custom_investigation_skill, skill_toolset
from ambient_quality_agent.tools.ingestion.base import BigQueryJobFetcher
from ambient_quality_agent.tools.ingestion.bigquery_fetcher import (
    BigQueryFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_logging_fetcher import (
    CloudLoggingFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_ops_fetcher import (
    CloudOpsFetcher,
)
from ambient_quality_agent.tools.observed_agent_config import effective_config
from ambient_quality_agent.tools.telemetry import describe, schema
from ambient_quality_shared.usage_headers import (
    build_product_token,
    detect_platform,
)
from google.adk.tools.function_tool import FunctionTool
from google.api_core import exceptions as api_exceptions
from google.cloud import bigquery

from .conftest import StateContext, load_schema_fixture, make_config

_SCHEMA = [
    bigquery.SchemaField(
        "timestamp", "TIMESTAMP", mode="REQUIRED", description="When."
    ),
    bigquery.SchemaField("session_id", "STRING"),
    bigquery.SchemaField(
        "content_parts",
        "RECORD",
        mode="REPEATED",
        fields=[
            bigquery.SchemaField("text", "STRING"),
            bigquery.SchemaField(
                "object_ref",
                "RECORD",
                fields=[bigquery.SchemaField("uri", "STRING")],
            ),
        ],
    ),
]


class _Table:
    """Minimal `bigquery.Table` stand-in exposing `schema`.

    Args:
        fields: Schema fields for the fake table.
    """

    def __init__(self, fields: list[bigquery.SchemaField]) -> None:
        self.schema = fields


class _Client:
    """Fake BigQuery client that records `get_table` calls.

    Args:
        error: Optional exception raised by `get_table`.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = 0

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_: object) -> None:
        self.closed += 1

    def get_table(self, table_ref: str, **kwargs: Any) -> _Table:
        """Records the call and returns the canned schema or raises `self.error`.

        Args:
            table_ref: Fully-qualified table reference.
            **kwargs: Request options passed to `get_table`.

        Returns:
            Fake table holding `_SCHEMA`.
        """
        self.calls.append((table_ref, kwargs))
        if self.error is not None:
            raise self.error
        return _Table(_SCHEMA)


class _Attached:
    """Mutable effective-configuration stub for simulating attach changes.

    Args:
        config: Initial configuration returned by `load`.
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    def load(self, _state: Any) -> Config:
        """Returns the currently attached `Config`.

        Args:
            _state: Unused session state.

        Returns:
            Current `Config` instance.
        """
        return self.config


def _build_config(source: str, **overrides: Any) -> Config:
    """Builds a configuration reading a telemetry source's default table.

    Args:
        source: Telemetry source key.
        **overrides: Field values overriding the source defaults.

    Returns:
        A configured `Config`.
    """
    fields: dict[str, Any] = {
        "telemetry_ingestion_source": source,
        "telemetry_dataset": f"dataset_{source}",
        "telemetry_table": DEFAULT_TELEMETRY_TABLES[source],
    }
    fields.update(overrides)
    return make_config(**fields)


_EXPECTED_TABLES = {
    "big_query": "test-project.dataset_big_query.agent_events",
    "cloud_ops": "test-project.dataset_cloud_ops._AllSpans",
    "cloud_logging": "test-project.dataset_cloud_logging.completions_view",
}
"""The table each source's default configuration lets a selector read."""


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _Client:
    """Serves every metadata read from one fake client."""
    fake = _Client()
    monkeypatch.setattr(describe, "client_factory", lambda _config: fake)
    return fake


@pytest.fixture
def attached(monkeypatch: pytest.MonkeyPatch) -> _Attached:
    """Resolves the effective config from a stub starting on `big_query`."""
    stub = _Attached(_build_config("big_query"))
    monkeypatch.setattr(effective_config, "load", stub.load)
    return stub


def _run_describe_telemetry() -> dict[str, Any]:
    """Calls the tool the way ADK does, with a context carrying session state.

    Returns:
        The tool's response.
    """
    return asyncio.run(describe.describe_telemetry(StateContext()))  # type: ignore[arg-type]


def _read_skill_resource(args: dict[str, str]) -> dict[str, Any]:
    """Calls the chat agent's `load_skill_resource` tool with `args`.

    Args:
        args: The tool's arguments, as the model would pass them.

    Returns:
        The tool's response.
    """
    toolset = skill_toolset.build_skill_toolset()
    tools = asyncio.run(toolset.get_tools())
    load_resource = next(t for t in tools if t.name == "load_skill_resource")
    context = types.SimpleNamespace(invocation_id="inv-1", state={})
    return asyncio.run(
        load_resource.run_async(args=args, tool_context=context)  # type: ignore[arg-type]
    )


# --- what the tool reports ------------------------------------------------- #


@pytest.mark.parametrize("source", sorted(ALLOWED_TELEMETRY_INGESTION_SOURCES))
def test_the_answer_follows_the_effective_config(
    source: str, client: _Client, attached: _Attached
) -> None:
    attached.config = _build_config(source)

    result = _run_describe_telemetry()

    table = _EXPECTED_TABLES[source]
    assert result["source"] == source
    assert result["table"] == table
    assert result["selector_recipes"] == {
        "skill_name": custom_investigation_skill.SKILL_NAME,
        "file_path": f"references/{source}.md",
    }
    assert [ref for ref, _ in client.calls] == [table]
    assert "error" not in result


def test_an_attach_between_calls_moves_the_table_that_is_read(
    client: _Client, attached: _Attached
) -> None:
    first = _run_describe_telemetry()
    attached.config = _build_config(
        "cloud_logging", telemetry_table="custom_inference_sink"
    )

    second = _run_describe_telemetry()

    assert first["source"] == "big_query"
    assert second["source"] == "cloud_logging"
    assert second["table"] == _EXPECTED_TABLES["cloud_logging"]
    assert (
        second["selector_recipes"]["file_path"] == "references/cloud_logging.md"
    )
    assert [ref for ref, _ in client.calls] == [first["table"], second["table"]]


def test_a_table_override_reaches_the_reported_and_read_table(
    client: _Client, attached: _Attached
) -> None:
    attached.config = _build_config(
        "big_query", telemetry_table="custom_events"
    )

    result = _run_describe_telemetry()

    assert result["table"] == "test-project.dataset_big_query.custom_events"
    assert [ref for ref, _ in client.calls] == [result["table"]]


def test_the_default_client_targets_aquas_project_and_the_telemetry_location(
    monkeypatch: pytest.MonkeyPatch, usage_analytics_on
) -> None:
    built: list[dict[str, Any]] = []
    monkeypatch.setattr(
        describe.bigquery, "Client", lambda **kwargs: built.append(kwargs)
    )

    describe._create_default_client(
        _build_config(
            "cloud_ops", project_id="aqua-project", telemetry_location="EU"
        )
    )

    assert len(built) == 1
    assert built[0]["project"] == "aqua-project"
    assert built[0]["location"] == "EU"
    assert built[0]["client_info"].user_agent == build_product_token(
        platform=detect_platform()
    )


def test_the_client_is_built_from_the_effective_config(
    monkeypatch: pytest.MonkeyPatch, attached: _Attached
) -> None:
    attached.config = _build_config("cloud_ops", project_id="attached-project")
    seen: list[Config] = []
    fake = _Client()

    def factory(config: Config) -> _Client:
        seen.append(config)
        return fake

    monkeypatch.setattr(describe, "client_factory", factory)

    _run_describe_telemetry()

    assert seen == [attached.config]
    assert fake.closed == 1


def test_nested_record_fields_are_reported(
    client: _Client, attached: _Attached
) -> None:
    del client, attached

    columns = _run_describe_telemetry()["columns"]

    assert columns == [
        {
            "name": "timestamp",
            "type": "TIMESTAMP",
            "mode": "REQUIRED",
            "description": "When.",
        },
        {"name": "session_id", "type": "STRING", "mode": "NULLABLE"},
        {
            "name": "content_parts",
            "type": "RECORD",
            "mode": "REPEATED",
            "fields": [
                {"name": "text", "type": "STRING", "mode": "NULLABLE"},
                {
                    "name": "object_ref",
                    "type": "RECORD",
                    "mode": "NULLABLE",
                    "fields": [
                        {"name": "uri", "type": "STRING", "mode": "NULLABLE"}
                    ],
                },
            ],
        },
    ]


def test_every_call_reads_the_live_table(
    client: _Client, attached: _Attached
) -> None:
    del attached

    _run_describe_telemetry()
    _run_describe_telemetry()

    assert len(client.calls) == 2


def test_the_metadata_read_is_bounded_by_the_timeout(
    client: _Client, attached: _Attached
) -> None:
    del attached

    _run_describe_telemetry()

    (_, kwargs), *_ = client.calls
    assert kwargs["timeout"] == describe.SCHEMA_READ_TIMEOUT_SECONDS
    assert kwargs["retry"].timeout == describe.SCHEMA_READ_TIMEOUT_SECONDS


# --- failures -------------------------------------------------------------- #


def test_a_failed_read_still_names_the_table_and_the_recipes(
    monkeypatch: pytest.MonkeyPatch,
    attached: _Attached,
    caplog: pytest.LogCaptureFixture,
) -> None:
    attached.config = _build_config("cloud_ops")
    failing = _Client(
        error=api_exceptions.NotFound(
            "Not found: Table test-project:dataset_cloud_ops._AllSpans",
            details=["DebugInfo: at com.google.SERVER_STACK_TRACE"],
        )
    )
    monkeypatch.setattr(describe, "client_factory", lambda _config: failing)

    with caplog.at_level(logging.WARNING, logger=describe.__name__):
        result = _run_describe_telemetry()

    table = _EXPECTED_TABLES["cloud_ops"]
    assert result["source"] == "cloud_ops"
    assert result["table"] == table
    assert result["selector_recipes"]["file_path"] == "references/cloud_ops.md"
    assert result["error"].startswith(f"Could not read the schema of {table}:")
    assert (
        "Not found: Table test-project:dataset_cloud_ops._AllSpans"
        in result["error"]
    )
    assert "SERVER_STACK_TRACE" not in result["error"]
    assert "columns" not in result
    assert failing.closed == 1
    assert any(record.exc_info for record in caplog.records)


def test_an_api_error_without_a_message_still_reports_its_status(
    monkeypatch: pytest.MonkeyPatch, attached: _Attached
) -> None:
    del attached
    failing = _Client(error=api_exceptions.ServiceUnavailable(""))
    monkeypatch.setattr(describe, "client_factory", lambda _config: failing)

    result = _run_describe_telemetry()

    assert result["error"].endswith(": 503")


def test_a_failed_read_that_is_not_an_api_error_keeps_its_text(
    monkeypatch: pytest.MonkeyPatch, attached: _Attached
) -> None:
    del attached
    failing = _Client(error=OSError("connection reset"))
    monkeypatch.setattr(describe, "client_factory", lambda _config: failing)

    result = _run_describe_telemetry()

    assert result["error"].endswith(": connection reset")


def test_a_failed_config_load_is_reported(
    monkeypatch: pytest.MonkeyPatch, client: _Client
) -> None:
    def fail(_state: Any) -> Config:
        raise RuntimeError("object store unavailable")

    monkeypatch.setattr(effective_config, "load", fail)

    result = _run_describe_telemetry()

    assert set(result) == {"error"}
    assert "object store unavailable" in result["error"]
    assert client.calls == []


# --- the contract with the model and the skill ----------------------------- #


def test_the_model_sees_a_tool_without_arguments() -> None:
    """ADK supplies `tool_context`, so the declaration must take nothing."""
    declaration = FunctionTool(describe.describe_telemetry)._get_declaration()

    assert declaration is not None
    assert declaration.name == "describe_telemetry"
    assert not (declaration.parameters and declaration.parameters.properties)


@pytest.mark.parametrize("source", sorted(ALLOWED_TELEMETRY_INGESTION_SOURCES))
def test_the_reported_recipes_load_through_load_skill_resource(
    source: str, client: _Client, attached: _Attached
) -> None:
    del client
    attached.config = _build_config(source)

    loaded = _read_skill_resource(_run_describe_telemetry()["selector_recipes"])

    assert "error" not in loaded
    assert "## The selector table: `SELECTOR_TABLE`" in loaded["content"]


# --- the schema reader ----------------------------------------------------- #


def test_the_schema_reader_raises_rather_than_hiding_a_failure() -> None:
    failing = _Client(error=api_exceptions.Forbidden("denied"))

    with pytest.raises(api_exceptions.Forbidden):
        schema.load_telemetry_schema(
            "p.d.t",
            client=failing,  # type: ignore[arg-type]
            timeout=1.0,
        )


def test_an_empty_description_and_no_nested_fields_are_left_out() -> None:
    column = schema.TelemetryColumn(
        name="a", field_type="STRING", mode="NULLABLE"
    )

    assert column.to_payload() == {
        "name": "a",
        "type": "STRING",
        "mode": "NULLABLE",
    }


def test_the_table_is_in_the_agents_project_when_it_names_one(
    client: _Client, attached: _Attached
) -> None:
    attached.config = _build_config(
        "big_query",
        observed_project_id="agent-project",
        telemetry_table="custom_events",
    )

    result = _run_describe_telemetry()

    assert result["table"] == "agent-project.dataset_big_query.custom_events"
    assert [ref for ref, _ in client.calls] == [result["table"]]


@pytest.mark.parametrize(
    "fetcher", [BigQueryFetcher, CloudLoggingFetcher, CloudOpsFetcher]
)
def test_every_fetcher_names_a_schema_file_that_loads(
    fetcher: type[BigQueryJobFetcher],
) -> None:
    assert schema.load_required_schema(fetcher.SCHEMA_FILE)


@pytest.mark.parametrize(
    ("fixture", "schema_file"),
    [
        ("agent_events.json", BigQueryFetcher.SCHEMA_FILE),
        ("all_spans_view.json", CloudOpsFetcher.SCHEMA_FILE),
    ],
)
def test_a_table_its_source_writes_has_every_column_ingestion_reads(
    fixture: str, schema_file: str
) -> None:
    mismatches = schema.list_schema_mismatches(
        load_schema_fixture(fixture), schema.load_required_schema(schema_file)
    )

    assert mismatches == {}


def test_a_table_of_another_source_misses_the_columns_ingestion_reads() -> None:
    spans = load_schema_fixture("all_spans_view.json")

    mismatches = schema.list_schema_mismatches(
        spans, schema.load_required_schema(BigQueryFetcher.SCHEMA_FILE)
    )

    assert mismatches == {
        "timestamp": "missing",
        "event_type": "missing",
        "agent": "missing",
        "session_id": "missing",
        "invocation_id": "missing",
        "content": "missing",
        "error_message": "missing",
    }


def test_a_column_of_another_type_names_both_types() -> None:
    mismatches = schema.list_schema_mismatches(
        [bigquery.SchemaField("attributes", "STRING")],
        [bigquery.SchemaField("attributes", "JSON")],
    )

    assert mismatches == {"attributes": "STRING, expected JSON"}


def test_a_field_of_a_record_is_named_by_its_path() -> None:
    mismatches = schema.list_schema_mismatches(
        [
            bigquery.SchemaField(
                "status",
                "RECORD",
                fields=[bigquery.SchemaField("message", "STRING")],
            )
        ],
        [
            bigquery.SchemaField(
                "status",
                "RECORD",
                fields=[
                    bigquery.SchemaField("code", "INTEGER"),
                    bigquery.SchemaField("message", "STRING"),
                ],
            )
        ],
    )

    assert mismatches == {"status.code": "missing"}


def test_a_column_is_repeated_exactly_when_the_reader_wants_it() -> None:
    mismatches = schema.list_schema_mismatches(
        [
            bigquery.SchemaField("events", "RECORD"),
            bigquery.SchemaField("attributes", "JSON", mode="REPEATED"),
        ],
        [
            bigquery.SchemaField("events", "RECORD", mode="REPEATED"),
            bigquery.SchemaField("attributes", "JSON"),
        ],
    )

    assert mismatches == {"events": "not REPEATED", "attributes": "REPEATED"}


def test_names_ignore_case_and_type_aliases_equal_their_legacy_names() -> None:
    """BigQuery column names are case-insensitive, and its API can return either type spelling."""
    mismatches = schema.list_schema_mismatches(
        [
            bigquery.SchemaField("Timestamp", "TIMESTAMP"),
            bigquery.SchemaField(
                "status",
                "STRUCT",
                fields=[bigquery.SchemaField("code", "INT64")],
            ),
        ],
        [
            bigquery.SchemaField("timestamp", "TIMESTAMP"),
            bigquery.SchemaField(
                "status",
                "RECORD",
                fields=[bigquery.SchemaField("code", "INTEGER")],
            ),
        ],
    )

    assert mismatches == {}


def test_extra_columns_and_a_required_mode_do_not_count() -> None:
    mismatches = schema.list_schema_mismatches(
        [
            bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
            bigquery.SchemaField("event_id", "STRING"),
        ],
        [bigquery.SchemaField("timestamp", "TIMESTAMP")],
    )

    assert mismatches == {}


def test_a_similar_name_is_not_the_same_column() -> None:
    """The Cloud Trace view's span_id is not the log sink's spanId."""
    mismatches = schema.list_schema_mismatches(
        [bigquery.SchemaField("span_id", "STRING")],
        [bigquery.SchemaField("spanId", "STRING")],
    )

    assert mismatches == {"spanId": "missing"}


@pytest.mark.parametrize(
    ("held", "wanted"),
    [
        ("INT64", "INTEGER"),
        ("INTEGER", "INT64"),
        ("FLOAT64", "FLOAT"),
        ("FLOAT", "FLOAT64"),
        ("BOOL", "BOOLEAN"),
        ("BOOLEAN", "BOOL"),
        ("STRUCT", "RECORD"),
        ("RECORD", "STRUCT"),
    ],
)
def test_either_spelling_of_a_type_matches_on_either_side(
    held: str, wanted: str
) -> None:
    """all_spans.schema.json spells status.code INT64 where the API says
    INTEGER."""
    mismatches = schema.list_schema_mismatches(
        [bigquery.SchemaField("column", held)],
        [bigquery.SchemaField("column", wanted)],
    )

    assert mismatches == {}


def test_a_record_held_as_another_type_is_reported_once() -> None:
    mismatches = schema.list_schema_mismatches(
        [bigquery.SchemaField("status", "JSON")],
        [
            bigquery.SchemaField(
                "status",
                "RECORD",
                fields=[bigquery.SchemaField("code", "INTEGER")],
            )
        ],
    )

    assert mismatches == {"status": "JSON, expected RECORD"}


def test_a_field_of_a_repeated_record_is_named_by_its_path() -> None:
    mismatches = schema.list_schema_mismatches(
        [
            bigquery.SchemaField(
                "events",
                "RECORD",
                mode="REPEATED",
                fields=[bigquery.SchemaField("time", "STRING")],
            )
        ],
        [
            bigquery.SchemaField(
                "events",
                "RECORD",
                mode="REPEATED",
                fields=[bigquery.SchemaField("time", "TIMESTAMP")],
            )
        ],
    )

    assert mismatches == {"events.time": "STRING, expected TIMESTAMP"}


def test_a_type_mismatch_is_reported_before_a_mode_mismatch() -> None:
    mismatches = schema.list_schema_mismatches(
        [bigquery.SchemaField("attributes", "STRING", mode="REPEATED")],
        [bigquery.SchemaField("attributes", "JSON")],
    )

    assert mismatches == {"attributes": "STRING, expected JSON"}


def test_an_unknown_schema_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        schema.load_required_schema("no_such_table.schema.json")
