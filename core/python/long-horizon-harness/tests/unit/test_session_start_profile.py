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

"""``on_session_start_callback`` loads the user profile into session state.

The live agent's ``## User Profile`` block renders ``state['user_profile']``;
session start is where that key gets populated (once per session) from the
user's structured profile.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


def _ctx(state: dict, *, user_id: str = "user-1") -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        state=state,
        _invocation_context=SimpleNamespace(
            memory_service=object(),
            app_name="app",
            user_id=user_id,
        ),
    )


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")


async def test_populates_user_profile_state(monkeypatch):
    from app.conversation import session_start

    calls: list[dict[str, Any]] = []

    async def fake_load(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "Alice is an SRE."

    monkeypatch.setattr(session_start, "load_user_profile", fake_load)

    state: dict[str, Any] = {}
    await session_start.on_session_start_callback(_ctx(state))

    assert state["user_profile"] == "Alice is an SRE."
    assert len(calls) == 1
    assert calls[0]["app_name"] == "app"
    assert calls[0]["user_id"] == "user-1"
    # Injected every turn, so the volatile block stays narrative-only.
    assert calls[0].get("include_lists", False) is False


async def test_loads_once_per_session(monkeypatch):
    from app.conversation import session_start

    calls: list[Any] = []

    async def fake_load(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "profile"

    monkeypatch.setattr(session_start, "load_user_profile", fake_load)

    state: dict[str, Any] = {}
    ctx = _ctx(state)
    await session_start.on_session_start_callback(ctx)
    await session_start.on_session_start_callback(ctx)  # second turn

    assert len(calls) == 1  # run-once guard prevents a refetch


async def test_empty_profile_still_sets_key(monkeypatch):
    from app.conversation import session_start

    async def fake_load(**_kwargs: Any) -> str:
        return ""

    monkeypatch.setattr(session_start, "load_user_profile", fake_load)

    state: dict[str, Any] = {}
    await session_start.on_session_start_callback(_ctx(state))
    assert state["user_profile"] == ""
