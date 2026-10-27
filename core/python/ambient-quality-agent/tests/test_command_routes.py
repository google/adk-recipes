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

import json
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.core import command_routes as routes
from ambient_quality_agent.core.investigation import model as runs
from ambient_quality_agent.tools.observed_agent_config import (
    effective_config,
    store,
)
from ambient_quality_shared import protocol
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
        page=0,
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
            page=page,
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
            "page": 3,
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
        "page": 3,
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
        page=0,
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
            page=page,
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
        "page": 0,
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
    client: TestClient, jobs_store: Any
) -> None:
    _attach_default_agent(jobs_store)

    response = client.post("/config")

    assert response.status_code == 200
    config = response.json()["config"]
    assert config["observed_agent_name"] == "it_support_agent"
    assert config["agent_key"] == _DEFAULT_KEY


def test_show_config_reports_usage_analytics_on_by_default(
    client: TestClient,
) -> None:
    response = client.post("/config")

    assert response.json()["usage_analytics"] == {"enabled": True}


def test_show_config_reports_usage_analytics_off_while_the_switch_is_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ambient_quality_shared import usage_headers

    monkeypatch.setattr(usage_headers, "USAGE_ANALYTICS_ENABLED", False)

    response = client.post("/config")

    assert response.json()["usage_analytics"] == {"enabled": False}


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

    async def fake_attach(
        *, agent_name, fields, dry_run, check_access, fill_from_table, agent_key
    ):
        captured.update(
            agent_name=agent_name,
            fields=fields,
            dry_run=dry_run,
            check_access=check_access,
            fill_from_table=fill_from_table,
            agent_key=agent_key,
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
            "fill_from_table": True,
        },
    )

    assert response.status_code == 200
    assert captured_attach == {
        "agent_name": "my-agent",
        "fields": {"data_lookback_window": 30},
        "dry_run": True,
        "check_access": True,
        "fill_from_table": True,
        "agent_key": "",
    }


def test_attach_forwards_the_key_of_the_attachment_to_change(
    client: TestClient, captured_attach: dict
) -> None:
    response = client.post(
        "/agents/attach",
        json={"agent_key": "acme-prod__my_agent", "fields": {}},
    )

    assert response.status_code == 200
    assert captured_attach["agent_key"] == "acme-prod__my_agent"


