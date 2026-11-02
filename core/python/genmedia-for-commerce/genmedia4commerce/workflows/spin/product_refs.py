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

"""Reference images of a non-shoe product for the 360 spin.

The best photo per angle (eight around the product: front, three-quarters,
sides, back), sent from the most frontal one onward around the circle: a
packshot when the angle has one, else a photo on a model, cut out
by the product's name so the person is left behind (the judge model's
outline, as for shoes). Each is reduced to one
item on white with a border all round (no AI upscale: the video model scales
every reference to its 720p frame). One item: a pair or set in one photo (two earrings) is cut
apart and the largest kept; items that touch stay whole. The border keeps
the product off the edges of the video frame, which the video model copies.
A photo on a busy background, or one whose cut by name fails, is sent as it
is (no segmentation model).
"""

import io
import logging
from collections.abc import Sequence

import cv2
import numpy as np
from PIL import Image, ImageOps

from genmedia4commerce.core import gen, media
from genmedia4commerce.core.models import get_models
from genmedia4commerce.core.retry import ContextPool
from genmedia4commerce.workflows.spin import shoe_split as split

__all__ = ["BORDER", "PRODUCT_VIEWS", "build", "one_item"]

logger = logging.getLogger(__name__)

# The eight angles around the product, in order (the classifier's views).
PRODUCT_VIEWS = (
    "front",
    "front_right",
    "right",
    "back_right",
    "back",
    "back_left",
    "left",
    "front_left",
)
# The first reference is the video's starting view: the most frontal angle.
_START = (
    "front",
    "front_right",
    "front_left",
    "right",
    "left",
    "back_right",
    "back_left",
    "back",
)
BORDER = 0.10  # white space on every side, as a fraction of the item's size
_ITEM_RATIO = 0.5  # objects at least this x the largest are separate items
_MIN_CUT = 0.01  # a cut by name smaller than this share of the photo failed
_WHITE = (255, 255, 255)
_CUT_SIDE = 1024  # a photo on a model is sent at most this size to be cut
# A packshot keeps its own background only when nearly all of its border is
# that colour: a white showroom with a grey floor is cut out instead.
_PACKSHOT_PLAIN = 0.95
_CUT_ASK = (
    "Give the segmentation mask of {name} in the image: only the product "
    "itself, nothing else (no person wearing or holding it, no mannequin, "
    "hanger or stand, no shadow, no background). Output a JSON list with one "
    'entry per item, each with the 2D bounding box in the key "box_2d", the '
    'segmentation mask in key "mask" as a polygon of [x, y] points '
    "normalized to 0-1000 following the outline closely, and the text label "
    'in the key "label". Use at most 60 points per polygon.'
)
_CUT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "boxes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "box_2d": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                    "mask": {
                        "type": "ARRAY",
                        "items": {
                            "type": "ARRAY",
                            "items": {"type": "INTEGER"},
                        },
                    },
                    "label": {"type": "STRING"},
                },
                "required": ["box_2d", "mask", "label"],
            },
        }
    },
    "required": ["boxes"],
}


def _keep_mask(mask: np.ndarray) -> np.ndarray:
    """The largest item's pixels: its object plus the small parts nearest it.

    Small objects (a hook the cut-out sees apart from the stone) go with the
    nearest item, so a part is never lost; with a single item all is kept.
    """
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8)
    )
    areas = stats[1:, cv2.CC_STAT_AREA]
    items = [
        i + 1 for i, a in enumerate(areas) if a >= _ITEM_RATIO * areas.max()
    ]
    if len(items) < 2:
        return mask
    largest = max(items, key=lambda i: stats[i, cv2.CC_STAT_AREA])

    def nearest(i: int) -> int:
        return min(
            items, key=lambda j: np.linalg.norm(centroids[i] - centroids[j])
        )

    keep = [i for i in range(1, n) if nearest(i) == largest]
    return np.isin(labels, keep)


def _cut_by_name(img: Image.Image, name: str) -> np.ndarray:
    """The judge model's outline of ``name`` (the person left out)."""
    small = media.resize_max(split.png(img), _CUT_SIDE)
    raw = gen.generate_json(
        [_CUT_ASK.format(name=name), small],
        schema=_CUT_SCHEMA,
        params=gen.TextParams(
            model=get_models().judge,
            thinking="MEDIUM",
            max_output_tokens=65535,
            deadline_s=240,
        ),
    )
    boxes = raw.get("boxes") if isinstance(raw, dict) else None
    items = []
    for entry in boxes or []:
        poly, _ = split.decode(entry, img.width, img.height)
        if poly:
            items.append({"poly": poly})
    mask = np.zeros((img.height, img.width), bool)
    for item_mask in split.to_masks(items, img.width, img.height):
        mask |= item_mask
    return mask


