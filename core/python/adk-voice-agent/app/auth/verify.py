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


"""Saving a new connection's tokens, labelled from a live identity check."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.app_utils.http_client import get_http_client
from app.auth.credential_store import save_user_credentials
from app.auth.github.api import GITHUB_API, GITHUB_PROVIDER, github_headers
from app.auth.google_apis import USERINFO_URL
from app.auth.providers import (
    WORKSPACE_PROVIDER,
    bearer_headers,
    record_provider_state,
    require_provider,
)

logger = logging.getLogger(__name__)

CHECK_ERRORS = (httpx.HTTPError, ValueError, KeyError)
IdentityProbe = Callable[[httpx.AsyncClient, str], Awaitable[str]]


async def _github_identity(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.get(f"{GITHUB_API}/user", headers=github_headers(token))
    resp.raise_for_status()
    return f"@{resp.json().get('login') or 'user'}"


async def _google_identity(client: httpx.AsyncClient, token: str) -> str:
    resp = await client.get(USERINFO_URL, headers=bearer_headers(token))
    resp.raise_for_status()
    data = resp.json()
    return str(data.get("email") or data.get("name") or "")


IDENTITY_PROBES: dict[str, IdentityProbe] = {
    WORKSPACE_PROVIDER: _google_identity,
    GITHUB_PROVIDER: _github_identity,
}


async def verify_and_save_token(
    provider: str,
    token: str,
    *,
    extra_env: dict[str, str | None] | None = None,
    expires_in: int | None = None,
) -> dict[str, Any]:
    """Save `token` for `provider` even if the live identity probe fails."""
    cfg = require_provider(provider)
    token = token.strip()
    if not token:
        raise ValueError("Token cannot be empty")

    updates: dict[str, str | None] = {cfg.access_token_env: token}
    updates.update({k: v.strip() for k, v in (extra_env or {}).items() if v})
    label = ""
    if probe := IDENTITY_PROBES.get(provider):
        try:
            async with get_http_client(timeout=8.0) as client:
                label = await probe(client, token)
        except CHECK_ERRORS as exc:
            logger.debug(
                "Live token verification skipped for %s: %s", provider, exc
            )
    if label and cfg.account_label_env:
        updates[cfg.account_label_env] = label
    await asyncio.to_thread(save_user_credentials, updates)
    state = record_provider_state(
        provider,
        label or cfg.account_label or f"{cfg.display_name} Connected",
        expires_in=expires_in,
    )
    return {
        "ok": True,
        "provider": provider,
        "account_label": state.account_label,
        "saved_keys": list(updates),
    }
