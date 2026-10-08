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

"""Source snapshot layout, manifest models, size limits, and ignore rules.

Each snapshot represents a single deployment revision of an observed agent's
repository, kept under that agent's name in the source store
(`objects.store.source_store_factory`):

    <agent_name>/<revision>/manifest.json
    <agent_name>/<revision>/files/<repo-relative-path>

so that one store holds several agents apart. Snapshots may also sit
unprefixed at `<revision>/`; the reader falls back to them.

The manifest is uploaded last. Its presence indicates a complete upload; a missing
manifest indicates an interrupted upload that must be treated as unavailable.

The manifest records `ignore_patterns` so callers can distinguish between files excluded
by rule and files that never existed in the repository.

Revisions are numeric strings and may not be contiguous due to lifecycle deletion
(e.g., "10", "8", "2", "1"). Use `sort_revisions_desc` to order them numerically.
"""

from __future__ import annotations

import functools
import json
import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, ValidationError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

SCHEMA_VERSION = 1
"""Manifest schema version supported by this module."""

MANIFEST_OBJECT = "manifest.json"
"""Object name of the manifest file under a revision prefix."""

FILES_PREFIX = "files"
"""Object prefix for file contents under a revision prefix."""

MAX_FILE_BYTES = 1024 * 1024
"""Maximum bytes per file. Larger files are truncated and flagged in the manifest."""

MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024
"""Maximum total snapshot size. Files exceeding this limit are omitted."""

MAX_BATCH_FILES = 200
"""Files one upload request may carry, so that one request decodes at most this
many `MAX_FILE_BYTES` files into memory."""


class SnapshotFileEntry(BaseModel):
    """A file entry recorded in a source manifest."""

    path: str
    """Repository-relative file path with forward slashes."""

    size: int = 0
    """Published file size in bytes after any truncation."""

    truncated: bool = False
    """Whether the file exceeded MAX_FILE_BYTES and was truncated."""


class SourceManifest(BaseModel):
    """Index and metadata for a single revision's source snapshot.

    The existence of the manifest indicates that the snapshot upload completed successfully.
    """

    schema_version: int = SCHEMA_VERSION
    revision: str = ""
    engine: str = ""
    """Agent Runtime resource name associated with this revision."""

    created_at: str = ""
    """RFC 3339 timestamp when the snapshot was published."""

    agent_directory: str = ""
    """Repository subdirectory containing the agent code."""

    ignore_patterns: list[str] = Field(default_factory=list)
    """Gitignore-style patterns excluded during publishing."""

    files: list[SnapshotFileEntry] = Field(default_factory=list)
    total_bytes: int = 0
    truncated_files: int = 0
    omitted_files: int = 0
    """Number of files omitted due to MAX_SNAPSHOT_BYTES limit."""

    def get_entry(self, path: str) -> SnapshotFileEntry | None:
        """Finds the manifest entry for a given file path.

        Args:
            path: Repository-relative file path to locate.

        Returns:
            The matching SnapshotFileEntry, or None if not found in this snapshot.
        """
        wanted = normalize_path(path)
        return next(
            (f for f in self.files if normalize_path(f.path) == wanted), None
        )

    def find_excluding_pattern(self, path: str) -> str | None:
        """Finds the first ignore pattern excluding the given path.

        Args:
            path: Repository-relative file path to test.

        Returns:
            The matching ignore pattern string, or None if no pattern excludes the path.
        """
        return find_matching_ignore_pattern(path, self.ignore_patterns)


_KEY_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")


def validate_key_segment(value: str, what: str) -> str:
    """Checks that an agent name or a revision can be one segment of an object name.

    Both reach here from a command route, so a value like `../x` or `a/b` must
    not become a name outside its agent's prefix.

    Args:
        value: The agent name or revision.
        what: What the value is, for the error message.

    Returns:
        The value, unchanged.

    Raises:
        ValueError: If the value is empty or not a single safe segment.
    """
    if not _KEY_SEGMENT.fullmatch(value):
        raise ValueError(
            f"{what} {value!r} is not a valid snapshot key: use letters, "
            "digits, '.', '_' and '-', starting with a letter, digit or '_'"
        )
    return value


def validate_agent_name(agent_name: str) -> str:
    """Checks that an agent name can be the prefix of its snapshots.

    Args:
        agent_name: The observed agent's name.

    Returns:
        The name, unchanged.

    Raises:
        ValueError: If it is not a valid key segment, or is all digits, which
            the reader would take for an unprefixed revision.
    """
    validate_key_segment(agent_name, "agent name")
    if agent_name.isdecimal():
        raise ValueError(
            f"agent name {agent_name!r} is all digits, like a revision"
        )
    return agent_name


def build_agent_root(agent_name: str) -> str:
    """Returns the prefix an agent's snapshots live under.

    Args:
        agent_name: Observed agent's name; empty for the unprefixed
            snapshots.

    Returns:
        `<agent_name>/`, or the empty string.
    """
    return f"{agent_name}/" if agent_name else ""


def build_manifest_object_name(revision: str, root: str = "") -> str:
    """Returns the object name of a revision's manifest.

    Args:
        revision: Deployment revision identifier.
        root: The agent's prefix, from `build_agent_root`.

    Returns:
        Object name of manifest.json under the revision prefix.
    """
    return f"{root}{revision}/{MANIFEST_OBJECT}"


def build_file_object_name(revision: str, path: str, root: str = "") -> str:
    """Returns the object name of a file within a revision.

    Args:
        revision: Deployment revision identifier.
        path: Repository-relative file path.
        root: The agent's prefix, from `build_agent_root`.

    Returns:
        Object name under the revision's files prefix.
    """
    return f"{root}{revision}/{FILES_PREFIX}/{normalize_path(path)}"


