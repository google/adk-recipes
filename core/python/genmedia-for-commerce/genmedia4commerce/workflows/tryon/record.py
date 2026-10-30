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

"""The run record of an image try-on: what its images were made from.

A video of one of its images (``source_run``) reuses it instead of
classifying the products again and searching their close-ups: the
classification (framing, each product's category and description), every
photo of each product as the try-on used it (uploads cropped to the
product, reference-model crops), the one chosen for the drawing (its hero),
and the close-ups found in its photos.
``payload`` is the classified event's payload;
``record`` builds the ``RecordEvent``
(``core.runs`` keeps it as the run's ``manifest.json``); ``read`` turns a
kept one back into the video's inputs.

The payload: ``{"version": 1, "workflow": "tryon", "classification": <the
classified event's payload>, "products": [{"refs": [ref, ...], "chosen":
<index in refs>, "closeups": [detail, ...] or null}]}``, one entry per
classified product. A ref is ``{"file", "view", "rank", "presentation",
"source"}``; a detail has the fields of ``details.Detail`` with ``file``
for its image. ``closeups`` is null when the search was off.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, get_args

from genmedia4commerce.core.errors import InputError
from genmedia4commerce.core.events import RecordEvent
from genmedia4commerce.core.types import (
    Category,
    Classification,
    Framing,
    Mode,
    Presentation,
    Product,
    ProductRef,
    RefSource,
    View,
)
from genmedia4commerce.workflows.details import Detail
from genmedia4commerce.workflows.tryon.outfit import best_upload

__all__ = ["VERSION", "Source", "events", "payload", "read", "record"]

logger = logging.getLogger(__name__)

VERSION = 1


@dataclass
class Source:
    """What a video reuses from a try-on run: the best upload of each
    product (``outfit.best_upload``: a look-photo crop only for a product
    the look added), the classification naming them, and the close-ups of
    each photo (None when the try-on did not search them)."""

    photos: list[bytes]
    classification: Classification
    closeups: list[list[Detail]] | None


def _origin(
    ref: ProductRef, images: Sequence[bytes], crops: Mapping[str, bytes]
) -> dict:
    """Where a ref's image comes from: ``{"band"}``, the name of its
    reference crop, or ``{"index"}``, its product image."""
    if ref.source == "model_crop":
        return {"band": next(b for b, c in crops.items() if c is ref.image)}
    return {
        "index": next(i for i, img in enumerate(images) if img is ref.image)
    }


def payload(
    classification: Classification,
    chosen: Sequence[tuple[Product, ProductRef]],
    images: Sequence[bytes],
    crops: Mapping[str, bytes],
) -> dict:
    """The classified payload: ``Classification.to_dict()`` plus, per
    product, the garment image chosen."""
    out = classification.to_dict()
    for entry, (_, ref) in zip(out["products"], chosen, strict=True):
        entry["chosen"] = {
            "source": ref.source,
            "presentation": ref.presentation,
            **_origin(ref, images, crops),
        }
    return out


def _file(
    ref: ProductRef, images: Sequence[bytes], crops: Mapping[str, bytes]
) -> str:
    """The file name of a ref's image: its crop's or its upload's."""
    origin = _origin(ref, images, crops)
    if "band" in origin:
        return f"crop-{origin['band']}"
    return f"photo-{origin['index']}"


def record(
    classified: dict,
    chosen: Sequence[tuple[Product, ProductRef]],
    images: Sequence[bytes],
    crops: Mapping[str, bytes],
    closeups: Sequence[tuple[str, Detail]] | None,
) -> RecordEvent:
    """The record of a try-on: its ``classified`` payload, each
    product with the image ``chosen`` for it, the product ``images`` and
    reference ``crops`` the refs come from, and the close-ups found per
    product id (None: not searched)."""
    files: dict[str, bytes] = {}
    products = []
    for product, ref in chosen:
        refs = []
        for r in product.refs:
            name = _file(r, images, crops)
            files[name] = r.image
            refs.append({"file": name, **_ref_entry(r)})
        found = None
        if closeups is not None:
            mine = [d for pid, d in closeups if pid == product.id]
            found = [_detail_entry(d) for d in mine]
            for k, d in enumerate(mine):
                found[k]["file"] = f"closeup-{product.id}-{k}"
                files[found[k]["file"]] = d.image
        index = next(i for i, r in enumerate(product.refs) if r is ref)
        products.append({"refs": refs, "chosen": index, "closeups": found})
    body = {
        "version": VERSION,
        "workflow": "tryon",
        "classification": classified,
        "products": products,
    }
    return RecordEvent(payload=body, files=files)


