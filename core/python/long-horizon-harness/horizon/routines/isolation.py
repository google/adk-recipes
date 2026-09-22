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

"""Shared routine-run isolation: the three ContextVars + headless preamble that
both the cron fire path (``routine_tick_endpoint``) and the synchronous test path
(``run_once``) install so they cannot drift.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator

from horizon.guardrails.permission_guard import (
    reset_headless_mode,
    set_headless_mode,
)
from horizon.routines.run_context import (
    RoutineRun,
    reset_routine_run,
    set_routine_run,
)
from horizon.secrets.inject import (
    reset_routine_secret_scope,
    set_routine_secret_scope,
)

HEADLESS_PREAMBLE = (
    "You are running UNATTENDED as a scheduled routine ({routine_id}). No user is "
    "present, so do not ask questions. You have your OWN fresh isolated sandbox: "
    "shell commands (git clone, install, build, commit) run normally in it. "
    "Anything else that would need interactive approval can't be granted, and "
    "secret-exfiltration and known-destructive commands are still refused — design "
    "around those. Save files you want to keep with artifact(save); your final "
    "text response is what gets reported back after the run. Task follows.\n\n"
)


@contextlib.contextmanager
def routine_isolation(
    routine_id: str, *, owner: str, secrets: Iterable[str]
) -> Iterator[None]:
    """Install the routine-run marker (isolated ``lhart-`` sandbox), headless mode
    (shell approvals run in the sandbox; other approvals fail-closed), and the
    secret scope (blast-radius bound) for the block, resetting all three on exit."""
    run_token = set_routine_run(RoutineRun(routine_id=routine_id, owner=owner))
    headless_token = set_headless_mode(True)
    scope_token = set_routine_secret_scope(secrets)
    try:
        yield
    finally:
        reset_routine_secret_scope(scope_token)
        reset_headless_mode(headless_token)
        reset_routine_run(run_token)


__all__ = ["HEADLESS_PREAMBLE", "routine_isolation"]
