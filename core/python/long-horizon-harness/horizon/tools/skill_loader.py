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

"""Bridge lha's dual-root skill layout onto ADK's ``SkillToolset``.

User skills live under ``<workspace>/.agents/skills/<name>/SKILL.md`` — the
Skills CLI / ``npx skills add`` staging location and the ecosystem standard,
where the agent also authors and curates; built-ins ship with the package at
``horizon/builtin_skills/<name>/SKILL.md``. ADK's
``SkillToolset`` only loads from a single dict, so we walk both roots
ourselves and let the user side shadow the built-in on name collision.

The returned toolset is a ``HorizonSkillToolset`` (short preamble instead of
ADK's ~2 KB tutorial) with its ``_tools`` list adjusted: ``ListSkillsTool``
is dropped — that flips ``process_llm_request`` into auto-injecting the
``<available_skills>`` XML catalog every turn, which is the in-prompt skill
index we want — and ADK's ``LoadSkillTool`` / ``LoadSkillResourceTool`` /
``RunSkillScriptTool`` trio is replaced with horizon's merged
``LoadSkillTool`` (``load_skill(skill_name)`` for instructions,
``load_skill(skill_name, resource=...)`` for a bundled file; no successor
for running a script — use ``bash``).
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from google.adk.skills import load_skill_from_dir, models
from google.adk.tools.skill_toolset import (
    ListSkillsTool,
    LoadSkillResourceTool,
    RunSkillScriptTool,
    SkillToolset,
)
from google.adk.tools.skill_toolset import (
    LoadSkillTool as _AdkLoadSkillTool,
)

from horizon.tools.skill_toolset import HorizonSkillToolset, LoadSkillTool

logger = logging.getLogger(__name__)

_LIST_LIMIT = 1000


def builtin_skills_root() -> Path:
    return Path(__file__).resolve().parent.parent / "builtin_skills"


# User skills live under ``<workspace>/.agents/skills/`` — the Skills CLI
# (``npx skills add``) staging location and the ecosystem standard. The agent
# authors + curates skills there too; there is no separate ``skills/`` root.
_USER_SKILLS_SUBDIR = ".agents/skills"


async def mirror_user_skills_to_host(env: Any, *, dest_root: Path) -> Path:
    """Copy ``<env.working_dir>/.agents/skills`` from the env interface into ``dest_root``.

    The host loader (``walk_skill_dirs``) reads with raw pathlib, but in the
    sandbox backend ``env.working_dir`` is the in-container ``/workspace`` — not
    a host path. Mirroring through the interface's async ``list_directory`` /
    ``read_file`` makes the catalog work for both backends. A missing source
    skills dir yields an empty-but-existing ``dest_root``.
    """
    dest_root.parent.mkdir(parents=True, exist_ok=True)
    src_root = Path(env.working_dir) / _USER_SKILLS_SUBDIR

    # Mirror into a temp sibling and swap in only on full success — a failed
    # re-mirror must never leave the live catalog dir empty.
    tmp_root = dest_root.with_name(f"{dest_root.name}.tmp-{uuid.uuid4().hex}")
    try:
        await _list_dir(env, src_root)
    except FileNotFoundError:
        tmp_root.mkdir(parents=True, exist_ok=True)
        _swap_in(tmp_root, dest_root)
        return dest_root

    tmp_root.mkdir(parents=True, exist_ok=True)
    try:
        await _mirror_tree(env, src_root, tmp_root)
        _swap_in(tmp_root, dest_root)
    except BaseException:
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise
    return dest_root


def _swap_in(tmp_root: Path, dest_root: Path) -> None:
    if dest_root.exists():
        shutil.rmtree(dest_root)
    os.replace(tmp_root, dest_root)


async def _mirror_tree(env: Any, src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for entry in await _list_dir(env, src):
        name = entry["name"]
        kind = entry.get("kind")
        # Skills are plain files/dirs; symlinks aren't needed and mirroring a
        # symlink-to-dir via read_file would raise.
        if kind == "symlink":
            continue
        child_src = src / name
        child_dest = dest / name
        try:
            if kind == "dir":
                await _mirror_tree(env, child_src, child_dest)
            else:
                child_dest.parent.mkdir(parents=True, exist_ok=True)
                child_dest.write_bytes(await env.read_file(child_src))
        except FileNotFoundError as exc:
            # One transient/racy entry must not drop the rest of the catalog.
            logger.warning("Skipping skill entry %s: %s", child_src, exc)


async def _list_dir(env: Any, path: Path) -> list[dict[str, Any]]:
    list_directory = getattr(env, "list_directory", None)
    if list_directory is None:
        return _list_dir_host(path)
    entries, truncated = await list_directory(path, limit=_LIST_LIMIT)
    if truncated:
        logger.warning(
            "Skill listing of %s truncated at %d entries — some skills may be missing",
            path,
            _LIST_LIMIT,
        )
    return entries


def _list_dir_host(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    return [
        {"name": child.name, "kind": "dir" if child.is_dir() else "file"}
        for child in sorted(path.iterdir())
    ]


def _load_one(skill_dir: Path) -> models.Skill | None:
    try:
        return load_skill_from_dir(skill_dir)
    except Exception as exc:
        logger.warning("Skipping skill at %s: %s", skill_dir, exc)
        return None


def _walk_root(root: Path) -> dict[str, models.Skill]:
    out: dict[str, models.Skill] = {}
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if (
            not (child / "SKILL.md").exists()
            and not (child / "skill.md").exists()
        ):
            continue
        skill = _load_one(child)
        if skill is None:
            continue
        out[skill.frontmatter.name] = skill
    return out


def walk_skill_dirs(
    *, user_dir: Path, builtin_dir: Path
) -> dict[str, models.Skill]:
    """Return ``{name: Skill}`` merged from both roots, user shadowing builtin.

    Missing dirs are treated as empty. Malformed skills are skipped with a
    warning so one bad SKILL.md never poisons the rest of the catalog.
    """
    skills: dict[str, models.Skill] = {}
    for name, skill in _walk_root(builtin_dir).items():
        skills[name] = skill
    for name, skill in _walk_root(user_dir).items():
        skills[name] = skill
    return skills


def build_skill_toolset(
    *, user_dir: Path, builtin_dir: Path | None = None
) -> SkillToolset:
    """Construct a ``HorizonSkillToolset`` ready to plug into ``Agent(tools=[...])``.

    ``ListSkillsTool`` is removed so ADK auto-injects the
    ``<available_skills>`` catalog into the system prompt on every turn —
    the model never has to spend a round trip on ``list_skills``. ADK's
    ``LoadSkillTool`` / ``LoadSkillResourceTool`` / ``RunSkillScriptTool``
    are replaced with horizon's merged ``LoadSkillTool`` (see
    ``horizon.tools.skill_toolset``).
    """
    resolved_builtin = (
        builtin_dir if builtin_dir is not None else builtin_skills_root()
    )
    skills_dict = walk_skill_dirs(
        user_dir=user_dir, builtin_dir=resolved_builtin
    )
    toolset = HorizonSkillToolset(skills=list(skills_dict.values()))
    toolset._tools = [
        t
        for t in toolset._tools
        if not isinstance(
            t,
            (
                ListSkillsTool,
                _AdkLoadSkillTool,
                LoadSkillResourceTool,
                RunSkillScriptTool,
            ),
        )
    ]
    toolset._tools.append(LoadSkillTool(toolset))
    return toolset