def _mask(
    img: Image.Image, name: str | None
) -> tuple[np.ndarray, tuple | None]:
    """The product's pixels, and its background colour when the photo is a
    packshot on a plain background (no model: ``media.content_mask``).

    A photo on a model is cut by ``name``. A busy background, or a cut by
    name that fails or finds almost nothing, gives an empty mask: the photo
    is then used as it is (no segmentation model).
    """
    if not name:
        plain = media.content_mask(img, plain=_PACKSHOT_PLAIN)
        if plain is not None:
            return plain
    else:
        try:
            mask = _cut_by_name(img, name)
            if mask.mean() >= _MIN_CUT:
                return mask, None
            logger.info(f"cut by name found no {name!r}; photo used as is")
        except Exception as exc:
            logger.warning(f"cut by name failed ({exc}); photo used as is")
    return np.zeros((img.height, img.width), bool), None


def _crop(
    pixels: np.ndarray, box: tuple[int, int, int, int], fill: tuple
) -> Image.Image:
    """``box`` plus ``BORDER`` on every side; outside the photo is ``fill``."""
    x0, y0, x1, y1 = box
    pad_x, pad_y = round(BORDER * (x1 - x0)), round(BORDER * (y1 - y0))
    w, h = x1 - x0 + 2 * pad_x, y1 - y0 + 2 * pad_y
    out = Image.new("RGB", (w, h), tuple(int(c) for c in fill))
    sx0, sy0 = max(0, x0 - pad_x), max(0, y0 - pad_y)
    sx1 = min(pixels.shape[1], x1 + pad_x)
    sy1 = min(pixels.shape[0], y1 + pad_y)
    region = Image.fromarray(pixels[sy0:sy1, sx0:sx1])
    out.paste(region, (sx0 - (x0 - pad_x), sy0 - (y0 - pad_y)))
    return out


def _box(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(mask)
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


def one_item(image: bytes, name: str | None = None) -> bytes:
    """``image`` as one item with a ``BORDER`` on every side (JPEG).

    A packshot of a single item on a plain background is cropped as it is
    (the photo's own pixels and background): a cut-out can bite into the
    product. Otherwise the item is cut out onto white: ``name`` (the
    product's description) cuts out only that product, for a photo on a
    model; a pair keeps its largest item. With no foreground found (a busy
    background, a cut by name that fails) the photo is returned as is.
    """
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(image))).convert("RGB")
    mask, background = _mask(img, name)
    if mask.shape != (img.height, img.width) or not mask.any():
        return image
    pixels = np.array(img)
    keep = _keep_mask(mask)
    if background is not None and keep is mask:  # one item on a plain colour
        out = _crop(pixels, _box(mask), background)
    else:
        x0, y0, x1, y1 = _box(keep)
        item = np.full((y1 - y0, x1 - x0, 3), _WHITE, np.uint8)
        region = keep[y0:y1, x0:x1]
        item[region] = pixels[y0:y1, x0:x1][region]
        padded = np.full_like(pixels, _WHITE)
        padded[y0:y1, x0:x1] = item
        out = _crop(padded, (x0, y0, x1, y1), _WHITE)
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=95, subsampling=0)  # full colour
    return buf.getvalue()


def _worn(view: dict) -> bool:
    return view.get("presentation") == "worn"


def _order(view: dict) -> tuple:
    """Packshots before photos on a model, then rank, then upload order."""
    return (_worn(view), view["rank"], view["index"])


def build(
    images: Sequence[bytes], views: list[dict], description: str
) -> dict[str, bytes]:
    """Best image per main view, each as one item with a border, at 2K.

    With no main view at all, the best image overall is used as ``other``.
    A photo on a model is cut out by ``description``.
    """
    best: dict[str, dict] = {}
    for v in sorted(views, key=_order):
        best.setdefault(v["view"], v)
    start = next((view for view in _START if view in best), None)
    picks = {}
    if start:
        i = PRODUCT_VIEWS.index(start)
        ring = PRODUCT_VIEWS[i:] + PRODUCT_VIEWS[:i]
        picks = {view: best[view] for view in ring if view in best}
    if not picks:
        picks = {"other": min(views, key=_order)}

    def reference(v: dict) -> bytes:
        return one_item(images[v["index"]], description if _worn(v) else None)

    with ContextPool(max_workers=len(picks)) as pool:
        return dict(
            zip(picks, pool.map(reference, picks.values()), strict=True)
        )
