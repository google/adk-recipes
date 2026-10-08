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

"""Hermetic tests for the `/a2a/...` proxy and its SSE relay.

Drives the FastAPI app in-process with `httpx.ASGITransport`, the same seam
`tests/test_ui_investigations.py` uses -- no socket and no real engine. The
proxy's own outbound call is stubbed at `app._forward`, which exists as a
separate function for exactly this reason: `httpx.AsyncClient` is also how the
ASGI test client below talks to the app under test, so patching that name
globally would stub both sides of one test.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar

import httpx
import pytest

# The UI is not a package -- it is flattened to the image root -- so `app` is
# a top-level module. `pythonpath` in pyproject.toml puts both roots in place.
import app as ui_app

_ENGINE_ID = "projects/123/locations/us-east1/reasoningEngines/456"
_ENGINE_API = f"https://us-east1-aiplatform.googleapis.com/reasoningEngines/v1/{_ENGINE_ID}/api"


@pytest.fixture(autouse=True)
def _fresh_backend_choice() -> Any:
    """`app._get_backend_client` is cached for the process, which is right in production
    and wrong here: each test picks its own backend through the environment."""
    ui_app._get_backend_client.cache_clear()
    yield
    ui_app._get_backend_client.cache_clear()


class _FakeUpstreamResponse:
    def __init__(
        self, status_code: int, content: bytes, content_type: str
    ) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}

    def json(self) -> Any:
        return json.loads(self.content)


class _CallRecorder:
    """Records every call to the stubbed `_forward`; returns a canned reply."""

    def __init__(self, response: _FakeUpstreamResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, method: str, url: str, **kwargs: Any
    ) -> _FakeUpstreamResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


def _inject_upstream(
    monkeypatch: pytest.MonkeyPatch, response: _FakeUpstreamResponse
) -> _CallRecorder:
    fake = _CallRecorder(response)
    monkeypatch.setattr(ui_app, "_forward", fake)
    return fake


def _use_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_ADK_BASE_URL", raising=False)
    monkeypatch.delenv("AQA_BACKEND", raising=False)
    monkeypatch.setenv("AGENT_ENGINE_RESOURCE_ID", _ENGINE_ID)
    monkeypatch.setattr(ui_app, "get_adc_token", lambda: "fake-token")


async def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=ui_app.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.request(method, path, **kwargs)


# --- routing and auth ------------------------------------------------------ #


def test_the_local_adk_backend_is_forwarded_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local-dev path: no ADC, plain forward."""
    monkeypatch.setenv("AGENT_ADK_BASE_URL", "http://127.0.0.1:8801")
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    fake = _inject_upstream(
        monkeypatch,
        _FakeUpstreamResponse(200, b'{"ok": true}', "application/json"),
    )

    resp = asyncio.run(
        _request("POST", "/a2a/aqa_chat", content=b'{"method": "message/send"}')
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert fake.calls[0]["url"] == "http://127.0.0.1:8801/a2a/aqa_chat"
    assert "authorization" not in fake.calls[0]["headers"]


def test_the_deployed_engine_gets_an_adc_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser holds no ADC, so this hop has to attach one -- against the
    same container passthrough the command routes already use."""
    _use_engine(monkeypatch)
    fake = _inject_upstream(
        monkeypatch, _FakeUpstreamResponse(200, b"{}", "application/json")
    )

    resp = asyncio.run(
        _request("GET", "/a2a/aqa_chat/.well-known/agent-card.json")
    )

    assert resp.status_code == 200
    call = fake.calls[0]
    assert (
        call["url"] == f"{_ENGINE_API}/a2a/aqa_chat/.well-known/agent-card.json"
    )
    assert call["headers"]["authorization"] == "Bearer fake-token"


def test_the_upstream_is_whatever_client_from_env_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy asks the shared client which backend is live instead of
    re-reading the environment, so the chat transport and the command-route
    transport cannot disagree. `AGENT_ENGINE_URL` alone is the case that
    catches a reimplementation: `build_client_from_env` honours it and a
    resource-id-only reading does not."""
    monkeypatch.delenv("AGENT_ADK_BASE_URL", raising=False)
    monkeypatch.delenv("AGENT_ENGINE_RESOURCE_ID", raising=False)
    monkeypatch.delenv("AQA_BACKEND", raising=False)
    monkeypatch.setenv(
        "AGENT_ENGINE_URL",
        f"https://us-east1-aiplatform.googleapis.com/v1/{_ENGINE_ID}",
    )
    monkeypatch.setattr(ui_app, "get_adc_token", lambda: "fake-token")
    fake = _inject_upstream(
        monkeypatch, _FakeUpstreamResponse(200, b"{}", "application/json")
    )

    resp = asyncio.run(
        _request("POST", "/a2a", content=b'{"method": "message/send"}')
    )

    assert resp.status_code == 200
    assert fake.calls[0]["url"] == f"{_ENGINE_API}/a2a/aqa_chat"


def test_no_configured_backend_is_a_503_that_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AGENT_ADK_BASE_URL",
        "AGENT_ENGINE_RESOURCE_ID",
        "AGENT_ENGINE_URL",
        "AQA_BACKEND",
    ):
        monkeypatch.delenv(name, raising=False)

    resp = asyncio.run(
        _request("GET", "/a2a/aqa_chat/.well-known/agent-card.json")
    )

    assert resp.status_code == 503
    assert "AGENT_ENGINE_RESOURCE_ID" in resp.json()["error"]


