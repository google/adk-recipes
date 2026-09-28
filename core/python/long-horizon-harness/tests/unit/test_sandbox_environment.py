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

"""``SandboxEnvironment`` — ``BaseEnvironment`` backed by Agent Runtime BYOC sandbox.

Pins the contract between the env abstraction (used by ``terminal``,
``file_ops``, ``skills``) and the FastAPI shim running inside the
sandbox container. All HTTP is direct REST against the sandbox's load
balancer hostname with three headers (Authorization JWT,
X-Sandbox-Routing-Token, X-Sandbox-Port).

Tests stub ``httpx.AsyncClient`` end-to-end via ``respx`` — no GCP or
network traffic.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx

LB_HOST = "abc123.us-central1.sandbox.aiplatform.googleapis.com"
BASE_URL = f"https://{LB_HOST}"
SANDBOX_NAME = (
    "projects/p/locations/l/reasoningEngines/r/sandboxEnvironments/sb-1"
)


def _make_env(
    *,
    client: Any | None = None,
    sandbox_name: str = SANDBOX_NAME,
    owner: str = "alice",
    lb_host: str = LB_HOST,
    routing_token: str = "route-token-xyz",
    sandbox_token: str = "jwt-abc",
    port: str = "8080",
    root: Path = Path("/workspace"),
) -> Any:
    from app.environment.sandbox import SandboxEnvironment

    return SandboxEnvironment(
        client=client if client is not None else MagicMock(),
        sandbox_name=sandbox_name,
        lb_host=lb_host,
        routing_token=routing_token,
        sandbox_token=sandbox_token,
        port=port,
        owner=owner,
        root=root,
    )


# =============================================================================
# Module surface
# =============================================================================


def test_module_exposes_class() -> None:
    from app.environment.sandbox import SandboxEnvironment

    assert callable(SandboxEnvironment)


def test_working_dir_returns_root() -> None:
    env = _make_env(root=Path("/workspace"))
    # /workspace is conceptual: the path resolves inside the sandbox, not
    # on the host. Tools call it to scope file ops; the sandbox shim is
    # the only thing that actually walks it.
    assert env.working_dir == Path("/workspace")


def test_working_dir_defaults_to_workspace() -> None:
    env = _make_env()
    assert env.working_dir == Path("/workspace")


def test_sandbox_name_exposed() -> None:
    env = _make_env(sandbox_name=SANDBOX_NAME)
    assert env.sandbox_name == SANDBOX_NAME


# =============================================================================
# execute — shell command round-trip via /exec
# =============================================================================


@pytest.mark.asyncio
class TestExecute:
    async def test_posts_to_exec_with_command_and_parses_response(self) -> None:
        env = _make_env()
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post(f"{BASE_URL}/exec").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "exit_code": 0,
                        "stdout": "hi\n",
                        "stderr": "",
                        "timed_out": False,
                    },
                )
            )

            result = await env.execute("echo hi")

        assert result.exit_code == 0
        assert result.stdout == "hi\n"
        assert result.stderr == ""
        assert result.timed_out is False

        assert route.called
        sent = route.calls.last.request
        body = sent.read().decode()
        assert "echo hi" in body
        # Three headers are mandatory for the LB to route the call.
        assert sent.headers["authorization"] == "Bearer jwt-abc"
        assert sent.headers["x-sandbox-routing-token"] == "route-token-xyz"
        assert sent.headers["x-sandbox-port"] == "8080"

    async def test_passes_timeout_in_body(self) -> None:
        env = _make_env()
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post(f"{BASE_URL}/exec").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "exit_code": 0,
                        "stdout": "",
                        "stderr": "",
                        "timed_out": False,
                    },
                )
            )

            await env.execute("sleep 5", timeout=15.0)

        body = route.calls.last.request.read().decode()
        # The shim reads timeout from the JSON body to kill the subprocess.
        assert '"timeout": 15' in body or '"timeout":15' in body

    async def test_marks_timed_out_when_runtime_says_so(self) -> None:
        env = _make_env()
        with respx.mock() as mock:
            mock.post(f"{BASE_URL}/exec").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "exit_code": -1,
                        "stdout": "partial\n",
                        "stderr": "",
                        "timed_out": True,
                    },
                )
            )

            result = await env.execute("sleep 100", timeout=1.0)

        assert result.timed_out is True
        assert result.exit_code == -1
        assert result.stdout == "partial\n"

    async def test_http_5xx_surfaces_nonzero_exit(self) -> None:
        """If the shim itself errors (500), we must not crash on JSON parse —
        the env should surface a useful nonzero ExecutionResult so tools can
        log it and the agent can keep going."""
        env = _make_env()
        with respx.mock() as mock:
            mock.post(f"{BASE_URL}/exec").mock(
                return_value=httpx.Response(500, text="internal server error")
            )

            result = await env.execute("echo hi")

        assert result.exit_code != 0
        assert (
            "internal server error" in result.stderr.lower()
            or "500" in result.stderr
        )


# =============================================================================
# read_file / write_file — base64 round-trip via /files
# =============================================================================


@pytest.mark.asyncio
class TestFileIO:
    async def test_read_file_decodes_base64(self) -> None:
        env = _make_env()
        with respx.mock(assert_all_called=True) as mock:
            route = mock.get(f"{BASE_URL}/files").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "content_b64": base64.b64encode(b"hello world").decode(
                            "ascii"
                        )
                    },
                )
            )

            out = await env.read_file(Path("/workspace/hello.txt"))

        assert out == b"hello world"
        sent = route.calls.last.request
        # httpx urlencodes path values, so check the decoded query.
        assert sent.url.params.get("path") == "/workspace/hello.txt"

    async def test_read_file_handles_binary(self) -> None:
        """Non-utf8 bytes must round-trip unmangled — base64 is the only
        safe wire format."""
        env = _make_env()
        payload = bytes([0x00, 0xFF, 0x80, 0xAB])
        with respx.mock() as mock:
            mock.get(f"{BASE_URL}/files").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "content_b64": base64.b64encode(payload).decode("ascii")
                    },
                )
            )
            out = await env.read_file(Path("/workspace/bin.dat"))

        assert out == payload

    async def test_read_file_raises_file_not_found_on_404(self) -> None:
        env = _make_env()
        with respx.mock() as mock:
            mock.get(f"{BASE_URL}/files").mock(
                return_value=httpx.Response(
                    404,
                    json={"detail": "[Errno 2] No such file: '/missing.txt'"},
                )
            )

            with pytest.raises(FileNotFoundError, match=r"missing\.txt"):
                await env.read_file(Path("/workspace/missing.txt"))

    async def test_read_file_raises_permission_error_on_403(self) -> None:
        env = _make_env()
        with respx.mock() as mock:
            mock.get(f"{BASE_URL}/files").mock(
                return_value=httpx.Response(
                    403, json={"detail": "[Errno 13] Permission denied"}
                )
            )

            with pytest.raises(PermissionError):
                await env.read_file(Path("/root/forbidden"))

    async def test_write_file_posts_base64_payload(self) -> None:
        env = _make_env()
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post(f"{BASE_URL}/files").mock(
                return_value=httpx.Response(200, json={"status": "ok"})
            )

            await env.write_file(Path("/workspace/out.bin"), b"\x00\x01\x02")

        sent = route.calls.last.request
        assert sent.url.params.get("path") == "/workspace/out.bin"
        body = sent.read().decode()
        encoded = base64.b64encode(b"\x00\x01\x02").decode("ascii")
        assert encoded in body

    async def test_write_file_accepts_str(self) -> None:
        env = _make_env()
        with respx.mock() as mock:
            route = mock.post(f"{BASE_URL}/files").mock(
                return_value=httpx.Response(200, json={"status": "ok"})
            )

            await env.write_file(Path("/workspace/hello.txt"), "hello")

        body = route.calls.last.request.read().decode()
        # Strings are UTF-8 encoded before base64.
        encoded = base64.b64encode(b"hello").decode("ascii")
        assert encoded in body

    async def test_write_file_raises_permission_error_on_403(self) -> None:
        env = _make_env()
        with respx.mock() as mock:
            mock.post(f"{BASE_URL}/files").mock(
                return_value=httpx.Response(
                    403, json={"detail": "[Errno 13] Permission denied"}
                )
            )

            with pytest.raises(PermissionError):
                await env.write_file(Path("/root/forbidden"), b"x")

    async def test_download_zip_returns_raw_bytes(self) -> None:
        env = _make_env()
        with respx.mock(assert_all_called=True) as mock:
            route = mock.get(f"{BASE_URL}/files/zip").mock(
                return_value=httpx.Response(200, content=b"PK\x03\x04zipdata")
            )

            data = await env.download_zip(Path("/workspace"))

        assert data == b"PK\x03\x04zipdata"
        assert route.calls.last.request.url.params.get("path") == "/workspace"

    async def test_upload_zip_posts_raw_bytes(self) -> None:
        env = _make_env()
        with respx.mock(assert_all_called=True) as mock:
            route = mock.post(f"{BASE_URL}/files/zip").mock(
                return_value=httpx.Response(200, json={"status": "ok"})
            )

            await env.upload_zip(Path("/workspace"), b"PK\x03\x04payload")

        sent = route.calls.last.request
        assert sent.url.params.get("path") == "/workspace"
        assert sent.read() == b"PK\x03\x04payload"

    async def test_upload_zip_raises_on_error(self) -> None:
        env = _make_env()
        with respx.mock() as mock:
            mock.post(f"{BASE_URL}/files/zip").mock(
                return_value=httpx.Response(400, json={"detail": "invalid zip"})
            )

            with pytest.raises(OSError, match="invalid zip"):
                await env.upload_zip(Path("/workspace"), b"bad")


# =============================================================================
# initialize — wait-for-ready loop
# =============================================================================


@pytest.mark.asyncio
class TestInitialize:
    async def test_polls_healthz_until_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """initialize() exists so the first tool call doesn't eat the
        cold-start cost. It waits for /healthz to 200 — a successful
        response proves both the runtime is up AND the LB IAM gate has
        propagated, since /healthz traverses the same auth path as every
        other endpoint."""
        env = _make_env()
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        monkeypatch.setattr("app.environment.sandbox.asyncio.sleep", fake_sleep)

        with respx.mock() as mock:
            mock.get(f"{BASE_URL}/healthz").mock(
                side_effect=[
                    httpx.Response(503, text="not ready"),
                    httpx.Response(401, text="permission denied"),
                    httpx.Response(200, json={"status": "ok", "port": 8080}),
                ]
            )
            await env.initialize()

        assert sleeps  # at least one sleep happened

    async def test_initialize_is_idempotent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = _make_env()

        async def fake_sleep(delay: float) -> None:
            return

        monkeypatch.setattr("app.environment.sandbox.asyncio.sleep", fake_sleep)

        with respx.mock() as mock:
            healthz = mock.get(f"{BASE_URL}/healthz").mock(
                return_value=httpx.Response(200, json={"status": "ok"})
            )

            await env.initialize()
            await env.initialize()

        assert healthz.call_count == 1


# =============================================================================
# close — sandboxes are long-lived; close just shuts the local HTTP client
# =============================================================================


@pytest.mark.asyncio
class TestClose:
    async def test_close_aclose_the_http_client(self) -> None:
        """Sandboxes outlive the process — close must NOT snapshot or
        delete; it just shuts the local httpx client so we don't leak
        sockets."""
        env = _make_env()
        # Sanity: the http client is live before close.
        assert env._http.is_closed is False  # type: ignore[attr-defined]

        await env.close()

        assert env._http.is_closed is True  # type: ignore[attr-defined]

    async def test_close_is_idempotent(self) -> None:
        env = _make_env()
        await env.close()
        await env.close()  # Must not raise.
        assert env._http.is_closed is True  # type: ignore[attr-defined]

    async def test_close_does_not_call_sandbox_delete_or_snapshot(self) -> None:
        """Regression guard: the new persistent-sandbox model relies on
        the sandbox surviving the process. Either calling snapshot or
        calling delete here would silently break that."""
        client = MagicMock()
        env = _make_env(client=client)
        await env.close()

        client.agent_engines.sandboxes.delete.assert_not_called()
        client.agent_engines.sandboxes.snapshots.create.assert_not_called()


# =============================================================================
# close_sync — atexit path, no event loop required
# =============================================================================


def test_close_sync_marks_closed_without_event_loop() -> None:
    """The atexit hook fires after the event loop is gone; close_sync must
    not touch asyncio. Sockets are reclaimed by the OS at process exit, so
    it's enough to flip the closed flag — no snapshot, no delete."""
    client = MagicMock()
    env = _make_env(client=client)
    env.close_sync()

    assert env._closed is True  # type: ignore[attr-defined]
    client.agent_engines.sandboxes.delete.assert_not_called()
    client.agent_engines.sandboxes.snapshots.create.assert_not_called()


