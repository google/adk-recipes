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

"""Dataclasses with images to JSON and back, keeping which image is which
(used by ``core.cache``).

Any value of dataclasses, lists, tuples, string-keyed dicts, strings,
numbers, booleans, None and bytes is written field by field from its
dataclass, so a new field needs no change here. Each bytes object is either
one of the ``inputs`` (by identity: ``{"$input": position}``) or a blob
(``{"$blob": token}``), one blob per object however often it appears.
``decode`` gives back the caller's own input at each position and one bytes
object per blob, so code that matches images by identity (``is``) finds
the same pairs as in the encoded value.
"""

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["decode", "encode"]

_SCALARS = (str, int, float, bool, type(None))


def encode(
    value: Any, inputs: Sequence[bytes | None] = ()
) -> tuple[Any, dict[str, bytes]]:
    """``value`` as JSON-ready data, and its blobs by token. ``TypeError``
    for anything else than the types above."""
    positions: dict[int, int] = {}
    for i, image in enumerate(inputs):
        if image is not None:
            positions.setdefault(id(image), i)
    tokens: dict[int, str] = {}
    blobs: dict[str, bytes] = {}

    def walk(v: Any) -> Any:
        if isinstance(v, bytes):
            if id(v) in positions:
                return {"$input": positions[id(v)]}
            token = tokens.setdefault(id(v), f"b{len(tokens)}")
            blobs[token] = v
            return {"$blob": token}
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            fields = {
                f.name: walk(getattr(v, f.name)) for f in dataclasses.fields(v)
            }
            return {"$type": type(v).__name__, "fields": fields}
        if isinstance(v, tuple):
            return {"$tuple": [walk(x) for x in v]}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict) and all(isinstance(k, str) for k in v):
            return {"$dict": {k: walk(x) for k, x in v.items()}}
        if isinstance(v, _SCALARS):
            return v
        raise TypeError(f"cannot encode a {type(v).__name__}")

    return walk(value), blobs


def decode(
    data: Any,
    inputs: Sequence[bytes | None],
    blobs: Mapping[str, bytes],
    types: Mapping[str, type],
) -> Any:
    """The value ``encode`` made ``data`` from, around these ``inputs`` and
    ``blobs``; only dataclasses named in ``types``. ``ValueError``,
    ``KeyError``, ``IndexError`` or ``TypeError`` when ``data`` does not
    fit."""

    def walk(d: Any) -> Any:
        if isinstance(d, list):
            return [walk(x) for x in d]
        if not isinstance(d, dict):
            return d
        if "$input" in d:
            i = d["$input"]
            image = inputs[i] if isinstance(i, int) and i >= 0 else None
            if not isinstance(image, bytes):
                raise ValueError(f"no input at {i!r}")
            return image
        if "$blob" in d:
            return blobs[d["$blob"]]
        if "$tuple" in d:
            return tuple(walk(x) for x in d["$tuple"])
        if "$dict" in d:
            return {k: walk(x) for k, x in d["$dict"].items()}
        if "$type" in d:
            fields = {k: walk(x) for k, x in d["fields"].items()}
            return types[d["$type"]](**fields)
        raise ValueError(f"unknown entry {sorted(d)}")

    return walk(data)
