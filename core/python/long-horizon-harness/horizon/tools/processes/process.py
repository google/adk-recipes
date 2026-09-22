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

"""``process`` tool — single-function multi-action dispatcher.

The model drives every backgrounded session through this tool:
``spawn``, ``list``, ``poll``, ``log``, ``wait``, ``wait_for``, ``kill``,
``write``. ``spawn`` is where ``bash``'s former ``background=True`` moved
(the minimal parameter space dropped it from ``bash``): it is a NEW
shell
execution path and must run through the exact same guard-chain extraction
(``permission_guard._shell_command``, ``policies._command_for``) as every
other shell entry point, or one argument bypasses the whole
anti-obfuscation layer; see the guardrail modules for where that is wired.
Lookup is scoped to the calling session's ``ProcessRegistry``, held in a
module-level store keyed by ADK session id, falling back to ``temp:``
state for context-less callers.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Literal

from horizon.environment.process import ProcessHandle
from horizon.environment.registry import resolve_registry
from horizon.environment_context import active_environment
from horizon.tools._paths import path_under_root
from horizon.tools.processes._spawn import open_handle
from horizon.tools.processes.interactive import detect_interactive_prompt

POLL_TAIL_BYTES = 1024
DEFAULT_LOG_LIMIT = 8192
_WAIT_FOR_POLL_S = 0.2
_DEFAULT_WAIT_FOR_TIMEOUT_S = 60.0


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _summarize(handle: ProcessHandle) -> dict[str, Any]:
    return {
        "session_id": handle.session_id,
        "command": handle.command,
        "pid": handle.pid,
        "status": "running" if handle.is_running else "exited",
        "exit_code": handle.exit_code,
        "started_at": handle.started_at,
        "finished_at": handle.finished_at,
        "output_size": handle.output_size,
    }


async def _poll(handle: ProcessHandle) -> dict[str, Any]:
    size = handle.output_size
    tail_offset = max(0, size - POLL_TAIL_BYTES)
    data, _, exit_code = await handle.read(offset=tail_offset)
    payload: dict[str, Any] = {
        "session_id": handle.session_id,
        "status": "running" if handle.is_running else "exited",
        "exit_code": exit_code,
        "tail": _decode(data),
        "output_size": handle.output_size,
    }
    if handle.is_running:
        prompt = detect_interactive_prompt(
            data, idle_seconds=handle.idle_seconds
        )
        if prompt is not None:
            payload["interactive"] = True
            payload["prompt"] = prompt
            payload["hint"] = (
                "Process appears to be waiting on stdin. Either rerun with "
                "non-interactive flags (e.g. -y, --yes), or drive it via "
                "process(action='write', session_id=..., data='...')."
            )
    return payload


async def _wait_for(
    handle: ProcessHandle, pattern: str, timeout_s: float
) -> dict[str, Any]:
    """Block until the process's output matches ``pattern`` (a regex), the
    process exits, or ``timeout_s`` elapses — polling the buffer server-side so
    the model doesn't burn a turn-loop tailing ``log``."""
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return {"error": f"invalid pattern {pattern!r}: {exc}"}

    deadline = time.monotonic() + timeout_s
    while True:
        data, _, exit_code = await handle.read()
        match = rx.search(_decode(data))
        tail = _decode(data[-POLL_TAIL_BYTES:])
        if match is not None:
            return {
                "session_id": handle.session_id,
                "matched": True,
                "pattern": pattern,
                "match": match.group(0),
                "status": "running" if handle.is_running else "exited",
                "exit_code": exit_code,
                "tail": tail,
                "timed_out": False,
            }
        if not handle.is_running:
            return {
                "session_id": handle.session_id,
                "matched": False,
                "pattern": pattern,
                "status": "exited",
                "exit_code": exit_code,
                "tail": tail,
                "timed_out": False,
                "hint": "Process exited before the pattern appeared.",
            }
        if time.monotonic() >= deadline:
            return {
                "session_id": handle.session_id,
                "matched": False,
                "pattern": pattern,
                "status": "running",
                "exit_code": None,
                "tail": tail,
                "timed_out": True,
            }
        await asyncio.sleep(_WAIT_FOR_POLL_S)


