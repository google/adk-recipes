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

"""Tests for finding clustering (offline, no GCP).

Drives clustering from hand-written `Finding`s -- the source-neutral input the
node hands it -- so the eval-page shape stays in `test_findings_adapter`. Covers
the chunked LLM call, the prompt it builds, grounding validation, and
evidence attachment.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from ambient_quality_agent.tools import genai_json
from ambient_quality_agent.tools.insights import clustering
from ambient_quality_agent.tools.insights.clustering import (
    MAX_EXAMPLES,
    Cluster,
)
from ambient_quality_agent.tools.insights.findings import Finding
from google.genai import types as genai_types

# --- builders -----------------------------------------------------------------


def _finding(
    expected: str = "e", actual: str = "a", session_id: str = ""
) -> Finding:
    return Finding(
        expected_behavior=expected,
        actual_behavior=actual,
        session_id=session_id,
    )


def _findings(count: int) -> list[Finding]:
    return [_finding() for _ in range(count)]


def _fake_call(payload: Any) -> Any:
    """Creates a mock model call returning ``payload``.

    A cluster's id list is returned under the ``finding_ids`` key, matching the
    prompt and response schema contract.

    ``call.prompts`` records incoming prompts from worker threads in completion
    order.

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


# --- the prompt ---------------------------------------------------------------


def test_the_prompt_carries_no_tool_declarations() -> None:
    """The prompt is the instructions and the tuples and nothing else, and a
    tuple is its id and the two behavior strings -- so no agent's declared
    tool names, schemas or parameter lists ride along, whatever the tuples
    themselves say about a tool."""
    call = _fake_call({"clusters": []})

    clustering.cluster_and_label(
        [
            _finding("calls create_incident", "called nothing"),
            _finding("passes a severity", "passed none"),
        ],
        model_call=call,
    )

    instructions, tuples = call.prompts[0].split(clustering._TUPLES_HEADER)
    assert instructions == clustering._CLUSTER_INSTRUCTIONS
    assert {tuple(sorted(entry)) for entry in json.loads(tuples)} == {
        ("actual_behavior", "expected_behavior", "id")
    }


# --- chunking -----------------------------------------------------------------


def test_a_sweep_is_clustered_in_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One call per `CHUNK_SIZE` findings, so a response only ever has to carry
    its own chunk's ids -- the thing that runs out on a large sweep."""
    monkeypatch.setattr(clustering, "CHUNK_SIZE", 2)
    call = _fake_call(
        {"clusters": [{"label": "made no tool call", "finding_ids": [0]}]}
    )

    clustering.cluster_and_label(_findings(5), model_call=call)

    assert len(call.prompts) == 3
    # Every finding reached exactly one chunk, and none was dropped to fit.
    for finding_id in range(5):
        assert sum(f'"id": {finding_id},' in p for p in call.prompts) == 1


def test_a_findings_id_is_its_global_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunking must not restart ids: a finding's id is its index in the whole
    list, so a later chunk carries the offset positions, not 0 again."""
    monkeypatch.setattr(clustering, "CHUNK_SIZE", 2)
    call = _fake_call({"clusters": []})

    clustering.cluster_and_label(_findings(5), model_call=call)

    def chunk_ids(prompt: str) -> list[int]:
        payload = prompt.rsplit(clustering._TUPLES_HEADER, maxsplit=1)[-1]
        return [entry["id"] for entry in json.loads(payload)]

    # Sorted because the chunks partition 0..4, so their sorted order is unique
    # and independent of which chunk finished first. Ids that restarted per
    # chunk would read [[0], [0, 1], [0, 1]].
    assert sorted(chunk_ids(p) for p in call.prompts) == [[0, 1], [2, 3], [4]]


def test_chunk_clusters_are_numbered_across_the_whole_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`merge_clusters` names candidates by `cluster_id`, so ids that restarted
    per chunk would make two different candidates share one."""
    monkeypatch.setattr(clustering, "CHUNK_SIZE", 1)
    call = _fake_call(
        {"clusters": [{"label": "made no tool call", "finding_ids": [0]}]}
    )

    clusters, _ = clustering.cluster_and_label(_findings(3), model_call=call)

    assert [c.cluster_id for c in clusters] == [0, 1, 2]


