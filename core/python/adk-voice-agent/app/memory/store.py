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

"""Per-user profile storage over Vertex AI Memory Bank (`{"user_id": ...}` scope)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Protocol

from google.genai import types

from app.app_utils.services import AgentEngine, agent_engine
from app.memory.profile import PROFILE_SCHEMA_ID, UserProfile

logger = logging.getLogger(__name__)

RECALL_TIMEOUT_S = 1.5

_PROFILE_TYPE = "STRUCTURED_PROFILE"
_MEMORY_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_PAGE_SIZE = 100


@dataclass(frozen=True)
class ProfileField:
    id: str
    field: str
    value: Any
    updated_at: float | None = None


class MemoryStore(Protocol):
    """Per-user profile storage. Implementations scope every call to `user_id`."""

    async def profile(self, user_id: str) -> dict[str, Any]:
        """The user's consolidated profile: field -> value (empty when none yet)."""
        ...

    async def fields(self, user_id: str) -> list[ProfileField]:
        """The user's profile fields, each with the id `delete_field` takes."""
        ...

    async def delete_field(self, user_id: str, memory_id: str) -> bool:
        """Forget one of the user's fields; False if it isn't theirs or doesn't exist."""
        ...

    async def generate(
        self,
        user_id: str,
        contents: Sequence[types.Content],
        revision_labels: dict[str, str] | None = None,
    ) -> None:
        """Update the user's profile from conversation `contents` (returns before it finishes).

        `revision_labels` tag the revisions it creates (for example the conversation
        that taught them)."""
        ...


def _scope(user_id: str) -> dict[str, str]:
    return {"user_id": user_id}


def _platform_client(engine: AgentEngine) -> Any:
    """An Agent Platform client for `engine`'s project and region (reads ADC)."""
    import agentplatform

    return agentplatform.Client(
        project=engine.project, location=engine.location
    )


class MemoryBankStore:
    """`MemoryStore` over the Agent Engine's Memory Bank (`agentplatform` memory_banks.memories)."""

    def __init__(self, engine: AgentEngine) -> None:
        self.engine = engine

    @property
    def engine_name(self) -> str:
        """The reasoning engine's full resource name."""
        e = self.engine
        if "/" in e.engine_id:
            return e.engine_id
        return f"projects/{e.project}/locations/{e.location}/reasoningEngines/{e.engine_id}"

    @cached_property
    def _client(self) -> Any:
        return _platform_client(self.engine)

    async def _memories(self) -> Any:
        # The first client build reads ADC (maybe the metadata server): keep it off the loop.
        client = await asyncio.to_thread(lambda: self._client)
        return client.aio.memory_banks.memories

    async def profile(self, user_id: str) -> dict[str, Any]:
        memories = await self._memories()
        response = await memories.retrieve_profiles(
            name=self.engine_name, scope=_scope(user_id)
        )
        found = (response.profiles or {}).get(PROFILE_SCHEMA_ID)
        return dict(found.profile or {}) if found else {}

    async def fields(self, user_id: str) -> list[ProfileField]:
        memories = await self._memories()
        pager = await memories.retrieve(
            name=self.engine_name,
            scope=_scope(user_id),
            simple_retrieval_params={"page_size": _PAGE_SIZE},
            config={"memory_types": [_PROFILE_TYPE]},
        )
        found: list[ProfileField] = []
        async for item in pager:
            memory = item.memory
            content = memory.structured_content if memory else None
            if not memory or not memory.name or not content:
                continue
            if content.schema_id != PROFILE_SCHEMA_ID:
                continue
            updated = (
                memory.update_time.timestamp() if memory.update_time else None
            )
            found.extend(
                ProfileField(
                    id=memory.name.rsplit("/", 1)[-1],
                    field=key,
                    value=value,
                    updated_at=updated,
                )
                for key, value in (content.data or {}).items()
            )
        return found

    async def delete_field(self, user_id: str, memory_id: str) -> bool:
        if not _MEMORY_ID.match(memory_id):
            return False
        from google.genai import errors

        memories = await self._memories()
        name = f"{self.engine_name}/memories/{memory_id}"
        try:
            memory = await memories.get(name=name)
        except errors.ClientError as exc:
            if exc.code == 404:
                return False
            raise
        memory_type = getattr(memory.memory_type, "value", memory.memory_type)
        # Owner check on the server side of the API: the scope must be exactly theirs.
        if memory.scope != _scope(user_id) or memory_type != _PROFILE_TYPE:
            return False
        await memories.delete(name=name)
        return True

    async def generate(
        self,
        user_id: str,
        contents: Sequence[types.Content],
        revision_labels: dict[str, str] | None = None,
    ) -> None:
        memories = await self._memories()
        config: dict[str, Any] = {"wait_for_completion": False}
        if revision_labels:
            config["revision_labels"] = revision_labels
        await memories.generate(
            name=self.engine_name,
            direct_contents_source={
                "events": [{"content": content} for content in contents]
            },
            scope=_scope(user_id),
            config=config,
        )


