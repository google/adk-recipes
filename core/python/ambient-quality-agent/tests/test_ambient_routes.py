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

"""Tests for ambient HTTP routes, payload parsing, and error responses."""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Iterator
from unittest import mock

import pytest
from ambient_quality_agent.core.ambient import handler, routes
from ambient_quality_agent.tools import cloudtasks
from ambient_quality_agent.tools.observed_agent_config import effective_config
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .conftest import make_config

_RESOURCE = "projects/p/locations/l/reasoningEngines/1"


@pytest.fixture
def client() -> Iterator[TestClient]:
    """The router alone, with no ADK app around it."""
    app = FastAPI()
    app.include_router(routes.router)
    yield TestClient(app)


@pytest.fixture
def mock_schedule(monkeypatch: pytest.MonkeyPatch) -> mock.AsyncMock:
    """Patch the `_launch_investigation` seam the handler calls."""
    fake = mock.AsyncMock(return_value={"run_id": "r1", "status": "pending"})
    monkeypatch.setattr(handler, "_launch_investigation", fake)
    return fake


@pytest.fixture
def mock_enqueue(monkeypatch: pytest.MonkeyPatch) -> mock.Mock:
    """Patch the Cloud Tasks seam the update path calls."""
    fake = mock.Mock()
    monkeypatch.setattr(cloudtasks, "enqueue_http_task", fake)
    return fake


