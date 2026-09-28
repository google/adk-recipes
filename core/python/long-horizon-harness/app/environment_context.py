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

"""Per-session ``Environment`` lookup via ``ContextVar``.

The active environment is the storage abstraction skills and (later)
file/shell tools use to read and write persistent state. It is
installed at session start by ``before_agent_callback`` and torn down
at session end. Tests install one directly to exercise IO without
running the ADK runtime.

A ``ContextVar`` (not a module global) so concurrent sessions running
in the same process don't see each other's environments — each
``asyncio.Task`` inherits its own snapshot.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from app.environment import Environment

_active_env: ContextVar[Environment | None] = ContextVar(
    "lha_active_environment", default=None
)

# Plain module global, not a ContextVar: set once at app construction
# (process-wide backend choice), not per-session, so no per-task isolation needed.
_provider_override: Callable[[str], Environment] | None = None


def set_environment_provider(
    factory: Callable[[str], Environment] | None,
) -> None:
    # None clears the override → falls back to the LHA_ENVIRONMENT_BACKEND env-var path.
    global _provider_override
    _provider_override = factory


def environment_provider() -> Callable[[str], Environment] | None:
    return _provider_override


# Process-wide sandbox provider override (provisioning/reattach/snapshot interface),
# parallel to the per-user env-factory override above. None = fall back to the
# LHA_ENVIRONMENT_BACKEND-selected built-in provider.
_sandbox_provider_override: Any = None


def set_sandbox_provider(provider: Any | None) -> None:
    global _sandbox_provider_override
    _sandbox_provider_override = provider


def sandbox_provider() -> Any | None:
    return _sandbox_provider_override


def set_active_environment(env: Environment) -> None:
    _active_env.set(env)


def clear_active_environment() -> None:
    _active_env.set(None)


def active_environment() -> Environment:
    env = _active_env.get()
    if env is None:
        raise RuntimeError(
            "No active environment. Call set_active_environment() "
            "(tests) or rely on before_agent_callback (runtime)."
        )
    return env