def test_the_root_aliases_reach_the_chat_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dashboard's client hardcodes `/a2a` and the well-known root, so both
    have to resolve to the chat agent without the browser naming it."""
    _use_engine(monkeypatch)
    fake = _inject_upstream(
        monkeypatch, _FakeUpstreamResponse(200, b"{}", "application/json")
    )

    asyncio.run(_request("POST", "/a2a", content=b'{"method": "message/send"}'))
    asyncio.run(_request("GET", "/.well-known/agent-card.json"))

    assert fake.calls[0]["url"] == f"{_ENGINE_API}/a2a/aqa_chat"
    assert fake.calls[1]["url"] == (
        f"{_ENGINE_API}/a2a/aqa_chat/.well-known/agent-card.json"
    )


# --- the agent card -------------------------------------------------------- #


def test_the_card_is_rewritten_to_point_back_at_this_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An A2A client builds its transport from the card's URLs. Passed through
    unchanged they name an endpoint the browser cannot call, so the card fetch
    succeeds and every call after it goes nowhere."""
    _use_engine(monkeypatch)
    upstream_card = json.dumps(
        {
            "name": "aqa_chat",
            "url": f"{_ENGINE_API}/a2a/aqa_chat",
            "supportedInterfaces": [{"url": f"{_ENGINE_API}/a2a/aqa_chat"}],
        }
    ).encode()
    _inject_upstream(
        monkeypatch,
        _FakeUpstreamResponse(200, upstream_card, "application/json"),
    )

    resp = asyncio.run(_request("GET", "/.well-known/agent-card.json"))

    card = resp.json()
    assert card["url"] == "/a2a/aqa_chat"
    assert card["supportedInterfaces"][0]["url"] == "/a2a/aqa_chat"


def test_a_placeholder_engine_answers_503_rather_than_a_page_the_client_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terraform creates the engine with a hello-world placeholder image, which
    answers every path with its own page under a 200. Passed through, the card
    fetch succeeds and the browser's A2A client fails on HTML."""
    _use_engine(monkeypatch)
    _inject_upstream(
        monkeypatch,
        _FakeUpstreamResponse(
            200, b"<html>Congratulations</html>", "text/html"
        ),
    )

    resp = asyncio.run(_request("GET", "/.well-known/agent-card.json"))

    assert resp.status_code == 503
    assert resp.json()["error"] == ui_app._NOT_DEPLOYED


def test_an_empty_success_is_read_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An engine swapping revisions answers 200 with nothing at all, which is
    no more readable than the placeholder's page."""
    _use_engine(monkeypatch)
    _inject_upstream(
        monkeypatch, _FakeUpstreamResponse(200, b"", "application/json")
    )

    resp = asyncio.run(
        _request("POST", "/a2a", content=b'{"method": "message/send"}')
    )

    assert resp.status_code == 503
    assert resp.json()["error"] == ui_app._NOT_DEPLOYED


