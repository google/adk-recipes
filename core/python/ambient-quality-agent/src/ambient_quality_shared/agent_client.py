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

"""Async client for AQuA's command routes and the address of its A2A chat agent.

Wiring::

    aqua_cli, ui/app.py
        │ build_client_from_env() or AgentRuntimeClient(...)
        ├────────────────────────────────────────┐
        ▼                                        ▼
    AgentRuntimeClient                        AdkHttpClient
        │ post_command()                         │ post_command()
        │ <container_base>/api/<route>           │ <base_url>/<route>
        ▼                                        ▼
    Agent Runtime                             local agent server

    AgentRuntimeClient, aqua_cli (metrics, traces, attach), ui/app.py A2A proxy
        │ get_adc_token()
        ▼
    ADC bearer token

    aqua_cli run, ui/app.py A2A proxy
        │ resolve_a2a_base_url(client)
        ▼
    AgentRuntimeClient.container_base + /api | AdkHttpClient.base_url
        │ aqua_cli run ──► agents-cli run --mode a2a; ui/app.py ──► httpx
        │ /a2a/<CHAT_A2A_APP>
        ▼
    chat agent

Backends (selected in ``build_client_from_env`` via env vars)
-------------------------------------------------------------
* **Agent Runtime** -- ``AgentRuntimeClient``, the default for a deployed
  agent. Calls the custom container routes with an ADC bearer token. Accepts a
  full engine URL or a resource id; requires only google-auth and httpx.

* **Local agent server** -- ``AdkHttpClient``. Targets a fast local loop against
  ``ambient_quality_agent.fast_api_app`` (started by ``tools/local_ui.sh``) when
  configured via ``AGENT_ADK_BASE_URL`` or ``AQA_BACKEND=adk``. Calls the same
  routes without authentication.

Both expose ``post_command`` with the same signature, so callers do not depend
on which backend they talk to.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any

import httpx

_ADC_CREDS = None  # Cached credentials for `get_adc_token`.


def get_adc_token() -> str:
    """Returns a valid Google Cloud Application Default Credentials access token.

    Lazily initializes and refreshes cached credentials as needed.

    Returns:
        Valid OAuth2 access token string.
    """
    global _ADC_CREDS
    import google.auth
    from google.auth.transport.requests import Request

    if _ADC_CREDS is None:
        _ADC_CREDS, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    if not getattr(_ADC_CREDS, "valid", False):
        _ADC_CREDS.refresh(Request())
    return _ADC_CREDS.token


# Matches the resource name wherever it sits in the path, so it is found in
# engine (`/v1/`, `/v1beta1/`, with or without a `:query` suffix) and container
# passthrough (`/reasoningEngines/v1/.../api`) URLs alike.
_RESOURCE_ID_PATTERN = re.compile(
    r"projects/[^/]+/locations/(?P<location>[^/]+)/reasoningEngines/[^/:?]+"
)


# ========================================================================== #
# Agent Runtime backend
# ========================================================================== #
class AgentRuntimeClient:
    """Client for AQuA's command routes on an Agent Runtime deployment.

    Authenticates every request with an ADC bearer token.
    """

    def __init__(
        self,
        resource_id: str | None = None,
        *,
        url: str | None = None,
    ) -> None:
        """Initializes an Agent Runtime client.

        Args:
            resource_id: Agent Runtime resource path
                (projects/P/locations/L/reasoningEngines/ID). Its location picks
                the regional endpoint unless url is also given.
            url: Engine (``/v1/``, ``/v1beta1/``, optionally with a
                ``:query`` / ``:streamQuery`` suffix) or container passthrough
                URL. Its host is kept, so non-regional endpoints resolve.

        Raises:
            ValueError: If neither resource_id nor url is provided, resource_id
                is not a full resource path, or url is not absolute or names no
                engine.
        """
        if not resource_id and not url:
            raise ValueError("AgentRuntimeClient needs a resource_id or a url.")
        if resource_id:
            match = _RESOURCE_ID_PATTERN.fullmatch(resource_id)
            if match is None:
                raise ValueError(
                    "Expected an engine resource like "
                    "projects/P/locations/L/reasoningEngines/ID, got "
                    f"{resource_id!r}."
                )
            self.resource_id = resource_id
            self._origin = (
                f"https://{match['location']}-aiplatform.googleapis.com"
            )
        if url:
            parsed = urllib.parse.urlsplit(url)
            if not (parsed.scheme and parsed.netloc):
                raise ValueError(
                    f"Expected an absolute engine URL, got {url!r}."
                )
            self._origin = f"{parsed.scheme}://{parsed.netloc}"
            if not resource_id:
                match = _RESOURCE_ID_PATTERN.search(parsed.path)
                if match is None:
                    raise ValueError(
                        "Expected an engine URL containing "
                        "projects/P/locations/L/reasoningEngines/ID, got "
                        f"{url!r}."
                    )
                self.resource_id = match.group(0)

    @classmethod
    def from_env(cls) -> AgentRuntimeClient:
        """Constructs an AgentRuntimeClient from environment variables.

        Returns:
            Configured AgentRuntimeClient instance.

        Raises:
            RuntimeError: If neither AGENT_ENGINE_RESOURCE_ID nor AGENT_ENGINE_URL
                is set, or either one is malformed.
        """
        url = os.getenv("AGENT_ENGINE_URL") or None
        rid = os.getenv("AGENT_ENGINE_RESOURCE_ID") or None
        if not url and not rid:
            raise RuntimeError(
                "Set AGENT_ENGINE_RESOURCE_ID (or AGENT_ENGINE_URL) for the "
                "Agent Runtime backend."
            )
        try:
            return cls(resource_id=rid, url=url)
        except ValueError as exc:
            # Callers of `build_client_from_env` handle a misconfigured
            # environment as RuntimeError.
            raise RuntimeError(
                f"AGENT_ENGINE_RESOURCE_ID / AGENT_ENGINE_URL: {exc}"
            ) from exc

    @property
    def container_base(self) -> str:
        """Base URL for custom routes served directly by the deployed container.

        Container routes always sit under ``/reasoningEngines/v1/<resource>``,
        whatever API version or form the engine URL uses.
        """
        return f"{self._origin}/reasoningEngines/v1/{self.resource_id}"

    def _build_headers(self) -> dict[str, str]:
        """Builds the headers for an authenticated JSON request.

        Returns:
            Header dictionary carrying an ADC bearer token.
        """
        return {
            "Authorization": f"Bearer {get_adc_token()}",
            "Content-Type": "application/json",
        }

    async def post_command(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Sends a POST request to an agent command route and returns the JSON response.

        Only the connection phase has a timeout; request duration is unbounded
        to accommodate long-running operations like BigQuery scans.

        Args:
            path: Command route path (e.g., 'investigations/list').
            payload: Optional JSON request payload.

        Returns:
            Decoded JSON response dictionary.

        Raises:
            httpx.HTTPStatusError: If the server returns a non-2xx status code.
        """
        url = f"{self.container_base}/api/{path}"
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=30.0)
        ) as http:
            r = await http.post(
                url, headers=self._build_headers(), json=payload or {}
            )
            r.raise_for_status()
            return r.json()


