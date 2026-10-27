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
Recipe Deprecator: automate the deprecation of inactive recipes.

Runs twice monthly on schedule (2nd and 16th) to find recipes marked
`status: inactive` in their manifest.yaml whose latest Git commit or file
modification date is 60+ days ago.

For expired inactive recipes:
  1. Identifies the repo_admin from .github/policy.yml.
  2. Checks for an existing deletion PR:
     - If open: does nothing.
     - If closed/ignored (unmerged): reopens it and merges the current main
       into its branch, so files added to the recipe since the PR was
       opened are deleted too. If it cannot be reopened (for example its
       branch was swept), a fresh PR is opened instead.
     - If no PR exists: creates a branch, deletes the recipe directory, commits,
       pushes, and opens a new PR.
  3. Assigns new or reopened PRs to repo_admin for manual review.

Exits non-zero when any PR action fails, so a scheduled run that achieved
nothing does not show green.

Usage:
  python .github/scripts/recipe_deprecator.py
  python .github/scripts/recipe_deprecator.py --dry-run
  python .github/scripts/recipe_deprecator.py --inactive-days 60
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import recipe_manifests
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = Path(__file__).resolve().parents[1] / "policy.yml"

# Shared with the other recipe-tree walkers so a renamed or added recipe root
# is picked up here too.
SCAN_ROOTS = recipe_manifests.SCAN_ROOTS
SKIP_DIRS = recipe_manifests.SKIP_DIRS | {".ruff_cache", ".agent-tmp"}

DEFAULT_INACTIVE_DAYS = 60
DEFAULT_REPO_ADMIN = "happyhuman"
DEFAULT_PR_LIMIT = 1000
BASE_BRANCH = "main"

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


@dataclass
class RecipeInfo:
    rel_path: str
    dir_path: Path
    manifest_path: Path
    status: str
    mtime: datetime


def load_policy(policy_path: Path = POLICY_PATH) -> dict:
    """Safely load .github/policy.yml."""
    if not policy_path.is_file():
        return {}
    try:
        with open(policy_path, "rb") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"warning: failed to read {policy_path}: {exc}", file=sys.stderr)
        return {}


def get_repo_admin(
    policy: dict | None = None, policy_path: Path = POLICY_PATH
) -> str:
    """Get the repo_admin username from policy.yml with fallback."""
    if policy is None:
        policy = load_policy(policy_path)
    admin = policy.get("repo_admin")
    if isinstance(admin, str) and admin.strip():
        return admin.strip()
    return DEFAULT_REPO_ADMIN


def get_manifest_mtime(
    manifest_path: Path, repo_root: Path = REPO_ROOT
) -> datetime:
    """Get latest Git commit timestamp for manifest.yaml, falling back to mtime."""
    try:
        rel_manifest = manifest_path.relative_to(repo_root).as_posix()
        res = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", rel_manifest],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            ts = int(res.stdout.strip())
            return datetime.fromtimestamp(ts, tz=UTC)
    except Exception as exc:
        logging.debug("git log failed for %s: %s", manifest_path, exc)

    try:
        mtime = manifest_path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=UTC)
    except Exception as exc:
        logging.debug("stat failed for %s: %s", manifest_path, exc)
        return datetime.now(UTC)


