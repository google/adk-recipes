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

"""Connecting apps: the OAuth 2.0 sign-in (Connect, Grant access) and Disconnect."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.auth import (
    build_oauth_authorize_url,
    disconnect_provider,
    exchange_oauth_code,
    oauth_opener_origin,
    revoke_provider_grant,
)
from app.auth.oauth_popup import render_oauth_popup_html
from app.auth.provider_hooks import provider_hooks
from app.auth.providers import get_redirect_uri, url_origin

router = APIRouter(prefix="/auth")


@router.post("/{provider}/authorize")
async def api_prepare_oauth_authorize(
    provider: str, request: Request
) -> dict[str, Any]:
    prov = provider.strip().lower()
    try:
        url = build_oauth_authorize_url(
            prov,
            request=request,
            opener_origin=request.headers.get("origin"),
        )
        # Backend origin differs under Vite, and GitHub App install URLs omit redirect_uri.
        callback_origin = url_origin(get_redirect_uri(prov, request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "provider": prov,
        "authorize_url": url,
        "callback_origin": callback_origin,
    }


@router.get("/{provider}/callback")
async def api_oauth_callback(
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    setup_action: str | None = None,
) -> HTMLResponse:
    prov = provider.strip().lower()
    opener_origin = oauth_opener_origin(state)
    hooks = provider_hooks(prov)
    if opener_origin is None and (
        handled := await hooks.on_callback_without_sign_in(setup_action)
    ):
        title, subtitle = handled
        return render_oauth_popup_html(
            provider=prov,
            ok=True,
            title=title,
            subtitle=subtitle,
            opener_origin=None,
        )
    if error or not code:
        return render_oauth_popup_html(
            provider=prov,
            ok=False,
            title="Authorization Cancelled",
            subtitle=error
            or "No authorization code was returned by the provider.",
            opener_origin=opener_origin,
        )
    try:
        result = await exchange_oauth_code(prov, code=code, state=state)
    except Exception as exc:
        return render_oauth_popup_html(
            provider=prov,
            ok=False,
            title="Authentication Failed",
            subtitle=str(exc),
            opener_origin=opener_origin,
        )
    await hooks.on_connected()
    account = result.get("account_label") or prov.title()
    return render_oauth_popup_html(
        provider=prov,
        ok=True,
        title=f"Connected to {account}",
        subtitle="Credentials and refresh tokens saved. This window will close automatically.",
        opener_origin=opener_origin,
        account_label=account,
    )


@router.post("/{provider}/disconnect")
async def api_disconnect_provider(provider: str) -> dict[str, Any]:
    prov = provider.strip().lower()
    await revoke_provider_grant(prov)
    await asyncio.to_thread(disconnect_provider, prov)
    return {"ok": True, "provider": prov}
