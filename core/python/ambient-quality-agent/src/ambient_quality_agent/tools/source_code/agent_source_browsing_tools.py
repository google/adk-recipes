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

"""ADK function tools for browsing observed agent source code snapshots during RCA.

Tools that resolve a revision report the revision used and its provenance:
* ``explicit``: Revision supplied by the caller. An insight's revision comes from
  ``get_insight``'s ``occurrences[].agent_revision`` or from the ``agent_revision``
  field of a ``get_full_trajectories`` response.
* ``latest_fallback``: Newest available complete snapshot, used when the caller
  supplies no revision.

File absence distinguishes between:
1. File present in the snapshot.
2. File excluded by a configured ignore pattern (e.g., build or infrastructure configs).
3. File not present in the repository at that revision.

When snapshots are unavailable or unconfigured, tools return explicit capability refusal
dictionaries (`available: False`) rather than empty results to avoid false negative conclusions.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
from typing import TYPE_CHECKING, Any

# Runtime import, not TYPE_CHECKING: ADK resolves a tool's annotations with
# `typing.get_type_hints` when it builds the function declaration, so a deferred
# name raises NameError and takes the whole agent down.
from ambient_quality_agent.core.session_state import LaunchContext
from ambient_quality_agent.tools.source_code import reader as source_reader
from ambient_quality_agent.tools.source_code import snapshot

if TYPE_CHECKING:
    from ambient_quality_agent.core.session_state import ADKStateLike

logger = logging.getLogger(__name__)

MAX_LISTED_FILES = 500
"""Maximum number of file entries returned by list_source_files before pagination truncates."""

_CAPABILITY = "Reading the observed agent's source"


def _build_capability_refusal(reason: str) -> dict[str, Any]:
    """Constructs a standard capability refusal dictionary when source snapshots cannot be read.

    Args:
        reason: Explanatory message for why the snapshot is unavailable.

    Returns:
        Refusal payload containing availability status and diagnostic explanation.
    """
    return {
        "available": False,
        "capability": _CAPABILITY,
        "reason": reason,
        "would_enable": (
            "citing the observed agent's own code as `<path>:<start>-<end>` when "
            "explaining why it behaved the way it did"
        ),
    }


class _Snapshot:
    """Encapsulates a resolved snapshot reader, revision metadata, and manifest."""

    def __init__(
        self,
        reader: source_reader.SourceSnapshotReader,
        revision: str,
        revision_source: str,
        manifest: snapshot.SourceManifest,
    ) -> None:
        """Initializes snapshot context.

        Args:
            reader: SourceSnapshotReader instance for accessing GCS objects.
            revision: Resolved deployment revision identifier.
            revision_source: How the revision was resolved ('explicit' or 'latest_fallback').
            manifest: Parsed SourceManifest for the revision.
        """
        self.reader = reader
        self.revision = revision
        self.revision_source = revision_source
        self.manifest = manifest

    def stamp(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Attaches revision and revision provenance metadata to a tool response dictionary.

        Args:
            payload: Tool result dictionary.

        Returns:
            Dictionary containing original payload plus revision and revision_source fields.
        """
        return {
            "revision": self.revision,
            "revision_source": self.revision_source,
            **payload,
        }


async def resolve_snapshot(
    state: ADKStateLike, revision: str
) -> tuple[_Snapshot | None, dict[str, Any] | None]:
    """Resolves the target revision, loads its manifest, and prepares the reader.

    Args:
        state: ADK session state, used to configure the snapshot reader.
        revision: Revision to read, or empty string to read the newest complete
            snapshot.

    Returns:
        Tuple of (_Snapshot, None) on success, or (None, refusal_dict) if unavailable.
    """
    reader = source_reader.reader_factory(state)
    if reader is None:
        return None, _build_capability_refusal(
            "No source snapshot store is configured for this deployment, so the "
            "observed agent's code cannot be read."
        )

    if revision:
        chosen, chosen_source = revision, "explicit"
    else:
        try:
            latest = await asyncio.to_thread(
                reader.get_latest_complete_revision
            )
        # Catch expected GCS and authentication errors; unexpected exceptions
        # indicate bugs and should propagate.
        except source_reader.READ_ERRORS as exc:
            logger.warning("source snapshot: listing revisions failed: %s", exc)
            return None, _build_capability_refusal(
                f"Could not list source snapshots: {exc}"
            )
        if not latest:
            return None, _build_capability_refusal(
                f"No complete source snapshot has been published to "
                f"{reader.location} yet, so the observed agent's code cannot "
                "be read for any revision."
            )
        chosen, chosen_source = latest, "latest_fallback"

    try:
        manifest = await asyncio.to_thread(reader.load_manifest, chosen)
    except source_reader.READ_ERRORS as exc:
        logger.warning(
            "source snapshot %s: manifest read failed: %s", chosen, exc
        )
        return None, _build_capability_refusal(
            f"Could not read snapshot {chosen}: {exc}"
        )
    if manifest is None:
        return None, _build_capability_refusal(
            f"Revision {chosen} has no complete source snapshot -- its manifest is "
            "missing, so either the snapshot passed the bucket's retention window "
            "and was deleted, or its upload did not finish. Either way "
            "this revision's code cannot be read. Try another revision from "
            "list_revisions."
        )
    return _Snapshot(reader, chosen, chosen_source, manifest), None