def test_one_unusable_chunk_does_not_cost_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated chunk is findings lost from that chunk, not a failed sweep."""
    monkeypatch.setattr(clustering, "CHUNK_SIZE", 1)
    responses = iter(
        [
            json.dumps({"clusters": [{"label": "first", "finding_ids": [0]}]}),
            "{ truncated",
            json.dumps({"clusters": [{"label": "third", "finding_ids": [2]}]}),
        ]
    )

    def call(prompt: str) -> str:
        return next(responses)

    clusters, _ = clustering.cluster_and_label(_findings(3), model_call=call)

    assert [c.label for c in clusters] == ["first", "third"]


def test_a_failed_call_does_not_cost_the_chunks_that_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The calls are minutes long and fail on infrastructure often enough that
    raising would routinely discard a sweep's completed chunks."""
    monkeypatch.setattr(clustering, "CHUNK_SIZE", 1)
    responses = iter(
        [json.dumps({"clusters": [{"label": "first", "finding_ids": [0]}]})]
    )

    def call(prompt: str) -> str:
        try:
            return next(responses)
        except StopIteration:
            raise RuntimeError("503 UNAVAILABLE") from None

    clusters, findings_skipped_to_errors = clustering.cluster_and_label(
        _findings(3), model_call=call
    )

    assert [c.label for c in clusters] == ["first"]
    # Two single-finding chunks raised, so their two findings are the error count.
    assert findings_skipped_to_errors == 2


def test_every_chunk_failing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sweep whose every chunk failed is broken, not degraded: it raises so
    the orchestrator retries rather than silently recording nothing."""
    monkeypatch.setattr(clustering, "CHUNK_SIZE", 1)

    def call(prompt: str) -> str:
        raise RuntimeError("model unavailable")

    with pytest.raises(RuntimeError, match="model unavailable"):
        clustering.cluster_and_label(_findings(2), model_call=call)


def test_chunks_are_clustered_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chunks are independent, and serializing them sequentially causes
    excessive latency on large sweeps."""
    import threading

    monkeypatch.setattr(clustering, "CHUNK_SIZE", 1)
    monkeypatch.setattr(clustering, "CHUNK_CONCURRENCY", 4)
    entered = threading.Barrier(4, timeout=10)

    def call(prompt: str) -> str:
        # Only returns if four calls are in flight at once; a sequential
        # implementation deadlocks here and the barrier times out.
        entered.wait()
        return json.dumps({"clusters": []})

    clustering.cluster_and_label(_findings(4), model_call=call)


