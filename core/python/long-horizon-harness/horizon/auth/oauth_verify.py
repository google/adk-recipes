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

"""Verify a Google OAuth2 access token via the tokeninfo endpoint.

Gemini Enterprise forwards the end-user's access token in `Authorization:
Bearer` on /a2a. We confirm it was minted for our OAuth client (the `aud`
claim) and return the verified email, so the A2A turn runs as the real user.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request
from collections.abc import Callable

logger = logging.getLogger(__name__)

_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
_TOKENINFO_TIMEOUT = 10.0  # seconds; tight timeout for synchronous per-request token verification
_CACHE_MAX_TTL = (
    300.0  # cap cache entries at 5 min even if the token lives longer
)
_CACHE_MAX_ENTRIES = (
    4096  # only valid tokens are cached; bound unbounded growth
)

# sha256(token) -> (email, expiry_epoch)
_cache: dict[str, tuple[str, float]] = {}


class OAuthVerifyError(Exception):
    """The access token was missing, invalid, expired, or not for our client."""


def _default_get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=_TOKENINFO_TIMEOUT) as resp:  # noqa: S310 -- https URL with timeout
        return json.loads(resp.read())


def _truthy(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def verify_google_access_token(
    token: str,
    *,
    expected_aud: str,
    http_get: Callable[[str], dict] = _default_get,
    now: Callable[[], float] = time.time,
) -> str:
    """Return the verified email for ``token`` or raise ``OAuthVerifyError``."""
    if not token:
        raise OAuthVerifyError("empty token")
    key = hashlib.sha256(token.encode()).hexdigest()
    current = now()
    hit = _cache.get(key)
    if hit and hit[1] > current:
        return hit[0]

    query = urllib.parse.urlencode({"access_token": token})
    try:
        info = http_get(f"{_TOKENINFO_URL}?{query}")
    except Exception as err:  # network / HTTP / JSON
        raise OAuthVerifyError(f"tokeninfo request failed: {err}") from err

    if not isinstance(info, dict):
        raise OAuthVerifyError("tokeninfo returned non-dict")
    if info.get("aud") != expected_aud:
        raise OAuthVerifyError("token audience mismatch")
    email = info.get("email")
    if not isinstance(email, str) or not email:
        raise OAuthVerifyError("token missing email")
    if not _truthy(info.get("email_verified")):
        raise OAuthVerifyError("email not verified")
    try:
        exp = float(info.get("exp"))  # type: ignore[arg-type]
    except (TypeError, ValueError) as err:
        raise OAuthVerifyError("token missing/invalid exp") from err
    if exp <= current:
        raise OAuthVerifyError("token expired")

    if len(_cache) >= _CACHE_MAX_ENTRIES:
        # NOTE: drop expired entries first; clear wholesale only if still full.
        for k in [k for k, (_, e) in _cache.items() if e <= current]:
            del _cache[k]
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            _cache.clear()
    _cache[key] = (email, min(exp, current + _CACHE_MAX_TTL))
    return email
