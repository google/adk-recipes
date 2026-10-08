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

"""Load-test insight clustering and correlation on synthetic failed rubrics.

The two LLM stages of the insight pipeline only. No telemetry fetch and no
evaluation in front of them. Rubrics are generated here, so a run costs two
model stages instead of a full sweep, and the size is ours to choose.

## What the clustering stage is looking for

A sweep's whole insight yield hinges on one JSON response:

- `clustering._parse_clusters` returns ``[]`` for a response it cannot decode,
  and the node then persists nothing.
- The response has to echo back every rubric id it clustered, so the output
  grows with the input while the model's output budget does not.

The failure to expect first is therefore a generation truncated mid-JSON, not a
model that writes bad syntax. The two are separated here: `finish_reason` and
the output-token count are recorded beside the decode result, so a malformed
response reads as "hit the ceiling" or as "the model really did emit junk".

The corpus comes from a fixed catalog of defects, so each rubric's true cluster
is known. The run also reports how well the model recovered them. A response
that parses but scatters one defect across nine clusters is its own failure.

## What the correlation stage is looking for

Whether the same-issue judge recognises a defect it has already seen, and
declines one it has not. How a cell is built:

1. Populate the store from one sweep's clusters.
2. Withhold a share of the defects (`--new-frac`), so some candidates match
   nothing. A corpus in which everything matches is the cheap case for any
   judge that stops at its first acceptance.
3. Correlate a *different* sweep's clusters against it, so a recurring defect
   arrives worded as a later sweep would word it, not as a copy of the stored
   string.
4. Score each answer against the defect behind the cluster, not against the
   label. An insight an earlier sweep worded differently is still the right
   answer.

Every judge in `--matchers` runs over that one store and those same candidates,
so their answers are comparable pair for pair:

    production   `find_existing_insights` end to end, as a sweep runs it
    thread_pool  a plain ``SELECT``, then one pairwise Gemini call per
                 (candidate, insight) pair, stopping at the first acceptance

`--audit-pairs` adds a stage beneath them. It puts labelled pairs to the
pairwise judge with no scan around it, which separates two things that are
easily confused:

- what a judge decides about one pair, and
- what the rule wrapped around it makes of those decisions.

The difference between them turned out to be the whole result: per pair the
judges score alike, and only the rule varies.

## Running it

    GOOGLE_CLOUD_PROJECT=my-project \\
        uv run --frozen python tools/insights_load_test.py --sizes 250,1000,5000

Correlation runs against real BigQuery, under a throwaway agent name deleted
again at the end. It needs the two insight tables in `AQA_DATASET`.

- ``--keep`` retains the throwaway agent's insights.
- ``--no-correlate`` skips the stage entirely, and with it the BigQuery setup.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import json
import os
import pathlib
import random
import statistics
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

# Defaults for the env vars `config.load()` requires, set before the agent
# imports below resolve `config`. `setdefault` means an exported value wins.
# Disable `.env` loading from the agent package so the run uses only
# explicitly exported environment variables and the defaults below.
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("AQA_OBSERVED_AGENT_NAME", "insights-load-test")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "local-dev-project")

from agentplatform._genai.types import (
    EvalCase,
    EvalCaseMetricResult,
    EvalCaseResult,
    EvaluationDataset,
    EvaluationResult,
    ResponseCandidateResult,
    Rubric,
    RubricContent,
    RubricContentProperty,
    RubricVerdict,
)
from ambient_quality_agent.tools.evaluation.findings_adapter import (
    eval_results_to_finding_set,
)
from ambient_quality_agent.tools.insights import clustering, merge
from ambient_quality_agent.tools.insights.clustering import Cluster
from ambient_quality_agent.tools.insights.matching import MATCH_CONCURRENCY
from ambient_quality_agent.tools.insights.models import (
    Insight,
    InsightOccurrence,
    InsightStatus,
    OccurrenceState,
)

# The tools the synthetic agent declares. They travel the way a real sweep's do
# -- written into each eval case's `agent_data.agents` and recovered into
# `FindingSet.agent_revisions` by the adapter -- so a run measures that recovery
# against declarations the pipeline actually built.
TOOL_CATALOG = [
    {
        "name": "create_ticket",
        "parameters": ["description", "priority", "title"],
        "description": "Creates a new IT support ticket. Args: title, description, priority.",
    },
    {
        "name": "lookup_travel_policy",
        "parameters": ["destination", "employee_level"],
        "description": "Looks up the travel policy for a destination and employee level.",
    },
    {
        "name": "file_expense",
        "parameters": ["amount", "category", "currency"],
        "description": "Files an expense claim. Args: amount, currency, category.",
    },
    {
        "name": "search_knowledge_base",
        "parameters": ["max_results", "query"],
        "description": "Searches the internal knowledge base for articles.",
    },
    {
        "name": "escalate_to_human",
        "parameters": ["queue", "reason"],
        "description": "Hands the conversation to a human agent.",
    },
    {
        "name": "transfer_to_agent",
        "parameters": ["agent_name"],
        "description": "Transfers the conversation to another agent.",
    },
]


@dataclasses.dataclass(frozen=True)
class Defect:
    """One seeded root cause, and the many ways a rubric can report it.

    `key` is the ground truth: every tuple minted from this defect belongs in
    one cluster, whichever phrasing it drew. The phrasings differ on purpose --
    a corpus where one defect always reads the same rewards string matching
    rather than the grouping we are measuring.
    """

    key: str
    expected: tuple[str, ...]
    actual: tuple[str, ...]


# Twelve defects over the six tools, covering the label families the clustering
# prompt enumerates: a missing call, a wrong argument on a call that happened,
# a hallucination, and a response-quality miss.
DEFECTS = (
    Defect(
        key="failed to call create_ticket",
        expected=(
            "The agent must call create_ticket when the user reports a broken system.",
            "A support ticket has to be opened for any unresolved incident.",
            "The response should result in a create_ticket call being made.",
        ),
        actual=(
            "No tool call was found in the trajectory for {case}.",
            "The agent replied in prose about {case} and never invoked any tool.",
            "create_ticket was never called; the agent only apologised for {case}.",
        ),
    ),
    Defect(
        key="called create_ticket with an invalid priority argument",
        expected=(
            "The priority argument must be one of P0, P1, P2 or P3.",
            "create_ticket must be given a priority from the allowed enum.",
            "The ticket's priority has to use the documented severity codes.",
        ),
        actual=(
            "called create_ticket but priority was 'Urgent' while handling {case}.",
            "The call passed priority='very high', which is not an allowed value ({case}).",
            "priority was set to 'ASAP' on the create_ticket call for {case}.",
        ),
    ),
    Defect(
        key="called create_ticket omitting the required title argument",
        expected=(
            "Every create_ticket call must include a title.",
            "The ticket must carry a one-line title summarising the issue.",
            "create_ticket requires the title argument to be populated.",
        ),
        actual=(
            "create_ticket was called with description only; title was absent ({case}).",
            "The call for {case} omitted title entirely.",
            "No title argument appears on the create_ticket call about {case}.",
        ),
    ),
    Defect(
        key="failed to call lookup_travel_policy",
        expected=(
            "The agent must consult lookup_travel_policy before answering a "
            "travel question.",
            "Travel questions require the policy tool to be called first.",
            "lookup_travel_policy has to be invoked for destination questions.",
        ),
        actual=(
            "The agent answered the {case} question from memory; no tool call was found.",
            "lookup_travel_policy was never called during the {case} exchange.",
            "No policy lookup happened before the agent answered about {case}.",
        ),
    ),
    Defect(
        key="called lookup_travel_policy omitting the required employee_level argument",
        expected=(
            "lookup_travel_policy must be given the employee's level.",
            "The policy lookup requires employee_level so the right tier applies.",
            "employee_level must be supplied on every policy lookup.",
        ),
        actual=(
            "The call for {case} passed destination only.",
            "employee_level was missing from the lookup_travel_policy call ({case}).",
            "lookup_travel_policy was called without employee_level while handling {case}.",
        ),
    ),
    Defect(
        key="called file_expense with amount of the wrong type",
        expected=(
            "The amount argument must be a number.",
            "file_expense expects a numeric amount, not a formatted string.",
            "Expense amounts have to be passed as numbers.",
        ),
        actual=(
            "amount was passed as the string '120 EUR' for {case}.",
            "The file_expense call for {case} sent amount='€120,00'.",
            "amount arrived as text rather than a number on the {case} claim.",
        ),
    ),
    Defect(
        key="called file_expense omitting the required currency argument",
        expected=(
            "Every expense must state its currency.",
            "file_expense requires the currency argument.",
            "The claim's currency has to be supplied explicitly.",
        ),
        actual=(
            "currency was absent from the file_expense call for {case}.",
            "The agent filed the {case} expense with amount and category only.",
            "No currency argument appears on the claim for {case}.",
        ),
    ),
    Defect(
        key="called the wrong tool instead of search_knowledge_base",
        expected=(
            "How-to questions must be answered from search_knowledge_base.",
            "The agent should search the knowledge base before escalating.",
            "Documentation questions require a knowledge-base search.",
        ),
        actual=(
            "The agent called escalate_to_human for {case} instead of searching.",
            "escalate_to_human was invoked although {case} is a documented question.",
            "The agent skipped the knowledge base and escalated {case} straight away.",
        ),
    ),
    Defect(
        key="claimed to call search_knowledge_base without calling it",
        expected=(
            "Any cited article must come from an actual tool result.",
            "The agent must not describe searches it did not perform.",
            "Claims about the knowledge base have to be backed by a tool call.",
        ),
        actual=(
            "The reply for {case} says 'I searched our docs' but no call was made.",
            "The agent claimed to have found an article about {case}; the "
            "trajectory has no search.",
            "No search_knowledge_base call backs the citation given for {case}.",
        ),
    ),
    Defect(
        key="invented a search_knowledge_base result",
        expected=(
            "Cited article ids must appear in the tool output.",
            "The agent must only quote articles the search returned.",
            "Article references have to be grounded in the tool result.",
        ),
        actual=(
            "The agent cited KB-4471 for {case}; the tool returned no such article.",
            "The article id quoted for {case} is absent from the search result.",
            "The reply about {case} references a document the tool never returned.",
        ),
    ),
    Defect(
        key="repeated the same escalate_to_human call without progress",
        expected=(
            "The agent must not repeat an escalation that already succeeded.",
            "One escalation per conversation is enough.",
            "Escalating twice for the same request is not allowed.",
        ),
        actual=(
            "escalate_to_human was called three times for {case} with identical arguments.",
            "The agent re-escalated {case} after the first hand-off had succeeded.",
            "Two identical escalations appear in the {case} trajectory.",
        ),
    ),
    Defect(
        key="stopped before completing the request",
        expected=(
            "The agent must finish the multi-step request it accepted.",
            "Every step the user asked for has to be carried out.",
            "The agent should not end its turn with work outstanding.",
        ),
        actual=(
            "The agent handled the first half of {case} and then ended the turn.",
            "Work on {case} stops after the acknowledgement; nothing follows.",
            "The turn for {case} ends with the second request untouched.",
        ),
    ),
)

# Case-specific noise. Real rubric prose names the user's particular request,
# which is exactly the wording the prompt tells the model to keep out of a
# label -- so the corpus has to carry it.
CASE_CONTEXTS = (
    "the VPN outage",
    "a laptop that will not boot",
    "a password reset for a contractor",
    "a Tokyo trip in March",
    "a taxi receipt from Zurich",
    "the shared printer on floor 3",
    "onboarding a new hire's mailbox",
    "a locked-out badge",
    "a conference registration refund",
    "the payroll portal timing out",
)


# One page per this many eval cases, and this many failed rubrics per case
# spread over two metrics -- the shape a sweep produces, where one conversation
# trips several checks and one defect therefore fails rubrics under more than
# one metric.
CASES_PER_PAGE = 25
RUBRICS_PER_CASE = 4
METRICS = ("multi_turn_task_success_v1", "multi_turn_tool_use_quality_v1")


def build_corpus(
    size: int, seed: int
) -> tuple[list[dict], dict[tuple[str, str], str]]:
    """Mint eval pages carrying ``size`` failed rubrics, round-robin over `DEFECTS`.

    Generates serialized evaluation pages rather than `Finding` objects directly,
    ensuring findings undergo `eval_results_to_finding_set` processing and inherit
    positional identifiers.

    Args:
        size: Target number of failed rubrics to generate.
        seed: Random seed for deterministic generation.

    Returns:
        Tuple of:
            - List of serialized evaluation page dictionaries.
            - Mapping of `(expected_behavior, actual_behavior)` to defect key.
    """
    rng = random.Random(seed)  # noqa: S311 - seeded for reproducible test data
    truth: dict[tuple[str, str], str] = {}
    pages: list[dict] = []
    verdicts: list[RubricVerdict] = []
    cases: list[list[RubricVerdict]] = []

    for index in range(size):
        defect = DEFECTS[index % len(DEFECTS)]
        expected = rng.choice(defect.expected)
        actual = rng.choice(defect.actual).format(
            case=rng.choice(CASE_CONTEXTS)
        )
        truth[expected, actual] = defect.key
        verdicts.append(
            RubricVerdict(
                # The autorater's own id, which extraction is expected to ignore
                # in favour of an ordinal -- present so the run proves it does.
                evaluated_rubric=Rubric(
                    rubric_id=f"autorater-rubric-{index:06d}",
                    content=RubricContent(
                        property=RubricContentProperty(description=expected)
                    ),
                ),
                verdict=False,
                reasoning=actual,
            )
        )
        if len(verdicts) == RUBRICS_PER_CASE:
            cases.append(verdicts)
            verdicts = []
        if len(cases) == CASES_PER_PAGE:
            pages.append(_build_eval_page(cases, len(pages)))
            cases = []
    if verdicts:
        cases.append(verdicts)
    if cases:
        pages.append(_build_eval_page(cases, len(pages)))
    return pages, truth


def _build_eval_page(cases: list[list[RubricVerdict]], page_index: int) -> dict:
    """Build one evaluation page with case results and matching dataset.

    Args:
        cases: Nested list of rubric verdicts per eval case.
        page_index: Ordinal page index for unique ID prefixing.

    Returns:
        Serialized EvaluationResult dictionary.
    """
    return EvaluationResult(
        eval_case_results=[
            EvalCaseResult(
                eval_case_index=case_index,
                response_candidate_results=[
                    ResponseCandidateResult(
                        response_index=0,
                        metric_results={
                            # Split across metrics so one defect's rubrics arrive
                            # under more than one, as they do in a real sweep.
                            metric: EvalCaseMetricResult(
                                score=0.0,
                                rubric_verdicts=verdicts[
                                    offset :: len(METRICS)
                                ],
                            )
                            for offset, metric in enumerate(METRICS)
                        },
                    )
                ],
            )
            for case_index, verdicts in enumerate(cases)
        ],
        evaluation_dataset=[
            EvaluationDataset(
                eval_cases=[
                    _build_eval_case(f"case-{page_index:03d}-{case_index:03d}")
                    for case_index in range(len(cases))
                ]
            )
        ],
    ).model_dump(mode="json")


def _build_eval_case(case_id: str) -> EvalCase:
    """Build an evaluation case conversation with tool declarations.

    Args:
        case_id: Unique evaluation case identifier.

    Returns:
        Configured EvalCase instance.
    """
    return EvalCase.model_validate(
        {
            "eval_case_id": case_id,
            "agent_data": {
                "agents": {
                    "it_support_agent": {
                        "agent_id": "it_support_agent",
                        "tools": [
                            {
                                "function_declarations": [
                                    {
                                        "name": tool["name"],
                                        "description": tool["description"],
                                        "parameters_json_schema": {
                                            "type": "object",
                                            "properties": {
                                                name: {"type": "string"}
                                                for name in tool["parameters"]
                                            },
                                        },
                                    }
                                ]
                            }
                            for tool in TOOL_CATALOG
                        ],
                    }
                },
                "turns": [
                    {
                        "turn_index": 0,
                        "events": [
                            {
                                "author": "user",
                                "content": {
                                    "role": "user",
                                    "parts": [
                                        {"text": f"request for {case_id}"}
                                    ],
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )


@dataclasses.dataclass
class ClusteringAttempt:
    """One clustering call, from the raw response to the recovered clusters."""

    size: int
    attempt: int
    latency_s: float = 0.0
    """Time in the clustering calls; `_build_instrumented_model_call` records the last
    chunk's, so with several chunks this understates the sweep."""

    merge_latency_s: float = 0.0
    error: str = ""
    """Set when the API call itself raised -- distinct from a bad response."""

    finish_reason: str = ""
    prompt_tokens: int = 0
    answer_tokens: int = 0
    """Tokens of JSON the model actually returned."""

    thought_tokens: int = 0
    """Reasoning tokens. Charged against the SAME ~65k output budget as the
    answer, so a prompt that reasons hard leaves less room to enumerate ids --
    they compete, and only splitting them says which one ran out."""

    response_chars: int = 0
    prompt_chars: int = 0
    json_status: str = ""
    """Worst `_classify_json_status` across the sweep's chunks, so one bad chunk cannot
    be reported as a clean run."""

    chunk_statuses: list[str] = dataclasses.field(default_factory=list)
    """`_classify_json_status` per chunk, in order. A sweep is only as good as its worst
    chunk, and the tokens above are summed across all of them."""

    parsed_clusters: int = 0
    validated_clusters: int = 0
    """After `clustering.validate_clusters` -- an ungrounded cluster is dropped
    whole, so the gap from `parsed_clusters` is rubrics silently lost."""

    assigned_ids: int = 0
    """Input rubrics that reached a cluster. Below `size` the rest were dropped,
    and nothing downstream notices: `clustering.validate_clusters` only checks
    the ids the model did return, so the occurrence's ``item_count`` reports the
    smaller number as if it were the whole finding."""

    stopped_after: int = 0
    """Corpus position the model stopped at, when the ids it returned are a
    contiguous prefix; ``0`` when they are not. Separates "gave up enumerating
    after the first N rubrics", which leaves a whole tail of the sweep
    unexamined, from ids missed here and there across the corpus."""

    invented_ids: int = 0
    duplicate_ids: int = 0
    purity: float = 0.0
    """Share of assigned rubrics sitting in a cluster whose majority defect is
    their own. 1.0 means no cluster mixes two root causes."""

    defects_recovered: int = 0
    """Distinct seeded defects that are some cluster's majority. Below
    ``len(DEFECTS)`` means defects were merged or lost."""

    extracted_rubrics: int = 0
    """Findings `eval_results_to_finding_set` produced from the pages. Below `size` means the
    corpus and the adapter disagree about what a failure is, which would make
    every other number here describe a different sweep than intended."""

    tools_recovered: int = 0
    """Distinct tools the adapter recovered onto the failing sessions. Below
    ``len(TOOL_CATALOG)`` means the corpus and the adapter disagree about where
    an agent's declarations live, which is what the verification pass reads."""

    examples_attached: int = 0
    """Evidence rows `attach_examples` hung off the clusters -- the traces an
    occurrence carries into BigQuery."""

    chunks: int = 0
    """Clustering calls the sweep took, one per `clustering.CHUNK_SIZE`."""

    clusters_before_merge: int = 0
    """Candidates the chunks produced between them. Above `defects_recovered`
    means the same defect came back from more than one chunk, which is what
    `merge_clusters` is there to fold -- and what would mint duplicate insights
    if it did not."""

    raw_response: str = dataclasses.field(default="", repr=False)

    @property
    def ok(self) -> bool:
        return not self.error and self.json_status in ("ok", "fake")

    def settle_status(self) -> None:
        """Reduce the chunks' verdicts to the attempt's.

        Anything but a clean sweep is named after the failure, because a chunk
        that truncated is thousands of rubrics that reached no cluster -- the
        run is degraded even though the chunks around it answered.
        """
        # A chunk whose call raised never recorded a status at all, so the
        # count that is missing is the count that failed outright -- without
        # this a sweep that lost a chunk to a 503 reads as clean.
        missing = max(0, self.chunks - len(self.chunk_statuses))
        self.chunk_statuses += ["failed"] * missing
        bad = [s for s in self.chunk_statuses if s not in ("ok", "fake")]
        if not self.chunk_statuses:
            return
        clean = "fake" if "fake" in self.chunk_statuses else "ok"
        self.json_status = (
            clean
            if not bad
            else f"{bad[0]} {len(bad)}/{len(self.chunk_statuses)}"
        )


