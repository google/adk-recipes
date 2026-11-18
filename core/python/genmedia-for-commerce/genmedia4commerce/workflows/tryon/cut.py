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

"""The box call (``reference``): one detector call per photo.

The judge model at medium thinking (about three times faster and steadier
than the text model on it) boxes, in one photo, each product it is told
(category and name, from the classifier), by its index, and, in the look
photo, a top and a bottom half (chest to just below the waist, just above
the waist to the feet; the bottom one only when the lowest hem on the legs
reaches the shoes: with a mini skirt or shorts it would show mostly the
model's bare legs). Each box becomes a crop; a product it does not box gets
none (logged, ``degraded``), never another product's.
"""

import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import cast

from PIL import Image

from genmedia4commerce.core import degraded, gen, media, trace
from genmedia4commerce.core.errors import GenerationError
from genmedia4commerce.core.gen import TextParams
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.types import Category

__all__ = ["Boxed", "box_items"]

logger = logging.getLogger(__name__)

Box = tuple[int, int, int, int]
Asked = tuple[Category, str]  # a product to box: its category and name

_DETECTOR_KEYS = {"top": "upper_body", "bottom": "lower_body"}  # band: key
DETECT_TRIES = 2  # an invalid answer is asked again once
ITEM_CONTEXT = 0.03  # a product crop adds this x its box on each side
MIN_ITEM_SIDE = 150  # smaller product crops (a ring, an earring) are skipped
HALF_IOU = 0.7  # a product boxed this close to a half is it
HALF_MARGIN = 30  # and its box fits inside the half within this (0-1000)
# Half: the products it can be (a dress or long coat runs past the bottom)
_HALF_OF: dict[str, tuple[Category, ...]] = {
    "top": ("top", "full_body_outer"),  # upper-body garments
    "bottom": ("bottom",),
}

_BOXES = "Return boxes as [ymin, xmin, ymax, xmax] normalised to 0-1000:\n"
_BANDS_PROMPT = (
    f"You see a full-body photo of a fashion model. {_BOXES}"
    '- "upper_body": from just below the chin to just below the waist, head '
    "excluded, the whole torso and arms.\n"
    '- "lower_body": from just above the waist to below the feet, shoes '
    "included. Return it only when the hem of the lowest garment on the "
    "legs (the bottom, a dress or a long coat) reaches the shoes or boots, "
    "with at most a hand's width of bare leg between them; tall boots count "
    "as shoes (a mini skirt over knee boots reaches them). Otherwise return "
    '"lower_body": [].\n'
)
_PHOTO_PROMPT = f"You see a fashion photo. {_BOXES}"
_ITEMS_PROMPT = (
    '- "items": one entry per product listed below, by its number: '
    '{{"index": its number, "box": the whole product as visible (shoes: '
    'one box around both)}}; "box": [] when the product is not in the '
    "photo.\n{products}\n"
)

# The answer's shape: the model writes valid JSON (free, it once dropped a
# box's closing bracket in 2 answers of 6, and the look was lost).
_BOX = {"type": "ARRAY", "items": {"type": "INTEGER"}}
_ITEM = {
    "type": "OBJECT",
    "properties": {"index": {"type": "INTEGER"}, "box": _BOX},
    "required": ["index", "box"],
    "propertyOrdering": ["index", "box"],
}
_BANDS = {"upper_body": _BOX, "lower_body": _BOX}
_ITEMS = {"items": {"type": "ARRAY", "items": _ITEM}}


def _schema(properties: dict) -> dict:
    keys = list(properties)
    return {
        "type": "OBJECT",
        "properties": properties,
        "required": keys,
        "propertyOrdering": keys,
    }


@dataclass
class Boxed:
    """What ``box_items`` cuts from one photo."""

    bands: dict[str, bytes]  # top / bottom: the look photo's halves
    crops: list[bytes | None]  # one per product asked; None: no crop
    boxes: list[Box | None]  # each product's box in px (no margin)
    # the indexes of the upper products boxed like the top half, of the
    # bottoms boxed like the bottom half (``HALF_IOU``: the same picture
    # twice), the largest first
    top_half: list[int] = field(default_factory=list)
    bottom_half: list[int] = field(default_factory=list)


def _prompt(items: Sequence[Asked], bands: bool) -> tuple[str, dict]:
    """The detector's prompt and schema: the halves with ``bands``, then
    the products of ``items``, numbered from 0."""
    text, keys, properties = _PHOTO_PROMPT, [], {}
    if bands:
        text, properties = _BANDS_PROMPT, dict(_BANDS)
        keys.append('"upper_body": [...], "lower_body": [...]')
    if items:
        listed = "\n".join(f'{k}: {c} "{n}"' for k, (c, n) in enumerate(items))
        text += _ITEMS_PROMPT.format(products=listed)
        keys.append('"items": [...]')
        properties |= _ITEMS
    return f"{text}Return ONLY {{{', '.join(keys)}}}.", _schema(properties)


def _size(image: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(image)).size


def _pixel_box(raw, width: int, height: int) -> Box | None:
    """Convert a normalised [ymin, xmin, ymax, xmax] box; None if invalid."""
    if not (isinstance(raw, list) and len(raw) == 4):
        return None
    if not all(isinstance(v, int | float) for v in raw):
        return None
    ymin, xmin, ymax, xmax = raw
    if not (0 <= ymin < ymax <= 1000 and 0 <= xmin < xmax <= 1000):
        return None
    return (
        round(xmin * width / 1000),
        round(ymin * height / 1000),
        round(xmax * width / 1000),
        round(ymax * height / 1000),
    )


