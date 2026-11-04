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

"""``media.fit``, ``media.trim`` and the plain-background check they share
with the spins (``content_mask``, ``crop_to_content``): an image sized or
cropped for a model's input (private to ``media``)."""

import io
import math

import cv2
import numpy as np
from PIL import Image, ImageOps

from genmedia4commerce.core.errors import InputError

__all__ = [
    "content_mask",
    "crop_to_content",
    "fit",
    "has_alpha",
    "open_lazy",
    "trim",
]

_ORIENTATION = 0x0112  # EXIF tag
# A pixel is background within this channel difference (0-255) of the
# border's colour: 12 on a clean border (a white shoe on white keeps its
# toes), up to 20 on a noisy or graded one (a light grey gradient stays out).
TRIM_TOLERANCE = 12
TRIM_TOLERANCE_MAX = 20
# The border is plain when this share of it is within the maximum tolerance
# (a product touching the edges still leaves most of it).
TRIM_PLAIN = 0.6
TRIM_MARGIN = 0.04  # kept around the content, a fraction of its longer side
_FRAME = 0.01  # the background is read on a frame this share of the side
_SPECK = 2e-5  # lone blobs under this share of the picture are noise


def open_lazy(data: bytes) -> Image.Image:
    """The image, header read only; ``InputError`` if unreadable or too large."""
    try:
        return Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise InputError(f"image too large: {exc}") from exc
    except OSError as exc:
        raise InputError(f"unreadable image: {exc}") from exc


def has_alpha(img: Image.Image) -> bool:
    """Whether ``img`` has transparency."""
    return img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info


def _size(
    current: tuple[int, int],
    max_pixels: int | None,
    multiple: int,
    box: tuple[int, int] | None,
    pad: bool,
) -> tuple[int, int]:
    """The size ``fit`` gives an image of size ``current``."""
    w, h = current
    if max_pixels and w * h > max_pixels:
        scale = math.sqrt(max_pixels / (w * h))
        w = max(multiple, int(w * scale) // multiple * multiple)
        h = max(multiple, int(h * scale) // multiple * multiple)
    if box:
        scale = min(box[0] / w, box[1] / h)
        if scale < 1 or pad:
            w = min(box[0], max(1, round(w * scale)))
            h = min(box[1], max(1, round(h * scale)))
    return w, h


def _border_color(pixels: np.ndarray) -> tuple[int, ...]:
    """The median colour of the 1-pixel border of an (h, w, channels) array."""
    edge = np.concatenate([pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]])
    return tuple(int(v) for v in np.median(edge, axis=0))


def _render(
    img: Image.Image,
    size: tuple[int, int],
    canvas: tuple[int, int] | None,
    lossless: bool,
) -> bytes:
    """``img`` resized to ``size``, centred on ``canvas`` if given, encoded."""
    alpha = has_alpha(img)
    out = img.convert("RGBA" if alpha else "RGB")
    if size != out.size:
        out = out.resize(size, Image.Resampling.LANCZOS)
    if canvas:
        padded = Image.new(out.mode, canvas, _border_color(np.asarray(out)))
        padded.paste(
            out, ((canvas[0] - size[0]) // 2, (canvas[1] - size[1]) // 2)
        )
        out = padded
    buf = io.BytesIO()
    icc = img.info.get("icc_profile")  # colours stay as they were
    if alpha or lossless:
        out.save(buf, format="PNG", icc_profile=icc)
    else:
        out.save(buf, format="JPEG", quality=95, subsampling=0, icc_profile=icc)
    return buf.getvalue()


def fit(
    image: bytes,
    *,
    max_pixels: int | None = None,
    multiple: int = 1,
    box: tuple[int, int] | None = None,
    pad: bool = False,
    lossless: bool = False,
) -> bytes:
    """``image`` sized for a model's input, with its EXIF orientation applied.

    ``max_pixels``: scaled down (Lanczos) to at most that many pixels, the
    sides then rounded down to a multiple of ``multiple``. ``box=(w, h)``:
    scaled to fit inside; with ``pad`` also scaled up, then centred on a
    canvas of exactly (w, h) filled with the image's own background colour
    (the median of its border). An upright image already within the limits
    (and exactly ``box`` when padded) is returned unchanged; any other comes
    back as JPEG (quality 95, 4:4:4), or PNG when it has transparency or
    ``lossless`` is set.
    """
    img = open_lazy(image)
    upright = img.getexif().get(_ORIENTATION, 1) == 1
    if not upright:
        img = ImageOps.exif_transpose(img)
    padded = box is not None and pad
    size = _size(img.size, max_pixels, multiple, box, pad)
    if (
        upright
        and size == img.size
        and (box is None or not pad or size == tuple(box))
    ):
        return image
    return _render(img, size, box if padded else None, lossless)


def content_mask(
    img: Image.Image, *, plain: float = TRIM_PLAIN
) -> tuple[np.ndarray, tuple] | None:
    """What is not background (bool, height x width) and the background
    colour, when the background is plain; None when it is busy.

    With transparency the background is the transparent pixels (white).
    Otherwise it is the median colour of a frame (``_FRAME`` of the shorter
    side, at least 2 px), plain when ``plain`` (``TRIM_PLAIN``) of the frame
    is within ``TRIM_TOLERANCE_MAX`` of it. The tolerance follows the frame's own
    spread (``TRIM_TOLERANCE`` up to ``TRIM_TOLERANCE_MAX``). The content is
    every pixel further from the colour, lone specks left out (blobs under
    ``_SPECK`` of the picture: JPEG noise, dust); thin parts joined to the
    product (a chain, a strap) stay.
    """
    if has_alpha(img):
        alpha = np.asarray(img.convert("RGBA").getchannel("A"))
        if alpha.min() < 255:
            return _no_specks(alpha > TRIM_TOLERANCE), (255, 255, 255)
    rgb = np.asarray(img.convert("RGB"))
    h, w = rgb.shape[:2]
    k = max(2, round(_FRAME * min(h, w)))
    sides = [rgb[:k], rgb[-k:], rgb[:, :k], rgb[:, -k:]]
    frame = np.concatenate([side.reshape(-1, 3) for side in sides])
    bg = np.median(frame, axis=0).astype(np.uint8)
    off = np.abs(frame.astype(np.int16) - bg).max(axis=1)
    calm = off[off <= TRIM_TOLERANCE_MAX]
    if calm.size < plain * off.size:
        return None
    spread = 2 * np.percentile(calm, 95) + 4
    tolerance = np.clip(spread, TRIM_TOLERANCE, TRIM_TOLERANCE_MAX)
    diff = cv2.absdiff(rgb, np.full_like(rgb, bg)).max(axis=2)
    return _no_specks(diff > tolerance), tuple(int(v) for v in bg)


def _no_specks(mask: np.ndarray) -> np.ndarray:
    _, labels, stats, _ = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8), connectivity=8
    )
    small = max(16, _SPECK * mask.size)
    keep = np.flatnonzero(stats[:, cv2.CC_STAT_AREA] >= small)
    return np.isin(labels, keep[keep > 0])


