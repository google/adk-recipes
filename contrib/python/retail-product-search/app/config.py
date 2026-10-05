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

"""Single source of truth for retail-product-search agent configuration.

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


def _read_env(key: str) -> str:
    """Read an environment variable, falling back to `.env.example`."""
    val = os.getenv(key)
    if val is not None and val != "" and not val.startswith("<"):
        return val
    default_val = _DEFAULTS.get(key)
    if default_val and not default_val.startswith("<"):
        return default_val
    return ""


def _read_bool(key: str) -> bool:
    """Read a boolean environment variable, falling back to `.env.example`."""
    return _read_env(key).strip().lower() in ("1", "true", "yes", "on")


if "GOOGLE_GENAI_USE_VERTEXAI" not in os.environ:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = _read_env(
        "GOOGLE_GENAI_USE_VERTEXAI"
    )


class _Config:
    """Lazy env-var accessor."""

    @property
    def GOOGLE_CLOUD_PROJECT(self) -> str:
        return _read_env("GOOGLE_CLOUD_PROJECT")

    @property
    def GOOGLE_CLOUD_LOCATION(self) -> str:
        return _read_env("GOOGLE_CLOUD_LOCATION")

    @property
    def VECTOR_SEARCH_LOCATION(self) -> str:
        return _read_env("VECTOR_SEARCH_LOCATION")

    @property
    def VECTOR_SEARCH_COLLECTION(self) -> str:
        return _read_env("VECTOR_SEARCH_COLLECTION")

    @property
    def GEMINI_MODEL(self) -> str:
        for key in ("MODEL_NAME", "GEMINI_MODEL"):
            val = os.getenv(key)
            if val is not None and val != "" and not val.startswith("<"):
                return val
        return _read_env("MODEL_NAME") or _read_env("GEMINI_MODEL")

    @property
    def EMBEDDING_MODEL(self) -> str:
        return _read_env("EMBEDDING_MODEL")

    @property
    def AGENT_VERSION(self) -> str:
        return _read_env("AGENT_VERSION")

    @property
    def ALLOW_ORIGINS(self) -> str:
        return _read_env("ALLOW_ORIGINS")

    @property
    def APP_URL(self) -> str:
        return _read_env("APP_URL")

    @property
    def PORT(self) -> int:
        return int(_read_env("PORT"))

    @property
    def DEPLOY_AGENT_ENGINE(self) -> bool:
        return _read_bool("DEPLOY_AGENT_ENGINE")

    @property
    def PUBLISH_GEMINI_ENTERPRISE(self) -> bool:
        return _read_bool("PUBLISH_GEMINI_ENTERPRISE")

    @property
    def PUBLISH_AGENT_GARDEN(self) -> bool:
        return _read_bool("PUBLISH_AGENT_GARDEN")

    @property
    def DEPLOY_CLOUD_RUN(self) -> bool:
        return _read_bool("DEPLOY_CLOUD_RUN")

    @property
    def RUN_LOCAL_WEB(self) -> bool:
        return _read_bool("RUN_LOCAL_WEB")

    def __getattr__(self, name: str) -> str:
        return _read_env(name)


config = _Config()