@pytest.fixture(autouse=True)
def pinned_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the config both the route and the handler load."""
    cfg = make_config(
        observed_agent_name="agent-x",
        project_id="p",
        location="us-east4",
        aqa_engine_id="99",
        agent_revision_trigger_delay_seconds=120,
        delay_task_queue="projects/p/locations/l/queues/updates",
        ambient_caller_sa="caller@p.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(effective_config, "load", lambda state: cfg)


def _log_entry(resource_name: str | None, operation_id: str | None) -> dict:
    """Constructs an audit `LogEntry` dictionary shaped like sink output.

    Args:
        resource_name: The resource name of the updated Agent Runtime agent.
        operation_id: The audit operation identifier.

    Returns:
        A dictionary formatted as an audit log entry.
    """
    return {
        "protoPayload": {
            "methodName": "google.cloud.aiplatform.v1.ReasoningEngineService."
            "UpdateReasoningEngine",
            "resourceName": resource_name,
        },
        "operation": {"id": operation_id, "last": True},
    }


def test_scheduled_without_the_header_anchors_on_the_receiver_clock(
    client: TestClient, mock_schedule: mock.AsyncMock
) -> None:
    response = client.post("/ambient/trigger", json={"type": "scheduled"})

    assert response.status_code == 200
    assert "r1" in response.json()["note"]
    kwargs = mock_schedule.call_args.kwargs
    assert kwargs["anchor"] is None
    assert kwargs["idempotency_key"] is None
    assert kwargs["ambient"] is True


def test_schedule_time_header_becomes_the_anchor(
    client: TestClient, mock_schedule: mock.AsyncMock
) -> None:
    client.post(
        "/ambient/trigger",
        json={"type": "scheduled"},
        headers={routes.SCHEDULE_TIME_HEADER: "2026-01-02T03:30:00Z"},
    )

    anchor = mock_schedule.call_args.kwargs["anchor"]
    assert anchor == dt.datetime(2026, 1, 2, 3, 30, tzinfo=dt.UTC)


def test_schedule_time_offset_is_honoured(
    client: TestClient, mock_schedule: mock.AsyncMock
) -> None:
    # Cloud Scheduler sends the instant in the job's own time zone.
    client.post(
        "/ambient/trigger",
        json={"type": "scheduled"},
        headers={routes.SCHEDULE_TIME_HEADER: "2026-01-01T22:00:00-07:00"},
    )

    anchor = mock_schedule.call_args.kwargs["anchor"]
    assert anchor == dt.datetime(2026, 1, 2, 5, 0, tzinfo=dt.UTC)


def test_unparseable_schedule_time_leaves_the_anchor_unset(
    client: TestClient,
    mock_schedule: mock.AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        response = client.post(
            "/ambient/trigger",
            json={"type": "scheduled"},
            headers={routes.SCHEDULE_TIME_HEADER: "half past three"},
        )

    assert response.status_code == 200
    assert mock_schedule.call_args.kwargs["anchor"] is None
    assert any(routes.SCHEDULE_TIME_HEADER in r.message for r in caplog.records)


def test_malformed_body_is_rejected(
    client: TestClient, mock_schedule: mock.AsyncMock
) -> None:
    response = client.post(
        "/ambient/trigger",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]
    mock_schedule.assert_not_called()


def test_unknown_type_is_rejected(
    client: TestClient, mock_schedule: mock.AsyncMock
) -> None:
    response = client.post("/ambient/trigger", json={"type": "bogus"})

    assert response.status_code == 400
    mock_schedule.assert_not_called()


def test_observed_update_reads_the_raw_log_entry(
    client: TestClient, mock_schedule: mock.AsyncMock, mock_enqueue: mock.Mock
) -> None:
    # Pub/Sub push with `no_wrapper` delivers the audit LogEntry directly.
    dedup_id = "projects/p/locations/us-central1/operations/757240111"
    response = client.post(
        "/ambient/observed-update", json=_log_entry(_RESOURCE, dedup_id)
    )

    assert response.status_code == 200
    assert dedup_id in response.json()["note"]
    mock_schedule.assert_not_called()
    kwargs = mock_enqueue.call_args.kwargs
    assert (
        kwargs["task_id"]
        == "projects_p_locations_us-central1_operations_757240111"
    )
    assert json.loads(kwargs["body"].decode()) == {
        "type": "task_fire",
        "resource_name": _RESOURCE,
        "dedup_id": dedup_id,
    }


def test_observed_update_without_an_operation_id_is_rejected(
    client: TestClient, mock_enqueue: mock.Mock
) -> None:
    # Without an operation id the delayed task cannot be named, so a redelivery
    # would buy another investigation. A 400 nacks the push instead.
    response = client.post(
        "/ambient/observed-update", json=_log_entry(_RESOURCE, None)
    )

    assert response.status_code == 400
    mock_enqueue.assert_not_called()


def test_observed_update_with_an_unexpected_shape_is_rejected(
    client: TestClient, mock_enqueue: mock.Mock
) -> None:
    response = client.post(
        "/ambient/observed-update",
        json={"protoPayload": "surprise", "operation": {"id": "op-1"}},
    )

    assert response.status_code == 400
    mock_enqueue.assert_not_called()


def test_naive_schedule_time_is_read_as_utc(
    client: TestClient,
    mock_schedule: mock.AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A timestamp without an offset must be read as UTC whatever the container's zone.
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        client.post(
            "/ambient/trigger",
            json={"type": "scheduled"},
            headers={routes.SCHEDULE_TIME_HEADER: "2026-01-02T03:30:00"},
        )
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()

    anchor = mock_schedule.call_args.kwargs["anchor"]
    assert anchor == dt.datetime(2026, 1, 2, 3, 30, tzinfo=dt.UTC)


def test_body_that_is_not_an_object_is_rejected(
    client: TestClient, mock_schedule: mock.AsyncMock
) -> None:
    response = client.post("/ambient/observed-update", json=["not-an-object"])

    assert response.status_code == 400
    mock_schedule.assert_not_called()


def test_the_served_app_mounts_both_routes(
    mock_schedule: mock.AsyncMock, mock_enqueue: mock.Mock
) -> None:
    # `include_router` hides the paths behind an opaque route object, so the
    # only way to see the mount is to drive the app that the container serves.
    from ambient_quality_agent import fast_api_app

    served = TestClient(fast_api_app.app)

    assert (
        served.post("/ambient/trigger", json={"type": "scheduled"}).status_code
        == 200
    )
    assert (
        served.post(
            "/ambient/observed-update", json=_log_entry(_RESOURCE, "op-1")
        ).status_code
        == 200
    )


def test_a_body_over_the_cap_is_rejected(
    client: TestClient, mock_schedule: mock.AsyncMock
) -> None:
    # The cap reads the stream, so a sender that understates `content-length`
    # is refused.
    oversize = b'{"type":"scheduled","pad":"' + b"x" * (1 << 20) + b'"}'
    response = client.post(
        "/ambient/trigger",
        content=oversize,
        headers={"Content-Type": "application/json", "Content-Length": "10"},
    )

    assert response.status_code == 400
    mock_schedule.assert_not_called()