def test_a_card_that_fails_upstream_is_passed_through_unrewritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rewriting a 404 body would dress a failure up as a card."""
    _use_engine(monkeypatch)
    _inject_upstream(
        monkeypatch,
        _FakeUpstreamResponse(404, b'{"error": "no such agent"}', "text/x"),
    )

    resp = asyncio.run(_request("GET", "/.well-known/agent-card.json"))

    assert resp.status_code == 404
    assert resp.json() == {"error": "no such agent"}


# --- which calls stream ---------------------------------------------------- #


def test_streaming_methods_are_relayed_and_others_are_not() -> None:
    """Cancelling a turn needs the task id while the turn is still running, so
    a streaming call must not be buffered into one reply at the end."""
    assert ui_app._is_streaming_send(b'{"method": "message/stream"}')
    assert ui_app._is_streaming_send(b'{"method": "SendStreamingMessage"}')
    assert not ui_app._is_streaming_send(b'{"method": "GetTask"}')
    assert not ui_app._is_streaming_send(b'{"method": "message/send"}')
    assert not ui_app._is_streaming_send(b"not json")
    assert not ui_app._is_streaming_send(b"")


def test_resubscribe_streams_too() -> None:
    """Their version listed only the two send methods, so a resubscribe took
    the buffered branch and arrived as one blob at the end of the turn. It has
    not bitten them because nothing calls resubscribe yet."""
    assert ui_app._is_streaming_send(b'{"method": "tasks/resubscribe"}')
    assert ui_app._is_streaming_send(b'{"method": "TaskSubscription"}')


# --- the relay ------------------------------------------------------------- #


def _fake_stream(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    lines: list[str],
    content_type: str = "text/event-stream",
) -> None:
    """Stubs the upstream streaming client with an in-memory SSE response.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        status: HTTP status code for the upstream response.
        lines: Lines of the streaming response body.
        content_type: Media type header value.
    """
    streaming = status == 200 and content_type.startswith("text/event-stream")

    class _Upstream:
        status_code = status
        headers: ClassVar[dict[str, str]] = {"content-type": content_type}

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        async def aread(self) -> bytes:
            return "\n".join(lines).encode()

        async def aiter_lines(self) -> Any:
            if not streaming:
                raise AssertionError(
                    "must not read lines from an unframed response"
                )
            for line in lines:
                yield line

    class _Client:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        def stream(self, *_a: Any, **_k: Any) -> Any:
            return _Upstream()

    # `ui_app._create_streaming_client`, not `ui_app.httpx.AsyncClient`: that name is the
    # shared httpx module, which is also how the ASGI client below reaches the
    # app, so patching it there stubs both sides of the test.
    monkeypatch.setattr(ui_app, "_create_streaming_client", _Client)


def _relay(body: bytes = b"{}") -> list[str]:
    async def collect() -> list[str]:
        return [
            chunk.decode()
            async for chunk in ui_app._stream_a2a("http://up/a2a/x", {}, body)
        ]

    return asyncio.run(collect())


def test_a_stream_missing_its_artifacts_is_repaired_before_the_final_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent Runtime drops the artifact updates ADK emits during a run, so
    without the repair the browser gets a reply with no tool calls -- and the
    repaired frames have to precede the frame a client may stop reading at."""
    _fake_stream(
        monkeypatch,
        200,
        [
            'data: {"id":1,"result":{"task":{"id":"t9","contextId":"c9"}}}',
            'data: {"id":1,"result":{"statusUpdate":{"final":true,'
            '"status":{"state":"completed"}}}}',
        ],
    )

    async def fake_forward(_m: str, _u: str, **_k: Any) -> Any:
        # Yields to the event loop to simulate asynchronous network forwarding.
        await asyncio.sleep(0)
        return _FakeUpstreamResponse(
            200,
            json.dumps(
                {
                    "result": {
                        "task": {
                            "artifacts": [
                                {"parts": [{"data": {"name": "list_insights"}}]}
                            ]
                        }
                    }
                }
            ).encode(),
            "application/json",
        )

    monkeypatch.setattr(ui_app, "_forward", fake_forward)

    frames = _relay()

    kinds = [
        next(iter(json.loads(f[len("data: ") :])["result"])) for f in frames
    ]
    assert kinds == ["task", "artifactUpdate", "statusUpdate"]
    assert "list_insights" in frames[1]


