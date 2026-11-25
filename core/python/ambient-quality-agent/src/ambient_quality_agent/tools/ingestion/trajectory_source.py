# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The protocol every ATIF source implements, and what a source returns.

A source lists the trajectories in a sweep window and returns complete
documents: external subagents embedded, and each readable `source.path`
resolved to an attachment AQuA can copy (unreadable references go to
`markers`). Everything after the source is shared by all sources: validation,
the trajectory id, copying and annotation. Copying reads an external
attachment through the source's `read_attachment` and decodes an inline
`data:` URL with `decode_inline_attachment`, which needs no source. A source
writes nothing into `extra.aqua`: the markers it found beside the document go
to `annotate_document`.

This module lives in `ambient_quality_agent`, not in `aqua_atif`, because
sources run inside AQuA and only converter plugins are barred from importing
AQuA code.

Wiring::

    ingestion (the caller) ──► decode_inline_attachment()
        │ count_trajectories(), list_trajectory_keys(), load_trajectories(),
        │ read_attachment()
        ▼
    TrajectorySource (Protocol)
        ▲ implements
        └── atif_source.AtifFileSource ──► atif_files: GCS, local directory

Caller sequence for one sweep window::

    window = source.count_trajectories(start, end)
    sampled = source.list_trajectory_keys(start, end, limit=sample_size)
    for item in source.load_trajectories([entry.key for entry in sampled]):
        if isinstance(item, FailedDocument):
            ...  # record item.reason (e.g., "too_large", "invalid JSON")
        else:
            # validate item.document, copy attachments via read_attachment()
            # or decode_inline_attachment(), then annotate with item.markers
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime as dt
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import unquote_to_bytes

from ambient_quality_agent.tools.ingestion.models import SamplingRecord
from aqua_atif import JsonPath, PendingMissingMarker, Reason

# Why a root document is not ingested. A reason is a string, since
# validation's reasons are free text; these are the ones a source or ingestion
# gives.
TOO_LARGE = "too_large"
"""The rejection reason of a root over the document cap."""

PATH_TRAVERSAL = "path_traversal"
"""Rejection reason for a root file whose symbolic link leaves the prefix."""

READ_ERROR = "read_error"
"""The rejection reason of a root file that could not be read, followed by the
HTTP status when there is one, such as `read_error 403`."""

LOAD_ERROR = "could not load the document"
"""The rejection reason of a root that failed for an unexpected cause; the
cause's type is logged."""

MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024
"""Default maximum size in bytes of one attachment, including a decoded
`data:` URL."""

DATA_URL = "data_url"
"""The marker detail of a malformed `data:` URL."""

# A well-formed base64 payload, padding included.
_BASE64_RE = re.compile(
    r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?"
)
# A `%` that does not start a two-digit escape.
_BAD_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")

# How much of a percent-encoded payload is decoded at a time, so decoding
# never builds one Python object per escape for the whole payload.
_UNQUOTE_CHUNK = 1 << 20


@dataclasses.dataclass(frozen=True)
class ExternalAttachment:
    """An attachment stored outside the JSON that the source reads for copying.

    Attributes:
        uri: Where the attachment is: for a file source, its `gs://` URL or its
            absolute local path after symbolic links are resolved.
        key: The attachment's name in its source; for a file source, its path
            relative to the attached prefix. `read_attachment` reads it.
        size: Its size in bytes; None when the source did not report one.
    """

    uri: str
    key: str
    size: int | None


@dataclasses.dataclass(frozen=True)
class InlineAttachment:
    """A well-formed inline `data:` URL within the attachment cap.

    AQuA uploads its bytes, which `decode_inline_attachment` decodes.

    Attributes:
        uri: The URL as written.
        size: Its decoded size in bytes.
    """

    uri: str
    size: int


ResolvedAttachment = ExternalAttachment | InlineAttachment
"""Where a readable attachment's bytes live."""


