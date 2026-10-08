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

"""Snapshots an observed agent's packaged source and sends it to AQuA.

Root-cause analysis reads the observed agent's source as deployed, keyed by
the Agent Runtime revision that runs it. This module builds that snapshot
from the project directory and uploads it through AQuA's command routes, which
write it with the engine's own credentials, as
`<agent_name>/<revision>/manifest.json` and `<agent_name>/<revision>/files/…`
in AQuA's source store. The CLI therefore needs no grant on AQuA's storage,
and the same upload reaches a deployed AQuA and a standalone one.

1. The revision is resolved with the user's credentials: the newest runtime
   revision of the observed engine.
2. The file set is what `agents-cli deploy` packages, read with the same
   `.gcloudignore`-aware walk, less image, video and audio files
   (`MEDIA_PATTERNS`), capped at `PER_FILE_LIMIT` a file and `TOTAL_LIMIT` a
   snapshot.
3. Files travel gzipped and base64-encoded, in batches that fit the command
   routes' body cap. The rare file that would not fit a batch on its own is
   truncated further and flagged as truncated.
4. The manifest is committed last, so its presence still means the snapshot is
   complete.

`pathspec` is imported lazily: it is needed only to walk the project, and the
other commands of the CLI do not pay for it.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import gzip
import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ambient_quality_cli import rest_client as rest_client_lib

METADATA_FILE = "deployment_metadata.json"
"""Deployment metadata `agents-cli deploy` writes for the observed agent."""

PER_FILE_LIMIT = 1024 * 1024
"""Bytes kept of each file; larger files are truncated and flagged."""

TOTAL_LIMIT = 50 * 1024 * 1024
"""Bytes kept of a snapshot; the files after it are omitted and counted."""

MEDIA_PATTERNS = (
    # Images.
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.bmp",
    "*.tif",
    "*.tiff",
    "*.webp",
    "*.ico",
    "*.heic",
    "*.avif",
    # Video.
    "*.mp4",
    "*.mov",
    "*.avi",
    "*.mkv",
    "*.webm",
    "*.mpeg",
    "*.mpg",
    "*.m4v",
    "*.wmv",
    # Audio.
    "*.mp3",
    "*.wav",
    "*.flac",
    "*.aac",
    "*.ogg",
    "*.opus",
    "*.m4a",
)
"""Gitignore-style patterns of image, video and audio files, left out of a snapshot.

A root-cause analysis session reads the snapshot as text, so these files would
only take up room in its context. Any other packaged file is sent, because the
files an agent reads cannot be known in advance. The patterns are recorded in
the manifest's `ignore_patterns`, which is how the engine explains that such a
file is absent.
"""

BATCH_BYTES = 768 * 1024
"""Encoded bytes sent in one upload request.

Below the command routes' 1 MiB body cap, with room for the JSON around it.
"""

BATCH_FILES = 200
"""Files sent in one upload request: the most the engine accepts in one.

