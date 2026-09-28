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

"""Generic ``/<skill>`` → skill bridge in the slash-command dispatcher.

A skill named ``foo`` is invocable as ``/foo``: when the built-in lookup
misses but a bound skill matches, the dispatcher rewrites the last user
turn to instruct the model to use that skill (skills run via the model,
not via canned LlmResponses) and returns ``None`` so the turn proceeds.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.adk.models import LlmResponse
from google.genai.types import Blob, Content, Part

pytestmark = pytest.mark.asyncio


def _request(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        contents=[Content(role="user", parts=[Part(text=text)])]
    )


def _last_user_text(req: SimpleNamespace) -> str | None:
    for content in reversed(req.contents):
        if content.role != "user":
            continue
        for part in content.parts:
            if part.text:
                return part.text
    return None


async def test_skill_with_args_rewrites_and_falls_through(monkeypatch):
    from app.commands import dispatcher as disp

    monkeypatch.setattr(disp, "bound_skill_names", lambda: {"chathelp"})
    req = _request("/chathelp triage my inbox")

    result = await disp.make_slash_command_dispatcher()(
        callback_context=SimpleNamespace(), llm_request=req
    )

    assert result is None
    assert _last_user_text(req) == "Use the chathelp skill. triage my inbox"


async def test_skill_no_args_rewrites_without_trailing_space(monkeypatch):
    from app.commands import dispatcher as disp

    monkeypatch.setattr(disp, "bound_skill_names", lambda: {"chathelp"})
    req = _request("/chathelp")

    result = await disp.make_slash_command_dispatcher()(
        callback_context=SimpleNamespace(), llm_request=req
    )

    assert result is None
    assert _last_user_text(req) == "Use the chathelp skill."


async def test_unknown_command_left_untouched(monkeypatch):
    from app.commands import dispatcher as disp

    monkeypatch.setattr(disp, "bound_skill_names", lambda: {"chathelp"})
    req = _request("/nope do x")

    result = await disp.make_slash_command_dispatcher()(
        callback_context=SimpleNamespace(), llm_request=req
    )

    assert result is None
    assert _last_user_text(req) == "/nope do x"


async def test_builtin_short_circuits(monkeypatch):
    from app.commands import dispatcher as disp

    async def _ping(args: str, callback_context):
        return "pong"

    monkeypatch.setitem(disp.BUILTIN_COMMAND_REGISTRY, "ping", _ping)
    monkeypatch.setattr(disp, "bound_skill_names", set)
    req = _request("/ping now")

    result = await disp.make_slash_command_dispatcher()(
        callback_context=SimpleNamespace(), llm_request=req
    )

    assert isinstance(result, LlmResponse)
    assert result.content.parts[0].text == "pong"
    assert _last_user_text(req) == "/ping now"


async def test_image_only_turn_falls_through(monkeypatch):
    from app.commands import dispatcher as disp

    monkeypatch.setattr(disp, "bound_skill_names", lambda: {"chathelp"})
    req = SimpleNamespace(
        contents=[
            Content(
                role="user",
                parts=[
                    Part(
                        inline_data=Blob(mime_type="image/png", data=b"\x89PNG")
                    )
                ],
            )
        ]
    )

    result = await disp.make_slash_command_dispatcher()(
        callback_context=SimpleNamespace(), llm_request=req
    )

    assert result is None
    assert _last_user_text(req) is None


async def test_mixed_case_skill_does_not_match(monkeypatch):
    from app.commands import dispatcher as disp

    monkeypatch.setattr(disp, "bound_skill_names", lambda: {"chathelp"})
    req = _request("/ChatHelp do x")

    result = await disp.make_slash_command_dispatcher()(
        callback_context=SimpleNamespace(), llm_request=req
    )

    assert result is None
    assert _last_user_text(req) == "/ChatHelp do x"


async def test_builtin_wins_over_colliding_skill(monkeypatch):
    from app.commands import dispatcher as disp

    async def _model(args: str, callback_context):
        return "builtin-model"

    monkeypatch.setitem(disp.BUILTIN_COMMAND_REGISTRY, "model", _model)
    monkeypatch.setattr(disp, "bound_skill_names", lambda: {"model"})
    req = _request("/model")

    result = await disp.make_slash_command_dispatcher()(
        callback_context=SimpleNamespace(), llm_request=req
    )

    assert isinstance(result, LlmResponse)
    assert result.content.parts[0].text == "builtin-model"
    assert _last_user_text(req) == "/model"
