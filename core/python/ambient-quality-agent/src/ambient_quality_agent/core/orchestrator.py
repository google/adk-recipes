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

"""The AQA conversational orchestrator agent.

The orchestrator is a chat `LlmAgent` that sits above the investigation
workflow. It greets the user with its abilities and drives them via tools.
The investigation lifecycle lives under `core.investigation`:
`job_scheduling` (the synchronous user turn) and `job_execution` (the
durable background job), over the shared `model` registry. This module only
assembles the agent, wires its tools, and deterministically routes self-issued
protocol commands (see `core.protocol`) -- an ambient trigger, a `RUN_KEYWORD`
job, or a dashboard/CLI investigation read/write command -- to their handlers
without consulting the LLM.
"""

from __future__ import annotations

import logging

from ambient_quality_agent import config as config_module
from ambient_quality_agent.core.investigation import job_execution
from ambient_quality_agent.core.investigation.job_scheduling import (
    schedule_investigation,
)
from ambient_quality_agent.core.investigation.model import (
    get_investigation,
    get_investigation_stats,
    list_investigations,
)
from ambient_quality_agent.core.models import build_gemini
from ambient_quality_agent.core.protocol import (
    RUN_KEYWORD,
)
from ambient_quality_agent.tools.orchestrator.config_tools import show_config
from ambient_quality_agent.tools.orchestrator.insight_tools import (
    get_insight,
    list_insights,
)
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.llm_agent import LlmAgent, ToolUnion
from google.genai import types as genai_types

logger = logging.getLogger(__name__)


ORCHESTRATOR_TOOLS: list[ToolUnion] = [
    schedule_investigation,
    list_investigations,
    get_investigation,
    get_investigation_stats,
    list_insights,
    get_insight,
    show_config,
]


# --------------------------------------------------------------------------- #
# Deterministic routing                                                        #
#                                                                              #
# `before_agent` matches the incoming message against the known action         #
# prefixes in `core.protocol`. A match is a self-issued command handled here   #
# deterministically (the LLM is never consulted): its handler validates the    #
# arguments and returns a `Content` that ends the turn. When nothing matches,  #
# routing returns None and the LLM handles the turn. Add a new deterministic   #
# action by defining its prefix in `core.protocol` and adding a branch to      #
# `_route_deterministic_action`.                                               #
# --------------------------------------------------------------------------- #


def _extract_incoming_text(callback_context: CallbackContext) -> str:
    """Best-effort plain text of the incoming user message.

    Args:
        callback_context: ADK callback context containing user content.

    Returns:
        Extracted plain text string, or empty string if no content parts.
    """
    content = callback_context.user_content
    if content is None or not content.parts:
        return ""
    return "".join(p.text or "" for p in content.parts).strip()


def _build_model_message(text: str) -> genai_types.Content:
    """Builds a single-part model turn to complete the turn without the LLM.

    Args:
        text: Response text to return.

    Returns:
        genai_types.Content object with role "model".
    """
    return genai_types.Content(
        role="model", parts=[genai_types.Part(text=text)]
    )


def _parse_command_args(text: str, prefix: str) -> list[str]:
    """Parses whitespace-delimited arguments following `prefix` in `text`.

    Args:
        text: Command input text.
        prefix: Command prefix to strip.

    Returns:
        List of argument strings.
    """
    return text[len(prefix) :].split()


async def _run_investigation_action(
    text: str, callback_context: CallbackContext
) -> genai_types.Content:
    """Handles `RUN_KEYWORD <run_id>` by dispatching the durable investigation run.

    Woken by the self-issued long-running query, this executes the investigation
    graph via `job_execution` and completes the turn with a status note.

    Args:
        text: Input message containing the run keyword and run ID.
        callback_context: ADK callback context for the invocation.

    Returns:
        Model Content indicating final status of the investigation.
    """
    args = _parse_command_args(text, RUN_KEYWORD)
    if not args:
        msg = f"{RUN_KEYWORD} received without a run id; ignoring."
        logger.warning(msg)
        return _build_model_message(msg)

    run_id = args[0]
    result = await job_execution.execute_investigation(run_id, callback_context)
    return _build_model_message(
        f"Investigation {run_id} finished with status {result.get('status')}."
    )


