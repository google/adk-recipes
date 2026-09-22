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

"""Memory preload with a bounded block.

``BaseMemoryService.search_memory`` takes no ``top_k``, so ADK's stock
``PreloadMemoryTool`` block grows without bound as memories accumulate. This
subclass renders the same text under a count and character cap. Placement stays
ADK's (``_insert_transient_user_content`` keeps the block out of the cached
prefix); only the caps are ours.
"""

from __future__ import annotations

import logging
import os

from google.adk.models import LlmRequest
from google.adk.tools import _memory_entry_utils
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from typing_extensions import override

logger = logging.getLogger(__name__)

_HEADER = (
    "The following content is from your previous conversations with the "
    "user.\nThey may be useful for answering the user's current query."
)

DEFAULT_MAX_MEMORIES = 20
DEFAULT_MAX_CHARS = 4_000


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


class HorizonPreloadMemoryTool(PreloadMemoryTool):
    """PreloadMemoryTool with a capped recall block."""

    @override
    async def process_llm_request(
        self,
        *,
        tool_context: ToolContext,
        llm_request: LlmRequest,
    ) -> None:
        user_content = tool_context.user_content
        if (
            not user_content
            or not user_content.parts
            or not user_content.parts[0].text
        ):
            return

        user_query = user_content.parts[0].text
        try:
            response = await tool_context.search_memory(user_query)
        except Exception:
            logger.warning("preload: memory search failed")
            return

        memories = list(getattr(response, "memories", None) or [])
        if not memories:
            return

        max_memories = _int_env(
            "LHA_PRELOAD_MAX_MEMORIES", DEFAULT_MAX_MEMORIES
        )
        dropped = max(0, len(memories) - max_memories)
        memories = memories[:max_memories]

        lines: list[str] = []
        for memory in memories:
            if memory.timestamp:
                lines.append(f"Time: {memory.timestamp}")
            text = _memory_entry_utils.extract_text(memory)
            if text:
                lines.append(
                    f"{memory.author}: {text}" if memory.author else text
                )
        if not lines:
            return

        body = "\n".join(lines)
        max_chars = _int_env("LHA_PRELOAD_MAX_CHARS", DEFAULT_MAX_CHARS)
        if len(body) > max_chars:
            body = body[:max_chars] + "\n[…truncated]"
            dropped += 1
        if dropped:
            logger.info("preload: trimmed %d memory entries", dropped)

        llm_request._insert_transient_user_content(
            [
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            text=f"{_HEADER}\n<PAST_CONVERSATIONS>\n{body}\n"
                            "</PAST_CONVERSATIONS>"
                        )
                    ],
                )
            ]
        )


__all__ = ["HorizonPreloadMemoryTool"]
