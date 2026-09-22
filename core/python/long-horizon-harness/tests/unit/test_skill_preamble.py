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

"""``HorizonSkillToolset``: a short skills preamble instead of ADK's tutorial,
without wiping the rest of the prompt.

``system_instruction`` already holds static_instruction plus the
project-context tier by the time this toolset runs, so splitting the WHOLE
string on ``<available_skills>`` and keeping only the tail would silently
delete everything appended earlier. A test starting from a fresh
``LlmRequest()`` can't see that bug;
``test_existing_system_instruction_survives`` catches it by seeding a
sentinel first.
"""

from __future__ import annotations

import pytest
from google.adk.models import LlmRequest
from google.adk.tools.skill_toolset import ListSkillsTool, SkillToolset

from horizon.tools.skill_toolset import HorizonSkillToolset

pytestmark = pytest.mark.asyncio


@pytest.fixture
def toolset() -> HorizonSkillToolset:
    # Constructing HorizonSkillToolset directly still carries ADK's default
    # ListSkillsTool, which flips SkillToolset.process_llm_request into
    # SKIPPING the <available_skills> XML append (a list_skills tool call
    # becomes the catalog's only path instead). Production strips it in
    # horizon.tools.skill_loader.build_skill_toolset; mirror that here so
    # this fixture actually exercises the shape the app ships.
    ts = HorizonSkillToolset(skills=[])
    ts._tools = [t for t in ts._tools if not isinstance(t, ListSkillsTool)]
    return ts


async def test_preamble_is_short_and_index_survives(
    toolset: HorizonSkillToolset,
) -> None:
    req = LlmRequest()
    await toolset.process_llm_request(tool_context=None, llm_request=req)
    si = req.config.system_instruction
    head = si.split("<available_skills>", 1)[0]
    assert len(head) <= 200, len(head)
    assert "</available_skills>" in si


async def test_existing_system_instruction_survives(
    toolset: HorizonSkillToolset,
) -> None:
    """The test that catches the whole-string-split bug (see module docstring)."""
    req = LlmRequest()
    req.config.system_instruction = "SENTINEL-PREFIX"
    await toolset.process_llm_request(tool_context=None, llm_request=req)
    assert req.config.system_instruction.startswith("SENTINEL-PREFIX")
    assert "<available_skills>" in req.config.system_instruction
    assert "</available_skills>" in req.config.system_instruction


async def test_process_llm_request_still_registers_no_declaration(
    toolset: HorizonSkillToolset,
) -> None:
    """The toolset-level process_llm_request only appends instructions — it
    carries no FunctionDeclaration of its own (that's each tool's job via
    get_tools()). Confirms the override didn't accidentally start
    registering (or dropping) a tool declaration."""
    req = LlmRequest()
    await toolset.process_llm_request(tool_context=None, llm_request=req)
    assert not (req.config.tools or [])


async def test_missing_available_skills_index_raises(
    toolset: HorizonSkillToolset, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silently empty index would strip every skill from the model's view
    with no failure signal — the drift guard must raise instead."""

    async def _fake_process_llm_request(self, *, tool_context, llm_request):
        llm_request.append_instructions(["no index in this fake output"])

    monkeypatch.setattr(
        SkillToolset, "process_llm_request", _fake_process_llm_request
    )

    req = LlmRequest()
    with pytest.raises(RuntimeError, match="no <available_skills> index"):
        await toolset.process_llm_request(tool_context=None, llm_request=req)


async def test_preamble_survives_a_second_turn(
    toolset: HorizonSkillToolset,
) -> None:
    """Calling process_llm_request twice (two turns sharing a request
    object's accumulation pattern) must not compound or corrupt the delta
    math — each call's own "before" snapshot is independent."""
    req = LlmRequest()
    req.config.system_instruction = "TURN-ONE-STATIC-PREFIX"
    await toolset.process_llm_request(tool_context=None, llm_request=req)
    first = req.config.system_instruction

    req2 = LlmRequest()
    req2.config.system_instruction = first
    await toolset.process_llm_request(tool_context=None, llm_request=req2)
    second = req2.config.system_instruction

    assert second.startswith("TURN-ONE-STATIC-PREFIX")
    assert second.count("<available_skills>") == 2
