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
"""Deterministic evidence about a working copy: Bandit findings + test results.

Nothing here calls a model. `scan()` returns the same structure for the
baseline, for the current working copy and for every candidate patch, so the
validator can diff them.
"""

import json
import re
import shlex
import sys
from typing import Any

from .sandbox import python_argv, run_limited

_BANDIT_EXCLUDES = (
    "*/tests/*,*/test/*,*/test_*.py,*/*_test.py,*/conftest.py,"
    "*/.venv/*,*/venv/*,*/node_modules/*,*/site-packages/*"
)
_MAX_DETAIL_CHARS = 400

# Discovers and runs unittest tests, then prints one JSON line with the
# per-test outcome. Test output goes to stderr so it cannot corrupt the result.
_UNITTEST_HARNESS = r"""
import json, sys, unittest
real_stdout, sys.stdout = sys.stdout, sys.stderr
sys.path.insert(0, ".")

class Result(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.started = []
    def startTest(self, test):
        super().startTest(test)
        self.started.append(test.id())

def owner(test):
    return getattr(test, "test_case", test).id()

def last_line(tb):
    lines = [l for l in tb.strip().splitlines() if l.strip()]
    return lines[-1] if lines else ""

suite = unittest.defaultTestLoader.discover(sys.argv[1], top_level_dir=".")
result = Result()
suite.run(result)
failed = {}
for test, tb in result.failures + result.errors:
    failed.setdefault(owner(test), last_line(tb))
tests = []
for tid in dict.fromkeys(result.started):
    if tid in failed:
        tests.append({"id": tid, "status": "failed", "detail": failed.pop(tid)})
    else:
        tests.append({"id": tid, "status": "passed", "detail": ""})
for tid, detail in failed.items():
    tests.append({"id": tid, "status": "failed", "detail": detail})
real_stdout.write("\n@@RESULT@@" + json.dumps({"tests": tests}) + "\n")
"""

_PYTEST_LINE = re.compile(r"^(PASSED|FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$")


def run_bandit(
    work_dir: str, timeout_seconds: float, skips: str
) -> dict[str, Any]:
    """Runs Bandit on `work_dir` (tests excluded, `# nosec` ignored).

    `skips` is a comma-separated list of Bandit check ids to skip (BANDIT_SKIPS),
    typically checks that flag API usage rather than a vulnerability.

    Returns:
        `{"findings": [...], "error": str | None}`. Each finding has `key`
        (file + check id, stable across line shifts), `file`, `line`,
        `test_id`, `test_name`, `severity`, `confidence`, `issue`, `code`.
    """
    argv = python_argv(
        "-m",
        "bandit",
        "-r",
        ".",
        "-f",
        "json",
        "-q",
        "--ignore-nosec",
        "-x",
        _BANDIT_EXCLUDES,
    )
    if skips:
        argv += ["-s", skips]
    run = run_limited(argv, work_dir, timeout_seconds)
    if run["timed_out"]:
        return {"findings": [], "error": "bandit timed out"}
    try:
        report = json.loads(run["stdout"])
    except json.JSONDecodeError:
        tail = run["stderr"].strip().splitlines()[-1:] or ["no output"]
        return {
            "findings": [],
            "error": f"bandit failed: {tail[0][:_MAX_DETAIL_CHARS]}",
        }
    findings = []
    for r in report.get("results", []):
        path = r["filename"].removeprefix("./")
        findings.append(
            {
                "key": f"{path}:{r['test_id']}",
                "file": path,
                "line": r["line_number"],
                "test_id": r["test_id"],
                "test_name": r["test_name"],
                "severity": r["issue_severity"],
                "confidence": r["issue_confidence"],
                "issue": r["issue_text"],
                "code": r.get("code", "").strip()[:_MAX_DETAIL_CHARS],
            }
        )
    findings.sort(key=lambda f: (f["file"], f["line"], f["test_id"]))
    errors = report.get("errors") or []
    error = None
    if errors:
        error = f"bandit could not parse: {errors[0].get('filename')}"
    return {"findings": findings, "error": error}


