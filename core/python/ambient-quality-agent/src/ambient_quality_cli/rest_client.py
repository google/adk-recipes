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

"""A minimal JSON client for Google REST APIs, for discovery.

Discovery reads the observed agent's project with the user's Application
Default Credentials, over REST: one of its calls, the engine's ADK `list-apps`
route, has no gcloud command, and the CLI already speaks REST with ADC to AQuA
itself. httpx is already among the CLI's imports, so this adds no dependency.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

import httpx

DEFAULT_TIMEOUT_SECONDS = 60.0
"""How long a request waits for its response; `list-apps` may wait on an engine
instance starting."""


class ApiError(Exception):
    """A Google API answered with an error status."""

    def __init__(self, status: int, message: str):
        """Initializes the error.

        Args:
            status: HTTP status code.
            message: The API's error message, or the response text.
        """
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class JsonApi(Protocol):
    """The two calls discovery makes; tests stand in for them."""

    def get_json(
        self, url: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Sends a GET and decodes the JSON reply.

        Args:
            url: Full request URL.
            params: Query parameters.

        Returns:
            The decoded JSON object.

        Raises:
            ApiError: On an error status, a transport failure, or a reply
                that is not a JSON object.
        """
        ...

    def post_json(self, url: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Sends a POST with a JSON body and decodes the JSON reply.

        Args:
            url: Full request URL.
            body: Request body.

        Returns:
            The decoded JSON object.

        Raises:
            ApiError: On an error status, a transport failure, or a reply
                that is not a JSON object.
        """
        ...


class RestClient:
    """Calls Google REST APIs with a bearer token from ``token_provider``."""

    def __init__(
        self,
        token_provider: Callable[[], str],
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        """Initializes the client.

        Args:
            token_provider: Returns a valid OAuth2 access token.
            timeout: Seconds to wait for each response.
        """
        self._token_provider = token_provider
        self._timeout = timeout

    def get_json(
        self, url: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Sends a GET and decodes the JSON reply.

        Args:
            url: Full request URL.
            params: Query parameters.

        Returns:
            The decoded JSON object.

        Raises:
            ApiError: On a non-2xx status, a transport failure, or a reply
                that is not a JSON object.
        """
        return self._send("GET", url, params=params)

    def post_json(self, url: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """Sends a POST with a JSON body and decodes the JSON reply.

        Args:
            url: Full request URL.
            body: Request body.

        Returns:
            The decoded JSON object.

        Raises:
            ApiError: On a non-2xx status, a transport failure, or a reply
                that is not a JSON object.
        """
        return self._send("POST", url, json=body)

    def _send(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """Sends one request and decodes the JSON reply.

        Args:
            method: HTTP method.
            url: Full request URL.
            **kwargs: `params` or `json` for httpx.

        Returns:
            The decoded JSON object; empty for an empty body.

        Raises:
            ApiError: On a non-2xx status, a transport failure, or a reply
                that is not a JSON object.
        """
        headers = {"Authorization": f"Bearer {self._token_provider()}"}
        try:
            response = httpx.request(
                method, url, headers=headers, timeout=self._timeout, **kwargs
            )
        except httpx.HTTPError as exc:
            raise ApiError(0, f"{type(exc).__name__}: {exc}") from exc
        if not response.is_success:
            raise ApiError(response.status_code, _extract_api_message(response))
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise ApiError(response.status_code, "not a JSON reply") from exc
        if not isinstance(body, dict):
            raise ApiError(response.status_code, "not a JSON object")
        return body


def _extract_api_message(response: httpx.Response) -> str:
    """Extracts the message from a Google API error response.

    Args:
        response: The failed response.

    Returns:
        `error.message` from the body, or the body text.
    """
    try:
        return str(response.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return response.text.strip()[:300]
