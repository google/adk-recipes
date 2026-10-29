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

"""Single shoes cut from a photo of several: masks, checks, cuts, trims.

The judge model draws one polygon per shoe (``ask_masks``); ``quality`` says
whether each fits its box and covers its shoe. ``grow`` widens each mask a
little, never into another, and ``cut_keep`` crops one shoe with its real
background and shadow, repainting only the other shoes.
"""

import io

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps

from genmedia4commerce.core import gen, media
from genmedia4commerce.core.models import get_models
from genmedia4commerce.workflows.spin.shoe_prompts import MASK_ASK, MASK_SCHEMA

__all__ = [
    "ask_masks",
    "bad",
    "cut_keep",
    "decode",
    "grow",
    "iou",
    "open_rgb",
    "png",
    "quality",
    "smooth_fill",
    "to_masks",
]

FIT_MIN = 0.70  # a polygon's box must overlap the model's box this much
COVER_MIN = 0.80  # and cover this share of the shoe pixels in that box
GROW = 0.05  # a mask grows this share of its shoe's long side
MASK_SIDE = 1024  # the photo is sent at most this size for the masks


def open_rgb(image: bytes) -> Image.Image:
    """The image upright (EXIF orientation applied), RGB."""
    img = Image.open(io.BytesIO(image))
    return ImageOps.exif_transpose(img).convert("RGB")


def png(img: Image.Image) -> bytes:
    """``img`` as PNG."""
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def _border(arr: np.ndarray, width: int) -> np.ndarray:
    """The pixels of the ``width``-pixel frame of ``arr``, one per row."""
    return np.concatenate(
        [
            arr[:width].reshape(-1, 3),
            arr[-width:].reshape(-1, 3),
            arr[:, :width].reshape(-1, 3),
            arr[:, -width:].reshape(-1, 3),
        ]
    )


def iou(a, b) -> float:
    """Intersection over union of two (x0, y0, x1, y1) boxes."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1])
    union -= inter
    return inter / union if union > 0 else 0.0


def _reading(points, order: str, rel: str, box, width: int, height: int):
    """The polygon in pixels for one reading of its points."""
    poly = []
    for p in points:
        a, b = (p[0], p[1]) if order == "xy" else (p[1], p[0])
        if rel == "image":
            poly.append((a / 1000 * width, b / 1000 * height))
        else:
            poly.append(
                (
                    box[0] + a / 1000 * (box[2] - box[0]),
                    box[1] + b / 1000 * (box[3] - box[1]),
                )
            )
    return poly


def decode(item: dict, width: int, height: int) -> tuple[list | None, float]:
    """The polygon in pixels and its fit: of the 4 readings (x/y order,
    image- or box-relative), the one whose bbox best fits ``box_2d``.

    The documented reading (x, y of the image) wins ties. ``(None, 0.0)``
    without a box or with fewer than 3 points.
    """
    if not isinstance(item, dict):
        return None, 0.0
    box_2d = item.get("box_2d")
    if not (isinstance(box_2d, list) and len(box_2d) == 4):
        return None, 0.0
    y0, x0, y1, x1 = [v / 1000 for v in box_2d]
    box = (x0 * width, y0 * height, x1 * width, y1 * height)
    points = [
        p for p in item.get("mask") or [] if isinstance(p, list) and len(p) == 2
    ]
    if len(points) < 3:
        return None, 0.0
    best, fit = None, -1.0
    for order in ("xy", "yx"):
        for rel in ("image", "box"):
            poly = _reading(points, order, rel, box, width, height)
            xs, ys = [q[0] for q in poly], [q[1] for q in poly]
            score = iou((min(xs), min(ys), max(xs), max(ys)), box)
            score -= 0.01 if rel == "box" else 0
            if score > fit:
                best, fit = poly, score
    return best, fit


def ask_masks(image: bytes, size: tuple[int, int]) -> list[dict]:
    """One mask call; per shoe ``poly`` and ``box`` in pixels of ``size``
    (the upright photo's width, height), the polygon's ``fit`` and the
    shoe's ``view`` (``missing`` when the answer has none)."""
    params = gen.TextParams(
        model=get_models().judge,
        thinking="MEDIUM",
        max_output_tokens=65535,
        deadline_s=240,
    )
    raw = gen.generate_json(
        [MASK_ASK, media.resize_max(image, MASK_SIDE)],
        schema=MASK_SCHEMA,
        params=params,
    )
    width, height = size
    boxes = raw.get("boxes") if isinstance(raw, dict) else None
    items = []
    for entry in boxes or []:
        poly, fit = decode(entry, width, height)
        if not poly:
            continue
        y0, x0, y1, x1 = entry["box_2d"]
        box = [x0 * width / 1000, y0 * height / 1000]
        box += [x1 * width / 1000, y1 * height / 1000]
        view = entry.get("view") or "missing"
        items.append(
            {"poly": poly, "box": box, "fit": round(fit, 2), "view": view}
        )
    return items


def to_masks(items: list[dict], width: int, height: int) -> list[np.ndarray]:
    """Each item's polygon filled, as a boolean (height, width) mask."""
    masks = []
    for item in items:
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).polygon([tuple(p) for p in item["poly"]], fill=255)
        masks.append(np.array(mask) > 0)
    return masks


