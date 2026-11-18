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

"""The user-message limit on ADK's run routes (``POST /run``, ``/run_sse``).

The Studio's MCP tool and REST route refuse a message over
``session_limits.MAX_MESSAGE`` characters; ADK's routes have no hook
before the run starts (and ``/run_sse`` has answered 200 by then), so this
middleware reads the request body first and answers 400 with the same
message. The session service still cuts a longer message when it stores it
(``session_limits.capped``), for the servers this app does not build:
``adk web`` in ``make playground`` and ``adk eval``.
"""

import json
from typing import Any

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from genmedia4commerce.studio.session_limits import MAX_MESSAGE, TOO_LONG

__all__ = ["MessageLimit"]

_RUN_PATHS = ("/run", "/run_sse")


def _typed_length(body: bytes) -> int:
    """Characters of text in the request's ``new_message`` (ADK accepts
    snake_case and camelCase); 0 when the body is not such a request, which
    ADK then refuses itself."""
    try:
        req = json.loads(body)
    except ValueError:
        return 0
    message: Any = isinstance(req, dict) and (
        req.get("new_message") or req.get("newMessage")
    )
    parts = message.get("parts") if isinstance(message, dict) else None
    if not isinstance(parts, list):
        return 0
    return sum(
        len(p["text"])
        for p in parts
        if isinstance(p, dict) and isinstance(p.get("text"), str)
    )


class MessageLimit:
    """400 for an ADK run whose user message is over ``MAX_MESSAGE``
    characters; every other request passes untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if not (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"] in _RUN_PATHS
        ):
            await self.app(scope, receive, send)
            return
        chunks, more = [], True
        while more:
            message = await receive()
            if message["type"] != "http.request":  # the client went away
                return
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)
        if _typed_length(body) > MAX_MESSAGE:
            refused = JSONResponse({"detail": TOO_LONG}, status_code=400)
            await refused(scope, receive, send)
            return
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)
