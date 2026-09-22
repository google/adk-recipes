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

"""Dynamic ``delegate`` function tool — the parent's dispatch entry point.

Replaces a static ``AgentTool(agent=delegate_agent)`` carve-out with a
function tool the root LLM calls when
it wants a child context for parallelizable / isolatable work.

The surface intentionally exposes many knobs (model, tools, inline
skills, JSON output) so the LLM can shape a child precisely without
having to author a ``SKILL.md`` file or pre-register an agent. The
heavy lifting lives in ``build_child_agent`` (prompt + tools assembly)
and ``run_delegate`` (state-isolated execution).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from google.adk.flows.llm_flows.functions import (
    REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
)
from google.adk.tools.tool_context import ToolContext
from google.genai.types import Content, FunctionResponse, Part

from horizon.guardrails.permission_guard import is_headless
from horizon.guardrails.permission_rules import (
    PERMISSION_GRANTS_STATE_KEY,
    read_session_grants,
)
from horizon.guardrails.policy_grants import POLICY_GRANTS_STATE_KEY
from horizon.subagents.delegate_builder import build_child_agent
from horizon.subagents.delegate_runner import (
    DELEGATE_APP_NAME,
    ChildDriveResult,
    build_resumable_child_runner,
    drive_child,
    run_delegate,
)
from horizon.subagents.resurface_context import child_drain
from horizon.subagents.toolsets import available_toolset_names
from horizon.telemetry.ui import record_delegate_run

_logger = logging.getLogger(__name__)
_CHILD_CONF_KEY = "_delegate_child_conf:"
_KICKOFF_CONTENT_ROLE = "user"

_KICKOFF_MESSAGE = (
    "Begin work on the delegated task. The goal and context are in the "
    "system instruction above. Return a concise summary when finished."
)

_JSON_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*\n(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE
)


def _strip_json_fences(text: str) -> str:
    m = _JSON_FENCE_RE.match(text.strip())
    return m.group(1) if m else text


def _empty_telemetry() -> dict[str, Any]:
    return {"iterations": 0, "duration_ms": 0}


def _halt(
    summary: str, *, telemetry: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "status": "halted",
        "summary": summary,
        "telemetry": telemetry or _empty_telemetry(),
    }


def _record_run(
    *,
    tool_context: ToolContext | None,
    goal: str,
    result: dict[str, Any],
) -> None:
    telem = result.get("telemetry") or {}
    summary = result.get("summary", "")
    record_delegate_run(
        tool_context=tool_context,
        goal=goal,
        status=str(result.get("status", "completed")),
        iterations=int(telem.get("iterations", 0)),
        duration_ms=int(telem.get("duration_ms", 0)),
        summary=summary if isinstance(summary, str) else str(summary),
    )


def _process_json_output(result: dict[str, Any]) -> dict[str, Any]:
    raw = str(result.get("summary", ""))
    try:
        parsed = json.loads(_strip_json_fences(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        return {
            "status": "halted",
            "summary": f"Child returned non-JSON output: {exc}",
            "summary_raw": raw,
            "telemetry": result.get("telemetry", _empty_telemetry()),
        }
    return {
        "status": str(result.get("status", "completed")),
        "summary": parsed,
        "telemetry": result.get("telemetry", _empty_telemetry()),
    }


async def run_child(
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
    parent_grants: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Build + run a child agent with the standard kickoff message.

    Shared by ``delegate()`` (blocking) and the ``spawn`` action of the
    ``agent`` tool (background) so they cannot drift on parameter
    forwarding or post-processing. Builder/runner errors propagate to
    the caller — ``delegate()`` wraps them in a halt envelope; the
    spawn path lets the task registry record the exception.
    """
    child = await _build_child(
        goal=goal,
        context=context,
        toolsets=toolsets,
        skills=skills,
        model=model,
        tools=tools,
        instructions=instructions,
        inline_skills=inline_skills,
        output_format=output_format,
        output_schema=output_schema,
        name=name,
        profile=profile,
        parent_grants=parent_grants,
    )

    runner_kwargs: dict[str, Any] = {
        "child": child,
        "user_message": _KICKOFF_MESSAGE,
        "timeout_s": timeout_s,
    }
    if max_iterations is not None:
        runner_kwargs["max_iterations"] = max_iterations

    result = await run_delegate(**runner_kwargs)

    if output_format == "json" and result.get("status") == "completed":
        result = _process_json_output(result)

    return result