def build_revision_prefix(revision: str, root: str = "") -> str:
    """Returns the prefix of everything in a revision's snapshot.

    Args:
        revision: Deployment revision identifier.
        root: The agent's prefix, from `build_agent_root`.

    Returns:
        Prefix ending with a slash.
    """
    return f"{root}{revision}/"


def normalize_path(path: str) -> str:
    """Normalizes a file path to use forward slashes without leading/trailing slashes.

    Args:
        path: File path to normalize.

    Returns:
        Normalized repository-relative path string.
    """
    return path.replace("\\", "/").strip("/")


def parse_manifest(payload: str | bytes) -> SourceManifest:
    """Parses and validates a manifest JSON document.

    Args:
        payload: Raw JSON content of manifest.json as text or bytes.

    Returns:
        Validated SourceManifest instance.

    Raises:
        ValueError: If payload is not valid JSON, fails schema validation, or uses an unsupported schema version.
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest is not valid JSON: {exc}") from exc
    try:
        manifest = SourceManifest.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"manifest does not match the snapshot schema: {exc}"
        ) from exc
    # Reject unsupported schema versions explicitly to prevent silent misinterpretation.
    if manifest.schema_version > SCHEMA_VERSION:
        raise ValueError(
            f"manifest schema version {manifest.schema_version} is newer than "
            f"the supported version {SCHEMA_VERSION}"
        )
    return manifest


def sort_revisions_desc(revisions: Iterable[str]) -> list[str]:
    """Sorts revision strings in descending order, placing numeric revisions first.

    Numeric revisions are ordered by their integer values (so "10" precedes "8").
    Non-numeric revisions are retained and ordered lexicographically after numeric ones.

    Args:
        revisions: Iterable of revision strings to sort.

    Returns:
        List of revision strings sorted newest first.
    """
    return sorted(revisions, key=_build_revision_sort_key, reverse=True)


def _build_revision_sort_key(revision: str) -> tuple[int, int, str]:
    """Builds a sort key that prioritizes valid decimal integers over other strings.

    Args:
        revision: Revision string to convert to a sort key.

    Returns:
        Tuple of (is_numeric, numeric_value, fallback_string) for sorting.
    """
    if revision.isdecimal():
        return (1, int(revision), "")
    return (0, 0, revision)


def find_matching_ignore_pattern(
    path: str, patterns: Sequence[str]
) -> str | None:
    """Finds the first ignore pattern that excludes the given path.

    Args:
        path: Repository-relative file path.
        patterns: Sequence of ignore patterns to evaluate.

    Returns:
        First matching ignore pattern string, or None if no pattern matches.
    """
    return next((p for p in patterns if matches_ignore_pattern(path, p)), None)


def matches_ignore_pattern(path: str, pattern: str) -> bool:
    """Evaluates whether a repository path is excluded by a gitignore pattern.

    Supports directory patterns (trailing `/`), anchored paths (leading or
    internal `/`), and unanchored segment patterns matching at any depth. Blank
    lines and `#` comment lines match nothing.

    Negation patterns (`!pattern`) are not evaluated for re-inclusion and treat
    `!` literally; this check only runs on absent files to explain why they were
    omitted from the manifest.

    Args:
        path: Repository-relative file path to test.
        pattern: Gitignore-style ignore pattern.

    Returns:
        True if the pattern excludes the path, False otherwise.
    """
    cleaned = pattern.strip()
    if not cleaned or cleaned.startswith("#"):
        return False
    directory_only = cleaned.endswith("/")
    anchored = cleaned.startswith("/") or "/" in cleaned.strip("/")
    cleaned = cleaned.strip("/")
    if not cleaned:
        return False

    normalized = normalize_path(path)
    if not normalized:
        return False
    segments = normalized.split("/")
    # Anchored patterns match full path prefixes; unanchored patterns match individual segments.
    if anchored:
        candidates = ["/".join(segments[: i + 1]) for i in range(len(segments))]
    else:
        candidates = segments

    matcher = _compile_ignore_pattern(cleaned)
    last = len(candidates) - 1
    for index, candidate in enumerate(candidates):
        if not matcher.fullmatch(candidate):
            continue
        # Directory patterns exclude contents, so they must match an ancestor directory.
        if directory_only and index == last:
            continue
        return True
    return False


@functools.lru_cache(maxsize=512)
def _compile_ignore_pattern(pattern: str) -> re.Pattern[str]:
    """Compiles a gitignore-style pattern into a regular expression.

    Translates `*` and `?` wildcards to match within a single path segment
    without crossing `/`, and `**` to match across directory boundaries.

    Args:
        pattern: Ignore pattern with surrounding slashes stripped.

    Returns:
        Compiled regular expression for full matching against candidate paths.
    """
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        rest = pattern[index:]
        if rest.startswith("**/"):
            parts.append("(?:.*/)?")
            index += 3
        elif rest.startswith("**"):
            parts.append(".*")
            index += 2
        elif rest.startswith("*"):
            parts.append("[^/]*")
            index += 1
        elif rest.startswith("?"):
            parts.append("[^/]")
            index += 1
        elif rest.startswith("["):
            closing = pattern.find("]", index + 1)
            if closing == -1:
                parts.append(re.escape("["))
                index += 1
                continue
            body = pattern[index + 1 : closing]
            parts.append(
                f"[{'^' + body[1:] if body.startswith('!') else body}]"
            )
            index = closing + 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(parts))
