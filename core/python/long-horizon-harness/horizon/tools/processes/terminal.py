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

"""``bash``: foreground shell with a minimal parameter space,
``bash(command, timeout_s)``. No ``cwd`` (use ``cd dir && cmd``), no
``background``/``on_timeout``: background spawn moved to
``process(action='spawn', command=..., cwd=...)`` so the tool count stays
the same and nothing is lost.

A command still running past ``timeout_s`` auto-promotes to a background
session (partial output preserved) rather than being killed — this is now
fixed internal behavior, not a model-facing choice; ``on_timeout='kill'``
had no other caller and is gone.

Per-operation permission gating is handled centrally by ``permission_guard``
in the ``before_tool_callback`` chain, so this tool just runs the command.
"""

from __future__ import annotations

from typing import Any

from horizon.environment.registry import resolve_registry
from horizon.environment_context import active_environment
from horizon.tools._output_overflow import emit_stream, make_preview
from horizon.tools.processes._spawn import open_handle


async def bash(
    command: str,
    timeout_s: int = 30,
    tool_context: Any | None = None,
) -> dict[str, Any]:
    """Run a shell command (POSIX /bin/sh, not bash/zsh) in the workspace
    root.

    Still running past timeout_s? Auto-backgrounded (partial output kept)
    — drive it via `process`. Use `cd dir && cmd` for another directory,
    or `process(action='spawn', ...)` to background from the start.
    """
    root = active_environment().working_dir.resolve()

    handle = await open_handle(command, root)
    exit_code = await handle.wait(timeout=float(timeout_s))

    if exit_code is not None:
        data, _, _ = await handle.read()
        # No promotion needed; drop the handle without registering. Spill
        # oversized output to lha/tool-output/ (same recovery path as the
        # foreground tool) so the full text isn't lost behind the preview.
        output, truncated, overflow_path = await emit_stream(
            data.decode("utf-8", errors="replace"), stream="stdout"
        )
        result: dict[str, Any] = {
            "stdout": output,
            "stderr": "",
            "exit_code": exit_code,
            "timed_out": False,
            "truncated": truncated,
        }
        if overflow_path:
            result["stdout_overflow_path"] = overflow_path
        return result

    # Still running past timeout — promote.
    data, _, _ = await handle.read()
    partial, _ = make_preview(data.decode("utf-8", errors="replace"))
    resolve_registry(tool_context).register(handle)
    return {
        "session_id": handle.session_id,
        "pid": handle.pid,
        "status": "running",
        "timed_out": True,
        "partial_output": partial,
        "command": command,
        "hint": (
            f"Foreground timeout exceeded; the command is now backgrounded "
            f"as session {handle.session_id}. Drive it via the process tool "
            "(poll/wait/kill/write)."
        ),
    }
