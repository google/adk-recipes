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

"""`attach --observed-agent-resource`: discovery from the observed engine.

Google's REST APIs are faked at the `JsonApi` seam, answering by URL with the
shapes the fishfood deployment `it-support-agent` returned. The deployment's
side of `attach` is `tests/test_aqua_cli_attach.py`.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

import pytest
from ambient_quality_cli import aqua_cli
from ambient_quality_cli import discovery as discovery_lib
from ambient_quality_cli import rest_client as rest_client_lib
from ambient_quality_cli import trace_link as trace_link_lib
from ambient_quality_cli.discovery import DiscoveredValue
from ambient_quality_cli.rest_client import ApiError
from click.testing import CliRunner

_PROJECT = "my-project"
_NUMBER = "123"
_ENGINE = f"projects/{_NUMBER}/locations/us-east1/reasoningEngines/42"
_ENGINE_URL = f"https://us-east1-aiplatform.googleapis.com/v1/{_ENGINE}"
_LIST_APPS = (
    "https://us-east1-aiplatform.googleapis.com/reasoningEngines/v1/"
    f"{_ENGINE}/api/list-apps"
)
_SINKS = f"https://logging.googleapis.com/v2/projects/{_NUMBER}/sinks"
_RESOLVE_NUMBER = (
    f"https://cloudresourcemanager.googleapis.com/v3/projects/{_NUMBER}"
)
_BQ = "https://bigquery.googleapis.com/bigquery/v2"
_OBSERVABILITY = (
    f"https://observability.googleapis.com/v1/projects/{_PROJECT}/locations"
)
_BUCKETS = f"{_OBSERVABILITY}/-/buckets"
_SETTINGS = f"{_OBSERVABILITY}/global/settings"
_TEST_PERMISSIONS = (
    "https://cloudresourcemanager.googleapis.com/v3/projects/"
    f"{_PROJECT}:testIamPermissions"
)
_API_DISABLED = ApiError(
    403, "Observability API has not been used", "SERVICE_DISABLED"
)
_TABLE = discovery_lib.ENGINE_LOG_TABLE
_SINK_FILTER = (
    'labels."event.name"="gen_ai.client.inference.operation.details" AND '
    'labels."gen_ai.input.messages_ref" =~ ".*it-support-agent.*"'
)
_LOG_SCHEMA = {
    "fields": [
        {"name": "timestamp", "type": "TIMESTAMP"},
        {
            "name": "labels",
            "type": "RECORD",
            "fields": [{"name": "gen_ai_agent_name"}, {"name": "event_name"}],
        },
        {
            "name": "resource",
            "type": "RECORD",
            "fields": [
                {"name": "type"},
                {
                    "name": "labels",
                    "type": "RECORD",
                    "fields": [{"name": "reasoning_engine_id"}],
                },
            ],
        },
    ]
}


class _FakeApi:
    """Answers GETs and POSTs by URL and records them."""

    def __init__(self, responses: dict[str, Any]):
        """Initializes the fake.

        Args:
            responses: Reply per URL: a dict, a callable taking the request
                body, or an `ApiError` to raise.
        """
        self.responses = responses
        self.calls: list[tuple[str, Any]] = []

    def get_json(self, url, params=None):
        """Answers a GET.

        Args:
            url: Request URL.
            params: Query parameters.

        Returns:
            The scripted reply.
        """
        return self._answer(url, params)

    def post_json(self, url, body):
        """Answers a POST.

        Args:
            url: Request URL.
            body: Request body.

        Returns:
            The scripted reply.
        """
        return self._answer(url, body)

    def _answer(self, url, payload):
        """Records a call and returns its scripted reply.

        Args:
            url: Request URL.
            payload: Query parameters or request body.

        Returns:
            The scripted reply.

        Raises:
            ApiError: For a URL with no reply, or one scripted to fail.
        """
        self.calls.append((url, payload))
        if url not in self.responses:
            raise ApiError(404, f"no fake for {url}")
        reply = self.responses[url]
        if isinstance(reply, ApiError):
            raise reply
        return reply(payload) if callable(reply) else reply

    def list_urls(self) -> list[str]:
        """Lists the URLs called, in order.

        Returns:
            The URLs.
        """
        return [url for url, _ in self.calls]


def _build_query_reply(
    rows: list[tuple[str | None, str | None, int]],
) -> dict[str, Any]:
    """Builds a `jobs.query` reply with ``rows``.

    Args:
        rows: `(engine, agent, n)` triples.

    Returns:
        The reply body.
    """
    return {
        "jobComplete": True,
        "rows": [
            {"f": [{"v": engine}, {"v": agent}, {"v": str(n)}]}
            for engine, agent, n in rows
        ],
    }


def _build_sink(
    name: str, dataset: str, project: str = _PROJECT
) -> dict[str, Any]:
    """Builds a logging sink exporting inference events to BigQuery.

    Args:
        name: Sink name.
        dataset: Destination dataset.
        project: The dataset's project.

    Returns:
        The sink as `sinks.list` returns it.
    """
    return {
        "name": name,
        "destination": (
            f"bigquery.googleapis.com/projects/{project}/datasets/{dataset}"
        ),
        "filter": _SINK_FILTER,
    }


def _serve_dataset(
    responses: dict[str, Any],
    dataset: str,
    rows: list[tuple[str | None, str | None, int]] | None,
    *,
    project: str = _PROJECT,
    location: str = "us-east1",
) -> None:
    """Adds a sink dataset holding the engine log table to ``responses``.

    Args:
        responses: Replies by URL, changed in place.
        dataset: Dataset name.
        rows: The table's rows by engine and agent; None for no table yet.
        project: The dataset's project.
        location: The dataset's location.
    """
    base = f"{_BQ}/projects/{project}/datasets/{dataset}"
    responses[base] = {"location": location}
    tables = [
        {"tableReference": {"tableId": "completions"}, "type": "EXTERNAL"},
        {"tableReference": {"tableId": "completions_view"}, "type": "VIEW"},
    ]
    if rows is not None:
        tables.append({"tableReference": {"tableId": _TABLE}, "type": "TABLE"})
        responses[f"{base}/tables/{_TABLE}"] = {"schema": _LOG_SCHEMA}
    responses[f"{base}/tables"] = {"tables": tables}
    queries = f"{_BQ}/projects/{project}/queries"
    by_dataset = responses.setdefault("_queries", {})
    by_dataset[dataset] = rows or []

    def answer(body):
        for name, dataset_rows in by_dataset.items():
            if f".{name}." in body["query"]:
                return _build_query_reply(dataset_rows)
        raise AssertionError(body["query"])

    responses[queries] = answer


def _build_responses(**overrides: Any) -> dict[str, Any]:
    """Builds the replies of a deployment like `it-support-agent`.

    Its project's Observability API is off and the user may not enable it, so
    discovery reads the logging sink.

    Args:
        **overrides: Replies to replace, by URL.

    Returns:
        Replies by URL.
    """
    responses: dict[str, Any] = {
        _ENGINE_URL: {
            "name": _ENGINE,
            "displayName": "it-support-agent",
            "spec": {
                "deploymentSpec": {
                    "env": [
                        {"name": "OTEL_SERVICE_NAME", "value": "x"},
                        {
                            "name": "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH",
                            "value": f"gs://{_PROJECT}-logs/completions",
                        },
                    ]
                }
            },
        },
        _LIST_APPS: {"apps": [{"name": "app", "rootAgentName": "it_agent"}]},
        _RESOLVE_NUMBER: {"name": f"projects/{_NUMBER}", "projectId": _PROJECT},
        _SINKS: {
            "sinks": [
                _build_sink("it-support-agent-genai-logs", "it_telemetry"),
                {
                    "name": "_Default",
                    "destination": "logging.googleapis.com/projects/p/buckets/x",
                    "filter": "NOT LOG_ID(x)",
                },
            ]
        },
        _BUCKETS: _API_DISABLED,
        _TEST_PERMISSIONS: {},
    }
    _serve_dataset(responses, "it_telemetry", [("42", "it_agent", 3)])
    responses.update(overrides)
    return responses


def _discover(api: _FakeApi, given=None):
    """Discovers the fake engine over a 7-day window.

    Args:
        api: Fake REST client.
        given: Fields the operator set.

    Returns:
        What discovery found.
    """
    return discovery_lib.discover_observed_agent(
        _ENGINE, api=api, given=given or {}, window_days=7
    )


def _extract_values(discovery) -> dict[str, Any]:
    """Extracts the discovered values without their sources.

    Args:
        discovery: What discovery found.

    Returns:
        Values by field.
    """
    return {field: v.value for field, v in discovery.values.items()}


def _list_telemetry_fields(discovery) -> list[str]:
    """Lists the discovered telemetry fields a chosen sink or trace link sets.

    The payload bucket comes from the engine's environment, sink or not.

    Args:
        discovery: What discovery found.

    Returns:
        Field names.
    """
    return [
        f
        for f in discovery.values
        if f.startswith("telemetry_") and f != "telemetry_payload_bucket"
    ]


# --- discovery ------------------------------------------------------------------ #


def test_discovers_every_field_from_the_engine():
    api = _FakeApi(_build_responses())

    discovery = _discover(api)

    assert _extract_values(discovery) == {
        "observed_deployment_name": "it-support-agent",
        "observed_agent_name": "it_agent",
        "observed_project_id": _PROJECT,
        "telemetry_payload_bucket": f"{_PROJECT}-logs",
        "telemetry_ingestion_source": "cloud_logging",
        "telemetry_dataset": "it_telemetry",
        "telemetry_table": _TABLE,
        "telemetry_location": "us-east1",
    }
    sources = {f: v.source for f, v in discovery.values.items()}
    assert sources["observed_agent_name"] == "rootAgentName from list-apps"
    assert sources["telemetry_dataset"] == "sink it-support-agent-genai-logs"
    assert sources["observed_project_id"] == "project of the observed engine"
    assert (
        sources["telemetry_table"] == "3 rows for it_agent in the last 7 days"
    )
    (note,) = discovery.notes
    assert "you may not enable it" in note
    assert note.endswith("looking for a logging sink.")


@pytest.mark.parametrize(
    "env",
    [
        [],
        [
            {
                "name": "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH",
                "value": "/tmp/x",
            }
        ],
    ],
)
def test_no_gcs_upload_path_clears_the_payload_bucket(env):
    """So a re-attach forgets a bucket an earlier attach found."""
    responses = _build_responses()
    responses[_ENGINE_URL] = {
        **responses[_ENGINE_URL],
        "spec": {"deploymentSpec": {"env": env}},
    }

    discovery = _discover(_FakeApi(responses))

    assert discovery.values["telemetry_payload_bucket"] == DiscoveredValue(
        "",
        "no gs:// OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH in the observed engine",
    )


def _discover_with_env(env: dict[str, str]):
    """Discovers the fake engine with ``env`` as its environment.

    Args:
        env: Environment variables by name.

    Returns:
        What discovery found.
    """
    responses = _build_responses()
    responses[_ENGINE_URL] = {
        **responses[_ENGINE_URL],
        "spec": {
            "deploymentSpec": {
                "env": [{"name": k, "value": v} for k, v in env.items()]
            }
        },
    }
    return _discover(_FakeApi(responses))


_NO_CONTENT_ENV = {
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT",
    "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "false",
}
"""What `agents-cli deploy` sets on an Agent Runtime engine by default."""

_CONTENT_FIX = (
    "--update-env-vars OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental,"
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=EVENT_ONLY"
)


@pytest.mark.parametrize(
    "override",
    [
        {},
        {"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": ""},
        # ADK's own spans: ingestion drops the content it writes there.
        {"ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "true"},
        # The experimental conventions reject the boolean.
        {
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
            "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
        },
    ],
)
def test_an_engine_that_records_no_content_is_pointed_out(override):
    discovery = _discover_with_env({**_NO_CONTENT_ENV, **override})

    (note,) = [n for n in discovery.notes if "no message content" in n]
    assert "investigations drop every conversation" in note
    assert _CONTENT_FIX in note


def test_a_capture_mode_without_the_experimental_conventions_is_explained():
    """The default conventions read the mode as a boolean, so EVENT_ONLY is
    off: what the it-support-ar deployment recorded."""
    discovery = _discover_with_env(
        {
            **_NO_CONTENT_ENV,
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "EVENT_ONLY",
            "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "true",
        }
    )

    (note,) = [n for n in discovery.notes if "no message content" in n]
    assert (
        "records content only under "
        "OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental" in note
    )
    assert _CONTENT_FIX in note


@pytest.mark.parametrize(
    "override",
    [
        {"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true"},
        {"OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "TRUE"},
        {
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "EVENT_ONLY",
            "OTEL_SEMCONV_STABILITY_OPT_IN": "http, gen_ai_latest_experimental",
        },
        {
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH": (
                f"gs://{_PROJECT}-logs/completions"
            )
        },
    ],
)
def test_an_engine_that_records_content_is_not_pointed_out(override):
    discovery = _discover_with_env({**_NO_CONTENT_ENV, **override})

    assert not any("no message content" in n for n in discovery.notes)


def test_a_big_query_agent_is_not_checked_for_content_capture():
    """The analytics plugin records content whatever the engine sets."""
    responses = _build_responses()
    responses[_ENGINE_URL] = {
        **responses[_ENGINE_URL],
        "spec": {"deploymentSpec": {"env": []}},
    }

    discovery = _discover(
        _FakeApi(responses), given={"telemetry_ingestion_source": "big_query"}
    )

    assert not any("no message content" in n for n in discovery.notes)


def test_counts_rows_where_the_dataset_is_over_the_window():
    api = _FakeApi(_build_responses())

    _discover(api)

    (body,) = [b for url, b in api.calls if url.endswith("/queries")]
    assert body["location"] == "us-east1"
    assert body["queryParameters"][0]["parameterValue"] == {"value": "7"}
    assert f"`{_PROJECT}.it_telemetry.{_TABLE}`" in body["query"]


def test_a_given_field_is_not_discovered():
    api = _FakeApi(_build_responses())

    discovery = _discover(
        api,
        given={"observed_agent_name": "it_agent", "telemetry_location": "x"},
    )

    assert "observed_agent_name" not in discovery.values
    assert "telemetry_location" not in discovery.values
    assert _LIST_APPS not in api.list_urls()
    # The given name is still the one rows are counted for.
    assert discovery.chosen is not None
    assert discovery.chosen.agent_rows == 3


_BIG_QUERY = {"telemetry_ingestion_source": "big_query"}
_ANALYTICS_DATASET = "it_support_bqaa_telemetry"
_ANALYTICS_BASE = f"{_BQ}/projects/{_PROJECT}/datasets/{_ANALYTICS_DATASET}"
_ANALYTICS_TABLE_URL = f"{_ANALYTICS_BASE}/tables/agent_events"


def _build_analytics_responses(
    *,
    dataset_var: str | None = _ANALYTICS_DATASET,
    table: dict[str, Any] | None = None,
    dataset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Builds the replies of an engine recording with the analytics plugin.

    The engine's environment is what agents-cli's Terraform sets for
    `--bq-analytics`, as the it-support-bqaa deployment configures it.

    Args:
        dataset_var: The engine's `BQ_ANALYTICS_DATASET_ID`; None for unset.
        table: The `agent_events` table's reply; None for a table that does
            not exist yet.
        dataset: The dataset's reply; None for a dataset that does not exist.

    Returns:
        Replies by URL.
    """
    responses = _build_responses()
    env = [
        {
            "name": "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH",
            "value": f"gs://{_PROJECT}-logs/completions",
        }
    ]
    if dataset_var is not None:
        env.append({"name": "BQ_ANALYTICS_DATASET_ID", "value": dataset_var})
    responses[_ENGINE_URL] = {
        **responses[_ENGINE_URL],
        "spec": {"deploymentSpec": {"env": env}},
    }
    base = f"{_BQ}/projects/{_PROJECT}/datasets/{dataset_var or 'adk_agent_analytics'}"
    if table is not None:
        responses[f"{base}/tables/agent_events"] = table
    if dataset is not None:
        responses[base] = dataset
    return responses


