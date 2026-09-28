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

"""Shared fixtures for the lha test suite.


1. **No credential env vars.** All provider/credential-shaped env vars
   (ending in _API_KEY, _TOKEN, _SECRET, _PASSWORD, _CREDENTIALS) are
   unset before every test so a developer's local keys cannot leak in.
2. **Deterministic runtime.** TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0.
3. **Isolated tmpdir** for any file-backed state — file-tool tests get a
   fresh CWD-shaped scratch area per test.
4. **In-memory ADK services.** Tests use `InMemoryMemoryService` /
   `InMemorySessionService` — never real Vertex / Memory Bank.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from google.adk.agents import BaseAgent
from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from app.environment import LocalEnvironment

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


_CREDENTIAL_SUFFIXES = (
    "_API_KEY",
    "_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_CREDENTIALS",
    "_ACCESS_KEY",
    "_PRIVATE_KEY",
    "_OAUTH_TOKEN",
)


def _looks_like_credential(name: str) -> bool:
    return any(name.endswith(suf) for suf in _CREDENTIAL_SUFFIXES)


@pytest.fixture(autouse=True)
def _hermetic_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ.keys()):
        if _looks_like_credential(name):
            monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("PYTHONHASHSEED", "0")

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(scratch)


@pytest.fixture(autouse=True)
def _scoped_environment(tmp_path: Path) -> Iterator[None]:
    """Bind a per-test ``LocalEnvironment`` rooted at ``tmp_path`` so
    path-scoped tools (file_ops, terminal) resolve under it. Tests that
    need a different working_dir override the binding by calling
    ``set_active_environment`` themselves."""
    from app.context.compaction_context import clear_compaction_context
    from app.environment_context import (
        clear_active_environment,
        set_active_environment,
    )

    set_active_environment(LocalEnvironment(working_dir=tmp_path))
    clear_compaction_context()
    try:
        yield
    finally:
        clear_active_environment()
        clear_compaction_context()


@pytest.fixture()
def fake_memory_service() -> InMemoryMemoryService:
    return InMemoryMemoryService()


@pytest.fixture()
def fake_session_service() -> InMemorySessionService:
    return InMemorySessionService()


@pytest.fixture()
def runner_factory(
    fake_session_service: InMemorySessionService,
    fake_memory_service: InMemoryMemoryService,
) -> Callable[..., Runner]:
    """Build a Runner wired to in-memory services.

    Usage: `runner = runner_factory(agent=my_agent, app_name="test_app")`
    Override session_service or memory_service per-call when a test needs
    a different fake.
    """

    def _build(
        *,
        agent: BaseAgent,
        app_name: str = "test_app",
        session_service: InMemorySessionService | None = None,
        memory_service: InMemoryMemoryService | None = None,
    ) -> Runner:
        return Runner(
            app_name=app_name,
            agent=agent,
            session_service=session_service or fake_session_service,
            memory_service=memory_service or fake_memory_service,
        )

    return _build
