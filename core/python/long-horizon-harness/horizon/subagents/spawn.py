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

"""Fire-and-forget sub-agent spawn/status/result/cancel/list tools.

Single ``agent(action=...)`` dispatch lets the root agent fan out
parallel work without blocking. ``delegate()`` stays as the synchronous-
feeling wrapper; this module exposes the underlying async lifecycle.

The ``spawn`` action mirrors ``delegate()``'s parameter surface, same
model/tools/instructions/output_format knobs, so the LLM can pick
between blocking and background execution without losing expressiveness.
"""

from __future__ import annotations

from typing import Any, Literal

from google.adk.tools.tool_context import ToolContext

from horizon.subagents.delegate import run_child
from horizon.subagents.registry import get_registry
from horizon.telemetry.ui import record_subagent_spawn, update_subagent_status

_DEFAULT_TIMEOUT_S = 300.0
_DEFAULT_RESULT_WAIT_TIMEOUT_S = 120.0

# Registry status vocabulary -> the panel's (DelegationTree STATUS_META).
_TERMINAL = {"completed", "timeout", "failed", "cancelled"}
_PANEL_STATUS = {
    "completed": "completed",
    "timeout": "timeout",
    "failed": "halted",
    "cancelled": "halted",
}


def _result_summary(payload: dict[str, Any]) -> str:
    inner = payload.get("result")
    if isinstance(inner, dict) and isinstance(inner.get("summary"), str):
        return inner["summary"]
    return str(payload.get("error", ""))


def _sync_row(
    tool_context: ToolContext | None, task_id: str, payload: dict
) -> None:
    if tool_context is None or payload.get("status") not in _TERMINAL:
        return
    update_subagent_status(
        tool_context=tool_context,
        task_id=task_id,
        status=_PANEL_STATUS.get(payload["status"], "halted"),
        summary=_result_summary(payload),
    )


async def _spawn(
    *,
    goal: str,
    context: str,
    toolsets: list[str] | None,
    skills: list[str] | None,
    timeout_s: float,
    model: str | None,
    tools: list[str] | None,
    instructions: str | None,
    inline_skills: list[dict[str, str]] | None,
    output_format: Literal["text", "json"],
    output_schema: dict[str, Any] | None,
    max_iterations: int | None,
    name: str | None,
    profile: str | None,
    tool_context: ToolContext | None,
) -> dict[str, Any]:
    registry = get_registry()

    async def _factory() -> dict[str, Any]:
        return await run_child(
            goal=goal,
            context=context,
            toolsets=toolsets,
            skills=skills,
            timeout_s=timeout_s,
            model=model,
            tools=tools,
            instructions=instructions,
            inline_skills=inline_skills,
            output_format=output_format,
            output_schema=output_schema,
            max_iterations=max_iterations,
            name=name,
            profile=profile,
            parent_grants=None,
        )

    task_id = await registry.register(
        coro_factory=_factory, goal=goal, name=name
    )
    record_subagent_spawn(tool_context=tool_context, task_id=task_id, goal=goal)
    return {"success": True, "task_id": task_id, "status": "pending"}


async def _status(
    task_id: str, tool_context: ToolContext | None
) -> dict[str, Any]:
    payload = await get_registry().get_status(task_id)
    _sync_row(tool_context, task_id, payload)
    return payload


async def _result(
    task_id: str, wait: bool, timeout_s: float, tool_context: ToolContext | None
) -> dict[str, Any]:
    payload = await get_registry().get_result(
        task_id, wait=wait, timeout_s=timeout_s
    )
    _sync_row(tool_context, task_id, payload)
    return payload


async def _wait(
    task_ids: list[str] | None,
    timeout_s: float,
    tool_context: ToolContext | None,
) -> dict[str, Any]:
    payload = await get_registry().wait(task_ids=task_ids, timeout_s=timeout_s)
    tid = payload.get("task_id")
    if tid is not None:
        _sync_row(
            tool_context,
            tid,
            {
                "status": payload["status"],
                "result": payload.get("result") or {},
            },
        )
    return payload


async def _cancel(task_id: str) -> dict[str, Any]:
    cancelled = await get_registry().cancel(task_id)
    return {"task_id": task_id, "cancelled": cancelled}


async def _list_active() -> dict[str, Any]:
    return {"active": await get_registry().list_active()}


