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

"""Computer daemon serving `/exec`, `/runs/*`, `/files/*`, `/activity`, and `/mcp-relay/*` on port 8080."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import os
import signal
import tempfile
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

WORKSPACE = os.environ.get("SANDBOX_WORKSPACE") or "/workspace"
DEFAULT_TIMEOUT_S = 600.0
MAX_TIMEOUT_S = 3600.0
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
POLL_READ_CAP = 2 * 1024 * 1024
_DRAIN_GRACE_S = 2.0
_CANCEL_GRACE_S = 1.0
_TIMEOUT_TERM_GRACE_S = 2.0
FILE_READ_CAP = 1024 * 1024
_DEFAULT_FILE_MODE = 0o644
_AUTH_EXEMPT_PATHS = frozenset({"/healthz"})
# Loopback-only; System 1 verifies the thread bearer token.
MCP_RELAY_PREFIX = "/mcp-relay/"
PROC_ROOT = "/proc"


@dataclass
class _ManagedRun:
    run_id: str
    run_dir: str
    proc: asyncio.subprocess.Process | None
    max_runtime_s: float | None = None
    exit_code: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    error: str | None = None
    supervisor_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return (
            self.supervisor_task is not None and not self.supervisor_task.done()
        )


@dataclass
class _Activity:
    last: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last = time.time()


_active_runs: dict[str, _ManagedRun] = {}
_activity = _Activity()
# Cache `(token, audience) -> exp` so frequent polls do not re-fetch signing certs.
_verified_tokens: dict[tuple[str, str], float] = {}

app = FastAPI(title="adk-voice-agent-cloud-computer")


def _auth_required() -> bool:
    raw = os.environ.get("COMPUTER_REQUIRE_AUTH", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _caller_error(authorization: str) -> str | None:
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return "Missing bearer token."
    audience = os.environ.get("COMPUTER_AUTH_AUDIENCE", "").strip()
    allowed = {
        email.strip().lower()
        for email in os.environ.get("COMPUTER_ALLOWED_CALLERS", "").split(",")
        if email.strip()
    }
    if not audience or not allowed:
        return (
            "COMPUTER_AUTH_AUDIENCE and COMPUTER_ALLOWED_CALLERS must be set."
        )
    now = time.time()
    cache_key = (token, audience)
    if _verified_tokens.get(cache_key, 0.0) > now:
        return None
    from google.auth import exceptions as google_auth_exceptions
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(
            token, google_requests.Request(), audience=audience
        )
    except (ValueError, google_auth_exceptions.GoogleAuthError) as exc:
        return f"Invalid ID token: {exc}"
    email = str(claims.get("email") or "").lower()
    if not claims.get("email_verified") or email not in allowed:
        return f"Caller {email or '(no email)'} is not allowed."
    for stale in [k for k, exp in _verified_tokens.items() if exp <= now]:
        del _verified_tokens[stale]
    _verified_tokens[cache_key] = float(claims.get("exp") or now)
    return None


@app.middleware("http")
async def _require_caller(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if (
        request.url.path in _AUTH_EXEMPT_PATHS
        or request.url.path.startswith(MCP_RELAY_PREFIX)
        or not _auth_required()
    ):
        return await call_next(request)
    error = await asyncio.to_thread(
        _caller_error, request.headers.get("authorization", "")
    )
    if error:
        return JSONResponse({"detail": error}, status_code=401)
    return await call_next(request)


class ExecRequest(BaseModel):
    command: str
    env: dict[str, str] = Field(default_factory=dict)
    timeout: float | None = None


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class RunStartRequest(BaseModel):
    run_id: str
    argv: list[str]
    cwd: str | None = None
    run_dir: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    path_prepend: list[str] = Field(default_factory=list)
    max_runtime_s: float | None = None


class RunStartResponse(BaseModel):
    run_id: str
    run_dir: str
    pid: int


class RunPollResponse(BaseModel):
    lines: list[str] = Field(default_factory=list)
    bytes_read: int = 0
    cursor: int = 0
    skipping_long_line: bool = False
    exit_code: int | None = None
    lost: bool = False
    stderr_tail: str = ""
    error: str | None = None


class FileWriteRequest(BaseModel):
    path: str
    content: str
    mode: str | None = None


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def _drain(stream: asyncio.StreamReader, sink: bytearray) -> None:
    while chunk := await stream.read(65536):
        room = MAX_OUTPUT_BYTES - len(sink)
        if room > 0:
            sink.extend(chunk[:room])


@app.post("/exec")
async def exec_command(req: ExecRequest) -> ExecResponse:
    timeout = min(req.timeout or DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S)
    os.makedirs(WORKSPACE, exist_ok=True)
    _activity.touch()
    # Process-group leader so timeout can kill its children.
    proc = await asyncio.create_subprocess_shell(
        req.command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=WORKSPACE,
        env={**os.environ, **req.env},
        start_new_session=True,
    )
    out, err = bytearray(), bytearray()
    if proc.stdout is None or proc.stderr is None:
        raise RuntimeError("subprocess pipes were not created")
    readers = [
        asyncio.create_task(_drain(proc.stdout, out)),
        asyncio.create_task(_drain(proc.stderr, err)),
    ]
    # Keep readers running at the deadline so pre-kill output is collected.
    _, pending = await asyncio.wait(
        [asyncio.create_task(proc.wait()), *readers], timeout=timeout
    )
    timed_out = bool(pending)
    if timed_out:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Bound wait in case a grandchild escaped the process group and holds pipes.
        _, pending = await asyncio.wait(pending, timeout=_DRAIN_GRACE_S)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    response = ExecResponse(
        exit_code=124 if timed_out else (proc.returncode or 0),
        stdout=out.decode(errors="replace"),
        stderr=err.decode(errors="replace")
        or ("timed out" if timed_out else ""),
        timed_out=timed_out,
    )
    _activity.touch()
    return response


def _write_status_atomic(run_dir: str, status: dict[str, Any]) -> None:
    tmp = os.path.join(run_dir, "status.tmp")
    target = os.path.join(run_dir, "status.json")
    with open(tmp, "w") as f:
        json.dump(status, f)
    os.replace(tmp, target)


async def _pipe_to_file(
    stream: asyncio.StreamReader | None,
    path: str,
) -> None:
    if stream is None:
        return
    with open(path, "ab") as f:
        while chunk := await stream.read(65536):
            f.write(chunk)
            f.flush()
            _activity.touch()


async def _supervise_run(managed: _ManagedRun) -> None:
    proc = managed.proc
    if proc is None:
        return
    stdout_path = os.path.join(managed.run_dir, "stdout.jsonl")
    stderr_path = os.path.join(managed.run_dir, "stderr.log")
    out_task = asyncio.create_task(_pipe_to_file(proc.stdout, stdout_path))
    err_task = asyncio.create_task(_pipe_to_file(proc.stderr, stderr_path))
    wait_task = asyncio.create_task(proc.wait())

    limit = managed.max_runtime_s
    timed_out = False
    try:
        if limit is not None and limit > 0:
            done, _ = await asyncio.wait({wait_task}, timeout=limit)
            if not done:
                timed_out = True
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGTERM)
                _, still_running = await asyncio.wait(
                    {wait_task}, timeout=_TIMEOUT_TERM_GRACE_S
                )
                if still_running:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    await asyncio.wait({wait_task}, timeout=_DRAIN_GRACE_S)
        else:
            await wait_task
    finally:
        _, pending_io = await asyncio.wait(
            {out_task, err_task}, timeout=_DRAIN_GRACE_S
        )
        for t in pending_io:
            t.cancel()
        await asyncio.gather(out_task, err_task, return_exceptions=True)

    if managed.cancelled:
        status: dict[str, Any] = {"exit_code": -15, "cancelled": True}
        managed.exit_code = -15
    elif timed_out:
        message = f"Stopped: the run exceeded its maximum run time of {round(limit or 0)} seconds."
        with open(stderr_path, "ab") as err_f:
            err_f.write(f"\n{message}\n".encode())
        status = {"exit_code": -9, "timed_out": True, "error": message}
        managed.exit_code = -9
        managed.timed_out = True
        managed.error = message
    else:
        code = proc.returncode if proc.returncode is not None else 0
        status = {"exit_code": code}
        managed.exit_code = code

    if not os.path.exists(os.path.join(managed.run_dir, "status.json")):
        _write_status_atomic(managed.run_dir, status)


@app.post("/runs/start")
async def start_run(req: RunStartRequest) -> RunStartResponse:
    if not req.argv:
        raise ValueError("argv must not be empty")
    cwd = req.cwd or WORKSPACE
    os.makedirs(cwd, exist_ok=True)
    run_dir = req.run_dir or os.path.join(
        WORKSPACE, ".voice-agent", "runs", req.run_id
    )
    os.makedirs(run_dir, exist_ok=True)
    _activity.touch()

    spec = {"argv": req.argv, "cwd": cwd, "max_runtime_s": req.max_runtime_s}
    with open(os.path.join(run_dir, "spec.json"), "w") as f:
        json.dump(spec, f)

    Path(os.path.join(run_dir, "stdout.jsonl")).write_bytes(b"")
    Path(os.path.join(run_dir, "stderr.log")).write_bytes(b"")

    run_env = {**os.environ, **req.env}
    if req.path_prepend:
        home = run_env.get("HOME") or os.environ.get("HOME", "")
        expanded = [
            p.replace("${HOME}", home).replace("$HOME", home)
            for p in req.path_prepend
            if p
        ]
        existing_path = run_env.get("PATH", "")
        run_env["PATH"] = (
            f"{':'.join(expanded)}:{existing_path}"
            if existing_path
            else ":".join(expanded)
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            *req.argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=run_env,
            start_new_session=True,
        )
    except OSError as exc:
        reason = (
            f"{req.argv[0]} is not installed in the computer image ({exc})"
            if isinstance(exc, FileNotFoundError)
            else str(exc)
        )
        with open(os.path.join(run_dir, "stderr.log"), "wb") as err_f:
            err_f.write(f"failed to start {req.argv[0]}: {reason}\n".encode())
        _write_status_atomic(run_dir, {"exit_code": 127})
        managed = _ManagedRun(
            run_id=req.run_id,
            run_dir=run_dir,
            proc=None,
            exit_code=127,
        )
        _active_runs[req.run_id] = managed
        return RunStartResponse(
            run_id=req.run_id,
            run_dir=run_dir,
            pid=0,
        )

    with open(os.path.join(run_dir, "pid"), "w") as pid_f:
        pid_f.write(str(proc.pid))

    managed = _ManagedRun(
        run_id=req.run_id,
        run_dir=run_dir,
        proc=proc,
        max_runtime_s=req.max_runtime_s,
    )
    _active_runs[req.run_id] = managed
    managed.supervisor_task = asyncio.create_task(_supervise_run(managed))
    return RunStartResponse(
        run_id=req.run_id,
        run_dir=run_dir,
        pid=proc.pid,
    )


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat_file = f"/proc/{pid}/stat"
    if os.path.exists(stat_file):
        with contextlib.suppress(OSError):
            content = Path(stat_file).read_text()
            after_comm = content.rsplit(")", 1)[-1].split()
            if after_comm and after_comm[0] == "Z":
                return False
    return True


def _read_poll_snapshot(
    run_id: str,
    cursor: int,
    skipping_long_line: bool,
    max_bytes: int = POLL_READ_CAP,
) -> RunPollResponse:
    managed = _active_runs.get(run_id)
    run_dir = (
        managed.run_dir
        if managed is not None
        else os.path.join(WORKSPACE, ".voice-agent", "runs", run_id)
    )
    if not os.path.isdir(run_dir):
        return RunPollResponse(
            cursor=cursor,
            skipping_long_line=skipping_long_line,
            lost=True,
            error=f"Run directory {run_dir} is missing.",
        )

    status_path = os.path.join(run_dir, "status.json")
    stderr_path = os.path.join(run_dir, "stderr.log")
    stdout_path = os.path.join(run_dir, "stdout.jsonl")
    pid_path = os.path.join(run_dir, "pid")

    exit_code: int | None = None
    status_error: str | None = None
    parse_error: str | None = None
    if os.path.isfile(status_path):
        try:
            raw_status = Path(status_path).read_text()
            if raw_status.strip():
                status_data = json.loads(raw_status)
                exit_code = int(status_data["exit_code"])
                if status_data.get("error"):
                    status_error = str(status_data["error"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parse_error = f"Unreadable status.json: {exc}"
    elif managed is not None and managed.exit_code is not None:
        exit_code = managed.exit_code
        status_error = managed.error

    lost = False
    if exit_code is None and parse_error is None:
        if managed is not None and managed.proc is not None:
            if (
                managed.proc.returncode is not None
                and not os.path.isfile(status_path)
                and managed.exit_code is None
            ):
                lost = True
        else:
            try:
                pid = int(Path(pid_path).read_text().strip())
            except (OSError, ValueError):
                pid = -1
            if not _is_pid_alive(pid) and not os.path.isfile(status_path):
                lost = True

    stderr_tail = ""
    if exit_code is not None and os.path.isfile(stderr_path):
        with contextlib.suppress(OSError):
            with open(stderr_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 4000), os.SEEK_SET)
                stderr_tail = f.read().decode("utf-8", errors="replace").strip()
    if status_error:
        stderr_tail = f"{status_error}\n{stderr_tail}".strip()

    raw = b""
    if os.path.isfile(stdout_path):
        with contextlib.suppress(OSError):
            with open(stdout_path, "rb") as f:
                f.seek(cursor, os.SEEK_SET)
                raw = f.read(max_bytes)

    bytes_read = len(raw)
    new_cursor = cursor
    new_skipping = skipping_long_line
    lines: list[str] = []

    if new_skipping:
        newline = raw.find(b"\n")
        if newline < 0:
            new_cursor += len(raw)
            return RunPollResponse(
                lines=[],
                bytes_read=bytes_read,
                cursor=new_cursor,
                skipping_long_line=True,
                exit_code=exit_code,
                lost=lost,
                stderr_tail=stderr_tail,
                error=parse_error,
            )
        new_cursor += newline + 1
        new_skipping = False
        raw = raw[newline + 1 :]

    end = raw.rfind(b"\n")
    if end < 0:
        if len(raw) >= max_bytes:
            new_cursor += len(raw)
            new_skipping = True
        return RunPollResponse(
            lines=[],
            bytes_read=bytes_read,
            cursor=new_cursor,
            skipping_long_line=new_skipping,
            exit_code=exit_code,
            lost=lost,
            stderr_tail=stderr_tail,
            error=parse_error,
        )

    complete = raw[: end + 1]
    new_cursor += len(complete)
    lines = [
        ln
        for ln in complete.decode("utf-8", errors="replace").splitlines()
        if ln.strip()
    ]
    return RunPollResponse(
        lines=lines,
        bytes_read=bytes_read,
        cursor=new_cursor,
        skipping_long_line=new_skipping,
        exit_code=exit_code,
        lost=lost,
        stderr_tail=stderr_tail,
        error=parse_error,
    )


@app.get("/runs/{run_id}/poll")
async def poll_run(
    run_id: str,
    cursor: int = Query(default=0, ge=0),
    skipping_long_line: bool = Query(default=False),
    max_bytes: int = Query(default=POLL_READ_CAP, ge=1),
) -> RunPollResponse:
    return _read_poll_snapshot(run_id, cursor, skipping_long_line, max_bytes)


@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, Any]:
    managed = _active_runs.get(run_id)
    run_dir = (
        managed.run_dir
        if managed is not None
        else os.path.join(WORKSPACE, ".voice-agent", "runs", run_id)
    )
    if managed is not None:
        managed.cancelled = True
        managed.exit_code = -15

    pid: int | None = None
    if managed is not None and managed.proc is not None:
        pid = managed.proc.pid
    else:
        pid_path = os.path.join(run_dir, "pid")
        with contextlib.suppress(OSError, ValueError):
            pid = int(Path(pid_path).read_text().strip())

    if pid is not None and pid > 0:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        if managed is not None and managed.proc is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    managed.proc.wait(), timeout=_CANCEL_GRACE_S
                )
        else:
            await asyncio.sleep(_CANCEL_GRACE_S)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)

    if os.path.isdir(run_dir):
        status_path = os.path.join(run_dir, "status.json")
        if not os.path.isfile(status_path):
            _write_status_atomic(run_dir, {"exit_code": -15, "cancelled": True})

    return {"status": "cancelled", "run_id": run_id, "exit_code": -15}


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def _within(path: str, root: str) -> bool:
    return os.path.commonpath([path, root]) == root


def _resolve_root(root: str | None) -> str:
    workspace = os.path.realpath(WORKSPACE)
    if root is None:
        return workspace
    if not os.path.isabs(root) or "\0" in root:
        raise HTTPException(400, "root must be an absolute path.")
    resolved = os.path.realpath(root)
    if not _within(resolved, workspace):
        raise HTTPException(400, "root is outside the workspace.")
    return resolved


def _confined(root: str, rel: str, *, allow_root: bool = False) -> str:
    if "\0" in rel or os.path.isabs(rel) or ".." in Path(rel).parts:
        raise HTTPException(
            400, "path must be relative and stay inside the root."
        )
    if not allow_root and rel.strip() in ("", "."):
        raise HTTPException(400, "path must name a file.")
    resolved = os.path.realpath(os.path.join(root, rel))
    if not _within(resolved, root):
        raise HTTPException(400, "path escapes the root.")
    return resolved


def _parse_mode(mode: str | None) -> int | None:
    if mode is None:
        return None
    try:
        bits = int(mode, 8)
    except ValueError:
        bits = -1
    if not 0 <= bits <= 0o777:
        raise HTTPException(
            400, f"mode {mode!r} is not an octal permission like 0600."
        )
    return bits


@app.get("/files/list")
async def list_files(path: str = "", root: str | None = None) -> dict[str, Any]:
    base = _resolve_root(root)
    target = _confined(base, path, allow_root=True)
    if not os.path.exists(target):
        raise HTTPException(404, f"{path or '.'} does not exist.")
    if not os.path.isdir(target):
        raise HTTPException(400, f"{path} is not a directory.")
    entries: list[dict[str, Any]] = []
    with os.scandir(target) as it:
        for entry in it:
            if not _within(os.path.realpath(entry.path), base):
                continue
            with contextlib.suppress(OSError):
                info = entry.stat()
                entries.append(
                    {
                        "name": entry.name,
                        "type": "dir" if entry.is_dir() else "file",
                        "size": info.st_size,
                        "mtime": _iso(info.st_mtime),
                    }
                )
    entries.sort(key=lambda e: e["name"])
    return {"entries": entries}


@app.get("/files/read")
async def read_file(
    path: str,
    root: str | None = None,
    max_bytes: int = Query(default=FILE_READ_CAP, ge=1, le=MAX_OUTPUT_BYTES),
) -> dict[str, Any]:
    target = _confined(_resolve_root(root), path)
    if not os.path.exists(target):
        raise HTTPException(404, f"{path} does not exist.")
    if os.path.isdir(target):
        raise HTTPException(400, f"{path} is a directory.")
    with open(target, "rb") as f:
        data = f.read(max_bytes + 1)
    return {
        "content": data[:max_bytes].decode("utf-8", errors="replace"),
        "truncated": len(data) > max_bytes,
    }


@app.put("/files/write")
async def write_file(
    req: FileWriteRequest, root: str | None = None
) -> dict[str, bool]:
    mode = _parse_mode(req.mode)
    target = _confined(_resolve_root(root), req.path)
    if os.path.isdir(target):
        raise HTTPException(400, f"{req.path} is a directory.")
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
            f.write(req.content.encode("utf-8"))
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _activity.touch()
    return {"ok": True}


def _interactive_sessions() -> int:
    """Count distinct sessions of this UID with a controlling terminal (`tty_nr != 0`)."""
    uid = os.getuid()
    sessions: set[str] = set()
    try:
        entries = list(os.scandir(PROC_ROOT))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            with open(os.path.join(entry.path, "stat"), encoding="utf-8") as f:
                stat = f.read()
        except OSError:
            continue
        fields = stat.rpartition(")")[2].split()
        if len(fields) > 4 and fields[4] != "0":
            sessions.add(fields[3])
    return len(sessions)


@app.get("/activity")
async def activity() -> dict[str, Any]:
    if await asyncio.to_thread(_interactive_sessions):
        _activity.touch()
    return {
        "active_runs": sum(1 for m in _active_runs.values() if m.running),
        "last_activity": _iso(_activity.last),
    }


# Cloud Run IAP requires a self-signed SA JWT in `Proxy-Authorization` rather than
# a metadata-server ID token.
_PROXY_TOKEN_REFRESH_MARGIN_S = 300.0
_PROXY_TOKEN_LIFETIME_S = 3600
_METADATA_SA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default"
_SIGN_JWT = "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa}:signJwt"

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",  # codespell:ignore
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)
_RELAY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
# SSE streams stay open for the life of the session.
_RELAY_TIMEOUT = httpx.Timeout(30.0, read=None)

_proxy_tokens: dict[str, tuple[str, float]] = {}
_relay_client: httpx.AsyncClient | None = None


def _relay_http() -> httpx.AsyncClient:
    global _relay_client
    if _relay_client is None:
        _relay_client = httpx.AsyncClient(timeout=_RELAY_TIMEOUT)
    return _relay_client


def _token_expiry(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
        return float(claims["exp"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def _fetch_proxy_token(audience: str, http: httpx.Client | None = None) -> str:
    """Sign a JWT for `audience` via IAM Credentials using the VM's default SA."""
    with http or httpx.Client(timeout=30.0) as client:
        metadata = {"Metadata-Flavor": "Google"}
        email = client.get(f"{_METADATA_SA}/email", headers=metadata)
        email.raise_for_status()
        sa = email.text.strip()
        token = client.get(f"{_METADATA_SA}/token", headers=metadata)
        token.raise_for_status()
        now = int(time.time())
        claims = {
            "iss": sa,
            "sub": sa,
            "aud": audience,
            "iat": now,
            "exp": now + _PROXY_TOKEN_LIFETIME_S,
        }
        signed = client.post(
            _SIGN_JWT.format(sa=sa),
            headers={"Authorization": f"Bearer {token.json()['access_token']}"},
            json={"payload": json.dumps(claims)},
        )
        signed.raise_for_status()
        return str(signed.json()["signedJwt"])


