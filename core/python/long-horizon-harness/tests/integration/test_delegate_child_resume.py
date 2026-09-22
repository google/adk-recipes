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

"""drive_child: drain a resumable child runner, detect its HITL pause, resume it."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from horizon.subagents.delegate_runner import (
    DELEGATE_APP_NAME,
    build_resumable_child_runner,
    drive_child,
)

_CONFIRM_FN = "adk_request_confirmation"
EFFECTS: list[str] = []


def side_effect(tool_context: ToolContext) -> dict[str, Any]:
    EFFECTS.append("A")
    return {"status": "ok"}


def needs_approval(tool_context: ToolContext) -> dict[str, Any]:
    tc = getattr(tool_context, "tool_confirmation", None)
    if tc is None:
        tool_context.request_confirmation(hint="run X?", payload={"op": "X"})
        EFFECTS.append("B:req")
        return {"status": "pending"}
    if getattr(tc, "confirmed", False):
        EFFECTS.append("B:ok")
        return {"status": "done"}
    EFFECTS.append("B:no")
    return {"status": "declined"}


def _called(contents: list[types.Content] | None) -> set[str]:
    out: set[str] = set()
    for c in contents or []:
        for p in c.parts or []:
            fc = getattr(p, "function_call", None)
            if fc and fc.name:
                out.add(fc.name)
    return out


class _Llm(BaseLlm):
    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        called = _called(llm_request.contents)
        if "side_effect" not in called:
            name: str | None = "side_effect"
        elif "needs_approval" not in called:
            name = "needs_approval"
        else:
            name = None
        if name:
            yield LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(name=name, args={})
                        )
                    ],
                )
            )
        else:
            yield LlmResponse(
                content=types.Content(
                    role="model", parts=[types.Part(text="done")]
                )
            )


@pytest.mark.asyncio
async def test_drive_child_pauses_then_resumes_without_repeating_side_effect() -> (
    None
):
    EFFECTS.clear()
    child = LlmAgent(
        name="child", model=_Llm(model="x"), tools=[side_effect, needs_approval]
    )
    sessions = InMemorySessionService()
    runner = build_resumable_child_runner(child, sessions)
    session = await sessions.create_session(
        app_name=DELEGATE_APP_NAME, user_id="cu"
    )

    first = await drive_child(
        runner,
        user_id="cu",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="go")]),
    )
    assert first.status == "pending"
    assert first.pending is not None and first.pending.confirmation_id
    assert first.pending.hint == "run X?"
    assert EFFECTS == ["A", "B:req"]
    # A paused child has NOT done the work. An eval caught the parent
    # reporting "the subagent created the tests" off a run that stopped for
    # approval, so the summary has to say it paused, not just `status`.
    assert "PAUSED" in first.summary, first.summary

    resumed = await drive_child(
        runner,
        user_id="cu",
        session_id=session.id,
        new_message=types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=first.pending.confirmation_id,
                        name=_CONFIRM_FN,
                        response={"confirmed": True},
                    )
                )
            ],
        ),
    )
    assert resumed.status == "completed"
    assert "B:ok" in EFFECTS
    assert EFFECTS.count("A") == 1
