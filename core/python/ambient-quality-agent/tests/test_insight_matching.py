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

"""Tests for the same-issue judge behind insight correlation.

No call reaches Gemini. Every test injects a ``prompt -> text`` callable.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from ambient_quality_agent.config import DEFAULT_LLM_LOCATION, Model
from ambient_quality_agent.tools.insights import matching

LABELS = [
    "made no tool call",
    "answered from memory",
    "closed the wrong support case",
]


def answering(choice: int) -> Any:
    """Creates a mock judge function returning `choice`.

    Args:
        choice: Predetermined index to return in match payload.

    Returns:
        Callable mock model function with recorded `prompts` list.
    """
    prompts: list[str] = []

    def call(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({"match": choice})

    call.prompts = prompts  # type: ignore[attr-defined]
    return call


# --------------------------------------------------------------------------- #
# choose                                                                       #
# --------------------------------------------------------------------------- #


def test_the_judge_is_offered_every_label_in_the_batch_at_once() -> None:
    """One decision among the options, not one yes/no per option.

    That is the whole design. A pairwise judge cannot decline, because it makes
    each answer without sight of the alternatives."""
    call = answering(1)

    assert matching.choose_match("some candidate", LABELS, model_call=call) == 1

    (prompt,) = call.prompts
    assert prompt.count("some candidate") == 1
    for index, label in enumerate(LABELS):
        assert f"{index}. {label}" in prompt


def test_minus_one_means_none_of_them() -> None:
    """The answer a scan of independent yes/no questions cannot express."""
    assert (
        matching.choose_match("a new issue", LABELS, model_call=answering(-1))
        is None
    )


def test_a_choice_outside_the_batch_is_no_choice() -> None:
    """A model naming a label it was not offered answered about something that
    is not there. Its number would index an unrelated insight."""
    assert (
        matching.choose_match("candidate", LABELS, model_call=answering(7))
        is None
    )


def test_the_answer_is_an_index_into_the_whole_list_not_the_batch() -> None:
    """Batching is an implementation detail of the call.

    The second batch's first option is index 2, not index 0."""
    calls: list[str] = []

    def call(prompt: str) -> str:
        calls.append(prompt)
        # Decline the first batch, take the first option of the second.
        return json.dumps({"match": -1 if len(calls) == 1 else 0})

    assert (
        matching.choose_match("candidate", LABELS, model_call=call, batch=2)
        == 2
    )


def test_the_first_batch_to_answer_wins() -> None:
    """Labels arrive newest-first.

    Stopping at the first batch that matches prefers a recent insight to an
    older one, and never asks about the rest."""
    calls: list[str] = []

    def call(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"match": 0})

    assert (
        matching.choose_match("candidate", LABELS, model_call=call, batch=2)
        == 0
    )
    assert len(calls) == 1


def test_a_failed_batch_costs_its_labels_and_not_its_candidate() -> None:
    """One batch lost is a few labels unconsidered.

    Abandoning the candidate instead would mint an insight for a defect the next
    batch may well hold."""
    calls: list[str] = []

    def call(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return json.dumps({"match": 0})

    assert (
        matching.choose_match("candidate", LABELS, model_call=call, batch=2)
        == 2
    )


def test_a_candidate_no_call_answered_for_raises_rather_than_minting() -> None:
    """Every call failing is not the verdict "this issue is new".

    Answering `None` would mint a duplicate of an insight the store already
    holds, with nothing but a log line to say why."""

    def call(prompt: str) -> str:
        del prompt
        raise RuntimeError("judge unreachable")

    with pytest.raises(matching.MatchUnavailable):
        matching.choose_match("candidate", LABELS, model_call=call)


def test_an_unparseable_answer_is_a_decline_not_a_failure() -> None:
    """The model answered, just not usefully.

    That is evidence of nothing. It is also not the transport failure
    `MatchUnavailable` reports."""
    assert (
        matching.choose_match("candidate", LABELS, model_call=lambda _: "{}")
        is None
    )


# --------------------------------------------------------------------------- #
# match_candidates                                                             #
# --------------------------------------------------------------------------- #


def test_an_empty_store_matches_nothing_without_asking() -> None:
    def forbidden(prompt: str) -> str:
        raise AssertionError("the judge was called against an empty store")

    assert matching.match_candidates(["a", "b"], [], model_call=forbidden) == {
        0: None,
        1: None,
    }


def test_candidates_are_judged_concurrently() -> None:
    """They are independent questions, and each spends its time waiting.

    A sweep against a populated store is only bearable in parallel."""
    barrier = threading.Barrier(3, timeout=10)

    def call(prompt: str) -> str:
        # Deadlocks unless three calls are genuinely in flight together.
        barrier.wait()
        return json.dumps({"match": 0})

    hits = matching.match_candidates(
        ["a", "b", "c"], LABELS, model_call=call, concurrency=3
    )
    assert hits == {0: 0, 1: 0, 2: 0}


def test_one_unjudgeable_candidate_is_isolated() -> None:
    """It mints an insight, which is visible and correctable.

    Failing the whole sweep instead would lose the other candidates' results
    too."""

    def call(prompt: str) -> str:
        if "explodes" in prompt:
            raise RuntimeError("transient")
        return json.dumps({"match": 1})

    hits = matching.match_candidates(
        ["fine", "explodes"], LABELS, model_call=call
    )
    assert hits == {0: 1, 1: None}


def test_a_judge_that_answers_for_nobody_raises() -> None:
    """Reporting "all new" against a populated store would duplicate every
    insight in it. Same policy as `clustering.cluster_and_label`."""

    def call(prompt: str) -> str:
        del prompt
        raise RuntimeError("judge unreachable")

    with pytest.raises(matching.MatchUnavailable) as raised:
        matching.match_candidates(["a", "b"], LABELS, model_call=call)
    # The transport failure is kept as the cause, so a log carries the reason
    # rather than just "no verdict".
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert "judge unreachable" in str(raised.value.__cause__)


# --------------------------------------------------------------------------- #
# call_match_model                                                           #
# --------------------------------------------------------------------------- #


def test_the_judge_runs_the_configured_match_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`insights_match_model` is the cheap model chosen for this question, and
    the one an operator configures.

    Falling back to `call_gemini`'s default would silently run the clustering
    model instead."""
    from .conftest import make_config

    captured: dict[str, Any] = {}

    class _FakeModels:
        def generate_content(
            self, *, model: str, contents: str, config: Any
        ) -> Any:
            del contents
            captured["model"] = model
            captured["thinking_config"] = config.thinking_config
            return SimpleNamespace(text='{"match": -1}')

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["client_kwargs"] = kwargs
            self.models = _FakeModels()

    monkeypatch.setattr(
        "ambient_quality_agent.config.config",
        make_config(
            project_id="cfg-project",
            insights_model=Model(
                model="clustering-model", location="cfg-location"
            ),
            insights_match_model="match-model",
        ),
    )
    monkeypatch.setattr("google.genai.Client", _FakeClient)

    assert matching.call_match_model("prompt") == '{"match": -1}'

    assert captured["model"] == "match-model"
    assert captured["client_kwargs"]["project"] == "cfg-project"
    # `insights_match_model` carries no region of its own, so the judge serves
    # from the default LLM location like every other model role.
    assert captured["client_kwargs"]["location"] == DEFAULT_LLM_LOCATION
    # A thinking level is a Gemini 3 field that earlier models reject outright,
    # and the model here is the operator's choice.
    assert captured["thinking_config"] is None
