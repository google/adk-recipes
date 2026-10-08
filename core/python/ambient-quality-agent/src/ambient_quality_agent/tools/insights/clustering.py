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

"""Group a sweep's findings into labeled candidate issues."""

from __future__ import annotations

import json
import logging
from concurrent import futures
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.tools import genai_json
from ambient_quality_agent.tools.insights.findings import Finding
from ambient_quality_agent.tools.insights.models import RubricExample
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)


class Cluster(BaseModel):
    """Findings sharing one concrete underlying issue.

    The clustering LLM's output unit, and the candidate occurrence matched
    against existing insights. Transient, and here rather than in `models`
    because of it: nothing reads a `Cluster` back or writes one down. It is
    built by `cluster_and_label`, checked by `validate_clusters`, enriched by
    `attach_examples`, and then spent -- the durable `Insight` and
    `InsightOccurrence` records are created from it and it is dropped.
    """

    cluster_id: int = 0
    """Ordinal identifier assigned for cluster deduplication and merging."""

    label: str
    """Short controlled-vocabulary description, e.g. "made no tool call". The
    entire matching key -- the only thing `insights.matching` compares."""

    finding_ids: list[int] = Field(default_factory=list)
    """The finding positions the model answered with. A finding's id is its
    index in the list handed to `cluster_and_label`, and the key the prompt and
    `_build_cluster_response_schema` ask the model to return them under."""

    item_count: int = 0
    """Findings covered. Stored rather than derived from ``examples``, which is
    capped."""

    trace_ids: list[str] = Field(default_factory=list)
    """Ids of the distinct sessions the covered findings came from, in
    first-seen order. Ids rather than a number so `merge.merge_clusters` can
    union two candidates without counting a shared conversation twice; only
    their number is persisted."""

    agent_revisions: list[str] = Field(default_factory=list)
    """Deployment revisions the covered findings came from, in first-seen order.
    Kept as the full set rather than one resolved value so `merge.merge_clusters`
    can union two candidates; the occurrence records the newest of them."""

    examples: list[RubricExample] = Field(default_factory=list)
    """Capped at `MAX_EXAMPLES`; persisted verbatim as JSON on the occurrence."""

    @property
    def trace_count(self) -> int:
        """How many conversations this issue was seen in.

        Counted over every covered finding, not just the capped `examples`,
        which would understate a widespread issue as a handful of cases.
        """
        return len(self.trace_ids)


class ClusteringInfo(BaseModel):
    """Findings that produced no insight this sweep, by cause.

    Surfaced in the node summary so a shrunken sweep is visible, not just logged.
    """

    errored_rubrics: int = 0
    """Number of findings discarded due to LLM chunk processing failures."""

    unclustered_rubrics: int = 0
    """Number of findings omitted from valid clusters, or excluded
    because their text describes no problem."""


MAX_EXAMPLES = 10
"""Traces kept per cluster. Kept small because each example carries a full
conversation trace and is persisted verbatim into the occurrence's JSON payload,
while a cluster can cover hundreds of findings."""

CHUNK_CONCURRENCY = 4
"""Number of concurrent clustering llm calls."""

CHUNK_SIZE = 4_000
"""Findings per clustering call.

The maximum number of output tokens is the limiting factor. The model echoes an
id back for every finding it clusters, at ~5 tokens each, against an output
budget of ~65k that it also has to think inside. The input side needs no guard
at this size -- 4k findings is ~210k tokens against a 1M context."""