async def _spawn(
    command: str, cwd: str | None, tool_context: Any | None
) -> dict[str, Any]:
    env = active_environment()
    root = env.working_dir.resolve()

    if cwd is None:
        resolved_cwd = root
    else:
        resolved, err = path_under_root(cwd, root)
        if err is not None:
            return {"error": err.replace("Path outside", "cwd outside")}
        resolved_cwd = resolved

    handle = await open_handle(command, resolved_cwd)
    resolve_registry(tool_context).register(handle)
    return {
        "session_id": handle.session_id,
        "pid": handle.pid,
        "status": "running",
        "command": command,
    }


async def process(
    action: Literal[
        "spawn", "list", "poll", "log", "wait", "wait_for", "kill", "write"
    ],
    session_id: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LOG_LIMIT,
    timeout_s: float | None = None,
    data: str | None = None,
    submit: bool = True,
    pattern: str | None = None,
    command: str | None = None,
    cwd: str | None = None,
    tool_context: Any | None = None,
) -> dict[str, Any]:
    """Inspect or control a background process; `spawn` starts one.

    Args:
        command: spawn: shell command.
        cwd: spawn: working dir, defaults to the workspace root.
        session_id: the `proc_...` id spawn returns; required except for
            list/spawn.
        offset: log: byte offset into a rolling 200 KB buffer.
        limit: log: max bytes.
        timeout_s: wait: max seconds, None waits indefinitely. wait_for:
            max seconds before giving up, None means 60.
        pattern: wait_for: regex; returns on match, exit, or timeout. Use
            instead of polling log in a loop.
        data: write: stdin string.
        submit: write: append a newline; False sends raw bytes.
    """
    registry = resolve_registry(tool_context)

    if action == "list":
        return {
            "running": [_summarize(h) for h in registry.list_running()],
            "finished": [_summarize(h) for h in registry.list_finished()],
        }

    if action == "spawn":
        if not command:
            return {"error": "action 'spawn' requires command"}
        return await _spawn(command, cwd, tool_context)

    if action not in {"poll", "log", "wait", "wait_for", "kill", "write"}:
        return {"error": f"unknown action {action!r}"}

    if not session_id:
        return {"error": f"action {action!r} requires session_id"}
    handle = registry.get(session_id)
    if handle is None:
        return {"error": f"no such session {session_id!r}"}

    if action == "poll":
        return await _poll(handle)

    if action == "log":
        chunk, next_offset, exit_code = await handle.read(
            offset=offset, limit=limit
        )
        return {
            "session_id": handle.session_id,
            "data": _decode(chunk),
            "offset": offset,
            "next_offset": next_offset,
            "exit_code": exit_code,
            "status": "running" if handle.is_running else "exited",
        }

    if action == "wait_for":
        if not pattern:
            return {"error": "action 'wait_for' requires pattern"}
        return await _wait_for(
            handle,
            pattern,
            _DEFAULT_WAIT_FOR_TIMEOUT_S if timeout_s is None else timeout_s,
        )

    if action == "wait":
        exit_code = await handle.wait(timeout=timeout_s)
        return {
            "session_id": handle.session_id,
            "status": "running" if handle.is_running else "exited",
            "exit_code": exit_code,
            "timed_out": handle.is_running,
        }

    if action == "kill":
        await handle.kill()
        return {
            "session_id": handle.session_id,
            "status": "killed",
            "exit_code": handle.exit_code,
        }

    if action == "write":
        if data is None:
            return {"error": "action 'write' requires data"}
        payload = data.encode("utf-8")
        if submit and not payload.endswith(b"\n"):
            payload += b"\n"
        try:
            await handle.write(payload)
        except (RuntimeError, OSError) as exc:
            return {
                "error": f"write failed ({exc}); the process may have exited"
            }
        return {
            "session_id": handle.session_id,
            "status": "written",
            "bytes": len(payload),
        }


__all__ = ["process"]
