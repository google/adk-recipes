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

"""Live round-trip of the native Structured Profile against Memory Bank.

Gated by ``LHA_LIVE_MEMORY_TESTS=1`` because it hits a real Vertex Agent
Engine (one whose ``structured_memory_configs`` already include the
``user-profile`` schema — see ``scripts/provision_agent_engine.py``).
Requires ``AGENT_ENGINE_RESOURCE_NAME`` + GCP creds. Not part of CI.

Run:
    LHA_LIVE_MEMORY_TESTS=1 AGENT_ENGINE_RESOURCE_NAME=projects/.../reasoningEngines/ID \\
      uv run pytest tests/smoke/backend/test_memory_profiles_smoke.py -q
"""

from __future__ import annotations

import os

import pytest
from google.adk.events.event import Event
from google.genai.types import Content, Part

pytestmark = pytest.mark.skipif(
    os.environ.get("LHA_LIVE_MEMORY_TESTS") != "1",
    reason="live Memory Bank test; set LHA_LIVE_MEMORY_TESTS=1 to run",
)


@pytest.mark.asyncio
async def test_generate_then_retrieve_profile_round_trip():
    from google.adk.memory import VertexAiMemoryBankService

    from app.infrastructure.memory_config import USER_PROFILE_SCHEMA_ID
    from app.memory.dream_review import _run_dream_review_for_user
    from app.memory.user_profile import load_user_profile

    resource = os.environ["AGENT_ENGINE_RESOURCE_NAME"]
    engine_id = resource.rsplit("/", 1)[-1]
    memory_service = VertexAiMemoryBankService(agent_engine_id=engine_id)

    app_name = "app"
    user_id = "lha-smoke-profile-user"

    transcript = (
        "Hi, I'm Dana, a staff data engineer at a logistics company. I live in "
        "EST, work mostly in dbt and BigQuery, and like terse answers with "
        "runnable SQL."
    )

    class _OneSession:
        async def list_sessions(self, *, app_name, user_id=None):
            del app_name
            from types import SimpleNamespace

            return SimpleNamespace(
                sessions=[
                    SimpleNamespace(
                        id="s1", user_id=user_id or "u", last_update_time=1.0
                    )
                ]
            )

        async def get_session(self, *, app_name, user_id, session_id):
            del app_name, user_id, session_id
            from types import SimpleNamespace

            return SimpleNamespace(
                id="s1",
                events=[
                    Event(
                        author="user",
                        invocation_id="smoke",
                        content=Content(
                            role="user", parts=[Part(text=transcript)]
                        ),
                    )
                ],
            )

    result = await _run_dream_review_for_user(
        memory_service=memory_service,
        app_name=app_name,
        user_id=user_id,
        session_service=_OneSession(),
    )
    assert result["success"] is True, result

    profile = await load_user_profile(
        memory_service=memory_service,
        app_name=app_name,
        user_id=user_id,
        include_lists=True,
    )
    assert profile, f"expected a populated {USER_PROFILE_SCHEMA_ID} profile"
    assert "Dana" in profile or "data" in profile.lower()
