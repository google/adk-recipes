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

"""Connected apps from `config/integrations.yaml`, their connection state, and OAuth URLs."""

from __future__ import annotations

import functools
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from fastapi import Request

from app.auth.credential_store import (
    credential_owner,
    save_user_credentials,
    user_credential,
)
from app.auth.env_store import PROJECT_ROOT, load_dotenv_defaults

INTEGRATIONS_CONFIG_PATH = PROJECT_ROOT / "config" / "integrations.yaml"
LOCAL_BASE_URL = "http://127.0.0.1:8000"
AUTH_API_PREFIX = "/api/v1/auth"
WORKSPACE_PROVIDER = "google_workspace"


@dataclass
class AuthProvider:
    name: str
    display_name: str
    access_token_env: str
    client_id_env: str
    client_secret_env: str
    authorize_url: str
    token_url: str
    icon: str = ""
    scopes: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    mcp_servers: dict[str, list[str]] = field(default_factory=dict)
    apis: list[str] = field(default_factory=list)
    refresh_token_env: str | None = None
    pkce: bool = False
    authorize_params: dict[str, str] = field(default_factory=dict)
    token_source_env: str | None = None
    granted_scopes_env: str | None = None
    account_label_env: str | None = None
    revoke_url: str | None = None

    def __post_init__(self) -> None:
        self.scopes = list(
            dict.fromkeys(
                [
                    *self.scopes,
                    *(s for v in self.mcp_servers.values() for s in v),
                ]
            )
        )

    @property
    def access_token(self) -> str:
        return user_credential(self.access_token_env)

    @property
    def client_id(self) -> str:
        return _env(self.client_id_env)

    @property
    def client_secret(self) -> str:
        return _env(self.client_secret_env)

    @property
    def refresh_token(self) -> str:
        return user_credential(self.refresh_token_env)

    @property
    def account_label(self) -> str:
        return user_credential(self.account_label_env)

    @property
    def user_env_keys(self) -> list[str]:
        keys = [
            self.access_token_env,
            self.refresh_token_env,
            self.token_source_env,
            self.granted_scopes_env,
            self.account_label_env,
        ]
        return list(dict.fromkeys(k for k in keys if k))

    @property
    def missing_scopes(self) -> list[str]:
        return scopes_missing_from(
            self.scopes, user_credential(self.granted_scopes_env)
        )

    @property
    def has_client_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def token_source(self) -> str:
        return user_credential(self.token_source_env)

    @property
    def can_refresh(self) -> bool:
        return bool(self.refresh_token and self.has_client_credentials)


TOKEN_SOURCE_OAUTH = "oauth"  # noqa: S105 - a source label, not a secret
GRANT_ACCESS_HINT = "Grant access in Settings → Apps."


def scopes_missing_from(wanted: list[str], granted: str) -> list[str]:
    """Return scopes in `wanted` absent from `granted` (empty `granted` means unknown, so none missing)."""
    have = set(granted.split())
    if not have:
        return []
    return [s for s in wanted if s not in have]


def _env(key: str | None) -> str:
    return os.getenv(key, "").strip() if key else ""


@functools.cache
def load_integrations_config(
    path: Path = INTEGRATIONS_CONFIG_PATH,
) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) or {}


@functools.cache
def all_providers() -> dict[str, AuthProvider]:
    raw = load_integrations_config()
    return {
        name: AuthProvider(name=name, **cfg)
        for name, cfg in (raw.get("apps") or {}).items()
    }


def get_provider(name: str) -> AuthProvider | None:
    return all_providers().get(name)


def require_provider(name: str) -> AuthProvider:
    provider = get_provider(name)
    if provider is None:
        raise ValueError(f"Unsupported provider: {name}")
    return provider


def is_configured(name: str) -> bool:
    from app.auth.provider_hooks import provider_hooks

    provider = get_provider(name)
    return bool(
        provider
        and provider.has_client_credentials
        and provider_hooks(name).configured()
    )


def configured_apps() -> list[AuthProvider]:
    return [p for p in all_providers().values() if is_configured(p.name)]


def app_for_server(server: str) -> AuthProvider | None:
    return next(
        (p for p in all_providers().values() if server in p.mcp_servers), None
    )


def server_only_env_keys() -> frozenset[str]:
    """Env keys stripped from local harness subprocesses so OAuth/CLI tokens stay on the server."""
    keys: set[str] = {"GH_TOKEN"}
    for p in all_providers().values():
        keys.update(
            k
            for k in (*p.user_env_keys, p.client_id_env, p.client_secret_env)
            if k
        )
    return frozenset(keys)


def user_credential_env_keys() -> frozenset[str]:
    return frozenset(
        k for p in all_providers().values() for k in p.user_env_keys
    )


def bearer_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass
class ProviderAuthState:
    account_label: str
    expires_at: float | None = None
    verified_at: float = field(default_factory=time.time)


_states: dict[tuple[str, str], ProviderAuthState] = {}


def record_provider_state(
    provider: str, account_label: str, *, expires_in: float | None = None
) -> ProviderAuthState:
    now = time.time()
    state = ProviderAuthState(
        account_label=account_label,
        expires_at=now + expires_in if expires_in else None,
        verified_at=now,
    )
    _states[(credential_owner(), provider)] = state
    return state


def provider_state(provider: str) -> ProviderAuthState | None:
    return _states.get((credential_owner(), provider))


def has_provider_credentials(name: str) -> bool:
    load_dotenv_defaults()
    provider = get_provider(name)
    return bool(provider and (provider.access_token or provider.can_refresh))


def disconnect_provider(name: str) -> None:
    provider = get_provider(name)
    if provider is None:
        return
    save_user_credentials(dict.fromkeys(provider.user_env_keys))
    _states.pop((credential_owner(), name), None)


def is_remote_deployment() -> bool:
    if os.getenv("K_SERVICE") or os.getenv("CLOUD_RUN_JOB"):
        return True
    app_url = os.getenv("APP_URL", "")
    return bool(
        app_url and "localhost" not in app_url and "127.0.0.1" not in app_url
    )


def auth_path(*parts: str) -> str:
    return "/".join((AUTH_API_PREFIX, *parts))


def url_origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def get_public_base_url(request: Request | None = None) -> str:
    """Return `APP_URL`, or `request`'s host locally; never trust proxy headers on remote deployments."""
    app_url = (os.getenv("APP_URL") or "").strip().rstrip("/")
    if app_url:
        return app_url
    if is_remote_deployment():
        raise ValueError(
            "APP_URL is not set. Set it to this service's public URL "
            "(for example https://adk-voice-agent-xxxx.a.run.app) to enable OAuth sign-in."
        )
    if request is None:
        return LOCAL_BASE_URL
    host = request.headers.get("host") or request.url.netloc
    return f"{request.url.scheme}://{host}" if host else LOCAL_BASE_URL


def get_redirect_uri(
    provider: str,
    request: Request | None = None,
    *,
    base_url: str | None = None,
) -> str:
    override = os.getenv(f"{provider.upper()}_REDIRECT_URI")
    if override:
        return override.strip()
    base = (base_url or "").strip().rstrip("/") or get_public_base_url(request)
    return f"{base}{auth_path(provider, 'callback')}"
