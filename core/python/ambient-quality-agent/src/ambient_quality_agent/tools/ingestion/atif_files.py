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

"""Read access to the files under an attached ATIF prefix: GCS or a directory.

`atif_source.AtifFileSource` keeps every rule (roots, paths, limits,
references) and reaches the files only through `AtifFiles`, so both backends
share that logic. A key is a `/`-separated name relative to the prefix, as in
GCS.

Every failure to read one file, whatever its cause (a missing object, a
denied read, a dropped connection, a path the file system refuses), is an
`ObjectReadError`, so one file never fails a whole batch. Keys come from
untrusted documents and are never put in a log message.

`ObjectStore` (`tools/objects/store.py`) does not fit here: it reports no
object time or size, cannot bound a read, and resolves no symbolic links.

Wiring::

    AtifFileSource
        │ list_objects(), resolve_key(), find_key(), stat_object(),
        │ read_object(), build_uri()
        ▼
    AtifFiles (Protocol)
        ▲ implements
        ├── GcsAtifFiles ───► Cloud Storage: list_blobs, get_blob,
        │                     ranged download without decompression
        └── LocalAtifFiles ─► the directory, symbolic links resolved

Key and URI mapping::

    GcsAtifFiles("acme", "runs")  key "a/b.json" <-> "gs://acme/runs/a/b.json"
    LocalAtifFiles(Path("/runs")) key "a/b.json" <-> "/runs/a/b.json"
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import os
import stat as stat_module
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol

from ambient_quality_agent.tools import gcs
from ambient_quality_shared.atif_uri import GcsPrefix, parse_atif_uri

_LOGGER = logging.getLogger(__name__)

_GCS_SCHEME = "gs://"

# Opening never blocks on a FIFO or a device, and never follows a link in the
# last component; the path is already resolved, so it has none.
_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)

# Where Linux shows the path of an open file descriptor.
_PROC_FD = Path("/proc/self/fd")


@dataclasses.dataclass(frozen=True)
class ObjectInfo:
    """One file under the prefix.

    Attributes:
        key: Name relative to the prefix.
        size: Size in bytes, as stored; None when the backend did not say.
        updated: Object time in UTC: the GCS `updated` time or the file's
            `mtime`; None when the backend did not say.
    """

    key: str
    size: int | None
    updated: dt.datetime | None


class ObjectReadError(Exception):
    """A file, or the prefix itself, that could not be read.

    Attributes:
        status: The HTTP status, such as `404` or `403`, for the marker's
            `detail`; None when there is none.
    """

    def __init__(self, status: str | None, message: str) -> None:
        """Initializes the error.

        Args:
            status: The HTTP status, or None.
            message: What failed, without the key; never file content.
        """
        super().__init__(message)
        self.status = status


class ObjectTooLargeError(Exception):
    """A file over the size limit of the read.

    Attributes:
        size: The size the metadata reported, or the bytes read past the
            limit when the metadata did not show it.
    """

    def __init__(self, size: int) -> None:
        """Initializes the error.

        Args:
            size: The size known to exceed the limit.
        """
        super().__init__(f"{size} bytes is over the limit")
        self.size = size


class PathOutsidePrefixError(Exception):
    """A key whose file resolves outside the prefix, through a symbolic link."""


class AtifFiles(Protocol):
    """The files under one attached prefix."""

    def list_objects(self) -> Iterator[ObjectInfo]:
        """Lists every file under the prefix, one at a time.

        Returns:
            The files, in no particular order.

        Raises:
            ObjectReadError: When the prefix cannot be listed: a missing or
                unreadable directory, or a bucket that is missing or denied.
                A GCS prefix with no objects lists as empty.
        """
        ...

    def resolve_key(self, key: str) -> str:
        """Resolves symbolic links in a key, refusing a file outside the prefix.

        Args:
            key: A normalized key, without `.` or `..` segments.

        Returns:
            The key of the file the link points to; `key` itself when there is
            no link.

        Raises:
            PathOutsidePrefixError: When the file resolves outside the prefix.
            ObjectReadError: When the path cannot be resolved at all.
        """
        ...

    def find_key(self, location: str) -> str | None:
        """Finds the key of a `gs://` URL.

        Args:
            location: A `gs://` URL (scheme in any case).

        Returns:
            Its key when it names a file under the prefix; None when it names
            something outside. A local directory holds no `gs://` location,
            so its answer is always None.
        """
        ...

    def stat_object(self, key: str) -> ObjectInfo:
        """Reads a file's size and time without reading its content.

        Args:
            key: A key from `resolve_key`.

        Returns:
            The file's metadata.

        Raises:
            ObjectReadError: When the file is missing, not a regular file, or
                cannot be read.
            PathOutsidePrefixError: When a symbolic link now points outside.
        """
        ...

    def read_object(self, key: str, max_bytes: int) -> tuple[bytes, ObjectInfo]:
        """Reads a file whole, refusing one over `max_bytes`.

        The limit is checked against the metadata before reading and enforced
        while reading. GCS objects are read as stored, without decompression,
        so a compressed object cannot exceed its listed size.

        Args:
            key: A key from `resolve_key`.
            max_bytes: Maximum bytes to accept.

        Returns:
            The bytes and the file's metadata.

        Raises:
            ObjectReadError: When the file is missing, not a regular file, or
                cannot be read.
            ObjectTooLargeError: When the file is over `max_bytes`.
            PathOutsidePrefixError: When a symbolic link now points outside.
        """
        ...

    def build_uri(self, key: str) -> str:
        """Builds where a file lives, for `source_uri` and locations.

        Args:
            key: A key under the prefix.

        Returns:
            Its `gs://` URL, or its absolute local path.
        """
        ...


def create_atif_files(
    atif_uri: str, *, client_factory: Callable[[], Any] | None = None
) -> AtifFiles:
    """Creates the file access for an attached `atif_uri`.

    Args:
        atif_uri: `gs://<bucket>[/<prefix>]`, or a local directory.
        client_factory: Returns a `storage.Client` for a `gs://` URI; defaults
            to one client per thread.

    Returns:
        `GcsAtifFiles` for a `gs://` URI, otherwise `LocalAtifFiles`.

    Raises:
        ValueError: When `parse_atif_uri` refuses the value (empty, unsupported
            scheme, invalid bucket name, `?` or `#`, or an empty, `.` or `..`
            segment in the prefix).
    """
    parsed = parse_atif_uri(atif_uri)
    if isinstance(parsed, GcsPrefix):
        return GcsAtifFiles(
            parsed.bucket,
            parsed.prefix,
            client_factory=client_factory
            or gcs.build_per_thread_client_factory(),
        )
    return LocalAtifFiles(Path(parsed.path))


def normalize_key(segments: list[str]) -> str | None:
    """Normalizes path segments into a key, refusing one that leaves the root.

    Empty and `.` segments are dropped and `..` removes the segment before it.

    Args:
        segments: The segments, the base directory's first.

    Returns:
        The key, or None when `..` climbs above the root or nothing is left.
    """
    stack: list[str] = []
    for segment in segments:
        if segment in ("", "."):
            continue
        if segment == "..":
            if not stack:
                return None
            stack.pop()
        else:
            stack.append(segment)
    return "/".join(stack) or None


def normalize_utc(value: dt.datetime) -> dt.datetime:
    """Converts a time to UTC; a naive time is taken as UTC.

    Args:
        value: The time.

    Returns:
        The time in UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _normalize_optional_utc(value: Any) -> dt.datetime | None:
    """Converts a backend's object time to UTC, if it gave one.

    Args:
        value: The time, or anything else when the backend gave none.

    Returns:
        The time in UTC, or None.
    """
    return normalize_utc(value) if isinstance(value, dt.datetime) else None


