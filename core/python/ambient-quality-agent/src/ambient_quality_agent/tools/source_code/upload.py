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

"""What `agents-cli aqua publish-source` does, behind command routes.

The CLI talks to AQuA only through its API, so it needs no grant on the source
bucket: it sends a snapshot here in batches, and the engine writes it into the
source store (`objects.store.source_store_factory`) with its own credentials --
the source bucket in a deployment, `.aqua/source/` in a standalone run.

A snapshot arrives as:

1. Any number of `write_files` calls, each a batch of whole files, so that no
   request outgrows the command routes' body cap. A file travels gzipped and
   base64-encoded, so most source fits several to a batch; the CLI truncates
   the rare file that would not fit in one on its own, and says so in the
   manifest.
2. One `commit`, carrying the manifest. It checks that every file the manifest
   lists was written, removes any file of an earlier upload of the same
   revision that the manifest does not list, and writes the manifest last, so
   that a manifest still means the snapshot is complete.

Nothing is held between calls, so any replica can serve any of them.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import logging
import zlib
from typing import Any

from ambient_quality_agent.tools.objects import store as objects_store
from ambient_quality_agent.tools.source_code import reader, snapshot

logger = logging.getLogger(__name__)

_NO_STORE = (
    "This deployment has no source bucket, so there is nowhere to keep a "
    "source snapshot. Set AQA_SOURCE_GCS_BUCKET, or run AQuA standalone."
)

# gzip framing for `zlib`.
_GZIP_WBITS = 31


class UploadError(ValueError):
    """A batch or a manifest this module refuses, with the reason for the caller."""


def write_files(
    *, agent_name: str, revision: str, files: list[Any]
) -> dict[str, Any]:
    """Writes one batch of a snapshot's files.

    Args:
        agent_name: Observed agent the snapshot belongs to.
        revision: Runtime revision the snapshot is of.
        files: Entries of `{"path": ..., "content": ...}`, where `content` is
            the file gzipped and base64-encoded.

    Returns:
        `{"agent_name", "revision", "written"}`, or `{"error": ...}` when the
        batch is refused or there is no source store.
    """
    if len(files) > snapshot.MAX_BATCH_FILES:
        return {
            "error": (
                f"{len(files)} files in one batch; the most is "
                f"{snapshot.MAX_BATCH_FILES}"
            )
        }
    try:
        root = _build_root(agent_name, revision)
        decoded = [_decode_file(entry) for entry in files]
    except UploadError as exc:
        return {"error": str(exc)}
    store = objects_store.source_store_factory()
    if store is None:
        return {"error": _NO_STORE}
    for path, data in decoded:
        store.write_bytes(
            snapshot.build_file_object_name(revision, path, root), data
        )
    return {
        "agent_name": agent_name,
        "revision": revision,
        "written": len(decoded),
    }


def commit(
    *, agent_name: str, revision: str, manifest: dict[str, Any]
) -> dict[str, Any]:
    """Completes a snapshot by writing its manifest, last.

    Steps:
    1. Validate the manifest against the snapshot schema and the size limits.
    2. Refuse it if a file it lists was not written.
    3. Delete the revision's previous manifest, if any, and this process's
       cached copy of it.
    4. Delete any written file it does not list, left by an earlier upload of
       the same revision.
    5. Write the manifest.

    Args:
        agent_name: Observed agent the snapshot belongs to.
        revision: Runtime revision the snapshot is of.
        manifest: The manifest's fields as the CLI built them: `engine`,
            `agent_directory`, `ignore_patterns`, `files` and `omitted_files`.
            The rest are filled in here.

    Returns:
        A summary of the stored snapshot with its manifest's `uri`, or
        `{"error": ...}` when the manifest is refused or there is no source
        store.
    """
    try:
        root = _build_root(agent_name, revision)
        parsed = _build_manifest(revision, manifest)
    except UploadError as exc:
        return {"error": str(exc)}
    store = objects_store.source_store_factory()
    if store is None:
        return {"error": _NO_STORE}

    files_prefix = f"{snapshot.build_revision_prefix(revision, root)}{snapshot.FILES_PREFIX}/"
    written = {
        name.removeprefix(files_prefix)
        for name in store.list_names(files_prefix)
    }
    listed = {snapshot.normalize_path(entry.path) for entry in parsed.files}
    if missing := sorted(listed - written):
        return {
            "error": (
                f"{len(missing)} file(s) the manifest lists were not uploaded, "
                f"so the snapshot is incomplete: {', '.join(missing[:5])}"
            )
        }
    # The old manifest goes first: while stale files are removed, none lists
    # them as present.
    name = snapshot.build_manifest_object_name(revision, root)
    store.delete(name)
    reader.forget_cached_manifest(store.uri(root), revision)
    for stale in sorted(written - listed):
        store.delete(files_prefix + stale)
    store.write_text(name, parsed.model_dump_json(indent=2))
    return {
        "agent_name": agent_name,
        "revision": revision,
        "file_count": len(parsed.files),
        "total_bytes": parsed.total_bytes,
        "truncated_files": parsed.truncated_files,
        "omitted_files": parsed.omitted_files,
        "uri": store.uri(name),
    }


def _build_root(agent_name: str, revision: str) -> str:
    """Validates the snapshot's key and returns the agent's prefix.

    Args:
        agent_name: Observed agent the snapshot belongs to.
        revision: Runtime revision the snapshot is of.

    Returns:
        The agent's prefix, `<agent_name>/`.

    Raises:
        UploadError: If either is not a valid key segment.
    """
    try:
        snapshot.validate_agent_name(agent_name)
        snapshot.validate_key_segment(revision, "revision")
    except ValueError as exc:
        raise UploadError(str(exc)) from exc
    return snapshot.build_agent_root(agent_name)


def _validate_path(path: Any) -> str:
    """Checks that a file path stays inside the revision's `files/` prefix.

    Args:
        path: Repository-relative path as sent.

    Returns:
        The normalized path.

    Raises:
        UploadError: If the path is not a string, is empty, or has an empty,
            `.` or `..` segment.
    """
    if not isinstance(path, str):
        raise UploadError(f"file path is not a string: {path!r}")
    normalized = snapshot.normalize_path(path)
    if not normalized or any(
        part in ("", ".", "..") for part in normalized.split("/")
    ):
        raise UploadError(f"not a valid repository-relative path: {path!r}")
    return normalized


def _decode_file(entry: Any) -> tuple[str, bytes]:
    """Decodes one file of a batch, refusing one larger than a snapshot file may be.

    Args:
        entry: `{"path": ..., "content": <base64 of gzip>}`.

    Returns:
        The normalized path and the file's bytes.

    Raises:
        UploadError: If the entry is malformed, does not decode, or inflates
            past `snapshot.MAX_FILE_BYTES`.
    """
    if not isinstance(entry, dict):
        raise UploadError(f"file entry is not a JSON object: {entry!r}")
    path = _validate_path(entry.get("path"))
    content = entry.get("content")
    if not isinstance(content, str):
        raise UploadError(f"{path}: content is not a string")
    try:
        compressed = base64.b64decode(content, validate=True)
        inflater = zlib.decompressobj(wbits=_GZIP_WBITS)
        # Bounded, so a small body cannot inflate into an unbounded write.
        data = inflater.decompress(compressed, snapshot.MAX_FILE_BYTES + 1)
    except (binascii.Error, zlib.error) as exc:
        raise UploadError(f"{path}: content is not base64 gzip: {exc}") from exc
    if len(data) > snapshot.MAX_FILE_BYTES or inflater.unconsumed_tail:
        raise UploadError(
            f"{path}: larger than {snapshot.MAX_FILE_BYTES} bytes; the CLI "
            "truncates a file to that before it sends it"
        )
    return path, data


def _build_manifest(
    revision: str, fields: dict[str, Any]
) -> snapshot.SourceManifest:
    """Builds the manifest to store from the fields the CLI sent.

    The counters are computed here rather than taken from the request, so that
    they agree with the file list.

    Args:
        revision: Runtime revision the snapshot is of.
        fields: The manifest's fields as sent.

    Returns:
        The manifest, stamped with the revision and the time of the commit.

    Raises:
        UploadError: If the fields do not match the snapshot schema, list a
            path twice, or exceed `snapshot.MAX_SNAPSHOT_BYTES` in total.
    """
    if not isinstance(fields, dict):
        raise UploadError(f"manifest is not a JSON object: {fields!r}")
    try:
        manifest = snapshot.SourceManifest.model_validate(
            {
                "engine": fields.get("engine", ""),
                "agent_directory": fields.get("agent_directory", ""),
                "ignore_patterns": fields.get("ignore_patterns") or [],
                "files": fields.get("files") or [],
                "omitted_files": fields.get("omitted_files", 0),
            }
        )
    except ValueError as exc:
        raise UploadError(
            f"manifest does not match the snapshot schema: {exc}"
        ) from exc
    paths = [_validate_path(entry.path) for entry in manifest.files]
    if len(set(paths)) != len(paths):
        raise UploadError("manifest lists a path more than once")
    total = sum(entry.size for entry in manifest.files)
    if total > snapshot.MAX_SNAPSHOT_BYTES:
        raise UploadError(
            f"snapshot of {total} bytes is larger than "
            f"{snapshot.MAX_SNAPSHOT_BYTES} bytes"
        )
    return manifest.model_copy(
        update={
            "schema_version": snapshot.SCHEMA_VERSION,
            "revision": revision,
            "created_at": dt.datetime.now(dt.UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "total_bytes": total,
            "truncated_files": sum(entry.truncated for entry in manifest.files),
        }
    )
