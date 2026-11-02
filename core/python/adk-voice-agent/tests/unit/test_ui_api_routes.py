# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for the /api/v1 control plane: thread serialization, context cards, state, preferences, and endpoints."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.api.serializers import (
    thread_detail,
    thread_summary,
)
from app.context_cards import (
    active_context_cards,
    dismiss_context_card,
    record_context_card,
)
from app.fast_api_app import app
from app.store.thread_store import ThreadStore
from app.threads.model import (
    EventKind,
    PermissionRequest,
    ThreadHandle,
    ThreadState,
    Turn,
)
from app.threads.registry import ThreadRegistry, set_thread_registry
from app.voice.focus import clear_focus
from app.workers.harnesses.registry import set_harness_registry
from tests.fakes import GOAL, REPO, Lab, configure_apps, seed_thread

client = TestClient(app)

PREFERENCE_KEYS = (
    "USER_TIMEZONE",
    "LIVE_VOICE_NAME",
    "CODING_HARNESS",
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ThreadStore]:
    # TestClient serves each request on its own event loop, so no pooled connections.
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ui.db'}", poolclass=NullPool
    )
    yield ThreadStore(engine)
    asyncio.run(engine.dispose())


@pytest.fixture(autouse=True)
def registry(store: ThreadStore) -> Iterator[ThreadRegistry]:
    set_harness_registry(None)
    clear_focus()
    reg = ThreadRegistry(store)
    set_thread_registry(reg)
    yield reg
    clear_focus()
    set_thread_registry(None)


