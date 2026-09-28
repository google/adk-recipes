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

"""Process representation owned by the environment layer: the backend-agnostic
``ProcessHandle`` protocol every ``Environment.spawn_process`` returns."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["BackendGoneError", "ProcessHandle"]


class BackendGoneError(Exception):
    """A refresh closure raises this to signal the backend instance is gone
    (e.g. sandbox 404) so the orchestrator evicts and rebuilds."""


@runtime_checkable
class ProcessHandle(Protocol):
    session_id: str
    pid: int
    command: str
    started_at: float
    finished_at: float | None

    @property
    def is_running(self) -> bool: ...

    @property
    def exit_code(self) -> int | None: ...

    @property
    def output_size(self) -> int: ...

    @property
    def idle_seconds(self) -> float: ...

    async def read(
        self, offset: int = 0, limit: int | None = None
    ) -> tuple[bytes, int, int | None]: ...

    async def write(self, data: bytes) -> None: ...

    async def kill(self) -> None: ...

    async def wait(self, timeout: float | None = None) -> int | None: ...
