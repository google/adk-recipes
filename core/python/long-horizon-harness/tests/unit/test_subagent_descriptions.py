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

"""before_model_callback rewrites the subagent tool's description live.

The skill catalog used to be spliced into this description too (cut A of
the prompt-minimalism plan's skills-surface work) — a second copy of the
same ``<available_skills>`` content the model already has in its system
prompt. The suffix now carries only a one-line pointer at that block plus
the child-profile registry.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

pytestmark = pytest.mark.asyncio


def _make_request(*tool_names: str):
    from google.genai import types

    fds = [
        types.FunctionDeclaration(name=n, description=f"BASE {n} description.")
        for n in tool_names
    ]
    config = types.GenerateContentConfig(
        tools=[types.Tool(function_declarations=fds)]
    )

    class _Req:
        pass

    req = _Req()
    req.config = config
    return req


class _Cb:
    state: ClassVar[dict[str, Any]] = {}


def _desc(req: Any, name: str) -> str:
    for tool in req.config.tools or []:
        for fd in tool.function_declarations or []:
            if fd.name == name:
                return fd.description
    raise AssertionError(name)


async def test_rewrites_subagent_with_pointer_and_profiles() -> None:
    from horizon.subagents.descriptions import (
        make_subagent_description_callback,
    )

    cb = make_subagent_description_callback()
    req = _make_request("subagent", "read_file")
    await cb(_Cb(), req)

    subagent_desc = _desc(req, "subagent")
    assert subagent_desc.startswith("BASE subagent description.")
    assert "<available_skills>" in subagent_desc
    assert "## Child profiles" in subagent_desc
    # other tools are untouched.
    assert _desc(req, "read_file") == "BASE read_file description."


async def test_suffix_carries_no_skill_catalog() -> None:
    """The catalog is in <available_skills> once, not spliced in a second
    time here (cut A) — regardless of what skills happen to be loaded."""
    from horizon.subagents.descriptions import _build_suffix

    suffix = _build_suffix()
    assert "## Skills you can pass to a child" not in suffix


async def test_idempotent_across_turns() -> None:
    """Calling the callback twice must not double-append the dynamic block."""
    from horizon.subagents.descriptions import (
        make_subagent_description_callback,
    )

    cb = make_subagent_description_callback()
    req = _make_request("subagent")
    await cb(_Cb(), req)
    first = _desc(req, "subagent")
    await cb(_Cb(), req)
    second = _desc(req, "subagent")
    assert first == second
    assert second.count("## Child profiles") == 1


async def test_handles_request_with_no_tools() -> None:
    from horizon.subagents.descriptions import (
        make_subagent_description_callback,
    )

    cb = make_subagent_description_callback()

    class _Req:
        class config:
            tools = None

    # Must not raise.
    await cb(_Cb(), _Req())
