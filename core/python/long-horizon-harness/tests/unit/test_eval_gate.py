"""The 0.8 pass threshold `adk eval` applied, reapplied to graded results."""

import json

import pytest

from scripts.eval_gate import GateError, failing_cases, load_results


def _results(scores: list[float]) -> dict:
    return {
        "eval_case_results": [
            {
                "eval_case_index": i,
                "response_candidate_results": [
                    {
                        "response_index": 0,
                        "metric_results": {
                            "final_response_quality_v1": {
                                "score": s,
                                "explanation": "",
                            }
                        },
                    }
                ],
            }
            for i, s in enumerate(scores)
        ]
    }


def test_scores_below_the_threshold_are_reported():
    failures = failing_cases(_results([0.9, 0.4]), threshold=0.8)
    assert [(f.case_index, f.score) for f in failures] == [(1, 0.4)]


def test_a_score_exactly_at_the_threshold_passes():
    assert failing_cases(_results([0.8]), threshold=0.8) == []


def test_a_case_the_service_could_not_score_fails():
    results = _results([0.9])
    results["eval_case_results"][0]["response_candidate_results"][0][
        "metric_results"
    ]["final_response_quality_v1"] = {"error_message": "judge unavailable"}
    failures = failing_cases(results, threshold=0.8)
    assert [f.detail for f in failures] == ["judge unavailable"]


def test_a_results_file_with_no_scores_raises_rather_than_passing():
    with pytest.raises(GateError, match="no metric scores"):
        failing_cases({"eval_case_results": []}, threshold=0.8)


def test_load_results_picks_the_newest_file(tmp_path):
    older = tmp_path / "results_20260101_000000.json"
    newer = tmp_path / "results_20260102_000000.json"
    older.write_text(json.dumps({"eval_case_results": ["old"]}))
    newer.write_text(json.dumps({"eval_case_results": ["new"]}))
    assert load_results(str(tmp_path))["eval_case_results"] == ["new"]


def test_load_results_without_any_file_raises(tmp_path):
    with pytest.raises(GateError, match="no results"):
        load_results(str(tmp_path))
