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

"""Persistence layer for root-cause records.

Defines the `RootCauseWriter` protocol and its BigQuery implementation,
`RootCauseStore`. Read queries reside in `tools/insights/bigquery_reader.py` to
combine root-cause markers with insight aggregations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from google.cloud import bigquery

if TYPE_CHECKING:
    from ambient_quality_agent.tools.insights.models import RootCause

ROOT_CAUSES_TABLE = "insight_root_causes"
"""Append-only table name where queries resolve the latest record per occurrence."""


class RootCauseWriter(Protocol):
    """Protocol for appending root-cause records to storage."""

    def save(self, record: RootCause) -> None:
        """Appends one diagnosis record to storage.

        Args:
            record: The diagnosis record to store.
        """
        ...


class RootCauseStore:
    """Appends root-cause records to BigQuery, one load job per record."""

    def __init__(
        self,
        *,
        client: bigquery.Client,
        project_id: str,
        dataset: str,
    ) -> None:
        """Binds the store to a BigQuery dataset.

        Args:
            client: Shared BigQuery client.
            project_id: GCP project ID containing the dataset.
            dataset: BigQuery dataset name containing `ROOT_CAUSES_TABLE`.
        """
        self._client = client
        self._project_id = project_id
        self._dataset = dataset

    def _build_table_ref(self, table: str) -> str:
        """Returns a fully-qualified ``project.dataset.table`` reference.

        Args:
            table: Target table name.

        Returns:
            Fully-qualified table identifier.
        """
        return f"{self._project_id}.{self._dataset}.{table}"

    def save(self, record: RootCause) -> None:
        """Appends a diagnosis record to BigQuery.

        Args:
            record: Diagnosis record to persist.
        """
        row: dict[str, Any] = {
            "root_cause_id": record.root_cause_id,
            "insight_id": record.insight_id,
            "occurrence_id": record.occurrence_id,
            "agent_revision": record.agent_revision,
            "summary": record.summary,
            # Passed as raw dicts so BigQuery loads a JSON array;
            # JSON_QUERY_ARRAY returns null for JSON string scalars.
            "edits": [edit.model_dump(mode="json") for edit in record.edits],
            "created_at": record.created_at.isoformat(),
        }
        # Load jobs are immediately queryable, unlike streaming buffer inserts.
        self._client.load_table_from_json(
            [row],
            self._build_table_ref(ROOT_CAUSES_TABLE),
            job_config=bigquery.LoadJobConfig(
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
                autodetect=False,
            ),
        ).result()
