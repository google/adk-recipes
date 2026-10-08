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

"""Chat tools the dashboard expects, which this deployment cannot serve yet.

Six tools are declared here as placeholders while their backing services (dig
phase and telemetry queries) are developed. Each capability can be enabled independently.

**Declared rather than omitted, on purpose.** An absent tool is invisible: the
model cannot mention a capability it has no name for, so it either answers
"I have no access to the code" — indistinguishable from a permission problem —
or invents an answer from telemetry it does have. A declared tool that returns
a worded refusal makes the model say the true thing: the capability exists in
the product, this deployment has not enabled it, and here is what would.

Same discipline the `/lha/*` routes follow. An empty answer renders as "nothing
here", which is a lie about a surface that has simply not been built yet.

Every signature here matches the real implementation it is waiting for, so
retiring one is an import swap and a line moved between two lists in
`chat_agent.py` — not a change to the wire contract the model was trained on
across a conversation. `tests/test_chat_agent.py` pins which list each tool is
in, so a swap that forgets one half fails.

Retiring a placeholder:

1. Land the real tool.
2. Delete the placeholder here.
3. Move its name from `PENDING_CHAT_TOOLS` to the real import in `chat_agent.py`.
4. Add the instruction paragraph for it — theirs are quoted in each docstring
   below, so the wording does not have to be reinvented.
"""

from __future__ import annotations

from typing import Any

from google.adk.agents.llm_agent import ToolUnion
from google.adk.tools import ToolContext


def _build_pending_response(
    capability: str, arrives_with: str, enables: str
) -> dict[str, Any]:
    """Builds a standardized refusal dictionary for a pending tool.

    `available: False` rather than an empty result allows the model to
    distinguish an unenabled feature from an empty query result.

    Args:
        capability: Name or description of the requested capability.
        arrives_with: Milestone, component, or PR introducing the capability.
        enables: Functionality enabled by the feature.

    Returns:
        Structured dictionary indicating unavailability and rationale.
    """
    return {
        "available": False,
        "capability": capability,
        "reason": (
            f"{capability} is not enabled in this AQuA deployment yet. "
            f"It arrives with {arrives_with}."
        ),
        "arrives_with": arrives_with,
        "would_enable": enables,
    }


# --------------------------------------------------------------------------- #
# The telemetry-query tool (`tools/telemetry/query.py`), beside the real
# `describe_telemetry` in `tools/telemetry/describe.py`.
#
# `query_telemetry` decides safety with a BigQuery dry run rather than a regex
# over the SQL -- the dry run reports the statement type, every table touched
# and the bytes it would scan.
#
# Its half of the shared instruction, to restore verbatim:
#
#   "... and query_telemetry to execute read-only SELECT queries when answering
#   questions about raw trace events, error counts, tool usage rates, or custom
#   telemetry analysis."
# --------------------------------------------------------------------------- #

_QUERY_ARRIVES = "the telemetry-query tool (`tools/telemetry/query.py`)"
_QUERY_ENABLES = (
    "answering questions about raw trace events, error counts and tool usage "
    "rates straight from the observed agent's telemetry"
)


def query_telemetry(sql: str, *, limit: int = 200) -> dict[str, Any]:
    """NOT AVAILABLE YET. Run one read-only SELECT against the telemetry dataset.

    Args:
        sql: a single SELECT statement.
        limit: maximum rows to return.
    """
    del sql, limit
    return _build_pending_response(
        "Querying the observed agent's telemetry",
        _QUERY_ARRIVES,
        _QUERY_ENABLES,
    )


# --------------------------------------------------------------------------- #
# Fleet-health questions (`tools/telemetry/trend.py`, `deployment.py`).
#
# Their instruction, to restore verbatim:
#
#   "Use failure_trend to compare failure rates and trends between the current
#   period and the previous period (e.g. whether the agent is behaving worse or
#   better than last week). Always include both the current and previous period
#   figures when describing trends. If the previous period has no traffic,
#   plainly report that the previous baseline is unknown rather than asserting
#   that the failure rate is flat or zero.
#
#   Use deployment_health to check whether deployment version comparison or
#   post-deploy health analysis is available. If unavailable because telemetry
#   lacks a deployment version column, plainly report that deployment health
#   cannot be evaluated without version tagging and explain that the observed
#   agent must emit a version tag alongside completions."
# --------------------------------------------------------------------------- #

_FLEET_ARRIVES = (
    "the fleet-health tools (`tools/telemetry/trend.py`, `deployment.py`)"
)


def failure_trend(days: int = 7) -> dict[str, Any]:
    """NOT AVAILABLE YET. Compare failure rates against the previous period.

    Args:
        days: length of each window; the comparison is [now-days, now] against
            [now-2*days, now-days].
    """
    del days
    return _build_pending_response(
        "Comparing failure rates between periods",
        _FLEET_ARRIVES,
        "saying whether the observed agent is behaving worse or better than last week",
    )