def test_a_stream_that_carried_its_artifacts_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repairing unconditionally would duplicate every tool call."""
    _fake_stream(
        monkeypatch,
        200,
        [
            'data: {"id":1,"result":{"task":{"id":"t1","contextId":"c1"}}}',
            'data: {"id":1,"result":{"artifactUpdate":{"artifact":{"parts":[]}}}}',
            'data: {"id":1,"result":{"statusUpdate":{"final":true}}}',
        ],
    )

    async def must_not_fetch(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(0)
        raise AssertionError(
            "a stream that carried artifacts must not be repaired"
        )

    monkeypatch.setattr(ui_app, "_forward", must_not_fetch)

    assert len(_relay()) == 3


def test_a_failed_repair_still_delivers_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The text already reached the browser. Letting the GetTask failure
    propagate would discard it along with the withheld final frame."""
    _fake_stream(
        monkeypatch,
        200,
        [
            'data: {"id":1,"result":{"task":{"id":"t9","contextId":"c9"}}}',
            'data: {"id":1,"result":{"statusUpdate":{"final":true}}}',
        ],
    )

    async def boom(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(0)
        raise RuntimeError("GetTask exploded")

    monkeypatch.setattr(ui_app, "_forward", boom)

    frames = _relay()

    assert len(frames) == 2
    assert "statusUpdate" in frames[-1]


def test_an_upstream_failure_becomes_one_error_frame_not_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Iterating a non-200 body for `data:` lines yields nothing, and an empty
    stream renders as a reply that never arrives."""
    _fake_stream(monkeypatch, 500, ['{"error":"engine exploded"}'])

    frames = _relay()

    assert len(frames) == 1
    assert "engine exploded" in frames[0]
    assert json.loads(frames[0][len("data: ") :])["error"]["code"] == 500


def test_a_jsonrpc_error_frame_is_passed_straight_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A2A reports a refusal as a 200 carrying an error object; it is the
    turn's answer, not a transport failure."""
    _fake_stream(
        monkeypatch,
        200,
        ['data: {"id":1,"error":{"code":-32000,"message":"nope"}}'],
    )

    frames = _relay()

    assert len(frames) == 1
    assert json.loads(frames[0][len("data: ") :])["error"]["message"] == "nope"


def test_a_200_without_sse_framing_becomes_one_error_frame_not_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected request is answered with 200 and a bare JSON-RPC error body.
    None of it starts with `data:`, so relaying it line by line yields nothing
    -- the same hang as an upstream failure, from a response that succeeded."""
    _fake_stream(
        monkeypatch,
        200,
        [
            '{"jsonrpc":"2.0","id":7,"error":{"code":-32600,"message":"Invalid request"}}'
        ],
        content_type="application/json",
    )

    frames = _relay(b'{"jsonrpc":"2.0","id":7,"method":"message/stream"}')

    assert len(frames) == 1
    error = json.loads(frames[0][len("data: ") :])["error"]
    assert error == {"code": -32600, "message": "Invalid request"}


def test_an_unframed_body_that_is_not_jsonrpc_is_reported_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An intermediary answering 200 with an HTML page carries no error object
    to reuse, and dropping the body would leave the user with nothing to act
    on."""
    _fake_stream(
        monkeypatch,
        200,
        ["<html>upstream ate it</html>"],
        content_type="text/html",
    )

    frames = _relay()

    assert "upstream ate it" in frames[0]
    assert json.loads(frames[0][len("data: ") :])["error"]["code"] == -32603


def test_a_synthesized_error_frame_carries_the_id_of_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client matches every frame's id against the one it sent and raises
    a mismatch, which would reach the user in place of the error itself."""
    _fake_stream(monkeypatch, 500, ['{"error":"engine exploded"}'])

    frames = _relay(b'{"jsonrpc":"2.0","id":42,"method":"message/stream"}')

    assert json.loads(frames[0][len("data: ") :])["id"] == 42


def test_an_unparseable_frame_is_forwarded_rather_than_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silently swallowing it would leave the client waiting on a frame that
    was sent."""
    _fake_stream(monkeypatch, 200, ["data: {not json", "ignored: heartbeat"])

    frames = _relay()

    assert frames == ["data: {not json\n\n"]


def test_a_streaming_call_leaves_the_route_as_an_event_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tests above cover the relay; this one covers the route choosing it.
    A streaming send that took the buffered branch would still answer 200 with
    the right bytes, just all at the end -- so from outside, the media type and
    the no-buffering header are what tell the two apart."""
    _use_engine(monkeypatch)
    _fake_stream(
        monkeypatch,
        200,
        [
            'data: {"id":1,"result":{"task":{"id":"t1","contextId":"c1"}}}',
            'data: {"id":1,"result":{"artifactUpdate":{"artifact":{"parts":[]}}}}',
            'data: {"id":1,"result":{"statusUpdate":{"final":true}}}',
        ],
    )

    async def go() -> tuple[httpx.Response, str]:
        transport = httpx.ASGITransport(app=ui_app.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as client:
            async with client.stream(
                "POST", "/a2a", content=b'{"method": "message/stream"}'
            ) as response:
                body = "".join([chunk async for chunk in response.aiter_text()])
                return response, body

    resp, body = asyncio.run(go())

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["x-accel-buffering"] == "no"
    assert body.count("data: ") == 3


# --- final-frame detection ------------------------------------------------- #


def test_a_terminal_state_counts_as_the_final_frame() -> None:
    """The proto-JSON dialect has no `final` field. Detecting only `final:
    true` lets the repaired artifacts arrive after the frame that ends the
    turn, which a client is entitled to stop reading at."""
    assert ui_app._is_final_frame(
        {"statusUpdate": {"status": {"state": "TASK_STATE_COMPLETED"}}}
    )
    assert ui_app._is_final_frame({"kind": "status-update", "final": True})
    assert ui_app._is_final_frame(
        {"statusUpdate": {"status": {"state": "TASK_STATE_FAILED"}}}
    )
    assert ui_app._is_final_frame({"status": {"state": "completed"}})
    assert not ui_app._is_final_frame(
        {"statusUpdate": {"status": {"state": "TASK_STATE_WORKING"}}}
    )
    assert not ui_app._is_final_frame({"task": {"id": "t"}})