_PLUGIN_TABLE = {"location": "us-east1", "labels": {"adk_schema_version": "2"}}


def test_a_big_query_agent_is_read_from_the_plugins_table():
    api = _FakeApi(_build_analytics_responses(table=_PLUGIN_TABLE))

    discovery = _discover(api, given=_BIG_QUERY)

    assert {
        f: v for f, v in discovery.values.items() if f.startswith("telemetry_")
    } == {
        "telemetry_payload_bucket": DiscoveredValue(
            f"{_PROJECT}-logs",
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH of the observed engine",
        ),
        "telemetry_dataset": DiscoveredValue(
            _ANALYTICS_DATASET, "BQ_ANALYTICS_DATASET_ID of the observed engine"
        ),
        "telemetry_table": DiscoveredValue(
            "agent_events", "the plugin's default table, which exists"
        ),
        "telemetry_location": DiscoveredValue(
            "us-east1", f"location of table {_ANALYTICS_DATASET}.agent_events"
        ),
    }
    assert discovery.notes == []
    # The sink agents-cli creates beside the plugin is not considered.
    assert _SINKS not in api.list_urls()
    assert not any("observability" in url for url in api.list_urls())
    # The agent's project is still discovered: the telemetry is read there.
    assert discovery.values["observed_project_id"] == DiscoveredValue(
        _PROJECT, "project of the observed engine"
    )


