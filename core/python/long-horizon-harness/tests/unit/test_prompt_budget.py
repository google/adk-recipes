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

"""Coarse size guards for horizon's per-turn fixed prompt prefix.

Imports the same helpers `scripts/measure_prefix.py` uses, so the two can't
quietly disagree about what "the prefix" means. No trajectory grader backs
this work (too few evalset invocations carry real tool-use args to score
above noise), so this plus the registry consistency tests are the safety net.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from google.adk.models import LlmRequest

from horizon.agent import _SKILL_TOOLSET, root_agent
from horizon.conversation.system_prompt import build_static_instruction

# Two coarse guards, deliberately not a per-component ratchet set.
#
# These exist because the prefix reached 70,774 chars with a green suite:
# nobody was watching the aggregate. They are sized to catch that failure,
# not to notice ordinary edits. Caps set at current-plus-epsilon just make
# every prompt tweak a two-line diff, so the numbers stop meaning anything
# and get bumped reflexively.
#
# Run `uv run python scripts/measure_prefix.py` for the live composition;
# that script, not this file, is where you look at current sizes.

# ~50% headroom over the measured prefix. Tripping this means a whole block
# or tool set came back, not that someone added a sentence.
MAX_TOTAL_PREFIX_CHARS = 32_000

# The largest legitimate description is a dispatch tool listing its actions.
# This catches a docstring someone pasted an essay into.
MAX_TOOL_DESC_CHARS = 1_400


async def _measure() -> dict[str, int]:
    tools = await root_agent.canonical_tools()
    tool_names = [t.name for t in tools]

    static = 0
    for tool in tools:
        decl = tool._get_declaration()
        if decl is not None:
            static += len(decl.model_dump_json(exclude_none=True))

    req = LlmRequest()
    await _SKILL_TOOLSET.process_llm_request(tool_context=None, llm_request=req)
    skills_block = req.config.system_instruction or ""
    idx = skills_block.find("<available_skills>")
    preamble = idx if idx >= 0 else len(skills_block)
    index = len(skills_block) - preamble if idx >= 0 else 0

    try:
        from horizon.subagents.descriptions import _build_suffix

        suffix = len(_build_suffix())
    except Exception:
        suffix = 0
    subagent_tool_count = len(
        [n for n in tool_names if n in {"delegate", "agent", "subagent"}]
    )
    dynamic = suffix * subagent_tool_count

    # Read the assembled instruction off the built agent (has_code_executor=
    # False in this repo's default dev config), so this can never disagree
    # with what the app actually serves, and separately at True so a real
    # deployment with CODE_SANDBOX_RESOURCE_NAME set is measured too.
    static_instruction = len(root_agent.static_instruction or "")
    static_instruction_with_executor = len(
        build_static_instruction(
            tool_names=tool_names,
            model_name="gemini-3.7-flash",
            has_code_executor=True,
        )
    )

    total = static_instruction + preamble + index + static + dynamic
    return {
        "static_instruction": static_instruction,
        "static_instruction_with_executor": static_instruction_with_executor,
        "skills_preamble": preamble,
        "skills_index": index,
        "total_schema": static + dynamic,
        "total_prefix": total,
    }


@pytest.fixture(scope="module")
def measured() -> dict[str, int]:
    return asyncio.run(_measure())


async def _tool_descriptions() -> dict[str, str]:
    # Measure what the model receives, not the raw docstring: ADK's
    # non-pydantic path keeps source indentation and
    # normalize_tool_schemas_callback strips it per request.
    tools = await root_agent.canonical_tools()
    out: dict[str, str] = {}
    for tool in tools:
        decl = tool._get_declaration()
        if decl is not None:
            out[tool.name] = inspect.cleandoc(decl.description or "")
    return out


def test_no_tool_description_is_an_essay():
    over = {
        name: len(desc)
        for name, desc in asyncio.run(_tool_descriptions()).items()
        if len(desc) > MAX_TOOL_DESC_CHARS
    }
    assert not over, over


def test_total_prefix_within_budget(measured):
    # The one number that matters: what every model call pays.
    assert measured["total_prefix"] <= MAX_TOTAL_PREFIX_CHARS, measured
