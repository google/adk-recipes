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

"""Extension hooks for connected-app behavior beyond `config/integrations.yaml`."""

from __future__ import annotations

import functools


class ProviderHooks:
    """Per-provider behavior beyond `config/integrations.yaml`; defaults are no-ops."""

    def configured(self) -> bool:
        return True

    def authorize_url(
        self, *, redirect_uri: str, opener_origin: str
    ) -> str | None:
        return None

    def manage_url(self) -> str | None:
        return None

    async def on_connected(self) -> None:
        pass

    async def on_callback_without_sign_in(
        self, setup_action: str | None
    ) -> tuple[str, str] | None:
        return None

    async def access_token(self, provider: str) -> str | None:
        from app.auth.oauth import ensure_fresh_access_token

        return await ensure_fresh_access_token(provider)


_NO_HOOKS = ProviderHooks()


@functools.cache
def _registry() -> dict[str, ProviderHooks]:
    from app.auth.github.api import GITHUB_PROVIDER
    from app.auth.github.provider import GitHubHooks

    return {GITHUB_PROVIDER: GitHubHooks()}


def provider_hooks(name: str) -> ProviderHooks:
    return _registry().get(name, _NO_HOOKS)
