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
"""A CodeMender-style security fixer built with ADK.

Applied to an AppSec team's finding backlog on a Python web service. The loop
is: detect -> patch -> validate.

- Detect: `list_findings` runs Bandit and the target's own tests (including
  exploit tests) on a throwaway working copy.
- Patch: the Gemini agent reads the vulnerable code and proposes a minimal,
  complete replacement for one file.
- Validate: `propose_patch` re-runs Bandit and the tests on a disposable copy
  of the patched code. Only patches that fix something and break nothing are
  kept; rejected ones come back with the exact reason, within a bounded
  retry budget.

The result is a unified diff and evidence for human review. The target is
never modified, committed or pushed.
"""

import os
from pathlib import Path
from typing import Any

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.genai import types

from . import config, scanner, validator, workspace

_SOURCE_KEY = "cm_source_dir"
_WORK_KEY = "cm_work_dir"
_APPLIED_KEY = "cm_applied"
_BASELINE_KEY = "cm_baseline"
_CURRENT_KEY = "cm_current"
_ATTEMPTS_KEY = "cm_attempts"


def _max_attempts() -> int:
    return int(
        config.require_number(
            "MAX_PATCH_ATTEMPTS", os.getenv("MAX_PATCH_ATTEMPTS"), int
        )
    )


def _timeout_seconds() -> float:
    return float(
        config.require_number(
            "VALIDATION_TIMEOUT_SECONDS",
            os.getenv("VALIDATION_TIMEOUT_SECONDS"),
            float,
        )
    )


def _test_command() -> str | None:
    return os.getenv("TEST_COMMAND") or None


def _bandit_skips() -> str:
    return config.require("BANDIT_SKIPS", os.getenv("BANDIT_SKIPS"))


def _scan(work_dir: Path) -> dict[str, Any]:
    return scanner.scan(
        str(work_dir), _timeout_seconds(), _test_command(), _bandit_skips()
    )


def _workspace(tool_context: ToolContext) -> tuple[Path, Path]:
    """Returns (source, working copy), creating the copy on first use."""
    state = tool_context.state
    if not state.get(_SOURCE_KEY):
        state[_SOURCE_KEY] = str(
            workspace.resolve_target(os.getenv("TARGET_DIR") or None)
        )
    source = Path(state[_SOURCE_KEY])
    work = state.get(_WORK_KEY)
    if not work or not Path(work).is_dir():
        work = workspace.create_working_copy(
            source, state.get(_APPLIED_KEY) or {}
        )
        state[_WORK_KEY] = str(work)
    return source, Path(work)


def _attempts(tool_context: ToolContext) -> list[dict[str, Any]]:
    return list(tool_context.state.get(_ATTEMPTS_KEY) or [])


def _remaining(tool_context: ToolContext) -> int:
    return max(0, _max_attempts() - len(_attempts(tool_context)))


def _current(tool_context: ToolContext, work: Path) -> dict[str, Any]:
    """The latest scan of the working copy; scans (and baselines) if needed."""
    current = tool_context.state.get(_CURRENT_KEY)
    if current is None:
        current = _scan(work)
        tool_context.state[_CURRENT_KEY] = current
        if tool_context.state.get(_BASELINE_KEY) is None:
            tool_context.state[_BASELINE_KEY] = current
    return current


def list_findings(tool_context: ToolContext) -> dict[str, Any]:
    """Scans the working copy with Bandit and runs the target's tests.

    Call this first, and again whenever you want fresh evidence. Failing tests
    are findings too: they are exploit or regression tests.

    Returns:
        Static findings (file, line, check id, severity, issue, code),
        failing tests with their failure message, the source files you may
        patch, and the remaining patch budget.
    """
    try:
        source, work = _workspace(tool_context)
    except workspace.WorkspaceError as exc:
        return {"error": str(exc)}
    tool_context.state[_CURRENT_KEY] = None
    current = _current(tool_context, work)
    files = workspace.list_files(work)
    return {
        "target": source.name,
        **scanner.summarize(current),
        "patchable_files": [f for f in files if not workspace.is_test_path(f)],
        "test_files": [f for f in files if workspace.is_test_path(f)],
        "patch_attempts_remaining": _remaining(tool_context),
    }


def read_file(file_path: str, tool_context: ToolContext) -> dict[str, Any]:
    """Reads a file from the working copy (accepted patches included).

    Args:
        file_path: Path relative to the target root, e.g. "orders/db.py".

    Returns:
        The file's full content, or an error.
    """
    try:
        _source, work = _workspace(tool_context)
        content = workspace.read_file(work, file_path)
    except workspace.WorkspaceError as exc:
        return {"error": str(exc)}
    return {"file_path": file_path, "content": content}


