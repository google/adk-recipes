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


"""GitHub `ProviderHooks`: connects via GitHub App installation and refreshes repo access."""

from __future__ import annotations

import logging
import os
from urllib.parse import urlencode

from app.auth.github.api import GITHUB_PROVIDER
from app.auth.github.installation_repos import (
    refresh_installation_repos,
    reset_installation_repos,
)
from app.auth.provider_hooks import ProviderHooks

logger = logging.getLogger(__name__)

GITHUB_APP_SLUG_ENV = "GITHUB_APP_SLUG"


def github_app_slug() -> str:
    return os.getenv(GITHUB_APP_SLUG_ENV, "").strip()


def github_install_url(state: str | None = None) -> str:
    query = f"?{urlencode({'state': state})}" if state else ""
    return (
        f"https://github.com/apps/{github_app_slug()}/installations/new{query}"
    )


async def _refresh_repos_quietly(action: str) -> None:
    try:
        await refresh_installation_repos(force=True)
    except Exception as exc:
        logger.warning(
            "Could not %s GitHub installation repos: %s", action, exc
        )


class GitHubHooks(ProviderHooks):
    def configured(self) -> bool:
        return bool(github_app_slug())

    def authorize_url(
        self, *, redirect_uri: str, opener_origin: str
    ) -> str | None:
        from app.auth.oauth import start_pending

        state = start_pending(GITHUB_PROVIDER, redirect_uri, opener_origin)
        return github_install_url(state)

    def manage_url(self) -> str | None:
        return github_install_url()

    async def on_connected(self) -> None:
        await _refresh_repos_quietly("list")

    async def on_callback_without_sign_in(
        self, setup_action: str | None
    ) -> tuple[str, str] | None:
        if setup_action != "update":
            return None
        # GitHub's "Choose repos" redirects here without an OAuth state/code exchange.
        await _refresh_repos_quietly("refresh")
        return (
            "Repositories Updated",
            "ADK Voice Agent will use the repositories you picked. You can close this tab.",
        )


def reset_github_caches() -> None:
    reset_installation_repos()
