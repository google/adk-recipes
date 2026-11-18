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

"""Computer daemon (`sandbox/server.py`): caller auth, files, activity, reattach, and MCP relay."""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from google.oauth2 import id_token

from app.workers import runs
from app.workers.environment import (
    Environment,
    LocalEnvironment,
    SandboxEnvironment,
)
from sandbox import server as daemon_server
from tests.fakes import wait_until

AUDIENCE = "https://computer.test"
SYSTEM_ONE = "voice-agent@proj.iam.gserviceaccount.com"
UPSTREAM = "http://bridge.test"
RELAY_AUDIENCE = "https://voice-agent-123.us-central1.run.app/*"


# ---- 1. Daemon caller auth (COMPUTER_REQUIRE_AUTH=1) --------------------------


@pytest.fixture
def verifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace Google's verifier: token `<email>` is valid for that email."""
    monkeypatch.setattr(daemon_server, "WORKSPACE", str(tmp_path))
    monkeypatch.setattr(daemon_server, "_verified_tokens", {})
    calls: list[str] = []

    def verify(
        token: str, request: Any, audience: str | None = None
    ) -> dict[str, Any]:
        calls.append(token)
        if audience != AUDIENCE or "@" not in token:
            raise ValueError("Token has wrong audience or is malformed")
        return {
            "email": token,
            "email_verified": True,
            "exp": time.time() + 3600,
        }

    monkeypatch.setattr(id_token, "verify_oauth2_token", verify)
    return calls


def _client(token: str = "") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=daemon_server.app),
        base_url=AUDIENCE,
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )


