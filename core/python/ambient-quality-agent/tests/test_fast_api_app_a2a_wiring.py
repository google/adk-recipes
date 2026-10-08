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

"""How the A2A surface is attached to the agent's app.

Each of these guards something that fails silently rather than loudly: an SSE
separator that makes a chat hang with no error, an executor flag whose absence
drops every tool call from the transcript, a runner over the wrong agent that
publishes a card describing something else.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from ambient_quality_agent import fast_api_app
from ambient_quality_agent.app_utils import a2a as a2a_module
from ambient_quality_agent.app_utils.task_store import GcsTaskStore
from ambient_quality_agent.core import chat_agent
from fastapi import FastAPI

# --- trap T1: the SSE frame separator ---------------------------------------- #


def test_sse_frames_end_with_lf_not_crlf() -> None:
    """Agent Runtime's relay dispatches only on a blank LF line, and
    `sse_starlette` writes CRLF. Without this the browser gets exactly one
    frame and the chat hangs -- no error, no log, just silence (b/556488800).

    Importing `fast_api_app` is what applies it, so this also pins that the
    patch runs at import and not somewhere a later refactor could skip.
    """
    from sse_starlette.event import ServerSentEvent
    from sse_starlette.sse import EventSourceResponse

    assert ServerSentEvent.DEFAULT_SEPARATOR == "\n"
    assert EventSourceResponse.DEFAULT_SEPARATOR == "\n"


def test_a_rendered_frame_actually_uses_lf() -> None:
    """The assignment above is only useful if the SDK reads it when rendering.
    Asserting the attribute alone would still pass if the class stopped using
    it."""
    from sse_starlette.event import ServerSentEvent

    rendered = ServerSentEvent(data="hello").encode()

    assert rendered.endswith(b"\n\n")
    assert b"\r" not in rendered


# --- trap T3: the executor flags --------------------------------------------- #


def _captured_handler(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Attaches A2A routes against stub targets and returns captured route configuration.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        Dictionary of captured handlers, routes, and keyword arguments.
    """
    seen: dict[str, Any] = {}

    def _capture(
        app: Any, *, agent_card_routes: Any, jsonrpc_routes: Any
    ) -> None:
        seen["card_routes"] = agent_card_routes
        seen["rpc_routes"] = jsonrpc_routes

    def _jsonrpc(handler: Any, **kwargs: Any) -> list[Any]:
        seen["handler"] = handler
        seen["jsonrpc_kwargs"] = kwargs
        return []

    monkeypatch.setattr(a2a_module, "add_a2a_routes_to_fastapi", _capture)
    monkeypatch.setattr(a2a_module, "create_jsonrpc_routes", _jsonrpc)
    monkeypatch.setattr(
        a2a_module, "create_agent_card_routes", lambda *a, **k: []
    )

    agent = chat_agent.build_chat_agent()
    asyncio.run(
        a2a_module.attach_a2a_routes(
            FastAPI(),
            agent=agent,
            runner=object(),
            task_store=object(),
            rpc_path="/a2a/aqa_chat",
        )
    )
    return seen


def test_the_executor_is_forced_onto_the_new_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`force_new_version` is what puts tool events on the wire at all. Without
    it, only text is sent, so every tool call silently vanishes from the
    transcript and the dashboard renders a reply that appears to come from
    nowhere."""
    executor = _captured_handler(monkeypatch)["handler"].agent_executor

    assert executor._force_new_version is True


def test_saved_artifacts_are_attached_to_the_a2a_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the interceptor an artifact stays in the artifact service and
    never reaches the client, so `save_artifact` writes a file nobody can
    open."""
    from google.adk.a2a.executor.interceptors.include_artifacts_in_a2a_event import (
        include_artifacts_in_a2a_event_interceptor,
    )

    executor = _captured_handler(monkeypatch)["handler"].agent_executor

    assert include_artifacts_in_a2a_event_interceptor in (
        executor._config.execute_interceptors
    )


# --- trap T2: the wire dialect ------------------------------------------------ #


