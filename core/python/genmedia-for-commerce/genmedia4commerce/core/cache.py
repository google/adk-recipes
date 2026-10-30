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

"""A cache of preprocessing results in the media bucket.

An entry lives in the folder ``<prefix><key>/`` of the media bucket
(``PRODUCT_CACHE``, for what comes from product photos only: nothing
derived from a person photo is ever cached; top-level, so no image
reference rule, ``gcs.resolve_image``, ``studio.media.allowed_ref`` or
``GET /api/media``, ever reaches it). The key (``key``) hashes what the
result depends on: the callers' parts (the input images' ``digest``,
options, model ids), the format ``VERSION`` and the code
(``code_fingerprint``: any change to the package's source starts a new
cache).

An entry is a JSON index, ``<name>.json``, and the images it names, one blob
each (``<entry id>-<token>.<ext>``). The value is written field by field
from its dataclasses (``core._codec``); the caller's input images are not
stored but named by position, and come back as the caller's own objects.
Writes (``save``) put the blobs first and the index last, create-only
(``gcs.create``: the first writer wins, a later one keeps what is there);
``replace`` overwrites the index instead (a refresh, or an index that could
not be used). Blob names are new on every write, so a reader of the old
index still finds its own blobs. Reads (``load``) treat anything wrong (no
index, an index of another version or key, a missing blob, an error) as a
miss. Objects under the prefix are deleted by the bucket's lifecycle rule
(``infra/terraform/storage.tf``).

``refreshing`` marks a run whose reads are skipped (the Studio's "Try
again"): it recomputes and replaces the entries.
"""

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from genmedia4commerce.core import _codec, gcs
from genmedia4commerce.core.media import EXTENSIONS, sniff_mime
from genmedia4commerce.core.retry import ContextPool
from genmedia4commerce.core.settings import get_settings

__all__ = [
    "PRODUCT_CACHE",
    "VERSION",
    "Draft",
    "Lookup",
    "code_fingerprint",
    "digest",
    "draft",
    "in_background",
    "key",
    "load",
    "refresh_requested",
    "refreshing",
    "save",
]

logger = logging.getLogger(__name__)

PRODUCT_CACHE = "product_cache/"
VERSION = 2  # 2: garments have a span (``Product.span``)
_PACKAGE = Path(__file__).parents[1]
_SOURCES = (".py", ".md", ".txt")  # code and prompts
_DOWNLOADS = 8  # blobs read at once
_REFRESH: ContextVar[bool] = ContextVar("cache_refresh", default=False)
_WRITES: set[asyncio.Task] = set()  # running background writes