def _require_auth(
    monkeypatch: pytest.MonkeyPatch, *, allowed: str = SYSTEM_ONE
) -> None:
    monkeypatch.setenv("COMPUTER_REQUIRE_AUTH", "1")
    monkeypatch.setenv("COMPUTER_AUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("COMPUTER_ALLOWED_CALLERS", f"other@x.test, {allowed}")


@pytest.mark.asyncio
async def test_auth_is_off_by_default(
    verifier: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COMPUTER_REQUIRE_AUTH", raising=False)
    async with _client() as client:
        assert (await client.get("/activity")).status_code == 200
    assert verifier == []


@pytest.mark.asyncio
async def test_only_allowed_callers_get_through(
    verifier: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_auth(monkeypatch)
    async with _client() as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/activity")).status_code == 401
        assert (
            await client.post("/exec", json={"command": "echo hi"})
        ).status_code == 401
    async with _client("not-a-token") as client:
        resp = await client.get("/activity")
        assert resp.status_code == 401 and "Invalid ID token" in resp.text
    async with _client("intruder@evil.test") as client:
        resp = await client.get("/files/list")
        assert resp.status_code == 401 and "not allowed" in resp.text
    async with _client(SYSTEM_ONE) as client:
        assert (await client.get("/activity")).status_code == 200
        assert (await client.get("/activity")).status_code == 200
    assert verifier.count(SYSTEM_ONE) == 1

    # A token verified for one owner's computer audience is rejected on another owner's computer.
    monkeypatch.setenv("COMPUTER_AUTH_AUDIENCE", f"{AUDIENCE}/u-other")
    async with _client(SYSTEM_ONE) as client:
        assert (await client.get("/activity")).status_code == 401


@pytest.mark.asyncio
async def test_auth_fails_closed_without_audience_or_callers(
    verifier: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_auth(monkeypatch)
    monkeypatch.delenv("COMPUTER_AUTH_AUDIENCE")
    async with _client(SYSTEM_ONE) as client:
        assert (await client.get("/activity")).status_code == 401
    _require_auth(monkeypatch)
    monkeypatch.setenv("COMPUTER_ALLOWED_CALLERS", " ")
    async with _client(SYSTEM_ONE) as client:
        assert (await client.get("/activity")).status_code == 401
    assert verifier == []


@pytest.mark.asyncio
async def test_unverified_emails_are_refused(
    verifier: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_auth(monkeypatch)
    monkeypatch.setattr(
        id_token,
        "verify_oauth2_token",
        lambda token, request, audience=None: {"email": SYSTEM_ONE},
    )
    async with _client(SYSTEM_ONE) as client:
        assert (await client.get("/activity")).status_code == 401


@pytest.mark.asyncio
async def test_the_environment_sends_its_bearer_token(
    verifier: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _require_auth(monkeypatch)
    env = SandboxEnvironment(
        url=AUDIENCE,
        access_token=SYSTEM_ONE,
        workspace_root=str(tmp_path),
        transport=httpx.ASGITransport(app=daemon_server.app),
    )
    await env.write_file("hello.txt", "hi")
    assert (await env.read_file("hello.txt")).content == "hi"
    assert (await env.exec("echo ok")).stdout.strip() == "ok"

    anonymous = SandboxEnvironment(
        url=AUDIENCE,
        workspace_root=str(tmp_path),
        transport=httpx.ASGITransport(app=daemon_server.app),
    )
    with pytest.raises(RuntimeError, match="HTTP 401"):
        await anonymous.activity()
    assert (await anonymous.exec("echo ok")).error


# ---- 2. File & activity access (/files/*, /activity) -------------------------


def _daemon(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> SandboxEnvironment:
    monkeypatch.setattr(daemon_server, "WORKSPACE", str(workspace))
    monkeypatch.delenv("COMPUTER_REQUIRE_AUTH", raising=False)
    daemon_server._active_runs.clear()
    return SandboxEnvironment(
        name="cloud-computer-test",
        url="https://computer.test",
        workspace_root=str(workspace),
        transport=httpx.ASGITransport(app=daemon_server.app),
    )


@pytest.fixture(params=["daemon", "local"])
def env(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Environment:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("secret")
    if request.param == "daemon":
        return _daemon(workspace, monkeypatch)
    return LocalEnvironment(workspace)


@pytest.mark.asyncio
async def test_write_read_and_list_round_trip(env: Environment) -> None:
    root = Path(env.workspace_root)
    await env.write_file("notes/today/plan.md", "# Plan\n")
    await env.write_file("token", "s3cret", mode="0600")

    assert (root / "notes/today/plan.md").read_text() == "# Plan\n"
    assert (root / "token").stat().st_mode & 0o777 == 0o600
    assert (await env.read_file("notes/today/plan.md")).content == "# Plan\n"

    listing = await env.list_files()
    assert [(e.name, e.type) for e in listing] == [
        ("notes", "dir"),
        ("token", "file"),
    ]
    token = listing[1]
    assert token.size == len("s3cret")
    assert datetime.fromisoformat(token.mtime).tzinfo is not None
    assert [e.name for e in await env.list_files("notes/today")] == ["plan.md"]


@pytest.mark.asyncio
async def test_writes_are_atomic_and_keep_an_existing_mode(
    env: Environment,
) -> None:
    root = Path(env.workspace_root)
    await env.write_file("cfg.json", "{}", mode="0640")
    await env.write_file("cfg.json", '{"a": 1}')

    assert (root / "cfg.json").read_text() == '{"a": 1}'
    assert (root / "cfg.json").stat().st_mode & 0o777 == 0o640
    assert os.listdir(root) == ["cfg.json"]


@pytest.mark.asyncio
async def test_reads_are_capped_and_decode_with_replacement(
    env: Environment,
) -> None:
    root = Path(env.workspace_root)
    (root / "big.txt").write_text("x" * 100)
    (root / "bin.dat").write_bytes(b"ok\xff\xfe")

    capped = await env.read_file("big.txt", max_bytes=10)
    assert capped.content == "x" * 10 and capped.truncated
    whole = await env.read_file("big.txt")
    assert len(whole.content) == 100 and not whole.truncated
    assert (await env.read_file("bin.dat")).content.startswith("ok\ufffd")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["../outside/secret.txt", "a/../../outside/secret.txt", "/etc/passwd"],
)
async def test_paths_outside_the_root_are_rejected(
    env: Environment, path: str
) -> None:
    with pytest.raises(ValueError):
        await env.read_file(path)
    with pytest.raises(ValueError):
        await env.write_file(path, "x")
    with pytest.raises(ValueError):
        await env.list_files(path)


@pytest.mark.asyncio
async def test_symlinks_cannot_escape_the_root(
    env: Environment, tmp_path: Path
) -> None:
    root = Path(env.workspace_root)
    (root / "link").symlink_to(tmp_path / "outside")
    (root / "secret-link").symlink_to(tmp_path / "outside" / "secret.txt")
    (root / "inside.txt").write_text("fine")
    (root / "inside-link").symlink_to(root / "inside.txt")

    for path in ("link/secret.txt", "secret-link"):
        with pytest.raises(ValueError):
            await env.read_file(path)
    with pytest.raises(ValueError):
        await env.write_file("link/new.txt", "x")
    assert not (tmp_path / "outside" / "new.txt").exists()
    with pytest.raises(ValueError):
        await env.list_files("link")

    assert (await env.read_file("inside-link")).content == "fine"
    assert [e.name for e in await env.list_files()] == [
        "inside-link",
        "inside.txt",
    ]


@pytest.mark.asyncio
async def test_root_must_be_inside_the_workspace(
    env: Environment, tmp_path: Path
) -> None:
    root = Path(env.workspace_root)
    (root / "alice").mkdir()
    await env.write_file("a.txt", "hi", root=str(root / "alice"))
    assert (root / "alice" / "a.txt").read_text() == "hi"
    assert [e.name for e in await env.list_files(root=str(root / "alice"))] == [
        "a.txt"
    ]

    with pytest.raises(ValueError):
        await env.read_file("secret.txt", root=str(tmp_path / "outside"))
    with pytest.raises(ValueError):
        await env.list_files(root="relative/root")


@pytest.mark.asyncio
async def test_missing_paths_bad_modes_and_directories(
    env: Environment,
) -> None:
    root = Path(env.workspace_root)
    (root / "dir").mkdir()
    with pytest.raises(FileNotFoundError):
        await env.read_file("nope.txt")
    with pytest.raises(FileNotFoundError):
        await env.list_files("nope")
    with pytest.raises(ValueError):
        await env.write_file("x.txt", "x", mode="rw-r--r--")
    with pytest.raises(ValueError):
        await env.write_file("x.txt", "x", mode="07777")
    with pytest.raises(ValueError):
        await env.read_file("dir")
    with pytest.raises(ValueError):
        await env.write_file("dir", "x")
    with pytest.raises(ValueError):
        await env.read_file("")


@pytest.mark.asyncio
async def test_activity_counts_running_runs_and_advances(
    env: Environment,
) -> None:
    root = Path(env.workspace_root)
    before = await env.activity()
    assert before.active_runs == 0
    start = datetime.fromisoformat(before.last_activity)

    gate = root / "go"
    handle = await runs.launch(
        env,
        run_id="act-1",
        argv=[
            "python3",
            "-c",
            f"import os, time\nwhile not os.path.exists({str(gate)!r}): time.sleep(0.05)",
        ],
        cwd=str(root),
    )

    async def active(count: int) -> bool:
        return (await env.activity()).active_runs == count

    await wait_until(
        lambda: active(1),
        timeout_s=10,
        interval_s=0.05,
        message="an active run",
    )
    await env.write_file("go", "")
    await wait_until(
        lambda: active(0),
        timeout_s=10,
        interval_s=0.05,
        message="the run to end",
    )
    after = await env.activity()
    assert datetime.fromisoformat(after.last_activity) >= start
    assert (await runs.poll(env, handle, runs.RunCursor())).exit_code == 0


@pytest.mark.asyncio
async def test_daemon_file_errors_are_http_400_and_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _daemon(tmp_path, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=daemon_server.app),
        base_url="http://computer.test",
    ) as client:
        for params in (
            {"path": "../etc/passwd"},
            {"path": "/etc/passwd"},
            {"path": "x", "root": "/"},
        ):
            assert (
                await client.get("/files/read", params=params)
            ).status_code == 400
        assert (
            await client.put(
                "/files/write", json={"path": "../x", "content": ""}
            )
        ).status_code == 400
        assert (
            await client.get("/files/read", params={"path": "missing"})
        ).status_code == 404
        written = await client.put(
            "/files/write",
            json={"path": "a/b.txt", "content": "hi", "mode": "0600"},
        )
        assert written.json() == {"ok": True}
        listing = (await client.get("/files/list")).json()
        assert listing["entries"][0]["name"] == "a"
        assert listing["entries"][0]["type"] == "dir"
        assert (await client.get("/activity")).json()["active_runs"] == 0


def _fake_proc(root: Path, processes: dict[int, tuple[int, int]]) -> None:
    (root / "self").mkdir(parents=True)
    for pid, (session, tty) in processes.items():
        (root / str(pid)).mkdir()
        (root / str(pid) / "stat").write_text(
            f"{pid} (bash -l) S 0 {session} {session} {tty} {pid} 4194560 0 0\n"
        )


@pytest.mark.asyncio
async def test_an_open_shell_counts_as_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _daemon(tmp_path / "ws", monkeypatch)
    proc = tmp_path / "proc"
    _fake_proc(proc, {1: (1, 0), 40: (40, 0), 77: (77, 34816), 78: (77, 34816)})
    monkeypatch.setattr(daemon_server, "PROC_ROOT", str(proc))
    assert daemon_server._interactive_sessions() == 1

    monkeypatch.setattr(daemon_server._activity, "last", 0.0)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=daemon_server.app),
        base_url="http://computer.test",
    ) as client:
        body = (await client.get("/activity")).json()
    assert body["active_runs"] == 0
    assert datetime.fromisoformat(body["last_activity"]).timestamp() > 0


@pytest.mark.asyncio
async def test_no_shell_leaves_activity_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _daemon(tmp_path / "ws", monkeypatch)
    proc = tmp_path / "proc"
    _fake_proc(proc, {1: (1, 0), 77: (77, 34816)})
    monkeypatch.setattr(daemon_server, "PROC_ROOT", str(proc))
    monkeypatch.setattr(os, "getuid", lambda: os.stat(proc).st_uid + 1)
    assert daemon_server._interactive_sessions() == 0
    monkeypatch.setattr(daemon_server, "PROC_ROOT", str(tmp_path / "missing"))
    assert daemon_server._interactive_sessions() == 0

    monkeypatch.setattr(daemon_server._activity, "last", 0.0)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=daemon_server.app),
        base_url="http://computer.test",
    ) as client:
        body = (await client.get("/activity")).json()
    assert datetime.fromisoformat(body["last_activity"]).timestamp() == 0


# ---- 3. Reattach after System 1 restart ---------------------------------------


def _daemon_client(workspace: Path) -> SandboxEnvironment:
    return SandboxEnvironment(
        url="https://computer.test",
        workspace_root=str(workspace),
        transport=httpx.ASGITransport(app=daemon_server.app),
    )


@pytest.fixture(params=["daemon", "local"])
def new_client(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[], Environment]:
    if request.param == "daemon":
        monkeypatch.setattr(daemon_server, "WORKSPACE", str(tmp_path))
        monkeypatch.delenv("COMPUTER_REQUIRE_AUTH", raising=False)
        daemon_server._active_runs.clear()
        return lambda: _daemon_client(tmp_path)
    return lambda: LocalEnvironment(tmp_path)


def _script(gate: Path) -> str:
    return (
        "import os, time\n"
        "print('{\"n\": 1}', flush=True)\n"
        "print('{\"n\": 2}', flush=True)\n"
        f"while not os.path.exists({str(gate)!r}): time.sleep(0.05)\n"
        "print('{\"n\": 3}', flush=True)\n"
    )


async def _read_until(
    env: Environment, handle: runs.RunHandle, cursor: runs.RunCursor, count: int
) -> list[str]:
    lines: list[str] = []

    async def enough() -> bool:
        lines.extend((await runs.poll(env, handle, cursor)).lines)
        return len(lines) >= count

    await wait_until(
        enough, timeout_s=10, interval_s=0.05, message=f"{count} lines"
    )
    return lines


@pytest.mark.asyncio
async def test_a_new_client_resumes_polling_from_the_saved_cursor(
    tmp_path: Path, new_client: Callable[[], Environment]
) -> None:
    gate = tmp_path / "go"
    first = new_client()
    handle = await runs.launch(
        first,
        run_id="thread-1-r1",
        argv=["python3", "-c", _script(gate)],
        cwd=str(tmp_path),
    )
    cursor = runs.RunCursor()
    assert await _read_until(first, handle, cursor, 2) == [
        '{"n": 1}',
        '{"n": 2}',
    ]

    saved = (
        handle.run_id,
        handle.run_dir,
        cursor.offset,
        cursor.skipping_long_line,
    )
    del first, handle, cursor

    second = new_client()
    run_id, run_dir, offset, skipping = saved
    handle = runs.RunHandle(run_id=run_id, run_dir=run_dir)
    cursor = runs.RunCursor(offset=offset, skipping_long_line=skipping)
    still_running = await runs.poll(second, handle, cursor)
    assert still_running.exit_code is None and not still_running.lost
    assert still_running.lines == []

    gate.write_text("")
    lines: list[str] = []
    exit_codes: list[int] = []

    async def finished() -> bool:
        polled = await runs.poll(second, handle, cursor)
        lines.extend(polled.lines)
        if polled.exit_code is not None and polled.bytes_read == 0:
            exit_codes.append(polled.exit_code)
        return bool(exit_codes)

    await wait_until(
        finished, timeout_s=10, interval_s=0.05, message="the run to end"
    )
    assert lines == ['{"n": 3}'] and exit_codes == [0]


# ---- 4. MCP relay (/mcp-relay/{path}) ----------------------------------------


def _fake_bridge(seen: list[dict[str, Any]]) -> FastAPI:
    bridge = FastAPI()

    @bridge.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
    async def anything(path: str, request: Request) -> Any:
        seen.append(
            {
                "method": request.method,
                "path": path,
                "query": request.url.query,
                "headers": dict(request.headers),
                "body": await request.body(),
            }
        )
        if path.endswith("sse"):

            async def events() -> AsyncIterator[bytes]:
                yield b"event: message\ndata: one\n\n"
                yield b"event: message\ndata: two\n\n"

            return StreamingResponse(events(), media_type="text/event-stream")
        return JSONResponse(
            {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
            headers={"mcp-session-id": "s-1"},
        )

    return bridge


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    monkeypatch.setenv("COMPUTER_MCP_UPSTREAM", UPSTREAM + "/")
    monkeypatch.delenv("COMPUTER_MCP_AUDIENCE", raising=False)
    monkeypatch.setattr(daemon_server, "_proxy_tokens", {})
    monkeypatch.setattr(
        daemon_server,
        "_relay_client",
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_fake_bridge(seen))
        ),
    )
    return seen


def _harness(client_host: str = "127.0.0.1") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=daemon_server.app, client=(client_host, 40000)
        ),
        base_url="http://127.0.0.1:8080",
    )