@pytest_asyncio.fixture
async def api(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    # Preference writes go to os.environ; start from "unset" so teardown restores it.
    for key in PREFERENCE_KEYS:
        monkeypatch.setenv(key, "")
    set_harness_registry(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        yield async_client
    set_harness_registry(None)


def _handle(
    state: ThreadState, thread_id: str = "thread-0000abcd", **fields
) -> ThreadHandle:
    handle = ThreadHandle(
        thread_id=thread_id,
        goal="Add refresh-token rotation",
        project="code/my-service",
        harness="claude",
        branch=f"agent/{thread_id}",
        state=state,
        **fields,
    )
    if state in (
        ThreadState.COMPLETED,
        ThreadState.FAILED,
        ThreadState.CANCELLED,
    ):
        handle.ended_at = time.time()
    return handle


def _add(store: ThreadStore, handle: ThreadHandle) -> ThreadHandle:
    return asyncio.run(seed_thread(store, handle))


def test_thread_serializers_expose_the_contract_and_hide_internals() -> None:
    handle = _handle(
        ThreadState.EXECUTING, workdir="/w/code/my-service", base_sha="abc"
    )
    handle.sessions["claude"] = "s-1"
    handle.turns = [
        Turn(
            seq=1,
            prompt="Add refresh-token rotation",
            outcome="running",
        )
    ]
    handle.record(EventKind.ACTIVITY, "Editing app/auth.py")
    handle.record(
        EventKind.PROGRESS, "Rotation logic is in place; running tests."
    )

    summary, data = thread_summary(handle), thread_detail(handle)

    assert summary["thread_id"] == handle.thread_id
    assert "task_id" not in summary and "task_id" not in data
    assert summary["status"] == "running" and summary["outcome"] == ""
    assert summary["turn_count"] == 1
    assert summary["state"] == "executing"
    assert (
        summary["is_running"]
        and not summary["is_gate"]
        and not summary["is_finished"]
    )
    assert "events" not in summary and "raw_diff" not in summary
    for dropped_diff_key in (
        "raw_diff",
        "files_changed",
        "diff_summary",
        "commit_sha",
        "pushed",
    ):
        assert dropped_diff_key not in summary
        assert dropped_diff_key not in data
        assert dropped_diff_key not in data["turns"][0]
    for removed_key in (
        "harness_display_name",
        "harness_icon",
        "state_label",
        "iteration",
    ):
        assert removed_key not in summary
    for internal in ("sessions", "workdir", "base_sha", "segment"):
        assert internal not in data
    assert data["latest_update"] == "Rotation logic is in place; running tests."
    assert [e["kind"] for e in data["events"]] == [
        "activity",
        "progress",
    ]
    assert len(data["turns"]) == 1
    assert data["turns"][0]["prompt"] == "Add refresh-token rotation"
    assert data["is_stale"] is False


def test_thread_detail_carries_the_whole_answer_and_no_documents() -> None:
    answer = "# Rotation\n\n" + "Details. " * 60
    handle = _handle(
        ThreadState.COMPLETED,
        result_text=answer,
        artifacts=[{"name": "notes.md", "title": "Notes", "content": "x"}],
    )

    data = thread_detail(handle)

    assert data["result_text"] == answer
    assert data["status"] == "idle" and data["outcome"] == "finished"
    assert "artifacts" not in data
    summary = thread_summary(handle)
    assert summary["result_text"] == answer


def _github_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_url: str
) -> None:
    local = tmp_path / "workspaces.local.yaml"
    local.write_text(
        f"repositories:\n  - name: my-service\n    git_url: {git_url}\n"
    )
    monkeypatch.setenv("VOICE_AGENT_WORKSPACES_LOCAL_YAML", str(local))


def test_links_list_the_pushed_branch_then_drive_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _github_repo(tmp_path, monkeypatch, "git@github.com:acme/my-service.git")
    handle = _handle(ThreadState.COMPLETED, pushed=True)
    handle.record(
        EventKind.PROGRESS,
        "Saved the notes to Drive.",
        {
            "output": {
                "title": "Rotation notes",
                "url": "https://docs.google.com/document/d/1",
                "kind": "doc",
            }
        },
    )

    assert thread_summary(handle)["links"] == [
        {
            "kind": "branch",
            "title": "agent/thread-0000abcd",
            "url": "https://github.com/acme/my-service/tree/agent/thread-0000abcd",
        },
        {
            "kind": "doc",
            "title": "Rotation notes",
            "url": "https://docs.google.com/document/d/1",
        },
    ]


@pytest.mark.parametrize(
    ("git_url", "pushed", "project"),
    [
        ("https://github.com/acme/my-service.git", False, "code/my-service"),
        ("https://gitlab.com/acme/my-service.git", True, "code/my-service"),
        ("", True, "code/my-service"),
        ("https://github.com/acme/my-service.git", True, "projects/notes"),
    ],
)
def test_no_branch_link_unless_pushed_to_github(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_url: str,
    pushed: bool,
    project: str,
) -> None:
    _github_repo(tmp_path, monkeypatch, git_url or '""')
    handle = _handle(ThreadState.COMPLETED, pushed=pushed)
    handle.project = project

    assert thread_summary(handle)["links"] == []


def test_context_cards_are_recorded_and_dismissed() -> None:
    record_context_card(
        kind="calendar",
        brand_icon="google",
        surface_id="a2ui-ctx-calendar",
        title="t",
        subtitle="",
        bullets=[],
    )
    shown = active_context_cards()
    assert [c["subkind"] for c in shown] == ["calendar"]
    assert shown[0]["catalogId"].endswith("/catalog.json")
    dismiss_context_card(shown[0]["surfaceId"])
    assert active_context_cards() == []


def test_state_endpoint_lists_threads_and_gates_first(
    store: ThreadStore,
) -> None:
    permission = PermissionRequest(
        action_class="deploy", command="agents-cli deploy", key="k1"
    )
    _add(
        store,
        _handle(
            ThreadState.AWAITING_PERMISSION,
            thread_id="thread-b",
            permission=permission,
        ),
    )
    _add(store, _handle(ThreadState.EXECUTING, thread_id="thread-c"))
    _add(store, _handle(ThreadState.COMPLETED, thread_id="thread-d"))

    data = client.get("/api/v1/state").json()

    assert "tasks" not in data
    threads = {t["thread_id"]: t for t in data["threads"]}
    assert set(threads) == {"thread-b", "thread-c", "thread-d"}
    assert {t for t in threads if threads[t]["is_gate"]} == {"thread-b"}
    assert {t for t in threads if threads[t]["is_running"]} == {"thread-c"}
    assert threads["thread-b"]["status"] == "awaiting_approval"
    assert threads["thread-b"]["permission"]["key"] == "k1"
    assert threads["thread-c"]["status"] == "running"
    assert threads["thread-d"]["status"] == "idle"


def test_focus_and_threads_routes(store: ThreadStore) -> None:
    _add(store, _handle(ThreadState.COMPLETED, thread_id="thread-1234"))

    focus_res = client.post(
        "/api/v1/focus",
        json={
            "view": "thread",
            "thread_id": "thread-1234",
            "project": "code/my-service",
        },
    )
    assert focus_res.status_code == 200
    assert focus_res.json()["focus"] == {
        "view": "thread",
        "thread_id": "thread-1234",
        "project": "code/my-service",
    }

    bad_focus = client.post("/api/v1/focus", json={"view": "nope"})
    assert bad_focus.status_code == 200
    assert bad_focus.json()["focus"]["view"] == "home"

    detail_res = client.get("/api/v1/threads/thread-1234")
    assert detail_res.status_code == 200
    assert detail_res.json()["thread_id"] == "thread-1234"


def test_thread_endpoints_map_errors(store: ThreadStore) -> None:
    _add(store, _handle(ThreadState.COMPLETED, thread_id="thread-done"))
    _add(
        store,
        _handle(
            ThreadState.AWAITING_PERMISSION,
            thread_id="thread-gate",
            permission=PermissionRequest(
                action_class="gmail.send*",
                command="gmail__send_message",
                key="k1",
            ),
        ),
    )

    assert (
        client.post(
            "/api/v1/threads/thread-missing/approve",
            json={"permission_key": "k1"},
        ).status_code
        == 404
    )
    assert (
        client.post("/api/v1/threads/thread-missing/cancel").status_code == 404
    )
    assert (
        client.post(
            "/api/v1/threads/thread-missing/message", json={"instruction": "x"}
        ).status_code
        == 404
    )
    assert (
        client.get("/api/v1/threads/thread-missing/events").status_code == 404
    )
    assert client.get("/api/v1/threads/thread-missing").status_code == 404

    assert client.post("/api/v1/threads/thread-gate/approve").status_code == 400
    assert (
        client.post(
            "/api/v1/threads/thread-gate/approve",
            json={"permission_key": "  "},
        ).status_code
        == 400
    )
    stale = client.post(
        "/api/v1/threads/thread-gate/approve",
        json={"permission_key": "stale-key"},
    )
    assert stale.status_code == 409
    assert "review it again" in stale.json()["detail"]

    res = client.post(
        "/api/v1/threads/thread-done/approve", json={"permission_key": "k1"}
    )
    assert res.status_code == 409
    assert "review it again" in res.json()["detail"]
    assert client.post("/api/v1/threads/thread-done/cancel").status_code == 409
    assert (
        client.post(
            "/api/v1/threads/thread-done/message", json={"instruction": " "}
        ).status_code
        == 400
    )
    assert (
        client.post("/api/v1/threads", json={"goal": "  "}).status_code == 400
    )
    assert (
        client.post(
            "/api/v1/threads", json={"goal": "x", "harness": "nope"}
        ).status_code
        == 400
    )


def test_cancel_records_cancelled_state_and_event(store: ThreadStore) -> None:
    handle = _handle(
        ThreadState.AWAITING_PERMISSION,
        permission=PermissionRequest(
            action_class="git_push", command="git push origin main", key="k1"
        ),
    )
    handle.record(EventKind.PERMISSION_REQUESTED, "Needs approval.")
    _add(store, handle)

    res = client.post("/api/v1/threads/thread-0000abcd/cancel")
    assert res.status_code == 200
    assert res.json()["state"] == "cancelled"
    assert res.json()["status"] == "idle"
    assert res.json()["outcome"] == "cancelled"

    detail = client.get("/api/v1/threads/thread-0000abcd").json()
    assert detail["state"] == "cancelled"
    assert [e["kind"] for e in detail["events"]][-2:] == [
        "state_changed",
        "cancelled",
    ]


APP_KEYS = {
    "name",
    "display_name",
    "icon",
    "connected",
    "account_label",
    "needs_grant",
    "services",
    "manage_url",
}


def _apps(**headers: str) -> dict[str, dict[str, Any]]:
    config = client.get("/api/v1/config", headers=headers).json()
    for gone in ("integrations", "workspace_connection", "is_operator"):
        assert gone not in config
    return {a["name"]: a for a in config["apps"]}


def test_config_lists_only_the_apps_this_deployment_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _apps() == {}

    configure_apps(monkeypatch, "github")
    assert list(_apps()) == ["github"]

    configure_apps(monkeypatch)
    apps = _apps()
    # Config order, each with exactly the fields Settings draws.
    assert list(apps) == ["google_workspace", "github"]
    assert all(set(a) == APP_KEYS for a in apps.values())
    google = apps["google_workspace"]
    assert google["display_name"] == "Google" and google["icon"] == "google"
    assert google["services"] == [
        "Calendar",
        "Gmail",
        "Drive",
        "Docs",
        "Sheets",
        "Slides",
    ]
    assert not google["connected"] and google["account_label"] == ""
    assert google["needs_grant"] is False and google["manage_url"] is None


def test_config_shows_a_connected_apps_account_and_where_to_manage_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.auth.providers import require_provider

    configure_apps(monkeypatch)
    monkeypatch.setenv("GITHUB_ACCESS_TOKEN", "ghu-mine")
    monkeypatch.setenv("GITHUB_ACCOUNT", "@octo")
    github = _apps()["github"]
    assert github["connected"] and github["account_label"] == "@octo"
    assert github["manage_url"] == (
        "https://github.com/apps/voice-agent-test/installations/new"
    )
    assert github["needs_grant"] is False

    # A Google grant that lacks a scope the app now asks for needs Grant access.
    scopes = require_provider("google_workspace").scopes
    monkeypatch.setenv("GOOGLE_WORKSPACE_REFRESH_TOKEN", "refresh-xyz")
    monkeypatch.setenv("GOOGLE_WORKSPACE_GRANTED_SCOPES", " ".join(scopes))
    assert _apps()["google_workspace"]["needs_grant"] is False
    monkeypatch.setenv("GOOGLE_WORKSPACE_GRANTED_SCOPES", " ".join(scopes[:-1]))
    google = _apps()["google_workspace"]
    assert google["connected"] and google["needs_grant"] is True


def test_config_reports_connections_per_user_on_a_deployment(
    monkeypatch: pytest.MonkeyPatch, iap_user: dict[str, str]
) -> None:
    from app.auth.credential_store import get_credential_store

    configure_apps(monkeypatch)
    monkeypatch.setenv("K_SERVICE", "adk-voice-agent")
    monkeypatch.setenv("APP_URL", "https://adk-voice-agent-prod.a.run.app")
    assert _apps(**iap_user)["google_workspace"]["connected"] is False
    get_credential_store().set_many(
        "user@example.com",
        {"GOOGLE_WORKSPACE_REFRESH_TOKEN": "refresh-token-xyz"},
    )
    assert _apps(**iap_user)["google_workspace"]["connected"] is True


# ---- async state, preferences, harness, and thread steering routes ------------


@pytest.mark.asyncio
async def test_preferences_are_applied_and_persisted(api: httpx.AsyncClient):
    from app.auth.env_store import dotenv_path
    from app.user_settings import reset_user_settings_cache

    res = await api.patch(
        "/api/v1/preferences",
        json={
            "timezone": " UTC ",
            "voice_name": "Kore",
            "save_to_drive": "Off",
        },
    )
    prefs = res.json()["preferences"]
    assert res.status_code == 200 and res.json()["ok"] is True
    assert (prefs["timezone"], prefs["voice_name"], prefs["save_to_drive"]) == (
        "UTC",
        "Kore",
        "off",
    )
    # Saved per user in the thread database; `.env` holds deployment defaults only.
    assert "USER_TIMEZONE" not in dotenv_values(dotenv_path())
    reset_user_settings_cache()
    config = (await api.get("/api/v1/config")).json()
    assert config["preferences"]["voice_name"] == "Kore"

    unchanged = (
        await api.patch("/api/v1/preferences", json={"timezone": "  "})
    ).json()
    assert unchanged["preferences"]["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_voice_mode_round_trips_through_the_preferences_api(
    api: httpx.AsyncClient,
):
    from app.user_settings import reset_user_settings_cache

    before = (await api.get("/api/v1/config")).json()["preferences"]
    assert before["voice_mode"] == "native"
    assert before["available_voice_modes"] == ["native", "cascaded"]

    res = await api.patch(
        "/api/v1/preferences", json={"voice_mode": " Cascaded "}
    )
    assert res.status_code == 200
    assert res.json()["preferences"]["voice_mode"] == "cascaded"
    reset_user_settings_cache()
    config = (await api.get("/api/v1/config")).json()
    assert config["preferences"]["voice_mode"] == "cascaded"

    refused = await api.patch(
        "/api/v1/preferences", json={"voice_mode": "robot"}
    )
    assert refused.status_code == 400 and "native, cascaded" in refused.text


@pytest.mark.asyncio
async def test_default_harness_selection(api: httpx.AsyncClient):
    from app.auth.env_store import dotenv_path

    res = await api.post("/api/v1/harnesses/default", json={"harness": "agy"})
    assert res.status_code == 200
    # The user's choice, not a deployment-wide `.env` change.
    assert "CODING_HARNESS" not in dotenv_values(dotenv_path())
    assert res.json()["default_harness"] == {
        "name": "antigravity",
        "display_name": "Antigravity",
        "icon": "antigravity",
    }
    config = (await api.get("/api/v1/config")).json()
    assert [h["name"] for h in config["harnesses"] if h["is_default"]] == [
        "antigravity"
    ]
    # In registry order, each with the icon key the UI draws.
    assert [(h["name"], h["icon"]) for h in config["harnesses"]] == [
        ("claude", "claude"),
        ("antigravity", "antigravity"),
    ]

    missing = await api.post(
        "/api/v1/harnesses/default", json={"harness": "nope"}
    )
    assert (
        missing.status_code == 404 and "Unknown coding harness" in missing.text
    )


@pytest.mark.asyncio
async def test_state_is_cheap_enough_to_poll(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    import subprocess

    def no_subprocess(*args, **kwargs):
        raise AssertionError(f"/state ran a subprocess: {args!r}")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    res = await api.get("/api/v1/state")
    assert res.status_code == 200
    assert set(res.json()) == {
        "threads",
        "a2ui_surfaces",
        "computer",
    }


@pytest.mark.asyncio
async def test_state_reports_the_computer_as_last_known(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    from app.workers.providers import registry as providers_registry
    from app.workers.providers.base import ComputerState
    from app.workers.providers.last_known import record_status

    def no_provider() -> None:
        raise AssertionError("/state asked the computer provider")

    monkeypatch.setattr(
        providers_registry, "get_computer_provider", no_provider
    )
    state = (await api.get("/api/v1/state")).json()
    assert state["computer"] == {"state": None, "detail": ""}

    record_status("local", ComputerState.STOPPED)
    state = (await api.get("/api/v1/state")).json()
    assert state["computer"] == {"state": "stopped", "detail": ""}

    record_status("someone-else@example.com", ComputerState.ERROR, "no quota")
    record_status("local", ComputerState.ERROR, "the boot disk is full")
    state = (await api.get("/api/v1/state")).json()
    assert state["computer"] == {
        "state": "error",
        "detail": "the boot disk is full",
    }


@pytest.mark.asyncio
async def test_config_lists_harnesses_apps_and_preferences(
    api: httpx.AsyncClient,
):
    config = (await api.get("/api/v1/config")).json()
    assert {
        "harnesses",
        "apps",
        "is_remote",
        "preferences",
    } <= set(config)
    assert sum(h["is_default"] for h in config["harnesses"]) == 1


@pytest.mark.asyncio
async def test_state_snapshot_and_context_card_dismissal(
    api: httpx.AsyncClient,
):
    record_context_card(
        kind="calendar",
        title="Today",
        subtitle="",
        brand_icon="google",
        bullets=["Standup"],
        surface_id="a2ui-ctx-test",
    )
    state = (await api.get("/api/v1/state")).json()
    assert any(
        s["surfaceId"] == "a2ui-ctx-test" for s in state["a2ui_surfaces"]
    )

    res = await api.post(
        "/api/v1/a2ui/dismiss", json={"surface_id": "a2ui-ctx-test"}
    )
    assert res.json() == {"ok": True}
    assert not any(
        c["surfaceId"] == "a2ui-ctx-test" for c in active_context_cards()
    )


@pytest.mark.asyncio
async def test_steering_over_the_api_continues_the_thread_and_switches_harness(
    api: httpx.AsyncClient, lab: Lab
):
    lab.agent.script(
        {},
        {"result": "Used f-strings"},
        {"result": "Used pathlib"},
    )
    created = await api.post(
        "/api/v1/threads", json={"goal": GOAL, "repo": REPO}
    )
    assert created.status_code == 200
    thread_id = created.json()["thread_id"]
    await lab.settle(thread_id)

    steered = await api.post(
        f"/api/v1/threads/{thread_id}/message",
        json={"instruction": "Use f-strings"},
    )
    assert steered.status_code == 200
    await lab.settle(thread_id)
    detail = (await api.get(f"/api/v1/threads/{thread_id}")).json()
    assert detail["state"] == "completed"
    assert detail["result_text"] == "Used f-strings"

    switched = await api.post(
        f"/api/v1/threads/{thread_id}/message",
        json={"instruction": "Prefer pathlib", "harness": "antigravity"},
    )
    assert (
        switched.status_code == 200
        and switched.json()["harness"] == "antigravity"
    )
    await lab.settle(thread_id)
    assert lab.agent.calls[-1]["dialect"] == "agy"
