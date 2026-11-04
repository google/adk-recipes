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

"""`COMPUTER_PROVIDER=static`: externally managed computers at `COMPUTER_URL_TEMPLATE` or `COMPUTER_URL`."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from app.workers.environment import SandboxEnvironment
from app.workers.providers.base import (
    ComputerState,
    ComputerStatus,
    ComputerUnavailableError,
)
from app.workers.providers.common import (
    IdTokenSource,
    computer_auth_audience,
    is_loopback_url,
    owner_id,
    wait_for_daemon,
)


def computer_url(owner: str) -> str:
    template = (os.getenv("COMPUTER_URL_TEMPLATE") or "").strip()
    if template:
        return template.format(owner=owner).rstrip("/")
    from app.auth.identity import AuthMode, get_auth_mode

    if get_auth_mode() is AuthMode.IAP:
        raise ComputerUnavailableError(
            "COMPUTER_PROVIDER=static requires COMPUTER_URL_TEMPLATE when VOICE_AGENT_AUTH_MODE=iap."
        )
    return (os.getenv("COMPUTER_URL") or "").strip().rstrip("/")


class StaticProvider:
    name = "static"

    def __init__(self, *, transport: Any | None = None) -> None:
        self._transport = transport
        self._tokens: dict[str, IdTokenSource] = {}

    def _environment(self, user_id: str) -> SandboxEnvironment:
        from app.auth.env_store import load_dotenv_defaults

        load_dotenv_defaults()
        owner = owner_id(user_id)
        url = computer_url(owner)
        if not url:
            raise ComputerUnavailableError(
                "COMPUTER_PROVIDER=static needs COMPUTER_URL_TEMPLATE or COMPUTER_URL."
            )
        if "://" not in url:
            url = f"https://{url}"
        token_source = None
        if not is_loopback_url(url):
            has_template = bool(
                (os.getenv("COMPUTER_URL_TEMPLATE") or "").strip()
            )
            audience = computer_auth_audience(owner if has_template else None)
            token_source = self._tokens.setdefault(
                audience, IdTokenSource(audience)
            )
        return SandboxEnvironment(
            name=f"computer-{owner}",
            url=url,
            transport=self._transport,
            token_source=token_source,
        )

    async def ensure(self, user_id: str) -> SandboxEnvironment:
        env = await asyncio.to_thread(self._environment, user_id)
        # Mint (and cache) the first token off the event loop.
        await asyncio.to_thread(env.headers)
        return env

    async def status(self, user_id: str) -> ComputerStatus:
        try:
            env = await self.ensure(user_id)
            await wait_for_daemon(
                env.base_url,
                headers=env.headers,
                transport=self._transport,
                timeout_s=1.0,
            )
        except ComputerUnavailableError as exc:
            return ComputerStatus(ComputerState.ERROR, str(exc))
        return ComputerStatus(ComputerState.RUNNING)

    async def stop(self, user_id: str) -> None:
        return None

    async def known_users(self) -> list[str]:
        # The URLs can't be enumerated; nothing here is ours to stop anyway.
        return []
