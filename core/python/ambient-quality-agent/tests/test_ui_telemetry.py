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

"""Hermetic tests for the dashboard's telemetry and selector endpoints.

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


# --------------------------------------------------------------------------- #
# POST /api/selector/check and PUT /api/selector                               #
# --------------------------------------------------------------------------- #

_SQL = (
    "SELECT DISTINCT session_id AS target_id FROM analytics.agent_events "
    "WHERE timestamp BETWEEN @window_start AND @window_end "
    "AND agent = @agent_name"
)
_SELECTOR = {"agent_key": _KEY, "investigation_selector_sql": _SQL}
_SELECTOR_CHANGE = {
    "field": "investigation_selector_sql",
    "before": "",
    "after": _SQL,
}


def test_selector_check_asks_for_the_plan_with_the_precheck(inject) -> None:
    client = inject(
        FakeClient(attach={**_PASSING, "changes": [_SELECTOR_CHANGE]})
    )

    response = _send("POST", "/api/selector/check", _SELECTOR)

    assert response.status_code == 200
    assert client.calls == [
        (
            "agents/attach",
            {
                "agent_key": _KEY,
                "fields": {"investigation_selector_sql": _SQL},
                "dry_run": True,
                "check_access": True,
            },
        )
    ]
    assert response.json() == {"refusal": "", "changes": [_SELECTOR_CHANGE]}


def test_a_refused_selector_comes_back_with_the_explanation_alone(
    inject,
) -> None:
    """The card says the selector is refused; the CLI's prefix repeats it."""
    explanation = (
        "A selector must read the selector table analytics.agent_events by "
        "name and name no other table."
    )
    inject(
        FakeClient(
            attach={
                "error": f"Refusing to attach {_KEY}: investigation_selector_sql "
                f"is refused (unauthorized_table): {explanation}"
            }
        )
    )

    response = _send("POST", "/api/selector/check", _SELECTOR)

    assert response.status_code == 200
    assert response.json() == {"refusal": explanation, "changes": []}


def test_another_refusal_comes_back_as_the_agent_wrote_it(inject) -> None:
    reason = (
        f"Refusing to attach {_KEY}: investigation_selector_sql needs "
        "quality_analysis_mode 'session_review'."
    )
    inject(FakeClient(attach={"error": reason}))

    body = _send("POST", "/api/selector/check", _SELECTOR).json()

    assert body == {"refusal": reason, "changes": []}


def test_a_selector_on_a_table_that_cannot_be_read_is_refused(inject) -> None:
    """The engine skips the precheck when the access check refuses the table."""
    inject(FakeClient(attach=_build_plan(("table", "missing"))))

    body = _send("POST", "/api/selector/check", _SELECTOR).json()

    assert body["refusal"] == f"{_TABLE} does not exist."


@pytest.mark.parametrize("path", ["/api/selector/check", "/api/selector"])
@pytest.mark.parametrize(
    "body",
    [
        {**_SELECTOR, "agent_key": ""},
        {"agent_key": _KEY},
        {**_SELECTOR, "investigation_selector_sql": None},
        ["not", "an", "object"],
    ],
)
def test_selector_routes_refuse_a_body_without_the_agent_and_the_selector(
    inject, path: str, body: Any
) -> None:
    client = inject(FakeClient(attach={"written": True}))

    response = _send("POST" if path.endswith("check") else "PUT", path, body)

    assert response.status_code == 400
    assert response.json()["error"]
    assert client.calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/api/selector/check"), ("PUT", "/api/selector")],
)
def test_selector_routes_report_an_unreachable_agent(
    inject, method: str, path: str
) -> None:
    inject(BoomClient())

    response = _send(method, path, _SELECTOR)

    assert response.status_code == 502
    assert "backend exploded" in response.json()["error"]


