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

"""Tests for the cluster verification pass (offline, no GCP).

`verify_clusters` returns verdicts keyed by the cluster's position in the input
and leaves the `Cluster` objects alone, so a cluster it skipped or failed on is
absent from the map rather than marked in place.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from agentplatform._genai.types import EvalCase
from ambient_quality_agent.config import DEFAULT_VERIFICATION_MODEL
from ambient_quality_agent.tools.agent_revision import (
    AgentRevision,
    AgentRevisionCache,
    ToolDefinition,
)
from ambient_quality_agent.tools.insights.clustering import Cluster
from ambient_quality_agent.tools.insights.findings import Finding
from ambient_quality_agent.tools.insights.models import RubricExample
from ambient_quality_agent.tools.insights.verification import (
    _VERIFY_INSTRUCTIONS,
    MAX_EVIDENCE_CHARS,
    MAX_TRACE_CHARS,
    MAX_VERIFIED_CLUSTERS,
    VerifyInfo,
    _clip_turns,
    clean_proposed_label,
    verify_clusters,
)

# --- builders -----------------------------------------------------------------


def _example(
    case_id: str = "case-1", trace: list[dict[str, Any]] | None = None
) -> RubricExample:
    return RubricExample(
        rubric=Finding(
            expected_behavior="call tool X",
            actual_behavior="called tool Y",
            session_id=case_id,
        ),
        eval_case_id=case_id,
        trace=trace
        if trace is not None
        else [{"role": "user", "text": "hello"}],
    )


def _cluster(
    cluster_id: int = 0,
    label: str = "failed to call tool",
    examples: list[RubricExample] | None = None,
    item_count: int = 1,
) -> Cluster:
    return Cluster(
        cluster_id=cluster_id,
        label=label,
        examples=examples if examples is not None else [_example()],
        item_count=item_count,
        finding_ids=[cluster_id],
    )


def _tooled_case(case_id: str, tool: str) -> EvalCase:
    """Builds an evaluation case declaring a single tool on the root agent.

    Args:
        case_id: Evaluation case identifier.
        tool: Name of the tool function declaration.

    Returns:
        Configured EvalCase instance.
    """
    return EvalCase.model_validate(
        {
            "eval_case_id": case_id,
            "agent_data": {
                "agents": {
                    "root": {
                        "agent_id": "root",
                        "instruction": "be helpful",
                        "tools": [{"function_declarations": [{"name": tool}]}],
                    }
                },
                "turns": [],
            },
        }
    )


def _verdict(
    valid: bool = True,
    proposed_label: str = "",
    description: str = "",
) -> str:
    return json.dumps(
        {
            "valid": valid,
            "proposed_label": proposed_label,
            "description": description,
        }
    )


# --- verdicts and tallies -----------------------------------------------------


def test_no_clusters_returns_no_outcomes() -> None:
    outcomes, info = verify_clusters([], model_call=lambda _: "")

    assert outcomes == {}
    assert info == VerifyInfo()


def test_a_verified_cluster_carries_an_explanation_and_a_refined_label() -> (
    None
):
    """A cluster supported by its evidence comes back with a verdict against its index."""
    response = _verdict(
        proposed_label="called tool Y instead of tool X",
        description="Agent consistently calls tool Y instead of tool X on greeting.",
    )

    outcomes, info = verify_clusters(
        [_cluster(label="tool call bug")], model_call=lambda _: response
    )

    assert list(outcomes) == [0]
    verification = outcomes[0].verification
    assert verification.refined_label == "called tool Y instead of tool X"
    assert (
        verification.explanation
        == "Agent consistently calls tool Y instead of tool X on greeting."
    )
    assert outcomes[0].rejected is False
    assert outcomes[0].examined_case_ids == ["case-1"]
    assert info == VerifyInfo(verified=1, rejected=0, skipped=0, failed=0)


def test_an_unsupported_cluster_is_marked_rejected() -> None:
    """A cluster the evidence does not support is returned rejected, not omitted."""
    response = _verdict(valid=False)

    outcomes, info = verify_clusters(
        [_cluster()], model_call=lambda _: response
    )

    assert outcomes[0].rejected is True
    assert outcomes[0].verification.explanation == ""
    assert info == VerifyInfo(verified=0, rejected=1, skipped=0, failed=0)


def test_a_cluster_without_traces_is_skipped_without_a_model_call() -> None:
    """No evidence is not a verdict: the cluster is left unjudged and absent."""
    prompts: list[str] = []

    def recording_call(prompt: str) -> str:
        prompts.append(prompt)
        return _verdict(description="desc")

    outcomes, info = verify_clusters(
        [_cluster(examples=[_example(trace=[])])], model_call=recording_call
    )

    assert prompts == []
    assert outcomes == {}
    assert info == VerifyInfo(verified=0, rejected=0, skipped=1, failed=0)


def test_only_the_largest_clusters_up_to_the_cap_are_verified() -> None:
    """The pass spends its calls on the top `MAX_VERIFIED_CLUSTERS` by item_count."""
    count = MAX_VERIFIED_CLUSTERS + 5
    # Increasing item_count, so the top of the cap is clusters 5..count-1.
    clusters = [_cluster(cluster_id=i, item_count=i + 1) for i in range(count)]
    evaluated: list[str] = []

    def model_call(prompt: str) -> str:
        evaluated.append(prompt)
        return _verdict(description="verified")

    outcomes, info = verify_clusters(clusters, model_call=model_call)

    assert len(evaluated) == MAX_VERIFIED_CLUSTERS
    assert info.verified == MAX_VERIFIED_CLUSTERS
    assert info.skipped == 5
    assert info.rejected == 0
    assert info.failed == 0

    # The smallest five were never judged, so they carry no verdict at all.
    assert sorted(outcomes) == list(range(5, count))
    for idx in range(5, count):
        assert outcomes[idx].verification.explanation == "verified"


def test_the_three_field_response_maps_onto_the_verification() -> None:
    """Each of the model's three fields lands on its own `ClusterVerification` field."""
    response = json.dumps(
        {
            "valid": True,
            "proposed_label": "called execute_command with invalid path",
            "description": "Agent passes relative path to execute_command which requires absolute.",
        }
    )

    outcomes, info = verify_clusters(
        [_cluster(label="generic error")], model_call=lambda _: response
    )

    verification = outcomes[0].verification
    assert verification.valid is True
    assert (
        verification.refined_label == "called execute_command with invalid path"
    )
    assert (
        verification.explanation
        == "Agent passes relative path to execute_command which requires absolute."
    )
    # The verification pass has its own model, tuned apart from the clustering one.
    assert verification.model == DEFAULT_VERIFICATION_MODEL
    assert verification.created_at is not None
    assert info.verified == 1
    assert info.rejected == 0