def _classify_json_status(raw: str) -> str:
    """Classify a raw model response into its parse status.

    Separates unclosed JSON responses (caused by generation token caps) from
    malformed syntax.

    Args:
        raw: Raw model output string.

    Returns:
        Status string: 'empty', 'truncated', 'invalid', or 'ok'.
    """
    if not raw.strip():
        return "empty"
    try:
        json.loads(raw)
    except json.JSONDecodeError:
        return "truncated" if _is_unclosed(raw) else "invalid"
    return "ok"


def _is_unclosed(raw: str) -> bool:
    """Check whether raw JSON text ends inside an unclosed string or bracket.

    Args:
        raw: String to examine.

    Returns:
        True if string quotes or brackets remain open at end of string.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    return in_string or depth > 0


def _compute_prefix_length(assigned: set[str], truth: dict[str, str]) -> int:
    """Compute how many contiguous IDs from index 0 were assigned.

    Args:
        assigned: Set of rubric IDs assigned to clusters.
        truth: Mapping of rubric IDs to defect keys.

    Returns:
        Contiguous prefix length if assignment stopped early, or 0.
    """
    ordered = sorted(truth)
    prefix = 0
    while prefix < len(ordered) and ordered[prefix] in assigned:
        prefix += 1
    return prefix if prefix < len(ordered) and prefix == len(assigned) else 0


def _score(
    attempt: ClusteringAttempt,
    parsed: list[Cluster],
    validated: list[Cluster],
    truth: dict[str, str],
) -> None:
    """Populate grounding and clustering quality metrics on ``attempt``.

    Args:
        attempt: Target ClusteringAttempt instance to update.
        parsed: Raw parsed clusters before validation.
        validated: Validated clusters after filtering ungrounded IDs.
        truth: Mapping of rubric IDs to defect keys.
    """
    attempt.parsed_clusters = len(parsed)
    attempt.validated_clusters = len(validated)

    seen: set[str] = set()
    duplicates = 0
    invented = 0
    # Measured on `parsed`, before validation: validate_clusters drops a whole
    # cluster for one unknown id, so counting there would hide how often the
    # model invents them.
    for cluster in parsed:
        for rubric_id in (str(i) for i in cluster.finding_ids):
            if rubric_id not in truth:
                invented += 1
            elif rubric_id in seen:
                duplicates += 1
            else:
                seen.add(rubric_id)
    attempt.assigned_ids = len(seen)
    attempt.invented_ids = invented
    attempt.duplicate_ids = duplicates
    attempt.stopped_after = _compute_prefix_length(seen, truth)

    correct = 0
    majorities: set[str] = set()
    for cluster in validated:
        keys = [
            truth[str(rid)] for rid in cluster.finding_ids if str(rid) in truth
        ]
        if not keys:
            continue
        majority = max(set(keys), key=keys.count)
        majorities.add(majority)
        correct += keys.count(majority)
    attempt.purity = (
        correct / attempt.assigned_ids if attempt.assigned_ids else 0.0
    )
    attempt.defects_recovered = len(majorities)


def _build_fake_model_call(
    attempt: ClusteringAttempt, truth: dict[str, str]
) -> Callable[[str], str]:
    """Build a deterministic model call function answering from ground truth.

    Args:
        attempt: ClusteringAttempt instance recording metrics.
        truth: Mapping of rubric IDs to defect keys.

    Returns:
        Callable taking prompt string and returning synthetic cluster JSON.
    """

    def call(prompt: str) -> str:
        # Only the ids this chunk was actually given: answering from the whole
        # corpus would hand every chunk every rubric, and the scoring would read
        # that as the model duplicating ids.
        asked = json.loads(
            prompt.rsplit(clustering._TUPLES_HEADER, maxsplit=1)[-1]
        )
        by_defect: dict[str, list[str]] = {}
        for entry in asked:
            rubric_id = str(entry["id"])
            by_defect.setdefault(truth[rubric_id], []).append(rubric_id)
        raw = json.dumps(
            {
                "clusters": [
                    {"label": label, "finding_ids": ids}
                    for label, ids in by_defect.items()
                ]
            }
        )
        attempt.prompt_chars = len(prompt)
        attempt.raw_response = raw
        attempt.response_chars += len(raw)
        attempt.chunk_statuses.append("fake")
        attempt.finish_reason = "FAKE"
        return raw

    return call


def _build_fake_merge_call() -> Callable[[str], str]:
    """Group candidates by exact label, standing in for the merge model.

    Returns:
        Callable taking prompt string and returning merged cluster groups JSON.
    """

    def call(prompt: str) -> str:
        payload = json.loads(
            prompt.rsplit(merge._MERGE_INSTRUCTIONS, maxsplit=1)[-1]
        )
        by_label: dict[str, list[int]] = {}
        for entry in payload:
            by_label.setdefault(entry["label"], []).append(entry["id"])
        return json.dumps(
            {"groups": [{"ids": ids} for ids in by_label.values()]}
        )

    return call


def _build_instrumented_merge_call(
    attempt: ClusteringAttempt,
) -> Callable[[str], str]:
    """Build a timed wrapper around the production merge model call.

    Args:
        attempt: Target ClusteringAttempt instance to record merge latency.

    Returns:
        Instrumented callable taking prompt string and returning merge response.
    """

    def call(prompt: str) -> str:
        started = time.monotonic()
        raw = merge.call_merge_model(prompt)
        attempt.merge_latency_s = time.monotonic() - started
        return raw

    return call


def _build_instrumented_model_call(
    attempt: ClusteringAttempt,
) -> Callable[[str], str]:
    """Build an instrumented wrapper capturing clustering model response metadata.

    Args:
        attempt: Target ClusteringAttempt instance to record token usage and latency.

    Returns:
        Callable taking prompt string and returning model response text.
    """

    def call(prompt: str) -> str:
        started = time.monotonic()
        response = clustering.fetch_clustering_response(prompt)
        # Accumulated, not assigned: `cluster_and_label` calls this once per
        # chunk, and a per-chunk figure would describe whichever chunk happened
        # to go last.
        attempt.latency_s += time.monotonic() - started
        usage = response.usage_metadata
        if usage:
            attempt.prompt_tokens += usage.prompt_token_count or 0
            attempt.answer_tokens += usage.candidates_token_count or 0
            attempt.thought_tokens += usage.thoughts_token_count or 0
        candidates = response.candidates or []
        if candidates and candidates[0].finish_reason:
            attempt.finish_reason = str(candidates[0].finish_reason)
        raw = response.text or ""
        attempt.raw_response = raw
        attempt.response_chars += len(raw)
        attempt.chunk_statuses.append(_classify_json_status(raw))
        return raw

    return call


@dataclasses.dataclass(frozen=True)
class Candidate:
    """A merged cluster, and the seeded defect its findings actually came from.

    The correlation stage judges its answers against `defect_key` rather than
    against a label or an insight id: the store can hold several insights for
    one defect -- earlier sweeps worded it differently -- and matching any of
    them is correct. A key is the majority vote over the cluster's findings, the
    same rule `_score` uses for purity; a cluster whose findings are all
    invented ids has no key and is excluded from scoring.
    """

    cluster: Cluster
    defect_key: str


def _compute_defect_key(cluster: Cluster, truth: dict[str, str]) -> str:
    """Return the defect key representing the majority of findings in a cluster.

    Args:
        cluster: Cluster instance to evaluate.
        truth: Mapping of rubric IDs to defect keys.

    Returns:
        Majority defect key string, or empty string if no findings match truth.
    """
    keys = [truth[str(rid)] for rid in cluster.finding_ids if str(rid) in truth]
    return max(set(keys), key=keys.count) if keys else ""


def run_clustering(
    size: int, attempt_index: int, seed: int, fake_model: bool = False
) -> tuple[ClusteringAttempt, list[Candidate]]:
    """Execute end-to-end clustering pipeline for a simulated sweep payload.

    Args:
        size: Number of failed rubrics in the synthetic corpus.
        attempt_index: Ordinal index of the current attempt.
        seed: Random seed for corpus generation.
        fake_model: If True, uses deterministic local response generator.

    Returns:
        Tuple of (ClusteringAttempt record, list of generated Candidate clusters).
    """
    pages, truth_by_text = build_corpus(size, seed)
    attempt = ClusteringAttempt(size=size, attempt=attempt_index)
    attempt.chunks = -(-size // clustering.CHUNK_SIZE)

    finding_set = eval_results_to_finding_set(pages)
    findings = finding_set.findings
    attempt.extracted_rubrics = len(findings)
    attempt.tools_recovered = len(
        {
            tool.name
            for revisions in finding_set.agent_revisions.values()
            for revision in revisions
            for tool in revision.tools
        }
    )
    # Ground truth is keyed by a finding's position -- the id the adapter assigns
    # -- looked up by the text the corpus generated, never by a guessed id.
    truth = {
        str(index): truth_by_text[f.expected_behavior, f.actual_behavior]
        for index, f in enumerate(findings)
    }

    call = (
        _build_fake_model_call(attempt, truth)
        if fake_model
        else _build_instrumented_model_call(attempt)
    )
    try:
        parsed, _ = clustering.cluster_and_label(findings, model_call=call)
    except Exception as exc:
        attempt.error = f"{type(exc).__name__}: {exc}"
        return attempt, []
    attempt.settle_status()
    validated = clustering.validate_clusters(parsed, len(findings))
    enriched = clustering.attach_examples(
        validated, findings, finding_set.sessions
    )
    attempt.clusters_before_merge = len(enriched)
    merge_call = (
        _build_fake_merge_call()
        if fake_model
        else _build_instrumented_merge_call(attempt)
    )
    try:
        merged = merge.merge_clusters(enriched, model_call=merge_call)
    except Exception as exc:
        attempt.error = f"merge: {type(exc).__name__}: {exc}"
        return attempt, []
    _score(attempt, parsed, merged, truth)
    attempt.examples_attached = sum(len(c.examples) for c in merged)
    return attempt, [
        Candidate(c, _compute_defect_key(c, truth)) for c in merged
    ]


@dataclasses.dataclass
class MatcherOutcome:
    """One judge's answers to one set of candidates, scored against the truth.

    The five counts partition the candidates. Each names a distinct production
    consequence, not a distance from some ideal number.
    """

    matcher: str
    latency_s: float = 0.0
    judged_pairs: int = 0
    """(candidate, insight) pairs the judge was actually asked about. Every
    matcher here short-circuits, so this is below the full cross product by
    however much the early exit bought."""

    calls: int = 0
    """Model round trips spent, which is what decides how a matcher scales --
    not `judged_pairs`. A scan spends one call per pair; a matcher that puts
    several labels in one prompt judges a whole batch per call."""

    matched: int = 0
    """Right answer, recurring defect: an insight carrying the same defect
    existed and the judge found it. The sighting joins the insight already
    tracking it."""

    minted: int = 0
    """Right answer, new defect: no insight carried this defect and the judge
    said so. A new insight is opened, which is what should happen."""

    duplicate: int = 0
    """Wrong, on a defect the store holds: the judge matched nothing. A second
    insight is opened for a defect already being tracked, so the dashboard shows
    one problem twice and the evidence for it is split across both."""

    mislinked: int = 0
    """Wrong, on a defect the store holds: the judge matched a *different*
    defect's insight. Distinct from `duplicate` in where the damage lands --
    this sighting's evidence is filed under an unrelated label, and the insight
    that should have received it goes untouched and ages towards
    auto-resolution."""

    swallowed: int = 0
    """Wrong, on a defect the store does *not* hold: the judge matched an
    unrelated insight rather than declining. The worst of the three, because
    nothing visible happens -- a brand-new defect never gets an insight, so it
    never reaches the dashboard at all. `duplicate` and `mislinked` at least
    leave a trace someone can find."""

    stale: int = 0
    """Right defect, but not the newest insight recording it.

    Every matcher does this, for the same reason: each ranks by *acceptance*,
    and none can promise the newest insight of a defect is the one accepted. A
    newest-first scan returns its first acceptance, and a batched judge returns
    the option it picks out of the first batch that answers. When the judge
    rejects a defect's newest wording and accepts an older one, both land on the
    older insight. So this counts the judge's inconsistency, not the execution
    strategy's."""

    unscored: int = 0
    """Candidates with no defect key -- every finding id in them was invented,
    so there is nothing to score them against."""

    error: str = ""


