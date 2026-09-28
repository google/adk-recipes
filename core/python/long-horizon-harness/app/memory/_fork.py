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

"""Pure helpers shared by fork-style review agents.

Owns the snapshot/event helpers and the whitelist-callback factory that
the three fork modules (``review_fork``, ``flush_fork``, ``dream_review``)
use to format their conversation input and gate their toolsets. The
runner / task-lifecycle plumbing they used to share now lives in
``app/memory/sibling_agent_plugin.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)


def event_text(event: Any) -> str:
    content = getattr(event, "content", None)
    if not content:
        return ""
    parts = getattr(content, "parts", None) or []
    return "".join(part.text for part in parts if getattr(part, "text", None))


def format_conversation_snapshot(
    events: list[Any], *, tag: str = "CONVERSATION"
) -> str:
    lines: list[str] = []
    for event in events:
        text = event_text(event)
        if not text:
            continue
        author = getattr(event, "author", "?") or "?"
        lines.append(f"[{author}]\n{text}")
    body = "\n\n".join(lines) if lines else "(no conversation)"
    return f"<{tag}>\n{body}\n</{tag}>"


ExtraCheck = Callable[[str, Any], "dict[str, Any] | None"]


def make_whitelist_callback(
    allowed: frozenset[str],
    *,
    fork_name: str,
    extra_check: ExtraCheck | None = None,
) -> Callable[..., Any]:
    """Build a ``before_tool_callback`` that blocks anything outside ``allowed``.

    ``extra_check(name, args)`` runs after the allow-list check passes and
    can return its own block dict (e.g. path-scoped writes) — same shape as
    ADK guardrails: dict short-circuits, ``None`` lets the call through.
    """

    async def _callback(
        *, tool: Any, args: Any, tool_context: Any
    ) -> dict[str, Any] | None:
        del tool_context
        name = getattr(tool, "name", "") or ""
        if name not in allowed:
            return {
                "error": (
                    f"Tool {name!r} is not available in the {fork_name} fork. "
                    f"Allowed: {sorted(allowed)}."
                )
            }
        if extra_check is not None:
            return extra_check(name, args)
        return None

    return _callback


def log_successful_writes(event: Any, *, log_prefix: str) -> None:
    """Optional ``on_event`` helper: log function responses with success=True.

    Used by review_fork to surface memory/skill writes to ops logs since
    ADK callbacks have no UI channel for mid-stream summaries.
    """
    content = getattr(event, "content", None)
    if not content:
        return
    parts = getattr(content, "parts", None) or []
    for part in parts:
        function_response = getattr(part, "function_response", None)
        if function_response is None:
            continue
        name = getattr(function_response, "name", "?")
        response = getattr(function_response, "response", None) or {}
        if isinstance(response, Mapping) and response.get("success"):
            logger.info(
                "%s: %s -> %s",
                log_prefix,
                name,
                response.get("message", "success"),
            )


__all__ = [
    "ExtraCheck",
    "event_text",
    "format_conversation_snapshot",
    "log_successful_writes",
    "make_whitelist_callback",
]
