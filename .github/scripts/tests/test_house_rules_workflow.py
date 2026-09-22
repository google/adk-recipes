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
"""Pin the four properties that make the house-rules lane safe.

It is the only AI-review workflow that checks out the pull request's own code.
That is a deliberate trade, and it holds only while all four of these are true:

  1. the job that sees PR code holds no secret and no write token
  2. nothing from the PR is executed, imported or installed
  3. the checker comes from the base checkout, not the PR's copy
  4. posting happens in a job that never saw the PR's code

Each is one edit away from being false, and none of them would fail loudly:
a workflow that adds `uv sync` in the wrong directory works perfectly right up
until someone points a hostile PR at it. So they are asserted here rather than
described in a comment nobody re-reads.
"""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "ai-pr-review-house-rules.yml"
)


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert WORKFLOW.exists(), f"{WORKFLOW.name} is gone; delete these tests too"
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def raw() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _steps(job: dict) -> list[dict]:
    return [s for s in job.get("steps", []) if isinstance(s, dict)]


def _run_scripts(job: dict) -> str:
    return "\n".join(str(s.get("run", "")) for s in _steps(job))


def _with(step: dict) -> dict:
    """A step's `with:` block. `run:` steps have none, and `with:` present but
    empty parses as None, so neither may be an AttributeError here."""
    return step.get("with") or {}


# `secrets.FOO` and `secrets['FOO']` are the same reference, and GitHub
# resolves context names case-insensitively, so `SECRETS.FOO` is too. Matching
# only the lowercase dotted spelling leaves two ways to hand this job a
# credential without tripping the test that exists to stop exactly that.
SECRET_REF = re.compile(r"secrets\s*(?:\.\s*[A-Za-z_]\w*|\[)", re.IGNORECASE)


def _secret_reference(node) -> str | None:
    """The first secret reference anywhere in a YAML subtree, or None.

    Serialising and searching beats walking the structure: a secret can arrive
    through `env:`, `with:`, an action input, a `run:` body or a job-level
    `secrets: inherit`, and enumerating those is how one gets missed.
    """
    if node is None:
        return None
    text = yaml.dump(node, default_flow_style=False)
    m = SECRET_REF.search(text)
    if m:
        return m.group(0)
    # `secrets: inherit` on a reusable-workflow call names no secret and so
    # matches nothing above, while handing over every secret there is.
    if isinstance(node, dict) and str(node.get("secrets", "")) == "inherit":
        return "secrets: inherit"
    return None


def _write_scopes(permissions) -> set[str]:
    """Every scope a `permissions:` value grants beyond read.

    Three shapes are legal and all three have to be handled: omitted (inherits
    the workflow default, which this file pins to `{}`), a bare string
    (`read-all` / `write-all`), or a mapping. Indexing straight into it
    assumed the mapping and turned a genuine `write-all` regression into a
    KeyError or an AttributeError somewhere unrelated.
    """
    if permissions is None or permissions == {}:
        return set()
    if isinstance(permissions, str):
        return set() if permissions == "read-all" else {permissions}
    return {
        scope
        for scope, level in permissions.items()
        if str(level) not in ("read", "none")
    }


# ------------------------------------------------------- 1. no credentials


def test_the_checking_job_cannot_post(workflow):
    perms = workflow["jobs"]["check"]["permissions"]
    assert perms.get("pull-requests") == "read", (
        "the job that reads PR code must not be able to write to the PR"
    )
    assert perms.get("contents") == "read"
    assert "issues" not in perms, "issues: write would let it comment"


def test_the_checking_job_holds_no_secret(raw):
    """No `secrets.` reference anywhere in the check job.

    Checked as text over the job's span rather than per-step: a secret can
    enter through `env:`, `with:`, or an action input, and enumerating the
    places it could hide is how one gets missed.
    """
    check_span = raw.split("jobs:", 1)[1].split("\n  post:", 1)[0]
    leaked = SECRET_REF.findall(check_span)
    assert not leaked, (
        f"the check job references {sorted(set(leaked))}. It sees untrusted "
        "code; it must hold nothing worth stealing."
    )


def test_the_top_level_grants_nothing(workflow):
    assert workflow["permissions"] == {}, (
        "a top-level grant is handed to every job, including the one that "
        "reads the pull request's code"
    )


# --------------------------------------------- 2. nothing from the PR runs

