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

"""Tests for the Cloud Tasks helper (`enqueue_http_task`).

The Cloud Tasks client is faked (see `FakeTasksClient`) so the task the helper
builds -- and its `AlreadyExists` dedup path -- are exercised without network or
the `google-cloud-tasks` client. The task is a plain dict, so tests assert on it
directly.
"""

from __future__ import annotations

import datetime as dt
import types
from typing import Any

import pytest
from ambient_quality_agent.tools import cloudtasks
from google.api_core import exceptions as api_exceptions

_QUEUE = "projects/p/locations/l/queues/q"
_URL = "https://pubsub.googleapis.com/v1/projects/p/topics/t:publish"
_WHEN = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)


class FakeTasksClient:
    """In-memory `CloudTasksClient` stand-in recording `create_task` calls.

    `create_task` records each `request` dict (the GAPIC dict form the helper
    uses) and returns a Task-like object whose `.name` echoes the task's name
    (or a generated one when unnamed), mirroring the real client. Constructed
    with `already_exists=True` to raise `AlreadyExists`, exercising the helper's
    name-based-dedup path.
    """

    def __init__(self, *, already_exists: bool = False) -> None:
        self.requests: list[dict[str, Any]] = []
        self._already_exists = already_exists

    def create_task(self, *, request: dict[str, Any]) -> Any:
        self.requests.append(request)
        if self._already_exists:
            raise api_exceptions.AlreadyExists("task already exists")
        task = request["task"]
        parent = request["parent"]
        return types.SimpleNamespace(
            name=task.get("name", f"{parent}/tasks/generated")
        )


def test_enqueue_http_task_builds_task(monkeypatch: pytest.MonkeyPatch) -> None:
    # The helper builds a POST task carrying the given url/body/headers, an OAuth
    # token for `oauth_sa`, the schedule_time, and a `name` for name-based dedup.
    fake = FakeTasksClient()
    monkeypatch.setattr(cloudtasks, "_create_tasks_client", lambda: fake)

    name = cloudtasks.enqueue_http_task(
        queue_path=_QUEUE,
        url=_URL,
        body=b'{"messages":[]}',
        schedule_time=_WHEN,
        oauth_sa="caller@p.iam.gserviceaccount.com",
        task_id="abc123",
        headers={"Content-Type": "application/json"},
    )

    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request["parent"] == _QUEUE
    task = request["task"]
    http = task["http_request"]
    assert http["http_method"] == "POST"
    assert http["url"] == _URL
    assert http["body"] == b'{"messages":[]}'
    assert http["headers"] == {"Content-Type": "application/json"}
    assert (
        http["oauth_token"]["service_account_email"]
        == "caller@p.iam.gserviceaccount.com"
    )
    # schedule_time is a protobuf Timestamp; it round-trips to the aware datetime.
    assert task["schedule_time"].ToDatetime(tzinfo=dt.UTC) == _WHEN
    assert task["name"].endswith("/tasks/abc123")
    assert name == task["name"]


def test_enqueue_http_task_defaults_no_task_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without a task_id there is no name (Cloud Tasks generates one); headers
    # default to an empty dict. The returned name is the client's generated one.
    fake = FakeTasksClient()
    monkeypatch.setattr(cloudtasks, "_create_tasks_client", lambda: fake)

    name = cloudtasks.enqueue_http_task(
        queue_path=_QUEUE,
        url=_URL,
        body=b"{}",
        schedule_time=_WHEN,
        oauth_sa="caller@p.iam.gserviceaccount.com",
    )

    task = fake.requests[0]["task"]
    assert "name" not in task
    assert task["http_request"]["headers"] == {}
    assert name == f"{_QUEUE}/tasks/generated"


def test_enqueue_http_task_already_exists_returns_existing_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A duplicate task_id (name-based dedup) raises AlreadyExists; the helper
    # treats it as success -- the delayed fire is already scheduled -- returning
    # the existing name rather than raising.
    fake = FakeTasksClient(already_exists=True)
    monkeypatch.setattr(cloudtasks, "_create_tasks_client", lambda: fake)

    name = cloudtasks.enqueue_http_task(
        queue_path=_QUEUE,
        url=_URL,
        body=b"{}",
        schedule_time=_WHEN,
        oauth_sa="caller@p.iam.gserviceaccount.com",
        task_id="dup",
    )

    assert name == f"{_QUEUE}/tasks/dup"


def test_enqueue_http_task_propagates_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Errors other than AlreadyExists (auth, network, quota) must propagate -- a
    # failed enqueue must not be silently read as "scheduled".
    class _BoomClient:
        def create_task(self, *, request: dict[str, Any]) -> Any:
            raise api_exceptions.ServiceUnavailable("503")

    monkeypatch.setattr(cloudtasks, "_create_tasks_client", _BoomClient)
    with pytest.raises(api_exceptions.ServiceUnavailable):
        cloudtasks.enqueue_http_task(
            queue_path=_QUEUE,
            url=_URL,
            body=b"{}",
            schedule_time=_WHEN,
            oauth_sa="caller@p.iam.gserviceaccount.com",
            task_id="x",
        )
