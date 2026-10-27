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

"""Which agent's goal and memories a request or a run reads and writes.

Each attached agent keeps its documents under ``agents/<agent key>/`` in the
jobs store, beside its attachment object ``agents/<agent key>.json``.
`AgentDocumentStore` presents that prefix as a store of its own, so `goal` and
`memories` lay out the same relative names for every agent.

**The instance's documents belong to the default agent.** The goal and memories
at the root of the jobs store are the default agent's, the default attachment
(`resolve_default_agent_key`). It reads them
where its own prefix holds nothing under a name, so they stay where they are.
Its writes go to its own prefix, which then shadows the instance's copy, and
deleting a memory deletes both copies. Which agent is the default can change,
and the instance's documents follow it. The code-metric library belongs to the
default agent the same way, but is read whole, never merged file by file with
the agent's own (`metrics.publishing`).

A request or a run that resolves no agent, because none is attached, reads and
writes the instance's documents. One whose default agent cannot be resolved,
because the attachments cannot be read, fails rather than falling back to the
instance's documents, where its writes would land unseen.

An agent's documents outlive its detachment, and are its own again if it is
attached under the same key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ambient_quality_agent.tools.objects import store as objects
from ambient_quality_agent.tools.observed_agent_config import effective_config
from ambient_quality_agent.tools.observed_agent_config import (
    store as agent_store,
)

if TYPE_CHECKING:
    from ambient_quality_agent.core.session_state import ADKStateLike
    from ambient_quality_agent.tools.objects.store import ObjectStore

AGENT_DOCUMENTS_PREFIX = "agents/"
"""Under which each agent's documents are kept, at ``agents/<agent key>/``."""


def build_agent_documents_prefix(agent_key: str) -> str:
    """Builds the prefix an agent's documents are kept under.

    Slashes in the key are replaced, as in the attachment's object name, so a
    key cannot reach outside its own prefix.

    Args:
        agent_key: Key of the agent.

    Returns:
        ``agents/<agent key>/``.
    """
    return f"{AGENT_DOCUMENTS_PREFIX}{agent_key.replace('/', '_')}/"


def resolve_default_agent_key() -> str:
    """Resolves the default agent, failing if the attachments cannot be read.

    `effective_config.resolve_default_agent_key` answers a failed read as no
    agent, and caches that answer. Read that way here, the default agent's
    documents would be written to the instance's, where its own shadow them.

    Returns:
        The default attached agent's key; empty when none is attached or there
        is no jobs store.

    Raises:
        Exception: Whatever listing or reading the attachments raised.
    """
    try:
        return agent_store.resolve_default(objects.jobs_store_factory()) or ""
    except objects.StorageNotConfiguredError:
        return ""


def resolve_selected_agent_key(state: ADKStateLike) -> str:
    """Resolves the agent a request selects, else the default agent.

    The request's key is checked where the request arrives
    (`command_routes._build_route_context`).

    Args:
        state: Session state; `effective_config.AGENT_KEY_STATE` in it selects
            the agent.

    Returns:
        The agent's key; empty when the request selects none and none is
        attached.

    Raises:
        Exception: Whatever reading the attachments raised, when the request
            selects no agent.
    """
    selected = str(state.get(effective_config.AGENT_KEY_STATE) or "").strip()
    return selected or resolve_default_agent_key()


def inherits_instance_documents(agent_key: str) -> bool:
    """Checks whether an agent reads the documents kept for the whole instance.

    Args:
        agent_key: Key of the agent.

    Returns:
        Whether `agent_key` is the default agent's key.

    Raises:
        Exception: Whatever reading the attachments raised.
    """
    return bool(agent_key) and agent_key == resolve_default_agent_key()


def resolve_document_store(agent_key: str | None) -> ObjectStore:
    """Resolves the store holding an agent's goal and memories.

    Args:
        agent_key: Key of the agent; empty or None when no agent is resolved.

    Returns:
        The agent's `AgentDocumentStore`, or the jobs store itself when
        `agent_key` is empty.

    Raises:
        Exception: Whatever reading the attachments raised, while resolving
            whether the agent is the default one.
    """
    jobs = objects.jobs_store_factory()
    if not agent_key:
        return jobs
    return AgentDocumentStore(
        jobs, agent_key, inherit=inherits_instance_documents(agent_key)
    )