# Anything that would execute, import or install code out of the checkout.
FORBIDDEN_IN_CHECK = (
    "uv sync",
    "uv run",
    "uv pip install -r",
    "pip install -e",
    "pip install -r",
    "poetry install",
    "npm install",
    "npm ci",
    "make ",
    "docker build",
    "pytest",
)


def test_the_check_job_never_runs_anything_from_the_pr(workflow):
    script = _run_scripts(workflow["jobs"]["check"])
    for forbidden in FORBIDDEN_IN_CHECK:
        assert forbidden not in script, (
            f"{forbidden!r} appears in the check job. It has a checkout of "
            "untrusted code; running a build or a test out of it hands a "
            "hostile PR arbitrary execution."
        )


def test_the_only_install_is_pinned_and_from_pypi(workflow):
    """PyYAML by exact version, from the index — never from the PR's own
    pyproject.toml, lockfile or vendored wheels."""
    script = _run_scripts(workflow["jobs"]["check"])
    installs = re.findall(r"pip install[^\n]*", script)
    assert len(installs) == 1, f"expected exactly one install, got {installs}"
    assert re.search(r'"pyyaml==\d+\.\d+(\.\d+)?"', installs[0]), (
        f"the install is not a pinned PyYAML: {installs[0]!r}"
    )
    assert "pr-head" not in installs[0]


def test_the_pr_checkout_carries_no_credentials(workflow):
    for step in _steps(workflow["jobs"]["check"]):
        if "actions/checkout" in str(step.get("uses", "")):
            assert step["with"]["persist-credentials"] is False, (
                "a token left in .git/config is readable by anything that "
                "runs in that directory"
            )


# ------------------------------------------ 3. the checker is the base copy


def test_the_checker_comes_from_the_base_checkout(workflow):
    script = _run_scripts(workflow["jobs"]["check"])
    assert "--checker base/.agents/skills/github-pr-review" in script, (
        "the checker must be the base branch's copy. Reading it out of "
        "pr-head/ would let a PR rewrite the script that reviews it."
    )
    assert "--checker pr-head" not in script


def test_the_pr_tree_is_only_ever_the_subject(workflow):
    """`pr-head` may be passed as data (--repo-root) and never invoked."""
    script = _run_scripts(workflow["jobs"]["check"])
    for match in re.findall(r"python3?\s+(\S+)", script):
        assert not match.startswith("pr-head"), (
            f"running {match!r} executes code from the pull request"
        )


def test_only_the_pr_checkout_opts_out_of_the_fork_guard(workflow):
    """`allow-unsafe-pr-checkout` is what lets a FORK pull request be checked
    at all -- without it org policy blocks the step and the lane is dead on
    most contrib PRs, which is how it failed on PR #2612. It is safe here only
    because of invariants 1-4, so it must sit on the PR-head checkout and
    nowhere else: on the base checkout it is meaningless, and in any other job
    it would mean that job has fork code in front of it."""
    opted = [
        _with(step).get("path")
        for step in _steps(workflow["jobs"]["check"])
        if "actions/checkout" in str(step.get("uses", ""))
        and _with(step).get("allow-unsafe-pr-checkout") is True
    ]
    assert opted == ["pr-head"], (
        f"expected the opt-in on the pr-head checkout alone, got {opted}"
    )
    for name, job in workflow["jobs"].items():
        if name == "check":
            continue
        for step in _steps(job):
            assert "allow-unsafe-pr-checkout" not in _with(step), (
                f"job {name!r} opts into a fork checkout. Only `check` is "
                "built to hold PR code, and only because it holds nothing else"
            )


def test_the_fork_opt_in_is_paid_for_by_the_read_only_token(workflow):
    """The opt-in and invariant 1 are one decision, not two. If the check job
    ever gains a write scope or a secret, the opt-in has to go with it, and
    this is the assertion that makes that impossible to miss."""
    check = workflow["jobs"]["check"]
    opted_in = any(
        _with(step).get("allow-unsafe-pr-checkout") is True
        for step in _steps(check)
    )
    if not opted_in:
        pytest.skip("no fork opt-in to justify")

    writable = _write_scopes(check.get("permissions"))
    assert not writable, (
        f"check can write {sorted(writable)}. A job that checks out fork "
        "code under pull_request_target must be read-only"
    )

    # Workflow-level `env` and `defaults` are inherited by every job, so a
    # secret parked there is in front of the fork's code just as surely as one
    # written inside the job.
    for label, node in (
        ("the check job", check),
        ("workflow-level env", workflow.get("env")),
        ("workflow-level defaults", workflow.get("defaults")),
    ):
        ref = _secret_reference(node)
        assert ref is None, (
            f"{label} reads a secret ({ref!r}) while fork code is on disk"
        )