@dataclasses.dataclass
class CorrelationResult:
    """The matching stage's behaviour on one sweep's worth of candidates."""

    candidates: int
    insights_in_store: int
    """Rows the judge runs against, counted from what was seeded rather than
    assumed from the candidate list."""

    defects_in_store: int = 0
    defects_held_out: int = 0
    """Defects deliberately absent from the store.

    The candidates carrying them have nothing to match, and pay a full scan to
    find that out. Without them every candidate matches, which is the cheap case
    for any matcher that stops at its first acceptance."""

    seed_latency_s: float = 0.0
    outcomes: list[MatcherOutcome] = dataclasses.field(default_factory=list)
    audits: list[JudgeAudit] = dataclasses.field(default_factory=list)
    error: str = ""


def _plan_store(
    store_candidates: list[Candidate],
    candidates: list[Candidate],
    store_size: int,
    new_frac: float,
    seed: int,
) -> tuple[list[Candidate], set[str]]:
    """Choose what goes into the store, and which defects stay out of it.

    Args:
        store_candidates: Clusters from the sweeps standing in for history.
        candidates: The sweep being correlated, whose defects decide what there
            is to hold out.
        store_size: Insights to seed. Distinct defects are seeded first, so a
            size below the defect count narrows coverage rather than duplicating
            it; above it, the surplus is spent on the alternative wordings
            earlier sweeps produced for defects already present. ``0`` means one
            insight per defect.
        new_frac: Share of the candidates' defects to keep out of the store.
        seed: Fixes the hold-out choice, so two runs are comparable.

    Returns:
        The clusters to seed, and the held-out defect keys.
    """
    rng = random.Random(seed)  # noqa: S311 - seeded for reproducible test data
    candidate_keys = sorted({c.defect_key for c in candidates if c.defect_key})
    held_out = set(
        rng.sample(candidate_keys, round(len(candidate_keys) * new_frac))
    )

    by_key: dict[str, list[Candidate]] = {}
    for entry in store_candidates:
        if entry.defect_key and entry.defect_key not in held_out:
            by_key.setdefault(entry.defect_key, []).append(entry)

    # One per defect first, then the spares round-robin, so a store too small to
    # hold every defect still holds each of the ones it does hold exactly once.
    first = [entries[0] for entries in by_key.values()]
    spares = [entry for entries in by_key.values() for entry in entries[1:]]
    planned = first + spares
    return (planned[:store_size] if store_size else first), held_out


