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

"""Detail close-ups in the front drawing prompt.

A small detail (a padlock about 40 px wide in the try-on) comes out garbled
when the drawing sees it only in the product photo. Once the classifier has
run, each uploaded product gets one search (``details.find``) over all its
photos but those of its back, all products at once (at most
``MAX_SEARCHES`` calls at a time), each told its product's name (only
details on it): its whole uploads, worn or flat, and its detail photos
(zoom photos: close-ups of a part of it the classifier found among the
uploads, ``Product.details``); never a look-photo crop. First of all goes
its anchor (``_anchor``), the photo its hero is or is cut from, known from
the classification alone: only details visible on it are kept (a fur
jacket's hero, worn open with the fur out, got the plaque and zip pull of
its closed flat lay, which the hero does not show). Seen together, the photos
can be compared: each small detail (lettering, logos, hardware, labels)
is cropped with its surroundings from the photo where it is largest and
sharpest, the same detail in two photos once, and not upscaled (no
image-model call); searched one photo at a time, a photo could not be
compared with the others and a logo could come from a blurry photo when a
sharp one existed. A close-up from a zoom photo is of ``PHOTO`` kind (on
its card it always stays) and is dropped when larger than
``MAX_ZOOM_SHARE`` of its photo; a zoom photo that gives none is not used.
Two close-ups from the same photo whose crops would show nearly the same
area become one (``closeup_merge``; a zoom photo's only within
``MAX_ZOOM_SHARE``); then the first ``LIMIT_PER_PRODUCT`` are kept, of the
``SEARCHED`` asked, so a merge frees a slot for another detail. A product
the look adds has no upload: no search. The close-ups go into the front
drawing prompt: for a single product as separate images after the
products (``lines``), for an outfit on each product's card, under a label
(``label``) and listed in its INPUTS entry (``item``). In a test (5 cases,
1 run each) 12 of 15 images were published with them, against 8 of 15
without.

The front drawing waits for the search (each of its calls has the shared
stall deadline and retry, ``core.gen``); with the search off or failed it
draws without them, each zoom photo (not of the back) sent as it is. Not
waiting, a live run started drawing 7 s before the search ended: all three
images lost the close-ups and the padlock came out solid gold. The back
view gets none. ``GENMEDIA_TRYON_CLOSEUPS=false`` turns the search off.
"""

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace

from genmedia4commerce.core import degraded, trace
from genmedia4commerce.core.retry import ContextPool
from genmedia4commerce.core.settings import get_settings
from genmedia4commerce.core.types import Classification, Product
from genmedia4commerce.workflows import details
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.tryon import cards, closeup_merge, hero

__all__ = [
    "LIMIT_PER_PRODUCT",
    "PHOTO",
    "SEARCHED",
    "collect",
    "item",
    "label",
    "lines",
    "search",
    "wait",
    "what",
]

logger = logging.getLogger(__name__)

LIMIT_PER_PRODUCT = 3  # close-ups per product
SEARCHED = 5  # close-ups asked of a search (its most), merged, then cut
MAX_SEARCHES = 4  # search calls at once, like the box calls
# A zoom photo's crop wider or taller than this share of the photo is no
# detail (a whole fur lapel, 43-94% of it, came with the model's face); the
# largest good one, a belt buckle, was 32%.
MAX_ZOOM_SHARE = 0.4
PHOTO = "photo"  # the kind of a close-up found in a zoom photo

Found = list[tuple[str, list[Detail]]]  # each product searched, by id

# A close-up's short description (``what``): the words of its ``where``
# before the first preposition, without filler, its lettering (up to
# MAX_TEXT_WORDS words) first, at most MAX_WORDS words.
MAX_WORDS = 5
MAX_TEXT_WORDS = 3
_CUT = re.compile(
    r"\s+(?:on|of|at|near|in|inside|with|along|above|below|under|by|from|to"
    r"|for|between|around|across|over)\s+|\s*[,;:()]\s*",
    re.IGNORECASE,
)
_FILLER = {
    "hardware",
    "detail",
    "details",
    "area",
    "region",
    "the",
    "a",
    "an",
    "small",
}
_STYLE = {  # a style word in ``where`` names the lettering
    "embossed": "stamp",
    "debossed": "stamp",
    "stamped": "stamp",
    "printed": "print",
    "engraved": "engraving",
    "embroidered": "embroidery",
}
_NOUN = {
    "label": "label",
    "logo": "logo",
    "text": "lettering",
    "hardware": "hardware",
}
_LABEL = "{code}: {what}"  # on its card: "A1: MO padlock"
_ITEM = "{code} {what}{where}"  # in its product's entry
_LETTERING = " (lettering '{text}')"  # when the label shortened it
# A single product's close-up, after the products (``lines``).
_LINE = (
    "DETAIL CLOSE-UP of {product} ({what}): {source}; keep its shape, "
    "colours, materials and lettering at its true size on the product. "
    "Reproduce this detail exactly where it is visible on the product; "
    "never uncover it."
)
_FROM_CROP = "a crop of its product photo, shown enlarged"
_FROM_PHOTO = "a close-up photo of the product"


