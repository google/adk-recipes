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


"""The connected apps as Settings shows them (`GET /config` → `apps`)."""

from __future__ import annotations

from typing import Any

from app.auth.provider_hooks import provider_hooks
from app.auth.providers import (
    AuthProvider,
    configured_apps,
    has_provider_credentials,
)


def app_info(app: AuthProvider) -> dict[str, Any]:
    """Assumes the caller warmed the user's tokens (`warm_user_context()`)."""
    connected = has_provider_credentials(app.name)
    return {
        "name": app.name,
        "display_name": app.display_name,
        "icon": app.icon,
        "connected": connected,
        "account_label": app.account_label if connected else "",
        "needs_grant": connected and bool(app.missing_scopes),
        "services": list(app.services),
        "manage_url": provider_hooks(app.name).manage_url()
        if connected
        else None,
    }


def apps_state() -> list[dict[str, Any]]:
    return [app_info(app) for app in configured_apps()]
