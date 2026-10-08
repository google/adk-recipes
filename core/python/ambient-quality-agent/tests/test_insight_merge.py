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

"""Tests for folding per-chunk candidates into one defect (offline, no GCP)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from ambient_quality_agent.tools.insights import merge
from ambient_quality_agent.tools.insights.clustering import Cluster


def _fake_call(payload: Any) -> Any:
    """Creates a mock model call returning `payload`.

    Args:
        payload: Expected response payload (JSON-encoded if not string).

    Returns:
        Callable mock model function with recorded `prompts` list.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload)

    def call(prompt: str) -> str:
        call.prompts.append(prompt)  # type: ignore[attr-defined]
        return text

    call.prompts = []  # type: ignore[attr-defined]
    return call


def _candidates(*labels: str) -> list[Cluster]:
    return [
        Cluster(cluster_id=i, label=label, finding_ids=[i], item_count=1)
        for i, label in enumerate(labels)
    ]


def test_merge_folds_two_wordings_of_one_defect() -> None:
    """Chunks cannot see each other, so one defect comes back once per chunk.
    The store never compares candidates to each other, so unmerged they would
    mint an insight each."""
    clusters = _candidates(
        "failed to call create_ticket", "did not call create_ticket"
    )
    call = _fake_call({"groups": [{"ids": [0, 1]}]})

    (merged,) = merge.merge_clusters(clusters, model_call=call)

    assert merged.label == "failed to call create_ticket"
    assert merged.item_count == 2
    assert merged.finding_ids == [0, 1]
    # The labels went out keyed by id, and the rubrics did not: the merge costs
    # one small call whatever the sweep's size.
    assert '"id": 0' in call.prompts[0] and '"id": 1' in call.prompts[0]


def test_merge_keeps_distinct_defects_apart() -> None:
    clusters = _candidates(
        "failed to call create_ticket", "produced an empty response"
    )
    call = _fake_call({"groups": [{"ids": [0]}, {"ids": [1]}]})

    merged = merge.merge_clusters(clusters, model_call=call)

    assert [c.label for c in merged] == [
        "failed to call create_ticket",
        "produced an empty response",
    ]
    assert [c.item_count for c in merged] == [1, 1]


def test_merge_keeps_a_candidate_the_model_left_out() -> None:
    """Silence must not lose a finding: an id in no group stands alone."""
    clusters = _candidates("a", "b", "c")
    call = _fake_call({"groups": [{"ids": [0, 1]}]})

    merged = merge.merge_clusters(clusters, model_call=call)

    assert [c.label for c in merged] == ["a", "c"]
    assert [c.item_count for c in merged] == [2, 1]


@pytest.mark.parametrize(
    ("groups", "labels", "counts"),
    [
        pytest.param(
            [[0, 1], [1, 2]], ["a", "c"], [2, 1], id="first_group_keeps_1"
        ),
        pytest.param(
            [[1, 2], [0, 2]], ["a", "b"], [1, 2], id="first_group_keeps_2"
        ),
    ],
)
def test_merge_leaves_a_shared_id_with_the_group_that_claimed_it_first(
    groups: list[list[int]], labels: list[str], counts: list[int]
) -> None:
    """Overlapping groups are the model contradicting the partition it was asked
    for. The second claim lapses, so every candidate lands in exactly one group
    -- honoring both would duplicate one -- whichever order they arrive in."""
    clusters = _candidates("a", "b", "c")
    call = _fake_call({"groups": [{"ids": ids} for ids in groups]})

    merged = merge.merge_clusters(clusters, model_call=call)

    assert [c.label for c in merged] == labels
    assert [c.item_count for c in merged] == counts
    assert sorted(rid for c in merged for rid in c.finding_ids) == [0, 1, 2]


def test_merge_ignores_an_id_it_was_never_given() -> None:
    clusters = _candidates("a", "b")
    call = _fake_call({"groups": [{"ids": [0, 99]}]})

    merged = merge.merge_clusters(clusters, model_call=call)

    assert [c.item_count for c in merged] == [1, 1]


@pytest.mark.parametrize(
    "response",
    [
        pytest.param("not json at all", id="not_json"),
        pytest.param('{"groups": "a string"}', id="groups_not_a_list"),
        pytest.param("", id="empty_response"),
    ],
)
def test_an_unusable_merge_response_keeps_every_candidate(
    response: str,
) -> None:
    """The merge is an improvement on the answer, not part of producing it, so
    losing it costs deduplication and nothing else."""
    clusters = _candidates("a", "b")

    merged = merge.merge_clusters(clusters, model_call=_fake_call(response))

    assert [c.label for c in merged] == ["a", "b"]


def test_a_failed_merge_call_keeps_every_candidate() -> None:
    """A call that never returns must cost deduplication only: raising here
    would lose the whole sweep's findings, which the merge did not produce."""

    def call(prompt: str) -> str:
        raise RuntimeError("model unavailable")

    merged = merge.merge_clusters(_candidates("a", "b"), model_call=call)

    assert [c.label for c in merged] == ["a", "b"]


def test_merge_makes_no_call_for_a_single_candidate() -> None:
    call = _fake_call({"groups": []})

    assert len(merge.merge_clusters(_candidates("a"), model_call=call)) == 1
    assert call.prompts == []


def test_merge_unions_the_traces_of_the_folded_candidates() -> None:
    """One conversation that hit the defect under both wordings is one
    conversation; adding the counts would report it twice."""
    clusters = [
        Cluster(
            cluster_id=0,
            label="failed to call create_ticket",
            finding_ids=[0],
            item_count=1,
            trace_ids=["case-1", "case-2"],
        ),
        Cluster(
            cluster_id=1,
            label="did not call create_ticket",
            finding_ids=[1],
            item_count=1,
            trace_ids=["case-2", "case-3"],
        ),
    ]
    call = _fake_call({"groups": [{"ids": [0, 1]}]})

    (merged,) = merge.merge_clusters(clusters, model_call=call)

    assert merged.trace_ids == ["case-1", "case-2", "case-3"]
    assert merged.trace_count == 3


def test_merge_unions_the_revisions_of_the_folded_candidates() -> None:
    """Two wordings of one defect were seen on both builds; the merged
    candidate has to keep both so the occurrence can date itself to the newer."""
    clusters = [
        Cluster(
            cluster_id=0,
            label="failed to call create_ticket",
            finding_ids=[0],
            item_count=1,
            agent_revisions=["2"],
        ),
        Cluster(
            cluster_id=1,
            label="did not call create_ticket",
            finding_ids=[1],
            item_count=1,
            agent_revisions=["2", "4"],
        ),
    ]
    call = _fake_call({"groups": [{"ids": [0, 1]}]})

    (merged,) = merge.merge_clusters(clusters, model_call=call)

    assert merged.agent_revisions == ["2", "4"]