def _anchor(
    p: Product, c: Classification, images: Sequence[bytes]
) -> bytes | None:
    """The photo ``p``'s hero is or is cut from (``hero``), known before
    the box calls: its whole worn upload; else the photo its crop is cut
    from (``hero.crop_sources``: the look photo first), even if the crop
    turns out too small and its flat upload wins; else its whole flat
    upload, else its best upload. None when that is of its back."""
    if hero.worn(p, c, images) is None:
        source = next(iter(hero.crop_sources(p, c)), None)
        if source is not None:
            return images[source]
    picked = hero.pick(p, None, c, images)  # worn, flat or fallback
    if picked is None or picked[1].view == "back":
        return None
    return picked[1].image


def _photos(
    p: Product, c: Classification, images: Sequence[bytes]
) -> tuple[bytes | None, list[bytes], list[bytes]]:
    """The photos ``p`` is searched in: (its anchor, ``_anchor``; its
    other whole uploads of its front or another side, worn or flat; its
    zoom photos, ``_zooms``)."""
    anchor = _anchor(p, c, images)
    whole = [
        r.image
        for r in p.refs
        if r.source == "upload" and r.view != "back" and r.image is not anchor
    ]
    return anchor, whole, _zooms(p)


def _search_one(
    product_id: str,
    name: str,
    anchor: bytes | None,
    whole: list[bytes],
    zooms: list[bytes],
) -> tuple[str, list[Detail]] | None:
    """One product's close-ups, in ``details.find``'s order: those of a
    zoom photo of ``PHOTO`` kind, dropped when larger than
    ``MAX_ZOOM_SHARE`` of it; those that would show nearly the same area
    merged (``closeup_merge``), then the first ``LIMIT_PER_PRODUCT``; None
    when the search fails."""
    try:
        found = details.find(
            [*whole, *zooms],
            limit=SEARCHED,
            upscale=False,
            product=name,
            anchor=anchor,
        )
    except Exception as exc:  # optional: the other products go on
        logger.warning(f"try-on: detail search of {product_id} failed: {exc}")
        degraded.report("close-up search")
        return None
    whole_photos = len(whole) + (anchor is not None)  # zoom photos after
    kept = []
    for d in found:
        if d.photo < whole_photos:
            kept.append(d)
        elif _small(d):
            kept.append(replace(d, kind=PHOTO))
    photos = [*([] if anchor is None else [anchor]), *whole, *zooms]
    merged = closeup_merge.merge(
        kept,
        photos,
        fits=lambda d: d.kind != PHOTO or _small(d),
        max_side=min(cards.CROP_CAP_SIDE, cards.CROP_CAP_ROW),
    )
    return product_id, merged[:LIMIT_PER_PRODUCT]


def search(c: Classification, images: Sequence[bytes]) -> asyncio.Task | None:
    """Start the search of the photos of each of ``c``'s products
    (``_photos``; ``images``: the photos of ``c.photos``, in order), one
    call per product told its name, at most ``MAX_SEARCHES`` at a time, in
    the background; None when ``GENMEDIA_TRYON_CLOSEUPS`` is off or there
    is nothing to search. The task returns each product searched, by id,
    with its close-ups; a product whose search failed is left out."""
    jobs = [(p.id, p.description, *_photos(p, c, images)) for p in c.products]
    jobs = [job for job in jobs if job[2] is not None or job[3] or job[4]]
    if not get_settings().tryon_closeups or not jobs:
        return None

    def run() -> Found:
        with ContextPool(max_workers=min(MAX_SEARCHES, len(jobs))) as pool:
            done = pool.map(lambda job: _search_one(*job), jobs)
            return [hit for hit in done if hit is not None]

    return asyncio.create_task(asyncio.to_thread(run))


async def wait(task: asyncio.Task | None) -> Found:
    """Each product searched with its close-ups, once ``task`` is over; []
    when the search is off or failed (the drawing goes on without them)."""
    if task is None:
        return []
    try:
        return await task
    except Exception as exc:
        logger.warning(f"try-on: detail search failed, no close-ups: {exc}")
        degraded.report("close-up search")
        return []


async def collect(
    task: asyncio.Task | None, products: Sequence[Product]
) -> list[tuple[str, Detail]] | None:
    """(product id, close-up) for the front drawing, once ``task``
    (``search``) is over: what it found for each of ``products`` (each
    close-up traced) and, for a product not searched (the search off or
    failed), each zoom photo as it is (``_as_is``); None with neither."""
    found = dict(await wait(task))
    out: list[tuple[str, Detail]] = []
    for p in products:
        hits = found.get(p.id)
        if hits is None:
            out += [(p.id, _as_is(p, image)) for image in _zooms(p)]
            continue
        for k, detail in enumerate(hits):
            trace.save(f"closeup-{p.id}-{k}", detail.image)
        out += [(p.id, detail) for detail in hits]
    return out if found or out else None