def _jwt(exp: float) -> str:
    claims = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()
    ).decode()
    return f"h.{claims.rstrip('=')}.sig"


@pytest.mark.asyncio
async def test_relays_requests_and_keeps_the_thread_token(
    bridge: list[dict[str, Any]],
) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    async with _harness() as client:
        resp = await client.post(
            "/mcp-relay/api/v1/mcp?x=1",
            json=body,
            headers={"Authorization": "Bearer thread-token"},
        )
    assert resp.status_code == 200
    assert resp.json()["result"] == {"ok": True}
    assert resp.headers["mcp-session-id"] == "s-1"
    [call] = bridge
    assert call["method"] == "POST"
    assert call["path"] == "api/v1/mcp" and call["query"] == "x=1"
    assert json.loads(call["body"]) == body
    assert call["headers"]["authorization"] == "Bearer thread-token"
    assert "proxy-authorization" not in call["headers"]


@pytest.mark.asyncio
async def test_streams_server_sent_events(bridge: list[dict[str, Any]]) -> None:
    async with _harness() as client:
        async with client.stream("GET", "/mcp-relay/api/v1/mcp/sse") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            text = b"".join(
                [chunk async for chunk in resp.aiter_raw()]
            ).decode()
    assert "data: one" in text and "data: two" in text
    assert bridge[0]["method"] == "GET" and bridge[0]["body"] == b""


