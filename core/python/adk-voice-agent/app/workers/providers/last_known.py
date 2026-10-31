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

"""In-memory per-user computer status cache so `GET /state` never blocks on cloud API calls."""

from __future__ import annotations

import asyncio

from app.workers.environment import Environment
from app.workers.providers.base import (
    ComputerProvider,
    ComputerState,
    ComputerStatus,
)
from app.workers.providers.common import owner_id

_last_known: dict[str, ComputerStatus] = {}


def record_status(
    user_or_owner: str, state: ComputerState, detail: str = ""
) -> None:
    _last_known[owner_id(user_or_owner)] = ComputerStatus(state, detail)


def record_probe(user_or_owner: str, env: Environment | None) -> None:
    record_status(
        user_or_owner,
        ComputerState.STOPPED if env is None else ComputerState.RUNNING,
    )


def last_known_status(user_or_owner: str) -> ComputerStatus | None:
    return _last_known.get(owner_id(user_or_owner))


def reset_last_known() -> None:
    _last_known.clear()


async def ensure_recorded(
    provider: ComputerProvider, user_id: str
) -> Environment:
    owner = owner_id(user_id)
    previous = _last_known.get(owner)
    record_status(owner, ComputerState.STARTING)
    try:
        env = await provider.ensure(user_id)
    except asyncio.CancelledError:
        if previous is None:
            _last_known.pop(owner, None)
        else:
            _last_known[owner] = previous
        raise
    except Exception as exc:
        record_status(
            user_id, ComputerState.ERROR, str(exc) or type(exc).__name__
        )
        raise
    record_status(user_id, ComputerState.RUNNING)
    return env
