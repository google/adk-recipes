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
"""Every in-repo `uses:` must point at a file that exists and can be called.

A `uses:` clause naming a workflow in this repository is resolved by GitHub
at run time, not at lint time. Rename or delete the callee and nothing goes
red until the caller next fires — which for the scheduled lanes can be hours
later, and for `workflow_dispatch` only when someone asks.

actionlint checks each file in isolation and cannot see across the pair.
zizmor audits the caller's syntax, not whether the target resolves. So the
two sides of every internal call sit in different files with nothing joining
them.

This is not hypothetical. Rewriting these callers from `./...` to GitHub's
`$/...` self-repository form broke the caller/callee pairing in
test_ai_workflow_hardening.py, which matches on the `./` prefix; the
org-level zizmor scan then rejected `$/` outright because it pins a version
that predates the syntax. A change of prefix is exactly the kind of edit
that looks local and is not.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# `uses:` forms that name this repository rather than an external action.
# `./` is workspace-relative; `$/` is GitHub's self-repository syntax, not
# usable here yet but accepted so this test keeps working if that changes.
LOCAL_PREFIXES = ("./", "$/")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _triggers(doc: dict) -> dict:
    """The `on:` block.

    PyYAML resolves a bare `on` key to the boolean True, so a plain
    `doc["on"]` misses it on every workflow that does not quote the key.
    """
    for key in ("on", True):
        value = doc.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def _local_uses(doc: dict) -> list[tuple[str, str]]:
    """Every in-repo `uses:` in a workflow, as (location, target) pairs."""
    found: list[tuple[str, str]] = []
    for job_name, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue

        uses = job.get("uses")
        if isinstance(uses, str) and uses.startswith(LOCAL_PREFIXES):
            found.append((f"job {job_name}", uses))

        for index, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            step_uses = step.get("uses")
            if isinstance(step_uses, str) and step_uses.startswith(
                LOCAL_PREFIXES
            ):
                found.append((f"job {job_name} step {index}", step_uses))
    return found


def _resolve(target: str) -> Path:
    """The on-disk path a local `uses:` names, ref stripped."""
    # A local reference carries no `@ref`, but tolerate one rather than
    # turning a stray ref into a confusing missing-file failure.
    path = target.split("@", maxsplit=1)[0]
    for prefix in LOCAL_PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    return REPO_ROOT / path


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_local_uses_targets_exist(path: Path) -> None:
    """A `uses:` naming this repo resolves to a file that is actually here."""
    for location, target in _local_uses(_load(path)):
        resolved = _resolve(target)
        assert resolved.is_file(), (
            f"{path.name}: {location} uses '{target}', which resolves to "
            f"{resolved.relative_to(REPO_ROOT)} — no such file. The call "
            f"will fail at run time, not at lint time."
        )


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_called_workflows_accept_workflow_call(path: Path) -> None:
    """A job-level `uses:` target declares `workflow_call`.

    A reusable workflow that loses its `workflow_call` trigger still parses,
    still passes actionlint, and fails every caller the next time one runs.
    """
    doc = _load(path)
    for job_name, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        if not (isinstance(uses, str) and uses.startswith(LOCAL_PREFIXES)):
            continue

        resolved = _resolve(uses)
        if not resolved.is_file():
            continue  # test_local_uses_targets_exist owns this failure.

        triggers = _triggers(_load(resolved))
        assert "workflow_call" in triggers, (
            f"{path.name}: job {job_name} calls {resolved.name}, which does "
            f"not declare a `workflow_call` trigger. Every caller of it "
            f"fails the next time it runs."
        )


def test_the_ai_lanes_are_still_covered() -> None:
    """At least one job-level in-repo call exists to be checked.

    Both tests above pass vacuously if the set of callers is ever empty —
    which is precisely what a prefix change does. Anchor them to the AI
    lanes, which are the reason this file exists.
    """
    callers = [
        path.name
        for path in _workflow_files()
        for _, target in _local_uses(_load(path))
        if "_ai-" in target
    ]
    assert len(callers) >= 6, (
        f"expected the AI review and triage lanes to call shared core "
        f"workflows; found {len(callers)} such calls: {sorted(callers)}"
    )