def test_both_checkouts_are_separate_directories(workflow):
    paths = [
        step["with"]["path"]
        for step in _steps(workflow["jobs"]["check"])
        if "actions/checkout" in str(step.get("uses", ""))
    ]
    assert paths == ["base", "pr-head"], (
        f"expected a base and a pr-head checkout, got {paths}. Checking the "
        "PR out over the base would substitute the PR's checker for ours."
    )


# ---------------------------------------------------- 4. posting is split


def test_posting_happens_in_a_job_with_no_checkout(workflow):
    post = workflow["jobs"]["post"]
    assert post["permissions"].get("pull-requests") == "write"
    for step in _steps(post):
        assert "actions/checkout" not in str(step.get("uses", "")), (
            "the posting job holds a write token; it must never have the "
            "pull request's code in front of it"
        )


def test_the_post_job_waits_for_the_check_job(workflow):
    assert workflow["jobs"]["post"]["needs"] == ["check"]


def test_a_dry_run_posts_nothing(workflow):
    script = _run_scripts(workflow["jobs"]["post"])
    assert "if [[ \"${DRY_RUN}\" == 'true' ]]; then" in script
    body = script.split("DRY_RUN", 1)[1]
    assert body.index("exit 0") < body.index("--method POST"), (
        "the dry-run branch must return before anything is posted"
    )


# ------------------------------- the helpers the invariants are asserted with
#
# The bypasses below are not in the workflow today. That is the point: these
# assertions are the reason a future edit introducing one would be caught
# rather than quietly passing a test that only ever saw the good case.


def test_with_survives_a_step_that_has_none():
    assert _with({"run": "echo hi"}) == {}
    assert _with({"uses": "x", "with": None}) == {}
    assert _with({"with": {"path": "base"}}) == {"path": "base"}


@pytest.mark.parametrize(
    "permissions,expected",
    [
        (None, set()),  # omitted: inherits the top-level {}
        ({}, set()),
        ("read-all", set()),
        ({"contents": "read", "pull-requests": "read"}, set()),
        ({"contents": "read", "pull-requests": "none"}, set()),
        ("write-all", {"write-all"}),
        ({"contents": "read", "pull-requests": "write"}, {"pull-requests"}),
        ({"id-token": "write"}, {"id-token"}),
    ],
)
def test_write_scopes_handles_every_legal_permissions_shape(
    permissions, expected
):
    assert _write_scopes(permissions) == expected


@pytest.mark.parametrize(
    "node",
    [
        {"env": {"K": "${{ secrets.APP_PRIVATE_KEY }}"}},
        {"env": {"K": "${{ SECRETS.APP_PRIVATE_KEY }}"}},  # contexts are
        {"env": {"K": "${{ Secrets.App_Private_Key }}"}},  # case-insensitive
        {"env": {"K": "${{ secrets['APP_PRIVATE_KEY'] }}"}},  # bracket access
        {"env": {"K": "${{ secrets . APP_PRIVATE_KEY }}"}},  # spaced
        {"steps": [{"uses": "a/b", "with": {"key": "${{ secrets.X }}"}}]},
        {"steps": [{"run": "echo ${{ secrets.X }}"}]},
        {"secrets": "inherit"},  # names nothing, hands over everything
    ],
)
def test_secret_reference_catches_every_spelling(node):
    assert _secret_reference(node) is not None, f"missed a secret in {node}"


@pytest.mark.parametrize(
    "node",
    [
        None,
        {},
        {"env": {"GH_TOKEN": "${{ github.token }}"}},
        {"run": "python3 base/.github/scripts/house_rules_lane.py"},
        # A word ending in "secrets" is not a reference to the context, and a
        # test that fired on prose would be turned off within the week.
        {"name": "No secrets are read here"},
        {"secrets": "none"},
    ],
)
def test_secret_reference_does_not_fire_on_innocent_yaml(node):
    assert _secret_reference(node) is None, f"false positive on {node}"
