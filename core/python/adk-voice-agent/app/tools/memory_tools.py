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

"""Voice tools over episode memories and the user profile (`app.memory`)."""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

from google.adk.tools.tool_context import ToolContext
from google.genai import types

from app.auth.identity import acting_user_id
from app.memory import episodes
from app.memory.ingest import curated_turns
from app.memory.profile import UserProfile, field_label
from app.memory.store import get_memory_store
from app.threads.describe import STATE_PHRASES, thread_ref
from app.threads.registry import get_thread_registry
from app.tools.thread_tools import _session

logger = logging.getLogger(__name__)

_MAX_CONVERSATION_TURNS = 30
_MAX_TURN_CHARS = 300


async def recall(query: str) -> str:
    """Search memories of earlier conversations and threads with the user.

    Args:
        query: Keywords describing what to look for.
    """
    store = episodes.get_episode_store()
    if store is None:
        return "Nothing is remembered yet."
    user_id = acting_user_id()
    try:
        found = await asyncio.wait_for(
            asyncio.gather(
                *(
                    store.search(
                        user_id, kind, query, top_k=episodes.RECALL_TOP_K
                    )
                    for kind in episodes.KINDS
                )
            ),
            episodes.READ_TIMEOUT_S * 2,
        )
    except Exception as exc:
        logger.warning("Could not search memories: %r", exc)
        return "Memory isn't available right now."
    lines = [
        line
        for kind_found in found
        for e in kind_found
        if (line := await _line(e))
    ]
    return (
        "\n".join(lines)
        if lines
        else f"Nothing remembered matches '{query}'. Try `list_threads` for recent threads."
    )


async def recall_conversation(
    session_id: str, tool_context: ToolContext | None = None
) -> str:
    """Read the transcript of an earlier conversation by its ID from `recall`.

    Args:
        session_id: Conversation ID returned by `recall`.
    """
    session = _session(tool_context)
    try:
        service = tool_context._invocation_context.session_service  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        return "That conversation isn't available."
    if session is None or service is None:
        return "That conversation isn't available."
    try:
        # Scoped to the signed-in user: another user's conversation is simply not found.
        earlier = await asyncio.wait_for(
            service.get_session(
                app_name=session.app_name,
                user_id=session.user_id,
                session_id=session_id.strip(),
            ),
            episodes.READ_TIMEOUT_S * 2,
        )
    except Exception as exc:
        logger.warning("Could not read conversation %s: %r", session_id, exc)
        return "That conversation isn't available."
    turns = curated_turns(earlier.events if earlier else [])
    if not turns:
        return "That conversation is no longer available."
    return "\n".join(
        f"{'User' if role == 'user' else 'You'}: {' '.join(text.split())[:_MAX_TURN_CHARS]}"
        for role, text in turns[-_MAX_CONVERSATION_TURNS:]
    )


async def _line(episode: episodes.Episode) -> str:
    when = (
        datetime.datetime.fromtimestamp(
            episode.created_at, datetime.UTC
        ).strftime("%b %d")
        if episode.created_at
        else ""
    )
    head = f"- [{episode.kind}{', ' + when if when else ''}] {episode.fact}"
    if episode.kind == episodes.THREAD:
        thread_id = str(episode.metadata.get("thread_id") or "")
        handle = (
            await get_thread_registry().get(thread_id) if thread_id else None
        )
        if handle is None:
            # The thread is gone (deleted, or past retention): don't offer a dead pointer.
            return ""
        return f"{head} Now: {thread_ref(handle)}, {STATE_PHRASES[handle.state]} (use `read_thread`)."
    session_id = str(episode.metadata.get("session_id") or "")
    return (
        f"{head} (conversation {session_id}; `recall_conversation` reads it)"
        if session_id
        else head
    )


async def remember(fact: str, tool_context: ToolContext | None = None) -> str:
    """Save a preference, standing rule, or fact the user explicitly asks you to remember.

    Args:
        fact: What to remember, in the user's words.
    """
    store = get_memory_store()
    text = " ".join(fact.split())
    if store is None or not text:
        return "Memory is off, so nothing was saved."
    session = _session(tool_context)
    labels = (
        {"session_id": episodes.revision_label(session.id)}
        if session is not None and session.id
        else None
    )
    try:
        await store.generate(
            acting_user_id(),
            [
                types.Content(
                    role="user", parts=[types.Part.from_text(text=text)]
                )
            ],
            revision_labels=labels,
        )
    except Exception as exc:
        logger.warning("Could not save a memory: %r", exc)
        return "Couldn't save that right now."
    return "Saved. It shows under What ADK Voice Agent Remembers, where the user can change it."


async def forget(field: str) -> str:
    """Forget one part of what you remember about the user, when they ask you to."""
    store = get_memory_store()
    if store is None:
        return "Memory is off; nothing is remembered."
    user_id = acting_user_id()
    key = field.strip().lower().replace(" ", "_")
    try:
        matches = [f for f in await store.fields(user_id) if f.field == key]
        for found in matches:
            await store.delete_field(user_id, found.id)
    except Exception as exc:
        logger.warning("Could not forget a memory: %r", exc)
        return "Couldn't forget that right now."
    return (
        f"Forgot {field_label(key).lower()}."
        if matches
        else "Nothing was remembered there."
    )


# The fields `forget` takes come from the one profile schema, so they never drift.
forget.__doc__ = (
    f"{forget.__doc__}\n\n    Args:\n        field: One of: "
    + ", ".join(UserProfile.model_fields)
    + "."
)


def episode_tools() -> list[Any]:
    """The memory tools for the live agent: none when memory is off."""
    from app.voice.non_blocking import non_blocking_tool

    if not episodes.episodes_enabled():
        return []
    return [non_blocking_tool(recall), recall_conversation, remember, forget]
