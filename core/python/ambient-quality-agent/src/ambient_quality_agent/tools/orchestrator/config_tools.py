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

"""The orchestrator's tool that reports the configuration and ongoing activity in effect.

The configuration itself is resolved in `tools.observed_agent_config.effective_config`.
`show_config` is a self-contained async function taking a `LaunchContext`,
registered directly on the agent, and read-only: nothing in a conversation can
change the configuration.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ambient_quality_agent.core.investigation import model
from ambient_quality_agent.core.session_state import LaunchContext
from ambient_quality_agent.tools.ingestion import starter
from ambient_quality_agent.tools.investigations import ongoing
from ambient_quality_agent.tools.observed_agent_config import effective_config
from ambient_quality_shared import usage_headers

logger = logging.getLogger(__name__)


async def show_config(tool_context: LaunchContext) -> dict[str, Any]:
    """Returns the configuration, ambient trigger status, and ongoing investigations.

    Args:
        tool_context: Tool context carrying the session state.

    Returns:
        `config`, every configuration field as a JSON-safe value;
        `ambient`, the scheduled and update-trigger settings in effect;
        `ongoing_investigations`, active or scheduled investigations (or None
        paired with `ongoing_investigations_error` if the store or attachment
        cannot be read);
        `starter_selector`, the selector that picks what random sampling picks
        from the agent's telemetry (`starter.build_starter_selector`); and
        `usage_analytics`: `enabled`, whether this release sends usage analytics
        headers at all.
    """

    def load_config_and_ambient() -> tuple[
        effective_config.Config, dict[str, Any]
    ]:
        cfg = effective_config.load(tool_context.state)
        amb = effective_config.resolve_ambient_status(
            tool_context.state,
            standalone=cfg.standalone,
            agent_key=cfg.agent_key or None,
        )
        return cfg, amb

    config, ambient = await asyncio.to_thread(load_config_and_ambient)
    ongoing_list, ongoing_error = await _list_ongoing_investigations(
        tool_context,
        has_agent=bool(config.observed_agent_name),
        ambient_error=ambient.get("error"),
    )
    result: dict[str, Any] = {
        "config": effective_config.serialize_config(config),
        "ambient": ambient,
        "ongoing_investigations": ongoing_list,
        "starter_selector": starter.build_starter_selector(
            config.telemetry_ingestion_source,
            project_id=config.resolve_observed_project_id(),
            dataset=config.telemetry_dataset,
            telemetry_table=config.telemetry_table,
        ),
        "usage_analytics": {"enabled": usage_headers.USAGE_ANALYTICS_ENABLED},
    }
    if ongoing_error is not None:
        result["ongoing_investigations_error"] = ongoing_error
    return result


async def _list_ongoing_investigations(
    tool_context: LaunchContext,
    *,
    has_agent: bool,
    ambient_error: str | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Lists ongoing investigations for the selected agent without raising on store errors.

    Args:
        tool_context: Tool context carrying the session state.
        has_agent: Whether an observed agent is attached and selected.
        ambient_error: Error encountered while reading the agent's attachment,
            if any.

    Returns:
        Tuple of `(ongoing_investigations, error_message)`. When reading the
        attachment or store fails, `ongoing_investigations` is None so callers
        never mistake a broken read for zero active investigations.
    """
    if ambient_error:
        return None, f"could not read agent attachment: {ambient_error}"
    if not has_agent:
        return [], None
    try:
        listed = await model.list_investigations(tool_context)
    except Exception as exc:
        logger.warning(
            "config: could not list investigations for ongoing status: %s", exc
        )
        return None, f"{type(exc).__name__}: {exc}"
    if error := listed.get("error"):
        return None, str(error)
    records = listed.get("investigations") or []
    return [
        ongoing.format_ongoing_investigation(inv)
        for inv in ongoing.list_ongoing_investigations(records)
    ], None
