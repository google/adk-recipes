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

"""Single ``subagent`` tool: a thin dispatcher over two still-internal
callables, blocking ``delegate()`` (``horizon/subagents/delegate.py``) and
fire-and-forget ``agent()`` (``horizon/subagents/spawn.py``). Neither's body,
including the child driver and the background task registry, moves here;
this module only decides which entry point a call reaches.
"""

from __future__ import annotations

from typing import Any, Literal

from google.adk.tools.tool_context import ToolContext

from horizon.subagents.delegate import delegate
from horizon.subagents.spawn import agent
from horizon.subagents.toolsets import available_toolset_names
from horizon.subagents.transcript import parent_transcript

# Lifecycle actions carried over from `agent(action=...)`. `spawn` is not one
# of them: `background=True` on a fresh call replaces it.
_SUPPORTED_ACTIONS: tuple[str, ...] = (
    "status",
    "result",
    "wait",
    "cancel",
    "list",
)

_UNKNOWN_ACTION_ERROR = (
    "Unknown action {action!r}. Use one of {actions}, or omit action "
    "(optionally with background=True) to start a new task."
)


def _parent_events(tool_context: Any) -> Any:
    ic = getattr(tool_context, "_invocation_context", None)
    return getattr(getattr(ic, "session", None), "events", None)


def _with_parent_transcript(
    context: str, include: bool, tool_context: Any
) -> str:
    """Opt-in only: the contract is that a child sees just what it is handed."""
    if not include:
        return context
    transcript = parent_transcript(_parent_events(tool_context))
    if not transcript:
        return context
    block = f"## Parent conversation so far\n\n{transcript}"
    return f"{block}\n\n{context.strip()}" if context.strip() else block


async def subagent(
    goal: str | None = None,
    context: str = "",
    *,
    background: bool = False,
    include_transcript: bool = False,
    action: Literal["status", "result", "wait", "cancel", "list"] | None = None,
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
    """Run or manage a sub-agent task in an isolated child (no memory of
    this chat).

    Start: pass `goal`. Blocks for the summary by default;
    `background=True` returns a `task_id` instead. Manage a running one
    via `action` (status/result/wait/cancel/list); `wait` blocks for the
    next to finish, optionally scoped to `task_ids`.

    Args:
      toolsets/tools: Bundles ({available_names}) or names; default
        file+shell.
      inline_skills: Ad-hoc {"name", "body"} procedures alongside
        `skills`.
      output_format="json": parses into `summary`; failure ->
        `status="halted"`, raw in `summary_raw`.
      profile: Deny-by-default archetype (e.g. "explore" = read-only);
        see `## Child profiles` below.
      include_transcript: give the child this chat's text (capped) when
        briefing would be lossy; default off, brief it instead.

    Brief like a colleague who just arrived: goal, what's ruled out,
    success criteria. Hand off facts, not decisions. Verify a child's
    diff before reporting code done. Batch independent calls.
    """
    if action is not None:
        if action not in _SUPPORTED_ACTIONS:
            return {
                "success": False,
                "error": _UNKNOWN_ACTION_ERROR.format(
                    action=action, actions=", ".join(_SUPPORTED_ACTIONS)
                ),
            }
        return await agent(
            action=action,
            task_id=task_id,
            task_ids=task_ids,
            wait=wait,
            timeout_s=timeout_s,
            tool_context=tool_context,
        )

    if goal is None:
        return {
            "success": False,
            "error": "goal is required to start a new subagent task.",
        }

    context = _with_parent_transcript(context, include_transcript, tool_context)

    if background:
        return await agent(
            action="spawn",
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
            tool_context=tool_context,
        )

    return await delegate(
        goal=goal,
        context=context,
        toolsets=toolsets,
        skills=skills,
        # delegate() takes a required float; None means "caller didn't ask",
        # so resolve to its own blocking default here rather than forwarding
        # None. background=True below forwards timeout_s AS-IS (including
        # None) so spawn()'s own 300s default applies instead of this 120s.
        timeout_s=120.0 if timeout_s is None else timeout_s,
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


if subagent.__doc__ and "{available_names}" in subagent.__doc__:
    subagent.__doc__ = subagent.__doc__.replace(
        "{available_names}", repr(available_toolset_names())
    )


__all__ = ["subagent"]
