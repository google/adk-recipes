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

"""ID-token minting with TTL caching for orchestrator → Cloud Run auth.

Google ID tokens expire after 1 hour. Pipelines that exceed that wall
without refreshing start returning silent 401s. This helper caches the
token per audience and refreshes it shortly before expiry, so a worker
can call `get_id_token(url)` for every request without paying the
per-call mint cost.

Uses google.auth.default() credentials which work across all GCP
environments (GCE, Cloud Run, Vertex AI Pipelines, local dev with ADC).
"""

from __future__ import annotations

import logging
import threading
import time

import google.auth
import google.auth.transport.requests

logger = logging.getLogger(__name__)

_TOKEN_TTL_SECONDS = 50 * 60

_token_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()

_credentials = None
_auth_request = None


def _get_credentials():
    global _credentials, _auth_request
    if _credentials is None:
        _credentials, _ = google.auth.default()
        _auth_request = google.auth.transport.requests.Request()
    return _credentials, _auth_request


def _mint_id_token(audience: str) -> str:
    """Mint an ID token using the default credentials."""
    from google.oauth2 import id_token

    creds, auth_request = _get_credentials()

    # Try the standard metadata-based approach first (works on GCE, Cloud Run)
    try:
        return id_token.fetch_id_token(auth_request, audience)
    except Exception:
        pass

    # Fallback for Vertex AI Pipelines: use the service account credentials
    # to generate an ID token via IAM signBlob
    if hasattr(creds, "service_account_email"):
        from google.auth import impersonated_credentials

        target_creds = impersonated_credentials.IDTokenCredentials(
            target_credentials=creds,
            target_audience=audience,
        )
        target_creds.refresh(auth_request)
        return target_creds.token

    # Last resort: use access token (works but Cloud Run may reject it
    # if configured for ID tokens only)
    creds.refresh(auth_request)
    return creds.token


def get_id_token(audience: str) -> str:
    """Return a fresh-enough ID token for the given audience.

    Thread-safe. Caches token per audience and refreshes 10 min before
    the 60-min expiry.
    """
    now = time.monotonic()

    cached = _token_cache.get(audience)
    if cached and (now - cached[1]) < _TOKEN_TTL_SECONDS:
        return cached[0]

    with _cache_lock:
        cached = _token_cache.get(audience)
        if cached and (time.monotonic() - cached[1]) < _TOKEN_TTL_SECONDS:
            return cached[0]

        last_err = None
        for attempt in range(5):
            try:
                token = _mint_id_token(audience)
                break
            except Exception as e:
                last_err = e
                logger.warning(
                    "ID token mint attempt %d failed: %s", attempt + 1, e
                )
                time.sleep(2**attempt)
        else:
            raise last_err

        _token_cache[audience] = (token, time.monotonic())
        return token
