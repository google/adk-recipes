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

"""Model names read from the environment (`.env.example` holds the defaults)."""

from __future__ import annotations

import os


def require_model(var: str) -> str:
    value = (os.environ.get(var) or "").strip()
    if not value:
        raise RuntimeError(
            f"{var} is not set; copy .env.example to .env or set {var} in the environment."
        )
    return value


def live_model() -> str:
    return require_model("LIVE_MODEL")


def grounding_model() -> str:
    return require_model("GROUNDING_MODEL")


def memory_model() -> str:
    return (os.environ.get("MEMORY_MODEL") or "").strip() or grounding_model()


def cascade_stt_model() -> str:
    return require_model("CASCADE_STT_MODEL")


def cascade_llm_model() -> str:
    return require_model("CASCADE_LLM_MODEL")


def cascade_tts_model() -> str:
    return require_model("CASCADE_TTS_MODEL")


def claude_model() -> str:
    return require_model("ANTHROPIC_MODEL")


def claude_sonnet_model() -> str:
    return require_model("ANTHROPIC_DEFAULT_SONNET_MODEL")


def claude_haiku_model() -> str:
    small = (os.environ.get("ANTHROPIC_SMALL_FAST_MODEL") or "").strip()
    if small:
        return small
    return require_model("ANTHROPIC_DEFAULT_HAIKU_MODEL")


def antigravity_model() -> str:
    return require_model("ANTIGRAVITY_MODEL")
