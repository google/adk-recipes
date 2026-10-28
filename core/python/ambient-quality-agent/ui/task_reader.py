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

Five fields are read from each task: its id, its conversation, when it last
changed, and the text and the agent key of its first user message (the
``role``, ``parts`` and ``metadata`` of each ``history`` entry). The transcript itself is passed through untouched.

Saved chat names belong to the UI service and are never read by the agent.
They live under ``chat-titles/<context_id>.json`` rather than ``tasks/``
because both the agent's store and this reader parse every object under
``tasks/`` as a task.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Iterator
from typing import Any

from google.api_core.exceptions import NotFound

TASKS_PREFIX = "tasks/"
TITLES_PREFIX = "chat-titles/"

# Bounds the `/lha/contexts` payload. The dashboard truncates the first message
# to a shorter title, so this limit is never visible in the UI.
FIRST_MESSAGE_MAX_LENGTH = 200

logger = logging.getLogger(__name__)


def _create_storage_client() -> Any:
    """Creates a storage client using a lazy import so tests can inject a fake.

    Follows the same injection seam as `ambient_quality_agent.tools.gcs`.

    Returns:
        Storage client instance, sending AQuA's product token.
    """
    from ambient_quality_shared.usage_headers import build_product_token
    from google.api_core.client_info import ClientInfo
    from google.cloud import storage

    return storage.Client(
        client_info=ClientInfo(user_agent=build_product_token())
    )


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


