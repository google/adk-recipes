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

"""Constant prompt prefix + context tier — deterministic tests.

Covers ``build_static_instruction`` (the whole constant prefix, now placed
by ADK's own request processor as ``Agent.static_instruction`` rather than
hand-rolled per session) and the context-file AGENTS.md / CLAUDE.md /
.cursorrules loaders that still ride the per-turn callback.

Two tiers remain (the third, volatile, always lived in the reminder tail):

  * static  — ``Agent.static_instruction``, built once by
              ``build_static_instruction()`` at App-build time
              (``horizon/agent.py:_build_app_object``). Never touched by
              ``system_prompt_assembly_callback``; testing
              ``build_static_instruction(...)`` output directly IS testing
              the assembled prefix now, since (unlike the old
              ROOT_AGENT_INSTRUCTION + build_stable_tier split) everything
              constant lives in this one function.
  * context — discovered AGENTS.md / CLAUDE.md / .cursorrules under cwd,
              appended to ``system_instruction`` every turn by
              ``system_prompt_assembly_callback``. Session-stable.

These tests assert on STATE (returned strings / mutated
``llm_request.config.system_instruction``), never on LLM response content.
Pytest is for code correctness; LLM behavior validation belongs in
evalsets.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.adk.models import LlmRequest

from horizon.conversation.soul_loader import (
    DEFAULT_AGENT_IDENTITY,
    load_soul_identity,
)
from horizon.conversation.system_prompt import (
    ACTING_GUIDANCE,
    CODE_EXECUTION_GUIDANCE,
    MEMORY_GUIDANCE,
    SAFETY_GUIDANCE,
    SKILLS_GUIDANCE,
    STYLE_GUIDANCE,
    build_context_block,
    build_static_instruction,
    discover_context_files,
    system_prompt_assembly_callback,
)

pytestmark = pytest.mark.asyncio


def _fake_callback_context(
    *, cwd: str | Path | None = None, state: dict | None = None
) -> object:
    """Duck-typed CallbackContext exposing .state and (optionally) a cwd hint."""
    return SimpleNamespace(state=state if state is not None else {}, cwd=cwd)


def _seed_request(system_instruction: str = "STATIC PREFIX") -> LlmRequest:
    """LlmRequest with system_instruction already populated.

    Mirrors production: by the time system_prompt_assembly_callback runs,
    ADK's own request processor has already placed Agent.static_instruction
    onto config.system_instruction (flows/llm_flows/instructions.py, which
    runs before any before_model_callback).
    """
    request = LlmRequest(model="gemini-3.7-flash")
    request.config.system_instruction = system_instruction
    return request


# =============================================================================
# 1. system_prompt_assembly_callback: context tier only, appended after
#    whatever static_instruction already put in system_instruction.
# =============================================================================


async def test_context_appended_after_existing_system_instruction(
    tmp_path: Path,
):
    (tmp_path / "AGENTS.md").write_text(
        "## Project rule: tests must hit InMemoryMemoryService.\n"
    )

    request = _seed_request()
    ctx = _fake_callback_context(cwd=tmp_path, state={"iteration": 0})

    with patch("os.getcwd", return_value=str(tmp_path)):
        result = await system_prompt_assembly_callback(ctx, request)

    assert result is None
    assembled = request.config.system_instruction
    assert isinstance(assembled, str)

    static_idx = assembled.index("STATIC PREFIX")
    context_idx = assembled.index("InMemoryMemoryService")
    assert static_idx < context_idx, (
        f"Context tier must follow the existing static prefix — got "
        f"static@{static_idx}, context@{context_idx} in:\n{assembled}"
    )


async def test_missing_context_files_leaves_system_instruction_unchanged(
    tmp_path: Path,
):
    """No AGENTS.md / CLAUDE.md / .cursorrules at cwd -> system_instruction
    is untouched (byte-identical to what static_instruction already set)."""
    assert discover_context_files(tmp_path) == []

    request = _seed_request()
    ctx = _fake_callback_context(cwd=tmp_path, state={"iteration": 0})
    with patch("os.getcwd", return_value=str(tmp_path)):
        await system_prompt_assembly_callback(ctx, request)

    assert request.config.system_instruction == "STATIC PREFIX"


async def test_context_tier_is_deterministic_across_calls(tmp_path: Path):
    """Two calls with the same cwd/content produce byte-identical output —
    required for the context-cache fingerprint to keep hitting."""
    (tmp_path / "AGENTS.md").write_text("Project rules.\n")
    ctx = _fake_callback_context(cwd=tmp_path, state={"iteration": 0})

    request_a = _seed_request()
    request_b = _seed_request()

    with patch("os.getcwd", return_value=str(tmp_path)):
        await system_prompt_assembly_callback(ctx, request_a)
        await system_prompt_assembly_callback(ctx, request_b)

    assert (
        request_a.config.system_instruction
        == request_b.config.system_instruction
    )


async def test_callback_never_reads_volatile_state(tmp_path: Path):
    """iteration/last_error must not perturb the context tier — those ride
    the reminder tail exclusively now."""
    ctx_early = _fake_callback_context(cwd=tmp_path, state={"iteration": 1})
    req_early = _seed_request()
    with patch("os.getcwd", return_value=str(tmp_path)):
        await system_prompt_assembly_callback(ctx_early, req_early)

    ctx_late = _fake_callback_context(
        cwd=tmp_path, state={"iteration": 9, "last_error": "terminal blew up"}
    )
    req_late = _seed_request()
    with patch("os.getcwd", return_value=str(tmp_path)):
        await system_prompt_assembly_callback(ctx_late, req_late)

    assert (
        req_early.config.system_instruction
        == req_late.config.system_instruction
    )
    assert "terminal blew up" not in req_late.config.system_instruction


# =============================================================================
# 2. SOUL.md identity loader
# =============================================================================


async def test_load_soul_identity_uses_soul_md_when_present(tmp_path: Path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("You are Project lha, a careful and concise assistant.\n")

    result = load_soul_identity(soul_path=soul)

    assert result == "You are Project lha, a careful and concise assistant."
    assert result != DEFAULT_AGENT_IDENTITY


async def test_load_soul_identity_falls_back_when_missing(tmp_path: Path):
    missing = tmp_path / "does-not-exist.md"

    result = load_soul_identity(soul_path=missing)

    assert result == DEFAULT_AGENT_IDENTITY


async def test_load_soul_identity_falls_back_when_empty(tmp_path: Path):
    empty = tmp_path / "SOUL.md"
    empty.write_text("   \n\n")

    result = load_soul_identity(soul_path=empty)

    assert result == DEFAULT_AGENT_IDENTITY


# =============================================================================
# 3. Identity is stated exactly once (cut F: no duplicate opener)
# =============================================================================


async def test_identity_appears_once_in_static_instruction(tmp_path: Path):
    instruction = build_static_instruction(
        tool_names=[],
        model_name="gemini-3.7-flash",
        has_code_executor=False,
        soul_path=tmp_path / "no-soul.md",
    )

    assert instruction.startswith(DEFAULT_AGENT_IDENTITY)
    # The old ROOT_AGENT_INSTRUCTION opened with a second, near-duplicate
    # identity sentence ahead of the soul tier — that must not come back.
    assert instruction.count("helpful") <= 1


# =============================================================================
# 4. Tool-conditional guidance blocks — gated on the real tool name, not a
#    dead placeholder ("skill", "session_search") the old code used.
# =============================================================================


async def test_memory_guidance_present_when_memory_tool_loaded(tmp_path: Path):
    from horizon.tools import names

    instruction = build_static_instruction(
        tool_names=[names.MEMORY, names.PRELOAD_MEMORY],
        model_name="gemini-3.7-flash",
        soul_path=tmp_path / "no-soul.md",
    )

    assert MEMORY_GUIDANCE in instruction


async def test_memory_guidance_absent_when_memory_tool_not_loaded(
    tmp_path: Path,
):
    instruction = build_static_instruction(
        tool_names=["read", "bash"],
        model_name="gemini-3.7-flash",
        soul_path=tmp_path / "no-soul.md",
    )

    assert MEMORY_GUIDANCE not in instruction


async def test_skills_guidance_present_when_load_skill_tool_loaded(
    tmp_path: Path,
):
    # Real tool name, not the dead "skill" placeholder the old gate used.
    from horizon.tools import names

    instruction = build_static_instruction(
        tool_names=[names.LOAD_SKILL],
        model_name="gemini-3.7-flash",
        soul_path=tmp_path / "no-soul.md",
    )

    assert SKILLS_GUIDANCE in instruction
    # And the rewritten text must reference the real authoring path, not
    # the nonexistent skill(action='create'/'patch') calls it used to.
    # (load_skill(action='reload') is the real, legitimate action-dispatch
    # call and deliberately contains the substring "skill(action=" too, so
    # the negative check names the exact bogus actions, not the substring.)
    assert "skill(action='create'" not in SKILLS_GUIDANCE
    assert "skill(action='patch'" not in SKILLS_GUIDANCE


async def test_skills_guidance_absent_when_load_skill_tool_not_loaded(
    tmp_path: Path,
):
    instruction = build_static_instruction(
        tool_names=["read", "bash"],
        model_name="gemini-3.7-flash",
        soul_path=tmp_path / "no-soul.md",
    )

    assert SKILLS_GUIDANCE not in instruction


async def test_cross_session_recall_guidance_present_when_memory_tool_loaded(
    tmp_path: Path,
):
    """session_search folded into memory(action='search') — the
    cross-session-recall paragraph now lives inside MEMORY_GUIDANCE, gated
    on names.MEMORY like the rest of that block."""
    from horizon.tools import names

    instruction = build_static_instruction(
        tool_names=[names.MEMORY],
        model_name="gemini-3.7-flash",
        soul_path=tmp_path / "no-soul.md",
    )

    assert "Cross-session recall" in instruction
    assert "action='search'" in instruction


async def test_cross_session_recall_guidance_absent_when_memory_tool_not_loaded(
    tmp_path: Path,
):
    instruction = build_static_instruction(
        tool_names=["read", "bash"],
        model_name="gemini-3.7-flash",
        soul_path=tmp_path / "no-soul.md",
    )

    assert "Cross-session recall" not in instruction


async def test_artifact_html_guidance_is_gone():
    """ARTIFACT_HTML_GUIDANCE duplicated the artifact tool's own description
    and was deleted outright (single-source-of-truth), not merged."""
    import horizon.conversation.system_prompt as sp

    assert not hasattr(sp, "ARTIFACT_HTML_GUIDANCE")


# =============================================================================
# 5. Acting/Safety/Style — the three blocks that replaced the old nine
#    (TOOL_USE_ENFORCEMENT, OUTPUT_STYLE, TOOL_USE_SAFETY, SUBAGENT_USAGE,
#    PLANNING_DISCIPLINE, FAILURE_LOOP, FILESYSTEM_WRITE,
#    GOOGLE_MODEL_OPERATIONAL, WEB_RESEARCH_CITATION).
# =============================================================================


async def test_acting_safety_style_present_for_enforcement_model(
    tmp_path: Path,
):
    instruction = build_static_instruction(
        tool_names=["read_file"],
        model_name="gemini-3.7-flash",
        soul_path=tmp_path / "no-soul.md",
    )

    assert ACTING_GUIDANCE in instruction
    assert SAFETY_GUIDANCE in instruction
    assert STYLE_GUIDANCE in instruction


async def test_acting_safety_style_absent_for_non_enforcement_model(
    tmp_path: Path,
):
    instruction = build_static_instruction(
        tool_names=["read_file"],
        model_name="claude-3-5-sonnet",
        soul_path=tmp_path / "no-soul.md",
    )

    assert ACTING_GUIDANCE not in instruction
    assert SAFETY_GUIDANCE not in instruction
    assert STYLE_GUIDANCE not in instruction


async def test_acting_safety_style_absent_when_no_tools_loaded(tmp_path: Path):
    """No tools -> no enforcement scaffolding on an otherwise-conversational
    agent answering 'hi'."""
    instruction = build_static_instruction(
        tool_names=[],
        model_name="gemini-3.7-flash",
        soul_path=tmp_path / "no-soul.md",
    )

    assert ACTING_GUIDANCE not in instruction
    assert SAFETY_GUIDANCE not in instruction
    assert STYLE_GUIDANCE not in instruction


async def test_enforcement_env_force_off(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LHA_TOOL_USE_ENFORCEMENT", "off")

    instruction = build_static_instruction(
        tool_names=["read_file"],
        model_name="gemini-3.7-flash",
        soul_path=tmp_path / "no-soul.md",
    )

    assert ACTING_GUIDANCE not in instruction


async def test_enforcement_env_force_on(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LHA_TOOL_USE_ENFORCEMENT", "on")

    instruction = build_static_instruction(
        tool_names=["read_file"],
        model_name="claude-3-5-sonnet",
        soul_path=tmp_path / "no-soul.md",
    )

    assert ACTING_GUIDANCE in instruction
    assert SAFETY_GUIDANCE in instruction
    assert STYLE_GUIDANCE in instruction


async def test_memory_guidance_lists_do_save_categories():
    assert "durable preferences" in MEMORY_GUIDANCE
    assert "stated rationale" in MEMORY_GUIDANCE


async def test_memory_guidance_warns_against_derivable_facts():
    assert "re-derivable from the code" in MEMORY_GUIDANCE


async def test_memory_guidance_requires_why_clause():
    assert '"why" clause' in MEMORY_GUIDANCE


async def test_memory_guidance_keeps_recall_new_redundant_distinction():
    """memory_recall.evalset.json grades exactly this three-way decision
    procedure — it must survive the merge with the old ROOT_AGENT_INSTRUCTION
    contract, not just MEMORY_GUIDANCE's do/don't lists."""
    assert "RECALL" in MEMORY_GUIDANCE
    assert "REDUNDANT" in MEMORY_GUIDANCE
    assert "memory once" in MEMORY_GUIDANCE


# =============================================================================
# 6. Code-execution guidance — conditional on has_code_executor, not on
#    whether ROOT_AGENT_INSTRUCTION happened to mention it unconditionally.
# =============================================================================


async def test_code_execution_guidance_absent_without_executor(tmp_path: Path):
    instruction = build_static_instruction(
        tool_names=["read_file"],
        model_name="gemini-3.7-flash",
        has_code_executor=False,
        soul_path=tmp_path / "no-soul.md",
    )

    assert CODE_EXECUTION_GUIDANCE not in instruction


async def test_code_execution_guidance_present_with_executor(tmp_path: Path):
    instruction = build_static_instruction(
        tool_names=["read_file"],
        model_name="gemini-3.7-flash",
        has_code_executor=True,
        soul_path=tmp_path / "no-soul.md",
    )

    assert CODE_EXECUTION_GUIDANCE in instruction


# =============================================================================
# 7. Context discovery is first-match-wins, wrapped with header
# =============================================================================


async def test_context_discovery_is_first_match_wins(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("AGENTS rule: be careful.\n")
    (tmp_path / "CLAUDE.md").write_text("CLAUDE rule: be bold.\n")
    (tmp_path / ".cursorrules").write_text("cursor rule: be terse.\n")

    found = discover_context_files(tmp_path)

    assert len(found) == 1
    assert found[0][0] == "AGENTS.md"


async def test_context_discovery_prefers_lha_md_over_agents_md(tmp_path: Path):
    (tmp_path / ".horizon.md").write_text("lha config wins.\n")
    (tmp_path / "AGENTS.md").write_text("AGENTS rule: be careful.\n")

    found = discover_context_files(tmp_path)

    assert len(found) == 1
    assert found[0][0] == ".horizon.md"


async def test_context_block_has_project_context_header(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("project rules here\n")

    block = build_context_block(tmp_path)

    assert block is not None
    assert block.startswith("# Project Context")
    assert "## AGENTS.md" in block
    assert "project rules here" in block


# =============================================================================
# 8. Environment hints — build_environment_hints itself is unchanged (see
#    tests/unit/test_runtime_env_hints.py); these confirm it no longer rides
#    build_static_instruction, since it moved to the reminder tail.
# =============================================================================


async def test_static_instruction_excludes_environment_hint(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    instruction = build_static_instruction(
        tool_names=[],
        model_name="gemini-3.7-flash",
        soul_path=tmp_path / "no-soul.md",
    )

    # cwd is no longer a build_static_instruction parameter at all — the env
    # hint (and its cwd-derived project-type detection) rides the reminder
    # tail via build_environment_reminder instead.
    assert "Python project" not in instruction
    assert str(tmp_path) not in instruction


async def test_static_instruction_byte_stable_across_calls(tmp_path: Path):
    """Pure function: identical inputs -> byte-identical output, every
    time — required for prefix-cache warmth now that ADK, not horizon,
    owns placement."""
    first = build_static_instruction(
        tool_names=["read", "bash"],
        model_name="gemini-3.7-flash",
        soul_path=tmp_path / "no-soul.md",
    )
    second = build_static_instruction(
        tool_names=["read", "bash"],
        model_name="gemini-3.7-flash",
        soul_path=tmp_path / "no-soul.md",
    )
    assert first == second


async def test_static_instruction_has_no_session_state_dependency():
    """build_static_instruction takes no callback_context/session.state at
    all — the old _ensure_stable_tier / _STABLE_TIER_STATE_KEY caching is
    gone, not just unused."""
    import inspect

    params = inspect.signature(build_static_instruction).parameters
    assert "callback_context" not in params
    assert "state" not in params

    import horizon.conversation.system_prompt as sp

    assert not hasattr(sp, "_ensure_stable_tier")
    assert not hasattr(sp, "_STABLE_TIER_STATE_KEY")
