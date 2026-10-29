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

import numpy as np
import pytest

from genmedia4commerce.core import video as core_video
from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.testing import mp4, mp4_stub
from genmedia4commerce.eval import motion

WIDTH, HEIGHT, FRAMES = 160, 128, 72
BOX_X, BOX_Y, BOX_W, BOX_H = 48, 16, 64, 88  # the product, on white


def _texture(width: int) -> np.ndarray:
    """Blocky grey texture, never near white (the product's surface)."""
    rng = np.random.default_rng(0)
    cells = rng.integers(40, 200, size=(BOX_H // 8, width // 8 + 1))
    return np.kron(cells.astype(np.uint8), np.ones((8, 8), dtype=np.uint8))


def _encode(frames: list[np.ndarray]) -> bytes:
    return mp4([np.stack([gray] * 3, axis=-1) for gray in frames], fps=24)


def _spin(speeds: list[int]) -> bytes:
    """A product whose surface slides ``speeds[t]`` px per frame (> 0: left,
    clockwise) on a white background."""
    positions = np.concatenate([[0], np.cumsum(speeds)])
    positions -= positions.min()
    texture = _texture(int(positions.max()) + BOX_W)
    frames = []
    for x in positions:
        frame = np.full((HEIGHT, WIDTH), 255, dtype=np.uint8)
        frame[BOX_Y : BOX_Y + BOX_H, BOX_X : BOX_X + BOX_W] = texture[
            :, x : x + BOX_W
        ]
        frames.append(frame)
    return _encode(frames)


def _outline_only() -> bytes:
    """A plain product that narrows and widens: only its outline moves."""
    frames = []
    for t in range(FRAMES):
        half = int(8 + 24 * abs(np.cos(np.pi * t / FRAMES)))
        frame = np.full((HEIGHT, WIDTH), 255, dtype=np.uint8)
        frame[BOX_Y : BOX_Y + BOX_H, 80 - half : 80 + half] = 90
        frames.append(frame)
    return _encode(frames)


class TestCheckRotation:
    def test_surface_moving_left_is_clockwise(self):
        assert motion.check_rotation(_spin([3] * FRAMES)) == "clockwise"

    def test_surface_moving_right_is_anticlockwise(self):
        assert motion.check_rotation(_spin([-3] * FRAMES)) == "anticlockwise"

    def test_a_spin_that_turns_back_is_invalid(self):
        assert motion.check_rotation(_spin([3] * 44 + [-3] * 28)) == "invalid"

    def test_a_brief_wobble_is_not_a_turn_back(self):
        assert motion.check_rotation(_spin([3] * 66 + [-3] * 4)) == "clockwise"

    def test_outline_motion_alone_does_not_make_a_direction(self):
        # What fooled the previous classifier on plain garments.
        assert motion.check_rotation(_outline_only()) == "unknown"

    def test_static_clip_is_unknown(self):
        assert motion.check_rotation(_spin([0] * FRAMES)) == "unknown"

    def test_featureless_clip_is_unknown(self):
        assert motion.check_rotation(mp4_stub(frames=12)) == "unknown"

    def test_unreadable_bytes_are_unknown(self):
        assert motion.check_rotation(b"not a video") == "unknown"

    def test_an_animated_webp_is_read(self):
        # A spin the checks re-encoded (reversed, cut) is an animated WebP.
        clip = core_video.to_webp(_spin([3] * FRAMES))
        assert motion.check_rotation(clip) == "clockwise"
        reversed_ = core_video.reverse(clip)
        assert motion.check_rotation(reversed_) == "anticlockwise"


class TestDetectGlitches:
    def test_glitch_rejects(self, fake_gen):
        fake_gen.json({"is_valid": False, "explanation": "spin reverses at 3s"})
        video = mp4_stub()
        verdict = motion.detect_glitches(video)
        assert not verdict.publish and not verdict.error
        assert verdict.imperfection_count == 1
        assert verdict.description == "spin reverses at 3s"
        call = fake_gen.calls[-1]
        assert call["raw_parts"][0] == (video, {"fps": 2})
        assert call["system"] == motion.GLITCH_PROMPT
        assert call["params"].thinking == "HIGH"

    def test_clean_video_publishes(self, fake_gen):
        fake_gen.json({"is_valid": True, "explanation": "smooth"})
        verdict = motion.detect_glitches(mp4_stub())
        assert verdict.publish and verdict.imperfection_count == 0

    @pytest.mark.parametrize(
        "answer", [GenerationError("timeout"), {"explanation": "no verdict"}]
    )
    def test_evaluator_failure_is_error(self, fake_gen, answer):
        fake_gen.json(answer)
        verdict = motion.detect_glitches(mp4_stub())
        assert verdict.error and not verdict.publish
        assert verdict.description.startswith("glitch check failed")