def _extract_first_message(task: dict[str, Any]) -> str | None:
    """Extracts the text of the first user message in a task's history.

    Proto JSON serializes the role enum by name (`ROLE_USER`). User messages
    without text (such as file-only attachments) are skipped.

    Args:
        task: Deserialized task dictionary.

    Returns:
        Joined text parts with whitespace collapsed and truncated to
        `FIRST_MESSAGE_MAX_LENGTH` characters, or None if no user message
        contains text.
    """
    history = task.get("history")
    if not isinstance(history, list):
        return None
    for message in history:
        if not isinstance(message, dict) or message.get("role") != "ROLE_USER":
            continue
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        texts = [
            part["text"]
            for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        text = " ".join(" ".join(texts).split())
        if text:
            return text[:FIRST_MESSAGE_MAX_LENGTH]
    return None


def _extract_agent_key(task: dict[str, Any]) -> str | None:
    """Extracts the agent a task's user messages say the conversation is about.

    The dashboard sends the agent key in each message's metadata as well as in
    the request's, because only the message is kept in the transcript: the
    agent reads the request's, and the conversation list reads this.

    Args:
        task: Deserialized task dictionary.

    Returns:
        The `agent_key` of the first user message that carries one, or None.
    """
    history = task.get("history")
    if not isinstance(history, list):
        return None
    for message in history:
        if not isinstance(message, dict) or message.get("role") != "ROLE_USER":
            continue
        metadata = message.get("metadata")
        if not isinstance(metadata, dict):
            continue
        agent_key = metadata.get("agent_key")
        if isinstance(agent_key, str) and agent_key.strip():
            return agent_key.strip()
    return None


def _build_title_blob_name(context_id: str) -> str:
    """Builds the object name that holds a conversation's saved name.

    Mirrors `GcsTaskStore._build_blob_name`: a context id containing a slash
    would otherwise escape the prefix and could overwrite another object in
    the bucket.

    Args:
        context_id: Conversation context identifier.

    Returns:
        Object name under `TITLES_PREFIX`.
    """
    return f"{TITLES_PREFIX}{context_id.replace('/', '_')}.json"


def _get_context_titles(
    bucket: str, context_ids: Iterable[str]
) -> dict[str, str]:
    """Reads the saved names for the given conversations.

    Lists `TITLES_PREFIX` once and downloads only blobs matching `context_ids`.

    Args:
        bucket: Cloud Storage bucket name.
        context_ids: Conversation context identifiers to look up.

    Returns:
        Mapping of context_id to its saved name. Conversations without a
        readable saved name are omitted.
    """
    wanted = {_build_title_blob_name(c): c for c in context_ids if c}
    if not wanted:
        return {}
    client = _create_storage_client()
    titles: dict[str, str] = {}
    for blob in client.list_blobs(bucket, prefix=TITLES_PREFIX):
        context_id = wanted.get(blob.name)
        if context_id is None:
            continue
        try:
            payload = json.loads(blob.download_as_bytes())
        except Exception:
            # Skip unreadable title blobs so the listing succeeds and the UI
            # falls back to a derived title.
            logger.warning("skipping unreadable chat title blob %s", blob.name)
            continue
        title = payload.get("title") if isinstance(payload, dict) else None
        if isinstance(title, str) and title.strip():
            titles[context_id] = title
    return titles


async def list_contexts(bucket: str) -> list[dict[str, Any]]:
    """Lists conversation contexts ordered newest first.

    Each context's ``task_ids`` are oldest first, the order the chat replays
    them in. ``contextId`` and other keys remain lowerCamelCase because the
    agent serializes the `a2a_pb2.Task` proto with `MessageToJson`.

    Args:
        bucket: Cloud Storage bucket name holding the task transcripts.

    Returns:
        Conversation summary dictionaries sorted by updated_at descending.
        Each carries ``title``, the saved name or None,
        ``first_message``, the first user text in replay order or None, and
        ``agent_key``, the agent the conversation is about or None when its
        messages name none.
    """
    tasks = await asyncio.to_thread(_load_all, bucket)
    grouped: dict[str, dict[str, Any]] = {}
    for task in sorted(tasks, key=_extract_updated_at):
        context_id = task.get("contextId") or ""
        entry = grouped.setdefault(
            context_id,
            {
                "context_id": context_id,
                "task_ids": [],
                "updated_at": "",
                "title": None,
                "first_message": None,
                "agent_key": None,
            },
        )
        if task_id := task.get("id"):
            entry["task_ids"].append(task_id)
        stamp = _extract_updated_at(task)
        entry["updated_at"] = max(entry["updated_at"], stamp)
        if entry["first_message"] is None:
            entry["first_message"] = _extract_first_message(task)
        if entry["agent_key"] is None:
            entry["agent_key"] = _extract_agent_key(task)
    titles = await asyncio.to_thread(_get_context_titles, bucket, grouped)
    for context_id, title in titles.items():
        grouped[context_id]["title"] = title
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
    """Synchronously deletes a conversation's task blobs and its saved name.

    An empty `context_id` deletes nothing: tasks stored without a `contextId`
    read as `""`, so matching an empty string would erase every malformed blob
    in the bucket.

    Deletes the saved name after all task blobs so a failed task deletion does
    not strip the conversation's name.

    Args:
        bucket: Cloud Storage bucket name.
        context_id: Identifier of the conversation context to erase.

    Returns:
        Count of deleted task blobs. The saved name is not counted.
    """
    if not context_id:
        return 0
    deleted = 0
    for blob, task in _list_stored_tasks(bucket):
        if task.get("contextId") == context_id:
            blob.delete()
            deleted += 1
    title_blob = (
        _create_storage_client()
        .bucket(bucket)
        .blob(_build_title_blob_name(context_id))
    )
    try:
        title_blob.delete()
    except NotFound:
        pass
    return deleted


async def delete_context(bucket: str, context_id: str) -> int:
    """Deletes all task blobs and the saved name of a conversation context.

    Conversations are identified by shared `contextId`. Erasing these blobs
    prevents the context from reappearing in subsequent listings. Nonexistent
    contexts return 0; storage errors raise to avoid masking partial deletions.

    Args:
        bucket: Cloud Storage bucket name.
        context_id: Identifier of the conversation context to erase.

    Returns:
        Count of deleted task blobs. The saved name is not counted.
    """
    return await asyncio.to_thread(_delete_context, bucket, context_id)


def _save_context_title(bucket: str, context_id: str, title: str) -> None:
    """Synchronously writes a conversation's saved name.

    Args:
        bucket: Cloud Storage bucket name.
        context_id: Conversation context identifier.
        title: Name to save.
    """
    blob = (
        _create_storage_client()
        .bucket(bucket)
        .blob(_build_title_blob_name(context_id))
    )
    blob.upload_from_string(
        json.dumps({"title": title}), content_type="application/json"
    )


async def save_context_title(bucket: str, context_id: str, title: str) -> None:
    """Saves the name a user gave a conversation, replacing any earlier one.

    The name is stored apart from the conversation's tasks, so saving one for
    a conversation with no stored task yet is not an error.

    Args:
        bucket: Cloud Storage bucket name.
        context_id: Conversation context identifier.
        title: Name to save, already validated by the caller.

    Raises:
        ValueError: If `context_id` is empty, since tasks without a
            `contextId` all read as `""` and would share one name.
    """
    if not context_id:
        raise ValueError("context_id must not be empty")
    await asyncio.to_thread(_save_context_title, bucket, context_id, title)
