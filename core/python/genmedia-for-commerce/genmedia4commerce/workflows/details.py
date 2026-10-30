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

"""Close-up references of a product's small identity details.

The video model reads every reference at 720p in 16 px blocks: a plaque
40 px wide gets two to four blocks and comes out garbled. One judge-model
call finds the small details on the product (lettering, logos, branded
hardware, labels) in the original photos; each kept one is cropped with its
surroundings (``CONTEXT`` x its box, so the model can place it) from the
full-resolution photo, where it gets dozens of blocks.

Each close-up is then upscaled by the image model with its lettering in the
prompt (``upscale``; ``find(..., upscale=False)`` leaves it to the caller):
in a test the video model then spelled an embossed "Aurelia" right 5 times
of 5, against 2 of 5 from the plain crop.

Kept: details on the outside of the product (not inside or underneath),
large enough in their photo to add something (``MIN_SOURCE``), one per
thing (a detail inside another's box is that one, duplicates dropped), in
the judge model's order of importance with legible lettering first. What
counts as one detail is the judge model's call: a label of several lines is
one box; each box covers a small area, at most about a third of the
product's width or height (buttons: a short run of two or three, never a
whole placket; never a pocket set or a panel). ``find`` never merges
details near each other: merged, a padlock and a stamp on the strap below it
made one tall close-up with the padlock small, and the drawing lost the
padlock's leather face. The photo try-on merges two whose close-ups would
show nearly the same area (``tryon.closeup_merge``).
"""

import io
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

import numpy as np
from PIL import Image, ImageOps

from genmedia4commerce.core import degraded, gen, media
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.retry import ContextPool

__all__ = ["CONTEXT", "Detail", "find", "find_each", "upscale"]

logger = logging.getLogger(__name__)

CONTEXT = 2.75  # a close-up spans this x the detail's box
MIN_CROP = 256  # px, the close-up's smallest side in the photo
MIN_SOURCE = 48  # px: a detail smaller in its photo has nothing to add
_HIDDEN = {"inside", "bottom"}  # never seen while the product turns
_ALIKE = 0.8  # correlation of two 32x32 grey boxes that show the same thing

_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "details": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "kind": {
                        "type": "STRING",
                        "enum": ["text", "logo", "hardware", "label"],
                    },
                    "text": {"type": "STRING"},
                    "legible": {"type": "BOOLEAN"},
                    "photo": {"type": "INTEGER"},
                    "box_2d": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                    "side": {
                        "type": "STRING",
                        "enum": [
                            "front",
                            "back",
                            "left",
                            "right",
                            "top",
                            "bottom",
                            "inside",
                            "other",
                        ],
                    },
                    "where": {"type": "STRING"},
                    "name": {"type": "STRING"},
                },
                "required": [
                    "kind",
                    "text",
                    "legible",
                    "photo",
                    "box_2d",
                    "side",
                    "where",
                    "name",
                ],
            },
        }
    },
    "required": ["details"],
}

_ASK = (
    "List the small, identity-critical details ON THE PRODUCT itself (not on "
    "a person or the background), most important first, at most 5: brand "
    "lettering or printed/embossed/engraved text, logos and emblems, branded "
    "or distinctive hardware (clasps, buckles, plaques, zip pulls, charms), "
    "labels and tags. Skip large prints and all-over patterns. For each: "
    "kind; the exact text letter by letter if it has letters (else empty) "
    "and whether it is legible; the index of the photo (IMAGE_<n>) where it "
    "is clearest; its box_2d [ymin, xmin, ymax, xmax] in 0-1000 in that "
    "photo; which side of the product it is on; a few words on where; its "
    "name: one to three words for the object itself, never its place "
    "('side zip', 'padlock', 'front buttons', 'brand plaque'). One "
    "box per detail: a label or patch with several lines of text is one "
    "box; the buttons, snaps, studs or rivets of one closure are one box "
    "around the whole set, from the first to the last (it shows their "
    "count, spacing and where the garment ends). Never box a pocket set, a "
    "panel or most of the garment."
)
# The try-on names the product each search is for: told nothing, a search
# found details on the wrong product (a skirt's buckle for a jacket).
_PRODUCT = (
    " The product is the {product}: return only details on it, never on "
    "other garments, shoes, accessories or the person."
)
# The try-on sends the photo its hero is or is cut from first: told nothing,
# a search took a jacket's plaque and zip pull from its closed flat lay
# while the hero showed it worn open, fur side out.
_ANCHOR = (
    " IMAGE_0 shows the product as it will be drawn: return only details "
    "visible on the product in IMAGE_0, each from the photo where it is "
    "largest and sharpest (another photo may show the same detail closer)."
)


