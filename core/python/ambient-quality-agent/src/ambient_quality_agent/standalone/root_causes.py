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

"""Adapts `InMemoryStore` root causes to implement the `RootCauseWriter` contract.

Provides the write interface for root cause diagnoses. Read operations are handled
by `InMemoryInsightReader.list_root_causes`.

Diagnoses are stored append-only so revision history is preserved across agent
updates, allowing readers to track diagnostic changes alongside agent revisions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ambient_quality_agent.standalone.store import InMemoryStore
    from ambient_quality_agent.tools.insights.models import RootCause


class InMemoryRootCauseStore:
    """Appends diagnoses to the in-process `InMemoryStore`."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def save(self, record: RootCause) -> None:
        """Appends a root cause diagnosis record.

        Storage is not scoped by agent name because diagnoses reference insights
        that are already agent-scoped.

        Args:
            record: Root cause diagnosis record to persist.
        """
        self._store.append_root_cause(record)
