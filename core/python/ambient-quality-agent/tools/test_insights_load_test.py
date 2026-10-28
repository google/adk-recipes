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

"""Tests for the insight load test's offline half (``tools/insights_load_test.py``).

The script's point is to measure a real model, so what is pinned here is
everything the measurement rests on: a corpus whose ground truth actually holds,
a JSON classifier that separates truncation from junk, and scoring that counts
what it claims to count. A silently wrong metric would make the load test lie.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys

import pytest
from ambient_quality_agent.tools.insights.clustering import Cluster

# Load tools/insights_load_test.py as a module (it lives outside the src packages).
_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "insights_load_test.py"
)
_spec = importlib.util.spec_from_file_location("insights_load_test", _PATH)
assert _spec is not None and _spec.loader is not None
load_test = importlib.util.module_from_spec(_spec)
# Registered before execution because `@dataclasses.dataclass` resolves the
# defining module out of `sys.modules`, and fails on a module that is not there.
sys.modules[_spec.name] = load_test
_spec.loader.exec_module(load_test)


# --- corpus -------------------------------------------------------------------
#
# The corpus is eval pages, not findings, so these go through the same
# `eval_results_to_finding_set` the node does. That is the point of it: the ids the run
# measures have to be the ids production assigns.


def _extract(size: int, seed: int = 1):
    """Builds an evaluation corpus and extracts findings with ground truth defects.

    Args:
        size: Number of cases to generate in the corpus.
        seed: Random seed for corpus generation.

    Returns:
        Tuple of (pages, finding_set, truth_mapping).
    """
    from ambient_quality_agent.tools.evaluation.findings_adapter import (
        eval_results_to_finding_set,
    )

    pages, truth_by_text = load_test.build_corpus(size, seed)
    finding_set = eval_results_to_finding_set(pages)
    truth = {
        str(i): truth_by_text[f.expected_behavior, f.actual_behavior]
        for i, f in enumerate(finding_set.findings)
    }
    return pages, finding_set, truth


def test_corpus_yields_exactly_the_requested_number_of_failed_rubrics():
    """A page shape the adapter reads differently would silently resize the
    sweep, and every measurement would describe a corpus nobody asked for."""
    _, finding_set, _ = _extract(120)

    assert len(finding_set.findings) == 120


def test_corpus_ids_are_the_ordinals_production_mints():
    """A finding's id is its position, not the autorater's own rubric id, which
    the pages also carry. Id length is what the load test measures, so leaking
    the autorater's scheme into a finding would measure the wrong thing."""
    _, finding_set, _ = _extract(30)

    assert len(finding_set.findings) == 30
    assert all(
        "autorater-rubric" not in f.expected_behavior
        for f in finding_set.findings
    )
    assert all(
        "autorater-rubric" not in f.actual_behavior
        for f in finding_set.findings
    )


