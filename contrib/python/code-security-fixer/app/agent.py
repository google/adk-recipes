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
"""A code security fixer built as an ADK 2.0 graph workflow.

Works down security findings in a Python codebase (by default, Django's
real CVE-2021-3281 / CVE-2025-59682 archive-extraction code)::

    START -> scan --has_findings--> prepare_patch -> patch_agent -> validate
               |                         ^                            |
               |                         +-------- continue ----------+
               +--clean--> report <------------------ done -----------+

- `scan` (function): copies the target to a throwaway working copy and
  records the baseline: Bandit findings plus the project's own tests,
  including exploit tests.
- `prepare_patch` (function): packs the open findings, the source, the
  failing tests and the last rejection reason into the agent's input.
- `patch_agent` (single-turn Gemini Agent): returns one structured
  `PatchProposal` (file, complete new content, rationale). It never sees or
  changes anything outside its input.
- `validate` (function): the deterministic gate. Re-runs Bandit and every test
  on a scratch copy and keeps the patch only if it resolves a finding without
  regressions or new findings. Loops while findings remain and the
  MAX_PATCH_ATTEMPTS budget lasts.
- `report` (function): the unified diff and evidence for human review.

The target is never modified, committed or pushed.
"""

import os
import tempfile
from collections.abc import Iterator, MutableMapping
from pathlib import Path
from typing import Any

from google.adk import Agent, Context, Event, Workflow
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types
from pydantic import BaseModel, Field

from . import config, scanner, validator, workspace

_SOURCE_KEY = "cm_source_dir"
_WORK_KEY = "cm_work_dir"
_APPLIED_KEY = "cm_applied"
_BASELINE_KEY = "cm_baseline"
_CURRENT_KEY = "cm_current"
_ATTEMPTS_KEY = "cm_attempts"
_FEEDBACK_KEY = "cm_feedback"
_ERROR_KEY = "cm_error"
_RUN_KEYS = (
    _SOURCE_KEY,
    _WORK_KEY,
    _APPLIED_KEY,
    _BASELINE_KEY,
    _CURRENT_KEY,
    _ATTEMPTS_KEY,
    _FEEDBACK_KEY,
    _ERROR_KEY,
)
# Upper bound on source text packed into one patch request.
MAX_CONTEXT_CHARS = 120_000

State = MutableMapping[str, Any]


# ---------------------------------------------------------------------------
# Configuration (defaults live in .env.example)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Schemas for the LLM node
# ---------------------------------------------------------------------------


class PatchRequest(BaseModel):
    """Everything the patch agent needs for one attempt."""

    static_findings: list[dict[str, Any]] = Field(
        description="Open Bandit findings: file, line, check id, issue, code."
    )
    failing_tests: list[dict[str, str]] = Field(
        description="Failing exploit/regression tests and their messages."
    )
    source_files: dict[str, str] = Field(
        description="Patchable source files (path -> current content)."
    )
    test_files: dict[str, str] = Field(
        description="Test files that define the attacks. Read-only."
    )
    previous_attempt_feedback: str = Field(
        description="Why the last proposal was rejected, or empty."
    )
    attempts_remaining: int


class PatchProposal(BaseModel):
    """One file-level fix."""

    file_path: str = Field(
        description="Source file to replace, relative to the target root."
    )
    new_content: str = Field(description="The COMPLETE new file content.")
    rationale: str = Field(
        description="One or two sentences: root cause and why this fixes it."
    )


# ---------------------------------------------------------------------------
# Deterministic steps (plain functions over session state, unit-tested)
# ---------------------------------------------------------------------------


def _attempts(state: State) -> list[dict[str, Any]]:
    return list(state.get(_ATTEMPTS_KEY) or [])


def _work_dir(state: State) -> Path:
    """The working copy, rebuilt from state if this instance lacks it."""
    work = state.get(_WORK_KEY)
    if not work or not Path(work).is_dir():
        work = workspace.create_working_copy(
            Path(state[_SOURCE_KEY]), state.get(_APPLIED_KEY) or {}
        )
        state[_WORK_KEY] = str(work)
    return Path(work)


def _is_clean(scan_result: dict[str, Any]) -> bool:
    return not scan_result["findings"] and not scanner.failed_tests(scan_result)


