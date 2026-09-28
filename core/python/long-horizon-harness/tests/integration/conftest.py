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

"""Shared fixtures for tests that drive a real model."""

from __future__ import annotations

import functools
import os

import pytest

from app.infrastructure.env import env_flag


@functools.cache
def _has_adc() -> bool:
    try:
        import google.auth

        google.auth.default()
    except Exception:
        return False
    return True


@pytest.fixture
def requires_model() -> None:
    """Skip unless a real model is reachable: Vertex needs ADC plus
    GOOGLE_GENAI_USE_VERTEXAI, otherwise google-genai falls back to the Gemini
    API and fails on the missing key."""
    if os.environ.get("GOOGLE_API_KEY"):
        return
    if not env_flag("GOOGLE_GENAI_USE_VERTEXAI"):
        pytest.skip(
            "GOOGLE_GENAI_USE_VERTEXAI is not set; skipping model-backed test"
        )
    if not _has_adc():
        pytest.skip(
            "no application default credentials; skipping model-backed test"
        )
