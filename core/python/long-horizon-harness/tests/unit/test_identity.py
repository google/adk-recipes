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

"""Unit tests for ``app.auth.identity``.

Covers the env-driven mode selector, the dev-user fallback, IAP JWT
verification (mocked), the request-scoped ContextVar populated by
``resolve_user_id``, and the middleware's bypass list.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.auth import identity as auth
from app.auth.identity import (
    AuthMode,
    DevAuthInProductionError,
    IdentityMiddleware,
    UnknownAuthModeError,
    _user_id_var,
    current_user_id,
    get_auth_mode,
    get_user_id_from_context,
    resolve_user_id,
)


@pytest.fixture(autouse=True)
def reset_contextvar():
    token = _user_id_var.set(None)
    try:
        yield
    finally:
        _user_id_var.reset(token)


def _make_request(
    headers: dict[str, str] | None = None, path: str = "/x"
) -> Request:
    """Build a minimal Starlette Request usable by ``resolve_user_id``."""
    raw_headers = [
        (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "headers": raw_headers,
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
    }
    return Request(scope)


def test_get_auth_mode_defaults_to_dev(monkeypatch):
    monkeypatch.delenv("LHA_AUTH_MODE", raising=False)
    assert get_auth_mode() is AuthMode.DEV


def test_get_auth_mode_reads_env(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    assert get_auth_mode() is AuthMode.IAP


def test_get_auth_mode_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "bogus")
    with pytest.raises(UnknownAuthModeError):
        get_auth_mode()


def test_dev_mode_on_cloud_run_raises(monkeypatch):
    monkeypatch.delenv("LHA_AUTH_MODE", raising=False)
    monkeypatch.setenv("K_SERVICE", "lha-backend")
    with pytest.raises(DevAuthInProductionError):
        get_auth_mode()


def test_iap_mode_on_cloud_run_ok(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.setenv("K_SERVICE", "lha-backend")
    assert get_auth_mode() is AuthMode.IAP


def test_dev_mode_without_cloud_run_ok(monkeypatch):
    monkeypatch.delenv("LHA_AUTH_MODE", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert get_auth_mode() is AuthMode.DEV


def test_resolve_user_id_dev_on_cloud_run_returns_500(monkeypatch):
    monkeypatch.delenv("LHA_AUTH_MODE", raising=False)
    monkeypatch.setenv("K_SERVICE", "lha-backend")
    req = _make_request()
    with pytest.raises(Exception) as exc_info:
        resolve_user_id(req)
    assert getattr(exc_info.value, "status_code", None) == 500


def test_dev_mode_returns_configured_user_id(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "dev")
    monkeypatch.setenv("LHA_DEV_USER_ID", "sam@local")
    req = _make_request()
    assert resolve_user_id(req) == "sam@local"
    assert req.state.user_id == "sam@local"
    assert get_user_id_from_context() == "sam@local"


def test_dev_mode_defaults_user_when_unset(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "dev")
    monkeypatch.delenv("LHA_DEV_USER_ID", raising=False)
    assert resolve_user_id(_make_request()) == "dev@local"


def test_iap_mode_missing_jwt_returns_401(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.setenv("LHA_IAP_AUDIENCE", "aud")
    with pytest.raises(Exception) as exc:
        resolve_user_id(_make_request())
    assert getattr(exc.value, "status_code", None) == 401


def test_iap_mode_extracts_email_from_verified_jwt(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.setenv("LHA_IAP_AUDIENCE", "aud")

    captured: dict[str, Any] = {}

    def fake_verify(token, request, audience=None, certs_url=None):
        captured.update(token=token, audience=audience, certs_url=certs_url)
        return {"email": "alice@example.com", "sub": "123"}

    monkeypatch.setattr(auth.id_token, "verify_token", fake_verify)

    req = _make_request(headers={"X-Goog-IAP-JWT-Assertion": "signed-jwt-here"})
    assert resolve_user_id(req) == "alice@example.com"
    assert captured["token"] == "signed-jwt-here"
    assert captured["audience"] == "aud"
    assert captured["certs_url"] == auth._IAP_PUBLIC_KEYS_URL


def test_iap_mode_accepts_forwarded_iap_jwt_header(monkeypatch):
    # The web proxy forwards the IAP JWT under X-LHA-IAP-Assertion because Google
    # strips the native X-Goog-* header inbound to a non-IAP backend.
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.setenv("LHA_IAP_AUDIENCE", "aud")

    captured: dict[str, Any] = {}

    def fake_verify(token, request, audience=None, certs_url=None):
        captured.update(token=token)
        return {"email": "web@example.com"}

    monkeypatch.setattr(auth.id_token, "verify_token", fake_verify)

    req = _make_request(headers={"X-LHA-IAP-Assertion": "fwd-jwt"})
    assert resolve_user_id(req) == "web@example.com"
    assert captured["token"] == "fwd-jwt"


def test_iap_mode_rejects_invalid_jwt(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.setenv("LHA_IAP_AUDIENCE", "aud")

    def boom(*a, **k):
        raise ValueError("bad signature")

    monkeypatch.setattr(auth.id_token, "verify_token", boom)
    req = _make_request(headers={"X-Goog-IAP-JWT-Assertion": "garbage"})
    with pytest.raises(Exception) as exc:
        resolve_user_id(req)
    assert getattr(exc.value, "status_code", None) == 401


def test_iap_mode_rejects_jwt_missing_email_claim(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.setenv("LHA_IAP_AUDIENCE", "aud")
    monkeypatch.setattr(
        auth.id_token, "verify_token", lambda *a, **k: {"sub": "no-email"}
    )
    req = _make_request(headers={"X-Goog-IAP-JWT-Assertion": "signed"})
    with pytest.raises(Exception) as exc:
        resolve_user_id(req)
    assert getattr(exc.value, "status_code", None) == 401


def test_iap_mode_requires_audience_env(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.delenv("LHA_IAP_AUDIENCE", raising=False)
    req = _make_request(headers={"X-Goog-IAP-JWT-Assertion": "x"})
    with pytest.raises(Exception) as exc:
        resolve_user_id(req)
    assert getattr(exc.value, "status_code", None) == 500


def test_trusted_header_mode_reads_user_id_from_header(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "trusted_header")
    req = _make_request(headers={"X-LHA-User-Id": "alice@example.com"})
    assert resolve_user_id(req) == "alice@example.com"


def test_trusted_header_mode_missing_header_returns_401(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "trusted_header")
    with pytest.raises(Exception) as exc:
        resolve_user_id(_make_request())
    assert getattr(exc.value, "status_code", None) == 401


def test_current_user_id_is_idempotent(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "dev")
    monkeypatch.setenv("LHA_DEV_USER_ID", "u@local")
    req = _make_request()
    a = current_user_id(req)
    b = current_user_id(req)
    assert a == b == "u@local"


def _app_with_middleware(record: dict[str, Any]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(IdentityMiddleware)

    @app.get("/protected")
    def protected(request: Request):
        record["user_id"] = current_user_id(request)
        return {"ok": True}

    @app.get("/.well-known/agent-card.json")
    def well_known(request: Request):
        record["public_called"] = True
        record["had_user_id"] = bool(getattr(request.state, "user_id", None))
        return {"ok": True}

    @app.post("/scheduler/tick")
    def scheduler_tick(request: Request):
        record["scheduler_called"] = True
        record["scheduler_had_user_id"] = bool(
            getattr(request.state, "user_id", None)
        )
        return {"ok": True}

    return app


def test_middleware_resolves_dev_user_for_protected_route(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "dev")
    monkeypatch.setenv("LHA_DEV_USER_ID", "dev@local")
    record: dict[str, Any] = {}
    client = TestClient(_app_with_middleware(record))
    resp = client.get("/protected")
    assert resp.status_code == 200
    assert record["user_id"] == "dev@local"


def test_middleware_returns_401_in_iap_mode_without_jwt(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.setenv("LHA_IAP_AUDIENCE", "aud")
    client = TestClient(_app_with_middleware({}))
    resp = client.get("/protected")
    assert resp.status_code == 401


def test_middleware_bypasses_public_paths_in_iap_mode(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.setenv("LHA_IAP_AUDIENCE", "aud")
    record: dict[str, Any] = {}
    client = TestClient(_app_with_middleware(record))
    resp = client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    assert record["public_called"] is True
    assert record["had_user_id"] is False


def test_middleware_bypasses_scheduler_paths_in_trusted_header_mode(
    monkeypatch,
):
    monkeypatch.setenv("LHA_AUTH_MODE", "trusted_header")
    record: dict[str, Any] = {}
    client = TestClient(_app_with_middleware(record))
    # No X-LHA-User-Id header — scheduler routes must still pass through.
    resp = client.post("/scheduler/tick")
    assert resp.status_code == 200
    assert record["scheduler_called"] is True
    assert record["scheduler_had_user_id"] is False


def test_middleware_allows_options_preflight(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.setenv("LHA_IAP_AUDIENCE", "aud")
    client = TestClient(_app_with_middleware({}))
    # OPTIONS without any headers must not 401 — CORS handles its own model.
    resp = client.options("/protected")
    assert resp.status_code != 401


def test_iap_mode_accepts_oauth_bearer_when_no_iap_jwt(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.setenv("LHA_IAP_AUDIENCE", "aud")
    monkeypatch.setenv("LHA_GCP_OAUTH_CLIENT_ID", "client-123")

    from app.auth import oauth_verify
    from app.secrets import inject

    monkeypatch.setattr(
        oauth_verify,
        "verify_google_access_token",
        lambda token, *, expected_aud: "ge-user@example.com",
    )
    captured = {}
    monkeypatch.setattr(
        inject, "set_delegated_google_token", lambda t: captured.update(tok=t)
    )

    req = _make_request(
        headers={"Authorization": "Bearer ya29.ge"}, path="/a2a"
    )
    assert resolve_user_id(req) == "ge-user@example.com"
    assert captured["tok"] == "ya29.ge"


def test_iap_mode_prefers_iap_jwt_over_bearer(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.setenv("LHA_IAP_AUDIENCE", "aud")
    monkeypatch.setenv("LHA_GCP_OAUTH_CLIENT_ID", "client-123")
    monkeypatch.setattr(
        auth.id_token,
        "verify_token",
        lambda *a, **k: {"email": "web@example.com"},
    )
    req = _make_request(
        headers={
            "X-Goog-IAP-JWT-Assertion": "signed",
            "Authorization": "Bearer ya29.ge",
        },
        path="/a2a",
    )
    assert resolve_user_id(req) == "web@example.com"


def test_iap_mode_rejects_invalid_oauth_bearer(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.setenv("LHA_IAP_AUDIENCE", "aud")
    monkeypatch.setenv("LHA_GCP_OAUTH_CLIENT_ID", "client-123")

    from app.auth import oauth_verify

    def boom(token, *, expected_aud):
        raise oauth_verify.OAuthVerifyError("bad aud")

    monkeypatch.setattr(oauth_verify, "verify_google_access_token", boom)
    req = _make_request(headers={"Authorization": "Bearer nope"}, path="/a2a")
    with pytest.raises(Exception) as exc:
        resolve_user_id(req)
    assert getattr(exc.value, "status_code", None) == 401


def test_iap_mode_missing_both_credentials_returns_401(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.setenv("LHA_IAP_AUDIENCE", "aud")
    monkeypatch.setenv("LHA_GCP_OAUTH_CLIENT_ID", "client-123")
    with pytest.raises(Exception) as exc:
        resolve_user_id(_make_request(path="/a2a"))
    assert getattr(exc.value, "status_code", None) == 401


def test_oauth_bearer_without_client_id_configured_returns_500(monkeypatch):
    monkeypatch.setenv("LHA_AUTH_MODE", "iap")
    monkeypatch.setenv("LHA_IAP_AUDIENCE", "aud")
    monkeypatch.delenv("LHA_GCP_OAUTH_CLIENT_ID", raising=False)
    req = _make_request(
        headers={"Authorization": "Bearer ya29.ge"}, path="/a2a"
    )
    with pytest.raises(Exception) as exc:
        resolve_user_id(req)
    assert getattr(exc.value, "status_code", None) == 500
