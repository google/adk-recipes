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

"""FastAPI shim that runs *inside* the BYOC sandbox container.

The Agent Platform proxies HTTP from the caller (via the sandbox load balancer)
to this server on the port declared in the SandboxEnvironmentTemplate.

Endpoints:
  GET    /healthz                       -> {"status": "ok"}
  POST   /exec                          -> ExecResponse (run shell command via subprocess)
  GET    /files?path=...                -> FileReadResponse (base64-encoded bytes)
  GET    /files/list?path=...&limit=N   -> ListResponse (directory entries)
  POST   /files?path=...                -> {"status": "ok"} (base64-encoded write, optional mode)
  DELETE /files?path=...&recursive=...  -> {"status": "ok"} (unlink, or rmtree if recursive)
  GET    /files/zip?path=...            -> application/zip (directory archived in-memory)
  POST   /processes                     -> ProcessSpawnResponse (background spawn)
  GET  /processes                     -> ProcessListResponse (every session)
  GET  /processes/{sid}/status        -> ProcessStatusResponse
  GET  /processes/{sid}/output        -> ProcessOutputResponse (paginated rolling buffer)
  POST /processes/{sid}/stdin         -> {} (write to PTY, optional newline)
  POST /processes/{sid}/kill          -> ProcessKillResponse (SIGTERM then SIGKILL)
"""

from __future__ import annotations

import asyncio
import base64
import errno
import fcntl
import io
import os
import pty
import shutil
import signal
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response

try:
    from protocol import (
        RUNTIME_PORT,
        ExecRequest,
        ExecResponse,
        FileReadResponse,
        FileWriteRequest,
        ListEntry,
        ListResponse,
        ProcessKillResponse,
        ProcessListResponse,
        ProcessOutputResponse,
        ProcessSpawnRequest,
        ProcessSpawnResponse,
        ProcessStatusResponse,
        ProcessStdinRequest,
    )
except (
    ModuleNotFoundError
):  # imported as a package on the host, not from /opt/runtime
    from horizon.sandbox.runtime.protocol import (
        RUNTIME_PORT,
        ExecRequest,
        ExecResponse,
        FileReadResponse,
        FileWriteRequest,
        ListEntry,
        ListResponse,
        ProcessKillResponse,
        ProcessListResponse,
        ProcessOutputResponse,
        ProcessSpawnRequest,
        ProcessSpawnResponse,
        ProcessStatusResponse,
        ProcessStdinRequest,
    )

WORKSPACE_ROOT = Path(
    os.environ.get("SANDBOX_WORKSPACE", "").strip() or "/workspace"
).resolve()
DEFAULT_EXEC_TIMEOUT_SEC = float(
    os.environ.get("SANDBOX_EXEC_TIMEOUT_SEC", "").strip() or "600"
)

app = FastAPI(title="lha-sandbox-runtime")


def _resolve_within_workspace(raw: str) -> Path:
    """Resolve ``raw`` and confirm it lands inside ``WORKSPACE_ROOT``.

    Defends against path traversal (``../``) and absolute paths pointing
    outside the workspace. The container runs as a non-root user so the
    OS would block most escapes anyway, but failing fast at the API layer
    keeps the contract honest.
    """
    target = (
        WORKSPACE_ROOT / raw if not Path(raw).is_absolute() else Path(raw)
    ).resolve()
    try:
        target.relative_to(WORKSPACE_ROOT)
    except ValueError as err:
        raise HTTPException(
            status_code=403,
            detail=f"path {raw!r} escapes workspace root {WORKSPACE_ROOT}",
        ) from err
    return target


@app.get("/healthz")
async def healthz() -> dict[str, str | int]:
    return {"status": "ok", "port": RUNTIME_PORT}


