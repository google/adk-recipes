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

"""A failed model call reaches the chat as one sentence, not a response dump.

The A2A executor ends the task with the error event's `error_message`, so the
end-to-end test stops at the runner's events: that field is what the dashboard
shows.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from ambient_quality_agent.core import chat_agent, model_errors
from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import _ResourceExhaustedError
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import errors as genai_errors
from google.genai import types

_DEBUG_DETAIL = "extensible_stubs::UNABLE_TO_RETRY: " + "x" * 5000


def _api_error(
    code: int, status: str, message: str, **extra: Any
) -> genai_errors.APIError:
    body = {
        "error": {"code": code, "message": message, "status": status, **extra}
    }
    cls = genai_errors.ClientError if code < 500 else genai_errors.ServerError
    return cls(code, body)


def _quota_error() -> genai_errors.APIError:
    """Builds a simulated ADK 429 ResourceExhaustedError with service debug info.

    Returns:
        ResourceExhaustedError instance wrapping a ClientError.
    """
    return _ResourceExhaustedError(
        _api_error(
            429,
            "RESOURCE_EXHAUSTED",
            "Resource exhausted. Please try again later.",
            details=[
                {
                    "@type": "type.googleapis.com/google.rpc.DebugInfo",
                    "detail": _DEBUG_DETAIL,
                }
            ],
        )
    )


class _FailingModel(BaseLlm):
    error: Exception

    async def generate_content_async(  # type: ignore[override]
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        raise self.error
        yield  # pragma: no cover - makes this an async generator


def _run_turn(error: Exception) -> list[Any]:
    agent = LlmAgent(
        name="aqa_chat",
        model=_FailingModel(model="fake", error=error),
        on_model_error_callback=model_errors.on_model_error,
    )
    runner = InMemoryRunner(agent=agent, app_name="t")

    async def go() -> list[Any]:
        session = await runner.session_service.create_session(
            app_name="t", user_id="u"
        )
        message = types.Content(
            role="user", parts=[types.Part(text="root cause?")]
        )
        return [
            event
            async for event in runner.run_async(
                user_id="u", session_id=session.id, new_message=message
            )
        ]

    return asyncio.run(go())


def test_the_quota_error_from_the_report_ends_the_turn_with_one_short_message() -> (
    None
):
    events = _run_turn(_quota_error())

    errors = [e for e in events if e.error_message]
    assert len(errors) == 1
    message = errors[0].error_message
    assert message == (
        "The model is out of capacity right now (429 RESOURCE_EXHAUSTED). "
        "Wait a minute and send your message again."
    )
    assert errors[0].error_code == "429 RESOURCE_EXHAUSTED"


def test_the_full_error_is_still_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _run_turn(_quota_error())

    assert any(
        _DEBUG_DETAIL in (r.exc_text or "") + r.getMessage()
        for r in caplog.records
    )


def test_an_error_that_is_not_the_models_still_propagates() -> None:
    with pytest.raises(KeyError):
        _run_turn(KeyError("bug in AQuA"))


def test_a_server_error_says_to_try_again() -> None:
    error = _api_error(
        503, "UNAVAILABLE", "The service is currently unavailable."
    )

    assert model_errors.render_user_message(error) == (
        "The model service failed to answer (503 UNAVAILABLE). Try again in a moment."
    )


def test_a_request_error_names_what_the_service_said() -> None:
    error = _api_error(
        400, "INVALID_ARGUMENT", "Request contains an invalid argument."
    )

    assert model_errors.render_user_message(error) == (
        "The model request failed (400 INVALID_ARGUMENT): "
        "Request contains an invalid argument."
    )


def test_a_long_service_message_is_cut_short() -> None:
    error = _api_error(400, "INVALID_ARGUMENT", "y" * 5000)

    assert len(model_errors.render_user_message(error)) < 400


def test_the_chat_agent_uses_the_handler() -> None:
    agent = chat_agent.build_chat_agent()

    assert agent.on_model_error_callback is model_errors.on_model_error
