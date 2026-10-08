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
from google.adk.cli.service_registry import get_service_registry

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
