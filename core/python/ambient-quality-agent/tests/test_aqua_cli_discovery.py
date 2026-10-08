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
from typing import Any

import pytest
from ambient_quality_cli import aqua_cli
from ambient_quality_cli import discovery as discovery_lib
from ambient_quality_cli import rest_client as rest_client_lib
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


def _list_sink_fields(discovery) -> list[str]:
    """Lists the discovered telemetry fields a chosen sink sets.

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
    assert discovery.notes == []


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


def test_another_telemetry_source_is_left_to_its_flags():
    api = _FakeApi(_build_responses())

    discovery = _discover(
        api, given={"telemetry_ingestion_source": "big_query"}
    )

    assert _SINKS not in api.list_urls()
    assert _list_sink_fields(discovery) == []
    # The agent's project is still discovered: the telemetry is read there.
    assert discovery.values["observed_project_id"] == DiscoveredValue(
        _PROJECT, "project of the observed engine"
    )


def test_a_given_dataset_narrows_the_sinks():
    api = _FakeApi(_build_responses())

    discovery = _discover(api, given={"telemetry_dataset": "elsewhere"})

    assert _list_sink_fields(discovery) == []
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
    assert discovery.notes == []


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


def test_no_sink_leaves_the_telemetry_fields_alone():
    api = _FakeApi(_build_responses(**{_SINKS: {"sinks": []}}))

    discovery = _discover(api)

    assert _list_sink_fields(discovery) == []
    assert any(
        "agents-cli infra single-project --apply" in note
        for note in discovery.notes
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

    assert _list_sink_fields(discovery) == []
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
