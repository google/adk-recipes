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

"""`ObjectStore` over a local directory, for a standalone run.

An object named `memories/abc.json` is the file `<root>/memories/abc.json`,
so the layout matches the jobs bucket and the files can be read and edited by
hand. Unlike the in-memory store, these outlive the process: a goal written in
one standalone run is there for the next.

Writes go to a hidden temporary file beside the target and are renamed over
it, so a reader never sees a partial file. Create-if-absent is an exclusive
rename, so two writers cannot both create the same name. `list_names` skips
those temporary files, `.<name>.*.tmp`.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

JOBS_DIR = Path(".aqua") / "job"
"""Where a standalone run keeps its files, relative to the working directory.

`.aqua/` is gitignored, both here and in a project `agents-cli` scaffolds."""

SOURCE_DIR_NAME = "source"
"""Where a standalone run keeps the source snapshots: `.aqua/source/`, beside
the jobs directory, as the source bucket is beside the jobs bucket."""


class FileObjectStore:
    """Objects at `<root>/<name>`."""

    def __init__(self, root: Path) -> None:
        """Initializes the store; the directory is created by the first write.

        Args:
            root: Directory holding the objects.
        """
        self._root = root.absolute()

    def _path(self, name: str) -> Path:
        """Resolves an object name to its file, refusing one outside the root.

        A memory id reaches here from a route, so a name like `../x` must not
        become a file outside the store.

        Args:
            name: Object name relative to the root.

        Returns:
            The file's path.

        Raises:
            ValueError: If the name is empty, absolute, or leaves the root.
        """
        parts = name.split("/")
        if (
            not name
            or name.startswith("/")
            or any(p in ("", ".", "..") for p in parts)
        ):
            raise ValueError(f"not a valid object name: {name!r}")
        return self._root.joinpath(*parts)

    def read_text(self, name: str) -> str | None:
        data = self.read_bytes(name)
        return None if data is None else data.decode("utf-8")

    def read_bytes(self, name: str) -> bytes | None:
        try:
            return self._path(name).read_bytes()
        # A directory is a prefix, which GCS also answers with "no such
        # object".
        except (FileNotFoundError, IsADirectoryError):
            return None

    def write_text(
        self, name: str, text: str, *, overwrite: bool = True
    ) -> bool:
        return self._write(name, text.encode("utf-8"), overwrite=overwrite)

    def write_bytes(self, name: str, data: bytes) -> None:
        self._write(name, data, overwrite=True)

    def _write(self, name: str, data: bytes, *, overwrite: bool) -> bool:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            if overwrite:
                os.replace(tmp, path)
                return True
            try:
                # A hard link fails if the target exists, which makes
                # create-if-absent atomic without ever exposing a partial file.
                os.link(tmp, path)
            except FileExistsError:
                return False
            return True
        finally:
            Path(tmp).unlink(missing_ok=True)

    def delete(self, name: str) -> bool:
        try:
            self._path(name).unlink()
        except FileNotFoundError:
            return False
        return True

    def list_names(self, prefix: str) -> list[str]:
        if not self._root.is_dir():
            return []
        names = []
        for path in self._root.rglob("*"):
            if _is_temporary(path):
                continue
            name = path.relative_to(self._root).as_posix()
            if path.is_file() and name.startswith(prefix):
                names.append(name)
        return sorted(names)

    def list_prefixes(self, prefix: str) -> list[str]:
        directory = self._path(prefix.rstrip("/")) if prefix else self._root
        if not directory.is_dir():
            return []
        return sorted(
            f"{prefix}{child.name}/"
            for child in directory.iterdir()
            if child.is_dir()
        )

    def uri(self, name: str) -> str:
        # A prefix such as `memories/` names a directory, and the empty prefix
        # the root.
        if not name.rstrip("/"):
            return f"{self._root}/"
        path = str(self._path(name.rstrip("/")))
        return path + "/" if name.endswith("/") else path


def _is_temporary(path: Path) -> bool:
    """Checks whether a file is a write in progress rather than an object.

    Object names may hold dot segments, such as a snapshot's `.github/`, so
    only the temporary files `_write` creates are skipped.

    Args:
        path: A file under the store's root.

    Returns:
        True for a `.<name>.*.tmp` file.
    """
    return path.name.startswith(".") and path.name.endswith(".tmp")
