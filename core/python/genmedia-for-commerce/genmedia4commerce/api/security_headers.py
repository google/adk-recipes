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

"""Browser security headers on every response but the Studio MCP App's.

The Content Security Policy is enforced: the browser blocks whatever it does
not allow. It allows what the web app loads: its own bundle, Google Fonts,
and media from signed Cloud Storage URLs. ``/mcp`` is left alone: Gemini
Enterprise renders the MCP App with its own policy.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["CSP", "HEADERS", "SecurityHeadersMiddleware"]

_STORAGE = "https://storage.googleapis.com"
CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "connect-src 'self'",
        f"img-src 'self' data: blob: {_STORAGE}",
        f"media-src 'self' data: blob: {_STORAGE}",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "font-src 'self' https://fonts.gstatic.com",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": CSP,
}


def _is_mcp(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


class SecurityHeadersMiddleware:
    """Adds ``HEADERS`` to each HTTP response outside ``/mcp`` (a header the
    route already set is kept)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http" or _is_mcp(scope["path"]):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
