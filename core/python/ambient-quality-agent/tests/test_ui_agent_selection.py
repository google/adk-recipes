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

"""Hermetic tests for the dashboard's agent selection.

Every `/api/*` route passes the selected agent, `?agent_key=`, on to its
command route, and `/api/agents` lists what can be selected. Drives the FastAPI
app in-process with a stub client that records what it is sent.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

import app as ui_app

_KEY = "acme-prod__support"


class RecordingClient:
    """Stub agent client that records every command and answers `reply`."""

    def __init__(self, reply: dict[str, Any] | None = None) -> None:
        self.reply = reply if reply is not None else {"ok": True}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def post_command(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((path, payload))
        return self.reply


class RefusingClient:
    """Stub whose command route refuses the request, as FastAPI answers one."""

    async def post_command(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request = httpx.Request("POST", f"http://engine/{path}")
        response = httpx.Response(
            400,
            json={"detail": f"No agent is attached as {_KEY!r}."},
            request=request,
        )
        raise httpx.HTTPStatusError(
            "Client error '400 Bad Request'", request=request, response=response
        )


async def _send_request(
    method: str, path: str, **kwargs: Any
) -> httpx.Response:
    transport = httpx.ASGITransport(app=ui_app.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.request(method, path, **kwargs)


def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    return asyncio.run(_send_request(method, path, **kwargs))


@pytest.fixture
def inject(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _install(client: Any) -> Any:
        monkeypatch.setattr(ui_app, "_get_backend_client", lambda: client)
        return client

    return _install


# Each route, with what the request needs besides the key.
_ROUTES: list[tuple[str, str, str, dict[str, Any]]] = [
    ("GET", "/api/config", "config", {}),
    ("GET", "/api/health", "health", {}),
    ("GET", "/api/source", "source", {}),
    ("GET", "/api/investigations", "investigations/list", {}),
    ("POST", "/api/investigations", "investigations/schedule", {}),
    ("GET", "/api/stats", "investigations/stats", {}),
    ("GET", "/api/daily", "investigations/daily", {}),
    ("GET", "/api/investigations/r1", "investigations/get", {}),
    ("GET", "/api/investigations/r1/cases/c1", "trajectories/case", {}),
    ("GET", "/api/insights", "insights/list", {}),
    ("GET", "/api/insights/i1", "insights/get", {}),
    ("POST", "/api/insights/i1/dismiss", "insights/dismiss", {}),
    (
        "POST",
        "/api/insights/i1/merge",
        "insights/merge",
        {"json": {"target_insight_id": "i2"}},
    ),
    ("GET", "/api/goal", "documents/goal/get", {}),
    ("PUT", "/api/goal", "documents/goal/set", {"json": {"goal": "g"}}),
    ("GET", "/api/goal/versions", "documents/goal/versions", {}),
    ("GET", "/api/memories", "documents/memories/list", {}),
    ("DELETE", "/api/memories/m1", "documents/memories/delete", {}),
]


def test_the_routes_cover_every_selectable_endpoint() -> None:
    """A new `/api/*` route must be added here, or to the exceptions."""
    # `/api/agents` does not select an agent, and test_ui_schedule.py and
    # test_ui_telemetry.py cover how the other routes select the agent.
    exceptions = {
        "/api/agents",
        "/api/schedule",
        "/api/telemetry",
        "/api/telemetry/check",
        "/api/telemetry/grants",
    }
    covered = {
        path.replace("r1", "{run_id}")
        .replace("c1", "{case_id}")
        .replace("i1", "{insight_id}")
        .replace("m1", "{memory_id}")
        for _, path, _, _ in _ROUTES
    }
    routes = {
        route.path
        for route in ui_app.app.routes
        if getattr(route, "path", "").startswith("/api/")
    }
    assert routes - exceptions == covered


@pytest.mark.parametrize(
    ("method", "path", "command", "kwargs"),
    _ROUTES,
    ids=[f"{m} {p}" for m, p, _, _ in _ROUTES],
)
def test_every_route_passes_the_selected_agent_on(
    inject, method, path, command, kwargs
) -> None:
    client = inject(RecordingClient())

    _request(method, path, params={"agent_key": _KEY}, **kwargs)

    assert [called for called, _ in client.calls] == [command]
    assert (client.calls[0][1] or {}).get("agent_key") == _KEY


@pytest.mark.parametrize(
    ("method", "path", "command", "kwargs"),
    _ROUTES,
    ids=[f"{m} {p}" for m, p, _, _ in _ROUTES],
)
def test_a_request_that_selects_no_agent_sends_no_key(
    inject, method, path, command, kwargs
) -> None:
    client = inject(RecordingClient())

    _request(method, path, params={"agent_key": "  "}, **kwargs)

    assert "agent_key" not in (client.calls[0][1] or {})


def test_a_refused_key_reads_as_the_agents_reason(inject) -> None:
    inject(RefusingClient())

    body = _request("GET", "/api/insights", params={"agent_key": _KEY}).json()

    assert body["error"] == f"No agent is attached as {_KEY!r}."


# --------------------------------------------------------------------------- #
# GET /api/agents                                                              #
# --------------------------------------------------------------------------- #


def _build_entry(key: str, **overrides: Any) -> dict[str, Any]:
    return {
        "agent_key": key,
        "agent_name": key.rsplit("__", 1)[-1],
        "default": False,
        "investigation_schedule": "0 12 * * *",
        "grants": [{"role": "roles/x"}],
        "watched": False,
        **overrides,
    }


def test_agents_lists_each_attachment_with_its_project(inject) -> None:
    inject(
        RecordingClient(
            {
                "agents": [
                    _build_entry("support", watched=True, default=True),
                    _build_entry("acme-prod__support"),
                ],
                "default_agent": "support",
            }
        )
    )

    body = _request("GET", "/api/agents").json()

    assert body == {
        "agents": [
            {
                "agent_key": "support",
                "agent_name": "support",
                "project_id": None,
                "default": True,
            },
            {
                "agent_key": "acme-prod__support",
                "agent_name": "support",
                "project_id": "acme-prod",
                "default": False,
            },
        ],
        "default_agent_key": "support",
    }


def test_with_nothing_attached_there_is_no_default(inject) -> None:
    inject(RecordingClient({"agents": [], "default_agent": None}))

    body = _request("GET", "/api/agents").json()

    assert body == {"agents": [], "default_agent_key": None}


def test_an_unreadable_attachment_is_listed_with_its_reason(inject) -> None:
    inject(RecordingClient({"agents": [{"agent_key": "x", "error": "denied"}]}))

    agents = _request("GET", "/api/agents").json()["agents"]

    assert agents == [
        {
            "agent_key": "x",
            "agent_name": None,
            "project_id": None,
            "default": False,
            "error": "denied",
        }
    ]


def test_a_listing_failure_is_an_error_not_an_empty_list(inject) -> None:
    inject(RecordingClient({"error": "no jobs bucket"}))

    body = _request("GET", "/api/agents").json()

    assert body == {"agents": [], "error": "no jobs bucket"}


# --------------------------------------------------------------------------- #
# GET /api/schedule                                                            #
# --------------------------------------------------------------------------- #


def test_the_schedule_is_the_selected_agents(inject) -> None:
    inject(
        RecordingClient(
            {
                "agents": [
                    _build_entry("support", watched=True),
                    _build_entry(
                        _KEY,
                        investigation_schedule="30 6 * * 1-5",
                    ),
                ]
            }
        )
    )

    body = _request("GET", "/api/schedule", params={"agent_key": _KEY}).json()

    assert body["agent_key"] == _KEY
    assert body["investigation_schedule"] == "30 6 * * 1-5"


def test_the_schedule_of_an_agent_that_is_not_attached_is_an_error(
    inject,
) -> None:
    inject(RecordingClient({"agents": [_build_entry("support", watched=True)]}))

    body = _request("GET", "/api/schedule", params={"agent_key": _KEY}).json()

    assert body == {"error": f"Agent {_KEY!r} is not attached."}