def test_an_invalid_verdict_rejects_the_cluster() -> None:
    response = _verdict(valid=False)

    outcomes, info = verify_clusters(
        [_cluster(label="hallucinated defect")], model_call=lambda _: response
    )

    assert outcomes[0].rejected is True
    assert info.verified == 0
    assert info.rejected == 1


def test_a_verdict_never_overwrites_the_clusters_label() -> None:
    """A rename rides on the verification; `cluster.label` is what the sweep observed.

    `label` is the whole matching key the same-issue judge compares, so a pass
    that rewrote it in place would re-point the candidate at a different durable
    insight between clustering and persistence.
    """
    examples = [_example("case-2"), _example("case-1")]
    cluster = _cluster(label="failed to call tool", examples=examples)
    response = _verdict(
        proposed_label="called tool Y instead of tool X",
        description="Agent calls tool Y where tool X was required.",
    )

    outcomes, _ = verify_clusters([cluster], model_call=lambda _: response)

    assert (
        outcomes[0].verification.refined_label
        == "called tool Y instead of tool X"
    )
    assert cluster.label == "failed to call tool"
    # Reporting what was read is the pass's job; marking the examples is the caller's.
    assert [e.eval_case_id for e in cluster.examples] == ["case-2", "case-1"]
    assert [e.examined for e in cluster.examples] == [False, False]


def test_a_negative_verdict_still_carries_its_label_and_description() -> None:
    """Ensures negative verification outcomes retain refined labels and explanations from the model."""
    response = _verdict(
        valid=False,
        proposed_label="dropped the ticket id",
        description="The agent omits the ticket id the tool returned.",
    )

    outcomes, info = verify_clusters(
        [_cluster()], model_call=lambda _: response
    )

    assert info.rejected == 1
    verification = outcomes[0].verification
    assert verification.valid is False
    assert verification.refined_label == "dropped the ticket id"
    assert (
        verification.explanation
        == "The agent omits the ticket id the tool returned."
    )