def _score_matches(
    outcome: MatcherOutcome,
    candidates: list[Candidate],
    matches: dict[int, str | None],
    key_of_insight: dict[str, str],
    newest_of_key: dict[str, str],
) -> None:
    """Classify candidate matching results against ground truth defect keys.

    Args:
        outcome: MatcherOutcome instance to update with match statistics.
        candidates: List of evaluated Candidate clusters.
        matches: Mapping of candidate index to matched insight ID, or None.
        key_of_insight: Mapping of insight ID to defect key.
        newest_of_key: Mapping of defect key to newest insight ID.
    """
    stored_keys = set(key_of_insight.values())
    for index, candidate in enumerate(candidates):
        if not candidate.defect_key:
            outcome.unscored += 1
            continue
        matched = matches.get(index)
        recurring = candidate.defect_key in stored_keys
        if matched is None:
            if recurring:
                outcome.duplicate += 1
            else:
                outcome.minted += 1
        elif key_of_insight.get(matched) != candidate.defect_key:
            if recurring:
                outcome.mislinked += 1
            else:
                outcome.swallowed += 1
        else:
            outcome.matched += 1
            if newest_of_key.get(candidate.defect_key) != matched:
                outcome.stale += 1


def _list_open_labels(store: Any, agent_name: str) -> list[tuple[str, str]]:
    """Fetch open, unmerged insight labels for an agent ordered newest-first.

    Args:
        store: BigQuery store instance.
        agent_name: Target agent identifier string.

    Returns:
        List of `(insight_id, label)` tuples.
    """
    from ambient_quality_agent.tools.insights.models import InsightStatus

    sql = f"""
SELECT insight_id, label
FROM `{store._build_table_ref("insights")}`
WHERE agent_name = @agent_name
  AND status != '{InsightStatus.RESOLVED.value}'
  AND merged_into_insight_id IS NULL
ORDER BY updated_at DESC, insight_id
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
    from google.cloud import bigquery

    rows = store._run(
        sql, [bigquery.ScalarQueryParameter("agent_name", "STRING", agent_name)]
    )
    return [(row["insight_id"], row["label"]) for row in rows]


@dataclasses.dataclass
class JudgeAudit:
    """One judge's verdicts on labelled pairs, apart from any scan around them.

    The outcome tables measure a judge and an execution strategy together, and
    cannot say which of the two a difference belongs to. This puts both judges
    the identical pairs and scores each against the defect behind them, so the
    judges compare directly.
    """

    judge: str
    same_pairs: int = 0
    same_accepted: int = 0
    """Of pairs that *are* one defect, the ones accepted. Recall."""

    different_pairs: int = 0
    different_accepted: int = 0
    """Of pairs that are two defects, the ones accepted anyway. This is the
    number a scan compounds: a candidate asking about `n` labels gets about
    `n` chances to hit one, and stops at the first."""

    error: str = ""


def _audit_pairs(
    store: Any,
    pairs: list[tuple[str, str, bool]],
    concurrency: int,
    temperature: float | None,
) -> list[JudgeAudit]:
    """Put labelled (candidate, insight) pairs to the pairwise judge.

    Args:
        store: Unused; kept so the caller need not know which judges run here.
        pairs: ``(candidate label, insight label, same defect)`` triples.
        concurrency: Calls in flight for the `generateContent` judge.
        temperature: Sampling temperature for it; ``None`` for the default.

    Returns:
        One `JudgeAudit` for the pairwise judge.
    """
    results: list[JudgeAudit] = []

    audit = JudgeAudit(judge="pairwise")
    try:

        def is_accepted(pair: tuple[str, str, bool]) -> bool:
            candidate, existing, _ = pair
            prompt = (
                _PAIRWISE_FIRST_PART
                + candidate
                + _PAIRWISE_SECOND_PART
                + existing
            )
            return _is_pair_accepted(prompt, temperature)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=concurrency
        ) as pool:
            answers = list(pool.map(is_accepted, pairs))
        _tally(audit, dict(enumerate(answers)), pairs)
    except Exception as exc:
        audit.error = f"{type(exc).__name__}: {exc}"
    results.append(audit)
    return results


def _sample_pairs(
    candidates: list[Candidate],
    key_of_insight: dict[str, str],
    insights: list[Insight],
    wanted: int,
    seed: int,
) -> list[tuple[str, str, bool]]:
    """Draw a balanced sample of labelled pairs from candidates and insights.

    Args:
        candidates: List of candidate clusters.
        key_of_insight: Mapping of insight ID to defect key.
        insights: List of existing insights.
        wanted: Desired total number of pairs.
        seed: Random seed for deterministic pair selection.

    Returns:
        List of `(candidate_label, insight_label, same_defect)` tuples.
    """
    rng = random.Random(seed)  # noqa: S311 - seeded for reproducible test data
    same: list[tuple[str, str, bool]] = []
    different: list[tuple[str, str, bool]] = []
    for candidate in candidates:
        if not candidate.defect_key:
            continue
        for insight in insights:
            is_same = key_of_insight[insight.insight_id] == candidate.defect_key
            pair = (candidate.cluster.label, insight.label, is_same)
            (same if is_same else different).append(pair)
    half = max(1, wanted // 2)
    return rng.sample(same, min(half, len(same))) + rng.sample(
        different, min(half, len(different))
    )


def _tally(
    audit: JudgeAudit,
    verdicts: dict[int, bool],
    pairs: list[tuple[str, str, bool]],
) -> None:
    """Record judge accuracy metrics on audit pairs.

    Args:
        audit: Target JudgeAudit instance to populate.
        verdicts: Mapping of pair index to judge verdict boolean.
        pairs: List of `(candidate_label, insight_label, same_defect)` tuples.
    """
    for index, (_, _, same_defect) in enumerate(pairs):
        accepted = verdicts.get(index, False)
        if same_defect:
            audit.same_pairs += 1
            audit.same_accepted += int(accepted)
        else:
            audit.different_pairs += 1
            audit.different_accepted += int(accepted)


def _call_json(prompt: str, schema: Any, temperature: float | None) -> str:
    """Call the match model via Gemini JSON API with schema enforcement.

    Args:
        prompt: Formatted prompt text string.
        schema: Expected response structure schema.
        temperature: Sampling temperature, or None for model default.

    Returns:
        Raw JSON response string.
    """
    from ambient_quality_agent.config import Model, config
    from ambient_quality_agent.tools import genai_json

    return (
        genai_json.call_gemini(
            prompt,
            schema,
            thinking_level=None,
            model=Model(model=config.insights_match_model),
            temperature=temperature,
        ).text
        or ""
    )


_PAIRWISE_FIRST_PART = (
    "Do these two agent-quality failure descriptions describe the same "
    "underlying issue?\nFirst: "
)
_PAIRWISE_SECOND_PART = "\nSecond: "
"""The pairwise question: one pair, one yes or no.

