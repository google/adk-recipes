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

"""End-to-end of the container-side ``/processes/*`` REST surface.

Exercises the FastAPI shim's process endpoints with a real PTY-backed
subprocess. Mirrors the local-handle integration tests but against the
container-runtime contract that ``SandboxProcessHandle`` consumes.
"""

from __future__ import annotations

import base64
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

RUNTIME_DIR = (
    Path(__file__).resolve().parents[3] / "app" / "sandbox" / "runtime"
)


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("SANDBOX_WORKSPACE", str(tmp_path))
    monkeypatch.syspath_prepend(str(RUNTIME_DIR))
    for mod in ("server", "protocol"):
        sys.modules.pop(mod, None)
    import server  # type: ignore[import-not-found]

    server._PROCESSES.clear()
    with TestClient(server.app) as test_client:
        yield test_client
    for sid, proc in list(server._PROCESSES.items()):
        try:
            proc.kill()
        finally:
            server._PROCESSES.pop(sid, None)


def _spawn(client: TestClient, command: str, cwd: str | None = None) -> dict:
    body: dict = {"command": command}
    if cwd is not None:
        body["cwd"] = cwd
    r = client.post("/processes", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _wait_until_finished(
    client: TestClient, sid: str, deadline: float = 5.0
) -> dict:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        r = client.get(f"/processes/{sid}/status")
        assert r.status_code == 200, r.text
        body = r.json()
        if not body["is_running"]:
            return body
        time.sleep(0.05)
    raise AssertionError(f"process {sid} did not exit within {deadline}s")


class TestSpawnAndStatus:
    def test_spawn_returns_session_id_and_pid(self, client: TestClient) -> None:
        body = _spawn(client, "echo hello")
        assert body["session_id"].startswith("proc_")
        assert body["pid"] > 0

    def test_status_reports_running_then_finished(
        self, client: TestClient
    ) -> None:
        sid = _spawn(client, "sleep 0.2")["session_id"]
        r = client.get(f"/processes/{sid}/status")
        assert r.status_code == 200
        assert r.json()["is_running"] is True
        body = _wait_until_finished(client, sid)
        assert body["exit_code"] == 0
        assert body["output_size"] == 0

    def test_status_unknown_session_returns_404(
        self, client: TestClient
    ) -> None:
        r = client.get("/processes/proc_missing/status")
        assert r.status_code == 404


class TestOutput:
    def test_output_captures_stdout(self, client: TestClient) -> None:
        sid = _spawn(client, "echo marker-xyz")["session_id"]
        _wait_until_finished(client, sid)
        r = client.get(f"/processes/{sid}/output")
        assert r.status_code == 200
        body = r.json()
        decoded = base64.b64decode(body["data_b64"]).decode(
            "utf-8", errors="replace"
        )
        assert "marker-xyz" in decoded
        assert body["exit_code"] == 0
        assert body["next_offset"] == len(base64.b64decode(body["data_b64"]))

    def test_output_pagination_offset(self, client: TestClient) -> None:
        sid = _spawn(client, "printf 'first\\n'; printf 'second\\n'")[
            "session_id"
        ]
        _wait_until_finished(client, sid)
        r1 = client.get(f"/processes/{sid}/output")
        body1 = r1.json()
        full = base64.b64decode(body1["data_b64"])
        cut = max(1, len(full) // 2)
        r2 = client.get(f"/processes/{sid}/output", params={"offset": cut})
        body2 = r2.json()
        assert base64.b64decode(body2["data_b64"]) == full[cut:]


class TestKill:
    def test_kill_stops_running_process(self, client: TestClient) -> None:
        sid = _spawn(client, "sleep 30")["session_id"]
        r = client.post(f"/processes/{sid}/kill")
        assert r.status_code == 200, r.text
        body = _wait_until_finished(client, sid)
        assert body["is_running"] is False

    def test_kill_finished_process_is_idempotent(
        self, client: TestClient
    ) -> None:
        sid = _spawn(client, "true")["session_id"]
        _wait_until_finished(client, sid)
        r = client.post(f"/processes/{sid}/kill")
        assert r.status_code == 200


class TestStdin:
    def test_stdin_write_then_read(self, client: TestClient) -> None:
        spawn = _spawn(client, "read line; echo got=$line")
        sid = spawn["session_id"]
        r = client.post(
            f"/processes/{sid}/stdin",
            json={
                "data_b64": base64.b64encode(b"hi").decode("ascii"),
                "submit": True,
            },
        )
        assert r.status_code == 200, r.text
        _wait_until_finished(client, sid)
        out = client.get(f"/processes/{sid}/output").json()
        decoded = base64.b64decode(out["data_b64"]).decode(
            "utf-8", errors="replace"
        )
        assert "got=hi" in decoded

    def test_stdin_to_finished_process_returns_409(
        self, client: TestClient
    ) -> None:
        sid = _spawn(client, "true")["session_id"]
        _wait_until_finished(client, sid)
        r = client.post(
            f"/processes/{sid}/stdin",
            json={"data_b64": base64.b64encode(b"x").decode("ascii")},
        )
        assert r.status_code == 409


class TestListing:
    def test_list_includes_spawned(self, client: TestClient) -> None:
        sid1 = _spawn(client, "sleep 1")["session_id"]
        sid2 = _spawn(client, "true")["session_id"]
        _wait_until_finished(client, sid2)
        r = client.get("/processes")
        assert r.status_code == 200
        ids = {s["session_id"] for s in r.json()["sessions"]}
        assert {sid1, sid2} <= ids


class TestCwdConfinement:
    def test_cwd_outside_workspace_is_rejected(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        outside = tmp_path.parent
        r = client.post(
            "/processes",
            json={"command": "pwd", "cwd": str(outside)},
        )
        assert r.status_code == 403
