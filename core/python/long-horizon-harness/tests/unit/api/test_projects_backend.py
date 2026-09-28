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

"""Projects backend (Task 1): workspace-folder mkdir + session-window
read/write. Pure unit tests — InMemorySessionService + a tmp-rooted
LocalEnvironment, no Vertex, no network.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.sessions import InMemorySessionService

from app.api.sessions import attach_session_routes
from app.api.uploads import attach_uploads_routes
from app.auth import current_user_id
from app.conversation import session_start
from app.environment import LocalEnvironment
from app.infrastructure.constants import APP_NAME, TITLE_KEY
from app.workspace_window import WORKSPACE_WINDOW_STATE_KEY

USER_ID = "projects-unit@local"


@dataclass
class _Runner:
    session_service: InMemorySessionService


def _row(rows: list[dict], session_id: str) -> dict:
    match = [r for r in rows if r["id"] == session_id]
    assert match, f"session {session_id} not in list: {[r['id'] for r in rows]}"
    return match[0]


# --------------------------------------------------------------------------
# workspace folder (mkdir)
# --------------------------------------------------------------------------


@pytest.fixture
def uploads_client(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, object]:
    env = LocalEnvironment(working_dir=tmp_path)

    async def _fake_ensure(_user_id: str) -> LocalEnvironment:
        return env

    monkeypatch.setattr(session_start, "_ensure_environment", _fake_ensure)
    app = FastAPI()
    attach_uploads_routes(app)
    app.dependency_overrides[current_user_id] = lambda: USER_ID
    return TestClient(app), tmp_path


def test_create_folder_happy(uploads_client) -> None:
    client, tmp_path = uploads_client
    resp = client.post("/lha/workspace/folder", json={"name": "projA"})
    assert resp.status_code == 200
    assert resp.json()["path"] == "/workspace/projA"
    assert (tmp_path / "projA").is_dir()


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b", "nested/../x"])
def test_create_folder_rejects_bad_names(uploads_client, name: str) -> None:
    client, _ = uploads_client
    resp = client.post("/lha/workspace/folder", json={"name": name})
    assert resp.status_code == 400


def test_create_folder_is_idempotent(uploads_client) -> None:
    client, tmp_path = uploads_client
    first = client.post("/lha/workspace/folder", json={"name": "projA"})
    second = client.post("/lha/workspace/folder", json={"name": "projA"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert (tmp_path / "projA").is_dir()


# --------------------------------------------------------------------------
# session window (read via list, write via PUT)
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sessions_client() -> TestClient:
    svc = InMemorySessionService()
    await svc.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id="s-anchored",
        state={TITLE_KEY: "a", WORKSPACE_WINDOW_STATE_KEY: ["projA"]},
    )
    await svc.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id="s-general",
        state={TITLE_KEY: "b"},
    )
    app = FastAPI()
    attach_session_routes(app, runner=_Runner(session_service=svc))
    app.dependency_overrides[current_user_id] = lambda: USER_ID
    return TestClient(app)


def test_list_rows_include_workspace_window(
    sessions_client: TestClient,
) -> None:
    rows = sessions_client.get("/lha/sessions?source=user").json()
    assert _row(rows, "s-anchored")["workspaceWindow"] == ["projA"]
    assert _row(rows, "s-general")["workspaceWindow"] == []


def test_put_window_creates_missing_session(
    sessions_client: TestClient,
) -> None:
    resp = sessions_client.put(
        "/lha/sessions/brand-new/window", json={"dirs": ["projB"]}
    )
    assert resp.status_code == 204
    rows = sessions_client.get("/lha/sessions?source=user").json()
    assert _row(rows, "brand-new")["workspaceWindow"] == ["projB"]


def test_put_window_updates_existing_session(
    sessions_client: TestClient,
) -> None:
    resp = sessions_client.put(
        "/lha/sessions/s-anchored/window", json={"dirs": ["projZ"]}
    )
    assert resp.status_code == 204
    rows = sessions_client.get("/lha/sessions?source=user").json()
    assert _row(rows, "s-anchored")["workspaceWindow"] == ["projZ"]


def test_put_window_cleans_input(sessions_client: TestClient) -> None:
    resp = sessions_client.put(
        "/lha/sessions/s-general/window",
        json={"dirs": ["/projC/", "", ".", "x"]},
    )
    assert resp.status_code == 204
    rows = sessions_client.get("/lha/sessions?source=user").json()
    assert _row(rows, "s-general")["workspaceWindow"] == ["projC", "x"]
