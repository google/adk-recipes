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

"""Single source of truth for retail-virtual-tryon configuration.

Loads `.env` via `python-dotenv` at import, then exposes every
configurable value via the module-level `config` object. Reads are
lazy — each attribute access calls `os.getenv()` — so that scripts
which mutate `os.environ` (for example `setup_tryon._design_spec_to_env`)
see their changes reflected on the very next read.

`.env.example` at the recipe root documents every key below. When adding
a new value, add it in three places: `.env.example`, this module, and
the code that consumes it.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# Vertex AI genai client bootstrap. Centralized so `_get_client` helpers
# can rely on it being set before the first genai call.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")


class _Config:
    """Lazy env-var accessor. Each read hits os.getenv() fresh."""

    @property
    def GOOGLE_CLOUD_PROJECT(self) -> str:
        return os.getenv("GOOGLE_CLOUD_PROJECT", "")

    @property
    def GCP_REGION(self) -> str:
        return os.getenv("GCP_REGION", "us-west1")

    @property
    def GEMINI_MODEL_LOCATION(self) -> str:
        return os.getenv("GEMINI_MODEL_LOCATION", "global")

    @property
    def GEMINI_IMAGE_MODEL(self) -> str:
        return os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

    @property
    def GEMINI_MODEL(self) -> str:
        return os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

    @property
    def GEMINI_TEXT_MODEL(self) -> str:
        return os.getenv("GEMINI_TEXT_MODEL", "gemini-3.5-flash")

    @property
    def TRYON_OUTPUT_BUCKET(self) -> str:
        return os.getenv("TRYON_OUTPUT_BUCKET", "")

    @property
    def TRYON_UPLOAD_BUCKET(self) -> str:
        return os.getenv("TRYON_UPLOAD_BUCKET", "")

    @property
    def TRYON_CATALOG_PATH(self) -> str:
        return os.getenv("TRYON_CATALOG_PATH", "catalog_images")

    @property
    def PORT(self) -> int:
        return int(os.getenv("PORT", "8080"))


config = _Config()
