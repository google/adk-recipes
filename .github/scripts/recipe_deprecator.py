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
     - If closed/ignored (unmerged): reopens it.
     - If no PR exists: creates a branch, deletes the recipe directory, commits,
       pushes, and opens a new PR.
  3. Assigns new or reopened PRs to repo_admin for manual review.

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
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = Path(__file__).resolve().parents[1] / "policy.yml"

SCAN_ROOTS = ["core", "contrib", "skills"]
SKIP_DIRS = {
    ".venv",
    "node_modules",
    ".gradle",
    ".git",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    "dist",
    "build",
    ".ruff_cache",
    ".agent-tmp",
}

DEFAULT_INACTIVE_DAYS = 60
DEFAULT_REPO_ADMIN = "happyhuman"


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
    recipe_rel_path: str, repo_admin: str, days_inactive: int
) -> str:
    """PR description for recipe deprecation."""
    return (
        f"## Deprecation of inactive recipe\n\n"
        f"This PR deletes the inactive recipe at `{recipe_rel_path}` because "
        f"it has been marked as `status: inactive` for {days_inactive} days (threshold: 60+ days).\n\n"
        f"Assigned to @{repo_admin} for manual review.\n"
    )


def fetch_prs(repo: str | None = None) -> list[dict]:
    """Fetch existing PRs from GitHub using gh CLI."""
    cmd = [
        "gh",
        "pr",
        "list",
        "--state",
        "all",
        "--limit",
        "1000",
        "--json",
        "number,title,headRefName,state,url,assignees,closedAt,mergedAt",
    ]
    if repo:
        cmd.extend(["--repo", repo])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout)
    except Exception as exc:
        print(f"warning: failed to fetch PRs via gh: {exc}", file=sys.stderr)
    return []


def find_existing_deletion_pr(
    recipe_rel_path: str, prs: list[dict]
) -> dict | None:
    """Find existing deletion PR for this recipe."""
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
        elif recipe_rel_path in title and any(
            kw in title.lower() for kw in ("deprecate", "delete")
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
        f"repos/{repo}/issues/{pr_number}/assignees"
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


def create_deletion_pr(
    recipe: RecipeInfo,
    repo_admin: str,
    repo_root: Path = REPO_ROOT,
    repo: str | None = None,
    dry_run: bool = False,
    days_inactive: int = DEFAULT_INACTIVE_DAYS,
) -> int | None:
    """Create a branch, delete the recipe folder, commit, push, and open a PR."""
    branch_name = get_deletion_branch_name(recipe.rel_path)
    title = get_deletion_pr_title(recipe.rel_path)
    body = get_deletion_pr_body(recipe.rel_path, repo_admin, days_inactive)

    if dry_run:
        print(
            f"[DRY RUN] Would create branch {branch_name}, delete {recipe.rel_path}, "
            f"push, and open PR assigned to @{repo_admin}"
        )
        return None

    # Ensure git user is configured if not present
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "github-actions[bot]")
    env.setdefault(
        "GIT_AUTHOR_EMAIL",
        "41898282+github-actions[bot]@users.noreply.github.com",
    )
    env.setdefault("GIT_COMMITTER_NAME", "github-actions[bot]")
    env.setdefault(
        "GIT_COMMITTER_EMAIL",
        "41898282+github-actions[bot]@users.noreply.github.com",
    )

    try:
        # 1. Checkout new branch from origin/main (fallback to main or HEAD)
        checkout_res = subprocess.run(
            ["git", "checkout", "-B", branch_name, "origin/main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if checkout_res.returncode != 0:
            subprocess.run(
                ["git", "checkout", "-B", branch_name],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )

        # 2. Delete recipe directory
        recipe_full_path = repo_root / recipe.rel_path
        if recipe_full_path.exists():
            shutil.rmtree(recipe_full_path, ignore_errors=True)
            subprocess.run(
                ["git", "add", "-A", recipe.rel_path],
                cwd=repo_root,
                check=True,
                env=env,
            )

        # 3. Commit
        commit_msg = f"Deprecate inactive recipe: {recipe.rel_path}"
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )

        # 4. Push to remote
        subprocess.run(
            ["git", "push", "-u", "origin", branch_name],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )

        # 5. Open PR
        pr_cmd = [
            "gh",
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--head",
            branch_name,
            "--assignee",
            repo_admin,
        ]
        if repo:
            pr_cmd.extend(["--repo", repo])

        pr_res = subprocess.run(
            pr_cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        pr_url = pr_res.stdout.strip()
        print(f"Created PR: {pr_url}")

        # Try to parse PR number from URL or output if possible
        pr_num = None
        if "/" in pr_url:
            try:
                pr_num = int(pr_url.rstrip("/").split("/")[-1])
            except ValueError:
                pass

        return pr_num
    except Exception as exc:
        print(
            f"error: failed to create deletion PR for {recipe.rel_path}: {exc}",
            file=sys.stderr,
        )
        return None
    finally:
        # Switch back to main
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )


def process_recipes(
    repo_root: Path = REPO_ROOT,
    policy_path: Path = POLICY_PATH,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
    dry_run: bool = False,
    now: datetime | None = None,
    repo: str | None = None,
) -> dict:
    """Execute the deprecation process across all recipes in the repository."""
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
    }

    print(
        f"Scanning {len(recipes)} recipes (repo_admin: @{repo_admin}, inactive threshold: {inactive_days}d)..."
    )

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
                else:
                    # Closed unmerged PR exists
                    summary["pr_reopened"] += 1
                    print(
                        f"  -> Closed deletion PR #{pr_num} exists for {recipe.rel_path}; reopening"
                    )
                    reopen_pr(pr_num, repo_admin, repo=repo, dry_run=dry_run)
            else:
                # No existing PR
                summary["pr_opened"] += 1
                print(
                    f"  -> No PR exists for {recipe.rel_path}; creating branch and opening PR"
                )
                create_deletion_pr(
                    recipe,
                    repo_admin,
                    repo_root=repo_root,
                    repo=repo,
                    dry_run=dry_run,
                    days_inactive=elapsed_days,
                )
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
    process_recipes(
        repo_root=args.repo_root,
        policy_path=args.policy_path,
        inactive_days=args.inactive_days,
        dry_run=args.dry_run,
        repo=args.repo,
    )


if __name__ == "__main__":
    main()
