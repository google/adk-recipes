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

"""Fixtures for the spin workflow tests: synthetic media, mocked eval/media."""

import io

import numpy as np
import pytest
from PIL import Image

from genmedia4commerce.core import media
from genmedia4commerce.core.testing import mp4, png
from genmedia4commerce.eval import motion, video_fidelity
from genmedia4commerce.workflows.spin import product_check, product_refs

_GUARDED = {
    media: ("upscale",),
    motion: ("check_rotation", "detect_glitches"),
    video_fidelity: ("evaluate_video_fidelity",),
    product_check: ("label_frames",),
}


@pytest.fixture(autouse=True)
def _no_real_media_or_eval(monkeypatch):
    """Fail loudly if a test reaches an image upscale or an eval.

    Tests that need one of these patch it themselves (after this fixture).
    """
    for module, names in _GUARDED.items():
        for name in names:

            def guard(*_args, _name=name, **_kwargs):
                pytest.fail(f"{_name} is not mocked in this test")

            monkeypatch.setattr(module, name, guard)


def _frame(value: int, size: int = 32) -> np.ndarray:
    """A frame with a distinct pattern per ``value`` (SSIM can tell them apart)."""
    rng = np.random.default_rng(value)
    return rng.integers(0, 256, (size, size, 3), dtype=np.uint8)


def clip(values: list[int], *, fps: int = 24) -> bytes:
    """Encode one patterned frame per value as an MP4 (the video model's)."""
    return mp4([_frame(v) for v in values], fps=fps)


def frame_png(value: int) -> bytes:
    """The PNG of ``_frame(value)``."""
    buf = io.BytesIO()
    Image.fromarray(_frame(value)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def images() -> list[bytes]:
    """Three distinct product photos."""
    return [png(color=(10 * i, 20, 30)) for i in range(1, 4)]


@pytest.fixture
def no_media(monkeypatch):
    """Pass-through one-item cut-out (the references are never upscaled)."""
    calls: dict[str, list] = {"one_item": []}

    def one_item(data, name=None):
        calls["one_item"].append((data, name))
        return data

    monkeypatch.setattr(product_refs, "one_item", one_item)
    return calls
