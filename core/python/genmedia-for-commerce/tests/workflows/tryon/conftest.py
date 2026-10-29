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

"""Fixtures for the try-on workflow tests (synthetic images only)."""

import pytest

from genmedia4commerce.core.testing import png
from genmedia4commerce.workflows.tryon import worn

REAL_NORMALISE = worn.normalise  # the root conftest turns it off


@pytest.fixture
def mannequin_on(monkeypatch):
    """Worn photos converted to a mannequin (``worn.normalise``)."""
    monkeypatch.setattr(worn, "normalise", REAL_NORMALISE)


@pytest.fixture
def images() -> list[bytes]:
    """Nine distinct solid-colour PNGs (product uploads)."""
    return [png(color=(20 + 25 * i, 40, 60)) for i in range(9)]


@pytest.fixture
def crops() -> list[bytes]:
    """Three distinct reference-model crops (top, middle, bottom)."""
    return [png(color=(10, 200, 10 + 60 * i)) for i in range(3)]


@pytest.fixture
def person() -> bytes:
    """The wearer photo."""
    return png(96, 128, color=(230, 200, 180))