def test_a_big_query_agent_with_no_traffic_gets_the_table_the_plugin_creates():
    """The plugin creates its table on the first event; Terraform creates the
    dataset, which gives the location."""
    api = _FakeApi(_build_analytics_responses(dataset={"location": "us-east1"}))

    discovery = _discover(api, given=_BIG_QUERY)

    assert discovery.values["telemetry_table"] == DiscoveredValue(
        "agent_events",
        "the plugin's default table; not created yet, the plugin creates it "
        "on the agent's first event",
    )
    assert discovery.values["telemetry_location"] == DiscoveredValue(
        "us-east1", f"location of dataset {_ANALYTICS_DATASET}"
    )
    assert discovery.notes == []


def test_an_engine_naming_no_analytics_dataset_gets_agents_clis_default():
    api = _FakeApi(
        _build_analytics_responses(
            dataset_var=None, table={**_PLUGIN_TABLE, "location": "US"}
        )
    )

    discovery = _discover(api, given=_BIG_QUERY)

    assert discovery.values["telemetry_dataset"] == DiscoveredValue(
        "adk_agent_analytics",
        "the default of agents-cli's analytics code; the observed engine "
        "sets no BQ_ANALYTICS_DATASET_ID",
    )
    assert discovery.values["telemetry_location"].value == "US"


def test_a_table_the_plugin_did_not_create_is_pointed_out():
    api = _FakeApi(_build_analytics_responses(table={"location": "us-east1"}))

    discovery = _discover(api, given=_BIG_QUERY)

    (note,) = discovery.notes
    assert "has no adk_schema_version label" in note
    assert discovery.values["telemetry_table"].value == "agent_events"


def test_an_analytics_dataset_that_does_not_exist_leaves_the_location():
    api = _FakeApi(_build_analytics_responses())

    discovery = _discover(api, given=_BIG_QUERY)

    assert "telemetry_location" not in discovery.values
    assert discovery.values["telemetry_dataset"].value == _ANALYTICS_DATASET
    (note,) = discovery.notes
    assert "--telemetry-location" in note


def test_an_unreadable_analytics_table_is_noted():
    responses = _build_analytics_responses(dataset={"location": "us-east1"})
    responses[_ANALYTICS_TABLE_URL] = ApiError(403, "denied")

    discovery = _discover(_FakeApi(responses), given=_BIG_QUERY)

    (note,) = discovery.notes
    assert note.startswith(
        f"Cannot read table {_PROJECT}.{_ANALYTICS_DATASET}.agent_events"
    )
    # Denied is not missing: the table may well exist.
    assert discovery.values["telemetry_table"] == DiscoveredValue(
        "agent_events", "the plugin's default table"
    )
    assert discovery.values["telemetry_location"].value == "us-east1"


def test_a_given_analytics_dataset_and_table_are_the_ones_looked_up():
    responses = _build_analytics_responses()
    base = f"{_BQ}/projects/{_PROJECT}/datasets/mine"
    responses[f"{base}/tables/events"] = _PLUGIN_TABLE
    api = _FakeApi(responses)

    discovery = _discover(
        api,
        given={
            **_BIG_QUERY,
            "telemetry_dataset": "mine",
            "telemetry_table": "events",
        },
    )

    assert f"{base}/tables/events" in api.list_urls()
    assert "telemetry_dataset" not in discovery.values
    assert "telemetry_table" not in discovery.values
    assert discovery.values["telemetry_location"] == DiscoveredValue(
        "us-east1", "location of table mine.events"
    )


def test_a_given_analytics_table_that_does_not_exist_is_noted():
    api = _FakeApi(_build_analytics_responses(dataset={"location": "us-east1"}))

    discovery = _discover(
        api, given={**_BIG_QUERY, "telemetry_table": "events"}
    )

    (note,) = discovery.notes
    assert note.startswith(
        f"Table {_PROJECT}.{_ANALYTICS_DATASET}.events does not exist."
    )
    assert "telemetry_table" not in discovery.values
    assert discovery.values["telemetry_location"].value == "us-east1"


def test_a_given_location_skips_the_analytics_dataset_lookup():
    api = _FakeApi(_build_analytics_responses())

    discovery = _discover(
        api, given={**_BIG_QUERY, "telemetry_location": "us-east1"}
    )

    assert _ANALYTICS_BASE not in api.list_urls()
    assert "telemetry_location" not in discovery.values
    # A missing dataset is not noted: its location is all it was read for.
    assert discovery.notes == []


def test_a_given_dataset_narrows_the_sinks():
    api = _FakeApi(_build_responses())

    discovery = _discover(api, given={"telemetry_dataset": "elsewhere"})

    assert _list_telemetry_fields(discovery) == []
    assert any("No logging sink" in note for note in discovery.notes)


def test_without_list_apps_the_name_comes_from_the_engines_rows():
    """A pickled AdkApp serves no list-apps; its telemetry still names it."""
    responses = _build_responses(**{_LIST_APPS: ApiError(404, "Not Found")})
    _serve_dataset(
        responses,
        "it_telemetry",
        [("42", "it_agent", 3), ("99", "someone_else", 50)],
    )
    api = _FakeApi(responses)

    discovery = _discover(api)

    assert discovery.values["observed_agent_name"] == DiscoveredValue(
        "it_agent", f"gen_ai_agent_name of this engine's rows in {_TABLE}"
    )
    assert any("list-apps did not name" in note for note in discovery.notes)


def test_a_project_number_is_reported_as_its_id():
    """A grant, a table reference and a sink's project all take the id."""
    api = _FakeApi(_build_responses())

    discovery = _discover(api)

    assert _RESOLVE_NUMBER in api.list_urls()
    assert discovery.values["observed_project_id"].value == _PROJECT


def test_a_given_project_matches_the_sinks_and_needs_no_lookup():
    """The remedy the note below names must work when the lookup is denied."""
    responses = _build_responses(
        **{
            _RESOLVE_NUMBER: ApiError(403, "denied"),
            _SINKS: {
                "sinks": [_build_sink("far", "far_telemetry", project="far-p")]
            },
        }
    )
    _serve_dataset(
        responses, "far_telemetry", [("42", "it_agent", 5)], project="far-p"
    )
    api = _FakeApi(responses)

    discovery = _discover(api, given={"observed_project_id": "far-p"})

    assert _RESOLVE_NUMBER not in api.list_urls()
    assert "observed_project_id" not in discovery.values
    assert discovery.values["telemetry_dataset"].value == "far_telemetry"
    assert all("logging sink" in note for note in discovery.notes)


def test_an_unresolvable_project_number_is_kept_with_a_note():
    api = _FakeApi(
        _build_responses(**{_RESOLVE_NUMBER: ApiError(403, "denied")})
    )

    discovery = _discover(api)

    assert discovery.values["observed_project_id"].value == _NUMBER
    assert any("Cannot resolve project 123" in n for n in discovery.notes)


def test_an_unreadable_engine_fails():
    api = _FakeApi(_build_responses(**{_ENGINE_URL: ApiError(403, "denied")}))

    with pytest.raises(discovery_lib.DiscoveryError, match="Cannot read"):
        _discover(api)


