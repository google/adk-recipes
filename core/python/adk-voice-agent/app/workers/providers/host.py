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

"""`COMPUTER_PROVIDER=host` (default): one local directory per user for development."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from app.store.engine import get_default_state_dir
from app.workers.environment import Environment, LocalEnvironment
from app.workers.providers.base import ComputerState, ComputerStatus
from app.workers.providers.common import is_owner_id, owner_id

_SEED_DIR = Path(__file__).resolve().parents[3] / "sandbox"


def get_workspace_root() -> Path:
    # Lives outside the repo tree so IDE git watchers don't pick up user workspaces.
    default_root = get_default_state_dir() / "workspaces"
    root = Path(
        os.getenv("VOICE_AGENT_WORKSPACE_ROOT") or str(default_root)
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def copy_missing(src: Path, dst: Path) -> None:
    for path in src.rglob("*"):
        target = dst / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def seed_home(home: Path) -> None:
    copy_missing(_SEED_DIR / "home", home)
    copy_missing(_SEED_DIR / "skills", home / ".agents" / "skills")


class HostProvider:
    name = "host"

    async def ensure(self, user_id: str) -> Environment:
        home = get_workspace_root() / owner_id(user_id)
        await asyncio.to_thread(seed_home, home)
        return LocalEnvironment(home)

    async def status(self, user_id: str) -> ComputerStatus:
        return ComputerStatus(ComputerState.RUNNING, "this machine")

    async def stop(self, user_id: str) -> None:
        return None

    async def known_users(self) -> list[str]:
        root = get_workspace_root()
        return sorted(
            p.name for p in root.iterdir() if p.is_dir() and is_owner_id(p.name)
        )