def _render_numbered_lines(start_line: int, lines: tuple[str, ...]) -> str:
    """Formats a tuple of code lines with right-aligned line numbers for citations.

    Args:
        start_line: 1-based starting line number.
        lines: Code lines to format.

    Returns:
        Single string with formatted `<line_number>: <content>` lines.
    """
    return "\n".join(
        f"{start_line + offset:>6}: {line}" for offset, line in enumerate(lines)
    )


async def list_revisions(tool_context: LaunchContext) -> dict[str, Any]:
    """List deployment revisions with available source code snapshots.

    Use this tool to discover which versions of the observed agent's source code
    are available for inspection and verify if a specific failing revision can be read.
    Revision numbers are non-contiguous because older snapshots are periodically deleted.

    Args:
        tool_context: ADK tool context providing access to session state.

    Returns:
        Dictionary containing:
        - revisions: List of revision metadata objects ordered newest first, including
          revision identifier, upload completion status (complete), creation timestamp
          (created_at), Agent Runtime resource (engine), agent directory, file count,
          and total snapshot size in bytes.
        Returns a capability refusal object if snapshots are unavailable or unconfigured.
    """
    state = tool_context.state
    reader = source_reader.reader_factory(state)
    if reader is None:
        return _build_capability_refusal(
            "No source snapshot store is configured for this deployment, so the "
            "observed agent's code cannot be read."
        )
    try:
        # GCS operations are synchronous; offload to a worker thread to keep the event loop responsive.
        revisions = await asyncio.to_thread(reader.list_revisions)
    except source_reader.READ_ERRORS as exc:
        logger.warning("source snapshot: listing revisions failed: %s", exc)
        return _build_capability_refusal(
            f"Could not list source snapshots: {exc}"
        )
    if not revisions:
        return _build_capability_refusal(
            f"No source snapshot has been published to {reader.location} yet, "
            "so the observed agent's code cannot be read for any revision."
        )

    entries: list[dict[str, Any]] = []
    for revision in revisions:
        manifest = await asyncio.to_thread(reader.load_manifest, revision)
        if manifest is None:
            entries.append({"revision": revision, "complete": False})
            continue
        entries.append(
            {
                "revision": revision,
                "complete": True,
                "created_at": manifest.created_at,
                "engine": manifest.engine,
                "agent_directory": manifest.agent_directory,
                "file_count": len(manifest.files),
                "total_bytes": manifest.total_bytes,
            }
        )
    return {"revisions": entries}


async def list_source_files(
    tool_context: LaunchContext,
    *,
    glob: str = "",
    path_contains: str = "",
    revision: str = "",
) -> dict[str, Any]:
    """List source files available in the observed agent's snapshot for a deployment revision.

    Use this tool to explore repository structure and locate files before searching
    or reading code. Files excluded during publishing are listed under ignore_patterns.

    Args:
        tool_context: ADK tool context providing access to session state.
        glob: Shell-style glob pattern matching repository-relative paths (e.g.
            'app/*.py' or '**/tools/*.py'). Empty string matches all files.
        path_contains: Case-insensitive substring filter for file paths. Empty string matches all.
        revision: Deployment revision to inspect, so the code read is the code
            that ran. Take it from get_insight's occurrences[].agent_revision or
            from get_full_trajectories' agent_revision. If empty, falls back to
            the newest complete snapshot, which may not be the code that ran.

    Returns:
        Dictionary containing:
        - files: List of file metadata objects with path, size (bytes), and truncation status.
        - total_files: Total number of files matching the filters.
        - returned_files: Number of files returned in this page (up to 500).
        - truncated_listing: True if matching files exceeded the page limit.
        - files_in_snapshot: Total file count across the entire snapshot revision.
        - agent_directory: Repository subdirectory containing the agent code.
        - ignore_patterns: Publish-time ignore patterns explaining omitted paths.
        - revision: Revision identifier used for the query.
        - revision_source: Provenance of the revision: 'explicit' when the revision
          argument was supplied, 'latest_fallback' when it was omitted.
        Returns a capability refusal object if snapshots are unavailable or unconfigured.
    """
    resolved, refusal = await resolve_snapshot(tool_context.state, revision)
    if resolved is None:
        return refusal or _build_capability_refusal(
            "The source snapshot could not be read."
        )

    manifest = resolved.manifest
    needle = path_contains.lower()
    selected = [
        entry
        for entry in manifest.files
        if (not needle or needle in entry.path.lower())
        and (not glob or _matches_glob(entry.path, glob))
    ]
    page = selected[:MAX_LISTED_FILES]
    return resolved.stamp(
        {
            "files": [
                {"path": e.path, "size": e.size, "truncated": e.truncated}
                for e in page
            ],
            "total_files": len(selected),
            "returned_files": len(page),
            "truncated_listing": len(page) < len(selected),
            "files_in_snapshot": len(manifest.files),
            "agent_directory": manifest.agent_directory,
            "ignore_patterns": manifest.ignore_patterns,
        }
    )


