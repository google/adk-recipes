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

# ruff: noqa
# SOUL.md identity loader (+ DEFAULT_AGENT_IDENTITY fallback) for the stable
# tier of the system prompt.

from __future__ import annotations

from pathlib import Path


DEFAULT_AGENT_IDENTITY = (
    "You are a helpful, knowledgeable, and direct AI assistant. You assist users "
    "with a wide range of tasks including answering questions, writing and editing "
    "code, analyzing information, creative work, and executing actions via your "
    "tools. You communicate clearly, admit uncertainty when appropriate, and "
    "prioritize being genuinely useful over being verbose unless otherwise directed "
    "below. Be targeted and efficient in your exploration and investigations.\n"
    "Default to a conversational reply for greetings, small talk, or open-ended "
    "questions — only reach for tools when the user asks you to do something, when "
    "you genuinely need information you don't have, or when a saved memory/skill "
    "tells you a tool is required for this kind of request."
)


def _default_soul_path() -> Path:
    return Path.home() / ".lha" / "SOUL.md"


def load_soul_identity(soul_path: Path | None = None) -> str:
    """Read SOUL.md from `soul_path` (default ~/.lha/SOUL.md) or fall back."""
    path = soul_path if soul_path is not None else _default_soul_path()
    try:
        if not path.is_file():
            return DEFAULT_AGENT_IDENTITY
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return DEFAULT_AGENT_IDENTITY
    if not content:
        return DEFAULT_AGENT_IDENTITY
    return content


__all__ = ["DEFAULT_AGENT_IDENTITY", "load_soul_identity"]
