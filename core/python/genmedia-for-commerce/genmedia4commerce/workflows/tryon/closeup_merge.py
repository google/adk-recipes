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

"""Close-ups of one product that would show nearly the same area, made one.

A close-up spans ``details.CONTEXT`` x its detail's box: two details next
to each other (a lettered label right above a triangle patch) gave two
crops of almost the same area, which took two of a product's three slots
(``closeups.LIMIT_PER_PRODUCT``). Two close-ups from the same photo whose
crops overlap by at least ``MIN_OVERLAP`` of the smaller one become one
(``merge``): its box the union of their boxes, its crop cut again around it
with the same rules (``details.crop``), its name both names ('label and
triangle patch'), its lettering both letterings, its kind the text or logo
one when either is, its side and where the first's. Only the photo try-on
merges (``closeups``): ``details.find``, also used by the spin and the video
try-on, never does.
"""

from collections.abc import Callable, Sequence
from dataclasses import replace

from PIL import Image

from genmedia4commerce.workflows import details
from genmedia4commerce.workflows.details import Detail

__all__ = ["MIN_OVERLAP", "merge", "too_long"]

MIN_OVERLAP = 0.5  # of the smaller crop's area
_LETTERED = ("text", "logo")  # the kinds a merged close-up keeps first
_NOUN = {"text": "lettering", "photo": "detail"}  # a part with no name

Rect = tuple[int, int, int, int]  # left, top, right, bottom (px)


def _area(rect: Rect) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def _overlap(size: tuple[int, int], a: Detail, b: Detail) -> float:
    """The area the crops of ``a`` and ``b`` share in a photo of ``size``,
    as a share of the smaller crop's (``details.crop_rect``)."""
    ra, rb = details.crop_rect(size, a.box), details.crop_rect(size, b.box)
    shared = (
        max(ra[0], rb[0]),
        max(ra[1], rb[1]),
        min(ra[2], rb[2]),
        min(ra[3], rb[3]),
    )
    return _area(shared) / max(1, min(_area(ra), _area(rb)))


def _side(size: tuple[int, int], d: Detail) -> int:
    """The long side (px) of ``d``'s crop in a photo of ``size``."""
    left, top, right, bottom = details.crop_rect(size, d.box)
    return max(right - left, bottom - top)


def too_long(
    size: tuple[int, int], joined: Detail, a: Detail, b: Detail, cap: int
) -> bool:
    """``joined`` would show ``a`` and ``b`` smaller than apart on a card,
    which shows a close-up at its own size up to ``cap``: its crop is
    longer than ``cap`` and than both of theirs."""
    longest = max(cap, _side(size, a), _side(size, b))
    return _side(size, joined) > longest


def _text(a: str, b: str) -> str:
    """Both letterings ('AURELIA, MO'); one when it holds the other."""
    if b.lower() in a.lower():
        return a
    if a.lower() in b.lower():
        return b
    return f"{a}, {b}"


def _name(detail: Detail) -> str:
    """What the detail is: its name, else a noun for its kind."""
    return detail.name or _NOUN.get(detail.kind, detail.kind)


def _joined(a: Detail, b: Detail, photo: Image.Image) -> Detail:
    """``a`` and ``b`` as one close-up cut from ``photo`` around both."""
    box = (
        min(a.box[0], b.box[0]),
        min(a.box[1], b.box[1]),
        max(a.box[2], b.box[2]),
        max(a.box[3], b.box[3]),
    )
    kind = next((d.kind for d in (a, b) if d.kind in _LETTERED), a.kind)
    return replace(
        a,
        kind=kind,
        text=_text(a.text, b.text),
        image=details.crop(photo, box),
        box=box,
        name=f"{_name(a)} and {_name(b)}",
    )


def merge(
    found: Sequence[Detail],
    images: Sequence[bytes],
    fits: Callable[[Detail], bool] = lambda _: True,
    max_side: int | None = None,
) -> list[Detail]:
    """``found`` in its order, each close-up whose crop overlaps a kept
    one's from the same photo by ``MIN_OVERLAP`` merged into it, in its
    place (``_joined``); ``images``: the photos ``Detail.photo`` counts. A
    merged close-up that ``fits`` refuses, or ``too_long`` for a card
    showing close-ups up to ``max_side``, stays two."""
    opened: dict[int, Image.Image] = {}
    kept: list[Detail] = []
    for d in found:
        for i, k in enumerate(kept):
            if k.photo != d.photo:
                continue
            if d.photo not in opened:
                opened[d.photo] = details.open_photo(images[d.photo])
            photo = opened[d.photo]
            if _overlap(photo.size, k, d) < MIN_OVERLAP:
                continue
            joined = _joined(k, d, photo)
            if max_side and too_long(photo.size, joined, k, d, max_side):
                continue
            if fits(joined):
                kept[i] = joined
                break
        else:
            kept.append(d)
    return kept
