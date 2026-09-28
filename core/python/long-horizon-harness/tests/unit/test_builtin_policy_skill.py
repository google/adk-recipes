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

"""Smoke test for the bundled ``policy`` built-in skill.

Catches "forgot to commit the file" and "introduced a YAML typo" by
loading the skill via ADK's loader — the same one ``skill_loader.walk_skill_dirs``
delegates to.
"""

from __future__ import annotations

from pathlib import Path

_ALLOWED_SUBDIRS = frozenset({"references", "assets", "scripts"})


def test_policy_builtin_exists_and_parses():
    from google.adk.skills import load_skill_from_dir

    from app.tools.skill_loader import builtin_skills_root

    skill_dir = builtin_skills_root() / "policy"
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file(), f"missing builtin: {skill_md}"

    skill = load_skill_from_dir(skill_dir)
    assert skill.frontmatter.name == "policy"
    assert skill.frontmatter.description


def test_policy_builtin_directory_layout():
    """Built-in skill dir holds only SKILL.md (+ optional supporting subdirs).

    Guards against accidentally committing scratch files that would
    end up shipped in the wheel.
    """
    from app.tools.skill_loader import builtin_skills_root

    policy_dir = builtin_skills_root() / "policy"
    for entry in policy_dir.iterdir():
        if entry.is_file():
            assert entry.name == "SKILL.md", (
                f"unexpected file in builtin skill dir: {entry}"
            )
        else:
            assert entry.name in _ALLOWED_SUBDIRS, (
                f"unexpected subdir in builtin skill dir: {entry}"
            )


def test_policy_builtin_packaged_with_app():
    """The builtin lives under the installed ``app/`` package — so it
    rides in the wheel as long as packaging includes non-.py data files
    under app/. If this test fails, ``builtin_skills_root()`` is pointing
    somewhere unexpected."""
    from app import tools as app_tools

    app_root = Path(app_tools.__file__).resolve().parent.parent
    expected = app_root / "builtin_skills" / "policy" / "SKILL.md"
    assert expected.is_file(), f"builtin not under app package: {expected}"