@app.post("/exec", response_model=ExecResponse)
async def exec_command(req: ExecRequest) -> ExecResponse:
    proc = await asyncio.create_subprocess_shell(
        req.command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=req.cwd,
        env=_resolve_env(req.env),
    )
    stdout_buf = bytearray()
    stderr_buf = bytearray()

    async def _drain(
        stream: asyncio.StreamReader | None, buf: bytearray
    ) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            buf.extend(chunk)

    timeout = (
        req.timeout if req.timeout is not None else DEFAULT_EXEC_TIMEOUT_SEC
    )
    drain_task = asyncio.gather(
        _drain(proc.stdout, stdout_buf),
        _drain(proc.stderr, stderr_buf),
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        await drain_task
    except TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            pass
        drain_task.cancel()
        try:
            await drain_task
        except (asyncio.CancelledError, Exception):  # noqa: S110
            pass
        return ExecResponse(
            exit_code=-1,
            stdout=bytes(stdout_buf).decode("utf-8", errors="replace"),
            stderr=bytes(stderr_buf).decode("utf-8", errors="replace"),
            timed_out=True,
        )
    return ExecResponse(
        exit_code=proc.returncode if proc.returncode is not None else 0,
        stdout=bytes(stdout_buf).decode("utf-8", errors="replace"),
        stderr=bytes(stderr_buf).decode("utf-8", errors="replace"),
        timed_out=False,
    )


@app.get("/files", response_model=FileReadResponse)
async def read_file(path: str = Query(...)) -> FileReadResponse:
    target = _resolve_within_workspace(path)
    try:
        data = target.read_bytes()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IsADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return FileReadResponse(content_b64=base64.b64encode(data).decode("ascii"))


@app.get("/files/list", response_model=ListResponse)
async def list_files(
    path: str = Query(...),
    limit: int = Query(500, ge=1, le=10_000),
) -> ListResponse:
    target = _resolve_within_workspace(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"{path} not found")
    if not target.is_dir():
        raise HTTPException(
            status_code=400, detail=f"{path} is not a directory"
        )

    entries: list[ListEntry] = []
    truncated = False
    with os.scandir(target) as it:
        rows = sorted(it, key=lambda e: e.name)
    for entry in rows:
        if len(entries) >= limit:
            truncated = True
            break
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if entry.is_symlink():
            kind = "symlink"
        elif entry.is_dir(follow_symlinks=False):
            kind = "dir"
        else:
            kind = "file"
        entries.append(
            ListEntry(
                name=entry.name,
                kind=kind,
                size=int(st.st_size),
                mtime=int(st.st_mtime),
            )
        )
    return ListResponse(entries=entries, truncated=truncated)


@app.post("/files")
async def write_file(
    req: FileWriteRequest, path: str = Query(...)
) -> dict[str, str]:
    target = _resolve_within_workspace(path)
    try:
        data = base64.b64decode(req.content_b64)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if req.mode is not None:
            target.chmod(req.mode)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok"}


@app.delete("/files")
async def delete_file(
    path: str = Query(...),
    recursive: bool = Query(False),
) -> dict[str, str]:
    target = _resolve_within_workspace(path)
    if target == WORKSPACE_ROOT:
        raise HTTPException(
            status_code=400, detail="cannot delete workspace root"
        )
    try:
        if recursive and target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IsADirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except OSError as exc:
        if exc.errno == errno.EISDIR:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"status": "ok"}


@app.get("/files/zip")
async def download_zip(path: str = Query(...)) -> Response:
    target = _resolve_within_workspace(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"{path} not found")
    if not target.is_dir():
        raise HTTPException(
            status_code=400, detail=f"{path} is not a directory"
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for entry in target.rglob("*"):
            if entry.is_symlink() or not entry.is_file():
                continue
            zf.write(entry, arcname=entry.relative_to(target).as_posix())
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{target.name or "workspace"}.zip"',
        },
    )


@app.post("/files/zip")
async def upload_zip(
    request: Request, path: str = Query(...)
) -> dict[str, str]:
    target = _resolve_within_workspace(path)
    body = await request.body()
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            members = zf.infolist()
            # Validate every destination stays inside the workspace before
            # writing anything (zip-slip), so a malicious entry can't leave
            # a half-extracted tree behind.
            dests = [
                _resolve_within_workspace((target / m.filename).as_posix())
                for m in members
            ]
            target.mkdir(parents=True, exist_ok=True)
            for member, dest in zip(members, dests, strict=True):
                if member.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
    except zipfile.BadZipFile as exc:
        raise HTTPException(
            status_code=400, detail=f"invalid zip: {exc}"
        ) from exc
    return {"status": "ok"}