Mirrors the engine's `snapshot.MAX_BATCH_FILES`.
"""

MAX_BODY_BYTES = 1 << 20
"""The command routes' body cap, which the manifest's commit must fit too."""

# Room each file takes in a batch beyond its content: its path and the JSON
# punctuation around both.
_ENTRY_OVERHEAD = 64

# Agent Runtime revisions are exposed on v1beta1 only; v1 returns 404.
_REVISIONS_API_VERSION = "v1beta1"
_REVISIONS_KEY = "reasoningEngineRuntimeRevisions"


class SnapshotError(RuntimeError):
    """The snapshot cannot be built or uploaded; the message says why."""


def load_deployment_metadata(root: Path) -> dict[str, Any]:
    """Loads the observed agent's deployment metadata from its project root.

    Args:
        root: Root directory of the observed agent project.

    Returns:
        The parsed metadata, which has a `remote_agent_runtime_id`.

    Raises:
        SnapshotError: If the file is missing, is not JSON, or names no engine.
    """
    path = root / METADATA_FILE
    if not path.is_file():
        raise SnapshotError(
            f"no {METADATA_FILE} at '{root}'; deploy the observed agent first"
        )
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise SnapshotError(f"could not parse '{path}': {error}") from error
    if not isinstance(metadata, dict) or not metadata.get(
        "remote_agent_runtime_id"
    ):
        raise SnapshotError(f"'{path}' has no remote_agent_runtime_id")
    return metadata


def extract_engine_location(engine: str) -> str:
    """Extracts the region from an Agent Runtime resource name.

    Args:
        engine: Full resource name, `projects/P/locations/L/reasoningEngines/ID`.

    Returns:
        The location segment.

    Raises:
        SnapshotError: If the resource name has no location segment.
    """
    parts = engine.split("/")
    if "locations" in parts:
        index = parts.index("locations")
        if index + 1 < len(parts) and parts[index + 1]:
            return parts[index + 1]
    raise SnapshotError(f"no location in engine resource name '{engine}'")


def _parse_created_at(revision: dict[str, Any]) -> dt.datetime:
    """Parses a runtime revision's RFC 3339 creation timestamp.

    Args:
        revision: One `reasoningEngineRuntimeRevisions` entry.

    Returns:
        The parsed creation timestamp.

    Raises:
        SnapshotError: If the timestamp is absent or unparseable. Without it
            the snapshot could be attributed to the wrong revision.
    """
    created = str(revision.get("createTime") or "")
    if not created:
        raise SnapshotError(f"runtime revision has no createTime: {revision!r}")
    try:
        return dt.datetime.fromisoformat(created)
    except ValueError as error:
        raise SnapshotError(
            f"unparseable createTime '{created}': {error}"
        ) from error


def resolve_newest_revision(revisions: Iterable[dict[str, Any]]) -> str:
    """Selects the newest revision ID by creation timestamp.

    Revision IDs are sparse and non-sequential, so only the timestamp orders
    them. Every entry must carry one: an unorderable entry anywhere in the batch
    makes "newest" a guess, and guessing wrong attributes the snapshot to a
    revision that never ran this code.

    Args:
        revisions: Runtime revision entries as returned by `list_revisions`.

    Returns:
        The revision ID: the last segment of the newest revision's resource name.

    Raises:
        SnapshotError: If there are no revisions, a revision has no usable
            timestamp, or the newest one has no name.
    """
    newest: dict[str, Any] | None = None
    newest_created: dt.datetime | None = None
    for revision in revisions:
        created = _parse_created_at(revision)
        if newest_created is None or created > newest_created:
            newest, newest_created = revision, created
    if newest is None:
        raise SnapshotError(
            "the Agent Runtime agent reports no runtime revisions"
        )
    revision_id = str(newest.get("name") or "").rsplit("/", 1)[-1]
    if not revision_id:
        raise SnapshotError(f"unnamed runtime revision: {newest!r}")
    return revision_id


def list_revisions(
    engine: str, api: rest_client_lib.JsonApi
) -> list[dict[str, Any]]:
    """Lists an Agent Runtime's runtime revisions, across every page.

    Args:
        engine: Full Agent Runtime resource name.
        api: REST client carrying the user's credentials.

    Returns:
        Every runtime revision entry.

    Raises:
        rest_client_lib.ApiError: If the API answers with an error.
        SnapshotError: If the resource name has no location.
    """
    url = (
        f"https://{extract_engine_location(engine)}-aiplatform.googleapis.com/"
        f"{_REVISIONS_API_VERSION}/{engine}/runtimeRevisions"
    )
    revisions: list[dict[str, Any]] = []
    page_token = ""
    while True:
        payload = api.get_json(
            url, params={"pageToken": page_token} if page_token else None
        )
        revisions.extend(payload.get(_REVISIONS_KEY) or [])
        page_token = str(payload.get("nextPageToken") or "")
        if not page_token:
            return revisions


# TODO(b/560444388): Replace with agents-cli public packaging API once available.
# `_read_ignore_lines` and `_list_packaged_paths` mirror private helpers in
# `google.agents.cli.deploy.agent_runtime` to match the exact file set packaged
# by `agents-cli deploy` using a `.gcloudignore`-aware walk.

# Always ignored, mirroring gcloud's generated .gcloudignore defaults.
_DEFAULT_IGNORE_LINES = (".git", ".gcloudignore", ".gitignore")
_INCLUDE_DIRECTIVE = "#!include:"


def _read_ignore_lines(root: Path) -> list[str]:
    """Read gitignore-style patterns from the project ignore file.

    Checks `.gcloudignore` if present, falling back to `.gitignore`, and adds
    :data:`_DEFAULT_IGNORE_LINES`. Expands top-level `#!include:<file>` entries.

    Args:
        root: Project root directory.

    Returns:
        List of ignore pattern strings.
    """
    lines = list(_DEFAULT_IGNORE_LINES)
    source = root / ".gcloudignore"
    if not source.exists():
        source = root / ".gitignore"
    if not source.exists():
        return lines
    for raw in source.read_text(encoding="utf-8").splitlines():
        directive = raw.strip()
        if directive.startswith(_INCLUDE_DIRECTIVE):
            included = root / directive.removeprefix(_INCLUDE_DIRECTIVE).strip()
            if included.exists():
                lines += included.read_text(encoding="utf-8").splitlines()
        else:
            lines.append(raw)
    return lines


def _list_packaged_paths(root: Path) -> list[str]:
    """List `./`-prefixed paths of files under `root`, excluding ignored paths.

    Args:
        root: Project root directory.

    Returns:
        List of packaged relative path strings starting with `./`.
    """
    import pathspec

    spec = pathspec.PathSpec.from_lines(
        "gitwildmatch", _read_ignore_lines(root)
    )
    files: list[str] = []
    # Sort dirs/files for a deterministic, reproducible archive order.
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        # Prune ignored directories in place so os.walk never descends into them
        # (the trailing slash tells gitwildmatch to match directory patterns).
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not spec.match_file((rel_dir / d).as_posix() + "/")
        )
        # A file in a kept directory can still be individually ignored (e.g.
        # ``*.secret``), so check each one even after pruning its parent.
        for name in sorted(filenames):
            rel = (rel_dir / name).as_posix()
            if not spec.match_file(rel):
                files.append(f"./{rel}")
    return files


def list_packaged_files(root: Path) -> tuple[list[str], list[str]]:
    """Determines the files `agents-cli deploy` packages and the rules it applies.

    Args:
        root: Project root directory to evaluate.

    Returns:
        A tuple containing:
            - List of relative file paths included in packaging.
            - List of ignore pattern strings applied.
    """
    paths = [path.removeprefix("./") for path in _list_packaged_paths(root)]
    return paths, _read_ignore_lines(root)


def _read_capped(path: Path) -> tuple[bytes, bool]:
    """Reads file contents up to PER_FILE_LIMIT without loading unbounded files into memory.

    Reads one byte past the limit to detect truncation efficiently.

    Args:
        path: Path to the target file.

    Returns:
        A tuple of (file_bytes, is_truncated).
    """
    with path.open("rb") as handle:
        data = handle.read(PER_FILE_LIMIT + 1)
    if len(data) <= PER_FILE_LIMIT:
        return data, False
    return data[:PER_FILE_LIMIT], True


def _encode(data: bytes) -> str:
    """Encodes a file as it travels: gzipped, then base64.

    Args:
        data: The file's bytes.

    Returns:
        The encoded content.
    """
    # mtime=0 keeps the encoding a function of the content alone.
    return base64.b64encode(gzip.compress(data, mtime=0)).decode("ascii")


def _encode_to_fit(data: bytes, budget: int) -> tuple[bytes, str]:
    """Encodes a file, truncating it until the encoding fits one batch.

    Only content that does not compress, such as minified or generated text,
    reaches the truncation: 1 MiB of source compresses well below the budget.

    Args:
        data: The file's bytes, already capped at PER_FILE_LIMIT.
        budget: Encoded bytes the file may take.

    Returns:
        The bytes kept and their encoding.
    """
    encoded = _encode(data)
    while len(encoded) > budget:
        # base64 inflates by 4/3; gzip of content that does not compress
        # adds a little more, which the next round removes.
        data = data[: min(len(data) * 9 // 10, budget * 3 // 4)]
        encoded = _encode(data)
    return data, encoded


@dataclasses.dataclass(frozen=True)
class PackedFile:
    """One snapshot file as it travels."""

    path: str
    content: str
    """Gzipped, then base64-encoded."""


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """A project's source, packed for upload."""

    files: list[PackedFile]
    manifest: dict[str, Any]
    """The manifest's fields the engine takes from the CLI."""

    skipped_files: int = 0
    """Packaged files left out because they match `MEDIA_PATTERNS`."""

    @property
    def total_bytes(self) -> int:
        return sum(entry["size"] for entry in self.manifest["files"])

    @property
    def truncated_files(self) -> int:
        return sum(entry["truncated"] for entry in self.manifest["files"])


