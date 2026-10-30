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

"""Process-wide ADK session/artifact services shared by every serving surface.

Registered under ``shared://`` so every ADK route `get_fast_api_app` mounts
(`/run_live` and the session REST routes) uses one instance: a session created
through any of them is visible to the others.
"""

from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass

from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
from google.adk.cli.service_registry import get_service_registry

SESSION_SERVICE_URI = "shared://session"
ARTIFACT_SERVICE_URI = "shared://artifact"


@dataclass(frozen=True)
class AgentEngine:
    project: str | None
    location: str | None
    engine_id: str


_ENGINE_NAME = re.compile(
    r"^projects/([^/]+)/locations/([^/]+)/reasoningEngines/[^/]+$"
)


def agent_engine() -> AgentEngine | None:
    """Return the configured regional Agent Engine, or None when unset."""
    engine_id = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID", "").strip()
    if not engine_id:
        return None
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION", "").strip()
    if match := _ENGINE_NAME.match(engine_id):
        project = match.group(1)
        location = location or match.group(2)
    if not location or location == "global":
        raise RuntimeError(
            "GOOGLE_CLOUD_AGENT_ENGINE_ID is set without its region: set "
            "GOOGLE_CLOUD_AGENT_ENGINE_LOCATION (e.g. us-central1) or use the full "
            "engine name, projects/<project>/locations/<region>/reasoningEngines/<id>."
        )
    return AgentEngine(
        project=project,
        location=location,
        engine_id=engine_id,
    )


@functools.cache
def get_session_service():
    """Process-wide session service shared across every serving surface: ADK's
    `DatabaseSessionService` in the thread database (Cloud SQL deployed, SQLite
    locally), so sessions outlive restarts and live with the threads they belong to."""
    from google.adk.sessions.database_session_service import (
        DatabaseSessionService,
    )

    from app.store.engine import get_async_engine

    return DatabaseSessionService(db_engine=get_async_engine())


async def purge_expired_sessions(
    cutoff: float, *, user_id: str | None = None
) -> int:
    """Delete ADK sessions last updated before `cutoff` (epoch seconds)."""
    service = get_session_service()
    listed = await service.list_sessions(app_name="app", user_id=user_id)
    expired = [
        s for s in listed.sessions or [] if (s.last_update_time or 0.0) < cutoff
    ]
    for s in expired:
        await service.delete_session(
            app_name=s.app_name, user_id=s.user_id, session_id=s.id
        )
    return len(expired)


async def clear_user_sessions(user_id: str) -> int:
    """Delete all ADK sessions belonging to `user_id`."""
    return await purge_expired_sessions(float("inf"), user_id=user_id)


@functools.cache
def get_artifact_service():
    """Process-wide artifact service: GCS when a bucket is set, else in-memory."""
    if bucket := os.environ.get("LOGS_BUCKET_NAME"):
        return GcsArtifactService(bucket_name=bucket)
    return InMemoryArtifactService()


_registry = get_service_registry()
_registry.register_session_service(
    "shared", lambda uri, **kw: get_session_service()
)
_registry.register_artifact_service(
    "shared", lambda uri, **kw: get_artifact_service()
)
