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
"""Unit tests for the scanner, validator, workspace and workflow. No model calls.

The workflow tests run the real ADK 2.0 graph with the Gemini patch agent
swapped for a deterministic function node that returns reference fixes.
"""

import asyncio
import hashlib
import os
import shutil
from pathlib import Path

import pytest
from google.adk import Workflow
from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.genai import types
from reference_fixes import (
    ARCHIVE,
    BROKEN,
    FIX_2021,
    FIX_2025,
    INCOMPLETE,
)

from app import agent, config, scanner, validator, workspace

TIMEOUT = 60
EXPLOIT_2021 = {
    "tests.test_traversal.TraversalTest.test_cve_2021_3281_absolute_path",
    "tests.test_traversal.TraversalTest.test_cve_2021_3281_dot_segments",
}
EXPLOIT_2025 = (
    "tests.test_traversal.TraversalTest.test_cve_2025_59682_sibling_prefix"
)
SKIPS = os.environ["BANDIT_SKIPS"]  # loaded from .env.example by conftest


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for rel in workspace.list_files(root, suffix=""):
        if "__pycache__" in rel:
            continue
        digest.update(rel.encode())
        digest.update((root / rel).read_bytes())
    return digest.hexdigest()


@pytest.fixture(scope="module")
def baseline() -> dict:
    return scanner.scan(str(workspace.BUNDLED_TARGET), TIMEOUT, None, SKIPS)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    return workspace.copy_tree(workspace.BUNDLED_TARGET, str(tmp_path))


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.delenv("TARGET_DIR", raising=False)
    monkeypatch.delenv("TEST_COMMAND", raising=False)
    monkeypatch.setenv("VALIDATION_TIMEOUT_SECONDS", str(TIMEOUT))
    monkeypatch.setenv("MAX_PATCH_ATTEMPTS", "8")
    return {}


# --- scanner -----------------------------------------------------------------


def test_bandit_findings_are_parsed_and_tests_excluded(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "loader.py").write_text(
        "import subprocess\n\n\ndef run(cmd):\n"
        "    return subprocess.call(cmd, shell=True)\n"
    )
    (tmp_path / "tests" / "test_loader.py").write_text(
        "import pickle\n\n\ndef test_x():\n    pickle.loads(b'')\n"
    )
    result = scanner.run_bandit(str(tmp_path), TIMEOUT, SKIPS)
    assert result["error"] is None
    assert [f["key"] for f in result["findings"]] == ["loader.py:B602"]
    assert result["findings"][0]["severity"] == "HIGH"
    # `# nosec` does not hide a finding from the validator.
    (tmp_path / "loader.py").write_text(
        "import subprocess\n\n\ndef run(cmd):\n"
        "    return subprocess.call(cmd, shell=True)  # nosec\n"
    )
    hidden = scanner.run_bandit(str(tmp_path), TIMEOUT, SKIPS)
    assert [f["key"] for f in hidden["findings"]] == ["loader.py:B602"]


def test_baseline_on_the_django_cve(baseline: dict) -> None:
    # Bandit has no rule for this traversal; the exploit tests catch it.
    assert baseline["findings"] == []
    assert baseline["scanner_error"] is None
    assert baseline["tests_error"] is None
    failed = set(scanner.failed_tests(baseline))
    assert failed == EXPLOIT_2021 | {EXPLOIT_2025}
    passed = scanner.passed_tests(baseline)
    assert "tests.test_archive.TestArchive.test_extract_function" in passed
    assert len(passed) == 3


