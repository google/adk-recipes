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

"""ADK's non-pydantic declaration path ships raw indented docstrings."""

from __future__ import annotations

import asyncio

from google.adk.models import LlmRequest
from google.genai import types

from horizon.context.schema_normalization import normalize_tool_descriptions


def _request(description: str) -> LlmRequest:
    req = LlmRequest()
    req.config.tools = [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name="t", description=description)
            ]
        )
    ]
    return req


def test_source_indentation_is_stripped():
    req = _request("First line.\n\n    Indented continuation.\n    ")
    saved = normalize_tool_descriptions(req)
    text = req.config.tools[0].function_declarations[0].description
    assert text == "First line.\n\nIndented continuation."
    assert saved > 0


def test_is_idempotent():
    req = _request("First line.\n\n    Indented continuation.\n    ")
    normalize_tool_descriptions(req)
    assert normalize_tool_descriptions(req) == 0


def test_no_description_is_not_a_crash():
    req = _request("")
    assert normalize_tool_descriptions(req) == 0


def test_live_agent_declarations_are_normalized_once_and_stay_normalized():
    # Args: blocks keep their indentation on purpose; the invariant is that
    # a second pass finds nothing left to strip.
    from horizon.agent import root_agent

    async def _run() -> tuple[int, int]:
        req = LlmRequest()
        req.append_tools(list(await root_agent.canonical_tools()))
        return normalize_tool_descriptions(req), normalize_tool_descriptions(
            req
        )

    first, second = asyncio.run(_run())
    assert first > 0, "expected source indentation to strip"
    assert second == 0, "normalization is not idempotent"
