# Copyright 2026 Google LLC
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

"""Tests for the chat's `set_goal`: nothing is written until the user approves.

A fake tool context stands in for ADK's. ADK runs the tool once to ask, then
again with the user's answer in `tool_confirmation`; the fake records the
request and lets a test supply the answer.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import AsyncGenerator
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.core import goal_chat_tools
from google.adk.agents.llm_agent import LlmAgent
from google.adk.events.event import Event
from google.adk.flows.llm_flows.functions import (
    REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
)
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.tools.tool_confirmation import ToolConfirmation
from google.genai import types as genai_types


class ConfirmingToolContext:
    """`.state`, `.tool_confirmation`, `.actions` and `request_confirmation`."""

    def __init__(self, confirmation: ToolConfirmation | None = None) -> None:
        self.state: dict[str, Any] = {}
        self.tool_confirmation = confirmation
        self.actions = types.SimpleNamespace(skip_summarization=False)
        self.requested: list[str | None] = []

    def request_confirmation(
        self, *, hint: str | None = None, payload: Any = None
    ) -> None:
        del payload
        self.requested.append(hint)


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replaces the save with a recorder; returns the goals it was given."""
    goals: list[str] = []

    def _save(tool_context: Any, *, goal: str) -> dict[str, Any]:
        del tool_context
        goals.append(goal)
        return {"goal": goal.strip(), "version": "v" if goal.strip() else None}

    monkeypatch.setattr(
        goal_chat_tools.document_tools,
        "set_goal",
        mock.AsyncMock(side_effect=_save),
    )
    return goals


def _call(goal: str, context: ConfirmingToolContext) -> dict[str, Any]:
    return asyncio.run(
        goal_chat_tools.set_goal(goal=goal, tool_context=context)
    )


def test_the_first_call_asks_for_approval_and_writes_nothing(saved) -> None:
    context = ConfirmingToolContext()

    result = _call("Look hardest at refunds.", context)

    assert saved == []
    assert context.requested == ["Save this as the developer goal?"]
    assert result["saved"] is False
    # What the model reads while the card waits: the card has the text.
    assert "do not repeat the text" in result["reason"]


def test_the_request_leaves_the_result_to_the_model() -> None:
    """With skip_summarization the result lands in the history as text, and
    Gemini refused the request that resumes after the approval: "400 Requests
    ending with a model turn are not supported", seen against
    gemini-3.8-flash. A scripted model does not enforce that rule, so this
    pins the setting instead."""
    context = ConfirmingToolContext()

    _call("Look hardest at refunds.", context)

    assert context.actions.skip_summarization is False


def test_an_empty_goal_asks_to_remove_it(saved) -> None:
    context = ConfirmingToolContext()

    _call("   ", context)

    assert saved == []
    assert context.requested == ["Remove the developer goal?"]


def test_an_approved_goal_is_saved_as_asked(saved) -> None:
    result = _call(
        "Look hardest at refunds.",
        ConfirmingToolContext(ToolConfirmation(confirmed=True)),
    )

    assert saved == ["Look hardest at refunds."]
    assert result == {
        "saved": True,
        "goal": "Look hardest at refunds.",
        "version": "v",
    }


def test_an_approved_removal_removes_the_goal(saved) -> None:
    result = _call("", ConfirmingToolContext(ToolConfirmation(confirmed=True)))

    assert saved == [""]
    assert result == {"saved": True, "goal": "", "version": None}


def test_a_declined_goal_is_not_saved(saved) -> None:
    result = _call(
        "Look hardest at refunds.",
        ConfirmingToolContext(ToolConfirmation(confirmed=False)),
    )

    assert saved == []
    assert result["saved"] is False
    assert "declined" in result["reason"]


def test_a_goal_over_8_kib_is_refused_before_asking(saved) -> None:
    """Asking the user to approve a text the save would refuse wastes their
    approval."""
    context = ConfirmingToolContext()

    result = _call("x" * 8193, context)

    assert context.requested == []
    assert saved == []
    assert "8 KiB" in result["error"]


class _ScriptedModel(BaseLlm):
    """Calls `set_goal` once, then answers in text."""

    goal: str

    async def generate_content_async(  # type: ignore[override]
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        del stream
        called = any(
            part.function_call and part.function_call.name == "set_goal"
            for content in llm_request.contents
            for part in content.parts or []
        )
        part = (
            genai_types.Part(text="Done.")
            if called
            else genai_types.Part(
                function_call=genai_types.FunctionCall(
                    name="set_goal", args={"goal": self.goal}
                )
            )
        )
        yield LlmResponse(
            content=genai_types.Content(role="model", parts=[part])
        )


def _run_with_answer(goal: str, *, confirmed: bool) -> list[Event]:
    """Runs a real ADK turn up to the approval request, then answers it.

    Returns:
        The events of the turn that carried the answer.
    """
    agent = LlmAgent(
        name="aqa_chat",
        model=_ScriptedModel(model="fake", goal=goal),
        tools=[goal_chat_tools.set_goal],
    )
    runner = InMemoryRunner(agent=agent, app_name="t")

    async def go() -> list[Event]:
        session = await runner.session_service.create_session(
            app_name="t", user_id="u"
        )
        asked = [
            e
            async for e in runner.run_async(
                user_id="u",
                session_id=session.id,
                new_message=genai_types.Content(
                    role="user", parts=[genai_types.Part(text="save it")]
                ),
            )
        ]
        [request] = [
            call
            for e in asked
            for call in e.get_function_calls()
            if call.name == REQUEST_CONFIRMATION_FUNCTION_CALL_NAME
        ]
        # What the dashboard's card reads: the original call and its arguments.
        assert request.args["originalFunctionCall"]["args"] == {"goal": goal}
        answer = genai_types.Content(
            role="user",
            parts=[
                genai_types.Part(
                    function_response=genai_types.FunctionResponse(
                        id=request.id,
                        name=REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
                        response={"confirmed": confirmed},
                    )
                )
            ],
        )
        return [
            e
            async for e in runner.run_async(
                user_id="u", session_id=session.id, new_message=answer
            )
        ]

    return asyncio.run(go())


def test_adk_runs_the_approved_call_and_saves_its_text(saved) -> None:
    goal = "Look hardest at refunds.\nTone does not matter."

    _run_with_answer(goal, confirmed=True)

    assert saved == [goal]


def test_adk_saves_nothing_when_the_user_declines(saved) -> None:
    _run_with_answer("Look hardest at refunds.", confirmed=False)

    assert saved == []


def test_a_failed_save_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        goal_chat_tools.document_tools,
        "set_goal",
        mock.AsyncMock(return_value={"error": "OSError: bucket gone"}),
    )

    result = _call(
        "A goal.", ConfirmingToolContext(ToolConfirmation(confirmed=True))
    )

    assert result == {"saved": False, "error": "OSError: bucket gone"}
