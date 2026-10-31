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

"""Execution environments (`Environment` protocol, `SandboxEnvironment`, and `LocalEnvironment`)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)

# Connection failures never reached the daemon, so retrying cannot run a command twice.
_CONNECT_RETRY_DELAYS_S = (1.0, 4.0, 10.0)


def _why(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


COMPUTER_HOME = "/home/agent"
SANDBOX_IMAGE_VENV = "/opt/venv"
SANDBOX_IMAGE_NPM_PREFIX = "$HOME/.local"
SANDBOX_PATH_PREPEND = (
    f"{SANDBOX_IMAGE_NPM_PREFIX}/bin",
    f"{SANDBOX_IMAGE_VENV}/bin",
)
COMPUTER_DAEMON_PORT = 8080
COMPUTER_MCP_RELAY_URL = f"http://127.0.0.1:{COMPUTER_DAEMON_PORT}/mcp-relay"

FILE_READ_CAP = 1024 * 1024
_DEFAULT_FILE_MODE = 0o644


@dataclass
class ExecResult:
    """`error` is set only for transport failures; a failed command has `error=None` and non-zero `exit_code`."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.exit_code == 0

    def describe_failure(self) -> str:
        return (
            self.error
            or self.stderr
            or self.stdout
            or f"exit code {self.exit_code}"
        ).strip()[:400]


@dataclass(frozen=True)
class FileEntry:
    name: str
    type: Literal["file", "dir"]
    size: int
    mtime: str


@dataclass(frozen=True)
class FileContent:
    content: str
    truncated: bool = False


@dataclass(frozen=True)
class Activity:
    active_runs: int
    last_activity: str


class Environment(Protocol):
    """Execution contract over a persistent workspace root (remote computer daemon or local directory)."""

    @property
    def name(self) -> str: ...

    @property
    def workspace_root(self) -> str: ...

    @property
    def managed(self) -> bool: ...

    @property
    def mcp_base_url(self) -> str | None: ...

    async def exec(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
        timeout_s: float = 60.0,
    ) -> ExecResult: ...

    def credential_env(self) -> dict[str, str]: ...

    async def list_files(
        self, path: str = "", *, root: str | None = None
    ) -> list[FileEntry]: ...

    async def read_file(
        self,
        path: str,
        *,
        max_bytes: int = FILE_READ_CAP,
        root: str | None = None,
    ) -> FileContent: ...

    async def write_file(
        self,
        path: str,
        content: str,
        *,
        mode: str | None = None,
        root: str | None = None,
    ) -> None: ...

    async def activity(self) -> Activity: ...


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def _within(path: str, root: str) -> bool:
    return os.path.commonpath([path, root]) == root


def confine(root: str, rel: str, *, allow_root: bool = False) -> str:
    """Resolve `rel` under `root`, raising `ValueError` if it escapes `root`."""
    if "\0" in rel or os.path.isabs(rel) or ".." in Path(rel).parts:
        raise ValueError("path must be relative and stay inside the root.")
    if not allow_root and rel.strip() in ("", "."):
        raise ValueError("path must name a file.")
    resolved = os.path.realpath(os.path.join(root, rel))
    if not _within(resolved, root):
        raise ValueError("path escapes the root.")
    return resolved


def parse_file_mode(mode: str | None) -> int | None:
    if mode is None:
        return None
    try:
        bits = int(mode, 8)
    except ValueError:
        bits = -1
    if not 0 <= bits <= 0o777:
        raise ValueError(f"mode {mode!r} is not an octal permission like 0600.")
    return bits


def _file_error(status: int, detail: str) -> Exception:
    if status == 400:
        return ValueError(detail)
    if status == 404:
        return FileNotFoundError(detail)
    return RuntimeError(detail)


class ComputerUnreachable(RuntimeError):
    def __init__(self, endpoint: str, host: str, exc: BaseException) -> None:
        logger.warning(
            "Computer daemon unreachable at %s%s: %s",
            host,
            endpoint,
            _why(exc),
        )
        super().__init__(
            f"Couldn't reach the user's computer ({endpoint}: {_why(exc)})."
        )
        self.endpoint, self.host = endpoint, host


