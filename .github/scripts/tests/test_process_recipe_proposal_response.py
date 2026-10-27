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
"""Tests for process_recipe_proposal_response.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from process_recipe_proposal_response import (
    extract_json,
    process_proposal_response,
)


def test_extract_json_direct():
    raw = '{"decision": "ACCEPT", "gate": null, "close_issue": false, "response": "Approved!"}'
    data = extract_json(raw)
    assert data["decision"] == "ACCEPT"


def test_extract_json_fenced():
    raw = 'Here is my evaluation:\n```json\n{"decision": "DECLINE", "gate": "C", "close_issue": true, "response": "Declined: not deployable."}\n```'
    data = extract_json(raw)
    assert data["decision"] == "DECLINE"
    assert data["gate"] == "C"


def test_process_proposal_accept():
    d = {
        "decision": "ACCEPT",
        "gate": None,
        "close_issue": False,
        "response": "Thanks for the proposal — approved from my side.",
    }
    decision, gate, response_body, close_issue = process_proposal_response(d)
    assert decision == "ACCEPT"
    assert gate is None
    assert "approved" in response_body
    assert close_issue is False


def test_process_proposal_decline():
    d = {
        "decision": "DECLINE",
        "gate": "A",
        "close_issue": True,
        "response": "Closing as not actionable: template empty.",
    }
    decision, gate, _response_body, close_issue = process_proposal_response(d)
    assert decision == "DECLINE"
    assert gate == "A"
    assert close_issue is True


def test_process_proposal_invalid_decision():
    with pytest.raises(ValueError, match="Invalid decision"):
        process_proposal_response({"decision": "FOO", "response": "bar"})


def test_cli_execution_decline(tmp_path: Path):
    result_file = tmp_path / "agy_result.json"
    comment_out = tmp_path / "comment.md"
    close_out = tmp_path / "close.txt"
    github_out = tmp_path / "github_output.txt"

    payload = {
        "status": "SUCCESS",
        "response": json.dumps(
            {
                "decision": "DECLINE",
                "gate": "C",
                "close_issue": True,
                "response": "Declining this one: Recipe Checklist §1 requires Dockerfile.",
            }
        ),
    }
    result_file.write_text(json.dumps(payload), encoding="utf-8")

    script = (
        Path(__file__).resolve().parents[1]
        / "process_recipe_proposal_response.py"
    )
    cmd = [
        sys.executable,
        str(script),
        "--result",
        str(result_file),
        "--issue-number",
        "999",
        "--comment-out",
        str(comment_out),
        "--close-out",
        str(close_out),
        "--github-output",
        str(github_out),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    assert "Declining this one" in comment_out.read_text(encoding="utf-8")
    assert close_out.read_text(encoding="utf-8").strip() == "true"
    gh_text = github_out.read_text(encoding="utf-8")
    assert "decision=DECLINE" in gh_text
    assert "gate=C" in gh_text
    assert "close_issue=true" in gh_text