def test_close_sync_is_idempotent() -> None:
    env = _make_env()
    env.close_sync()
    env.close_sync()  # Must not raise.
    assert env._closed is True  # type: ignore[attr-defined]


# =============================================================================
# refresh_auth — per-turn JWT rotation
# =============================================================================


@pytest.mark.asyncio
async def test_gateway_error_marks_env_gone_so_refresh_evicts() -> None:
    """A 502/503/504 from the LB (can't reach the sandbox) flags the env gone so
    refresh_auth() returns False and the orchestrator evicts + re-resolves — no
    waiting for the 30-min routing refresh to notice a deleted sandbox."""
    from types import SimpleNamespace

    for code in (502, 503, 504):
        env = _make_env()
        env._initialized = True  # type: ignore[attr-defined]
        assert await env.refresh_auth() is True  # healthy before
        await env._note_response(SimpleNamespace(status_code=code))  # type: ignore[attr-defined]
        assert await env.refresh_auth() is False, code


@pytest.mark.asyncio
async def test_ok_response_keeps_env_live() -> None:
    from types import SimpleNamespace

    env = _make_env()
    env._initialized = True  # type: ignore[attr-defined]
    await env._note_response(SimpleNamespace(status_code=200))  # type: ignore[attr-defined]
    assert await env.refresh_auth() is True


