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

Loads `.env` via `python-dotenv` at import, then exposes every configurable
value via the module-level `config` object. Reads are lazy — each attribute
access calls `os.getenv()` — so that scripts which mutate `os.environ` (for
example `setup._design_spec_to_env`) see their changes reflected on the
very next read.

`.env.example` at the recipe root documents every key below. When adding
a new value, add it in three places: `.env.example`, this module, and
the code that consumes it.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# Vertex AI genai client bootstrap. Centralized so downstream helpers can
# rely on it being set before the first genai call.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")


class _Config:
    """Lazy env-var accessor. Each read hits os.getenv() fresh."""

    @property
    def GOOGLE_CLOUD_PROJECT(self) -> str:
        return os.getenv("GOOGLE_CLOUD_PROJECT", "")

    @property
    def GOOGLE_CLOUD_LOCATION(self) -> str:
        return os.getenv("GOOGLE_CLOUD_LOCATION", "global")

    @property
    def VECTOR_SEARCH_LOCATION(self) -> str:
        return os.getenv("VECTOR_SEARCH_LOCATION", "us-central1")

    @property
    def VECTOR_SEARCH_COLLECTION(self) -> str:
        """Explicit collection path if set; empty string means 'derive from other config'."""
        return os.getenv("VECTOR_SEARCH_COLLECTION", "")

    @property
    def GEMINI_MODEL(self) -> str:
        return os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

    @property
    def EMBEDDING_MODEL(self) -> str:
        return os.getenv("EMBEDDING_MODEL", "gemini-embedding-001")


config = _Config()
