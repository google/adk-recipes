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

"""The references in an ATIF document, resolved before the source returns it.

A file is untrusted input, and ATIF allows any path and any URL, so each
`source.path` on an image or audio part, and each subagent reference's
`trajectory_path`, is read only when it resolves under the attached prefix:

| Form                         | Resolves to              | When not read     |
| ---------------------------- | ------------------------ | ----------------- |
| Relative path                | A file, from the         | `read_error`      |
|                              | document's directory     |                   |
| `gs://` URL under the prefix | A file                   | `read_error`      |
| `data:` URL                  | Its decoded bytes        | `invalid_content` |
| `http://` or `https://` URL  | A link, never fetched    | none              |
| Relative path or `gs://` URL | Nothing                  | `path_traversal`  |
| outside the prefix           |                          |                   |
| Absolute path                | Nothing                  | `path_traversal`  |
| Another scheme               | Nothing                  | `out_of_scope`    |
| Over the attachment cap      | Nothing                  | `too_large`       |

A relative path resolves from the directory where the file holding it was
listed or referenced, the directory the producer wrote it in, even when that
file is a symbolic link; every target is then checked after symbolic links
are resolved. A reference that fails for any cause gets a marker and never
fails the document. The resolver writes nothing into `extra.aqua`; it returns
its markers as `PendingMissingMarker`s and, for each subagent it embeds, the
copy's path and the `trajectory_path` it was read for. `annotate_document`
writes both once the document is validated.

A subagent reference is handled in one of three ways:

1. It resolves to an embedded subagent: nothing to do.
2. It has a `trajectory_path` and no match: the file is read, validated,
   embedded in the `subagent_trajectories` of the trajectory that holds the
   reference, and the reference gets its `trajectory_id` when unset. A file
   that validation rejects, such as one with an invalid `extra.aqua` key, is
   not embedded.
3. It cannot be resolved: it gets a `trajectory` marker. A reference with
   neither a match nor a `trajectory_path`, such as a v1.6 reference that has
   only `session_id`, gets its marker from `annotate_document`.

Example, for a root file at `runs/t-001.atif.json`::

    root JSON (`source.path` stays as written):
      steps[0].message[1].source.path = "img/ok.png"
      steps[0].message[2].source.path = "img/missing.png"
      subagent_trajectory_ref[0]      = {"trajectory_path": "sub/qa.json"}

    resolver outputs (`extra.aqua` stays untouched):
      locations:
        ("steps", 0, "message", 1) -> ExternalAttachment(key="runs/img/ok.png", ...)
      markers:
        ("steps", 0) -> MissingMarker("message[2]", READ_ERROR, "404")
    subagents:
      root["subagent_trajectories"][0] holds sub/qa.json, and the parent
      reference's `trajectory_id` points to it
"""

from __future__ import annotations

import collections
import dataclasses
import enum
import logging
import re
from typing import Any

from ambient_quality_agent.tools.ingestion.atif_files import (
    AtifFiles,
    ObjectReadError,
    ObjectTooLargeError,
    PathOutsidePrefixError,
    normalize_key,
)
from ambient_quality_agent.tools.ingestion.trajectory_source import (
    MAX_ATTACHMENT_BYTES,
    AttachmentUnreadableError,
    ExternalAttachment,
    ResolvedAttachment,
    check_data_url,
)
from aqua_atif import (
    AQUA_KEY,
    MAX_JSON_DEPTH,
    SUBAGENT_TRAJECTORY,
    DocumentRejectedError,
    HolderKind,
    JsonPath,
    MissingMarker,
    PendingMissingMarker,
    Reason,
    SubagentIndex,
    add_embedded_subagent,
    build_subagent_index,
    format_location,
    format_path,
    get_aqua,
    get_value,
    has_subagent_id,
    list_holders,
    parse_document,
    resolve_subagent,
    validate_document,
)

_LOGGER = logging.getLogger(__name__)

_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")

# The part types whose `source.path` the resolver reads: ATIF's media parts in
# `message` and `content`, plus `file` in the `extra.aqua` bridge.
_MEDIA_PART_TYPES = ("image", "audio")
_BRIDGE_MEDIA_PART_TYPES = ("image", "audio", "file")

