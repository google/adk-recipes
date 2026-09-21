#!/usr/bin/env python3
"""
Close orphaned Dependabot PRs.

Scans the recipe tree for dependency manifests to build the set of live
(ecosystem, directory) pairs, lists every open Dependabot PR, and closes any
whose head-ref maps to a pair that is NOT in that set. This catches PRs
stranded when a recipe is removed or renamed — Dependabot itself does not
close such PRs, so without this cleanup they linger indefinitely.

Where the live set comes from
-----------------------------
The tree, via recipe_manifests.scan() — NOT .github/dependabot.yml.

This script used to recover the set by regex-scanning enumerated `directory:`
keys out of dependabot.yml. Reading that file is now actively worse than it
sounds. It does list every recipe ecosystem, so a glance suggests the data is
right there — but those entries exist only to suppress updates and carry
`directories: ["/", "**/*"]`. A parser would recover a glob and no concrete
recipe pair, judge every open Dependabot PR an orphan, and close them all
with --delete-branch.

The tree is also the more accurate source. Which recipe directories exist is
a property of the tree, not of a config file that never enumerated them —
and the 98 PRs open when the policy changed are precisely the ones that
would be swept up if this ever started reading dependabot.yml again.

Uses `gh pr close <n> --delete-branch` with no explanatory comment: the GitHub
GraphQL `addComment` mutation has an anti-abuse throttle that trips on large
batches (observed in practice at ~80 comments in a burst). The close+delete-
branch itself is the audit signal; the workflow log below enumerates every
closed PR.

Invoked by .github/workflows/dependabot-housekeeping.yml.

Requires: `gh` on PATH, GITHUB_TOKEN in the environment.

Safety
------
Two guards, because the operation is destructive and not easily undone:

  - Refuses to run at all if the manifest scan comes back empty, which would
    make every open Dependabot PR look orphaned.
  - Refuses to close a batch larger than --max-close (default
    DEFAULT_MAX_CLOSE). Routine cleanup strands a handful of PRs; a large
    batch means something reclassified healthy ones, and wants a human.

Usage
-----
  python .github/scripts/close_orphan_dependabot_prs.py             # apply
  python .github/scripts/close_orphan_dependabot_prs.py --dry-run   # list
  python .github/scripts/close_orphan_dependabot_prs.py --max-close 40
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import recipe_manifests

# Dependabot renames a few ecosystems in the branch prefix relative to the
# `package-ecosystem` value used in dependabot.yml. Everything not listed here
# uses the same string in both places. Mirrors dependabot-core's
# `PACKAGE_MANAGER_LOOKUP` in config/file.rb.
BRANCH_PREFIX = {
    "npm": "npm_and_yarn",
    "github-actions": "github_actions",
    # recipe_manifests.py reports `gomod` (its `_is_gomod` detector fires on
    # a recipe's go.mod), matching the `package-ecosystem` key; Dependabot
    # names the branch `go_modules`. Without this entry every Go dependency
    # PR is classified as an orphan and closed with --delete-branch.
    "gomod": "go_modules",
}

# Ecosystems whose package identifiers legitimately contain a slash:
#   go_modules      module paths        github.com/spf13/cobra
#   npm_and_yarn    scoped packages     @types/node -> types/node
#   github_actions  owner/repo          actions/checkout
# See head_ref_matches for why the usual single-segment rule is dropped here.
#
# The github-actions entry in dependabot.yml uses a `groups: patterns: ["*"]`
# block, so its version updates arrive pre-grouped with a single-segment tail
# (`all-actions-<hash>`) and never hit this path.
#
# Security updates are grouped as well. GitHub's "grouped security updates"
# setting batches them per ecosystem across directories, which is why the
# branches this repo saw in August 2026 looked like
# `dependabot/uv/<dir>/uv-<hash>` rather than one branch per package. An
# earlier version of this comment asserted the opposite — that security
# updates open one PR per package. That was true when written and is not
# now, so do not restore it.
#
# The single-segment rule is still dropped here, because grouping is a
# repository setting rather than a guarantee: with it off, or for an
# ecosystem it does not cover, an ungrouped security PR names one package,
# and that is where a slash-bearing identifier shows up.
MULTI_SEGMENT_PACKAGE_ECOSYSTEMS = {
    "go_modules",
    "npm_and_yarn",
    "github_actions",
}

# The GitHub repo to operate on. Set by Actions automatically; fall back to
# the known upstream when running locally.
REPO = os.environ.get("GITHUB_REPOSITORY", "google/adk-recipes")

# Upper bound on how many PRs one run may close. See the circuit breaker in
# main() for the rationale. Sized well above routine cleanup (a recipe removal
# strands a handful) and well below "something is badly wrong".
#
# It was sized against a repo carrying ~115 open Dependabot PRs at any time.
# That number is now falling towards zero: dependabot.yml suppresses every
# recipe ecosystem, so nothing new arrives and the backlog only drains. The
# limit is deliberately NOT retuned downwards to match — a smaller ceiling
# would trip on the one case that still matters, a bulk recipe removal
# stranding many PRs at once, and the cost of it being generous is only that
# a genuinely wrong scan closes more before someone notices. Revisit if the
# steady state stays near zero for a few months.
DEFAULT_MAX_CLOSE = 30


def live_pairs(
    discovered: list[tuple[str, str]] | None = None,
) -> set[tuple[str, str]]:
    """Return the (branch-prefix-ecosystem, directory) pairs that still exist.

    Derived from the recipe tree, translated from the `package-ecosystem`
    spelling used in config into the spelling Dependabot uses in branch names.

    `discovered` lets a caller that has already run recipe_manifests.scan()
    pass the result in rather than walking the tree a second time; it defaults
    to scanning.

    recipe_manifests.STATIC_ENTRIES are folded in unconditionally. They are
    configured rather than discovered, so nothing in the tree would keep their
    PRs from looking orphaned. Reading them from the shared module rather than
    restating them here is what keeps this set aligned with dependabot.yml —
    tests/test_dependabot_config.py asserts the two agree, in both directions.
    An entry present in the config but missing from that list has its PRs
    closed with --delete-branch, at roughly one grouped PR a week, which is
    far too slow for --max-close to notice.
    """
    if discovered is None:
        discovered = recipe_manifests.scan()
    return {
        (BRANCH_PREFIX.get(eco, eco), directory)
        for eco, directory in [*discovered, *recipe_manifests.static_pairs()]
    }


def head_ref_matches(head_ref: str, pair: tuple[str, str]) -> bool:
    """True iff `head_ref` is a Dependabot branch targeting `pair`.

    Branch naming convention:
      - Root directory ("/"):  dependabot/<eco>/<package>
      - Non-root:              dependabot/<eco>/<dir-no-leading-slash>/<package>

    For most ecosystems the tail after the directory prefix must be a single
    path segment (the package identifier), so we require no further slashes.
    That prevents a tracked pair for "/x" from spuriously claiming a branch
    that actually belongs to the untracked subdirectory "/x/y".

    That rule cannot hold for the ecosystems in
    MULTI_SEGMENT_PACKAGE_ECOSYSTEMS, whose package identifiers contain
    slashes of their own — a Go branch looks like
    `dependabot/go_modules/contrib/go/my-recipe/github.com/spf13/cobra-1.8.0`.
    Applying the strict rule there marks every such PR an orphan and closes
    it. For those the slash check is dropped.

    The trade-off is deliberate and asymmetric: a nested module (a tracked
    "/x" with an untracked "/x/y" beneath it) can now have its stranded PR go
    unrecognised and linger. That is the safe direction to fail — a lingering
    PR is noise, whereas the strict rule's failure mode is closing a valid PR
    and deleting its branch.
    """
    eco, directory = pair
    if directory == "/":
        prefix = f"dependabot/{eco}/"
    else:
        prefix = f"dependabot/{eco}/{directory.lstrip('/')}/"
    if not head_ref.startswith(prefix):
        return False
    tail = head_ref[len(prefix) :]
    if not tail:
        return False
    if eco in MULTI_SEGMENT_PACKAGE_ECOSYSTEMS:
        return True
    return "/" not in tail


def is_orphan(head_ref: str, tracked: set[tuple[str, str]]) -> bool:
    return not any(head_ref_matches(head_ref, p) for p in tracked)


def list_open_dependabot_prs() -> list[dict]:
    """Return [{number, headRefName}, ...] for every open Dependabot PR."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            REPO,
            "--author",
            "app/dependabot",
            "--state",
            "open",
            "--limit",
            "500",
            "--json",
            "number,headRefName",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def close_pr(number: int) -> tuple[bool, str]:
    result = subprocess.run(
        [
            "gh",
            "pr",
            "close",
            str(number),
            "--repo",
            REPO,
            "--delete-branch",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Close orphaned Dependabot PRs (see module docstring).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List orphans without closing them.",
    )
    parser.add_argument(
        "--max-close",
        type=int,
        default=DEFAULT_MAX_CLOSE,
        help=(
            "Refuse to close anything if more than this many orphans are "
            f"found (default: {DEFAULT_MAX_CLOSE}). Raise it deliberately "
            "after reviewing a --dry-run."
        ),
    )
    args = parser.parse_args(argv)

    # Fail closed. Every open Dependabot PR is an orphan relative to an empty
    # live set, so a scan that comes back empty would mass-close the repo's
    # PRs and delete their branches. An empty result is far more likely to
    # mean "run from the wrong directory" or "the scan is broken" than "the
    # repo genuinely has no dependency-managed recipes", so refuse to act.
    #
    # Guard on the scan itself rather than on len(live_pairs()): live_pairs()
    # folds in static entries that exist regardless of the tree, so counting
    # it would mean hardcoding how many of those there are today.
    discovered = recipe_manifests.scan()
    if not discovered:
        roots = "/, ".join(recipe_manifests.SCAN_ROOTS)
        print(
            f"::error::Found no dependency manifests under {roots}/. Refusing "
            "to close anything — every open Dependabot PR would look "
            "orphaned. Check that this ran from a full repository checkout.",
            file=sys.stderr,
        )
        return 1

    tracked = live_pairs(discovered)
    print(f"Recipe tree has {len(tracked)} live (ecosystem, directory) pairs.")

    prs = list_open_dependabot_prs()
    print(f"Found {len(prs)} open Dependabot PRs on {REPO}.")

    orphans = [pr for pr in prs if is_orphan(pr["headRefName"], tracked)]
    print(f"Of those, {len(orphans)} target directories that no longer exist.")

    if not orphans:
        return 0

    # A dry run is read-only, so it is never blocked — it only reports. The
    # circuit breaker below deliberately sits AFTER this: capping a dry run
    # would make the escape hatch unreachable, since the advice for an
    # oversized batch is to go and review it.
    if args.dry_run:
        over = len(orphans) > args.max_close
        print("\n[dry-run] Would close:")
        for pr in orphans:
            print(f"  #{pr['number']}  {pr['headRefName']}")
        if over:
            print(
                f"\nNote: {len(orphans)} orphans is above the --max-close "
                f"limit of {args.max_close}, so a real run would refuse. If "
                "this list is right, re-run without --dry-run and with "
                f"--max-close {len(orphans)}."
            )
        return 0

    # Circuit breaker. The fail-closed guard above only catches a scan that
    # returns NOTHING; it cannot catch a scan that returns a plausible-looking
    # but wrong set, or a change to how Dependabot names branches, either of
    # which would silently reclassify healthy PRs as orphans.
    #
    # Any such bug shows up first as an unusually large batch, so cap the
    # batch. Routine cleanup closes a handful; anything approaching this many
    # is a claim that most of the repo's recipes vanished at once, which
    # deserves a human rather than a --delete-branch loop.
    if len(orphans) > args.max_close:
        print(
            f"::error::{len(orphans)} orphans exceeds the --max-close limit "
            f"of {args.max_close}. Refusing to close anything. Re-run with "
            "--dry-run to review the full list without closing; if it is "
            f"genuinely correct, re-run with --max-close {len(orphans)}.",
            file=sys.stderr,
        )
        for pr in orphans:
            print(f"  #{pr['number']}  {pr['headRefName']}", file=sys.stderr)
        return 1

    print("\nClosing orphans (gh pr close --delete-branch, no comment):")
    failures: list[int] = []
    for pr in orphans:
        ok, err = close_pr(pr["number"])
        if ok:
            print(f"  closed #{pr['number']}  {pr['headRefName']}")
        else:
            print(
                f"  FAILED #{pr['number']}  {pr['headRefName']}  -- {err[:200]}",
                file=sys.stderr,
            )
            failures.append(pr["number"])

    if failures:
        print(
            f"\n{len(failures)} orphan close(s) failed: {failures}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
