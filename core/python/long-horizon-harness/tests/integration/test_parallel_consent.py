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

"""Parallel ask_user consents are independent, partial-safe, and recoverable.

Drives the real ADK runner + ``permission_guard`` with a scripted LLM that emits
two parallel function calls, both resolving to ask_user. Pins the contract a user
asked about ("with two consents, do we wait for both?"):

  - approving ONE consent advances the model immediately (no hard barrier),
  - the still-pending tool does NOT run (no approval, no execution),
  - approving it later still runs it (the second card is not orphaned).

Scripted LLM, no Vertex — runs in CI.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.memory import InMemoryMemoryService
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.tool_confirmation import ToolConfirmation
from google.genai import types

from app.guardrails.permission_guard import permission_guard

pytestmark = pytest.mark.asyncio

APP = "test_parallel_consent"


class _ScriptedLlm(BaseLlm):
    model: str = "scripted"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        # A tool response in the contents means a prior call resolved → the model
        # is being re-invoked. Emit a sentinel so the test can see it advanced.
        advanced = any(
            p.function_response
            for c in llm_request.contents or []
            for p in (c.parts or [])
        )
        if advanced:
            yield LlmResponse(
                content=types.Content(
                    role="model", parts=[types.Part(text="ADVANCED")]
                )
            )
            return
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="fc_alpha", name="alpha", args={}
                        )
                    ),
                    types.Part(
                        function_call=types.FunctionCall(
                            id="fc_beta", name="beta", args={}
                        )
                    ),
                ],
            )
        )


def _confirm_ids(events) -> dict[str, str]:
    """originalFunctionCall.name -> adk_request_confirmation function-call id."""
    out: dict[str, str] = {}
    for e in events:
        for fc in e.get_function_calls() or []:
            if fc.name == "adk_request_confirmation":
                out[fc.args.get("originalFunctionCall", {}).get("name")] = fc.id
    return out


def _texts(events) -> list[str]:
    return [
        p.text
        for e in events
        for p in (e.content.parts if e.content else [])
        if p.text
    ]


def _approve(confirm_id: str) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=confirm_id,
                    name="adk_request_confirmation",
                    response=ToolConfirmation(
                        confirmed=True, payload={}
                    ).model_dump(),
                )
            )
        ],
    )


async def test_partial_approval_advances_without_running_pending_tool() -> None:
    ran: list[str] = []

    def alpha() -> dict:
        ran.append("alpha")
        return {"ok": True}

    def beta() -> dict:
        ran.append("beta")
        return {"ok": True}

    ss, ms = InMemorySessionService(), InMemoryMemoryService()
    agent = Agent(
        name="root_agent",
        model=_ScriptedLlm(),
        tools=[alpha, beta],
        before_tool_callback=permission_guard,
    )
    runner = Runner(
        app=App(name=APP, root_agent=agent),
        session_service=ss,
        memory_service=ms,
    )
    sess = await ss.create_session(app_name=APP, user_id="u")

    async def drive(message: types.Content) -> list:
        return [
            e
            async for e in runner.run_async(
                user_id="u", session_id=sess.id, new_message=message
            )
        ]

    # Turn 1: two parallel calls → two independent consent cards, nothing runs.
    t1 = await drive(types.Content(role="user", parts=[types.Part(text="go")]))
    confirms = _confirm_ids(t1)
    assert set(confirms) == {"alpha", "beta"}
    assert ran == []

    # Approve ONLY alpha: alpha runs, model advances, beta stays un-run.
    t2 = await drive(_approve(confirms["alpha"]))
    assert ran == ["alpha"]
    assert "ADVANCED" in _texts(t2)

    # Approve beta later: it still executes — the second card was not orphaned.
    t3 = await drive(_approve(confirms["beta"]))
    assert ran == ["alpha", "beta"]
    assert "ADVANCED" in _texts(t3)


async def test_both_consents_approved_in_one_resume() -> None:
    """Both confirmations can be answered in a single resume message — ADK's
    resume processor parses every adk_request_confirmation response on the last
    user event, so both tools run in one go."""
    ran: list[str] = []

    def alpha() -> dict:
        ran.append("alpha")
        return {"ok": True}

    def beta() -> dict:
        ran.append("beta")
        return {"ok": True}

    ss, ms = InMemorySessionService(), InMemoryMemoryService()
    agent = Agent(
        name="root_agent",
        model=_ScriptedLlm(),
        tools=[alpha, beta],
        before_tool_callback=permission_guard,
    )
    runner = Runner(
        app=App(name=APP, root_agent=agent),
        session_service=ss,
        memory_service=ms,
    )
    sess = await ss.create_session(app_name=APP, user_id="u")

    async def drive(message: types.Content) -> list:
        return [
            e
            async for e in runner.run_async(
                user_id="u", session_id=sess.id, new_message=message
            )
        ]

    t1 = await drive(types.Content(role="user", parts=[types.Part(text="go")]))
    confirms = _confirm_ids(t1)
    assert set(confirms) == {"alpha", "beta"}

    # One resume Content carrying BOTH confirmation responses.
    both = types.Content(
        role="user",
        parts=[
            _approve(confirms["alpha"]).parts[0],
            _approve(confirms["beta"]).parts[0],
        ],
    )
    t2 = await drive(both)
    assert sorted(ran) == ["alpha", "beta"]
    assert "ADVANCED" in _texts(t2)