_CLUSTER_INSTRUCTIONS = """\
You are given multiple tuples of {id, expected_behavior, actual_behavior}, each
describing one defect observed in a production AI agent.

Group the tuples by the same ROOT CAUSE -- the single underlying defect one fix
would address -- and give each group a label that states that defect. Two tuples
belong together when the same change -- to the agent's instruction, to a tool, or
to a guardrail around it -- would prevent both, however differently they are
worded and whichever conversation each came from.

Each label becomes a durable issue that later sweeps match new defects against,
so it has to be two things at once: SPECIFIC enough that two genuinely different
defects never collide under it, and STABLE enough that the SAME defect always
produces the SAME string. Everything below serves those two goals.

## What a label names -- the rule that decides granularity

**A tool-call or tool-argument defect names the exact tool** (and argument). The
tool name is the defect's identity: `failed to call send_notification` and
`failed to call update_contract` are two different bugs with two different fixes,
so they are two clusters. NEVER merge defects about different tools into one
cluster, and NEVER widen a label to "a tool" / "the required tool" / "a required
argument" to make a cluster bigger. A label that names no specific tool where the
defect is about a specific tool cannot be deduplicated and cannot be acted on --
it is worse than a cluster of one. If two tuples are about different tools, they
stay apart even if that leaves each alone.

**A rule, constraint, step or policy defect names the exact rule**, and there it
DOES aggregate across tools -- because the rule is the fix target, not any one
tool. `ignored the one operation at a time constraint` is a single cluster
however many different tools were called in parallel to break it; `ignored the
English-only constraint` is one cluster regardless of what the agent was doing
when it answered in another language.

Name a rule by a short, canonical description of what it requires -- NOT by
copying the instruction's exact words. The same rule is phrased differently
across domains and revisions, and if you copy the wording verbatim, one rule
splits into several clusters that no longer deduplicate. Normalize it:
  - Drop the instruction's leading verb and its casing. `Perform one operation
    at a time` and `Perform one validated operation at a time` are the SAME rule
    and must both become `ignored the one operation at a time constraint`.
  - Name the specific rule that failed, not the section heading it sits under. A
    response that omits its next-steps section is `omitted the Suggested Next
    Steps section`, never `ignored the RESPONSE STRUCTURE GUIDELINES constraint`
    -- the ALL-CAPS section title is a heading, not the rule.
This normalization only merges different wordings of ONE rule. It never merges
two genuinely different rules: `exactly one lookup per entity type` is a distinct
rule from `one operation at a time` and keeps its own canonical label.

The test for which kind you have: would the fix change a specific tool (its
schema, whether the agent calls it) or a specific instruction rule? Name what the
fix would change, in canonical short form.

## When several checks fail from one cause

When one failure makes several checks fail at once, those checks are ONE issue.
If the agent never called a required tool, the complaints about that call's
arguments -- their value, their format, their presence -- all follow from the
missing call, because there is no call to carry them. Fold them into the single
`failed to call <tool>` cluster, and do not emit an argument-level label there:
no argument was dropped from a call that was never made. `actual_behavior` tells
the two apart: "no tool call was found" means the call is missing, while "called
<tool> but priority was 'Urgent'" means the call happened and the argument was
the defect.

## Writing the label

Write a short verb phrase: what the agent did wrong, plus the exact tool,
argument or rule a fix would target. Derive it from both fields --
`expected_behavior` names the tool, argument, rule or step that was required, and
`actual_behavior` says how the agent fell short.

Keep out anything that changes from case to case -- the user's particular
request ("...for the VPN outage"), ids, quoted values, counts. Those vary
session to session and would stop two hits of one bug from matching. The tool,
argument and rule names are the opposite: they are what MUST stay in, because
they are what makes the same defect resolve to the same string across sweeps.

A label must be true of EVERY tuple in its cluster. Read each label back against
its members before answering. If a cluster has collected tuples about more than
one tool, that is not a labelling problem to paper over with a vaguer word -- it
is a grouping mistake: SPLIT it into one cluster per tool, each with its own
concrete label. Never resolve it by widening the label.

Use only <arg> names that appear verbatim in the tuples as a real tool
parameter. Do not turn a requirement's prose into a parameter: if a check says
the ticket should "include that customers are impacted", that is something the
call should have conveyed, not a parameter named "customer impact". With no real
parameter name to use, describe the defect without naming an argument (e.g.
"called <tool> with details that miss the reported urgency").

The tuples are all you are given -- no instruction text and no tool
declarations -- so never label a defect by what the agent was or was not given.
A phrase like "which the agent was not given" is a claim about a configuration
you cannot read; name the observed defect instead, e.g. "failed to call <tool>"
rather than "instruction requires <tool>, which the agent was not given".

## Singletons are expected

A defect that appears once in this batch is a cluster of one, and that is
correct -- on a later sweep, when it appears again, it will match this same label
and become a recurring issue. Do NOT merge unrelated defects together to reduce
the number of single-tuple clusters. A precise cluster of one is useful; a vague
cluster of nine unrelated defects is not.

## Label examples

The phrasings below are EXAMPLES of the style and granularity wanted, covering
the failure modes seen most often. They are NOT a fixed menu: when a defect fits
none of them, write your own phrase in the same spirit. Substitute the real
names, e.g. "failed to call create_ticket", "called create_ticket with an
invalid priority argument", "ignored the English-only constraint".

Tool calling (name the tool):
  - "failed to call <tool>"                         (the call is missing)
  - "called the wrong tool instead of <tool>"
  - "forced a <tool> call instead of answering directly"
  - "repeated the same <tool> call without progress"
  - "repeated a <tool> lookup already answered in context"

Tool arguments, only when the call WAS made (name the tool and the argument):
  - "called <tool> with an invalid <arg> argument"  (bad value)
  - "called <tool> omitting the required <arg> argument"
  - "called <tool> with <arg> of the wrong type"
  - "called <tool> passing <value> to the wrong argument"
  - "called <tool> with a made-up <arg> value"

Handling of results (name the tool):
  - "misread the <tool> result"
  - "treated the <tool> error as success"
  - "acted on data the <tool> result contradicts"

Instructions and policy (name the rule):
  - "ignored the <constraint> constraint"
  - "skipped the required <step> step"
  - "performed <action> the policy forbids"
  - "asked the user to confirm instead of proceeding"

Configuration (name the rules that conflict):
  - "given rules that cannot both be followed"

Grounding (name the tool or the fabricated detail):
  - "claimed to call <tool> without calling it"
  - "invented a <tool> result"
  - "fabricated a <detail> absent from the tool output"

Response quality:
  - "produced an empty response"
  - "stopped before completing the request"
  - "answered only part of the request"
  - "omitted the required <section> section"

Rules:
  - An id is the integer `id` of a tuple below. Return those integers, and never
    one that was not given to you.
  - Every id belongs in exactly one cluster. Leave one out only when its text
    describes no problem at all.
  - Merge two clusters only when one and the same fix would address both: the
    same tool and the same failure (even if worded differently), or the same
    rule. Different tool, different argument, or different rule stay apart.
  - Write the JSON compactly: no space after a comma or a colon, and no line
    break inside `finding_ids`. Write [1,2,3], never [1, 2, 3]. A sweep returns
    thousands of ids, and the spaces alone cost enough of the response limit to
    truncate the answer.
"""