def _client_to_read_error(err: Exception, what: str) -> ObjectReadError:
    """Converts any client error into an `ObjectReadError`.

    Args:
        err: The error: a Cloud Storage API error, a transport error, an
            authentication error, or anything else the client raised.
        what: What was being read, for the message; never a key.

    Returns:
        The error, with the HTTP status when the client reported one.
    """
    code = getattr(err, "code", None)
    status = str(int(code)) if isinstance(code, int) else None
    return ObjectReadError(
        status, f"Could not read {what}: {type(err).__name__}"
    )


class GcsAtifFiles:
    """The objects under `gs://<bucket>/<prefix>`."""

    def __init__(
        self, bucket: str, prefix: str, *, client_factory: Callable[[], Any]
    ) -> None:
        """Initializes the access; nothing is read until the first call.

        Args:
            bucket: The bucket.
            prefix: The prefix inside the bucket; leading and trailing `/` are
                ignored, and empty means the whole bucket.
            client_factory: Returns a `storage.Client`.
        """
        self._bucket = bucket
        trimmed = prefix.strip("/")
        self._prefix = f"{trimmed}/" if trimmed else ""
        self._client_factory = client_factory

    def list_objects(self) -> Iterator[ObjectInfo]:
        try:
            for blob in self._client_factory().list_blobs(
                self._bucket, prefix=self._prefix
            ):
                name: str = blob.name
                if not name.startswith(self._prefix) or name.endswith("/"):
                    continue
                yield ObjectInfo(
                    key=name[len(self._prefix) :],
                    size=blob.size,
                    updated=_normalize_optional_utc(blob.updated),
                )
        except Exception as err:
            raise _client_to_read_error(err, "the prefix") from err

    def resolve_key(self, key: str) -> str:
        return key

    def find_key(self, location: str) -> str | None:
        if location[: len(_GCS_SCHEME)].lower() != _GCS_SCHEME:
            return None
        bucket, _, name = location[len(_GCS_SCHEME) :].partition("/")
        if bucket != self._bucket:
            return None
        normalized = normalize_key(name.split("/"))
        if normalized is None or not normalized.startswith(self._prefix):
            return None
        return normalized[len(self._prefix) :] or None

    def stat_object(self, key: str) -> ObjectInfo:
        blob = self._get_blob(key)
        return ObjectInfo(
            key=key,
            size=blob.size,
            updated=_normalize_optional_utc(blob.updated),
        )

    def read_object(self, key: str, max_bytes: int) -> tuple[bytes, ObjectInfo]:
        blob = self._get_blob(key)
        info = ObjectInfo(
            key=key,
            size=blob.size,
            updated=_normalize_optional_utc(blob.updated),
        )
        if info.size is not None and info.size > max_bytes:
            raise ObjectTooLargeError(info.size)
        if info.size == 0:
            return b"", info
        try:
            # `end` is inclusive, so a file over the limit returns one byte
            # more than `max_bytes`.
            data = blob.download_as_bytes(
                start=0, end=max_bytes, raw_download=True
            )
        except Exception as err:
            raise _client_to_read_error(err, "an object") from err
        if len(data) > max_bytes:
            raise ObjectTooLargeError(len(data))
        return data, info

    def build_uri(self, key: str) -> str:
        return f"gs://{self._bucket}/{self._prefix}{key}"

    def _get_blob(self, key: str) -> Any:
        """Reads an object's metadata.

        Args:
            key: The object's key.

        Returns:
            The `storage.Blob`, with `size` and `updated` set.

        Raises:
            ObjectReadError: When the object is missing or cannot be read.
        """
        try:
            blob = (
                self._client_factory()
                .bucket(self._bucket)
                .get_blob(f"{self._prefix}{key}")
            )
        except Exception as err:
            raise _client_to_read_error(err, "an object") from err
        if blob is None:
            raise ObjectReadError("404", "The object does not exist")
        return blob


