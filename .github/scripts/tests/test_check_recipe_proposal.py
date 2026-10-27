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
"""Tests for check_recipe_proposal.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from check_recipe_proposal import is_recipe_proposal


def test_standard_title_marker():
    is_prop, reason = is_recipe_proposal(
        "[RECIPE PROPOSAL] Healthcare diagnostic agent"
    )
    assert is_prop is True
    assert reason == "title_marker"


def test_lowercase_title_marker():
    is_prop, reason = is_recipe_proposal(
        "[recipe proposal] Real estate valuation"
    )
    assert is_prop is True
    assert reason == "title_marker"


def test_template_body_signal():
    body = (
        "### ⚠️ Scope Notice\n"
        "For `contrib/` recipes only.\n\n"
        "1- What is the intent/purpose of your new recipe?\n"
        "To showcase BigQuery agent patterns."
    )
    is_prop, reason = is_recipe_proposal("New agent idea", body=body)
    assert is_prop is True
    assert "template_signal" in reason


def test_label_contribute():
    is_prop, reason = is_recipe_proposal(
        "Financial agent", labels=["contribute"]
    )
    assert is_prop is True
    assert reason == "label_contribute"


def test_proposal_title_patterns():
    proposals = [
        "Proposal: new recipe for store ops",
        "Propose recipe for BigQuery FinOps",
        "Add new recipe: Smart Retail Assistant",
        "Create recipe for fraud detection",
        "Add new plugin in plugins/retail",
        "New recipe for core/python: Agent Engine sample",
        "Propose adding recipe to contrib/python/foo",
    ]
    for title in proposals:
        is_prop, _reason = is_recipe_proposal(title)
        assert is_prop is True, f"Failed for title: {title}"


def test_proposal_body_patterns():
    body = "I would like to propose a new recipe for contrib/python demonstrating ADK tool usage."
    is_prop, reason = is_recipe_proposal("Tool usage agent", body=body)
    assert is_prop is True
    assert "body_pattern" in reason


def test_negative_cases_not_proposals():
    non_proposals = [
        ("[BUG] KeyError in contrib/python/react-ad-flow", "Stack trace here"),
        ("[QUESTION] How to authenticate with Vertex AI?", "Details here"),
        ("[DOCS] Fix typo in README.md", "Fixed typo in line 12"),
        ("Update poetry dependencies in core/python", "Bumping versions"),
        (
            "Crash on startup when DEMO_ASSETS_DRIVE_ID is missing",
            "Crash report",
        ),
    ]
    for title, body in non_proposals:
        is_prop, reason = is_recipe_proposal(title, body=body)
        assert is_prop is False, f"Expected False for {title}, got {reason}"


def test_cli_execution(tmp_path: Path):
    out_file = tmp_path / "github_output.txt"
    script = Path(__file__).resolve().parents[1] / "check_recipe_proposal.py"

    cmd = [
        sys.executable,
        str(script),
        "--title",
        "[RECIPE PROPOSAL] Smart Agent",
        "--github-output",
        str(out_file),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert res.returncode == 0
    assert '"is_proposal": true' in res.stdout
    assert "is_proposal=true" in out_file.read_text(encoding="utf-8")
