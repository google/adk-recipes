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

"""What AQA checks once, at boot.

The schema tests hold the models to the Terraform declarations; these check
whether a deployment's tables caught up with them, which otherwise surfaces as
a load job failing per write. Kept out of `fast_api_app` so a test can drive a
check without building the ADK app.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ambient_quality_agent.config import Config
    from ambient_quality_agent.tools.insights.bigquery_store import (
        BigQueryInsightStore,
    )

logger = logging.getLogger(__package__)


async def check_investigations_schema() -> None:
    """Name any run-record column AQA writes that the live table lacks.

    Never blocks startup; an unreachable table logs at info, not error.
    """
    try:
        from ambient_quality_agent import config as config_module
        from ambient_quality_agent.tools.investigations.bigquery_store import (
            BigQueryInvestigationStore,
        )

        cfg = config_module.config or config_module.load()
        store = BigQueryInvestigationStore.from_config(cfg)
        missing = await asyncio.to_thread(store.list_missing_columns)
    except Exception:
        logger.exception("could not check the investigations table's schema")
        return
    if missing is None:
        logger.info(
            "investigations table not reachable yet; skipping the schema check"
        )
    elif missing:
        logger.error(
            "investigations table is missing column(s) %s that AQA writes; every "
            "write touching them will fail until the schema is updated "
            "(terraform/modules/aqa/schemas/investigations.json).",
            missing,
        )


async def check_insights_schema() -> None:
    """Name any insight column AQA writes that the live tables lack.

    Per table, so one unreachable table does not hide drift in the other.
    """
    try:
        from ambient_quality_agent import config as config_module

        cfg = config_module.config or config_module.load()
        store = _build_insight_store(cfg)
        missing = await asyncio.to_thread(store.list_missing_columns)
    except Exception:
        logger.exception("could not check the insight tables' schema")
        return
    for table, columns in missing.items():
        if columns is None:
            logger.info(
                "%s table not reachable yet; skipping the schema check", table
            )
        elif columns:
            logger.error(
                "%s table is missing column(s) %s that AQA writes; every write "
                "touching them will fail until the schema is updated "
                "(terraform/modules/aqa/schemas/%s.json).",
                table,
                columns,
                table,
            )


def _build_insight_store(cfg: Config) -> BigQueryInsightStore:
    """Builds a `BigQueryInsightStore` over the configured dataset.

    The store's own factories bind it to a session or to workflow state, and a
    schema read has neither. Its agent scope goes unused: no query is run.

    Args:
        cfg: Configuration specifying dataset coordinates.

    Returns:
        BigQueryInsightStore instance for schema inspection.
    """
    from ambient_quality_agent.tools.insights.bigquery_store import (
        BigQueryInsightStore,
    )
    from google.cloud import bigquery

    return BigQueryInsightStore(
        client=bigquery.Client(
            project=cfg.project_id, location=cfg.aqa_dataset_location
        ),
        project_id=cfg.project_id,
        dataset=cfg.aqa_dataset,
        agent_name=cfg.observed_agent_name,
    )
