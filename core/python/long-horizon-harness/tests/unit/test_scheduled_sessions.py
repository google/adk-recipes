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

"""Unit tests for the scheduled-session tagging helper."""

from __future__ import annotations

import pytest
from google.adk.sessions import InMemorySessionService

from horizon.api.sessions import TITLE_KEY
from horizon.infrastructure.constants import (
    SCHEDULER_JOB_KEY,
    SCHEDULER_SOURCE,
    SESSION_SOURCE_KEY,
)
from horizon.scheduler.sessions import (
    create_scheduled_session,
    scheduled_session_state,
)


def test_state_carries_source_job_and_title() -> None:
    state = scheduled_session_state(job_type="routine", title="Routine: digest")
    assert state[SESSION_SOURCE_KEY] == SCHEDULER_SOURCE
    assert state[SCHEDULER_JOB_KEY] == "routine"
    assert state[TITLE_KEY] == "Routine: digest"


@pytest.mark.asyncio
async def test_create_scheduled_session_persists_tag() -> None:
    svc = InMemorySessionService()
    session = await create_scheduled_session(
        svc,
        app_name="lha",
        user_id="alice",
        job_type="dream_review",
        title="Dream review — 2026-05-29",
    )
    full = await svc.get_session(
        app_name="lha", user_id="alice", session_id=session.id
    )
    assert full is not None
    assert full.state[SESSION_SOURCE_KEY] == SCHEDULER_SOURCE
    assert full.state[SCHEDULER_JOB_KEY] == "dream_review"
    assert full.state[TITLE_KEY] == "Dream review — 2026-05-29"
