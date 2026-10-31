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

"""Live smoke test: one real Gemini Live turn through `Runner.run_live()`."""

from __future__ import annotations

import asyncio

import pytest
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

APP_NAME = "test"
USER_ID = "test_user"
TURN_TIMEOUT_S = 90.0


async def test_agent_live_stream() -> None:
    from app.agent import root_agent

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID
    )
    runner = Runner(
        agent=root_agent, session_service=session_service, app_name=APP_NAME
    )
    queue = LiveRequestQueue()
    queue.send_content(
        types.Content(
            role="user",
            parts=[
                types.Part(text="What coding threads are currently running?")
            ],
        )
    )

    transcript, audio_chunks = "", 0

    async def one_turn() -> None:
        nonlocal transcript, audio_chunks
        async for event in runner.run_live(
            user_id=USER_ID,
            session_id=session.id,
            live_request_queue=queue,
            run_config=RunConfig(
                response_modalities=[types.Modality.AUDIO],
                output_audio_transcription=types.AudioTranscriptionConfig(),
            ),
        ):
            transcription = event.output_transcription
            if transcription and transcription.finished and transcription.text:
                transcript += transcription.text
            if event.content and event.content.parts:
                audio_chunks += sum(
                    1 for p in event.content.parts if p.inline_data
                )
            # A tool call (e.g. list_threads) ends its own turn before any audio; the
            # spoken answer is the turn after it.
            if event.turn_complete and audio_chunks:
                return

    try:
        await asyncio.wait_for(one_turn(), TURN_TIMEOUT_S)
    finally:
        queue.close()

    assert audio_chunks > 0, "the live model streamed no audio"
