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

"""Regression tests for two dead-gate bugs plus a has_code_executor
threading bug, each caught by review.

Broader per-block presence/absence coverage lives in
``test_system_prompt.py``; this file only pins the specific defects, so a
regression fails with an unambiguous name instead of a generic
"guidance absent" assertion.
"""

from __future__ import annotations

from horizon.conversation.system_prompt import (
    SKILLS_GUIDANCE,
    build_static_instruction,
)
from horizon.tools import names


def test_skills_guidance_gate_is_a_real_tool_name():
    """v1's SKILLS_GUIDANCE gated on a tool literally named "skill", which
    never existed, so the block never injected in production. The gate must
    be a name that is actually a registered tool."""
    assert names.LOAD_SKILL != "skill"

    instruction_with_tool = build_static_instruction(
        tool_names=[names.LOAD_SKILL], model_name="gemini-3.7-flash"
    )
    instruction_without_tool = build_static_instruction(
        tool_names=["skill"], model_name="gemini-3.7-flash"
    )

    assert SKILLS_GUIDANCE in instruction_with_tool
    assert SKILLS_GUIDANCE not in instruction_without_tool


def test_skills_guidance_text_matches_the_real_authoring_api():
    """v1's text told the model to call skill(action='create'/'patch'),
    which do not exist. The real path is write + load_skill(action='reload').
    (That legitimate call deliberately contains the substring
    "skill(action=" too, so the negative check names the exact bogus
    actions instead of the substring.)"""
    assert "skill(action='create'" not in SKILLS_GUIDANCE
    assert "skill(action='patch'" not in SKILLS_GUIDANCE
    assert "write" in SKILLS_GUIDANCE
    assert "reload" in SKILLS_GUIDANCE


def test_cross_session_recall_guidance_is_gated_on_memory_not_a_placeholder():
    """The former standalone session_search tool (and, before Task 11's
    rename, its dead _SESSION_SEARCH_TOOL_NAMES gate keyed on a name that
    never matched the live tool) was folded into memory(action='search').
    Its cross-session-recall paragraph now lives inside MEMORY_GUIDANCE,
    gated on the real names.MEMORY — proven here with a name that is
    definitely not a registered tool."""
    from horizon.conversation.system_prompt import MEMORY_GUIDANCE

    dead_gate = build_static_instruction(
        tool_names=["definitely_not_a_registered_tool"],
        model_name="gemini-3.7-flash",
    )
    real_gate = build_static_instruction(
        tool_names=[names.MEMORY], model_name="gemini-3.7-flash"
    )

    assert MEMORY_GUIDANCE not in dead_gate
    assert MEMORY_GUIDANCE in real_gate
    assert "action='search'" in MEMORY_GUIDANCE


def test_has_code_executor_flag_actually_reaches_the_built_agent():
    """v1's draft passed has_code_executor=False unconditionally because
    agent.py never called build_stable_tier at all, so the flag would have
    defaulted to False forever even with a real executor configured. Pin
    that horizon.agent now threads _build_code_executor()'s presence
    through to build_static_instruction at App-build time."""
    import inspect

    import horizon.agent as agent_module

    source = inspect.getsource(agent_module._static_instruction_for)
    assert "has_code_executor" in source


def test_code_execution_guidance_follows_the_flag_not_a_hardcoded_default():
    from horizon.conversation.system_prompt import CODE_EXECUTION_GUIDANCE

    without = build_static_instruction(
        tool_names=[], model_name="gemini-3.7-flash", has_code_executor=False
    )
    with_ = build_static_instruction(
        tool_names=[], model_name="gemini-3.7-flash", has_code_executor=True
    )

    assert CODE_EXECUTION_GUIDANCE not in without
    assert CODE_EXECUTION_GUIDANCE in with_
