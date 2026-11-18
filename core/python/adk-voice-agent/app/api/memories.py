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

"""Routes for listing and deleting the signed-in user's profile fields and memory episodes."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException

from app.auth.identity import current_user_id
from app.memory.episodes import KINDS, EpisodeStore, get_episode_store
from app.memory.profile import field_label, field_order, field_text
from app.memory.store import MemoryStore, get_memory_store

router = APIRouter()

_EPISODE_PAGE = 50
_MEMORY_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _store() -> MemoryStore:
    store = get_memory_store()
    if store is None:
        raise HTTPException(status_code=404, detail="Memory is off.")
    return store


@router.get("/memories")
async def list_memories() -> dict[str, Any]:
    fields = await _store().fields(current_user_id())
    return {
        "memories": [
            {
                "id": f.id,
                "field": f.field,
                "label": field_label(f.field),
                "value": field_text(f.value),
                "updated_at": f.updated_at,
            }
            for f in sorted(fields, key=lambda f: field_order(f.field))
        ]
    }


@router.delete("/memories/{memory_id}")
async def delete_memory(memory_id: str) -> dict[str, bool]:
    if not await _store().delete_field(current_user_id(), memory_id):
        raise HTTPException(status_code=404, detail="Memory not found.")
    return {"deleted": True}


def _episode_store() -> EpisodeStore:
    store = get_episode_store()
    if store is None:
        raise HTTPException(status_code=404, detail="Memory is off.")
    return store


@router.get("/memory-episodes")
async def list_episodes() -> dict[str, Any]:
    store, user_id = _episode_store(), current_user_id()
    found = {
        kind: await store.latest(user_id, kind, limit=_EPISODE_PAGE)
        for kind in KINDS
    }
    return {
        kind: [
            {
                "id": e.id,
                "kind": e.kind,
                "fact": e.fact,
                "created_at": e.created_at,
                "ref": e.ref,
            }
            for e in items
        ]
        for kind, items in found.items()
    }


@router.delete("/memory-episodes/{kind}/{memory_id}")
async def delete_episode(kind: str, memory_id: str) -> dict[str, bool]:
    if kind not in KINDS or not _MEMORY_ID.match(memory_id):
        raise HTTPException(status_code=404, detail="Memory not found.")
    if not await _episode_store().delete(current_user_id(), kind, memory_id):
        raise HTTPException(status_code=404, detail="Memory not found.")
    return {"deleted": True}
