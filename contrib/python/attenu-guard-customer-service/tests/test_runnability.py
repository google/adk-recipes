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
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.

"""Runnability tests for the recipe."""

import pytest
from attenu_guard.adapters.google_adk import DelegationGuardPlugin

import app.agent


@pytest.fixture(autouse=True)
def _cli_model_env(monkeypatch):
    """The CLI-facing module attributes are built lazily and require
    MODEL_NAME (there is deliberately no in-code default): provide one for
    these tests and reset the singleton cache around each."""
    monkeypatch.setenv("MODEL_NAME", "test-model")
    app.agent._cli_singletons.clear()
    yield
    app.agent._cli_singletons.clear()


def test_cli_objects_require_model_name(monkeypatch):
    monkeypatch.delenv("MODEL_NAME", raising=False)
    app.agent._cli_singletons.clear()
    with pytest.raises(RuntimeError, match="MODEL_NAME"):
        _ = app.agent.app


def test_agent_runnability() -> None:
    """agent.py imports and defines the expected globals."""
    assert app.agent.root_agent is not None
    assert app.agent.app is not None


def test_app_carries_the_guard() -> None:
    """The App the ADK CLI loads has the plugin attached."""
    plugins = getattr(app.agent.app, "plugins", None) or []
    assert any(isinstance(p, DelegationGuardPlugin) for p in plugins)


def test_sub_agent_is_reachable_by_transfer() -> None:
    names = [a.name for a in app.agent.root_agent.sub_agents]
    assert names == ["billing_agent"]
