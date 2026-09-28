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

"""Async sub-agent registry — the substrate behind ``agent(action='spawn')``.

Each ``SubAgentHandle`` wraps an ``asyncio.Task`` plus enough metadata
to report status without blocking on the task itself. The default
registry is a process-wide singleton because ADK runs each tool call
in its own copied ``contextvars.Context`` — a ContextVar-scoped
registry would lose handles between ``spawn`` and ``result`` calls.
Tests construct a fresh ``SubAgentRegistry()`` directly for isolation.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger(__name__)

CoroFactory = Callable[[], Awaitable[dict[str, Any]]]


@dataclass
class SubAgentHandle:
    task_id: str
    goal: str
    task: asyncio.Task
    started_at: float
    parent_id: str | None = None
    cancelled_explicitly: bool = False
    name: str | None = None
    consumed: bool = (
        False  # returned by wait(); don't surface it again next wait()
    )


def _status_label(handle: SubAgentHandle) -> str:
    task = handle.task
    if task.cancelled() or handle.cancelled_explicitly:
        return "cancelled"
    if not task.done():
        return "running"
    exc = task.exception()
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if exc is not None:
        return "failed"
    return "completed"


class SubAgentRegistry:
    def __init__(self) -> None:
        self._handles: dict[str, SubAgentHandle] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        *,
        coro_factory: CoroFactory,
        goal: str,
        parent_id: str | None = None,
        name: str | None = None,
    ) -> str:
        task_id = uuid.uuid4().hex[:12]

        async def _wrap() -> dict[str, Any]:
            try:
                return await coro_factory()
            except Exception:
                _logger.exception("sub-agent %s failed", task_id)
                raise

        task = asyncio.create_task(_wrap(), name=f"subagent-{task_id}")
        handle = SubAgentHandle(
            task_id=task_id,
            goal=goal,
            task=task,
            started_at=time.monotonic(),
            parent_id=parent_id,
            name=name,
        )
        async with self._lock:
            self._handles[task_id] = handle
        return task_id

    async def get_status(self, task_id: str) -> dict[str, Any]:
        async with self._lock:
            handle = self._handles.get(task_id)
        if handle is None:
            return {"status": "unknown", "task_id": task_id}
        payload: dict[str, Any] = {
            "status": _status_label(handle),
            "task_id": task_id,
            "goal": handle.goal,
            "elapsed_s": round(time.monotonic() - handle.started_at, 3),
        }
        if handle.name:
            payload["name"] = handle.name
        return payload

    async def get_result(
        self,
        task_id: str,
        *,
        wait: bool = True,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        async with self._lock:
            handle = self._handles.get(task_id)
        if handle is None:
            return {"status": "unknown", "task_id": task_id}

        if not handle.task.done():
            if not wait:
                return {"status": _status_label(handle), "task_id": task_id}
            try:
                await asyncio.wait_for(
                    asyncio.shield(handle.task), timeout=timeout_s
                )
            except TimeoutError:
                return {
                    "status": "timeout",
                    "task_id": task_id,
                    "elapsed_s": round(time.monotonic() - handle.started_at, 3),
                }
            except asyncio.CancelledError:
                return {"status": "cancelled", "task_id": task_id}
            except Exception as exc:
                return {
                    "status": "failed",
                    "task_id": task_id,
                    "error": str(exc),
                }

        if handle.cancelled_explicitly or handle.task.cancelled():
            return {"status": "cancelled", "task_id": task_id}

        exc = handle.task.exception()
        if exc is not None:
            return {
                "status": "failed",
                "task_id": task_id,
                "error": str(exc),
            }

        return {
            "status": "completed",
            "task_id": task_id,
            "result": handle.task.result(),
        }

    async def wait(
        self,
        *,
        task_ids: list[str] | None = None,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        """Block until the next tracked child finishes; return it + who's left.

        The fleet primitive behind ``agent(action='wait')`` — launch N with
        ``spawn``, ``wait`` for the next to finish, react, spawn a replacement,
        ``wait`` again. A finished handle is marked ``consumed`` so a subsequent
        ``wait`` surfaces the *next* one instead of re-returning it.
        """
        async with self._lock:
            if task_ids is None:
                handles = [h for h in self._handles.values() if not h.consumed]
            else:
                handles = [
                    self._handles[t] for t in task_ids if t in self._handles
                ]
        if not handles:
            return {"status": "no_active_tasks"}

        done, _pending = await asyncio.wait(
            {h.task for h in handles},
            timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            return {
                "status": "timeout",
                "still_running": [h.task_id for h in handles],
            }

        finished = next(h for h in handles if h.task in done)
        async with self._lock:
            finished.consumed = True
        result = await self.get_result(finished.task_id, wait=False)
        return {
            "task_id": finished.task_id,
            "status": result["status"],
            "result": result.get("result"),
            "still_running": [
                h.task_id
                for h in handles
                if h.task is not finished.task and not h.task.done()
            ],
        }

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            handle = self._handles.get(task_id)
            children = [
                h for h in self._handles.values() if h.parent_id == task_id
            ]
        if handle is None:
            return False
        handle.cancelled_explicitly = True
        cancelled = handle.task.cancel()
        for child in children:
            child.cancelled_explicitly = True
            child.task.cancel()
        return cancelled or handle.task.done()

    async def list_active(self) -> list[dict[str, Any]]:
        async with self._lock:
            snapshot = list(self._handles.values())
        active: list[dict[str, Any]] = []
        for handle in snapshot:
            label = _status_label(handle)
            if label != "running":
                continue
            entry: dict[str, Any] = {
                "task_id": handle.task_id,
                "goal": handle.goal,
                "status": label,
                "elapsed_s": round(time.monotonic() - handle.started_at, 3),
            }
            if handle.name:
                entry["name"] = handle.name
            active.append(entry)
        return active


_active_registry: SubAgentRegistry | None = None


def get_registry() -> SubAgentRegistry:
    global _active_registry
    if _active_registry is None:
        _active_registry = SubAgentRegistry()
    return _active_registry


def reset_registry() -> None:
    global _active_registry
    _active_registry = None


__all__ = [
    "SubAgentHandle",
    "SubAgentRegistry",
    "get_registry",
    "reset_registry",
]
