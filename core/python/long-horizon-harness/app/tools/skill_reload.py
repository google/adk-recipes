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

"""Bind the live ``SkillToolset`` to a session and refresh its catalog.

ADK leaves ``SkillToolset._skills`` mutable and sets
``_use_invocation_cache = False`` precisely so the catalog can change
mid-session. ``bind_toolset`` registers the per-session roots;
``refresh_skills`` re-walks those roots and replaces ``_skills`` in
place. The ``/reload`` slash command and model-facing ``reload`` tool
in ``app/commands/`` call ``resync_and_refresh``, which first re-mirrors
the user's skills from the env interface so sandbox-side edits land on the
host before the re-walk.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Any

from google.adk.tools.skill_toolset import SkillToolset

from app.tools.skill_loader import (
    builtin_skills_root,
    mirror_user_skills_to_host,
    walk_skill_dirs,
)

logger = logging.getLogger(__name__)


def host_mirror_dir(working_dir: Path, owner: str | None = None) -> Path:
    """The per-user host skill-mirror cache dir.

    Keyed on owner + working_dir: in the sandbox backend every user's
    ``working_dir`` is the same in-container ``/workspace``, so the owner is
    what keeps one user's mirror from clobbering another's.
    """
    digest = hashlib.sha256(
        f"{owner or ''}\0{working_dir}".encode()
    ).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "lha-skills-mirror" / digest


_TOOLSET: SkillToolset | None = None
_USER_DIR: Path | None = None
_BUILTIN_DIR: Path | None = None

# Per-user mirror cache: key (str host_mirror_dir) -> (user_dir, skills, catalog).
# Skipping the env-interface re-mirror + re-walk on every turn is the point — the
# catalog only changes via /reload (resync_and_refresh), which refreshes this.
_mirror_cache: dict[str, tuple[Path, dict[str, Any], list[dict[str, Any]]]] = {}

BOUND_SKILLS_STATE_KEY = "bound_skills"

# Sentinel "no such dir" used to re-walk only the user root for source tagging.
_EMPTY_SKILL_DIR = builtin_skills_root().parent / "_none"


def bound_skill_catalog() -> list[dict[str, Any]]:
    """Serialized view of the currently bound catalog: name, description, source.

    A name present under the user root is tagged ``source:"user"`` even when a
    builtin of the same name exists (the user skill shadows the builtin).
    """
    if _TOOLSET is None:
        return []
    user_names: set[str] = set()
    if _USER_DIR is not None:
        user_names = set(
            walk_skill_dirs(user_dir=_USER_DIR, builtin_dir=_EMPTY_SKILL_DIR)
        )
    return [
        {
            "name": name,
            "description": getattr(skill.frontmatter, "description", "") or "",
            "source": "user" if name in user_names else "builtin",
        }
        for name, skill in _TOOLSET._skills.items()
    ]


def bind_toolset(
    toolset: SkillToolset,
    *,
    user_dir: Path,
    builtin_dir: Path,
) -> None:
    """Register the toolset + roots that ``refresh_skills`` will refresh."""
    global _TOOLSET, _USER_DIR, _BUILTIN_DIR
    _TOOLSET = toolset
    _USER_DIR = user_dir
    _BUILTIN_DIR = builtin_dir


def bound_skill_names() -> set[str]:
    """Names in the currently bound skill catalog (empty if none bound)."""
    if _TOOLSET is None:
        return set()
    return set(_TOOLSET._skills.keys())


def unbind_toolset() -> None:
    """Drop the registered toolset — used by tests."""
    global _TOOLSET, _USER_DIR, _BUILTIN_DIR
    _TOOLSET = None
    _USER_DIR = None
    _BUILTIN_DIR = None
    _mirror_cache.clear()


def refresh_skills() -> dict[str, Any] | None:
    """Re-scan the bound skill dirs and update ``SkillToolset._skills``.

    Returns a diff dict ``{loaded, removed, total}`` or ``None`` when no
    toolset is bound (e.g. unit tests that don't construct a SkillToolset).
    Never raises — the unified reload path treats a missing toolset as a
    no-op for the skills tier.
    """
    if _TOOLSET is None or _USER_DIR is None or _BUILTIN_DIR is None:
        return None

    before = set(_TOOLSET._skills.keys())
    refreshed = walk_skill_dirs(user_dir=_USER_DIR, builtin_dir=_BUILTIN_DIR)
    _TOOLSET._skills = refreshed
    after = set(refreshed.keys())

    return {
        "loaded": sorted(after - before),
        "removed": sorted(before - after),
        "total": len(after),
    }


async def resync_and_refresh() -> dict[str, Any] | None:
    """Re-mirror user skills from the env interface, then refresh the catalog.

    The ``/reload`` path runs after the user has edited skills in their
    workspace (which in sandbox mode lives behind the env interface), so the
    host mirror must be rebuilt before re-walking it. Falls back to a plain
    ``refresh_skills`` when no environment is active.
    """
    try:
        from app.environment_context import active_environment

        env = active_environment()
    except RuntimeError:
        return refresh_skills()

    if _TOOLSET is not None and _USER_DIR is not None:
        try:
            await mirror_user_skills_to_host(env, dest_root=_USER_DIR)
        except Exception as exc:
            logger.warning("skill resync from env interface failed: %s", exc)
    result = refresh_skills()
    if _TOOLSET is not None and _USER_DIR is not None:
        _mirror_cache[str(_USER_DIR)] = (
            _USER_DIR,
            _TOOLSET._skills,
            bound_skill_catalog(),
        )
    return result


async def bind_session_skills_callback(callback_context: Any) -> None:
    """``before_agent_callback`` that retargets the live ``SkillToolset``
    at the active session's workspace.

    lha's working_dir is per-user and, in sandbox mode, lives behind the
    env interface rather than on the host. We mirror ``<workspace>/.agents/skills/`` from
    the interface into a per-workspace host cache, then re-point the single
    module-level toolset at that mirror and refresh ``_skills`` in place.
    Builtins are constant across sessions.
    """
    if _TOOLSET is None:
        return None
    try:
        from app.environment_context import active_environment

        env = active_environment()
    except RuntimeError:
        return None

    mirror_root = host_mirror_dir(
        Path(env.working_dir), getattr(env, "owner", None)
    )
    builtin_dir = builtin_skills_root()
    try:
        cached = _mirror_cache.get(str(mirror_root))
        if cached is None:
            user_dir = await mirror_user_skills_to_host(
                env, dest_root=mirror_root
            )
            bind_toolset(_TOOLSET, user_dir=user_dir, builtin_dir=builtin_dir)
            _TOOLSET._skills = walk_skill_dirs(
                user_dir=user_dir, builtin_dir=builtin_dir
            )
            catalog = bound_skill_catalog()
            _mirror_cache[str(mirror_root)] = (
                user_dir,
                _TOOLSET._skills,
                catalog,
            )
        else:
            user_dir, skills, catalog = cached
            bind_toolset(_TOOLSET, user_dir=user_dir, builtin_dir=builtin_dir)
            _TOOLSET._skills = skills
        callback_context.state[BOUND_SKILLS_STATE_KEY] = catalog
    except Exception as exc:
        logger.warning("session-start skill reload failed: %s", exc)
    return None
