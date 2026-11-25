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

"""`AtifFileSource`: ATIF trajectory files under a GCS prefix or a directory.

One attached prefix holds one agent's trajectories. Because ATIF step
timestamps are optional, a document falls in the sweep window based on its
object time. The source finds root files by name without reading them, matching
`*.atif.json` directly in the prefix or in `trajectories/` under it.

For any other layout, `atif_glob` replaces that rule (for example,
`**/agent/trajectory.json` finds Harbor trial roots).

Root matching (default rule vs `atif_glob="**/agent/trajectory.json"`)::

    t-001.atif.json                  default: root
    trajectories/t-001.atif.json     default: root
    a/trajectories/t-001.atif.json   default: skipped (nested too deep)
    subagents/t-001-qa.json          default: skipped
    trial-1/agent/trajectory.json    default: skipped; matched by the glob

Counting and then listing the same window lists the prefix once. Roots are
loaded one at a time, each when the caller asks for the next one. Loading a
root:

1. Reads the file whole, refusing one over the document cap ("too_large").
2. Parses it strictly ("invalid JSON" past 256 levels of nesting), refusing
   JSON that is not an object ("not an ATIF document").
3. Resolves every reference and embeds external subagents
   (`atif_references`).

Validation, the trajectory id and copying run later, in ingestion, for every
source alike. A problem with one document or one reference never raises; a
failed document becomes a `FailedDocument` with the reason, and an unreadable
reference gets a marker.

Wiring::

    ingestion (the caller)
        │ count_trajectories(), list_trajectory_keys(),
        │ load_trajectories(), read_attachment()
        ▼
    AtifFileSource ──► aqua_atif: parse_document()
        │
        ├─► atif_references.DocumentResolver
        │        │   ──► aqua_atif: validate_document()
        │        │   ──► trajectory_source: check_data_url()
        ▼        ▼
    atif_files.AtifFiles ◄── GcsAtifFiles, LocalAtifFiles
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import re
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from ambient_quality_agent.tools.ingestion.atif_files import (
    AtifFiles,
    ObjectReadError,
    ObjectTooLargeError,
    PathOutsidePrefixError,
    create_atif_files,
    normalize_utc,
)
from ambient_quality_agent.tools.ingestion.atif_references import (
    AtifLimits,
    DocumentResolver,
)
from ambient_quality_agent.tools.ingestion.models import SamplingRecord
from ambient_quality_agent.tools.ingestion.trajectory_source import (
    LOAD_ERROR,
    PATH_TRAVERSAL,
    READ_ERROR,
    TOO_LARGE,
    AttachmentUnreadableError,
    ExternalAttachment,
    FailedDocument,
    LoadedDocument,
    SampledKey,
    WindowCount,
)
from aqua_atif import DocumentRejectedError, Reason, parse_document
from aqua_atif.validation import NOT_ATIF

_LOGGER = logging.getLogger(__name__)


MAX_GLOB_DOUBLE_STARS = 2
"""Maximum number of `**` wildcards allowed in an `atif_glob`, to keep
matching fast."""


def build_glob_pattern(pattern: str) -> re.Pattern[str]:
    """Builds the regular expression of an `atif_glob`.

    `*` and `?` match within one path segment, `**` across segments, and
    `[...]` a character class (`[!...]` negated). Leading and trailing `/` are
    ignored.

    Args:
        pattern: The glob, relative to the prefix.

    Returns:
        A pattern that matches whole keys.

    Raises:
        ValueError: When the glob is not valid, such as a class with a bad
            range (`[z-a]`) or a nested `[` (`[[:alpha:]]`), which globs here
            do not support, or it holds more than `MAX_GLOB_DOUBLE_STARS`
            `**`.
    """
    trimmed = pattern.strip("/")
    # Each `**` multiplies the ways a key can be split between the pattern's
    # parts, so a glob with many of them would make every match slow.
    if trimmed.count("**") > MAX_GLOB_DOUBLE_STARS:
        raise ValueError(
            f"atif_glob {pattern!r} has more than {MAX_GLOB_DOUBLE_STARS} '**'."
        )
    parts: list[str] = []
    index = 0
    while index < len(trimmed):
        if trimmed.startswith("**/", index):
            # Zero or more whole segments. `[^/]+` cannot cross a `/`, so a key
            # splits into them one way only, and repeated `**/` are one.
            while trimmed.startswith("**/", index + 3):
                index += 3
            parts.append("(?:[^/]+/)*")
            index += 3
        elif trimmed.startswith("/**", index) and index + 3 == len(trimmed):
            parts.append("(?:/.*)?")
            index += 3
        elif trimmed.startswith("**", index):
            parts.append(".*")
            index += 2
        elif trimmed[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif trimmed[index] == "?":
            parts.append("[^/]")
            index += 1
        elif (
            trimmed[index] == "["
            and (
                end := trimmed.find(
                    "]",
                    index + 3
                    if trimmed.startswith(("!", "^"), index + 1)
                    else index + 2,
                )
            )
            != -1
        ):
            inner = trimmed[index + 1 : end]
            if "[" in inner:
                raise ValueError(
                    f"atif_glob {pattern!r}: a character class cannot hold '['."
                )
            if inner.startswith("!"):
                inner = "^" + inner[1:]
            escaped = inner.replace("\\", "\\\\")
            parts.append(f"(?!/)[{escaped}]")
            index = end + 1
        else:
            parts.append(re.escape(trimmed[index]))
            index += 1
    try:
        return re.compile("".join(parts))
    except re.error as err:
        raise ValueError(f"atif_glob {pattern!r} is not valid: {err}.") from err


ATIF_ROOT_RULE = (
    "*.atif.json directly in the prefix or in a trajectories/ directory "
    "directly under it"
)
"""`is_root_trajectory_key` in words, for operators and the chat model."""


def is_root_trajectory_key(key: str) -> bool:
    """Checks the default root rule on a key, without reading the file.

    It covers a flat layout (`t-001.atif.json`) and run bundles
    (`trajectories/<file>.atif.json`), and skips subagent files and
    attachments in other directories.

    Args:
        key: Path relative to the prefix.

    Returns:
        True for `*.atif.json` in the prefix or in `trajectories/` directly
        under it.
    """
    parts = key.split("/")
    leaf = parts[-1]
    if not leaf.endswith(".atif.json") or leaf == ".atif.json":
        return False
    return len(parts) == 1 or (len(parts) == 2 and parts[0] == "trajectories")


class AtifFileSource:
    """A `TrajectorySource` over ATIF files under a GCS prefix or directory.

    The prefix holds one agent's trajectories, so the source takes no agent
    name.
    """

    def __init__(
        self,
        atif_uri: str,
        *,
        atif_glob: str | None = None,
        client_factory: Callable[[], Any] | None = None,
        limits: AtifLimits | None = None,
        seed: int | None = None,
        max_listed_objects: int | None = None,
    ) -> None:
        """Initializes the source; nothing is read until the first call.

        Args:
            atif_uri: `gs://<bucket>[/<prefix>]`, or a local directory.
            atif_glob: A glob over paths relative to the prefix that replaces
                the default root rule; None or empty keeps the rule.
            client_factory: Returns a `storage.Client` for a `gs://` URI;
                defaults to one client per thread.
            limits: Size and reference caps; defaults to `AtifLimits()`.
            seed: Seeds the sample of keys, for tests.
            max_listed_objects: Maximum objects one listing reads before
                stopping, for checks that must answer in bounded time; the
                count then reports `truncated=True`. None lists the whole
                prefix, as a sweep does.

        Raises:
            ValueError: When `atif_uri` is neither a `gs://` URI with a bucket
                nor a local path, or `atif_glob` is not valid.
        """
        self._files: AtifFiles = create_atif_files(
            atif_uri, client_factory=client_factory
        )
        self._glob = build_glob_pattern(atif_glob) if atif_glob else None
        self._limits = limits or AtifLimits()
        self._random = random.Random(seed)  # noqa: S311 - not for security
        # The window `count_trajectories` listed, in UTC, its keys, and whether
        # listing was truncated, kept for the `list_trajectory_keys` call of
        # the same window that follows.
        self._counted: (
            tuple[dt.datetime, dt.datetime, list[str], bool] | None
        ) = None
        self._max_listed_objects = max_listed_objects

    def count_trajectories(
        self, start: dt.datetime, end: dt.datetime
    ) -> WindowCount:
        """Counts the root files whose object time is in `[start, end]`.

        Args:
            start: Start of the window, inclusive; a naive time is UTC.
            end: End of the window, inclusive; a naive time is UTC.

        Returns:
            How many root files are in the window, and whether the listing
            stopped at `max_listed_objects`, so the count covers the first
            objects only.

        Raises:
            ObjectReadError: When the prefix cannot be listed.
        """
        start, end = normalize_utc(start), normalize_utc(end)
        keys, truncated = self._list_window_keys(start, end)
        self._counted = (start, end, keys, truncated)
        return WindowCount(count=len(keys), truncated=truncated)

    def list_trajectory_keys(
        self,
        start: dt.datetime,
        end: dt.datetime,
        limit: int,
        *,
        sampling_seed: str | None = None,
    ) -> list[SampledKey]:
        """Lists a uniform random sample of the root files in `[start, end]`.

        Args:
            start: Start of the window, inclusive; a naive time is UTC.
            end: End of the window, inclusive; a naive time is UTC.
            limit: Maximum number of keys to return.
            sampling_seed: Optional seed for reproducible sampling; None uses
                this source's RNG.

        Returns:
            At most `limit` `SampledKey` entries sorted by `key`, each carrying
            its uniform `SamplingRecord(inclusion_probability=len(sample) / total)`.
            With `max_listed_objects` truncated, the sample is drawn from the
            listed objects only and carries `sampling=None`.

        Raises:
            ObjectReadError: When the prefix cannot be listed.
        """
        start, end = normalize_utc(start), normalize_utc(end)
        counted, self._counted = self._counted, None
        if limit <= 0:
            return []
        if counted is not None and counted[:2] == (start, end):
            keys, truncated = counted[2], counted[3]
        else:
            keys, truncated = self._list_window_keys(start, end)
        if not keys:
            return []
        if len(keys) <= limit:
            selected = keys
        else:
            rng = (
                self._random
                if sampling_seed is None
                else random.Random(sampling_seed)  # noqa: S311 - not for security
            )
            selected = sorted(rng.sample(keys, limit))
        record = (
            None
            if truncated
            else SamplingRecord(inclusion_probability=len(selected) / len(keys))
        )
        return [SampledKey(key=key, sampling=record) for key in selected]

    def load_trajectories(
        self, keys: Sequence[str]
    ) -> Iterator[LoadedDocument | FailedDocument]:
        """Loads root files, embedding subagents and resolving references.

        A root file and what it references are read when the consumer asks
        for the next item, not before.

        Args:
            keys: Keys from `list_trajectory_keys`.

        Yields:
            One `LoadedDocument` or `FailedDocument` per key, in order.
        """
        for key in keys:
            yield self._load_root(key)

    def read_attachment(self, attachment: ExternalAttachment) -> bytes:
        """Reads the bytes of an external attachment under the prefix.

        Args:
            attachment: An `ExternalAttachment` from `LoadedDocument.locations`
                of this source.

        Returns:
            The attachment's bytes, read without decompression; at most the
            attachment cap.

        Raises:
            AttachmentUnreadableError: With the marker reason when the bytes
                cannot be read, for any cause.
        """
        cap = self._limits.attachment_bytes
        try:
            data, _ = self._files.read_object(attachment.key, cap)
        except ObjectTooLargeError as err:
            raise AttachmentUnreadableError(
                Reason.TOO_LARGE, str(err.size)
            ) from err
        except ObjectReadError as err:
            raise AttachmentUnreadableError(
                Reason.READ_ERROR, err.status
            ) from err
        except PathOutsidePrefixError as err:
            raise AttachmentUnreadableError(Reason.PATH_TRAVERSAL) from err
        except Exception as err:
            raise AttachmentUnreadableError(Reason.READ_ERROR) from err
        return data

    def _list_window_keys(
        self, start: dt.datetime, end: dt.datetime
    ) -> tuple[list[str], bool]:
        """Lists the root files whose object time is in `[start, end]`.

        Args:
            start: Start of the window, inclusive, in UTC.
            end: End of the window, inclusive, in UTC.

        Returns:
            The keys, sorted, and whether the listing stopped at
            `max_listed_objects`.
        """
        objects = self._files.list_objects()
        keys = []
        truncated = False
        for listed, info in enumerate(objects):
            if (
                self._max_listed_objects is not None
                and listed == self._max_listed_objects
            ):
                truncated = True
                break
            if (
                info.updated is not None
                and start <= info.updated <= end
                and self._is_root(info.key)
            ):
                keys.append(info.key)
        return sorted(keys), truncated

    def _is_root(self, key: str) -> bool:
        """Checks whether a key names a root file, by the glob or the rule.

        Args:
            key: Path relative to the prefix.

        Returns:
            True for a root file.
        """
        if self._glob is not None:
            return self._glob.fullmatch(key) is not None
        return is_root_trajectory_key(key)

    def _load_root(self, key: str) -> LoadedDocument | FailedDocument:
        """Loads one root file, turning any failure into a `FailedDocument`.

        Args:
            key: Path of the root file relative to the prefix.

        Returns:
            The document with its references resolved, or the reason loading
            failed.
        """
        try:
            return self._read_root(key)
        except Exception as err:
            _LOGGER.warning(
                "Could not load a root document: %s", type(err).__name__
            )
            return FailedDocument(
                key=key,
                reason=LOAD_ERROR,
                source_uri=self._files.build_uri(key),
            )

    def _read_root(self, key: str) -> LoadedDocument | FailedDocument:
        """Reads, parses and resolves one root file.

        Args:
            key: Path of the root file relative to the prefix.

        Returns:
            The document with its references resolved, or the reason loading
            failed.
        """
        source_uri = self._files.build_uri(key)
        try:
            resolved_key = self._files.resolve_key(key)
            raw, info = self._files.read_object(
                resolved_key, self._limits.document_bytes
            )
        except ObjectTooLargeError:
            return FailedDocument(
                key=key, reason=TOO_LARGE, source_uri=source_uri
            )
        except PathOutsidePrefixError:
            return FailedDocument(
                key=key, reason=PATH_TRAVERSAL, source_uri=source_uri
            )
        except ObjectReadError as err:
            return FailedDocument(
                key=key,
                reason=f"{READ_ERROR} {err.status}"
                if err.status
                else READ_ERROR,
                source_uri=source_uri,
            )
        bytes_read = len(raw)
        try:
            parsed = parse_document(raw)
            if not isinstance(parsed.value, dict):
                raise DocumentRejectedError(NOT_ATIF)
        except DocumentRejectedError as err:
            return FailedDocument(
                key=key,
                reason=err.reason,
                source_uri=source_uri,
                source_updated_at=info.updated,
            )
        finally:
            del raw
        document: dict[str, Any] = parsed.value
        resolver = DocumentResolver(
            self._files, self._limits, document, bytes_read=bytes_read
        )
        resolver.resolve(key, resolved_key)
        return LoadedDocument(
            key=key,
            document=document,
            source_uri=source_uri,
            source_updated_at=info.updated,
            locations=resolver.locations,
            markers=(*parsed.markers, *resolver.markers),
        )
