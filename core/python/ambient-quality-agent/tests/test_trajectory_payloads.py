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

"""`build_archive`: one assembled case becomes a manifest and its turns.

The store is dumb about content, so this is where the judgement lives -- what
goes in which table, what a copy's status is, and the one thing that is
deliberately left out. Worth holding: the round trip, since an archive nobody
can read back is worse than no archive; and the write cap, since it is the only
place the writer discards anything.
"""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any

import pytest
from agentplatform._genai.types import EvalCase
from agentplatform._genai.types.evals import (
    AgentConfig,
    AgentData,
    AgentEvent,
    ConversationTurn,
)
from ambient_quality_agent.tools.trajectories import payloads
from ambient_quality_agent.tools.trajectories.models import PayloadStatus
from ambient_quality_agent.tools.trajectories.payloads import build_archive
from google.genai import types as genai_types

_AGENT = "root_agent"
_TRAJECTORY = "session-abc"
_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


def _turn(index: int, text: str = "hello") -> ConversationTurn:
    return ConversationTurn(
        turn_index=index,
        turn_id=f"turn-{index}",
        events=[
            AgentEvent(
                author="user",
                content=genai_types.Content(
                    role="user", parts=[genai_types.Part(text=text)]
                ),
            )
        ],
    )


def _case(
    *, turns: list[ConversationTurn] | None = None, agents: Any = None
) -> EvalCase:
    return EvalCase(
        eval_case_id=_TRAJECTORY,
        agent_data=AgentData(
            agents=agents
            if agents is not None
            else {
                _AGENT: AgentConfig(agent_id=_AGENT, instruction="be helpful")
            },
            turns=turns if turns is not None else [_turn(0), _turn(1)],
        ),
    )


def _archive(
    case: EvalCase, *, partial: bool = False
) -> payloads.TrajectoryArchive:
    return build_archive(
        case,
        agent_name=_AGENT,
        trajectory_id=_TRAJECTORY,
        created_at=_NOW,
        partial=partial,
    )


def test_a_conversation_becomes_a_manifest_and_a_row_per_turn() -> None:
    archive = _archive(_case())

    assert archive.payload.agent_name == _AGENT
    assert archive.payload.trajectory_id == _TRAJECTORY
    assert archive.payload.status is PayloadStatus.INGESTED
    assert [turn.turn_index for turn in archive.turns] == [0, 1]
    assert all(turn.trajectory_id == _TRAJECTORY for turn in archive.turns)


def test_the_manifest_counts_the_turns_beside_it() -> None:
    """`turn_count` is a promise about rows a reader will find, which is what
    lets it tell a short conversation from a partly-written one. Made here so
    it cannot be made by one caller and broken by another."""
    archive = _archive(_case(turns=[_turn(0), _turn(1), _turn(2)]))

    assert archive.payload.turn_count == len(archive.turns) == 3


def test_the_agent_configs_go_on_the_manifest_and_not_on_a_turn() -> None:
    """They belong to the conversation. Repeated per turn there would be no
    rule for which copy wins, which is the whole reason for two tables."""
    archive = _archive(_case())

    assert archive.payload.agents is not None
    assert _AGENT in archive.payload.agents
    assert all("agents" not in turn.turn for turn in archive.turns)


def test_a_turn_is_indexed_by_its_position_not_by_what_the_sdk_says() -> None:
    """`ConversationTurn.turn_index` is optional in the SDK and the column is
    not. A conversation whose turns forgot to number themselves still has to
    key and order, and its position is the answer that always exists."""
    unnumbered = [
        ConversationTurn(turn_id="a", events=[]),
        ConversationTurn(turn_id="b", events=[]),
    ]

    archive = _archive(_case(turns=unnumbered))

    assert [turn.turn_index for turn in archive.turns] == [0, 1]


def test_the_stored_turn_reads_back_as_the_turn_that_was_stored() -> None:
    """An archive nobody can reassemble is worse than no archive. The dumped
    form drops unset fields, which is most of a sparsely-populated SDK model;
    validating it back has to reconstruct the same value."""
    original = _turn(0, text="what is the weather?")

    archive = _archive(_case(turns=[original]))
    restored = ConversationTurn.model_validate(archive.turns[0].turn)

    assert restored == original


def test_the_stored_agent_configs_read_back_as_themselves() -> None:
    configs = {_AGENT: AgentConfig(agent_id=_AGENT, instruction="be helpful")}

    archive = _archive(_case(agents=configs))

    assert archive.payload.agents is not None
    restored = {
        name: AgentConfig.model_validate(value)
        for name, value in archive.payload.agents.items()
    }
    assert restored == configs


def test_the_stored_json_is_a_mapping_rather_than_text() -> None:
    """The column takes the object. Text would land as a JSON string scalar and
    `turn.events` would read nothing -- silently, which is the failure mode
    `BigQueryInvestigationStore` carries `_UNWRAPPED_COUNTERS` to work around."""
    archive = _archive(_case())

    assert isinstance(archive.payload.agents, dict)
    assert all(isinstance(turn.turn, dict) for turn in archive.turns)


