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

"""Tests for the command HTTP routes, argument parsing, and error responses."""

from __future__ import annotations

from collections.abc import Iterator
from unittest import mock

import pytest
from ambient_quality_agent.core import command_routes as routes
from ambient_quality_agent.core.investigation import model as runs
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .conftest import create_run


@pytest.fixture
def client() -> Iterator[TestClient]:
    """The router alone, with no ADK app around it."""
    app = FastAPI()
    app.include_router(routes.router)
    yield TestClient(app)


@pytest.fixture
def mock_schedule(monkeypatch: pytest.MonkeyPatch) -> mock.AsyncMock:
    """Patch the scheduling tool, so no durable job is submitted."""
    fake = mock.AsyncMock(return_value={"run_id": "r9", "status": "pending"})
    monkeypatch.setattr(routes.job_scheduling, "schedule_investigation", fake)
    return fake


def test_list_investigations_returns_the_recent_runs(
    client: TestClient,
) -> None:
    create_run(
        {}, runs.InvestigationRecord(run_id="r1", status=runs.RunStatus.PENDING)
    )

    response = client.post("/investigations/list")

    assert response.status_code == 200
    assert [run["run_id"] for run in response.json()["runs"]] == ["r1"]


def test_get_investigation_returns_the_named_run(client: TestClient) -> None:
    create_run(
        {}, runs.InvestigationRecord(run_id="r1", status=runs.RunStatus.DONE)
    )

    response = client.post("/investigations/get", json={"run_id": "r1"})

    assert response.status_code == 200
    assert response.json()["run_id"] == "r1"


def test_get_investigation_surfaces_an_unknown_run_as_a_tool_error(
    client: TestClient,
) -> None:
    # An id nothing has created is a result the caller reads, not a bad request.
    response = client.post("/investigations/get", json={"run_id": "nope"})

    assert response.status_code == 200
    assert "error" in response.json()


def test_get_investigation_without_a_run_id_is_rejected(
    client: TestClient,
) -> None:
    response = client.post("/investigations/get", json={})

    assert response.status_code == 400
    assert "run_id" in response.json()["detail"]


def test_schedule_investigation_returns_the_new_run(
    client: TestClient, mock_schedule: mock.AsyncMock
) -> None:
    response = client.post("/investigations/schedule")

    assert response.status_code == 200
    assert response.json()["run_id"] == "r9"
    mock_schedule.assert_awaited_once()


