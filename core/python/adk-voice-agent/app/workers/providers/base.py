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

"""Computer lifecycle protocol (`ComputerProvider`, `IdleStoppable`, `ProviderNotices`)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.workers.environment import Environment


class ComputerState(StrEnum):
    MISSING = "missing"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True)
class ComputerStatus:
    state: ComputerState
    detail: str = ""


@runtime_checkable
class ComputerProvider(Protocol):
    """Lifecycle seam for a user's computer; returns an `Environment` once running."""

    @property
    def name(self) -> str: ...

    async def ensure(self, user_id: str) -> Environment: ...

    async def status(self, user_id: str) -> ComputerStatus: ...

    async def stop(self, user_id: str) -> None: ...

    async def known_users(self) -> list[str]: ...


@runtime_checkable
class IdleStoppable(Protocol):
    async def running_environment(self, user_id: str) -> Environment | None: ...


@runtime_checkable
class ProviderNotices(Protocol):
    def pop_notice(self, user_id: str) -> str | None: ...


class ComputerUnavailableError(RuntimeError):
    pass