_TUPLES_HEADER = """
Failed tuples:
"""


def _build_prompt(findings: Sequence[Finding], start: int) -> str:
    """The full clustering prompt: instructions, then the tuples.

    ``start`` is the chunk's offset in the whole findings list, so the ``id`` a
    finding carries is its global position -- the id `validate_clusters` and
    `attach_examples` address it by.

    Args:
        findings: Findings included in this prompt chunk.
        start: Global index offset for these findings.

    Returns:
        Rendered prompt string.
    """
    payload = json.dumps(
        [
            {
                "id": start + offset,
                "expected_behavior": finding.expected_behavior,
                "actual_behavior": finding.actual_behavior,
            }
            for offset, finding in enumerate(findings)
        ],
        ensure_ascii=False,
    )
    # No tool declarations here. A toolset is per session and one chunk mixes
    # findings from many, so grounding them would mean a session dimension on
    # every tuple; a flat union instead attributes one session's tools to
    # another, which is how a label names a tool that session never had.
    # Nothing downstream corrects such a label: insight matching keys on
    # `label`, so a wrong tool name in one survives into later sweeps.
    return _CLUSTER_INSTRUCTIONS + _TUPLES_HEADER + payload


def cluster_and_label(
    findings: Sequence[Finding], model_call: Any | None = None
) -> tuple[list[Cluster], int]:
    """Group the findings into labelled clusters, `CHUNK_SIZE` at a time.

    Args:
        findings: The sweep's findings, from a `FindingSet`.
        model_call: Prompt-to-text callable; `call_clustering_model` when omitted.

    Returns:
        The chunks' clusters, numbered from zero in `cluster_id`, and the count
        of findings dropped because their chunk's clustering call failed.
    """
    if not findings:
        return [], 0

    call = model_call or call_clustering_model
    starts = list(range(0, len(findings), CHUNK_SIZE))

    def cluster_chunk(start: int) -> tuple[list[Cluster], Exception | None]:
        chunk = findings[start : start + CHUNK_SIZE]
        logger.info(
            "insights: clustering findings %d-%d of %d.",
            start,
            start + len(chunk),
            len(findings),
        )
        try:
            return _parse_clusters(call(_build_prompt(chunk, start))), None
        except Exception as exc:
            return [], exc

    with futures.ThreadPoolExecutor(max_workers=CHUNK_CONCURRENCY) as pool:
        results = list(pool.map(cluster_chunk, starts))

    clusters: list[Cluster] = []
    failures: list[Exception] = []
    findings_skipped_to_errors = 0
    # Collected in the input's order, not completion's, so a sweep's
    # `cluster_id`s and the merge prompt built from them do not depend on which
    # chunk happened to finish first.
    for start, (found, exc) in zip(starts, results, strict=True):
        if exc is not None:
            failures.append(exc)
            findings_skipped_to_errors += len(
                findings[start : start + CHUNK_SIZE]
            )
            logger.warning(
                "insights: clustering chunk at finding %d failed: %s",
                start,
                exc,
            )
        clusters.extend(found)
    chunk_count = len(starts)
    if failures:
        logger.warning(
            "insights: %d of %d clustering chunk(s) failed; their findings are ignored.",
            len(failures),
            chunk_count,
        )
        if len(failures) == chunk_count:
            raise failures[0]
    numbered = [
        cluster.model_copy(update={"cluster_id": index})
        for index, cluster in enumerate(clusters)
    ]
    return numbered, findings_skipped_to_errors


