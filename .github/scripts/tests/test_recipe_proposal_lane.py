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
"""Recipe proposals are reviewed by humans, not by the AI workflows.

Each of these pins a mechanism that fails SILENTLY: the YAML still parses,
every check stays green, and the only symptom is a bot replying to a
contributor's proposal weeks later.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
POLICY = ROOT / ".github" / "policy.yml"
TEMPLATE = ROOT / ".github" / "ISSUE_TEMPLATE" / "propose-a-new-recipe.md"
MARKER = "[RECIPE PROPOSAL]"


def _yaml(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _scripts(job: dict) -> str:
    return "".join(step.get("run") or "" for step in job["steps"])


def test_every_ai_issue_workflow_skips_proposals():
    """Each gate is a couple of lines in one file and deleting any of them
    restores the behaviour with nothing going red."""
    quick = _yaml("ai-issue-quick-response.yml")
    assert MARKER in _scripts(quick["jobs"]["resolve"])
    assert "skip" in str(quick["jobs"]["respond"].get("if"))

    triage = _yaml("ai-issue-automated-triage.yml")
    assert MARKER in _scripts(triage["jobs"]["resolve"])
    assert "skip" in str(triage["jobs"]["triage"].get("if"))

    # The on-open gates are not enough: the 4-hourly sweep reaches every
    # issue that has no `status/ai-triaged` marker, which is every proposal.
    sweep = _yaml("ai-issue-scheduled-triage.yml")
    assert MARKER in _scripts(sweep["jobs"]["find"])


def test_every_gate_folds_case():
    """A gate that compares case-sensitively lets `[Recipe Proposal]` reach
    the model while every other workflow still treats it as a proposal —
    which is exactly what happened to the automated-triage gate in review.

    Bash folds with `${VAR^^}`, jq with `ascii_upcase`.
    """
    for name, job in (
        ("ai-issue-quick-response.yml", "resolve"),
        ("ai-issue-automated-triage.yml", "resolve"),
        ("recipe-proposal-intake.yml", "route"),
    ):
        # The COMPARISON line itself must fold, not merely some comment
        # nearby mentioning that it does.
        lines = [
            ln
            for ln in _scripts(_yaml(name)["jobs"][job]).splitlines()
            if MARKER in ln and not ln.strip().startswith("#")
        ]
        assert lines, f"{name}: no {MARKER} comparison found at all"
        for line in lines:
            assert "^^" in line, (
                f"{name}: case-sensitive comparison, so `[Recipe Proposal]` "
                f"would slip past this gate:\n  {line.strip()}"
            )

    # The sweep's jq splits the fold and the literal across two lines, so
    # this matches the whole expression rather than one line of it.
    sweep = _scripts(_yaml("ai-issue-scheduled-triage.yml")["jobs"]["find"])
    sweep = " ".join(
        ln for ln in sweep.splitlines() if not ln.strip().startswith("#")
    )
    assert re.search(
        r'ascii_upcase\s*\)\s*\|\s*contains\(\s*"\[RECIPE PROPOSAL\]"',
        sweep,
    ), "the scheduled sweep no longer upper-cases the title before matching"


def test_intake_takes_its_assignees_from_policy():
    """Hardcoding the names in the workflow would work and would silently
    put the source of truth in two places."""
    script = _scripts(_yaml("recipe-proposal-intake.yml")["jobs"]["route"])
    assert "recipe_proposals.assignees" in script


def test_policy_and_the_issue_template_name_the_same_people():
    """Two paths assign a proposal: the template (for issues filed through
    it) and intake (for everything else). If they disagree, who reviews a
    proposal depends on how it was filed — and nothing else would catch it.
    """
    policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    _, front, _ = TEMPLATE.read_text(encoding="utf-8").split("---", 2)
    front = yaml.safe_load(front)

    template_assignees = [n.strip() for n in str(front["assignees"]).split(",")]
    template_labels = [n.strip() for n in str(front["labels"]).split(",")]

    assert template_assignees == policy["recipe_proposals"]["assignees"]
    assert policy["recipe_proposals"]["label"] in template_labels
    assert MARKER in front["title"]


def test_intake_never_comments():
    """Intake holds `issues: write`; nothing but this stops a comment being
    added here later."""
    script = _scripts(_yaml("recipe-proposal-intake.yml")["jobs"]["route"])
    assert "gh issue comment" not in script


def test_the_failure_notifier_stays_quiet_on_proposals():
    """`resolve` can fail before any skip decision exists — an API blip —
    and `notify-failure` comments on whatever is in its `needs`. Without
    this guard that failure posts a bot comment on a recipe proposal, which
    is the symptom the lane was built to stop."""
    for name in (
        "ai-issue-automated-triage.yml",
        "ai-issue-quick-response.yml",
    ):
        steps = _yaml(name)["jobs"]["notify-failure"]["steps"]
        script = "".join(
            str((s.get("with") or {}).get("script") or "") for s in steps
        )
        assert "RECIPE PROPOSAL" in script.upper(), (
            f"{name}: notify-failure will comment on a recipe proposal when "
            "an upstream job fails"
        )