async def agent(
    action: Literal["spawn", "status", "result", "wait", "cancel", "list"],
    *,
    goal: str | None = None,
    context: str = "",
    toolsets: list[str] | None = None,
    skills: list[str] | None = None,
    task_id: str | None = None,
    task_ids: list[str] | None = None,
    wait: bool = True,
    timeout_s: float | None = None,
    model: str | None = None,
    tools: list[str] | None = None,
    instructions: str | None = None,
    inline_skills: list[dict[str, str]] | None = None,
    output_format: Literal["text", "json"] = "text",
    output_schema: dict[str, Any] | None = None,
    max_iterations: int | None = None,
    name: str | None = None,
    profile: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Fan-out / lifecycle dispatch for background sub-agents.

    ``action`` picks the operation:

    - ``spawn``: start a child in the background; returns a ``task_id``
      immediately (it does NOT wait for the child). Use it to run several
      children at once or to keep working while one runs. If you need the
      result before continuing, use ``delegate`` instead of spawn followed
      by an immediate ``result`` (wait=True): that just blocks like a
      slower delegate. Required: ``goal`` (what the child should achieve).
      Optional knobs match ``delegate()`` exactly: ``context``,
      ``toolsets``, ``skills``, ``timeout_s`` (default 300), ``model``
      (Gemini variant override), ``tools`` (concrete tool names),
      ``instructions`` (caller text appended to the prompt),
      ``inline_skills`` (ad-hoc skill bodies), ``output_format``
      (``"text"`` or ``"json"``), ``output_schema`` (JSON Schema for
      json output), ``max_iterations`` (event-count cap),
      ``name`` (telemetry descriptor), ``profile`` (deny-by-default
      archetype, e.g. ``"explore"``).
    - ``status``: return the current status of a spawned agent. Requires
      ``task_id``.
    - ``result``: fetch a spawned agent's result. Requires ``task_id``.
      Optional ``wait`` (block until done, default True) and ``timeout_s``
      (max seconds to block when waiting, default 120).
    - ``wait``: block until the NEXT spawned agent finishes, then return it
      (``task_id``, ``status``, ``result``) plus ``still_running``. This is the
      fleet primitive: ``spawn`` several, ``wait`` for the next to finish, react,
      spawn a replacement, ``wait`` again: far cheaper than polling ``status`` in
      a loop. Optional ``task_ids`` (scope the wait to specific tasks; default all
      active) and ``timeout_s`` (max seconds to block, default 120). A finished
      task is not re-returned by a later ``wait``. No active tasks →
      ``{"status": "no_active_tasks"}``.
    - ``cancel``: cancel a spawned agent (and any descendants it spawned).
      Requires ``task_id``.
    - ``list``: return all spawned agents that are still pending or running.
      No other args.
    """
    if action == "spawn":
        if goal is None:
            return {"success": False, "error": "action='spawn' requires 'goal'"}
        return await _spawn(
            goal=goal,
            context=context,
            toolsets=toolsets,
            skills=skills,
            timeout_s=_DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s,
            model=model,
            tools=tools,
            instructions=instructions,
            inline_skills=inline_skills,
            output_format=output_format,
            output_schema=output_schema,
            max_iterations=max_iterations,
            name=name,
            profile=profile,
            tool_context=tool_context,
        )
    if action == "status":
        if task_id is None:
            return {
                "success": False,
                "error": "action='status' requires 'task_id'",
            }
        return await _status(task_id, tool_context)
    if action == "result":
        if task_id is None:
            return {
                "success": False,
                "error": "action='result' requires 'task_id'",
            }
        return await _result(
            task_id,
            wait,
            _DEFAULT_RESULT_WAIT_TIMEOUT_S if timeout_s is None else timeout_s,
            tool_context,
        )
    if action == "wait":
        return await _wait(
            task_ids,
            _DEFAULT_RESULT_WAIT_TIMEOUT_S if timeout_s is None else timeout_s,
            tool_context,
        )
    if action == "cancel":
        if task_id is None:
            return {
                "success": False,
                "error": "action='cancel' requires 'task_id'",
            }
        return await _cancel(task_id)
    if action == "list":
        return await _list_active()
    return {
        "success": False,
        "error": (
            f"Unknown action {action!r}. Use: spawn, status, result, wait, cancel, list."
        ),
    }


__all__ = ["agent"]
