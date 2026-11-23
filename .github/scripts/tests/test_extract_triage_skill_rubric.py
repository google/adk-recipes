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
"""Tests for extract_triage_skill_rubric.py and workflow Gate A / B-F wiring."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from extract_triage_skill_rubric import (
    DEFAULT_SKILL_PATH,
    extract_gates_b_to_f,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
TRIAGE_CORE = (
    REPO_ROOT / ".github" / "workflows" / "_ai-recipe-proposal-triage-core.yml"
)
SCRIPT = Path(__file__).resolve().parents[1] / "extract_triage_skill_rubric.py"


def test_extracts_only_gates_b_to_f_from_repo_skill():
    raw = DEFAULT_SKILL_PATH.read_text(encoding="utf-8")
    extracted = extract_gates_b_to_f(raw)

    for gate in ("B", "C", "D", "E", "F"):
        assert f"### Gate {gate}" in extracted

    # Gate A is handled deterministically by check_recipe_proposal.py, and
    # Gate G / maintainer sign-off / first-person instructions stay excluded.
    assert "### Gate A" not in extracted
    assert "### Gate G" not in extracted
    assert "## 3. Draft the comment" not in extracted
    assert "Draft in first person for Pier" not in extracted
    assert "@happyhuman" not in extracted


def test_extract_raises_when_gate_b_missing():
    with pytest.raises(ValueError, match="Gate B"):
        extract_gates_b_to_f("### Gate C — Deployability\nText\n")


def test_extract_raises_when_required_gate_missing():
    incomplete = (
        "### Gate B — Wrong root\nText\n"
        "### Gate C — Deployability\nText\n"
        "### Gate D — Scope fit\nText\n"
        "### Gate F — Scope overlap\nText\n"
    )
    with pytest.raises(ValueError, match="Gate E"):
        extract_gates_b_to_f(incomplete)


def test_cli_writes_extracted_rubric(tmp_path: Path):
    out_file = tmp_path / "gates_b_to_f.md"
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(out_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    content = out_file.read_text(encoding="utf-8")
    assert content.startswith("### Gate B")
    assert "### Gate F" in content
    assert "### Gate G" not in content


def test_cli_missing_skill_file_returns_infra_fault(tmp_path: Path):
    missing = tmp_path / "missing-SKILL.md"
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--skill", str(missing)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 2


def test_workflow_wires_gate_a_and_skill_gates_b_to_f_without_auto_close():
    doc = yaml.safe_load(TRIAGE_CORE.read_text(encoding="utf-8"))
    steps = doc["jobs"]["triage"]["steps"]
    by_id = {s.get("id"): s for s in steps if s.get("id")}

    assert "gate_a" in by_id
    assert "--check-gate-a" in by_id["gate_a"]["run"]

    # Skill extraction must happen inside catalog before `.agents` is deleted.
    catalog_run = by_id["catalog"]["run"]
    extract_pos = catalog_run.index("extract_triage_skill_rubric.py")
    rm_pos = catalog_run.index("rm -rf .agents")
    assert extract_pos < rm_pos

    # Evaluate must inject the extracted skill rubric and enforce neutral
    # non-closing first-pass bot tone.
    evaluate_step = by_id["evaluate"]
    assert "triage_skill_gates_b_to_f.md" in evaluate_step["run"]
    prompt = evaluate_step["env"]["PROMPT"]
    assert "Mandatory Workflow Overrides" in prompt
    assert "neutral automated first-pass bot tone" in prompt
    assert "Never auto-close" in prompt
    assert "Never grant approval" in prompt

    # No step in the workflow may close an issue or PR automatically.
    all_runs = "\n".join(str(s.get("run") or "") for s in steps)
    assert "gh issue close" not in all_runs
    assert "gh pr close" not in all_runs
