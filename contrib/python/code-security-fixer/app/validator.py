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
"""The deterministic gate every model-written patch must pass.

A candidate file is written into a disposable copy of the current working
copy, then Bandit and the target's tests run on it. The patch is accepted
only if, compared with the working copy before the patch:

1. no previously passing test fails (no regressions),
2. Bandit reports no new finding, and
3. at least one finding is resolved: a Bandit finding disappears or a
   failing (exploit) test now passes.

The model's own claims play no part in the decision.
"""

import shutil
import tempfile
from pathlib import Path
from typing import Any

from . import scanner, workspace


def _precheck(work_dir: Path, rel_path: str, new_content: str) -> str | None:
    """Cheap checks done before running anything. Returns a reason or None."""
    try:
        old = workspace.read_file(work_dir, rel_path)
    except workspace.WorkspaceError as exc:
        return str(exc)
    if workspace.is_test_path(rel_path):
        return "test files are evidence and cannot be modified; fix the code"
    if len(new_content) > workspace.MAX_FILE_CHARS:
        return f"new content exceeds {workspace.MAX_FILE_CHARS} characters"
    if new_content == old:
        return "new content is identical to the current file"
    if new_content.count("nosec") > old.count("nosec"):
        return "suppressing findings with '# nosec' is not a fix"
    if rel_path.endswith(".py"):
        try:
            compile(new_content, rel_path, "exec")
        except SyntaxError as exc:
            return f"syntax error: {exc.msg} (line {exc.lineno})"
    return None


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Diffs two scans and decides whether `after` is a strict improvement."""
    before_counts = scanner.finding_counts(before)
    after_counts = scanner.finding_counts(after)
    resolved = sorted(
        k for k, n in before_counts.items() if after_counts.get(k, 0) < n
    )
    introduced = sorted(
        k for k, n in after_counts.items() if n > before_counts.get(k, 0)
    )
    before_failed = scanner.failed_tests(before)
    after_passed = scanner.passed_tests(after)
    after_failed = scanner.failed_tests(after)
    regressions = sorted(scanner.passed_tests(before) - after_passed)
    fixed_tests = sorted(t for t in before_failed if t in after_passed)

    reasons = []
    if after["tests_error"]:
        reasons.append(after["tests_error"])
    if after["scanner_error"]:
        reasons.append(after["scanner_error"])
    for tid in regressions:
        detail = after_failed.get(tid, "test no longer runs")
        reasons.append(f"regression: {tid} now fails: {detail}")
    for key in introduced:
        reasons.append(f"new static finding: {key}")
    if not reasons and not resolved and not fixed_tests:
        reasons.append(
            "no finding resolved: Bandit findings and failing tests unchanged"
        )
    return {
        "accepted": not reasons,
        "reasons": reasons,
        "resolved_findings": resolved,
        "fixed_tests": fixed_tests,
        "regressions": regressions,
        "new_findings": introduced,
    }


def validate_patch(
    work_dir: Path,
    rel_path: str,
    new_content: str,
    before: dict[str, Any],
    timeout_seconds: float,
    test_command: str | None,
    bandit_skips: str,
) -> dict[str, Any]:
    """Validates replacing `rel_path` with `new_content` in a scratch copy.

    Never touches `work_dir`; the caller applies accepted patches.

    Returns:
        The `compare()` verdict plus `after`, the full scan of the candidate
        (None if the patch was rejected before running anything).
    """
    reason = _precheck(work_dir, rel_path, new_content)
    if reason:
        return {
            "accepted": False,
            "reasons": [reason],
            "resolved_findings": [],
            "fixed_tests": [],
            "regressions": [],
            "new_findings": [],
            "after": None,
        }
    scratch = tempfile.mkdtemp(prefix="code-security-candidate-")
    try:
        candidate = workspace.copy_tree(work_dir, scratch)
        workspace.write_file(candidate, rel_path, new_content)
        after = scanner.scan(
            str(candidate), timeout_seconds, test_command, bandit_skips
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    verdict = compare(before, after)
    verdict["after"] = after
    return verdict
