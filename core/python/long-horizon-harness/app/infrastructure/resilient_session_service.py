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

"""Wraps a SQL-backed session service so transient Cloud SQL disconnects
(mid-query drops, refused-socket reconnects during a restart/failover) are
retried on a fresh pooled connection instead of surfacing as a 500.
"""

from __future__ import annotations

import functools
from typing import Any

from google.adk.sessions import BaseSessionService

from app.infrastructure.db_resilience import retry_on_disconnect


class ResilientSessionService(BaseSessionService):
    def __init__(self, inner: BaseSessionService, *, base_delay: float = 0.25):
        self._inner = inner
        self._base_delay = base_delay

    def __getattr__(self, name: str) -> Any:
        # Delegate anything not explicitly wrapped to the wrapped service.
        return getattr(self._inner, name)

    async def _retry(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        inner_method = getattr(self._inner, method_name)
        return await retry_on_disconnect(
            functools.partial(inner_method, *args, **kwargs),
            base_delay=self._base_delay,
        )

    async def get_session(self, *args: Any, **kwargs: Any) -> Any:
        return await self._retry("get_session", *args, **kwargs)

    async def list_sessions(self, *args: Any, **kwargs: Any) -> Any:
        return await self._retry("list_sessions", *args, **kwargs)

    async def create_session(self, *args: Any, **kwargs: Any) -> Any:
        return await self._retry("create_session", *args, **kwargs)

    async def delete_session(self, *args: Any, **kwargs: Any) -> Any:
        return await self._retry("delete_session", *args, **kwargs)

    async def append_event(self, *args: Any, **kwargs: Any) -> Any:
        return await self._retry("append_event", *args, **kwargs)