class AgentDocumentStore:
    """One agent's documents, as an `ObjectStore` rooted at its prefix.

    With `inherit`, a name the agent's prefix does not hold is read from the
    root of the jobs store, and listings cover both, leaving out `agents/`.
    `uri` always names the agent's prefix, where the next write goes, even for
    an object read from the root.
    """

    def __init__(
        self,
        store: ObjectStore,
        agent_key: str,
        *,
        inherit: bool = False,
    ) -> None:
        """Initializes the view.

        Args:
            store: The jobs store.
            agent_key: Key of the agent whose documents these are.
            inherit: Whether the instance's documents are read where the
                agent's prefix holds nothing.
        """
        self._store = store
        self._prefix = build_agent_documents_prefix(agent_key)
        self._inherits = inherit

    def _should_read_instance(self, name: str) -> bool:
        """Checks whether a name is also read, written or deleted at the root.

        The root's own `agents/` holds every agent's documents and
        attachments, none of which is the instance's, so no name under it is
        ever reached through the root.

        Args:
            name: Object name or prefix, relative to the agent's prefix.

        Returns:
            Whether the agent inherits the instance's documents and `name` is
            not under `agents/`.
        """
        return self._inherits and not name.startswith(AGENT_DOCUMENTS_PREFIX)

    def read_text(self, name: str) -> str | None:
        """Reads an object, from the agent's prefix, else from the instance.

        Args:
            name: Object name relative to the agent's prefix.

        Returns:
            The object's text, or None if neither holds it.
        """
        text = self._store.read_text(self._prefix + name)
        if text is None and self._should_read_instance(name):
            return self._store.read_text(name)
        return text

    def write_text(
        self, name: str, text: str, *, overwrite: bool = True
    ) -> bool:
        """Writes an object under the agent's prefix.

        Args:
            name: Object name relative to the agent's prefix.
            text: Content to write.
            overwrite: When False, the write happens only if no object has that
                name, in the agent's prefix or the instance's documents it
                reads.

        Returns:
            True if the object was written, False if `overwrite` was False and
            the object already existed.
        """
        if (
            not overwrite
            and self._should_read_instance(name)
            and self._store.read_text(name) is not None
        ):
            return False
        return self._store.write_text(
            self._prefix + name, text, overwrite=overwrite
        )

    def read_bytes(self, name: str) -> bytes | None:
        """Reads an object's bytes, from the agent's prefix, else the root.

        Args:
            name: Object name relative to the agent's prefix.

        Returns:
            The object's bytes, or None if neither holds it.
        """
        data = self._store.read_bytes(self._prefix + name)
        if data is None and self._should_read_instance(name):
            return self._store.read_bytes(name)
        return data

    def write_bytes(self, name: str, data: bytes) -> None:
        """Writes an object's raw content under the agent's prefix.

        Args:
            name: Object name relative to the agent's prefix.
            data: Content to write.
        """
        self._store.write_bytes(self._prefix + name, data)

    def delete(self, name: str) -> bool:
        """Deletes an object, and the instance's copy the agent reads.

        Both go, or the instance's copy would reappear once the agent's is
        deleted.

        Args:
            name: Object name relative to the agent's prefix.

        Returns:
            True if any copy was deleted, False if neither existed.
        """
        deleted = self._store.delete(self._prefix + name)
        if self._should_read_instance(name):
            deleted = self._store.delete(name) or deleted
        return deleted

    def list_names(self, prefix: str) -> list[str]:
        """Lists the names under a prefix, in the agent's prefix and the root.

        Args:
            prefix: Leading part of the names to list, relative to the agent's
                prefix.

        Returns:
            Names relative to the agent's prefix, each once, sorted.
        """
        names = {
            name.removeprefix(self._prefix)
            for name in self._store.list_names(self._prefix + prefix)
        }
        if self._should_read_instance(prefix):
            names.update(
                name
                for name in self._store.list_names(prefix)
                if self._should_read_instance(name)
            )
        return sorted(names)

    def list_prefixes(self, prefix: str) -> list[str]:
        """Lists the next level of names under a prefix, in both places.

        Args:
            prefix: Empty, or a prefix ending in `/`, relative to the agent's
                prefix.

        Returns:
            Each distinct `<prefix><segment>/`, relative to the agent's prefix,
            sorted.
        """
        prefixes = {
            name.removeprefix(self._prefix)
            for name in self._store.list_prefixes(self._prefix + prefix)
        }
        if self._should_read_instance(prefix):
            prefixes.update(
                p
                for p in self._store.list_prefixes(prefix)
                if self._should_read_instance(p)
            )
        return sorted(prefixes)

    def uri(self, name: str) -> str:
        """Returns where an object of the agent's lives, for display.

        Args:
            name: Object name relative to the agent's prefix.

        Returns:
            A location string; the object need not exist.
        """
        return self._store.uri(self._prefix + name)
