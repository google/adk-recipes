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

"""Approval round-trip for gated MCP gateway calls."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.threads.model import PermissionKind, PermissionRequest
from app.threads.registry import get_thread_registry

_MAX_CALL_DISPLAY = 300
_MAX_TARGET_TEXT = 120
_MAX_FREE_TEXT = 80
_TARGET_KEYS = (
    "to",
    "cc",
    "bcc",
    "attendees",
    "owner",
    "repo",
    "head",
    "base",
    "title",
    "subject",
    "calendar_id",
    "file_id",
    "document_id",
    "spreadsheet_id",
    "issue_number",
    "pull_number",
    "branch",
)
_FREE_TEXT_KEYS = (
    "body",
    "content",
    "description",
    "message",
    "comment",
    "text",
    "notes",
)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    message: str = ""


def glob_match(pattern: str, text: str) -> bool:
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, text, flags=re.DOTALL) is not None


def matching_rule(text: str, globs: list[str]) -> str | None:
    return next((g for g in globs if glob_match(g, text)), None)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def permission_key(tool_name: str, tool_input: dict[str, Any]) -> str:
    """One-time grant key for an exact tool call and its arguments."""
    digest = hashlib.sha256(
        (tool_name + canonical_json(tool_input)).encode("utf-8")
    )
    return digest.hexdigest()[:16]


def _clip_value(key: str, value: Any) -> Any:
    if isinstance(value, str):
        limit = _MAX_TARGET_TEXT if key in _TARGET_KEYS else _MAX_FREE_TEXT
        return value[: limit - 1] + "…" if len(value) > limit else value
    if isinstance(value, dict):
        return {k: _clip_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip_value(key, item) for item in value]
    return value


def describe_call(tool_name: str, tool_input: dict[str, Any]) -> str:
    ordered: dict[str, Any] = {}
    for key in _TARGET_KEYS:
        if key in tool_input:
            ordered[key] = _clip_value(key, tool_input[key])
    for key in sorted(tool_input):
        if key not in ordered and key not in _FREE_TEXT_KEYS:
            ordered[key] = _clip_value(key, tool_input[key])
    for key in _FREE_TEXT_KEYS:
        if key in tool_input and key not in ordered:
            ordered[key] = _clip_value(key, tool_input[key])
    args = json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)
    display = f"{tool_name} {args}"
    if len(display) > _MAX_CALL_DISPLAY:
        display = display[: _MAX_CALL_DISPLAY - 1] + "…"
    return display


def paused_message(what: str) -> str:
    return (
        f"Paused for the user's approval of: {what}. Stop here and end your turn; "
        "you will be resumed once they decide."
    )


async def request_permission(
    thread_id: str, tool_name: str, tool_input: dict[str, Any], *, rule: str
) -> Decision:
    """Allow a gated call with a matching grant, or pause the thread for approval."""
    arguments = dict(tool_input or {})
    permission = PermissionRequest(
        action_class=rule,
        command=describe_call(tool_name, arguments),
        key=permission_key(tool_name, arguments),
        kind=PermissionKind.TOOL,
        arguments=arguments,
    )
    if await get_thread_registry().request_permission(thread_id, permission):
        return Decision(True)
    return Decision(False, paused_message(permission.command))