@dataclasses.dataclass(frozen=True)
class LoadedDocument:
    """One root document that a source loaded.

    Attributes:
        key: The key `list_trajectory_keys` returned; for a file source, the
            root file's path relative to the attached prefix.
        document: The parsed JSON object with external subagents embedded.
            Not validated: ingestion validates it, embedded subagents
            included.
        source_uri: Where the root file is: a `gs://` URL or a local path.
            None for a source that has no URIs.
        source_updated_at: The root file's object time, in UTC.
        locations: For each readable content part with a `source.path` (other
            than `http://` or `https://` links, which stay untouched in the
            document), keyed by the part's path from the document root (such as
            `("steps", 0, "message", 1)`), the `ExternalAttachment` or
            `InlineAttachment` to copy. Unreadable references go in `markers`.
        markers: The markers the source found: parsing's markers for replaced
            lone surrogates, in the root and in embedded files, and a marker
            for each part or subagent reference it did not read. Pass them to
            `annotate_document`; they are not written into the document.
    """

    key: str
    document: dict[str, Any]
    source_uri: str | None = None
    source_updated_at: dt.datetime | None = None
    locations: Mapping[JsonPath, ResolvedAttachment] = dataclasses.field(
        default_factory=dict
    )
    markers: tuple[PendingMissingMarker, ...] = ()


@dataclasses.dataclass(frozen=True)
class FailedDocument:
    """One root document that a source could not load.

    Attributes:
        key: The key `list_trajectory_keys` returned.
        reason: Why loading failed, such as "too_large", "read_error 403" or
            "invalid JSON": the rejection reason of the document.
        source_uri: Where the root file is: a `gs://` URL or a local path.
            None for a source that has no URIs.
        source_updated_at: The root file's object time, in UTC; None when
            the file was not read.
    """

    key: str
    reason: str
    source_uri: str | None = None
    source_updated_at: dt.datetime | None = None


class AttachmentUnreadableError(Exception):
    """An attachment that `read_attachment` could not read.

    Attributes:
        reason: The marker reason for the part: `read_error`, `too_large`,
            `invalid_content` or `path_traversal`.
        detail: The marker detail, such as `404` or the size; None when there
            is none.
    """

    def __init__(self, reason: Reason, detail: str | None = None) -> None:
        """Initializes the error.

        Args:
            reason: The marker reason.
            detail: The marker detail.
        """
        super().__init__(f"{reason} {detail or ''}".strip())
        self.reason = reason
        self.detail = detail


def _measure_data_url(url: str, max_bytes: int) -> tuple[bool, int, int]:
    """Checks a `data:` URL's form and measures it, without decoding it.

    The work is linear in the URL's length and copies none of it.

    Args:
        url: The URL, starting with `data:` in any case.
        max_bytes: The attachment cap.

    Returns:
        Whether the payload is base64, where the payload starts in `url`, and
        its decoded size.

    Raises:
        AttachmentUnreadableError: `invalid_content` / `data_url` when the URL
            has no comma, is not ASCII, or has malformed base64 or escapes;
            `too_large` with the decoded size when it is over `max_bytes`.
    """
    comma = url.find(",")
    if comma == -1 or not url.isascii():
        raise AttachmentUnreadableError(Reason.INVALID_CONTENT, DATA_URL)
    start = comma + 1
    length = len(url) - start
    is_base64 = url[len("data:") : comma].lower().endswith(";base64")
    if is_base64:
        if _BASE64_RE.fullmatch(url, start) is None:
            raise AttachmentUnreadableError(Reason.INVALID_CONTENT, DATA_URL)
        padding = 2 if url.endswith("==") else 1 if url.endswith("=") else 0
        size = length // 4 * 3 - min(padding, length)
    else:
        if _BAD_ESCAPE_RE.search(url, start) is not None:
            raise AttachmentUnreadableError(Reason.INVALID_CONTENT, DATA_URL)
        size = length - 2 * url.count("%", start)
    if size > max_bytes:
        raise AttachmentUnreadableError(Reason.TOO_LARGE, str(size))
    return is_base64, start, size


def check_data_url(url: str, max_bytes: int) -> InlineAttachment:
    """Checks that a `data:` URL is well formed and within the attachment cap.

    Args:
        url: The URL, starting with `data:`.
        max_bytes: The attachment cap.

    Returns:
        An `InlineAttachment` with the URL and its decoded size.

    Raises:
        AttachmentUnreadableError: `invalid_content` when the URL is
            malformed, `too_large` when it is over `max_bytes`.
    """
    _, _, size = _measure_data_url(url, max_bytes)
    return InlineAttachment(uri=url, size=size)


