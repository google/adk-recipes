# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Authentication configuration for OAuth 2.0 with Google APIs.

Follows the pattern from:
https://fmind.medium.com/powering-up-your-agent-in-production-with-adk-oauth-and-gemini-enterprise-a52b0716fcba

In local dev (ADK Web UI): AUTH_CONFIG is used by the ADK OAuth flow
to prompt the user for consent and exchange the auth code for tokens.

In production (Agent Engine + Gemini Enterprise): The token is injected
by Gemini Enterprise into tool_context.state — AUTH_CONFIG is not used,
but TOKEN_CACHE_KEY and SCOPES are still referenced by negotiate_creds().
"""

import os

from fastapi.openapi.models import (
    OAuth2,
    OAuthFlowAuthorizationCode,
    OAuthFlows,
)
from google.adk.auth.auth_credential import (
    AuthCredential,
    AuthCredentialTypes,
    OAuth2Auth,
)
from google.adk.auth.auth_tool import AuthConfig

# --- OAuth 2.0 Endpoints ---
AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105

# --- Scopes ---
# drive.readonly: read file content from Google Drive
SCOPES = {
    "https://www.googleapis.com/auth/drive.readonly": "Google Drive API (read-only)",
}

# --- Token cache key ---
# Must match the AUTH_ID used in register_oauth.py for Gemini Enterprise.
# In production, Gemini Enterprise injects the token at "temp:<AUTH_ID>".
# In local dev, we cache the full credential dict under this key.
TOKEN_CACHE_KEY = os.environ.get("AUTH_ID", "google-drive-auth")

# --- OAuth scheme + credential (used only for local ADK Web UI dev) ---
AUTH_SCHEME = OAuth2(
    flows=OAuthFlows(
        authorizationCode=OAuthFlowAuthorizationCode(
            authorizationUrl=AUTHORIZATION_URL,
            tokenUrl=TOKEN_URL,
            scopes=SCOPES,
        )
    )
)

AUTH_CREDENTIAL = AuthCredential(
    auth_type=AuthCredentialTypes.OAUTH2,
    oauth2=OAuth2Auth(
        client_id=os.environ.get("OAUTH_CLIENT_ID", ""),
        client_secret=os.environ.get("OAUTH_CLIENT_SECRET", ""),
    ),
)

AUTH_CONFIG = AuthConfig(
    auth_scheme=AUTH_SCHEME,
    raw_auth_credential=AUTH_CREDENTIAL,
)
