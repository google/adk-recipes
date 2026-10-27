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

"""The ``init`` workflow node: emits a starting summary from seeded state.

Config merging and window/budget derivation happen once at the orchestrator
boundary (`derive_window_and_budget`, seeded into state before the graph runs).
This node renders a progress message from resolved inputs and sets
`quality_analysis_mode` as the event route to select the workflow producer branch.

It also erases the trajectory rows a previous attempt of this run left behind.
That has to happen here rather than in the node that writes them, because both
producers write and this node is the one that runs first and once.
"""

from __future__ import annotations

import logging

from ambient_quality_agent.config import DEFAULT_QUALITY_ANALYSIS_MODE
from ambient_quality_agent.core._logging import log_node_run
from ambient_quality_agent.core.nodes import _common
from ambient_quality_agent.core.session_state import ADKStateLike
from ambient_quality_agent.core.state import WorkflowState
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import node

logger = logging.getLogger(__name__)


@node(name="init")
@log_node_run("init")
def init_node(ctx: Context) -> Event:
    """Emit a starting progress event routed to the selected analysis producer.

    Args:
        ctx: Workflow execution context.

    Returns:
        ADK Event containing the initial progress message and producer routing action.
    """
    state = ctx.state
    mode = state.get("quality_analysis_mode", DEFAULT_QUALITY_ANALYSIS_MODE)
    _delete_run_trajectories(ctx)

    text = (
        f"## Ambient Quality Agent\n\n"
        f"Investigating **`{state.get('observed_agent_name', '<unknown>')}`**.\n\n"
        f"- Window: `{state.get('window_start')}` → `{state.get('window_end')}`\n"
        f"- Quality analysis mode: `{mode}`\n"
        f"- Budget per metric: `{state.get('budget_per_metric', 0)}`\n"
    ) + _render_metric_lines(state, mode)
    event = WorkflowState.record_progress_event(state, text, "init")
    # Routing key used by workflow edges to select the producer node branch.
    event.actions.route = mode
    return event


def _render_metric_lines(state: ADKStateLike, mode: str) -> str:
    """Render configured metrics when running in eval_service mode.

    Args:
        state: Workflow execution state.
        mode: Active quality analysis mode.

    Returns:
        Markdown-formatted string listing metrics, or empty string if not applicable.
    """
    if mode != "eval_service":
        return ""
    multi = list(state.get("multi_turn_metrics") or [])
    single = list(state.get("single_turn_metrics") or [])
    return (
        f"- Multi-turn metrics: {', '.join(f'`{m}`' for m in multi) or '_none_'}\n"
        f"- Single-turn metrics: {', '.join(f'`{m}`' for m in single) or '_none_'}\n"
    )


def _delete_run_trajectories(ctx: Context) -> None:
    """Erase any trajectory rows a prior attempt of this run wrote.

    Enforces idempotent retries by clearing partial trajectory records.
    Catches exceptions so BigQuery errors during cleanup do not abort initialization.

    Args:
        ctx: Workflow execution context.
    """
    try:
        _common.trajectory_recorder_factory(ctx).delete_run_trajectories()
    except Exception as exc:
        logger.warning(
            "init: could not erase run %s's earlier trajectory rows: %s",
            ctx.state.get("run_id", ""),
            exc,
        )
