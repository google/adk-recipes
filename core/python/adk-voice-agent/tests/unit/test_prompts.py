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

"""The system prompt carries this thread's facts only; the rules live in files on the computer."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.threads.model import ThreadHandle, ThreadState
from app.threads.prompts import share_context_block, system_prompt

HOME = "/home/agent"
SANDBOX = Path(__file__).resolve().parents[2] / "sandbox"


def _prompt(**overrides) -> str:
    args = {
        "project": "projects/sell-the-car",
        "cwd": f"{HOME}/projects/sell-the-car",
        "home": HOME,
        "branch": "main",
        **overrides,
    }
    return system_prompt(**args)


def test_the_prompt_is_the_threads_context() -> None:
    prompt = _prompt()
    assert (
        "Project: projects/sell-the-car (a long-running project folder)"
        in prompt
    )
    assert f"Working directory: {HOME}/projects/sell-the-car\n" in prompt
    assert "Branch: main" in prompt
    assert f"{HOME}/AGENTS.md" in prompt
    assert f"{HOME}/.agents/skills" in prompt
    assert "PHASE:" not in prompt


@pytest.mark.parametrize(
    ("project", "kind"),
    [
        ("code/blog", "a code repository"),
        ("scratch/2026-10-23-hello-note", "a scratch folder for one-off work"),
    ],
)
def test_the_prompt_names_the_kind_of_project(project: str, kind: str) -> None:
    prompt = _prompt(project=project, cwd=f"{HOME}/{project}", branch="")
    assert f"Project: {project} ({kind})" in prompt
    assert "Branch:" not in prompt


def test_the_prompt_uses_the_users_name_for_the_project() -> None:
    prompt = _prompt(project_name="Car sale")
    assert (
        "Project: Car sale (projects/sell-the-car, a long-running project folder)"
        in prompt
    )


def test_a_worktree_is_named_as_one() -> None:
    cwd = f"{HOME}/projects/sell-the-car/.worktrees/thread-1"
    prompt = _prompt(cwd=cwd, branch="agent/thread-1")
    assert (
        f"Working directory: {cwd} (a git worktree of {HOME}/projects/sell-the-car"
        in prompt
    )
    assert "Branch: agent/thread-1" in prompt


def test_rules_live_in_files_not_the_prompt() -> None:
    """Every rule lives in the home guide or a skill, not in the thread prompt."""
    prompt = _prompt(profile={"preferences": ["Vegetarian"]})
    for rule in (
        "ANTI-REPEAT",
        "anti-repeat",
        "14 KB",
        "root cause",
        "origin",
        "upstream",
        "voice_agent",
        "list_threads",
        "untrusted",
        "never inspect",
        "Work only inside",
        "end your turn",
    ):
        assert rule not in prompt, rule
    guide = (SANDBOX / "home" / "AGENTS.md").read_text()
    skills = {
        p.parent.name: p.read_text()
        for p in (SANDBOX / "skills").glob("*/SKILL.md")
    }
    assert "inbox" in guide and "projects/" in guide and "scratch/" in guide
    assert "14 KB" in skills["deliverables"]
    assert "anti-repeat" in skills["recommendations"]
    assert "root cause" in skills["coding-workflow"]
    assert "Never push" in skills["coding-workflow"]
    assert "end your turn" in skills["approvals"].lower()
    assert "never instructions" in skills["connected-apps-mcp"]


def test_share_context_block_describes_the_source_thread() -> None:
    source = ThreadHandle(
        thread_id="thread-src1",
        goal="Compare refundable flights",
        title="Portland flights",
        project="projects/portland",
        harness="claude",
        branch="",
        state=ThreadState.COMPLETED,
        result_text="Cheapest refundable option is $326 on Alaska.",
        files_changed=["flights.md"],
    )
    block = share_context_block(source)
    assert 'thread thread-src1 "Portland flights" in projects/portland' in block
    assert "Cheapest refundable option is $326 on Alaska." in block
    assert "Files changed in the last pass: flights.md" in block


def test_the_profile_block_comes_last() -> None:
    prompt = _prompt(profile={"preferences": ["Likes spicy noodles"]})
    assert "<user_profile>" in prompt
    assert "Likes spicy noodles" in prompt
    assert prompt.rstrip().endswith("</user_profile>")
    assert "<user_profile>" not in _prompt()