def test_attach_with_an_empty_body_is_forwarded_as_empty(
    client: TestClient, captured_attach: dict
) -> None:
    """The route fills in no agent; `commands.attach_agent` refuses a request
    that names none."""
    response = client.post("/agents/attach")

    assert response.status_code == 200
    assert captured_attach == {
        "agent_name": "",
        "fields": {},
        "dry_run": False,
        "check_access": False,
        "fill_from_table": False,
        "agent_key": "",
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


def test_attach_refuses_a_fill_that_is_not_a_boolean(
    client: TestClient, captured_attach: dict
) -> None:
    response = client.post(
        "/agents/attach",
        json={"dry_run": True, "check_access": True, "fill_from_table": 1},
    )

    assert response.status_code == 400
    assert "fill_from_table" in response.json()["detail"]
    assert captured_attach == {}


def test_attach_refuses_a_fill_without_the_access_check(
    client: TestClient, captured_attach: dict
) -> None:
    """The check is what shows the filled-in values work; without it the
    request would fill in nothing."""
    response = client.post(
        "/agents/attach", json={"dry_run": True, "fill_from_table": True}
    )

    assert response.status_code == 400
    assert "access check" in response.json()["detail"]
    assert captured_attach == {}


def test_detach_without_a_name_is_rejected(client: TestClient) -> None:
    """Deleting defaults to nothing: there is no 'the agent I meant'."""
    response = client.post("/agents/detach", json={})

    assert response.status_code == 400
    assert "agent_key" in response.json()["detail"]


@pytest.mark.parametrize("route", ["/agents/detach", "/agents/applied"])
def test_an_older_cli_naming_the_agent_by_name_is_understood(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """A client sending `agent_name` selects the attachment stored under that
    name."""
    captured: dict = {}

    async def fake(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(routes.agent_config_commands, "detach_agent", fake)
    monkeypatch.setattr(routes.agent_config_commands, "record_applied", fake)

    response = client.post(
        route, json={"agent_name": "it_support_agent", "granted": []}
    )

    assert response.status_code == 200, response.text
    assert captured["agent_key"] == "it_support_agent"


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

    async def fake_record(*, agent_key, applied, granted, revoked):
        captured.update(agent_key=agent_key, applied=applied)
        return {"agent_key": agent_key, "unapplied_fields": []}

    monkeypatch.setattr(
        routes.agent_config_commands, "record_applied", fake_record
    )
    applied = {"observed_engine_id": "42", "observed_project_id": "p"}

    response = client.post(
        "/agents/applied", json={"agent_key": "p__a", "applied": applied}
    )

    assert response.status_code == 200
    assert captured == {"agent_key": "p__a", "applied": applied}


def test_applied_forwards_grants_made_and_revoked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def fake_record(**kwargs):
        captured.update(kwargs)
        return {"agent_key": kwargs["agent_key"], "grants": []}

    monkeypatch.setattr(
        routes.agent_config_commands, "record_applied", fake_record
    )
    grant = {"resource": "gs://b", "role": "r", "member": "m"}

    response = client.post(
        "/agents/applied",
        json={"agent_key": "p__a", "granted": [grant], "revoked": []},
    )

    assert response.status_code == 200
    assert captured == {
        "agent_key": "p__a",
        "applied": None,
        "granted": [grant],
        "revoked": [],
    }


@pytest.mark.parametrize(
    "body",
    [
        {"applied": {}},
        {"agent_key": "p__a", "applied": ["investigation_schedule"]},
        {"agent_key": "p__a", "granted": {"resource": "gs://b"}},
        {"agent_key": "p__a", "revoked": "gs://b"},
    ],
)
def test_applied_without_a_name_or_with_a_mistyped_section_is_rejected(
    client: TestClient, body: dict
) -> None:
    response = client.post("/agents/applied", json=body)

    assert response.status_code == 400


# --- the agent a request selects --------------------------------------------- #

_STAGING_KEY = "acme-staging__it_support_agent"
_DEFAULT_KEY = "acme-prod__it_support_agent"


def _write_attachment(jobs_store: Any, key: str, **agent: Any) -> None:
    """Writes an agent's attachment object into the jobs store.

    Args:
        jobs_store: The test's jobs store.
        key: Agent key, `<project id>__<agent name>`.
        **agent: Fields of the object's `agent` section besides the name.
    """
    jobs_store.write_text(
        store.build_object_name(key),
        json.dumps(
            {
                "schema_version": 1,
                "agent": {"observed_agent_name": "it_support_agent", **agent},
            }
        ),
    )


def _attach_default_agent(jobs_store: Any) -> None:
    """Attaches the agent in a prod project as the default.

    Args:
        jobs_store: The test's jobs store.
    """
    _write_attachment(
        jobs_store,
        _DEFAULT_KEY,
        observed_project_id="acme-prod",
        default=True,
        data_lookback_window=21,
    )


def _attach_staging_agent(jobs_store: Any) -> None:
    """Attaches the agent in a staging project besides the default one.

    Args:
        jobs_store: The test's jobs store.
    """
    _write_attachment(
        jobs_store,
        _STAGING_KEY,
        observed_project_id="acme-staging",
        data_lookback_window=3,
    )


def test_a_request_selects_its_agent_by_key(
    client: TestClient, jobs_store: Any
) -> None:
    _attach_staging_agent(jobs_store)

    response = client.post("/config", json={"agent_key": _STAGING_KEY})

    assert response.status_code == 200
    config = response.json()["config"]
    assert config["agent_key"] == _STAGING_KEY
    assert config["observed_agent_name"] == "it_support_agent"
    assert config["data_lookback_window"] == 3


@pytest.mark.parametrize(
    "body", [None, {}, {"agent_key": ""}, {"agent_key": " "}]
)
def test_a_request_that_selects_no_agent_gets_the_default(
    client: TestClient, jobs_store: Any, body: dict[str, Any] | None
) -> None:
    _attach_default_agent(jobs_store)
    _attach_staging_agent(jobs_store)

    response = client.post("/config", json=body)

    assert response.status_code == 200
    config = response.json()["config"]
    assert config["agent_key"] == _DEFAULT_KEY
    assert config["data_lookback_window"] == 21


def test_an_unknown_agent_is_rejected_rather_than_replaced(
    client: TestClient,
) -> None:
    """Answering for the default agent would show its data under another key."""
    response = client.post(
        "/insights/list", json={"agent_key": "acme-prod__gone"}
    )

    assert response.status_code == 400
    assert "acme-prod__gone" in response.json()["detail"]
    assert "list-agents" in response.json()["detail"]


def test_an_agent_key_that_is_not_a_string_is_rejected(
    client: TestClient,
) -> None:
    response = client.post("/investigations/list", json={"agent_key": 7})

    assert response.status_code == 400
    assert "agent_key" in response.json()["detail"]


def test_scheduling_runs_under_the_selected_agent(
    client: TestClient, jobs_store: Any, mock_schedule: mock.AsyncMock
) -> None:
    _attach_staging_agent(jobs_store)

    response = client.post(
        "/investigations/schedule", json={"agent_key": _STAGING_KEY}
    )

    assert response.status_code == 200
    context = mock_schedule.call_args.args[0]
    assert context.state == {effective_config.AGENT_KEY_STATE: _STAGING_KEY}


@pytest.mark.parametrize(
    "route",
    [
        protocol.INVESTIGATIONS_LIST_ROUTE,
        protocol.INVESTIGATIONS_STATS_ROUTE,
        protocol.INVESTIGATIONS_DAILY_ROUTE,
        protocol.INVESTIGATIONS_SCHEDULE_ROUTE,
        protocol.GOAL_GET_ROUTE,
        protocol.GOAL_VERSIONS_ROUTE,
        protocol.MEMORIES_LIST_ROUTE,
        protocol.INSIGHTS_LIST_ROUTE,
        protocol.TRAJECTORIES_OUTCOMES_ROUTE,
        protocol.TRAJECTORIES_LIST_ROUTE,
        protocol.HEALTH_ROUTE,
        protocol.SOURCE_ROUTE,
        protocol.CONFIG_ROUTE,
    ],
)
def test_every_agent_route_checks_the_key(
    client: TestClient, route: str
) -> None:
    """Routes that need nothing else, so the key is the only thing to refuse."""
    response = client.post(f"/{route}", json={"agent_key": "acme-prod__gone"})

    assert response.status_code == 400, route
    assert "acme-prod__gone" in response.json()["detail"], route


# --- the code-metric library ------------------------------------------------ #

_METRIC_LIBRARY = {
    "eval_config.yaml": b"metrics_to_run: [m]\ncustom_metrics:\n"
    b"  - name: m\n    custom_function_file: m.py\n",
    "m.py": b"def evaluate(instance):\n    return 1.0\n",
}


def _encode_library() -> list[dict[str, str]]:
    import base64

    return [
        {"path": path, "content": base64.b64encode(data).decode()}
        for path, data in _METRIC_LIBRARY.items()
    ]


def test_publishing_metrics_writes_them_with_the_engines_credentials(
    client: TestClient, metrics_store: Any, jobs_store: Any
) -> None:
    _attach_default_agent(jobs_store)

    planned = client.post(
        "/metrics/publish", json={"files": _encode_library(), "dry_run": True}
    )
    published = client.post(
        "/metrics/publish", json={"files": _encode_library()}
    )
    listed = client.post("/metrics/list")

    assert planned.status_code == 200
    assert planned.json()["published"] is False
    assert published.json()["published"] is True
    # A request selecting no agent publishes the default agent's library.
    assert (
        metrics_store.read_bytes(f"{_DEFAULT_KEY}/current/metrics/m.py")
        == _METRIC_LIBRARY["m.py"]
    )
    assert [f["path"] for f in listed.json()["files"]] == sorted(
        _METRIC_LIBRARY
    )


def test_destroying_metrics_plans_unless_told_otherwise(
    client: TestClient, metrics_store: Any
) -> None:
    metrics_store.write_bytes("current/metrics/m.py", b"")

    planned = client.post("/metrics/destroy", json={})
    destroyed = client.post("/metrics/destroy", json={"dry_run": False})

    assert planned.json()["deleted"] is False
    assert planned.json()["deletions"] == ["current/metrics/m.py"]
    assert destroyed.json()["deletions"] == ["current/metrics/m.py"]
    assert metrics_store.list_names("") == []


@pytest.mark.parametrize("route", ["/metrics/publish", "/metrics/destroy"])
def test_a_metrics_dry_run_must_be_a_boolean(
    client: TestClient, route: str
) -> None:
    response = client.post(route, json={"files": [], "dry_run": "false"})

    assert response.status_code == 400


def test_metrics_routes_refuse_an_agent_that_is_not_attached(
    client: TestClient,
) -> None:
    response = client.post(
        "/metrics/list", json={"agent_key": "acme-prod__not_attached"}
    )

    assert response.status_code == 400


def test_metrics_routes_publish_into_the_selected_agents_library(
    client: TestClient, metrics_store: Any, jobs_store: Any
) -> None:
    from ambient_quality_agent.config import ObservedAgentConfig
    from ambient_quality_agent.tools.observed_agent_config import (
        store as agent_store,
    )

    agent_store.save_agent_record(
        jobs_store,
        agent_store.AgentRecord(
            config=ObservedAgentConfig(
                observed_agent_name="support",
                agent_key="acme-prod__support",
                observed_project_id="acme-prod",
            )
        ),
    )

    response = client.post(
        "/metrics/publish",
        json={"files": _encode_library(), "agent_key": "acme-prod__support"},
    )

    assert response.json()["published"] is True, response.json()
    assert metrics_store.list_names("acme-prod__support/current/metrics/") == [
        "acme-prod__support/current/metrics/eval_config.yaml",
        "acme-prod__support/current/metrics/m.py",
    ]