def test_no_spans_and_no_sink_leave_the_telemetry_fields_alone():
    api = _FakeApi(_build_responses(**{_SINKS: {"sinks": []}}))

    discovery = _discover(api)

    assert _list_telemetry_fields(discovery) == []
    spans_note, sink_note = discovery.notes
    assert spans_note.startswith("The Observability API is disabled")
    assert sink_note == (
        f"No logging sink in {_NUMBER} exports "
        "gen_ai.client.inference.operation.details to BigQuery, so the "
        "telemetry fields keep their values."
    )


def test_a_new_agent_gets_the_table_its_sink_will_create():
    """No traffic yet: a sink creates its table on the first row."""
    responses = _build_responses()
    _serve_dataset(responses, "it_telemetry", None)
    api = _FakeApi(responses)

    discovery = _discover(api)

    assert discovery.values["telemetry_table"] == DiscoveredValue(
        _TABLE, "not created yet; the sink creates it on the first row"
    )
    assert not any(url.endswith("/queries") for url in api.list_urls())


def test_the_sink_with_the_agents_rows_wins_and_the_rest_are_listed():
    responses = _build_responses(
        **{
            _SINKS: {
                "sinks": [
                    _build_sink("old-sink", "old_telemetry"),
                    _build_sink("it-support-agent-genai-logs", "it_telemetry"),
                ]
            }
        }
    )
    _serve_dataset(responses, "old_telemetry", [("7", "other_agent", 90)])
    api = _FakeApi(responses)

    discovery = _discover(api)

    assert discovery.values["telemetry_dataset"].value == "it_telemetry"
    lines = discovery_lib.render_discovery_lines(_ENGINE, [], discovery)
    assert any(f"old_telemetry.{_TABLE}" in line for line in lines)


def test_a_sink_into_another_project_is_not_considered():
    """Ingestion reads the telemetry in the observed agent's own project."""
    responses = _build_responses(
        **{
            _SINKS: {
                "sinks": [_build_sink("far", "far_telemetry", project="far-p")]
            }
        }
    )
    api = _FakeApi(responses)

    discovery = _discover(api)

    assert _list_telemetry_fields(discovery) == []
    assert any(
        "outside the observed project my-project" in note
        for note in discovery.notes
    )


@pytest.mark.parametrize(
    "resource",
    ["", "projects/p/locations/l", "reasoningEngines/1", "projects/p/x/l/y/1"],
)
def test_a_malformed_resource_is_refused(resource):
    with pytest.raises(discovery_lib.DiscoveryError):
        discovery_lib.parse_engine_resource(resource)


# --- Cloud Trace spans, with no sink ----------------------------------------------- #

_LINKS = f"{_OBSERVABILITY}/us/buckets/_Trace/datasets/Spans/links"
_SPANS_DATASET = (
    f"projects/{_PROJECT}/locations/us/buckets/_Trace/datasets/Spans"
)


def _build_trace_responses(
    links: list[str], *, traces: int = 4, **overrides: Any
) -> dict[str, Any]:
    """Builds the replies of an agent that exports only spans to Cloud Trace.

    Args:
        links: Ids of the BigQuery links on `_Trace/Spans`.
        traces: The agent's traces in each linked dataset.
        **overrides: Replies to replace, by URL.

    Returns:
        Replies by URL.
    """
    responses = _build_responses(**{_SINKS: {"sinks": []}})
    responses[_BUCKETS] = {
        "buckets": [
            {"name": f"projects/{_PROJECT}/locations/global/buckets/other"},
            {"name": f"projects/{_PROJECT}/locations/us/buckets/_Trace"},
        ]
    }
    responses[_LINKS] = {
        "links": [{"name": f"{_SPANS_DATASET}/links/{link}"} for link in links]
    }
    for link in links:
        responses[f"{_BQ}/projects/{_PROJECT}/datasets/{link}"] = {
            "location": "US"
        }
    responses[f"{_BQ}/projects/{_PROJECT}/queries"] = lambda body: {
        "jobComplete": True,
        "rows": [{"f": [{"v": str(traces)}]}],
    }
    responses.update(overrides)
    return responses


def test_with_no_sink_the_agents_spans_are_read_through_the_trace_link():
    api = _FakeApi(_build_trace_responses(["spans_link"]))

    discovery = _discover(api)

    values = _extract_values(discovery)
    assert {f: values[f] for f in _list_telemetry_fields(discovery)} == {
        "telemetry_ingestion_source": "cloud_ops",
        "telemetry_dataset": "spans_link",
        "telemetry_table": "_AllSpans",
        "telemetry_location": "US",
    }
    assert discovery.values["telemetry_table"].source == (
        "4 traces for it_agent in the last 7 days"
    )
    assert discovery.trace_link == discovery_lib.TraceLink(
        _PROJECT, "us", "spans_link", exists=True
    )
    (body,) = [b for url, b in api.calls if url.endswith("/queries")]
    assert f"`{_PROJECT}.spans_link._AllSpans`" in body["query"]
    assert body["location"] == "US"
    assert {
        p["name"]: p["parameterValue"]["value"] for p in body["queryParameters"]
    } == {"days": "7", "agent": "it_agent"}


def test_a_trace_link_with_no_traces_for_the_agent_is_still_taken():
    api = _FakeApi(_build_trace_responses(["spans_link"], traces=0))

    discovery = _discover(api)

    assert discovery.values["telemetry_dataset"].value == "spans_link"
    assert any(
        "no traces for it_agent" in note
        and "--telemetry-source cloud_logging" in note
        for note in discovery.notes
    )


def test_with_no_trace_link_discovery_plans_one():
    api = _FakeApi(_build_trace_responses([]))

    discovery = _discover(api)

    link = discovery_lib.TraceLink(
        _PROJECT, "us", discovery_lib.DEFAULT_TRACE_LINK, exists=False
    )
    assert discovery.trace_link == link
    assert discovery.values["telemetry_dataset"] == DiscoveredValue(
        link.link_id, "not linked yet; attach creates the link"
    )
    # BigQuery names the multi-region in capitals.
    assert discovery.values["telemetry_location"].value == "US"
    assert not any(url.endswith("/queries") for url in api.list_urls())
    assert link.build_create_argv() == [
        "gcloud",
        "observability",
        "buckets",
        "datasets",
        "links",
        "create",
        f"{_SPANS_DATASET}/links/{link.link_id}",
        "--dataset=Spans",
        "--bucket=_Trace",
        "--location=us",
        f"--project={_PROJECT}",
    ]


def test_a_given_cloud_ops_source_skips_the_sinks():
    api = _FakeApi(_build_trace_responses(["spans_link"]))

    discovery = _discover(
        api, given={"telemetry_ingestion_source": "cloud_ops"}
    )

    assert _SINKS not in api.list_urls()
    assert discovery.values["telemetry_dataset"].value == "spans_link"


def test_a_given_cloud_ops_dataset_names_the_link_to_create():
    api = _FakeApi(_build_trace_responses([]))

    discovery = _discover(
        api,
        given={
            "telemetry_ingestion_source": "cloud_ops",
            "telemetry_dataset": "my_spans",
        },
    )

    assert discovery.trace_link is not None
    assert discovery.trace_link.link_id == "my_spans"
    assert not discovery.trace_link.exists


def test_a_second_link_on_the_spans_is_not_planned():
    """An observability dataset takes one link."""
    api = _FakeApi(_build_trace_responses(["spans_link"]))

    discovery = _discover(
        api,
        given={
            "telemetry_ingestion_source": "cloud_ops",
            "telemetry_dataset": "my_spans",
        },
    )

    assert discovery.trace_link is None
    assert "telemetry_table" not in discovery.values
    assert any("--telemetry-dataset spans_link" in n for n in discovery.notes)


def test_a_given_sink_dataset_that_is_not_found_plans_no_link():
    api = _FakeApi(_build_trace_responses([]))

    discovery = _discover(api, given={"telemetry_dataset": "elsewhere"})

    assert discovery.trace_link is None
    assert _list_telemetry_fields(discovery) == []


