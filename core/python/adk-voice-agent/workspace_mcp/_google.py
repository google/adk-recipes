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

"""Authenticated Google REST client and error formatting for built-in Workspace MCP servers."""

from __future__ import annotations

import os
from html.parser import HTMLParser
from typing import Any

import httpx

ACCESS_TOKEN_ENV = "GOOGLE_WORKSPACE_ACCESS_TOKEN"  # noqa: S105 - an env var name
TIMEOUT_S = 20.0
_DETAIL_LIMIT = 200

_RECONNECT = "Google sign-in expired: the user should reconnect Google in Settings → Apps."
_GRANT = "missing permission: the user should Grant access in Settings → Apps."
_RATE = "Google is rate limiting these calls: wait a minute, then try again."


class GoogleApiError(Exception):
    def __init__(self, status: int, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.status, self.message, self.reason = status, message, reason

    def __str__(self) -> str:
        detail = self.message[:_DETAIL_LIMIT]
        if self.status == 401:
            return _RECONNECT
        if self.status == 429 or self.reason in (
            "rateLimitExceeded",
            "userRateLimitExceeded",
            "RATE_LIMIT_EXCEEDED",
        ):
            return _RATE
        if self.status == 403:
            if self.reason in ("accessNotConfigured", "SERVICE_DISABLED"):
                return (
                    "this Google API is not enabled in the OAuth client's project: "
                    f"run `make onboard` or enable it in the console ({detail})"
                )
            if self.reason in (
                "insufficientPermissions",
                "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
            ):
                return _GRANT
            return f"Google refused: {detail}"
        if self.status == 404:
            return f"not found: check the ID, or search again ({detail})"
        if self.status == 400:
            return f"Google rejected the request: {detail}"
        return f"Google API error {self.status}: {detail}"


def _error_from(response: httpx.Response) -> GoogleApiError:
    message, reason = response.reason_phrase or "error", ""
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or message)
        details = error.get("errors") or []
        if details and isinstance(details[0], dict):
            reason = str(details[0].get("reason") or "")
        for detail in error.get("details") or []:
            if isinstance(detail, dict) and detail.get("reason"):
                reason = str(detail["reason"])
    elif isinstance(error, str):
        message = str(body.get("error_description") or error)
    return GoogleApiError(response.status_code, message, reason)


class GoogleApi:
    def __init__(
        self, token: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_S,
            transport=transport,
        )

    @classmethod
    def from_env(cls) -> GoogleApi:
        token = os.environ.get(ACCESS_TOKEN_ENV, "").strip()
        if not token:
            raise GoogleApiError(401, f"{ACCESS_TOKEN_ENV} is not set")
        return cls(token)

    async def __aenter__(self) -> GoogleApi:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(
        self, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise GoogleApiError(
                0, f"could not reach Google ({type(exc).__name__}): try again"
            ) from exc
        if response.status_code >= 400:
            raise _error_from(response)
        return response

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        response = await self._send(method, url, **kwargs)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise GoogleApiError(
                response.status_code, "Google's reply was not JSON"
            ) from exc

    async def get(self, url: str, **kwargs: Any) -> Any:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> Any:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> Any:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> Any:
        return await self.request("DELETE", url, **kwargs)

    async def get_text(self, url: str, **kwargs: Any) -> str:
        return (await self._send("GET", url, **kwargs)).text


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


_BLOCK_TAGS = frozenset(
    "address article aside blockquote br dd div dl dt footer form h1 h2 h3 h4 h5 h6 "
    "header hr li main nav ol p pre section table td th tr ul".split()
)
_SKIP_TAGS = frozenset(("script", "style", "head", "title"))


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in _SKIP_TAGS:
            self._skipping += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    lines = (
        " ".join(line.split()) for line in "".join(parser.parts).splitlines()
    )
    return "\n".join(line for line in lines if line)
