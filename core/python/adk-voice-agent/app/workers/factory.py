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

"""Per-user execution environment resolution through `ComputerProvider`."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

from app.auth.identity import acting_user_id
from app.auth.sandbox_env import sandbox_location
from app.workers.environment import Environment
from app.workers.providers.common import (
    caller_service_account,
    owner_id,
    sandbox_owner,
)
from app.workers.providers.host import get_workspace_root
from app.workers.providers.last_known import ensure_recorded, record_probe

__all__ = [
    "ResolvedEnvironment",
    "caller_service_account",
    "get_workspace_root",
    "invalidate_environment",
    "resolve_environment",
    "running_environment",
    "sandbox_owner",
    "sandbox_project_and_location",
    "set_environment",
]

# Avoid per-call provider probes while still noticing externally stopped computers.
_CACHE_TTL_S = 300.0

_override: Environment | None = None
_cache: dict[tuple[str, str], tuple[float, Environment]] = {}
_locks: dict[tuple[str, str], asyncio.Lock] = {}


def sandbox_project_and_location() -> tuple[str, str]:
    project = (
        os.getenv("SANDBOX_GCP_PROJECT")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or ""
    )
    return project, sandbox_location()


@dataclass
class ResolvedEnvironment:
    env: Environment
    notice: str | None = None


async def resolve_environment() -> ResolvedEnvironment:
    if _override is not None:
        return ResolvedEnvironment(_override)
    from app.workers.providers.base import ProviderNotices
    from app.workers.providers.registry import get_computer_provider

    user_id = acting_user_id()
    provider = get_computer_provider()
    key = (provider.name, owner_id(user_id))
    async with _locks.setdefault(key, asyncio.Lock()):
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL_S:
            return ResolvedEnvironment(cached[1])
        env = await ensure_recorded(provider, user_id)
        _cache[key] = (time.monotonic(), env)
        notice = (
            provider.pop_notice(user_id)
            if isinstance(provider, ProviderNotices)
            else None
        )
        return ResolvedEnvironment(env, notice)


async def running_environment() -> Environment | None:
    """Return the acting user's environment only if already running (never starts a stopped computer)."""
    if _override is not None:
        return _override
    from app.workers.providers.base import IdleStoppable
    from app.workers.providers.registry import get_computer_provider

    user_id = acting_user_id()
    provider = get_computer_provider()
    if not isinstance(provider, IdleStoppable):
        return (await resolve_environment()).env
    cached = _cache.get((provider.name, owner_id(user_id)))
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_S:
        return cached[1]
    env = await provider.running_environment(user_id)
    record_probe(user_id, env)
    return env


def invalidate_environment(owner: str | None = None) -> None:
    target = owner_id(owner or acting_user_id())
    for key in [k for k in _cache if k[1] == target]:
        _cache.pop(key, None)


def set_environment(env: Environment | None) -> None:
    global _override
    _override = env
    _cache.clear()