def _parse_clusters(response: str) -> list[Cluster]:
    """Decode the model's JSON into `Cluster`s, tolerating junk.

    The response schema constrains the shape, but a refusal or truncated
    generation can still produce something else.

    Args:
        response: Raw JSON response text from the model.

    Returns:
        List of validated Cluster objects.
    """
    try:
        decoded = json.loads(response or "")
    except json.JSONDecodeError as exc:
        logger.warning(
            "insights: clustering response was not valid JSON: %s", exc
        )
        return []

    raw_clusters = (
        decoded.get("clusters") if isinstance(decoded, dict) else decoded
    ) or []
    if not isinstance(raw_clusters, list):
        logger.warning("insights: clustering response held no cluster list.")
        return []

    clusters: list[Cluster] = []
    for raw in raw_clusters:
        try:
            clusters.append(Cluster.model_validate(raw))
        except Exception as exc:
            logger.warning("insights: skipping a malformed cluster: %s", exc)
    return clusters


def validate_clusters(
    clusters: Iterable[Cluster], finding_count: int
) -> list[Cluster]:
    """Drop clusters the model did not ground in the findings it was given.

    A finding id is grounded when it is an in-range position, i.e. below
    ``finding_count``. ``item_count`` is recomputed here so the count is ours,
    not the model's.

    Args:
        clusters: Candidate clusters to validate.
        finding_count: Total number of valid finding indices.

    Returns:
        List of grounded clusters with updated item counts.
    """
    in_range = range(finding_count)
    validated: list[Cluster] = []
    dropped = 0
    for cluster in clusters:
        unknown = [fid for fid in cluster.finding_ids if fid not in in_range]
        if not cluster.label.strip() or not cluster.finding_ids or unknown:
            dropped += 1
            continue
        validated.append(
            cluster.model_copy(update={"item_count": len(cluster.finding_ids)})
        )
    if dropped:
        logger.warning(
            "insights: dropped %d ungrounded cluster(s) (empty label, no finding ids, "
            "or ids out of range).",
            dropped,
        )
    return validated