def _parse_unittest(run: dict[str, Any]) -> dict[str, Any]:
    marker = run["stdout"].rfind("@@RESULT@@")
    if marker < 0:
        tail = run["stderr"].strip().splitlines()[-1:] or ["no output"]
        return {
            "tests": [],
            "error": f"test run crashed: {tail[0][:_MAX_DETAIL_CHARS]}",
        }
    payload = json.loads(run["stdout"][marker + len("@@RESULT@@") :])
    return {"tests": payload["tests"], "error": None}


def _parse_generic(run: dict[str, Any]) -> dict[str, Any]:
    """Parses `pytest -rA` style summary lines, else uses the exit code."""
    tests = []
    for line in (run["stdout"] + "\n" + run["stderr"]).splitlines():
        m = _PYTEST_LINE.match(line.strip())
        if m:
            status = "passed" if m.group(1) == "PASSED" else "failed"
            detail = (m.group(3) or "")[:_MAX_DETAIL_CHARS]
            tests.append({"id": m.group(2), "status": status, "detail": detail})
    if not tests:
        ok = run["returncode"] == 0
        tail = (run["stdout"] + run["stderr"]).strip().splitlines()[-1:]
        tests.append(
            {
                "id": "test-suite",
                "status": "passed" if ok else "failed",
                "detail": "" if ok else (tail or [""])[0][:_MAX_DETAIL_CHARS],
            }
        )
    return {"tests": tests, "error": None}


def build_test_argv(test_command: str) -> list[str]:
    """Turns TEST_COMMAND into an argv, pinning `python` to this interpreter."""
    argv = shlex.split(test_command)
    if argv and argv[0] in {"python", "python3"}:
        argv[0] = sys.executable
    return argv


def run_tests(
    work_dir: str,
    timeout_seconds: float,
    test_command: str | None = None,
    tests_dir: str = "tests",
) -> dict[str, Any]:
    """Runs the target's test suite.

    With no `test_command`, discovers stdlib `unittest` tests under
    `tests_dir` and reports per-test outcomes. Otherwise runs the command
    (no shell) and parses `pytest -rA` lines, falling back to the exit code.

    Returns:
        `{"tests": [{"id", "status", "detail"}], "error": str | None}`.
    """
    if test_command:
        argv = build_test_argv(test_command)
    else:
        argv = python_argv("-c", _UNITTEST_HARNESS, tests_dir)
    run = run_limited(argv, work_dir, timeout_seconds)
    if run["timed_out"]:
        return {
            "tests": [],
            "error": f"tests timed out after {timeout_seconds:.0f}s",
        }
    if test_command:
        return _parse_generic(run)
    return _parse_unittest(run)


def scan(
    work_dir: str,
    timeout_seconds: float,
    test_command: str | None,
    bandit_skips: str,
) -> dict[str, Any]:
    """Collects all evidence for one working copy."""
    bandit = run_bandit(work_dir, timeout_seconds, bandit_skips)
    tests = run_tests(work_dir, timeout_seconds, test_command)
    return {
        "findings": bandit["findings"],
        "scanner_error": bandit["error"],
        "tests": tests["tests"],
        "tests_error": tests["error"],
    }


def passed_tests(scan_result: dict[str, Any]) -> set[str]:
    return {t["id"] for t in scan_result["tests"] if t["status"] == "passed"}


def failed_tests(scan_result: dict[str, Any]) -> dict[str, str]:
    return {
        t["id"]: t["detail"]
        for t in scan_result["tests"]
        if t["status"] == "failed"
    }


def finding_counts(scan_result: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in scan_result["findings"]:
        counts[f["key"]] = counts.get(f["key"], 0) + 1
    return counts


def summarize(scan_result: dict[str, Any]) -> dict[str, Any]:
    """A compact, model-readable view of a scan."""
    return {
        "static_findings": [
            {
                k: f[k]
                for k in (
                    "file",
                    "line",
                    "test_id",
                    "severity",
                    "issue",
                    "code",
                )
            }
            for f in scan_result["findings"]
        ],
        "failing_tests": [
            {"test": tid, "detail": detail}
            for tid, detail in failed_tests(scan_result).items()
        ],
        "tests_passed": len(passed_tests(scan_result)),
        "tests_total": len(scan_result["tests"]),
        "scanner_error": scan_result["scanner_error"],
        "tests_error": scan_result["tests_error"],
    }