def test_the_prompt_asks_for_a_label_and_a_description_on_every_cluster() -> (
    None
):
    """The label and the description are the insight a reader is shown, so a
    candidate kept under an unenforced verdict needs both. Tying either to
    `valid` empties them on exactly the candidates that are kept, and the
    worked answers teach that contract as forcefully as the instructions.
    """
    prompts: list[str] = []

    def model_call(prompt: str) -> str:
        prompts.append(prompt)
        return _verdict()

    verify_clusters([_cluster()], model_call=model_call)

    assert 'Return "" when `valid` is false' not in prompts[0]
    assert "If the cluster is valid" not in prompts[0]
    assert '"proposed_label": "",\n  "description": ""' not in prompts[0]


def test_the_prompt_never_lets_a_label_argue_with_the_quality_checks() -> None:
    """A label is what an insight is stored and later matched under, so a
    verdict that rebutted the evaluator in the label would rename a real issue
    into a rebuttal of itself and break the recurrence key. The prompt has to
    say so, and its worked answers have to show it.
    """
    prompts: list[str] = []

    def model_call(prompt: str) -> str:
        prompts.append(prompt)
        return _verdict()

    verify_clusters([_cluster()], model_call=model_call)

    # Matched on unwrapped text, so rewrapping the prompt cannot break it.
    unwrapped = " ".join(prompts[0].split())
    assert (
        "naming the observed behavior and not your verdict on it" in unwrapped
    )
    assert "against the failed quality check" not in prompts[0]


# --- failure handling ---------------------------------------------------------


def test_an_unparseable_response_leaves_only_that_cluster_unjudged() -> None:
    """A response that is not JSON costs its own cluster's verdict and no other."""
    clusters = [
        _cluster(cluster_id=0, label="cluster-good"),
        _cluster(1, "bad"),
    ]

    def model_call(prompt: str) -> str:
        if "cluster-good" in prompt:
            return _verdict(description="fine")
        return "not json at all"

    outcomes, info = verify_clusters(clusters, model_call=model_call)

    assert outcomes[0].verification.explanation == "fine"
    assert 1 not in outcomes
    assert info == VerifyInfo(verified=1, rejected=0, skipped=0, failed=1)


def test_one_failed_call_does_not_cost_the_cluster_that_answered() -> None:
    """A per-cluster exception fails open rather than raising out of the pass."""
    clusters = [
        _cluster(cluster_id=0, label="cluster-good"),
        _cluster(1, "boom"),
    ]

    def model_call(prompt: str) -> str:
        if "cluster-good" in prompt:
            return _verdict(description="fine")
        raise RuntimeError("quota exceeded")

    outcomes, info = verify_clusters(clusters, model_call=model_call)

    assert outcomes[0].verification.explanation == "fine"
    assert 1 not in outcomes
    assert info == VerifyInfo(verified=1, rejected=0, skipped=0, failed=1)


def test_every_call_failing_raises_the_first_exception() -> None:
    """Everything failing is an outage, not a sweep that found nothing."""

    def model_call(prompt: str) -> str:
        raise RuntimeError("total outage")

    with pytest.raises(RuntimeError, match="total outage"):
        verify_clusters(
            [_cluster(cluster_id=0, label="c1"), _cluster(1, "c2")],
            model_call=model_call,
        )


def test_a_response_without_a_verdict_is_not_read_as_a_pass() -> None:
    """`valid` is the whole verdict, so a response missing it has judged nothing.
    Defaulting the absent field would silently confirm every candidate the model
    failed to answer for, which is the outcome the pass exists to prevent."""
    with pytest.raises(ValueError, match="has no 'valid'"):
        verify_clusters(
            [_cluster(cluster_id=0, label="c1")],
            model_call=lambda _: json.dumps(
                {"proposed_label": "", "description": "a description"}
            ),
        )


def test_every_response_unparseable_raises_like_any_other_outage() -> None:
    """A model answering nothing but garbage is the same loss as one that throws."""
    with pytest.raises(ValueError, match="not valid JSON"):
        verify_clusters(
            [_cluster(cluster_id=0, label="c1"), _cluster(1, "c2")],
            model_call=lambda _: "not json at all",
        )


# --- evidence -----------------------------------------------------------------


