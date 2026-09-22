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

"""Skills-surface cuts C and E (cut A: test_subagent_descriptions.py; cut B:
test_skill_preamble.py; cut F: test_static_instruction.py's golden file).

Cut C: every builtin skill's frontmatter description is capped at 200 chars.

Cut E: ``load_skill`` merges ADK's ``LoadSkillTool`` and
``LoadSkillResourceTool`` (``skill_name`` kept, ``resource`` added;
``run_skill_script`` has no successor). Each reproduced behavior gets its
own test, so one could vanish behind an "only the happy path"
implementation with every other test in this file still green.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from google.adk.skills import models

from horizon.tools.skill_loader import (
    build_skill_toolset,
    builtin_skills_root,
    walk_skill_dirs,
)
from horizon.tools.skill_toolset import HorizonSkillToolset, LoadSkillTool

pytestmark = pytest.mark.asyncio


class _Ctx:
    def __init__(self, invocation_id: str = "inv-1") -> None:
        self.invocation_id = invocation_id
        self.agent_name = "root_agent"
        self.state: dict[str, Any] = {}


def _builtin_toolset() -> HorizonSkillToolset:
    # build_skill_toolset(), not a bare HorizonSkillToolset(...): the raw
    # constructor still carries ADK's own ListSkillsTool/LoadSkillTool/
    # LoadSkillResourceTool/RunSkillScriptTool quartet, and ADK's LoadSkillTool
    # shares the exact name "load_skill" with horizon's merged replacement, so
    # a bare construction would silently find ADK's tool instead of ours.
    return build_skill_toolset(
        user_dir=builtin_skills_root().parent / "_unbound_user_skills",
        builtin_dir=builtin_skills_root(),
    )


# =============================================================================
# Cut C: builtin frontmatter description budget
# =============================================================================


def test_every_builtin_skill_description_is_capped():
    over = {}
    for skill_dir in sorted(builtin_skills_root().iterdir()):
        md = skill_dir / "SKILL.md"
        if not md.is_file():
            continue
        frontmatter = yaml.safe_load(md.read_text().split("---", 2)[1])
        description = frontmatter.get("description", "")
        if len(description) > 200:
            over[skill_dir.name] = len(description)
    assert not over, over


def test_builtin_skills_still_load():
    """A trimmed description must not break frontmatter parsing (a stray
    unquoted colon can silently invalidate the YAML: caught this exact
    bug drafting the routines/SKILL.md trim: "(cron: pull data, ...)" reads
    as a YAML mapping key, not plain text)."""
    skills_dict = walk_skill_dirs(
        user_dir=builtin_skills_root().parent / "_unbound_user_skills",
        builtin_dir=builtin_skills_root(),
    )
    on_disk = {
        p.name
        for p in builtin_skills_root().iterdir()
        if (p / "SKILL.md").is_file()
    }
    assert set(skills_dict) == on_disk, (set(skills_dict), on_disk)


# =============================================================================
# Cut E: the merged load_skill tool
# =============================================================================


def _load_skill_tool(toolset: HorizonSkillToolset) -> LoadSkillTool:
    return next(t for t in toolset._tools if t.name == "load_skill")


async def test_load_skill_reads_instructions():
    toolset = _builtin_toolset()
    tool = _load_skill_tool(toolset)
    ctx = _Ctx()
    result = await tool.run_async(
        args={"skill_name": "policy"}, tool_context=ctx
    )
    assert result["skill_name"] == "policy"
    assert "instructions" in result
    assert "frontmatter" in result


async def test_load_skill_activates_skill_in_state():
    """Behavior 1: the _adk_activated_skill_{agent_name} state write, which
    drives SkillToolset._resolve_additional_tools_from_state. Omitting it
    silently breaks a skill that ships adk_additional_tools. That is the exact
    kind of regression a "just make it return instructions" implementation
    would introduce without any test here noticing."""
    toolset = _builtin_toolset()
    tool = _load_skill_tool(toolset)
    ctx = _Ctx()
    await tool.run_async(args={"skill_name": "policy"}, tool_context=ctx)
    assert ctx.state["_adk_activated_skill_root_agent"] == ["policy"]

    await tool.run_async(args={"skill_name": "routines"}, tool_context=ctx)
    assert ctx.state["_adk_activated_skill_root_agent"] == [
        "policy",
        "routines",
    ]

    # Loading the same skill twice must not duplicate the entry.
    await tool.run_async(args={"skill_name": "policy"}, tool_context=ctx)
    assert ctx.state["_adk_activated_skill_root_agent"] == [
        "policy",
        "routines",
    ]


async def test_load_skill_missing_skill_name_is_invalid_arguments():
    toolset = _builtin_toolset()
    tool = _load_skill_tool(toolset)
    result = await tool.run_async(args={}, tool_context=_Ctx())
    assert result["error_code"] == "INVALID_ARGUMENTS"


async def test_load_skill_unknown_skill_is_not_found():
    toolset = _builtin_toolset()
    tool = _load_skill_tool(toolset)
    result = await tool.run_async(
        args={"skill_name": "does-not-exist"}, tool_context=_Ctx()
    )
    assert result["error_code"] == "SKILL_NOT_FOUND"


async def test_load_skill_resource_reads_a_bundled_file(tmp_path: Path):
    root = tmp_path / "skills"
    skill_dir = root / "withref"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: withref\ndescription: test\n---\nBody.\n"
    )
    (skill_dir / "references" / "notes.md").write_text("reference content")

    skills_dict = walk_skill_dirs(user_dir=root, builtin_dir=tmp_path / "empty")
    toolset = HorizonSkillToolset(skills=list(skills_dict.values()))
    # LoadSkillTool(toolset) directly, not _load_skill_tool(toolset): a bare
    # HorizonSkillToolset still carries ADK's own LoadSkillTool in _tools
    # (only build_skill_toolset() swaps it out), and ADK's tool shares the
    # exact name "load_skill" with horizon's replacement, so searching
    # _tools here would silently find and exercise ADK's original instead.
    tool = LoadSkillTool(toolset)

    result = await tool.run_async(
        args={"skill_name": "withref", "resource": "references/notes.md"},
        tool_context=_Ctx(),
    )
    assert result == {
        "skill_name": "withref",
        "resource": "references/notes.md",
        "content": "reference content",
    }


async def test_load_skill_resource_invalid_path_prefix():
    toolset = _builtin_toolset()
    tool = _load_skill_tool(toolset)
    result = await tool.run_async(
        args={"skill_name": "policy", "resource": "not-a-valid-prefix.md"},
        tool_context=_Ctx(),
    )
    assert result["error_code"] == "INVALID_RESOURCE_PATH"


async def test_load_skill_resource_not_found_first_miss_is_recoverable():
    toolset = _builtin_toolset()
    tool = _load_skill_tool(toolset)
    result = await tool.run_async(
        args={"skill_name": "policy", "resource": "references/missing.md"},
        tool_context=_Ctx(),
    )
    assert result["error_code"] == "RESOURCE_NOT_FOUND"


async def test_load_skill_resource_not_found_second_miss_is_fatal():
    """Behavior 2: the invocation-scoped RESOURCE_NOT_FOUND retry guard.
    Counts failures across ALL resource paths within one invocation (not
    per-path), because the model retries with a DIFFERENT hallucinated path
    each time, so a per-path counter would never trip. An "only the happy
    path" implementation drops this guard silently: every call would just
    return RESOURCE_NOT_FOUND forever, and the model would retry forever."""
    toolset = _builtin_toolset()
    tool = _load_skill_tool(toolset)
    ctx = _Ctx()
    first = await tool.run_async(
        args={"skill_name": "policy", "resource": "references/one.md"},
        tool_context=ctx,
    )
    assert first["error_code"] == "RESOURCE_NOT_FOUND"

    # A DIFFERENT path, same invocation: the counter is invocation-scoped,
    # not path-scoped, so this still escalates to FATAL.
    second = await tool.run_async(
        args={"skill_name": "policy", "resource": "references/two.md"},
        tool_context=ctx,
    )
    assert second["error_code"] == "RESOURCE_NOT_FOUND_FATAL"
    assert "do not retry" in second["error"].lower()


async def test_load_skill_resource_not_found_counter_is_scoped_per_invocation():
    toolset = _builtin_toolset()
    tool = _load_skill_tool(toolset)
    await tool.run_async(
        args={"skill_name": "policy", "resource": "references/one.md"},
        tool_context=_Ctx(invocation_id="inv-a"),
    )
    # A fresh invocation must not inherit the previous invocation's count.
    fresh = await tool.run_async(
        args={"skill_name": "policy", "resource": "references/one.md"},
        tool_context=_Ctx(invocation_id="inv-b"),
    )
    assert fresh["error_code"] == "RESOURCE_NOT_FOUND"


async def test_load_skill_resource_binary_content_is_reported_honestly():
    """Binary-file detection, deliberately narrower than ADK's
    LoadSkillResourceTool (see horizon/tools/skill_toolset.py's module
    docstring): this tool does not reproduce the process_llm_request hook
    that injects a resource's raw bytes into the next turn, so it must not
    claim it did. No builtin skill ships a binary resource today. The
    filesystem loader (google.adk.skills._utils._load_dir) reads every
    resource as UTF-8 text and silently drops anything that fails to
    decode, so this path is unreachable via horizon's current skill
    loading; a Skill built directly, as a future registry-backed loader
    might, still hits it)."""
    skill = models.Skill(
        frontmatter=models.Frontmatter(name="bintest", description="test"),
        instructions="Body.",
        resources=models.Resources(assets={"logo.png": b"\x89PNG\r\n"}),
    )
    toolset = HorizonSkillToolset(skills=[skill])
    # LoadSkillTool(toolset) directly: see the comment in
    # test_load_skill_resource_reads_a_bundled_file above for why
    # _load_skill_tool(toolset) would silently find ADK's own tool here.
    tool = LoadSkillTool(toolset)

    result = await tool.run_async(
        args={"skill_name": "bintest", "resource": "assets/logo.png"},
        tool_context=_Ctx(),
    )
    assert result["error_code"] == "BINARY_RESOURCE_UNSUPPORTED"
    assert "injected" not in result["error"].lower()


async def test_adk_inject_state_metadata_branch(
    monkeypatch: pytest.MonkeyPatch,
):
    """Behavior 3: no builtin skill sets adk_inject_state, but a user skill
    can. This confirms the branch still calls ADK's own template-substitution
    helper rather than silently skipping it. inject_session_state itself
    needs a real ReadonlyContext (._invocation_context); horizon's fake
    tool_context is not one, so the helper is stubbed rather than exercised
    end-to-end: ADK's own template substitution is not this repo's code
    to re-verify."""
    import horizon.tools.skill_toolset as skill_toolset_mod

    calls = []

    async def _fake_inject_session_state(instructions, tool_context):
        calls.append((instructions, tool_context))
        return "INJECTED:" + instructions

    monkeypatch.setattr(
        skill_toolset_mod.instructions_utils,
        "inject_session_state",
        _fake_inject_session_state,
    )

    skill = models.Skill(
        frontmatter=models.Frontmatter(
            name="statetest",
            description="test",
            metadata={"adk_inject_state": True},
        ),
        instructions="Hello {state.foo}!",
    )
    toolset = HorizonSkillToolset(skills=[skill])
    # LoadSkillTool(toolset) directly: without it this test silently
    # monkeypatches and then exercises ADK's OWN LoadSkillTool instead of
    # horizon's (both import the same shared instructions_utils module
    # object, so the patch would "work" either way, and the assertions
    # would pass for the wrong tool). See the comment above.
    tool = LoadSkillTool(toolset)

    ctx = _Ctx()
    result = await tool.run_async(
        args={"skill_name": "statetest"}, tool_context=ctx
    )
    assert result["instructions"] == "INJECTED:Hello {state.foo}!"
    assert len(calls) == 1
    assert calls[0][1] is ctx


async def test_detect_error_in_response_hook():
    """Behavior 4: the _detect_error_in_response telemetry hook ADK's
    functions.py reaches for via getattr on every tool after a call."""
    toolset = _builtin_toolset()
    tool = _load_skill_tool(toolset)

    assert (
        tool._detect_error_in_response(
            {"error": "x", "error_code": "SKILL_NOT_FOUND"}
        )
        == "SKILL_NOT_FOUND"
    )
    assert tool._detect_error_in_response({"error": "x"}) == "TOOL_ERROR"
    assert tool._detect_error_in_response({"instructions": "ok"}) is None
    assert tool._detect_error_in_response("not a dict") is None


def test_load_skill_declaration_schema():
    toolset = _builtin_toolset()
    tool = _load_skill_tool(toolset)
    decl = tool._get_declaration()
    # skill_name is not schema-required: action='reload' doesn't need one.
    # The runtime check (test_load_skill_missing_skill_name_is_invalid_arguments)
    # is what actually enforces it for action='load'.
    assert "required" not in decl.parameters_json_schema
    assert set(decl.parameters_json_schema["properties"]) == {
        "action",
        "skill_name",
        "resource",
    }


# =============================================================================
# reload folded into load_skill(action='reload') — the /reload tool merge
# =============================================================================


async def test_load_skill_reload_refreshes_the_catalog(tmp_path: Path):
    # resync_and_refresh() (the production reload path) re-mirrors from the
    # ACTIVE environment's .agents/skills dir before re-walking, which would
    # clobber a plain tmp_path user_dir under the autouse _scoped_environment
    # fixture. Clearing the active environment for this call exercises the
    # same "no environment" fallback (plain refresh_skills()) production
    # hits when reload runs outside a session — and matches the existing
    # test_build_skill_toolset_survives_reload pattern.
    from horizon.environment import LocalEnvironment
    from horizon.environment_context import (
        clear_active_environment,
        set_active_environment,
    )
    from horizon.tools.skill_reload import bind_toolset

    user_dir = tmp_path / "user_skills"
    user_dir.mkdir()
    toolset = build_skill_toolset(
        user_dir=user_dir, builtin_dir=builtin_skills_root()
    )
    bind_toolset(toolset, user_dir=user_dir, builtin_dir=builtin_skills_root())
    tool = _load_skill_tool(toolset)

    _write_skill(user_dir, "brand-new", "Body.")

    saved_env = LocalEnvironment(working_dir=tmp_path)
    clear_active_environment()
    try:
        result = await tool.run_async(
            args={"action": "reload"}, tool_context=_Ctx()
        )
    finally:
        set_active_environment(saved_env)

    assert result["skills_refreshed"] is True
    assert "brand-new" in result["loaded"]


async def test_load_skill_reload_needs_no_skill_name():
    toolset = _builtin_toolset()
    tool = _load_skill_tool(toolset)
    result = await tool.run_async(
        args={"action": "reload"}, tool_context=_Ctx()
    )
    assert "error_code" not in result


async def test_load_skill_unknown_action_is_invalid_arguments():
    toolset = _builtin_toolset()
    tool = _load_skill_tool(toolset)
    result = await tool.run_async(
        args={"action": "delete"}, tool_context=_Ctx()
    )
    assert result["error_code"] == "INVALID_ARGUMENTS"


def _write_skill(root: Path, name: str, body: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test\n---\n{body}\n"
    )


def test_run_skill_script_and_load_skill_resource_are_gone():
    """No successor tool for run_skill_script: the model uses bash on a
    resource-loaded script instead."""
    toolset = _builtin_toolset()
    tool_names = {t.name for t in toolset._tools}
    assert tool_names == {"load_skill"}


def test_build_skill_toolset_survives_reload():
    """refresh_skills() (horizon.tools.skill_reload) rebuilds only _skills,
    never _tools. The merged load_skill and the dropped
    ListSkillsTool/LoadSkillResourceTool/RunSkillScriptTool trio must
    survive a /reload."""
    from horizon.tools.skill_reload import bind_toolset, refresh_skills

    user_dir = builtin_skills_root().parent / "_unbound_user_skills"
    toolset = build_skill_toolset(
        user_dir=user_dir, builtin_dir=builtin_skills_root()
    )
    before = {t.name for t in toolset._tools}
    bind_toolset(toolset, user_dir=user_dir, builtin_dir=builtin_skills_root())
    refresh_skills()
    after = {t.name for t in toolset._tools}
    assert before == after == {"load_skill"}