def _as_is(product: Product, image: bytes) -> Detail:
    """A zoom photo not searched, whole as a ``PHOTO`` close-up of
    ``product``."""
    where = f"close-up of the {product.description}"
    return Detail(PHOTO, "", "other", where, image)


def _zooms(product: Product) -> list[bytes]:
    """The product's zoom photos the front drawing may use: those of its
    front, of a side or of no side, never of its back."""
    return [ref.image for ref in product.details if ref.view != "back"]


def _small(detail: Detail) -> bool:
    """The detail's box is within ``MAX_ZOOM_SHARE`` of its photo."""
    y0, x0, y1, x1 = detail.box
    return max(y1 - y0, x1 - x0) <= MAX_ZOOM_SHARE * 1000


def _what(detail: Detail) -> str:
    """``name 'text'`` when the detail has lettering, else its name (its
    kind when it has none)."""
    noun = detail.name or detail.kind
    return f"{noun} '{detail.text}'" if detail.text else noun


def lines(
    closeups: Sequence[tuple[str, Detail]], named: Mapping[str, str]
) -> list[tuple[str, bytes]]:
    """One INPUTS legend line per close-up, with its image; ``named`` maps
    a product id to its name in the prompt ('PRODUCT A field jacket')."""
    out = []
    for product_id, d in closeups:
        photo = d.kind == PHOTO
        text = _LINE.format(
            product=named.get(product_id, f"PRODUCT {product_id}"),
            what=_photo_what(d) if photo else f"{_what(d)}; {d.where}",
            source=_FROM_PHOTO if photo else _FROM_CROP,
        )
        out.append((text, d.image))
    return out


def _split_where(where: str) -> tuple[list[str], str]:
    """(the words of ``where`` before its first preposition, the rest)."""
    m = _CUT.search(where)
    head, tail = (where[: m.start()], where[m.start() :]) if m else (where, "")
    return head.split(), tail.strip(" ,;:()")


def what(detail: Detail) -> tuple[str, bool]:
    """(short description, lettering shortened): 'padlock', 'front
    buttons', 'MAISON ORVELLE PARIS stamp'. The detail's name says what it
    is (a merged close-up's, whole: 'label and triangle patch'); without
    one, the first words of where it is stand in."""
    head, _ = _split_where(detail.where)
    style = next((_STYLE[w.lower()] for w in head if w.lower() in _STYLE), None)
    words = [w for w in head if w.lower() not in _FILLER | _STYLE.keys()]
    words = detail.name.split() or words
    most = MAX_WORDS + len(words) * (" and " in detail.name)
    if not detail.text:
        desc = words or [style or _NOUN.get(detail.kind, "detail")]
        return " ".join(desc[-most:]), False
    text = detail.text.split()
    shortened = len(text) > MAX_TEXT_WORDS
    if shortened:
        last = text[MAX_TEXT_WORDS - 1] + "\u2026"
        text = [*text[: MAX_TEXT_WORDS - 1], last]
    if words and (detail.name or detail.kind == "hardware"):
        lettering = {w.lower() for w in detail.text.split()}
        noun = [w for w in words if w.lower() not in lettering] or words
    else:
        noun = [style or _NOUN.get(detail.kind, detail.kind)]
    return " ".join([*text, *noun[-(most - len(text)) :]]), shortened


def _rest(detail: Detail, short: str) -> str | None:
    """The rest of ``where`` when ``short``, the detail's short
    description, names its first words ("" when nothing is left); None
    when it does not."""
    head, tail = _split_where(detail.where)
    named = {w.lower() for w in short.split()}
    skip = _FILLER | _STYLE.keys()
    if all(w.lower() in named for w in head if w.lower() not in skip):
        return tail
    return None


def _where(detail: Detail, short: str) -> str:
    """Where the detail is, after its short description: the rest of
    ``where`` (``_rest``), else all of it in brackets."""
    rest = _rest(detail, short)
    if rest is None:
        return f" ({detail.where})"
    return f" {rest}" if rest else ""


def _photo_what(detail: Detail) -> str:
    """A zoom photo's close-up in its line, as on an outfit's card: what
    it is with its lettering (``what``, in full when shortened), then
    where ('AURELIA MILANO buckle, at the waist'); a zoom photo sent as it
    is (no name nor lettering) only says where."""
    if not (detail.name or detail.text):
        return detail.where
    short, shortened = what(detail)
    rest = _rest(detail, short)
    if shortened:
        short += _LETTERING.format(text=detail.text)
    if rest is None:
        return f"{short}, {detail.where}"
    return f"{short} {rest}" if rest else short


def label(code: str, detail: Detail) -> str:
    """The label of a close-up on its card: 'A1: MO padlock'."""
    return _LABEL.format(code=code, what=what(detail)[0])


def item(code: str, detail: Detail) -> str:
    """A close-up in its product's INPUTS entry: 'A1 MO padlock on upper
    strap', the lettering in full when the label shortened it."""
    short, shortened = what(detail)
    text = _ITEM.format(code=code, what=short, where=_where(detail, short))
    return text + (_LETTERING.format(text=detail.text) if shortened else "")
