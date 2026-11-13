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

"""Hermetic tests for the dashboard's schedule endpoints, `/api/schedule`.

Drives the FastAPI app in-process with a stub client whose `post_command()`
answers with what the `agents/list` and `agents/attach` routes return.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from ambient_quality_agent.tools.observed_agent_config import store

import app as ui_app


class FakeClient:
    """Stub agent client answering `agents/list` and `agents/attach`."""

    def __init__(
        self,
        listing: dict[str, Any] | None = None,
        attach: dict[str, Any] | None = None,
    ) -> None:
        self.listing = listing
        self.attach = attach
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def post_command(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((path, payload))
        if path == "agents/list":
            return self.listing if self.listing is not None else {}
        if path == "agents/attach":
            return self.attach if self.attach is not None else {}
        raise AssertionError(f"unexpected command path {path!r}")


class BoomClient:
    """Stub whose `post_command()` raises, to exercise the error path."""

    async def post_command(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raise RuntimeError("backend exploded")


async def _send_request(
    method: str, path: str, **kwargs: Any
) -> httpx.Response:
    transport = httpx.ASGITransport(app=ui_app.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.request(method, path, **kwargs)


def _get(path: str) -> httpx.Response:
    return asyncio.run(_send_request("GET", path))


def _put(path: str, **kwargs: Any) -> httpx.Response:
    return asyncio.run(_send_request("PUT", path, **kwargs))


@pytest.fixture
def inject(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _install(client: Any) -> Any:
        monkeypatch.setattr(ui_app, "_get_backend_client", lambda: client)
        return client

    return _install


def _build_entry(
    key: str, *, watched: bool, **overrides: Any
) -> dict[str, Any]:
    return {
        "agent_key": key,
        "agent_name": key.rsplit("/", 1)[-1],
        "default": False,
        "investigation_schedule": "0 12 * * *",
        "scheduled_trigger_enabled": True,
        "updated_at": "2026-10-14T09:00:00+00:00",
        "grants": [],
        "watched": watched,
        **overrides,
    }


# --------------------------------------------------------------------------- #
# GET /api/schedule                                                            #
# --------------------------------------------------------------------------- #


def test_get_shows_the_investigated_agent_not_the_first(inject) -> None:
    inject(
        FakeClient(
            listing={
                "agents": [
                    _build_entry("p/other", watched=False),
                    _build_entry(
                        "p/billing",
                        watched=True,
                        investigation_schedule="30 6 * * 1-5",
                        scheduled_trigger_enabled=False,
                    ),
                ],
            }
        )
    )
    body = _get("/api/schedule").json()
    assert body == {
        "available": True,
        "agent_key": "p/billing",
        "agent_name": "billing",
        "investigation_schedule": "30 6 * * 1-5",
        "scheduled_trigger_enabled": False,
        "updated_at": "2026-10-14T09:00:00+00:00",
    }


def test_get_does_not_pass_the_grants_through(inject) -> None:
    inject(FakeClient(listing={"agents": [_build_entry("p/a", watched=True)]}))
    assert "grants" not in _get("/api/schedule").json()


def test_get_with_no_attached_agent_says_how_to_attach(inject) -> None:
    inject(FakeClient(listing={"agents": []}))
    body = _get("/api/schedule").json()
    assert body["available"] is False
    assert "agents-cli aqua attach" in body["reason"]


def test_get_with_no_investigated_agent_says_how_to_attach(inject) -> None:
    # Neither selected nor investigated, the listed attachment is not the
    # agent whose schedule a request with no key would show.
    inject(
        FakeClient(listing={"agents": [_build_entry("p/other", watched=False)]})
    )
    body = _get("/api/schedule").json()
    assert body["available"] is False
    assert "p/other" not in body["reason"]
    assert "agents-cli aqua attach" in body["reason"]


def test_get_passes_the_agents_refusal_on_as_unavailable(inject) -> None:
    inject(FakeClient(listing={"error": "This deployment has no jobs bucket"}))
    body = _get("/api/schedule").json()
    assert body == {
        "available": False,
        "reason": "This deployment has no jobs bucket",
    }


def test_get_reports_an_unreadable_attachment_as_an_error(inject) -> None:
    # Not "no agent attached": the attachment is there and cannot be read.
    inject(
        FakeClient(
            listing={
                "agents": [
                    {
                        "agent_key": "p/a",
                        "error": "ValueError: bad",
                        "watched": True,
                    }
                ]
            }
        )
    )
    body = _get("/api/schedule").json()
    assert "p/a" in body["error"]
    assert "ValueError: bad" in body["error"]
    assert "available" not in body


def test_get_reports_an_empty_reply_as_an_error(inject) -> None:
    inject(FakeClient())
    assert "error" in _get("/api/schedule").json()


def test_get_reports_an_unreachable_agent_as_an_error(inject) -> None:
    inject(BoomClient())
    assert "backend exploded" in _get("/api/schedule").json()["error"]


# --------------------------------------------------------------------------- #
# PUT /api/schedule                                                            #
# --------------------------------------------------------------------------- #


def test_the_editable_fields_are_attachment_fields() -> None:
    assert set(ui_app._SCHEDULE_FIELDS) <= store.RECORD_FIELDS


def test_put_stores_both_fields_on_the_named_attachment(inject) -> None:
    client = inject(
        FakeClient(attach={"agent_key": "p/a", "written": True, "changes": []})
    )
    resp = _put(
        "/api/schedule",
        json={
            "agent_key": "p/a",
            "investigation_schedule": "0 6 * * *",
            "scheduled_trigger_enabled": False,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["written"] is True
    assert client.calls == [
        (
            "agents/attach",
            {
                "agent_key": "p/a",
                "fields": {
                    "investigation_schedule": "0 6 * * *",
                    "scheduled_trigger_enabled": False,
                },
                "dry_run": False,
            },
        )
    ]


def test_put_returns_the_changes_and_not_the_engines_resources(
    inject,
) -> None:
    change = {"field": "investigation_schedule", "before": "a", "after": "b"}
    inject(
        FakeClient(
            attach={
                "agent_key": "p/a",
                "written": True,
                "changes": [change],
                "updated_at": "2026-10-15T07:00:00+00:00",
                "object": "gs://jobs/agents/p/a.json",
                "trigger_target": {"topic": "projects/p/topics/t"},
            }
        )
    )
    body = _put(
        "/api/schedule",
        json={"agent_key": "p/a", "investigation_schedule": "0 6 * * *"},
    ).json()
    assert body == {
        "written": True,
        "changes": [change],
        "updated_at": "2026-10-15T07:00:00+00:00",
    }


def test_put_sends_only_the_fields_given(inject) -> None:
    client = inject(FakeClient(attach={"written": True}))
    _put(
        "/api/schedule",
        json={"agent_key": "p/a", "scheduled_trigger_enabled": True},
    )
    assert client.calls[0][1]["fields"] == {"scheduled_trigger_enabled": True}


def test_put_drops_fields_other_than_the_schedule(inject) -> None:
    # Every other field is `aqua attach`'s, which checks it before storing.
    client = inject(FakeClient(attach={"written": True}))
    _put(
        "/api/schedule",
        json={
            "agent_key": "p/a",
            "investigation_schedule": "0 6 * * *",
            "telemetry_table": "other.dataset.table",
        },
    )
    assert client.calls[0][1]["fields"] == {
        "investigation_schedule": "0 6 * * *"
    }


@pytest.mark.parametrize(
    "body",
    [
        {"investigation_schedule": "0 6 * * *"},
        {"agent_key": "", "investigation_schedule": "0 6 * * *"},
        {"agent_key": 7, "investigation_schedule": "0 6 * * *"},
    ],
)
def test_put_refuses_a_body_without_an_agent_key(inject, body) -> None:
    # Without a key the change has no attachment to apply to.
    client = inject(FakeClient())
    resp = _put("/api/schedule", json=body)
    assert resp.status_code == 400
    assert "agent_key" in resp.json()["error"]
    assert client.calls == []


def test_put_refuses_a_body_with_nothing_to_change(inject) -> None:
    client = inject(FakeClient())
    resp = _put("/api/schedule", json={"agent_key": "p/a"})
    assert resp.status_code == 400
    assert "investigation_schedule" in resp.json()["error"]
    assert client.calls == []


@pytest.mark.parametrize("content", ["not json", "[1, 2]"])
def test_put_refuses_a_body_that_is_not_a_json_object(inject, content) -> None:
    client = inject(FakeClient())
    resp = _put(
        "/api/schedule",
        content=content,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert client.calls == []


def test_put_passes_the_agents_refusal_on_as_a_400(inject) -> None:
    inject(
        FakeClient(
            attach={
                "error": "investigation_schedule must be a five-field cron "
                "expression in UTC"
            }
        )
    )
    resp = _put(
        "/api/schedule",
        json={"agent_key": "p/a", "investigation_schedule": "every day"},
    )
    assert resp.status_code == 400
    assert "five-field cron" in resp.json()["error"]


def test_put_reports_an_empty_reply_as_a_502(inject) -> None:
    # Not "saved": nothing says the change was stored.
    inject(FakeClient())
    resp = _put(
        "/api/schedule",
        json={"agent_key": "p/a", "investigation_schedule": "0 6 * * *"},
    )
    assert resp.status_code == 502


def test_put_reports_an_unreachable_agent_as_a_502(inject) -> None:
    inject(BoomClient())
    resp = _put(
        "/api/schedule",
        json={"agent_key": "p/a", "investigation_schedule": "0 6 * * *"},
    )
    assert resp.status_code == 502
    assert "backend exploded" in resp.json()["error"]
