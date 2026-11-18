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

"""Video frame helpers (synchronous, one implementation of each).

The video model's MP4 is decoded by OpenCV; an animated WebP (what
``encode_frames`` writes, the 360 spin's format) by Pillow, as OpenCV cannot
read one.
"""

import contextlib
import io
import math
import os
import tempfile
from collections.abc import Iterator, Sequence

import cv2
import numpy as np
from PIL import Image, ImageSequence

from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.media import sniff_mime

__all__ = [
    "encode_frames",
    "extract_frames",
    "iter_frames",
    "pick_stable_frames",
    "probe",
    "reverse",
    "stillest_frames",
    "to_webp",
    "trim",
]

_DEFAULT_FPS = 24.0
# Animated WebP (libwebp): at quality 75 about the size of the H.264 MP4.
_WEBP = {"quality": 75, "method": 4, "loop": 0}


def _read(cap: cv2.VideoCapture) -> Iterator[np.ndarray]:
    while True:
        ok, bgr = cap.read()
        if not ok:
            return
        yield bgr


def _durations(webp: bytes) -> list[int]:
    """The duration (ms) of each frame of an animated WebP, read from its
    ANMF chunks; none for a still WebP."""
    durations, pos = [], 12  # after "RIFF", the size and "WEBP"
    while pos + 8 <= len(webp):
        size = int.from_bytes(webp[pos + 4 : pos + 8], "little")
        if webp[pos : pos + 4] == b"ANMF":  # x, y, w, h, then the duration
            durations.append(
                int.from_bytes(webp[pos + 20 : pos + 23], "little")
            )
        pos += 8 + size + size % 2
    return durations


def _webp_frames(img: Image.Image, repeats: list[int]) -> Iterator[np.ndarray]:
    for frame, repeat in zip(
        ImageSequence.Iterator(img), repeats, strict=False
    ):
        bgr = cv2.cvtColor(np.asarray(frame.convert("RGB")), cv2.COLOR_RGB2BGR)
        for _ in range(repeat):
            yield bgr