async def _build_child(
    *,
    goal: str,
    context: str,
    toolsets: list[str] | None,
    skills: list[str] | None,
    model: str | None,
    tools: list[str] | None,
    instructions: str | None,
    inline_skills: list[dict[str, str]] | None,
    output_format: Literal["text", "json"],
    output_schema: dict[str, Any] | None,
    name: str | None,
    profile: str | None,
    parent_grants: list[dict[str, Any]] | None,
) -> Any:
    builder_kwargs: dict[str, Any] = {
        "goal": goal,
        "context": context,
        "toolsets": toolsets,
        "skills": skills,
        "tools": tools,
        "instructions": instructions,
        "inline_skills": inline_skills,
        "output_format": output_format,
        "output_schema": output_schema,
        "name": name,
        "profile": profile,
        "parent_grants": parent_grants,
    }
    if model is not None:
        builder_kwargs["model_name"] = model
    return await build_child_agent(**builder_kwargs)


def _can_resurface(tool_context: Any) -> bool:
    """Resurfacing needs a live parent turn: a durable session service, a stable
    function_call_id, and an interactive confirmation channel. Routines
    (headless) and bare/test contexts fall back to the in-memory child."""
    if tool_context is None or is_headless():
        return False
    ic = getattr(tool_context, "_invocation_context", None)
    if ic is None or getattr(ic, "session_service", None) is None:
        return False
    if not getattr(tool_context, "function_call_id", None):
        return False
    return callable(getattr(tool_context, "request_confirmation", None))


def _awaiting_summary(hint: str) -> str:
    """An eval caught the parent reporting a paused child's work as finished.
    The status field alone lost to the hint's confident phrasing, so say it."""
    return (
        "PAUSED: the child stopped to ask for approval, so none of the work "
        f"is done yet. Relay this to the user and wait: {hint}"
    )


def _bubble_hint(name: str | None, child_hint: str) -> str:
    return f"[delegated: {name or 'task'}] {child_hint or 'approval required'}"


def _finalize(
    result: ChildDriveResult, output_format: Literal["text", "json"]
) -> dict[str, Any]:
    envelope = {
        "status": result.status,
        "summary": result.summary,
        "telemetry": result.telemetry,
    }
    if output_format == "json" and result.status == "completed":
        return _process_json_output(envelope)
    return envelope


def _forwarded_child_state(
    tool_context: Any, parent_grants: list[dict[str, Any]] | None
) -> dict[str, Any]:
    """Seed the child session with the user's already-earned approvals so it
    doesn't re-prompt for what the parent session already allowed."""
    state: dict[str, Any] = {}
    perm = read_session_grants(getattr(tool_context, "state", None))
    if perm:
        state[PERMISSION_GRANTS_STATE_KEY] = perm
    if parent_grants:
        state[POLICY_GRANTS_STATE_KEY] = list(parent_grants)
    return state


async def _cleanup_child_session(
    session_service: Any, user_id: str, session_id: str
) -> None:
    try:
        await session_service.delete_session(
            app_name=DELEGATE_APP_NAME, user_id=user_id, session_id=session_id
        )
    except Exception:
        _logger.debug("delegate child session cleanup failed", exc_info=True)


