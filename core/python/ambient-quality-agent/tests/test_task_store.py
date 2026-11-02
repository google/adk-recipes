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

"""A transcript that dies with the process is not a record.

Agent Runtime recycles instances, so the SDK's `InMemoryTaskStore` loses
conversations minutes after they happen. These pin what the dashboard needs
from a store that outlives one: a task survives, a conversation can be listed,
and a read failure is never reported as "no such task".

The GCS client is faked through the same `gcs.client` seam `tests/test_gcs.py`
uses, so no network and no credentials. `conftest.FakeGcsClient` models the
create-if-absent precondition the idempotency marker needs and stores no object
bodies, which is the one thing these tests are entirely about -- hence a second,
content-carrying fake here.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from a2a.types import a2a_pb2
from ambient_quality_agent.app_utils.task_store import GcsTaskStore
from ambient_quality_agent.tools import gcs
from google.api_core import exceptions as api_exceptions
from google.protobuf import json_format

# Marks an object whose download fails, standing in for a 503 or a permission
# error -- anything that is not "the object is absent".
UNREADABLE = object()


class _Blob:
    def __init__(self, bucket: _Bucket, name: str) -> None:
        self._bucket = bucket
        self.name = name

    def upload_from_string(self, payload: str, content_type: str = "") -> None:
        self._bucket.objects[self.name] = payload

    def download_as_bytes(self) -> bytes:
        if self.name not in self._bucket.objects:
            raise api_exceptions.NotFound(self.name)
        if self._bucket.objects[self.name] is UNREADABLE:
            raise RuntimeError("503 backend error")
        return str(self._bucket.objects[self.name]).encode("utf-8")

    def delete(self) -> None:
        if self.name not in self._bucket.objects:
            raise api_exceptions.NotFound(self.name)
        del self._bucket.objects[self.name]


class _Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, Any] = {}

    def blob(self, name: str) -> _Blob:
        return _Blob(self, name)


class _Client:
    def __init__(self, bucket: _Bucket) -> None:
        self._bucket = bucket

    def bucket(self, _name: str) -> _Bucket:
        return self._bucket

    def list_blobs(self, _bucket: str, prefix: str = "") -> list[_Blob]:
        return [
            _Blob(self._bucket, name)
            for name in sorted(self._bucket.objects)
            if name.startswith(prefix)
        ]


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> _Bucket:
    """One in-memory bucket behind `gcs.client`, as the real store sees it."""
    fake = _Bucket()
    monkeypatch.setattr(gcs, "create_storage_client", lambda: _Client(fake))
    return fake


def _task(task_id: str, context_id: str) -> a2a_pb2.Task:
    task = a2a_pb2.Task()
    task.id = task_id
    task.context_id = context_id
    return task


def test_a_saved_task_is_readable_by_a_new_store(bucket: _Bucket) -> None:
    """The point of the whole class: a later process must see it."""
    asyncio.run(GcsTaskStore("jobs").save(_task("t1", "ctx-a"), None))

    got = asyncio.run(GcsTaskStore("jobs").get("t1", None))

    assert got is not None
    assert got.id == "t1"
    assert got.context_id == "ctx-a"


def test_an_absent_task_is_none(bucket: _Bucket) -> None:
    assert asyncio.run(GcsTaskStore("jobs").get("nope", None)) is None


def test_a_read_failure_raises_rather_than_reporting_no_such_task(
    bucket: _Bucket,
) -> None:
    """Absence must never be inferred from a failure. Returning None here would
    draw a transcript that could not be loaded as an empty conversation."""
    store = GcsTaskStore("jobs")
    asyncio.run(store.save(_task("t1", "ctx-a"), None))
    bucket.objects["tasks/t1.json"] = UNREADABLE

    with pytest.raises(RuntimeError):
        asyncio.run(store.get("t1", None))


def test_deleting_an_absent_task_is_not_an_error(bucket: _Bucket) -> None:
    asyncio.run(GcsTaskStore("jobs").delete("never-existed", None))


def test_contexts_groups_tasks_by_conversation(bucket: _Bucket) -> None:
    store = GcsTaskStore("jobs")
    for task_id, ctx in [("t1", "ctx-a"), ("t2", "ctx-a"), ("t3", "ctx-b")]:
        asyncio.run(store.save(_task(task_id, ctx), None))

    contexts = asyncio.run(store.list_contexts())

    assert {c["context_id"] for c in contexts} == {"ctx-a", "ctx-b"}
    by_id = {c["context_id"]: sorted(c["task_ids"]) for c in contexts}
    assert by_id["ctx-a"] == ["t1", "t2"]
    assert by_id["ctx-b"] == ["t3"]


def test_contexts_are_newest_first(bucket: _Bucket) -> None:
    """The sidebar shows this list in order, so the order is the contract."""
    store = GcsTaskStore("jobs")
    for task_id, ctx, seconds in [("t1", "old", 100), ("t2", "new", 900)]:
        task = _task(task_id, ctx)
        task.status.timestamp.seconds = seconds
        asyncio.run(store.save(task, None))

    contexts = asyncio.run(store.list_contexts())

    assert [c["context_id"] for c in contexts] == ["new", "old"]


def test_list_filters_to_one_conversation(bucket: _Bucket) -> None:
    store = GcsTaskStore("jobs")
    asyncio.run(store.save(_task("t1", "ctx-a"), None))
    asyncio.run(store.save(_task("t2", "ctx-b"), None))

    params = a2a_pb2.ListTasksRequest()
    params.context_id = "ctx-b"
    result = asyncio.run(store.list(params, None))

    assert [t.id for t in result.tasks] == ["t2"]


def test_a_task_id_with_a_slash_cannot_escape_the_prefix(
    bucket: _Bucket,
) -> None:
    """The jobs bucket also holds investigation artifacts, so a task id is not
    allowed to address anything outside `tasks/`."""
    asyncio.run(
        GcsTaskStore("jobs").save(
            _task("../investigations/run/artifact", "ctx"), None
        )
    )

    # `tasks/../investigations/x.json` also starts with `tasks/`, so the check
    # has to be that nothing escapes that one prefix segment.
    for name in bucket.objects:
        assert name.startswith("tasks/")
        assert "/" not in name[len("tasks/") :], name


def test_one_unreadable_blob_does_not_empty_the_whole_listing(
    bucket: _Bucket,
) -> None:
    """One corrupt task must not make every conversation unlistable."""
    store = GcsTaskStore("jobs")
    asyncio.run(store.save(_task("t1", "ctx-a"), None))
    asyncio.run(store.save(_task("t2", "ctx-b"), None))
    bucket.objects["tasks/t1.json"] = "not json"

    contexts = asyncio.run(store.list_contexts())

    assert [c["context_id"] for c in contexts] == ["ctx-b"]


def test_a_saved_task_round_trips_its_message_history(bucket: _Bucket) -> None:
    """The transcript is the payload, so the encoding has to keep it."""
    store = GcsTaskStore("jobs")
    task = _task("t1", "ctx-a")
    message = task.history.add()
    message.message_id = "m1"
    message.parts.add().text = "why did create_ticket fail?"

    asyncio.run(store.save(task, None))
    got = asyncio.run(store.get("t1", None))

    assert got is not None
    assert got.history[0].parts[0].text == "why did create_ticket fail?"


def test_the_stored_object_is_json_a_human_can_read_in_the_bucket(
    bucket: _Bucket,
) -> None:
    asyncio.run(GcsTaskStore("jobs").save(_task("t1", "ctx-a"), None))

    payload = bucket.objects["tasks/t1.json"]

    assert json_format.Parse(payload, a2a_pb2.Task()).id == "t1"
