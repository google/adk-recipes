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

"""Shared provider helpers: owner ids, labels, Google credentials, and daemon health polling."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC
from typing import Any
from urllib.parse import urlsplit

from app.workers.providers.base import ComputerUnavailableError

logger = logging.getLogger(__name__)

COMPUTER_LABEL = "goog-adk-computer"
OWNER_LABEL = "owner"
DEPLOYMENT_LABEL = "deployment"
DEFAULT_DEPLOYMENT = "adk-voice-agent"
DEFAULT_COMPUTER_AUDIENCE = "adk-voice-agent-computer"
DEFAULT_IDLE_STOP_MIN = 20.0
APP_SERVICE_ACCOUNT_ID = "adk-voice-agent"

_OWNER_RE = re.compile(r"u-[0-9a-f]{16}")


def sandbox_owner(user_id: str) -> str:
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()
    return f"u-{digest[:16]}"


def owner_id(user_or_owner: str) -> str:
    if _OWNER_RE.fullmatch(user_or_owner):
        return user_or_owner
    return sandbox_owner(user_or_owner)


def is_owner_id(value: str) -> bool:
    return bool(_OWNER_RE.fullmatch(value))


def caller_service_account(project: str) -> str:
    return (
        os.getenv("SANDBOX_CALLER_SA")
        or f"{APP_SERVICE_ACCOUNT_ID}@{project}.iam.gserviceaccount.com"
    )


def computer_deployment() -> str:
    return (os.getenv("COMPUTER_DEPLOYMENT") or DEFAULT_DEPLOYMENT).strip()


def computer_auth_audience(owner: str | None = None) -> str:
    base = (
        (os.getenv("COMPUTER_AUTH_AUDIENCE") or DEFAULT_COMPUTER_AUDIENCE)
        .strip()
        .rstrip("/")
    )
    if not owner:
        return base
    return f"{base}/{owner_id(owner)}"


def idle_stop_minutes() -> float:
    try:
        return float(
            os.getenv("COMPUTER_IDLE_STOP_MIN") or DEFAULT_IDLE_STOP_MIN
        )
    except ValueError:
        return DEFAULT_IDLE_STOP_MIN


def app_port() -> int:
    url = (os.getenv("APP_URL") or "").strip()
    with contextlib.suppress(ValueError):
        if url and (port := urlsplit(url).port):
            return port
    return 8000


def is_loopback_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".test")


class OwnerLocks:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    @contextlib.asynccontextmanager
    async def hold(self, owner: str) -> AsyncIterator[None]:
        async with self._locks.setdefault(owner, asyncio.Lock()):
            yield


class _RefreshingToken:
    _EARLY_S = 300.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._credentials: Any = None

    def _load(self) -> Any:  # pragma: no cover - needs Google credentials
        raise NotImplementedError

    def token(self) -> str:
        from google.auth.transport.requests import Request

        with self._lock:
            if self._credentials is None:
                self._credentials = self._load()
            creds = self._credentials
            expiry = getattr(creds, "expiry", None)
            stale = expiry is None or (
                expiry.replace(tzinfo=UTC).timestamp() - time.time()
                < self._EARLY_S
            )
            if not getattr(creds, "token", None) or stale:
                creds.refresh(Request())
            return str(creds.token or "")


class IdTokenSource(_RefreshingToken):
    """Google ID tokens for `audience`; returns `""` and logs once if credentials cannot mint one."""

    def __init__(self, audience: str) -> None:
        super().__init__()
        self.audience = audience
        self._warned = False

    def _load(self) -> Any:  # pragma: no cover - needs Google credentials
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        return id_token.fetch_id_token_credentials(self.audience, Request())

    def __call__(self) -> str:
        try:
            return self.token()
        except Exception:
            if not self._warned:
                self._warned = True
                logger.warning(
                    "Could not mint an ID token for %s; calling the computer without one",
                    self.audience,
                    exc_info=True,
                )
            return ""


class AccessTokenSource(_RefreshingToken):
    def _load(self) -> Any:  # pragma: no cover - needs Google credentials
        import google.auth

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        return credentials

    def __call__(self) -> str:
        return self.token()


async def wait_for_daemon(
    base_url: str,
    *,
    headers: Callable[[], dict[str, str]],
    transport: Any | None = None,
    timeout_s: float = 180.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    import httpx

    from app.app_utils.http_client import get_pooled_http_client

    interval = 2.0
    attempts = max(1, int(timeout_s / interval))
    last = "no answer"
    req_timeout = httpx.Timeout(10.0, connect=5.0)
    for attempt in range(attempts):
        if attempt:
            await sleep(interval)
        try:
            req_headers = await asyncio.to_thread(headers)
            async with get_pooled_http_client(
                base_url, transport=transport
            ) as client:
                resp = await client.get(
                    f"{base_url}/healthz",
                    headers=req_headers,
                    timeout=req_timeout,
                )
            if resp.status_code == 200:
                return
            last = f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc) or type(exc).__name__
    logger.warning(
        "Computer daemon at %s did not become healthy within %.0fs: %s",
        base_url,
        timeout_s,
        last,
    )
    raise ComputerUnavailableError(
        f"The user's computer did not become healthy ({last})."
    )