def start_run(state: State) -> dict[str, Any]:
    """Starts a fresh run: new working copy, baseline scan, empty budget.

    Returns:
        `{"route": "has_findings" | "clean", "summary": {...}}`, or a route of
        "clean" with an `error` if the target cannot be used.
    """
    for key in _RUN_KEYS:
        state[key] = None
    try:
        source = workspace.resolve_target(os.getenv("TARGET_DIR") or None)
        state[_SOURCE_KEY] = str(source)
        work = workspace.create_working_copy(source, {})
    except workspace.WorkspaceError as exc:
        state[_ERROR_KEY] = str(exc)
        return {"route": "clean", "error": str(exc)}
    state[_WORK_KEY] = str(work)
    state[_APPLIED_KEY] = {}
    state[_ATTEMPTS_KEY] = []
    state[_FEEDBACK_KEY] = ""
    baseline = _scan(work)
    state[_BASELINE_KEY] = baseline
    state[_CURRENT_KEY] = baseline
    return {
        "route": "clean" if _is_clean(baseline) else "has_findings",
        "summary": scanner.summarize(baseline),
    }


def _test_module(test_id: str) -> str:
    """Maps a unittest id (pkg.mod.Class.test) or pytest id to its file."""
    if "::" in test_id:
        return test_id.split("::", 1)[0]
    return test_id.rsplit(".", 2)[0].replace(".", "/") + ".py"


def build_patch_request(state: State) -> dict[str, Any]:
    """Packs the current evidence into a `PatchRequest` dict."""
    work = _work_dir(state)
    current = state[_CURRENT_KEY]
    summary = scanner.summarize(current)
    files = workspace.list_files(work)
    flagged = {f["file"] for f in current["findings"]}
    # Files with static findings first, then the rest, within the budget.
    ordered = sorted(
        (f for f in files if not workspace.is_test_path(f)),
        key=lambda f: (f not in flagged, f),
    )
    sources: dict[str, str] = {}
    used = 0
    for rel in ordered:
        try:
            text = workspace.read_file(work, rel)
        except workspace.WorkspaceError:
            continue
        if used + len(text) > MAX_CONTEXT_CHARS and rel not in flagged:
            continue
        sources[rel] = text
        used += len(text)
    failing_modules = {
        _test_module(t["test"]) for t in summary["failing_tests"]
    }
    tests = {}
    for rel in files:
        if rel in failing_modules and used < MAX_CONTEXT_CHARS:
            tests[rel] = workspace.read_file(work, rel)
            used += len(tests[rel])
    return PatchRequest(
        static_findings=summary["static_findings"],
        failing_tests=summary["failing_tests"],
        source_files=sources,
        test_files=tests,
        previous_attempt_feedback=state.get(_FEEDBACK_KEY) or "",
        attempts_remaining=_max_attempts() - len(_attempts(state)),
    ).model_dump()


def apply_proposal(
    state: State, proposal: PatchProposal | dict[str, Any] | None
) -> dict[str, Any]:
    """Validates one proposal, keeps it if accepted, and picks the next route.

    Returns:
        The validator verdict plus `route` ("continue" or "done"), the
        remaining findings and the remaining budget.
    """
    parsed: PatchProposal | None = (
        PatchProposal.model_validate(proposal)
        if isinstance(proposal, dict)
        else proposal
    )
    work = _work_dir(state)
    before = state[_CURRENT_KEY]
    verdict: dict[str, Any]
    if parsed is None:
        verdict = {
            "accepted": False,
            "reasons": ["the model did not return a valid PatchProposal"],
            "resolved_findings": [],
            "fixed_tests": [],
            "regressions": [],
            "new_findings": [],
            "after": None,
        }
        file_path, rationale = "", ""
    else:
        file_path, rationale = parsed.file_path, parsed.rationale
        verdict = validator.validate_patch(
            work,
            file_path,
            parsed.new_content,
            before,
            _timeout_seconds(),
            _test_command(),
            _bandit_skips(),
        )
    after = verdict.pop("after")
    if verdict["accepted"] and parsed is not None:
        workspace.write_file(work, file_path, parsed.new_content)
        applied = dict(state.get(_APPLIED_KEY) or {})
        applied[file_path] = parsed.new_content
        state[_APPLIED_KEY] = applied
        state[_CURRENT_KEY] = after
        state[_FEEDBACK_KEY] = ""
        before = after
    else:
        state[_FEEDBACK_KEY] = (
            f"Your proposal for {file_path or '(none)'} was rejected: "
            + "; ".join(verdict["reasons"])
        )
    attempts = _attempts(state)
    attempts.append(
        {
            "attempt": len(attempts) + 1,
            "file": file_path,
            "rationale": rationale,
            **verdict,
        }
    )
    state[_ATTEMPTS_KEY] = attempts
    remaining = _max_attempts() - len(attempts)
    clean = _is_clean(before)
    return {
        **verdict,
        "file": file_path,
        "attempt": len(attempts),
        "attempts_remaining": remaining,
        "clean": clean,
        "route": "done" if clean or remaining <= 0 else "continue",
    }


