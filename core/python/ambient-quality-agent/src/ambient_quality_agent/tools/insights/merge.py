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

"""Merges candidate issues across clustering chunks that represent the same defect.

Because `clustering.cluster_and_label` evaluates findings in separate chunks,
identical or overlapping defect clusters can emerge across chunks. This pass
consolidates them into canonical clusters, one agent at a time: candidates of
different agents are never shown to the model together, so a look-alike defect
in two agents stays two candidates.
"""

from __future__ import annotations

import json
import logging
from concurrent import futures
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.tools.genai_json import call_gemini
from ambient_quality_agent.tools.insights.clustering import Cluster

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

MERGE_CONCURRENCY = 4
"""Maximum number of agents whose candidates are merged concurrently."""


_MERGE_INSTRUCTIONS = """\
You are given labelled candidate issues found in one sweep of a production AI
agent, each with an id. Several may describe the SAME underlying defect in
different words, because they were found in separate batches that could not see
each other.

Group the ids whose labels describe the same defect -- the single underlying
problem a fix would address.

Two labels naming the same target and the same failure belong together even when
the wording differs: "failed to call create_ticket" and "did not invoke
create_ticket"; "ignored the English-only constraint" and "answered in a
language other than English". So do two labels that describe one defect at
different levels of detail, when the more general one plainly covers the other.

Two labels naming different targets -- different tools, different arguments,
different rules -- or different failure modes do NOT: "called create_ticket with
an invalid priority argument" is not "called create_ticket omitting the required
title argument", and neither is "failed to call create_ticket".

Rules:
  - Every id must appear in exactly one group. An id that matches no other is a
    group of one.
  - Never return an id that was not given to you.

Candidate labels:
"""


def merge_clusters(
    clusters: Sequence[Cluster], model_call: Any | None = None
) -> list[Cluster]:
    """Merge candidates that describe one defect into one candidate using LLM.

    Candidates are grouped by `Cluster.agent_id`, and each agent with two or
    more candidates gets its own merge call. Within each merge group, the first
    candidate is kept. Omitted candidates are kept as is, and a failed call
    leaves that agent's candidates unmerged.

    Args:
        clusters: Candidate clusters to consolidate.
        model_call: Optional prompt-to-text callable; `call_merge_model` when
            omitted.

    Returns:
        Consolidated list of clusters, ordered by the input position of each
        kept candidate.
    """
    call = model_call or call_merge_model
    by_agent: dict[str, list[tuple[int, Cluster]]] = {}
    for position, cluster in enumerate(clusters):
        by_agent.setdefault(cluster.agent_id, []).append((position, cluster))
    to_merge = [members for members in by_agent.values() if len(members) >= 2]
    kept: list[tuple[int, Cluster]] = [
        member
        for members in by_agent.values()
        if len(members) < 2
        for member in members
    ]
    if to_merge:
        with futures.ThreadPoolExecutor(max_workers=MERGE_CONCURRENCY) as pool:
            for merged in pool.map(
                lambda members: _merge_agent_candidates(members, call),
                to_merge,
            ):
                kept.extend(merged)
    # Sort by input position so the output order does not depend on thread
    # completion order.
    return [cluster for _, cluster in sorted(kept, key=lambda item: item[0])]


def _merge_agent_candidates(
    members: Sequence[tuple[int, Cluster]], call: Any
) -> list[tuple[int, Cluster]]:
    """Merge one agent's candidates in a single model call.

    Args:
        members: The agent's candidates, each paired with its index in the
            input `clusters` sequence.
        call: Prompt-to-text callable.

    Returns:
        Kept candidates, each paired with the input index of the primary
        candidate in its merge group.
    """
    candidates = [cluster for _, cluster in members]
    agent_id = candidates[0].agent_id
    payload = json.dumps(
        [{"id": c.cluster_id, "label": c.label} for c in candidates],
        ensure_ascii=False,
    )
    try:
        response = call(_MERGE_INSTRUCTIONS + payload)
    except Exception as exc:
        # Fall back to unmerged candidates if the model call fails.
        logger.warning(
            "insights: merge call for agent %r failed; keeping its candidates "
            "unmerged: %s",
            agent_id,
            exc,
        )
        return list(members)

    group_of_cluster = _parse_cluster_groups(response, candidates)
    kept_by_group: dict[int, tuple[int, Cluster]] = {}
    # Walked in the input's order, so which candidate a group keeps -- and the
    # order the groups come back in -- does not depend on the order the model
    # answered in.
    for position, candidate in members:
        # An unclaimed candidate takes a group of its own, keyed negative so it
        # cannot collide with a model group index and fold two findings together.
        group_ord = group_of_cluster.get(candidate.cluster_id, -1 - position)
        kept = kept_by_group.get(group_ord)
        kept_by_group[group_ord] = (
            (position, candidate)
            if kept is None
            else (kept[0], _fold(kept[1], candidate))
        )

    logger.info(
        "insights: merged %d candidate(s) of agent %r into %d.",
        len(candidates),
        agent_id,
        len(kept_by_group),
    )
    return list(kept_by_group.values())