def events(
    classified: dict,
    chosen: Sequence[tuple[Product, ProductRef]],
    images: Sequence[bytes],
    crops: Mapping[str, bytes],
    closeups: Sequence[tuple[str, Detail]] | None,
) -> list[RecordEvent]:
    """The record (``record``), or none when it cannot be made (the run
    goes on without one)."""
    try:
        return [record(classified, chosen, images, crops, closeups)]
    except Exception as exc:
        logger.warning(f"try-on: no run record: {exc}")
        return []


def _ref_entry(r: ProductRef) -> dict:
    return {
        "view": r.view,
        "rank": r.rank,
        "presentation": r.presentation,
        "source": r.source,
    }


def _detail_entry(d: Detail) -> dict:
    return {
        "kind": d.kind,
        "text": d.text,
        "side": d.side,
        "where": d.where,
        "name": d.name,
        "photo": d.photo,
        "box": list(d.box),
    }


def _choice(value: Any, allowed: Any) -> Any:
    if value not in get_args(allowed):
        raise ValueError(f"unknown value {value!r}")
    return value


def _ref(entry: dict, files: Mapping[str, bytes]) -> ProductRef:
    return ProductRef(
        files[entry["file"]],
        _choice(entry["view"], View),
        int(entry.get("rank", 1)),
        _choice(entry["presentation"], Presentation),
        _choice(entry["source"], RefSource),
    )


def _detail(entry: dict, files: Mapping[str, bytes]) -> Detail:
    y0, x0, y1, x1 = (int(v) for v in entry.get("box", (0, 0, 0, 0)))
    return Detail(
        kind=str(entry["kind"]),
        text=str(entry.get("text", "")),
        side=str(entry.get("side", "other")),
        where=str(entry.get("where", "")) or "detail",
        image=files[entry["file"]],
        photo=int(entry.get("photo", 0)),
        box=(y0, x0, y1, x1),
        name=str(entry.get("name", "")),
    )


def _product(
    meta: dict, entry: dict, files: Mapping[str, bytes]
) -> tuple[Product, bytes, list[Detail] | None]:
    """One product, its best upload and its close-ups."""
    refs = [_ref(r, files) for r in entry["refs"]]
    category = _choice(meta["category"], Category)
    product = Product(meta["id"], category, meta.get("description", ""), refs)
    found = entry.get("closeups")
    closeups = None if found is None else [_detail(d, files) for d in found]
    return product, best_upload(product).image, closeups


def read(manifest: Mapping, files: Mapping[str, bytes]) -> Source:
    """A kept record (``manifest.json`` and its files by name) as the
    video's inputs; ``InputError`` when it is not a try-on record."""
    try:
        if manifest.get("version") != VERSION:
            raise ValueError(f"version {manifest.get('version')!r}")
        classified = manifest["classification"]
        metas, entries = classified["products"], manifest["products"]
        if len(entries) != len(metas):
            raise ValueError("one entry per product expected")
        found = [
            _product(meta, entry, files)
            for meta, entry in zip(metas, entries, strict=True)
        ]
        classification = Classification(
            _choice(classified["mode"], Mode),
            [product for product, _, _ in found],
            _choice(classified["framing"], Framing),
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise InputError(
            f"source_run: not a try-on run record ({exc})"
        ) from exc
    closeups = [c for _, _, c in found if c is not None]
    return Source(
        photos=[photo for _, photo, _ in found],
        classification=classification,
        closeups=closeups if len(closeups) == len(found) else None,
    )
