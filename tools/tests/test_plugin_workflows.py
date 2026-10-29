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

import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_workflow_yaml_syntax():
    """Verify that all new workflow YAML files have valid syntax and structure."""
    workflow_files = [
        REPO_ROOT / ".github/workflows/plugin-scaffolding-check.yml",
        REPO_ROOT / ".github/workflows/plugin-test-and-eval.yml",
        REPO_ROOT / ".github/workflows/ai-plugin-evaluator.yml",
    ]

    for wf in workflow_files:
        assert wf.is_file(), f"Workflow file {wf} missing."
        content = yaml.safe_load(wf.read_text(encoding="utf-8"))
        assert isinstance(content, dict), f"{wf} must be a valid YAML mapping."
        assert "name" in content, f"{wf} missing 'name'."
        assert "on" in content or True in content, f"{wf} missing 'on' triggers."
        assert "jobs" in content, f"{wf} missing 'jobs'."


def test_ai_plugin_evaluator_interface():
    """Ensure ai-plugin-evaluator.yml correctly interfaces with _ai-pr-review-core.yml."""
    wf_path = REPO_ROOT / ".github/workflows/ai-plugin-evaluator.yml"
    data = yaml.safe_load(wf_path.read_text(encoding="utf-8"))

    trigger_job = data["jobs"].get("trigger")
    assert trigger_job is not None, "ai-plugin-evaluator missing trigger job"
    assert trigger_job.get("uses") == "./.github/workflows/_ai-pr-review-core.yml"

    with_inputs = trigger_job.get("with", {})
    assert with_inputs.get("review_label") == "Plugin Evaluator"
    assert "role" in with_inputs
    assert "criteria" in with_inputs
    assert "hunt_list" in with_inputs


def test_scaffolding_workflow_cli_positive():
    """Simulate the runner command in plugin-scaffolding-check.yml on real repo plugins."""
    cmd = [
        sys.executable,
        str(REPO_ROOT / "tools/validate_plugin_scaffolding.py"),
        "plugins",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), check=False
    )
    assert result.returncode == 0, (
        f"Validator failed on repo plugins:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "[PASS]" in result.stdout


def test_scaffolding_workflow_cli_negative(tmp_path: Path):
    """Simulate runner failure when a broken plugin is encountered."""
    broken_plugin = tmp_path / "bad_vertical"
    broken_plugin.mkdir(parents=True, exist_ok=True)
    # Missing plugin.json and invalid structure
    (broken_plugin / "README.md").write_text("broken")

    cmd = [
        sys.executable,
        str(REPO_ROOT / "tools/validate_plugin_scaffolding.py"),
        str(broken_plugin),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), check=False
    )
    assert result.returncode == 1, (
        f"Expected returncode 1 for broken plugin, got {result.returncode}"
    )
    assert "plugin.json" in result.stdout or "skills" in result.stdout


def test_eval_workflow_cli_positive():
    """Simulate the evaluation runner command in plugin-test-and-eval.yml on real repo plugins."""
    cmd = [
        sys.executable,
        str(REPO_ROOT / "tools/run_plugin_eval.py"),
        "plugins",
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), check=False
    )
    assert result.returncode == 0, (
        f"Eval failed on repo plugins:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "ALL PLUGIN EVALUATIONS PASSED" in result.stdout


def test_eval_workflow_cli_negative(tmp_path: Path):
    """Simulate evaluation runner failure on a failing EVAL.yaml."""
    bad_eval = tmp_path / "plugins" / "sample" / "skills" / "s1" / "EVAL.yaml"
    bad_eval.parent.mkdir(parents=True, exist_ok=True)
    bad_eval.write_text(
        "threshold: 0.90\ncases:\n  - id: bad\n    prompt: 'x'\n    rubric: ['poorly worded without modal keywords']\n"
    )

    cmd = [
        sys.executable,
        str(REPO_ROOT / "tools/run_plugin_eval.py"),
        str(tmp_path / "plugins"),
    ]
    result = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(REPO_ROOT), check=False
    )
    assert result.returncode == 1, (
        f"Expected returncode 1 for failing eval, got {result.returncode}"
    )
    assert "EVALUATION FAILED" in result.stdout


def test_plugin_skills_test_structure():
    """Verify that every existing plugin skill provides tests and runnability files."""
    skills = [
        REPO_ROOT / "plugins/retail/skills/product-search",
        REPO_ROOT / "plugins/retail/skills/virtual-tryon",
    ]
    for skill_dir in skills:
        assert (skill_dir / "tests").is_dir(), f"{skill_dir} missing tests directory"
        test_files = list((skill_dir / "tests").glob("test_*.py"))
        assert len(test_files) > 0, f"{skill_dir} has no test_*.py files"
        assert (skill_dir / "tests/test_runnability.py").is_file(), (
            f"{skill_dir} missing test_runnability.py"
        )
