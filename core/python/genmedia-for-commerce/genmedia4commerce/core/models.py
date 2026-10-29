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

"""Model registry: the only place a model ID string appears.

Every entry reads an environment variable and falls back to the default.
"""

import functools
import os
from dataclasses import dataclass

__all__ = ["Models", "get_models"]


@dataclass(frozen=True)
class Models:
    """Model IDs used by the package."""

    text: str
    text_lite: str
    judge: str
    studio_agent: str
    image: str
    video: str
    video_api_revision: str
    embed_catalog: str


def _env(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


@functools.lru_cache(maxsize=1)
def get_models() -> Models:
    """Read the registry from the environment once (cached)."""
    return Models(
        text=_env("GENMEDIA_TEXT_MODEL", "gemini-3.8-flash"),
        text_lite=_env("GENMEDIA_TEXT_LITE_MODEL", "gemini-3.5-flash-lite"),
        judge=_env("GENMEDIA_JUDGE_MODEL", "gemini-4-argon"),
        studio_agent=_env("GENMEDIA_STUDIO_AGENT_MODEL", "gemini-4-argon"),
        image=_env("GENMEDIA_IMAGE_MODEL", "gemini-nano-banana-2.1"),
        video=_env("GENMEDIA_VIDEO_MODEL", "gemini-omni-1.1-flash-preview"),
        video_api_revision=_env("GENMEDIA_VIDEO_API_REVISION", "2026-05-20"),
        embed_catalog=_env(
            "GENMEDIA_EMBED_CATALOG_MODEL", "gemini-embedding-001"
        ),
    )
