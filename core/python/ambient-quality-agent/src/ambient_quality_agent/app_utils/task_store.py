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

"""An A2A task store that outlives the process.

The SDK's `InMemoryTaskStore` loses every transcript when Agent Runtime recycles
an instance, so a conversation becomes unreadable minutes after it happened.
Tasks go to the jobs bucket instead -- the one investigation artifacts already
use -- so this adds no new bucket and no new IAM.

The layout is flat, `tasks/<task_id>.json`, because `get` is handed only a task
id. Listing by conversation therefore reads every task and filters in memory,
which is what the in-memory store does too and is sufficient at AQuA's scale: one
workspace, tens of conversations. It is O(all tasks ever) and the sidebar polls
it, so if that scale assumption stops holding, add a context index -- not a
second source of truth.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.server.tasks import InMemoryTaskStore, TaskStore
from a2a.types import a2a_pb2
from ambient_quality_agent.tools import gcs
from google.api_core import exceptions as api_exceptions
from google.protobuf import json_format

logger = logging.getLogger(__name__)

TASKS_PREFIX = "tasks/"
FIRST_MESSAGE_MAX_LENGTH = 200


class GcsTaskStore(TaskStore):
    """Persists A2A tasks as the `a2a_pb2.Task` proto in JSON form.

    Single-owner by design: AQuA has one shared workspace, so the owner
    dimension the in-memory store carries would only ever hold one key. Access
    control is the bucket's IAM, not a key in this store.
    """

    def __init__(self, bucket: str) -> None:
        self._bucket_name = bucket

    def _build_bucket(self) -> Any:
        return gcs.create_storage_client().bucket(self._bucket_name)

    @staticmethod
    def _build_blob_name(task_id: str) -> str:
        # A task id containing a slash would otherwise escape the prefix and
        # could overwrite an investigation artifact in the same bucket.
        return f"{TASKS_PREFIX}{task_id.replace('/', '_')}.json"

    async def save(
        self, task: a2a_pb2.Task, context: ServerCallContext
    ) -> None:
        await asyncio.to_thread(
            self._build_bucket()
            .blob(self._build_blob_name(task.id))
            .upload_from_string,
            json_format.MessageToJson(task),
            content_type="application/json",
        )

    async def get(
        self, task_id: str, context: ServerCallContext
    ) -> a2a_pb2.Task | None:
        try:
            raw = await asyncio.to_thread(
                self._build_bucket()
                .blob(self._build_blob_name(task_id))
                .download_as_bytes
            )
        except api_exceptions.NotFound:
            return None
        except Exception:
            # A read failure is not an absent task. Returning None here would
            # let the UI render a transcript it could not load as an empty one.
            logger.exception("could not read task %s", task_id)
            raise
        return json_format.Parse(raw.decode("utf-8"), a2a_pb2.Task())

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        try:
            await asyncio.to_thread(
                self._build_bucket().blob(self._build_blob_name(task_id)).delete
            )
        except api_exceptions.NotFound:
            return

    async def list(
        self, params: a2a_pb2.ListTasksRequest, context: ServerCallContext
    ) -> a2a_pb2.ListTasksResponse:
        tasks = await self._list_tasks()
        if context_id := getattr(params, "context_id", ""):
            tasks = [task for task in tasks if task.context_id == context_id]
        if status := getattr(params, "status", None):
            tasks = [task for task in tasks if task.status.state == status]
        return a2a_pb2.ListTasksResponse(tasks=tasks)

    async def list_contexts(self) -> Sequence[dict[str, Any]]:
        """One entry per conversation, newest first.

        Not part of `TaskStore`; this is what the dashboard's conversation list
        reads. Grouping lives here so the route stays a thin adapter.

        Returns:
            Sequence of dictionaries containing context_id, task_ids, and updated_at.
        """
        grouped: dict[str, dict[str, Any]] = {}
        for task in await self._list_tasks():
            entry = grouped.setdefault(
                task.context_id,
                {
                    "context_id": task.context_id,
                    "task_ids": [],
                    "updated_at": "",
                },
            )
            entry["task_ids"].append(task.id)
            stamp = _extract_status_timestamp(task)
            entry["updated_at"] = max(entry["updated_at"], stamp)
        return sorted(
            grouped.values(), key=lambda e: e["updated_at"], reverse=True
        )

    # `Sequence`, not `list`: `TaskStore.list` shadows the builtin for
    # annotations evaluated against this class body.
    async def _list_tasks(self) -> Sequence[a2a_pb2.Task]:
        """Lists every stored task from GCS.

        One listing plus a download each (see module docstring).

        Returns:
            Sequence of parsed Task protobuf messages.
        """

        def _load_all_tasks() -> list[a2a_pb2.Task]:
            client = gcs.create_storage_client()
            tasks = []
            for blob in client.list_blobs(
                self._bucket_name, prefix=TASKS_PREFIX
            ):
                try:
                    tasks.append(
                        json_format.Parse(
                            blob.download_as_bytes().decode("utf-8"),
                            a2a_pb2.Task(),
                        )
                    )
                except Exception:
                    # One corrupt blob must not empty the whole sidebar.
                    logger.warning(
                        "skipping unreadable task blob %s", blob.name
                    )
            return tasks

        return await asyncio.to_thread(_load_all_tasks)


def _extract_status_timestamp(task: a2a_pb2.Task) -> str:
    """Extracts the task's status timestamp as a sortable string.

    Args:
        task: Task message to inspect.

    Returns:
        JSON timestamp string, or empty string if unavailable.
    """
    try:
        return task.status.timestamp.ToJsonString()
    except Exception:
        return ""


def _extract_task_updated_at(task: dict[str, Any]) -> str:
    """Extracts the RFC 3339 status timestamp from a serialized task dict.

    Args:
        task: Deserialized proto-JSON task dictionary.

    Returns:
        Timestamp string, or empty string when absent.
    """
    status = task.get("status")
    if not isinstance(status, dict):
        return ""
    stamp = status.get("timestamp")
    return stamp if isinstance(stamp, str) else ""


def _extract_task_first_message(task: dict[str, Any]) -> str | None:
    """Extracts the text of the first user message in a task's history.

    Args:
        task: Deserialized proto-JSON task dictionary.

    Returns:
        Collapsed user text truncated to `FIRST_MESSAGE_MAX_LENGTH`, or None.
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


