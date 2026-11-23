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

import json
import subprocess
import sys
from pathlib import Path

from check_recipe_proposal import (
    check_gate_a,
    is_recipe_proposal,
    is_template_proposal,
)


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


def test_template_body_signal_language():
    body = (
        "2- What language is your recipe implemented in (e.g. Python, Go, etc)?"
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


def test_is_template_proposal_full_template():
    body = (
        "### ⚠️ Scope Notice\n"
        "For `contrib/` recipes only. `core/` recipes follow a different process.\n\n"
        "1- What is the intent/purpose of your new recipe?\n"
        "To showcase enterprise agent workflows.\n\n"
        "2- What language is your recipe implemented in (e.g. Python, Go, etc)?\n"
        "Python\n\n"
        "3- Will this be a deployable recipe?\n"
        "Yes, includes Dockerfile.\n\n"
        "4- What GCP services or third-party APIs are required?\n"
        "Vertex AI, BigQuery.\n\n"
        "5- Who will own and maintain this?\n"
        "my-team\n"
    )
    is_prop, reason = is_template_proposal(
        "[RECIPE PROPOSAL] Enterprise Workflow Agent", body=body
    )
    assert is_prop is True
    assert "template_format" in reason


def test_is_template_proposal_rejects_freeform_proposal():
    # Issue that mentions proposing a recipe but does NOT use the template format
    title = "Propose a new recipe for retail automation"
    body = "Hi team, I would like to propose a new recipe for contrib/python."
    is_prop, reason = is_template_proposal(title, body=body)
    assert is_prop is False
    assert reason == "not_template_format"


def test_is_template_proposal_routes_marker_without_template_for_gate_a():
    # Title has [RECIPE PROPOSAL], but body has no template questions:
    # it must still route into proposal triage so Gate A can alert the author.
    title = "[RECIPE PROPOSAL] Quick idea"
    body = "Just opening this to ask if anyone has done this before."
    is_prop, reason = is_template_proposal(title, body=body)
    assert is_prop is True
    assert "marker_and_0_signals" in reason

    gate_a = check_gate_a(title, body=body)
    assert gate_a["passed"] is False
    assert gate_a["has_marker"] is True
    assert len(gate_a["missing_sections"]) == 5
    assert "Automated first-pass triage (Gate A" in gate_a["comment"]
    assert (
        "Leaving this issue open for @pierpaolo28 and @kgu-here"
        in gate_a["comment"]
    )


def test_gate_a_passes_full_template_multiline_and_inline():
    body = (
        "### ⚠️ Scope Notice\n"
        "For `contrib/` recipes only. `core/` recipes follow a different process.\n\n"
        "1- What is the intent/purpose of your new recipe? In what way will it help the ADK community?\n"
        "Demonstrates a retail inventory forecasting agent on BigQuery and Vertex AI.\n\n"
        "2- What language is your recipe implemented in (e.g. Python, Go, etc)? Python\n\n"
        "3- Will this be a deployable recipe with a root Dockerfile (e.g. targeting Cloud Run or Vertex AI Agent Engine)? "
        "Note: every `contrib/` recipe requires a root `Dockerfile` and `deployable: true` in `manifest.yaml`.\n"
        "Yes, includes a root Dockerfile targeting Cloud Run.\n\n"
        "4- What GCP services or third-party APIs (Salesforce, Jira, etc.) are required?\n"
        "Vertex AI, BigQuery, Cloud Run.\n\n"
        "5- Who (individual/team) will own and maintain this?\n"
        "@example-owner\n\n"
        "6- Please include any additional information you'd like to share.\n"
    )
    gate_a = check_gate_a(
        "[RECIPE PROPOSAL] Retail Forecasting Agent", body=body
    )
    assert gate_a["passed"] is True
    assert gate_a["has_marker"] is True
    assert gate_a["missing_sections"] == []
    assert gate_a["unanswered_sections"] == []
    assert gate_a["comment"] == ""


def test_gate_a_fails_empty_body():
    gate_a = check_gate_a("[RECIPE PROPOSAL] Empty proposal", body="   ")
    assert gate_a["passed"] is False
    assert gate_a["has_marker"] is True
    assert len(gate_a["missing_sections"]) == 5
    assert "empty body" in gate_a["comment"]
    assert "propose-a-new-recipe.md" in gate_a["comment"]


def test_gate_a_fails_unmodified_boilerplate_template():
    template_path = (
        Path(__file__).resolve().parents[3]
        / ".github"
        / "ISSUE_TEMPLATE"
        / "propose-a-new-recipe.md"
    )
    _, _, template_body = template_path.read_text(encoding="utf-8").split(
        "---", 2
    )
    gate_a = check_gate_a("[RECIPE PROPOSAL] Unfilled", body=template_body)
    assert gate_a["passed"] is False
    assert gate_a["missing_sections"] == []
    assert len(gate_a["unanswered_sections"]) == 5
    assert "Unanswered template questions" in gate_a["comment"]


def test_gate_a_handles_legacy_q2_typo_and_partial_missing():
    body = (
        "1- What is the intent/purpose of your new recipe?\n"
        "Cloud Run + Vertex AI agent.\n\n"
        "2- What language is your recipe is implemented in (e.g. Python, Go, etc)?\n"
        "Python\n\n"
        "3- Will this be a deployable recipe?\n"
        "Yes.\n\n"
        "4- What GCP services or third-party APIs are required?\n\n"
    )
    gate_a = check_gate_a("[RECIPE PROPOSAL] Partial", body=body)
    assert gate_a["passed"] is False
    assert gate_a["missing_sections"] == ["5. Owner / maintainer"]
    assert gate_a["unanswered_sections"] == [
        "4. Required GCP services or third-party APIs"
    ]
    assert "Missing template questions" in gate_a["comment"]
    assert "Unanswered template questions" in gate_a["comment"]


def test_cli_check_gate_a_failure_writes_comment_and_outputs(tmp_path: Path):
    out_file = tmp_path / "github_output.txt"
    comment_file = tmp_path / "comment.md"
    script = Path(__file__).resolve().parents[1] / "check_recipe_proposal.py"

    payload = json.dumps(
        {
            "title": "[RECIPE PROPOSAL] Missing template fields",
            "body": "Just a quick one-liner without the template.",
        }
    )
    res = subprocess.run(
        [
            sys.executable,
            str(script),
            "--stdin",
            "--check-gate-a",
            "--comment-out",
            str(comment_file),
            "--github-output",
            str(out_file),
        ],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    parsed = json.loads(res.stdout)
    assert parsed["passed"] is False
    assert comment_file.is_file()
    assert "Automated first-pass triage (Gate A" in comment_file.read_text(
        encoding="utf-8"
    )
    gh_out = out_file.read_text(encoding="utf-8")
    assert "gate_a_passed=false" in gh_out
    assert "decision=DECLINE" in gh_out
    assert "gate=A" in gh_out
    assert "close_issue=false" in gh_out


def test_cli_check_gate_a_pass_skips_comment(tmp_path: Path):
    out_file = tmp_path / "github_output.txt"
    comment_file = tmp_path / "comment.md"
    script = Path(__file__).resolve().parents[1] / "check_recipe_proposal.py"

    body = (
        "1- What is the intent/purpose of your new recipe?\n"
        "Enterprise support agent.\n"
        "2- What language is your recipe implemented in (e.g. Python, Go, etc)?\n"
        "Python\n"
        "3- Will this be a deployable recipe?\n"
        "Yes, with root Dockerfile.\n"
        "4- What GCP services or third-party APIs are required?\n"
        "Vertex AI, Cloud Run\n"
        "5- Who (individual/team) will own and maintain this?\n"
        "Team A\n"
    )
    payload = json.dumps(
        {"title": "[RECIPE PROPOSAL] Support Agent", "body": body}
    )
    res = subprocess.run(
        [
            sys.executable,
            str(script),
            "--stdin",
            "--check-gate-a",
            "--comment-out",
            str(comment_file),
            "--github-output",
            str(out_file),
        ],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    parsed = json.loads(res.stdout)
    assert parsed["passed"] is True
    assert not comment_file.exists()
    gh_out = out_file.read_text(encoding="utf-8")
    assert "gate_a_passed=true" in gh_out
    assert "decision=" not in gh_out


def test_cli_execution_template_only(tmp_path: Path):
    out_file = tmp_path / "github_output.txt"
    script = Path(__file__).resolve().parents[1] / "check_recipe_proposal.py"

    # Non-template proposal with --template-only should return false
    cmd = [
        sys.executable,
        str(script),
        "--title",
        "Add new recipe: Smart Agent",
        "--template-only",
        "--github-output",
        str(out_file),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert res.returncode == 0
    assert '"is_proposal": false' in res.stdout
    assert "is_proposal=false" in out_file.read_text(encoding="utf-8")


def test_cli_stdin_handles_hyphen_prefixed_body(tmp_path: Path):
    """Issues starting with `---` (YAML frontmatter / HR) must not crash argparse."""
    script = Path(__file__).resolve().parents[1] / "check_recipe_proposal.py"
    stdin_payload = '{"title": "[RECIPE PROPOSAL] Agent", "body": "---\\nfrontmatter\\n---", "labels": []}'
    res = subprocess.run(
        [sys.executable, str(script), "--stdin"],
        input=stdin_payload,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    assert '"is_proposal": true' in res.stdout


def test_bug_titles_mentioning_create_and_recipe_are_not_proposals():
    non_proposals = [
        "Cannot create session in rag-agent-search recipe",
        "Error when creating agent in financial-advisor recipe",
        "Bug in contrib/python/llm-auditor recipe",
    ]
    for title in non_proposals:
        is_prop, reason = is_recipe_proposal(title)
        assert is_prop is False, f"Expected False for {title!r}, got {reason}"
