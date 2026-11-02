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

"""Tests for recovering the observed agent from a sweep (offline).

`AgentRevisionCache` recovers the per-agent (instruction, toolset) pair, kept
distinct per session so each is judged against the configuration it ran under.
"""

from __future__ import annotations

from typing import Any

from agentplatform._genai.types import EvalCase
from ambient_quality_agent.tools import agent_revision


def _tool(
    name: str, parameters: list[str], description: str = "does a thing"
) -> dict:
    """Builds a function declaration shaped as emitted by `TraceAgentDataConverter`.

    Args:
        name: Name of the tool.
        parameters: Parameter names for the tool schema.
        description: Tool description.

    Returns:
        A function declaration dictionary.
    """
    return {
        "function_declarations": [
            {
                "name": name,
                "description": description,
                "parameters_json_schema": {
                    "type": "object",
                    "properties": {p: {"type": "string"} for p in parameters},
                },
            }
        ]
    }


def _agent(instruction: str = "", tools: list[dict] | None = None) -> dict:
    """Builds an `AgentConfig` dictionary payload.

    Args:
        instruction: System instruction for the agent.
        tools: Optional tool definitions.

    Returns:
        An agent configuration dictionary.
    """
    config: dict[str, Any] = {"instruction": instruction}
    if tools is not None:
        config["tools"] = tools
    return config


def _case(agents: dict[str, dict], case_id: str = "case-agents") -> EvalCase:
    """Builds an eval case declaring an `AgentConfig` for each specified agent.

    Args:
        agents: Mapping of agent names to their configuration dictionaries.
        case_id: Evaluation case identifier.

    Returns:
        A validated EvalCase instance.
    """
    return EvalCase.model_validate(
        {"eval_case_id": case_id, "agent_data": {"agents": agents}}
    )


def test_revisions_capture_each_agent_instruction_verbatim() -> None:
    """System instructions are preserved verbatim without stripping or normalization."""
    case = _case(
        {
            "root": _agent(
                instruction="  You are the router.\n", tools=[_tool("x", ["a"])]
            )
        }
    )

    (revision,) = agent_revision.AgentRevisionCache().build_revisions(case)

    assert revision.agent_id == "root"
    assert revision.revision_id == ""
    assert revision.instruction == "  You are the router.\n"


def test_revisions_include_every_agent_in_the_case() -> None:
    """Multi-agent cases yield separate revision records for root and subagents."""
    case = _case(
        {
            "root": _agent(
                instruction="route", tools=[_tool("transfer", ["agent_name"])]
            ),
            "sub": _agent(
                instruction="answer", tools=[_tool("file_expense", ["amount"])]
            ),
        }
    )

    revisions = agent_revision.AgentRevisionCache().build_revisions(case)

    assert [r.agent_id for r in revisions] == ["root", "sub"]
    assert [r.instruction for r in revisions] == ["route", "answer"]


def test_revisions_keep_each_agents_own_tools_for_a_shared_name() -> None:
    """Agents sharing a tool name maintain their distinct parameter declarations."""
    case = _case(
        {
            "hr": _agent(tools=[_tool("get_profile", ["employee_id"])]),
            "travel": _agent(
                tools=[_tool("get_profile", ["passenger_id", "trip_id"])]
            ),
        }
    )

    revisions = agent_revision.AgentRevisionCache().build_revisions(case)
    params = {
        r.agent_id: [t.parameters for t in r.tools if t.name == "get_profile"]
        for r in revisions
    }

    assert params["hr"] == [["employee_id"]]
    assert params["travel"] == [["passenger_id", "trip_id"]]


def test_revisions_are_sorted_by_agent_id() -> None:
    """Revisions are sorted by agent_id for deterministic ordering."""
    case = _case({name: _agent() for name in ("zeta", "alpha", "mu")})

    revisions = agent_revision.AgentRevisionCache().build_revisions(case)

    assert [r.agent_id for r in revisions] == ["alpha", "mu", "zeta"]


def test_cache_derives_an_agent_once_per_configuration() -> None:
    """Cases repeating one configuration share a single interned instance, which
    is what keeps a sweep's repeated sessions cheap."""
    cache = agent_revision.AgentRevisionCache()
    config = _agent(
        instruction="route", tools=[_tool("file_expense", ["amount"])]
    )
    first_case = _case({"agent": config}, case_id="c1")
    second_case = _case({"agent": config}, case_id="c2")

    (first,) = cache.build_revisions(first_case)
    (again,) = cache.build_revisions(second_case)

    assert again is first