def _matches_glob(path: str, pattern: str) -> bool:
    """Matches a repository path against a glob pattern, supporting leading recursive wildcards.

    Args:
        path: Repository-relative file path.
        pattern: Glob pattern (e.g., '**/*.py' or 'app/tools/*.py').

    Returns:
        True if the path matches the glob pattern, False otherwise.
    """
    normalized = snapshot.normalize_path(path)
    if fnmatch.fnmatch(normalized, pattern):
        return True
    if not pattern.startswith("**/"):
        return False
    tail = pattern[3:]
    if fnmatch.fnmatch(normalized, tail):
        return True
    return any(
        fnmatch.fnmatch(normalized.split("/", index)[-1], tail)
        for index in range(1, normalized.count("/") + 1)
    )


async def search_source(
    tool_context: LaunchContext,
    *,
    pattern: str,
    path_contains: str = "",
    context_lines: int = 2,
    revision: str = "",
) -> dict[str, Any]:
    """Search the observed agent's source code snapshot using a regular expression.

    Use this tool to locate function definitions, tool declarations, error messages,
    or prompt text across published source files. Quotes from results can be cited as
    `<path>:<start_line>-<end_line>`.

    Args:
        tool_context: ADK tool context providing access to session state.
        pattern: Python regular expression pattern matched against each line.
        path_contains: Case-insensitive substring filter for file paths. Empty string searches all files.
        context_lines: Number of surrounding context lines per match (default 2, maximum 20).
        revision: Deployment revision to inspect, so the code read is the code
            that ran. Take it from get_insight's occurrences[].agent_revision or
            from get_full_trajectories' agent_revision. If empty, falls back to
            the newest complete snapshot, which may not be the code that ran.

    Returns:
        Dictionary containing:
        - pattern: The regular expression searched.
        - matches: List of match objects, each containing path, line_number, line content,
          before/after context lines, and formatted citation string (`path:start-end`).
        - match_count: Total number of matches found.
        - files_searched: Number of files evaluated.
        - truncated_results: True if match results reached the maximum limit (200).
        - revision: Revision identifier used for the search.
        - revision_source: Provenance of the revision: 'explicit' when the revision
          argument was supplied, 'latest_fallback' when it was omitted.
        Returns an error dictionary if pattern is invalid or a capability refusal if unavailable.
    """
    if not pattern:
        return {
            "error": "pattern is required; use list_source_files to browse."
        }
    resolved, refusal = await resolve_snapshot(tool_context.state, revision)
    if resolved is None:
        return refusal or _build_capability_refusal(
            "The source snapshot could not be read."
        )

    try:
        # Offload synchronous GCS search operations to a worker thread.
        matches, searched, truncated = await asyncio.to_thread(
            lambda: resolved.reader.search(
                resolved.revision,
                pattern=pattern,
                path_contains=path_contains,
                context_lines=context_lines,
            )
        )
    except re.error as exc:
        return {"error": f"invalid regular expression {pattern!r}: {exc}"}
    except source_reader.READ_ERRORS as exc:
        logger.warning("source snapshot: search failed: %s", exc)
        return {"error": f"failed to search the source snapshot: {exc}"}

    return resolved.stamp(
        {
            "pattern": pattern,
            "matches": [
                {
                    "path": match.path,
                    "line_number": match.line_number,
                    "line": match.line,
                    "before": list(match.before),
                    "after": list(match.after),
                    "citation": match.citation,
                }
                for match in matches
            ],
            "match_count": len(matches),
            "files_searched": searched,
            "truncated_results": truncated,
        }
    )