def quality(arr: np.ndarray, items: list[dict], masks: list[np.ndarray]):
    """Per mask: (fit, coverage).

    Coverage: the share of the shoe pixels inside the item's box (pixels
    differing from the border's background by more than 25, minus the
    other masks) that the mask covers.
    """
    a = arr.astype(int)
    background = np.median(_border(a, 10), axis=0)
    fg = np.abs(a - background).max(axis=2) > 25
    scores = []
    for k, (item, mask) in enumerate(zip(items, masks, strict=True)):
        if not mask.any():
            scores.append((0.0, 0.0))
            continue
        x0, y0, x1, y1 = (max(0, int(v)) for v in item["box"])
        box = np.zeros_like(mask)
        box[y0 : y1 + 1, x0 : x1 + 1] = True
        others = np.zeros_like(mask)
        for j, other in enumerate(masks):
            if j != k:
                others |= other
        want = fg & box & ~others
        cover = float((mask & want).sum() / max(1, want.sum()))
        scores.append((float(item.get("fit", 1.0)), cover))
    return scores


def bad(scores) -> list[int]:
    """The masks failing the fit or the coverage threshold."""
    return [
        k
        for k, (fit, cover) in enumerate(scores)
        if fit < FIT_MIN or cover < COVER_MIN
    ]


def _distance(mask: np.ndarray) -> np.ndarray:
    """Euclidean distance of every pixel to the mask (0 inside)."""
    outside = (~mask).astype(np.uint8)
    return cv2.distanceTransform(outside, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)


def grow(masks: list[np.ndarray]) -> list[np.ndarray]:
    """Each mask grown by ``GROW`` x its long side, never into another mask;
    a pixel both could reach goes to the nearer one. Masks must be
    non-empty."""
    n = len(masks)
    dist = [_distance(m) for m in masks]
    reach = []
    for m, d in zip(masks, dist, strict=True):
        ys, xs = np.nonzero(m)
        reach.append(d <= GROW * max(np.ptp(ys) + 1, np.ptp(xs) + 1))
    out = []
    for i in range(n):
        g = reach[i].copy()
        for j in range(n):
            if j != i:
                g &= ~masks[j] & ~(reach[j] & (dist[j] < dist[i]))
        out.append(masks[i] | g)
    return out


def smooth_fill(img: np.ndarray, clean: np.ndarray) -> np.ndarray:
    """A smooth background estimate from the ``clean`` pixels (float RGB).

    Far from any clean pixel it falls back to their median colour, so a
    large hole never fills with black.
    """
    height, width = clean.shape
    f = 8
    small_size = (max(1, width // f), max(1, height // f))
    small = cv2.resize(
        img.astype(np.float32), small_size, interpolation=cv2.INTER_AREA
    )
    w = cv2.resize(
        clean.astype(np.float32), small_size, interpolation=cv2.INTER_AREA
    )
    sigma = 0.06 * max(width, height) / f
    num = cv2.GaussianBlur(small * w[..., None], (0, 0), sigma)
    den = cv2.GaussianBlur(w, (0, 0), sigma)[..., None]
    plain = (
        np.median(img[clean], axis=0)
        if clean.any()
        else np.array([245, 245, 245])
    )
    trust = np.clip(den / 0.05, 0, 1)  # little clean background nearby
    model = (num / np.maximum(den, 1e-6)) * trust + plain * (1 - trust)
    return cv2.resize(model, (width, height), interpolation=cv2.INTER_CUBIC)


def _hole(masks, grown, k: int) -> np.ndarray:
    """The other shoes, each dilated by max(9 px, 4 % of its size), off ours."""
    height, width = masks[k].shape
    ours = cv2.dilate(grown[k].astype(np.uint8), np.ones((9, 9), np.uint8))
    hole = np.zeros((height, width), bool)
    for j, mask in enumerate(masks):
        if j == k:
            continue
        ys, xs = np.nonzero(mask)
        r = max(9, int(0.04 * max(np.ptp(ys), np.ptp(xs))))
        disc = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)
        )
        hole |= cv2.dilate(mask.astype(np.uint8), disc).astype(bool)
    return hole & ~ours.astype(bool)


def cut_keep(arr: np.ndarray, masks, grown, k: int) -> Image.Image:
    """Shoe ``k`` cropped around its grown mask (+4 %), keeping the original
    pixels (its own shadow, the real background); only the other shoes are
    repainted with the smooth background (feathered edge)."""
    height, width = arr.shape[:2]
    hole = _hole(masks, grown, k)
    out = arr.copy()
    if hole.any():
        background = np.median(_border(arr, 8).astype(float), axis=0)
        anything = np.abs(arr.astype(int) - background).max(axis=2) > 8
        near = cv2.dilate(
            anything.astype(np.uint8), np.ones((31, 31), np.uint8)
        )
        model = smooth_fill(arr, ~near.astype(bool))
        alpha = hole.astype(np.float32)
        alpha = np.maximum(cv2.GaussianBlur(alpha, (0, 0), 4), alpha)[..., None]
        out = (arr * (1 - alpha) + model * alpha).clip(0, 255).astype(np.uint8)
    ys, xs = np.nonzero(grown[k])
    pad = int(0.04 * max(np.ptp(ys), np.ptp(xs)))
    return Image.fromarray(
        out[
            max(0, ys.min() - pad) : min(height, ys.max() + pad),
            max(0, xs.min() - pad) : min(width, xs.max() + pad),
        ]
    )
