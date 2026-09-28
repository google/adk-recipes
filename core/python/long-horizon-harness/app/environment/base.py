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

"""Horizon's runtime environment contract: the full I/O surface tools use,
plus capability flags — a superset of ADK's minimal ``BaseEnvironment``."""

from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.adk.environment import BaseEnvironment, ExecutionResult

if TYPE_CHECKING:
    from app.environment.process import ProcessHandle

__all__ = ["Environment", "ExecutionResult"]


class Environment(BaseEnvironment):
    # Safe defaults: an unknown backend is treated as remote (no host fs)
    # so a forgetful subclass can't accidentally hit the host.
    on_host_fs: bool = False

    @abstractmethod
    async def list_directory(
        self, path: Path, *, limit: int
    ) -> tuple[list[dict[str, Any]], bool]: ...

    @abstractmethod
    async def delete_file(
        self, path: Path, *, recursive: bool = False
    ) -> None: ...

    @abstractmethod
    async def make_dir(self, path: Path) -> None: ...

    @abstractmethod
    async def download_zip(self, path: Path) -> bytes: ...

    @abstractmethod
    async def upload_zip(self, path: Path, data: bytes) -> None: ...

    @abstractmethod
    async def spawn_process(
        self,
        command: str,
        *,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
    ) -> ProcessHandle: ...

    async def refresh_auth(self) -> bool:
        """Per-turn auth refresh. Default: nothing to refresh, still alive.
        Return False to signal this instance is gone so the orchestrator evicts."""
        return True

    async def list_processes(self) -> list[dict[str, Any]]:
        """Background processes visible to this environment. Default: none —
        backends that track processes (local, sandbox) override."""
        return []

    async def kill_process(self, session_id: str) -> bool:
        """Kill a background process by id. Default: not found."""
        return False

    def cache_identity(self) -> str:
        return str(self.working_dir)
