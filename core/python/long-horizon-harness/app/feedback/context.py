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
"""Build a redacted contextual summary of a session for opt-in feedback."""

from __future__ import annotations

from typing import Any

from app.guardrails.exfil_config import default_config

_MAX_TURNS = 20
_MAX_CHARS_PER_TURN = 600


def _redact(text: str) -> str:
    for label, pattern in default_config().secret_patterns:
        text = pattern.sub(f"[redacted:{label}]", text)
    return text


def build_redacted_context(
    session: Any,
    *,
    max_turns: int = _MAX_TURNS,
    max_chars_per_turn: int = _MAX_CHARS_PER_TURN,
) -> str:
    turns: list[tuple[str, str]] = []
    for event in getattr(session, "events", None) or []:
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None)
        if not parts:
            continue
        texts = [
            part.text.strip()
            for part in parts
            if isinstance(getattr(part, "text", None), str)
            and part.text.strip()
        ]
        if not texts:
            continue
        role = (
            "user" if getattr(event, "author", None) == "user" else "assistant"
        )
        turns.append((role, " ".join(texts)))

    lines: list[str] = []
    for role, raw_body in turns[-max_turns:]:
        body = _redact(raw_body)
        if len(body) > max_chars_per_turn:
            body = body[:max_chars_per_turn].rstrip() + "…"
        lines.append(f"{role}: {body}")
    return "\n".join(lines)