_store: MemoryStore | None = None


def memory_enabled() -> bool:
    """Whether ADK Voice Agent remembers: there is a store to remember with, the
    configured Agent Engine (or one passed to `set_memory_store`)."""
    return _store is not None or agent_engine() is not None


def get_memory_store() -> MemoryStore | None:
    """The memory store, or None when memory is off."""
    global _store
    if not memory_enabled():
        return None
    if _store is None:
        engine = agent_engine()
        if engine is None:
            return None
        _store = MemoryBankStore(engine)
    return _store


def set_memory_store(store: MemoryStore | None) -> None:
    """Use `store` (a test double, or None to go back to the configured backend)."""
    global _store
    _store = store


async def recall_profile(user_id: str) -> dict[str, Any]:
    """The user's profile, or {} when memory is off, it is empty, or the read fails or
    takes longer than `RECALL_TIMEOUT_S`. The one read path: voice sessions and threads."""
    store = get_memory_store()
    if store is None:
        return {}
    try:
        return await asyncio.wait_for(store.profile(user_id), RECALL_TIMEOUT_S)
    except Exception as exc:
        # Includes the timeout: a slow or failing Memory Bank never blocks the user.
        logger.warning("Could not read the user's profile: %r", exc)
        return {}


def ensure_profile_schema() -> str:
    """Register the `user-profile` schema on the Agent Engine's Memory Bank, with
    natural-language memories off, keeping its other settings. Idempotent: `make deploy`
    runs it after each apply with the deployer's credentials (scripts/deploy.py), since
    the app's own account can't change the engine. Returns what it did, for the log."""
    engine = agent_engine()
    if engine is None:
        return "skipped (no Agent Engine)"
    store = MemoryBankStore(engine)
    client = _platform_client(engine)
    resource = client.runtimes.get(name=store.engine_name).api_resource
    spec = resource.context_spec
    current = (
        spec.memory_bank_config.model_dump(mode="json", exclude_none=True)
        if spec and spec.memory_bank_config
        else {}
    )
    if _is_registered(current):
        return "already registered"
    client.runtimes.update(
        name=store.engine_name,
        config={
            "context_spec": {"memory_bank_config": memory_bank_config(current)}
        },
    )
    return "registered"


def _default_scope(configs: Any) -> dict[str, Any]:
    """The config without `scope_keys` (Memory Bank allows one), or {}."""
    return next((c for c in configs or [] if not c.get("scope_keys")), {})


def _is_registered(current: dict[str, Any]) -> bool:
    """Whether `current` already has this schema's fields and natural-language memories off."""
    schemas = _default_scope(current.get("structured_memory_configs")).get(
        "schema_configs"
    )
    ours = next(
        (s for s in schemas or [] if s.get("id") == PROFILE_SCHEMA_ID), None
    )
    fields = set((ours or {}).get("memory_schema", {}).get("properties") or {})
    nl_off = _default_scope(current.get("customization_configs")).get(
        "disable_natural_language_memories"
    )
    return fields == set(UserProfile.model_fields) and bool(nl_off)


def memory_bank_config(current: dict[str, Any]) -> dict[str, Any]:
    """`current` Memory Bank settings with ADK Voice Agent's profile schema registered and
    natural-language memories off for the default scope; everything else kept."""
    config = dict(current)
    schema = {
        "id": PROFILE_SCHEMA_ID,
        "memory_schema": UserProfile.model_json_schema(),
        "memory_type": _PROFILE_TYPE,
    }
    structured = _with_default_scope(config.get("structured_memory_configs"))
    structured[-1]["schema_configs"] = [
        *(
            s
            for s in structured[-1].get("schema_configs") or []
            if s.get("id") != PROFILE_SCHEMA_ID
        ),
        schema,
    ]
    customizations = _with_default_scope(config.get("customization_configs"))
    customizations[-1]["disable_natural_language_memories"] = True
    config["structured_memory_configs"] = structured
    config["customization_configs"] = customizations
    return config


def _with_default_scope(configs: Any) -> list[dict[str, Any]]:
    """Copies of `configs` with the default-scope one (created if missing) moved last."""
    copies = [dict(c) for c in configs or []]
    scoped = [c for c in copies if c.get("scope_keys")]
    default = [c for c in copies if not c.get("scope_keys")][:1] or [{}]
    return [*scoped, *default]