@dataclass(frozen=True)
class Detail:
    """One close-up: what it shows and the crop (JPEG)."""

    kind: str  # text | logo | label | hardware
    text: str  # the exact lettering when legible, else ""
    side: str  # front | back | left | right | top | other
    where: str  # a few words, e.g. "plaque on the lower front"
    image: bytes
    photo: int = 0  # which input photo it was cropped from
    box: tuple[int, int, int, int] = (0, 0, 0, 0)  # 0-1000, in that photo
    name: str = ""  # what the object is, e.g. "side zip", "padlock"


def open_photo(data: bytes) -> Image.Image:
    """The photo upright, in RGB: boxes are given in it."""
    return ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")


def _found(
    images: Sequence[bytes], product: str = "", anchored: bool = False
) -> list[dict]:
    parts: list[gen.Part] = []
    for i, image in enumerate(images):
        parts += [f"IMAGE_{i}:", image]
    ask = _ASK + (_PRODUCT.format(product=product) if product else "")
    parts.append(ask + (_ANCHOR if anchored else ""))
    raw = gen.generate_json(
        parts,
        schema=_SCHEMA,
        params=gen.TextParams(model=get_models().judge, thinking="LOW"),
    )
    return (raw or {}).get("details", []) if isinstance(raw, dict) else []


def _norm(text: str) -> str:
    return re.sub(r"\W", "", text.lower())