@contextlib.contextmanager
def _decoded(
    video: bytes,
) -> Iterator[tuple[float, int, Iterator[np.ndarray]]]:
    """The clip's fps, frame count and frames (BGR, decoded one at a time).

    libwebp stores a frame equal to the one before as a longer duration of
    that one. A WebP is read at the rate of its shortest frame, each frame
    repeated for its duration, so what ``encode_frames`` wrote reads back
    frame for frame.
    """
    if sniff_mime(video) == "image/webp":
        with Image.open(io.BytesIO(video)) as img:
            durations = _durations(video) or [0] * img.n_frames
            base = min((d for d in durations if d > 0), default=0)
            repeats = [
                max(1, round(d / base)) if base else 1 for d in durations
            ]
            fps = 1000 / base if base else _DEFAULT_FPS
            yield fps, sum(repeats), _webp_frames(img, repeats)
        return
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(video)
        path = f.name
    cap = cv2.VideoCapture(path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or _DEFAULT_FPS
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        yield fps, n_frames, _read(cap)
    finally:
        cap.release()
        os.unlink(path)


def _png(bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return buf.tobytes()


def _gray_small(bgr: np.ndarray, max_side: int = 256) -> np.ndarray:
    h, w = bgr.shape[:2]
    scale = max_side / max(h, w)
    if scale < 1.0:
        bgr = cv2.resize(
            bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA
        )
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def _sharpness(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def probe(video: bytes) -> tuple[int, float, int]:
    """Return ``(n_frames, fps, duration_ms)``."""
    with _decoded(video) as (fps, n_frames, _):
        pass
    return n_frames, fps, round(n_frames / fps * 1000)


def iter_frames(video: bytes) -> Iterator[np.ndarray]:
    """Decode the clip's frames (BGR arrays) one at a time."""
    with _decoded(video) as (_, _, frames):
        yield from frames


def extract_frames(
    video: bytes, *, fps: float | None = None, max_frames: int | None = None
) -> list[bytes]:
    """Decode frames as PNG bytes, optionally resampled to ``fps``."""
    frames: list[bytes] = []
    with _decoded(video) as (source_fps, _, decoded):
        step = source_fps / fps if fps else 1.0
        next_pick = 0.0
        for index, bgr in enumerate(decoded):
            if index >= next_pick - 1e-9:
                frames.append(_png(bgr))
                next_pick += step
                if len(frames) == max_frames:
                    break
    return frames


def encode_frames(frames: Sequence[bytes], *, fps: float) -> bytes:
    """Encode image frames into an animated WebP that loops."""
    if not frames:
        raise InputError("encode_frames needs at least one frame")
    images = [Image.open(io.BytesIO(f)).convert("RGB") for f in frames]
    buf = io.BytesIO()
    images[0].save(
        buf,
        format="WEBP",
        save_all=True,
        append_images=images[1:],
        duration=round(1000 / fps),
        **_WEBP,
    )
    return buf.getvalue()


def to_webp(video: bytes) -> bytes:
    """The clip as an animated WebP (one that already is, as it is)."""
    if sniff_mime(video) == "image/webp":
        return video
    _, fps, _ = probe(video)
    return encode_frames(extract_frames(video), fps=fps)


def reverse(video: bytes) -> bytes:
    """Return the video played backwards (same fps)."""
    _, fps, _ = probe(video)
    return encode_frames(extract_frames(video)[::-1], fps=fps)


def trim(video: bytes, *, start_frame: int, end_frame: int) -> bytes:
    """Keep frames ``start_frame`` (inclusive) to ``end_frame`` (exclusive)."""
    _, fps, _ = probe(video)
    frames = extract_frames(video)[start_frame:end_frame]
    if not frames:
        raise InputError(f"trim [{start_frame}:{end_frame}] leaves no frames")
    return encode_frames(frames, fps=fps)


def pick_stable_frames(
    video: bytes, *, window_s: float = 0.5
) -> list[tuple[int, bytes]]:
    """Return ``(frame_index, png)`` for the stillest frame of each window.

    Motion is the mean absolute difference to the previous frame; ties (within
    5% of the window minimum) go to the sharpest frame (Laplacian variance).
    """
    picks: list[tuple[int, bytes]] = []
    window: list[tuple[int, float, float, np.ndarray]] = []

    def flush() -> None:
        if not window:
            return
        tolerance = min(w[1] for w in window) * 1.05 + 1e-6
        best = max((w for w in window if w[1] <= tolerance), key=lambda w: w[2])
        picks.append((best[0], _png(best[3])))
        window.clear()

    with _decoded(video) as (fps, _, decoded):
        size = max(1, round(fps * window_s))
        previous = None
        for index, bgr in enumerate(decoded):
            gray = _gray_small(bgr)
            motion = (
                0.0
                if previous is None
                else float(np.mean(np.abs(gray.astype(np.int16) - previous)))
            )
            previous = gray.astype(np.int16)
            if index % size == 0:
                flush()
            window.append((index, motion, _sharpness(gray), bgr))
    flush()
    return picks


def _motions(video: bytes) -> tuple[list[float], float]:
    """Motion of every frame (mean absolute grey difference to the previous
    one; infinite for the first, which has none) and the fps."""
    motions: list[float] = []
    with _decoded(video) as (fps, _, decoded):
        previous = None
        for bgr in decoded:
            gray = _gray_small(bgr).astype(np.int16)
            motions.append(
                math.inf
                if previous is None
                else float(np.mean(np.abs(gray - previous)))
            )
            previous = gray
    return motions, fps


def _frames_at(video: bytes, indices: set[int]) -> dict[int, bytes]:
    """PNG of each frame in ``indices``, decoded up to the last one."""
    found: dict[int, bytes] = {}
    last = max(indices, default=-1)
    with _decoded(video) as (_, _, decoded):
        for index, bgr in enumerate(decoded):
            if index > last:
                break
            if index in indices:
                found[index] = _png(bgr)
    return found


def stillest_frames(
    video: bytes, *, per_half: int = 2, min_gap_s: float = 1.5
) -> list[tuple[int, bytes]]:
    """Return ``(frame_index, png)`` of the stillest frames, by index.

    In each half of the clip, the ``per_half`` frames with the lowest motion
    (mean absolute grey difference to the previous frame), each at least
    ``min_gap_s`` from every other pick: still frames are sharp, and the
    halves spread the picks over the clip. The clip is decoded twice (motion,
    then the picks) so only the picks are held in memory.
    """
    motions, fps = _motions(video)
    n, gap = len(motions), min_gap_s * fps
    picks: list[int] = []
    for half in (range(n // 2), range(n // 2, n)):
        chosen = 0
        for i in sorted(half, key=motions.__getitem__):
            if chosen == per_half or math.isinf(motions[i]):
                break
            if all(abs(i - j) >= gap for j in picks):
                picks.append(i)
                chosen += 1
    images = _frames_at(video, set(picks))
    return [(i, images[i]) for i in sorted(picks) if i in images]