def test_a_given_cloud_logging_source_is_not_mixed_with_the_spans():
    """The given source is kept, so the other fields must describe it too."""
    api = _FakeApi(_build_trace_responses(["spans_link"]))

    discovery = _discover(
        api, given={"telemetry_ingestion_source": "cloud_logging"}
    )

    assert _list_telemetry_fields(discovery) == []
    assert discovery.trace_link is None
    assert _BUCKETS not in api.list_urls()


def test_with_no_trace_bucket_discovery_plans_the_bucket_and_the_link():
    api = _FakeApi(_build_trace_responses([], **{_BUCKETS: {"buckets": []}}))

    discovery = _discover(api)

    link = discovery.trace_link
    assert link is not None
    assert not link.bucket_exists and link.location == "us"
    assert [argv[3] for argv in link.build_needed_argvs()] == [
        "create",
        "datasets",
    ]
    values = _extract_values(discovery)
    assert values["telemetry_ingestion_source"] == "cloud_ops"
    assert values["telemetry_location"] == "US"
    assert any("no traces to count" in n for n in discovery.notes)


def test_a_disabled_api_the_user_may_enable_is_planned_on():
    api = _FakeApi(
        _build_trace_responses(
            [],
            **{
                _BUCKETS: _API_DISABLED,
                _TEST_PERMISSIONS: {
                    "permissions": ["serviceusage.services.enable"]
                },
            },
        )
    )

    discovery = _discover(api)

    link = discovery.trace_link
    assert link is not None
    assert link.build_needed_argvs()[0][:3] == ["gcloud", "services", "enable"]
    assert discovery.values["telemetry_ingestion_source"].value == "cloud_ops"
    assert any("Applying enables it" in n for n in discovery.notes)


def test_a_disabled_api_the_user_may_not_enable_falls_back_to_the_sink():
    responses = _build_trace_responses([], **{_BUCKETS: _API_DISABLED})
    responses[_SINKS] = _build_responses()[_SINKS]
    _serve_dataset(responses, "it_telemetry", [("42", "it_agent", 3)])
    api = _FakeApi(responses)

    discovery = _discover(api)

    assert discovery.values["telemetry_ingestion_source"].value == (
        "cloud_logging"
    )
    assert discovery.trace_link is None
    (note,) = [n for n in discovery.notes if "Observability API" in n]
    assert "you may not enable it" in note
    assert (
        "gcloud services enable observability.googleapis.com "
        f"--project={_PROJECT}"
    ) in note


def _build_sink_and_trace_responses(links: list[str]) -> dict[str, Any]:
    """Builds the replies of an agent with both a logging sink and spans.

    Args:
        links: Ids of the BigQuery links on `_Trace/Spans`.

    Returns:
        Replies by URL.
    """
    responses = _build_trace_responses(links)
    responses[_SINKS] = _build_responses()[_SINKS]
    queries = f"{_BQ}/projects/{_PROJECT}/queries"
    traces = responses[queries]
    _serve_dataset(responses, "it_telemetry", [("42", "it_agent", 3)])
    sink_rows = responses[queries]
    responses[queries] = lambda body: (
        traces(body) if "_AllSpans" in body["query"] else sink_rows(body)
    )
    return responses


def test_the_spans_win_over_a_sink_with_the_agents_rows():
    api = _FakeApi(_build_sink_and_trace_responses(["spans_link"]))

    discovery = _discover(api)

    assert {
        f: discovery.values[f].value for f in _list_telemetry_fields(discovery)
    } == {
        "telemetry_ingestion_source": "cloud_ops",
        "telemetry_dataset": "spans_link",
        "telemetry_table": "_AllSpans",
        "telemetry_location": "US",
    }
    assert discovery.chosen is None
    assert discovery.notes == [
        "Logging sink it-support-agent-genai-logs exports "
        "gen_ai.client.inference.operation.details as well. The spans are "
        "preferred; --telemetry-source cloud_logging reads the sink's table "
        "instead."
    ]


def test_the_spans_win_over_a_sink_even_with_no_link_yet():
    api = _FakeApi(_build_sink_and_trace_responses([]))

    discovery = _discover(api)

    assert discovery.trace_link == discovery_lib.TraceLink(
        _PROJECT, "us", discovery_lib.DEFAULT_TRACE_LINK, exists=False
    )
    assert discovery.values["telemetry_ingestion_source"].value == "cloud_ops"


def test_without_list_apps_the_spans_are_counted_for_the_name_in_the_rows():
    responses = _build_sink_and_trace_responses(["spans_link"])
    responses[_LIST_APPS] = ApiError(404, "Not Found")
    api = _FakeApi(responses)

    discovery = _discover(api)

    assert discovery.values["observed_agent_name"].value == "it_agent"
    assert discovery.values["telemetry_table"].source == (
        "4 traces for it_agent in the last 7 days"
    )


def test_a_given_sink_dataset_skips_the_spans():
    api = _FakeApi(_build_sink_and_trace_responses(["spans_link"]))

    discovery = _discover(api, given={"telemetry_dataset": "it_telemetry"})

    assert _BUCKETS not in api.list_urls()
    assert discovery.trace_link is None
    assert (
        discovery.values["telemetry_ingestion_source"].value == "cloud_logging"
    )
    assert discovery.notes == []


def test_unreadable_spans_fall_back_to_the_sink_with_a_note():
    responses = _build_sink_and_trace_responses(["spans_link"])
    responses[_LINKS] = ApiError(403, "denied")
    api = _FakeApi(responses)

    discovery = _discover(api)

    assert (
        discovery.values["telemetry_ingestion_source"].value == "cloud_logging"
    )
    assert discovery.values["telemetry_dataset"].value == "it_telemetry"
    (note,) = discovery.notes
    assert note.startswith("Cannot list the BigQuery links of _Trace/Spans")


def test_a_given_cloud_ops_source_without_spans_names_no_sink():
    api = _FakeApi(_build_trace_responses([], **{_BUCKETS: _API_DISABLED}))

    discovery = _discover(
        api, given={"telemetry_ingestion_source": "cloud_ops"}
    )

    assert _list_telemetry_fields(discovery) == []
    assert _SINKS not in api.list_urls()
    (note,) = discovery.notes
    assert "you may not enable it" in note


# --- flags, discovery, hints ------------------------------------------------------ #


def test_a_flag_outranks_discovery_which_outranks_a_hint():
    resolved = discovery_lib.resolve_attach_fields(
        given={"telemetry_table": "mine"},
        discovered={
            "telemetry_table": DiscoveredValue("found", "sink"),
            "observed_agent_name": DiscoveredValue("running", "list-apps"),
        },
        hints={
            "observed_agent_name": DiscoveredValue("scaffolded", "manifest"),
            "observed_deployment_name": DiscoveredValue("dep", "manifest"),
        },
    )

    by_field = {r.field: r for r in resolved}
    assert "telemetry_table" not in by_field
    assert by_field["observed_agent_name"].value == "running"
    assert by_field["observed_agent_name"].disagreeing == DiscoveredValue(
        "scaffolded", "manifest"
    )
    assert by_field["observed_deployment_name"].value == "dep"

    lines = discovery_lib.render_discovery_lines(_ENGINE, resolved, None)
    assert any("manifest says scaffolded" in line for line in lines)


@pytest.mark.parametrize("text", ["[]", '{"a": 1}', '{"a": {"source": "s"}}'])
def test_malformed_hints_are_refused(text):
    with pytest.raises(ValueError):
        discovery_lib.parse_hints(text)


# --- at the command line ----------------------------------------------------------- #


class _FakeDeployment:
    """Answers the attach dry run with no change and records the requests."""

    def __init__(self):
        """Initializes the fake with no requests."""
        self.requests: list[Any] = []

    def __call__(self, *, url, resource, path, payload=None):
        """Records a request, standing in for `_fetch_command_result`.

        Args:
            url: Ignored engine URL.
            resource: Ignored engine resource name.
            path: Ignored route path.
            payload: Request body.

        Returns:
            A plan with no change.
        """
        self.requests.append(payload)
        return {"object": "gs://b/agents/x.json", "attached": True}


@pytest.fixture
def deployment(monkeypatch):
    fake = _FakeDeployment()
    monkeypatch.setattr(aqua_cli, "_fetch_command_result", fake)
    return fake


@pytest.fixture
def api(monkeypatch):
    fake = _FakeApi(_build_responses())
    monkeypatch.setattr(
        rest_client_lib, "RestClient", lambda *args, **kwargs: fake
    )
    return fake