@dataclass
class SandboxEnvironment:
    """The user's computer daemon reached over HTTP(S)."""

    name: str = "cloud-computer"
    access_token: str = ""
    url: str = ""
    workspace_root: str = COMPUTER_HOME
    transport: Any | None = None
    managed: bool = True
    path_prepend: tuple[str, ...] = SANDBOX_PATH_PREPEND
    mcp_base_url: str | None = COMPUTER_MCP_RELAY_URL
    token_source: Callable[[], str] | None = None

    @property
    def base_url(self) -> str:
        target = self.url.strip().rstrip("/")
        if not target:
            return ""
        if "://" in target:
            return target
        return f"https://{target}"

    @property
    def host_label(self) -> str:
        return (self.url or self.name).strip()

    def headers(self) -> dict[str, str]:
        token = self.token_source() if self.token_source else self.access_token
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    async def _async_headers(self) -> dict[str, str]:
        if self.token_source is not None:
            return await asyncio.to_thread(self.headers)
        return self.headers()

    def _client(self) -> Any:
        from app.app_utils.http_client import get_pooled_http_client

        return get_pooled_http_client(
            self.base_url,
            transport=self.transport,
        )

    async def exec(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
        timeout_s: float = 60.0,
    ) -> ExecResult:
        import httpx

        prelude = (
            f'export PATH="{":".join(self.path_prepend)}:$PATH"; '
            if self.path_prepend
            else ""
        )
        body = {
            "command": prelude + command,
            "env": env or {},
            "timeout": timeout_s,
        }
        resp = None
        req_timeout = httpx.Timeout(timeout_s + 15.0, connect=15.0)
        for delay in (*_CONNECT_RETRY_DELAYS_S, None):
            try:
                headers = await self._async_headers()
                async with self._client() as client:
                    resp = await client.post(
                        f"{self.base_url}/exec",
                        json=body,
                        headers=headers,
                        timeout=req_timeout,
                    )
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if delay is None:
                    logger.warning(
                        "Computer /exec unreachable at %s: %s",
                        self.host_label,
                        _why(exc),
                    )
                    return ExecResult(
                        exit_code=1,
                        error=f"The user's computer /exec was unreachable ({_why(exc)}).",
                    )
                await asyncio.sleep(delay)
            except httpx.HTTPError as exc:
                logger.warning(
                    "Computer /exec unreachable at %s: %s",
                    self.host_label,
                    _why(exc),
                )
                return ExecResult(
                    exit_code=1,
                    error=f"The user's computer /exec was unreachable ({_why(exc)}).",
                )
        if resp is None:
            raise RuntimeError(
                "sandbox /exec retry loop ended without a response"
            )

        if resp.status_code >= 400:
            logger.warning(
                "Computer /exec at %s returned HTTP %s: %s",
                self.host_label,
                resp.status_code,
                resp.text[:300],
            )
            return ExecResult(
                exit_code=resp.status_code,
                error=f"The user's computer /exec returned HTTP {resp.status_code}.",
            )
        try:
            data = resp.json() if resp.content else {}
        except ValueError as exc:
            logger.warning(
                "Computer /exec at %s returned unparsable body: %s",
                self.host_label,
                exc,
            )
            return ExecResult(
                exit_code=1,
                error="The user's computer /exec returned an unparsable body.",
            )
        if not isinstance(data, dict):
            logger.warning(
                "Computer /exec at %s returned %s, expected object",
                self.host_label,
                type(data).__name__,
            )
            return ExecResult(
                exit_code=1,
                error=f"The user's computer /exec returned {type(data).__name__}, expected an object.",
            )
        if data.get("timed_out"):
            return ExecResult(
                exit_code=124, error=f"Command timed out after {timeout_s:.0f}s"
            )

        stdout = str(data.get("stdout") or data.get("output") or "")
        stderr = str(data.get("stderr") or "")
        raw_exit = data.get("exit_code", 0)
        try:
            exit_code = int(raw_exit)
        except (TypeError, ValueError):
            return ExecResult(
                exit_code=1,
                stdout=stdout,
                stderr=stderr,
                error=f"Sandbox /exec returned a non-numeric exit_code ({raw_exit!r}).",
            )
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    async def start_run(
        self,
        *,
        run_id: str,
        argv: list[str],
        cwd: str,
        run_dir: str | None = None,
        env_vars: dict[str, str] | None = None,
        path_prepend: list[str] | None = None,
        max_runtime_s: float | None = None,
    ) -> dict[str, Any]:
        import httpx

        effective_path = [*(path_prepend or []), *self.path_prepend]
        body: dict[str, Any] = {
            "run_id": run_id,
            "argv": argv,
            "cwd": cwd,
            "env": env_vars or {},
            "path_prepend": effective_path,
            "max_runtime_s": max_runtime_s,
        }
        if run_dir is not None:
            body["run_dir"] = run_dir

        headers = await self._async_headers()
        async with self._client() as client:
            resp = await client.post(
                f"{self.base_url}/runs/start",
                json=body,
                headers=headers,
                timeout=httpx.Timeout(30.0, connect=15.0),
            )
        if resp.status_code >= 400:
            logger.warning(
                "Computer /runs/start at %s returned HTTP %s: %s",
                self.host_label,
                resp.status_code,
                resp.text[:300],
            )
            raise RuntimeError(
                f"Could not launch {argv[0]} on the user's computer (HTTP {resp.status_code})."
            )
        data = resp.json()
        if not isinstance(data, dict):
            logger.warning(
                "Computer /runs/start at %s returned non-object %r",
                self.host_label,
                data,
            )
            raise RuntimeError(
                f"Could not launch {argv[0]} on the user's computer (unexpected response)."
            )
        return data

    async def poll_run(
        self,
        run_id: str,
        *,
        cursor: int = 0,
        skipping_long_line: bool = False,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        import httpx

        params: dict[str, Any] = {
            "cursor": cursor,
            "skipping_long_line": skipping_long_line,
        }
        if max_bytes is not None:
            params["max_bytes"] = max_bytes

        try:
            headers = await self._async_headers()
            async with self._client() as client:
                resp = await client.get(
                    f"{self.base_url}/runs/{run_id}/poll",
                    params=params,
                    headers=headers,
                    timeout=httpx.Timeout(30.0, connect=15.0),
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "Computer /runs/%s/poll unreachable at %s: %s",
                run_id,
                self.host_label,
                exc,
            )
            return {
                "transport_error": f"the user's computer /runs/{run_id}/poll unreachable"
            }

        if resp.status_code >= 400:
            logger.warning(
                "Computer /runs/%s/poll at %s returned HTTP %s: %s",
                run_id,
                self.host_label,
                resp.status_code,
                resp.text[:300],
            )
            return {
                "transport_error": f"the user's computer /runs/{run_id}/poll returned HTTP {resp.status_code}"
            }
        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning(
                "Computer /runs/%s/poll at %s returned invalid JSON: %s",
                run_id,
                self.host_label,
                exc,
            )
            return {
                "transport_error": f"the user's computer /runs/{run_id}/poll returned invalid JSON"
            }
        return data if isinstance(data, dict) else {}

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        import httpx

        try:
            headers = await self._async_headers()
            async with self._client() as client:
                resp = await client.post(
                    f"{self.base_url}/runs/{run_id}/cancel",
                    headers=headers,
                    timeout=httpx.Timeout(20.0, connect=15.0),
                )
                if resp.status_code < 400 and resp.content:
                    data = resp.json()
                    if isinstance(data, dict):
                        return data
        except httpx.HTTPError:
            pass
        return {"status": "cancelled", "run_id": run_id}

    async def _files_request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import httpx

        resp = None
        req_timeout = httpx.Timeout(30.0, connect=15.0)
        for delay in (*_CONNECT_RETRY_DELAYS_S, None):
            try:
                headers = await self._async_headers()
                async with self._client() as client:
                    resp = await client.request(
                        method,
                        f"{self.base_url}{endpoint}",
                        params=params,
                        json=body,
                        headers=headers,
                        timeout=req_timeout,
                    )
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if delay is None:
                    raise ComputerUnreachable(
                        endpoint, self.host_label, exc
                    ) from exc
                await asyncio.sleep(delay)
            except httpx.HTTPError as exc:
                raise ComputerUnreachable(
                    endpoint, self.host_label, exc
                ) from exc
        if resp is None:
            raise RuntimeError(
                "file request retry loop ended without a response"
            )
        if resp.status_code >= 400:
            try:
                detail = str(resp.json().get("detail") or resp.text)
            except (ValueError, AttributeError):
                detail = resp.text
            logger.warning(
                "Computer %s at %s returned HTTP %s: %s",
                endpoint,
                self.host_label,
                resp.status_code,
                detail[:300],
            )
            raise _file_error(
                resp.status_code,
                f"The user's computer {endpoint} returned HTTP {resp.status_code}.",
            )
        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning(
                "Computer %s at %s returned invalid JSON: %s",
                endpoint,
                self.host_label,
                exc,
            )
            raise RuntimeError(
                f"The user's computer {endpoint} returned invalid JSON."
            ) from exc
        if not isinstance(data, dict):
            logger.warning(
                "Computer %s at %s returned %s, expected object",
                endpoint,
                self.host_label,
                type(data).__name__,
            )
            raise RuntimeError(
                f"The user's computer {endpoint} returned {type(data).__name__}, expected an object."
            )
        return data

    async def list_files(
        self, path: str = "", *, root: str | None = None
    ) -> list[FileEntry]:
        data = await self._files_request(
            "GET",
            "/files/list",
            params={"path": path, "root": root or self.workspace_root},
        )
        return [
            FileEntry(
                name=str(e["name"]),
                type="dir" if e.get("type") == "dir" else "file",
                size=int(e.get("size") or 0),
                mtime=str(e.get("mtime") or ""),
            )
            for e in data.get("entries") or []
            if isinstance(e, dict) and e.get("name")
        ]

    async def read_file(
        self,
        path: str,
        *,
        max_bytes: int = FILE_READ_CAP,
        root: str | None = None,
    ) -> FileContent:
        data = await self._files_request(
            "GET",
            "/files/read",
            params={
                "path": path,
                "root": root or self.workspace_root,
                "max_bytes": max_bytes,
            },
        )
        return FileContent(
            content=str(data.get("content") or ""),
            truncated=bool(data.get("truncated")),
        )

    async def write_file(
        self,
        path: str,
        content: str,
        *,
        mode: str | None = None,
        root: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"path": path, "content": content}
        if mode is not None:
            body["mode"] = mode
        await self._files_request(
            "PUT",
            "/files/write",
            params={"root": root or self.workspace_root},
            body=body,
        )

    async def activity(self) -> Activity:
        data = await self._files_request("GET", "/activity")
        return Activity(
            active_runs=int(data.get("active_runs") or 0),
            last_activity=str(data.get("last_activity") or ""),
        )

    def credential_env(self) -> dict[str, str]:
        from app.auth import get_sandbox_gcp_env

        return get_sandbox_gcp_env()


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()


@dataclass
class LocalEnvironment:
    """Runs commands with `/bin/sh` under a local workspace root."""

    root: Path
    name: str = "local"
    managed: bool = False
    mcp_base_url: str | None = None
    _last_activity: float = field(
        default_factory=time.time, init=False, repr=False, compare=False
    )

    @property
    def workspace_root(self) -> str:
        return str(self.root)

    async def exec(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
        timeout_s: float = 60.0,
    ) -> ExecResult:
        self.root.mkdir(parents=True, exist_ok=True)
        self._last_activity = time.time()
        from app.auth.providers import server_only_env_keys

        # Strip connected-app OAuth tokens loaded into the server's environment.
        hidden = server_only_env_keys()
        base_env = {k: v for k, v in os.environ.items() if k not in hidden}
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            command,
            cwd=str(self.root),
            env={**base_env, **(env or {})},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except TimeoutError:
            await _kill_process_group(proc)
            return ExecResult(
                exit_code=124, error=f"Command timed out after {timeout_s:.0f}s"
            )
        except asyncio.CancelledError:
            # Reap child group before cancellation propagates so it does not outlive the loop.
            await _kill_process_group(proc)
            raise
        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else 1,
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
        )

    def credential_env(self) -> dict[str, str]:
        return {}

    def _resolve_root(self, root: str | None) -> str:
        base = os.path.realpath(self.root)
        if root is None:
            return base
        if not os.path.isabs(root) or "\0" in root:
            raise ValueError("root must be an absolute path.")
        resolved = os.path.realpath(root)
        if not _within(resolved, base):
            raise ValueError("root is outside the workspace.")
        return resolved

    async def list_files(
        self, path: str = "", *, root: str | None = None
    ) -> list[FileEntry]:
        base = self._resolve_root(root)
        return await asyncio.to_thread(
            _list_dir, base, confine(base, path, allow_root=True), path
        )

    async def read_file(
        self,
        path: str,
        *,
        max_bytes: int = FILE_READ_CAP,
        root: str | None = None,
    ) -> FileContent:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive.")
        target = confine(self._resolve_root(root), path)
        return await asyncio.to_thread(_read_text, target, path, max_bytes)

    async def write_file(
        self,
        path: str,
        content: str,
        *,
        mode: str | None = None,
        root: str | None = None,
    ) -> None:
        bits = parse_file_mode(mode)
        target = confine(self._resolve_root(root), path)
        await asyncio.to_thread(_write_atomic, target, path, content, bits)
        self._last_activity = time.time()

    async def activity(self) -> Activity:
        return await asyncio.to_thread(
            _runs_activity,
            Path(self.workspace_root) / ".voice-agent" / "runs",
            self._last_activity,
        )