def test_chunk_results_keep_the_input_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrency must not make `cluster_id` -- and so the merge prompt built
    from it -- depend on which chunk finished first."""
    monkeypatch.setattr(clustering, "CHUNK_SIZE", 1)

    def call(prompt: str) -> str:
        # Labeled from the chunk's own finding, not from call order, so the
        # assertion below is about where results land rather than when they were
        # asked for. The first chunk answers last.
        finding_id = json.loads(
            prompt.rsplit(clustering._TUPLES_HEADER, maxsplit=1)[-1]
        )[0]["id"]
        time.sleep(0.05 if finding_id == 0 else 0.0)
        return json.dumps(
            {
                "clusters": [
                    {
                        "label": f"chunk-{finding_id}",
                        "finding_ids": [finding_id],
                    }
                ]
            }
        )

    clusters, _ = clustering.cluster_and_label(_findings(3), model_call=call)

    assert [c.label for c in clusters] == ["chunk-0", "chunk-1", "chunk-2"]
    assert [c.cluster_id for c in clusters] == [0, 1, 2]


# --- cluster_and_label --------------------------------------------------------


def test_empty_input_makes_no_model_call() -> None:
    """Nothing failed, so there is nothing to ask the model about."""
    call = _fake_call({"clusters": []})

    assert clustering.cluster_and_label([], model_call=call) == ([], 0)
    assert call.prompts == []


def test_cluster_and_label_parses_the_model_response() -> None:
    findings = [
        _finding("calls the tool", "did not"),
        _finding("calls the tool", "no call"),
    ]
    call = _fake_call(
        {"clusters": [{"label": "made no tool call", "finding_ids": [0, 1]}]}
    )

    clusters, _ = clustering.cluster_and_label(findings, model_call=call)

    assert [(c.label, c.finding_ids) for c in clusters] == [
        ("made no tool call", [0, 1])
    ]
    # The prompt carries the label templates and every finding.
    prompt = call.prompts[0]
    assert "failed to call <tool>" in prompt
    assert '"id": 0' in prompt and '"id": 1' in prompt


@pytest.mark.parametrize(
    "response",
    [
        pytest.param("not json at all", id="not_json"),
        pytest.param('{"clusters": "a string"}', id="clusters_not_a_list"),
        pytest.param("", id="empty_response"),
    ],
)
def test_unusable_model_response_yields_no_clusters(response: str) -> None:
    """A refusal or a truncated generation degrades to zero clusters, not a crash."""
    assert clustering.cluster_and_label(
        _findings(1), model_call=_fake_call(response)
    ) == (
        [],
        0,
    )


def test_malformed_cluster_is_skipped_but_siblings_survive() -> None:
    """One bad entry in the array must not discard the good ones."""
    call = _fake_call(
        {
            "clusters": [
                {"no_label_field": 1},
                {"label": "made no tool call", "finding_ids": [0]},
            ]
        }
    )

    clusters, _ = clustering.cluster_and_label(_findings(1), model_call=call)

    assert [c.label for c in clusters] == ["made no tool call"]


def test_the_model_may_answer_with_integers_or_strings() -> None:
    """`Cluster` coerces string IDs into integers to tolerate quoted IDs in LLM outputs."""
    for ids in ([0, 1], ["0", "1"]):
        call = _fake_call(
            {"clusters": [{"label": "made no tool call", "finding_ids": ids}]}
        )

        clusters, _ = clustering.cluster_and_label(
            _findings(2), model_call=call
        )
        (cluster,) = clusters

        assert cluster.finding_ids == [0, 1]


def test_an_id_that_is_not_a_number_costs_its_cluster() -> None:
    """Nothing tries to rescue it: the cluster fails validation and is skipped
    with a warning, which is where a model that ignored the schema should land."""
    call = _fake_call(
        {
            "clusters": [
                {"label": "made no tool call", "finding_ids": ["not-an-id"]}
            ]
        }
    )

    assert clustering.cluster_and_label(_findings(2), model_call=call) == (
        [],
        0,
    )


# --- validate_clusters --------------------------------------------------------


def test_validate_drops_a_cluster_with_an_out_of_range_id() -> None:
    """The whole cluster goes, not just the bad id: if the model invented
    evidence, its grouping is not trustworthy either."""
    clusters = [
        Cluster(label="made no tool call", finding_ids=[1, 99]),
        Cluster(label="produced an empty response", finding_ids=[2]),
    ]

    kept = clustering.validate_clusters(clusters, finding_count=3)

    assert [c.label for c in kept] == ["produced an empty response"]


@pytest.mark.parametrize(
    "cluster",
    [
        pytest.param(Cluster(label="", finding_ids=[1]), id="empty_label"),
        pytest.param(Cluster(label="   ", finding_ids=[1]), id="blank_label"),
        pytest.param(
            Cluster(label="made no tool call", finding_ids=[]), id="no_ids"
        ),
    ],
)
def test_validate_drops_unusable_clusters(cluster: Cluster) -> None:
    """A label is the entire matching key, and a cluster with no ids has no evidence."""
    assert clustering.validate_clusters([cluster], finding_count=3) == []


def test_validate_recomputes_item_count_from_the_ids() -> None:
    """The count is ours, not an assertion we take from the model."""
    cluster = Cluster(
        label="made no tool call", finding_ids=[1, 2], item_count=99
    )

    (kept,) = clustering.validate_clusters([cluster], finding_count=3)

    assert kept.item_count == 2


# --- attach_examples ----------------------------------------------------------


def test_attach_examples_carries_the_conversation_trace() -> None:
    """Evidence is the finding plus the conversation it was found in."""
    findings = [
        _finding("must call the tool", "did not", session_id="case-abc")
    ]
    sessions = {"case-abc": [{"role": "user", "text": "the printer is jammed"}]}
    clusters = [
        Cluster(label="made no tool call", finding_ids=[0], item_count=1)
    ]

    (enriched,) = clustering.attach_examples(clusters, findings, sessions)

    (example,) = enriched.examples
    assert example.rubric.expected_behavior == "must call the tool"
    assert example.eval_case_id == "case-abc"
    assert "printer is jammed" in json.dumps(example.trace)


def test_attach_examples_is_capped() -> None:
    """The payload is persisted verbatim, so it must stay bounded.

    Sized off `MAX_EXAMPLES` rather than a literal, so raising the cap cannot
    leave this green without the input actually reaching it.
    """
    total = MAX_EXAMPLES + 5
    findings = [_finding(session_id=f"case-{i}") for i in range(total)]
    sessions = {f"case-{i}": [{"turn": i}] for i in range(total)}
    clusters = [
        Cluster(
            label="made no tool call",
            finding_ids=list(range(total)),
            item_count=total,
        )
    ]

    (enriched,) = clustering.attach_examples(clusters, findings, sessions)

    assert len(enriched.examples) == MAX_EXAMPLES
    # The cap trims evidence only -- the count still reflects the whole cluster.
    assert enriched.item_count == total


def test_attach_examples_survives_a_finding_without_a_session() -> None:
    """A finding from a page that arrived without its dataset has no session id;
    degraded evidence still counts."""
    findings = [_finding("must call the tool", "did not", session_id="")]
    clusters = [
        Cluster(label="made no tool call", finding_ids=[0], item_count=1)
    ]

    (enriched,) = clustering.attach_examples(clusters, findings, {})

    (example,) = enriched.examples
    assert example.rubric.expected_behavior == "must call the tool"
    assert example.trace == []
    assert example.eval_case_id == ""


def test_attach_examples_counts_one_trace_per_conversation() -> None:
    """Two findings from one conversation are one trace, not two: the count says
    how widely the defect was seen, not how much it broke."""
    findings = [
        _finding("d1", "r1", session_id="case-1"),
        _finding("d2", "r2", session_id="case-1"),
        _finding("d3", "r3", session_id="case-2"),
    ]
    sessions = {"case-1": [{"t": 1}], "case-2": [{"t": 2}]}
    clusters = [
        Cluster(label="made no tool call", finding_ids=[0, 1, 2], item_count=3)
    ]

    (enriched,) = clustering.attach_examples(clusters, findings, sessions)

    assert enriched.trace_ids == ["case-1", "case-2"]
    assert enriched.trace_count == 2


def test_attach_examples_counts_traces_past_the_example_cap() -> None:
    """The examples are capped and the count is not, or a defect seen in a
    thousand conversations would report ten."""
    total = MAX_EXAMPLES + 5
    findings = [_finding(session_id=f"case-{i}") for i in range(total)]
    sessions = {f"case-{i}": [{"t": i}] for i in range(total)}
    clusters = [
        Cluster(
            label="made no tool call",
            finding_ids=list(range(total)),
            item_count=total,
        )
    ]

    (enriched,) = clustering.attach_examples(clusters, findings, sessions)

    assert len(enriched.examples) == MAX_EXAMPLES
    assert enriched.trace_count == total


# --- call_clustering_model -------------------------------------------------------


def test_default_model_call_uses_config_insights_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model and location come from `config.insights_model`, read at call time.

    Guards the seam every other model call in the codebase uses: routing this
    through workflow state instead would silently ignore an operator's
    ``AQA_INSIGHTS_MODEL``.
    """
    from ambient_quality_agent.config import Model

    from .conftest import make_config

    captured: dict[str, Any] = {}

    class _FakeModels:
        def generate_content(
            self, *, model: str, contents: str, config: Any
        ) -> Any:
            captured["model"] = model
            captured["thinking"] = config.thinking_config.thinking_level
            return SimpleNamespace(text='{"clusters": []}')

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["client_kwargs"] = kwargs
            self.models = _FakeModels()

    monkeypatch.setattr(
        "ambient_quality_agent.config.config",
        make_config(
            project_id="cfg-project",
            insights_model=Model(model="cfg-model", location="cfg-location"),
        ),
    )
    monkeypatch.setattr("google.genai.Client", _FakeClient)

    assert clustering.call_clustering_model("prompt") == '{"clusters": []}'

    assert captured["model"] == "cfg-model"
    assert captured["client_kwargs"]["location"] == "cfg-location"
    assert captured["client_kwargs"]["project"] == "cfg-project"
    # Thinking and the answer share one output budget and a chunk's answer needs
    # most of it, so this is deliberately not the model's default of HIGH.
    # `thinking_budget` is not the lever on Gemini 3: it is documented for
    # earlier models only, and setting it alongside a level is an API error.
    assert captured["thinking"] == genai_types.ThinkingLevel.MEDIUM


