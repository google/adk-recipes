# Copyright 2025 Google LLC
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

"""Custom AdkApp that preserves critical environment variables for Agent Engine.

This module provides workarounds for Agent Engine environment variable issues:

1. GOOGLE_CLOUD_LOCATION: Agent Engine overrides this to match the deployment
   region (us-central1), which breaks Gemini 3 models requiring the global endpoint.

2. GOOGLE_GENAI_USE_VERTEXAI: Despite being passed in env_vars, this may not be
   available at runtime due to ADK bug (GitHub issue #3208). Video generation
   requires this to be TRUE for the Vertex AI video byte extraction path.

Solution: Force-set critical env vars in set_up() after super().set_up().
See: https://github.com/google/adk-python/issues/3628#issuecomment-3666413473
"""

import os

import vertexai
from vertexai.agent_engines import AdkApp

# Use a custom env var that Agent Engine doesn't override
# GEMINI_MODEL_LOCATION is set in .env and passed via env_vars during deployment
# Falls back to 'global' for Gemini 3 models
GEMINI_MODEL_LOCATION = os.environ.get("GEMINI_MODEL_LOCATION", "global")

# Debug: Log at module import time
print("[GlobalAdkApp] Module imported")
print(f"[GlobalAdkApp] GEMINI_MODEL_LOCATION = {GEMINI_MODEL_LOCATION}")
print(
    f"[GlobalAdkApp] GOOGLE_CLOUD_LOCATION (at import) = {os.environ.get('GOOGLE_CLOUD_LOCATION', 'NOT SET')}"
)
print(
    f"[GlobalAdkApp] GOOGLE_GENAI_USE_VERTEXAI (at import) = {os.environ.get('GOOGLE_GENAI_USE_VERTEXAI', 'NOT SET')}"
)


class GlobalAdkApp(AdkApp):
    """AdkApp subclass that forces GOOGLE_CLOUD_LOCATION for Gemini 3 models.

    Agent Engine overrides GOOGLE_CLOUD_LOCATION to match the deployment region
    (us-central1). This subclass restores it to the value from GEMINI_MODEL_LOCATION
    (or 'global' by default) after setup, allowing Gemini 3 models to work.

    Usage:
        from app.agent_engine_app import GlobalAdkApp

        app = GlobalAdkApp(
            agent=root_agent,
            enable_tracing=True,
        )
    """

    def set_up(self) -> None:
        """Initialize the app and restore critical env vars for Gemini 3 and Vertex AI."""
        print("[GlobalAdkApp.set_up] Starting set_up...")
        print(
            f"[GlobalAdkApp.set_up] GOOGLE_CLOUD_LOCATION (before vertexai.init) = {os.environ.get('GOOGLE_CLOUD_LOCATION', 'NOT SET')}"
        )
        print(
            f"[GlobalAdkApp.set_up] GOOGLE_GENAI_USE_VERTEXAI (before vertexai.init) = {os.environ.get('GOOGLE_GENAI_USE_VERTEXAI', 'NOT SET')}"
        )

        # Initialize Vertex AI
        vertexai.init()
        print(
            f"[GlobalAdkApp.set_up] GOOGLE_CLOUD_LOCATION (after vertexai.init) = {os.environ.get('GOOGLE_CLOUD_LOCATION', 'NOT SET')}"
        )

        # Parent set_up() is where the GOOGLE_CLOUD_LOCATION override occurs
        super().set_up()
        print(
            f"[GlobalAdkApp.set_up] GOOGLE_CLOUD_LOCATION (after super().set_up) = {os.environ.get('GOOGLE_CLOUD_LOCATION', 'NOT SET')}"
        )

        # Restore the location for Gemini 3 models (from GEMINI_MODEL_LOCATION or default 'global')
        os.environ["GOOGLE_CLOUD_LOCATION"] = GEMINI_MODEL_LOCATION
        print(
            f"[GlobalAdkApp.set_up] GOOGLE_CLOUD_LOCATION (after restore) = {os.environ.get('GOOGLE_CLOUD_LOCATION', 'NOT SET')}"
        )

        # Force-set GOOGLE_GENAI_USE_VERTEXAI=TRUE for video generation
        # This is required for video byte extraction via Vertex AI
        # Agent Engine may not properly propagate env_vars (ADK bug #3208)
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
        print(
            f"[GlobalAdkApp.set_up] GOOGLE_GENAI_USE_VERTEXAI (forced) = {os.environ.get('GOOGLE_GENAI_USE_VERTEXAI', 'NOT SET')}"
        )

        print("[GlobalAdkApp.set_up] set_up complete!")