_PROCESS_INHERITED_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
)
_PROCESS_BUFFER_BYTES = 200 * 1024  # mirrors LocalProcessHandle
_PROCESS_MAX_SESSIONS = 64


def _resolve_env(requested: dict[str, str] | None) -> dict[str, str] | None:
    if requested is None:
        return None
    base = {
        k: os.environ[k] for k in _PROCESS_INHERITED_ENV_KEYS if k in os.environ
    }
    base.update(requested)
    return base


class _ContainerProcess:
    """PTY-backed background subprocess. Mirrors ``LocalProcessHandle``."""

    def __init__(
        self,
        command: str,
        *,
        cwd: Path | None,
        env: dict[str, str] | None,
        buffer_bytes: int = _PROCESS_BUFFER_BYTES,
    ) -> None:
        self.session_id = f"proc_{uuid.uuid4().hex[:12]}"
        self.command = command
        self.started_at = time.time()
        self.finished_at: float | None = None

        self._buffer_bytes = buffer_bytes
        self._buffer = bytearray()
        self._total_bytes = 0
        self._lock = threading.Lock()
        self._exit_code: int | None = None
        self._exit_event = threading.Event()
        self._last_output_at = self.started_at

        self._master_fd, slave_fd = pty.openpty()
        try:
            self._proc = subprocess.Popen(  # noqa: S603
                ["/bin/sh", "-c", command],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
                env=env,
                start_new_session=True,  # setsid, so kill() can killpg the tree
                close_fds=True,
            )
        finally:
            os.close(slave_fd)

        self.pid = self._proc.pid

        flags = fcntl.fcntl(self._master_fd, fcntl.F_GETFL)
        fcntl.fcntl(self._master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        self._reader = threading.Thread(
            target=self._reader_loop,
            name=f"sandbox-proc-reader-{self.session_id}",
            daemon=True,
        )
        self._reader.start()

    def _reader_loop(self) -> None:
        try:
            while True:
                try:
                    chunk = os.read(self._master_fd, 4096)
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        if self._proc.poll() is not None:
                            try:
                                chunk = os.read(self._master_fd, 4096)
                                if not chunk:
                                    break
                            except OSError:
                                break
                        else:
                            time.sleep(0.02)
                            continue
                    elif exc.errno == errno.EIO:
                        break
                    else:
                        break
                if not chunk:
                    break
                with self._lock:
                    self._buffer.extend(chunk)
                    self._total_bytes += len(chunk)
                    self._last_output_at = time.time()
                    overflow = len(self._buffer) - self._buffer_bytes
                    if overflow > 0:
                        del self._buffer[:overflow]
        finally:
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self._exit_code = self._proc.returncode
            self.finished_at = time.time()
            self._exit_event.set()
            try:
                os.close(self._master_fd)
            except OSError:
                pass

    @property
    def is_running(self) -> bool:
        return not self._exit_event.is_set()

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @property
    def output_size(self) -> int:
        with self._lock:
            return self._total_bytes

    @property
    def idle_seconds(self) -> float:
        return time.time() - self._last_output_at

    def read(
        self, offset: int = 0, limit: int | None = None
    ) -> tuple[bytes, int]:
        with self._lock:
            buffer_start = self._total_bytes - len(self._buffer)
            slice_start = max(offset, buffer_start) - buffer_start
            slice_start = max(0, min(slice_start, len(self._buffer)))
            if limit is None:
                data = bytes(self._buffer[slice_start:])
            else:
                data = bytes(self._buffer[slice_start : slice_start + limit])
            next_offset = buffer_start + slice_start + len(data)
            return data, next_offset

    def write(self, data: bytes) -> None:
        if self._exit_event.is_set():
            raise RuntimeError(f"process {self.session_id} has already exited")
        os.write(self._master_fd, data)

    def kill(self) -> int | None:
        if self._exit_event.is_set():
            return self._exit_code
        try:
            pgid = os.getpgid(self.pid)
        except ProcessLookupError:
            return self._exit_code
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return self._exit_code
        if self._exit_event.wait(2.0):
            return self._exit_code
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return self._exit_code
        self._exit_event.wait(1.0)
        return self._exit_code

    def status(self) -> ProcessStatusResponse:
        return ProcessStatusResponse(
            session_id=self.session_id,
            pid=self.pid,
            command=self.command,
            is_running=self.is_running,
            exit_code=self._exit_code,
            output_size=self._total_bytes,
            idle_seconds=self.idle_seconds,
        )


_PROCESSES: dict[str, _ContainerProcess] = {}
_PROCESSES_LOCK = threading.Lock()


def _get_process(sid: str) -> _ContainerProcess:
    with _PROCESSES_LOCK:
        proc = _PROCESSES.get(sid)
    if proc is None:
        raise HTTPException(status_code=404, detail=f"unknown session {sid!r}")
    return proc


def _resolve_process_cwd(raw: str | None) -> Path | None:
    if raw is None:
        return None
    return _resolve_within_workspace(raw)


@app.post("/processes", response_model=ProcessSpawnResponse)
async def spawn_process(req: ProcessSpawnRequest) -> ProcessSpawnResponse:
    with _PROCESSES_LOCK:
        alive = sum(1 for p in _PROCESSES.values() if p.is_running)
        if alive >= _PROCESS_MAX_SESSIONS:
            raise HTTPException(
                status_code=429,
                detail=f"too many live sessions ({alive}/{_PROCESS_MAX_SESSIONS})",
            )
    try:
        proc = _ContainerProcess(
            req.command,
            cwd=_resolve_process_cwd(req.cwd),
            env=_resolve_env(req.env),
        )
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"spawn failed: {exc}"
        ) from exc
    with _PROCESSES_LOCK:
        _PROCESSES[proc.session_id] = proc
    return ProcessSpawnResponse(session_id=proc.session_id, pid=proc.pid)


