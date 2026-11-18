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

"""Per-user settings backed by the thread database with an in-memory cache for sync readers on the audio loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from app.auth.identity import acting_user_id, acting_user_id_or_none
from app.store.user_settings import get_user_settings_store

if TYPE_CHECKING:
    from app.workers.harnesses.base import CodingHarness

logger = logging.getLogger(__name__)

SETTINGS_TTL_S = 10.0

TIMEZONE = "voice.timezone"
VOICE_NAME = "voice.voice_name"
VOICE_MODE = "voice.mode"
SAVE_TO_DRIVE = "drive.save_project_files"
DEFAULT_HARNESS = "harness.default"
CLEARED_PROJECTS = "projects.cleared"
PROJECT_NAMES = "projects.names"
HIDDEN_PROJECTS = "projects.hidden"
# Maps a promoted project id back to its original scratch id so its Drive folder stays stable.
PROJECT_ORIGINS = "projects.origins"


_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_reloads: dict[str, asyncio.Task[Any]] = {}
# Bumped on each save so an in-flight read cannot overwrite a newer save.
_generation: dict[str, int] = {}


async def load_user_settings(
    user_id: str | None = None, *, force: bool = False
) -> dict[str, Any]:
    user = user_id or acting_user_id_or_none()
    if user is None:
        return {}
    hit = _cache.get(user)
    if hit is not None and not force and hit[0] > time.monotonic():
        return dict(hit[1])
    generation = _generation.get(user, 0)
    values = await get_user_settings_store().load(user)
    if _generation.get(user, 0) == generation:
        _cache[user] = (time.monotonic() + SETTINGS_TTL_S, values)
    return dict(values)


def user_settings() -> dict[str, Any]:
    """Return cached settings without I/O, scheduling a background refresh on miss/expiry."""
    user = acting_user_id_or_none()
    if user is None:
        return {}
    hit = _cache.get(user)
    if hit is None or hit[0] <= time.monotonic():
        _reload_in_background(user)
    return dict(hit[1]) if hit is not None else {}


def user_setting(key: str, default: Any = None) -> Any:
    value = user_settings().get(key)
    return default if value is None else value


async def save_user_settings(updates: Mapping[str, Any]) -> dict[str, Any]:
    user = acting_user_id()
    generation = _generation.get(user, 0)
    values = await get_user_settings_store().save(user, updates)
    # Concurrent save finished while ours ran: expire immediately so next read reloads.
    newest = _generation.get(user, 0) == generation
    _generation[user] = _generation.get(user, 0) + 1
    expires = time.monotonic() + SETTINGS_TTL_S if newest else 0.0
    _cache[user] = (expires, values)
    return dict(values)


def _reload_in_background(user: str) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if (running := _reloads.get(user)) is not None and not running.done():
        return

    async def reload() -> None:
        try:
            await load_user_settings(user, force=True)
        except Exception:
            logger.warning(
                "Could not reload settings for a user", exc_info=True
            )
        finally:
            _reloads.pop(user, None)

    _reloads[user] = loop.create_task(reload(), name="user-settings-reload")


def reset_user_settings_cache() -> None:
    _cache.clear()
    _reloads.clear()
    _generation.clear()


def user_default_harness() -> CodingHarness:
    from app.workers.harnesses.registry import get_harness_registry

    registry = get_harness_registry()
    if name := user_setting(DEFAULT_HARNESS):
        with contextlib.suppress(ValueError):
            return registry.get(str(name))
    return registry.default_harness
