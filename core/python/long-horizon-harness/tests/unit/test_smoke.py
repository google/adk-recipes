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

"""Smoke tests proving the test infrastructure is wired correctly."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService


def test_app_module_loads() -> None:
    """The app entrypoint must import without side-effects that break tests."""
    from app import agent as agent_module

    assert agent_module.app is not None
    assert agent_module.root_agent is not None
    assert agent_module.app.root_agent is agent_module.root_agent


def test_fake_services_are_isolated(
    fake_memory_service: InMemoryMemoryService,
    fake_session_service: InMemorySessionService,
) -> None:
    assert isinstance(fake_memory_service, InMemoryMemoryService)
    assert isinstance(fake_session_service, InMemorySessionService)


def test_runner_factory_builds_runner(
    runner_factory: Callable[..., Runner],
) -> None:
    from app.agent import root_agent

    runner = runner_factory(agent=root_agent, app_name="smoke")
    assert isinstance(runner, Runner)
    assert runner.app_name == "smoke"
    assert runner.agent is root_agent


def test_credentials_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_API_KEY", "should-be-unset-next-test")
    # The autouse hermetic fixture has already run for this test, so the
    # var we just set survives this test only — the next test starts clean.
    import os

    assert "FAKE_API_KEY" in os.environ


def test_credentials_dont_leak_across_tests() -> None:
    import os

    assert "FAKE_API_KEY" not in os.environ


def test_deterministic_env() -> None:
    import os

    assert os.environ["TZ"] == "UTC"
    assert os.environ["PYTHONHASHSEED"] == "0"
