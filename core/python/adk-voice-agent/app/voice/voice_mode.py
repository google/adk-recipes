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

"""Voice modes: native Gemini Live or cascaded STT -> LLM -> TTS (`CascadeLive`).

`VoiceSessionPlugin` pins the user's mode in `temp:voice_mode` and sets `RunConfig`
at the start of each session so model, tools, and instructions stay consistent.
"""

from __future__ import annotations

import functools
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.agents.run_config import RunConfig
from google.adk.live import CascadeLive, GeminiSTT, GeminiTTS
from google.adk.models import BaseLlm, Gemini, LlmRequest, LlmResponse
from google.adk.plugins import BasePlugin
from google.genai import types

from app.app_utils.model_locations import SPEECH_LOCATION
from app.app_utils.models import (
    cascade_llm_model,
    cascade_stt_model,
    cascade_tts_model,
)
from app.user_settings import load_user_settings
from app.voice.preferences import (
    CASCADED,
    get_runtime_preferences,
    resolve_voice_mode,
)

VOICE_MODE_STATE = "temp:voice_mode"


class VoiceSessionPlugin(BasePlugin):
    """Pin the user's voice mode and `RunConfig` before the live connection opens."""

    def __init__(self) -> None:
        super().__init__(name="voice_session")

    async def before_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        await load_user_settings()
        mode = resolve_voice_mode()
        invocation_context.session.state[VOICE_MODE_STATE] = mode
        invocation_context.run_config = live_run_config(
            invocation_context.run_config or RunConfig(),
            mode,
            get_runtime_preferences()["voice_name"],
        )


def live_run_config(base: RunConfig, mode: str, voice_name: str) -> RunConfig:
    """Configure `base` with voice, session resumption, and sliding window for native sessions."""
    if mode == CASCADED:
        return base
    return base.model_copy(
        update={
            "speech_config": types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name
                    )
                )
            ),
            "session_resumption": base.session_resumption
            or types.SessionResumptionConfig(),
            "context_window_compression": base.context_window_compression
            or types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            ),
        }
    )


def is_cascaded(context: Any) -> bool:
    """Whether the session of `context` (any ADK context) runs cascaded.

    Outside a session, or before the mode is pinned, the user's current choice decides.
    """
    state = getattr(context, "state", None)
    mode = state.get(VOICE_MODE_STATE) if state is not None else None
    return (mode or resolve_voice_mode()) == CASCADED


class VoiceAgent(Agent):
    """The voice agent. `model` is the native Live model; a cascaded session opens a
    `CascadeLive` with the user's voice instead. ADK resolves the model once per
    `run_live`."""

    async def canonical_live_model_async(self, ctx: ReadonlyContext) -> BaseLlm:
        if is_cascaded(ctx):
            return cascade_model(get_runtime_preferences()["voice_name"])
        return await super().canonical_live_model_async(ctx)


def cascade_model(voice_name: str) -> CascadeLive:
    """The cascade for `voice_name` and the configured models, built once per combination."""
    return _cascade(
        voice_name,
        cascade_stt_model(),
        cascade_llm_model(),
        cascade_tts_model(),
    )


@functools.lru_cache(maxsize=32)
def _cascade(voice_name: str, stt: str, llm: str, tts: str) -> CascadeLive:
    speech = {"location": SPEECH_LOCATION}
    return CascadeLive(
        model=_MinimalThinking(
            model=llm, retry_options=types.HttpRetryOptions(attempts=3)
        ),
        stt=GeminiSTT(model=Gemini(model=stt, client_kwargs=speech)),
        tts=GeminiTTS(
            model=Gemini(model=tts, client_kwargs=speech),
            speech_config=voice_name,
        ),
    )


class _MinimalThinking(Gemini):
    """The cascade's text model, thinking at its lowest level: thinking delays the first
    word of every reply."""

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        config = (
            llm_request.config or types.GenerateContentConfig()
        ).model_copy(
            update={
                "thinking_config": types.ThinkingConfig(
                    thinking_level=types.ThinkingLevel.MINIMAL
                )
            }
        )
        async for response in super().generate_content_async(
            llm_request.model_copy(update={"config": config}), stream
        ):
            yield response
