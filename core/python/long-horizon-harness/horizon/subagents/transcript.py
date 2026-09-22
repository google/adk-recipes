# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Opt-in transcript handoff for a delegated child.

Copies the parent's CONVERSATION, not its session. A child declares fewer
tools than the parent (default file+shell, or a read-only profile), so
replaying `function_call` parts it cannot make is malformed history, and the
`function_response` payloads are the bulk that pruning and compaction exist
to remove. Text only, capped, oldest dropped first.
"""

from __future__ import annotations

from typing import Any

MAX_TRANSCRIPT_CHARS = 4_000

_ROLE = {"user": "User"}


def parent_transcript(events: Any) -> str:
    """Render parent user/model text as a briefing block, newest kept."""
    if not events:
        return ""

    turns: list[str] = []
    for event in events:
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None) if content else None
        if not parts:
            continue
        text = "".join(
            p.text
            for p in parts
            if getattr(p, "text", None)
            and not getattr(p, "function_call", None)
            and not getattr(p, "function_response", None)
        ).strip()
        if not text:
            continue
        author = getattr(event, "author", "") or ""
        turns.append(f"{_ROLE.get(author, 'Assistant')}: {text}")

    if not turns:
        return ""

    kept: list[str] = []
    size = 0
    for turn in reversed(turns):
        size += len(turn) + 1
        if size > MAX_TRANSCRIPT_CHARS:
            break
        kept.append(turn)
    return "\n".join(reversed(kept))


__all__ = ["MAX_TRANSCRIPT_CHARS", "parent_transcript"]