def test_a_conversation_and_its_turns_share_one_timestamp() -> None:
    """The partition column on both tables. Two instants could straddle
    midnight, and the manifest would then expire a day before its turns."""
    archive = _archive(_case())

    assert archive.payload.created_at == _NOW
    assert all(turn.created_at == _NOW for turn in archive.turns)


def test_lossy_telemetry_is_recorded_as_partial() -> None:
    """Which is what makes a later, whole copy an upgrade rather than a
    duplicate: the store only replaces a copy that is not `ingested`."""
    archive = _archive(_case(), partial=True)

    assert archive.payload.status is PayloadStatus.PARTIAL


def test_a_case_with_no_turns_still_gets_a_manifest() -> None:
    """It was sampled and it assembled; that the conversation is empty is a
    fact about the telemetry, not a reason to store nothing. `turn_count` says
    so plainly."""
    archive = _archive(_case(turns=[]))

    assert archive.turns == ()
    assert archive.payload.turn_count == 0
    assert archive.payload.status is PayloadStatus.INGESTED


def test_a_case_with_no_agent_data_at_all_is_still_archived() -> None:
    archive = _archive(EvalCase(eval_case_id=_TRAJECTORY))

    assert archive.payload.agents is None
    assert archive.payload.turn_count == 0


def test_an_oversized_turn_is_left_out_and_the_copy_says_so() -> None:
    """Dropped rather than trimmed: a trimmed turn reads as a whole one and is
    not, while a missing `turn_index` is a hole a reader can see."""
    huge = _turn(1, text="x" * (payloads.MAX_JSON_BYTES + 1))

    archive = _archive(_case(turns=[_turn(0), huge, _turn(2)]))

    assert [turn.turn_index for turn in archive.turns] == [0, 2]
    assert archive.payload.turn_count == 2
    assert archive.payload.status is PayloadStatus.TRUNCATED


def test_oversized_agent_configs_are_left_out_and_the_copy_says_so() -> None:
    """Same cap on the manifest's own JSON: a system instruction is not
    normally large, but nothing stops one being."""
    configs = {
        _AGENT: AgentConfig(
            agent_id=_AGENT, instruction="x" * (payloads.MAX_JSON_BYTES + 1)
        )
    }

    archive = _archive(_case(agents=configs))

    assert archive.payload.agents is None
    assert archive.payload.status is PayloadStatus.TRUNCATED
    assert archive.payload.turn_count == 2


def test_lossy_telemetry_outranks_our_own_cut() -> None:
    """`partial` is the state a later sweep can repair -- the source may supply
    the conversation whole next time. A copy we cut will be cut again by the
    same cap, so calling a lossy one `truncated` would take it out of the
    upgrade path it belongs in."""
    huge = _turn(0, text="x" * (payloads.MAX_JSON_BYTES + 1))

    archive = _archive(_case(turns=[huge]), partial=True)

    assert archive.payload.status is PayloadStatus.PARTIAL


def test_the_cap_leaves_an_ordinary_conversation_alone() -> None:
    """It is a guard against BigQuery's per-row load limit, not a content
    policy: a whole trajectory is ~50 KB and the cap is orders of magnitude
    above that, so it must never fire on one."""
    archive = _archive(_case(turns=[_turn(index) for index in range(20)]))

    assert archive.payload.status is PayloadStatus.INGESTED
    assert archive.payload.turn_count == 20


def test_the_case_is_not_modified_on_the_way_into_the_archive() -> None:
    """The same case is handed to the evaluator afterwards, so archiving it
    must be a read."""
    case = _case()
    before = copy.deepcopy(case)

    _archive(case)

    assert case == before


@pytest.mark.parametrize(
    ("partial", "expected"),
    [(False, PayloadStatus.INGESTED), (True, PayloadStatus.PARTIAL)],
)
def test_ingestions_verdict_reaches_the_stored_copy(
    partial: bool, expected: PayloadStatus
) -> None:
    assert _archive(_case(), partial=partial).payload.status is expected


def test_the_deployment_that_served_the_conversation_is_archived_with_it() -> (
    None
):
    """The copy has to say which build produced what it holds: an archived
    conversation outlives its telemetry, and what an agent did is only
    judgeable against the build that did it."""
    archive = build_archive(
        _case(),
        agent_name=_AGENT,
        trajectory_id=_TRAJECTORY,
        created_at=_NOW,
        partial=False,
        agent_revision="17",
    )

    assert archive.payload.agent_revision == "17"


def test_an_unversioned_source_archives_no_revision() -> None:
    """Empty means "this source has one unnamed revision", and it is stored as
    NULL rather than as an empty string a reader would have to special-case."""
    assert _archive(_case()).payload.agent_revision is None
