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

"""Antigravity CLI (`agy -p`) adapter."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from typing import Any, ClassVar

from app.app_utils.models import antigravity_model
from app.auth.sandbox_env import genai_enterprise_env
from app.threads.model import PermissionRequest
from app.workers import runs
from app.workers.environment import Environment
from app.workers.harnesses.activity import mcp_activity
from app.workers.harnesses.base import (
    WEB_SEARCH_VERB,
    CliHarness,
    HarnessEvent,
    HarnessResult,
    RunRequest,
    first_line,
    short_path,
)
from app.workers.policy import load_policy

logger = logging.getLogger(__name__)

DEFAULT_EFFORT = "medium"
_ERROR_STATUSES = {"ERROR", "INVALID", "CANCELED", "INTERRUPTED"}
_TRANSIENT = re.compile(
    r"\b(429|503)\b|UNAVAILABLE|RESOURCE_EXHAUSTED|high demand"
)
# agy `command(...)` rules match leading words and cannot express wildcards before the end or shell operators.
_NOT_A_WORD = re.compile(r"[*|&;<>]")

_TOOL_LABELS = {
    "read_file": ("Reading", "Path"),
    "write_file": ("Writing", "Path"),
    "edit_file": ("Editing", "Path"),
    "list_dir": ("Listing", "DirectoryPath"),
    "find_by_name": ("Finding", "Pattern"),
    "grep_search": ("Searching for", "Query"),
    "read_url_content": ("Fetching", "Url"),
    "search_web": (WEB_SEARCH_VERB, "query"),
}
_PATH_PARAMS = {"Path", "DirectoryPath"}


class AntigravityHarness(CliHarness):
    name = "antigravity"
    display_name = "Antigravity"
    icon = "antigravity"
    description = "Headless Antigravity CLI with stream-json output and conversation resume."
    aliases: ClassVar[tuple[str, ...]] = (
        "agy",
        "gemini",
        "gemini-cli",
        "gemini_cli",
    )

    def thread_files(self, req: RunRequest) -> dict[str, str]:
        files = super().thread_files(req)
        project = _project_id(req.thread_id)
        # agy discovers projects only under `~/.gemini/config/projects/`; `cleanup` removes it.
        files[_project_file(req.thread_id)] = json.dumps(
            {
                "id": project,
                "name": project,
                "projectResources": {
                    "resources": [{"folderUri": f"file://{req.cwd}"}]
                },
                "permissionGrants": {
                    "permissionGrants": {
                        "deny": deny_rules(load_policy().permissions)
                    }
                },
            },
            indent=2,
        )
        return files

    def build_argv(self, req: RunRequest) -> list[str]:
        # agy has no system-prompt flag, so prepend `system_prompt` to `-p`.
        argv = [
            "agy",
            "-p",
            f"{req.system_prompt}\n\n{req.prompt}",
            "--output-format",
            "stream-json",
            "--project",
            _project_id(req.thread_id),
            "--print-timeout",
            f"{round(runs.default_max_runtime_s())}s",
        ]
        model = antigravity_model()
        argv += ["--model", model]
        effort = (os.getenv("ANTIGRAVITY_EFFORT") or DEFAULT_EFFORT).strip()
        if effort:
            argv += ["--effort", effort]
        if req.resume and req.session_id:
            argv += ["--conversation", req.session_id]
        argv.append("--dangerously-skip-permissions")
        return argv

    async def cleanup(self, env: Environment, thread_id: str) -> None:
        path = shlex.quote(_project_file(thread_id).removeprefix("~/"))
        result = await env.exec(f'rm -f "$HOME"/{path}', timeout_s=15.0)
        if not result.ok:
            logger.warning(
                "Could not remove the agy project of %s: %s",
                thread_id,
                result.describe_failure(),
            )

    def build_env(self, base: dict[str, str]) -> dict[str, str]:
        return {
            **base,
            **genai_enterprise_env(),
            "AGY_ADC_AUTH": os.getenv("AGY_ADC_AUTH") or "1",
        }

    def parse_line(
        self, line: str, state: dict[str, Any]
    ) -> list[HarnessEvent]:
        try:
            payload = json.loads(line)
        except ValueError:
            return []
        if not isinstance(payload, dict):
            return []
        event = payload.get("event")
        if event == "init" and payload.get("conversation_id"):
            return [HarnessEvent.session(payload["conversation_id"])]
        if event == "step_update":
            step = payload.get("step_update") or {}
            if step.get("step_type") == "tool":
                label = _tool_label(step)
                return [HarnessEvent.activity(label)] if label else []
            if (
                step.get("step_type") == "agent_response"
                or "text_delta" in step
            ):
                return _narrate(step, state)
            return []
        if event == "permission_request":
            perm = PermissionRequest.from_dict(payload.get("permission"))
            return [HarnessEvent.permission_requested(perm)] if perm else []
        if event == "result":
            result = payload.get("result")
            res = result if isinstance(result, dict) else payload
            return _result_events(res)
        return []


_warned_deny_globs: set[str] = set()


def deny_rules(permissions: dict[str, list[str]]) -> list[str]:
    """Render expressible shell deny globs as `command(<leading words>)` rules."""
    rules = []
    for glob in permissions.get("deny", []):
        words = glob.removesuffix("*").strip()
        if not words or _NOT_A_WORD.search(words):
            if glob not in _warned_deny_globs:
                _warned_deny_globs.add(glob)
                logger.warning(
                    "agy cannot express the shell deny glob %r; skipped", glob
                )
            continue
        rules.append(f"command({words})")
    return rules


def _project_id(thread_id: str) -> str:
    return f"voice-agent-{thread_id}"


def _project_file(thread_id: str) -> str:
    return f"~/.gemini/config/projects/{_project_id(thread_id)}.json"


def _tool_label(step: dict[str, Any]) -> str:
    if step.get("state", "ACTIVE") != "ACTIVE":
        return ""
    info = step.get("tool_info") or {}
    tool = step.get("tool_name") or info.get("name") or "a tool"
    params = info.get("parameters") or {}
    if tool == "finish":
        return ""
    if tool == "run_command":
        return f"Running {first_line(str(params.get('CommandLine', '')), 80)}".strip()
    if tool == "call_mcp_tool":
        return mcp_activity(str(params.get("ToolName") or ""))
    if tool in _TOOL_LABELS:
        verb, param = _TOOL_LABELS[tool]
        value = str(params.get(param) or "")
        target = (
            short_path(value)
            if param in _PATH_PARAMS
            else first_line(value, 80)
        )
        return f"{verb} {target}".strip()
    return f"Using {tool}"


def _narrate(step: dict[str, Any], state: dict[str, Any]) -> list[HarnessEvent]:
    key = step.get("step_index")
    narrated: set = state.setdefault("narrated", set())
    pending: dict = state.setdefault("pending", {})
    if key is not None and key in narrated:
        return []
    text = pending.pop(key, "") + str(step.get("text_delta") or "")
    finished = step.get("state", "DONE") != "ACTIVE"
    if not finished and not any(ln.strip() for ln in text.split("\n")[:-1]):
        pending[key] = text
        return []
    if key is not None:
        narrated.add(key)
    line = first_line(text)
    if not line or line.startswith(("{", "[", "```")):
        return []
    return [HarnessEvent.progress(line)]


def _result_events(res: dict[str, Any]) -> list[HarnessEvent]:
    status = str(res.get("status", "SUCCESS")).upper()
    error = str(res.get("error") or "")
    is_error = status in _ERROR_STATUSES or bool(error)
    interrupted = is_error and (
        status == "INTERRUPTED" or "interrupted" in error.lower()
    )
    text = (
        error
        if is_error and error
        else str(res.get("response") or res.get("result") or "")
    )
    events = []
    if res.get("conversation_id"):
        events.append(HarnessEvent.session(res["conversation_id"]))
    events.append(
        HarnessEvent.finished(
            HarnessResult(
                text=text,
                is_error=is_error,
                retryable=is_error
                and bool(interrupted or _TRANSIENT.search(error)),
            )
        )
    )
    return events