def pack(root: Path, *, engine: str, agent_directory: str) -> Snapshot:
    """Packs the files `agents-cli deploy` packages, within the size limits.

    Files matching `MEDIA_PATTERNS` are left out.

    Args:
        root: Root directory of the observed agent project.
        engine: Full Agent Runtime resource name, recorded for provenance.
        agent_directory: The agent's directory relative to the root.

    Returns:
        The packed files and the manifest's fields.
    """
    import pathspec

    packaged, ignore_patterns = list_packaged_files(root)
    media = pathspec.PathSpec.from_lines("gitwildmatch", MEDIA_PATTERNS)
    paths = [path for path in packaged if not media.match_file(path)]
    budget = BATCH_BYTES - _ENTRY_OVERHEAD
    files: list[PackedFile] = []
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    omitted_files = 0
    for index, relative in enumerate(paths):
        try:
            data, truncated = _read_capped(root / relative)
        except OSError:
            # Skip unreadable or deleted files so individual file errors do not abort the snapshot.
            omitted_files += 1
            continue
        kept, encoded = _encode_to_fit(data, budget - len(relative))
        if total_bytes + len(kept) > TOTAL_LIMIT:
            omitted_files += len(paths) - index
            break
        total_bytes += len(kept)
        files.append(PackedFile(path=relative, content=encoded))
        entries.append(
            {
                "path": relative,
                "size": len(kept),
                "truncated": truncated or len(kept) < len(data),
            }
        )
    return Snapshot(
        files=files,
        skipped_files=len(packaged) - len(paths),
        manifest={
            "engine": engine,
            "agent_directory": agent_directory,
            "ignore_patterns": [*ignore_patterns, *MEDIA_PATTERNS],
            "files": entries,
            "omitted_files": omitted_files,
        },
    )


