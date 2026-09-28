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

"""/lha/processes router (Task 2): peek-only list + kill. Pure unit tests —
peek_environment monkeypatched to a fake env, no Vertex, no network."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.processes import attach_processes_routes
from app.auth import current_user_id
from app.conversation import session_start

USER_ID = "proc-unit@local"


class _FakeEnv:
    def __init__(self, rows, kill_result=True):
        self._rows = rows
        self._kill = kill_result
        self.killed: list[str] = []

    async def list_processes(self):
        return self._rows

    async def kill_process(self, session_id):
        self.killed.append(session_id)
        return self._kill


def _client(env, monkeypatch):
    async def _peek(_user_id):
        return env

    monkeypatch.setattr(session_start, "peek_environment", _peek)
    app = FastAPI()
    attach_processes_routes(app)
    app.dependency_overrides[current_user_id] = lambda: USER_ID
    return TestClient(app)


def _cold_client(monkeypatch):
    async def _peek(_user_id):
        return None

    monkeypatch.setattr(session_start, "peek_environment", _peek)
    app = FastAPI()
    attach_processes_routes(app)
    app.dependency_overrides[current_user_id] = lambda: USER_ID
    return TestClient(app)


def test_list_proxies_env(monkeypatch):
    rows = [
        {
            "session_id": "proc_a",
            "command": "npm run dev",
            "running": True,
            "exit_code": None,
            "idle_seconds": 2.0,
            "output_size": 10,
            "pid": 5,
            "started_at": 1.0,
        }
    ]
    client = _client(_FakeEnv(rows), monkeypatch)
    r = client.get("/lha/processes")
    assert r.status_code == 200
    assert r.json() == {"processes": rows}


def test_list_empty_when_cold(monkeypatch):
    r = _cold_client(monkeypatch).get("/lha/processes")
    assert r.status_code == 200
    assert r.json() == {"processes": []}


def test_delete_kills(monkeypatch):
    env = _FakeEnv([], kill_result=True)
    client = _client(env, monkeypatch)
    r = client.delete("/lha/processes/proc_a")
    assert r.status_code == 200
    assert r.json() == {"session_id": "proc_a", "ok": True}
    assert env.killed == ["proc_a"]


def test_delete_404_when_unknown(monkeypatch):
    client = _client(_FakeEnv([], kill_result=False), monkeypatch)
    assert client.delete("/lha/processes/nope").status_code == 404


def test_delete_404_when_cold(monkeypatch):
    assert (
        _cold_client(monkeypatch).delete("/lha/processes/x").status_code == 404
    )