def scan_recipes(repo_root: Path = REPO_ROOT) -> list[RecipeInfo]:
    """Scan SCAN_ROOTS for recipe manifest.yaml files."""
    recipes: list[RecipeInfo] = []

    for root_name in SCAN_ROOTS:
        root_path = repo_root / root_name
        if not root_path.is_dir():
            continue

        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            if "manifest.yaml" in filenames:
                manifest_path = Path(dirpath) / "manifest.yaml"
                try:
                    with open(manifest_path, encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                except Exception as exc:
                    logging.warning(
                        "Failed to parse manifest %s: %s", manifest_path, exc
                    )
                    continue

                if not isinstance(data, dict):
                    continue

                status = str(data.get("status", "")).strip().lower()
                mtime = get_manifest_mtime(manifest_path, repo_root)
                rel_path = Path(dirpath).relative_to(repo_root).as_posix()

                recipes.append(
                    RecipeInfo(
                        rel_path=rel_path,
                        dir_path=Path(dirpath),
                        manifest_path=manifest_path,
                        status=status,
                        mtime=mtime,
                    )
                )

    return sorted(recipes, key=lambda r: r.rel_path)


def classify_recipe(
    status: str,
    mtime: datetime,
    now: datetime,
    threshold_days: int = DEFAULT_INACTIVE_DAYS,
) -> str:
    """Classify recipe status into active, inactive_recent, inactive_expired, or unknown."""
    status = status.lower()
    if status == "active":
        return "active"
    if status == "inactive":
        elapsed_seconds = (now - mtime).total_seconds()
        elapsed_days = elapsed_seconds / 86400.0
        if elapsed_days < threshold_days:
            return "inactive_recent"
        return "inactive_expired"
    return "unknown"


def get_deletion_branch_name(recipe_rel_path: str) -> str:
    """Branch name for recipe deprecation."""
    return f"deprecate/{recipe_rel_path}"


def get_deletion_pr_title(recipe_rel_path: str) -> str:
    """PR title for recipe deprecation."""
    return f"Deprecate recipe: {recipe_rel_path}"


def get_deletion_pr_body(
    recipe_rel_path: str,
    repo_admin: str,
    days_inactive: int,
    threshold_days: int = DEFAULT_INACTIVE_DAYS,
) -> str:
    """PR description for recipe deprecation."""
    return (
        f"## Deprecation of inactive recipe\n\n"
        f"This PR deletes the inactive recipe at `{recipe_rel_path}`. Its "
        f"manifest.yaml says `status: inactive` and has not changed for "
        f"{days_inactive} days (threshold: {threshold_days}+ days).\n\n"
        f"Assigned to @{repo_admin} for manual review.\n"
    )


def fetch_prs(repo: str | None = None) -> list[dict]:
    """Fetch existing PRs from GitHub using gh CLI.

    Open PRs are listed on their own so a long-open deletion PR is never
    pushed out of the window by newer closed ones; missing it would mean
    force-pushing over its branch. Only the most recent closed PRs are
    listed, and a closed deletion PR outside that window just gets a fresh
    PR instead of a reopen.

    Raises RuntimeError on failure. Carrying on with an empty list would make
    every expired recipe look PR-less and open duplicates of open PRs.
    """
    return _list_prs("open", repo) + _list_prs("closed", repo)


def _list_prs(state: str, repo: str | None) -> list[dict]:
    cmd = [
        "gh",
        "pr",
        "list",
        "--state",
        state,
        "--limit",
        str(DEFAULT_PR_LIMIT),
        "--json",
        "number,title,headRefName,state,url,assignees,closedAt,mergedAt",
    ]
    if repo:
        cmd.extend(["--repo", repo])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError(f"failed to run gh pr list: {exc}") from exc
    if res.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {res.stderr.strip()}")
    try:
        prs = json.loads(res.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh pr list returned invalid JSON: {exc}") from exc
    if not isinstance(prs, list):
        raise RuntimeError("gh pr list did not return a JSON list")
    return prs


def find_existing_deletion_pr(
    recipe_rel_path: str, prs: list[dict]
) -> dict | None:
    """Find existing deletion PR for this recipe.

    Any matching open PR counts, including a hand-made one found by title or
    alternative branch name, since it means deletion is already under review.
    A closed PR is only returned (for reopening) when it is on this script's
    own branch, so a person's unrelated closed PR is never reopened.
    """
    matching: list[dict] = []
    expected_branch = get_deletion_branch_name(recipe_rel_path)
    alt_branch_1 = f"deprecate/{recipe_rel_path.replace('/', '-')}"
    alt_branch_2 = f"delete/{recipe_rel_path}"

    for pr in prs:
        head = pr.get("headRefName", "")
        title = pr.get("title", "")
        is_match = False
        if head in (expected_branch, alt_branch_1, alt_branch_2):
            is_match = True
        elif any(
            kw in title.lower() for kw in ("deprecate", "delete")
        ) and re.search(
            rf"(?<![a-zA-Z0-9_\-/]){re.escape(recipe_rel_path)}(?![a-zA-Z0-9_\-/])",
            title,
        ):
            is_match = True

        if is_match:
            matching.append(pr)

    if not matching:
        return None

    # Return open PR if one exists
    for pr in matching:
        if str(pr.get("state", "")).upper() == "OPEN":
            return pr

    # Otherwise return latest closed unmerged PR
    unmerged_closed = [
        pr
        for pr in matching
        if str(pr.get("state", "")).upper() == "CLOSED"
        and not pr.get("mergedAt")
        and pr.get("headRefName") == expected_branch
    ]
    if unmerged_closed:
        return max(unmerged_closed, key=lambda p: int(p.get("number", 0)))

    return None


def assign_pr(
    pr_number: int,
    repo_admin: str,
    repo: str | None = None,
    dry_run: bool = False,
) -> bool:
    """Assign PR to repo_admin using GitHub REST API."""
    if dry_run:
        print(f"[DRY RUN] Would assign PR #{pr_number} to @{repo_admin}")
        return True

    endpoint = (
        f"/repos/{repo}/issues/{pr_number}/assignees"
        if repo
        else f"/repos/:owner/:repo/issues/{pr_number}/assignees"
    )
    cmd = [
        "gh",
        "api",
        "-X",
        "POST",
        endpoint,
        "-f",
        f"assignees[]={repo_admin}",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            print(f"Assigned PR #{pr_number} to @{repo_admin}")
            return True
        print(
            f"warning: failed to assign PR #{pr_number}: {res.stderr.strip()}",
            file=sys.stderr,
        )
    except Exception as exc:
        print(
            f"warning: failed to assign PR #{pr_number}: {exc}", file=sys.stderr
        )
    return False


def reopen_pr(
    pr_number: int,
    repo_admin: str,
    repo: str | None = None,
    dry_run: bool = False,
) -> bool:
    """Reopen a closed PR and assign to repo_admin."""
    if dry_run:
        print(
            f"[DRY RUN] Would reopen PR #{pr_number} and assign to @{repo_admin}"
        )
        return True

    cmd = ["gh", "pr", "reopen", str(pr_number)]
    if repo:
        cmd.extend(["--repo", repo])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            print(f"Reopened PR #{pr_number}")
            assign_pr(pr_number, repo_admin, repo=repo, dry_run=dry_run)
            return True
        print(
            f"warning: failed to reopen PR #{pr_number}: {res.stderr.strip()}",
            file=sys.stderr,
        )
    except Exception as exc:
        print(
            f"warning: failed to reopen PR #{pr_number}: {exc}", file=sys.stderr
        )
    return False


def _git_env() -> dict[str, str]:
    """Environment with a commit identity, for runs outside the workflow."""
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", BOT_NAME)
    env.setdefault("GIT_AUTHOR_EMAIL", BOT_EMAIL)
    env.setdefault("GIT_COMMITTER_NAME", BOT_NAME)
    env.setdefault("GIT_COMMITTER_EMAIL", BOT_EMAIL)
    return env


def _run(
    cmd: list[str],
    repo_root: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a command in repo_root; raise RuntimeError with stderr on failure."""
    res = subprocess.run(
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {res.stderr.strip()}")
    return res


def _current_ref(repo_root: Path, env: dict[str, str]) -> str:
    """The checked-out branch name, or the commit SHA when detached."""
    res = _run(
        ["git", "symbolic-ref", "-q", "--short", "HEAD"],
        repo_root,
        env,
        check=False,
    )
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip()
    return _run(["git", "rev-parse", "HEAD"], repo_root, env).stdout.strip()


def _prepare_checkout(repo_root: Path, env: dict[str, str]) -> str | None:
    """Return the ref to restore afterwards, or None if the tree is dirty.

    The branch switching below ends with a hard reset, which would destroy
    uncommitted work in a local checkout, so refuse to start on one.
    """
    try:
        dirty = _run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            repo_root,
            env,
        ).stdout.strip()
        if dirty:
            print(
                f"error: {repo_root} has uncommitted changes; commit or stash "
                "them, or use --dry-run",
                file=sys.stderr,
            )
            return None
        return _current_ref(repo_root, env)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _restore_checkout(repo_root: Path, ref: str, env: dict[str, str]) -> None:
    """Return to ref with a clean tree.

    A step that fails part-way can leave staged deletions or a half-done
    merge behind. Carried into the next recipe's branch, they would turn its
    PR into one that deletes two recipes.
    """
    _run(["git", "reset", "-q", "--hard"], repo_root, env, check=False)
    _run(["git", "checkout", "-q", "-f", ref], repo_root, env, check=False)


def create_deletion_pr(
    recipe: RecipeInfo,
    repo_admin: str,
    repo_root: Path = REPO_ROOT,
    repo: str | None = None,
    dry_run: bool = False,
    days_inactive: int = 0,
    threshold_days: int = DEFAULT_INACTIVE_DAYS,
) -> int | None:
    """Create a branch, delete the recipe folder, commit, push, and open a PR.

    Returns the new PR number, or None on failure or in dry-run mode. If the
    PR was created but its number cannot be read from gh's output, returns 0:
    the PR exists, so this still counts as success.
    """
    branch_name = get_deletion_branch_name(recipe.rel_path)
    title = get_deletion_pr_title(recipe.rel_path)
    body = get_deletion_pr_body(
        recipe.rel_path,
        repo_admin,
        days_inactive=days_inactive,
        threshold_days=threshold_days,
    )

    if dry_run:
        print(
            f"[DRY RUN] Would create branch {branch_name}, delete {recipe.rel_path}, "
            f"push, and open PR assigned to @{repo_admin}"
        )
        return None

    env = _git_env()
    original_ref = _prepare_checkout(repo_root, env)
    if original_ref is None:
        return None
    try:
        # Always branch from the freshly fetched base. Falling back to HEAD
        # would put whatever branch the workflow was dispatched from into
        # the deletion PR.
        _run(["git", "fetch", "-q", "origin", BASE_BRANCH], repo_root, env)
        _run(
            [
                "git",
                "checkout",
                "-q",
                "-B",
                branch_name,
                f"origin/{BASE_BRANCH}",
            ],
            repo_root,
            env,
        )
        _run(["git", "rm", "-r", "-q", "--", recipe.rel_path], repo_root, env)
        _run(
            [
                "git",
                "commit",
                "-q",
                "-m",
                f"Deprecate inactive recipe: {recipe.rel_path}",
            ],
            repo_root,
            env,
        )
        # The branch belongs to this script and no open PR uses it (that case
        # never reaches here), so overwrite whatever an earlier failed run or
        # an unreopenable closed PR left on it. A plain push would be
        # rejected on every future run.
        _run(
            ["git", "push", "-q", "--force", "-u", "origin", branch_name],
            repo_root,
            env,
        )

        pr_cmd = [
            "gh",
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--base",
            BASE_BRANCH,
            "--head",
            branch_name,
            "--assignee",
            repo_admin,
        ]
        if repo:
            pr_cmd.extend(["--repo", repo])
        pr_url = _run(pr_cmd, repo_root, env).stdout.strip()
        print(f"Created PR: {pr_url}")

        try:
            return int(pr_url.rstrip("/").split("/")[-1])
        except ValueError:
            print(
                f"warning: could not parse PR number from {pr_url!r}",
                file=sys.stderr,
            )
            return 0
    except Exception as exc:
        print(
            f"error: failed to create deletion PR for {recipe.rel_path}: {exc}",
            file=sys.stderr,
        )
        return None
    finally:
        _restore_checkout(repo_root, original_ref, env)


def update_deletion_branch(
    recipe: RecipeInfo,
    repo_root: Path = REPO_ROOT,
    dry_run: bool = False,
) -> bool:
    """Bring a reopened deletion PR's branch up to date with the base branch.

    The branch deleted the recipe as it stood when the PR was first opened.
    Files added to the recipe on main since then would survive the merge and
    leave a half-deleted recipe behind, so merge main in and delete whatever
    of the recipe it brings back. Pushes only fast-forwards, which a reopened
    PR accepts.
    """
    branch_name = get_deletion_branch_name(recipe.rel_path)
    if dry_run:
        print(f"[DRY RUN] Would merge {BASE_BRANCH} into {branch_name}")
        return True

    env = _git_env()
    original_ref = _prepare_checkout(repo_root, env)
    if original_ref is None:
        return False
    try:
        _run(
            ["git", "fetch", "-q", "origin", BASE_BRANCH, branch_name],
            repo_root,
            env,
        )
        _run(
            [
                "git",
                "checkout",
                "-q",
                "-B",
                branch_name,
                f"origin/{branch_name}",
            ],
            repo_root,
            env,
        )
        merge = _run(
            ["git", "merge", "-q", "--no-edit", f"origin/{BASE_BRANCH}"],
            repo_root,
            env,
            check=False,
        )
        if merge.returncode != 0:
            # The branch only deletes the recipe, so any conflict is a
            # modify/delete inside it; resolve by deleting. A conflict
            # anywhere else stays unresolved and makes the commit fail.
            # -f because files main added to the recipe are staged by the
            # merge, and a plain `git rm` refuses staged files.
            _run(
                [
                    "git",
                    "rm",
                    "-r",
                    "-q",
                    "-f",
                    "--ignore-unmatch",
                    "--",
                    recipe.rel_path,
                ],
                repo_root,
                env,
            )
            _run(["git", "commit", "-q", "--no-edit"], repo_root, env)
        remaining = _run(
            ["git", "ls-files", "--", recipe.rel_path], repo_root, env
        ).stdout.strip()
        if remaining:
            _run(
                ["git", "rm", "-r", "-q", "--", recipe.rel_path], repo_root, env
            )
            _run(
                [
                    "git",
                    "commit",
                    "-q",
                    "-m",
                    f"Delete files added to {recipe.rel_path} since the "
                    "deprecation PR was opened",
                ],
                repo_root,
                env,
            )
        _run(["git", "push", "-q", "origin", branch_name], repo_root, env)
        print(f"Updated {branch_name} with {BASE_BRANCH}")
        return True
    except Exception as exc:
        print(f"error: failed to update {branch_name}: {exc}", file=sys.stderr)
        return False
    finally:
        _restore_checkout(repo_root, original_ref, env)


def process_recipes(
    repo_root: Path = REPO_ROOT,
    policy_path: Path = POLICY_PATH,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
    dry_run: bool = False,
    now: datetime | None = None,
    repo: str | None = None,
) -> dict:
    """Execute the deprecation process across all recipes in the repository.

    Raises RuntimeError if the existing PRs cannot be listed.
    """
    if now is None:
        now = datetime.now(UTC)

    repo_admin = get_repo_admin(policy_path=policy_path)
    recipes = scan_recipes(repo_root=repo_root)
    prs = fetch_prs(repo=repo)

    summary = {
        "total_recipes": len(recipes),
        "active": 0,
        "inactive_recent": 0,
        "inactive_expired": 0,
        "pr_opened": 0,
        "pr_reopened": 0,
        "pr_existing_open": 0,
        "skipped": 0,
        "failed": 0,
    }

    print(
        f"Scanning {len(recipes)} recipes (repo_admin: @{repo_admin}, inactive threshold: {inactive_days}d)..."
    )

    def open_new_pr(recipe: RecipeInfo, elapsed_days: int) -> None:
        pr_num = create_deletion_pr(
            recipe,
            repo_admin,
            repo_root=repo_root,
            repo=repo,
            dry_run=dry_run,
            days_inactive=elapsed_days,
            threshold_days=inactive_days,
        )
        if pr_num is not None or dry_run:
            summary["pr_opened"] += 1
        else:
            summary["failed"] += 1

    for recipe in recipes:
        classification = classify_recipe(
            recipe.status, recipe.mtime, now=now, threshold_days=inactive_days
        )
        elapsed_days = int((now - recipe.mtime).total_seconds() / 86400.0)

        if classification == "active":
            summary["active"] += 1
        elif classification == "inactive_recent":
            summary["inactive_recent"] += 1
            print(
                f"[RECENT] {recipe.rel_path}: inactive for {elapsed_days}d (< {inactive_days}d) - doing nothing"
            )
        elif classification == "inactive_expired":
            summary["inactive_expired"] += 1
            print(
                f"[EXPIRED] {recipe.rel_path}: inactive for {elapsed_days}d (>= {inactive_days}d) - processing deletion"
            )

            existing_pr = find_existing_deletion_pr(recipe.rel_path, prs)
            if existing_pr and existing_pr.get("number") is not None:
                pr_state = str(existing_pr.get("state", "")).upper()
                pr_num = int(existing_pr["number"])
                if pr_state == "OPEN":
                    summary["pr_existing_open"] += 1
                    print(
                        f"  -> Open deletion PR #{pr_num} already exists for {recipe.rel_path}; doing nothing"
                    )
                elif reopen_pr(pr_num, repo_admin, repo=repo, dry_run=dry_run):
                    summary["pr_reopened"] += 1
                    print(
                        f"  -> Reopened closed deletion PR #{pr_num} for {recipe.rel_path}"
                    )
                    if not update_deletion_branch(
                        recipe, repo_root=repo_root, dry_run=dry_run
                    ):
                        summary["failed"] += 1
                else:
                    # Typically the branch is gone (stale-branch sweep), and
                    # GitHub cannot reopen a PR without it. Retrying the
                    # reopen every run would never succeed.
                    print(
                        f"  -> Could not reopen PR #{pr_num} for {recipe.rel_path}; opening a new PR"
                    )
                    open_new_pr(recipe, elapsed_days)
            else:
                print(
                    f"  -> No PR exists for {recipe.rel_path}; creating branch and opening PR"
                )
                open_new_pr(recipe, elapsed_days)
        else:
            summary["skipped"] += 1

    print("\nSummary:")
    print(f"  Total recipes scanned: {summary['total_recipes']}")
    print(f"  Active recipes: {summary['active']}")
    print(f"  Inactive (< {inactive_days}d): {summary['inactive_recent']}")
    print(f"  Inactive (>= {inactive_days}d): {summary['inactive_expired']}")
    print(f"  Existing open PRs: {summary['pr_existing_open']}")
    print(f"  Reopened PRs: {summary['pr_reopened']}")
    print(f"  New PRs opened: {summary['pr_opened']}")
    print(f"  Failed actions: {summary['failed']}")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deprecate recipes marked status: inactive for 60+ days."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate actions without mutating git or opening PRs.",
    )
    parser.add_argument(
        "--inactive-days",
        type=int,
        default=DEFAULT_INACTIVE_DAYS,
        help=f"Inactive threshold in days (default: {DEFAULT_INACTIVE_DAYS}).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root directory.",
    )
    parser.add_argument(
        "--policy-path",
        type=Path,
        default=POLICY_PATH,
        help="Path to .github/policy.yml.",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="GitHub repository (owner/name) for gh calls.",
    )

    args = parser.parse_args()
    try:
        summary = process_recipes(
            repo_root=args.repo_root,
            policy_path=args.policy_path,
            inactive_days=args.inactive_days,
            dry_run=args.dry_run,
            repo=args.repo,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    if summary["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