@pytest.mark.asyncio
async def test_adds_a_cached_proxy_jwt_for_the_bridge_audience(
    bridge: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPUTER_MCP_AUDIENCE", RELAY_AUDIENCE)
    minted: list[str] = []

    def fetch(audience: str) -> str:
        minted.append(audience)
        return _jwt(time.time() + 3600)

    monkeypatch.setattr(daemon_server, "_fetch_proxy_token", fetch)
    async with _harness() as client:
        for _ in range(2):
            await client.post(
                "/mcp-relay/api/v1/mcp",
                json={},
                headers={
                    "Authorization": "Bearer thread-token",
                    "Proxy-Authorization": "Bearer forged",
                },
            )
    assert minted == [RELAY_AUDIENCE]
    token = daemon_server._proxy_tokens[RELAY_AUDIENCE][0]
    for call in bridge:
        assert call["headers"]["proxy-authorization"] == f"Bearer {token}"
        assert call["headers"]["authorization"] == "Bearer thread-token"


@pytest.mark.asyncio
async def test_signs_a_new_proxy_jwt_shortly_before_it_expires(
    bridge: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPUTER_MCP_AUDIENCE", RELAY_AUDIENCE)
    tokens = iter([_jwt(time.time() + 60), _jwt(time.time() + 3600)])
    monkeypatch.setattr(
        daemon_server, "_fetch_proxy_token", lambda _a: next(tokens)
    )
    async with _harness() as client:
        await client.post("/mcp-relay/api/v1/mcp", json={})
        await client.post("/mcp-relay/api/v1/mcp", json={})
    first, second = (c["headers"]["proxy-authorization"] for c in bridge)
    assert first != second


@pytest.mark.asyncio
async def test_refuses_clients_off_this_computer(
    bridge: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPUTER_REQUIRE_AUTH", "1")
    async with _harness("10.0.0.7") as client:
        resp = await client.post("/mcp-relay/api/v1/mcp", json={})
    assert resp.status_code == 403
    async with _harness("::1") as client:
        assert (
            await client.post("/mcp-relay/api/v1/mcp", json={})
        ).status_code == 200
    assert len(bridge) == 1


@pytest.mark.asyncio
async def test_relay_rejects_non_mcp_paths_and_traversal(
    bridge: list[dict[str, Any]],
) -> None:
    async with _harness() as client:
        for bad in (
            "/mcp-relay/api/v1/threads/t1/approve",
            "/mcp-relay/api/v1/state",
            "/mcp-relay/mcp",
            "/mcp-relay/api/v1/mcp/../threads/t1/approve",
            "/mcp-relay/api/v1/mcp/%2e%2e/threads/t1/approve",
            "/mcp-relay/api/v1/mcp%2f..%2fthreads",
            "/mcp-relay//api/v1/mcp",
            "/mcp-relay/api/v1/mcp\\..\\threads",
        ):
            resp = await client.post(bad, json={})
            assert resp.status_code == 403, bad
    assert bridge == []


@pytest.mark.asyncio
async def test_reports_a_missing_or_unreachable_upstream(
    bridge: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COMPUTER_MCP_UPSTREAM")
    async with _harness() as client:
        assert (
            await client.post("/mcp-relay/api/v1/mcp", json={})
        ).status_code == 503

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setenv("COMPUTER_MCP_UPSTREAM", UPSTREAM)
    monkeypatch.setattr(
        daemon_server,
        "_relay_client",
        httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )
    async with _harness() as client:
        resp = await client.post("/mcp-relay/api/v1/mcp", json={})
    assert resp.status_code == 502 and "unreachable" in resp.text


def test_the_proxy_jwt_is_signed_by_the_computers_service_account() -> None:
    sa = "computer@proj.iam.gserviceaccount.com"
    calls: list[httpx.Request] = []

    def google(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "metadata.google.internal":
            assert request.headers["metadata-flavor"] == "Google"
            if request.url.path.endswith("/email"):
                return httpx.Response(200, text=sa)
            return httpx.Response(
                200, json={"access_token": "fake-access-token-1"}
            )
        return httpx.Response(
            200, json={"keyId": "k", "signedJwt": "signed.jwt.value"}
        )

    token = daemon_server._fetch_proxy_token(
        RELAY_AUDIENCE, httpx.Client(transport=httpx.MockTransport(google))
    )

    assert token == "signed.jwt.value"
    sign = calls[-1]
    assert str(sign.url) == (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        f"{sa}:signJwt"
    )
    assert sign.headers["authorization"] == "Bearer fake-access-token-1"
    claims = json.loads(json.loads(sign.content)["payload"])
    assert claims["iss"] == claims["sub"] == sa
    assert claims["aud"] == RELAY_AUDIENCE
    assert claims["exp"] - claims["iat"] == 3600
