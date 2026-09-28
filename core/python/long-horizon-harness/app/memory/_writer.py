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

"""Shared helpers for writing scoped events into Memory Bank.

``add_memory_tool`` and ``skill_curator`` (and any future writer) share the
same dedup + event-shape contract: search for an exact text match first;
otherwise append an Event carrying a single text Part and a ``lha_memory_scope``
tag in both ``custom_metadata`` slots.
"""

from __future__ import annotations

from google.adk.events import Event
from google.adk.memory import BaseMemoryService
from google.genai.types import Content, Part


async def entry_exists(
    *,
    memory_service: BaseMemoryService,
    app_name: str,
    user_id: str,
    text: str,
) -> bool:
    """True if a memory entry with exactly this text already exists for the user."""
    response = await memory_service.search_memory(
        app_name=app_name, user_id=user_id, query=text
    )
    needle = text.strip()
    for memory in response.memories:
        if not memory.content or not memory.content.parts:
            continue
        for part in memory.content.parts:
            if part.text and part.text.strip() == needle:
                return True
    return False


async def write_memory_event(
    *,
    text: str,
    scope: str,
    invocation_id: str,
    author: str,
    memory_service: BaseMemoryService,
    app_name: str,
    user_id: str,
    session_id: str,
) -> None:
    event = Event(
        invocation_id=invocation_id,
        author=author,
        content=Content(parts=[Part(text=text)]),
        custom_metadata={"lha_memory_scope": scope},
    )
    await memory_service.add_events_to_memory(
        app_name=app_name,
        user_id=user_id,
        events=[event],
        session_id=session_id,
        custom_metadata={"lha_memory_scope": scope},
    )