class CheckThenStoreClient(FakeClient):
    """Stub answering an attach dry run with `plan` and a store with `stored`."""

    def __init__(self, plan: dict[str, Any], stored: dict[str, Any]) -> None:
        super().__init__()
        self.plan = plan
        self.stored = stored

    async def post_command(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((path, payload))
        return self.plan if (payload or {}).get("dry_run") else self.stored


_STORED = {
    "written": True,
    "changes": [_SELECTOR_CHANGE],
    "updated_at": "2026-11-02T09:00:00+00:00",
}


def test_selector_save_checks_the_selector_then_stores_only_it(inject) -> None:
    """A save re-checks the selector, so a stale tab cannot store one the
    precheck refuses."""
    client = inject(
        CheckThenStoreClient(
            plan={**_PASSING, "changes": [_SELECTOR_CHANGE]}, stored=_STORED
        )
    )

    response = _send(
        "PUT", "/api/selector", {**_SELECTOR, "telemetry_table": "other"}
    )

    assert response.status_code == 200
    fields = {"investigation_selector_sql": _SQL}
    assert client.calls == [
        (
            "agents/attach",
            {
                "agent_key": _KEY,
                "fields": fields,
                "dry_run": True,
                "check_access": True,
            },
        ),
        (
            "agents/attach",
            {"agent_key": _KEY, "fields": fields, "dry_run": False},
        ),
    ]
    assert response.json() == _STORED


@pytest.mark.parametrize(
    ("plan", "error"),
    [
        (
            {
                "error": f"Refusing to attach {_KEY}: investigation_selector_sql "
                "is refused (invalid_sql): Unrecognized name: target_id."
            },
            "Unrecognized name: target_id.",
        ),
        (_build_plan(("table", "missing")), f"{_TABLE} does not exist."),
    ],
)
def test_selector_save_stores_nothing_the_check_refuses(
    inject, plan: dict[str, Any], error: str
) -> None:
    client = inject(CheckThenStoreClient(plan=plan, stored=_STORED))

    response = _send("PUT", "/api/selector", _SELECTOR)

    assert response.status_code == 400
    assert response.json() == {"error": error}
    assert [p["dry_run"] for _, p in client.calls if p] == [True]


def test_an_empty_selector_clears_the_stored_one(inject) -> None:
    client = inject(FakeClient(attach={"written": True}))

    _send(
        "PUT", "/api/selector", {**_SELECTOR, "investigation_selector_sql": ""}
    )

    [(_, payload)] = client.calls
    assert payload is not None
    assert payload["fields"] == {"investigation_selector_sql": ""}


def test_selector_save_passes_the_agents_refusal_on(inject) -> None:
    reason = "investigation_selector_sql needs quality_analysis_mode"
    inject(FakeClient(attach={"error": reason}))

    response = _send("PUT", "/api/selector", _SELECTOR)

    assert response.status_code == 400
    assert response.json() == {"error": reason}


def test_a_multi_line_explanation_comes_back_whole(inject) -> None:
    explanation = "The selector failed the dry run:\nUnrecognized name: x."
    inject(
        FakeClient(
            attach={
                "error": f"Refusing to attach {_KEY}: investigation_selector_sql "
                f"is refused (invalid_sql): {explanation}"
            }
        )
    )

    body = _send("POST", "/api/selector/check", _SELECTOR).json()

    assert body["refusal"] == explanation


def test_a_lexical_refusal_comes_back_as_the_explanation(inject) -> None:
    """The engine's lexical check refuses without the attach prefix."""
    explanation = (
        "The selector must be a single SELECT expression; remove the ';'."
    )
    inject(
        FakeClient(
            attach={
                "error": "investigation_selector_sql is refused "
                f"(statement_separator): {explanation}"
            }
        )
    )

    body = _send("POST", "/api/selector/check", _SELECTOR).json()

    assert body == {"refusal": explanation, "changes": []}


@pytest.mark.parametrize(
    "error",
    [
        "No agent is attached as 'x'.",
        f"Could not read the stored attachment for {_KEY}: timed out",
        f"Could not check investigation_selector_sql for {_KEY}: 500 backend",
    ],
)
def test_selector_check_passes_other_agent_errors_on(
    inject, error: str
) -> None:
    """Only a refusal of the selector is a verdict; a failure is an error."""
    inject(FakeClient(attach={"error": error}))

    response = _send("POST", "/api/selector/check", _SELECTOR)

    assert response.status_code == 400
    assert response.json() == {"error": error}