def test_generic_test_command_parsing() -> None:
    run = {
        "returncode": 1,
        "stdout": "PASSED tests/test_a.py::test_ok\n"
        "FAILED tests/test_a.py::test_bad - AssertionError: boom\n",
        "stderr": "",
        "timed_out": False,
    }
    tests = scanner._parse_generic(run)["tests"]
    assert tests == [
        {"id": "tests/test_a.py::test_ok", "status": "passed", "detail": ""},
        {
            "id": "tests/test_a.py::test_bad",
            "status": "failed",
            "detail": "AssertionError: boom",
        },
    ]
    fallback = scanner._parse_generic({**run, "stdout": "1 failed\n"})
    assert fallback["tests"][0]["id"] == "test-suite"
    assert fallback["tests"][0]["status"] == "failed"
    argv = scanner.build_test_argv("python -m pytest -rA -q")
    assert argv[0] != "python" and argv[1:] == ["-m", "pytest", "-rA", "-q"]


# --- validator ---------------------------------------------------------------


def test_validator_accepts_the_2025_patch(work: Path, baseline: dict) -> None:
    verdict = validator.validate_patch(
        work, ARCHIVE, FIX_2025, baseline, TIMEOUT, None, SKIPS
    )
    assert verdict["accepted"], verdict["reasons"]
    assert set(verdict["fixed_tests"]) == EXPLOIT_2021 | {EXPLOIT_2025}
    assert verdict["regressions"] == []
    # Validation happens in a scratch copy; the working copy is untouched.
    assert "target_filename" not in (work / ARCHIVE).read_text()


def test_validator_accepts_the_2021_patch_as_partial(
    work: Path, baseline: dict
) -> None:
    """Django's first fix: accepted, but CVE-2025-59682 stays open."""
    verdict = validator.validate_patch(
        work, ARCHIVE, FIX_2021, baseline, TIMEOUT, None, SKIPS
    )
    assert verdict["accepted"], verdict["reasons"]
    assert set(verdict["fixed_tests"]) == EXPLOIT_2021
    assert scanner.failed_tests(verdict["after"]).keys() == {EXPLOIT_2025}


def test_validator_rejects_a_fix_that_breaks_behaviour(
    work: Path, baseline: dict
) -> None:
    verdict = validator.validate_patch(
        work, ARCHIVE, BROKEN, baseline, TIMEOUT, None, SKIPS
    )
    assert not verdict["accepted"]
    assert verdict["regressions"] == [
        "tests.test_archive.TestArchive.test_extract_function"
    ]


def test_validator_rejects_an_incomplete_fix(
    work: Path, baseline: dict
) -> None:
    verdict = validator.validate_patch(
        work, ARCHIVE, INCOMPLETE, baseline, TIMEOUT, None, SKIPS
    )
    assert not verdict["accepted"]
    assert verdict["fixed_tests"] == []
    assert any("no finding resolved" in r for r in verdict["reasons"])


@pytest.mark.parametrize(
    ("rel_path", "content", "reason"),
    [
        ("tests/test_traversal.py", "pass\n", "test files"),
        (ARCHIVE, "def broken(:\n", "syntax error"),
        (
            ARCHIVE,
            FIX_2025.replace("import os\n", "import os  # nosec\n", 1),
            "nosec",
        ),
        ("../outside.py", "x = 1\n", "relative"),
        ("django/missing.py", "x = 1\n", "not found"),
    ],
)
def test_validator_prechecks(
    work: Path, baseline: dict, rel_path: str, content: str, reason: str
) -> None:
    verdict = validator.validate_patch(
        work, rel_path, content, baseline, TIMEOUT, None, SKIPS
    )
    assert not verdict["accepted"]
    assert verdict["after"] is None  # rejected before running anything
    assert reason in verdict["reasons"][0]


# --- workspace ---------------------------------------------------------------