async def _delegate_resurfacing(
    *,
    tool_context: Any,
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
    parent_grants: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Drive a durable, resumable child that surfaces risky ops to the user.

    Two invocations of the same ``delegate`` call (across one parent HITL
    pause/resume): the first runs the child until it needs approval and bubbles
    that to the parent; the second resumes the child in place once approved.
    """
    ic = tool_context._invocation_context
    session_service = ic.session_service
    user_id = ic.user_id
    fc_id = tool_context.function_call_id
    child_session_id = f"delegate-{fc_id}"
    parent_state = tool_context.state
    confirmation = getattr(tool_context, "tool_confirmation", None)

    child = await _build_child(
        goal=goal,
        context=context,
        toolsets=toolsets,
        skills=skills,
        model=model,
        tools=tools,
        instructions=instructions,
        inline_skills=inline_skills,
        output_format=output_format,
        output_schema=output_schema,
        name=name,
        profile=profile,
        parent_grants=parent_grants,
    )
    runner = build_resumable_child_runner(child, session_service)

    if confirmation is None:
        # A re-entered invocation 1 (same fc_id, e.g. a retried turn) would
        # collide; clear any stale child session so create can't raise
        # AlreadyExistsError and leave an orphan behind.
        await _cleanup_child_session(session_service, user_id, child_session_id)
        await session_service.create_session(
            app_name=DELEGATE_APP_NAME,
            user_id=user_id,
            session_id=child_session_id,
            state=_forwarded_child_state(tool_context, parent_grants),
        )
        with child_drain(1):
            res = await drive_child(
                runner,
                user_id=user_id,
                session_id=child_session_id,
                new_message=Content(
                    role=_KICKOFF_CONTENT_ROLE,
                    parts=[Part(text=_KICKOFF_MESSAGE)],
                ),
                timeout_s=timeout_s,
                max_iterations=max_iterations,
            )
        if res.pending is not None:
            parent_state[f"{_CHILD_CONF_KEY}{fc_id}"] = (
                res.pending.confirmation_id
            )
            hint = _bubble_hint(name, res.pending.hint)
            tool_context.request_confirmation(
                hint=hint, payload=res.pending.payload
            )
            return {
                "status": "awaiting_approval",
                "summary": _awaiting_summary(hint),
                "telemetry": res.telemetry,
            }
        await _cleanup_child_session(session_service, user_id, child_session_id)
        return _finalize(res, output_format)

    # Invocation 2 — the parent turn resumed with the user's decision.
    conf_id = parent_state.get(f"{_CHILD_CONF_KEY}{fc_id}")
    if not conf_id:
        await _cleanup_child_session(session_service, user_id, child_session_id)
        return _halt(
            "Lost the child's pending-approval state; re-run the task."
        )
    if not bool(getattr(confirmation, "confirmed", False)):
        # Declined — don't spend a child turn just to reach a deny.
        await _cleanup_child_session(session_service, user_id, child_session_id)
        return _halt("User declined the delegated operation.")
    # Forward the parent's decision. An approval is a bare confirmed=True; an
    # ask_parent escalation also carries the free-text answer in payload, which
    # the child's ask_parent tool reads on resume.
    resume_response: dict[str, Any] = {"confirmed": True}
    parent_payload = getattr(confirmation, "payload", None)
    if parent_payload is not None:
        resume_response["payload"] = parent_payload
    resume_msg = Content(
        role=_KICKOFF_CONTENT_ROLE,
        parts=[
            Part(
                function_response=FunctionResponse(
                    id=conf_id,
                    name=REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
                    response=resume_response,
                )
            )
        ],
    )
    with child_drain(0):
        res = await drive_child(
            runner,
            user_id=user_id,
            session_id=child_session_id,
            new_message=resume_msg,
            timeout_s=timeout_s,
            max_iterations=max_iterations,
        )
    await _cleanup_child_session(session_service, user_id, child_session_id)
    if res.pending is not None:
        return _halt(
            "This delegated task needs another, different approval that isn't "
            "available in a single delegate run — split it into separate delegate "
            "calls or pre-grant the operation."
        )
    return _finalize(res, output_format)


async def delegate(
    goal: str,
    context: str = "",
    toolsets: list[str] | None = None,
    skills: list[str] | None = None,
    timeout_s: float = 120.0,
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
    """Run a self-contained task in an isolated child agent and return a summary.

    Use when:
      * The task is large enough to warrant its own context window.
      * The work is parallelizable / isolatable from the main thread.
      * You want tool-call sequences (file/shell/web) to not pollute the
        parent conversation.

    Args:
      goal: What the child should achieve. One paragraph, action-oriented.
      context: Optional background facts (file paths, prior findings, etc.)
        the child needs. The child has NO memory of the parent's
        conversation, so put everything it needs here.
      toolsets: Names of tool bundles to give the child. Available:
        {available_names}. Defaults to ``["file", "shell"]`` when neither
        ``toolsets`` nor ``tools`` is supplied.
      skills: Names of skills from the on-disk skill registry to inject
        into the child's prompt.
      timeout_s: Hard cap on child runtime in seconds. Default 120.
      model: Override the child's Gemini variant. Defaults to
        ``gemini-3.7-flash``; ``gemini-3.1-pro-preview`` trades latency for
        stronger reasoning.
      tools: Concrete tool names (e.g. ``["read", "bash"]``) to
        add on top of any ``toolsets``. When ``tools`` is supplied without
        ``toolsets``, the child receives exactly those tools and nothing else.
      instructions: Free-form instruction text appended under a
        ``## Caller instructions`` section in the child's prompt.
      inline_skills: Ad-hoc skill bodies of the form
        ``[{"name": str, "body": str}]`` — rendered alongside disk skills
        so you can hand a child a procedure without authoring a SKILL.md.
      output_format: ``"text"`` (default) or ``"json"``. When ``"json"``
        the child is instructed to return a JSON object; the response is
        parsed and returned as a dict in the ``summary`` field.
      output_schema: JSON Schema dict passed to ADK as the child's
        ``output_schema``. ADK enforces the shape at generation time via
        the SetModelResponseTool path.
      max_iterations: Hard cap on the child's event count. Separate axis
        from ``timeout_s``. Exceeding the cap → halted.
      name: Short descriptor surfaced in telemetry / traces. Defaults to
        ``delegate_<uuid>``. Sanitized and uuid-suffixed.
      profile: Lock the child into a deny-by-default archetype. ``"explore"``
        = read-only (file reads + search, no writes/shell/network). Omit
        for an unconstrained child. Available: see the live ``## Child
        profiles`` block in this tool's description.

    Returns:
      ``{"status": "completed"|"timeout"|"halted",
         "summary": <text or parsed JSON dict>,
         "telemetry": {"iterations", "duration_ms"}}``

      When ``output_format="json"`` and parsing/validation fails, the
      envelope also carries ``summary_raw`` with the unparsed child text.
    """
    parent_grants: list[dict[str, Any]] | None = None
    if tool_context is not None:
        state = getattr(tool_context, "state", None)
        if state is not None:
            raw = state.get(POLICY_GRANTS_STATE_KEY)
            parent_grants = list(raw) if isinstance(raw, list) else []

    child_kwargs: dict[str, Any] = {
        "goal": goal,
        "context": context,
        "toolsets": toolsets,
        "skills": skills,
        "timeout_s": timeout_s,
        "model": model,
        "tools": tools,
        "instructions": instructions,
        "inline_skills": inline_skills,
        "output_format": output_format,
        "output_schema": output_schema,
        "max_iterations": max_iterations,
        "name": name,
        "profile": profile,
        "parent_grants": parent_grants,
    }

    resuming = (
        tool_context is not None
        and getattr(tool_context, "tool_confirmation", None) is not None
    )

    try:
        if _can_resurface(tool_context):
            result = await _delegate_resurfacing(
                tool_context=tool_context, **child_kwargs
            )
        elif resuming:
            # We're resuming an approval but the resume channel is gone — never
            # restart a fresh child (it would re-run the already-done work).
            result = _halt("Lost the delegate resume channel; re-run the task.")
        else:
            result = await run_child(**child_kwargs)
    except KeyError as exc:
        bad = exc.args[0] if exc.args else "<unknown>"
        result = _halt(
            f"Unknown toolset or tool name {bad!r}. "
            f"Available toolsets: {available_toolset_names()}."
        )
    except ValueError as exc:
        result = _halt(f"Invalid delegate parameter: {exc}")
    except Exception as exc:
        _logger.exception("delegate resurfacing failed")
        result = _halt(f"delegate failed: {type(exc).__name__}: {exc}")

    # A pending approval isn't a finished run — don't record telemetry yet.
    if result.get("status") != "awaiting_approval":
        _record_run(tool_context=tool_context, goal=goal, result=result)
    return result


if delegate.__doc__ and "{available_names}" in delegate.__doc__:
    delegate.__doc__ = delegate.__doc__.replace(
        "{available_names}", repr(available_toolset_names())
    )
