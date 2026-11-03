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

"""GCP project and location env vars for harness runs on the computer (no credentials)."""

from __future__ import annotations

import os
from collections.abc import Mapping

from app.auth.env_store import read_dotenv


def _first(keys: tuple[str, ...], *sources: Mapping[str, str]) -> str:
    for source in sources:
        for key in keys:
            if value := (source.get(key) or "").strip():
                return value
    return ""


def sandbox_location() -> str:
    """Return `SANDBOX_GCP_LOCATION` or `global` (never the voice layer's `GOOGLE_CLOUD_LOCATION`)."""
    return (
        _first(("SANDBOX_GCP_LOCATION",), read_dotenv(), os.environ) or "global"
    )


def genai_enterprise_env() -> dict[str, str]:
    """Set both `GOOGLE_GENAI_USE_ENTERPRISE` and legacy `GOOGLE_GENAI_USE_VERTEXAI` to match."""
    value = (
        _first(
            ("GOOGLE_GENAI_USE_ENTERPRISE", "GOOGLE_GENAI_USE_VERTEXAI"),
            os.environ,
        )
        or "TRUE"
    )
    return {
        "GOOGLE_GENAI_USE_ENTERPRISE": value,
        "GOOGLE_GENAI_USE_VERTEXAI": value,
    }


def get_sandbox_gcp_env() -> dict[str, str]:
    file_env = read_dotenv()
    process_env = dict(os.environ)
    project_id = _first(
        ("SANDBOX_GCP_PROJECT", "GOOGLE_CLOUD_PROJECT"), file_env, process_env
    )

    env = {
        "GOOGLE_CLOUD_LOCATION": sandbox_location(),
        **genai_enterprise_env(),
        "CLOUD_ML_REGION": _first(("CLOUD_ML_REGION",), file_env, process_env)
        or "global",
    }
    if project_id:
        for key in (
            "GOOGLE_CLOUD_PROJECT",
            "GCLOUD_PROJECT",
            "CLOUDSDK_CORE_PROJECT",
            "GOOGLE_CLOUD_QUOTA_PROJECT",
        ):
            env[key] = project_id
        env["ANTHROPIC_VERTEX_PROJECT_ID"] = (
            _first(("ANTHROPIC_VERTEX_PROJECT_ID",), file_env) or project_id
        )
    return env