def test_evidence_is_sorted_by_eval_case_id_and_capped() -> None:
    """Evidence samples at most MAX_EVIDENCE_TRACES cases, sorted by eval_case_id."""
    examples = [
        _example("case-d"),
        _example("case-b"),
        _example("case-a"),
        _example("case-c"),
    ]
    captured_prompt = ""

    def model_call(prompt: str) -> str:
        nonlocal captured_prompt
        captured_prompt = prompt
        return _verdict(description="fine")

    verify_clusters([_cluster(examples=examples)], model_call=model_call)

    pos_a = captured_prompt.find("case-a")
    pos_b = captured_prompt.find("case-b")
    pos_c = captured_prompt.find("case-c")
    assert pos_a != -1
    assert pos_b != -1
    assert pos_c != -1
    assert pos_a < pos_b < pos_c
    assert "case-d" not in captured_prompt


def test_examined_case_ids_name_the_traces_the_model_was_shown() -> None:
    """The outcome reports which cases reached the prompt, in the order they did.

    A caller marks exactly these examples `examined`, so a list that disagreed
    with what was rendered would be worse than none.
    """
    examples = [
        _example("case-4"),
        _example("case-1"),
        _example("case-3"),
        _example("case-2"),
    ]

    outcomes, info = verify_clusters(
        [_cluster(examples=examples)],
        model_call=lambda _: _verdict(
            proposed_label="specific defect", description="verified description"
        ),
    )

    assert info.verified == 1
    # The three lowest case ids were sampled; case-4 never reached the model.
    assert outcomes[0].examined_case_ids == ["case-1", "case-2", "case-3"]


def test_evidence_clips_content_and_keeps_the_turn_intact() -> None:
    """An oversized turn loses `content`, not the fields the pass quotes back.

    Slicing the serialized JSON would be shorter, but the pass quotes argument
    values and field names, so a clip that can land mid-object is a clip that
    can invent one.
    """
    giant = "x" * (MAX_TRACE_CHARS + 500)
    trace = [
        {
            "role": "assistant",
            "content": giant,
            "tool_calls": [
                {"name": "submit_expense", "args": {"draft_id": "d1"}}
            ],
        }
    ]

    clipped, truncated = _clip_turns(trace, MAX_TRACE_CHARS)

    assert truncated
    assert len(json.dumps(clipped)) <= MAX_TRACE_CHARS
    assert clipped[0]["content"].endswith("... (content truncated)")
    assert clipped[0]["role"] == "assistant"
    assert clipped[0]["tool_calls"] == [
        {"name": "submit_expense", "args": {"draft_id": "d1"}}
    ]


def test_evidence_block_is_bounded_across_traces() -> None:
    """Three traces cannot each spend the per-trace ceiling."""
    big = [{"role": "user", "content": "x" * MAX_TRACE_CHARS}]
    cluster = _cluster(
        examples=[_example(f"case-{i}", trace=list(big)) for i in range(1, 4)]
    )
    captured_prompt = ""

    def model_call(prompt: str) -> str:
        nonlocal captured_prompt
        captured_prompt = prompt
        return _verdict(description="fine")

    verify_clusters([cluster], model_call=model_call)

    assert len(captured_prompt) < MAX_EVIDENCE_CHARS + 50_000


# --- the agent definition in the prompt ---------------------------------------


def _capturing_call() -> Any:
    """Builds a mock model call that records its prompt to `call.prompt`.

    Returns:
        Callable mock model function returning a verified JSON payload.
    """

    def call(prompt: str) -> str:
        call.prompt = prompt  # type: ignore[attr-defined]
        return _verdict(description="valid")

    call.prompt = ""  # type: ignore[attr-defined]
    return call


def _payload(prompt: str) -> dict[str, Any]:
    """Parses the JSON input block from a verification prompt string.

    Args:
        prompt: Formatted verification prompt.

    Returns:
        Deserialized JSON payload dictionary.
    """
    return json.loads(
        prompt.rsplit("### THE INSIGHT TO DIAGNOSE\n", maxsplit=1)[-1]
    )


def test_prompt_includes_agent_definition() -> None:
    call = _capturing_call()

    verify_clusters(
        [_cluster(label="tool failure")],
        model_call=call,
        agent_definitions={
            "case-1": [
                AgentRevision(agent_id="root", instruction="Always be polite")
            ]
        },
    )

    assert "agent_definition" in call.prompt
    assert "Always be polite" in call.prompt
    assert "issue_label" in call.prompt


def test_one_configuration_behind_every_shown_trace_is_hoisted() -> None:
    """Sessions that ran the same configuration are one specification, read once."""
    revisions = [
        AgentRevision(agent_id="root", revision_id="4", instruction="BE POLITE")
    ]
    call = _capturing_call()

    verify_clusters(
        [_cluster(examples=[_example("case-1"), _example("case-2")])],
        model_call=call,
        agent_definitions={"case-1": revisions, "case-2": revisions},
    )

    payload = _payload(call.prompt)
    assert payload["agent_definition"]["system_prompt"] == "BE POLITE"
    assert all("system_prompt" not in entry for entry in payload["evidence"])


