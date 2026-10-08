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

"""Deciding which existing insight, if any, a candidate cluster restates.

The same-issue judge, and the only place the question is worded.

Called by `InsightStore.find_existing_insights` on the sweep's write path.

## One choice among many, not many yes/no questions

What one candidate costs:

1. Take the open labels, most recently updated first.
2. Split them into batches of `MATCH_BATCH`.
3. Send one call per batch: the candidate, the numbered labels, and "which of
   these, if any?".
4. Stop at the first batch that names one. Answer `None` if none does.

Cost per candidate:

- one call at best,
- `|store| / MATCH_BATCH` calls at worst,
- the answer is the best match among the newest labels holding one.

## Why not one yes/no question per pair

The obvious alternative. It is measurably worse.

A per-pair error rate compounds across the store. A candidate that matches
nothing gets one chance to be wrongly accepted per label it is compared against.

- Per pair, both forms of the judge score alike: 98% recall, 4-6% false accepts.
- Scanning a 40-insight store pairwise turned that into 17 of 18 genuinely new
  defects absorbed into unrelated insights. Nothing surfaces them again.
- Offering the labels together makes it one decision. The options compete.
  "none of these" becomes an answer the model gives, not the absence of one.
"""

from __future__ import annotations

import json
import logging

# Imported at runtime, not under TYPE_CHECKING: `Matcher` below is a plain
# assignment, so it is evaluated when the module loads. `type Matcher = ...`
# would be lazy and would not need this, but it is 3.12 and the floor is 3.11.
from collections.abc import Callable, Sequence
from concurrent import futures
from typing import Any

logger = logging.getLogger(__name__)

MATCH_BATCH = 20
"""Existing labels offered in one call.

Bounds the prompt. Costs no short-circuit: a candidate matching something in the
newest batch never sees the rest of the store."""

MATCH_CONCURRENCY = 8
"""Candidates judged at once.

They are independent questions, and each spends its time waiting. A sweep
against a populated store is only bearable in parallel."""

Matcher = Callable[[Sequence[str], Sequence[str]], dict[int, int | None]]
"""What a store puts the same-issue question to: `match_candidates`'s signature.

Called as ``matcher(candidate_labels, existing_labels)``, the existing ones
most-recently-updated first, answering ``{candidate index: index into existing,
or None}``.

A whole batch of candidates rather than one at a time, because the judge decides
how to spend its calls: `match_candidates` runs them in parallel and applies a
failure policy across the set, and a per-candidate seam would put both of those
back on every caller. A store injects one so a tuning run can trade the deployed
judge for a free deterministic one without the store knowing which it has.
"""

_PROMPT_CANDIDATE = (
    "Which of the numbered agent-quality failure descriptions, if any, "
    "describes the same underlying issue as the candidate?\nCandidate: "
)
_PROMPT_OPTIONS = "\nNumbered descriptions:\n"
_PROMPT_TASK = (
    "\nAnswer with the number of the one that describes the same underlying "
    "issue, or -1 if none of them does."
)

NO_MATCH = -1
"""The answer for "none of these".

An out-of-band integer, not a null. A nullable response-schema field is one more
thing for a small model to get wrong, and the range check rejects it anyway."""


class MatchUnavailable(RuntimeError):
    """No judge call for a candidate produced a verdict.

    Raised instead of answering "no match". An unreachable judge is a failure,
    not a sweep in which every defect happens to look new.
    """


def build_match_response_schema() -> Any:
    """Constrain the judge's answer to ``{"match": int}``.

    Returns:
        Schema object enforcing an integer match field.
    """
    from google.genai import types

    return types.Schema(
        type=types.Type.OBJECT,
        properties={"match": types.Schema(type=types.Type.INTEGER)},
        required=["match"],
    )


def build_prompt(candidate: str, options: Sequence[str]) -> str:
    """The question put to the model, for one candidate against one batch.

    Args:
        candidate: Candidate defect description.
        options: One batch of existing insight labels, numbered in the prompt.

    Returns:
        Rendered prompt string.
    """
    numbered = "\n".join(
        f"{index}. {label}" for index, label in enumerate(options)
    )
    return (
        _PROMPT_CANDIDATE
        + candidate
        + _PROMPT_OPTIONS
        + numbered
        + _PROMPT_TASK
    )


