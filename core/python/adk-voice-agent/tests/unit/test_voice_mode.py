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

"""Voice modes: which mode a session runs in, the models it opens, and what changes in
a cascaded session (tools, voice config, instruction)."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncGenerator, AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig
from google.adk.flows.llm_flows.tools._agent_tools import (
    mark_live_async_tools_non_blocking,
)
from google.adk.live import CascadeLive
from google.adk.models import Gemini, LlmRequest, LlmResponse
from google.genai import types

from app.agent import MODEL, root_agent
from app.app_utils.model_locations import (
    DEFAULT_LIVE_MODEL_LOCATION,
    SPEECH_LOCATION,
    live_model_location,
)
from app.threads.registry import get_thread_registry
from app.tools.thread_tools import approve, start_thread
from app.user_settings import VOICE_MODE, save_user_settings
from app.voice.instruction import build_orchestrator_instruction
from app.voice.non_blocking import PENDING, non_blocking_tool, thread_tool
from app.voice.preferences import CASCADED, NATIVE, resolve_voice_mode
from app.voice.thread_updates import get_thread_updates
from app.voice.voice_mode import (
    VOICE_MODE_STATE,
    VoiceSessionPlugin,
    _MinimalThinking,
    cascade_model,
    is_cascaded,
    live_run_config,
)
from tests.fakes import GOAL, REPO, Lab, serve_mcp, wait_until


def _session(mode: str) -> SimpleNamespace:
    """A context of a session pinned to `mode`, as ADK's contexts expose its state."""
    return SimpleNamespace(state={VOICE_MODE_STATE: mode})


async def _background_turns(queue: LiveRequestQueue) -> list[str]:
    turns: list[str] = []
    while not queue._queue.empty():
        request = await queue.get()
        assert request.content is not None and request.content.parts is not None
        assert request.content.parts[0].text is not None
        turns.append(request.content.parts[0].text)
    return turns


# ---- which mode ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_users_choice_then_voice_mode_then_native(
    monkeypatch: pytest.MonkeyPatch,
):
    assert resolve_voice_mode() == NATIVE

    monkeypatch.setenv("VOICE_MODE", "Cascaded")
    assert resolve_voice_mode() == CASCADED, "the deployment default"

    await save_user_settings({VOICE_MODE: NATIVE})
    assert resolve_voice_mode() == NATIVE, "the user's choice wins"

    await save_user_settings({VOICE_MODE: "robot"})
    assert resolve_voice_mode() == CASCADED, "an unknown choice falls through"

    monkeypatch.setenv("VOICE_MODE", "robot")
    assert resolve_voice_mode() == NATIVE


def _invocation(run_config: RunConfig | None = None) -> SimpleNamespace:
    """What `VoiceSessionPlugin` gets: the invocation, its session, and its run config."""
    return SimpleNamespace(
        session=SimpleNamespace(state={}), run_config=run_config or RunConfig()
    )


