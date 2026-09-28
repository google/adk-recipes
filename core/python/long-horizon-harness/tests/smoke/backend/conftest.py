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

"""Session-scoped sandbox + per-test glue for the backend smoke suite.

One sandbox is provisioned at session start and reused across every
backend smoke test — provisioning is the expensive op (multi-second
cold start hitting Agent Engine); routing through one shared instance
keeps the suite from paying that cost N times.

If ``LHA_RUNTIME_IMAGE`` and ``LHA_SANDBOX_CALLER_SA`` are unset,
``_build_sandbox_environment`` silently degrades to a ``LocalEnvironment``
under a per-session tmpdir — that path is still worth exercising
because the artifact pipeline, callbacks, and tool dispatch all share
code regardless of backend.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from google.adk.environment import BaseEnvironment
from google.genai import types as genai_types


class _FakeActions:
    """Mirror of ``EventActions.artifact_delta``."""

    def __init__(self) -> None:
        self.artifact_delta: dict[str, int] = {}


class _FakeToolContext:
    """``ToolContext`` stub covering the artifact API surface."""

    def __init__(self) -> None:
        self._store: dict[str, list[genai_types.Part]] = {}
        self.actions = _FakeActions()

    async def save_artifact(
        self,
        filename: str,
        artifact: genai_types.Part,
        custom_metadata: dict[str, Any] | None = None,
    ) -> int:
        versions = self._store.setdefault(filename, [])
        versions.append(artifact)
        return len(versions) - 1

    async def load_artifact(
        self, filename: str, version: int | None = None
    ) -> genai_types.Part | None:
        versions = self._store.get(filename)
        if not versions:
            return None
        return versions[-1 if version is None else version]


@pytest.fixture(scope="session")
def smoke_user_id() -> str:
    """Stable per-session user_id keyed by a fresh UUID — keeps the
    smoke suite's snapshot lineage isolated from any real user's."""
    return f"smoke-{uuid.uuid4().hex[:12]}"


@pytest.fixture(scope="session")
def smoke_local_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Point ``LHA_LOCAL_ROOT`` at a session tmpdir so the smoke
    suite's snapshot index doesn't touch the developer's
    ``~/.lha/``."""
    root = tmp_path_factory.mktemp("smoke-local-root")
    os.environ["LHA_LOCAL_ROOT"] = str(root)
    return root


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def shared_environment(
    smoke_user_id: str, smoke_local_root: Path
) -> AsyncIterator[BaseEnvironment]:
    """One ``BaseEnvironment`` provisioned for the whole smoke session.

    Tests reuse this instance — see ``active_env`` for the per-test
    binding into the ``ContextVar`` that ``app/tools/*`` resolve against.

    Session-scoped event loop is required because the real
    ``SandboxEnvironment`` binds internal ``asyncio.Event`` / ``Lock``
    objects to the loop that called ``initialize()``; per-test loops
    would each see a "bound to a different event loop" error.
    """
    # Imported lazily so test collection doesn't fail if horizon imports
    # blow up under unusual env (e.g. missing GOOGLE_APPLICATION_CREDENTIALS).
    from app.conversation.session_start import _build_environment

    env, _ = _build_environment(smoke_user_id)
    await env.initialize()
    try:
        yield env
    finally:
        await env.close()


@pytest.fixture
def active_env(
    shared_environment: BaseEnvironment,
) -> Iterator[BaseEnvironment]:
    """Bind ``shared_environment`` into the ContextVar for the test body
    and clear it on teardown."""
    from app.environment_context import (
        clear_active_environment,
        set_active_environment,
    )

    set_active_environment(shared_environment)
    try:
        yield shared_environment
    finally:
        clear_active_environment()


@pytest.fixture
def tool_context() -> _FakeToolContext:
    """Fresh per-test ``ToolContext`` stub. Tests that need state to
    persist across two tool calls just reuse the same instance within
    one test body."""
    return _FakeToolContext()


@pytest.fixture
def unique_name() -> str:
    """Per-test filename prefix so concurrent tests don't clobber each
    other inside the shared workspace."""
    return f"smoke_{uuid.uuid4().hex[:8]}"
