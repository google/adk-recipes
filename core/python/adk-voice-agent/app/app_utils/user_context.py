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

"""Pre-request warm-up for the signed-in user's cached tokens, settings, and GitHub repos."""

from __future__ import annotations

import asyncio
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.credential_store import prefetch_user_credentials
from app.auth.github.installation_repos import warm_installation_repos
from app.user_settings import load_user_settings

logger = logging.getLogger(__name__)


async def warm_user_context(user_id: str | None = None) -> None:
    results = await asyncio.gather(
        prefetch_user_credentials(user_id),
        load_user_settings(user_id),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("Could not warm a user's context: %s", result)
    try:
        await warm_installation_repos()
    except Exception as exc:
        logger.warning("Could not list a user's GitHub repos: %s", exc)


class UserContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] in {"http", "websocket"}:
            await warm_user_context()
        await self.app(scope, receive, send)