def test_both_wire_dialects_are_served(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dashboard's client reads the 1.0 tagged-union encoding; on 0.3 every
    part accessor returns null and the chat renders blank with no error. 0.3 is
    advertised alongside it because registering the agent in Gemini validates
    the 0.3 card shape."""
    seen = _captured_handler(monkeypatch)

    assert seen["jsonrpc_kwargs"]["enable_v0_3_compat"] is True


def test_a_stripped_version_header_is_inferred_not_guessed_wrong() -> None:
    """A gateway can strip `A2A-Version`. The method name carries the dialect:
    0.3 uses `message/send`, 1.0 uses `SendMessage`."""
    from starlette.requests import Request

    builder = a2a_module._A2AServerCallContextBuilder()

    def _request(
        method: str, headers: list[tuple[bytes, bytes]] | None = None
    ) -> Request:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/a2a/aqa_chat",
                "headers": headers or [],
                "query_string": b"",
            }
        )
        # What the dispatcher has already parsed by the time the builder runs.
        request._json = {"method": method}
        return request

    for method, expected in (("message/send", "0.3"), ("SendMessage", "1.0")):
        context = builder.build(_request(method))

        assert context.state["headers"]["A2A-Version"] == expected, method


def test_a_version_header_that_survives_is_left_alone() -> None:
    """Inference is the fallback. A client that sent the header must be taken
    at its word, or a 0.3 client talking to us gets answered in 1.0."""
    from starlette.requests import Request

    builder = a2a_module._A2AServerCallContextBuilder()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/a2a/aqa_chat",
            "headers": [(b"a2a-version", b"0.3")],
            "query_string": b"",
        }
    )
    # A 1.0-looking method name, so inference would disagree with the header.
    request._json = {"method": "SendMessage"}

    context = builder.build(request)

    assert context.state["headers"]["A2A-Version"] == "0.3"


# --- the wiring in fast_api_app ------------------------------------------------ #


def test_the_rpc_path_is_the_one_the_ui_proxy_forwards_to() -> None:
    """The UI rewrites `/a2a` to `/a2a/<agent name>`; a mismatch here is a 404
    the browser reports as a chat that will not start."""
    assert fast_api_app.A2A_RPC_PATH == f"/a2a/{chat_agent.AGENT_NAME}"
    assert fast_api_app.A2A_RPC_PATH == "/a2a/aqa_chat"


def test_the_lifespan_serves_the_agent_it_advertises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`attach_a2a_routes` builds the card from `agent` but executes whatever
    `runner` wraps. Handing it a runner over a different agent publishes a card
    that does not describe what is served -- and nothing at runtime notices."""
    seen: dict[str, Any] = {}

    async def _capture(_app: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0)  # the real one awaits the card build
        seen.update(kwargs)

    monkeypatch.setattr(fast_api_app, "attach_a2a_routes", _capture)
    monkeypatch.setattr(fast_api_app, "_build_task_store", object)

    async def _drive() -> None:
        async with fast_api_app._lifespan(FastAPI()):
            pass

    asyncio.run(_drive())

    assert seen["agent"].name == chat_agent.AGENT_NAME
    assert seen["runner"].app.root_agent is seen["agent"]
    assert seen["rpc_path"] == fast_api_app.A2A_RPC_PATH


def test_the_runner_shares_the_processs_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `shared://` registry exists so a session created on the A2A path is
    visible to ADK's own routes. A runner built on its own services would undo
    that silently."""
    from ambient_quality_agent.app_utils import services

    seen: dict[str, Any] = {}

    async def _capture(_app: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0)  # the real one awaits the card build
        seen.update(kwargs)

    monkeypatch.setattr(fast_api_app, "attach_a2a_routes", _capture)
    monkeypatch.setattr(fast_api_app, "_build_task_store", object)

    async def _drive() -> None:
        async with fast_api_app._lifespan(FastAPI()):
            pass

    asyncio.run(_drive())

    assert seen["runner"].session_service is services.get_session_service()
    assert seen["runner"].artifact_service is services.get_artifact_service()


# --- the task store choice ------------------------------------------------------ #


def test_a_configured_bucket_gets_the_persistent_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AQA_JOBS_GCS_BUCKET", "some-jobs-bucket")

    store = fast_api_app._build_task_store()

    assert isinstance(store, GcsTaskStore)


def test_no_bucket_falls_back_to_memory_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Refusing to serve chat because history cannot be kept would be the worse
    trade -- a local run has no bucket. The warning is what keeps the
    difference from being invisible."""
    from a2a.server.tasks import InMemoryTaskStore

    monkeypatch.setenv("AQA_JOBS_GCS_BUCKET", "")

    with caplog.at_level("WARNING"):
        store = fast_api_app._build_task_store()

    assert isinstance(store, InMemoryTaskStore)
    assert any("AQA_JOBS_GCS_BUCKET" in r.message for r in caplog.records)
