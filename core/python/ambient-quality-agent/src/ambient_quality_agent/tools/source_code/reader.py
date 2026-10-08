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

"""Reader for deployment revision source snapshots in the source store.

Provides blocking read operations against the source store
(`objects.store.source_store_factory`), the source bucket in a deployment and
`.aqua/source/` in a standalone run, intended to be run in worker threads via
`asyncio.to_thread` by async callers.

A reader reads one agent's snapshots: those under `<agent_name>/` first, then
any unprefixed `<revision>/` ones.

Manifests are cached per (store location, revision) in memory for
`MANIFEST_CACHE_TTL_SECONDS`, to avoid repeated fetches while a model browses a
snapshot.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ambient_quality_agent.tools.objects import store as objects_store
from ambient_quality_agent.tools.observed_agent_config import effective_config
from ambient_quality_agent.tools.source_code import snapshot
from google.api_core import exceptions as api_exceptions
from google.auth import exceptions as auth_exceptions

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from ambient_quality_agent.core.session_state import ADKStateLike
    from ambient_quality_agent.tools.objects.store import ObjectStore

logger = logging.getLogger(__name__)

READ_ERRORS: tuple[type[Exception], ...] = (
    api_exceptions.GoogleAPIError,
    auth_exceptions.GoogleAuthError,
    objects_store.StorageNotConfiguredError,
    OSError,
)
"""What a read of either store raises when it fails, rather than finds nothing.

Callers report these as an unavailable snapshot; anything else is a bug.
"""

MAX_SEARCH_MATCHES = 200
"""Maximum search matches returned by search before truncating."""

MAX_CONTEXT_LINES = 20
"""Maximum surrounding context lines allowed per side of a search match."""


MANIFEST_CACHE_TTL_SECONDS = 300.0
"""How long a manifest read is reused.

