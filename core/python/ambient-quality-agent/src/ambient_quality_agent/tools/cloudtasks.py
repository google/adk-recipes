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

"""Cloud Tasks scheduling primitives shared across AQA.

Schedules delayed, authenticated HTTP POST tasks using Cloud Tasks with
name-based deduplication to collapse duplicate task dispatches.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from google.cloud import tasks_v2


def _create_tasks_client() -> tasks_v2.CloudTasksClient:
    """Create a Cloud Tasks client.

    Uses a lazy import to allow tests to inject a fake client without requiring
    network access or credentials.

    Returns:
        CloudTasksClient instance.
    """
    from google.cloud import tasks_v2

    return tasks_v2.CloudTasksClient()


def enqueue_http_task(
    queue_path: str,
    url: str,
    body: bytes,
    schedule_time: dt.datetime,
    oauth_sa: str,
    *,
    task_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """Enqueue a delayed, OAuth-authenticated HTTP POST task.

    Execution sequence:
    1. Converts `schedule_time` into a protobuf Timestamp.
    2. Constructs the task payload dictionary with OAuth credentials and optional task name.
    3. Calls the Cloud Tasks API to create the task.
    4. Handles `AlreadyExists` as an idempotent success and returns the existing task name.
    5. Returns the created task resource name on success.

    Args:
        queue_path: Fully qualified Cloud Tasks queue resource path.
        url: Target webhook URL for the HTTP POST request.
        body: Payload bytes to send in the request body.
        schedule_time: Scheduled execution time.
        oauth_sa: Service account email for generating the OAuth token.
        task_id: Optional unique identifier for task deduplication.
        headers: Optional HTTP headers to include with the request.

    Returns:
        Fully qualified resource name of the created or existing task.
    """
    from google.api_core import exceptions as api_exceptions
    from google.protobuf.timestamp_pb2 import Timestamp

    timestamp = Timestamp()
    timestamp.FromDatetime(schedule_time)

    task: dict[str, Any] = {
        "http_request": {
            "http_method": "POST",
            "url": url,
            "headers": headers or {},
            "body": body,
            "oauth_token": {"service_account_email": oauth_sa},
        },
        "schedule_time": timestamp,
    }
    if task_id is not None:
        task["name"] = f"{queue_path}/tasks/{task_id}"

    try:
        created = _create_tasks_client().create_task(
            request={"parent": queue_path, "task": task}
        )
    except api_exceptions.AlreadyExists:
        return task["name"]
    return created.name