# Marker details: the name of the check that failed.
CYCLE = "cycle"
DUPLICATE_ID = "duplicate_trajectory_id"
EMPTY_PATH = "empty_path"
ID_MISMATCH = "trajectory_id_mismatch"
INVALID_ATIF = "invalid_atif"
INVALID_JSON = "invalid_json"
NUL_CHARACTER = "nul_character"

# A queued file: the path of its trajectory from the root, the directory its
# relative paths resolve from, and the chain of resolved keys that led to it.
_QueuedFile = tuple[JsonPath, str, frozenset[str]]


@dataclasses.dataclass(frozen=True)
class AtifLimits:
    """The caps the source applies before it parses or copies anything.

    Attributes:
        document_bytes: Maximum bytes of a root file, and of the root plus all
            embedded subagent files combined.
        attachment_bytes: Maximum bytes of one attachment, including a decoded
            `data:` URL.
        references: Maximum references read in one document (attachment files,
            `data:` URLs, and subagent files).
        subagent_nesting: Deepest level at which a subagent is embedded from a
            file; the root is level 0.
    """

    document_bytes: int = 256 * 1024 * 1024
    attachment_bytes: int = MAX_ATTACHMENT_BYTES
    references: int = 1000
    subagent_nesting: int = 8


class PathForm(enum.Enum):
    """The forms a `source.path` or `trajectory_path` can take."""

    RELATIVE = "relative"
    GCS = "gcs"
    ABSOLUTE = "absolute"
    DATA = "data"
    LINK = "link"
    OTHER_SCHEME = "other_scheme"


def resolve_path_form(path: str) -> PathForm:
    """Resolves which form a reference's path has.

    Schemes are matched in any case, as URL schemes are.

    Args:
        path: The path as written.

    Returns:
        Its form; a path without a scheme that does not start with `/` is
        relative.
    """
    lowered = path[:8].lower()
    if lowered.startswith("gs://"):
        return PathForm.GCS
    if lowered.startswith("data:"):
        return PathForm.DATA
    if lowered.startswith(("http://", "https://")):
        return PathForm.LINK
    if path.startswith("/"):
        return PathForm.ABSOLUTE
    if _SCHEME_RE.match(path):
        return PathForm.OTHER_SCHEME
    return PathForm.RELATIVE


def _measure_depth(value: Any) -> int:
    """Measures how deep JSON containers nest, as the parser counts it.

    Args:
        value: A parsed JSON value.

    Returns:
        1 for an object or list holding no container, 0 for a scalar.
    """
    if isinstance(value, dict):
        return 1 + max(map(_measure_depth, value.values()), default=0)
    if isinstance(value, list):
        return 1 + max(map(_measure_depth, value), default=0)
    return 0


def _count_nesting(trajectory_path: JsonPath) -> int:
    """Counts the subagent level of a trajectory; the root is level 0.

    Args:
        trajectory_path: Path of the trajectory from the document root.

    Returns:
        How many `subagent_trajectories` lists enclose it.
    """
    return sum(
        1 for segment in trajectory_path if segment == "subagent_trajectories"
    )


def _extract_directory(key: str) -> str:
    """Extracts the directory of a key.

    Args:
        key: A key under the prefix.

    Returns:
        Its directory, or `""` at the prefix itself.
    """
    return key.rpartition("/")[0]


@dataclasses.dataclass(frozen=True)
class _LocatedFile:
    """A file a reference names, under the prefix.

    Attributes:
        key: Where the reference puts it, before symbolic links are resolved;
            relative paths in the file resolve from this key's directory.
        resolved_key: The file it is, after symbolic links are resolved.
    """

    key: str
    resolved_key: str


@dataclasses.dataclass(frozen=True)
class _Unreadable:
    """Internal cache entry for an attachment or subagent path not read."""

    reason: Reason
    detail: str | None = None