def build_batches(files: list[PackedFile]) -> list[list[PackedFile]]:
    """Groups the files into upload requests that each fit the body cap.

    Args:
        files: The packed files, each of which fits a batch on its own.

    Returns:
        The batches, in file order.
    """
    batches: list[list[PackedFile]] = []
    current: list[PackedFile] = []
    size = 0
    for packed in files:
        cost = len(packed.content) + len(packed.path) + _ENTRY_OVERHEAD
        if current and (
            size + cost > BATCH_BYTES or len(current) >= BATCH_FILES
        ):
            batches.append(current)
            current, size = [], 0
        current.append(packed)
        size += cost
    if current:
        batches.append(current)
    return batches


PostCommand = Callable[[str, dict[str, Any]], dict[str, Any]]
"""Sends one command route request to AQuA: a route and a body, to the reply."""


def upload(
    post_command: PostCommand,
    *,
    agent_name: str,
    revision: str,
    snapshot: Snapshot,
    upload_route: str,
    commit_route: str,
    on_batch: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Uploads a packed snapshot in batches, then commits its manifest.

    Args:
        post_command: Sends one command route request to AQuA.
        agent_name: Observed agent the snapshot belongs to.
        revision: Runtime revision the snapshot is of.
        snapshot: The packed snapshot.
        upload_route: The route that writes a batch of files.
        commit_route: The route that writes the manifest.
        on_batch: Called with (batches done, batches in all) after each batch.

    Returns:
        The engine's summary of the stored snapshot.

    Raises:
        SnapshotError: If the manifest would not fit one request, or the engine
            refuses a batch or the manifest.
    """
    commit_body = {
        "agent_name": agent_name,
        "revision": revision,
        "manifest": snapshot.manifest,
    }
    # Checked before anything is sent, so a snapshot that cannot be committed
    # writes nothing.
    if len(json.dumps(commit_body)) > MAX_BODY_BYTES - 1024:
        raise SnapshotError(
            f"the manifest of {len(snapshot.files)} files does not fit one "
            "request; add the generated or vendored directories to "
            ".gcloudignore"
        )
    batches = build_batches(snapshot.files)
    for done, batch in enumerate(batches, start=1):
        reply = post_command(
            upload_route,
            {
                "agent_name": agent_name,
                "revision": revision,
                "files": [
                    {"path": packed.path, "content": packed.content}
                    for packed in batch
                ],
            },
        )
        if reply.get("error"):
            raise SnapshotError(str(reply["error"]))
        if on_batch:
            on_batch(done, len(batches))
    reply = post_command(commit_route, commit_body)
    if reply.get("error"):
        raise SnapshotError(str(reply["error"]))
    return reply


def resolve_agent_for_engine(listing: dict[str, Any], engine: str) -> str:
    """Chooses the attached agent a snapshot of an engine belongs to.

    Steps:
    1. The attached agent whose `observed_engine_id` is the engine's.
    2. Otherwise the agent the deployment watches, unless it records another
       engine: filing one agent's code under another's name would have
       root-cause analysis cite the wrong code.
    3. With nothing attached, the agent the environment names, as when AQuA
       was deployed beside the agent.

    Args:
        listing: The reply of the `agents/list` route.
        engine: Full resource name of the observed engine.

    Returns:
        The agent's name, or the empty string if no agent can be told to be
        the engine's.
    """
    engine_id = engine.rsplit("/", 1)[-1]
    agents = [a for a in listing.get("agents") or [] if "error" not in a]
    for agent in agents:
        recorded = str(agent.get("observed_engine_id") or "")
        if recorded and recorded.rsplit("/", 1)[-1] == engine_id:
            return str(agent["agent_name"])
    watched = next((a for a in agents if a.get("watched")), None)
    if watched:
        recorded = str(watched.get("observed_engine_id") or "")
        if recorded and recorded.rsplit("/", 1)[-1] != engine_id:
            return ""
        return str(watched["agent_name"])
    if agents:
        return ""
    return str(listing.get("environment_agent") or "")
