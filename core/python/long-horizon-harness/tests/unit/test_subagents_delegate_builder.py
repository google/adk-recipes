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

"""Lazy builder for delegated child agents.

(``_build_child_agent``): assemble a fresh ``LlmAgent`` per
``delegate(...)`` call, with goal + context in the prompt, tools
expanded from the requested toolsets, and SKILL.md content for each
requested skill appended to the prompt.

No registry cache — prompt is goal-specific so caching does not help.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from horizon.tools import names

pytestmark = pytest.mark.asyncio


# Child tools mix plain functions and BaseTool instances, so identity checks
# break the moment one becomes the other. Compare registered names instead.
def _tool_names(child) -> list[str]:
    return [
        getattr(t, "name", None) or getattr(t, "__name__", "")
        for t in child.tools
    ]


def _write_skill(
    root: Path, name: str, body: str, description: str = "test"
) -> None:
    from horizon.tools.skill_reload import host_mirror_dir

    frontmatter = yaml.safe_dump(
        {"name": name, "description": description}, sort_keys=True
    )
    content = f"---\n{frontmatter}---\n\n{body}\n"
    skill_dir = root / ".agents" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    # The delegate builder reads from the per-session host mirror that
    # bind_session_skills_callback populates at runtime; mirror here too.
    mirror_dir = host_mirror_dir(root) / name
    mirror_dir.mkdir(parents=True, exist_ok=True)
    (mirror_dir / "SKILL.md").write_text(content, encoding="utf-8")


@pytest.fixture()
def skills_home(tmp_path: Path):
    import shutil

    from horizon.environment import LocalEnvironment
    from horizon.environment_context import (
        clear_active_environment,
        set_active_environment,
    )
    from horizon.tools.skill_reload import host_mirror_dir

    set_active_environment(LocalEnvironment(working_dir=tmp_path))
    try:
        yield tmp_path
    finally:
        clear_active_environment()
        shutil.rmtree(host_mirror_dir(tmp_path), ignore_errors=True)


async def test_build_child_agent_returns_llm_agent(skills_home: Path):
    from google.adk.agents import Agent

    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="do thing", context="", toolsets=["file"], skills=[]
    )
    assert isinstance(child, Agent)


async def test_build_child_agent_default_toolsets_used_when_empty(
    skills_home: Path,
):
    """``toolsets=[]`` triggers DEFAULT_TOOLSETS (file + shell)."""
    from horizon.subagents.delegate_builder import build_child_agent
    from horizon.tools.processes.terminal import bash

    child = await build_child_agent(
        goal="x", context="", toolsets=[], skills=[]
    )
    tools = set(child.tools)
    assert names.READ in _tool_names(child)
    assert bash in tools


async def test_build_child_agent_blocklist_enforced(skills_home: Path):
    """``memory``, ``clarify``, the subagent tool itself, and the
    skill-registry tools must NOT appear on the child even if a future
    toolset accidentally includes them."""
    from horizon.memory import memory
    from horizon.subagents.delegate_builder import (
        _BLOCKED_TOOL_NAMES,
        build_child_agent,
    )
    from horizon.tools.clarify import clarify

    child = await build_child_agent(
        goal="x", context="", toolsets=["file", "shell"], skills=[]
    )
    tools = set(child.tools)
    assert memory not in tools
    assert clarify not in tools
    # load_skill covers both reading a skill and action='reload' (the
    # former standalone reload tool, folded into it).
    for blocked in ("subagent", "memory", "clarify", "load_skill"):
        assert blocked in _BLOCKED_TOOL_NAMES


async def test_build_child_agent_unknown_toolset_raises(skills_home: Path):
    from horizon.subagents.delegate_builder import build_child_agent

    with pytest.raises(KeyError):
        await build_child_agent(
            goal="x", context="", toolsets=["bogus"], skills=[]
        )


async def test_build_child_agent_injects_goal_and_context_into_prompt(
    skills_home: Path,
):
    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="Summarize /tmp/foo.txt",
        context="The file was generated yesterday at 4pm by the ingest job.",
        toolsets=["file"],
        skills=[],
    )
    instruction = child.instruction
    assert "Summarize /tmp/foo.txt" in instruction
    assert "generated yesterday at 4pm" in instruction


async def test_build_child_agent_injects_skill_content(skills_home: Path):
    _write_skill(
        skills_home,
        "gws-drive",
        body="Run `gws drive files list` to enumerate.",
    )
    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="enumerate Drive docs",
        context="",
        toolsets=["shell"],
        skills=["gws-drive"],
    )
    assert "gws-drive" in child.instruction
    assert "gws drive files list" in child.instruction


async def test_build_child_agent_skips_missing_skill_silently(
    skills_home: Path,
):
    """A missing skill name is an LLM mistake but shouldn't blow up the
    delegate — surface it as a note in the prompt so the child can adapt.
    The dispatch layer surfaces it to the parent too via telemetry."""
    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="do something",
        context="",
        toolsets=["file"],
        skills=["does-not-exist"],
    )
    assert "does-not-exist" in child.instruction
    # No exception raised — child is built and ready to run.
    assert child.tools


async def test_build_child_agent_no_memory_no_clarify_no_subagent_recursion(
    skills_home: Path,
):
    """Three properties together: (a) child has no PreloadMemoryTool,
    (b) child has no clarify tool, (c) child has no subagent tool."""
    from google.adk.tools.preload_memory_tool import PreloadMemoryTool

    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="x", context="", toolsets=["file"], skills=[]
    )
    for tool in child.tools:
        assert not isinstance(tool, PreloadMemoryTool)
        # Recursion guard: no callable named "subagent" on the child.
        name = getattr(tool, "__name__", None) or getattr(tool, "name", None)
        assert name != "subagent"


async def test_build_child_agent_uses_isolated_name(skills_home: Path):
    """Child name must not collide with the root or any wired sub-agent."""
    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="x", context="", toolsets=["file"], skills=[]
    )
    assert child.name not in {"root_agent", "web_research"}
    # Allow a stable prefix so traces are searchable.
    assert child.name.startswith("delegate_")


async def test_build_child_agent_smoke_with_real_env(skills_home: Path):
    """ADK constructs the underlying model client at agent-init time;
    smoke-build to verify nothing in the assembly path raises."""
    # Make sure no Vertex calls happen — the child is built but never run.
    os.environ.pop("GOOGLE_CLOUD_PROJECT", None)

    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="enumerate docs",
        context="",
        toolsets=["shell", "file"],
        skills=[],
    )
    assert child.model is not None


async def test_build_child_agent_accepts_model_override(skills_home: Path):
    """Caller can pin a different Gemini variant (e.g. pro for deep
    reasoning); model_name flows through to the underlying Gemini model."""
    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="x",
        context="",
        toolsets=["file"],
        skills=[],
        model_name="gemini-3.1-pro-preview",
    )
    assert child.model.model == "gemini-3.1-pro-preview"


async def test_build_child_agent_rejects_unknown_model(skills_home: Path):
    """An off-allowlist model name is most likely an LLM typo. Fail loudly
    in the builder so the delegate layer surfaces a structured error rather
    than letting Vertex 404 at first use."""
    import pytest

    from horizon.subagents.delegate_builder import build_child_agent

    with pytest.raises(ValueError, match="model"):
        await build_child_agent(
            goal="x",
            context="",
            toolsets=["file"],
            skills=[],
            model_name="not-a-real-model",
        )


async def test_build_child_agent_tools_param_merges_with_toolsets(
    skills_home: Path,
):
    """`tools=[...]` is the fine-grained counterpart to `toolsets=[...]`.
    Both populate the child; the union is deduplicated; blocklist still
    applies."""
    from horizon.subagents.delegate_builder import build_child_agent
    from horizon.tools.processes.terminal import bash

    child = await build_child_agent(
        goal="x",
        context="",
        toolsets=["file"],
        skills=[],
        tools=["bash", "read"],
    )
    tool_set = set(child.tools)
    assert names.READ in _tool_names(child)
    assert bash in tool_set
    # No duplicates from the read overlap between toolset and tools list.
    assert sum(1 for t in _tool_names(child) if t == names.READ) == 1


async def test_build_child_agent_tools_only_no_toolsets(skills_home: Path):
    """`tools=[...]` with no `toolsets` skips DEFAULT_TOOLSETS — caller
    gets exactly what they asked for."""
    from horizon.subagents.delegate_builder import build_child_agent
    from horizon.tools.processes.terminal import bash

    child = await build_child_agent(
        goal="x",
        context="",
        toolsets=None,
        skills=[],
        tools=["read"],
    )
    tool_set = set(child.tools)
    assert names.READ in _tool_names(child)
    assert bash not in tool_set


async def test_build_child_agent_unknown_tool_name_raises(skills_home: Path):
    import pytest

    from horizon.subagents.delegate_builder import build_child_agent

    with pytest.raises(KeyError):
        await build_child_agent(
            goal="x",
            context="",
            toolsets=None,
            skills=[],
            tools=["not_a_real_tool"],
        )


async def test_build_child_agent_instructions_appended_under_caller_section(
    skills_home: Path,
):
    """Free-form `instructions` text shows up as its own labeled section in
    the child's system prompt — appended without clobbering the base
    instruction or goal/context."""
    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="enumerate docs",
        context="",
        toolsets=["file"],
        skills=[],
        instructions="Prefer markdown tables in the final summary.",
    )
    assert "## Caller instructions" in child.instruction
    assert "Prefer markdown tables in the final summary." in child.instruction
    # Base instruction still present.
    assert "delegated worker" in child.instruction


async def test_caller_instructions_appear_after_goal(skills_home: Path):
    """Caller instructions must come AFTER ## Goal and ## Context so they
    shape behavior on a known task instead of preempting the base
    contract before the goal is even stated."""
    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="enumerate docs",
        context="we are in a Python project",
        toolsets=["file"],
        skills=[],
        instructions="Prefer markdown tables in the final summary.",
    )
    text = child.instruction
    goal_at = text.index("## Goal")
    context_at = text.index("## Context")
    caller_at = text.index("## Caller instructions")
    assert goal_at < caller_at, "Caller instructions must come after ## Goal"
    assert context_at < caller_at, (
        "Caller instructions must come after ## Context"
    )


async def test_build_child_agent_inline_skills_render_like_disk_skills(
    skills_home: Path,
):
    """Inline skill bodies appear in the same `## Available skills` block
    as disk-loaded skills — caller doesn't have to author a SKILL.md to
    hand the child ad-hoc guidance."""
    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="x",
        context="",
        toolsets=["file"],
        skills=[],
        inline_skills=[
            {"name": "ad-hoc-guidance", "body": "Always echo the cwd first."},
        ],
    )
    assert "ad-hoc-guidance" in child.instruction
    assert "Always echo the cwd first." in child.instruction
    assert "## Available skills" in child.instruction


async def test_build_child_agent_disk_and_inline_skills_both_present(
    skills_home: Path,
):
    _write_skill(skills_home, "from-disk", body="Disk skill body.")
    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="x",
        context="",
        toolsets=["file"],
        skills=["from-disk"],
        inline_skills=[{"name": "inline-one", "body": "Inline body."}],
    )
    assert "Disk skill body." in child.instruction
    assert "Inline body." in child.instruction


async def test_build_child_agent_json_output_sets_output_schema_natively(
    skills_home: Path,
):
    """`output_format='json'` plus a schema flows the schema through to the
    child's ``output_schema`` field. ADK 2.0 enforces the shape at
    generation time via the SetModelResponseTool path; we no longer embed
    the schema in the prompt."""
    from horizon.subagents.delegate_builder import build_child_agent

    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    child = await build_child_agent(
        goal="x",
        context="",
        toolsets=["file"],
        skills=[],
        output_format="json",
        output_schema=schema,
    )
    assert child.output_schema == schema


async def test_build_child_agent_json_output_no_schema_keeps_prompt_hint(
    skills_home: Path,
):
    """`output_format='json'` without a schema falls back to a prompt-level
    hint so the child still knows to emit raw JSON."""
    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="x",
        context="",
        toolsets=["file"],
        skills=[],
        output_format="json",
    )
    assert "JSON" in child.instruction
    assert child.output_schema is None


async def test_build_child_agent_custom_name_sanitized_and_suffixed(
    skills_home: Path,
):
    """Caller-supplied name is sanitized and uuid-suffixed to keep trace
    names searchable AND unique across concurrent delegates."""
    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="x",
        context="",
        toolsets=["file"],
        skills=[],
        name="research lru cache!!",
    )
    # Spaces / punctuation stripped; uuid suffix appended.
    assert child.name.startswith(
        "research_lru_cache_"
    ) or child.name.startswith("research-lru-cache_")
    assert len(child.name) > len("research_lru_cache_")  # uuid suffix present


async def test_build_child_agent_name_excessively_long_truncated(
    skills_home: Path,
):
    from horizon.subagents.delegate_builder import build_child_agent

    huge = "a" * 200
    child = await build_child_agent(
        goal="x",
        context="",
        toolsets=["file"],
        skills=[],
        name=huge,
    )
    # Sanitized prefix capped at 32 chars before the uuid suffix.
    prefix_before_uuid = child.name.rsplit("_", 1)[0]
    assert len(prefix_before_uuid) <= 32


async def test_build_child_agent_explore_profile_restricts_tools(
    skills_home: Path,
):
    """An ``explore`` child gets file-read + search only, even when the
    caller asked for the shell toolset."""
    from horizon.subagents.delegate_builder import build_child_agent
    from horizon.tools.file_ops import search_files, write
    from horizon.tools.processes.terminal import bash

    child = await build_child_agent(
        goal="scan the repo",
        context="",
        toolsets=["file", "shell"],
        skills=[],
        profile="explore",
    )
    tool_set = set(child.tools)
    assert names.READ in _tool_names(child)
    assert search_files in tool_set
    assert bash not in tool_set
    assert write not in tool_set


async def test_build_child_agent_unknown_profile_raises(skills_home: Path):
    from horizon.subagents.delegate_builder import build_child_agent

    with pytest.raises(KeyError):
        await build_child_agent(
            goal="x", context="", toolsets=["file"], skills=[], profile="bogus"
        )


async def test_build_child_agent_attaches_child_policy_guard(skills_home: Path):
    """Every child carries a before_tool_callback so it honors policy +
    inherited grants — not just the static blocklist."""
    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(
        goal="x", context="", toolsets=["file"], skills=[]
    )
    callbacks = child.before_tool_callback
    assert callbacks  # non-empty list
    assert callable(callbacks[0] if isinstance(callbacks, list) else callbacks)


async def test_build_child_agent_profile_none_keeps_full_toolset(
    skills_home: Path,
):
    """No profile = no allowlist restriction; the shell toolset survives."""
    from horizon.subagents.delegate_builder import build_child_agent
    from horizon.tools.processes.terminal import bash

    child = await build_child_agent(
        goal="x", context="", toolsets=["file", "shell"], skills=[]
    )
    assert bash in set(child.tools)


# =============================================================================
# Agent-name sanitization — must yield a valid Python identifier (ADK requires
# it). Regression: a hyphenated `name` used to crash the child build.
# =============================================================================


@pytest.mark.asyncio
async def test_hyphenated_name_yields_valid_identifier(tmp_path):
    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(goal="x", tools=[], name="child-one")
    assert child.name.isidentifier(), child.name
    assert child.name.startswith("child_one_")


@pytest.mark.asyncio
async def test_leading_digit_name_yields_valid_identifier(tmp_path):
    from horizon.subagents.delegate_builder import build_child_agent

    child = await build_child_agent(goal="x", tools=[], name="2fast")
    assert child.name.isidentifier(), child.name


def test_child_model_allowlist_tracks_the_registry() -> None:
    """A hand-maintained copy drifts: the pro model was selectable by the root
    agent but not by a delegate, while the retired gemini-flash-latest alias
    still was."""
    from horizon.models.registry import MODEL_REGISTRY
    from horizon.subagents.delegate_builder import _ALLOWED_MODELS

    assert set(_ALLOWED_MODELS) == set(MODEL_REGISTRY)