_AQUA = f"projects/{_NUMBER}/locations/us-central1/reasoningEngines/1"


def _invoke_attach(*args: str):
    """Runs `attach --dry-run` against a fixed AQuA.

    Args:
        *args: More arguments.

    Returns:
        The Click invocation result.
    """
    return CliRunner().invoke(
        aqua_cli.cli,
        ["attach", "--dry-run", "--aqua-resource", _AQUA, *args],
    )


def test_attach_sends_what_discovery_found(deployment, api):
    result = _invoke_attach(
        "--observed-agent-resource", _ENGINE, "--lookback-days", "30"
    )

    assert result.exit_code == 0, result.output
    assert deployment.requests[0]["agent_name"] == "it_agent"
    assert deployment.requests[0]["fields"] == {
        "observed_engine_id": "42",
        "data_lookback_window": 30,
        "observed_deployment_name": "it-support-agent",
        "observed_project_id": _PROJECT,
        "telemetry_payload_bucket": f"{_PROJECT}-logs",
        "telemetry_ingestion_source": "cloud_logging",
        "telemetry_dataset": "it_telemetry",
        "telemetry_table": _TABLE,
        "telemetry_location": "us-east1",
    }
    assert f"Discovered from {_ENGINE}" in result.output
    assert "rootAgentName from list-apps" in result.output
    (body,) = [b for url, b in api.calls if url.endswith("/queries")]
    assert body["queryParameters"][0]["parameterValue"] == {"value": "30"}


def test_attach_with_a_big_query_source_sends_the_plugins_table(
    deployment, monkeypatch
):
    fake = _FakeApi(_build_analytics_responses(table=_PLUGIN_TABLE))
    monkeypatch.setattr(
        rest_client_lib, "RestClient", lambda *args, **kwargs: fake
    )

    result = _invoke_attach(
        "--observed-agent-resource",
        _ENGINE,
        "--telemetry-source",
        "big_query",
    )

    assert result.exit_code == 0, result.output
    fields = deployment.requests[0]["fields"]
    assert fields["telemetry_ingestion_source"] == "big_query"
    assert fields["telemetry_dataset"] == _ANALYTICS_DATASET
    assert fields["telemetry_table"] == "agent_events"
    assert fields["telemetry_location"] == "us-east1"
    assert "BQ_ANALYTICS_DATASET_ID of the observed engine" in result.output


@pytest.mark.parametrize(("lookback", "window"), [("0", "1"), ("-3", "1")])
def test_attach_counts_rows_over_at_least_a_day(
    deployment, api, lookback, window
):
    """A window of no days could not hold the agent's rows."""
    result = _invoke_attach(
        "--observed-agent-resource", _ENGINE, "--lookback-days", lookback
    )

    assert result.exit_code == 0, result.output
    (body,) = [b for url, b in api.calls if url.endswith("/queries")]
    assert body["queryParameters"][0]["parameterValue"] == {"value": window}
    # The deployment, not discovery, decides what is stored.
    assert deployment.requests[0]["fields"]["data_lookback_window"] == int(
        lookback
    )


def test_attach_with_no_discover_sends_only_the_flags(deployment, api):
    hints = json.dumps({"telemetry_dataset": {"value": "d", "source": "s"}})

    result = _invoke_attach(
        "--observed-agent-resource",
        _ENGINE,
        "--no-discover",
        f"--hints={hints}",
    )

    assert result.exit_code == 0, result.output
    assert api.calls == []
    assert deployment.requests[0] == {
        "agent_name": "",
        # As the resource name spells it: nothing resolves the number.
        "fields": {"observed_engine_id": "42", "observed_project_id": _NUMBER},
        "dry_run": True,
        "check_access": True,
        "fill_from_table": True,
    }


def test_a_payload_bucket_flag_reaches_the_request(deployment, api):
    """For an agent with no engine to discover the bucket from."""
    result = _invoke_attach(
        "--no-discover", "--telemetry-payload-bucket", "logs"
    )

    assert result.exit_code == 0, result.output
    assert deployment.requests[0]["fields"] == {
        "telemetry_payload_bucket": "logs"
    }


def test_attach_without_a_seed_takes_the_hints(deployment, api):
    hints = json.dumps(
        {"observed_agent_name": {"value": "hinted", "source": "manifest"}}
    )

    result = _invoke_attach(f"--hints={hints}")

    assert result.exit_code == 0, result.output
    assert api.calls == []
    assert deployment.requests[0]["agent_name"] == "hinted"
    assert "Discovered from this project" in result.output


def test_attach_refuses_a_malformed_observed_agent_resource(deployment, api):
    result = _invoke_attach("--observed-agent-resource", "reasoningEngines/42")

    assert result.exit_code == 2
    assert deployment.requests == []


def test_attach_observes_an_engine_in_another_project(deployment, api):
    result = CliRunner().invoke(
        aqua_cli.cli,
        [
            "attach",
            "--dry-run",
            "--aqua-resource",
            "projects/aqua-p/locations/us-central1/reasoningEngines/1",
            "--observed-agent-resource",
            _ENGINE,
        ],
    )

    assert result.exit_code == 0, result.output
    fields = deployment.requests[0]["fields"]
    assert fields["observed_project_id"] == _PROJECT
    assert fields["telemetry_dataset"] == "it_telemetry"


def test_an_observed_project_flag_outranks_discovery(deployment, api):
    api.responses[_SINKS] = {"sinks": []}

    result = _invoke_attach(
        "--observed-agent-resource", _ENGINE, "--observed-project", "mine"
    )

    assert result.exit_code == 0, result.output
    assert deployment.requests[0]["fields"]["observed_project_id"] == "mine"


def test_an_agent_without_an_engine_names_its_project_by_flag(deployment, api):
    """A Cloud Run or GKE agent runs in a project too, with no engine to
    discover it from."""
    result = _invoke_attach("--observed-project", "run-project")

    assert result.exit_code == 0, result.output
    assert api.calls == []
    assert deployment.requests[0]["fields"] == {
        "observed_project_id": "run-project"
    }


class _StoredDeployment(_FakeDeployment):
    """Answers each attach dry run with its changes over a stored attachment."""

    def __init__(self, stored: dict[str, Any]):
        """Initializes the fake.

        Args:
            stored: The attachment's stored fields.
        """
        super().__init__()
        self.stored = stored

    def __call__(self, *, url, resource, path, payload=None):
        """Plans a request over the stored fields.

        Args:
            url: Ignored engine URL.
            resource: Ignored engine resource name.
            path: Ignored route path.
            payload: Request body.

        Returns:
            A plan with each requested field that differs from its stored value.
            A field missing from ``stored`` is stored empty, as the engine
            reports an unset string setting.
        """
        self.requests.append(payload)
        return {
            "object": "gs://b/agents/x.json",
            "attached": True,
            "watched": True,
            "changes": [
                {
                    "field": field,
                    "before": self.stored.get(field, ""),
                    "after": value,
                }
                for field, value in sorted(payload["fields"].items())
                if self.stored.get(field, "") != value
            ],
        }

    def extract_planned_fields(self) -> dict[str, Any]:
        """Extracts the fields of the plan that checks access, the one attach shows.

        Returns:
            Its request's fields.
        """
        (fields,) = [
            r["fields"] for r in self.requests if r.get("check_access")
        ]
        return fields


_STORED_CLOUD_OPS = {
    "telemetry_ingestion_source": "cloud_ops",
    "telemetry_dataset": "trace_spans_linked",
    "telemetry_table": "_AllSpans",
    "telemetry_location": "US",
    "observed_deployment_name": "it-support-agent",
    "observed_project_id": _PROJECT,
    "telemetry_payload_bucket": f"{_PROJECT}-logs",
}

_SINK_HINTS = json.dumps(
    {
        "telemetry_ingestion_source": {"value": "cloud_logging", "source": "s"},
        "telemetry_dataset": {"value": "pairgo_telemetry", "source": "s"},
        "telemetry_location": {"value": "us-central1", "source": "s"},
    }
)