def call_match_model(prompt: str) -> str:
    """Ask `Config.insights_match_model` and return its raw JSON text.

    Args:
        prompt: Formatted matching prompt.

    Returns:
        Raw JSON response string from the model.
    """
    from ambient_quality_agent.config import Model, config
    from ambient_quality_agent.tools import genai_json

    return (
        genai_json.call_gemini(
            prompt,
            build_match_response_schema(),
            # A thinking level is a Gemini 3 field that earlier models reject
            # outright, and the operator chooses this model.
            thinking_level=None,
            model=Model(model=config.insights_match_model),
        ).text
        or ""
    )


def choose_match(
    candidate: str,
    existing: Sequence[str],
    *,
    model_call: Callable[[str], str] | None = None,
    batch: int = MATCH_BATCH,
) -> int | None:
    """Index of the existing label ``candidate`` restates, or ``None``.

    Steps:

    1. Split ``existing`` into batches of ``batch``, in the order given.
    2. Ask the judge which label in the batch the candidate restates.
    3. Return on the first batch that names one.
    4. Return ``None`` once the batches run out.

    A batch whose call fails is skipped. Its labels go unconsidered; the batches
    after it are still asked.

    Args:
        candidate: The candidate cluster's label.
        existing: Open insight labels, most recently updated first. This order is
            the tie-break -- the first batch to answer wins, so a match among
            newer labels beats one further down.
        model_call: Prompt-to-text callable; `call_match_model` when omitted.
        batch: Labels offered per call.

    Returns:
        The position in ``existing``, or ``None`` for "this is a new issue".

    Raises:
        MatchUnavailable: If no call answered at all. An unreachable judge must
            not report every candidate as new.
    """
    call = model_call or call_match_model
    failures: list[Exception] = []
    answered = False
    for start in range(0, len(existing), batch):
        window = existing[start : start + batch]
        try:
            decoded = json.loads(call(build_prompt(candidate, window)) or "")
        except Exception as exc:
            # One batch lost costs its labels, not the candidate. A match may
            # well be in a later batch.
            logger.warning("insights: match call failed for a batch: %s", exc)
            failures.append(exc)
            continue
        answered = True
        hit = decoded.get("match") if isinstance(decoded, dict) else None
        if not isinstance(hit, int):
            logger.warning(
                "insights: match response carried no integer choice."
            )
            continue
        if 0 <= hit < len(window):
            return start + hit
        if hit != NO_MATCH:
            logger.warning(
                "insights: match response chose %d, out of range.", hit
            )
    if failures and not answered:
        raise MatchUnavailable(
            f"no judge call answered for candidate {candidate!r}"
        ) from failures[0]
    if failures:
        logger.warning(
            "insights: %d batch(es) went unjudged for candidate %r.",
            len(failures),
            candidate,
        )
    return None


def match_candidates(
    candidates: Sequence[str],
    existing: Sequence[str],
    *,
    model_call: Callable[[str], str] | None = None,
    batch: int = MATCH_BATCH,
    concurrency: int = MATCH_CONCURRENCY,
) -> dict[int, int | None]:
    """Judge every candidate against ``existing``, in parallel.

    Failure policy, the same one `clustering.cluster_and_label` applies to its
    chunks:

    - One candidate the judge could not answer for is isolated. It is reported
      as a new issue, which mints an insight -- visible, and correctable.
    - Every candidate failing is not a result. Answering "all new" against a
      populated store would duplicate the whole of it, so the error is raised.

    Args:
        candidates: Candidate labels, keyed by position in the result.
        existing: Open insight labels, most recently updated first.
        model_call: Prompt-to-text callable; `call_match_model` when omitted.
        batch: Labels offered per call.
        concurrency: Candidates judged at once.

    Returns:
        ``{candidate index: index into existing, or None}``.

    Raises:
        Exception: Whatever the judge raised, if it answered for no candidate.
    """
    if not candidates or not existing:
        return dict.fromkeys(range(len(candidates)))

    def judge(candidate: str) -> tuple[int | None, Exception | None]:
        try:
            return choose_match(
                candidate, existing, model_call=model_call, batch=batch
            ), None
        except Exception as exc:
            return None, exc

    with futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(judge, candidates))

    failures = [exc for _, exc in results if exc is not None]
    if failures:
        logger.warning(
            "insights: %d of %d candidate(s) could not be judged.",
            len(failures),
            len(candidates),
        )
        if len(failures) == len(candidates):
            raise failures[0]
    return {index: hit for index, (hit, _) in enumerate(results)}
