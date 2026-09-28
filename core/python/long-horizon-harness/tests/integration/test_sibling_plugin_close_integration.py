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

"""ADK PluginManager.close() must not raise for an in-flight SiblingAgentPlugin.

Reproduces the production failure path exactly: ADK's PluginManager wraps each
plugin's close() in asyncio.timeout(close_timeout) and re-raises any exception
as RuntimeError("Failed to close plugins: ..."). With a slow sibling in flight,
the drain must finish (or be absorbed) within that budget.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.adk.plugins.plugin_manager import PluginManager

pytestmark = pytest.mark.asyncio


async def test_adk_plugin_manager_close_does_not_raise_with_slow_sibling(
    monkeypatch,
):
    from app.memory import sibling_agent_plugin

    started = asyncio.Event()

    async def slow_run_async(**_kw: Any) -> AsyncIterator[Any]:
        started.set()
        await asyncio.sleep(30)  # outlives any plugin-close budget
        return
        yield  # pragma: no cover

    runner = MagicMock()
    runner.run_async = slow_run_async
    monkeypatch.setattr(
        sibling_agent_plugin, "_build_runner", lambda **_kw: runner
    )

    plugin = sibling_agent_plugin.SiblingAgentPlugin()
    task = plugin.spawn_sibling(
        agent=MagicMock(),
        prompt="x",
        app_name="a",
        user_id="u",
        memory_service=object(),
    )
    await started.wait()

    # ADK default close budget is 5.0s; use a tiny one to keep the test fast
    # and to prove the plugin returns well under any realistic budget.
    manager = PluginManager(plugins=[plugin], close_timeout=0.2)

    # This is the call that raised in production. It must complete cleanly.
    await manager.close()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