def test_traces_under_different_configurations_keep_their_own_instruction() -> (
    None
):
    """One deployment revision serves sessions with different instructions and
    toolsets, so the shown traces need not agree. Joining two instructions into
    one `agent_definition` block reads as a specification contradicting itself
    and fabricates the very verdict the block exists to make trustworthy, so
    each stays inside the evidence entry it governs.
    """
    call = _capturing_call()

    verify_clusters(
        [_cluster(examples=[_example("case-1"), _example("case-2")])],
        model_call=call,
        agent_definitions={
            "case-1": [
                AgentRevision(
                    agent_id="root",
                    revision_id="4",
                    instruction="SPEC A",
                    tools=[ToolDefinition(name="only_a")],
                )
            ],
            "case-2": [
                AgentRevision(
                    agent_id="root",
                    revision_id="4",
                    instruction="SPEC B",
                    tools=[ToolDefinition(name="only_b")],
                )
            ],
        },
    )

    payload = _payload(call.prompt)
    assert "agent_definition" not in payload
    assert [
        (e["eval_case_id"], e["system_prompt"]) for e in payload["evidence"]
    ] == [
        ("case-1", "SPEC A"),
        ("case-2", "SPEC B"),
    ]
    # The toolset travels with its trace for the same reason the instruction
    # does: a call judged against another session's declarations reads as a
    # hallucinated tool.
    assert [
        [t["name"] for t in e["tool_declarations"]] for e in payload["evidence"]
    ] == [
        ["only_a"],
        ["only_b"],
    ]


def test_two_sessions_on_one_revision_reach_the_prompt_with_their_own_toolsets() -> (
    None
):
    """The regression this whole path exists for, driven from the producer.

    Interning a configuration by ``(agent_id, revision_id)`` would hand the
    second session the first session's toolset, and the prompt would then show
    one hoisted specification naming only `only_a` -- which is how a real call
    to `only_b` reads as a hallucinated one.
    """
    cache = AgentRevisionCache()
    agent_definitions = {
        case_id: cache.build_revisions(
            _tooled_case(case_id, tool), revision_id="4"
        )
        for case_id, tool in (("case-1", "only_a"), ("case-2", "only_b"))
    }
    call = _capturing_call()

    verify_clusters(
        [_cluster(examples=[_example("case-1"), _example("case-2")])],
        model_call=call,
        agent_definitions=agent_definitions,
    )

    payload = _payload(call.prompt)
    assert "agent_definition" not in payload
    assert [
        [t["name"] for t in e["tool_declarations"]] for e in payload["evidence"]
    ] == [
        ["only_a"],
        ["only_b"],
    ]


def test_a_trace_whose_session_recorded_no_configuration_is_shown_none() -> (
    None
):
    """A session the producer recovered nothing for is not handed its
    neighbour's specification: it is judged against its trajectory alone."""
    call = _capturing_call()

    verify_clusters(
        [_cluster(examples=[_example("case-1"), _example("case-2")])],
        model_call=call,
        agent_definitions={
            "case-1": [AgentRevision(agent_id="root", instruction="ONLY SPEC")]
        },
    )

    payload = _payload(call.prompt)
    assert "agent_definition" not in payload
    assert {
        e["eval_case_id"]: e.get("system_prompt") for e in payload["evidence"]
    } == {
        "case-1": "ONLY SPEC",
        "case-2": None,
    }


def test_prompt_names_each_agent_in_a_multi_agent_session() -> None:
    """A session with several agents keeps each instruction behind its `agent_id`.

    Distinct roles stay demarcated in the rendered specification rather than
    collapsing into one anonymous, self-contradictory block.
    """
    call = _capturing_call()

    verify_clusters(
        [_cluster(label="tool failure")],
        model_call=call,
        agent_definitions={
            "case-1": [
                AgentRevision(
                    agent_id="root", revision_id="7", instruction="ROUTE ONLY"
                ),
                AgentRevision(
                    agent_id="billing", revision_id="7", instruction="REFUND"
                ),
            ]
        },
    )

    assert "Agent root:" in call.prompt
    assert "Agent billing:" in call.prompt


