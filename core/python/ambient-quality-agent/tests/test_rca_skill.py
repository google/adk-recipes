# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the RCA skill definition.

Verifies skill loading, package bundling, prompt routing, the answer contract,
and that only read-only skill tools reach the agent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import ambient_quality_agent
from ambient_quality_agent.core import rca_skill, skill_toolset


def test_the_skill_loads_and_its_frontmatter_parses() -> None:
    """Verifies that `load_skill_from_dir` successfully parses the skill and validates frontmatter."""
    skill = rca_skill.load_rca_skill()

    assert skill.name == "rca"
    assert skill.description
    assert skill.instructions


def test_the_skill_ships_inside_the_installed_package() -> None:
    """Ensures SKILL.md is bundled inside the package rather than the excluded root directory."""
    package_root = Path(ambient_quality_agent.__file__).parent

    assert (rca_skill.SKILL_DIR / "SKILL.md").is_file()
    assert rca_skill.SKILL_DIR.resolve().is_relative_to(package_root.resolve())


def test_the_skill_answers_the_dashboards_diagnose_button() -> None:
    """Verifies the skill description contains keywords to match dashboard chat inquiries."""
    description = rca_skill.load_rca_skill().description.lower()

    assert "insight" in description
    assert "root cause" in description
    assert "fix" in description


def test_the_answer_contract_survives_an_edit_to_the_skill() -> None:
    """Ensures critical output contract requirements are present in the skill instructions."""
    instructions = rca_skill.load_rca_skill().instructions

    assert "unverified" in instructions
    assert "unified diff" in instructions
    assert "`<path>:<start>-<end>`" in instructions


def test_the_fix_is_recorded_rather_than_only_written_out() -> None:
    """Verifies that the skill contract requires calling record_root_cause with sighting arguments."""
    instructions = rca_skill.load_rca_skill().instructions

    assert "record_root_cause" in instructions
    for argument in ("insight_id", "occurrence_id", "summary", "edits"):
        assert argument in instructions


def test_the_written_answer_is_the_mechanism_and_the_caveat_only() -> None:
    """Verifies that the skill output contract excludes code fixes from prose to prevent UI duplication."""
    instructions = " ".join(rca_skill.load_rca_skill().instructions.split())

    assert "exactly two parts" in instructions
    assert "The fix is not one of them." in instructions
    assert "Never write the fix out" in instructions
    assert "shows the user the same edit twice" in instructions
    for forbidden in ("no path", "no line range", "no replacement text"):
        assert forbidden in instructions


def test_a_refused_record_is_the_only_time_the_fix_is_written_out() -> None:
    """Verifies that instructions permit writing fixes in prose only when the tool refuses the record."""
    instructions = " ".join(rca_skill.load_rca_skill().instructions.split())

    assert (
        "The one exception is a record the tool refused outright"
        in instructions
    )


def test_the_contract_says_every_call_carries_the_whole_edit_set() -> None:
    """Verifies that the contract specifies each call supersedes previous edits."""
    instructions = " ".join(rca_skill.load_rca_skill().instructions.split())

    assert "each call supersedes the last" in instructions
    assert "resend every edit you still stand behind" in instructions


def test_a_record_that_was_refused_is_reported_as_unsaved() -> None:
    """Verifies that refused records are explicitly communicated to the user as unsaved."""
    instructions = " ".join(rca_skill.load_rca_skill().instructions.split())

    assert "the record was not saved" in instructions


def test_the_procedure_passes_the_revision_explicitly() -> None:
    """The revision reaches the source tools as an argument, so the procedure must name it."""
    instructions = rca_skill.load_rca_skill().instructions

    assert "agent_revision" in instructions
    assert "revision=" in instructions
    for tool in ("list_source_files", "search_source", "read_source_file"):
        assert tool in instructions


def test_the_procedure_starts_from_get_insight_not_from_hidden_state() -> None:
    """The revision lookup is an ordinary read, so the procedure must name the tool it uses."""
    instructions = rca_skill.load_rca_skill().instructions

    assert "get_insight" in instructions
    assert "occurrence_id" in instructions


def test_the_procedure_widens_from_one_sighting_to_the_whole_insight() -> None:
    """get_full_trajectories scoping depends on its arguments, so instructions must document both."""
    instructions = rca_skill.load_rca_skill().instructions

    assert "get_full_trajectories" in instructions
    assert "occurrence_id=<occurrence_id>" in instructions
    assert "insight_id" in instructions
    # Under insight scope, the agent must read the revision from each trajectory entry.
    assert "scope" in instructions


def test_the_procedure_forbids_counting_occurrences_from_the_trajectories() -> (
    None
):
    """Trajectory reads are capped samples, so coverage fields must not be used as totals."""
    instructions = rca_skill.load_rca_skill().instructions

    assert "occurrences_truncated" in instructions
    assert "total_occurrences" in instructions
    # Normalize whitespace so phrase assertions survive paragraph reflowing.
    assert "Never state how often the issue happened" in " ".join(
        instructions.split()
    )


def test_the_contract_couples_the_source_revision_to_the_quoted_conversations() -> (
    None
):
    """Explicit arguments let the two diverge, so the answer contract has to forbid it."""
    instructions = rca_skill.load_rca_skill().instructions

    assert "Code and conversations must be the same revision" in instructions


def test_running_a_skill_script_is_not_offered() -> None:
    """Verifies `run_skill_script` is excluded from the exposed skill toolset."""
    tools = asyncio.run(skill_toolset.build_skill_toolset().get_tools())

    names = {tool.name for tool in tools}
    assert "run_skill_script" not in names
    assert names == {"list_skills", "load_skill", "load_skill_resource"}


def test_the_skill_bundles_no_scripts() -> None:
    """Ensures no executable scripts are bundled with the RCA skill."""
    assert rca_skill.load_rca_skill().resources.list_scripts() == []


def test_the_procedure_reads_the_developers_goal_first() -> None:
    """The goal says what the developer cares about, so the skill reads it
    before step 1 -- and says it is never by itself the defect."""
    instructions = rca_skill.load_rca_skill().instructions

    assert "### 0." in instructions
    assert "get_goal" in instructions
    assert instructions.index("get_goal") < instructions.index("### 1.")


def test_the_procedure_reads_the_memories_and_never_adds_one() -> None:
    """Memories are what a person asked AQuA to keep, so the skill reads them
    before step 1. A diagnosis is exactly where the model would be
    tempted to remember something, so the skill says it must not unless the
    developer asks."""
    instructions = " ".join(rca_skill.load_rca_skill().instructions.split())

    assert instructions.index("get_memories") < instructions.index("### 1.")
    assert "Never call `remember` on your own initiative" in instructions
