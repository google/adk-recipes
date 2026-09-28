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

"""Composition smoke for the routine foundation: manifest → scoped secrets →
headless guard, exercised together as the fire path will wire them. No LLM, no
GCP, no sandbox — uses the on-disk .lha/routines overlay + a fake secret store.
Gated by RUN_SMOKE=1 via tests/smoke/conftest.py.
"""

from __future__ import annotations

import pytest

from app.environment import LocalEnvironment
from app.environment_context import set_active_environment
from app.guardrails.permission_guard import (
    permission_guard,
    reset_headless_mode,
    set_headless_mode,
)
from app.routines.manifest import write_routine_via_env
from app.secrets import set_secret_store
from app.secrets.inject import scoped_secret_env
from app.secrets.store import SecretManagerStore
from tests.stubs import FakeTool, FakeToolContext
from tests.unit.test_secret_store import FakeSecretClient

pytestmark = pytest.mark.asyncio

_Tool = FakeTool
_Ctx = FakeToolContext


async def test_routine_foundation_resolution_path(tmp_path):
    class OwnedEnv(LocalEnvironment):
        @property
        def owner(self):  # type: ignore[override]
            return "alice@x"

    env = OwnedEnv(working_dir=tmp_path)
    set_active_environment(env)

    store = SecretManagerStore(
        client=FakeSecretClient(), project_id="proj", ttl_s=60.0
    )
    set_secret_store(store)
    await store.set_secret("alice@x", "STRIPE_KEY", "sk_live")
    await store.set_secret("alice@x", "GITHUB_PAT", "ghp_x")

    try:
        # 1. Author + load a manifest on the real .lha/routines overlay.
        manifest = await write_routine_via_env(
            env,
            {
                "name": "Daily digest",
                "schedule": "0 8 * * *",
                "task": "summarize and report",
                "secrets": ["STRIPE_KEY"],
            },
            routine_id="digest",
        )
        assert manifest.secrets == ("STRIPE_KEY",)

        # 2. Scoped injection yields ONLY the declared secret.
        env = await scoped_secret_env(manifest.secrets)
        assert env == {"STRIPE_KEY": "sk_live"}
        assert "GITHUB_PAT" not in env

        # 3. Headless guard: a non-shell approval is fail-closed, but a shell
        #    command runs (it's confined to the routine's isolated sandbox).
        token = set_headless_mode(True)
        try:
            denied = await permission_guard(
                tool=_Tool("send_email"),
                args={"to": "x@y"},
                tool_context=_Ctx(),
            )
            assert denied is not None and denied.get("headless_denied") is True

            shell_ctx = _Ctx()
            shell_ok = await permission_guard(
                tool=_Tool("terminal"),
                args={"command": "git clone https://example.com/repo"},
                tool_context=shell_ctx,
            )
            assert shell_ok is None
            assert shell_ctx._requested is None
        finally:
            reset_headless_mode(token)
    finally:
        set_secret_store(None)
