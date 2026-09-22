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

"""Tagging convention for sessions created by scheduled jobs.

Every cron/scheduled job (routine firings, dream-review) routes its
session through ``create_scheduled_session`` so the web UI can group and
search them. The tag lives in ``session.state`` under the keys defined in
``horizon/infrastructure/constants.py``; ``horizon/api/sessions.py`` reads them
back to filter the ``/lha/sessions`` list.
"""

from __future__ import annotations

from typing import Any, Literal

from horizon.api.sessions import TITLE_KEY
from horizon.infrastructure.constants import (
    SCHEDULER_JOB_KEY,
    SCHEDULER_SOURCE,
    SESSION_SOURCE_KEY,
)

ScheduledJob = Literal["dream_review", "routine"]


def scheduled_session_state(
    *, job_type: ScheduledJob, title: str
) -> dict[str, Any]:
    """Build the ``session.state`` delta that tags a scheduled session."""
    return {
        SESSION_SOURCE_KEY: SCHEDULER_SOURCE,
        SCHEDULER_JOB_KEY: job_type,
        TITLE_KEY: title,
    }


async def create_scheduled_session(
    session_service: Any,
    *,
    app_name: str,
    user_id: str,
    job_type: ScheduledJob,
    title: str,
) -> Any:
    """Create a persisted, tagged session in ``session_service``."""
    return await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        state=scheduled_session_state(job_type=job_type, title=title),
    )


__all__ = ["create_scheduled_session", "scheduled_session_state"]
