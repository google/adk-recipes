#!/usr/bin/env python3
"""
Delete stale branches from the GitHub remote.

Classifies every branch in the repository against three independent clocks
(configured under `stale_policy.branches` in .github/policy.yml) and deletes
the ones that have run out. Invoked by
.github/workflows/stale-branch-sweep.yml.

Why ancestry is not enough to detect a merge
--------------------------------------------
This repository is squash-merge only — `allow_squash_merge` is the sole
enabled merge method. A squash merge replays the branch as ONE NEW COMMIT on
the default branch, so the branch head never becomes an ancestor of `main`.

`git branch --merged main` therefore reports almost nothing here: at the time
this was written it found 1 merged branch out of 28, while 6 more had a
merged pull request. Trusting ancestry alone would have misfiled all six as
unmerged orphans — provably merged work, judged under the one clock that can
destroy unrecoverable work.

So `classify` consults the PULL REQUEST state first and only falls back to
ancestry, which now only catches the rare branch merged with no PR at all.

Safety
------
Deleting a branch cannot be undone from the UI, so:

  - Branches that are the head or base of an OPEN pull request, the default
    branch, anything carrying a GitHub branch-protection rule, and anything
    matching `stale_policy.branches.protected` are never touched.
  - The run refuses to act if the open-PR query comes back empty, since an
    API hiccup returning `[]` would unprotect every branch at once. Override
    with --allow-no-open-prs when the repository genuinely has none.
  - A batch larger than --max-delete is refused outright.
  - Every deletion logs the branch's SHA. A deleted branch is restorable via
    `POST /repos/{owner}/{repo}/git/refs` given its name and SHA, so the run
    log and job summary together are a complete undo record.

Usage
-----
  python .github/scripts/sweep_stale_branches.py             # apply
  python .github/scripts/sweep_stale_branches.py --dry-run   # report only
  python .github/scripts/sweep_stale_branches.py --max-delete 40

Requires: `gh` on PATH, GITHUB_TOKEN in the environment, PyYAML.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import yaml

POLICY_PATH = Path(__file__).resolve().parents[1] / "policy.yml"

# The GitHub repo to operate on. Set by Actions automatically; fall back to
# the known upstream when running locally.
REPO = os.environ.get("GITHUB_REPOSITORY", "google/adk-recipes")

# One REST call per branch would work at this repo's size, but GraphQL gets
# name + SHA + commit date for every branch in one paginated query.
#
# `first: 100` is the GitHub maximum for a connection. Pagination is driven
# by `gh api graphql --paginate`, which substitutes a variable that MUST be
# spelled `$endCursor` — any other name silently returns only the first page,
# and this script would then treat every branch beyond 100 as nonexistent.
REFS_QUERY = """
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    refs(refPrefix: "refs/heads/", first: 100, after: $endCursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        target { ... on Commit { oid committedDate } }
      }
    }
  }
}
"""


class GhError(RuntimeError):
    """A `gh` invocation failed.

    Never silently swallowed. Every protection this script applies is derived
    from an API response, so a call that fails and is treated as "no data"
    does not mean "nothing to protect" — it means the protections are gone.
    """


@dataclass(frozen=True)
class Branch:
    name: str
    sha: str
    last_commit: datetime


@dataclass(frozen=True)
class PullRequest:
    number: int
    head_ref: str
    base_ref: str
    state: str  # OPEN | CLOSED | MERGED
    merged_at: datetime | None
    closed_at: datetime | None
    cross_repository: bool


@dataclass(frozen=True)
class Verdict:
    """What the sweep decided about one branch, and why."""

    branch: Branch
    delete: bool
    category: str
    age_days: int | None
    reason: str


def gh(*args: str) -> str:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise GhError(
            f"gh {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def parse_ts(value: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp into an aware datetime."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def age_days(moment: datetime, now: datetime) -> int:
    return (now - moment).days


# ---------------------------------------------------------------------------
# Pure classification — no network. Everything below is unit-tested.
# ---------------------------------------------------------------------------


def open_pr_protected_refs(prs: list[PullRequest]) -> set[str]:
    """Branch names an open pull request depends on.

    Two rules, and the asymmetry between them is deliberate:

      base_ref  — protected for EVERY open PR, including those from forks. A
                  base ref always names a branch in THIS repository, so a
                  fork PR targeting `feature/x` is a real reason to keep
                  `feature/x` (stacked pull requests).

      head_ref  — protected only for same-repository PRs. A fork's head ref
                  lives in the fork, and its name is chosen by someone
                  outside this repo. Several open PRs here have head refs
                  literally named `main` or `dev`; counting those would let
                  an outsider's branch name shadow ours.

    Failing this the other way is the safe direction anyway: over-protecting
    only leaves a dead branch alive for another week.
    """
    protected: set[str] = set()
    for pr in prs:
        if pr.state != "OPEN":
            continue
        protected.add(pr.base_ref)
        if not pr.cross_repository:
            protected.add(pr.head_ref)
    return protected


def matches_any(name: str, patterns: list[str]) -> bool:
    """True if `name` equals or glob-matches any entry in `patterns`."""
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def prs_for_branch(branch: str, prs: list[PullRequest]) -> list[PullRequest]:
    """Same-repository pull requests whose head is this branch.

    Cross-repository PRs are excluded for the same reason as in
    `open_pr_protected_refs`: their head ref names a branch in a fork, not
    the one being classified here.
    """
    return [
        pr for pr in prs if pr.head_ref == branch and not pr.cross_repository
    ]


def prs_targeting_branch(
    branch: str, prs: list[PullRequest]
) -> list[PullRequest]:
    """Pull requests whose BASE is this branch — the stacked-PR case.

    Cross-repository PRs are included here, unlike `prs_for_branch`. A base
    ref always names a branch in THIS repository even when the head lives in
    a fork, which is the same asymmetry `open_pr_protected_refs` relies on.
    """
    return [pr for pr in prs if pr.base_ref == branch]


def classify(
    branch: Branch,
    prs: list[PullRequest],
    cfg: dict,
    now: datetime,
    is_merged_ancestor: Callable[[Branch], bool],
) -> Verdict:
    """Decide the fate of one branch. Assumes protections already applied.

    Resolution order matters, and step 1 must precede step 2:

      1. A MERGED pull request      -> merged clock, from `mergedAt`.
         Catches squash merges, which is nearly all of them here.
      2. Ancestor of the default branch -> merged clock, from last commit.
         Only reachable for a branch merged with no pull request at all.
      3. A pull request CLOSED without merging -> closed-PR clock, from the
         most recent `closedAt`. Measured from the close rather than the last
         commit because a PR closed last week may still be revived, however
         old its commits are.
      4. Otherwise -> orphan clock, from the last commit. The only branch of
         this function that can destroy work which exists nowhere else, hence
         the longest threshold.
    """
    mine = prs_for_branch(branch.name, prs)

    merged = [pr for pr in mine if pr.state == "MERGED" and pr.merged_at]
    if merged:
        newest = max(merged, key=lambda pr: pr.merged_at)
        return _verdict(
            branch,
            "merged",
            newest.merged_at,
            cfg["merged_after_days"],
            now,
            f"PR #{newest.number} merged",
        )

    if is_merged_ancestor(branch):
        return _verdict(
            branch,
            "merged",
            branch.last_commit,
            cfg["merged_after_days"],
            now,
            "already an ancestor of the default branch",
        )

    # A recently-closed PR keeps the branch on the closed-PR clock — whether
    # this branch was that PR's HEAD or its BASE.
    #
    # Base refs matter because `open_pr_protected_refs` only protects them
    # while the PR is OPEN, and stale-sweep.yml closes stale PRs an hour
    # before this sweep runs. In that window a branch that was only ever a
    # stack's base has no open PR and no head-PR credit, so it fell to the
    # orphan clock — measured from a last commit that is old by definition,
    # so it could be deleted immediately. That contradicts the close notice's
    # "reopen the pull request — your commits are unaffected".
    closed = [
        pr
        for pr in mine + prs_targeting_branch(branch.name, prs)
        if pr.state == "CLOSED" and not pr.merged_at and pr.closed_at
    ]
    if closed:
        newest = max(closed, key=lambda pr: pr.closed_at)
        role = "based on" if newest.base_ref == branch.name else "from"
        return _verdict(
            branch,
            "closed-pr",
            newest.closed_at,
            cfg["closed_pr_after_days"],
            now,
            f"PR #{newest.number} ({role} this branch) closed without merging",
        )

    return _verdict(
        branch,
        "orphan",
        branch.last_commit,
        cfg["orphan_after_days"],
        now,
        "unmerged, no pull request",
    )


def _verdict(
    branch: Branch,
    category: str,
    since: datetime,
    threshold: int,
    now: datetime,
    why: str,
) -> Verdict:
    age = age_days(since, now)
    ripe = age >= threshold
    detail = f"{why}; {age}d of {threshold}d"
    return Verdict(
        branch=branch,
        delete=ripe,
        category=category,
        age_days=age,
        reason=detail,
    )


def evaluate(
    branches: list[Branch],
    open_prs: list[PullRequest],
    cfg: dict,
    default_branch: str,
    api_protected: set[str],
    now: datetime,
    lookup_prs: Callable[[str], list[PullRequest]],
    is_merged_ancestor: Callable[[Branch], bool],
) -> list[Verdict]:
    """Apply protections, then classify whatever survives them.

    `open_prs` is the complete set of OPEN pull requests, used only to decide
    what is protected. Per-branch history comes from `lookup_prs`, one query
    per branch, rather than from a single "give me every pull request" call.

    That split is not an optimisation, it is a correctness fix. A repository
    with thousands of closed Dependabot PRs silently truncates any bulk
    `--state all` listing, and the pull requests that fall off the end are
    the OLDEST ones — exactly the ones attached to the oldest branches, which
    are exactly the branches this script is deciding whether to delete. In
    testing, a bulk query capped at 1000 lost the pull requests for five
    branches that had OPEN PRs and reported them as unmerged orphans.
    """
    keep_refs = open_pr_protected_refs(open_prs)
    patterns = list(cfg.get("protected") or [])
    verdicts: list[Verdict] = []

    for branch in sorted(branches, key=lambda b: b.name):
        if branch.name == default_branch:
            verdicts.append(_kept(branch, "protected", "default branch"))
        elif branch.name in api_protected:
            verdicts.append(
                _kept(branch, "protected", "branch-protection rule")
            )
        elif matches_any(branch.name, patterns):
            verdicts.append(_kept(branch, "protected", "listed in policy.yml"))
        elif branch.name in keep_refs:
            verdicts.append(
                _kept(branch, "open-pr", "head or base of an open PR")
            )
        else:
            verdicts.append(
                classify(
                    branch,
                    lookup_prs(branch.name),
                    cfg,
                    now,
                    is_merged_ancestor,
                )
            )
    return verdicts


def _kept(branch: Branch, category: str, reason: str) -> Verdict:
    return Verdict(
        branch=branch,
        delete=False,
        category=category,
        age_days=None,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------


def load_config(path: Path = POLICY_PATH) -> dict:
    with open(path, "rb") as handle:
        policy = yaml.safe_load(handle)
    try:
        return policy["stale_policy"]["branches"]
    except (KeyError, TypeError) as exc:
        raise SystemExit(
            f"error: {path} has no stale_policy.branches section"
        ) from exc


def fetch_default_branch() -> str:
    return gh("api", f"repos/{REPO}", "--jq", ".default_branch")


def fetch_api_protected() -> set[str]:
    """Branch names carrying a GitHub branch-protection rule."""
    raw = gh(
        "api",
        f"repos/{REPO}/branches?protected=true",
        "--paginate",
        "--jq",
        ".[].name",
    )
    return {line for line in raw.splitlines() if line}


def fetch_branches() -> list[Branch]:
    owner, name = REPO.split("/", 1)
    raw = gh(
        "api",
        "graphql",
        "--paginate",
        "-f",
        f"query={REFS_QUERY}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "--jq",
        ".data.repository.refs.nodes[]"
        " | [.name, .target.oid, .target.committedDate]"
        " | @tsv",
    )
    branches: list[Branch] = []
    for line in raw.splitlines():
        if not line:
            continue
        branch_name, sha, committed = line.split("\t")
        moment = parse_ts(committed)
        if moment is None:
            # A ref whose target is not a Commit (an annotated tag pushed to
            # refs/heads, say). Nothing sane to measure, so leave it alone.
            continue
        branches.append(Branch(branch_name, sha, moment))
    return branches


PR_FIELDS = (
    "number,headRefName,baseRefName,state,mergedAt,closedAt,isCrossRepository"
)

# Ceiling for the open-PR listing. Well above this repository's ~70 open PRs;
# hitting it is treated as an error rather than quietly truncated, because a
# missing open PR means an unprotected branch.
OPEN_PR_LIMIT = 500

# Ceiling for one branch's pull request history. A branch with more than this
# many pull requests does not exist in practice; the guard is here so that if
# one ever does, it fails loudly instead of losing the merge that matters.
BRANCH_PR_LIMIT = 100


def _parse_prs(raw: str) -> list[PullRequest]:
    return [
        PullRequest(
            number=item["number"],
            head_ref=item["headRefName"],
            base_ref=item["baseRefName"],
            state=item["state"],
            merged_at=parse_ts(item.get("mergedAt")),
            closed_at=parse_ts(item.get("closedAt")),
            cross_repository=item["isCrossRepository"],
        )
        for item in json.loads(raw)
    ]


def fetch_open_pull_requests() -> list[PullRequest]:
    """Every OPEN pull request. Used only to work out what is protected."""
    prs = _parse_prs(
        gh(
            "pr",
            "list",
            "--repo",
            REPO,
            "--state",
            "open",
            "--limit",
            str(OPEN_PR_LIMIT),
            "--json",
            PR_FIELDS,
        )
    )
    if len(prs) >= OPEN_PR_LIMIT:
        raise GhError(
            f"open pull request listing hit the {OPEN_PR_LIMIT} limit, so it "
            "is truncated. Branches belonging to the pull requests that fell "
            "off the end would look unprotected. Raise OPEN_PR_LIMIT."
        )
    return prs


def make_pr_lookup() -> Callable[[str], list[PullRequest]]:
    """Per-branch pull request history, fetched on demand and cached.

    Deliberately NOT a single bulk `--state all` listing. See `evaluate` for
    the failure that produced: bulk listings truncate oldest-first, which
    silently strips the history off precisely the branches being judged.
    """
    cache: dict[str, list[PullRequest]] = {}

    def _query(selector: str, branch_name: str) -> list[PullRequest]:
        prs = _parse_prs(
            gh(
                "pr",
                "list",
                "--repo",
                REPO,
                selector,
                branch_name,
                "--state",
                "all",
                "--limit",
                str(BRANCH_PR_LIMIT),
                "--json",
                PR_FIELDS,
            )
        )
        if len(prs) >= BRANCH_PR_LIMIT:
            raise GhError(
                f"branch {branch_name} returned {len(prs)} pull requests "
                f"for {selector}, hitting the {BRANCH_PR_LIMIT} limit. The "
                "history is truncated and the classification cannot be "
                "trusted. Raise BRANCH_PR_LIMIT."
            )
        return prs

    def lookup(branch_name: str) -> list[PullRequest]:
        if branch_name not in cache:
            # BOTH directions, and the --base half is load-bearing.
            #
            # `classify` puts a branch on the closed-PR clock when a recent
            # pull request referenced it — as that PR's head OR as its base,
            # the stacked-PR case. With only `--head` here, the list handed
            # to `classify` can never contain a PR whose base is this branch,
            # so the base arm of that rule was unreachable in production and
            # a stack's base fell through to the orphan clock and could be
            # deleted. Unit tests did not catch it: they build the PR list by
            # hand and can express an input this lookup could not produce.
            #
            # Deduplicated by number because a PR can appear in both halves
            # (a branch merging into itself is not possible, but a self-
            # referential query result should not be counted twice either).
            by_number: dict[int, PullRequest] = {}
            for selector in ("--head", "--base"):
                for pr in _query(selector, branch_name):
                    by_number[pr.number] = pr
            cache[branch_name] = list(by_number.values())
        return cache[branch_name]

    return lookup


def make_ancestor_check(default_branch: str) -> Callable[[Branch], bool]:
    """Lazy `is this branch already in the default branch?` predicate.

    One compare call per branch that reaches it, and `classify` only reaches
    it for branches with no merged pull request — so on a squash-merge repo
    this fires for a handful of branches, not all of them.
    """
    cache: dict[str, bool] = {}

    def check(branch: Branch) -> bool:
        if branch.name not in cache:
            ahead = gh(
                "api",
                f"repos/{REPO}/compare/{default_branch}...{branch.sha}",
                "--jq",
                ".ahead_by",
            )
            cache[branch.name] = ahead == "0"
        return cache[branch.name]

    return check


def delete_branch(branch: Branch) -> tuple[bool, str]:
    # The name is percent-encoded, and this is the one call in the script
    # where that matters. Git permits `#` in a ref name, `#` opens a URL
    # fragment, and `gh api` does not encode the path it is handed — so a
    # branch called `fix#1` would send DELETE .../heads/fix and destroy a
    # different, live branch. `safe="/"` keeps the path separators in a
    # namespaced name like `feature/x` intact.
    ref = quote(branch.name, safe="/")
    result = subprocess.run(
        [
            "gh",
            "api",
            "-X",
            "DELETE",
            f"repos/{REPO}/git/refs/heads/{ref}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def write_summary(
    deleted: list[Branch],
    doomed: list[Verdict],
    dry_run: bool,
    refused: bool = False,
) -> None:
    """Record what happened where a human will find it.

    On a real run this table IS the undo record: a deleted branch is
    restorable from nothing but its name and SHA, both of which are here. On
    a dry run it is the preview of the same table.

    `refused` covers the circuit breaker: nothing was deleted, but the run
    was not a dry run either, and the candidate list is precisely what has to
    be reviewed before anyone raises the limit.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return

    lines = ["## Stale branch sweep", ""]
    if not doomed:
        lines.append("No branches were eligible for deletion.")
    elif refused:
        lines += [
            f"**Refused** — {len(doomed)} branch(es) are eligible, above the "
            "`--max-delete` limit, so NOTHING was deleted. Read this list "
            "before re-running with a raised limit; an unexpectedly large "
            "batch means a classification bug, not a tidy repository.",
            "",
            "| branch | sha | why |",
            "|---|---|---|",
        ]
        lines += [_summary_row(v) for v in doomed]
    elif dry_run:
        lines += [
            f"**Dry run** — nothing was deleted. "
            f"{len(doomed)} branch(es) would be:",
            "",
            "| branch | sha | why |",
            "|---|---|---|",
        ]
        lines += [_summary_row(v) for v in doomed]
    else:
        deleted_names = {b.name for b in deleted}
        failed = [v for v in doomed if v.branch.name not in deleted_names]
        lines += [
            f"{len(deleted)} of {len(doomed)} eligible branch(es) deleted.",
            "",
            "Restore one with:",
            "",
            "```",
            f"gh api -X POST repos/{REPO}/git/refs \\",
            '  -f ref="refs/heads/<name>" -f sha="<sha>"',
            "```",
            "",
            "| branch | sha | why |",
            "|---|---|---|",
        ]
        lines += [
            _summary_row(v) for v in doomed if v.branch.name in deleted_names
        ]
        if failed:
            lines += [
                "",
                f"{len(failed)} deletion(s) FAILED and the branch survives:",
                "",
                "| branch | sha | why |",
                "|---|---|---|",
            ]
            lines += [_summary_row(v) for v in failed]

    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _summary_row(verdict: Verdict) -> str:
    return (
        f"| `{verdict.branch.name}` | `{verdict.branch.sha}` "
        f"| {verdict.reason} |"
    )


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser(
        description="Delete stale branches (see module docstring).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be deleted, and delete nothing.",
    )
    parser.add_argument(
        "--max-delete",
        type=int,
        default=cfg["max_delete_per_run"],
        help=(
            "Refuse to delete anything if more branches than this are "
            f"eligible (default: {cfg['max_delete_per_run']} from "
            "policy.yml). Raise it deliberately after a --dry-run."
        ),
    )
    parser.add_argument(
        "--allow-no-open-prs",
        action="store_true",
        help=(
            "Proceed even when the repository has no open pull requests. "
            "Without this the run aborts, because an API failure returning "
            "an empty list is indistinguishable from a genuinely quiet "
            "repository and would unprotect every branch at once."
        ),
    )
    args = parser.parse_args(argv)

    now = datetime.now(UTC)
    default_branch = fetch_default_branch()
    branches = fetch_branches()
    open_prs = fetch_open_pull_requests()
    api_protected = fetch_api_protected()

    print(f"Repository: {REPO} (default branch: {default_branch})")
    print(f"Branches: {len(branches)}")

    if not open_prs and not args.allow_no_open_prs:
        print(
            "::error::No open pull requests were returned. Refusing to act — "
            "open PRs are what protect their head and base branches, so an "
            "API failure here would make every branch look unprotected. "
            "Re-run with --allow-no-open-prs if the repository genuinely has "
            "none.",
            file=sys.stderr,
        )
        return 1
    print(f"Open pull requests protecting branches: {len(open_prs)}")

    verdicts = evaluate(
        branches,
        open_prs,
        cfg,
        default_branch,
        api_protected,
        now,
        make_pr_lookup(),
        make_ancestor_check(default_branch),
    )

    print("\nClassification:")
    for verdict in verdicts:
        flag = "DELETE" if verdict.delete else "keep  "
        print(
            f"  {flag}  {verdict.branch.name}  "
            f"[{verdict.category}] {verdict.reason}"
        )

    doomed = [v for v in verdicts if v.delete]
    if not doomed:
        print("\nNothing to delete.")
        write_summary([], [], args.dry_run)
        return 0

    if args.dry_run:
        print(f"\n[dry-run] Would delete {len(doomed)} branch(es):")
        for verdict in doomed:
            print(f"  {verdict.branch.name}  {verdict.branch.sha}")
        if len(doomed) > args.max_delete:
            print(
                f"\nNote: {len(doomed)} exceeds the --max-delete limit of "
                f"{args.max_delete}, so a real run would refuse."
            )
        write_summary([], doomed, True)
        return 0

    # Circuit breaker. The protections above are derived from API responses;
    # a plausible-but-wrong response, or a change in how this repo merges,
    # would reclassify healthy branches rather than produce no data at all.
    # Any such bug shows up first as an unusually large batch.
    if len(doomed) > args.max_delete:
        # The candidate list is exactly what has to be reviewed before anyone
        # raises the limit; leaving it out of the summary would strand it in
        # the raw log, which is the opposite of how the rest of this script
        # reports itself.
        write_summary([], doomed, False, refused=True)
        print(
            f"::error::{len(doomed)} branches are eligible, above the "
            f"--max-delete limit of {args.max_delete}. Refusing to delete "
            "anything. Re-run with --dry-run to review the list; if it is "
            f"genuinely correct, re-run with --max-delete {len(doomed)}.",
            file=sys.stderr,
        )
        return 1

    print("\nDeleting:")
    deleted: list[Branch] = []
    failures: list[str] = []
    for verdict in doomed:
        branch = verdict.branch
        ok, err = delete_branch(branch)
        if ok:
            # SHA first: this line is the undo record.
            print(f"  deleted {branch.sha}  {branch.name}")
            deleted.append(branch)
        else:
            print(
                f"  FAILED  {branch.name} -- {err[:200]}",
                file=sys.stderr,
            )
            failures.append(branch.name)

    write_summary(deleted, doomed, False)

    if failures:
        print(
            f"\n{len(failures)} deletion(s) failed: {failures}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
