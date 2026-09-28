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

"""on_session_start_callback stamps the sidebar title from the first user
message, so /lha/sessions reads it from state instead of scanning events.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import types

from app.infrastructure.constants import TITLE_KEY

pytestmark = pytest.mark.asyncio


def _ctx(state: dict, *, user_text: str | None, user_id: str = "user-1"):
    content = (
        types.Content(role="user", parts=[types.Part(text=user_text)])
        if user_text is not None
        else None
    )
    return SimpleNamespace(
        user_id=user_id,
        state=state,
        user_content=content,
        _invocation_context=SimpleNamespace(
            memory_service=object(),
            app_name="app",
            user_id=user_id,
        ),
    )


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.setenv("LHA_ENVIRONMENT_BACKEND", "local")


@pytest.fixture(autouse=True)
def _stub_profile(monkeypatch):
    from app.conversation import session_start

    async def fake_load(**_kwargs: Any) -> str:
        return ""

    monkeypatch.setattr(session_start, "load_user_profile", fake_load)


async def test_stamps_title_from_first_user_message():
    from app.conversation import session_start

    state: dict[str, Any] = {}
    await session_start.on_session_start_callback(
        _ctx(state, user_text="Build me a tetris game\nin python")
    )
    assert state[TITLE_KEY] == "Build me a tetris game"


async def test_does_not_overwrite_an_existing_title():
    from app.conversation import session_start

    state: dict[str, Any] = {TITLE_KEY: "renamed by user"}
    await session_start.on_session_start_callback(
        _ctx(state, user_text="some later message")
    )
    assert state[TITLE_KEY] == "renamed by user"


async def test_no_user_content_leaves_title_unset():
    from app.conversation import session_start

    state: dict[str, Any] = {}
    await session_start.on_session_start_callback(_ctx(state, user_text=None))
    assert TITLE_KEY not in state
