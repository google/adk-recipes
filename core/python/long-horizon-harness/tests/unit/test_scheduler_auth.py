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

"""``verify_cloud_scheduler_token`` — FastAPI dep gating ``/scheduler/*``.

Surface validated:

* Missing/malformed ``Authorization`` header → 401
* Token fails ``verify_oauth2_token`` → 401
* Token's ``email`` not in ``LHA_SCHEDULER_SA`` allowlist → 403
* Happy path: verified token + allowlisted email → returns None (allow)
* Dev bypass: ``LHA_SCHEDULER_AUTH_DISABLED=1`` skips all checks
* Default-deny: if ``LHA_SCHEDULER_AUDIENCE`` is unset and auth is
  enabled, we refuse to verify (fail-closed)
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.asyncio


def _request_with_header(header_value: str | None) -> Any:
    headers = {"authorization": header_value} if header_value else {}
    return SimpleNamespace(headers=headers)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "LHA_SCHEDULER_AUTH_DISABLED",
        "LHA_SCHEDULER_AUDIENCE",
        "LHA_SCHEDULER_SA",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


# =============================================================================
# Dev bypass
# =============================================================================


async def test_dev_bypass_allows_with_no_header(monkeypatch):
    from app.scheduler.auth import verify_cloud_scheduler_token

    monkeypatch.setenv("LHA_SCHEDULER_AUTH_DISABLED", "1")
    # Should not raise even with a missing Authorization header
    await verify_cloud_scheduler_token(_request_with_header(None))


async def test_dev_bypass_recognizes_truthy_values(monkeypatch):
    from app.scheduler.auth import verify_cloud_scheduler_token

    for value in ("1", "true", "TRUE", "yes"):
        monkeypatch.setenv("LHA_SCHEDULER_AUTH_DISABLED", value)
        await verify_cloud_scheduler_token(_request_with_header(None))


async def test_dev_bypass_logs_warning(monkeypatch, caplog):
    import logging

    from app.scheduler import auth

    monkeypatch.setenv("LHA_SCHEDULER_AUTH_DISABLED", "1")
    auth._reset_bypass_warning_for_tests()
    with caplog.at_level(logging.WARNING, logger=auth.__name__):
        await auth.verify_cloud_scheduler_token(_request_with_header(None))
    assert any(
        "LHA_SCHEDULER_AUTH_DISABLED" in record.message
        for record in caplog.records
    )


async def test_dev_bypass_warning_logged_once(monkeypatch, caplog):
    import logging

    from app.scheduler import auth

    monkeypatch.setenv("LHA_SCHEDULER_AUTH_DISABLED", "1")
    auth._reset_bypass_warning_for_tests()
    with caplog.at_level(logging.WARNING, logger=auth.__name__):
        await auth.verify_cloud_scheduler_token(_request_with_header(None))
        await auth.verify_cloud_scheduler_token(_request_with_header(None))
        await auth.verify_cloud_scheduler_token(_request_with_header(None))
    bypass_warnings = [
        r for r in caplog.records if "LHA_SCHEDULER_AUTH_DISABLED" in r.message
    ]
    assert len(bypass_warnings) == 1


# =============================================================================
# Default-deny when audience misconfigured
# =============================================================================


async def test_missing_audience_fails_closed(monkeypatch):
    from app.scheduler.auth import verify_cloud_scheduler_token

    monkeypatch.delenv("LHA_SCHEDULER_AUDIENCE", raising=False)
    with pytest.raises(HTTPException) as exc_info:
        await verify_cloud_scheduler_token(
            _request_with_header("Bearer some-token")
        )
    assert exc_info.value.status_code == 500
    assert "audience" in exc_info.value.detail.lower()


# =============================================================================
# Missing / malformed Authorization header
# =============================================================================


class TestHeaderValidation:
    async def test_missing_header_returns_401(self, monkeypatch):
        from app.scheduler.auth import verify_cloud_scheduler_token

        monkeypatch.setenv("LHA_SCHEDULER_AUDIENCE", "https://svc/")
        with pytest.raises(HTTPException) as exc_info:
            await verify_cloud_scheduler_token(_request_with_header(None))
        assert exc_info.value.status_code == 401

    async def test_non_bearer_scheme_returns_401(self, monkeypatch):
        from app.scheduler.auth import verify_cloud_scheduler_token

        monkeypatch.setenv("LHA_SCHEDULER_AUDIENCE", "https://svc/")
        with pytest.raises(HTTPException) as exc_info:
            await verify_cloud_scheduler_token(
                _request_with_header("Basic dXNlcjpwYXNz")
            )
        assert exc_info.value.status_code == 401

    async def test_empty_bearer_returns_401(self, monkeypatch):
        from app.scheduler.auth import verify_cloud_scheduler_token

        monkeypatch.setenv("LHA_SCHEDULER_AUDIENCE", "https://svc/")
        with pytest.raises(HTTPException) as exc_info:
            await verify_cloud_scheduler_token(_request_with_header("Bearer "))
        assert exc_info.value.status_code == 401


# =============================================================================
# Token verification
# =============================================================================


class TestTokenVerification:
    async def test_invalid_token_returns_401(self, monkeypatch):
        from app.scheduler import auth

        def _raise(*_a, **_kw):
            raise ValueError("token signature invalid")

        monkeypatch.setenv("LHA_SCHEDULER_AUDIENCE", "https://svc/")
        monkeypatch.setattr(auth, "_verify_oauth2_token", _raise)
        with pytest.raises(HTTPException) as exc_info:
            await auth.verify_cloud_scheduler_token(
                _request_with_header("Bearer bad-token")
            )
        assert exc_info.value.status_code == 401

    async def test_valid_token_no_allowlist_allows(self, monkeypatch):
        from app.scheduler import auth

        monkeypatch.setenv("LHA_SCHEDULER_AUDIENCE", "https://svc/")
        monkeypatch.setattr(
            auth,
            "_verify_oauth2_token",
            lambda token, audience: {"email": "anyone@example.com"},
        )
        # No allowlist → any verified caller allowed
        await auth.verify_cloud_scheduler_token(
            _request_with_header("Bearer good-token")
        )

    async def test_audience_passed_to_verifier(self, monkeypatch):
        from app.scheduler import auth

        captured: dict[str, Any] = {}

        def _capture(token, audience):
            captured["audience"] = audience
            captured["token"] = token
            return {"email": "sa@example.iam.gserviceaccount.com"}

        monkeypatch.setenv("LHA_SCHEDULER_AUDIENCE", "https://my-svc/")
        monkeypatch.setattr(auth, "_verify_oauth2_token", _capture)
        await auth.verify_cloud_scheduler_token(
            _request_with_header("Bearer the-token")
        )
        assert captured["audience"] == "https://my-svc/"
        assert captured["token"] == "the-token"


# =============================================================================
# Allowlist
# =============================================================================


class TestAllowlist:
    async def test_email_on_allowlist_allows(self, monkeypatch):
        from app.scheduler import auth

        monkeypatch.setenv("LHA_SCHEDULER_AUDIENCE", "https://svc/")
        monkeypatch.setenv(
            "LHA_SCHEDULER_SA", "scheduler@p.iam.gserviceaccount.com"
        )
        monkeypatch.setattr(
            auth,
            "_verify_oauth2_token",
            lambda t, a: {"email": "scheduler@p.iam.gserviceaccount.com"},
        )
        await auth.verify_cloud_scheduler_token(
            _request_with_header("Bearer t")
        )

    async def test_email_not_on_allowlist_returns_403(self, monkeypatch):
        from app.scheduler import auth

        monkeypatch.setenv("LHA_SCHEDULER_AUDIENCE", "https://svc/")
        monkeypatch.setenv(
            "LHA_SCHEDULER_SA", "scheduler@p.iam.gserviceaccount.com"
        )
        monkeypatch.setattr(
            auth,
            "_verify_oauth2_token",
            lambda t, a: {"email": "attacker@evil.com"},
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.verify_cloud_scheduler_token(
                _request_with_header("Bearer t")
            )
        assert exc_info.value.status_code == 403

    async def test_email_case_insensitive_match(self, monkeypatch):
        from app.scheduler import auth

        monkeypatch.setenv("LHA_SCHEDULER_AUDIENCE", "https://svc/")
        monkeypatch.setenv(
            "LHA_SCHEDULER_SA", "Scheduler@P.IAM.gserviceaccount.com"
        )
        monkeypatch.setattr(
            auth,
            "_verify_oauth2_token",
            lambda t, a: {"email": "scheduler@p.iam.gserviceaccount.com"},
        )
        await auth.verify_cloud_scheduler_token(
            _request_with_header("Bearer t")
        )

    async def test_allowlist_supports_multiple_emails(self, monkeypatch):
        from app.scheduler import auth

        monkeypatch.setenv("LHA_SCHEDULER_AUDIENCE", "https://svc/")
        monkeypatch.setenv(
            "LHA_SCHEDULER_SA",
            "a@p.iam.gserviceaccount.com,b@p.iam.gserviceaccount.com",
        )
        monkeypatch.setattr(
            auth,
            "_verify_oauth2_token",
            lambda t, a: {"email": "b@p.iam.gserviceaccount.com"},
        )
        await auth.verify_cloud_scheduler_token(
            _request_with_header("Bearer t")
        )

    async def test_missing_email_claim_returns_403(self, monkeypatch):
        from app.scheduler import auth

        monkeypatch.setenv("LHA_SCHEDULER_AUDIENCE", "https://svc/")
        monkeypatch.setenv(
            "LHA_SCHEDULER_SA", "scheduler@p.iam.gserviceaccount.com"
        )
        monkeypatch.setattr(
            auth, "_verify_oauth2_token", lambda t, a: {"sub": "123"}
        )
        with pytest.raises(HTTPException) as exc_info:
            await auth.verify_cloud_scheduler_token(
                _request_with_header("Bearer t")
            )
        assert exc_info.value.status_code == 403
