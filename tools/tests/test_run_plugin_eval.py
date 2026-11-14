#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pathlib import Path

import run_plugin_eval as rpe

# ==============================================================================
# Positive Tests
# ==============================================================================


def test_valid_eval_passes(tmp_path: Path):
    eval_file = tmp_path / "EVAL.yaml"
    eval_file.write_text(
        """
threshold: 0.85
cases:
  - id: test_case_1
    prompt: "I need a search query"
    rubric:
      - "The response MUST include relevant search results."
      - "The response SHOULD be concise."
    assertions:
      - type: response_not_empty
"""
    )
    res = rpe.run_eval_for_file(eval_file)
    assert res["status"] == "PASS"
    assert res["pass_rate"] >= 0.85


def test_percent_string_threshold_passes(tmp_path: Path):
    eval_file = tmp_path / "EVAL.yaml"
    eval_file.write_text(
        """
threshold: "80%"
cases:
  - id: test_percent
    prompt: "Valid test prompt for evaluation"
    rubric:
      - "The response MUST return a result."
    assertions:
      - type: response_not_empty
"""
    )
    res = rpe.run_eval_for_file(eval_file)
    assert res["status"] == "PASS"
    assert res["threshold"] == 0.80


def test_real_plugin_evals_pass():
    eval_files = rpe.find_eval_files()
    assert len(eval_files) >= 2
    for ef in eval_files:
        res = rpe.run_eval_for_file(ef)
        assert res["status"] == "PASS", f"Eval failed for {ef}: {res}"
        assert res["pass_rate"] >= 0.80


# ==============================================================================
# Negative Tests
# ==============================================================================


def test_corrupted_yaml_fails(tmp_path: Path):
    eval_file = tmp_path / "EVAL.yaml"
    eval_file.write_text("cases: [unclosed list")
    res = rpe.run_eval_for_file(eval_file)
    assert res["status"] == "ERROR"
    assert "YAML Parse error" in res["error"]


def test_non_dict_yaml_fails(tmp_path: Path):
    eval_file = tmp_path / "EVAL.yaml"
    eval_file.write_text("- item1\n- item2\n")
    res = rpe.run_eval_for_file(eval_file)
    assert res["status"] == "ERROR"
    assert "must be a top-level mapping" in res["error"]


def test_missing_cases_fails(tmp_path: Path):
    eval_file = tmp_path / "EVAL.yaml"
    eval_file.write_text("threshold: 0.85\n")
    res = rpe.run_eval_for_file(eval_file)
    assert res["status"] == "ERROR"
    assert "No 'cases' list found" in res["error"]


def test_empty_cases_fails(tmp_path: Path):
    eval_file = tmp_path / "EVAL.yaml"
    eval_file.write_text("cases: []\n")
    res = rpe.run_eval_for_file(eval_file)
    assert res["status"] == "ERROR"
    assert "No 'cases' list found" in res["error"]


def test_low_quality_rubric_fails_threshold(tmp_path: Path):
    eval_file = tmp_path / "EVAL.yaml"
    eval_file.write_text(
        """
threshold: 0.80
cases:
  - id: bad_case
    prompt: "test"
    rubric:
      - "poorly specified rubric with no modal verbs"
      - "another bad line"
      - "vague requirement"
"""
    )
    res = rpe.run_eval_for_file(eval_file)
    assert res["status"] == "FAIL"
    assert res["pass_rate"] < 0.80


def test_missing_prompt_penalized(tmp_path: Path):
    eval_file = tmp_path / "EVAL.yaml"
    eval_file.write_text(
        """
threshold: 0.80
cases:
  - id: no_prompt
    prompt: ""
    rubric:
      - "The response MUST reply."
"""
    )
    res = rpe.run_eval_for_file(eval_file)
    # 1 prompt failure + 1 rubric pass = 50% pass rate, which is below 80%
    assert res["status"] == "FAIL"
    assert res["pass_rate"] == 0.50