@pytest.mark.asyncio
async def test_the_plugin_pins_the_mode_and_sets_the_run_config(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LIVE_VOICE_NAME", "Kore")
    native = _invocation()
    await VoiceSessionPlugin().before_run_callback(invocation_context=native)  # type: ignore[arg-type]
    assert native.session.state[VOICE_MODE_STATE] == NATIVE
    voice = native.run_config.speech_config.voice_config.prebuilt_voice_config
    assert voice.voice_name == "Kore"

    await save_user_settings({VOICE_MODE: CASCADED})
    cascaded = _invocation()
    await VoiceSessionPlugin().before_run_callback(invocation_context=cascaded)  # type: ignore[arg-type]
    assert cascaded.session.state[VOICE_MODE_STATE] == CASCADED
    assert cascaded.run_config.speech_config is None

    await save_user_settings({VOICE_MODE: NATIVE})
    assert is_cascaded(cascaded.session), (
        "a change applies from the next conversation"
    )
    assert not is_cascaded(None), "outside a session, the current choice"


# ---- the models ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_native_session_opens_the_live_model():
    model = await root_agent.canonical_live_model_async(_session(NATIVE))  # type: ignore[arg-type]
    assert isinstance(model, Gemini) and model.model == MODEL


@pytest.mark.asyncio
async def test_a_cascaded_session_opens_the_cascade_with_the_users_voice(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("LIVE_VOICE_NAME", "Kore")
    model: Any = await root_agent.canonical_live_model_async(_session(CASCADED))  # type: ignore[arg-type]

    assert isinstance(model, CascadeLive)
    llm: Any = model._resolve_llm()
    assert isinstance(llm, _MinimalThinking)
    # The cascade's text model isn't pinned: it uses GOOGLE_CLOUD_LOCATION.
    assert not llm.client_kwargs
    assert model.model == "gemini-3.5-flash-lite"
    stt: Any = model.stt
    tts: Any = model.tts
    assert stt._llm.model == "gemini-3.5-transcribe-live"
    assert tts._llm.model == "gemini-3.8-flash-tts"
    # Vertex AI serves the speech models only from `global`.
    assert stt._llm.client_kwargs == {"location": SPEECH_LOCATION}
    assert tts._llm.client_kwargs == {"location": SPEECH_LOCATION}
    assert tts._config.speech_config == "Kore"
    # The agent's own model, and so every other ADK path, stays native.
    canonical: Any = root_agent.canonical_model
    assert canonical.model == MODEL
    # Live models are regional, so the native model is pinned to its own location.
    assert canonical.client_kwargs == {"location": DEFAULT_LIVE_MODEL_LOCATION}


@pytest.mark.asyncio
async def test_the_cascade_is_built_once_per_voice_and_models(
    monkeypatch: pytest.MonkeyPatch,
):
    assert cascade_model("Puck") is cascade_model("Puck")
    assert cascade_model("Puck") is not cascade_model("Kore")

    monkeypatch.setenv("CASCADE_STT_MODEL", "stt-x")
    monkeypatch.setenv("CASCADE_LLM_MODEL", "llm-x")
    monkeypatch.setenv("CASCADE_TTS_MODEL", "tts-x")
    custom: Any = cascade_model("Puck")
    assert (custom.stt._llm.model, custom.model, custom.tts._llm.model) == (
        "stt-x",
        "llm-x",
        "tts-x",
    )


@pytest.mark.asyncio
async def test_the_cascades_text_model_thinks_minimally(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: list[LlmRequest] = []

    async def fake_generate(
        self: Gemini, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        seen.append(llm_request)
        yield LlmResponse()

    monkeypatch.setattr(Gemini, "generate_content_async", fake_generate)
    request = LlmRequest(config=types.GenerateContentConfig(temperature=0.5))
    async for _ in _MinimalThinking(model="m").generate_content_async(request):
        pass

    (sent,) = seen
    assert sent.config.thinking_config is not None
    assert (
        sent.config.thinking_config.thinking_level
        == types.ThinkingLevel.MINIMAL
    )
    assert sent.config.temperature == 0.5
    assert request.config.thinking_config is None, (
        "the live request is left as it was"
    )


# ---- tools in a cascaded session ---------------------------------------------------


async def _declared(tool: Any, mode: str) -> LlmRequest:
    """The request a session in `mode` sends, with `tool` declared as ADK declares it live."""
    request = LlmRequest()
    await tool.process_llm_request(
        tool_context=_session(mode), llm_request=request
    )
    mark_live_async_tools_non_blocking(request)
    return request


@pytest.mark.asyncio
async def test_a_cascaded_session_declares_plain_tools():
    tool: Any = thread_tool(start_thread)

    native = await _declared(tool, NATIVE)
    assert native.tools_dict["start_thread"] is tool
    assert (
        native.config.tools is not None
        and native.config.tools[0].function_declarations is not None
    )
    (declaration,) = native.config.tools[0].function_declarations
    assert declaration.behavior == types.Behavior.NON_BLOCKING

    cascaded = await _declared(tool, CASCADED)
    plain = cascaded.tools_dict["start_thread"]
    assert plain is tool.cascaded and not inspect.isasyncgenfunction(plain.func)
    assert plain.response_scheduling is None
    assert (
        cascaded.config.tools is not None
        and cascaded.config.tools[0].function_declarations is not None
    )
    (cascaded_decl,) = cascaded.config.tools[0].function_declarations
    assert cascaded_decl.behavior is None, (
        "no Live-only fields reach the text model"
    )
    assert cascaded_decl.description == declaration.description


@pytest.mark.asyncio
async def test_a_cascaded_thread_tool_returns_at_once_and_milestones_follow(
    lab: Lab,
):
    queue = LiveRequestQueue()
    get_thread_updates().attach(get_thread_registry(), queue)
    dispatch_wrapper: Any = thread_tool(start_thread)
    approve_wrapper: Any = thread_tool(approve)
    dispatch = dispatch_wrapper.cascaded
    approve_tool = approve_wrapper.cascaded

    lab.agent.script({"steps": [{"run": "git push origin main"}]}, {})
    started = await dispatch.func(goal=GOAL, project=REPO)
    thread_id = started["thread_id"]
    assert "_since_seq" not in started
    await lab.settle(thread_id)
    (perm,) = await _background_turns(queue)
    assert perm.startswith("[BACKGROUND THREAD UPDATE]") and thread_id in perm
    assert "permission to run" in perm

    await approve_tool.func(thread_id=thread_id)
    await lab.settle(thread_id)
    (finished,) = await _background_turns(queue)
    assert thread_id in finished and "finished" in finished, (
        "no milestone is lost"
    )
    assert await _background_turns(queue) == [], "and none is repeated"


@pytest_asyncio.fixture
async def mcp_lab(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Lab]:
    """A lab whose harness runs reach the MCP bridge (`update_user`) over HTTP."""
    async with serve_mcp() as base_url:
        monkeypatch.delenv("VOICE_AGENT_MCP_BASE_URL", raising=False)
        monkeypatch.setenv("APP_URL", base_url)
        yield lab


@pytest.mark.asyncio
async def test_a_cascaded_thread_says_its_approach_then_its_result(
    mcp_lab: Lab,
):
    queue = LiveRequestQueue()
    get_thread_updates().attach(get_thread_registry(), queue)
    mcp_lab.agent.script(
        {
            "steps": [
                {"finding": "Starting with flights, then late-start days."},
                {"finding": "Cheapest refundable flights come to $1,320."},
                {"sleep": 0.3},
            ],
            "result": "A three-day Portland plan.",
        }
    )
    start_wrapper: Any = thread_tool(start_thread)
    started = await start_wrapper.cascaded.func(
        goal="Plan a Portland weekend", project="projects/portland"
    )
    await mcp_lab.settle(started["thread_id"])
    await wait_until(lambda: queue._queue.qsize() >= 2)

    approach, finished = await _background_turns(queue)
    assert approach.startswith("[BACKGROUND THREAD UPDATE]")
    assert (
        "is under way: Starting with flights, then late-start days." in approach
    )
    assert "$1,320" not in approach, "only the approach is spoken unasked"
    assert "A three-day Portland plan." in finished


@pytest.mark.asyncio
async def test_a_cascaded_slow_tool_returns_pending_then_sends_its_result():
    queue = LiveRequestQueue()
    get_thread_updates().attach(get_thread_registry(), queue)
    release = asyncio.Event()

    async def lookup(city: str) -> str:
        """Look up the weather.

        Args:
            city: The city.
        """
        await release.wait()
        return f"Sunny in {city}"

    assert (
        await non_blocking_tool(lookup).cascaded.func(city="Austin") == PENDING
    )
    assert await _background_turns(queue) == []

    release.set()
    request = await asyncio.wait_for(queue.get(), 1)
    assert request.content is not None and request.content.parts is not None
    assert (
        request.content.parts[0].text == "[TOOL RESULT] lookup: Sunny in Austin"
    )


# ---- the run config and the instruction --------------------------------------------


def test_a_native_session_connects_in_the_users_voice_with_continuity():
    config = live_run_config(RunConfig(), NATIVE, "Kore")
    assert (
        config.speech_config is not None
        and config.speech_config.voice_config is not None
        and config.speech_config.voice_config.prebuilt_voice_config is not None
        and config.speech_config.voice_config.prebuilt_voice_config.voice_name
        == "Kore"
    )
    assert config.session_resumption is not None
    assert (
        config.context_window_compression is not None
        and config.context_window_compression.sliding_window is not None
    )


def test_resumption_the_caller_chose_is_kept():
    """`/run_live?enable_session_resumption` (and any handle) wins over the default."""
    chosen = types.SessionResumptionConfig(transparent=True, handle="h-1")
    base = RunConfig(session_resumption=chosen)
    assert live_run_config(base, NATIVE, "Kore").session_resumption is chosen
    assert base.speech_config is None, "the caller's config is left as it was"


def test_a_cascaded_session_gets_no_live_connect_settings():
    config = live_run_config(RunConfig(), CASCADED, "Kore")
    assert (
        config.speech_config,
        config.session_resumption,
        config.context_window_compression,
    ) == (None, None, None)


@pytest.mark.asyncio
async def test_only_a_cascaded_session_is_told_to_speak_for_text_to_speech():
    cascaded = build_orchestrator_instruction(_session(CASCADED))  # type: ignore[arg-type]
    native = build_orchestrator_instruction(_session(NATIVE))  # type: ignore[arg-type]
    assert "SPEAKING FOR TEXT-TO-SPEECH" in cascaded
    assert "No markdown, lists" in cascaded
    assert "SPEAKING FOR TEXT-TO-SPEECH" not in native


def test_the_live_model_location_is_its_own_setting(
    monkeypatch: pytest.MonkeyPatch,
):
    # Never GOOGLE_CLOUD_LOCATION: that is `global`, where Live models aren't served.
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.delenv("LIVE_MODEL_LOCATION", raising=False)
    assert live_model_location() == DEFAULT_LIVE_MODEL_LOCATION
    monkeypatch.setenv("LIVE_MODEL_LOCATION", "europe-west4")
    assert live_model_location() == "europe-west4"