def test_corpus_is_balanced_across_defects():
    """Round-robin over the catalog, so a cluster count far from `len(DEFECTS)`
    is a grouping error rather than a skewed corpus."""
    _, _, truth = _extract(120)

    counts = {
        key: list(truth.values()).count(key) for key in set(truth.values())
    }
    assert set(counts.values()) == {120 // len(load_test.DEFECTS)}


def test_corpus_declares_its_tools_where_the_pipeline_looks_for_them():
    """Verify synthetic corpus embeds tools in eval cases for pipeline extraction."""
    _, finding_set, _ = _extract(60)
    recovered = {
        tool.name: tool
        for revisions in finding_set.agent_revisions.values()
        for revision in revisions
        for tool in revision.tools
    }

    assert set(recovered) == {t["name"] for t in load_test.TOOL_CATALOG}
    for tool in load_test.TOOL_CATALOG:
        assert recovered[tool["name"]].parameters == sorted(tool["parameters"])


def test_corpus_pages_carry_the_evidence_attach_examples_needs():
    """Results and dataset cases are joined positionally; if the corpus got that
    join wrong, every cluster would come back with empty examples."""
    from ambient_quality_agent.tools.insights import clustering
    from ambient_quality_agent.tools.insights.clustering import Cluster

    _, finding_set, _ = _extract(60)
    cluster = Cluster(
        label="d", finding_ids=list(range(len(finding_set.findings)))
    )

    (enriched,) = clustering.attach_examples(
        [cluster], finding_set.findings, finding_set.sessions
    )

    assert len(enriched.examples) == clustering.MAX_EXAMPLES
    assert all(e.eval_case_id for e in enriched.examples)
    assert all(e.trace for e in enriched.examples)


def test_corpus_is_deterministic_per_seed():
    _, first, _ = _extract(50, seed=3)
    _, same, _ = _extract(50, seed=3)
    _, other, _ = _extract(50, seed=4)

    assert [f.actual_behavior for f in first.findings] == [
        f.actual_behavior for f in same.findings
    ]
    assert [f.actual_behavior for f in first.findings] != [
        f.actual_behavior for f in other.findings
    ]


def test_corpus_varies_the_phrasing_of_one_defect():
    """One defect must not always read the same, or clustering it is string
    matching rather than the grouping being measured."""
    _, finding_set, truth = _extract(600, seed=5)
    key = load_test.DEFECTS[0].key
    phrasings = {
        f.actual_behavior
        for i, f in enumerate(finding_set.findings)
        if truth[str(i)] == key
    }

    assert len(phrasings) > len(load_test.DEFECTS[0].actual)


def test_corpus_carries_case_specific_noise():
    _, finding_set, _ = _extract(60, seed=6)

    assert any(
        context in f.actual_behavior
        for f in finding_set.findings
        for context in load_test.CASE_CONTEXTS
    )
    assert not any("{case}" in f.actual_behavior for f in finding_set.findings)


def test_defect_labels_only_name_things_the_catalog_declares():
    """A defect may name a tool or one of its parameters, or neither (a
    response-quality miss names no tool). What it must never do is name
    something absent from the catalog: the prompt calls that catalog the
    authoritative vocabulary, so an unlisted name would make the ground truth
    itself ungroundable."""
    vocabulary = {tool["name"] for tool in load_test.TOOL_CATALOG}
    vocabulary |= {
        p for tool in load_test.TOOL_CATALOG for p in tool["parameters"]
    }

    for defect in load_test.DEFECTS:
        identifiers = re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", defect.key)
        assert set(identifiers) <= vocabulary, defect.key


# --- JSON classification ------------------------------------------------------


def test_json_status_accepts_a_good_response():
    assert load_test._classify_json_status('{"clusters": []}') == "ok"


def test_json_status_reports_an_empty_response():
    assert load_test._classify_json_status("   ") == "empty"


def test_json_status_calls_a_cut_off_response_truncated():
    """The reported symptom: a generation that ran out of output budget mid-id.
    It fails to decode at the end of the text, which is what tells it apart."""
    raw = '{"clusters": [{"label": "failed to call create_ticket", "finding_ids": ["r00'

    assert load_test._classify_json_status(raw) == "truncated"


def test_json_status_calls_early_damage_invalid():
    assert (
        load_test._classify_json_status('{"clusters": ,"x"} trailing')
        == "invalid"
    )


# --- scoring ------------------------------------------------------------------


def _attempt() -> object:
    return load_test.ClusteringAttempt(size=4, attempt=0)


def test_score_counts_perfect_recovery():
    truth = {"0": "d1", "1": "d1", "2": "d2", "3": "d2"}
    clusters = [
        Cluster(label="d1", finding_ids=[0, 1], item_count=2),
        Cluster(label="d2", finding_ids=[2, 3], item_count=2),
    ]
    attempt = _attempt()

    load_test._score(attempt, clusters, clusters, truth)

    assert attempt.assigned_ids == 4
    assert attempt.invented_ids == 0
    assert attempt.duplicate_ids == 0
    assert attempt.purity == pytest.approx(1.0)
    assert attempt.defects_recovered == 2


def test_score_flags_invented_and_duplicated_ids():
    """Both are counted on the parsed clusters, before validation drops them --
    validation discards a whole cluster for one bad id, which would hide them."""
    truth = {"0": "d1", "1": "d1"}
    parsed = [
        Cluster(label="d1", finding_ids=[0, 1], item_count=2),
        Cluster(label="ghost", finding_ids=[0, 99], item_count=2),
    ]
    attempt = _attempt()

    load_test._score(attempt, parsed, parsed[:1], truth)

    assert attempt.assigned_ids == 2
    assert attempt.invented_ids == 1
    assert attempt.duplicate_ids == 1
    assert attempt.validated_clusters == 1


def test_score_penalises_a_cluster_that_mixes_two_defects():
    truth = {"0": "d1", "1": "d1", "2": "d2"}
    clusters = [Cluster(label="mixed", finding_ids=[0, 1, 2], item_count=3)]
    attempt = _attempt()

    load_test._score(attempt, clusters, clusters, truth)

    assert attempt.purity == pytest.approx(2 / 3)
    assert attempt.defects_recovered == 1


def test_score_reports_where_a_model_stopped_enumerating():
    """The failure measured at 5000 rubrics: valid JSON, correct labels, but
    only the corpus's first N ids inside it. `stopped_after` names that."""
    truth = {str(i): "d1" for i in range(10)}
    clusters = [Cluster(label="d1", finding_ids=list(range(4)))]
    attempt = _attempt()

    load_test._score(attempt, clusters, clusters, truth)

    assert attempt.assigned_ids == 4
    assert attempt.stopped_after == 4


def test_score_reports_no_stopping_point_for_scattered_misses():
    """Holes spread through the corpus are a different defect from a cut tail,
    so a prefix must not be claimed when the ids do not form one."""
    truth = {str(i): "d1" for i in range(10)}
    clusters = [Cluster(label="d1", finding_ids=[0, 1, 7])]
    attempt = _attempt()

    load_test._score(attempt, clusters, clusters, truth)

    assert attempt.stopped_after == 0


def test_score_reports_no_stopping_point_when_nothing_was_dropped():
    truth = {str(i): "d1" for i in range(3)}
    clusters = [Cluster(label="d1", finding_ids=[int(k) for k in truth])]
    attempt = _attempt()

    load_test._score(attempt, clusters, clusters, truth)

    assert attempt.stopped_after == 0


def test_attempt_is_usable_only_when_the_response_parsed():
    attempt = load_test.ClusteringAttempt(size=1, attempt=0, json_status="ok")
    assert attempt.ok

    attempt.json_status = "truncated"
    assert not attempt.ok

    attempt.json_status = "ok"
    attempt.error = "ResourceExhausted: 429"
    assert not attempt.ok


def test_matchers_accepts_the_judges_it_can_run(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["insights_load_test.py", "--matchers", "production, thread_pool"],
    )

    args = load_test._parse_args()

    assert load_test._split_matchers(args.matchers) == [
        "production",
        "thread_pool",
    ]


def test_matchers_refuses_a_name_it_would_run_as_the_scan(monkeypatch, capsys):
    """An unknown name would fall through to the pairwise scan and be reported
    under its own name, so it is refused rather than run."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["insights_load_test.py", "--matchers", "production,batched"],
    )

    with pytest.raises(SystemExit):
        load_test._parse_args()

    assert "unknown --matchers batched" in capsys.readouterr().err
