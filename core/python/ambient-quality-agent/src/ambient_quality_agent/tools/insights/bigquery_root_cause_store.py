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

"""The deployed `RootCauseWriter`: BigQuery, one load job per record."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.tools.insights.models import list_root_cause_columns
from google.cloud import bigquery

if TYPE_CHECKING:
    from ambient_quality_agent.tools.insights.models import RootCause

logger = logging.getLogger(__name__)

ROOT_CAUSES_TABLE = "insight_root_causes"
"""Append-only table name where queries resolve the latest record per occurrence."""


class BigQueryRootCauseStore:
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
        self._columns: frozenset[str] | None = None

    def _build_table_ref(self, table: str) -> str:
        """Returns a fully-qualified ``project.dataset.table`` reference.

        Args:
            table: Target table name.

        Returns:
            Fully-qualified table identifier.
        """
        return f"{self._project_id}.{self._dataset}.{table}"

    def _has_column(self, column: str) -> bool:
        """Checks whether the live `ROOT_CAUSES_TABLE` schema declares `column`.

        Caches the table schema on first write so rolling deployments against a
        dataset that lacks an optional column omit it cleanly while logging a
        warning. Mock clients with an empty schema default to the declared model
        columns so offline tests validate the full row payload.

        Args:
            column: BigQuery column name to check.

        Returns:
            True if the column is present in the destination table schema.
        """
        if self._columns is None:
            try:
                table = self._client.get_table(
                    self._build_table_ref(ROOT_CAUSES_TABLE)
                )
                schema = getattr(table, "schema", None) or ()
                names = frozenset(
                    field.name
                    for field in schema
                    if isinstance(getattr(field, "name", None), str)
                )
                self._columns = (
                    names if names else frozenset(list_root_cause_columns())
                )
            except Exception:
                # Defer missing-table or permission errors to load_table_from_json.
                self._columns = frozenset(list_root_cause_columns())
        return column in self._columns

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
        if self._has_column("context_id"):
            row["context_id"] = record.context_id or None
        elif record.context_id:
            logger.warning(
                "root cause: %s lacks column 'context_id'; saving diagnosis %s "
                "without conversation attribution",
                self._build_table_ref(ROOT_CAUSES_TABLE),
                record.root_cause_id,
            )
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
