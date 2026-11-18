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

"""Claude Code (`claude -p`) adapter."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, ClassVar

from app.app_utils.models import (
    claude_haiku_model,
    claude_model,
    claude_sonnet_model,
)
from app.threads.model import PermissionRequest
from app.workers.harnesses.activity import mcp_activity
from app.workers.harnesses.base import (
    MCP_CONFIG_FILE,
    MCP_SERVER_NAME,
    MCP_TOKEN_ENV,
    MCP_URL_ENV,
    THREAD_CONTEXT_FILE,
    WEB_SEARCH_VERB,
    CliHarness,
    HarnessEvent,
    HarnessResult,
    RunRequest,
    first_line,
    short_path,
)
from app.workers.policy import load_policy

SETTINGS_FILE = "claude-settings.json"

_TOOL_VERBS = {
    "Read": "Reading",
    "Edit": "Editing",
    "MultiEdit": "Editing",
    "Write": "Writing",
    "NotebookEdit": "Editing",
    "Grep": "Searching for",
    "Glob": "Listing",
    "WebFetch": "Fetching",
    "WebSearch": WEB_SEARCH_VERB,
}


class ClaudeCodeHarness(CliHarness):
    name = "claude"
    display_name = "Claude Code"
    icon = "claude"
    description = (
        "Headless Claude Code CLI with stream-json output and session resume."
    )
    aliases: ClassVar[tuple[str, ...]] = (
        "claude-code",
        "claude_code",
        "claudecode",
    )

    def new_session_id(self) -> str:
        return str(uuid.uuid4())

    def thread_files(self, req: RunRequest) -> dict[str, str]:
        files = super().thread_files(req)
        files[SETTINGS_FILE] = json.dumps(
            permission_settings(load_policy().permissions), indent=2
        )
        if _gateway_configured(req):
            files[MCP_CONFIG_FILE] = json.dumps(
                {"mcpServers": {MCP_SERVER_NAME: gateway_server()}}, indent=2
            )
        return files

    def build_argv(self, req: RunRequest) -> list[str]:
        argv = [
            "claude",
            "-p",
            req.prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--append-system-prompt-file",
            f"{req.thread_dir}/{THREAD_CONTEXT_FILE}",
            "--system-prompt-snapshot",
            "off",
            "--settings",
            f"{req.thread_dir}/{SETTINGS_FILE}",
        ]
        if req.session_id:
            argv += (
                ["--resume", req.session_id]
                if req.resume
                else ["--session-id", req.session_id]
            )
        # Headless mode cannot prompt interactively; `--settings` deny rules still apply.
        argv += ["--permission-mode", "bypassPermissions"]
        if _gateway_configured(req):
            argv += ["--mcp-config", f"{req.thread_dir}/{MCP_CONFIG_FILE}"]
        return argv

    def build_env(self, base: dict[str, str]) -> dict[str, str]:
        model = claude_model()
        sonnet = claude_sonnet_model()
        haiku = claude_haiku_model()
        env = {
            **base,
            "CLAUDE_CODE_USE_VERTEX": os.getenv("CLAUDE_CODE_USE_VERTEX")
            or "1",
            "CLOUD_ML_REGION": os.getenv("CLOUD_ML_REGION") or "global",
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": os.getenv(
                "ANTHROPIC_DEFAULT_OPUS_MODEL"
            )
            or model,
            "CLAUDE_CODE_SUBAGENT_MODEL": os.getenv(
                "CLAUDE_CODE_SUBAGENT_MODEL"
            )
            or model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": sonnet,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": haiku,
        }
        project = (
            base.get("ANTHROPIC_VERTEX_PROJECT_ID")
            or base.get("GOOGLE_CLOUD_PROJECT")
            or os.getenv("ANTHROPIC_VERTEX_PROJECT_ID")
            or os.getenv("GOOGLE_CLOUD_PROJECT")
        )
        if project:
            env["ANTHROPIC_VERTEX_PROJECT_ID"] = project
        for key in (
            "VERTEX_REGION_CLAUDE_HAIKU_4_5",
            "VERTEX_REGION_CLAUDE_4_6_SONNET",
        ):
            if os.getenv(key):
                env[key] = os.environ[key]
        return env

    def parse_line(
        self, line: str, state: dict[str, Any]
    ) -> list[HarnessEvent]:
        try:
            payload = json.loads(line)
        except ValueError:
            return []
        if not isinstance(payload, dict):
            return []
        kind = payload.get("type")
        if (
            kind == "system"
            and payload.get("subtype") == "init"
            and payload.get("session_id")
        ):
            return [HarnessEvent.session(payload["session_id"])]
        if kind == "assistant":
            return _assistant_events(payload.get("message") or {})
        if kind == "permission_request":
            perm = PermissionRequest.from_dict(payload.get("permission"))
            return [HarnessEvent.permission_requested(perm)] if perm else []
        if kind == "result":
            text = str(payload.get("result") or "")
            return [
                HarnessEvent.finished(
                    HarnessResult(
                        text=text or str(payload.get("subtype") or ""),
                        is_error=bool(payload.get("is_error"))
                        or payload.get("subtype", "success") != "success",
                    )
                )
            ]
        return []


def permission_settings(permissions: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "permissions": {
            "deny": [f"Bash({glob})" for glob in permissions.get("deny", [])]
        }
    }


def gateway_server() -> dict[str, Any]:
    return {
        "type": "http",
        "url": f"${{{MCP_URL_ENV}}}",
        "headers": {"Authorization": f"Bearer ${{{MCP_TOKEN_ENV}}}"},
    }


def _gateway_configured(req: RunRequest) -> bool:
    return bool(req.env.get(MCP_URL_ENV) and req.env.get(MCP_TOKEN_ENV))


def _assistant_events(message: dict[str, Any]) -> list[HarnessEvent]:
    events = []
    for part in message.get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and str(part.get("text") or "").strip():
            events.append(HarnessEvent.progress(first_line(part["text"])))
        elif part.get("type") == "tool_use":
            events.append(
                HarnessEvent.activity(
                    _tool_label(part.get("name", ""), part.get("input") or {})
                )
            )
    return events


def _tool_label(tool: str, args: dict[str, Any]) -> str:
    if tool == "Bash":
        return f"Running {first_line(str(args.get('command', '')), 80)}"
    if tool in _TOOL_VERBS:
        path = args.get("file_path") or args.get("notebook_path")
        if path:
            target = short_path(path)
        else:
            target = first_line(
                str(
                    args.get("pattern")
                    or args.get("url")
                    or args.get("query")
                    or ""
                ),
                80,
            )
        return f"{_TOOL_VERBS[tool]} {target}".strip()
    if tool == "Task":
        return f"Delegating: {first_line(str(args.get('description', 'subtask')), 80)}"
    if tool == "TodoWrite":
        return "Updating its checklist"
    if tool.startswith("mcp__"):
        return mcp_activity(tool)
    return f"Using {tool}"