def propose_patch(
    file_path: str, new_content: str, rationale: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Validates a fix and keeps it only if the validator accepts it.

    Args:
        file_path: Source file to replace, relative to the target root.
            Test files cannot be modified.
        new_content: The COMPLETE new content of the file.
        rationale: One or two sentences: root cause and why this fixes it.

    Returns:
        `accepted`, the reasons for a rejection, the findings resolved and
        exploit tests fixed, what remains, and the remaining budget.
    """
    remaining = _remaining(tool_context)
    if remaining <= 0:
        return {
            "budget_exhausted": True,
            "message": "No patch attempts left. Call get_patch_report.",
        }
    try:
        _source, work = _workspace(tool_context)
    except workspace.WorkspaceError as exc:
        return {"error": str(exc)}
    before = _current(tool_context, work)
    verdict = validator.validate_patch(
        work,
        file_path,
        new_content,
        before,
        _timeout_seconds(),
        _test_command(),
        _bandit_skips(),
    )
    after = verdict.pop("after")
    if verdict["accepted"]:
        workspace.write_file(work, file_path, new_content)
        applied = dict(tool_context.state.get(_APPLIED_KEY) or {})
        applied[file_path] = new_content
        tool_context.state[_APPLIED_KEY] = applied
        tool_context.state[_CURRENT_KEY] = after
        before = after
    attempts = _attempts(tool_context)
    attempts.append(
        {
            "attempt": len(attempts) + 1,
            "file": file_path,
            "rationale": rationale,
            **verdict,
        }
    )
    tool_context.state[_ATTEMPTS_KEY] = attempts
    summary = scanner.summarize(before)
    left = remaining - 1
    clean = not summary["static_findings"] and not summary["failing_tests"]
    return {
        **verdict,
        "remaining_static_findings": [
            f"{f['file']}:{f['line']} {f['test_id']}"
            for f in summary["static_findings"]
        ],
        "remaining_failing_tests": [
            t["test"] for t in summary["failing_tests"]
        ],
        "patch_attempts_remaining": left,
        "done": left <= 0 or clean,
    }


def get_patch_report(tool_context: ToolContext) -> dict[str, Any]:
    """Builds the final review package. Call once at the end.

    Returns:
        The unified diff of all accepted patches (also written to a .patch
        file), findings fixed and remaining versus the baseline, test results
        before and after, and every attempt with its verdict.
    """
    baseline = tool_context.state.get(_BASELINE_KEY)
    if baseline is None:
        return {"error": "No baseline yet. Call list_findings first."}
    try:
        source, work = _workspace(tool_context)
    except workspace.WorkspaceError as exc:
        return {"error": str(exc)}
    current = _current(tool_context, work)
    applied = tool_context.state.get(_APPLIED_KEY) or {}
    diff = workspace.unified_diff(source, applied)
    patch_file = work.parent / "codemender.patch"
    patch_file.write_text(diff, encoding="utf-8")

    base_counts = scanner.finding_counts(baseline)
    now_counts = scanner.finding_counts(current)
    base_failed = scanner.failed_tests(baseline)
    now_passed = scanner.passed_tests(current)
    attempts = _attempts(tool_context)
    return {
        "target": source.name,
        "files_changed": sorted(applied),
        "diff": diff,
        "patch_file": str(patch_file),
        "static_findings_before": len(baseline["findings"]),
        "static_findings_after": len(current["findings"]),
        "static_findings_fixed": sorted(
            k for k in base_counts if now_counts.get(k, 0) < base_counts[k]
        ),
        "static_findings_remaining": [
            f"{f['key']} line {f['line']}: {f['issue']}"
            for f in current["findings"]
        ],
        "tests_before": {
            "passed": len(scanner.passed_tests(baseline)),
            "total": len(baseline["tests"]),
        },
        "tests_after": {
            "passed": len(now_passed),
            "total": len(current["tests"]),
        },
        "exploit_tests_fixed": sorted(
            t for t in base_failed if t in now_passed
        ),
        "tests_still_failing": sorted(scanner.failed_tests(current)),
        "attempts": [
            {
                k: a[k]
                for k in ("attempt", "file", "accepted", "rationale", "reasons")
            }
            for a in attempts
        ],
        "attempts_used": len(attempts),
        "attempt_budget": _max_attempts(),
    }


INSTRUCTION = """\
You are an application-security engineer working like CodeMender. You fix
vulnerabilities in a Python service with a detect -> patch -> validate loop.
A deterministic validator (Bandit + the project's own tests, including
exploit tests) decides whether a patch is kept. Your opinion never does.

When the user asks you to fix, scan or review the code:
1. Call `list_findings`. Group the static findings and failing tests by root
   cause (one vulnerability often shows up as both).
2. For each root cause, highest severity first:
   a. `read_file` the vulnerable file (and the failing test, to see the
      exact attack it reproduces).
   b. Write the smallest complete fix that removes the vulnerability class,
      not just the one payload: parameterized SQL; resolve paths and check
      they stay inside the base directory; no shell, pass an argv list and
      validate input; JSON instead of pickle; read secrets from the
      environment and fail closed. Keep public function signatures and
      behaviour for legitimate input. Standard library only.
   c. Call `propose_patch` with the COMPLETE new file content and a short
      rationale. Never edit tests, never add `# nosec`.
   d. If rejected, read the reasons (regression, new finding, nothing
      fixed), adjust and retry. Never resubmit identical content.
3. Stop when nothing is left, or when a result has `done: true` or
   `budget_exhausted: true`. Then call `get_patch_report`.

Finish with a report for the AppSec reviewer: a table of findings (check id
or test, file, severity, status fixed/remaining), tests passed before and
after, attempts used of the budget, anything that still needs a human, and
the full diff in a ```diff code block. Say clearly that nothing was
committed and the patch needs human review. Do not ask for confirmation
between steps.
"""


def create_agent() -> Agent:
    """Creates a fresh, isolated instance of the Agent."""
    return Agent(
        name="root_agent",
        model=Gemini(
            model=os.getenv("MODEL_NAME"),
            retry_options=types.HttpRetryOptions(attempts=3),
        ),
        description=(
            "Fixes security findings in a Python codebase with a"
            " detect -> patch -> validate loop and returns a reviewable diff."
        ),
        instruction=INSTRUCTION,
        tools=[list_findings, read_file, propose_patch, get_patch_report],
    )


root_agent = create_agent()

app = App(
    root_agent=root_agent,
    name="app",
)