def test_list_insights_forwards_its_filters(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def fake_list(
        context,
        *,
        status="",
        run_id="",
        has_root_cause="",
        page_token="",
        window_start="",
        window_end="",
        order_by="",
        page_size=0,
        day="",
    ):
        captured.update(
            status=status,
            run_id=run_id,
            has_root_cause=has_root_cause,
            page_token=page_token,
            window_start=window_start,
            window_end=window_end,
            order_by=order_by,
            page_size=page_size,
            day=day,
        )
        return {"insights": [], "total": 0, "next_page_token": None}

    monkeypatch.setattr(routes.insight_tools, "list_insights", fake_list)

    response = client.post(
        "/insights/list",
        json={
            "status": "NEW",
            "run_id": "r1",
            "order_by": "impact",
            "page_size": 10,
            "day": "2026-09-20",
        },
    )

    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert captured == {
        "status": "NEW",
        "run_id": "r1",
        "has_root_cause": "",
        "page_token": "",
        "window_start": "",
        "window_end": "",
        "order_by": "impact",
        "page_size": 10,
        "day": "2026-09-20",
    }


def test_list_insights_without_a_body_uses_the_tool_defaults(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def fake_list(
        context,
        *,
        status="",
        run_id="",
        has_root_cause="",
        page_token="",
        window_start="",
        window_end="",
        order_by="",
        page_size=0,
        day="",
    ):
        captured.update(
            status=status,
            run_id=run_id,
            has_root_cause=has_root_cause,
            page_token=page_token,
            window_start=window_start,
            window_end=window_end,
            order_by=order_by,
            page_size=page_size,
            day=day,
        )
        return {"insights": []}

    monkeypatch.setattr(routes.insight_tools, "list_insights", fake_list)

    response = client.post("/insights/list")

    assert response.status_code == 200
    assert captured == {
        "status": "",
        "run_id": "",
        "has_root_cause": "",
        "page_token": "",
        "window_start": "",
        "window_end": "",
        "order_by": "",
        "page_size": 0,
        "day": "",
    }


@pytest.mark.parametrize(
    ("sent", "expected"),
    [("10", 10), ("", 0), ("ten", 0), (None, 0)],
)
def test_list_insights_reads_a_page_size_that_arrived_as_a_string(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, sent, expected: int
) -> None:
    """A count off a query param is a string by the time it reaches here.

    Unreadable means "no preference" rather than a 400: the tool's own default
    answers the request, and refusing it would blank a list over a typo in a
    URL.
    """
    captured: dict = {}

    async def fake_list(context, **kwargs):
        captured.update(kwargs)
        return {"insights": []}

    monkeypatch.setattr(routes.insight_tools, "list_insights", fake_list)

    response = client.post("/insights/list", json={"page_size": sent})

    assert response.status_code == 200
    assert captured["page_size"] == expected


def test_get_insight_forwards_its_arguments(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def fake_get(
        context,
        *,
        insight_id="",
        run_id="",
        include_traces=True,
        include_trajectories=None,
        include_edits=True,
        page_token="",
    ):
        captured.update(
            insight_id=insight_id,
            run_id=run_id,
            include_traces=include_traces,
            include_trajectories=include_trajectories,
            include_edits=include_edits,
        )
        return {"insight": {"insight_id": insight_id}, "occurrences": []}

    monkeypatch.setattr(routes.insight_tools, "get_insight", fake_get)

    response = client.post(
        "/insights/get",
        json={
            "insight_id": "ins-1",
            "run_id": "run-9",
            "include_traces": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["insight"]["insight_id"] == "ins-1"
    assert captured == {
        "insight_id": "ins-1",
        "run_id": "run-9",
        "include_traces": False,
        # Unset stays unset, so the tool's own default -- follow
        # `include_traces` -- decides rather than the route.
        "include_trajectories": None,
        "include_edits": True,
    }


def test_get_insight_without_an_insight_id_is_rejected(
    client: TestClient,
) -> None:
    response = client.post("/insights/get", json={"run_id": "run-9"})

    assert response.status_code == 400
    assert "insight_id" in response.json()["detail"]


def test_goal_versions_are_served_by_the_document_tool(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"versions": [], "available": True}
    fake = mock.AsyncMock(return_value=payload)
    monkeypatch.setattr(routes.document_tools, "list_goal_versions", fake)

    response = client.post("/documents/goal/versions")

    assert response.status_code == 200
    assert response.json() == payload
    fake.assert_awaited_once()


def test_show_config_returns_the_effective_configuration(
    client: TestClient,
) -> None:
    response = client.post("/config")

    assert response.status_code == 200
    assert response.json()["config"]["observed_agent_name"]


def test_malformed_body_is_rejected(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        response = client.post(
            "/insights/list",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["detail"]
    assert any("not JSON" in record.message for record in caplog.records)


def test_body_that_is_not_an_object_is_rejected(client: TestClient) -> None:
    response = client.post("/insights/list", json=["not-an-object"])

    assert response.status_code == 400


def test_trajectory_outcomes_forwards_the_window(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def fake_outcomes(context, *, window_start="", window_end=""):
        captured.update(window_start=window_start, window_end=window_end)
        return {"days": []}

    monkeypatch.setattr(
        routes.trajectory_tools, "count_trajectory_outcomes", fake_outcomes
    )

    response = client.post(
        "/trajectories/outcomes",
        json={
            "window_start": "2026-05-25T00:00:00Z",
            "window_end": "2026-06-01T00:00:00Z",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"days": []}
    assert captured == {
        "window_start": "2026-05-25T00:00:00Z",
        "window_end": "2026-06-01T00:00:00Z",
    }


def test_list_trajectories_forwards_every_filter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def fake_list(
        context,
        *,
        run_id="",
        outcome="",
        window_start="",
        window_end="",
        page_token="",
    ):
        captured.update(
            run_id=run_id,
            outcome=outcome,
            window_start=window_start,
            window_end=window_end,
            page_token=page_token,
        )
        return {"trajectories": [], "total": 0, "next_page_token": None}

    monkeypatch.setattr(routes.trajectory_tools, "list_trajectories", fake_list)

    response = client.post(
        "/trajectories/list", json={"outcome": "not_ingested", "run_id": "r1"}
    )

    assert response.status_code == 200
    assert captured == {
        "run_id": "r1",
        "outcome": "not_ingested",
        "window_start": "",
        "window_end": "",
        "page_token": "",
    }


def test_an_unusable_outcome_is_the_tools_answer_and_not_a_bad_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same split the insight routes make: a value the caller can read back
    and correct comes through as a 200 with an error, not a 400."""

    async def fake_list(context, **kwargs):
        return {"error": "invalid outcome 'passed'"}

    monkeypatch.setattr(routes.trajectory_tools, "list_trajectories", fake_list)

    response = client.post("/trajectories/list", json={"outcome": "passed"})

    assert response.status_code == 200
    assert "invalid outcome" in response.json()["error"]


def test_the_served_app_mounts_the_command_routes(
    mock_schedule: mock.AsyncMock,
) -> None:
    # `include_router` hides the paths behind an opaque route object, so the
    # only way to see the mount is to drive the app that the container serves.
    from ambient_quality_agent import fast_api_app

    served = TestClient(fast_api_app.app)

    assert served.post("/investigations/list").status_code == 200
    assert served.post("/investigations/schedule").status_code == 200
    assert served.post("/config").status_code == 200


def test_the_route_maps_a_json_boolean_to_the_tool_sentinel(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies booleans in JSON bodies are mapped to string sentinels."""
    captured: dict = {}

    async def fake_list(context, **kwargs):
        captured.update(kwargs)
        return {"insights": []}

    monkeypatch.setattr(routes.insight_tools, "list_insights", fake_list)

    assert (
        client.post("/insights/list", json={"has_root_cause": True}).status_code
        == 200
    )
    assert captured["has_root_cause"] == "true"

    assert (
        client.post(
            "/insights/list", json={"has_root_cause": False}
        ).status_code
        == 200
    )
    assert captured["has_root_cause"] == "false"


def test_a_null_root_cause_filter_does_not_become_the_string_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies None values for has_root_cause map to empty strings instead of 'None'."""
    captured: dict = {}

    async def fake_list(context, **kwargs):
        captured.update(kwargs)
        return {"insights": []}

    monkeypatch.setattr(routes.insight_tools, "list_insights", fake_list)

    response = client.post("/insights/list", json={"has_root_cause": None})

    assert response.status_code == 200
    assert captured["has_root_cause"] == ""


def test_the_route_forwards_a_request_to_drop_the_edit_bodies(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def fake_get(context, **kwargs):
        captured.update(kwargs)
        return {"insight": {}, "occurrences": []}

    monkeypatch.setattr(routes.insight_tools, "get_insight", fake_get)

    response = client.post(
        "/insights/get", json={"insight_id": "ins-1", "include_edits": False}
    )

    assert response.status_code == 200
    assert captured["include_edits"] is False


def test_dismiss_insight_forwards_the_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def fake_dismiss(context, *, insight_id=""):
        captured.update(insight_id=insight_id)
        return {"dismissed": True}

    monkeypatch.setattr(routes.insight_tools, "dismiss_insight", fake_dismiss)

    response = client.post("/insights/dismiss", json={"insight_id": "ins-1"})

    assert response.status_code == 200
    assert response.json() == {"dismissed": True}
    assert captured == {"insight_id": "ins-1"}


def test_dismiss_insight_without_an_insight_id_is_rejected(
    client: TestClient,
) -> None:
    response = client.post("/insights/dismiss")

    assert response.status_code == 400
    assert "insight_id" in response.json()["detail"]


def test_merge_insight_forwards_both_ids(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def fake_merge(context, *, insight_id="", target_insight_id=""):
        captured.update(
            insight_id=insight_id, target_insight_id=target_insight_id
        )
        return {"merged": True}

    monkeypatch.setattr(routes.insight_tools, "merge_insight", fake_merge)

    response = client.post(
        "/insights/merge",
        json={"insight_id": "ins-1", "target_insight_id": "ins-2"},
    )

    assert response.status_code == 200
    assert response.json() == {"merged": True}
    assert captured == {"insight_id": "ins-1", "target_insight_id": "ins-2"}


def test_merge_insight_without_a_target_is_rejected(client: TestClient) -> None:
    response = client.post("/insights/merge", json={"insight_id": "ins-1"})

    assert response.status_code == 400
    assert "target_insight_id" in response.json()["detail"]


# --- agents/*: how an agent is observed --------------------------------------- #


@pytest.fixture
def captured_attach(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    async def fake_attach(*, agent_name, fields, dry_run, check_access):
        captured.update(
            agent_name=agent_name,
            fields=fields,
            dry_run=dry_run,
            check_access=check_access,
        )
        return {"agent_name": agent_name or "watched-agent", "changes": []}

    monkeypatch.setattr(
        routes.agent_config_commands, "attach_agent", fake_attach
    )
    return captured


def test_attach_forwards_the_name_the_fields_and_the_dry_run(
    client: TestClient, captured_attach: dict
) -> None:
    response = client.post(
        "/agents/attach",
        json={
            "agent_name": "my-agent",
            "fields": {"data_lookback_window": 30},
            "dry_run": True,
            "check_access": True,
        },
    )

    assert response.status_code == 200
    assert captured_attach == {
        "agent_name": "my-agent",
        "fields": {"data_lookback_window": 30},
        "dry_run": True,
        "check_access": True,
    }


def test_attach_with_an_empty_body_means_the_watched_agent_as_it_is(
    client: TestClient, captured_attach: dict
) -> None:
    """No name is the agent the environment names; no fields change nothing."""
    response = client.post("/agents/attach")

    assert response.status_code == 200
    assert captured_attach == {
        "agent_name": "",
        "fields": {},
        "dry_run": False,
        "check_access": False,
    }


def test_attach_refuses_fields_that_are_not_an_object(
    client: TestClient, captured_attach: dict
) -> None:
    response = client.post(
        "/agents/attach", json={"fields": ["data_lookback_window"]}
    )

    assert response.status_code == 400
    assert "fields" in response.json()["detail"]
    assert captured_attach == {}


def test_attach_refuses_a_dry_run_that_is_not_a_boolean(
    client: TestClient, captured_attach: dict
) -> None:
    """Read loosely, the string "false" is truthy -- a plan the caller did not
    ask for, or a write they did not mean."""
    response = client.post("/agents/attach", json={"dry_run": "false"})

    assert response.status_code == 400
    assert "dry_run" in response.json()["detail"]
    assert captured_attach == {}


def test_attach_refuses_an_access_check_that_is_not_a_boolean(
    client: TestClient, captured_attach: dict
) -> None:
    response = client.post(
        "/agents/attach", json={"dry_run": True, "check_access": "true"}
    )

    assert response.status_code == 400
    assert "check_access" in response.json()["detail"]
    assert captured_attach == {}


def test_attach_refuses_an_access_check_on_a_write(
    client: TestClient, captured_attach: dict
) -> None:
    """The check belongs to the plan; a write that asked for it would store
    without the caller ever seeing the answer."""
    response = client.post("/agents/attach", json={"check_access": True})

    assert response.status_code == 400
    assert "dry run" in response.json()["detail"]
    assert captured_attach == {}


def test_detach_without_a_name_is_rejected(client: TestClient) -> None:
    """Deleting defaults to nothing: there is no 'the agent I meant'."""
    response = client.post("/agents/detach", json={})

    assert response.status_code == 400
    assert "agent_name" in response.json()["detail"]


def test_list_agents_returns_the_tool_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list():
        return {"agents": [], "default_agent": None}

    monkeypatch.setattr(routes.agent_config_commands, "list_agents", fake_list)

    response = client.post("/agents/list")

    assert response.status_code == 200
    assert response.json() == {"agents": [], "default_agent": None}


def test_applied_forwards_the_name_and_the_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def fake_record(*, agent_name, applied, granted, revoked):
        captured.update(agent_name=agent_name, applied=applied)
        return {"agent_name": agent_name, "unapplied_fields": []}

    monkeypatch.setattr(
        routes.agent_config_commands, "record_applied", fake_record
    )
    applied = {
        "investigation_schedule": "0 6 * * *",
        "scheduled_trigger_enabled": True,
        "observed_engine_id": "42",
    }

    response = client.post(
        "/agents/applied", json={"agent_name": "a", "applied": applied}
    )

    assert response.status_code == 200
    assert captured == {"agent_name": "a", "applied": applied}


def test_applied_forwards_grants_made_and_revoked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def fake_record(**kwargs):
        captured.update(kwargs)
        return {"agent_name": kwargs["agent_name"], "grants": []}

    monkeypatch.setattr(
        routes.agent_config_commands, "record_applied", fake_record
    )
    grant = {"resource": "gs://b", "role": "r", "member": "m"}

    response = client.post(
        "/agents/applied",
        json={"agent_name": "a", "granted": [grant], "revoked": []},
    )

    assert response.status_code == 200
    assert captured == {
        "agent_name": "a",
        "applied": None,
        "granted": [grant],
        "revoked": [],
    }


@pytest.mark.parametrize(
    "body",
    [
        {"applied": {}},
        {"agent_name": "a", "applied": ["investigation_schedule"]},
        {"agent_name": "a", "granted": {"resource": "gs://b"}},
        {"agent_name": "a", "revoked": "gs://b"},
    ],
)
def test_applied_without_a_name_or_with_a_mistyped_section_is_rejected(
    client: TestClient, body: dict
) -> None:
    response = client.post("/agents/applied", json=body)

    assert response.status_code == 400
