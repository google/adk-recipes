# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The package, the agent and the web app import without credentials."""

import importlib
from unittest.mock import patch

import pytest
from google.auth.exceptions import DefaultCredentialsError


@pytest.fixture
def no_adc(monkeypatch):
    """No Application Default Credentials and no GCE metadata probe."""
    monkeypatch.setenv("NO_GCE_CHECK", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    no_credentials = DefaultCredentialsError("no credentials in this test")
    with patch("google.auth.default", side_effect=no_credentials):
        yield


def test_package_imports(no_adc):
    """The package imports without credentials."""
    assert importlib.import_module("genmedia4commerce") is not None


def test_agent_runnability(no_adc):
    """The agent package exposes the Studio agent as ``root_agent`` and
    ``app``, also as the lazy ``genmedia4commerce.agent`` ADK reads."""
    agent = importlib.import_module("genmedia4commerce.agent")
    studio = importlib.import_module("genmedia4commerce.studio.agent")
    assert agent.root_agent is studio.root_agent
    assert agent.app is studio.app
    assert agent.app.name == "genmedia4commerce"
    package = importlib.import_module("genmedia4commerce")
    assert package.agent is agent


def test_api_app_runnability(no_adc):
    """The FastAPI app builds at import."""
    api_app = importlib.import_module("genmedia4commerce.api.app")
    assert api_app.app is not None