@pytest.mark.parametrize(
    "seed",
    [
        ("it_agent", "--observed-agent-resource", _ENGINE),
        # The agent's name is discovered from the engine.
        ("--observed-agent-resource", _ENGINE),
        ("it_agent", f"--hints={_SINK_HINTS}"),
    ],
    ids=["engine", "engine-discovered-name", "hints"],
)
def test_an_attached_agent_keeps_its_stored_values_over_discovered_ones(
    monkeypatch, api, seed
):
    """One flag changes one setting, as `attach --help` says (b/571028963)."""
    deployment = _StoredDeployment(_STORED_CLOUD_OPS)
    monkeypatch.setattr(aqua_cli, "_fetch_command_result", deployment)

    # A table the sink exports to, so discovery finds the rest of the sink.
    result = _invoke_attach(*seed, "--telemetry-table", _TABLE)

    assert result.exit_code == 0, result.output
    fields = deployment.extract_planned_fields()
    assert fields["telemetry_table"] == _TABLE
    for field in (
        "telemetry_ingestion_source",
        "telemetry_dataset",
        "telemetry_location",
    ):
        assert field not in fields
    assert "trace_spans_linked  (discovered" in result.output
    assert f"telemetry_table: _AllSpans -> {_TABLE}" in result.output
    assert "telemetry_dataset: trace_spans_linked ->" not in result.output


def test_a_changed_source_takes_the_telemetry_discovered_for_it(
    monkeypatch, api
):
    """The stored dataset, table and location belong to the old source."""
    deployment = _StoredDeployment(_STORED_CLOUD_OPS)
    monkeypatch.setattr(aqua_cli, "_fetch_command_result", deployment)

    result = _invoke_attach(
        "--observed-agent-resource",
        _ENGINE,
        "--telemetry-source",
        "cloud_logging",
    )

    assert result.exit_code == 0, result.output
    fields = deployment.extract_planned_fields()
    assert fields["telemetry_ingestion_source"] == "cloud_logging"
    assert fields["telemetry_dataset"] == "it_telemetry"
    assert fields["telemetry_table"] == _TABLE
    assert fields["telemetry_location"] == "us-east1"


def test_a_changed_dataset_takes_the_location_discovered_for_it(
    monkeypatch, api
):
    deployment = _StoredDeployment(
        {**_STORED_CLOUD_OPS, "telemetry_ingestion_source": "cloud_logging"}
    )
    monkeypatch.setattr(aqua_cli, "_fetch_command_result", deployment)

    result = _invoke_attach(
        "--observed-agent-resource",
        _ENGINE,
        "--telemetry-dataset",
        "it_telemetry",
    )

    assert result.exit_code == 0, result.output
    fields = deployment.extract_planned_fields()
    assert fields["telemetry_location"] == "us-east1"
    assert "telemetry_table" not in fields


def test_an_attached_agent_gets_a_discovered_value_for_an_empty_setting(
    monkeypatch, api
):
    deployment = _StoredDeployment(
        {**_STORED_CLOUD_OPS, "telemetry_payload_bucket": ""}
    )
    monkeypatch.setattr(aqua_cli, "_fetch_command_result", deployment)

    result = _invoke_attach("--observed-agent-resource", _ENGINE)

    assert result.exit_code == 0, result.output
    fields = deployment.extract_planned_fields()
    assert fields["telemetry_payload_bucket"] == f"{_PROJECT}-logs"
    assert "telemetry_dataset" not in fields


def test_rediscover_replaces_the_stored_values_with_discovered_ones(
    monkeypatch, api
):
    deployment = _StoredDeployment(
        {**_STORED_CLOUD_OPS, "observed_deployment_name": "old-name"}
    )
    monkeypatch.setattr(aqua_cli, "_fetch_command_result", deployment)

    result = _invoke_attach(
        "--observed-agent-resource", _ENGINE, "--rediscover"
    )

    assert result.exit_code == 0, result.output
    fields = deployment.extract_planned_fields()
    assert fields["observed_deployment_name"] == "it-support-agent"
    assert fields["telemetry_ingestion_source"] == "cloud_logging"
    assert "Stored values kept" not in result.output
    # Nothing is kept, so no plan is asked for what the agent stores.
    assert [r for r in deployment.requests if not r.get("check_access")] == []


def test_rediscover_and_no_discover_are_refused_together(deployment):
    result = _invoke_attach("it_agent", "--no-discover", "--rediscover")

    assert result.exit_code == 2
    assert "cannot be combined with --no-discover" in result.output
    assert deployment.requests == []


# --- the Cloud Trace link at the command line -------------------------------------- #

_LINK_TABLE = f"{_PROJECT}.{discovery_lib.DEFAULT_TRACE_LINK}._AllSpans"
_LINK_ACCESS_MISSING = {
    "service_account": "aqua@p.iam.gserviceaccount.com",
    "checks": [
        {
            "name": "table",
            "status": "missing",
            "target": _LINK_TABLE,
            "detail": "Not found",
        }
    ],
}
_LINK_ACCESS_PASSED = {
    "service_account": "aqua@p.iam.gserviceaccount.com",
    "checks": [
        {
            "name": "table",
            "status": "passed",
            "target": _LINK_TABLE,
            "detail": "",
        }
    ],
}


class _CheckingDeployment:
    """Answers each attach call with the next scripted access check."""

    def __init__(self, *accesses: dict[str, Any]):
        """Initializes the fake.

        Args:
            *accesses: The access check of each attach call in turn; the last
                answers every call after it.
        """
        self.accesses = list(accesses)
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, *, url, resource, path, payload=None):
        """Answers one route call, standing in for `_fetch_command_result`.

        Args:
            url: Ignored engine URL.
            resource: Ignored engine resource name.
            path: Route path.
            payload: Request body.

        Returns:
            The scripted response.
        """
        self.calls.append((path, payload))
        if path == "agents/applied":
            return {"agent_name": payload["agent_name"], "unapplied_fields": []}
        if payload["dry_run"] and not payload.get("check_access"):
            # The plan that tells discovery what is stored checks nothing.
            access = None
        elif len(self.accesses) > 1:
            access = self.accesses.pop(0)
        else:
            access = self.accesses[0]
        return {
            "agent_name": "it_agent",
            "object": "gs://b/agents/it_agent.json",
            "attached": False,
            "watched": True,
            "changes": [],
            "triggers": {},
            "trigger_target": {
                "region": "us-central1",
                "aqua_engine": _AQUA,
                "ambient_topic": f"projects/{_PROJECT}/topics/t",
                "ambient_caller_email": "caller@p.iam.gserviceaccount.com",
            },
            "access": access,
        }

    def list_writes(self) -> list[Any]:
        """Lists the attach calls that were not dry runs.

        Returns:
            Their request bodies.
        """
        return [
            p
            for path, p in self.calls
            if path == "agents/attach" and not p["dry_run"]
        ]


_LINKED_DATASET = (
    f"{_BQ}/projects/{_PROJECT}/datasets/{discovery_lib.DEFAULT_TRACE_LINK}"
)


@pytest.fixture
def unlinked_api(monkeypatch):
    # Served from the start: discovery reads it only for a link that exists,
    # and attach waits for it once it creates one.
    fake = _FakeApi(
        _build_trace_responses([], **{_LINKED_DATASET: {"location": "US"}})
    )
    monkeypatch.setattr(
        rest_client_lib, "RestClient", lambda *args, **kwargs: fake
    )
    return fake


@pytest.fixture
def link_commands(tmp_path, monkeypatch) -> list[list[str]]:
    """Records the commands attach runs.

    Args:
        tmp_path: Temporary directory fixture, made the working directory.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        Each command's argument vector, in order.
    """
    monkeypatch.chdir(tmp_path)
    commands: list[list[str]] = []

    def run_command(argv: list[str]) -> int:
        """Records a command as run, and reports it succeeded.

        Args:
            argv: Command-line arguments vector.

        Returns:
            Exit status 0.
        """
        commands.append(argv)
        return 0

    monkeypatch.setattr(aqua_cli, "_run_command", run_command)
    monkeypatch.setattr(aqua_cli.shutil, "which", lambda p: f"/bin/{p}")
    monkeypatch.setattr(trace_link_lib.time, "sleep", lambda seconds: None)
    return commands


