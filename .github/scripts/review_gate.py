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
"""
Hold review requests until a pull request is ready for review.

"Ready" means no failing checks. Unresolved review threads, from bots or
humans, never hold a review. Configured in .github/review-gate-config.yml.
Invoked by
.github/workflows/review-gate.yml, either for one PR (a review was just
requested) or as a sweep over every open PR (on a schedule).

What it does
------------
For each PR it decides one of:

  bounce   A reviewer is requested but the PR is not ready. Remove the
           requested reviewers, remember who they were, label the PR, and
           post (or update) one comment listing what is wrong.
  release  The PR was bounced and is now ready. Re-request the remembered
           reviewers, drop the label, and update the comment.
  update   Still bounced, but the list of problems changed. Edit the comment.
  none     Anything else, including checks that are still running.

Running checks never cause a bounce. People request a review seconds after
pushing; bouncing on "not finished yet" would bounce nearly everyone.

State
-----
The gate's own PR comment is the source of truth. A hidden marker at the top
carries the reviewers it removed, so a release knows whom to put back. The
label only exists so humans can filter on it. Only a marker written by the
gate's own account (`GATE_LOGIN`) is trusted; anyone can paste
a marker into a comment.

Usage
-----
GITHUB_REPOSITORY must name the repository (Actions sets it; set it by hand
for a local run).

  python .github/scripts/review_gate.py --pr 123
  python .github/scripts/review_gate.py --sweep
  python .github/scripts/review_gate.py --sweep --dry-run

Requires: `gh` on PATH, GITHUB_TOKEN in the environment, PyYAML.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[1] / "review-gate-config.yml"

# Assigned by main() from GITHUB_REPOSITORY, which is required: this script
# writes to pull requests, so a run must name its target rather than fall
# back to one.
REPO = ""

# The account GITHUB_TOKEN posts as. Only markers written by it are trusted,
# since anyone can paste a marker into a comment. A constant rather than
# config: GitHub fixes this identity, and the check that decides whose state
# the gate trusts should not be something a config edit can change.
GATE_LOGIN = "github-actions[bot]"

MARKER_RE = re.compile(r"<!-- review-gate (\{.*?\}) -->")

FAILED_CONCLUSIONS = {
    "FAILURE",
    "TIMED_OUT",
    "STARTUP_FAILURE",
    "ACTION_REQUIRED",
}
FAILED_STATES = {"FAILURE", "ERROR"}
PENDING_STATES = {"PENDING", "EXPECTED"}

# Sorts after every ISO-8601 timestamp.
NOT_STARTED = "~"

# Team reviewers are deliberately absent: the GraphQL `Team.slug` field needs
# `read:org`, which GITHUB_TOKEN never has, and asking for it fails the whole
# query. Requested reviewers are read over REST instead, which returns team
# slugs without that scope. Only the count is needed here.
#
# `first: 100` on contexts is the connection maximum. A PR past that limit is
# evaluated on what fits, which can only let a review through that should
# have been held — the harmless direction.
PR_FIELDS = """
  number
  state
  isDraft
  labels(first: 50) { nodes { name } }
  reviewRequests { totalCount }
  commits(last: 1) {
    nodes {
      commit {
        statusCheckRollup {
          contexts(first: 100) {
            nodes {
              __typename
              ... on CheckRun {
                name
                status
                conclusion
                detailsUrl
                startedAt
                checkSuite { workflowRun { workflow { name } } }
              }
              ... on StatusContext {
                context
                state
                targetUrl
                createdAt
              }
            }
          }
        }
      }
    }
  }