def _list_dir(base: str, target: str, path: str) -> list[FileEntry]:
    if not os.path.exists(target):
        raise FileNotFoundError(f"{path or '.'} does not exist.")
    if not os.path.isdir(target):
        raise ValueError(f"{path} is not a directory.")
    entries: list[FileEntry] = []
    with os.scandir(target) as it:
        for entry in it:
            if not _within(os.path.realpath(entry.path), base):
                continue
            with contextlib.suppress(OSError):
                info = entry.stat()
                entries.append(
                    FileEntry(
                        name=entry.name,
                        type="dir" if entry.is_dir() else "file",
                        size=info.st_size,
                        mtime=_iso(info.st_mtime),
                    )
                )
    return sorted(entries, key=lambda e: e.name)


def _read_text(target: str, path: str, max_bytes: int) -> FileContent:
    if not os.path.exists(target):
        raise FileNotFoundError(f"{path} does not exist.")
    if os.path.isdir(target):
        raise ValueError(f"{path} is a directory.")
    with open(target, "rb") as f:
        data = f.read(max_bytes + 1)
    return FileContent(
        content=data[:max_bytes].decode("utf-8", errors="replace"),
        truncated=len(data) > max_bytes,
    )


def _write_atomic(
    target: str, path: str, content: str, mode: int | None
) -> None:
    if os.path.isdir(target):
        raise ValueError(f"{path} is a directory.")
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    if mode is None:
        mode = (
            os.stat(target).st_mode & 0o777
            if os.path.isfile(target)
            else _DEFAULT_FILE_MODE
        )
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content.encode("utf-8"))
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _pid_alive(pid_file: Path) -> bool:
    try:
        os.kill(int(pid_file.read_text().strip()), 0)
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False
    return True


def _runs_activity(runs_dir: Path, since: float) -> Activity:
    active, last = 0, since
    with contextlib.suppress(OSError):
        for run in runs_dir.iterdir():
            for name in (
                "spec.json",
                "stdout.jsonl",
                "stderr.log",
                "status.json",
            ):
                with contextlib.suppress(OSError):
                    last = max(last, (run / name).stat().st_mtime)
            if not (run / "status.json").exists() and _pid_alive(run / "pid"):
                active += 1
    return Activity(active_runs=active, last_activity=_iso(last))