def decode_inline_attachment(
    attachment: InlineAttachment, max_bytes: int
) -> bytes:
    """Decodes the bytes of an inline `data:` URL, for copying.

    The bytes are in the document, so no source is read: the caller of any
    source decodes them here.

    Args:
        attachment: An `InlineAttachment` from `LoadedDocument.locations`.
        max_bytes: The attachment cap; a URL over it is refused before it is
            decoded.

    Returns:
        The decoded bytes, at most `max_bytes`.

    Raises:
        AttachmentUnreadableError: `invalid_content` when the URL is
            malformed, `too_large` when it is over `max_bytes`.
    """
    url = attachment.uri
    is_base64, start, _ = _measure_data_url(url, max_bytes)
    if is_base64:
        try:
            return base64.b64decode(url[start:], validate=True)
        except (binascii.Error, ValueError) as err:
            raise AttachmentUnreadableError(
                Reason.INVALID_CONTENT, DATA_URL
            ) from err
    decoded = bytearray()
    pos = start
    limit = len(url)
    while pos < limit:
        end = min(pos + _UNQUOTE_CHUNK, limit)
        # Do not split an escape across two chunks.
        escape = url.rfind("%", max(pos, end - 2), end)
        if escape > pos and end < limit:
            end = escape
        decoded += unquote_to_bytes(url[pos:end])
        pos = end
    return bytes(decoded)


@dataclasses.dataclass(frozen=True)
class WindowCount:
    """How many root documents a source found in a window.

    Attributes:
        count: The root documents found.
        truncated: Whether the source stopped listing before the end, so
            `count` covers only what it listed and the window may hold more.
    """

    count: int
    truncated: bool = False


@dataclasses.dataclass(frozen=True)
class SampledKey:
    """One sampled root document key and how the source picked it.

    Attributes:
        key: Key to pass to `TrajectorySource.load_trajectories`.
        sampling: How the trajectory was picked (its inclusion probability, and
            weight and risk signals under weighted sampling), or None when the
            probability is unknown (for example, after a truncated listing).
    """

    key: str
    sampling: SamplingRecord | None = None


class TrajectorySource(Protocol):
    """Lists and loads the ATIF documents of one observed agent.

    A source that needs the agent's name, as one that queries a store of
    several agents, takes it when it is built.
    """

    def count_trajectories(
        self, start: dt.datetime, end: dt.datetime
    ) -> WindowCount:
        """Counts the root documents in the window `[start, end]`.

        Args:
            start: Start of the window, inclusive.
            end: End of the window, inclusive.

        Returns:
            How many root documents are in the window, and whether the
            source stopped before it had listed them all.
        """
        ...

    def list_trajectory_keys(
        self,
        start: dt.datetime,
        end: dt.datetime,
        limit: int,
        *,
        sampling_seed: str | None = None,
    ) -> list[SampledKey]:
        """Lists a sample of the root documents in a window with how each was picked.

        A sweep counts a window first and asks for its sample later, with a
        limit that depends on the count. File sources sample uniformly;
        query-backed sources may draw a weighted sample and populate each
        `SampledKey.sampling` with its inclusion probability, weight, and
        risk signals.

        Args:
            start: Start of the window, inclusive.
            end: End of the window, inclusive.
            limit: Maximum number of keys to return.
            sampling_seed: Optional seed for reproducible sampling; None draws
                using the source's default randomness.

        Returns:
            At most `limit` `SampledKey` entries, sorted by `key`. When the
            window listing was truncated, the sample covers the listed part
            only and carries `sampling=None`.
        """
        ...

    def load_trajectories(
        self, keys: Sequence[str]
    ) -> Iterator[LoadedDocument | FailedDocument]:
        """Loads documents by key, one at a time.

        A document is loaded when the consumer asks for the next item, so a
        fetcher holds one document at a time and can check a memory guard
        between two documents.

        Args:
            keys: Keys from `list_trajectory_keys`.

        Returns:
            An iterator of one document per key, in the order of `keys`. It
            never raises for one document or one reference: a document that
            cannot be loaded is a `FailedDocument`, and a reference that
            cannot be read gets a marker.
        """
        ...

    def read_attachment(self, attachment: ExternalAttachment) -> bytes:
        """Reads the bytes of an external attachment the source resolved.

        Args:
            attachment: An `ExternalAttachment` from `LoadedDocument.locations`
                of this source.

        Returns:
            The attachment's bytes as stored, at most the attachment cap.

        Raises:
            AttachmentUnreadableError: With the marker reason when the bytes
                cannot be read, for any cause.
        """
        ...