def test_cache_separates_the_same_agent_with_a_different_toolset() -> None:
    """Sessions may each receive their own filtered toolset while all reporting
    the same empty revision_id, so the toolset itself has to separate them --
    otherwise the reviewer judges a session against another session's tools."""
    cache = agent_revision.AgentRevisionCache()
    itsm_case = _case(
        {"agent": _agent(tools=[_tool("create_incident", ["summary"])])}
    )
    csm_case = _case(
        {"agent": _agent(tools=[_tool("create_case", ["account_id"])])}
    )

    (itsm,) = cache.build_revisions(itsm_case)
    (csm,) = cache.build_revisions(csm_case)

    assert itsm is not csm
    assert [t.name for t in itsm.tools] == ["create_incident"]
    assert [t.name for t in csm.tools] == ["create_case"]


def test_cache_separates_the_same_agent_with_a_different_instruction() -> None:
    """Dynamic instructions for the same agent remain separate in the cache so
    sessions are evaluated against their own prompts."""
    cache = agent_revision.AgentRevisionCache()
    first_case = _case({"agent": _agent(instruction="first")}, case_id="c1")
    second_case = _case({"agent": _agent(instruction="differs")}, case_id="c2")

    (first,) = cache.build_revisions(first_case)
    (second,) = cache.build_revisions(second_case)

    assert first is not second
    assert (first.instruction, second.instruction) == ("first", "differs")


def test_cache_separates_the_same_agent_at_a_different_revision() -> None:
    """Distinct deployment revisions remain separate in the cache to attribute
    findings to the correct version."""
    cache = agent_revision.AgentRevisionCache()
    case_v1 = _case({"agent": _agent(instruction="same")})
    case_v2 = _case({"agent": _agent(instruction="same")})

    (v1,) = cache.build_revisions(case_v1, revision_id="rev-1")
    (v2,) = cache.build_revisions(case_v2, revision_id="rev-2")

    assert v1 is not v2
    assert (v1.revision_id, v2.revision_id) == ("rev-1", "rev-2")
    assert v1.instruction == v2.instruction == "same"


def test_revisions_tolerate_a_case_without_agent_data() -> None:
    """Cases lacking agent telemetry return an empty list without error."""
    empty = agent_revision.AgentRevisionCache().build_revisions(
        EvalCase(eval_case_id="c")
    )

    assert empty == []


def test_revisions_yield_empty_tools_for_a_toolless_agent() -> None:
    """Agents without declared tools retain their instruction with an empty tool list."""
    case = _case({"agent": _agent(instruction="do the thing")})

    (revision,) = agent_revision.AgentRevisionCache().build_revisions(case)

    assert revision.instruction == "do the thing"
    assert revision.tools == []


def test_revisions_leave_parameters_unknown_for_a_tool_recorded_by_name() -> (
    None
):
    """A declaration without a schema records no argument names, which is not
    the same as a tool that takes none."""
    case = _case(
        {
            "agent": _agent(
                tools=[{"function_declarations": [{"name": "transfer"}]}]
            )
        }
    )

    (revision,) = agent_revision.AgentRevisionCache().build_revisions(case)

    assert revision.tools[0].name == "transfer"
    assert revision.tools[0].parameters is None


def test_revisions_give_no_parameters_for_a_schema_without_properties() -> None:
    """A schema that declares no properties is a tool known to take none."""
    case = _case(
        {
            "agent": _agent(
                tools=[
                    {
                        "function_declarations": [
                            {
                                "name": "ping",
                                "parameters_json_schema": {"type": "object"},
                            }
                        ]
                    }
                ]
            )
        }
    )

    (revision,) = agent_revision.AgentRevisionCache().build_revisions(case)

    assert revision.tools[0].parameters == []


# --- resolve_latest_revision ----------------------------------------------------------


def test_latest_revision_passes_a_single_revision_through() -> None:
    assert agent_revision.resolve_latest_revision(["4", "4"]) == "4"


def test_latest_revision_takes_the_newest_build_numerically() -> None:
    """String order would call "9" the newest of a two-digit deployment."""
    assert agent_revision.resolve_latest_revision(["9", "10", "2"]) == "10"


def test_latest_revision_falls_back_to_string_order() -> None:
    """A deployment scheme that is not integer-like still yields an answer."""
    assert agent_revision.resolve_latest_revision(["v1", "v3", "v2"]) == "v3"


def test_latest_revision_ignores_the_unnamed_revision() -> None:
    """An empty id denotes the single unnamed revision and never outranks a
    named one."""
    assert agent_revision.resolve_latest_revision(["", "2", ""]) == "2"


def test_latest_revision_of_unnamed_revisions_is_unnamed() -> None:
    assert agent_revision.resolve_latest_revision(["", ""]) == ""
    assert agent_revision.resolve_latest_revision([]) == ""


def test_latest_revision_of_a_mixed_scheme_uses_string_order() -> None:
    """One unparseable id puts the whole comparison on string order, which is
    the documented fallback rather than a partial numeric answer."""
    assert agent_revision.resolve_latest_revision(["9", "10", "v2"]) == "v2"
