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

"""Filter and ingest session events into Memory Bank when a live session closes.

Profile extraction (`curated_contents`) receives only user speech and thread
requests (`start_thread` / `message_thread` / `share_context`); tool output,
model speech, and external content are excluded so untrusted text cannot plant
facts in the user's profile.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import Sequence
from typing import Any

from google.genai import types

from app.auth.identity import run_as
from app.memory.store import get_memory_store
from app.voice.background import spawn

logger = logging.getLogger(__name__)

_THREAD_ARGUMENTS = {
    "start_thread": ("goal", "Thread request"),
    "message_thread": ("instruction", "Thread feedback"),
    "share_context": ("instruction", "Thread feedback"),
}

_watched: weakref.WeakSet[asyncio.Task] = weakref.WeakSet()


def curated_contents(events: Sequence[Any]) -> list[types.Content]:
    """The only parts of session `events` that may update the user's profile: user speech and thread requests."""
    return [
        types.Content(role=role, parts=[types.Part.from_text(text=text)])
        for role, text in curated_turns(events)
        if role == "user"
    ]


def curated_turns(events: Sequence[Any]) -> list[tuple[str, str]]:
    """The same parts as `curated_contents`, as `(role, text)` pairs: role `user` or `model`."""
    return [
        (role, text.strip())
        for event in events
        for role, text in _remembered(event)
        if text.strip()
    ]


def _remembered(event: Any) -> list[tuple[str, str]]:
    found = spoken_turns(event)
    content = getattr(event, "content", None)
    for part in (content.parts or []) if content else []:
        call = part.function_call
        if call is None or call.name not in _THREAD_ARGUMENTS:
            continue
        argument, label = _THREAD_ARGUMENTS[call.name]
        value = (call.args or {}).get(argument)
        if isinstance(value, str) and value.strip():
            found.append(("user", f"{label}: {value}"))
    return found


def spoken_turns(event: Any) -> list[tuple[str, str]]:
    """What was said aloud in one session event, as `(role, text)` pairs (role `user` or
    `model`): the user's transcribed speech and ADK Voice Agent's transcribed reply.
    Typed or tagged turns, tool calls, and tool output are never speech."""
    found: list[tuple[str, str]] = []
    heard = getattr(event, "input_transcription", None)
    if heard is not None and heard.text:
        found.append(("user", heard.text))
    said = getattr(event, "output_transcription", None)
    if said is not None and said.text:
        found.append(("model", said.text))
    return found


def remember_on_close(
    user_id: str, session: Any, session_task: asyncio.Task | None
) -> None:
    """When `session_task` (the task running a live session) ends, update `user_id`'s
    profile from what this connection added to `session`, and remember the
    conversation itself (`app.memory.episodes`). Fire-and-forget."""
    store = get_memory_store()
    if store is None or session_task is None or session is None:
        return
    if session_task in _watched:
        return
    _watched.add(session_task)
    # A resumed session already holds earlier connections' events; those were sent then.
    start = len(session.events)
    started_at = time.time()

    def on_done(_: asyncio.Task) -> None:
        from app.memory import episodes

        turns = curated_turns(session.events[start:])
        if not turns:
            return
        contents = curated_contents(session.events[start:])
        session_id = str(getattr(session, "id", "") or "")
        # Each profile change records the conversation that taught it.
        labels = (
            {"session_id": episodes.revision_label(session_id)}
            if session_id
            else None
        )
        if contents:
            with run_as(user_id):
                spawn(
                    _generate(store, user_id, contents, labels),
                    name="memory-generate",
                )
        episodes.remember_conversation(user_id, session_id, turns, started_at)

    session_task.add_done_callback(on_done)


async def _generate(
    store: Any,
    user_id: str,
    contents: list[types.Content],
    labels: dict[str, str] | None = None,
) -> None:
    try:
        if labels:
            await store.generate(user_id, contents, revision_labels=labels)
        else:
            await store.generate(user_id, contents)
    except Exception:
        # Remembering is best effort; the conversation already ended normally.
        logger.warning("Could not update the user's profile", exc_info=True)
