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

"""Wiring tests for ``root_agent.code_executor``.

The slot is env-driven: both ``AGENT_ENGINE_RESOURCE_NAME`` and
``CODE_SANDBOX_RESOURCE_NAME`` unset → ``None`` (no-op so dev playground
works without GCP infra); either set → ``AgentEngineSandboxCodeExecutor``
with ``stateful=True`` so vars/files persist across calls in one session.

The slot must never be ``UnsafeLocalCodeExecutor`` (which ``exec()``s
LLM-generated Python in the current process) in either state.
"""

from __future__ import annotations

import importlib

import pytest
from google.adk.code_executors import UnsafeLocalCodeExecutor
from google.adk.code_executors.agent_engine_sandbox_code_executor import (
    AgentEngineSandboxCodeExecutor,
)

_FAKE_ENGINE_RESOURCE = (
    "projects/fake/locations/us-central1/reasoningEngines/123"
)
_FAKE_SANDBOX_RESOURCE = "projects/fake/locations/us-central1/reasoningEngines/123/sandboxEnvironments/456"


def _reload_app_agent():
    import app.agent

    importlib.reload(app.agent)
    return app.agent


# =============================================================================
# Env-driven wire
# =============================================================================


def test_code_executor_is_none_when_env_unset(monkeypatch: pytest.MonkeyPatch):
    """With neither ``AGENT_ENGINE_RESOURCE_NAME`` nor
    ``CODE_SANDBOX_RESOURCE_NAME`` set, the slot must be ``None`` so dev
    playground and ``agents-cli run`` work without GCP infra. Pins the
    graceful no-op contract."""
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)
    monkeypatch.delenv("CODE_SANDBOX_RESOURCE_NAME", raising=False)

    app_agent = _reload_app_agent()

    assert app_agent.root_agent.code_executor is None, (
        "Without an env-provided resource name, the slot must be None — "
        "constructing AgentEngineSandboxCodeExecutor with no resource "
        "would later fail at execute() time on a misleading error."
    )


def test_code_executor_wired_when_engine_resource_env_set(
    monkeypatch: pytest.MonkeyPatch,
):
    """With ``AGENT_ENGINE_RESOURCE_NAME`` set, the slot must be an
    ``AgentEngineSandboxCodeExecutor`` with ``stateful=True`` so vars/files
    survive across calls in the same session."""
    monkeypatch.setenv("AGENT_ENGINE_RESOURCE_NAME", _FAKE_ENGINE_RESOURCE)
    monkeypatch.delenv("CODE_SANDBOX_RESOURCE_NAME", raising=False)

    app_agent = _reload_app_agent()
    executor = app_agent.root_agent.code_executor

    assert isinstance(executor, AgentEngineSandboxCodeExecutor), (
        f"Expected AgentEngineSandboxCodeExecutor, got {type(executor).__name__}"
    )
    assert executor.stateful is True, (
        "stateful=True required — without it, each code call gets a "
        "fresh container and variables don't persist across turns."
    )


# =============================================================================
# Safety
# =============================================================================


def test_code_executor_is_never_unsafe_local_executor(
    monkeypatch: pytest.MonkeyPatch,
):
    """Defense-in-depth: in BOTH env states (unset → None, set →
    AgentEngineSandboxCodeExecutor) the slot must not be
    ``UnsafeLocalCodeExecutor``. That executor ``exec()``s arbitrary
    LLM-generated Python in the current process. Catches an accidental
    swap before it lands on main."""
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)
    monkeypatch.delenv("CODE_SANDBOX_RESOURCE_NAME", raising=False)
    app_agent = _reload_app_agent()
    assert not isinstance(
        app_agent.root_agent.code_executor, UnsafeLocalCodeExecutor
    )

    monkeypatch.setenv("AGENT_ENGINE_RESOURCE_NAME", _FAKE_ENGINE_RESOURCE)
    app_agent = _reload_app_agent()
    assert not isinstance(
        app_agent.root_agent.code_executor, UnsafeLocalCodeExecutor
    ), (
        "root_agent.code_executor must NOT be UnsafeLocalCodeExecutor — "
        "that executor runs arbitrary LLM code locally via exec()"
    )


# =============================================================================
# Boot safety
# =============================================================================


def test_root_agent_boots_in_either_env_state(monkeypatch: pytest.MonkeyPatch):
    """Importing ``app.agent`` must not raise in either env state. ADK
    validates Agent config at construction time; a bad slot (e.g.,
    pydantic-incompatible executor) would surface here. Also pins that
    ``_build_code_executor()`` doesn't call out to GCP at import time
    (it constructs the executor object only)."""
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_NAME", raising=False)
    monkeypatch.delenv("CODE_SANDBOX_RESOURCE_NAME", raising=False)
    app_agent = _reload_app_agent()
    assert app_agent.root_agent is not None

    monkeypatch.setenv("CODE_SANDBOX_RESOURCE_NAME", _FAKE_SANDBOX_RESOURCE)
    app_agent = _reload_app_agent()
    assert app_agent.root_agent is not None
