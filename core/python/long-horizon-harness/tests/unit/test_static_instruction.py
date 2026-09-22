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

"""Golden snapshot of the assembled ``Agent.static_instruction``.

Every later prose edit shows up here as a reviewable diff, unlike a
keyword-coverage test that would pass on almost any English paragraph.

To intentionally update the golden file after a real prose change:

    uv run python -c "
    from horizon.conversation.system_prompt import build_static_instruction
    from horizon.tools import names
    text = build_static_instruction(
        tool_names=sorted(names.ALL),
        model_name='gemini-3.7-flash',
        has_code_executor=False,
    )
    open('tests/unit/testdata/static_instruction_golden.txt', 'w').write(text)
    "

then read the diff line by line before committing it — that diff IS the
quality review of the rewrite.
"""

from __future__ import annotations

from pathlib import Path

from horizon.conversation.system_prompt import build_static_instruction
from horizon.tools import names

GOLDEN_PATH = (
    Path(__file__).parent / "testdata" / "static_instruction_golden.txt"
)


def test_static_instruction_matches_golden():
    text = build_static_instruction(
        tool_names=sorted(names.ALL),
        model_name="gemini-3.7-flash",
        has_code_executor=False,
    )
    expected = GOLDEN_PATH.read_text()
    assert text == expected, (
        "static_instruction changed; if this is an intentional prose edit, "
        "review the diff then regenerate the golden file per this module's "
        "docstring."
    )


def test_static_instruction_is_a_pure_function():
    """Same inputs -> same output, called twice, no caching required."""
    kwargs = {
        "tool_names": ["read", "bash", "memory"],
        "model_name": "gemini-3.7-flash",
        "has_code_executor": False,
    }
    assert build_static_instruction(**kwargs) == build_static_instruction(
        **kwargs
    )


def test_root_agent_actually_wires_static_instruction():
    """Without this, deleting static_instruction=... from Agent(...) in
    agent.py would make the entire system prompt vanish with every other
    test (which only exercises the pure function) still green."""
    import asyncio

    from horizon.agent import root_agent

    # instruction must be "" (not merely falsy) — a non-empty instruction
    # demotes into the uncached trailing user-content tail per
    # flows/llm_flows/instructions.py, silently defeating the whole point
    # of building the prefix as static_instruction.
    assert root_agent.instruction == ""
    assert isinstance(root_agent.static_instruction, str)
    assert root_agent.static_instruction.strip()

    tool_names = [
        getattr(t, "name", None) or getattr(t, "__name__", "")
        for t in asyncio.run(root_agent.canonical_tools())
    ]
    expected = build_static_instruction(
        tool_names=tool_names,
        model_name=root_agent.static_instruction and _model_name_used(),
        has_code_executor=root_agent.code_executor is not None,
    )
    assert root_agent.static_instruction == expected


def _model_name_used() -> str:
    # agent.py resolves the model with resolve_model_name(None) at
    # App-build time (no per-session /model override exists yet); match
    # that exactly rather than hardcoding a model name that could drift.
    from horizon.models.selector import resolve_model_name

    return resolve_model_name(None)