def test_safe_path_and_test_detection(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    (tmp_path / "link.py").symlink_to(tmp_path / "pkg" / "mod.py")
    assert workspace.safe_path(tmp_path, "pkg/mod.py").is_file()
    for bad in ("../etc/passwd", "/etc/passwd", "link.py", ""):
        with pytest.raises(workspace.WorkspaceError):
            workspace.safe_path(tmp_path, bad)
    assert workspace.is_test_path("tests/test_x.py")
    assert workspace.is_test_path("pkg/conftest.py")
    assert workspace.is_test_path("pkg/thing_test.py")
    assert not workspace.is_test_path(ARCHIVE)


def test_working_copy_replays_accepted_patches() -> None:
    copy = workspace.create_working_copy(
        workspace.BUNDLED_TARGET, {ARCHIVE: FIX_2025}
    )
    try:
        assert (copy / ARCHIVE).read_text() == FIX_2025
        assert copy.resolve() != workspace.BUNDLED_TARGET
    finally:
        shutil.rmtree(copy.parent)


# --- deterministic workflow steps ---------------------------------------------


def test_start_run_records_the_baseline(state: dict) -> None:
    result = agent.start_run(state)
    assert result["route"] == "has_findings"
    assert result["summary"]["tests_passed"] == 3
    assert len(result["summary"]["failing_tests"]) == 3
    assert state["cm_baseline"] is state["cm_current"]
    assert state["cm_attempts"] == []
    assert Path(state["cm_work_dir"]) != workspace.BUNDLED_TARGET


def test_start_run_routes_clean_targets_to_report(
    state: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "already-fixed"
    shutil.copytree(workspace.BUNDLED_TARGET, repo)
    (repo / ARCHIVE).write_text(FIX_2025)
    monkeypatch.setenv("TARGET_DIR", str(repo))
    assert agent.start_run(state)["route"] == "clean"
    assert state["cm_source_dir"] == str(repo)


def test_start_run_reports_an_unusable_target(
    state: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TARGET_DIR", str(tmp_path / "missing"))
    result = agent.start_run(state)
    assert result["route"] == "clean"
    assert "not found" in result["error"]
    assert "not found" in agent.render_report(agent.build_report(state))


def test_patch_request_carries_evidence_and_feedback(state: dict) -> None:
    agent.start_run(state)
    state["cm_feedback"] = "rejected: regression"
    request = agent.PatchRequest.model_validate(
        agent.build_patch_request(state)
    )
    assert ARCHIVE in request.source_files
    assert "django/core/exceptions.py" in request.source_files
    assert "tests/test_traversal.py" in request.test_files
    assert "tests/test_archive.py" not in request.test_files  # all passing
    assert not any(workspace.is_test_path(f) for f in request.source_files)
    assert len(request.failing_tests) == 3
    assert request.previous_attempt_feedback == "rejected: regression"
    assert request.attempts_remaining == 8


def test_apply_proposal_routes_and_records_feedback(state: dict) -> None:
    agent.start_run(state)
    rejected = agent.apply_proposal(
        state,
        {
            "file_path": ARCHIVE,
            "new_content": INCOMPLETE,
            "rationale": "normalize paths",
        },
    )
    assert not rejected["accepted"]
    assert rejected["route"] == "continue"
    assert "no finding resolved" in state["cm_feedback"]
    accepted = agent.apply_proposal(
        state,
        agent.PatchProposal(
            file_path=ARCHIVE,
            new_content=FIX_2021,
            rationale="CVE-2021-3281 patch",
        ),
    )
    assert accepted["accepted"]
    assert accepted["route"] == "continue"  # CVE-2025-59682 still open
    assert state["cm_feedback"] == ""
    assert state["cm_applied"] == {ARCHIVE: FIX_2021}
    missing = agent.apply_proposal(state, None)
    assert not missing["accepted"]
    assert "PatchProposal" in missing["reasons"][0]
    assert len(state["cm_attempts"]) == 3


def test_budget_ends_the_loop(
    state: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_PATCH_ATTEMPTS", "1")
    agent.start_run(state)
    result = agent.apply_proposal(
        state,
        {"file_path": ARCHIVE, "new_content": BROKEN, "rationale": ""},
    )
    assert result["attempts_remaining"] == 0
    assert result["route"] == "done"


def test_missing_config_fails_with_a_clear_error(
    state: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent.start_run(state)
    monkeypatch.delenv("MAX_PATCH_ATTEMPTS")
    with pytest.raises(config.ConfigError, match="MAX_PATCH_ATTEMPTS"):
        agent.apply_proposal(
            state,
            {"file_path": ARCHIVE, "new_content": FIX_2025, "rationale": ""},
        )


# --- the ADK 2.0 graph, end to end ----------------------------------------------


def _scripted_patcher(proposals: list[tuple[str, str]]):
    """A function node standing in for the Gemini patch agent."""
    queue = list(proposals)

    def scripted_patcher(node_input: dict) -> dict:
        agent.PatchRequest.model_validate(node_input)
        rel, content = queue.pop(0)
        return {"file_path": rel, "new_content": content, "rationale": "ref"}

    return scripted_patcher


def _run(workflow: Workflow) -> tuple[list[str], dict]:
    async def go() -> tuple[list[str], dict]:
        runner = InMemoryRunner(app=App(root_agent=workflow, name="app"))
        session = await runner.session_service.create_session(
            app_name="app", user_id="u"
        )
        texts = []
        message = types.Content(role="user", parts=[types.Part(text="fix it")])
        async for event in runner.run_async(
            user_id="u", session_id=session.id, new_message=message
        ):
            for part in (event.content.parts if event.content else None) or []:
                if part.text:
                    texts.append(part.text)
        final = await runner.session_service.get_session(
            app_name="app", user_id="u", session_id=session.id
        )
        return texts, final.state

    return asyncio.run(go())


def test_workflow_replays_djangos_history(state: dict) -> None:
    """Incomplete fix -> 2021 patch (partial) -> 2025 patch (complete)."""
    before_hash = _tree_hash(workspace.BUNDLED_TARGET)
    proposals = [
        (ARCHIVE, INCOMPLETE),
        (ARCHIVE, FIX_2021),
        (ARCHIVE, FIX_2025),
    ]
    texts, final_state = _run(
        agent.create_workflow(_scripted_patcher(proposals))
    )

    assert texts[0].startswith("Baseline: 0 Bandit findings, 3 failing tests")
    assert "rejected" in texts[1]
    assert "accepted (2 findings resolved" in texts[2]
    assert "accepted (1 findings resolved" in texts[3]
    report_text = texts[-1]
    assert "| Tests passing | 3/6 | 6/6 |" in report_text
    assert "Patch attempts: 3 of 8." in report_text
    assert "```diff" in report_text
    assert "Nothing was committed" in report_text
    report = agent.build_report(final_state)
    assert report["files_changed"] == [ARCHIVE]
    assert len(report["exploit_tests_fixed"]) == 3
    assert "+            if os.path.commonpath(" in report["diff"]
    assert Path(report["patch_file"]).read_text() == report["diff"]
    # The bundled target is never modified in place.
    assert _tree_hash(workspace.BUNDLED_TARGET) == before_hash


def test_workflow_stops_when_the_budget_is_spent(
    state: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_PATCH_ATTEMPTS", "2")
    proposals = [(ARCHIVE, BROKEN)] * 2
    texts, final_state = _run(
        agent.create_workflow(_scripted_patcher(proposals))
    )
    assert sum(t.startswith("Attempt") and "rejected" in t for t in texts) == 2
    assert "Patch attempts: 2 of 2." in texts[-1]
    assert "# no accepted changes" in texts[-1]
    assert len(final_state["cm_attempts"]) == 2


def test_root_agent_is_the_gemini_workflow() -> None:
    assert isinstance(agent.root_agent, Workflow)
    assert isinstance(agent.app, App)
    assert agent.app.root_agent is agent.root_agent
    patch_agent = agent.create_patch_agent()
    assert patch_agent.mode == "single_turn"
    assert patch_agent.output_schema is agent.PatchProposal
