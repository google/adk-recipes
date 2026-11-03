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

"""`GoogleApi`'s error hints and the text helpers the servers share."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from workspace_mcp._google import (
    GoogleApi,
    GoogleApiError,
    html_to_text,
    truncate,
)


def _error_body(reason: str = "", message: str = "nope") -> dict[str, Any]:
    error: dict[str, Any] = {"code": 0, "message": message}
    if reason:
        error["errors"] = [{"reason": reason}]
    return {"error": error}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "hint"),
    [
        (401, _error_body(), "reconnect Google in Settings → Apps"),
        (403, _error_body("insufficientPermissions"), "Grant access"),
        (
            403,
            {
                "error": {
                    "message": "scopes",
                    "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}],
                }
            },
            "Grant access",
        ),
        (403, _error_body("accessNotConfigured"), "not enabled"),
        (
            403,
            _error_body("forbidden", "no access"),
            "Google refused: no access",
        ),
        (403, _error_body("rateLimitExceeded"), "wait a minute"),
        (429, _error_body(), "wait a minute"),
        (404, _error_body(message="File not found"), "not found: check the ID"),
        (400, _error_body(message="Bad q"), "rejected the request: Bad q"),
        (500, "oops", "Google API error 500"),
    ],
)
async def test_errors_become_hints(status: int, body: Any, hint: str) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    async with GoogleApi("t", transport=httpx.MockTransport(reply)) as api:
        with pytest.raises(GoogleApiError) as caught:
            await api.get("https://example.test/x")
    assert hint in str(caught.value)
    assert caught.value.status == status


@pytest.mark.asyncio
async def test_an_unreachable_google_is_an_error_not_a_crash() -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    async with GoogleApi("t", transport=httpx.MockTransport(reply)) as api:
        with pytest.raises(GoogleApiError, match="could not reach Google"):
            await api.get("https://example.test/x")


@pytest.mark.asyncio
async def test_json_empty_and_text_replies() -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path == "/text":
            return httpx.Response(200, text="a,b\n1,2")
        return httpx.Response(200, json={"q": request.url.params["q"]})

    async with GoogleApi("t", transport=httpx.MockTransport(reply)) as api:
        assert await api.get("https://e.test/j", params={"q": "x"}) == {
            "q": "x"
        }
        assert await api.delete("https://e.test/d") == {}
        assert await api.get_text("https://e.test/text") == "a,b\n1,2"


def test_truncate() -> None:
    assert truncate("abc", 5) == ("abc", False)
    assert truncate("abcdef", 3) == ("abc", True)


def test_html_to_text() -> None:
    html = (
        "<html><head><style>p{}</style><title>T</title></head><body>"
        "<p>Hi&nbsp;Ada,</p><div>See <a href='x'>the  doc</a>.</div>"
        "<script>alert(1)</script><ul><li>one</li><li>two</li></ul></body></html>"
    )
    assert html_to_text(html) == "Hi Ada,\nSee the doc.\none\ntwo"