A revision published again replaces its manifest, and another replica's
cached copy would otherwise outlive it; this bounds how long it does.
"""

_manifest_cache: dict[
    tuple[str, str], tuple[float, snapshot.SourceManifest]
] = {}


def clear_manifest_cache() -> None:
    """Clears all cached manifests across all stores."""
    _manifest_cache.clear()


def forget_cached_manifest(location: str, revision: str) -> None:
    """Drops this process's cached manifest of one revision, once it is replaced.

    Args:
        location: The store location of the agent's prefix, `store.uri(root)`.
        revision: Deployment revision identifier.
    """
    _manifest_cache.pop((location, revision), None)


@dataclass(frozen=True)
class SearchMatch:
    """Represents a single search match line along with its surrounding context."""

    path: str
    line_number: int
    line: str
    before: tuple[str, ...]
    after: tuple[str, ...]

    @property
    def citation(self) -> str:
        """Returns `<path>:<start>-<end>` covering the match and its context lines."""
        start = self.line_number - len(self.before)
        end = self.line_number + len(self.after)
        return f"{self.path}:{start}-{end}"


@dataclass(frozen=True)
class FileSlice:
    """A contiguous slice of lines from a file with position metadata."""

    path: str
    start_line: int
    lines: tuple[str, ...]
    total_lines: int
    truncated_in_snapshot: bool

    @property
    def is_empty(self) -> bool:
        """Returns True if the requested offset starts past the end of the file.

        Because `read_file` always reads at least one line when within bounds,
        an empty slice indicates the offset exceeded the file length.
        """
        return not self.lines

    @property
    def end_line(self) -> int:
        """Returns the 1-based line number of the last line in the slice.

        Callers should check `is_empty` first, as an empty slice past EOF does
        not represent a valid line range.
        """
        return self.start_line + len(self.lines) - 1


class SourceSnapshotReader:
    """Provides read access to one agent's source snapshots in a store."""

    def __init__(self, *, store: ObjectStore, agent_name: str = "") -> None:
        """Initializes a reader for an agent's snapshots.

        Args:
            store: The source store.
            agent_name: Observed agent whose snapshots to read. Empty reads only
                the unprefixed snapshots.
        """
        self._store = store
        root = snapshot.build_agent_root(agent_name)
        # The agent's own prefix first, so that it wins over an unprefixed
        # snapshot of the same revision.
        self._roots = (root, "") if root else ("",)
        self._revision_roots: dict[str, str] = {}

    @property
    def location(self) -> str:
        """Where the agent's snapshots are published: a `gs://` URI or a directory."""
        return self._store.uri(self._roots[0])

    def build_manifest_uri(self, revision: str) -> str:
        """Returns where a revision's manifest is, for display.

        Args:
            revision: Deployment revision identifier.

        Returns:
            The manifest's `gs://` URI or file path.
        """
        return self._store.uri(
            snapshot.build_manifest_object_name(
                revision, self._resolve_root(revision)
            )
        )

    def list_revisions(self) -> list[str]:
        """Lists all revision prefixes of the agent, sorted newest first.

        Returns:
            List of revision identifier strings sorted in descending order.
        """
        found: dict[str, str] = {}
        # Unprefixed first, so that the agent's own prefix overwrites it.
        for root in reversed(self._roots):
            for prefix in self._store.list_prefixes(root):
                revision = prefix[len(root) :].strip("/")
                # The unprefixed level also holds every agent's prefix. Runtime
                # revisions are decimal, and an ADK agent name cannot be.
                if not root and not revision.isdecimal():
                    continue
                if revision:
                    found[revision] = root
        self._revision_roots.update(found)
        return snapshot.sort_revisions_desc(found)

    def get_latest_revision(self) -> str | None:
        """Returns the newest revision prefix, regardless of completeness.

        Returns:
            The newest revision string, or None if there is none.
        """
        revisions = self.list_revisions()
        return revisions[0] if revisions else None

    def get_latest_complete_revision(self) -> str | None:
        """Returns the newest revision that contains a valid manifest.

        Returns:
            Newest complete revision string, or None if no complete snapshot exists.
        """
        return next(
            (r for r in self.list_revisions() if self.load_manifest(r)), None
        )

    def load_manifest(self, revision: str) -> snapshot.SourceManifest | None:
        """Retrieves and parses the manifest for a given revision.

        Args:
            revision: Revision identifier to fetch.

        Returns:
            Parsed SourceManifest, or None if the manifest does not exist or is unparseable.
        """
        known = self._revision_roots.get(revision)
        for root in self._roots if known is None else (known,):
            key = (self._store.uri(root), revision)
            cached = _manifest_cache.get(key)
            if (
                cached
                and time.monotonic() - cached[0] < MANIFEST_CACHE_TTL_SECONDS
            ):
                self._revision_roots[revision] = root
                return cached[1]
            payload = self._read(
                snapshot.build_manifest_object_name(revision, root)
            )
            if payload is None:
                continue
            self._revision_roots[revision] = root
            try:
                parsed = snapshot.parse_manifest(payload)
            except ValueError as exc:
                logger.warning(
                    "source snapshot %s: unreadable manifest: %s", revision, exc
                )
                return None
            _manifest_cache[key] = (time.monotonic(), parsed)
            return parsed
        return None

    def _resolve_root(self, revision: str) -> str:
        """Returns the prefix a revision's snapshot is under.

        The one its manifest was found under, else the agent's own.

        Args:
            revision: Deployment revision identifier.

        Returns:
            The agent's prefix, or the empty string for an unprefixed snapshot.
        """
        if revision not in self._revision_roots:
            self.load_manifest(revision)
        return self._revision_roots.get(revision, self._roots[0])

    def read_file(
        self, revision: str, path: str, *, offset: int = 1, limit: int = 200
    ) -> FileSlice | None:
        """Reads a range of lines from a specific file in a revision snapshot.

        Args:
            revision: Deployment revision identifier.
            path: Repository-relative file path.
            offset: 1-based starting line number (minimum 1).
            limit: Maximum number of lines to return (minimum 1).

        Returns:
            FileSlice containing the requested lines, or None if the file object
            does not exist. When `offset` exceeds total lines, returns an empty
            FileSlice (`is_empty == True`).
        """
        root = self._resolve_root(revision)
        body = self._read(snapshot.build_file_object_name(revision, path, root))
        if body is None:
            return None
        lines = _split_lines(body)
        start = max(1, offset)
        window = tuple(lines[start - 1 : start - 1 + max(1, limit)])
        manifest = self.load_manifest(revision)
        entry = manifest.get_entry(path) if manifest else None
        return FileSlice(
            path=snapshot.normalize_path(path),
            start_line=start,
            lines=window,
            total_lines=len(lines),
            truncated_in_snapshot=bool(entry and entry.truncated),
        )

    def search(
        self,
        revision: str,
        *,
        pattern: str,
        path_contains: str = "",
        context_lines: int = 2,
    ) -> tuple[list[SearchMatch], int, bool]:
        """Searches files in a revision snapshot using a regular expression.

        Args:
            revision: Deployment revision identifier.
            pattern: Regular expression pattern to search line-by-line.
            path_contains: Optional substring filter for file paths.
            context_lines: Number of surrounding context lines per match (capped at MAX_CONTEXT_LINES).

        Returns:
            Tuple of (matches, files_searched, is_truncated) where is_truncated is True
            if the search reached MAX_SEARCH_MATCHES.

        Raises:
            re.error: If pattern is not a valid regular expression.
        """
        compiled = re.compile(pattern)
        manifest = self.load_manifest(revision)
        if manifest is None:
            return [], 0, False
        root = self._resolve_root(revision)
        context = min(max(0, context_lines), MAX_CONTEXT_LINES)
        matches: list[SearchMatch] = []
        searched = 0
        for entry in self._filter_entries(manifest.files, path_contains):
            body = self._read(
                snapshot.build_file_object_name(revision, entry.path, root)
            )
            if body is None:
                continue
            searched += 1
            lines = _split_lines(body)
            for index, line in enumerate(lines):
                if not compiled.search(line):
                    continue
                matches.append(
                    SearchMatch(
                        path=snapshot.normalize_path(entry.path),
                        line_number=index + 1,
                        line=line,
                        before=tuple(lines[max(0, index - context) : index]),
                        after=tuple(lines[index + 1 : index + 1 + context]),
                    )
                )
                if len(matches) >= MAX_SEARCH_MATCHES:
                    return matches, searched, True
        return matches, searched, False

    def _read(self, name: str) -> bytes | None:
        """Reads an object, treating a name no object can have as absent.

        A path reaches here from a tool call. GCS answers `../x` with "no such
        object", and the directory store refuses to resolve it; both mean there
        is nothing to read.

        Args:
            name: Object name in the store.

        Returns:
            The object's bytes, or None if there is no such object.
        """
        try:
            return self._store.read_bytes(name)
        except ValueError:
            return None

    @staticmethod
    def _filter_entries(
        entries: Sequence[snapshot.SnapshotFileEntry], path_contains: str
    ) -> Iterable[snapshot.SnapshotFileEntry]:
        """Filters manifest entries by an optional case-insensitive path substring.

        Args:
            entries: Sequence of snapshot file entries.
            path_contains: Substring to filter paths by; returns all entries if empty.

        Returns:
            Iterable of matching SnapshotFileEntry objects.
        """
        if not path_contains:
            return entries
        needle = path_contains.lower()
        return [e for e in entries if needle in e.path.lower()]


def _split_lines(body: bytes) -> list[str]:
    """Splits file bytes into lines matching standard editor line-numbering conventions.

    Decodes UTF-8 with replacement and splits strictly on standard newline boundaries.

    Args:
        body: Raw file bytes to split.

    Returns:
        List of decoded line strings without newline characters.
    """
    text = body.decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = text.split("\n")
    # Trailing newline terminates the final line rather than creating an extra empty line.
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _create_default_reader(state: ADKStateLike) -> SourceSnapshotReader | None:
    """Creates a reader of the watched agent's snapshots in the source store.

    Args:
        state: ADK session state containing effective configuration.

    Returns:
        SourceSnapshotReader instance, or None if there is no source store.
    """
    store = objects_store.source_store_factory()
    if store is None:
        return None
    return SourceSnapshotReader(
        store=store, agent_name=effective_config.load(state).observed_agent_name
    )


# Test seam for injecting custom or in-memory snapshot readers. Annotated as a
# callable rather than left to inference, so a substitute of the same shape is
# assignable.
reader_factory: Callable[[ADKStateLike], SourceSnapshotReader | None] = (
    _create_default_reader
)