def _content_box(img: Image.Image) -> tuple[int, int, int, int] | None:
    """The content's box plus ``TRIM_MARGIN``; None on a busy background,
    with no content, or when nothing would be cut."""
    found = content_mask(img)
    if found is None or not found[0].any():
        return None
    ys, xs = np.nonzero(found[0])
    x0, y0, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    m = TRIM_MARGIN * max(x1 - x0, y1 - y0)
    box = (
        max(0, round(x0 - m)),
        max(0, round(y0 - m)),
        min(img.width, round(x1 + m)),
        min(img.height, round(y1 + m)),
    )
    return None if box == (0, 0, *img.size) else box


def crop_to_content(img: Image.Image) -> Image.Image:
    """``img`` cropped to its content when it sits on a plain background
    (``content_mask``), else ``img`` itself."""
    box = _content_box(img)
    return img if box is None else img.crop(box)


def trim(image: bytes) -> bytes:
    """``image`` cropped to its content when it sits on a plain background.

    Computed at full resolution so thin parts (a chain, a strap) count. The
    original bytes come back when the border is busy or nothing would go, so
    trimming twice changes nothing; otherwise a PNG, EXIF orientation applied
    and transparency kept.
    """
    img = ImageOps.exif_transpose(open_lazy(image))
    box = _content_box(img)
    if box is None:
        return image
    out = img.crop(box).convert("RGBA" if has_alpha(img) else "RGB")
    buf = io.BytesIO()
    out.save(
        buf,
        format="PNG",
        compress_level=1,
        icc_profile=img.info.get("icc_profile"),
    )
    return buf.getvalue()