def test_the_client_retries_the_transient_failures_these_calls_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Roughly one call in four came back 503 or 499 without reaching the model,
    and a chunk lost that way is thousands of findings."""
    from .conftest import make_config

    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["http_options"] = kwargs.get("http_options")
            self.models = SimpleNamespace(
                generate_content=lambda **kw: SimpleNamespace(text="{}")
            )

    monkeypatch.setattr("ambient_quality_agent.config.config", make_config())
    monkeypatch.setattr("google.genai.Client", _FakeClient)

    clustering.call_clustering_model("prompt")

    # Given no retry options the SDK stops after one attempt, so this is what
    # turns retrying on at all.
    retry = captured["http_options"].retry_options
    assert retry.attempts == genai_json.CHUNK_ATTEMPTS
    assert {429, 499, 503} <= set(retry.http_status_codes)


def test_attach_examples_collects_the_distinct_revisions() -> None:
    """A cluster's findings can come from sessions on several builds; all of
    them are kept, so the occurrence can pick the newest."""
    findings = [
        Finding(
            expected_behavior="d1", actual_behavior="r1", agent_revision="2"
        ),
        Finding(
            expected_behavior="d2", actual_behavior="r2", agent_revision="4"
        ),
        Finding(
            expected_behavior="d3", actual_behavior="r3", agent_revision="2"
        ),
    ]
    clusters = [
        Cluster(label="made no tool call", finding_ids=[0, 1, 2], item_count=3)
    ]

    (enriched,) = clustering.attach_examples(clusters, findings, {})

    assert enriched.agent_revisions == ["2", "4"]


def test_attach_examples_leaves_unattributed_findings_without_a_revision() -> (
    None
):
    """Sources that report no revision leave the cluster unattributed rather
    than inventing an unknown one."""
    clusters = [
        Cluster(label="made no tool call", finding_ids=[0], item_count=1)
    ]

    (enriched,) = clustering.attach_examples(clusters, [_finding()], {})

    assert enriched.agent_revisions == []
