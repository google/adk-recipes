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

"""Unit tests for app.auth.oauth_verify (tokeninfo-based verification)."""

from __future__ import annotations

import pytest

from app.auth import oauth_verify
from app.auth.oauth_verify import (
    OAuthVerifyError,
    verify_google_access_token,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    oauth_verify._cache.clear()
    yield
    oauth_verify._cache.clear()


def _info(**over):
    base = {
        "aud": "client-123",
        "email": "alice@example.com",
        "email_verified": "true",
        "exp": "2000000000",
        "scope": "openid email https://www.googleapis.com/auth/cloud-platform",
    }
    base.update(over)
    return base


def test_valid_token_returns_email():
    calls = {"n": 0}

    def get(url):
        calls["n"] += 1
        assert "access_token=tok" in url
        return _info()

    email = verify_google_access_token(
        "tok", expected_aud="client-123", http_get=get, now=lambda: 1.0
    )
    assert email == "alice@example.com"
    assert calls["n"] == 1


def test_cache_hit_skips_second_http_call():
    calls = {"n": 0}

    def get(url):
        calls["n"] += 1
        return _info()

    for _ in range(2):
        verify_google_access_token(
            "tok", expected_aud="client-123", http_get=get, now=lambda: 1.0
        )
    assert calls["n"] == 1


def test_audience_mismatch_rejected():
    with pytest.raises(OAuthVerifyError):
        verify_google_access_token(
            "tok",
            expected_aud="other",
            http_get=lambda u: _info(),
            now=lambda: 1.0,
        )


def test_unverified_email_rejected():
    with pytest.raises(OAuthVerifyError):
        verify_google_access_token(
            "tok",
            expected_aud="client-123",
            http_get=lambda u: _info(email_verified="false"),
            now=lambda: 1.0,
        )


def test_missing_email_rejected():
    with pytest.raises(OAuthVerifyError):
        verify_google_access_token(
            "tok",
            expected_aud="client-123",
            http_get=lambda u: _info(email=""),
            now=lambda: 1.0,
        )


def test_expired_token_rejected():
    with pytest.raises(OAuthVerifyError):
        verify_google_access_token(
            "tok",
            expected_aud="client-123",
            http_get=lambda u: _info(exp="100"),
            now=lambda: 200.0,
        )


def test_empty_token_rejected():
    with pytest.raises(OAuthVerifyError):
        verify_google_access_token("", expected_aud="client-123")


def test_http_failure_wrapped():
    def boom(url):
        raise RuntimeError("network down")

    with pytest.raises(OAuthVerifyError):
        verify_google_access_token(
            "tok", expected_aud="client-123", http_get=boom, now=lambda: 1.0
        )


def test_cache_entry_capped_at_max_ttl():
    import hashlib

    verify_google_access_token(
        "tok",
        expected_aud="client-123",
        http_get=lambda u: _info(exp="9999999999"),
        now=lambda: 1000.0,
    )
    key = hashlib.sha256(b"tok").hexdigest()
    assert oauth_verify._cache[key][1] == 1000.0 + oauth_verify._CACHE_MAX_TTL


def test_cache_evicts_expired_when_full():
    import hashlib

    for i in range(oauth_verify._CACHE_MAX_ENTRIES):
        oauth_verify._cache[f"stale{i}"] = ("old@x", 1.0)  # expired at now=1000
    verify_google_access_token(
        "fresh",
        expected_aud="client-123",
        http_get=lambda u: _info(),
        now=lambda: 1000.0,
    )
    assert len(oauth_verify._cache) < oauth_verify._CACHE_MAX_ENTRIES
    assert hashlib.sha256(b"fresh").hexdigest() in oauth_verify._cache
