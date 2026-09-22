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

"""memory tool: write durable facts into ADK Memory Bank, or search past
sessions (action='search', folded in from the former standalone
session_search tool).

Supports an "agent" vs "user" scope split for writes; ADK Memory Bank owns
persistence (there is no on-disk store).
"""

from __future__ import annotations

from typing import Any, Literal

from google.adk.memory import BaseMemoryService
from google.adk.tools.tool_context import ToolContext

from horizon.infrastructure.constants import APP_NAME
from horizon.memory._content_safety import (
    MEMORY_CHAR_LIMIT,
    USER_CHAR_LIMIT,
    scan_memory_content,
)
from horizon.memory._writer import entry_exists, write_memory_event
from horizon.telemetry.ui import record_memory_write

# recall_past_sessions_entries is imported lazily inside _search_past_sessions:
# horizon.tools's __init__ import chain reaches add_memory_tool.memory, so a
# top-level import of anything under horizon.tools here would be circular.

Scope = Literal["agent", "user"]
_VALID_SCOPES: tuple[str, ...] = ("agent", "user")
_SCOPE_PREFIX = {"agent": "[agent] ", "user": "[user] "}


def _char_limit(scope: Scope) -> int:
    if scope == "user":
        return USER_CHAR_LIMIT
    return MEMORY_CHAR_LIMIT


def _scoped_text(scope: Scope, content: str) -> str:
    return f"{_SCOPE_PREFIX[scope]}{content}"


async def add_memory_entry(
    *,
    content: str,
    scope: str,
    memory_service: BaseMemoryService,
    app_name: str,
    user_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Append one durable fact to the configured Memory Bank.

    Pure helper: takes the service + identifiers directly so it can be unit-
    tested without a Runner / InvocationContext. The LLM-callable surface is
    `memory` below, which pulls the same identifiers off ToolContext.
    """
    if scope not in _VALID_SCOPES:
        return {
            "success": False,
            "error": (
                f"Invalid scope {scope!r}. Use one of: {', '.join(_VALID_SCOPES)}."
            ),
        }

    cleaned = content.strip()
    if not cleaned:
        return {"success": False, "error": "Content cannot be empty."}

    limit = _char_limit(scope)
    if len(cleaned) > limit:
        return {
            "success": False,
            "error": (
                f"Content is {len(cleaned)} chars; {scope} scope limit is "
                f"{limit}. Shorten the entry or split it across multiple "
                "writes."
            ),
        }

    scan_error = scan_memory_content(cleaned)
    if scan_error:
        return {"success": False, "error": scan_error}

    scoped_text = _scoped_text(scope, cleaned)
    if await entry_exists(
        memory_service=memory_service,
        app_name=app_name,
        user_id=user_id,
        text=scoped_text,
    ):
        return {
            "success": True,
            "scope": scope,
            "message": "Entry already in memory (no duplicate added).",
        }

    await write_memory_event(
        text=scoped_text,
        scope=scope,
        invocation_id=f"add_memory:{scope}",
        author="user" if scope == "user" else "system",
        memory_service=memory_service,
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )

    return {"success": True, "scope": scope, "message": "Entry added."}


async def _search_past_sessions(
    *,
    session_id: str | None,
    limit: int | None,
    tool_context: ToolContext | None,
) -> dict[str, Any]:
    from horizon.tools.past_sessions import recall_past_sessions_entries

    if tool_context is None:
        return {
            "success": False,
            "error": "memory must be called via the agent runtime.",
        }

    invocation_context = tool_context._invocation_context
    session_service = invocation_context.session_service
    if session_service is None:
        return {
            "success": False,
            "error": "Session service is not configured for this runtime.",
        }

    return await recall_past_sessions_entries(
        session_service=session_service,
        app_name=invocation_context.app_name or APP_NAME,
        user_id=invocation_context.user_id,
        current_session_id=invocation_context.session.id,
        session_id=session_id,
        limit=limit,
    )


async def memory(
    action: Literal["add", "search"] = "add",
    content: str | None = None,
    scope: Literal["user", "agent"] = "agent",
    session_id: str | None = None,
    limit: int | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Save a durable fact, or search your other chat sessions.

    Args:
        content: add: the fact, compact and self-contained.
        scope: add: "user" about the user, "agent" your own notes.
        session_id: search: omit to list recent sessions, or pass one from
            a prior list call to read its turns.
        limit: search: cap on results (list 20/50, read 100/200).
    """
    if action == "search":
        return await _search_past_sessions(
            session_id=session_id, limit=limit, tool_context=tool_context
        )
    if action != "add":
        return {
            "success": False,
            "error": f"Unknown action {action!r}; use 'add' or 'search'.",
        }

    if tool_context is None:
        return {
            "success": False,
            "error": "memory must be called via the agent runtime.",
        }
    if not content:
        return {
            "success": False,
            "error": "content is required for action='add'.",
        }

    invocation_context = tool_context._invocation_context
    if invocation_context.memory_service is None:
        return {
            "success": False,
            "error": "Memory service is not configured for this runtime.",
        }

    result = await add_memory_entry(
        content=content,
        scope=scope,
        memory_service=invocation_context.memory_service,
        app_name=invocation_context.app_name,
        user_id=invocation_context.user_id,
        session_id=invocation_context.session.id,
    )
    if result.get("success"):
        record_memory_write(
            tool_context=tool_context, scope=scope, content=content
        )
    return result