def _proxy_token(audience: str) -> str:
    now = time.time()
    cached = _proxy_tokens.get(audience)
    if cached and cached[1] - _PROXY_TOKEN_REFRESH_MARGIN_S > now:
        return cached[0]
    token = _fetch_proxy_token(audience)
    _proxy_tokens[audience] = (token, _token_expiry(token) or now + 600.0)
    return token


def _is_loopback(host: str | None) -> bool:
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


_MCP_ALLOWED_PATH = "api/v1/mcp"


def _valid_mcp_relay_path(request: Request, path: str) -> str | None:
    raw = (request.scope.get("raw_path") or b"").decode(
        "latin-1", errors="replace"
    )
    if raw:
        raw_path = raw.partition("?")[0]
        if not raw_path.startswith(MCP_RELAY_PREFIX):
            return None
        raw_tail = raw_path[len(MCP_RELAY_PREFIX) :]
        lower_tail = raw_tail.lower()
        if (
            raw_tail.startswith("/")
            or "\\" in raw_tail
            or "%2e" in lower_tail
            or "%2f" in lower_tail
            or "%5c" in lower_tail
            or ".." in raw_tail.split("/")
        ):
            return None
    decoded = urllib.parse.unquote(path)
    if (
        not decoded
        or decoded != path
        or decoded.startswith("/")
        or "\\" in decoded
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in decoded)
    ):
        return None
    segments = decoded.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        return None
    if decoded != _MCP_ALLOWED_PATH and not decoded.startswith(
        _MCP_ALLOWED_PATH + "/"
    ):
        return None
    return decoded


