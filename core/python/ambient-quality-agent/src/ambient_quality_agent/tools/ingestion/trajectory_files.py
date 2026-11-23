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

"""Store the files a conversation carries and link them from the eval case.

Every ingestion source maps its rows to `EvalCase` objects whose parts can hold
file bytes as ``inline_data`` (see `message_parts`). `store_trajectory_files`
writes those bytes to the jobs store and replaces each part with a
``file_data`` link, so the trajectory store and the reviewer receive the link
in place of the bytes. A top-level file that is not stored becomes a text
placeholder that names its MIME type, size and digest; one inside a function
response stays as it is.

The name "trajectory files" avoids a clash with ``aqua attach``, which attaches
an observed agent to AQuA.

The evaluation service forwards every ``file_data`` part to its judge model,
and the code metrics read a case's messages as text, so each of them gets a
copy of a case in which each trajectory-file link is placeholder text (see
`build_case_without_trajectory_files`).

Wiring::

    BigQueryJobFetcher._map_page
        │ store_trajectory_files()
        ▼
    store_trajectory_files ──► TrajectoryFileStore.store_file()
                                │ write_bytes(), uri()
                                ▼
                            ObjectStore (jobs store)

    AgentPlatformEvalService.evaluate ─┐
    runner.build_instance ─────────────┼─► build_case_without_trajectory_files()
    remote.build_request_dataset ──────┘
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from ambient_quality_agent.tools.ingestion.message_parts import (
    DEFAULT_MIME_TYPE,
    build_attachment_placeholder,
    build_digest_placeholder_text,
)
from ambient_quality_agent.tools.objects.store import StorageNotConfiguredError
from google.genai.types import (
    FileData,
    FunctionResponseFileData,
    FunctionResponsePart,
    Part,
)

if TYPE_CHECKING:
    from agentplatform._genai.types import EvalCase
    from ambient_quality_agent.tools.objects.store import ObjectStore

logger = logging.getLogger(__name__)

TRAJECTORY_FILES_PREFIX = "aqa-trajectory-files/"
"""Top-level prefix for trajectory files in the jobs store. Placed at the root
because GCS lifecycle rules match only prefix starts, and the jobs bucket
deletes these objects by age."""

MAX_TRAJECTORY_FILE_BYTES = 20 * 1024 * 1024
"""Maximum file size stored (20 MiB). Bounds upload and stored object size;
bytes are already decoded in memory when checked. See
`TrajectoryFilesReport.not_stored` for other exclusions."""

_LOGGABLE_MIME_TYPE = re.compile(r"[\w.+-]+/[\w.+-]+", re.ASCII)
"""MIME type pattern safe to log, avoiding free-text telemetry values (such as
``data:`` URI headers with line breaks)."""

_TRAJECTORY_FILE_LINK = re.compile(
    rf"(?:^|/){re.escape(TRAJECTORY_FILES_PREFIX)}"
    r"[^/]+/[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9a-f]{64}\Z"
)
"""Suffix pattern matching a trajectory-file link in ``gs://`` or file-path
form."""


class TrajectoryFileStore:
    """Stores one agent's trajectory files in an `ObjectStore`.

    Objects are named
    ``aqa-trajectory-files/<agent key>/<YYYY-MM-DD>/<sha256 hex>`` using the UTC
    ingestion date. Because the jobs bucket deletes these objects by age and
    archived conversations are partitioned by ingestion day, objects and the
    rows linking to them expire together. A file ingested on N days produces N
    objects.
    """

    def __init__(
        self,
        store: ObjectStore,
        agent_key: str,
        *,
        today: Callable[[], dt.date] = lambda: dt.datetime.now(dt.UTC).date(),
    ) -> None:
        """Initialize the store.

        Args:
            store: The jobs object store.
            agent_key: Key of the observed agent (`/` is replaced with `_` to
                form a single path segment).
            today: Callable returning the UTC ingestion date.
        """
        self._store = store
        self._agent_key = agent_key.replace("/", "_")
        self._today = today

    def store_file(self, data: bytes, sha256_hex: str) -> str:
        """Store a file's bytes and return its URI or path.

        Uses a create-if-absent write so an existing object succeeds without
        rewriting identical content. Single-turn and multi-turn ingestion in the
        same sweep read the same sessions, and GCS rate-limits repeated writes
        to the same object name.

        Args:
            data: Raw file bytes.
            sha256_hex: Hex SHA-256 digest of `data`.

        Returns:
            Object URI (`gs://...` when deployed) or local file path (in
            standalone mode).

        Raises:
            StorageNotConfiguredError: If the jobs store has no storage
                configured.
        """
        name = (
            f"{TRAJECTORY_FILES_PREFIX}{self._agent_key}/"
            f"{self._today().isoformat()}/{sha256_hex}"
        )
        self._store.write_bytes(name, data, overwrite=False)
        return self._store.uri(name)


@dataclasses.dataclass
class TrajectoryFilesReport:
    """What `store_trajectory_files` did with the files of one case."""

    stored: int = 0
    """Files written to the store, or found there, and linked."""

    not_stored: int = 0
    """Files kept out of the store by design: no store, empty, too large, or
    no storage configured."""

    failed: int = 0
    """Files whose write raised an error."""

    @property
    def total(self) -> int:
        """Number of files the case carried."""
        return self.stored + self.not_stored + self.failed

    def add(self, other: TrajectoryFilesReport) -> None:
        """Add another report's counts to this one.

        Args:
            other: The report to add.
        """
        self.stored += other.stored
        self.not_stored += other.not_stored
        self.failed += other.failed


def store_trajectory_files(
    case: EvalCase, store: TrajectoryFileStore | None
) -> TrajectoryFilesReport:
    """Store the bytes of every file in a case and link them in place.

    Walks every part of every event of every turn:

    1. A part with ``inline_data`` bytes becomes a ``file_data`` link when
       stored, and a digest placeholder text part otherwise.
    2. A ``function_response.parts`` entry with ``inline_data`` bytes becomes a
       ``file_data`` entry when stored, and stays unchanged otherwise, because
       a `FunctionResponsePart` has no text form.

    ``file_data`` parts the agent sent are left as they are.

    Args:
        case: The case to update in place.
        store: Where to store the files; None stores none.

    Returns:
        How many files were stored, not stored and failed.
    """
    report = TrajectoryFilesReport()
    for content_parts in _list_content_parts(case):
        content_parts[:] = [
            _replace_top_level_part(part, store, report)
            for part in content_parts
        ]
    return report


def _replace_top_level_part(
    part: Part, store: TrajectoryFileStore | None, report: TrajectoryFilesReport
) -> Part:
    """Replace a part's file bytes with a link or a placeholder.

    Args:
        part: One part of an event's content.
        store: Where to store the files; None stores none.
        report: Counts to update.

    Returns:
        The part to keep in place of `part`.
    """
    response = part.function_response
    if response is not None and response.parts:
        response.parts = [
            _replace_nested_part(nested, store, report)
            for nested in response.parts
        ]
    inline = part.inline_data
    if inline is None or inline.data is None:
        return part
    mime_type = inline.mime_type or DEFAULT_MIME_TYPE
    sha256_hex = hashlib.sha256(inline.data).hexdigest()
    link = _store_one_file(inline.data, sha256_hex, mime_type, store, report)
    if link is None:
        return Part(
            text=build_digest_placeholder_text(
                mime_type, inline.data, sha256_hex
            )
        )
    return Part(file_data=FileData(file_uri=link, mime_type=mime_type))


def _replace_nested_part(
    part: FunctionResponsePart,
    store: TrajectoryFileStore | None,
    report: TrajectoryFilesReport,
) -> FunctionResponsePart:
    """Replace a function response part's file bytes with a link.

    Args:
        part: One entry of ``function_response.parts``.
        store: Where to store the files; None stores none.
        report: Counts to update.

    Returns:
        A ``file_data`` entry when the file was stored, else `part` unchanged.
    """
    inline = part.inline_data
    if inline is None or inline.data is None:
        return part
    mime_type = inline.mime_type or DEFAULT_MIME_TYPE
    sha256_hex = hashlib.sha256(inline.data).hexdigest()
    link = _store_one_file(inline.data, sha256_hex, mime_type, store, report)
    if link is None:
        return part
    return FunctionResponsePart(
        file_data=FunctionResponseFileData(file_uri=link, mime_type=mime_type)
    )


def _store_one_file(
    data: bytes,
    sha256_hex: str,
    mime_type: str,
    store: TrajectoryFileStore | None,
    report: TrajectoryFilesReport,
) -> str | None:
    """Store one file and count the outcome.

    Args:
        data: The file's bytes.
        sha256_hex: The hex SHA-256 digest of `data`.
        mime_type: The file's MIME type, for the warning on failure; logged
            only when it has the form ``type/subtype``.
        store: Where to store the file; None stores none.
        report: Counts to update.

    Returns:
        The file's link, or None if it was not stored.
    """
    if store is None or not data or len(data) > MAX_TRAJECTORY_FILE_BYTES:
        report.not_stored += 1
        return None
    try:
        link = store.store_file(data, sha256_hex)
    except StorageNotConfiguredError:
        report.not_stored += 1
        return None
    except Exception as exc:
        # Avoid logging raw exception details or file links, which can contain
        # end-user data.
        logger.warning(
            "Failed to store a trajectory file (%s, %d bytes): %s.",
            mime_type
            if _LOGGABLE_MIME_TYPE.fullmatch(mime_type)
            else "invalid",
            len(data),
            type(exc).__name__,
        )
        report.failed += 1
        return None
    report.stored += 1
    return link


def build_case_without_trajectory_files(case: EvalCase) -> EvalCase:
    """Build a copy of a case whose trajectory-file links are placeholder text.

    1. A top-level ``file_data`` part that is a trajectory-file link becomes the
       bare attachment placeholder for its MIME type.
    2. A ``function_response.parts`` entry that is a trajectory-file link is
       removed, because a `FunctionResponsePart` has no text form; ``parts``
       becomes None when no entry remains.

    ``file_data`` links the agent sent are kept.

    Args:
        case: The case to read; it is not changed.

    Returns:
        `case` itself when it holds no trajectory-file link, else a deep copy
        with the links replaced.
    """
    if not any(
        any(_has_trajectory_file_link(part) for part in content_parts)
        for content_parts in _list_content_parts(case)
    ):
        return case
    copy = case.model_copy(deep=True)
    for content_parts in _list_content_parts(copy):
        content_parts[:] = [
            _replace_trajectory_file_link(part) for part in content_parts
        ]
    return copy


def _list_content_parts(case: EvalCase) -> list[list[Part]]:
    """List the part lists of every event of every turn.

    Args:
        case: The case to read.

    Returns:
        Each event content's ``parts`` list, in conversation order.
    """
    turns = (case.agent_data.turns if case.agent_data else None) or []
    part_lists: list[list[Part]] = []
    for turn in turns:
        for event in turn.events or []:
            if event.content is not None and event.content.parts:
                part_lists.append(event.content.parts)
    return part_lists


def _is_trajectory_file_link(file_uri: str | None) -> bool:
    """Whether a URI names an object `TrajectoryFileStore` wrote.

    Args:
        file_uri: The ``file_data`` URI.

    Returns:
        True for a ``gs://`` URI or a file path that ends with a trajectory-file
        object name.
    """
    return (
        file_uri is not None
        and _TRAJECTORY_FILE_LINK.search(file_uri) is not None
    )


def _has_trajectory_file_link(part: Part) -> bool:
    """Whether a part or one of its function response entries is a
    trajectory-file link.

    Args:
        part: One part of an event's content.

    Returns:
        True if any ``file_data`` it holds links to a trajectory file.
    """
    if part.file_data is not None and _is_trajectory_file_link(
        part.file_data.file_uri
    ):
        return True
    response = part.function_response
    return response is not None and any(
        nested.file_data is not None
        and _is_trajectory_file_link(nested.file_data.file_uri)
        for nested in response.parts or []
    )


def _replace_trajectory_file_link(part: Part) -> Part:
    """Replace the trajectory-file links a part holds.

    Args:
        part: One part of a copied event's content; its function response is
            updated in place.

    Returns:
        The bare attachment placeholder for a trajectory-file link, else `part`.
    """
    response = part.function_response
    if response is not None and response.parts:
        kept = [
            nested
            for nested in response.parts
            if nested.file_data is None
            or not _is_trajectory_file_link(nested.file_data.file_uri)
        ]
        response.parts = kept or None
    file_data = part.file_data
    if file_data is not None and _is_trajectory_file_link(file_data.file_uri):
        return build_attachment_placeholder(
            file_data.mime_type or DEFAULT_MIME_TYPE
        )
    return part
