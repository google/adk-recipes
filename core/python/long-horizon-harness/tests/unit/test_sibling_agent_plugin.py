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

"""SiblingAgentPlugin — pins the plugin contract.

The plugin replaces the old module-level fork machinery (``_fork.spawn_fork`` +
``_fork._background_tasks`` + ``_fork.run_fork``) with a ``BasePlugin``-
hosted entrypoint. The three callers (review_fork, flush_fork, dream_review)
all hand the plugin a pre-built ``Agent`` plus the parent's
``(app_name, user_id, memory_service)``; the plugin owns runner
construction, session creation, exception swallowing, task tracking, and
graceful drain on ``close()``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.asyncio


def _fake_runner(
    events: list[Any] | None = None, *, raise_exc: bool = False
) -> Any:
    invoked: list[dict[str, Any]] = []

    async def run_async(**kwargs: Any) -> AsyncIterator[Any]:
        invoked.append(kwargs)
        if raise_exc:
            raise RuntimeError("boom")
        for event in events or []:
            yield event

    runner = MagicMock()
    runner.run_async = run_async
    runner.invoked = invoked
    return runner


# =============================================================================
# spawn_sibling — runner construction + identifiers
# =============================================================================


class TestSpawnSiblingIdentifiers:
    async def test_session_created_under_parent_ids(self, monkeypatch):
        from app.memory import sibling_agent_plugin

        captured: dict[str, Any] = {}

        def fake_build_runner(
            *,
            agent: Any,
            app_name: str,
            session_service: Any,
            memory_service: Any,
        ) -> Any:
            captured["agent"] = agent
            captured["app_name"] = app_name
            captured["session_service"] = session_service
            captured["memory_service"] = memory_service
            return _fake_runner([object()])

        monkeypatch.setattr(
            sibling_agent_plugin, "_build_runner", fake_build_runner
        )

        plugin = sibling_agent_plugin.SiblingAgentPlugin()
        memory = object()
        agent = MagicMock(name="agent")
        task = plugin.spawn_sibling(
            agent=agent,
            prompt="hello",
            app_name="parent_app",
            user_id="alice",
            memory_service=memory,
        )
        await task

        assert captured["agent"] is agent
        assert captured["app_name"] == "parent_app"
        assert captured["memory_service"] is memory
        # Session service is the plugin's own InMemorySessionService instance.
        from google.adk.sessions import InMemorySessionService

        assert isinstance(captured["session_service"], InMemorySessionService)

    async def test_passes_prompt_text_into_user_message(self, monkeypatch):
        from app.memory import sibling_agent_plugin

        runner = _fake_runner([])
        monkeypatch.setattr(
            sibling_agent_plugin, "_build_runner", lambda **_kw: runner
        )

        plugin = sibling_agent_plugin.SiblingAgentPlugin()
        task = plugin.spawn_sibling(
            agent=MagicMock(),
            prompt="PROMPT_BODY",
            app_name="a",
            user_id="u",
            memory_service=object(),
        )
        await task

        assert runner.invoked, "runner.run_async should have been called"
        new_message = runner.invoked[0]["new_message"]
        text = "".join(
            p.text for p in new_message.parts if getattr(p, "text", None)
        )
        assert text == "PROMPT_BODY"
        assert runner.invoked[0]["user_id"] == "u"


class TestSpawnSiblingPersistentSession:
    async def test_uses_provided_session_service_with_state(self, monkeypatch):
        from google.adk.sessions import InMemorySessionService

        from app.memory import sibling_agent_plugin

        captured: dict[str, Any] = {}

        def fake_build_runner(
            *,
            agent: Any,
            app_name: str,
            session_service: Any,
            memory_service: Any,
        ) -> Any:
            captured["session_service"] = session_service
            return _fake_runner([object()])

        monkeypatch.setattr(
            sibling_agent_plugin, "_build_runner", fake_build_runner
        )

        svc = InMemorySessionService()
        plugin = sibling_agent_plugin.SiblingAgentPlugin()
        task = plugin.spawn_sibling(
            agent=MagicMock(),
            prompt="hi",
            app_name="lha",
            user_id="alice",
            memory_service=object(),
            session_service=svc,
            session_state={"system:session_source": "scheduler"},
        )
        await task

        assert captured["session_service"] is svc
        listed = await svc.list_sessions(app_name="lha", user_id="alice")
        assert len(listed.sessions) == 1
        full = await svc.get_session(
            app_name="lha", user_id="alice", session_id=listed.sessions[0].id
        )
        assert full.state["system:session_source"] == "scheduler"


# =============================================================================
# spawn_sibling — event streaming + on_event
# =============================================================================


class TestSpawnSiblingOnEvent:
    async def test_invokes_on_event_for_each_event(self, monkeypatch):
        from app.memory import sibling_agent_plugin

        events = [object(), object(), object()]
        monkeypatch.setattr(
            sibling_agent_plugin,
            "_build_runner",
            lambda **_kw: _fake_runner(events),
        )

        seen: list[Any] = []
        plugin = sibling_agent_plugin.SiblingAgentPlugin()
        task = plugin.spawn_sibling(
            agent=MagicMock(),
            prompt="x",
            app_name="a",
            user_id="u",
            memory_service=object(),
            on_event=seen.append,
        )
        await task
        assert seen == events

    async def test_no_on_event_is_fine(self, monkeypatch):
        from app.memory import sibling_agent_plugin

        events = [object(), object()]
        monkeypatch.setattr(
            sibling_agent_plugin,
            "_build_runner",
            lambda **_kw: _fake_runner(events),
        )

        plugin = sibling_agent_plugin.SiblingAgentPlugin()
        task = plugin.spawn_sibling(
            agent=MagicMock(),
            prompt="x",
            app_name="a",
            user_id="u",
            memory_service=object(),
        )
        await task  # should not raise


# =============================================================================
# spawn_sibling — exception swallowing
# =============================================================================


class TestSpawnSiblingErrorHandling:
    async def test_swallows_runner_exceptions(self, monkeypatch, caplog):
        from app.memory import sibling_agent_plugin

        monkeypatch.setattr(
            sibling_agent_plugin,
            "_build_runner",
            lambda **_kw: _fake_runner(raise_exc=True),
        )

        plugin = sibling_agent_plugin.SiblingAgentPlugin()
        task = plugin.spawn_sibling(
            agent=MagicMock(),
            prompt="x",
            app_name="a",
            user_id="u",
            memory_service=object(),
            log_prefix="myfork",
        )
        # Awaiting must not raise the inner RuntimeError.
        await task
        assert any("myfork" in rec.message for rec in caplog.records)


# =============================================================================
# Task tracking + release
# =============================================================================


class TestTaskTracking:
    async def test_task_tracked_then_released(self, monkeypatch):
        from app.memory import sibling_agent_plugin

        monkeypatch.setattr(
            sibling_agent_plugin,
            "_build_runner",
            lambda **_kw: _fake_runner([]),
        )

        plugin = sibling_agent_plugin.SiblingAgentPlugin()
        task = plugin.spawn_sibling(
            agent=MagicMock(),
            prompt="x",
            app_name="a",
            user_id="u",
            memory_service=object(),
        )
        assert task in plugin._pending
        await task
        # done_callback fires after the task resolves; yield once more so it runs.
        await asyncio.sleep(0)
        assert task not in plugin._pending

    async def test_returns_asyncio_task(self, monkeypatch):
        from app.memory import sibling_agent_plugin

        monkeypatch.setattr(
            sibling_agent_plugin,
            "_build_runner",
            lambda **_kw: _fake_runner([]),
        )
        plugin = sibling_agent_plugin.SiblingAgentPlugin()
        task = plugin.spawn_sibling(
            agent=MagicMock(),
            prompt="x",
            app_name="a",
            user_id="u",
            memory_service=object(),
        )
        assert isinstance(task, asyncio.Task)
        await task


# =============================================================================
# close() — graceful drain
# =============================================================================


class TestClose:
    async def test_awaits_pending_tasks(self, monkeypatch):
        from app.memory import sibling_agent_plugin

        finished = asyncio.Event()

        async def slow_run_async(**_kw: Any) -> AsyncIterator[Any]:
            await asyncio.sleep(0.05)
            finished.set()
            return
            yield  # pragma: no cover

        runner = MagicMock()
        runner.run_async = slow_run_async
        monkeypatch.setattr(
            sibling_agent_plugin, "_build_runner", lambda **_kw: runner
        )

        plugin = sibling_agent_plugin.SiblingAgentPlugin(drain_timeout=1.0)
        plugin.spawn_sibling(
            agent=MagicMock(),
            prompt="x",
            app_name="a",
            user_id="u",
            memory_service=object(),
        )
        await plugin.close()
        assert finished.is_set()
        # All tasks released after drain.
        assert not plugin._pending

    async def test_drain_times_out_without_raising(self, monkeypatch, caplog):
        from app.memory import sibling_agent_plugin

        started = asyncio.Event()

        async def hang_run_async(**_kw: Any) -> AsyncIterator[Any]:
            started.set()
            await asyncio.sleep(10)
            return
            yield  # pragma: no cover

        runner = MagicMock()
        runner.run_async = hang_run_async
        monkeypatch.setattr(
            sibling_agent_plugin, "_build_runner", lambda **_kw: runner
        )

        plugin = sibling_agent_plugin.SiblingAgentPlugin(drain_timeout=0.05)
        task = plugin.spawn_sibling(
            agent=MagicMock(),
            prompt="x",
            app_name="a",
            user_id="u",
            memory_service=object(),
        )
        await started.wait()

        await plugin.close()  # must not raise
        # Log surfaces the unfinished count so ops can see drops on shutdown.
        assert any(
            "sibling_agent" in rec.name and "drain" in rec.message.lower()
            for rec in caplog.records
        )
        task.cancel()
        with pytest.raises((asyncio.CancelledError, BaseException)):
            await task

    async def test_close_is_noop_when_no_pending(self):
        from app.memory.sibling_agent_plugin import SiblingAgentPlugin

        plugin = SiblingAgentPlugin()
        await plugin.close()  # no exception, no log noise required

    async def test_close_inside_shorter_outer_timeout_does_not_raise(
        self, monkeypatch
    ):
        # Reproduces ADK PluginManager.close(): it wraps plugin.close() in
        # `async with asyncio.timeout(close_timeout)`. If our drain outlives
        # that budget, the outer timeout cancels us — close() must absorb it,
        # not let RuntimeError("Failed to close plugins...") propagate.
        from app.memory import sibling_agent_plugin

        started = asyncio.Event()

        async def hang_run_async(**_kw):
            started.set()
            await asyncio.sleep(10)
            return
            yield  # pragma: no cover

        runner = MagicMock()
        runner.run_async = hang_run_async
        monkeypatch.setattr(
            sibling_agent_plugin, "_build_runner", lambda **_kw: runner
        )

        # Drain longer than the simulated ADK budget so the outer timeout wins.
        plugin = sibling_agent_plugin.SiblingAgentPlugin(drain_timeout=4.5)
        task = plugin.spawn_sibling(
            agent=MagicMock(),
            prompt="x",
            app_name="a",
            user_id="u",
            memory_service=object(),
        )
        await started.wait()

        # Simulate ADK's wrapper with a tiny budget; close() must swallow it.
        async with asyncio.timeout(0.05):
            await plugin.close()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# =============================================================================
# Drain timeout must stay under ADK's plugin-close budget (5.0s)
# =============================================================================


class TestDrainTimeoutBudget:
    async def test_default_drain_is_under_adk_close_budget(self):
        from app.memory.sibling_agent_plugin import SiblingAgentPlugin

        plugin = SiblingAgentPlugin()
        # ADK's PluginManager wraps plugin.close() in asyncio.timeout(5.0).
        # Our drain must finish with headroom under that, or close() is
        # cancelled mid-drain and re-raised as RuntimeError.
        assert plugin._drain_timeout < 5.0
        assert plugin._drain_timeout <= 4.5

    async def test_override_is_capped_under_budget(self, monkeypatch):
        from app.memory.sibling_agent_plugin import SiblingAgentPlugin

        # An operator setting a too-large override must still be capped.
        monkeypatch.setenv("LHA_SIBLING_DRAIN_TIMEOUT", "30")
        plugin = SiblingAgentPlugin()
        assert plugin._drain_timeout <= 4.5

    async def test_ctor_override_is_capped_under_budget(self):
        from app.memory.sibling_agent_plugin import SiblingAgentPlugin

        plugin = SiblingAgentPlugin(drain_timeout=30.0)
        assert plugin._drain_timeout <= 4.5

    async def test_small_override_is_respected(self, monkeypatch):
        from app.memory.sibling_agent_plugin import SiblingAgentPlugin

        monkeypatch.setenv("LHA_SIBLING_DRAIN_TIMEOUT", "1.5")
        plugin = SiblingAgentPlugin()
        assert plugin._drain_timeout == 1.5


# =============================================================================
# BasePlugin contract
# =============================================================================


async def test_is_base_plugin():
    from google.adk.plugins.base_plugin import BasePlugin

    from app.memory.sibling_agent_plugin import SiblingAgentPlugin

    plugin = SiblingAgentPlugin()
    assert isinstance(plugin, BasePlugin)
    assert plugin.name == "sibling_agent"