@app.api_route(MCP_RELAY_PREFIX + "{path:path}", methods=_RELAY_METHODS)
async def mcp_relay(path: str, request: Request) -> Response:
    if not _is_loopback(request.client.host if request.client else None):
        return JSONResponse(
            {"detail": "The MCP relay serves this computer only."},
            status_code=403,
        )
    clean_path = _valid_mcp_relay_path(request, path)
    if clean_path is None:
        return JSONResponse(
            {"detail": "The MCP relay only forwards /api/v1/mcp."},
            status_code=403,
        )
    upstream = os.environ.get("COMPUTER_MCP_UPSTREAM", "").strip().rstrip("/")
    if not upstream:
        return JSONResponse(
            {"detail": "COMPUTER_MCP_UPSTREAM is not set."}, status_code=503
        )
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP
    }
    if audience := os.environ.get("COMPUTER_MCP_AUDIENCE", "").strip():
        try:
            token = await asyncio.to_thread(_proxy_token, audience)
        except Exception as exc:
            return JSONResponse(
                {
                    "detail": f"Could not sign a proxy token for the bridge: {exc}"
                },
                status_code=502,
            )
        headers["proxy-authorization"] = f"Bearer {token}"
    has_body = "content-length" in request.headers or (
        "transfer-encoding" in request.headers
    )
    client = _relay_http()
    outbound = client.build_request(
        request.method,
        f"{upstream}/{clean_path}",
        params=request.url.query or None,
        headers=headers,
        content=request.stream() if has_body else None,
    )
    _activity.touch()
    try:
        upstream_response = await client.send(outbound, stream=True)
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"detail": f"The MCP bridge is unreachable: {exc}"}, status_code=502
        )
    return StreamingResponse(
        upstream_response.aiter_raw(),
        status_code=upstream_response.status_code,
        headers={
            k: v
            for k, v in upstream_response.headers.items()
            if k.lower() not in _HOP_BY_HOP
        },
        background=BackgroundTask(upstream_response.aclose),
    )