# ========================================================================== #
# Local agent server backend
# ========================================================================== #
class AdkHttpClient:
    """Client for AQuA's command routes on an unauthenticated local agent server.

    ``post_command`` calls the routes mounted by
    ``ambient_quality_agent.fast_api_app`` (served by ``tools/local_ui.sh``).
    """

    def __init__(self, base_url: str) -> None:
        """Initializes a local agent server client.

        Args:
            base_url: Root URL of the local agent server.
        """
        self.base_url = base_url.rstrip("/")

    @classmethod
    def from_env(cls) -> AdkHttpClient:
        """Constructs an AdkHttpClient from environment variables.

        Returns:
            Configured AdkHttpClient targeting the local dev server.
        """
        return cls(
            base_url=os.getenv("AGENT_ADK_BASE_URL") or "http://localhost:8000"
        )

    async def post_command(
        self, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Sends a POST request to a command route on the local agent server.

        Args:
            path: Command route path matching deployed endpoint paths.
            payload: Optional JSON request payload.

        Returns:
            Decoded JSON response dictionary.

        Raises:
            httpx.HTTPStatusError: If the server returns a non-2xx status code.
        """
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=30.0)
        ) as http:
            r = await http.post(f"{self.base_url}/{path}", json=payload or {})
            r.raise_for_status()
            return r.json()


# ========================================================================== #
# Backend selection
# ========================================================================== #
def build_client_from_env() -> AgentRuntimeClient | AdkHttpClient:
    """Constructs the appropriate agent client for the configured environment.

    Selects backend according to environment variables:
    1. If `AGENT_ADK_BASE_URL` is set, or `AQA_BACKEND` is 'adk', 'adk_http', or 'local':
       returns an `AdkHttpClient` for local development.
    2. If `AGENT_ENGINE_RESOURCE_ID` or `AGENT_ENGINE_URL` is set:
       returns an `AgentRuntimeClient` for deployed Agent Runtime agents.

    Returns:
        Client instance (AdkHttpClient or AgentRuntimeClient); both expose
        ``post_command`` and are accepted by ``resolve_a2a_base_url``.

    Raises:
        RuntimeError: If configuration targets unsupported backends or if required
            target environment variables are missing or malformed.
    """
    adk_base = os.getenv("AGENT_ADK_BASE_URL") or None
    rid = os.getenv("AGENT_ENGINE_RESOURCE_ID") or None
    engine_url = os.getenv("AGENT_ENGINE_URL") or None
    backend = (os.getenv("AQA_BACKEND") or "auto").strip().lower()

    if adk_base or backend in ("adk", "adk_http", "local"):
        return AdkHttpClient.from_env()

    # A2A is reached through the engine or local server, never configured as a
    # separate target, so reject settings that suggest otherwise.
    if backend == "a2a" or os.getenv("AGENT_A2A_BASE_URL"):
        raise RuntimeError(
            "AQA_BACKEND=a2a and AGENT_A2A_BASE_URL are not valid targets. The "
            "target is always AQuA's engine or a local server; the CLI and the "
            "dashboard reach the chat agent over A2A through it. Unset them and "
            "use AGENT_ENGINE_RESOURCE_ID or AGENT_ENGINE_URL (deployed) or "
            "AGENT_ADK_BASE_URL (local)."
        )

    if rid or engine_url:
        return AgentRuntimeClient.from_env()

    raise RuntimeError(
        "No agent target configured. Set AGENT_ENGINE_RESOURCE_ID "
        "(projects/.../locations/.../reasoningEngines/...) or AGENT_ENGINE_URL "
        "for a deployed agent, or AGENT_ADK_BASE_URL (+ AQA_BACKEND=adk) for a "
        "local agent server (`ambient_quality_agent.fast_api_app`)."
    )


def resolve_a2a_base_url(
    client: AgentRuntimeClient | AdkHttpClient,
) -> tuple[str, bool]:
    """Resolves the base URL that serves the agent's A2A routes.

    The A2A routes live under ``<base>/a2a/<app>``. On Agent Runtime they are
    custom container routes, so they sit under the container base rather than
    the engine's query endpoint.

    Args:
        client: Backend client for AQuA's engine or a local server.

    Returns:
        Tuple of ``(base_url, needs_auth)``. ``needs_auth`` is True when
        requests need an ADC bearer token, which only Agent Runtime does.

    Raises:
        RuntimeError: If the client type has no A2A route.
    """
    if isinstance(client, AdkHttpClient):
        # A local agent server is unauthenticated.
        return client.base_url, False
    if isinstance(client, AgentRuntimeClient):
        return f"{client.container_base}/api", True
    raise RuntimeError(f"No A2A route to a {type(client).__name__} backend.")


__all__ = [
    "AdkHttpClient",
    "AgentRuntimeClient",
    "build_client_from_env",
    "get_adc_token",
    "resolve_a2a_base_url",
]
