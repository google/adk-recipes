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

"""Unit tests for ``app.a2a.user_converter.request_converter``.

Two contracts are covered:

* ``user_id`` falls back to the upstream default unless the auth contextvar
  has been populated by ``IdentityMiddleware`` for the current request.
* ``run_config.streaming_mode`` is forced to ``SSE`` so the runner emits
  partial token deltas — required for typewriter rendering in the web UI.
"""

from __future__ import annotations

import contextvars

import pytest
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.types import SendMessageRequest
from google.adk.a2a import _compat as a2a_compat
from google.adk.agents.run_config import StreamingMode

from app.a2a.user_converter import request_converter
from app.auth.identity import _user_id_var


def _make_context(metadata: dict | None = None) -> RequestContext:
    msg_kwargs = {"metadata": metadata} if metadata is not None else {}
    message = a2a_compat.make_message(
        message_id="m1",
        role=a2a_compat.ROLE_USER,
        parts=[a2a_compat.make_text_part("hi")],
        **msg_kwargs,
    )
    return RequestContext(
        call_context=ServerCallContext(),
        request=SendMessageRequest(message=message),
        context_id="ctx-1",
    )


def _with_user(user_id: str | None):
    """Run the converter inside a contextvar scope with ``user_id`` set."""

    def runner(ctx: RequestContext):
        carrier = contextvars.copy_context()

        def _inner():
            if user_id is not None:
                _user_id_var.set(user_id)
            return request_converter(ctx)

        return carrier.run(_inner)

    return runner


def test_authenticated_user_id_overrides_default() -> None:
    ctx = _make_context()
    run_req = _with_user("stable-user-42")(ctx)
    assert run_req.user_id == "stable-user-42"


def test_falls_back_to_default_user_id_without_auth() -> None:
    ctx = _make_context()
    run_req = _with_user(None)(ctx)
    # Upstream default is f"A2A_USER_{context_id}".
    assert run_req.user_id == "A2A_USER_ctx-1"


@pytest.mark.parametrize("user", [None, "alice@example.com"])
def test_streaming_mode_is_sse(user: str | None) -> None:
    ctx = _make_context()
    run_req = _with_user(user)(ctx)
    assert run_req.run_config is not None
    assert run_req.run_config.streaming_mode == StreamingMode.SSE


def test_untyped_datapart_becomes_readable_text() -> None:
    # Untyped client DataParts arrive with no ADK metadata-type key. ADK's
    # default converter would wrap them in an unrenderable <a2a_datapart_json>
    # text/plain blob that is opaque to the model; the converter must degrade
    # them to readable text instead.
    ctx = RequestContext(
        call_context=ServerCallContext(),
        request=SendMessageRequest(
            message=a2a_compat.make_message(
                message_id="m1",
                role=a2a_compat.ROLE_USER,
                parts=[
                    a2a_compat.make_data_part(
                        data={
                            "version": "v0.9",
                            "action": {
                                "name": "gws_write_decision",
                                "surfaceId": "surface-1",
                            },
                        }
                    )
                ],
            )
        ),
        context_id="ctx-3",
    )
    run_req = _with_user(None)(ctx)
    parts = run_req.new_message.parts
    assert len(parts) == 1
    assert parts[0].inline_data is None
    assert parts[0].text is not None
    assert "gws_write_decision" in parts[0].text


def test_custom_metadata_is_preserved() -> None:
    # Upstream stuffs the A2A request.metadata into RunConfig.custom_metadata
    # under the "a2a_metadata" key. The SSE flip must not drop it.
    ctx = RequestContext(
        call_context=ServerCallContext(),
        request=SendMessageRequest(
            message=a2a_compat.make_message(
                message_id="m1",
                role=a2a_compat.ROLE_USER,
                parts=[a2a_compat.make_text_part("hi")],
            ),
            metadata={"foo": "bar"},
        ),
        context_id="ctx-2",
    )
    run_req = _with_user(None)(ctx)
    assert run_req.run_config is not None
    custom = run_req.run_config.custom_metadata or {}
    assert custom.get("a2a_metadata") == {"foo": "bar"}