def _fold(kept: Cluster, folded: Cluster) -> Cluster:
    """Merge `folded` into `kept`, combining item counts, finding IDs, and the
    unique trace IDs and deployment revisions the two were seen on.

    Preserves `kept.agent_id` because only candidates from the same agent are
    folded together.

    Args:
        kept: Primary cluster that absorbs the duplicate.
        folded: Candidate cluster being folded into kept.

    Returns:
        Updated Cluster instance combining evidence from both.
    """
    return kept.model_copy(
        update={
            "item_count": kept.item_count + folded.item_count,
            "finding_ids": [*kept.finding_ids, *folded.finding_ids],
            "trace_ids": list(
                dict.fromkeys([*kept.trace_ids, *folded.trace_ids])
            ),
            "agent_revisions": list(
                dict.fromkeys([*kept.agent_revisions, *folded.agent_revisions])
            ),
        }
    )


def _parse_cluster_groups(
    response: str, clusters: Sequence[Cluster]
) -> dict[int, int]:
    """Map each candidate's ``cluster_id`` to the group the model put it in.

    Ids the model invented are ignored and candidates it omitted are absent from
    the result, so a response that omits, repeats, or invents ids can cost the
    answer's quality but never a candidate.

    First claim wins: the prompt asks for a partition, so an id named by two
    groups is the model contradicting itself, and letting the second claim lapse
    keeps that candidate in exactly one group. Joining the contradictory groups
    instead (union-find) would be the thorough reading, but we trust the model
    here and keep this simple.

    Args:
        response: Raw JSON response text from the model.
        clusters: Source candidate clusters used to ground IDs.

    Returns:
        Mapping of cluster_id to group index.
    """
    grounding_candidate_ids = {c.cluster_id for c in clusters}
    try:
        decoded = json.loads(response or "")
    except json.JSONDecodeError as exc:
        logger.error(
            "insights: merge response was not valid JSON (%d chars): %s",
            len(response or ""),
            exc,
        )
        return {}

    raw_groups = (
        decoded.get("groups") if isinstance(decoded, dict) else decoded
    ) or []
    if not isinstance(raw_groups, list):
        logger.error("insights: merge response held no group list.")
        return {}

    group_of_cluster: dict[int, int] = {}
    for group_ord, raw_group in enumerate(raw_groups):
        cluster_ids = (
            raw_group.get("ids") if isinstance(raw_group, dict) else raw_group
        )
        if not isinstance(cluster_ids, list):
            continue
        for cluster_id in cluster_ids:
            if (
                isinstance(cluster_id, int)
                and cluster_id in grounding_candidate_ids
            ):
                group_of_cluster.setdefault(cluster_id, group_ord)
    return group_of_cluster


def _build_merge_response_schema() -> Any:
    """Constrain the merge response to ``{"groups": [{"ids": [int]}]}``.

    Returns:
        Schema object enforcing group partitions.
    """
    from google.genai import types

    return types.Schema(
        type=types.Type.OBJECT,
        properties={
            "groups": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "ids": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.INTEGER),
                        )
                    },
                    required=["ids"],
                ),
            )
        },
        required=["groups"],
    )


def call_merge_model(prompt: str) -> str:
    """Call Gemini to merge candidate labels and return its raw JSON text.

    Args:
        prompt: Formatted merge prompt string.

    Returns:
        Raw JSON response string from the model.
    """
    return call_gemini(prompt, _build_merge_response_schema()).text or ""
