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

"""Centralized configuration for the ambient expense agent."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# --- Authentication ---
# If GOOGLE_API_KEY is set, use AI Studio (default).
# Otherwise, fall back to Vertex AI with Google Cloud credentials.
if os.getenv("GOOGLE_API_KEY"):
    # AI Studio mode: always set explicitly so a stale env value cannot
    # silently override the detected auth mode.
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
else:
    import google.auth

    _, project_id = google.auth.default()
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id or "")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
    # Vertex AI mode: always set explicitly (computed, not user-configurable).
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"


@dataclass
class ExpenseAgentConfig:
    """Agent configuration with sensible defaults."""

    model: str = os.getenv("MODEL_NAME")
    review_threshold: float = 100.0


config = ExpenseAgentConfig()