async def _route_deterministic_action(
    text: str, callback_context: CallbackContext
) -> genai_types.Content | None:
    """Routes self-issued protocol commands without consulting the LLM.

    Matches prefixes defined in `core.protocol`. Returns None if no prefix
    matches, deferring handling to the LLM.

    Args:
        text: Plain text of the incoming turn.
        callback_context: ADK callback context.

    Returns:
        Model Content if deterministically handled, or None to consult the LLM.
    """
    if text.startswith(RUN_KEYWORD):
        return await _run_investigation_action(text, callback_context)

    return None


async def before_agent(
    callback_context: CallbackContext,
) -> genai_types.Content | None:
    """Deterministically routes self-issued commands before LLM execution.

    Args:
        callback_context: ADK callback context for the turn.

    Returns:
        Model Content if handled deterministically, or None to defer to the LLM.
    """
    return await _route_deterministic_action(
        _extract_incoming_text(callback_context), callback_context
    )


# --------------------------------------------------------------------------- #
# Agent                                                                        #
# --------------------------------------------------------------------------- #


_INSTRUCTION = """\
You are the **Ambient Quality Agent (AQA) orchestrator**. When greeting or asked
what you can do, introduce these abilities:

1. **Run investigations** of an observed agent — ingest telemetry, evaluate it
   against the configured metrics, and correlate the failures into durable,
   deduplicated quality insights.
2. **List and look up investigations** you have launched, with their status and
   summary, via `list_investigations` / `get_investigation` — and report the
   totals across all of them (traces scanned, ingested and evaluated, and the
   rubrics, clusters and insights they produced) via `get_investigation_stats`.
3. **Browse durable quality insights** — the deduplicated, recurring issues past
   sweeps recorded for the agent — via `list_insights` (paginated triage list,
   filterable by status or run) and `get_insight` (one issue's occurrences,
   failing-rubric evidence, and any root cause already recorded against it).
4. **Report the configuration** you are running under — multi/single-turn
   metrics, the data lookback window (days), and the evaluation cap — via
   `show_config`. You cannot change it; it is set when the agent is attached.

Behavior rules:
- To start an investigation, call `schedule_investigation`. It returns
  immediately with a `pending` run that executes as a durable background job;
  tell the user it started (give the `run_id`) and that they can ask for status.
  Do not start a run if the user only asked a question; never launch the same
  run twice; do not wait for it to finish.
- For status/results, call `get_investigation` (or `list_investigations`); a run
  is finished when its status is `done` or `failed`. When a run is `done`, relay
  the workflow events in `summary.events` (the progress/diagnostic messages
  emitted while it ran, including the insight-correlation summary of failed
  rubrics grouped into new/recurring insights) so the user sees what happened.
  Render them as a markdown bullet list — one bullet per entry in
  `summary.events`, each condensed to a single line — never joined onto one line.
  The run's findings live durably in the insight store; point the user at
  `list_insights` / `get_insight` to explore them.
- For insights, call `list_insights` to show the agent's open issues (pass a
  `status` or `run_id` to narrow), and `get_insight` with an `insight_id` for one
  issue's occurrences and failing-rubric evidence. Only when asked.
- When asked why an insight happens or how to fix it, answer from the
  `root_causes` that `get_insight` returns: report the recorded summary and the
  files and line ranges its edits name. You cannot read the observed agent's
  source, so never diagnose one yourself or propose a fix of your own — if there
  is no record, say the insight has not been diagnosed and point the user at the
  dashboard chat, which can.
- Configuration is read-only here. If asked to change one, say it is set with
  `agents-cli aqua attach` and report what is in effect with `show_config`.
- Don't list every investigation or dump the whole config unprompted — call
  `list_investigations` / `show_config` only when the user asks.
- Answer general questions directly without calling a tool.

End your greeting by asking:
"Do you want me to show you the current effective configuration or schedule a
run now?"
"""


def build_orchestrator() -> LlmAgent:
    """Builds the AQA conversational orchestrator agent.

    Returns:
        Configured LlmAgent instance.
    """
    return LlmAgent(
        name="aqa_orchestrator",
        model=build_gemini(config_module.config.base_model),
        mode="chat",
        description="Conversational orchestrator for the Ambient Quality Agent.",
        instruction=_INSTRUCTION,
        tools=ORCHESTRATOR_TOOLS,
        before_agent_callback=before_agent,
    )
