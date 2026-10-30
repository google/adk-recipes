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

"""The product catalogue: one local parquet file (``settings.catalog_path``).

Each row is a product, a preset model or a spin set (``kind``), with its
metadata, its WebP images and, for most products, a 256-dimension unit
embedding. The file is read once, on first use, never on import.

Images are referenced as ``catalog:<id>`` (the first image) or
``catalog:<id>/<n>`` (image ``n``, from 0). Search is in
``core.catalog_search``.
"""

import functools
import io
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
from PIL import Image

from genmedia4commerce.core.errors import ConfigError, InputError
from genmedia4commerce.core.settings import get_settings

__all__ = [
    "FAMILIES",
    "REF_PREFIX",
    "THUMB_SIDE",
    "Catalog",
    "audiences",
    "featured",
    "get_catalog",
    "image",
    "is_ref",
    "item",
    "models",
    "parse_ref",
    "ref",
    "source_ref",
    "spin_sets",
    "thumbnail",
]

logger = logging.getLogger(__name__)

REF_PREFIX = "catalog:"
THUMB_SIDE = 256
#: Display order of the product families.
FAMILIES = (
    "tops",
    "dresses",
    "bottoms",
    "outerwear",
    "shoes",
    "bags_accessories",
    "other",
)
_META = (
    "id",
    "kind",
    "title",
    "description",
    "category",
    "family",
    "audience",
    "color",
    "color_family",
    "season",
    "style",
    "featured",
)
_lock = threading.Lock()
_loaded: dict[Path, "Catalog"] = {}


@dataclass(frozen=True, eq=False)
class Catalog:
    """The loaded file: one ``rows`` entry (metadata, ``images`` = count)
    per item, its images, and the unit embeddings (zeros where
    ``has_embedding`` is False)."""

    rows: list[dict[str, Any]]
    images: list[list[bytes]]
    embeddings: np.ndarray
    has_embedding: np.ndarray
    by_id: dict[str, int]
    by_source: dict[str, int]


def _embeddings(table: Any) -> tuple[np.ndarray, np.ndarray]:
    column = table.column("embedding").combine_chunks()
    valid = np.asarray(column.is_valid(), dtype=bool)
    dims = column.type.list_size
    flat = column.values.to_numpy(zero_copy_only=False).astype(np.float32)
    start = column.offset
    matrix = flat.reshape(-1, dims)[start : start + len(column)].copy()
    matrix[~valid] = 0.0
    return matrix, valid


def _load(path: Path) -> Catalog:
    if not path.is_file():
        raise ConfigError(f"The catalogue file is missing: {path}")
    table = pq.read_table(path)
    names = set(table.column_names)
    meta = {k: table.column(k).to_pylist() for k in _META if k in names}
    images = [list(x or []) for x in table.column("images").to_pylist()]
    rows = [
        {**{k: meta[k][i] for k in meta}, "images": len(images[i])}
        for i in range(table.num_rows)
    ]
    sources = (
        table.column("source_image").to_pylist()
        if "source_image" in names
        else []
    )
    matrix, valid = _embeddings(table)
    logger.info(
        f"Catalogue loaded from {path}: {len(rows)} items, "
        f"{int(valid.sum())} with embeddings"
    )
    return Catalog(
        rows=rows,
        images=images,
        embeddings=matrix,
        has_embedding=valid,
        by_id={r["id"]: i for i, r in enumerate(rows)},
        by_source={s: i for i, s in enumerate(sources) if s},
    )


def get_catalog() -> Catalog:
    """The catalogue of ``settings.catalog_path``, read once (thread-safe).

    ``ConfigError`` when the file is missing.
    """
    path = get_settings().catalog_path
    with _lock:
        if path not in _loaded:
            _loaded.clear()  # one file at a time (tests change the path)
            _loaded[path] = _load(path)
        return _loaded[path]


def item(item_id: str) -> dict[str, Any]:
    """The metadata of one item; ``InputError`` for an unknown id."""
    cat = get_catalog()
    if item_id not in cat.by_id:
        raise InputError(f"no catalogue item {item_id!r}")
    return cat.rows[cat.by_id[item_id]]


def _of_kind(kind: str) -> list[dict[str, Any]]:
    return [r for r in get_catalog().rows if r["kind"] == kind]


def audiences(audience: Sequence[str] | str) -> set[str]:
    """The audiences to keep for ``audience`` ("women", a list or a
    comma-separated mix), unisex included; empty for any audience."""
    values = audience.split(",") if isinstance(audience, str) else audience
    wanted = {a.strip().lower() for a in values if a.strip()}
    return wanted | {"unisex"} if wanted else set()


def featured(
    audience: Sequence[str] | str = (), family: str = ""
) -> list[dict[str, Any]]:
    """The featured products, by family, for ``audience`` (``audiences``)
    and ``family`` when given."""
    rank: dict[str | None, int] = {f: i for i, f in enumerate(FAMILIES)}
    keep = audiences(audience)
    out = [
        r
        for r in _of_kind("product")
        if r.get("featured")
        and (not keep or r.get("audience") in keep)
        and (not family or r.get("family") == family)
    ]
    return sorted(out, key=lambda r: rank.get(r.get("family"), len(rank)))


def models() -> list[dict[str, Any]]:
    """The preset models (people to dress)."""
    return _of_kind("model")


def spin_sets() -> list[dict[str, Any]]:
    """The products with several views, for 360° Spin."""
    return _of_kind("spin")


def ref(item_id: str, n: int = 0) -> str:
    """The ref of image ``n`` of an item."""
    return f"{REF_PREFIX}{item_id}" + (f"/{n}" if n else "")


def is_ref(value: object) -> bool:
    """Whether ``value`` is a ``catalog:`` ref (not whether it exists)."""
    return isinstance(value, str) and value.startswith(REF_PREFIX)


def parse_ref(value: str) -> tuple[str, int]:
    """``(id, n)`` of an existing catalogue image; ``InputError`` otherwise."""
    item_id, sep, index = value.removeprefix(REF_PREFIX).partition("/")
    if not is_ref(value) or (sep and not index.isdigit()):
        raise InputError(f"not a catalogue image ref: {value[:120]!r}")
    n = int(index or 0)
    if not 0 <= n < item(item_id)["images"]:
        raise InputError(f"no image {n} of catalogue item {item_id!r}")
    return item_id, n


def image(item_id: str, n: int = 0) -> bytes:
    """Image ``n`` of an item (WebP); ``InputError`` when it has none."""
    parse_ref(ref(item_id, n))
    cat = get_catalog()
    return cat.images[cat.by_id[item_id]][n]


@functools.lru_cache(maxsize=512)
def _thumbnail(cat: Catalog, item_id: str, n: int, side: int) -> bytes:
    del cat  # part of the cache key only: a new file gets new thumbnails
    with Image.open(io.BytesIO(image(item_id, n))) as source:
        img = source.copy()
    img.thumbnail((side, side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=80)
    return buf.getvalue()


def thumbnail(item_id: str, n: int = 0, side: int = THUMB_SIDE) -> bytes:
    """A WebP of image ``n``, at most ``side`` pixels a side (cached)."""
    return _thumbnail(get_catalog(), item_id, n, side)


def source_ref(uri: str) -> str | None:
    """The ``catalog:`` ref of an item whose ``source_image`` is ``uri``
    (the image an older catalogue served), or ``None``."""
    cat = get_catalog()
    index = cat.by_source.get(uri)
    return ref(cat.rows[index]["id"]) if index is not None else None
