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

"""Where the agent tells ADK to keep its sessions."""

import pytest
from ambient_quality_agent.app_utils import services
from ambient_quality_shared.usage_headers import (
    build_product_token,
    build_usage_headers,
    detect_platform,
)
from google.adk.cli.service_registry import get_service_registry
from google.adk.cli.utils.service_factory import (
    create_session_service_from_options,
)

_INJECTED = {
    "GOOGLE_CLOUD_AGENT_ENGINE_ID": "1234567890",
    "GOOGLE_CLOUD_PROJECT": "111111111111",
    "GOOGLE_CLOUD_AGENT_ENGINE_LOCATION": "us-central1",
}

_EXPECTED = (
    "agentengine://projects/111111111111"
    "/locations/us-central1/reasoningEngines/1234567890"
)


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SESSION_SERVICE_URI", *_INJECTED):
        monkeypatch.delenv(name, raising=False)


def test_names_the_engine_in_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare id would be resolved against `GOOGLE_CLOUD_LOCATION`, which
    `agents-cli` sets to `global`, where a regional engine does not exist."""
    _clear(monkeypatch)
    for name, value in _INJECTED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")

    assert services._resolve_session_service_uri() == _EXPECTED


def test_a_full_resource_name_is_not_nested_inside_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent Runtime injects a bare id, but a full resource name is a valid
    setting and must not be pasted into the middle of a second one."""
    _clear(monkeypatch)
    for name, value in _INJECTED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(
        "GOOGLE_CLOUD_AGENT_ENGINE_ID", _EXPECTED.split("://", 1)[1]
    )

    assert services._resolve_session_service_uri() == _EXPECTED


def test_falls_back_to_in_memory_off_agent_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is injected locally, and a partial name would not resolve."""
    _clear(monkeypatch)
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "123")

    assert services._resolve_session_service_uri() is None


def test_explicit_uri_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    for name, value in _INJECTED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SESSION_SERVICE_URI", "agentengine://elsewhere")

    assert services._resolve_session_service_uri() == "agentengine://elsewhere"


def test_the_shared_scheme_resolves_to_the_instance_we_hold() -> None:
    """The point of `shared://`. Every surface resolving the URI through ADK's
    registry gets this process's one service, not a second one built from the
    same settings -- which is what makes a session created on one surface
    visible to the others."""
    registry = get_service_registry()

    resolved = registry.create_session_service(
        services.SESSION_SERVICE_URI, agents_dir=services.AGENT_DIR
    )

    assert resolved is services.get_session_service()


def test_the_managed_session_service_sends_the_product_token(
    monkeypatch: pytest.MonkeyPatch, usage_analytics_on
) -> None:
    from google.adk.sessions.vertex_ai_session_service import (
        VertexAiSessionService,
    )

    monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", raising=False)

    service = services._create_session_service(_EXPECTED)

    assert isinstance(service, VertexAiSessionService)
    assert (
        service._project,
        service._location,
        service._agent_engine_id,
    ) == ("111111111111", "us-central1", "1234567890")
    # ADK sends the token only while its own client builder calls this hook.
    assert hasattr(VertexAiSessionService, "_api_client_http_options_override")
    headers = service._api_client_http_options_override().headers
    assert headers == build_usage_headers(platform=detect_platform())


def test_other_session_uris_resolve_as_adk_resolves_them() -> None:
    from google.adk.sessions.in_memory_session_service import (
        InMemorySessionService,
    )

    service = services._create_session_service("memory://")

    assert isinstance(service, InMemorySessionService)


def test_the_bucket_artifact_service_sends_the_product_token(
    monkeypatch: pytest.MonkeyPatch, usage_analytics_on
) -> None:
    import google.auth
    from google.adk.artifacts.gcs_artifact_service import GcsArtifactService
    from google.auth.credentials import AnonymousCredentials

    monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", raising=False)
    monkeypatch.setattr(
        google.auth,
        "default",
        lambda *a, **k: (AnonymousCredentials(), "test-project"),
    )

    service = services._create_artifact_service("gs://aqa-jobs")

    assert isinstance(service, GcsArtifactService)
    assert service.bucket_name == "aqa-jobs"
    user_agent = service.storage_client._connection.user_agent
    assert user_agent.startswith(
        f"{build_product_token(platform=detect_platform())} "
    )


def test_the_services_send_no_usage_headers_with_the_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import google.auth
    from ambient_quality_shared import usage_headers
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    monkeypatch.setattr(usage_headers, "USAGE_ANALYTICS_ENABLED", False)
    monkeypatch.setattr(
        google.auth,
        "default",
        lambda *a, **k: (AnonymousCredentials(), "test-project"),
    )

    session = services._create_session_service(_EXPECTED)
    artifact = services._create_artifact_service("gs://aqa-jobs")

    assert session._api_client_http_options_override().headers == {}
    assert (
        artifact.storage_client._connection.user_agent
        == storage.Client(project="test-project")._connection.user_agent
    )


def test_other_artifact_uris_resolve_as_adk_resolves_them() -> None:
    from google.adk.artifacts.in_memory_artifact_service import (
        InMemoryArtifactService,
    )

    service = services._create_artifact_service("memory://")

    assert isinstance(service, InMemoryArtifactService)


@pytest.mark.parametrize(
    "uri",
    [
        "agentengine://1234567890",
        "agentengine://projects/p/locations/europe-west1/reasoningEngines/9",
    ],
)
def test_managed_session_uris_name_the_engine_adk_would(
    monkeypatch: pytest.MonkeyPatch, uri: str
) -> None:
    """The service is built here rather than by ADK's factory, so both forms
    ADK accepts must still name the same engine."""
    monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "111111111111")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    ours = services._create_session_service(uri)
    adks = create_session_service_from_options(
        base_dir=services.AGENT_DIR, session_service_uri=uri
    )

    assert (ours._project, ours._location, ours._agent_engine_id) == (
        adks._project,
        adks._location,
        adks._agent_engine_id,
    )


@pytest.mark.parametrize(
    "uri", ["agentengine://", "agentengine://projects/p/reasoningEngines/9"]
)
def test_a_malformed_managed_session_uri_is_refused(uri: str) -> None:
    with pytest.raises(ValueError):
        services._create_session_service(uri)


def test_without_adks_parser_sessions_come_from_adks_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADK's parser is private. Losing it in an ADK upgrade costs the session
    requests their token, not the engine its sessions."""
    from google.adk.sessions.vertex_ai_session_service import (
        VertexAiSessionService,
    )

    monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", raising=False)
    monkeypatch.setattr(services, "_parse_agent_engine_kwargs", None)

    service = services._create_session_service(_EXPECTED)

    assert type(service) is VertexAiSessionService
    assert (
        service._project,
        service._location,
        service._agent_engine_id,
    ) == ("111111111111", "us-central1", "1234567890")
