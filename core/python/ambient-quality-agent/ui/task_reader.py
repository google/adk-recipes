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

"""Reads the A2A task store from the UI service, over the raw JSON.

The agent writes chat transcripts to ``gs://<jobs bucket>/tasks/<id>.json``
(`ambient_quality_agent.app_utils.task_store`). This reads them back for the
dashboard's conversation list.

**Deliberately a second implementation rather than an import.** The UI image
ships only ``app.py``, this package and ``ambient_quality_shared`` -- its
Dockerfile asserts exactly that -- so importing the agent's `GcsTaskStore`
would work under a local `make dev`, where the whole repository is on the path,
and fail with `ModuleNotFoundError` in the deployed service. It would also pull
in `a2a-sdk`, which the UI image does not install.

The cost is a contract to keep: the object layout and the field names below are
the agent's, duplicated here. `tests/test_ui_lha.py` pins them by having the
agent's own writer produce the bytes this parses, so the two cannot drift
silently.

Only three fields are read -- the task id, its conversation, and when it last
changed. The transcript itself is passed through untouched.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from typing import Any

TASKS_PREFIX = "tasks/"

logger = logging.getLogger(__name__)


def _create_storage_client() -> Any:
    """Creates a storage client using a lazy import so tests can inject a fake.

    Follows the same injection seam as `ambient_quality_agent.tools.gcs`.

    Returns:
        Storage client instance.
    """
    from google.cloud import storage

    return storage.Client()


def _list_stored_tasks(bucket: str) -> Iterator[tuple[Any, dict[str, Any]]]:
    """Yields parsed task objects paired with their GCS blobs.

    Applies `TASKS_PREFIX` to restrict operations to task transcripts, preventing
    access to investigation run state or `aqa-telemetry/` in the same bucket.
    Downloads each blob individually; this O(N) scan matches the current deployment
    scale with 90-day prefix lifecycle expiration.

    Args:
        bucket: Cloud Storage bucket name holding the task transcripts.

    Yields:
        Tuples of `(blob, task_data)`.
    """
    client = _create_storage_client()
    for blob in client.list_blobs(bucket, prefix=TASKS_PREFIX):
        try:
            task = json.loads(blob.download_as_bytes())
        except Exception:
            # Ignore unreadable objects so an invalid blob does not fail the entire
            # listing. A corrupt blob is therefore also undeletable, and lingers
            # until the prefix lifecycle rule expires it.
            logger.warning("skipping unreadable task blob %s", blob.name)
            continue
        if isinstance(task, dict):
            yield blob, task


def _load_all(bucket: str) -> list[dict[str, Any]]:
    """Loads all stored tasks from the bucket.

    Args:
        bucket: Cloud Storage bucket name.

    Returns:
        List of deserialized task dictionaries.
    """
    return [task for _, task in _list_stored_tasks(bucket)]


def _extract_updated_at(task: dict[str, Any]) -> str:
    """Extracts the task's status timestamp, or empty string if missing.

    Proto JSON omits unset fields, so a task without a status has no
    ``status`` key. RFC 3339 UTC timestamps sort lexicographically.

    Args:
        task: Deserialized task dictionary.

    Returns:
        RFC 3339 timestamp string, or an empty string when absent.
    """
    status = task.get("status")
    if not isinstance(status, dict):
        return ""
    stamp = status.get("timestamp")
    return stamp if isinstance(stamp, str) else ""


async def list_contexts(bucket: str) -> list[dict[str, Any]]:
    """Lists conversation contexts ordered newest first.

    Each context's ``task_ids`` are oldest first, the order the chat replays
    them in. ``contextId`` and other keys remain lowerCamelCase because the
    agent serializes the `a2a_pb2.Task` proto with `MessageToJson`.

    Args:
        bucket: Cloud Storage bucket name holding the task transcripts.

    Returns:
        Conversation summary dictionaries sorted by updated_at descending.
    """
    tasks = await asyncio.to_thread(_load_all, bucket)
    grouped: dict[str, dict[str, Any]] = {}
    for task in sorted(tasks, key=_extract_updated_at):
        context_id = task.get("contextId") or ""
        entry = grouped.setdefault(
            context_id,
            {"context_id": context_id, "task_ids": [], "updated_at": ""},
        )
        if task_id := task.get("id"):
            entry["task_ids"].append(task_id)
        stamp = _extract_updated_at(task)
        entry["updated_at"] = max(entry["updated_at"], stamp)
    return sorted(grouped.values(), key=lambda e: e["updated_at"], reverse=True)


async def list_context_tasks(
    bucket: str, context_id: str
) -> list[dict[str, Any]]:
    """Lists every task in a conversation, ordered chronologically for replay.

    Returned in raw stored format because the client parses the A2A encoding
    directly, avoiding schema drift from intermediate re-encoding.

    Args:
        bucket: Cloud Storage bucket name holding the task transcripts.
        context_id: Conversation identifier to filter tasks.

    Returns:
        List of task dictionaries sorted by timestamp ascending.
    """
    tasks = await asyncio.to_thread(_load_all, bucket)
    mine = [
        task for task in tasks if (task.get("contextId") or "") == context_id
    ]
    return sorted(mine, key=_extract_updated_at)


def _delete_context(bucket: str, context_id: str) -> int:
    """Synchronously deletes task blobs matching a conversation context.

    An empty `context_id` deletes nothing. A task written without a `contextId`
    reads as `""`, so matching on it would erase every malformed blob in the
    bucket at once -- too much damage for one mistyped request to do.

    Args:
        bucket: Cloud Storage bucket name.
        context_id: Identifier of the conversation context to erase.

    Returns:
        Count of deleted task blobs.
    """
    if not context_id:
        return 0
    deleted = 0
    for blob, task in _list_stored_tasks(bucket):
        if task.get("contextId") == context_id:
            blob.delete()
            deleted += 1
    return deleted


async def delete_context(bucket: str, context_id: str) -> int:
    """Deletes all task blobs for a conversation context.

    Conversations are identified by shared `contextId`. Erasing these blobs
    prevents the context from reappearing in subsequent listings. Nonexistent
    contexts return 0; storage errors raise to avoid masking partial deletions.

    Args:
        bucket: Cloud Storage bucket name.
        context_id: Identifier of the conversation context to erase.

    Returns:
        Count of deleted task blobs.
    """
    return await asyncio.to_thread(_delete_context, bucket, context_id)
