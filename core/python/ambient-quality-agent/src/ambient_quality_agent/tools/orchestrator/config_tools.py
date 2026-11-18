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

"""The orchestrator's tool that reports the configuration in effect.

The configuration itself is resolved in `tools.observed_agent_config.effective_config`.
`show_config` is a self-contained function taking a `LaunchContext`, registered
directly on the agent, and read-only: nothing in a conversation can change the
configuration.
"""

from __future__ import annotations

from typing import Any

from ambient_quality_agent.core.session_state import LaunchContext
from ambient_quality_agent.tools.ingestion import starter
from ambient_quality_agent.tools.observed_agent_config import effective_config
from ambient_quality_shared import usage_headers


def show_config(tool_context: LaunchContext) -> dict[str, Any]:
    """Returns the configuration this deployment is running under.

    Args:
        tool_context: Tool context carrying the session state.

    Returns:
        `config`, every configuration field as a JSON-safe value;
        `starter_selector`, the selector that picks what random sampling picks
        from the agent's telemetry (`starter.build_starter_selector`), which
        the dashboard's selector editor starts from; and `usage_analytics`:
        `enabled`, whether this release sends usage analytics headers at all.
    """
    config = effective_config.load(tool_context.state)
    return {
        "config": effective_config.serialize_config(config),
        "starter_selector": starter.build_starter_selector(
            config.telemetry_ingestion_source,
            project_id=config.resolve_observed_project_id(),
            dataset=config.telemetry_dataset,
            telemetry_table=config.telemetry_table,
        ),
        "usage_analytics": {"enabled": usage_headers.USAGE_ANALYTICS_ENABLED},
    }
