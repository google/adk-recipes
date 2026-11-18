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

"""What a store of root-cause records has to answer.

`bigquery_root_cause_store.BigQueryRootCauseStore` is the deployed
implementation; `standalone.InMemoryRootCauseStore` is the other. Read queries
reside in `tools/insights/bigquery_reader.py` to combine root-cause markers with
insight aggregations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ambient_quality_agent.tools.insights.models import RootCause


class RootCauseWriter(Protocol):
    """Protocol for appending root-cause records to storage."""

    def save(self, record: RootCause) -> None:
        """Appends one diagnosis record to storage.

        Args:
            record: The diagnosis record to store.
        """
        ...
