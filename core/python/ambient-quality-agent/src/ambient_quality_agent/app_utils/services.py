# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Process-wide ADK session and artifact services, shared by every serving surface.

Registered under ``shared://`` so that ADK's own web routes and anything the
container attaches later resolve the *same* instance: a session created on one
surface is then visible to the others. Without it each surface builds its own
service off the same URI, and a chat turn and the ADK routes see different
sessions.

`get_fast_api_app` takes URIs rather than objects, so a scheme in the registry
is the only seam for handing it an instance we also hold a reference to.
"""

from __future__ import annotations

import functools
import os
from typing import Any

from google.adk.cli.service_registry import get_service_registry
from google.adk.cli.utils.service_factory import (
    create_artifact_service_from_options,
    create_session_service_from_options,
)

SESSION_SERVICE_URI = "shared://session"
ARTIFACT_SERVICE_URI = "shared://artifact"

# ADK discovers agents by directory name beneath this root, so it is the
# package's parent -- making the app loadable as `ambient_quality_agent`.
AGENT_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def _resolve_session_service_uri() -> str | None:
    """Where sessions live; ``None`` falls back to ADK's in-memory service.

    Investigations are submitted from a user's turn but run in a *separate*
    durable job invocation. The run itself is in BigQuery, so its progress
    survives either way, but the config overrides the job reads (and the chat
    history) live in the session. On Agent Runtime that has to be the managed
    service (`agentengine://`), or the job resolves a different session from the
    turn that scheduled it and runs against the deploy-time config.

    The full resource name is what carries the location. ADK resolves a bare id
    against `GOOGLE_CLOUD_LOCATION`, which `agents-cli` sets to `global`, and a
    regional engine does not exist there.

    Returns:
        The session service URI string, or None to fall back to in-memory storage.
    """
    if uri := os.environ.get("SESSION_SERVICE_URI"):
        return uri
    # All three are injected by Agent Runtime, and absent when running locally.
    engine_id = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION")
    if engine_id and project and location:
        # Agent Runtime injects a bare id, but a full resource name is also a
        # valid setting; taking the last segment accepts both instead of
        # nesting one resource name inside another.
        return (
            f"agentengine://projects/{project}"
            f"/locations/{location}/reasoningEngines/{engine_id.split('/')[-1]}"
        )
    return None


def _resolve_artifact_service_uri() -> str | None:
    """Resolves the artifact service URI.

    Uses GCS when a jobs bucket is configured, otherwise falls back to
    ADK's in-memory service.

    Returns:
        GCS bucket URI string ("gs://..."), or None for in-memory storage.
    """
    if bucket := os.environ.get("AQA_JOBS_GCS_BUCKET"):
        return f"gs://{bucket}"
    return None


@functools.cache
def get_session_service() -> Any:
    """Provides the process-wide session service shared across all surfaces.

    Returns:
        Cached session service instance.
    """
    return create_session_service_from_options(
        base_dir=AGENT_DIR, session_service_uri=_resolve_session_service_uri()
    )


@functools.cache
def get_artifact_service() -> Any:
    """Provides the process-wide artifact service shared across all surfaces.

    Returns:
        Cached artifact service instance.
    """
    return create_artifact_service_from_options(
        base_dir=AGENT_DIR, artifact_service_uri=_resolve_artifact_service_uri()
    )


_registry = get_service_registry()
_registry.register_session_service(
    "shared", lambda uri, **kw: get_session_service()
)
_registry.register_artifact_service(
    "shared", lambda uri, **kw: get_artifact_service()
)
