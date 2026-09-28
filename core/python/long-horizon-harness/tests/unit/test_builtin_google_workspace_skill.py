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

"""Guards for the bundled ``google-workspace`` usage skill.

The skill is what an agent loads when a user asks about Gmail/Drive/Chat.
It MUST steer the agent to try the pre-injected ``GOOGLE_WORKSPACE_CLI_TOKEN``
before the OAuth loopback-login dance — the regression we hit when "can you
see my emails?" triggered a full ``gws auth login`` flow despite the token
being present and usable.
"""

from __future__ import annotations


def _skill_dir():
    from app.tools.skill_loader import builtin_skills_root

    return builtin_skills_root() / "google-workspace"


def test_google_workspace_builtin_exists_and_parses():
    from google.adk.skills import load_skill_from_dir

    skill_dir = _skill_dir()
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file(), f"missing builtin: {skill_md}"

    skill = load_skill_from_dir(skill_dir)
    assert skill.frontmatter.name == "google-workspace"
    assert skill.frontmatter.description


def test_google_workspace_mentions_pre_injected_token():
    text = (_skill_dir() / "SKILL.md").read_text()
    assert "GOOGLE_WORKSPACE_CLI_TOKEN" in text, (
        "usage skill must document the pre-injected token auth path"
    )


def test_google_workspace_puts_token_before_loopback_login():
    text = (_skill_dir() / "SKILL.md").read_text()
    token_at = text.find("GOOGLE_WORKSPACE_CLI_TOKEN")
    login_at = text.find("gws auth login")
    assert token_at != -1 and login_at != -1
    assert token_at < login_at, (
        "the token-first path must appear before the loopback login dance "
        "so the agent tries it first"
    )
