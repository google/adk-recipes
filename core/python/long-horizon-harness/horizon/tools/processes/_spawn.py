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

"""Shared background-process spawn helper.

Used by ``bash``'s auto-promote-on-timeout path (terminal.py) and by
``process(action='spawn')`` (process.py) — both need the identical
env.spawn_process + secret-injection call, so it lives here once rather
than being duplicated or cross-imported between sibling modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from horizon.environment_context import active_environment


async def open_handle(command: str, cwd: Path) -> Any:
    """Spawn a background-process handle on the active environment."""
    from horizon.secrets import secret_env

    env = active_environment()
    return await env.spawn_process(
        command, cwd=cwd, env=await secret_env() or None
    )


__all__ = ["open_handle"]
