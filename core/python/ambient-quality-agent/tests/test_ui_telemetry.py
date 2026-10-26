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

"""Hermetic tests for the dashboard's telemetry endpoints, `/api/telemetry`.

Drives the FastAPI app in-process with a stub client whose `post_command()`
answers with what the `agents/attach` and `agents/applied` routes return.
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Any

import httpx
import pytest

import app as ui_app

_SA = "aqua@proj.iam.gserviceaccount.com"
_KEY = "proj__support"
_TABLE = "proj.analytics.agent_events"
_CHECK = {
    "agent_key": _KEY,
    "telemetry_dataset": "analytics",
    "telemetry_table": "agent_events",
}


class FakeClient:
    """Stub agent client answering `agents/attach` and `agents/applied`."""

    def __init__(
        self,
        attach: dict[str, Any] | None = None,
        applied: dict[str, Any] | None = None,
    ) -> None:
        self.attach = attach
        self.applied = applied
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def post_command(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((path, payload))
        if path == "agents/attach":
            return self.attach if self.attach is not None else {}
        if path == "agents/applied":
            return self.applied if self.applied is not None else {}
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


def _send(method: str, path: str, body: Any) -> httpx.Response:
    return asyncio.run(_send_request(method, path, json=body))


@pytest.fixture
def inject(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _install(client: Any) -> Any:
        monkeypatch.setattr(ui_app, "_get_backend_client", lambda: client)
        return client

    return _install


def _build_plan(*checks: tuple[str, str], **plan: Any) -> dict[str, Any]:
    """Builds the attach route's dry-run plan with an access check.

    Args:
        *checks: `(name, status)` of each check on the table, in order.
        **plan: Plan fields to set besides the access check.

    Returns:
        The plan as the route returns it.
    """
    return {
        "agent_key": _KEY,
        "changes": [],
        "filled": [],
        "access": {
            "service_account": _SA,
            "checks": [
                {
                    "name": name,
                    "status": status,
                    "target": _TABLE,
                    "detail": f"{name} detail",
                    "matching_sources": [],
                }
                for name, status in checks
            ],
        },
        **plan,
    }


_PASSING = _build_plan(
    ("table", "passed"), ("schema", "passed"), ("rows", "passed")
)


# --------------------------------------------------------------------------- #
# POST /api/telemetry/check                                                    #
# --------------------------------------------------------------------------- #


def test_check_asks_for_the_plan_with_the_access_check_and_the_fill(
    inject,
) -> None:
    client = inject(FakeClient(attach=_PASSING))

    response = _send("POST", "/api/telemetry/check", _CHECK)

    assert response.status_code == 200
    assert client.calls == [
        (
            "agents/attach",
            {
                "agent_key": _KEY,
                "fields": {
                    "telemetry_dataset": "analytics",
                    "telemetry_table": "agent_events",
                },
                "dry_run": True,
                "check_access": True,
                "fill_from_table": True,
            },
        )
    ]
    body = response.json()
    assert body["refusal"] == ""
    assert body["grants"] == []
    assert [c["status"] for c in body["checks"]] == ["passed"] * 3


def test_check_passes_a_payload_bucket_on(inject) -> None:
    client = inject(FakeClient(attach=_PASSING))

    _send(
        "POST",
        "/api/telemetry/check",
        {**_CHECK, "telemetry_payload_bucket": "agent-logs"},
    )

    [(_, payload)] = client.calls
    assert payload is not None
    assert payload["fields"]["telemetry_payload_bucket"] == "agent-logs"


def test_check_reports_what_the_table_filled_in(inject) -> None:
    filled = [
        {
            "field": "telemetry_location",
            "value": "EU",
            "reason": f"the location of {_TABLE}",
        }
    ]
    changes = [
        {"field": "telemetry_location", "before": "us-central1", "after": "EU"}
    ]
    inject(
        FakeClient(
            attach={**_PASSING, "filled": filled, "changes": changes},
        )
    )

    body = _send("POST", "/api/telemetry/check", _CHECK).json()

    assert body["filled"] == filled
    assert body["changes"] == changes


def test_a_denied_read_comes_with_the_command_that_grants_it(inject) -> None:
    """The commands are the ones `aqua attach` prints, built the same way."""
    inject(
        FakeClient(
            attach=_build_plan(
                ("table", "denied"), ("schema", "skipped"), ("rows", "skipped")
            )
        )
    )

    body = _send("POST", "/api/telemetry/check", _CHECK).json()

    member = f"serviceAccount:{_SA}"
    assert body["service_account"] == _SA
    assert body["grants"] == [
        {
            "resource": "proj:analytics",
            "role": "roles/bigquery.dataViewer",
            "member": member,
            "checks": ["table"],
            "command": shlex.join(
                [
                    "bq",
                    "query",
                    "--nouse_legacy_sql",
                    "--project_id=proj",
                    "GRANT `roles/bigquery.dataViewer` ON SCHEMA "
                    f'`proj.analytics` TO "{member}"',
                ]
            ),
        }
    ]
    assert body["ungranted"] == []
    # A grant can follow the save, as it can follow `aqua attach`.
    assert body["refusal"] == ""


@pytest.mark.parametrize(
    ("check", "refusal"),
    [
        (("table", "missing"), f"{_TABLE} does not exist."),
        (("schema", "mismatch"), "schema detail"),
        (("rows", "error"), f"Reading {_TABLE} failed: rows detail"),
    ],
)
def test_a_table_that_cannot_be_investigated_is_refused(
    inject, check: tuple[str, str], refusal: str
) -> None:
    inject(FakeClient(attach=_build_plan(check)))

    body = _send("POST", "/api/telemetry/check", _CHECK).json()

    assert body["refusal"] == refusal


@pytest.mark.parametrize(
    "check", [("rows", "empty"), ("payload", "error"), ("log_view", "denied")]
)
def test_a_table_an_investigation_can_read_is_not_refused(
    inject, check: tuple[str, str]
) -> None:
    """A new agent may have no rows yet, and a failed payload read does not
    stop an investigation from reading the table."""
    inject(FakeClient(attach=_build_plan(("table", "passed"), check)))

    body = _send("POST", "/api/telemetry/check", _CHECK).json()

    assert body["refusal"] == ""


@pytest.mark.parametrize(
    "body",
    [
        {**_CHECK, "agent_key": " "},
        {**_CHECK, "telemetry_dataset": ""},
        {**_CHECK, "telemetry_table": 3},
        ["not", "an", "object"],
    ],
)
def test_check_refuses_a_body_without_the_agent_and_the_table(
    inject, body: Any
) -> None:
    client = inject(FakeClient(attach=_PASSING))

    response = _send("POST", "/api/telemetry/check", body)

    assert response.status_code == 400
    assert response.json()["error"]
    assert client.calls == []


def test_check_passes_the_agents_refusal_on(inject) -> None:
    inject(FakeClient(attach={"error": "No agent is attached as 'x'."}))

    response = _send("POST", "/api/telemetry/check", _CHECK)

    assert response.status_code == 400
    assert response.json() == {"error": "No agent is attached as 'x'."}


def test_check_reports_an_unreachable_agent(inject) -> None:
    inject(BoomClient())

    response = _send("POST", "/api/telemetry/check", _CHECK)

    assert response.status_code == 502
    assert "backend exploded" in response.json()["error"]


# --------------------------------------------------------------------------- #
# PUT /api/telemetry                                                           #
# --------------------------------------------------------------------------- #


def test_save_stores_the_table_and_what_the_check_filled_in(inject) -> None:
    client = inject(
        FakeClient(
            attach={
                "written": True,
                "changes": [{"field": "telemetry_table", "before": "a"}],
                "updated_at": "2026-10-22T09:00:00+00:00",
            }
        )
    )

    response = _send(
        "PUT",
        "/api/telemetry",
        {
            **_CHECK,
            "telemetry_location": "EU",
            "telemetry_ingestion_source": "cloud_ops",
        },
    )

    assert response.status_code == 200
    assert client.calls == [
        (
            "agents/attach",
            {
                "agent_key": _KEY,
                "fields": {
                    "telemetry_dataset": "analytics",
                    "telemetry_table": "agent_events",
                    "telemetry_location": "EU",
                    "telemetry_ingestion_source": "cloud_ops",
                },
                "dry_run": False,
            },
        )
    ]
    assert response.json() == {
        "written": True,
        "changes": [{"field": "telemetry_table", "before": "a"}],
        "updated_at": "2026-10-22T09:00:00+00:00",
    }


def test_save_changes_no_field_besides_the_telemetry_ones(inject) -> None:
    client = inject(FakeClient(attach={"written": True}))

    _send(
        "PUT",
        "/api/telemetry",
        {**_CHECK, "investigation_schedule": "* * * * *"},
    )

    [(_, payload)] = client.calls
    assert payload is not None
    assert "investigation_schedule" not in payload["fields"]


def test_save_refuses_a_body_without_the_table(inject) -> None:
    client = inject(FakeClient(attach={"written": True}))

    response = _send(
        "PUT", "/api/telemetry", {"agent_key": _KEY, "telemetry_table": "t"}
    )

    assert response.status_code == 400
    assert client.calls == []


def test_save_passes_the_agents_refusal_on(inject) -> None:
    inject(FakeClient(attach={"error": "telemetry_location must be str"}))

    response = _send("PUT", "/api/telemetry", _CHECK)

    assert response.status_code == 400
    assert response.json() == {"error": "telemetry_location must be str"}


def test_save_reports_an_unreachable_agent(inject) -> None:
    inject(BoomClient())

    response = _send("PUT", "/api/telemetry", _CHECK)

    assert response.status_code == 502


# --------------------------------------------------------------------------- #
# POST /api/telemetry/grants                                                   #
# --------------------------------------------------------------------------- #

_RECORD = {
    "resource": "proj:analytics",
    "role": "roles/bigquery.dataViewer",
    "member": f"serviceAccount:{_SA}",
}


def test_recorded_grants_go_on_the_attachment(inject) -> None:
    """So `detach` revokes them, as it revokes what `aqua attach` ran."""
    client = inject(FakeClient(applied={"agent_key": _KEY}))

    response = _send(
        "POST",
        "/api/telemetry/grants",
        {"agent_key": _KEY, "granted": [_RECORD]},
    )

    assert response.status_code == 200
    assert response.json() == {"recorded": 1}
    assert client.calls == [
        ("agents/applied", {"agent_key": _KEY, "granted": [_RECORD]})
    ]


@pytest.mark.parametrize(
    "granted",
    [
        [],
        "proj:analytics",
        [{"resource": "proj:analytics"}],
        [{**_RECORD, "role": 1}],
    ],
)
def test_recording_refuses_anything_but_bindings(inject, granted: Any) -> None:
    client = inject(FakeClient(applied={"agent_key": _KEY}))

    response = _send(
        "POST",
        "/api/telemetry/grants",
        {"agent_key": _KEY, "granted": granted},
    )

    assert response.status_code == 400
    assert client.calls == []


def test_recording_passes_the_agents_refusal_on(inject) -> None:
    inject(FakeClient(applied={"error": "A grant to anybody but this engine"}))

    response = _send(
        "POST",
        "/api/telemetry/grants",
        {"agent_key": _KEY, "granted": [_RECORD]},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "A grant to anybody but this engine"}
