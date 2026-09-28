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

"""Session-state telemetry the chat UI surfaces in its side panels.

The agent runtime writes these keys; `/lha/state` exposes them; the UI
renders them in the Activity / Memory / Delegation panels. Capped lists
to keep session state lean.
"""

from __future__ import annotations

import time
from typing import Any

from google.adk.tools.tool_context import ToolContext

TOOL_CALL_LOG_STATE_KEY = "tool_call_log"
DELEGATE_HISTORY_STATE_KEY = "delegate_history"
MEMORY_WRITES_STATE_KEY = "memory_writes"
IN_FLIGHT_TOOL_CALL_STATE_KEY = "in_flight_tool_call"

_TOOL_LOG_CAP = 50
_DELEGATE_CAP = 25
_MEMORY_CAP = 50
_ARG_PREVIEW_CHARS = 120
_SUMMARY_PREVIEW_CHARS = 240


def _now_ms() -> int:
    return int(time.time() * 1000)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _args_preview(args: Any) -> str:
    if not args:
        return ""
    if isinstance(args, dict):
        parts = []
        for k, v in list(args.items())[:3]:
            v_str = str(v).replace("\n", " ")
            parts.append(f"{k}={_truncate(v_str, 60)}")
        return ", ".join(parts)
    return _truncate(str(args), _ARG_PREVIEW_CHARS)


def _is_ok(response: Any) -> bool:
    if isinstance(response, dict):
        if response.get("error"):
            return False
        status = response.get("status")
        if isinstance(status, str) and status.lower() in {
            "error",
            "failed",
            "halted",
        }:
            return False
        if "success" in response:
            return bool(response["success"])
    return True


def _append_capped(
    state: Any, key: str, entry: dict[str, Any], cap: int
) -> None:
    existing = state.get(key) or []
    if not isinstance(existing, list):
        existing = []
    existing.append(entry)
    if len(existing) > cap:
        existing = existing[-cap:]
    state[key] = existing


async def before_tool_log_callback(tool, args, tool_context):
    """Mark a tool call as in-flight so the chat UI can show
    "running <tool> · Ns" instead of a generic "thinking…" spinner.

    Cleared by `tool_call_log_callback` once the tool returns (success or
    error). Returns None so ADK proceeds with the tool call.
    """
    try:
        name = getattr(tool, "name", None) or tool.__class__.__name__
        tool_context.state[IN_FLIGHT_TOOL_CALL_STATE_KEY] = {
            "name": name,
            "args": _args_preview(args),
            "started_ms": _now_ms(),
        }
    except Exception:  # noqa: S110
        pass


async def tool_call_log_callback(tool, args, tool_context, tool_response):
    """Append a compact tool-call record to state for the chat UI.

    Runs as part of `after_tool_callback` in `root_agent`. Returns None so
    the original tool response is preserved.
    """
    try:
        name = getattr(tool, "name", None) or tool.__class__.__name__
        entry = {
            "name": name,
            "args": _args_preview(args),
            "ok": _is_ok(tool_response),
            "ts": _now_ms(),
        }
        _append_capped(
            tool_context.state, TOOL_CALL_LOG_STATE_KEY, entry, _TOOL_LOG_CAP
        )
        tool_context.state[IN_FLIGHT_TOOL_CALL_STATE_KEY] = None
    except Exception:  # noqa: S110
        # Telemetry must never break a tool call.
        pass


def record_delegate_run(
    *,
    tool_context: ToolContext | None,
    goal: str,
    status: str,
    iterations: int,
    duration_ms: int,
    summary: str,
) -> None:
    if tool_context is None:
        return
    try:
        entry = {
            "goal": _truncate(goal.strip(), _ARG_PREVIEW_CHARS),
            "status": status,
            "iterations": iterations,
            "duration_ms": duration_ms,
            "summary": _truncate(summary.strip(), _SUMMARY_PREVIEW_CHARS),
            "ts": _now_ms(),
        }
        _append_capped(
            tool_context.state, DELEGATE_HISTORY_STATE_KEY, entry, _DELEGATE_CAP
        )
    except Exception:  # noqa: S110
        pass


def record_subagent_spawn(
    *,
    tool_context: ToolContext | None,
    task_id: str,
    goal: str,
) -> None:
    """Surface a background ``agent(spawn)`` child in the Agents panel as a
    live ``running`` row. ``update_subagent_status`` flips it terminal once the
    parent polls ``status``/``result``."""
    if tool_context is None:
        return
    try:
        entry = {
            "task_id": task_id,
            "goal": _truncate(goal.strip(), _ARG_PREVIEW_CHARS),
            "status": "running",
            "iterations": 0,
            "duration_ms": 0,
            "summary": "",
            "ts": _now_ms(),
        }
        _append_capped(
            tool_context.state, DELEGATE_HISTORY_STATE_KEY, entry, _DELEGATE_CAP
        )
    except Exception:  # noqa: S110
        pass


def update_subagent_status(
    *,
    tool_context: ToolContext | None,
    task_id: str,
    status: str,
    summary: str = "",
) -> None:
    # NOTE: background children only flip status when the parent polls
    # status/result (a detached task can't write session state). If the model
    # never polls, the row stays "running". Upgrade path: a completion hook that
    # appends a state-delta event.
    if tool_context is None:
        return
    try:
        existing = tool_context.state.get(DELEGATE_HISTORY_STATE_KEY) or []
        if not isinstance(existing, list):
            return
        for entry in existing:
            if isinstance(entry, dict) and entry.get("task_id") == task_id:
                entry["status"] = status
                if summary:
                    entry["summary"] = _truncate(
                        summary.strip(), _SUMMARY_PREVIEW_CHARS
                    )
                entry["duration_ms"] = max(
                    0, _now_ms() - int(entry.get("ts", _now_ms()))
                )
                tool_context.state[DELEGATE_HISTORY_STATE_KEY] = existing
                return
    except Exception:  # noqa: S110
        pass


def record_memory_write(
    *,
    tool_context: ToolContext | None,
    scope: str,
    content: str,
) -> None:
    if tool_context is None:
        return
    try:
        entry = {
            "scope": scope,
            "content": _truncate(content.strip(), _SUMMARY_PREVIEW_CHARS),
            "ts": _now_ms(),
        }
        _append_capped(
            tool_context.state, MEMORY_WRITES_STATE_KEY, entry, _MEMORY_CAP
        )
    except Exception:  # noqa: S110
        pass
