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

import io
import itertools

import numpy as np
import pytest
from PIL import Image

from genmedia4commerce.core import video
from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.media import sniff_mime
from genmedia4commerce.core.testing import mp4, mp4_stub, png


def brightness(frame_png: bytes) -> float:
    return float(
        np.asarray(Image.open(io.BytesIO(frame_png)).convert("L")).mean()
    )


def moving_square_clip(n=24, fps=12, still=()):
    """A textured square moving right, except at frame indices in ``still``."""
    rng = np.random.default_rng(0)
    texture = rng.integers(0, 255, (16, 16, 3), dtype=np.uint8)
    frames, x = [], 0
    for i in range(n):
        if i not in still:
            x += 2
        f = np.full((64, 64, 3), 30, dtype=np.uint8)
        f[24:40, x % 48 : x % 48 + 16] = texture
        frames.append(f)
    return mp4(frames, fps=fps)


def test_probe():
    n, fps, duration_ms = video.probe(mp4_stub(frames=24, fps=12))
    assert n == 24
    assert fps == pytest.approx(12)
    assert duration_ms == 2000


def test_extract_frames_all_and_resampled():
    data = mp4_stub(frames=24, fps=12)
    frames = video.extract_frames(data)
    assert len(frames) == 24
    assert frames[0][:8] == b"\x89PNG\r\n\x1a\n"
    assert len(video.extract_frames(data, fps=2)) == 4
    assert len(video.extract_frames(data, fps=6)) == 12
    assert len(video.extract_frames(data, max_frames=5)) == 5


def test_encode_frames_writes_an_animated_webp():
    frames = [png(64, 64, (c, c, c)) for c in (0, 120, 250)]
    data = video.encode_frames(frames, fps=6)
    assert sniff_mime(data) == "image/webp"
    with Image.open(io.BytesIO(data)) as img:
        assert img.n_frames == 3 and img.info["loop"] == 0
    n, fps, _ = video.probe(data)
    assert n == 3 and fps == pytest.approx(6, rel=0.01)  # whole ms a frame
    decoded = [brightness(f) for f in video.extract_frames(data)]
    assert decoded == pytest.approx([0, 120, 250], abs=6)
    with pytest.raises(InputError):
        video.encode_frames([], fps=6)


def test_a_webp_that_starts_still_keeps_its_frames_and_rate():
    # libwebp stores the 3 equal frames as one, 3 frames long.
    shades = (0, 0, 0, 120, 250)
    data = video.encode_frames([png(16, 16, (c, c, c)) for c in shades], fps=12)
    n, fps, _ = video.probe(data)
    assert n == 5 and fps == pytest.approx(12, rel=0.01)
    decoded = [brightness(f) for f in video.extract_frames(data)]
    assert decoded == pytest.approx(list(shades), abs=6)


def test_a_webp_clip_reads_like_the_mp4():
    # libwebp merges a still frame into the one before: it reads back.
    data = moving_square_clip(n=48, fps=12, still={3, 18, 31, 45})
    webp = video.to_webp(data)
    assert sniff_mime(webp) == "image/webp"
    assert video.to_webp(webp) is webp  # never encoded twice
    n, fps, _ = video.probe(webp)
    assert n == 48 and fps == pytest.approx(12, rel=0.01)
    assert len(video.extract_frames(webp, fps=6)) == 24
    assert len(list(video.iter_frames(webp))) == 48
    picks = video.stillest_frames(webp, per_half=2, min_gap_s=1.0)
    assert [i for i, _ in picks] == [3, 18, 31, 45]


def test_reverse_reverses_frame_order():
    data = mp4_stub(frames=12)
    original = video.extract_frames(data)
    backwards = video.reverse(data)
    assert sniff_mime(backwards) == "image/webp"
    reversed_frames = video.extract_frames(backwards)
    assert len(reversed_frames) == len(original)
    assert brightness(original[0]) < brightness(original[-1])
    assert brightness(reversed_frames[0]) == pytest.approx(
        brightness(original[-1]), abs=6
    )
    assert brightness(reversed_frames[-1]) == pytest.approx(
        brightness(original[0]), abs=6
    )
    # A reversed clip (a WebP) is read again: back to the first order.
    again = video.extract_frames(video.reverse(backwards))
    assert brightness(again[0]) == pytest.approx(brightness(original[0]), abs=6)


def test_trim_keeps_range():
    data = mp4_stub(frames=12)
    original = video.extract_frames(data)
    cut = video.trim(data, start_frame=2, end_frame=8)
    assert sniff_mime(cut) == "image/webp"
    trimmed = video.extract_frames(cut)
    assert len(trimmed) == 6
    assert brightness(trimmed[0]) == pytest.approx(
        brightness(original[2]), abs=6
    )
    # A cut clip (a WebP) is cut again.
    twice = video.extract_frames(video.trim(cut, start_frame=1, end_frame=3))
    assert len(twice) == 2
    assert brightness(twice[0]) == pytest.approx(brightness(original[3]), abs=6)
    with pytest.raises(InputError):
        video.trim(data, start_frame=5, end_frame=5)


def test_pick_stable_frames_one_per_window():
    data = moving_square_clip(n=24, fps=12)  # 0.5 s window = 6 frames
    picks = video.pick_stable_frames(data, window_s=0.5)
    assert len(picks) == 4
    indices = [i for i, _ in picks]
    assert indices[0] == 0  # first frame has zero motion by definition
    for w, i in enumerate(indices):
        assert w * 6 <= i < (w + 1) * 6
    assert all(png_bytes[:4] == b"\x89PNG" for _, png_bytes in picks)


def test_pick_stable_frames_prefers_still_frame():
    data = moving_square_clip(n=12, fps=12, still={9})
    picks = video.pick_stable_frames(data, window_s=0.5)
    assert [i for i, _ in picks][1] == 9


def test_stillest_frames_two_per_half_spread_apart():
    # 4 s at 12 fps: halves of 24 frames, picks at least 1 s (12) apart.
    data = moving_square_clip(n=48, fps=12, still={3, 18, 30, 44})
    picks = video.stillest_frames(data, per_half=2, min_gap_s=1.0)
    assert [i for i, _ in picks] == [3, 18, 30, 44]
    assert all(image[:4] == b"\x89PNG" for _, image in picks)


def test_stillest_frames_keep_the_gap_and_skip_the_first_frame():
    # Frames 4 and 6 are both still but closer than the gap: one is kept.
    data = moving_square_clip(n=48, fps=12, still={4, 6, 30, 44})
    indices = [i for i, _ in video.stillest_frames(data, min_gap_s=1.0)]
    assert len(indices) == 4 and 0 not in indices
    assert len({4, 6} & set(indices)) == 1 and {30, 44} <= set(indices)
    assert all(b - a >= 12 for a, b in itertools.pairwise(indices))
