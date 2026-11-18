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

"""Vertex AI locations pinned independently of `GOOGLE_CLOUD_LOCATION` (`global`)."""

from __future__ import annotations

import os

# Vertex AI serves TTS only from `global`.
SPEECH_LOCATION = "global"

# Live models are regional and unavailable in `global`.
DEFAULT_LIVE_MODEL_LOCATION = "us-central1"


def live_model_location() -> str:
    return (
        os.getenv("LIVE_MODEL_LOCATION", "").strip()
        or DEFAULT_LIVE_MODEL_LOCATION
    )
