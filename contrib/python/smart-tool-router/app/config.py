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

"""Single source of truth for smart-tool-router agent configuration.

Reads `.env` (loaded in `app/__init__.py`) and falls back to `.env.example`
at the recipe root so defaults live in `.env.example` rather than hardcoded
in Python calls.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

_DEFAULTS = dotenv_values(
    Path(__file__).resolve().parent.parent / ".env.example"
)
_PLACEHOLDER_PREFIX = "<"


def _read_env(key: str) -> str:
    """Read an environment variable, falling back to `.env.example`."""
    value = os.getenv(key)
    if (
        value is not None
        and value != ""
        and not value.startswith(_PLACEHOLDER_PREFIX)
    ):
        return value
    default_value = _DEFAULTS.get(key)
    if default_value and not default_value.startswith(_PLACEHOLDER_PREFIX):
        return default_value
    return ""


if "GOOGLE_GENAI_USE_ENTERPRISE" not in os.environ:
    os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = _read_env(
        "GOOGLE_GENAI_USE_ENTERPRISE"
    )


class _Config:
    """Lazy environment variable accessor."""

    @property
    def GOOGLE_CLOUD_PROJECT(self) -> str:
        return _read_env("GOOGLE_CLOUD_PROJECT")

    @property
    def GOOGLE_CLOUD_LOCATION(self) -> str:
        return _read_env("GOOGLE_CLOUD_LOCATION")

    @property
    def GOOGLE_CLOUD_AGENT_ENGINE_ID(self) -> str:
        return _read_env("GOOGLE_CLOUD_AGENT_ENGINE_ID")

    @property
    def GOOGLE_CLOUD_AGENT_ENGINE_LOCATION(self) -> str:
        return _read_env("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION")

    @property
    def GEMINI_MODEL(self) -> str:
        return _read_env("MODEL_NAME") or _read_env("GEMINI_MODEL")

    @property
    def SMART_ROUTER_MODE(self) -> str:
        return _read_env("SMART_ROUTER_MODE")

    @property
    def SMART_ROUTER_EMBEDDING_MODEL(self) -> str:
        return _read_env("SMART_ROUTER_EMBEDDING_MODEL")

    @property
    def SMART_ROUTER_BQ_PROJECT(self) -> str:
        return _read_env("SMART_ROUTER_BQ_PROJECT") or _read_env(
            "GOOGLE_CLOUD_PROJECT"
        )

    @property
    def SMART_ROUTER_BQ_DATASET(self) -> str:
        return _read_env("SMART_ROUTER_BQ_DATASET")

    @property
    def ALLOW_ORIGINS(self) -> str:
        return _read_env("ALLOW_ORIGINS")

    @property
    def APP_URL(self) -> str:
        return _read_env("APP_URL")

    @property
    def AGENT_VERSION(self) -> str:
        return _read_env("AGENT_VERSION")

    @property
    def SESSION_SERVICE_URI(self) -> str:
        return _read_env("SESSION_SERVICE_URI")

    @property
    def LOGS_BUCKET_NAME(self) -> str:
        return _read_env("LOGS_BUCKET_NAME")

    @property
    def INTEGRATION_TEST(self) -> str:
        return _read_env("INTEGRATION_TEST")

    @property
    def PORT(self) -> int:
        return int(_read_env("PORT"))


config = _Config()