def _found(raw, items: Sequence[Asked], size: tuple[int, int]) -> list:
    """Each product's box in px by its index (the first valid one); None
    for a product with no valid box (logged, ``degraded``)."""
    found: dict[int, Box] = {}
    entries = raw.get("items") if isinstance(raw.get("items"), list) else []
    for entry in (e for e in entries if isinstance(e, dict)):
        k, box = entry.get("index"), _pixel_box(entry.get("box"), *size)
        if isinstance(k, bool) or not isinstance(k, int):
            continue
        if 0 <= k < len(items) and box is not None:
            found.setdefault(k, box)
    for k, (category, name) in enumerate(items):
        if k not in found:
            logger.warning(f"box call: no box for {category} {name!r}")
            degraded.report("box call missed a product")
    return [found.get(k) for k in range(len(items))]


def _crop(image: bytes, box: Box | None, size: tuple[int, int]):
    """The crop of ``box`` with ``ITEM_CONTEXT`` around it; None without a
    box or under ``MIN_ITEM_SIDE`` px (logged)."""
    if box is None:
        return None
    w, h = box[2] - box[0], box[3] - box[1]
    if max(w, h) < MIN_ITEM_SIDE:
        logger.info(f"box call: a {w}x{h} px product is too small to crop")
        return None
    pad_x, pad_y = round(ITEM_CONTEXT * w), round(ITEM_CONTEXT * h)
    padded = (
        max(0, box[0] - pad_x),
        max(0, box[1] - pad_y),
        min(size[0], box[2] + pad_x),
        min(size[1], box[3] + pad_y),
    )
    return media.crop_box(image, padded)


def _area(box: Box) -> int:
    return (box[2] - box[0]) * (box[3] - box[1])


def _iou(a: Box, b: Box) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    both = max(0, w) * max(0, h)
    return both / max(1, _area(a) + _area(b) - both)


def _within(box: Box, outer: Box, margin: int) -> bool:
    """``box`` inside ``outer``, ``margin`` allowed on each side."""
    return (
        box[0] >= outer[0] - margin
        and box[1] >= outer[1] - margin
        and box[2] <= outer[2] + margin
        and box[3] <= outer[3] + margin
    )


def _like_half(
    items: Sequence[Asked],
    boxes: list,
    crops: list,
    half: Box | None,
    categories: tuple[Category, ...],
) -> list[int]:
    """The indexes of the products of ``categories`` cropped whose box, no
    margin, overlaps the half's box ``half`` by ``HALF_IOU`` and fits inside
    it within ``HALF_MARGIN`` (the same picture twice; a coat longer than
    the half keeps its own crop, never cut by the half), the largest first
    (equal ones in order)."""
    if half is None:
        return []
    like = [
        (k, box)
        for k, ((category, _), box, crop) in enumerate(
            zip(items, boxes, crops, strict=True)
        )
        if category in categories
        and crop is not None
        and _iou(box, half) >= HALF_IOU
        and _within(box, half, HALF_MARGIN)
    ]
    return [k for k, _ in sorted(like, key=lambda u: -_area(u[1]))]


def _detected(
    image: bytes, prompt: str, schema: dict, bands: bool
) -> tuple[dict[str, Box] | None, dict]:
    """The band boxes ({} when not asked; no bottom one when
    ``lower_body`` is ``[]``) and the last raw answer; None for the boxes
    when the answer is invalid ``DETECT_TRIES`` times."""
    size = _size(image)
    raw: dict = {}
    for attempt in range(1, DETECT_TRIES + 1):
        try:
            answer = gen.generate_json(
                [image, prompt],
                schema=schema,
                params=TextParams(model=get_models().judge, thinking="MEDIUM"),
            )
        except GenerationError as exc:
            logger.warning(f"box call failed ({attempt}): {exc}")
            continue
        raw = answer if isinstance(answer, dict) else {}
        boxes = {
            b: _pixel_box(raw.get(k), *size)
            for b, k in _DETECTOR_KEYS.items()
            if bands
        }
        if raw.get("lower_body") == [] and bands:  # bare legs above shoes
            del boxes["bottom"]
        ok = isinstance(raw.get("items"), list) or bands
        if ok and all(box is not None for box in boxes.values()):
            return cast(dict[str, Box], boxes), raw  # none is None
        logger.warning(f"box call: invalid answer {raw!r}")
    return None, raw


def box_items(
    image: bytes, items: Sequence[Asked] = (), *, bands: bool = False
) -> Boxed:
    """Box ``items`` (the category and name of each product to find) in
    ``image`` and, with ``bands``, its top and bottom halves (no bottom one
    when the legs' lowest hem is above the shoes; none when the boxes are
    invalid twice). No answer at all (each call failed) is a retryable
    ``GenerationError``: drawn without its crops, the run would quietly
    drop products."""
    image = media.to_png(image)
    size = _size(image)
    prompt, schema = _prompt(items, bands)
    halves, raw = _detected(image, prompt, schema, bands)
    if halves is None and not raw:
        raise GenerationError(
            "the photos could not be cut into their products; try again",
            retryable=True,
        )
    if halves is None and bands:  # invalid twice: no halves
        degraded.report("look photo cut without its halves")
    cut = {
        band: media.crop_box(image, box) for band, box in (halves or {}).items()
    }
    boxes = _found(raw, items, size) if items else []
    crops = [_crop(image, box, size) for box in boxes]
    for name, crop in cut.items():
        trace.save(f"reference-crop-{name}", crop)
    for k, crop in enumerate(crops):
        if crop is not None:
            trace.save(f"box-item-{k}", crop)
    top, bottom = (
        _like_half(items, boxes, crops, (halves or {}).get(band), categories)
        for band, categories in _HALF_OF.items()
    )
    return Boxed(cut, crops, boxes, top, bottom)
