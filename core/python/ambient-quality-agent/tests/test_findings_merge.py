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

"""Unit tests for `findings.merge_finding_sets` across multiple producer finding sets."""

from __future__ import annotations

from ambient_quality_agent.tools.agent_revision import AgentRevision
from ambient_quality_agent.tools.insights.findings import (
    Finding,
    FindingSet,
    merge_finding_sets,
)


def _finding(text: str, session_id: str = "") -> Finding:
    return Finding(
        expected_behavior=f"should {text}",
        actual_behavior=f"did not {text}",
        session_id=session_id,
    )


def test_findings_keep_the_order_they_were_merged_in() -> None:
    """Verify that findings maintain input order to ensure stable index-based IDs."""
    first = FindingSet(findings=[_finding("a"), _finding("b")])
    second = FindingSet(findings=[_finding("c")])

    merged = merge_finding_sets([first, second])

    assert [f.expected_behavior for f in merged.findings] == [
        "should a",
        "should b",
        "should c",
    ]


def test_the_first_set_holding_a_session_supplies_its_turns() -> None:
    """Verify that duplicate sessions retain the first encountered turn history."""
    first = FindingSet(sessions={"s-1": [{"turn": 1}], "s-2": [{"turn": 2}]})
    second = FindingSet(
        sessions={"s-1": [{"turn": "other"}], "s-3": [{"turn": 3}]}
    )

    merged = merge_finding_sets([first, second])

    assert merged.sessions == {
        "s-1": [{"turn": 1}],
        "s-2": [{"turn": 2}],
        "s-3": [{"turn": 3}],
    }


def test_two_sessions_on_one_build_keep_their_own_configurations() -> None:
    """Sharing an ``(agent_id, revision_id)`` pair across two producers' sets
    must not make two sessions share one configuration: each crosses the merge
    with the instruction it ran under."""
    first = FindingSet(
        agent_revisions={
            "sess-1": [
                AgentRevision(
                    agent_id="root", revision_id="4", instruction="first"
                )
            ]
        }
    )
    second = FindingSet(
        agent_revisions={
            "sess-2": [
                AgentRevision(
                    agent_id="root", revision_id="4", instruction="differs"
                )
            ]
        }
    )

    merged = merge_finding_sets([first, second])

    assert {
        session_id: [revision.instruction for revision in revisions]
        for session_id, revisions in merged.agent_revisions.items()
    } == {"sess-1": ["first"], "sess-2": ["differs"]}


def test_a_sessions_configuration_crosses_the_merge_once() -> None:
    """Producers sweeping the same session each recover its configuration; a
    system instruction runs to tens of kilobytes, so carrying both copies into
    the persisted state is what this prevents."""
    shared = AgentRevision(
        agent_id="root", revision_id="4", instruction="be helpful"
    )
    first = FindingSet(agent_revisions={"sess-1": [shared]})
    second = FindingSet(
        agent_revisions={
            "sess-1": [shared],
            "sess-2": [
                AgentRevision(
                    agent_id="sub", revision_id="4", instruction="answer"
                )
            ],
        }
    )

    merged = merge_finding_sets([first, second])

    assert {
        session_id: [r.agent_id for r in revisions]
        for session_id, revisions in merged.agent_revisions.items()
    } == {"sess-1": ["root"], "sess-2": ["sub"]}


def test_sets_that_travelled_through_state_merge_as_dicts() -> None:
    """Verify support for serialized dictionary inputs from workflow state."""
    dumped = FindingSet(
        findings=[_finding("a", session_id="s-1")],
        sessions={"s-1": [{"turn": 1}]},
        agent_revisions={
            "s-1": [AgentRevision(agent_id="root", instruction="be helpful")]
        },
    ).model_dump()

    merged = merge_finding_sets([dumped, dumped])

    assert [f.session_id for f in merged.findings] == ["s-1", "s-1"]
    assert merged.sessions == {"s-1": [{"turn": 1}]}
    assert [r.instruction for r in merged.agent_revisions["s-1"]] == [
        "be helpful"
    ]


def test_no_sets_merge_to_an_empty_set() -> None:
    """Verify that merging an empty sequence returns an empty FindingSet."""
    assert merge_finding_sets([]) == FindingSet()
