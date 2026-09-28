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

"""Unit coverage for the session-service retry wrapper."""

from __future__ import annotations

import asyncpg
import pytest

from app.infrastructure.resilient_session_service import (
    ResilientSessionService,
)


class _FlakyService:
    def __init__(self):
        self.calls = 0

    async def get_session(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise asyncpg.exceptions.ConnectionDoesNotExistError()
        return {"id": kwargs.get("session_id")}

    async def list_sessions(self, **kwargs):
        return {"sessions": []}


@pytest.mark.asyncio
async def test_get_session_retries_on_disconnect():
    inner = _FlakyService()
    svc = ResilientSessionService(inner, base_delay=0)
    out = await svc.get_session(app_name="a", user_id="u", session_id="s")
    assert out == {"id": "s"}
    assert inner.calls == 2  # retried once


@pytest.mark.asyncio
async def test_unknown_attribute_delegates():
    inner = _FlakyService()
    svc = ResilientSessionService(inner, base_delay=0)
    # passthrough attribute access for anything not explicitly wrapped
    assert svc.calls == 0