The alternative the shipped batched judge is measured against, so it is worded
here rather than imported. `insights.matching` words the shipped question, which asks
for a choice among many labels and has no pairwise form to borrow."""


def _build_pairwise_response_schema() -> Any:
    """Build response schema constraining output to `{"same_issue": bool}`.

    Returns:
        Configured Schema instance.
    """
    from google.genai import types

    return types.Schema(
        type=types.Type.OBJECT,
        properties={"same_issue": types.Schema(type=types.Type.BOOLEAN)},
        required=["same_issue"],
    )


def _call_pairwise_judge(prompt: str, temperature: float | None) -> str:
    """Query pairwise judge model and return raw JSON text.

    Args:
        prompt: Formatted pairwise comparison prompt.
        temperature: Optional sampling temperature override.

    Returns:
        Raw model response text string.
    """
    return _call_json(prompt, _build_pairwise_response_schema(), temperature)


def _is_pair_accepted(prompt: str, temperature: float | None) -> bool:
    """Query pairwise judge and determine whether it accepted the pair.

    Args:
        prompt: Formatted pairwise comparison prompt.
        temperature: Optional sampling temperature override.

    Returns:
        True if the judge verified that descriptions share the same issue.
    """
    try:
        decoded = json.loads(_call_pairwise_judge(prompt, temperature) or "")
    except Exception as exc:
        print(f"  warning: pairwise match call failed: {exc}", flush=True)
        return False
    return isinstance(decoded, dict) and decoded.get("same_issue") is True


def _scan_pairwise(
    candidate: str, labels: list[str], temperature: float | None
) -> int | None:
    """Scan labels in order and return the index of the first accepted label.

    Args:
        candidate: Candidate failure description string.
        labels: Ordered list of existing failure descriptions to compare against.
        temperature: Sampling temperature override, or None.

    Returns:
        Index of the first matching label, or None if none match.
    """
    for index, label in enumerate(labels):
        prompt = (
            _PAIRWISE_FIRST_PART + candidate + _PAIRWISE_SECOND_PART + label
        )
        if _is_pair_accepted(prompt, temperature):
            return index
    return None


def _run_thread_pool_matcher(
    store: Any,
    agent_name: str,
    candidates: list[Cluster],
    *,
    concurrency: int,
    outcome: MatcherOutcome,
    temperature: float | None,
) -> dict[int, str | None]:
    """Execute pairwise comparison using a thread pool against open insight labels.

    Args:
        store: BigQuery store instance.
        agent_name: Name of the agent whose open insights are matched.
        candidates: List of candidate clusters to match.
        concurrency: Max concurrent threads for judge queries.
        outcome: MatcherOutcome instance recording metrics.
        temperature: Sampling temperature override, or None.

    Returns:
        Mapping of candidate index to matched insight ID, or None.
    """
    rows = _list_open_labels(store, agent_name)
    if not rows or not candidates:
        return dict.fromkeys(range(len(candidates)))
    insight_ids = [insight_id for insight_id, _ in rows]
    labels = [label for _, label in rows]

    judged = 0
    counter_lock = threading.Lock()

    def scan(candidate: Cluster) -> int | None:
        nonlocal judged
        hit = _scan_pairwise(candidate.label, labels, temperature)
        with counter_lock:
            # A scan that stops at `hit` asked about the labels up to it.
            judged += len(labels) if hit is None else hit + 1
        return hit

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        hits = list(pool.map(scan, candidates))
    # What the scan actually spent. The SQL has no early exit, so this is the
    # number the refactor changes. One call per pair here, so they are equal.
    outcome.judged_pairs = judged
    outcome.calls = judged
    return {
        index: (insight_ids[hit] if hit is not None else None)
        for index, hit in enumerate(hits)
    }


def run_correlation(
    store_candidates: list[Candidate],
    candidates: list[Candidate],
    agent_name: str,
    *,
    keep: bool,
    store_size: int,
    new_frac: float,
    matchers: list[str],
    concurrency: int,
    seed: int,
    temperature: float | None = None,
    audit_pairs: int = 0,
) -> CorrelationResult:
    """Seed a store from earlier sweeps, then correlate a later sweep against it.

    The two sweeps cluster the same seeded defects under different RNG seeds, so
    a recurring defect reaches the judge worded the way a *second* production
    sweep would word it, rather than as a copy of the string already in the
    store. That is the question the judge exists to answer, and re-matching
    identical labels never asks it.

    Every matcher named runs over the same store and the same candidates, so
    their answers are comparable pair for pair.

    Args:
        store_candidates: Historic candidate clusters used to populate store.
        candidates: Current sweep candidate clusters being correlated.
        agent_name: Unique agent identifier string in BigQuery.
        keep: If True, retains seeded insights in BigQuery after testing.
        store_size: Total insights to seed (0 seeds one per defect).
        new_frac: Share of candidate defects excluded from store.
        matchers: List of matcher names to execute.
        concurrency: Max concurrent worker threads for matching.
        seed: Random seed for deterministic candidate selection.
        temperature: Sampling temperature override for matching model.
        audit_pairs: Number of pairwise evaluation pairs to audit.

    Returns:
        CorrelationResult containing latency, outcome counts, and audits.
    """
    from ambient_quality_agent.config import config
    from ambient_quality_agent.tools.insights.bigquery_store import (
        BigQueryInsightStore,
    )
    from ambient_quality_agent.tools.insights.models import mint_id
    from google.cloud import bigquery

    seeded, held_out = _plan_store(
        store_candidates, candidates, store_size, new_frac, seed
    )
    result = CorrelationResult(
        candidates=len(candidates),
        insights_in_store=len(seeded),
        defects_in_store=len({c.defect_key for c in seeded}),
        defects_held_out=len(held_out),
    )
    client = bigquery.Client(
        project=config.project_id, location=config.aqa_dataset_location
    )
    store = BigQueryInsightStore(
        client=client,
        project_id=config.project_id,
        dataset=config.aqa_dataset,
        agent_name=agent_name,
    )
    run_id = f"loadtest-{uuid.uuid4().hex[:8]}"
    now = dt.datetime.now(dt.UTC)
    # Staggered by a second per insight, oldest first, so "the newest insight
    # for this defect" is a fact rather than a coin toss. Production ties them
    # -- `_mark_recurring` stamps one timestamp across a whole sweep -- and a
    # tied store cannot show which insight a matcher preferred.
    insights = [
        Insight(
            insight_id=mint_id(),
            agent_name=agent_name,
            label=entry.cluster.label,
            status=InsightStatus.NEW,
            created_at=now,
            updated_at=now - dt.timedelta(seconds=len(seeded) - position),
        )
        for position, entry in enumerate(seeded)
    ]
    occurrences = [
        InsightOccurrence(
            occurrence_id=mint_id(),
            insight_id=insight.insight_id,
            occurrence_state=OccurrenceState.TRACKED,
            run_id=run_id,
            created_at=now,
            agent_name=agent_name,
            label=entry.cluster.label,
            item_count=entry.cluster.item_count,
            # The evidence `attach_examples` produced, traces and all, so the
            # load job writes the payload a real occurrence carries.
            rubrics=entry.cluster.examples,
        )
        for entry, insight in zip(seeded, insights, strict=True)
    ]
    key_of_insight = {
        insight.insight_id: entry.defect_key
        for entry, insight in zip(seeded, insights, strict=True)
    }
    # `updated_at` climbs with position, so the last insight seeded for a defect
    # is its newest one.
    newest_of_key = {
        entry.defect_key: insight.insight_id
        for entry, insight in zip(seeded, insights, strict=True)
    }

    clusters = [c.cluster for c in candidates]
    try:
        started = time.monotonic()
        store.save_investigation_result(insights, [], [], occurrences, now)
        result.seed_latency_s = time.monotonic() - started

        for matcher in matchers:
            outcome = MatcherOutcome(matcher=matcher)
            try:
                started = time.monotonic()
                if matcher == "production":
                    matches = store.find_existing_insights(clusters)
                    # The store reports no counters of its own, so the pairs are
                    # what it was given rather than what it spent.
                    outcome.judged_pairs = len(clusters) * len(insights)
                else:
                    matches = _run_thread_pool_matcher(
                        store,
                        agent_name,
                        clusters,
                        concurrency=concurrency,
                        outcome=outcome,
                        temperature=temperature,
                    )
                outcome.latency_s = time.monotonic() - started
                _score_matches(
                    outcome, candidates, matches, key_of_insight, newest_of_key
                )
            except Exception as exc:
                outcome.error = f"{type(exc).__name__}: {exc}"
            result.outcomes.append(outcome)

        if audit_pairs:
            result.audits = _audit_pairs(
                store,
                _sample_pairs(
                    candidates, key_of_insight, insights, audit_pairs, seed
                ),
                concurrency,
                temperature,
            )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if not keep:
            # Deletes this run's occurrences and then the insights left without
            # any -- which is all of them, since the run minted them itself.
            store.delete_failed_insights(run_id)
    return result


def _render_clustering_table(attempts: list[ClusteringAttempt]) -> str:
    """Render the clustering results table with one row per attempt.

    Args:
        attempts: List of completed ClusteringAttempt instances.

    Returns:
        Formatted multi-line text table.
    """
    header = (
        f"{'size':>6} {'#':>2} {'json':>9} {'finish':>26} {'in tok':>8} "
        f"{'answer':>8} {'think':>8} {'chars':>8} {'rubrics':>8} {'tools':>6} "
        f"{'chunks':>7} {'pre':>5} {'clusters':>8} {'kept':>5} {'evid':>5} "
        f"{'ids':>6} {'stop':>6} {'inv':>4} {'dup':>4} {'purity':>7} "
        f"{'defects':>7} {'secs':>6}"
    )
    lines = [header, "-" * len(header)]
    for attempt in attempts:
        if attempt.error:
            lines.append(
                f"{attempt.size:>6} {attempt.attempt:>2} ERROR {attempt.error}"
            )
            continue
        lines.append(
            f"{attempt.size:>6} {attempt.attempt:>2} {attempt.json_status:>9} "
            f"{attempt.finish_reason[-26:]:>26} {attempt.prompt_tokens:>8} "
            f"{attempt.answer_tokens:>8} {attempt.thought_tokens:>8} "
            f"{attempt.response_chars:>8} "
            f"{attempt.extracted_rubrics:>8} {attempt.tools_recovered:>6} "
            f"{attempt.chunks:>7} {attempt.clusters_before_merge:>5} "
            f"{attempt.parsed_clusters:>8} {attempt.validated_clusters:>5} "
            f"{attempt.examples_attached:>5} "
            f"{attempt.assigned_ids:>6} "
            f"{attempt.stopped_after or '-':>6} {attempt.invented_ids:>4} "
            f"{attempt.duplicate_ids:>4} {attempt.purity:>7.2f} "
            f"{attempt.defects_recovered:>7} {attempt.latency_s:>6.1f}"
        )
    return "\n".join(lines)


def _render_clustering_verdict(attempts: list[ClusteringAttempt]) -> str:
    """Render summary lines describing usable responses and rubric coverage per size.

    Args:
        attempts: List of completed ClusteringAttempt instances.

    Returns:
        Formatted summary text lines.
    """
    lines = []
    for size in sorted({a.size for a in attempts}):
        group = [a for a in attempts if a.size == size]
        good = [a for a in group if a.ok]
        statuses = ", ".join(
            f"{status}x{sum(1 for a in group if a.json_status == status)}"
            for status in sorted(
                {a.json_status for a in group if a.json_status}
            )
        )
        errors = sum(1 for a in group if a.error)
        coverage = (
            statistics.mean(a.assigned_ids / size for a in good)
            if good
            else 0.0
        )
        worst = min((a.assigned_ids / size for a in good), default=0.0)
        stopped = sorted(a.stopped_after for a in good if a.stopped_after)
        line = (
            f"  {size:>6} rubrics: {len(good)}/{len(group)} usable "
            f"({statuses}{f', {errors} call error(s)' if errors else ''}); "
            f"clustered {coverage:.0%} of the corpus (worst run {worst:.0%})"
        )
        if stopped:
            line += f"; stopped after {', '.join(str(s) for s in stopped)}"
        lines.append(line)
    return "\n".join(lines)


def _render_correlation_report(result: CorrelationResult) -> str:
    """Render correlation evaluation report with store statistics and judge outcomes.

    Args:
        result: Completed CorrelationResult instance.

    Returns:
        Formatted correlation report string.
    """
    if result.error:
        return f"ERROR {result.error}"
    lines = [
        f"{result.candidates} candidate(s) vs {result.insights_in_store} insight(s)"
        f" covering {result.defects_in_store} defect(s),"
        f" {result.defects_held_out} held out"
        f" (seed write {result.seed_latency_s:.1f}s)",
        f"    {'judge':>11} {'secs':>7} {'pairs':>7} {'calls':>6} |"
        f" {'matched':>8} {'minted':>7} |"
        f" {'duplicate':>10} {'mislinked':>10} {'swallowed':>10} | {'stale':>6}",
    ]
    for outcome in result.outcomes:
        if outcome.error:
            lines.append(f"    {outcome.matcher:>11} ERROR {outcome.error}")
            continue
        lines.append(
            f"    {outcome.matcher:>11} {outcome.latency_s:>7.1f}"
            f" {outcome.judged_pairs:>7} {outcome.calls or '-':>6} |"
            f" {outcome.matched:>8} {outcome.minted:>7} |"
            f" {outcome.duplicate:>10} {outcome.mislinked:>10}"
            f" {outcome.swallowed:>10} | {outcome.stale:>6}"
        )
    for audit in result.audits:
        if audit.error:
            lines.append(f"    {audit.judge:>11} AUDIT ERROR {audit.error}")
            continue
        recall = audit.same_accepted / (audit.same_pairs or 1)
        false_rate = audit.different_accepted / (audit.different_pairs or 1)
        lines.append(
            f"    {audit.judge:>11} per-pair: accepts"
            f" {audit.same_accepted}/{audit.same_pairs} ({recall:.0%}) of one defect,"
            f" {audit.different_accepted}/{audit.different_pairs} ({false_rate:.0%})"
            f" of two"
        )
    return "\n".join(lines)


MATCHERS = ("production", "thread_pool")
"""The judges `--matchers` can name. Every other name is refused, because the
dispatch in `run_correlation` treats anything but `production` as the scan."""


def _split_matchers(value: str) -> list[str]:
    """Split the `--matchers` value into matcher names.

    Args:
        value: Comma-separated matcher names.

    Returns:
        The non-empty names, in the order given.
    """
    return [m.strip() for m in value.split(",") if m.strip()]


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments for insights load testing.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sizes",
        default="250,1000,2500,5000",
        help="Comma-separated corpus sizes (failed rubrics per clustering call).",
    )
    parser.add_argument(
        "--repeats", type=int, default=3, help="Clustering calls per size."
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help=(
            "Clustering calls in flight at once. Above 1 this also exercises"
            " the model's rate limits, which surface as call errors, not as"
            " bad JSON -- keep it at 1 to measure response quality alone."
        ),
    )
    parser.add_argument("--seed", type=int, default=7, help="Corpus RNG seed.")
    parser.add_argument(
        "--fake-model",
        action="store_true",
        help=(
            "Answer from the ground truth instead of calling Gemini. Checks the"
            " pipeline's plumbing in seconds and measures nothing about the"
            " model; rows come back marked `fake`."
        ),
    )
    parser.add_argument(
        "--out-dir",
        default="scratch/insights_load_test",
        help="Where the summary JSON and every raw response are written.",
    )
    parser.add_argument(
        "--no-correlate",
        action="store_true",
        help="Skip the BigQuery matching stage and only run clustering.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Leave the throwaway agent's insights in BigQuery for inspection.",
    )
    parser.add_argument(
        "--matchers",
        default="production,thread_pool",
        help=(
            "Judges to run over the same store and candidates, comma-separated:"
            " `production` is `find_existing_insights` end to end, `thread_pool`"
            " a scan of pairwise calls."
        ),
    )
    parser.add_argument(
        "--store-size",
        type=int,
        default=0,
        help=(
            "Insights to seed, independent of the candidate count. Distinct"
            " defects are seeded first and the surplus spent on alternative"
            " wordings of defects already present. 0 seeds one per defect."
        ),
    )
    parser.add_argument(
        "--new-frac",
        type=float,
        default=0.25,
        help=(
            "Share of the candidates' defects kept out of the store, so those"
            " candidates match nothing and pay a full scan. At 0 every"
            " candidate recurs, which is the cheap case for a scanning matcher."
        ),
    )
    parser.add_argument(
        "--audit-pairs",
        type=int,
        default=0,
        help=(
            "Also put this many labelled (candidate, insight) pairs to both"
            " judges directly, half of them one defect and half two, to compare"
            " the judges apart from the scan around them. 0 skips it."
        ),
    )
    parser.add_argument(
        "--match-temperature",
        type=float,
        default=None,
        help=(
            "Sampling temperature for the `thread_pool` judge. Unset leaves the"
            " model's default from `call_gemini`."
        ),
    )
    parser.add_argument(
        "--match-concurrency",
        type=int,
        default=0,
        help="Candidates judged at once by `thread_pool`. 0 uses its default.",
    )
    args = parser.parse_args()
    unknown = set(_split_matchers(args.matchers)) - set(MATCHERS)
    if unknown:
        parser.error(
            f"unknown --matchers {', '.join(sorted(unknown))};"
            f" choose from {', '.join(MATCHERS)}"
        )
    return args


def main() -> None:
    args = _parse_args()
    sizes = [int(part) for part in args.sizes.split(",") if part.strip()]
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from ambient_quality_agent.config import config

    print(
        f"clustering model: {config.insights_model.model} "
        f"@ {config.insights_model.location}, project {config.project_id}"
    )
    print(
        f"sizes: {sizes}, repeats: {args.repeats}, concurrency: {args.concurrency}\n"
    )

    jobs = [
        (size, index, args.seed + index)
        for size in sizes
        for index in range(args.repeats)
    ]
    attempts: list[ClusteringAttempt] = []
    # Every successful sweep is kept, not just the first per size: correlation
    # needs one sweep to populate the store and a *different* one to correlate
    # against it, and the spares are what lets the store grow past one sweep's
    # worth of insights.
    sweeps: dict[tuple[int, int], list[Candidate]] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=args.concurrency
    ) as pool:
        futures = {
            pool.submit(run_clustering, size, index, seed, args.fake_model): (
                size,
                index,
            )
            for size, index, seed in jobs
        }
        for future in concurrent.futures.as_completed(futures):
            attempt, clusters = future.result()
            attempts.append(attempt)
            if attempt.ok:
                sweeps[attempt.size, attempt.attempt] = clusters
            print(
                f"  done: {attempt.size} rubrics #{attempt.attempt} -> "
                f"{attempt.json_status or attempt.error}",
                flush=True,
            )

    attempts.sort(key=lambda a: (a.size, a.attempt))
    for attempt in attempts:
        if attempt.raw_response:
            name = f"response_{attempt.size}_{attempt.attempt}_{attempt.json_status}.json"
            (out_dir / name).write_text(attempt.raw_response, encoding="utf-8")

    print("\n== clustering ==")
    print(_render_clustering_table(attempts))
    print("\n== verdict ==")
    print(_render_clustering_verdict(attempts))

    correlations: dict[int, CorrelationResult] = {}
    if not args.no_correlate and sweeps:
        matchers = _split_matchers(args.matchers)
        agent_name = f"loadtest-{uuid.uuid4().hex[:8]}"
        print(f"\n== correlation (agent {agent_name}) ==")
        print(
            f"match model: {config.insights_match_model}, "
            f"dataset {config.aqa_dataset} @ {config.aqa_dataset_location}"
        )
        if len(sweeps) < 2:
            print(
                "  WARNING: one sweep only -- the store and the candidates are"
                " the same clusters, so every label matches itself verbatim."
                " Raise --repeats to judge independently worded labels."
            )
        for size in sorted({size for size, _ in sweeps}):
            # The last attempt at this size is the sweep being correlated; every
            # other sweep, at any size, is history the store may be built from.
            attempt_indices = sorted(i for s, i in sweeps if s == size)
            candidates = sweeps[size, attempt_indices[-1]]
            history = [
                entry
                for key, clusters in sweeps.items()
                if key != (size, attempt_indices[-1])
                for entry in clusters
            ] or candidates
            result = run_correlation(
                history,
                candidates,
                agent_name,
                keep=args.keep,
                store_size=args.store_size,
                new_frac=args.new_frac,
                matchers=matchers,
                concurrency=args.match_concurrency or MATCH_CONCURRENCY,
                seed=args.seed,
                temperature=args.match_temperature,
                audit_pairs=args.audit_pairs,
            )
            correlations[size] = result
            print(f"\n  {size} rubrics: {_render_correlation_report(result)}")

    summary = {
        "model": config.insights_model.model,
        "match_model": config.insights_match_model,
        "sizes": sizes,
        "repeats": args.repeats,
        "concurrency": args.concurrency,
        "seed": args.seed,
        "store_size": args.store_size,
        "new_frac": args.new_frac,
        "clustering": [
            {
                k: v
                for k, v in dataclasses.asdict(a).items()
                if k != "raw_response"
            }
            for a in attempts
        ],
        "correlation": {
            str(size): dataclasses.asdict(result)
            for size, result in correlations.items()
        },
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"\nwrote {summary_path} and {len(attempts)} raw response(s) to {out_dir}/"
    )


if __name__ == "__main__":
    main()
