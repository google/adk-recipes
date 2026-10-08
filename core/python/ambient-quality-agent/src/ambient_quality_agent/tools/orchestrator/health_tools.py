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

"""Ambient health status formatting for dashboard display.

Aggregates recent run records and insight summaries via existing readers
and evaluates them against health rules without introducing additional storage
or custom queries.
"""

from __future__ import annotations

import logging
from typing import Any

from ambient_quality_agent import config as config_module
from ambient_quality_agent.core.investigation import model
from ambient_quality_agent.core.session_state import LaunchContext
from ambient_quality_agent.tools.investigations import health
from ambient_quality_agent.tools.orchestrator import insight_tools

logger = logging.getLogger(__name__)


async def get_ambient_health(tool_context: LaunchContext) -> dict[str, Any]:
    """Evaluates whether the ambient loop is actively running and explains the verdict.

    Args:
        tool_context: Runtime launch context containing session state and credentials.

    Returns:
        Dictionary containing health status fields (verdict, reason, run details),
        or an error dictionary if deployment state cannot be read.
    """
    try:
        runs = (await model.list_investigations(tool_context)).get("runs") or []
        insights = (await insight_tools.list_insights(tool_context)).get(
            "insights"
        ) or []
    except Exception as exc:
        logger.warning("health: could not read the deployment's state: %s", exc)
        # Prevents masking read failures with an inaccurate "watching" verdict.
        return {"error": f"{type(exc).__name__}: {exc}"}
    # Cloud Scheduler owns the cron definition externally, so health infers
    # cadence from run history while the UI displays reason.
    return health.assess(
        runs, insights, standalone=config_module.load().standalone
    ).to_payload()