class LocalAtifFiles:
    """The files under a local directory, for a standalone run.

    The directory's own symbolic links are resolved once. Every file is
    checked after its links are resolved, opened without following a link or
    blocking, checked to be a regular file, and checked again through the
    opened descriptor, so a link swapped in after the check cannot reach
    outside.
    """

    def __init__(self, root: Path) -> None:
        """Resolves symbolic links on `root`; no files under it are read yet.

        Args:
            root: The directory.
        """
        self._root = Path(os.path.realpath(root.expanduser()))

    def list_objects(self) -> Iterator[ObjectInfo]:
        try:
            with os.scandir(self._root):
                pass
        except OSError as err:
            raise _os_to_read_error(err, "the directory") from err
        skipped = 0

        def count_skipped(_: OSError) -> None:
            nonlocal skipped
            skipped += 1

        for directory, _, filenames in os.walk(
            self._root, onerror=count_skipped
        ):
            for filename in filenames:
                path = Path(directory) / filename
                key = path.relative_to(self._root).as_posix()
                try:
                    resolved = self._resolve_path(key)
                    file_stat = resolved.stat()
                except (OSError, PathOutsidePrefixError, ObjectReadError):
                    continue
                if stat_module.S_ISREG(file_stat.st_mode):
                    yield _stat_to_object_info(key, file_stat)
        if skipped:
            _LOGGER.warning(
                "Skipped %d unreadable directories under the attached "
                "directory.",
                skipped,
            )

    def resolve_key(self, key: str) -> str:
        return self._resolve_path(key).relative_to(self._root).as_posix()

    def find_key(self, location: str) -> str | None:
        del location
        return None

    def stat_object(self, key: str) -> ObjectInfo:
        fd, file_stat = self._open_regular(key)
        os.close(fd)
        return _stat_to_object_info(key, file_stat)

    def read_object(self, key: str, max_bytes: int) -> tuple[bytes, ObjectInfo]:
        fd, file_stat = self._open_regular(key)
        with os.fdopen(fd, "rb") as file:
            if file_stat.st_size > max_bytes:
                raise ObjectTooLargeError(file_stat.st_size)
            try:
                data = file.read(max_bytes + 1)
            except OSError as err:
                raise _os_to_read_error(err, "a file") from err
        if len(data) > max_bytes:
            raise ObjectTooLargeError(len(data))
        return data, _stat_to_object_info(key, file_stat)

    def build_uri(self, key: str) -> str:
        return str(self._root / key)

    def _resolve_path(self, key: str) -> Path:
        """Resolves a key's symbolic links and checks the file is inside.

        Args:
            key: A key under the directory.

        Returns:
            The resolved path.

        Raises:
            PathOutsidePrefixError: When it resolves outside the directory.
            ObjectReadError: When the path cannot be resolved, such as one
                with a NUL character.
        """
        try:
            resolved = Path(os.path.realpath(self._root / key))
        except (OSError, ValueError) as err:
            raise ObjectReadError(None, "Could not resolve a path") from err
        if not resolved.is_relative_to(self._root) or resolved == self._root:
            raise PathOutsidePrefixError
        return resolved

    def _open_regular(self, key: str) -> tuple[int, os.stat_result]:
        """Opens a key's file for reading, checking what was opened.

        Args:
            key: A key under the directory.

        Returns:
            The open descriptor, which the caller closes, and its `fstat`.

        Raises:
            PathOutsidePrefixError: When the file, or the file actually
                opened, is outside the directory.
            ObjectReadError: When it is missing, not a regular file, or
                cannot be opened.
        """
        path = self._resolve_path(key)
        try:
            fd = os.open(path, _OPEN_FLAGS)
        except OSError as err:
            raise _os_to_read_error(err, "a file") from err
        try:
            file_stat = os.fstat(fd)
            if not stat_module.S_ISREG(file_stat.st_mode):
                raise ObjectReadError("404", "Not a regular file")
            if not self._is_opened_inside(fd, path, file_stat):
                raise PathOutsidePrefixError
        except BaseException:
            os.close(fd)
            raise
        return fd, file_stat

    def _is_opened_inside(
        self, fd: int, path: Path, file_stat: os.stat_result
    ) -> bool:
        """Checks that an open descriptor is a file inside the directory.

        On Linux the kernel reports the descriptor's path. Elsewhere the path
        is resolved again and must name the same file, which narrows the
        window for a swapped link to the time between the two checks.

        Args:
            fd: The open descriptor.
            path: The resolved path it was opened from.
            file_stat: Its `fstat`.

        Returns:
            True when the opened file is inside the directory.
        """
        try:
            opened = Path(os.readlink(_PROC_FD / str(fd)))
        except OSError:
            try:
                resolved = Path(os.path.realpath(path))
                current = resolved.stat()
            except OSError:
                return False
            return (
                (current.st_dev, current.st_ino)
                == (file_stat.st_dev, file_stat.st_ino)
                and resolved.is_relative_to(self._root)
                and resolved != self._root
            )
        return opened.is_relative_to(self._root) and opened != self._root


def _stat_to_object_info(key: str, file_stat: os.stat_result) -> ObjectInfo:
    """Builds a file's metadata from its `stat`.

    Args:
        key: The file's key.
        file_stat: Its `stat`.

    Returns:
        The size and the `mtime` in UTC.
    """
    return ObjectInfo(
        key=key,
        size=file_stat.st_size,
        updated=dt.datetime.fromtimestamp(file_stat.st_mtime, tz=dt.UTC),
    )


def _os_to_read_error(err: OSError, what: str) -> ObjectReadError:
    """Converts a file system error into an `ObjectReadError`.

    A missing file reads as `404` and a denied one as `403`, the statuses a
    GCS read reports for the same cases.

    Args:
        err: The error.
        what: What was being read, for the message; never a key.

    Returns:
        The error.
    """
    if isinstance(err, PermissionError):
        status: str | None = "403"
    elif isinstance(
        err,
        (FileNotFoundError, NotADirectoryError, IsADirectoryError),
    ):
        status = "404"
    else:
        status = None
    return ObjectReadError(status, f"Could not read {what}: {err.strerror}")