class DocumentResolver:
    """Resolves every reference in one document, embedding subagent files.

    Attributes:
        markers: A marker for each part or subagent reference not read, and
            parsing's markers for each embedded file.
        locations: For each readable part with a `source.path`, keyed by the
            part's path from the document root, the attachment to copy.
    """

    def __init__(
        self,
        files: AtifFiles,
        limits: AtifLimits,
        document: dict[str, Any],
        *,
        bytes_read: int,
    ) -> None:
        """Initializes the resolver.

        Args:
            files: Access to the files under the prefix.
            limits: The caps.
            document: The parsed root document, changed in place by
                `resolve`.
            bytes_read: Bytes already read for the document: the root file.
        """
        self._files = files
        self._limits = limits
        self._document = document
        self._bytes_read = bytes_read
        self._reference_count = 0
        self._media_cache: dict[
            tuple[str, str], ResolvedAttachment | _Unreadable | None
        ] = {}
        self._index: SubagentIndex = build_subagent_index(document)
        self._embedded_by_path: dict[tuple[JsonPath, str], str] = {}
        self.markers: list[PendingMissingMarker] = []
        self.locations: dict[JsonPath, ResolvedAttachment] = {}

    def resolve(self, root_key: str, resolved_root_key: str) -> None:
        """Resolves the references of the root file and every file it embeds.

        Files are taken breadth first. Each carries the chain of files that
        led to it, so a reference back into that chain is a cycle.

        Args:
            root_key: The root file's key as listed; its relative paths
                resolve from this key's directory.
            resolved_root_key: The same file with symbolic links resolved.
        """
        queue: collections.deque[_QueuedFile] = collections.deque(
            [((), _extract_directory(root_key), frozenset({resolved_root_key}))]
        )
        while queue:
            trajectory_path, directory, chain = queue.popleft()
            self._resolve_file(trajectory_path, directory, chain, queue)

    def _resolve_file(
        self,
        trajectory_path: JsonPath,
        directory: str,
        chain: frozenset[str],
        queue: collections.deque[_QueuedFile],
    ) -> None:
        """Resolves the references in one file's trajectory.

        Covers the subagents embedded in the same file, whose relative paths
        resolve from the same directory. Copies embedded while this runs are
        queued, not walked here.

        Args:
            trajectory_path: Path of the file's trajectory from the root.
            directory: Directory the file's relative paths resolve from.
            chain: The resolved keys of the files that led to this one,
                itself included.
            queue: Receives each embedded file.
        """
        trajectory = get_value(self._document, trajectory_path)
        for holder in list_holders(trajectory):
            path = (*trajectory_path, *holder.path)
            if holder.kind == HolderKind.STEP:
                self._resolve_parts(path, holder.value, "message", directory)
            elif holder.kind == HolderKind.RESULT:
                self._resolve_parts(path, holder.value, "content", directory)
            elif holder.kind == HolderKind.SUBAGENT_REF:
                self._resolve_subagent_ref(
                    path,
                    holder.value,
                    (*trajectory_path, *holder.trajectory),
                    directory,
                    chain,
                    queue,
                )

    def _resolve_parts(
        self,
        holder_path: JsonPath,
        holder: dict[str, Any],
        field: str,
        directory: str,
    ) -> None:
        """Resolves the parts of a step's `message` or a result's `content`.

        Covers both standard ATIF parts on `holder[field]` and ATIF v1.8 bridge
        parts on `holder["extra"]["aqua"][field]`.

        Args:
            holder_path: Path of the step or result from the root.
            holder: The step or result.
            field: `message` or `content`.
            directory: Directory the paths resolve from.
        """
        self._resolve_part_list(
            holder_path,
            holder.get(field),
            (field,),
            _MEDIA_PART_TYPES,
            directory,
        )
        aqua = get_aqua(holder)
        if aqua is not None:
            self._resolve_part_list(
                holder_path,
                aqua.get(field),
                ("extra", AQUA_KEY, field),
                _BRIDGE_MEDIA_PART_TYPES,
                directory,
            )

    def _resolve_part_list(
        self,
        holder_path: JsonPath,
        parts: Any,
        field_prefix: JsonPath,
        media_types: tuple[str, ...],
        directory: str,
    ) -> None:
        """Resolves every media part in one part list.

        Args:
            holder_path: Path of the step or result from the root.
            parts: Candidate list of part dicts.
            field_prefix: Relative path from `holder_path` to `parts`.
            media_types: Part `type` values whose `source.path` is resolved.
            directory: Directory relative paths resolve from.
        """
        if not isinstance(parts, list):
            return
        for index, part in enumerate(parts):
            if isinstance(part, dict) and part.get("type") in media_types:
                self._resolve_part(
                    holder_path, field_prefix, index, part, directory
                )

    def _resolve_part(
        self,
        holder_path: JsonPath,
        field_prefix: JsonPath,
        index: int,
        part: dict[str, Any],
        directory: str,
    ) -> None:
        """Resolves one part's `source.path`.

        Args:
            holder_path: Path of the step or result from the root.
            field_prefix: Relative path from `holder_path` to the part list.
            index: The part's index.
            part: The part.
            directory: Directory the path resolves from.
        """
        source = part.get("source")
        path = source.get("path") if isinstance(source, dict) else None
        if not isinstance(path, str):
            return
        relative = (*field_prefix, index)
        part_path = (*holder_path, *relative)
        location = self._resolve_media(path, directory, part_path)
        if location is None:
            return
        if isinstance(location, _Unreadable):
            self.markers.append(
                PendingMissingMarker(
                    holder_path,
                    MissingMarker(
                        format_path(relative), location.reason, location.detail
                    ),
                )
            )
            return
        self.locations[part_path] = location

    def _resolve_media(
        self, path: str, directory: str, part_path: JsonPath
    ) -> ResolvedAttachment | _Unreadable | None:
        """Resolves a part's `source.path`, once per path and directory.

        Args:
            path: The path as written.
            directory: Directory a relative path resolves from.
            part_path: Path of the part from the root, for the log.

        Returns:
            The readable attachment, `_Unreadable` when it cannot be read, or
            None for an `http://` or `https://` link (kept untouched in the
            document). An unexpected error is a `read_error` with no detail, so
            one reference never fails the document.
        """
        cache_key = (directory, path)
        if cache_key not in self._media_cache:
            try:
                cached = self._locate_media(path, directory, part_path)
            except Exception as err:
                _LOGGER.warning(
                    "Could not resolve the part at %s: %s",
                    format_location(part_path),
                    type(err).__name__,
                )
                cached = _Unreadable(Reason.READ_ERROR)
            self._media_cache[cache_key] = cached
        return self._media_cache[cache_key]

    def _locate_media(
        self, path: str, directory: str, part_path: JsonPath
    ) -> ResolvedAttachment | _Unreadable | None:
        """Resolves a part's `source.path` by the path rules.

        Args:
            path: The path as written.
            directory: Directory a relative path resolves from.
            part_path: Path of the part from the root, for the log.

        Returns:
            The readable attachment, `_Unreadable`, or None for an `http://` or
            `https://` link.
        """
        if not path:
            return _Unreadable(Reason.INVALID_CONTENT, EMPTY_PATH)
        form = resolve_path_form(path)
        if form == PathForm.LINK:
            return None
        if form == PathForm.OTHER_SCHEME:
            return _Unreadable(Reason.OUT_OF_SCOPE)
        if form == PathForm.DATA:
            if not self._count_reference():
                return self._build_too_many()
            try:
                return check_data_url(path, self._limits.attachment_bytes)
            except AttachmentUnreadableError as err:
                return _Unreadable(err.reason, err.detail)
        located = self._locate_file(path, form, directory)
        if isinstance(located, _Unreadable):
            return located
        if not self._count_reference():
            return self._build_too_many()
        try:
            info = self._files.stat_object(located.resolved_key)
        except ObjectReadError as err:
            _LOGGER.info(
                "Could not read the attachment of the part at %s: status %s",
                format_location(part_path),
                err.status,
            )
            return _Unreadable(Reason.READ_ERROR, err.status)
        except PathOutsidePrefixError:
            return _Unreadable(Reason.PATH_TRAVERSAL)
        if info.size is not None and info.size > self._limits.attachment_bytes:
            return _Unreadable(Reason.TOO_LARGE, str(info.size))
        return ExternalAttachment(
            uri=self._files.build_uri(located.resolved_key),
            key=located.resolved_key,
            size=info.size,
        )

    def _locate_file(
        self, path: str, form: PathForm, directory: str
    ) -> _LocatedFile | _Unreadable:
        """Finds the key of a relative path or a `gs://` URL.

        Args:
            path: The path as written.
            form: Its form: relative, `gs://` or absolute.
            directory: Directory a relative path resolves from.

        Returns:
            The located file, or `_Unreadable`. Returns `invalid_content` for a
            NUL character, `path_traversal` for an absolute path or a target
            outside the prefix, and `read_error` when the path cannot be
            resolved.
        """
        if "\x00" in path:
            return _Unreadable(Reason.INVALID_CONTENT, NUL_CHARACTER)
        if form == PathForm.ABSOLUTE:
            return _Unreadable(Reason.PATH_TRAVERSAL)
        if form == PathForm.RELATIVE:
            key = normalize_key([*directory.split("/"), *path.split("/")])
        else:
            key = self._files.find_key(path)
        if key is None:
            return _Unreadable(Reason.PATH_TRAVERSAL)
        try:
            return _LocatedFile(key, self._files.resolve_key(key))
        except PathOutsidePrefixError:
            return _Unreadable(Reason.PATH_TRAVERSAL)
        except ObjectReadError as err:
            return _Unreadable(Reason.READ_ERROR, err.status)

    def _count_reference(self) -> bool:
        """Counts one reference read against the cap.

        Returns:
            False when the document already used every reference allowed.
        """
        if self._reference_count >= self._limits.references:
            return False
        self._reference_count += 1
        return True

    def _build_too_many(self) -> _Unreadable:
        """Builds an unreadable result for a reference past the reference cap.

        Returns:
            `_Unreadable` with `too_large` and the cap as detail.
        """
        return _Unreadable(Reason.TOO_LARGE, str(self._limits.references))

    def _resolve_subagent_ref(
        self,
        ref_path: JsonPath,
        ref: dict[str, Any],
        trajectory_path: JsonPath,
        directory: str,
        chain: frozenset[str],
        queue: collections.deque[_QueuedFile],
    ) -> None:
        """Resolves a subagent reference, embedding its file when needed.

        Args:
            ref_path: Path of the reference from the root.
            ref: The reference.
            trajectory_path: Path of the trajectory that holds it.
            directory: Directory a relative path resolves from.
            chain: The resolved keys of the files that led to the holding
                file, itself included.
            queue: Receives the embedded file.
        """
        if resolve_subagent(self._index, ref_path, ref) is not None:
            return
        path = ref.get("trajectory_path")
        if not isinstance(path, str):
            return
        ref_id = ref.get("trajectory_id")
        if ref_id is not None and not isinstance(ref_id, str):
            return
        try:
            reason = self._embed_subagent(
                ref_path, ref, path, trajectory_path, directory, chain, queue
            )
        except Exception as err:
            _LOGGER.warning(
                "Could not resolve the subagent reference at %s: %s",
                format_location(ref_path),
                type(err).__name__,
            )
            reason = (Reason.READ_ERROR, None)
        if reason is not None:
            self.markers.append(
                PendingMissingMarker(
                    ref_path, MissingMarker(SUBAGENT_TRAJECTORY, *reason)
                )
            )

    def _embed_subagent(
        self,
        ref_path: JsonPath,
        ref: dict[str, Any],
        path: str,
        trajectory_path: JsonPath,
        directory: str,
        chain: frozenset[str],
        queue: collections.deque[_QueuedFile],
    ) -> tuple[Reason, str | None] | None:
        """Reads, validates and embeds the file a subagent reference names.

        The copy keeps its `trajectory_id`. A file without one gets, on the
        copy only, the reference's `trajectory_id` when set, and otherwise the
        reference's `trajectory_path`, so the reference resolves to it. An
        empty `trajectory_id` counts as none.

        Args:
            ref_path: Path of the reference from the root.
            ref: The reference.
            path: Its `trajectory_path`.
            trajectory_path: Path of the trajectory that holds it.
            directory: Directory a relative path resolves from.
            chain: The resolved keys of the files that led to the holding
                file, itself included.
            queue: Receives the embedded file.

        Returns:
            The reason and detail of the `trajectory` marker when the file is
            not embedded; None when it is.
        """
        if not path:
            return Reason.INVALID_CONTENT, EMPTY_PATH
        form = resolve_path_form(path)
        if form in (PathForm.LINK, PathForm.DATA, PathForm.OTHER_SCHEME):
            return Reason.OUT_OF_SCOPE, None
        if _count_nesting(trajectory_path) + 1 > self._limits.subagent_nesting:
            return Reason.TOO_LARGE, str(self._limits.subagent_nesting)
        holding = get_value(self._document, trajectory_path)
        subagents = holding.get("subagent_trajectories")
        if subagents is not None and not isinstance(subagents, list):
            return Reason.INVALID_CONTENT, INVALID_ATIF
        located = self._locate_file(path, form, directory)
        if isinstance(located, _Unreadable):
            return located.reason, located.detail
        if located.resolved_key in chain:
            return Reason.INVALID_CONTENT, CYCLE
        ref_id = ref.get("trajectory_id")
        ref_id = ref_id if isinstance(ref_id, str) and ref_id else None
        if ref_id is None:
            existing_id = self._embedded_by_path.get(
                (trajectory_path, located.resolved_key)
            )
            if existing_id is not None:
                ref["trajectory_id"] = existing_id
                return None
        if not self._count_reference():
            return Reason.TOO_LARGE, str(self._limits.references)
        budget = max(self._limits.document_bytes - self._bytes_read, 0)
        try:
            data, _ = self._files.read_object(located.resolved_key, budget)
        except ObjectTooLargeError as err:
            return Reason.TOO_LARGE, str(err.size)
        except ObjectReadError as err:
            _LOGGER.info(
                "Could not read the subagent file of the reference at %s: "
                "status %s",
                format_location(ref_path),
                err.status,
            )
            return Reason.READ_ERROR, err.status
        except PathOutsidePrefixError:
            return Reason.PATH_TRAVERSAL, None
        self._bytes_read += len(data)

        try:
            parsed = parse_document(data)
        except DocumentRejectedError:
            return Reason.INVALID_CONTENT, INVALID_JSON
        copy_path = (
            *trajectory_path,
            "subagent_trajectories",
            len(subagents) if subagents is not None else 0,
        )
        # The copy's containers nest below its place in the document, and the
        # whole document stays within the parser's limit.
        if len(copy_path) + _measure_depth(parsed.value) > MAX_JSON_DEPTH:
            return Reason.INVALID_CONTENT, INVALID_JSON
        try:
            validate_document(parsed.value)
        except DocumentRejectedError:
            # The rejection reason can quote a key of the file, so it is not
            # logged.
            _LOGGER.info(
                "Did not embed the subagent of the reference at %s: "
                "validation rejected the file",
                format_location(ref_path),
            )
            return Reason.INVALID_CONTENT, INVALID_ATIF

        copy: dict[str, Any] = parsed.value
        # An empty id counts as none, as on a root.
        file_id = copy.get("trajectory_id") or None
        if file_id is not None and ref_id is not None and ref_id != file_id:
            return Reason.INVALID_CONTENT, ID_MISMATCH
        copy_id = file_id or ref_id or path
        if has_subagent_id(self._index, trajectory_path, copy_id):
            return Reason.INVALID_CONTENT, DUPLICATE_ID

        copy["trajectory_id"] = copy_id
        if ref_id is None:
            ref["trajectory_id"] = copy_id
        self._embedded_by_path.setdefault(
            (trajectory_path, located.resolved_key), copy_id
        )
        if subagents is None:
            subagents = holding["subagent_trajectories"] = []
        subagents.append(copy)
        add_embedded_subagent(self._index, trajectory_path, copy_path, copy)
        self.markers.extend(
            PendingMissingMarker((*copy_path, *marker.holder), marker.marker)
            for marker in parsed.markers
        )
        queue.append(
            (
                copy_path,
                _extract_directory(located.key),
                chain | {located.resolved_key},
            )
        )
        return None
