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

"""The mirror twin of a shoe: the same shoe for the other foot.

A plain mirror copies text and logos backwards, and the video model copies
them; the image model redraws them reading correctly. It often returns the
input unchanged instead, so a local check compares its output with the plain
mirror and with the input, and it is asked again, at most ``TRIES`` times.
On 7 hard photos, 3 runs each: all flipped within 3 tries, the lettering
right in all (logos sometimes drawn a little larger); 30-50 s a try. Licensed
characters are refused: no twin then.
"""

import logging
from collections.abc import Sequence

import numpy as np
from PIL import Image, ImageOps

from genmedia4commerce.core import gen
from genmedia4commerce.core.retry import ContextPool
from genmedia4commerce.workflows.spin import shoe_split as split
from genmedia4commerce.workflows.spin.shoe_prompts import twin_prompt

__all__ = ["TRIES", "flipped", "twin", "twins"]

logger = logging.getLogger(__name__)

TRIES = 3
_PARAMS = gen.ImageParams(
    aspect_ratio="auto", image_size="2K", thinking="MEDIUM"
)
_SIDE = 256  # compared at this size, in grey
_MARGIN = 0.1  # closer to the plain mirror than to the input by this much


def _grey(img: Image.Image) -> np.ndarray:
    arr = np.asarray(img.convert("L").resize((_SIDE, _SIDE)), dtype=float)
    return arr - arr.mean()


def _similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Correlation of two mean-centred grey images, -1 to 1."""
    return float(
        (a * b).sum() / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-9)
    )


def flipped(output: bytes, image: bytes) -> bool:
    """Whether ``output`` looks like ``image`` mirrored, not like ``image``."""
    src = split.open_rgb(image)
    out = _grey(split.open_rgb(output))
    to_mirror = _similarity(out, _grey(ImageOps.mirror(src)))
    return to_mirror > _similarity(out, _grey(src)) + _MARGIN


def twin(image: bytes, view: str, mirrored_view: str) -> bytes | None:
    """The mirror twin (PNG) of a shoe seen from ``view``; None when no try
    flips it or the image model refuses the photo."""
    prompt = twin_prompt(view, mirrored_view)
    for attempt in range(1, TRIES + 1):
        try:
            out = gen.generate_image([prompt, image], params=_PARAMS)
        except Exception as exc:
            logger.warning(f"mirror twin, try {attempt}: {exc}")
            if "PROHIBITED" in str(exc):  # refused again on every retry
                return None
            continue
        if flipped(out, image):
            return split.png(split.open_rgb(out))
        logger.info(f"mirror twin, try {attempt}: not flipped")
    return None


def twins(jobs: Sequence[tuple[bytes, str, str]]) -> list[bytes | None]:
    """``twin`` of every (image, view, mirrored_view), all at once."""
    with ContextPool(max_workers=max(1, len(jobs))) as pool:
        futures = [pool.submit(twin, *job) for job in jobs]
        return [f.result() for f in futures]