@pytest.mark.asyncio
async def test_boot_probe_gateway_error_does_not_mark_env_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /healthz wait tolerates 5xx while the sandbox boots, so a boot-time
    gateway error must not latch _gone and evict a sandbox that came up fine."""
    env = _make_env()

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("app.environment.sandbox.asyncio.sleep", fake_sleep)

    with respx.mock() as mock:
        mock.get(f"{BASE_URL}/healthz").mock(
            side_effect=[
                httpx.Response(503, text="not ready"),
                httpx.Response(200, json={"status": "ok", "port": 8080}),
            ]
        )
        await env.initialize()

    assert await env.refresh_auth() is True


def test_apply_sandbox_token_swaps_authorization_header() -> None:
    """JWTs minted at construction expire within ~an hour; the provider re-mints
    per turn and applies the token to keep the long-lived httpx client authed."""
    env = _make_env(sandbox_token="old.jwt")
    assert env._http.headers["Authorization"] == "Bearer old.jwt"  # type: ignore[attr-defined]

    env._apply_sandbox_token("new.jwt")

    assert env._http.headers["Authorization"] == "Bearer new.jwt"  # type: ignore[attr-defined]
    # Other routing headers must NOT be disturbed — only Authorization rotates.
    assert env._http.headers["X-Sandbox-Routing-Token"] == "route-token-xyz"  # type: ignore[attr-defined]
    assert env._http.headers["X-Sandbox-Port"] == "8080"  # type: ignore[attr-defined]


def test_refresh_routing_token_swaps_header() -> None:
    """Routing token TTL is independent of the Bearer JWT — when it
    expires, every shim call returns 401. refresh_routing_token rotates
    the X-Sandbox-Routing-Token header without disturbing Authorization."""
    env = _make_env(sandbox_token="jwt-abc", routing_token="route-old")
    assert env._http.headers["X-Sandbox-Routing-Token"] == "route-old"  # type: ignore[attr-defined]

    env.refresh_routing_token("route-new")

    assert env._http.headers["X-Sandbox-Routing-Token"] == "route-new"  # type: ignore[attr-defined]
    assert env._http.headers["Authorization"] == "Bearer jwt-abc"  # type: ignore[attr-defined]
    assert env._http.headers["X-Sandbox-Port"] == "8080"  # type: ignore[attr-defined]


def test_routing_token_age_seconds_tracks_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Age is exposed so the throttled refresher in session_start can
    decide whether to fire — it must grow with time and reset on refresh."""
    clock = {"now": 1000.0}

    def fake_monotonic() -> float:
        return clock["now"]

    monkeypatch.setattr(
        "app.environment.sandbox.time.monotonic", fake_monotonic
    )

    env = _make_env()
    assert env.routing_token_age_seconds() == pytest.approx(0.0)

    clock["now"] = 1000.0 + 1800.0  # 30 minutes later
    assert env.routing_token_age_seconds() == pytest.approx(1800.0)

    env.refresh_routing_token("route-new")
    assert env.routing_token_age_seconds() == pytest.approx(0.0)


# Suppress an asyncio deprecation noise on Python 3.12 when respx wraps.
@pytest.fixture(autouse=True)
def _silence_loop_warning() -> None:
    asyncio.get_event_loop_policy()
