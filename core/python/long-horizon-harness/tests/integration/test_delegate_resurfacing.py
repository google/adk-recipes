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

"""End-to-end: the real delegate() surfaces a child's risky op to the user.

Drives the production `delegate` tool through a real parent runner. The child is
scripted (model + tools injected by monkeypatching `_build_child`) but the
orchestration — durable child session, bubble to the parent turn, resume in
place — is the real code path.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest
from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
from google.genai import types

import app.subagents.delegate as delegate_mod
from app.subagents.child_guard import make_child_policy_guard
from app.subagents.delegate import delegate

_CONFIRM_FN = "adk_request_confirmation"


def _called(contents: list[types.Content] | None) -> set[str]:
    out: set[str] = set()
    for c in contents or []:
        for p in c.parts or []:
            fc = getattr(p, "function_call", None)
            if fc and fc.name:
                out.add(fc.name)
    return out


def _fc(name: str, args: dict[str, Any] | None = None) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(name=name, args=args or {})
                )
            ],
        )
    )


def _text(s: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=s)])
    )


class _ChildLlm(BaseLlm):
    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if "risky_thing" not in _called(llm_request.contents):
            yield _fc("risky_thing")
        else:
            yield _text("child done")


class _ParentLlm(BaseLlm):
    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if "delegate" not in _called(llm_request.contents):
            yield _fc("delegate", {"goal": "do the thing"})
        else:
            yield _text("parent done")


def _child_factory(effects: list[str]) -> Callable[..., Any]:
    def risky_thing(tool_context: ToolContext) -> dict[str, Any]:
        effects.append("risky")
        return {"ok": True}

    async def _build(**_kwargs: Any) -> LlmAgent:
        return LlmAgent(
            name="child",
            model=_ChildLlm(model="child"),
            tools=[risky_thing],
            before_tool_callback=[
                make_child_policy_guard(parent_grants=None, profile=None)
            ],
        )

    return _build


def _confirm_call(events: list[Any]) -> types.FunctionCall | None:
    for e in events:
        for fc in e.get_function_calls() or []:
            if fc.name == _CONFIRM_FN:
                return fc
    return None


async def _make_parent(
    state: dict[str, Any] | None = None,
) -> tuple[Runner, str, str]:
    app = App(
        name="parent",
        root_agent=LlmAgent(
            name="parent", model=_ParentLlm(model="parent"), tools=[delegate]
        ),
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    sessions = InMemorySessionService()
    runner = Runner(app=app, session_service=sessions)
    await sessions.create_session(
        app_name="parent", user_id="u", session_id="s", state=state or {}
    )
    return runner, "u", "s"


async def _drive(
    runner: Runner, user_id: str, session_id: str, msg: types.Content
) -> list[Any]:
    return [
        e
        async for e in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=msg
        )
    ]


@pytest.mark.asyncio
async def test_child_risky_op_surfaces_and_resumes_on_approval(
    monkeypatch,
) -> None:
    effects: list[str] = []
    monkeypatch.setattr(delegate_mod, "_build_child", _child_factory(effects))
    runner, u, s = await _make_parent()

    run1 = await _drive(
        runner, u, s, types.Content(role="user", parts=[types.Part(text="go")])
    )
    parent_conf = _confirm_call(run1)
    assert parent_conf is not None, (
        "the child's risky op should surface to the user"
    )
    assert effects == [], "risky op must not run before approval"

    approve = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=parent_conf.id,
                    name=_CONFIRM_FN,
                    response={"confirmed": True},
                )
            )
        ],
    )
    await _drive(runner, u, s, approve)
    assert effects.count("risky") == 1, effects


@pytest.mark.asyncio
async def test_child_risky_op_denied_on_decline(monkeypatch) -> None:
    effects: list[str] = []
    monkeypatch.setattr(delegate_mod, "_build_child", _child_factory(effects))
    runner, u, s = await _make_parent()

    run1 = await _drive(
        runner, u, s, types.Content(role="user", parts=[types.Part(text="go")])
    )
    parent_conf = _confirm_call(run1)
    assert parent_conf is not None

    decline = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=parent_conf.id,
                    name=_CONFIRM_FN,
                    response={"confirmed": False},
                )
            )
        ],
    )
    await _drive(runner, u, s, decline)
    assert effects.count("risky") == 0, effects


def _ask_parent_answer(contents: list[types.Content] | None) -> str | None:
    for c in contents or []:
        for p in c.parts or []:
            fr = getattr(p, "function_response", None)
            if fr is not None and fr.name == "ask_parent":
                return (fr.response or {}).get("answer")
    return None


class _AskParentChildLlm(BaseLlm):
    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if "ask_parent" not in _called(llm_request.contents):
            yield _fc("ask_parent", {"question": "Which region should I use?"})
        else:
            yield _text(
                f"used region {_ask_parent_answer(llm_request.contents)}"
            )


def _ask_parent_child_factory() -> Callable[..., Any]:
    from app.subagents.ask_parent import ask_parent as ask_parent_tool

    async def _build(**_kwargs: Any) -> LlmAgent:
        return LlmAgent(
            name="child",
            model=_AskParentChildLlm(model="child"),
            tools=[ask_parent_tool],
            before_tool_callback=[
                make_child_policy_guard(parent_grants=None, profile=None)
            ],
        )

    return _build


def _delegate_result(events: list[Any]) -> dict[str, Any] | None:
    for e in events:
        content = getattr(e, "content", None)
        for p in getattr(content, "parts", None) or []:
            fr = getattr(p, "function_response", None)
            if fr is not None and fr.name == "delegate":
                return fr.response
    return None


@pytest.mark.asyncio
async def test_child_ask_parent_bubbles_and_resumes_with_free_text(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        delegate_mod, "_build_child", _ask_parent_child_factory()
    )
    runner, u, s = await _make_parent()

    run1 = await _drive(
        runner, u, s, types.Content(role="user", parts=[types.Part(text="go")])
    )
    conf = _confirm_call(run1)
    assert conf is not None, (
        "ask_parent should bubble the question to the parent turn"
    )

    # The parent answers with FREE TEXT, not just a boolean approval.
    answer = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=conf.id,
                    name=_CONFIRM_FN,
                    response={
                        "confirmed": True,
                        "payload": {"answer": "us-central1"},
                    },
                )
            )
        ],
    )
    run2 = await _drive(runner, u, s, answer)
    res = _delegate_result(run2)
    assert res is not None, (
        "delegate should return a result after the child resumes"
    )
    assert "us-central1" in str(res.get("summary", "")), res


@pytest.mark.asyncio
async def test_forwarded_parent_grant_skips_the_prompt(monkeypatch) -> None:
    effects: list[str] = []
    monkeypatch.setattr(delegate_mod, "_build_child", _child_factory(effects))
    # Pre-approve `risky_thing` in the parent session before the run.
    runner, u, s = await _make_parent(
        state={
            "permission_grants": [
                {"toolName": "risky_thing", "decision": "allow"}
            ]
        }
    )

    run1 = await _drive(
        runner, u, s, types.Content(role="user", parts=[types.Part(text="go")])
    )
    assert _confirm_call(run1) is None, "a pre-granted op must not prompt"
    assert effects.count("risky") == 1, effects