async def read_source_file(
    tool_context: LaunchContext,
    *,
    path: str,
    offset: int = 1,
    limit: int = 200,
    revision: str = "",
) -> dict[str, Any]:
    """Read a range of numbered lines from a file in the observed agent's source snapshot.

    Lines are returned with line numbers to support precise `<path>:<start>-<end>` citations.
    Call with successive offsets to paginate through larger files.

    Args:
        tool_context: ADK tool context providing access to session state.
        path: Repository-relative file path (as returned by list_source_files or search_source).
        offset: 1-based starting line number (default 1).
        limit: Maximum number of lines to return (default 200).
        revision: Deployment revision to inspect, so the code read is the code
            that ran. Take it from get_insight's occurrences[].agent_revision or
            from get_full_trajectories' agent_revision. If empty, falls back to
            the newest complete snapshot, which may not be the code that ran.

    Returns:
        Dictionary containing:
        - path: Normalized repository-relative file path.
        - found: True if the file exists and was read successfully; False otherwise.
        - content: String of numbered lines formatted as `<line_number>: <line_text>`.
        - start_line: 1-based starting line number of the returned slice.
        - end_line: 1-based ending line number of the returned slice.
        - line_count: Number of lines included in content.
        - total_lines: Total line count of the published file.
        - truncated_in_snapshot: True if the file exceeded size limits at publish time.
        - revision: Revision identifier used.
        - revision_source: Provenance of the revision: 'explicit' when the revision
          argument was supplied, 'latest_fallback' when it was omitted.
        When `found` is False, includes `reason` and optional `excluded_by_pattern` explaining
        whether the file was omitted by ignore rules or absent from the repository.
        When `offset` exceeds the file length, `found` is True but the content is empty:
        `start_line` and `end_line` are omitted, and `requested_offset` and `reason` report
        the requested offset and the file's total line count.
    """
    if not path:
        return {"error": "path is required; use list_source_files to find one."}
    resolved, refusal = await resolve_snapshot(tool_context.state, revision)
    if resolved is None:
        return refusal or _build_capability_refusal(
            "The source snapshot could not be read."
        )

    manifest = resolved.manifest
    if manifest.get_entry(path) is None:
        return resolved.stamp(build_absence_reason(manifest, path))

    try:
        # Offload synchronous GCS file download to a worker thread.
        found = await asyncio.to_thread(
            lambda: resolved.reader.read_file(
                resolved.revision, path, offset=offset, limit=limit
            )
        )
    except source_reader.READ_ERRORS as exc:
        logger.warning("source snapshot: reading %s failed: %s", path, exc)
        return {"error": f"failed to read {path}: {exc}"}
    if found is None:
        return resolved.stamp(
            {
                "path": snapshot.normalize_path(path),
                "found": False,
                "reason": (
                    f"{path} is listed in the manifest for revision "
                    f"{resolved.revision} but its body is missing from the "
                    "snapshot, so the upload was incomplete."
                ),
            }
        )

    if found.is_empty:
        # Omit start_line and end_line for empty slices to avoid an invalid range.
        return resolved.stamp(
            {
                "path": found.path,
                "found": True,
                "content": "",
                "line_count": 0,
                "total_lines": found.total_lines,
                "truncated_in_snapshot": found.truncated_in_snapshot,
                "requested_offset": found.start_line,
                "reason": (
                    f"Offset {found.start_line} is past the end of {found.path}, "
                    f"which has {found.total_lines} lines in this snapshot. Read "
                    "again with a smaller offset."
                ),
            }
        )

    return resolved.stamp(
        {
            "path": found.path,
            "found": True,
            "content": _render_numbered_lines(found.start_line, found.lines),
            "start_line": found.start_line,
            "end_line": found.end_line,
            "line_count": len(found.lines),
            "total_lines": found.total_lines,
            "truncated_in_snapshot": found.truncated_in_snapshot,
        }
    )


def build_absence_reason(
    manifest: snapshot.SourceManifest, path: str
) -> dict[str, Any]:
    """Builds a diagnostic response explaining why a file path is missing from a snapshot.

    Args:
        manifest: SourceManifest for the revision.
        path: Requested repository-relative file path.

    Returns:
        Dictionary with found=False and diagnostic explanation (including any
        matching ignore pattern).
    """
    pattern = manifest.find_excluding_pattern(path)
    if pattern:
        return {
            "path": snapshot.normalize_path(path),
            "found": False,
            "excluded_by_pattern": pattern,
            "reason": (
                f"{path} is excluded by pattern `{pattern}` and is therefore not "
                "published in the snapshot. It may still exist in the repository."
            ),
        }
    return {
        "path": snapshot.normalize_path(path),
        "found": False,
        "reason": (
            f"There is no such path as {path} in the observed agent's repository "
            "at this revision, and no ignore pattern excludes it."
        ),
    }


AGENT_SOURCE_BROWSING_TOOLS = [
    list_revisions,
    list_source_files,
    search_source,
    read_source_file,
]
"""The four source browsing ADK tools exported for agent registration."""
