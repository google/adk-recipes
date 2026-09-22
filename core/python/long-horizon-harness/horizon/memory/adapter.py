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

"""The single memory-backend abstraction.

Every ``isinstance(memory_service, VertexAiMemoryBankService)`` /
``InMemoryMemoryService`` check and every ``_get_api_client()`` /
``_agent_engine_id`` / ``_session_events`` private-attr reach lives here, behind
one ``MemoryAdapter`` capability surface. Callers (``user_profile``,
``dream_review``, ``memory_list``) name no concrete service class; a non-Vertex
backend degrades cleanly through ``NoopMemoryAdapter``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from horizon.infrastructure.memory_config import (
    USER_PROFILE_SCHEMA_ID,
    profile_scope,
)

logger = logging.getLogger(__name__)

Scope = Literal["user", "agent", "user_profile"]

_FETCH_CAP = 500

_MARKERS: tuple[tuple[str, Scope], ...] = (
    ("[user_profile]", "user_profile"),
    ("[user] ", "user"),
    ("[agent] ", "agent"),
)

_ACTION_KEYS = {
    "CREATED": "created",
    "UPDATED": "updated",
    "DELETED": "deleted",
}


@dataclass
class MemoryWriteEntry:
    scope: Scope
    content: str
    ts_ms: int
    session_id: str | None = None


@dataclass
class MemoryListResult:
    entries: list[MemoryWriteEntry]
    counts: dict[str, int] = field(
        default_factory=lambda: {"user": 0, "agent": 0, "user_profile": 0}
    )
    has_more: bool = False


def parse_marker(text: str | None) -> tuple[Scope, str] | None:
    if not text:
        return None
    for prefix, scope in _MARKERS:
        if text.startswith(prefix):
            remainder = text[len(prefix) :].lstrip("\n").rstrip()
            return scope, remainder
    return None


def engine_resource_name(memory_service: Any) -> str:
    """Fully-qualified ``reasoningEngines`` name for Memory Bank data-plane calls.

    Prefer the fully-qualified ``projects/{p}/locations/{l}/reasoningEngines/{id}``
    form over the bare ``reasoningEngines/{id}``: the data-plane has been observed
    to serve the qualified form during windows when it 404s the bare one. Source
    order: a pinned ``AGENT_ENGINE_RESOURCE_NAME`` for this engine, then the
    service's project/location, then the bare form as a last resort.
    """
    engine_id = memory_service._agent_engine_id
    suffix = f"reasoningEngines/{engine_id}"
    pinned = os.environ.get("AGENT_ENGINE_RESOURCE_NAME", "").strip()
    if pinned.endswith(suffix):
        return pinned
    project = getattr(memory_service, "_project", None)
    location = getattr(memory_service, "_location", None)
    if project and location:
        return f"projects/{project}/locations/{location}/{suffix}"
    return suffix


def _count_actions(operation: Any) -> dict[str, int]:
    counts = {"created": 0, "updated": 0, "deleted": 0}
    response = getattr(operation, "response", None)
    for generated in getattr(response, "generated_memories", None) or []:
        action = getattr(generated, "action", None)
        name = getattr(action, "name", None) or str(action)
        key = _ACTION_KEYS.get(name)
        if key:
            counts[key] += 1
    return counts


@runtime_checkable
class MemoryAdapter(Protocol):
    def supports_profiles(self) -> bool: ...
    async def retrieve_profile(
        self, *, app_name: str, user_id: str
    ) -> dict[str, Any] | None: ...
    async def generate_profile(
        self,
        *,
        app_name: str,
        user_id: str,
        events: list[Any],
        consolidate: bool,
    ) -> dict[str, int]: ...
    async def list_all(
        self, *, app_name: str, user_id: str
    ) -> list[MemoryWriteEntry]: ...


class NoopMemoryAdapter:
    """Backend without structured profiles or a list-all surface."""

    def supports_profiles(self) -> bool:
        return False

    async def retrieve_profile(
        self, *, app_name: str, user_id: str
    ) -> dict[str, Any] | None:
        return None

    async def generate_profile(
        self,
        *,
        app_name: str,
        user_id: str,
        events: list[Any],
        consolidate: bool,
    ) -> dict[str, int]:
        raise NotImplementedError(
            "this memory backend has no structured profiles"
        )

    async def list_all(
        self, *, app_name: str, user_id: str
    ) -> list[MemoryWriteEntry]:
        return []


class InMemoryMemoryAdapter(NoopMemoryAdapter):
    """ADK ``InMemoryMemoryService``: list-all only, no profiles."""

    def __init__(self, service: Any) -> None:
        self._service = service

    async def list_all(
        self, *, app_name: str, user_id: str
    ) -> list[MemoryWriteEntry]:
        # Private attribute: ADK's BaseMemoryService exposes no list-all API and
        # search_memory keyword-matches. Revisit when ADK adds one.
        sessions = self._service._session_events.get((app_name, user_id), {})
        out: list[MemoryWriteEntry] = []
        for session_id, events in sessions.items():
            for event in events:
                content = getattr(event, "content", None)
                parts = getattr(content, "parts", None) or []
                text_parts = [p.text for p in parts if getattr(p, "text", None)]
                if not text_parts:
                    continue
                parsed = parse_marker("\n".join(text_parts))
                if parsed is None:
                    continue
                scope, body = parsed
                ts = getattr(event, "timestamp", None)
                out.append(
                    MemoryWriteEntry(
                        scope=scope,
                        content=body,
                        ts_ms=int(ts * 1000) if ts else 0,
                        session_id=session_id,
                    )
                )
        return out


class VertexMemoryAdapter:
    """Vertex Memory Bank: the only place that touches its client + engine id."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def _client(self) -> Any:
        return self._service._get_api_client()

    def _name(self) -> str:
        return engine_resource_name(self._service)

    def supports_profiles(self) -> bool:
        return True

    async def retrieve_profile(
        self, *, app_name: str, user_id: str
    ) -> dict[str, Any] | None:
        try:
            response = (
                await self._client().agent_engines.memories.retrieve_profiles(
                    name=self._name(), scope=profile_scope(app_name, user_id)
                )
            )
        except Exception:
            logger.exception("retrieve_profile: retrieve_profiles failed")
            return None
        memory_profile = (getattr(response, "profiles", None) or {}).get(
            USER_PROFILE_SCHEMA_ID
        )
        profile = getattr(memory_profile, "profile", None)
        return dict(profile) if profile else None

    async def generate_profile(
        self,
        *,
        app_name: str,
        user_id: str,
        events: list[Any],
        consolidate: bool,
    ) -> dict[str, int]:
        from vertexai._genai.types import (
            GenerateAgentEngineMemoriesConfig,
            GenerateMemoriesRequestDirectContentsSource,
            GenerateMemoriesRequestDirectContentsSourceEvent,
        )

        from horizon.memory.content_sanitizer import sanitize_content_for_memory

        request_events = [
            GenerateMemoriesRequestDirectContentsSourceEvent(
                content=sanitize_content_for_memory(event.content)
            )
            for event in events
        ]
        operation = await self._client().agent_engines.memories.generate(
            name=self._name(),
            direct_contents_source=GenerateMemoriesRequestDirectContentsSource(
                events=request_events
            ),
            scope=profile_scope(app_name, user_id),
            config=GenerateAgentEngineMemoriesConfig(
                wait_for_completion=True,
                disable_consolidation=not consolidate,
            ),
        )
        return _count_actions(operation)

    async def list_all(
        self, *, app_name: str, user_id: str
    ) -> list[MemoryWriteEntry]:
        pager = await self._client().agent_engines.memories.retrieve(
            name=self._name(),
            scope={"app_name": app_name, "user_id": user_id},
            simple_retrieval_params={"page_size": _FETCH_CAP},
        )
        out: list[MemoryWriteEntry] = []
        # First page only: the genai AsyncPager's next_page() injects page_token
        # into the request, which the installed vertexai SDK rejects
        # (extra_forbidden). page_size is set to the fetch cap so one page covers
        # the listing.
        for retrieved in getattr(pager, "page", None) or []:
            memory = getattr(retrieved, "memory", None) or retrieved
            text = getattr(memory, "fact", None)
            parsed = parse_marker(text)
            if parsed is None:
                # Memory Bank distils facts without our markers; surface them as
                # plain "user" memories rather than dropping them.
                if not text or not text.strip():
                    continue
                scope, body = "user", text.strip()
            else:
                scope, body = parsed
            update_time = getattr(memory, "update_time", None) or getattr(
                memory, "create_time", None
            )
            ts_ms = int(update_time.timestamp() * 1000) if update_time else 0
            out.append(
                MemoryWriteEntry(
                    scope=scope, content=body, ts_ms=ts_ms, session_id=None
                )
            )
            if len(out) >= _FETCH_CAP:
                break
        return out


def memory_adapter(memory_service: Any) -> MemoryAdapter:
    """The single dispatch point over the concrete ADK memory services."""
    from google.adk.memory import InMemoryMemoryService
    from google.adk.memory.vertex_ai_memory_bank_service import (
        VertexAiMemoryBankService,
    )

    if isinstance(memory_service, VertexAiMemoryBankService):
        return VertexMemoryAdapter(memory_service)
    if isinstance(memory_service, InMemoryMemoryService):
        return InMemoryMemoryAdapter(memory_service)
    return NoopMemoryAdapter()


__all__ = [
    "MemoryAdapter",
    "MemoryListResult",
    "MemoryWriteEntry",
    "NoopMemoryAdapter",
    "Scope",
    "VertexMemoryAdapter",
    "engine_resource_name",
    "memory_adapter",
    "parse_marker",
]