def digest(data: bytes) -> str:
    """The first 16 hex characters of the SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()[:16]


@functools.lru_cache(maxsize=1)
def code_fingerprint() -> str:
    """A hash of every source file of the package (code and prompts, by
    path), computed once per process."""
    h = hashlib.sha256()
    files = sorted(
        p for p in _PACKAGE.rglob("*") if p.suffix in _SOURCES and p.is_file()
    )
    for path in files:
        data = path.read_bytes()
        name = path.relative_to(_PACKAGE).as_posix()
        h.update(f"{name}\0{len(data)}\0".encode() + data)
    return h.hexdigest()[:16]


def key(**parts: Any) -> str:
    """The folder name of an entry: a hash of ``parts`` (JSON-able), the
    format ``VERSION`` and the code (``code_fingerprint``)."""
    text = json.dumps(
        {"version": VERSION, "code": code_fingerprint(), **parts},
        sort_keys=True,
    )
    return hashlib.sha256(text.encode()).hexdigest()[:32]


@contextlib.contextmanager
def refreshing(on: bool = True) -> Iterator[None]:
    """With ``on``, the runs of the block read no entry: they recompute and
    replace them."""
    token = _REFRESH.set(on)
    try:
        yield
    finally:
        _REFRESH.reset(token)


def refresh_requested() -> bool:
    """Whether this run is a refresh (``refreshing``)."""
    return _REFRESH.get()


def _folder(prefix: str, entry_key: str) -> str:
    return f"{prefix}{entry_key}/"


def _uri(path: str) -> str:
    return f"gs://{get_settings().media_bucket}/{path}"


@dataclass
class Lookup:
    """What ``load`` read: the index (None on a miss) and its blobs, and
    whether an index is there that cannot be used (a write replaces it)."""

    index: dict | None = None
    blobs: dict[str, bytes] = field(default_factory=dict)
    stale: bool = False

    def decode(
        self,
        expect: type,
        inputs: Sequence[bytes | None] = (),
        types: Sequence[type] = (),
    ) -> Any:
        """The value, an ``expect``, rebuilt around ``inputs`` with the
        dataclasses ``types``; None on a miss or when it does not fit
        (logged; the lookup is then ``stale``)."""
        if self.index is None:
            return None
        known = {t.__name__: t for t in (expect, *types)}
        try:
            value = _codec.decode(
                self.index["value"], inputs, self.blobs, known
            )
            if not isinstance(value, expect):
                raise TypeError(f"not a {expect.__name__}")
        except Exception as exc:  # anything wrong: recompute, then replace
            logger.info(f"cache: entry not usable, recomputing: {exc!r}")
            self.index, self.stale = None, True
            return None
        return value


def _blobs(folder: str, names: Mapping[str, str]) -> dict[str, bytes]:
    if any(not isinstance(n, str) or "/" in n for n in names.values()):
        raise ValueError("bad blob name")
    if not names:
        return {}
    with ContextPool(max_workers=min(_DOWNLOADS, len(names))) as pool:
        data = list(
            pool.map(lambda n: gcs.download(_uri(folder + n)), names.values())
        )
    return dict(zip(names, data, strict=True))


def load(prefix: str, entry_key: str, name: str) -> Lookup:
    """Read entry ``name`` of ``<prefix><entry_key>/`` (``Lookup.decode``
    gives its value); a miss when there is none or it cannot be read
    (logged)."""
    path = f"{_folder(prefix, entry_key)}{name}.json"
    try:
        raw = gcs.download(_uri(path))
    except Exception as exc:  # NotFound, or storage out of reach: a miss
        logger.info(f"cache: miss {path} ({type(exc).__name__})")
        return Lookup()
    try:
        index = json.loads(raw)
        if index.get("version") != VERSION or index.get("key") != entry_key:
            raise ValueError("another version or key")
        blobs = _blobs(_folder(prefix, entry_key), index["blobs"])
    except Exception as exc:  # anything wrong: recompute, then replace it
        logger.info(f"cache: {path} not usable, recomputing: {exc!r}")
        return Lookup(stale=True)
    logger.info(f"cache: hit {path}")
    return Lookup(index, blobs)


@dataclass
class Draft:
    """An entry ready to be written (``draft``): its value encoded when it
    was drafted, and its blobs."""

    prefix: str
    key: str
    name: str
    data: Any
    blobs: dict[str, bytes]
    replace: bool = False


def draft(
    prefix: str,
    entry_key: str,
    name: str,
    value: Any,
    *,
    inputs: Sequence[bytes | None] = (),
    replace: bool = False,
) -> Draft:
    """Entry ``name`` of ``<prefix><entry_key>/`` holding ``value``, the
    ``inputs`` named by position; ``replace`` overwrites an index there."""
    data, blobs = _codec.encode(value, inputs)
    return Draft(prefix, entry_key, name, data, blobs, replace)


def save(d: Draft, **extra: Any) -> str | None:
    """Write ``d`` (blobs first, the index last, with ``extra`` fields);
    its entry id, or None when an index was there already (create-only)
    or the write failed (logged, never raised)."""
    folder, entry = _folder(d.prefix, d.key), uuid.uuid4().hex[:12]
    path = f"{folder}{d.name}.json"
    names = {
        t: f"{entry}-{t}.{EXTENSIONS.get(sniff_mime(b), 'bin')}"
        for t, b in d.blobs.items()
    }
    index = {"version": VERSION, "key": d.key, "entry": entry, "blobs": names}
    body = json.dumps({**index, "value": d.data, **extra}).encode()
    bucket = get_settings().media_bucket
    try:
        for token, data in d.blobs.items():
            mime = sniff_mime(data)
            gcs.upload(
                data,
                bucket=bucket,
                path=folder + names[token],
                content_type=mime,
            )
        if d.replace:
            gcs.upload(
                body, bucket=bucket, path=path, content_type="application/json"
            )
        elif not gcs.create(body, _uri(path), content_type="application/json"):
            logger.info(f"cache: {path} already stored")
            return None
    except Exception as exc:  # a cache write never fails a run
        logger.warning(f"cache: could not store {path}: {exc!r}")
        return None
    logger.info(f"cache: stored {path}")
    return entry


def _done(task: asyncio.Task) -> None:
    _WRITES.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning(f"cache: background write failed: {task.exception()!r}")


def in_background(write: Callable[[], object]) -> None:
    """Run ``write`` in a thread without waiting for it (the event loop
    keeps the task until it ends)."""
    task = asyncio.get_running_loop().create_task(asyncio.to_thread(write))
    _WRITES.add(task)
    task.add_done_callback(_done)