def attach_examples(
    clusters: Iterable[Cluster],
    findings: Sequence[Finding],
    sessions: Mapping[str, list[dict]],
) -> list[Cluster]:
    """Hang the evidence off each cluster: capped examples, trace ids, revisions.

    Up to `MAX_EXAMPLES` findings-with-traces, plus the sessions and deployment
    revisions *all* of the cluster's findings came from.

    Built from the findings and their session map, so no extra calls. A finding
    whose session is absent from the map keeps an empty trace.

    Args:
        clusters: Clusters to enrich.
        findings: Full sequence of findings indexed by ID.
        sessions: Map of session ID to conversation turn history.

    Returns:
        List of clusters with attached examples, trace IDs, and revisions.
    """
    return [
        _attach_evidence(cluster, findings, sessions) for cluster in clusters
    ]


def _attach_evidence(
    cluster: Cluster,
    findings: Sequence[Finding],
    sessions: Mapping[str, list[dict]],
) -> Cluster:
    """One cluster with its findings' evidence attached.

    Args:
        cluster: Cluster to enrich.
        findings: Sequence of findings indexed by ID.
        sessions: Map of session ID to conversation turn history.

    Returns:
        Cluster copy with evidence fields populated.
    """
    covered = [
        findings[fid] for fid in cluster.finding_ids if 0 <= fid < len(findings)
    ]
    return cluster.model_copy(
        update={
            "examples": [
                _build_example(finding, sessions)
                for finding in covered[:MAX_EXAMPLES]
            ],
            "trace_ids": _deduplicate_non_empty(
                finding.session_id for finding in covered
            ),
            "agent_revisions": _deduplicate_non_empty(
                finding.agent_revision for finding in covered
            ),
        }
    )


def _deduplicate_non_empty(values: Iterable[str]) -> list[str]:
    """The non-empty values, deduplicated, in first-seen order.

    Args:
        values: Iterable of string values.

    Returns:
        Deduplicated list of non-empty strings.
    """
    return list(dict.fromkeys(value for value in values if value))


def _build_example(
    finding: Finding, sessions: Mapping[str, list[dict]]
) -> RubricExample:
    """One finding as stored evidence: the finding, its session, its trace.

    Args:
        finding: The finding providing rubric details.
        sessions: Mapping of session IDs to turn history dicts.

    Returns:
        Populated RubricExample instance.
    """
    return RubricExample(
        rubric=finding,
        eval_case_id=finding.session_id,
        trace=sessions.get(finding.session_id, []),
    )


def _build_cluster_response_schema() -> Any:
    """Constrain the response to ``{"clusters": [{"label", "finding_ids"}]}``.

    A deliberate subset of `Cluster` (``item_count`` and ``examples`` are
    derived by us, not asked of the model), and the API wants a
    ``google.genai.types.Schema`` rather than pydantic's dict -- so keep it in
    sync with `Cluster` by hand.

    Returns:
        Schema object defining the expected model output structure.
    """
    from google.genai import types

    return types.Schema(
        type=types.Type.OBJECT,
        properties={
            "clusters": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "label": types.Schema(type=types.Type.STRING),
                        "finding_ids": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.INTEGER),
                        ),
                    },
                    required=["label", "finding_ids"],
                ),
            )
        },
        required=["clusters"],
    )


def fetch_clustering_response(prompt: str) -> Any:
    """Call Gemini for clustering and return the full response object.

    Provides the response object (including usage metadata and finish reason)
    required by test harnesses.

    Args:
        prompt: Formatted clustering prompt.

    Returns:
        Full Gemini API response object.
    """
    return genai_json.call_gemini(prompt, _build_cluster_response_schema())


def call_clustering_model(prompt: str) -> str:
    """Call Gemini for clustering and return its raw JSON text.

    Args:
        prompt: Formatted clustering prompt.

    Returns:
        Generated text string from the model.
    """
    return fetch_clustering_response(prompt).text or ""