def build_report(state: State) -> dict[str, Any]:
    """The review package: diff, findings fixed/remaining, tests, attempts."""
    if state.get(_ERROR_KEY) or state.get(_BASELINE_KEY) is None:
        return {"error": state.get(_ERROR_KEY) or "no baseline scan"}
    source = Path(state[_SOURCE_KEY])
    baseline = state[_BASELINE_KEY]
    current = state[_CURRENT_KEY]
    applied = state.get(_APPLIED_KEY) or {}
    diff = workspace.unified_diff(source, applied)
    # A private, per-run directory (mkdtemp is 0700 and unique), outside the
    # working copy so the patch is never scanned or tested as project code.
    patch_file = (
        Path(tempfile.mkdtemp(prefix="code-security-patch-"))
        / "security-fix.patch"
    )
    patch_file.write_text(diff, encoding="utf-8")
    base_counts = scanner.finding_counts(baseline)
    now_counts = scanner.finding_counts(current)
    base_failed = scanner.failed_tests(baseline)
    now_passed = scanner.passed_tests(current)
    attempts = _attempts(state)
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


def render_report(report: dict[str, Any]) -> str:
    """Formats the report as Markdown for chat surfaces."""
    if "error" in report:
        return f"Could not scan the target: {report['error']}"
    lines = [f"## Security fix report: `{report['target']}`", ""]
    lines += [
        "| Evidence | Before | After |",
        "| --- | --- | --- |",
        f"| Bandit findings | {report['static_findings_before']} |"
        f" {report['static_findings_after']} |",
        f"| Tests passing | {report['tests_before']['passed']}/"
        f"{report['tests_before']['total']} |"
        f" {report['tests_after']['passed']}/{report['tests_after']['total']} |",
        "",
        f"Patch attempts: {report['attempts_used']} of"
        f" {report['attempt_budget']}.",
        "",
    ]
    if report["static_findings_fixed"]:
        lines.append(
            "**Fixed (Bandit):** "
            + ", ".join(f"`{k}`" for k in report["static_findings_fixed"])
        )
        lines.append("")
    if report["exploit_tests_fixed"]:
        lines.append(
            "**Exploit tests now passing:** "
            + ", ".join(
                f"`{t.rsplit('.', 1)[-1]}`"
                for t in report["exploit_tests_fixed"]
            )
        )
        lines.append("")
    remaining = (
        report["static_findings_remaining"] + report["tests_still_failing"]
    )
    lines.append(
        "**Still open (needs a human):** "
        + (", ".join(f"`{r}`" for r in remaining) if remaining else "none")
    )
    lines += [
        "",
        "| # | File | Verdict | Rationale / reason |",
        "| --- | --- | --- | --- |",
    ]
    for a in report["attempts"]:
        detail = a["rationale"] if a["accepted"] else "; ".join(a["reasons"])
        verdict = "accepted" if a["accepted"] else "rejected"
        lines.append(
            f"| {a['attempt']} | `{a['file']}` | {verdict} |"
            f" {detail.replace('|', '/')} |"
        )
    lines += [
        "",
        "```diff",
        report["diff"].rstrip() or "# no accepted changes",
        "```",
        "",
        "Nothing was committed or pushed. Review this patch like any other"
        " change before applying it (`git apply security-fix.patch`).",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Workflow nodes
# ---------------------------------------------------------------------------


def _say(text: str) -> Event:
    """A user-visible progress message (shown in chat and over A2A)."""
    return Event(
        content=types.Content(role="model", parts=[types.Part(text=text)])
    )


def scan(ctx: Context) -> Iterator[Event]:
    """Baseline: Bandit + the project's tests on a fresh working copy."""
    result = start_run(ctx.state)
    if "error" in result:
        yield _say(f"Cannot use the target: {result['error']}")
    else:
        s = result["summary"]
        yield _say(
            f"Baseline: {len(s['static_findings'])} Bandit findings,"
            f" {len(s['failing_tests'])} failing tests"
            f" ({s['tests_passed']}/{s['tests_total']} passing)."
        )
    yield Event(output=result, route=result["route"])


def prepare_patch(ctx: Context) -> Event:
    """Builds the patch agent's input from the current evidence."""
    return Event(output=build_patch_request(ctx.state))


def validate(ctx: Context, node_input: Any) -> Iterator[Event]:
    """Deterministic gate for the proposal coming from `patch_agent`."""
    result = apply_proposal(ctx.state, node_input)
    if result["accepted"]:
        fixed = len(result["resolved_findings"]) + len(result["fixed_tests"])
        yield _say(
            f"Attempt {result['attempt']}: `{result['file']}` accepted"
            f" ({fixed} findings resolved, no regressions)."
        )
    else:
        yield _say(
            f"Attempt {result['attempt']}: `{result['file'] or '(none)'}`"
            f" rejected: {'; '.join(result['reasons'])}"
        )
    yield Event(output=result, route=result["route"])


def report(ctx: Context) -> Iterator[Event]:
    """Final review package: Markdown for people, a dict for programs."""
    result = build_report(ctx.state)
    yield _say(render_report(result))
    yield Event(output={k: v for k, v in result.items() if k != "diff"})


PATCH_INSTRUCTION = """\
You are an application-security engineer fixing vulnerabilities. Your input
is a JSON PatchRequest with the open Bandit findings, the failing exploit and
regression tests, the current source files, the read-only test files, and the
reason your previous proposal was rejected (if any).

Pick ONE root cause (highest severity first; a vulnerability often shows up
as both a Bandit finding and a failing test) and return a PatchProposal for
the single source file that fixes it:
- Fix the vulnerability class, not just one payload: parameterized SQL;
  resolve paths and check they stay inside the base directory; no shell,
  pass an argv list and validate input with re.fullmatch; JSON instead of
  pickle; read secrets from the environment and fail closed.
- Keep public function signatures and behaviour for legitimate input. Use
  only the standard library and modules that already exist in the project.
- `new_content` is the COMPLETE file. Never edit tests, never add `# nosec`.
- If previous_attempt_feedback is set, address it; do not resubmit the same
  content.
A deterministic validator decides whether your patch is kept.
"""


def create_patch_agent() -> Agent:
    """The only LLM step: one structured patch proposal per call."""
    return Agent(
        name="patch_agent",
        model=Gemini(
            model=os.getenv("MODEL_NAME"),
            retry_options=types.HttpRetryOptions(attempts=3),
        ),
        mode="single_turn",
        include_contents="none",
        description="Proposes one file-level security fix.",
        instruction=PATCH_INSTRUCTION,
        input_schema=PatchRequest,
        output_schema=PatchProposal,
    )


def create_workflow(patcher: Any | None = None) -> Workflow:
    """Builds the detect -> patch -> validate graph.

    Args:
        patcher: Node that turns a PatchRequest into a PatchProposal.
            Defaults to the Gemini patch agent; tests pass a deterministic
            function node instead.
    """
    patch_node = patcher if patcher is not None else create_patch_agent()
    return Workflow(
        name="code_security_fixer",
        description=(
            "Fixes security findings in a Python codebase with a"
            " detect -> patch -> validate loop: Bandit and the project's own"
            " exploit tests gate every model-written patch. Returns a unified"
            " diff and evidence for human review; never commits."
        ),
        edges=[
            ("START", scan, {"has_findings": prepare_patch, "clean": report}),
            (prepare_patch, patch_node, validate),
            (validate, {"continue": prepare_patch, "done": report}),
        ],
    )


root_agent = create_workflow()

app = App(
    root_agent=root_agent,
    name="app",
)
