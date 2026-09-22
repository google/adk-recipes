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

"""The preload block must stay out of the cached prefix, and stay bounded.

GeminiContextCacheManager hashes the whole system_instruction, so a block
rebuilt per turn from a similarity search can never be byte-stable there. ADK
owns the placement; the caps are ours.
"""

from __future__ import annotations

import types as pytypes
from dataclasses import dataclass

import pytest
from google.adk.models import LlmRequest
from google.adk.models.gemini_context_cache_manager import (
    GeminiContextCacheManager,
)
from google.genai import types

from horizon.memory.preload import HorizonPreloadMemoryTool

pytestmark = pytest.mark.asyncio


@dataclass
class _Memory:
    author: str | None
    timestamp: str | None
    content: types.Content


def _memory(text: str) -> _Memory:
    return _Memory(
        author="user",
        timestamp=None,
        content=types.Content(role="user", parts=[types.Part(text=text)]),
    )


class _Ctx:
    def __init__(self, memories: list[_Memory]):
        self.user_content = types.Content(
            role="user", parts=[types.Part(text="what is my name?")]
        )
        self._memories = memories

    async def search_memory(self, query: str):
        return pytypes.SimpleNamespace(memories=self._memories)


def _request() -> LlmRequest:
    req = LlmRequest()
    req.config.system_instruction = "STABLE PREFIX"
    return req


async def test_block_lands_in_contents_not_system_instruction():
    req = _request()
    await HorizonPreloadMemoryTool().process_llm_request(
        tool_context=_Ctx([_memory("your name is Sam")]), llm_request=req
    )
    assert req.config.system_instruction == "STABLE PREFIX"
    text = "".join(p.text or "" for c in req.contents for p in c.parts)
    assert "<PAST_CONVERSATIONS>" in text
    assert "your name is Sam" in text


async def test_block_lands_outside_the_cached_prefix():
    req = _request()
    req.contents = [
        types.Content(role="user", parts=[types.Part(text="hi")]),
        types.Content(role="model", parts=[types.Part(text="hello")]),
        types.Content(role="user", parts=[types.Part(text="and now?")]),
    ]
    await HorizonPreloadMemoryTool().process_llm_request(
        tool_context=_Ctx([_memory("fact")]), llm_request=req
    )

    manager = GeminiContextCacheManager(
        genai_client=pytypes.SimpleNamespace(vertexai=True)
    )
    cached = manager._find_count_of_contents_to_cache(req.contents)
    block = next(
        i
        for i, c in enumerate(req.contents)
        if "<PAST_CONVERSATIONS>" in (c.parts[0].text or "")
    )
    assert block >= cached


async def test_memory_count_is_capped(monkeypatch):
    monkeypatch.setenv("LHA_PRELOAD_MAX_MEMORIES", "3")
    req = _request()
    await HorizonPreloadMemoryTool().process_llm_request(
        tool_context=_Ctx([_memory(f"fact {i}") for i in range(50)]),
        llm_request=req,
    )
    text = "".join(p.text or "" for c in req.contents for p in c.parts)
    assert "fact 2" in text
    assert "fact 3" not in text


async def test_total_chars_are_capped(monkeypatch):
    monkeypatch.setenv("LHA_PRELOAD_MAX_CHARS", "80")
    req = _request()
    await HorizonPreloadMemoryTool().process_llm_request(
        tool_context=_Ctx([_memory("x" * 500)]), llm_request=req
    )
    text = "".join(p.text or "" for c in req.contents for p in c.parts)
    assert "truncated" in text
    assert len(text) < 300


async def test_no_memories_is_a_noop():
    req = _request()
    await HorizonPreloadMemoryTool().process_llm_request(
        tool_context=_Ctx([]), llm_request=req
    )
    assert req.contents == []
    assert req.config.system_instruction == "STABLE PREFIX"
