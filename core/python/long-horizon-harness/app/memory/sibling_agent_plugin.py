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

"""Sibling-agent plugin — owns the fire-and-forget runner lifecycle.

Centralizes what the three fork modules (``review_fork``, ``flush_fork``,
``dream_review``) used to wire by hand: per-fork ``InMemorySessionService``,
``Runner`` construction, ``asyncio.create_task`` lifecycle, and the
exception-swallowed ``runner.run_async`` drive loop. Callers supply a
pre-built ``Agent`` (with whatever toolset / whitelist they want enforced)
and the parent's ``(app_name, user_id, memory_service)`` — the sibling
session is created under those ids so any memory writes land where the
parent's next-turn ``PreloadMemoryTool`` query will surface them.

Registered via ``App(plugins=[..., SiblingAgentPlugin()])``. The plugin
exposes no ``before_*``/``after_*`` hooks — it uses ``BasePlugin`` only
for the ``close()`` drain. Callers reach it via a module-level handle
defined in ``app/agent.py``.

``close()`` waits up to ``drain_timeout`` seconds for in-flight siblings
to finish so memory writes from a turn in progress at SIGTERM still land.
The default (4s) and ``LHA_SIBLING_DRAIN_TIMEOUT`` override are both capped
under ADK's 5s plugin-close budget so ADK never cancels the drain and
re-raises it as ``RuntimeError: Failed to close plugins``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from google.adk.agents import Agent
from google.adk.events.event import Event
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

logger = logging.getLogger(__name__)

_DRAIN_TIMEOUT_ENV = "LHA_SIBLING_DRAIN_TIMEOUT"
# Must stay strictly under ADK's PluginManager close budget (5.0s); otherwise
# ADK's asyncio.timeout cancels close() mid-drain and re-raises it as
# RuntimeError("Failed to close plugins: 'sibling_agent': TimeoutError").
_DEFAULT_DRAIN_TIMEOUT_SECONDS = 4.0
_MAX_DRAIN_TIMEOUT_SECONDS = 4.5


def _build_runner(
    *,
    agent: Agent,
    app_name: str,
    session_service: Any,
    memory_service: Any,
) -> Runner:
    # Module-level hook so tests can monkeypatch without touching the plugin
    # instance — mirrors how the deleted ``_fork.build_runner`` worked.
    return Runner(
        app_name=app_name,
        agent=agent,
        session_service=session_service,
        memory_service=memory_service,
    )


def _resolve_drain_timeout(override: float | None) -> float:
    if override is not None:
        return min(_MAX_DRAIN_TIMEOUT_SECONDS, max(0.0, float(override)))
    raw = os.environ.get(_DRAIN_TIMEOUT_ENV)
    if raw is None:
        return _DEFAULT_DRAIN_TIMEOUT_SECONDS
    try:
        return min(_MAX_DRAIN_TIMEOUT_SECONDS, max(0.0, float(raw)))
    except ValueError:
        return _DEFAULT_DRAIN_TIMEOUT_SECONDS


class SiblingAgentPlugin(BasePlugin):
    """Schedules and drains fire-and-forget sibling-agent runs."""

    def __init__(self, *, drain_timeout: float | None = None) -> None:
        super().__init__(name="sibling_agent")
        self._pending: set[asyncio.Task[None]] = set()
        self._drain_timeout = _resolve_drain_timeout(drain_timeout)

    def spawn_sibling(
        self,
        *,
        agent: Agent,
        prompt: str,
        app_name: str,
        user_id: str,
        memory_service: Any,
        on_event: Callable[[Event], None] | None = None,
        log_prefix: str = "sibling_agent",
        session_service: Any | None = None,
        session_state: dict[str, Any] | None = None,
    ) -> asyncio.Task[None]:
        """Schedule a fire-and-forget run of ``agent`` against ``prompt``.

        When ``session_service`` is provided the sibling runs (and persists
        its events) in that service — used so scheduled jobs land an
        inspectable session. ``session_state`` tags it at creation. When
        omitted, a throwaway ``InMemorySessionService`` is used as before.

        Returns the ``asyncio.Task`` so callers can await it in tests; in
        production code the return value is typically discarded.
        """
        coro = self._run_one(
            agent=agent,
            prompt=prompt,
            app_name=app_name,
            user_id=user_id,
            memory_service=memory_service,
            on_event=on_event,
            log_prefix=log_prefix,
            session_service=session_service,
            session_state=session_state,
        )
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    async def _run_one(
        self,
        *,
        agent: Agent,
        prompt: str,
        app_name: str,
        user_id: str,
        memory_service: Any,
        on_event: Callable[[Event], None] | None,
        log_prefix: str,
        session_service: Any | None = None,
        session_state: dict[str, Any] | None = None,
    ) -> None:
        if session_service is None:
            session_service = InMemorySessionService()
        runner = _build_runner(
            agent=agent,
            app_name=app_name,
            session_service=session_service,
            memory_service=memory_service,
        )
        session = await session_service.create_session(
            app_name=app_name, user_id=user_id, state=session_state or {}
        )
        user_message = Content(role="user", parts=[Part(text=prompt)])

        try:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session.id,
                new_message=user_message,
            ):
                if on_event is not None:
                    on_event(event)
        except Exception:
            logger.exception("%s: sibling agent run failed", log_prefix)

    async def close(self) -> None:
        if not self._pending:
            return
        pending = list(self._pending)
        try:
            done, not_done = await asyncio.wait(
                pending, timeout=self._drain_timeout
            )
        except (TimeoutError, asyncio.CancelledError):
            # ADK's PluginManager wraps this call in asyncio.timeout(); if that
            # budget fires it cancels us here. Drop the in-flight (best-effort)
            # siblings rather than let shutdown raise a user-visible 500.
            logger.warning(
                "sibling_agent: close cancelled by outer timeout — %d in-flight "
                "task(s) dropped",
                len(pending),
            )
            return
        if not_done:
            logger.warning(
                "sibling_agent: drain timed out — %d/%d tasks did not finish "
                "within %.1fs",
                len(not_done),
                len(pending),
                self._drain_timeout,
            )
        else:
            logger.info(
                "sibling_agent: drained %d/%d pending tasks",
                len(done),
                len(pending),
            )


__all__ = [
    "SiblingAgentPlugin",
]
