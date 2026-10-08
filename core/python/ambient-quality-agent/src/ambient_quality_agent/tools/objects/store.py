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

"""What a store of named objects has to answer, apart from where it keeps them.

`gcs_store.GcsObjectStore` is the deployed implementation, over a bucket;
`standalone.files.FileObjectStore` keeps the same names as files under a
directory. Two stores are laid out this way: the jobs store, holding the goal,
the memories and the attached agents' configurations, and the source store,
holding the observed agents' source snapshots. Every rule about a layout lives
with what it lays out rather than here.

Names are `/`-separated paths relative to the store's root, as in GCS. Both
implementations answer the operations below the same way;
`tests/test_object_store.py` runs one suite over both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ambient_quality_agent import providers

if TYPE_CHECKING:
    from collections.abc import Callable


class StorageNotConfiguredError(RuntimeError):
    """The backend has nowhere to keep objects, such as a deployment with no jobs bucket."""


class ObjectStore(Protocol):
    """One deployment's named objects: read, write, delete and list by prefix."""

    def read_text(self, name: str) -> str | None:
        """Reads an object as UTF-8 text.

        Args:
            name: Object name relative to the store's root.

        Returns:
            The object's text, or None if it does not exist.

        Raises:
            StorageNotConfiguredError: If the backend has nowhere to keep objects.
        """

    def write_text(
        self, name: str, text: str, *, overwrite: bool = True
    ) -> bool:
        """Writes an object as UTF-8 text, replacing it whole.

        A reader sees either the previous object or the new one, never a
        partial write.

        Args:
            name: Object name relative to the store's root.
            text: Content to write.
            overwrite: When False, the write happens only if no object has that
                name, atomically with respect to other writers.

        Returns:
            True if the object was written, False if `overwrite` was False and
            the object already existed.

        Raises:
            StorageNotConfiguredError: If the backend has nowhere to keep objects.
        """

    def read_bytes(self, name: str) -> bytes | None:
        """Reads an object's raw content.

        Args:
            name: Object name relative to the store's root.

        Returns:
            The object's bytes, or None if it does not exist.

        Raises:
            StorageNotConfiguredError: If the backend has nowhere to keep objects.
        """

    def write_bytes(self, name: str, data: bytes) -> None:
        """Writes an object's raw content, replacing it whole.

        A reader sees either the previous object or the new one, never a
        partial write.

        Args:
            name: Object name relative to the store's root.
            data: Content to write.

        Raises:
            StorageNotConfiguredError: If the backend has nowhere to keep objects.
        """

    def delete(self, name: str) -> bool:
        """Deletes an object.

        Args:
            name: Object name relative to the store's root.

        Returns:
            True if the object was deleted, False if it did not exist.

        Raises:
            StorageNotConfiguredError: If the backend has nowhere to keep objects.
        """

    def list_names(self, prefix: str) -> list[str]:
        """Lists the names of every object under a prefix, in lexical order.

        The prefix is matched as a string, as GCS does, so `goals/` also lists
        `goals/activations/...`.

        Args:
            prefix: Leading part of the names to list.

        Returns:
            Full object names, sorted.

        Raises:
            StorageNotConfiguredError: If the backend has nowhere to keep objects.
        """

    def list_prefixes(self, prefix: str) -> list[str]:
        """Lists the next level of names under a prefix, as a GCS `/` delimiter does.

        Answers "which revisions are there" without listing every file of every
        revision.

        Args:
            prefix: Empty, or a prefix ending in `/`.

        Returns:
            Each distinct `<prefix><segment>/` that some object's name starts
            with, sorted.

        Raises:
            StorageNotConfiguredError: If the backend has nowhere to keep objects.
        """

    def uri(self, name: str) -> str:
        """Returns where an object lives, for display: `gs://...` or a file path.

        Args:
            name: Object name relative to the store's root, or a prefix ending
                in `/`.

        Returns:
            A location string; the object need not exist.
        """


jobs_store_factory: Callable[[], ObjectStore] = (
    providers.build_unbound_provider("jobs_store_factory")
)
"""The store holding the goal, the memories and the attached agents' configurations.

Bound by `backends.configure_providers`: the jobs bucket in a deployment, a
directory in a standalone run.
"""

source_store_factory: Callable[[], ObjectStore | None] = (
    providers.build_unbound_provider("source_store_factory")
)
"""The store holding the observed agents' source snapshots, or None without one.

Bound by `backends.configure_providers`: the source bucket in a deployment, or
None when the deployment names none; a directory in a standalone run. Laid out
by `source_code.snapshot`.
"""