def _install_checking_deployment(monkeypatch, *accesses) -> _CheckingDeployment:
    """Replaces the CLI's route call with a `_CheckingDeployment`.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        *accesses: Arguments for `_CheckingDeployment`.

    Returns:
        The installed fake.
    """
    fake = _CheckingDeployment(*accesses)
    monkeypatch.setattr(aqua_cli, "_fetch_command_result", fake)
    return fake


def _invoke_attach_unlinked(*args: str, stdin: str | None = None):
    """Runs `attach` for the engine whose spans have no BigQuery link.

    Args:
        *args: More arguments.
        stdin: Text fed to prompts.

    Returns:
        The Click invocation result.
    """
    return CliRunner().invoke(
        aqua_cli.cli,
        [
            "attach",
            "--aqua-resource",
            _AQUA,
            "--observed-agent-resource",
            _ENGINE,
            *args,
        ],
        input=stdin,
    )


_LINK_COMMAND = discovery_lib.TraceLink(
    _PROJECT, "us", discovery_lib.DEFAULT_TRACE_LINK, exists=False
).build_create_argv()


def test_a_dry_run_prints_the_link_command_and_creates_no_link(
    monkeypatch, unlinked_api, link_commands
):
    """The table the check finds missing is the one the link creates, so the
    dry run goes ahead."""
    fake = _install_checking_deployment(monkeypatch, _LINK_ACCESS_MISSING)

    result = _invoke_attach_unlinked("--dry-run")

    assert result.exit_code == 0, result.output
    assert shlex.join(_LINK_COMMAND) in result.output
    assert "attach creates the link first" in result.output
    assert (
        fake.calls[0][1]["fields"]["telemetry_ingestion_source"] == "cloud_ops"
    )
    assert fake.list_writes() == []
    assert link_commands == []


def test_attach_creates_the_link_then_checks_the_reads(
    monkeypatch, unlinked_api, link_commands
):
    fake = _install_checking_deployment(
        monkeypatch, _LINK_ACCESS_MISSING, _LINK_ACCESS_PASSED
    )

    result = _invoke_attach_unlinked("--yes")

    assert result.exit_code == 0, result.output
    assert link_commands == [_LINK_COMMAND]
    assert [path for path, _ in fake.calls] == [
        # What is stored, the plan, the plan once the link exists, the write.
        "agents/attach",
        "agents/attach",
        "agents/attach",
        "agents/attach",
        "agents/applied",
    ]
    assert fake.list_writes()[0]["fields"]["telemetry_dataset"] == (
        discovery_lib.DEFAULT_TRACE_LINK
    )
    # Only the check made once the link exists is shown.
    assert "MISSING" not in result.output
    assert f"table  ok {_LINK_TABLE}" in result.output


def test_attach_asks_before_creating_the_link(
    monkeypatch, unlinked_api, link_commands
):
    fake = _install_checking_deployment(monkeypatch, _LINK_ACCESS_MISSING)

    result = _invoke_attach_unlinked(stdin="n\n")

    assert result.exit_code == 1
    assert "Run the commands above with your credentials?" in result.output
    assert link_commands == []
    assert fake.list_writes() == []


def test_a_failed_link_stores_nothing(monkeypatch, unlinked_api, link_commands):
    fake = _install_checking_deployment(monkeypatch, _LINK_ACCESS_MISSING)
    monkeypatch.setattr(aqua_cli, "_run_command", lambda argv: 1)

    result = _invoke_attach_unlinked("--yes")

    assert result.exit_code == 1
    assert "Nothing was stored or granted" in result.output
    assert fake.list_writes() == []


def test_an_existing_link_is_not_created_again(monkeypatch, link_commands):
    api = _FakeApi(_build_trace_responses([discovery_lib.DEFAULT_TRACE_LINK]))
    monkeypatch.setattr(
        rest_client_lib, "RestClient", lambda *args, **kwargs: api
    )
    _install_checking_deployment(monkeypatch, _LINK_ACCESS_PASSED)

    result = _invoke_attach_unlinked("--yes")

    assert result.exit_code == 0, result.output
    assert link_commands == []
    assert "gcloud observability" not in result.output


def _install_fake_api(monkeypatch, responses: dict[str, Any]) -> _FakeApi:
    """Makes every REST client the CLI builds the same `_FakeApi`.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        responses: Replies by URL.

    Returns:
        The installed fake.
    """
    fake = _FakeApi(responses)
    monkeypatch.setattr(
        rest_client_lib, "RestClient", lambda *args, **kwargs: fake
    )
    return fake


def test_attach_creates_the_bucket_then_the_link(monkeypatch, link_commands):
    api = _install_fake_api(
        monkeypatch,
        _build_trace_responses(
            [],
            **{_BUCKETS: {"buckets": []}, _LINKED_DATASET: {"location": "EU"}},
        ),
    )
    api.responses[_SETTINGS] = {"defaultStorageLocation": "eu"}
    fake = _install_checking_deployment(
        monkeypatch, _LINK_ACCESS_MISSING, _LINK_ACCESS_PASSED
    )

    result = _invoke_attach_unlinked("--yes")

    assert result.exit_code == 0, result.output
    assert (
        link_commands
        == discovery_lib.TraceLink(
            _PROJECT,
            "eu",
            discovery_lib.DEFAULT_TRACE_LINK,
            exists=False,
            bucket_exists=False,
        ).build_needed_argvs()
    )
    fields = fake.list_writes()[0]["fields"]
    assert fields["telemetry_ingestion_source"] == "cloud_ops"
    assert fields["telemetry_location"] == "EU"


def _build_disabled_api_responses() -> dict[str, Any]:
    """Builds the replies of a project whose Observability API is off.

    The user may enable it, and once enabled the project has a `_Trace`
    bucket with no link.

    Returns:
        Replies by URL.
    """
    return _build_trace_responses(
        [],
        **{
            _BUCKETS: _API_DISABLED,
            _TEST_PERMISSIONS: {
                "permissions": ["serviceusage.services.enable"]
            },
            _LINKED_DATASET: {"location": "US"},
        },
    )


def test_attach_enables_the_api_then_looks_up_and_links(
    monkeypatch, link_commands
):
    api = _install_fake_api(monkeypatch, _build_disabled_api_responses())
    enabled_buckets = _build_trace_responses([])[_BUCKETS]
    polls: list[str] = []

    def run_command(argv: list[str]) -> int:
        """Records a command, and turns the API on once it is enabled.

        Args:
            argv: Command-line arguments vector.

        Returns:
            Exit status 0.
        """
        link_commands.append(argv)
        if argv[:3] == ["gcloud", "services", "enable"]:
            # The API answers SERVICE_DISABLED once more after enabling.
            def answer(payload):
                """Answers SERVICE_DISABLED once, then lists the bucket.

                Args:
                    payload: Ignored query parameters.

                Returns:
                    The bucket listing.

                Raises:
                    ApiError: On the first lookup.
                """
                polls.append("poll")
                if len(polls) == 1:
                    raise _API_DISABLED
                return enabled_buckets

            api.responses[_BUCKETS] = answer
        return 0

    monkeypatch.setattr(aqua_cli, "_run_command", run_command)
    fake = _install_checking_deployment(
        monkeypatch, _LINK_ACCESS_MISSING, _LINK_ACCESS_PASSED
    )

    result = _invoke_attach_unlinked("--yes")

    assert result.exit_code == 0, result.output
    assert [argv[:3] for argv in link_commands] == [
        ["gcloud", "services", "enable"],
        _LINK_COMMAND[:3],
    ]
    assert link_commands[1] == _LINK_COMMAND
    assert len(polls) >= 2
    fields = fake.list_writes()[0]["fields"]
    assert fields["telemetry_dataset"] == discovery_lib.DEFAULT_TRACE_LINK
    assert fields["telemetry_location"] == "US"


def test_a_failed_enable_stores_nothing(monkeypatch, link_commands):
    _install_fake_api(monkeypatch, _build_disabled_api_responses())
    monkeypatch.setattr(aqua_cli, "_run_command", lambda argv: 1)
    fake = _install_checking_deployment(monkeypatch, _LINK_ACCESS_MISSING)

    result = _invoke_attach_unlinked("--yes")

    assert result.exit_code == 1
    assert "gcloud services enable" in result.output
    assert "Nothing was stored or granted" in result.output
    assert fake.list_writes() == []
