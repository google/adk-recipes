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

"""``SandboxEnvironment`` — ``BaseEnvironment`` backed by an Agent Runtime
BYOC sandbox.

All shell + file I/O is direct REST against the FastAPI shim running
inside the sandbox container, reached through the sandbox's load
balancer hostname. Three headers are mandatory for the LB to route the
request: ``Authorization: Bearer <jwt>``, ``X-Sandbox-Routing-Token``,
``X-Sandbox-Port``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shlex
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from google.adk.environment import ExecutionResult

from app.environment.base import Environment
from app.environment.process import BackendGoneError

logger = logging.getLogger(__name__)


_HEALTHZ_TIMEOUT_SEC = 180.0
_POLL_INTERVAL_SEC = 3.0


class SandboxEnvironment(Environment):
    """BaseEnvironment whose working directory lives inside a BYOC sandbox.

    The shim inside the container handles every operation:
    ``POST /exec`` for shell commands, ``GET /files``/``POST /files`` for
    file I/O. The sandbox itself is long-lived and shared across processes
    via Agent Platform's authoritative ``sandboxes.list`` (see
    ``find_latest_user_sandbox``), not a host-local index file; ``close()``
    just tears down the local HTTP client — the sandbox keeps running so the
    next process can reattach.
    """

    on_host_fs = False

    def __init__(
        self,
        *,
        client: Any,
        sandbox_name: str,
        lb_host: str,
        routing_token: str,
        sandbox_token: str,
        owner: str,
        port: str = "8080",
        root: Path = Path("/workspace"),
        token_minter: Callable[[], str] | None = None,
        routing_fetcher: Callable[[], str] | None = None,
        routing_refresh_interval: float = 30 * 60,
        internet_access: bool = False,
    ) -> None:
        self._client = client
        self._name = sandbox_name
        self._owner = owner
        self._internet_access = internet_access
        self._root = root
        self._closed = False
        self._initialized = False
        self._last_probe_error = ""
        self._token_minter = token_minter
        self._routing_fetcher = routing_fetcher
        self._routing_interval = routing_refresh_interval
        # Set by the response hook when the LB can't reach the sandbox (a deleted
        # or dead sandbox returns 502/503/504); makes refresh_auth() report gone so
        # the orchestrator evicts + re-resolves instead of hammering a dead cache
        # entry until the 30-min routing refresh notices.
        self._gone = False

        self._http = httpx.AsyncClient(
            base_url=f"https://{lb_host}",
            headers={
                "Authorization": f"Bearer {sandbox_token}",
                "X-Sandbox-Routing-Token": routing_token,
                "X-Sandbox-Port": port,
            },
            timeout=httpx.Timeout(60.0, connect=10.0),
            event_hooks={"response": [self._note_response]},
        )
        self._routing_token_refreshed_at: float = time.monotonic()

    async def _note_response(self, response: httpx.Response) -> None:
        """Flag the sandbox as gone on a gateway error from the LB (502/503/504 =
        can't reach the sandbox backend). A transient blip self-corrects: eviction
        just re-resolves — reattaching to the same sandbox if it still exists.
        Pre-initialize responses are exempt: the /healthz wait expects 5xx while
        the sandbox is still booting."""
        if not self._initialized:
            return
        if response.status_code in (502, 503, 504):
            if not self._gone:
                logger.info(
                    "sandbox %s unreachable (HTTP %d); marking gone for eviction",
                    self._name,
                    response.status_code,
                )
            self._gone = True

    @property
    def working_dir(self) -> Path:
        return self._root

    @property
    def sandbox_name(self) -> str:
        return self._name

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def internet_access(self) -> bool:
        """Whether this sandbox was provisioned with outbound internet egress."""
        return self._internet_access

    def cache_identity(self) -> str:
        return self._name or self._owner

    async def initialize(self) -> None:
        if self._initialized:
            return

        await self._wait_for(
            "/healthz", _HEALTHZ_TIMEOUT_SEC, self._probe_healthz
        )
        self._initialized = True

    async def _probe_healthz(self) -> bool:
        try:
            r = await self._http.get("/healthz", timeout=10.0)
        except httpx.HTTPError as exc:
            self._last_probe_error = f"{type(exc).__name__}: {exc}"
            return False
        if r.status_code != 200:
            body = r.text[:200] if r.text else ""
            self._last_probe_error = f"HTTP {r.status_code}: {body}"
            return False
        return True

    async def _wait_for(
        self, label: str, deadline_sec: float, probe: Callable[[], Any]
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + deadline_sec
        self._last_probe_error = ""
        while True:
            if await probe():
                return
            if loop.time() >= deadline:
                detail = (
                    f" last_error={self._last_probe_error!r}"
                    if self._last_probe_error
                    else ""
                )
                raise TimeoutError(
                    f"sandbox {label} never became ready within "
                    f"{deadline_sec:.0f}s (sandbox={self._name}){detail}"
                )
            await asyncio.sleep(_POLL_INTERVAL_SEC)

    def _apply_sandbox_token(self, sandbox_token: str) -> None:
        self._http.headers["Authorization"] = f"Bearer {sandbox_token}"

    async def refresh_auth(self) -> bool:
        """Re-mint the JWT + (throttled) routing token via injected closures.
        Returns False when the backend is gone — via ``BackendGoneError`` or a prior
        unreachable (502/503/504) response — so the orchestrator evicts; transient
        auth errors keep the cached token."""
        if self._gone:
            return False
        if self._token_minter is None:
            return True
        try:
            self._apply_sandbox_token(self._token_minter())
        except BackendGoneError:
            return False
        except Exception as exc:
            logger.warning(
                "sandbox JWT refresh failed (%s); using cached token", exc
            )
            return True
        if self.routing_token_age_seconds() < self._routing_interval:
            return True
        if self._routing_fetcher is None:
            return True
        try:
            routing_token = self._routing_fetcher()
        except BackendGoneError:
            logger.info("sandbox %s gone", self._name)
            return False
        except Exception as exc:
            logger.warning(
                "sandbox routing token refresh failed (%s); using cached", exc
            )
            return True
        self.refresh_routing_token(routing_token)
        return True

    def refresh_routing_token(self, routing_token: str) -> None:
        """Swap the ``X-Sandbox-Routing-Token`` header to a freshly-fetched value.

        The LB's routing token has its own TTL, independent of the Bearer
        JWT — when it expires, every shim call returns 401 ``Routing
        token has expired``. Callers throttle this refresh (see
        ``_refresh_sandbox_auth``) rather than per-request to avoid
        hammering Vertex on UI polls.
        """
        self._http.headers["X-Sandbox-Routing-Token"] = routing_token
        self._routing_token_refreshed_at = time.monotonic()

    def routing_token_age_seconds(self) -> float:
        """Seconds since the routing token was last refreshed (or set in __init__)."""
        return time.monotonic() - self._routing_token_refreshed_at

    async def close(self) -> None:
        """Tear down the local HTTP client. The sandbox itself keeps
        running — it's shared across processes via the on-disk index,
        and reattached by the next session for the same user."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._http.aclose()
        except Exception as exc:
            logger.warning(
                "SandboxEnvironment.close: http aclose failed: %s", exc
            )

    def close_sync(self) -> None:
        # atexit variant: the event loop is gone, so we can't await
        # ``httpx.AsyncClient.aclose`` — the OS reclaims those sockets
        # at process exit anyway. The sandbox itself stays alive on
        # the platform side.
        self._closed = True

    async def execute(
        self,
        command: str,
        *,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecutionResult:
        body: dict[str, Any] = {"command": command}
        if timeout is not None:
            body["timeout"] = timeout
        if env is not None:
            body["env"] = env
        # Request timeout is the wall-clock cap on the HTTP round trip — give
        # the runtime its full command-timeout plus headroom for serialization.
        http_timeout = (timeout + 30.0) if timeout is not None else 600.0
        try:
            r = await self._http.post("/exec", json=body, timeout=http_timeout)
        except httpx.HTTPError as exc:
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=f"sandbox /exec transport error: {exc}",
                timed_out=False,
            )

        if r.status_code != 200:
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=f"sandbox /exec HTTP {r.status_code}: {r.text}",
                timed_out=False,
            )

        try:
            envelope = r.json()
        except json.JSONDecodeError:
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=f"non-JSON response: {r.text}",
                timed_out=False,
            )

        return ExecutionResult(
            exit_code=int(envelope.get("exit_code", -1)),
            stdout=str(envelope.get("stdout", "")),
            stderr=str(envelope.get("stderr", "")),
            timed_out=bool(envelope.get("timed_out", False)),
        )

    async def spawn_process(
        self,
        command: str,
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        """Spawn a background process inside the container; return a
        ``SandboxProcessHandle`` that proxies to the runtime shim."""
        from app.environment.sandbox_process import SandboxProcessHandle

        body: dict[str, Any] = {"command": command}
        if cwd is not None:
            body["cwd"] = str(cwd)
        if env is not None:
            body["env"] = env
        r = await self._http.post("/processes", json=body, timeout=60.0)
        r.raise_for_status()
        envelope = r.json()
        return SandboxProcessHandle(
            http=self._http,
            session_id=str(envelope["session_id"]),
            pid=int(envelope["pid"]),
            command=command,
        )

    async def list_processes(self) -> list[dict[str, Any]]:
        r = await self._http.get("/processes", timeout=30.0)
        r.raise_for_status()
        rows: list[dict[str, Any]] = []
        for s in r.json().get("sessions", []):
            rows.append(
                {
                    "session_id": s["session_id"],
                    "command": s["command"],
                    "running": s["is_running"],
                    "exit_code": s.get("exit_code"),
                    "idle_seconds": s.get("idle_seconds", 0.0),
                    "output_size": s.get("output_size", 0),
                    "pid": s.get("pid", 0),
                    "started_at": 0.0,
                }
            )
        return rows

    async def kill_process(self, session_id: str) -> bool:
        r = await self._http.post(f"/processes/{session_id}/kill", timeout=30.0)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return True

    async def read_file(self, path: Path) -> bytes:
        r = await self._http.get(
            "/files", params={"path": str(path)}, timeout=60.0
        )
        if r.status_code == 200:
            try:
                envelope = r.json()
            except json.JSONDecodeError as exc:
                raise OSError(
                    f"sandbox read_file({path}) — non-JSON response: {r.text!r}"
                ) from exc
            encoded = envelope.get("content_b64")
            if encoded is None:
                raise OSError(
                    f"sandbox read_file({path}) — response missing "
                    f"'content_b64': {envelope!r}"
                )
            return base64.b64decode(encoded)

        self._raise_file_http_error("read_file", path, r)

    async def list_directory(
        self, path: Path, *, limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """List directory entries inside the sandbox.

        Returns ``(entries, truncated)`` where each entry is a dict with
        ``name``, ``kind`` (``file``/``dir``/``symlink``), ``size``,
        and ``mtime``. Raises ``FileNotFoundError`` /
        ``NotADirectoryError`` / ``PermissionError`` to match the
        ``LocalEnvironment`` paths.
        """
        r = await self._http.get(
            "/files/list",
            params={"path": str(path), "limit": limit},
            timeout=30.0,
        )
        if r.status_code == 200:
            try:
                envelope = r.json()
            except json.JSONDecodeError as exc:
                raise OSError(
                    f"sandbox list_directory({path}) — non-JSON response: {r.text!r}"
                ) from exc
            return (
                list(envelope.get("entries", [])),
                bool(envelope.get("truncated", False)),
            )
        self._raise_file_http_error("list_directory", path, r)

    async def write_file(self, path: Path, content: str | bytes) -> None:
        content_bytes = (
            content.encode("utf-8")
            if isinstance(content, str)
            else bytes(content)
        )
        encoded = base64.b64encode(content_bytes).decode("ascii")
        r = await self._http.post(
            "/files",
            params={"path": str(path)},
            json={"content_b64": encoded},
            timeout=120.0,
        )
        if r.status_code == 200:
            return
        self._raise_file_http_error("write_file", path, r)

    async def make_dir(self, path: Path) -> None:
        result = await self.execute(f"mkdir -p {shlex.quote(str(path))}")
        if result.exit_code != 0:
            raise OSError(result.stderr or f"mkdir failed for {path}")

    async def delete_file(self, path: Path, *, recursive: bool = False) -> None:
        params: dict[str, str] = {"path": str(path)}
        if recursive:
            params["recursive"] = "true"
        r = await self._http.delete("/files", params=params, timeout=30.0)
        if r.status_code == 200:
            return
        self._raise_file_http_error("delete_file", path, r)

    async def download_zip(self, path: Path) -> bytes:
        r = await self._http.get(
            "/files/zip", params={"path": str(path)}, timeout=120.0
        )
        if r.status_code == 200:
            return r.content
        self._raise_file_http_error("download_zip", path, r)
        return b""  # unreachable

    async def upload_zip(self, path: Path, data: bytes) -> None:
        """Extract a zip archive into ``path`` inside the sandbox (server-side
        unzip via the shim's ``POST /files/zip``). The migration counterpart
        to :meth:`download_zip`."""
        r = await self._http.post(
            "/files/zip",
            params={"path": str(path)},
            content=data,
            headers={"Content-Type": "application/zip"},
            timeout=300.0,
        )
        if r.status_code == 200:
            return
        self._raise_file_http_error("upload_zip", path, r)

    def _raise_file_http_error(
        self, op: str, path: Path, r: httpx.Response
    ) -> None:
        detail = r.text
        try:
            detail = str(r.json().get("detail", r.text))
        except json.JSONDecodeError:
            pass

        if r.status_code == 404:
            raise FileNotFoundError(f"sandbox {op}({path}): {detail}")
        if r.status_code == 403:
            raise PermissionError(f"sandbox {op}({path}): {detail}")

        # The shim signals directory/file-type collisions with HTTP 400 and a
        # detail string copied from the OS errno. Map back to typed errors so
        # callers like file_ops.read_file can branch on IsADirectoryError —
        # otherwise the LocalEnvironment vs SandboxEnvironment paths surface
        # different exception types for the same user error.
        if r.status_code == 400:
            lowered = detail.lower()
            if "is a directory" in lowered:
                raise IsADirectoryError(f"sandbox {op}({path}): {detail}")
            if "not a directory" in lowered:
                raise NotADirectoryError(f"sandbox {op}({path}): {detail}")
            if "file exists" in lowered:
                raise FileExistsError(f"sandbox {op}({path}): {detail}")

        raise OSError(f"sandbox {op}({path}) HTTP {r.status_code}: {detail}")
