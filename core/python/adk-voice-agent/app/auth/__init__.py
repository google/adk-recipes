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

"""Connected-app credentials: OAuth 2.0 + PKCE, token refresh, and per-user storage."""

from app.auth.env_store import persist_env_vars
from app.auth.oauth import (
    build_oauth_authorize_url,
    ensure_fresh_access_token,
    exchange_oauth_code,
    oauth_opener_origin,
    revoke_provider_grant,
)
from app.auth.providers import (
    INTEGRATIONS_CONFIG_PATH,
    bearer_headers,
    disconnect_provider,
    get_provider,
    has_provider_credentials,
    is_remote_deployment,
    record_provider_state,
)
from app.auth.sandbox_env import get_sandbox_gcp_env
from app.auth.verify import verify_and_save_token

__all__ = [
    "INTEGRATIONS_CONFIG_PATH",
    "bearer_headers",
    "build_oauth_authorize_url",
    "disconnect_provider",
    "ensure_fresh_access_token",
    "exchange_oauth_code",
    "get_provider",
    "get_sandbox_gcp_env",
    "has_provider_credentials",
    "is_remote_deployment",
    "oauth_opener_origin",
    "persist_env_vars",
    "record_provider_state",
    "revoke_provider_grant",
    "verify_and_save_token",
]
