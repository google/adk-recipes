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

"""Host-filesystem ``Environment`` — ADK's LocalEnvironment plus Horizon's
extended file/process surface (moved out of api/uploads.py + tools/terminal)."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.adk.environment import LocalEnvironment as _AdkLocalEnvironment

from app.environment import registry
from app.environment.base import Environment
from app.environment.local_process import LocalProcessHandle

if TYPE_CHECKING:
    # Annotation-only ProcessHandle return type; spawn_process imports the
    # concrete LocalProcessHandle lazily (see below).
    from app.environment.process import ProcessHandle

__all__ = ["LocalEnvironment"]


def _handle_to_row(h: Any) -> dict[str, Any]:
    return {
        "session_id": h.session_id,
        "command": h.command,
        "running": h.is_running,
        "exit_code": h.exit_code,
        "idle_seconds": h.idle_seconds,
        "output_size": h.output_size,
        "pid": h.pid,
        "started_at": h.started_at,
    }


class LocalEnvironment(_AdkLocalEnvironment, Environment):
    on_host_fs = True

    async def list_directory(
        self, path: Path, *, limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        return await asyncio.to_thread(self._scan, Path(path), limit)

    @staticmethod
    def _scan(target: Path, limit: int) -> tuple[list[dict[str, Any]], bool]:
        with os.scandir(target) as it:
            rows = sorted(it, key=lambda e: e.name)
        entries: list[dict[str, Any]] = []
        truncated = False
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
                {
                    "name": entry.name,
                    "kind": kind,
                    "size": int(st.st_size),
                    "mtime": int(st.st_mtime),
                }
            )
        return entries, truncated

    async def make_dir(self, path: Path) -> None:
        await asyncio.to_thread(
            lambda: Path(path).mkdir(parents=True, exist_ok=True)
        )

    async def delete_file(self, path: Path, *, recursive: bool = False) -> None:
        if recursive:
            await asyncio.to_thread(shutil.rmtree, Path(path))
            return
        await asyncio.to_thread(Path(path).unlink)

    async def download_zip(self, path: Path) -> bytes:
        return await asyncio.to_thread(self._zip_dir, Path(path))

    @staticmethod
    def _zip_dir(target: Path) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(
            buf, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as zf:
            for entry in target.rglob("*"):
                if entry.is_symlink() or not entry.is_file():
                    continue
                zf.write(entry, arcname=entry.relative_to(target).as_posix())
        return buf.getvalue()

    async def upload_zip(self, path: Path, data: bytes) -> None:
        await asyncio.to_thread(self._unzip_into, Path(path), data)

    @staticmethod
    def _unzip_into(target: Path, data: bytes) -> None:
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            zf.extractall(target)

    async def spawn_process(
        self,
        command: str,
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
    ) -> ProcessHandle:
        # Popen(env=...) REPLACES the child env; merge secrets over os.environ so
        # PATH/HOME survive (mirrors the sandbox shim's _resolve_env).
        proc_env = {**os.environ, **env} if env else None
        return LocalProcessHandle(
            command, cwd=Path(cwd) if cwd is not None else None, env=proc_env
        )

    async def list_processes(self) -> list[dict[str, Any]]:
        return [_handle_to_row(h) for h in registry.all_handles()]

    async def kill_process(self, session_id: str) -> bool:
        h = registry.find_handle(session_id)
        if h is None:
            return False
        await h.kill()
        return True
