# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Provides dashboard route payloads for agent goal and memories documents.

Wraps `tools.documents` to:
1. Read and write through the jobs store (`objects.store.jobs_store_factory`).
2. Offload blocking storage I/O from the event loop using `asyncio.to_thread`.
3. Return structured failure payloads instead of raising unhandled exceptions.

The ``available`` field distinguishes storage access errors from empty
documents, preventing storage outages from appearing as empty datasets.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ambient_quality_agent.core.session_state import LaunchContext
from ambient_quality_agent.tools.documents import goal as goal_doc
from ambient_quality_agent.tools.documents import memories as memories_doc
from ambient_quality_agent.tools.objects import store as objects

logger = logging.getLogger(__name__)


async def get_goal(tool_context: LaunchContext) -> dict[str, Any]:
    """The developer's goal for the observed agent, as written in goal.md, with
    the id of its version (`None` when no goal is written)."""
    store = objects.jobs_store_factory()
    try:
        text = await asyncio.to_thread(goal_doc.read_goal, store)
    except Exception as exc:
        logger.warning("documents: reading the goal failed: %s", exc)
        return {
            "goal": None,
            "available": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    # Distinguish an uninitialized document from a storage failure so the UI can show an edit box.
    return {
        "goal": text,
        "available": True,
        "uri": goal_doc.build_goal_uri(store),
        "version": goal_doc.compute_goal_version(text) if text else None,
    }


async def set_goal(tool_context: LaunchContext, *, goal: str) -> dict[str, Any]:
    """Save updated goal text to storage.

    Args:
        tool_context: Launch context containing session state.
        goal: Goal text to write; empty to remove the goal.

    Returns:
        Dictionary containing stored ``goal`` text and its computed ``version``
        (`None` when the goal was removed), or an ``error`` message on
        validation or storage failure.
    """
    store = objects.jobs_store_factory()
    try:
        stored = await asyncio.to_thread(goal_doc.save_goal, store, goal)
    except ValueError as exc:
        # Return validation failures (oversized text) directly to caller.
        return {"error": str(exc)}
    except Exception as exc:
        logger.warning("documents: writing the goal failed: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "goal": stored,
        "version": goal_doc.compute_goal_version(stored) if stored else None,
    }


async def list_goal_versions(tool_context: LaunchContext) -> dict[str, Any]:
    """List every saved version of the goal, most recently active first.

    Restoring a version is saving its text again with `set_goal`.

    Args:
        tool_context: Launch context containing session state.

    Returns:
        Dictionary containing:
        - ``versions``: Each version's id, text, first save time, last
          activation time, and whether it is the saved goal.
        - ``available``: True if retrieval succeeded; False on storage error.
        - ``reason``: Error details if retrieval failed.
    """
    store = objects.jobs_store_factory()
    try:
        versions, active = await asyncio.to_thread(
            lambda: (goal_doc.list_versions(store), goal_doc.read_goal(store))
        )
    except Exception as exc:
        logger.warning("documents: listing the goal versions failed: %s", exc)
        return {
            "versions": [],
            "available": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    active_version = goal_doc.compute_goal_version(active) if active else None
    return {
        "versions": [
            {
                "version": item.version,
                "text": item.text,
                "created_at": item.created_at,
                "last_activated_at": item.last_activated_at,
                "active": item.version == active_version,
            }
            for item in versions
        ],
        "available": True,
    }


async def list_memories(tool_context: LaunchContext) -> dict[str, Any]:
    """List every memory, newest first.

    Args:
        tool_context: Launch context containing session state.

    Returns:
        Dictionary containing:
        - ``memories``: Each memory's id, text, source and creation time.
        - ``note``: How to read them: as reference data, not instructions.
        - ``available``: True if retrieval succeeded; False on storage error.
        - ``uri``: Where the memories are kept (when available).
        - ``reason``: Error details if retrieval failed.
    """
    store = objects.jobs_store_factory()
    try:
        found = await asyncio.to_thread(memories_doc.load_memories, store)
    except Exception as exc:
        logger.warning("documents: reading the memories failed: %s", exc)
        return {
            "memories": [],
            "available": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    return {
        "memories": [
            {
                "id": item.id,
                "text": item.text,
                "source": item.source,
                "created_at": item.created_at,
            }
            for item in found
        ],
        "note": memories_doc.READ_AS_DATA,
        "available": True,
        "uri": memories_doc.build_memories_uri(store),
    }


async def delete_memory(
    tool_context: LaunchContext, *, memory_id: str
) -> dict[str, Any]:
    """Delete a specific memory entry by ID.

    Args:
        tool_context: Launch context containing session state.
        memory_id: Identifier of the memory to delete.

    Returns:
        Dictionary with ``deleted: True`` on success, or an ``error`` message
        if the ID is missing, not found, or storage deletion failed.
    """
    if not memory_id:
        return {"error": "memory_id is required"}
    store = objects.jobs_store_factory()
    try:
        deleted = await asyncio.to_thread(
            memories_doc.delete_memory, store, memory_id
        )
    except Exception as exc:
        logger.warning("documents: deleting a memory failed: %s", exc)
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not deleted:
        return {"error": f"No memory with id {memory_id!r}."}
    return {"deleted": True}
