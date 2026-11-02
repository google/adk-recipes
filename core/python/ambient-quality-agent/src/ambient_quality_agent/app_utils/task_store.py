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
import logging
from collections.abc import Sequence
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.types import a2a_pb2
from ambient_quality_agent.tools import gcs
from google.api_core import exceptions as api_exceptions
from google.protobuf import json_format

logger = logging.getLogger(__name__)

TASKS_PREFIX = "tasks/"


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
