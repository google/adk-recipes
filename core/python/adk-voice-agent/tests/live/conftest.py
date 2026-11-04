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

"""Fixtures for live tests (`uv run pytest -m live tests/live`)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import dotenv_values

_PROJECT_DOTENV = Path(__file__).resolve().parents[2] / ".env"


@pytest.fixture(autouse=True)
def _block_outbound_network() -> Iterator[None]:
    # Overrides the root conftest's network block so live tests can reach GCP.
    yield


@pytest.fixture(autouse=True)
def _real_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    if _PROJECT_DOTENV.exists():
        for key, value in dotenv_values(_PROJECT_DOTENV).items():
            if value is not None and key not in os.environ:
                monkeypatch.setenv(key, value)
    if not os.getenv("GOOGLE_CLOUD_PROJECT"):
        pytest.skip("Live tests need GOOGLE_CLOUD_PROJECT (set it in .env).")