# TODO(b/557208991): theirs takes `**kwargs: Any` and ignores it. Declared with
# no parameters here because ADK builds the tool schema from the signature and a
# var-keyword parameter has no schema; restore the real signature with the tool.
def deployment_health() -> dict[str, Any]:
    """NOT AVAILABLE YET. Evaluate health across the observed agent's deployments."""
    return _build_pending_response(
        "Post-deploy health and version comparison",
        _FLEET_ARRIVES,
        "spotting a regression introduced by a specific deployment of the observed agent",
    )


# --------------------------------------------------------------------------- #
# The chat-driven investigation flow (the dig phase, D2).
#
# These three are what let the chat run an investigation *in the conversation*
# rather than scheduling one: evaluate, inspect the failures, record what was
# found. `start_investigation` in `chat_agent.py` is the scheduled counterpart
# and is real today.
#
# Note `record_insight` takes `diagnosis` and `confidence`, neither of which
# AQuA records yet -- see the TODO on `_INSTRUCTION` in `chat_agent.py`. Those
# two arrive together.
#
# Their instruction, to restore verbatim:
#
#   "When the user asks to run an investigation, run a full investigation,
#   analyze quality, or check what is failing:
#   Execute the investigation directly in this conversation:
#   1. Call evaluate_telemetry to run quality metrics over telemetry and group
#      failures into clusters.
#   2. For the top failure clusters, call fetch_traces or query_telemetry to
#      inspect failing cases.
#   3. Call search_source and read_source_file to locate the exact defect
#      mechanism in code.
#   4. Call record_insight with your verified diagnosis, source citation
#      `<path>:<start>-<end>`, evidence case IDs, and confidence score.
#   5. Clearly explain the verified findings, code citation, and root cause to
#      the user.
#
#   Only use start_investigation if the user explicitly asks to schedule an
#   unattended background job."
# --------------------------------------------------------------------------- #

_DIG_ARRIVES = "the dig phase (D2)"
_DIG_ENABLES = (
    "running an investigation inside this conversation instead of scheduling "
    "one and waiting for it"
)


def evaluate_telemetry(
    tool_context: ToolContext | None = None,
    *,
    window_start: str | None = None,
    window_end: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """NOT AVAILABLE YET. Score telemetry against the configured quality metrics.

    Args:
        tool_context: supplied by ADK.
        window_start: RFC 3339 start of the window to evaluate.
        window_end: RFC 3339 end of the window to evaluate.
        limit: maximum cases to evaluate.
    """
    del tool_context, window_start, window_end, limit
    return _build_pending_response(
        "Evaluating telemetry in the conversation", _DIG_ARRIVES, _DIG_ENABLES
    )


def fetch_traces(
    *,
    case_ids: list[str],
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict[str, Any]:
    """NOT AVAILABLE YET. Fetch error spans and logs for specific failure cases.

    Args:
        case_ids: the failing case ids to inspect.
        window_start: RFC 3339 start of the window to search.
        window_end: RFC 3339 end of the window to search.
    """
    del case_ids, window_start, window_end
    return _build_pending_response(
        "Fetching traces for a failing case", _DIG_ARRIVES, _DIG_ENABLES
    )


def record_insight(
    tool_context: ToolContext | None = None,
    *,
    label: str,
    diagnosis: str,
    source_refs: list[str] | None = None,
    confidence: float = 0.5,
    evidence_case_ids: list[str] | None = None,
    tool_name: str = "",
    signature: str = "",
) -> dict[str, Any]:
    """NOT AVAILABLE YET. Record a verified finding with its diagnosis and evidence.

    Args:
        tool_context: supplied by ADK.
        label: short name for the failure mode.
        diagnosis: what actually causes it.
        source_refs: code citations as `<path>:<start>-<end>`.
        confidence: certainty in the diagnosis, 0 to 1.
        evidence_case_ids: the cases this conclusion rests on.
        tool_name: the observed agent's tool involved, if any.
        signature: stable signature used to match recurrences.
    """
    del tool_context, label, diagnosis, source_refs, confidence
    del evidence_case_ids, tool_name, signature
    return _build_pending_response(
        "Recording a diagnosed insight", _DIG_ARRIVES, _DIG_ENABLES
    )


# Declared to the model, refused at call time. Pinned by
# `tests/test_chat_agent.py`; a name leaves this list only when the real tool
# takes its place.
PENDING_CHAT_TOOLS: list[ToolUnion] = [
    query_telemetry,
    failure_trend,
    deployment_health,
    evaluate_telemetry,
    fetch_traces,
    record_insight,
]