@app.get("/processes", response_model=ProcessListResponse)
async def list_processes() -> ProcessListResponse:
    with _PROCESSES_LOCK:
        snapshot = list(_PROCESSES.values())
    return ProcessListResponse(sessions=[p.status() for p in snapshot])


@app.get("/processes/{sid}/status", response_model=ProcessStatusResponse)
async def process_status(sid: str) -> ProcessStatusResponse:
    return _get_process(sid).status()


@app.get("/processes/{sid}/output", response_model=ProcessOutputResponse)
async def process_output(
    sid: str,
    offset: int = Query(0, ge=0),
    limit: int | None = Query(None, gt=0),
) -> ProcessOutputResponse:
    proc = _get_process(sid)
    data, next_offset = proc.read(offset=offset, limit=limit)
    return ProcessOutputResponse(
        data_b64=base64.b64encode(data).decode("ascii"),
        next_offset=next_offset,
        exit_code=proc.exit_code,
    )


@app.post("/processes/{sid}/stdin")
async def process_stdin(sid: str, req: ProcessStdinRequest) -> dict[str, str]:
    proc = _get_process(sid)
    try:
        payload = base64.b64decode(req.data_b64)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"bad base64: {exc}"
        ) from exc
    if req.submit and not payload.endswith(b"\n"):
        payload = payload + b"\n"
    try:
        await asyncio.to_thread(proc.write, payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "ok"}


@app.post("/processes/{sid}/kill", response_model=ProcessKillResponse)
async def process_kill(sid: str) -> ProcessKillResponse:
    proc = _get_process(sid)
    exit_code = await asyncio.to_thread(proc.kill)
    return ProcessKillResponse(
        exit_code=exit_code if exit_code is not None else -1
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",  # noqa: S104
        port=int(os.environ.get("PORT", RUNTIME_PORT)),
    )
