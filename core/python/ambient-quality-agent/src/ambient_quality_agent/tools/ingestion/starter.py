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

"""The starter selector: the random sample's selection, written as a selector.

The dashboard opens its selector editor on this query, so a user narrows the
default selection. Each fetcher defines its starter
(`BigQueryJobFetcher.STARTER_SELECTOR`) next to the targets it samples from, so
the two cannot drift apart. This module sits apart from `selector`
because the fetchers import `selector`.
"""

from __future__ import annotations

import textwrap

from ambient_quality_agent.tools.ingestion import selector
from ambient_quality_agent.tools.ingestion.base import BigQueryJobFetcher
from ambient_quality_agent.tools.ingestion.bigquery_fetcher import (
    BigQueryFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_logging_fetcher import (
    CloudLoggingFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_ops_fetcher import (
    CloudOpsFetcher,
)

_FETCHERS: dict[str, type[BigQueryJobFetcher]] = {
    fetcher.TELEMETRY_SOURCE: fetcher
    for fetcher in (BigQueryFetcher, CloudLoggingFetcher, CloudOpsFetcher)
}


def build_starter_selector(
    source: str, *, project_id: str, dataset: str, telemetry_table: str
) -> str:
    """Builds the selector that picks what multi-turn random sampling picks.

    The selector reads the source's selector table by its backtick-quoted
    ``project.dataset.table`` name and passes `selector.find_lexical_violation`
    and `selector.find_table_violation`.

    Args:
        source: Telemetry source key (`big_query`, `cloud_ops` or `cloud_logging`).
        project_id: Project that owns the telemetry dataset.
        dataset: Telemetry dataset.
        telemetry_table: Configured telemetry table.

    Returns:
        The selector SQL, without leading indentation or a trailing newline.

    Raises:
        KeyError: If `source` is not a telemetry source key.
    """
    table_ref = selector.build_selector_table_ref(
        source,
        project_id=project_id,
        dataset=dataset,
        telemetry_table=telemetry_table,
    )
    template = textwrap.dedent(_FETCHERS[source].STARTER_SELECTOR).strip()
    return template.format(table_ref=table_ref)