def _thumb(photo: Image.Image, box: tuple[int, int, int, int]) -> np.ndarray:
    width, height = photo.size
    y0, x0, y1, x1 = box
    tight = photo.crop(
        (
            x0 * width // 1000,
            y0 * height // 1000,
            max(x0 * width // 1000 + 1, x1 * width // 1000),
            max(y0 * height // 1000 + 1, y1 * height // 1000),
        )
    )
    return np.asarray(tight.convert("L").resize((32, 32)), float).ravel()


def _alike(a: np.ndarray, b: np.ndarray) -> bool:
    if a.std() < 1 or b.std() < 1:  # flat boxes: alike when the same tone
        return abs(a.mean() - b.mean()) < 8
    return float(np.corrcoef(a, b)[0, 1]) >= _ALIKE


def _duplicate(kept: Detail, d: Detail, photos: list[Image.Image]) -> bool:
    """The same thing again. Both lettered: same kind and side, its text
    within kept's. One lettered and one not on the same photo: two details
    (a blazer's MO top button and its plain bottom one). Otherwise: the two
    boxes look alike (the side Gemini names can differ between photos, and
    two different emblems can share a side)."""
    if kept.kind != d.kind:
        return False
    if d.text and kept.text:
        return kept.side == d.side and _norm(d.text) in _norm(kept.text)
    if (d.text or kept.text) and kept.photo == d.photo:
        return False  # one lettered, one not, both seen: two details
    return _alike(
        _thumb(photos[kept.photo], kept.box), _thumb(photos[d.photo], d.box)
    )


def _inside(a: tuple, b: tuple) -> bool:
    """Box ``a`` within box ``b``."""
    return b[0] <= a[0] and b[1] <= a[1] and a[2] <= b[2] and a[3] <= b[3]


def _add(
    kept: list[Detail], d: Detail, photos: list[Image.Image]
) -> list[Detail]:
    """``kept`` with ``d``: a detail inside another's box on the same photo
    (the MO disc of a padlock) is that one, with either's lettering; a
    duplicate (``_duplicate``) is dropped; anything else is added."""
    for i, k in enumerate(kept):
        if k.photo == d.photo and _inside(d.box, k.box):
            kept[i] = replace(k, text=k.text or d.text)
            return kept
        if k.photo == d.photo and _inside(k.box, d.box):
            kept[i] = replace(d, text=d.text or k.text)
            return kept
        if _duplicate(k, d, photos):
            return kept
    return [*kept, d]


def _candidates(raw: list[dict], photos: list[Image.Image]) -> list[Detail]:
    sizes = [p.size for p in photos]
    kept: list[Detail] = []
    for item in raw:
        photo, box = item.get("photo"), item.get("box_2d") or []
        if item.get("side") in _HIDDEN or not isinstance(photo, int):
            continue
        if not 0 <= photo < len(sizes):
            continue
        if len(box) != 4 or box[2] <= box[0] or box[3] <= box[1]:
            continue
        width, height = sizes[photo]
        long_side = max(
            (box[3] - box[1]) / 1000 * width, (box[2] - box[0]) / 1000 * height
        )
        if long_side < MIN_SOURCE:
            continue
        text = item.get("text", "").strip() if item.get("legible") else ""
        detail = Detail(
            kind=item.get("kind", "hardware"),
            text=text,
            side=item.get("side", "other"),
            where=item.get("where", "").strip() or "detail",
            image=b"",
            photo=photo,
            box=(int(box[0]), int(box[1]), int(box[2]), int(box[3])),
            name=item.get("name", "").strip(),
        )
        kept = _add(kept, detail, photos)
    # The judge model lists the most important first; legible lettering, the
    # detail the video model garbles most, goes first of all.
    return sorted(kept, key=lambda d: not d.text)


def crop_rect(
    size: tuple[int, int], box: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """The close-up of ``box`` in a photo of ``size`` (px), as (left, top,
    right, bottom) px: ``CONTEXT`` x the box, at least ``MIN_CROP`` px a
    side, within the photo."""
    width, height = size
    y0, x0, y1, x1 = (v / 1000 for v in box)
    cx, cy = (x0 + x1) / 2 * width, (y0 + y1) / 2 * height
    w = min(width, max((x1 - x0) * width * CONTEXT, MIN_CROP))
    h = min(height, max((y1 - y0) * height * CONTEXT, MIN_CROP))
    left = min(max(0, cx - w / 2), width - w)
    top = min(max(0, cy - h / 2), height - h)
    return int(left), int(top), int(left + w), int(top + h)


def crop(photo: Image.Image, box: tuple[int, int, int, int]) -> bytes:
    """The detail with its surroundings (``crop_rect``), as a JPEG."""
    buf = io.BytesIO()
    cut = photo.crop(crop_rect(photo.size, box))
    cut.save(buf, format="JPEG", quality=95, subsampling=0)
    return buf.getvalue()


def _upscale(detail: Detail) -> Detail:
    """``detail`` with its close-up upscaled to 2K by the image model, the
    lettering (when legible) quoted so it is kept letter for letter; the
    crop as it is if the upscale fails."""
    prompt = media.UPSCALE_PROMPT
    if detail.text:
        prompt += (
            f"\n\nThe lettering in this image reads exactly: '{detail.text}'. "
            "Keep its exact typeface, letter shapes, stroke weight, spacing "
            "and position; do not re-letter it in another font."
        )
    try:
        image = gen.generate_image(
            [prompt, detail.image],
            params=gen.ImageParams(aspect_ratio="auto", image_size="2K"),
        )
    except Exception as exc:  # the crop is still a good close-up
        logger.warning(f"close-up upscale failed, keeping the crop: {exc}")
        degraded.report("close-up upscale")
        return detail
    return replace(detail, image=image)


def upscale(found: Sequence[Detail]) -> list[Detail]:
    """Each close-up of ``found`` upscaled (``_upscale``), all at once; a
    failed upscale keeps its crop."""
    if not found:
        return []
    with ContextPool(max_workers=len(found)) as pool:
        return list(pool.map(_upscale, found))


_upscale_all = upscale  # ``find``'s ``upscale`` flag hides the name


def find(
    images: Sequence[bytes],
    *,
    limit: int = 2,
    upscale: bool = True,
    product: str = "",
    anchor: bytes | None = None,
) -> list[Detail]:
    """Up to ``limit`` close-ups of ``images``' product; [] when there are
    none or the search fails (a run never stops for this). ``upscale``:
    False keeps the crops (no image-model call; see ``upscale``).
    ``product``: its name ('pencil skirt'), when the photos may show other
    products: only details on it are returned. ``anchor``: the photo that
    shows the product as it will be drawn, sent first (IMAGE_0): only
    details visible on it are returned, each from the photo where it is
    largest; a close-up's ``photo`` then counts it as 0."""
    images = list(images) if anchor is None else [anchor, *images]
    if limit <= 0 or not images:
        return []
    try:
        photos = [open_photo(image) for image in images]
        found = _found(images, product, anchored=anchor is not None)
        details = _candidates(found, photos)
    except Exception as exc:  # optional: the spin goes on without close-ups
        logger.warning(f"detail close-ups skipped: {exc}")
        degraded.report("close-up search")
        return []
    kept = details[:limit]
    logger.info(
        "detail close-ups: "
        + ", ".join(f"{d.kind} {d.text!r} ({d.side}, {d.where})" for d in kept)
    )
    cropped = [replace(d, image=crop(photos[d.photo], d.box)) for d in kept]
    return _upscale_all(cropped) if upscale else cropped


def find_each(
    photos: Sequence[bytes],
    *,
    limit: int = 2,
    products: Sequence[str] = (),
) -> list[list[Detail]]:
    """The close-ups of each photo on its own (``find``), all photos at once,
    cropped but not upscaled (no image-model call); [] for a photo whose
    search fails. ``products``: for each photo, the name of its product
    (``find``'s ``product``; none when empty)."""
    if not photos:
        return []
    names = list(products) or [""] * len(photos)

    def search(photo: bytes, product: str) -> list[Detail]:
        try:
            return find([photo], limit=limit, upscale=False, product=product)
        except Exception as exc:  # optional: the other photos go on
            logger.warning(f"detail close-ups skipped for a photo: {exc}")
            degraded.report("close-up search")
            return []

    with ContextPool(max_workers=len(photos)) as pool:
        return list(pool.map(search, photos, names))
