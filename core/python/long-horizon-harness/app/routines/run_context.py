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

"""Async-local marker that a turn is a scheduled routine run.

When set, the environment-binding path (``_ensure_environment``) builds the
routine's OWN isolated sandbox keyed by ``routine_id`` instead of resolving the
user's. A ContextVar (not a process global) so concurrent web turns are
unaffected — same pattern as ``set_headless_mode`` / ``set_routine_secret_scope``.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class RoutineRun:
    routine_id: str
    owner: str


_routine_run: contextvars.ContextVar[RoutineRun | None] = (
    contextvars.ContextVar("routine_run", default=None)
)


def set_routine_run(run: RoutineRun) -> contextvars.Token:
    return _routine_run.set(run)


def reset_routine_run(token: contextvars.Token) -> None:
    _routine_run.reset(token)


def active_routine_run() -> RoutineRun | None:
    return _routine_run.get()
