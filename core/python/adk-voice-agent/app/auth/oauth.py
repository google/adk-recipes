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

"""OAuth 2.0 authorization-code flow (with PKCE where supported) and token refresh."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import Request

from app.app_utils.http_client import get_http_client
from app.auth.credential_store import (
    credential_owner,
    prefetch_user_credentials,
    reload_user_credentials,
    save_user_credentials,
)
from app.auth.env_store import load_dotenv_defaults
from app.auth.providers import (
    TOKEN_SOURCE_OAUTH,
    AuthProvider,
    get_provider,
    get_public_base_url,
    get_redirect_uri,
    is_configured,
    is_remote_deployment,
    provider_state,
    record_provider_state,
    require_provider,
    url_origin,
)
from app.auth.verify import verify_and_save_token

logger = logging.getLogger(__name__)

REFRESH_MARGIN_S = 120
PENDING_TTL_S = 600.0
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1"}


class TokenEndpointError(ValueError):
    def __init__(
        self, message: str, *, status_code: int, error: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error

    @property
    def grant_rejected(self) -> bool:
        return self.error == "invalid_grant" or self.status_code in {400, 401}


@dataclass
class _PendingAuthorization:
    provider: str
    redirect_uri: str
    code_verifier: str | None
    opener_origin: str
    user_id: str
    created_at: float = field(default_factory=time.monotonic)


_pending: dict[str, _PendingAuthorization] = {}


def _live_pending() -> dict[str, _PendingAuthorization]:
    cutoff = time.monotonic() - PENDING_TTL_S
    for state in [s for s, p in _pending.items() if p.created_at < cutoff]:
        del _pending[state]
    return _pending


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def start_pending(
    provider: str,
    redirect_uri: str,
    opener_origin: str,
    *,
    code_verifier: str | None = None,
) -> str:
    state = secrets.token_urlsafe(24)
    _live_pending()[state] = _PendingAuthorization(
        provider, redirect_uri, code_verifier, opener_origin, credential_owner()
    )
    return state


def peek_pending(state: str | None, provider: str) -> _PendingAuthorization:
    pending = _live_pending().get(state) if state else None
    if pending is None or pending.provider != provider:
        raise ValueError(
            "This sign-in request is unknown or has expired. Start the sign-in again."
        )
    if pending.user_id != credential_owner():
        raise ValueError(
            "This sign-in was started by a different user. Start the sign-in again."
        )
    return pending


def take_pending(state: str | None, provider: str) -> _PendingAuthorization:
    try:
        return peek_pending(state, provider)
    finally:
        if state:
            pending = _live_pending().get(state)
            if pending is not None and pending.provider == provider:
                del _live_pending()[state]


def trusted_opener_origin(
    requested: str | None, request: Request | None
) -> str:
    own = url_origin(get_public_base_url(request))
    if not requested or requested == own:
        return own
    local = urlparse(requested).hostname in _LOOPBACK_HOSTS
    if (
        local
        and not is_remote_deployment()
        and url_origin(requested) == requested
    ):
        return requested
    raise ValueError(f"Untrusted app origin: {requested}")


def build_oauth_authorize_url(
    provider: str,
    request: Request | None = None,
    opener_origin: str | None = None,
) -> str:
    from app.auth.provider_hooks import provider_hooks

    cfg = require_provider(provider)
    if not is_configured(cfg.name):
        raise ValueError(
            f"{cfg.display_name} isn't set up for this deployment. "
            "Run `make onboard` to register it."
        )
    origin = trusted_opener_origin(opener_origin, request)
    redirect_uri = get_redirect_uri(cfg.name, request)
    if url := provider_hooks(cfg.name).authorize_url(
        redirect_uri=redirect_uri, opener_origin=origin
    ):
        return url
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(cfg.scopes),
        **cfg.authorize_params,
    }
    verifier = None
    if cfg.pkce:
        verifier, challenge = _pkce_pair()
        params |= {"code_challenge_method": "S256", "code_challenge": challenge}
    params["state"] = start_pending(
        cfg.name, redirect_uri, origin, code_verifier=verifier
    )
    return f"{cfg.authorize_url}?{urlencode(params)}"


def oauth_opener_origin(state: str | None) -> str | None:
    pending = _live_pending().get(state) if state else None
    return pending.opener_origin if pending else None


async def request_token(
    cfg: AuthProvider,
    grant: dict[str, str],
    *,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    grant = {**grant, "client_id": client_id, "client_secret": client_secret}
    async with get_http_client(timeout=10.0) as client:
        resp = await client.post(cfg.token_url, data=grant, headers=headers)
    payload = resp.json()
    if resp.status_code >= 400 or "access_token" not in payload:
        error = payload.get("error")
        raise TokenEndpointError(
            payload.get("error_description")
            or (error if isinstance(error, str) else None)
            or f"{cfg.display_name} token request failed ({resp.status_code})",
            status_code=resp.status_code,
            error=error if isinstance(error, str) else None,
        )
    return payload


def _expires_in(payload: dict[str, Any]) -> int | None:
    return int(payload["expires_in"]) if payload.get("expires_in") else None


def granted_scopes_update(
    cfg: AuthProvider, payload: dict[str, Any]
) -> dict[str, str | None]:
    scope = str(payload.get("scope") or "").strip()
    if not (cfg.granted_scopes_env and scope):
        return {}
    return {cfg.granted_scopes_env: " ".join(scope.split())}


async def exchange_oauth_code(
    provider: str, code: str, state: str | None
) -> dict[str, Any]:
    """Exchange `code` and save tokens for the user who started `state`."""
    cfg = require_provider(provider)
    pending = take_pending(state, cfg.name)
    grant = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
    }
    if pending.code_verifier:
        grant["code_verifier"] = pending.code_verifier
    payload = await request_token(
        cfg, grant, client_id=cfg.client_id, client_secret=cfg.client_secret
    )
    extra_env: dict[str, str | None] = {}
    if cfg.refresh_token_env and payload.get("refresh_token"):
        extra_env[cfg.refresh_token_env] = payload["refresh_token"]
    if cfg.token_source_env:
        extra_env[cfg.token_source_env] = TOKEN_SOURCE_OAUTH
    extra_env |= granted_scopes_update(cfg, payload)
    return await verify_and_save_token(
        cfg.name,
        payload["access_token"],
        expires_in=_expires_in(payload),
        extra_env=extra_env or None,
    )


def _needs_refresh(cfg: AuthProvider) -> bool:
    state = provider_state(cfg.name)
    return (
        not cfg.access_token
        or state is None
        or state.expires_at is None
        or time.time() >= state.expires_at - REFRESH_MARGIN_S
    )


async def _refresh_grant(
    cfg: AuthProvider, refresh_token: str
) -> dict[str, Any]:
    return await request_token(
        cfg,
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
    )


async def _refresh_with_retry(cfg: AuthProvider) -> dict[str, Any]:
    used = cfg.refresh_token
    try:
        return await _refresh_grant(cfg, used)
    except TokenEndpointError as exc:
        if not exc.grant_rejected:
            raise
        # Reload in case another instance rotated the refresh token within our cache TTL.
        await reload_user_credentials()
        latest = cfg.refresh_token
        if not latest or latest == used:
            raise
        logger.info("Retrying %s refresh with a newer refresh token", cfg.name)
        return await _refresh_grant(cfg, latest)


async def _refresh(cfg: AuthProvider) -> str:
    payload = await _refresh_with_retry(cfg)
    token = payload["access_token"]
    updates: dict[str, str | None] = {cfg.access_token_env: token}
    if cfg.refresh_token_env and payload.get("refresh_token"):
        updates[cfg.refresh_token_env] = payload["refresh_token"]
    updates |= granted_scopes_update(cfg, payload)
    await asyncio.to_thread(save_user_credentials, updates)
    previous = provider_state(cfg.name)
    record_provider_state(
        cfg.name,
        previous.account_label if previous else f"{cfg.display_name} Connected",
        expires_in=_expires_in(payload) or 3600,
    )
    return token


async def ensure_fresh_access_token(
    provider: str, *, force: bool = False
) -> str | None:
    """Return a valid access token for the acting user, refreshing first if needed."""
    load_dotenv_defaults()
    cfg = get_provider(provider)
    if cfg is None:
        return None
    await prefetch_user_credentials()
    if (force or _needs_refresh(cfg)) and cfg.can_refresh:
        try:
            return await _refresh(cfg)
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Failed to auto-refresh %s token: %s", provider, exc)
    return cfg.access_token or None


async def revoke_provider_grant(name: str) -> bool:
    """Revoke a grant issued to this app's OAuth client (`TOKEN_SOURCE_OAUTH`); never raises."""
    cfg = get_provider(name)
    url = cfg.revoke_url if cfg else None
    if cfg is None or url is None:
        return False
    try:
        await prefetch_user_credentials()
        if cfg.token_source != TOKEN_SOURCE_OAUTH:
            return False
        token = cfg.refresh_token or cfg.access_token
        if not token:
            return False
        async with get_http_client(timeout=10.0) as client:
            resp = await client.post(
                url,
                data={"token": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception:
        logger.warning(
            "Could not revoke the %s grant on disconnect", name, exc_info=True
        )
        return False
    if resp.status_code >= 400:
        logger.warning(
            "Revoking the %s grant returned HTTP %s", name, resp.status_code
        )
        return False
    return True
