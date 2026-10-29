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

"""Workflow events, streamed as SSE (REST) or collected into JSON (MCP).

A workflow run always ends with exactly one ``CompleteEvent`` or one
``ErrorEvent``.
"""

import base64
import dataclasses
import json
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from genmedia4commerce.core import media
from genmedia4commerce.core.types import Classification, Verdict

__all__ = [
    "ClassifiedEvent",
    "CompleteEvent",
    "ErrorEvent",
    "Event",
    "ProgressEvent",
    "RecordEvent",
    "VariationEvent",
]


def _base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _jsonable(value: Any, blob: Callable[[bytes], Any] = _base64) -> Any:
    """``value`` as plain JSON types; ``blob`` turns bytes into a value."""
    if isinstance(value, bytes):
        return blob(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _jsonable(getattr(value, f.name), blob)
            for f in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        return {k: _jsonable(v, blob) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v, blob) for v in value]
    return value


@dataclass
class Event:
    """Base event; ``type`` is fixed by each subclass."""

    type: str = field(init=False, default="")

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict (bytes become base64 strings)."""
        return _jsonable(self)

    def to_sse(self) -> str:
        """Return the event as one SSE ``data:`` frame."""
        return f"data: {json.dumps(self.to_dict())}\n\n"


@dataclass
class ClassifiedEvent(Event):
    """What the classifier decided (e.g. ``Classification.to_dict()``).

    ``classification`` (with the photos) stays in the process for the
    workflow that streams these events; ``to_dict`` sends only the payload.
    """

    type: str = field(init=False, default="classified")
    payload: dict
    classification: Classification | None = field(
        default=None, repr=False, compare=False
    )

    def to_dict(self) -> dict:
        """Return ``type`` and ``payload`` only."""
        return {"type": self.type, "payload": _jsonable(self.payload)}


@dataclass
class RecordEvent(Event):
    """What the run's results were made from, for a later run to reuse.

    ``payload`` is JSON and names its images by keys of ``files`` (the
    image bytes, which stay in the process). ``core.runs.deliver`` keeps it
    as the run's ``manifest.json`` in output mode gcs and streams a
    ``ProgressEvent("record")`` instead; inline output keeps nothing.
    """

    type: str = field(init=False, default="record")
    payload: dict
    files: dict[str, bytes] = field(
        default_factory=dict, repr=False, compare=False
    )

    def to_dict(self) -> dict:
        """Return ``type`` and ``payload`` only."""
        return {"type": self.type, "payload": _jsonable(self.payload)}


@dataclass
class ProgressEvent(Event):
    """A pipeline stage started or finished.

    ``message`` is a short sentence for people (clients show it as the
    status); ``data`` carries stage facts.
    """

    type: str = field(init=False, default="progress")
    stage: str
    message: str
    data: dict = field(default_factory=dict)


@dataclass
class VariationEvent(Event):
    """One generated variation with its verdict.

    The media travels as bytes (``media``) or, once saved to Cloud Storage,
    as ``media_uri`` (``gs://``) and ``media_url`` (``GET /api/media``).
    ``fixed``: the media is a rejected image corrected by a fix pass and
    published (``Verdict.details["fixed"]``).
    """

    type: str = field(init=False, default="variation")
    index: int
    status: Literal["ready", "rejected", "error"]
    media: bytes | None
    verdict: Verdict | None
    score: float
    attempt: int = 1
    error: str | None = None
    extra: dict = field(default_factory=dict)
    # Seconds spent making (generate_s) and judging (check_s) it.
    timing: dict = field(default_factory=dict)
    media_uri: str | None = None
    media_url: str | None = None
    fixed: bool = False

    def to_dict(self) -> dict:
        """Return the dict with ``media`` as ``media_base64`` + ``media_mime``.

        ``media_base64``, ``media_uri`` and ``media_url`` are always present
        (null when unset); ``media_mime`` is set whenever there is media.
        """
        out = super().to_dict()
        out["media_base64"] = out.pop("media")
        if self.media:
            out["media_mime"] = media.sniff_mime(self.media)
        elif self.media_uri:
            # The app's own extensions first: Python before 3.13 does not
            # know .webp (a spin) without a system MIME table.
            ext = self.media_uri.rsplit(".", 1)[-1].lower()
            mime = next(
                (m for m, e in media.EXTENSIONS.items() if e == ext),
                mimetypes.guess_type(self.media_uri)[0],
            )
            out["media_mime"] = mime or "application/octet-stream"
        else:
            out["media_mime"] = None
        return out


@dataclass
class CompleteEvent(Event):
    """The run finished; indices refer to the variations streamed."""

    type: str = field(init=False, default="complete")
    ranked_indices: list[int]
    best_index: int | None


@dataclass
class ErrorEvent(Event):
    """The run failed and stopped."""

    type: str = field(init=False, default="error")
    message: str
    retryable: bool = False