def _extract_task_metadata(task: dict[str, Any], key: str) -> str | None:
    """Extracts a string metadata field from the first user message carrying it.

    Args:
        task: Deserialized proto-JSON task dictionary.
        key: Metadata key to read (such as ``agent_key``).

    Returns:
        Trimmed metadata value, or None when absent.
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
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def build_context_summaries(
    tasks: Iterable[dict[str, Any]], titles: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Summarizes serialized tasks into `/lha/contexts` conversation entries.

    Args:
        tasks: Deserialized proto-JSON task dictionaries.
        titles: Mapping of ``context_id`` to user-saved chat titles.

    Returns:
        Conversation summaries sorted by ``updated_at`` descending.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for task in sorted(tasks, key=_extract_task_updated_at):
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
        stamp = _extract_task_updated_at(task)
        entry["updated_at"] = max(entry["updated_at"], stamp)
        if entry["first_message"] is None:
            entry["first_message"] = _extract_task_first_message(task)
        if entry["agent_key"] is None:
            entry["agent_key"] = _extract_task_metadata(task, "agent_key")
    for context_id, entry in grouped.items():
        if saved_title := titles.get(context_id, "").strip():
            entry["title"] = saved_title
    return sorted(grouped.values(), key=lambda e: e["updated_at"], reverse=True)


def list_tasks_for_context(
    tasks: Iterable[dict[str, Any]], context_id: str
) -> list[dict[str, Any]]:
    """Filters serialized tasks for one conversation, ordered chronologically.

    Args:
        tasks: Deserialized proto-JSON task dictionaries.
        context_id: Conversation context identifier.

    Returns:
        Matching task dictionaries sorted by status timestamp ascending.
    """
    matching = [
        task for task in tasks if (task.get("contextId") or "") == context_id
    ]
    return sorted(matching, key=_extract_task_updated_at)


class LocalTaskStore(InMemoryTaskStore):
    """In-memory `TaskStore` exposing `/lha/contexts` listing for local runs.

    Delegates A2A RPC operations to `InMemoryTaskStore` while mirroring tasks
    as proto-JSON strings so `/lha/contexts` shares the exact wire encoding
    `GcsTaskStore` writes to Cloud Storage.
    """

    def __init__(self) -> None:
        super().__init__()
        self._tasks: dict[str, str] = {}
        self._titles: dict[str, str] = {}

    async def save(
        self, task: a2a_pb2.Task, context: ServerCallContext
    ) -> None:
        if context is not None:
            await super().save(task, context)
        self._tasks[task.id] = json_format.MessageToJson(task)

    async def get(
        self, task_id: str, context: ServerCallContext
    ) -> a2a_pb2.Task | None:
        if context is not None:
            found = await super().get(task_id, context)
            if found is not None:
                return found
        raw = self._tasks.get(task_id)
        if raw is None:
            return None
        return json_format.Parse(raw, a2a_pb2.Task())

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        if context is not None:
            await super().delete(task_id, context)
        self._tasks.pop(task_id, None)

    def _load_task_dicts(self) -> Sequence[dict[str, Any]]:
        return [json.loads(raw) for raw in self._tasks.values()]

    def list_contexts(self) -> Sequence[dict[str, Any]]:
        """Lists conversation contexts ordered newest first.

        Returns:
            Sequence of `/lha/contexts` summary dictionaries.
        """
        return build_context_summaries(self._load_task_dicts(), self._titles)

    def list_context_tasks(self, context_id: str) -> Sequence[dict[str, Any]]:
        """Lists raw tasks for one conversation ordered chronologically.

        Args:
            context_id: Conversation context identifier.

        Returns:
            Sequence of serialized task dictionaries.
        """
        return list_tasks_for_context(self._load_task_dicts(), context_id)

    def delete_context(self, context_id: str) -> int:
        """Deletes every task and saved title for ``context_id``.

        Args:
            context_id: Conversation context identifier.

        Returns:
            Count of deleted tasks.
        """
        if not context_id:
            return 0
        to_remove = [
            task_id
            for task_id, raw in self._tasks.items()
            if json.loads(raw).get("contextId") == context_id
        ]
        for task_id in to_remove:
            self._tasks.pop(task_id, None)
            impl = getattr(self, "_impl", None)
            tasks_map = getattr(impl, "tasks", None)
            if isinstance(tasks_map, dict):
                for owner_tasks in tasks_map.values():
                    if isinstance(owner_tasks, dict):
                        owner_tasks.pop(task_id, None)
        self._titles.pop(context_id, None)
        return len(to_remove)

    def save_context_title(self, context_id: str, title: str) -> None:
        """Stores a user-chosen title for ``context_id``.

        Args:
            context_id: Conversation context identifier.
            title: Validated non-empty title string.

        Raises:
            ValueError: If ``context_id`` is empty or whitespace.
        """
        if not context_id or not context_id.strip():
            raise ValueError("context_id must not be empty")
        self._titles[context_id] = title
