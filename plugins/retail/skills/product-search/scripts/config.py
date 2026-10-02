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

"""Single source of truth for retail-product-search configuration.

Loads `.env` via `python-dotenv` at import and reads defaults from
`.env.example`, then exposes every configurable value via the module-level
`config` object. Reads are lazy — each attribute access calls `os.getenv()`
— so that scripts which mutate `os.environ` (for example
`setup._design_spec_to_env`) see their changes reflected on the very next read.

`.env.example` at the recipe root documents every key below. When adding
a new value, add it in three places: `.env.example`, this module, and
the code that consumes it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

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


# Gemini Enterprise Agent Platform genai client bootstrap. Centralized so
# downstream helpers can rely on it being set before the first genai call.
if "GOOGLE_GENAI_USE_VERTEXAI" not in os.environ:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = _read_env(
        "GOOGLE_GENAI_USE_VERTEXAI"
    )


class _Config:
    """Lazy env-var accessor. Each read hits os.getenv() fresh."""

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
        """Explicit collection path if set; empty string means 'derive from other config'."""
        return _read_env("VECTOR_SEARCH_COLLECTION")

    @property
    def GEMINI_MODEL(self) -> str:
        return _read_env("GEMINI_MODEL")

    @property
    def EMBEDDING_MODEL(self) -> str:
        return _read_env("EMBEDDING_MODEL")

    @property
    def SURFACE_GEMINI_SKILL(self) -> bool:
        return _read_bool("SURFACE_GEMINI_SKILL")

    @property
    def SURFACE_AGENTS_SKILL(self) -> bool:
        return _read_bool("SURFACE_AGENTS_SKILL")

    def __getattr__(self, name: str) -> Any:
        return _read_env(name)


config = _Config()