def test_a_sessions_own_tools_reach_the_prompt() -> None:
    """The declarations shown are the ones that session ran under."""
    call = _capturing_call()

    verify_clusters(
        [_cluster(label="tool failure")],
        model_call=call,
        agent_definitions={
            "case-1": [
                AgentRevision(
                    agent_id="root",
                    revision_id="",
                    instruction="Instruction for root",
                    tools=[
                        ToolDefinition(
                            name="submit_expense",
                            description="submit an expense report",
                            parameters=["expense_id"],
                        )
                    ],
                )
            ]
        },
    )

    assert "agent_definition" in call.prompt
    assert "Instruction for root" in call.prompt
    assert "submit_expense" in call.prompt


def test_unrecorded_parameters_reach_the_declarations_as_a_marker() -> None:
    """Tools recorded by name only render with the unknown-parameters marker so
    they are not read as taking no arguments."""
    call = _capturing_call()

    verify_clusters(
        [_cluster(label="tool failure")],
        model_call=call,
        agent_definitions={
            "case-1": [
                AgentRevision(
                    agent_id="root",
                    tools=[
                        ToolDefinition(name="transfer_to_agent"),
                        ToolDefinition(name="ping", parameters=[]),
                    ],
                )
            ]
        },
    )

    assert _payload(call.prompt)["agent_definition"]["tool_declarations"] == [
        {
            "name": "transfer_to_agent",
            "description": "",
            "parameters": "<PARAMETERS_UNKNOWN>",
        },
        {"name": "ping", "description": "", "parameters": []},
    ]


def test_a_cluster_whose_sessions_recorded_no_configuration_is_shown_none() -> (
    None
):
    """A sweep that carried no configuration at all still reaches the model: no
    specification beats one those traces never ran under."""
    call = _capturing_call()

    verify_clusters(
        [_cluster(examples=[_example("case-1"), _example("case-2")])],
        model_call=call,
        agent_definitions={},
    )

    payload = _payload(call.prompt)
    assert "agent_definition" not in payload
    assert [e["eval_case_id"] for e in payload["evidence"]] == [
        "case-1",
        "case-2",
    ]
    assert all(
        "tool_declarations" not in entry for entry in payload["evidence"]
    )


def test_the_instructions_describe_every_payload_shape_the_prompt_can_take() -> (
    None
):
    """Clustering can mint a label like "called a tool the agent was not given",
    and a payload carrying no `tool_declarations` lets the model confirm that
    label from the silence. So the static text has to name all three shapes
    `_build_prompt` emits and say what an absent configuration means -- pinned
    here, because a prompt edit that drops it fails nothing else.

    The same goes for the shapes the payload does carry. A prompt that describes
    inputs the code never sends is a contract the model cannot rely on, and the
    worked examples show an abbreviated trajectory, so INPUTS is the one place
    that states the real one.
    """
    inputs = " ".join(
        _VERIFY_INSTRUCTIONS.split("### INPUTS")[1]
        .split("### YOUR TASK")[0]
        .split()
    )

    assert (
        "Present only when every shown trace ran under the same configuration"
        in inputs
    )
    assert (
        "each entry carries its own `system_prompt` and `tool_declarations`"
        in inputs
    )
    assert (
        "A missing `agent_definition`, `system_prompt` or `tool_declarations` means "
        "that configuration was not recorded. It does not mean the agent had no such "
        "instruction or no such tool." in inputs
    )

    # What `_build_evidence` and `_extract_parameter_names` actually produce: turn dumps,
    # bare parameter names, and a case id on every entry.
    assert (
        "A `trajectory` is a list of turns, each `{turn_index, events}`, whose events "
        "are `{author, content}` with the content's `parts` holding `text`, a "
        "`function_call` or a `function_response`." in inputs
    )
    assert "The worked examples below abbreviate it to flat turns" in inputs
    assert (
        "`parameters` is a list of parameter names alone, with no types and no "
        "per-parameter descriptions" in inputs
    )
    assert (
        "each entry naming the conversation it came from in `eval_case_id`"
        in inputs
    )


# --- label cleaning -----------------------------------------------------------


def test_clean_proposed_label_drops_non_proposals() -> None:
    """Noise is stripped, and empty, unchanged, or non-proposals become ''."""
    assert clean_proposed_label("none", "old label") == ""
    assert clean_proposed_label("no change", "old label") == ""
    assert clean_proposed_label("old label", "old label") == ""
    assert clean_proposed_label("OLD LABEL", "old label") == ""
    assert (
        clean_proposed_label("  refined label. ", "old label")
        == "refined label"
    )
