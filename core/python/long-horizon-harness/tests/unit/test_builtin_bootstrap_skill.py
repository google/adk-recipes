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

"""Smoke test for the bundled ``bootstrap-google-tools`` built-in skill.

Catches "forgot to commit the file" and "introduced a YAML typo" by
loading the skill via ADK's loader — the same one ``skill_loader.walk_skill_dirs``
delegates to.
"""

from __future__ import annotations

from pathlib import Path

_ALLOWED_SUBDIRS = frozenset({"references", "assets", "scripts"})


def test_bootstrap_builtin_exists_and_parses():
    from google.adk.skills import load_skill_from_dir

    from app.tools.skill_loader import builtin_skills_root

    skill_dir = builtin_skills_root() / "bootstrap-google-tools"
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file(), f"missing builtin: {skill_md}"

    skill = load_skill_from_dir(skill_dir)
    assert skill.frontmatter.name == "bootstrap-google-tools"
    assert skill.frontmatter.description


def test_bootstrap_builtin_directory_layout():
    from app.tools.skill_loader import builtin_skills_root

    skill_dir = builtin_skills_root() / "bootstrap-google-tools"
    for entry in skill_dir.iterdir():
        if entry.is_file():
            assert entry.name == "SKILL.md", (
                f"unexpected file in builtin skill dir: {entry}"
            )
        else:
            assert entry.name in _ALLOWED_SUBDIRS, (
                f"unexpected subdir in builtin skill dir: {entry}"
            )


def test_bootstrap_builtin_packaged_with_app():
    from app import tools as app_tools

    app_root = Path(app_tools.__file__).resolve().parent.parent
    expected = (
        app_root / "builtin_skills" / "bootstrap-google-tools" / "SKILL.md"
    )
    assert expected.is_file(), f"builtin not under app package: {expected}"


def test_bootstrap_token_path_precedes_loopback_login():
    from app.tools.skill_loader import builtin_skills_root

    text = (
        builtin_skills_root() / "bootstrap-google-tools" / "SKILL.md"
    ).read_text()
    token_at = text.find("GOOGLE_WORKSPACE_CLI_TOKEN")
    login_at = text.find("gws auth login")
    assert token_at != -1, "bootstrap skill must document the token path"
    assert login_at != -1
    assert token_at < login_at, (
        "the pre-injected token must be presented before the loopback "
        "login flow so the agent probes it first"
    )


def test_bootstrap_token_path_is_unconditional_probe():
    from app.tools.skill_loader import builtin_skills_root

    text = (
        (builtin_skills_root() / "bootstrap-google-tools" / "SKILL.md")
        .read_text()
        .lower()
    )
    # Anchor on the actual directive heading so the test fails if the
    # token-first guidance is removed — not just any "always"/"probe" word
    # elsewhere in the file (e.g. "almost always missing IAM").
    assert "first, always: probe the pre-injected token" in text, (
        "token-first guidance should be an unconditional probe-first directive"
    )