"""

ONE_PR_QUERY = (
    """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {"""
    + PR_FIELDS
    + """
    }
  }
}
"""
)

# Pagination is done by hand rather than with `gh api graphql --paginate`,
# which follows the FIRST pageInfo it finds. The nested connections above
# would make that ambiguous.
OPEN_PRS_QUERY = (
    """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 20, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {"""
    + PR_FIELDS
    + """
      }
    }
  }
}
"""
)


class GhError(RuntimeError):
    """A `gh` invocation failed."""


@dataclass(frozen=True)
class Config:
    label: str
    ignored_workflows: tuple[str, ...]


@dataclass(frozen=True)
class Check:
    name: str
    url: str | None


@dataclass
class Reviewers:
    users: list[str] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.users or self.teams)

    def merged(self, other: Reviewers) -> Reviewers:
        return Reviewers(
            users=sorted(set(self.users) | set(other.users)),
            teams=sorted(set(self.teams) | set(other.teams)),
        )


@dataclass(frozen=True)
class Readiness:
    failing: list[Check]
    pending: bool

    @property
    def blocked(self) -> bool:
        return bool(self.failing)


@dataclass
class GateComment:
    comment_id: int
    body: str
    state: str
    stored: Reviewers


@dataclass(frozen=True)
class Decision:
    action: str  # bounce | release | update | none
    reason: str


# ---------------------------------------------------------------------------
# Pure logic. Everything below up to the API section is unit-tested.
# ---------------------------------------------------------------------------


def load_config(path: Path = CONFIG_PATH) -> Config:
    with open(path, "rb") as f:
        section = yaml.safe_load(f)
    return Config(
        label=section["label"],
        ignored_workflows=tuple(section.get("ignored_workflows") or ()),
    )


def evaluate_checks(
    contexts: list[dict], cfg: Config
) -> tuple[list[Check], bool]:
    """Return (failing checks, whether any check is still running).

    A re-run leaves the earlier attempt in the rollup, so each check is
    reduced to its most recent run first. Otherwise a failure that was fixed
    by re-running would keep the PR bounced forever.
    """
    latest: dict[tuple[str, str], dict] = {}
    for ctx in contexts:
        if ctx.get("__typename") == "CheckRun":
            workflow = (
                ((ctx.get("checkSuite") or {}).get("workflowRun") or {}).get(
                    "workflow"
                )
                or {}
            ).get("name") or ""
            if any(fnmatch.fnmatch(workflow, p) for p in cfg.ignored_workflows):
                continue
            key = (workflow, ctx.get("name") or "")
            # A queued re-run has not started yet, so it has no timestamp. It
            # is still the newest attempt and must supersede the one it
            # re-runs, or a failure already being retried would bounce.
            stamp = ctx.get("startedAt") or NOT_STARTED
        elif ctx.get("__typename") == "StatusContext":
            key = ("", ctx.get("context") or "")
            stamp = ctx.get("createdAt") or ""
        else:
            continue
        previous = latest.get(key)
        if previous is None or stamp >= previous["_stamp"]:
            latest[key] = {**ctx, "_stamp": stamp}

    failing: list[Check] = []
    pending = False
    for (workflow, name), ctx in sorted(latest.items()):
        label = f"{workflow} / {name}" if workflow else name
        if ctx["__typename"] == "CheckRun":
            if ctx.get("status") != "COMPLETED":
                pending = True
            elif ctx.get("conclusion") in FAILED_CONCLUSIONS:
                failing.append(Check(label, ctx.get("detailsUrl")))
        elif ctx.get("state") in PENDING_STATES:
            pending = True
        elif ctx.get("state") in FAILED_STATES:
            failing.append(Check(label, ctx.get("targetUrl")))
    failing.sort(key=lambda c: c.name)
    return failing, pending


def readiness(pr: dict, cfg: Config) -> Readiness:
    commits = (pr.get("commits") or {}).get("nodes") or []
    rollup = (
        commits[0]["commit"].get("statusCheckRollup") if commits else None
    ) or {}
    contexts = (rollup.get("contexts") or {}).get("nodes") or []
    failing, pending = evaluate_checks(contexts, cfg)
    return Readiness(failing, pending)


def decide(
    ready: Readiness,
    has_requests: bool,
    gate: GateComment | None,
) -> Decision:
    bounced = gate is not None and gate.state == "blocked"
    if ready.blocked:
        if has_requests:
            return Decision(
                "bounce", "review requested on a PR that is not ready"
            )
        if bounced:
            return Decision("update", "still not ready")
        return Decision("none", "not ready, but nobody is requested")
    if ready.pending:
        # A bounced PR stays bounced while its fix is still being checked.
        return Decision("none", "checks still running")
    if bounced:
        return Decision("release", "ready for review")
    return Decision("none", "ready, nothing to do")


def parse_marker(body: str) -> tuple[str, Reviewers] | None:
    match = MARKER_RE.search(body)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return data.get("state", ""), Reviewers(
        users=list(data.get("users") or []),
        teams=list(data.get("teams") or []),
    )


def marker(state: str, reviewers: Reviewers) -> str:
    data = {"state": state, "users": reviewers.users, "teams": reviewers.teams}
    return f"<!-- review-gate {json.dumps(data, sort_keys=True)} -->"


def _names(reviewers: Reviewers) -> str:
    # Backticks, not @-mentions: mentioning the reviewers here would notify
    # them about the very PR they were just taken off.
    names = [f"`{u}`" for u in reviewers.users]
    names += [f"`{t}` (team)" for t in reviewers.teams]
    return ", ".join(names)


def blocked_body(ready: Readiness, stored: Reviewers, label: str) -> str:
    lines = [
        marker("blocked", stored),
        "**This pull request isn't ready for review yet.**",
        "",
    ]
    if stored:
        lines += [
            f"I removed the review request for {_names(stored)} until the "
            "items below are fixed.",
            "",
        ]
    if ready.failing:
        lines.append(f"**Failing checks ({len(ready.failing)})**")
        for check in ready.failing:
            lines.append(
                f"- [{check.name}]({check.url})"
                if check.url
                else f"- {check.name}"
            )
        lines.append("")
    lines += [
        "Fix the failing checks. Once everything is green I will re-request "
        "the review automatically, so "
        "there is no need to ping anyone. This is re-checked every 15 minutes.",
        "",
        f"<sub>Applied by the review gate "
        f"(`.github/review-gate-config.yml`); the `{label}` label is removed "
        "on release."
        "</sub>",
    ]
    return "\n".join(lines)


def released_body(requested: Reviewers, failed: Reviewers) -> str:
    lines = [
        marker("released", Reviewers()),
        "**Ready for review.** All checks pass.",
    ]
    if requested:
        lines += ["", f"Re-requested review from {_names(requested)}."]
    if failed:
        lines += [
            "",
            f"Could not re-request {_names(failed)}; please add them back by "
            "hand.",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# API.
# ---------------------------------------------------------------------------


def gh(*args: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["gh", *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GhError(
            f"gh {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def graphql(query: str, **variables: str | int | None) -> dict:
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        flag = "-F" if isinstance(value, int) else "-f"
        args += [flag, f"{key}={value}"]
    return json.loads(gh(*args))["data"]


def rest_json(method: str, path: str, payload: dict | None = None) -> object:
    args = ["api", "-X", method, path]
    if payload is not None:
        args += ["--input", "-"]
    out = gh(*args, stdin=json.dumps(payload) if payload is not None else None)
    return json.loads(out) if out.strip() else None


def fetch_pr(number: int) -> dict:
    owner, name = REPO.split("/")
    data = graphql(ONE_PR_QUERY, owner=owner, name=name, number=number)
    return data["repository"]["pullRequest"]


def fetch_open_prs() -> list[dict]:
    owner, name = REPO.split("/")
    prs: list[dict] = []
    cursor: str | None = None
    while True:
        data = graphql(OPEN_PRS_QUERY, owner=owner, name=name, cursor=cursor)
        page = data["repository"]["pullRequests"]
        prs.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return prs
        cursor = page["pageInfo"]["endCursor"]


def reviewers_path(number: int) -> str:
    return f"repos/{REPO}/pulls/{number}/requested_reviewers"


def fetch_requested(number: int) -> Reviewers:
    data = rest_json("GET", reviewers_path(number))
    if not isinstance(data, dict):
        raise GhError(f"unexpected requested_reviewers response: {data!r}")
    return Reviewers(
        users=sorted(u["login"] for u in data.get("users", [])),
        teams=sorted(t["slug"] for t in data.get("teams", [])),
    )


def fetch_gate_comment(number: int) -> GateComment | None:
    out = gh(
        "api",
        "--paginate",
        f"repos/{REPO}/issues/{number}/comments",
        "--jq",
        ".[] | {id, body, login: .user.login}",
    )
    found: GateComment | None = None
    for line in out.splitlines():
        if not line.strip():
            continue
        comment = json.loads(line)
        if comment["login"] != GATE_LOGIN:
            continue
        parsed = parse_marker(comment["body"] or "")
        if parsed:
            state, stored = parsed
            found = GateComment(comment["id"], comment["body"], state, stored)
    return found


def write_comment(number: int, gate: GateComment | None, body: str) -> None:
    if gate is None:
        rest_json(
            "POST", f"repos/{REPO}/issues/{number}/comments", {"body": body}
        )
    elif gate.body != body:
        rest_json(
            "PATCH",
            f"repos/{REPO}/issues/comments/{gate.comment_id}",
            {"body": body},
        )


def reviewers_payload(reviewers: Reviewers) -> dict:
    return {"reviewers": reviewers.users, "team_reviewers": reviewers.teams}


def request_reviewers(number: int, reviewers: Reviewers) -> Reviewers:
    """Re-request everyone; return those that could not be re-requested.

    One call first. If it fails (typically one login lost access), retry one
    at a time so a single bad entry does not strand everyone else.
    """
    path = reviewers_path(number)
    try:
        rest_json("POST", path, reviewers_payload(reviewers))
        return Reviewers()
    except GhError:
        pass
    failed = Reviewers()
    singles = [(Reviewers(users=[u]), failed.users, u) for u in reviewers.users]
    singles += [
        (Reviewers(teams=[t]), failed.teams, t) for t in reviewers.teams
    ]
    for single, failures, name in singles:
        try:
            rest_json("POST", path, reviewers_payload(single))
        except GhError as exc:
            print(f"  ! could not re-request {name}: {exc}", file=sys.stderr)
            failures.append(name)
    return failed


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


def process(pr: dict, cfg: Config, dry_run: bool) -> str:
    number = pr["number"]
    if pr.get("state") != "OPEN" or pr.get("isDraft"):
        return f"#{number}: skipped (draft or not open)"

    labels = {n["name"] for n in (pr.get("labels") or {}).get("nodes") or []}
    has_requests = (pr.get("reviewRequests") or {}).get("totalCount", 0) > 0
    # Only fetch comments when the PR could possibly be ours to act on.
    if not has_requests and cfg.label not in labels:
        return f"#{number}: none (no review requested)"

    ready = readiness(pr, cfg)
    gate = fetch_gate_comment(number)
    decision = decide(ready, has_requests, gate)
    summary = (
        f"#{number}: {decision.action} ({decision.reason}; "
        f"{len(ready.failing)} failing, pending={ready.pending})"
    )
    if dry_run or decision.action == "none":
        return summary

    if decision.action == "bounce":
        requested = fetch_requested(number)
        stored = (
            gate.stored if gate and gate.state == "blocked" else Reviewers()
        ).merged(requested)
        # Label first, and record who is removed before removing them. The
        # sweep only revisits PRs with a review request or the label, so
        # once the reviewers are gone the label is the only way back. If a
        # later step fails, the next sweep finds the PR again and retries.
        if cfg.label not in labels:
            rest_json(
                "POST",
                f"repos/{REPO}/issues/{number}/labels",
                {"labels": [cfg.label]},
            )
        write_comment(number, gate, blocked_body(ready, stored, cfg.label))
        if requested:
            rest_json(
                "DELETE", reviewers_path(number), reviewers_payload(requested)
            )
    elif decision.action == "update" and gate is not None:
        write_comment(number, gate, blocked_body(ready, gate.stored, cfg.label))
    elif decision.action == "release" and gate is not None:
        failed = (
            request_reviewers(number, gate.stored)
            if gate.stored
            else Reviewers()
        )
        requested = Reviewers(
            users=[u for u in gate.stored.users if u not in failed.users],
            teams=[t for t in gate.stored.teams if t not in failed.teams],
        )
        write_comment(number, gate, released_body(requested, failed))
        if cfg.label in labels:
            try:
                rest_json(
                    "DELETE",
                    f"repos/{REPO}/issues/{number}/labels/{quote(cfg.label, safe='')}",
                )
            except GhError as exc:
                print(f"  ! could not remove label: {exc}", file=sys.stderr)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=int, help="evaluate one pull request")
    target.add_argument("--sweep", action="store_true", help="every open PR")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    global REPO
    if not re.fullmatch(
        r"[\w.-]+/[\w.-]+", os.environ.get("GITHUB_REPOSITORY") or ""
    ):
        parser.error("set GITHUB_REPOSITORY to the owner/name to act on")
    REPO = os.environ["GITHUB_REPOSITORY"]

    cfg = load_config()
    prs = [fetch_pr(args.pr)] if args.pr else fetch_open_prs()

    errors = 0
    lines: list[str] = []
    for pr in prs:
        try:
            line = process(pr, cfg, args.dry_run)
        except GhError as exc:
            errors += 1
            line = f"#{pr['number']}: ERROR {exc}"
        print(line)
        lines.append(line)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            mode = " (dry run)" if args.dry_run else ""
            f.write(f"## Review gate{mode}\n\n")
            f.writelines(
                f"- {line}\n" for line in lines if "none (no review" not in line
            )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
