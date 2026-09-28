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

"""Identity resolution + FastAPI middleware for IAP-protected deployments."""

from app.auth.identity import (
    AuthMode,
    IdentityMiddleware,
    UnknownAuthModeError,
    current_user_id,
    get_auth_mode,
    get_user_id_from_context,
    resolve_user_id,
    user_identity_scope,
)

__all__ = [
    "AuthMode",
    "IdentityMiddleware",
    "UnknownAuthModeError",
    "current_user_id",
    "get_auth_mode",
    "get_user_id_from_context",
    "resolve_user_id",
    "user_identity_scope",
]
