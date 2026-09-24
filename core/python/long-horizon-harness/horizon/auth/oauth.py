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

"""/lha/gcp — user-initiated "Connect Google" OAuth, split into two independent
connections:

* **GCP** (`target=gcp`) → `cloud-platform` scope → `CLOUDSDK_AUTH_ACCESS_TOKEN`
  (used by `gcloud`/`bq`).
* **Workspace** (`target=workspace`) → a configurable, least-privilege set of
  Workspace scopes (per surface: drive/gmail/calendar/sheets/docs/chat/tasks/
  slides/keep/script/meet/forms, read-only by default) →
  `GOOGLE_WORKSPACE_CLI_TOKEN` (gws's highest-priority auth source).

Each is its own consent + token, stored as ordinary per-user secrets, so the
existing `secret_env()` injection carries them into the sandbox — no terminal
changes. No refresh token is requested (access-token only, ~1h; re-click to renew).

Why not ADK's native auth? ADK delivers credentials to *in-process tools*, not a
shell command in a separate sandbox; this thin layer fills that gap. If refresh is
ever wanted, route it through ADK's `OAuth2CredentialRefresher`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets as _secrets
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from horizon.auth import current_user_id
from horizon.secrets import get_secret_store

logger = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105

_SCOPE_PREFIX = "https://www.googleapis.com/auth/"
CLOUD_PLATFORM_SCOPE = _SCOPE_PREFIX + "cloud-platform"

# Workspace surfaces → read-only ("ro") / read-write ("rw") scope names. The
# connect request picks surfaces + a readonly flag; the scope list is assembled
# from this map. Chat carries two scopes; the rest one.
WORKSPACE_SURFACES: dict[str, dict[str, tuple[str, ...]]] = {
    "drive": {"ro": ("drive.readonly",), "rw": ("drive",)},
    "gmail": {"ro": ("gmail.readonly",), "rw": ("gmail.modify",)},
    "calendar": {"ro": ("calendar.readonly",), "rw": ("calendar",)},
    "sheets": {"ro": ("spreadsheets.readonly",), "rw": ("spreadsheets",)},
    "docs": {"ro": ("documents.readonly",), "rw": ("documents",)},
    "chat": {
        "ro": ("chat.messages.readonly", "chat.spaces.readonly"),
        "rw": ("chat.messages", "chat.spaces"),
    },
    "tasks": {"ro": ("tasks.readonly",), "rw": ("tasks",)},
    "slides": {"ro": ("presentations.readonly",), "rw": ("presentations",)},
    "keep": {"ro": ("keep.readonly",), "rw": ("keep",)},
    "script": {"ro": ("script.projects.readonly",), "rw": ("script.projects",)},
    # Meet's write capability is the "create" scope — there is no plain
    # `meetings.space` write scope.
    "meet": {
        "ro": ("meetings.space.readonly",),
        "rw": ("meetings.space.created",),
    },
    # Reading form responses needs a scope orthogonal to body read/write, so it
    # rides along in both modes.
    "forms": {
        "ro": ("forms.body.readonly", "forms.responses.readonly"),
        "rw": ("forms.body", "forms.responses.readonly"),
    },
    # People/Directory read — resolves the user IDs that Chat/Gmail return into
    # display names. Inherently read-only, so both modes map to the same scope.
    "directory": {"ro": ("directory.readonly",), "rw": ("directory.readonly",)},
}

# Per-user secret names. Token keys are read by gcloud/gws from the sandbox env;
# the *_EXPIRES_AT / *_META keys are metadata for /status (harmless extra env).
GCP_TOKEN_KEY = "CLOUDSDK_AUTH_ACCESS_TOKEN"  # noqa: S105
GCP_EXPIRES_KEY = "CLOUDSDK_AUTH_TOKEN_EXPIRES_AT"
GWS_TOKEN_KEY = "GOOGLE_WORKSPACE_CLI_TOKEN"  # noqa: S105
GWS_EXPIRES_KEY = "GOOGLE_WORKSPACE_TOKEN_EXPIRES_AT"
GWS_META_KEY = "GOOGLE_WORKSPACE_SCOPES_META"  # json: {"surfaces": [...], "readonly": bool}

_GCP_KEYS = (GCP_TOKEN_KEY, GCP_EXPIRES_KEY)
_GWS_KEYS = (GWS_TOKEN_KEY, GWS_EXPIRES_KEY, GWS_META_KEY)


class OAuthError(Exception):
    """A token exchange returned no access token."""


# --- scopes --------------------------------------------------------------


def workspace_scopes(surfaces: list[str], *, readonly: bool) -> list[str]:
    """Assemble Workspace scope URLs for the chosen surfaces + access mode.

    Unknown surfaces are skipped. De-duplicates while preserving order.
    """
    mode = "ro" if readonly else "rw"
    out: list[str] = []
    for surface in surfaces:
        spec = WORKSPACE_SURFACES.get(surface)
        if not spec:
            continue
        for name in spec[mode]:
            url = _SCOPE_PREFIX + name
            if url not in out:
                out.append(url)
    return out


# --- signed state (CSRF) -------------------------------------------------


def _sig(body: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def sign_state(payload: dict[str, Any], *, secret: str) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{body}.{_sig(body, secret)}"


def verify_state(token: str, *, secret: str) -> dict[str, Any] | None:
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sig(body, secret)):
        return None
    try:
        pad = "=" * (-len(body) % 4)
        obj = json.loads(base64.urlsafe_b64decode(body + pad))
    except (ValueError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


# --- OAuth flow ----------------------------------------------------------


def build_auth_url(
    *, client_id: str, redirect_uri: str, scopes: list[str], state: str
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        # No include_granted_scopes: each connection's token carries ONLY its
        # requested scopes, so a read-only Workspace token can't inherit a prior
        # cloud-platform grant — the point of the GCP/Workspace split.
        # No access_type=offline: access token only, nothing durable.
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def _http_post(url: str, data: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(  # noqa: S310
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read())


def exchange_code(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    post: Callable[[str, dict[str, str]], dict[str, Any]] = _http_post,
) -> tuple[str, int]:
    """Return ``(access_token, expires_in_seconds)`` (expires_in 0 if omitted)."""
    resp = post(
        TOKEN_ENDPOINT,
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    token = resp.get("access_token")
    if not token:
        raise OAuthError(
            f"token exchange failed: {resp.get('error', 'no access_token')}"
        )
    return str(token), int(resp.get("expires_in") or 0)


# --- connection persistence (reuses the per-user secret store) -----------


async def persist_gcp(
    store: Any, user_id: str, *, token: str, expires_at: int
) -> None:
    await store.set_secret(user_id, GCP_TOKEN_KEY, token)
    if expires_at:
        await store.set_secret(user_id, GCP_EXPIRES_KEY, str(expires_at))


async def persist_workspace(
    store: Any,
    user_id: str,
    *,
    token: str,
    expires_at: int,
    surfaces: list[str],
    readonly: bool,
) -> None:
    await store.set_secret(user_id, GWS_TOKEN_KEY, token)
    if expires_at:
        await store.set_secret(user_id, GWS_EXPIRES_KEY, str(expires_at))
    await store.set_secret(
        user_id,
        GWS_META_KEY,
        json.dumps({"surfaces": surfaces, "readonly": readonly}),
    )


async def clear_connection(store: Any, user_id: str, target: str) -> None:
    keys = _GCP_KEYS if target == "gcp" else _GWS_KEYS
    for key in keys:
        await store.delete_secret(user_id, key)


# --- config --------------------------------------------------------------


def _client_id() -> str:
    return os.environ.get("LHA_GCP_OAUTH_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("LHA_GCP_OAUTH_CLIENT_SECRET", "").strip()


def _redirect_uri() -> str:
    return os.environ.get("LHA_GCP_OAUTH_REDIRECT_URI", "").strip()


def _state_secret() -> str:
    # The client secret doubles as the HMAC key (stable across requests). No
    # fallback: unset ⇒ feature off (connect/callback 503), never a guessable key.
    return _client_secret()


def _oauth_configured() -> bool:
    return bool(_client_id() and _client_secret() and _redirect_uri())


def _int_or_none(raw: str | None) -> int | None:
    return int(raw) if raw else None


_CONNECTED_HTML = (
    "<!doctype html><meta charset=utf-8><title>Connected</title>"
    "<body style='font-family:system-ui;padding:2rem'>"
    "<h3>Google connected ✓</h3><p>You can close this window and return to the chat.</p>"
    "<script>setTimeout(()=>window.close(),1500)</script></body>"
)


def attach_gcp_oauth_routes(app: FastAPI) -> None:
    router = APIRouter(prefix="/lha/gcp", tags=["lha"])

    @router.post("/connect")
    async def connect(
        target: str,
        readonly: bool = True,
        surfaces: str = "",
        user_id: str = Depends(current_user_id),
    ) -> dict[str, Any]:
        if not _oauth_configured():
            raise HTTPException(
                status_code=503,
                detail="GCP OAuth not configured (set LHA_GCP_OAUTH_CLIENT_ID / _SECRET / _REDIRECT_URI)",
            )
        surface_list = [s for s in surfaces.split(",") if s]
        if target == "gcp":
            scopes = [CLOUD_PLATFORM_SCOPE]
        elif target == "workspace":
            scopes = workspace_scopes(surface_list, readonly=readonly)
            if not scopes:
                raise HTTPException(
                    status_code=400,
                    detail="select at least one Workspace surface",
                )
        else:
            raise HTTPException(
                status_code=400, detail="target must be gcp or workspace"
            )
        state = sign_state(
            {
                "user_id": user_id,
                "target": target,
                "surfaces": surface_list,
                "readonly": readonly,
                "nonce": _secrets.token_urlsafe(12),
            },
            secret=_state_secret(),
        )
        return {
            "auth_url": build_auth_url(
                client_id=_client_id(),
                redirect_uri=_redirect_uri(),
                scopes=scopes,
                state=state,
            )
        }

    @router.get("/callback")
    async def callback(
        code: str = "",
        state: str = "",
        user_id: str = Depends(current_user_id),
    ) -> HTMLResponse:
        if not _oauth_configured():
            raise HTTPException(
                status_code=503, detail="GCP OAuth not configured"
            )
        data = verify_state(state, secret=_state_secret()) if state else None
        if not data:
            raise HTTPException(
                status_code=400, detail="invalid or missing state"
            )
        # Defense-in-depth (see horizon/auth/identity.py): the browser is already
        # IAP-authenticated, so the signed state's user_id must match the caller.
        if str(data.get("user_id")) != user_id:
            raise HTTPException(status_code=403, detail="state/user mismatch")
        if not code:
            raise HTTPException(status_code=400, detail="missing code")
        try:
            token, expires_in = exchange_code(
                code=code,
                client_id=_client_id(),
                client_secret=_client_secret(),
                redirect_uri=_redirect_uri(),
            )
        except OAuthError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        expires_at = int(time.time()) + expires_in if expires_in else 0
        store = get_secret_store()
        if data.get("target") == "workspace":
            await persist_workspace(
                store,
                user_id,
                token=token,
                expires_at=expires_at,
                surfaces=list(data.get("surfaces") or []),
                readonly=bool(data.get("readonly", True)),
            )
        else:
            await persist_gcp(
                store, user_id, token=token, expires_at=expires_at
            )
        return HTMLResponse(_CONNECTED_HTML)

    @router.get("/status")
    async def status(user_id: str = Depends(current_user_id)) -> dict[str, Any]:
        values = await get_secret_store().get_all(user_id)
        try:
            ws_meta = json.loads(values.get(GWS_META_KEY) or "{}")
        except (ValueError, json.JSONDecodeError):
            ws_meta = {}
        return {
            "gcp": {
                "connected": GCP_TOKEN_KEY in values,
                "expires_at": _int_or_none(values.get(GCP_EXPIRES_KEY)),
            },
            "workspace": {
                "connected": GWS_TOKEN_KEY in values,
                "expires_at": _int_or_none(values.get(GWS_EXPIRES_KEY)),
                "surfaces": ws_meta.get("surfaces", []),
                "readonly": bool(ws_meta.get("readonly", True)),
            },
            "surfaces": list(WORKSPACE_SURFACES.keys()),
        }

    @router.delete("/disconnect")
    async def disconnect(
        target: str, user_id: str = Depends(current_user_id)
    ) -> dict[str, Any]:
        if target not in ("gcp", "workspace"):
            raise HTTPException(
                status_code=400, detail="target must be gcp or workspace"
            )
        await clear_connection(get_secret_store(), user_id, target)
        return {"ok": True}

    app.include_router(router)
