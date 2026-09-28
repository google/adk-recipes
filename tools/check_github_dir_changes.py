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
Verify that changes to the .github/ directory are made only by repository admins.

Files under .github/ (CI workflows, issue templates, automation scripts, and
repository policy configuration) control repository-wide infrastructure and
security. Non-admin pull requests that touch any file under .github/ are
rejected with actionable diagnostic messages.

Reads changed file paths from stdin (one per line) or from --changed-files,
and inspects the PR author's repository permission level.

NOT A SECURITY BOUNDARY
-----------------------
This is a guard rail, not enforcement. The workflow that runs it lives under
.github/ and is triggered by `pull_request`, so GitHub executes the PR's OWN
copy of it -- a contributor can edit or delete the check in the same pull
request that edits the rest of .github/. Read a green result as "nobody
tripped over the rule by accident", never as "nobody can change .github/".
What actually enforces this is GitHub-native and lives outside the tree:

    * a CODEOWNERS entry for `/.github/`, with required review from that team;
    * a repository or organization ruleset restricting those paths;
    * this job marked as a required status check, so DELETING it blocks the
      merge (a never-reported required check is not a pass) instead of
      quietly skipping the gate.

Usage:
    git -c core.quotePath=false diff --no-renames --name-only \\
        origin/main...HEAD | \\
        uv run --no-project python tools/check_github_dir_changes.py \\
            --author "<login>" \\
            --author-association "<association>" \\
            --repo "<owner>/<repo>"

Both git flags are load-bearing. With rename detection on (the default),
moving a file OUT of .github/ reports only the destination path, so the
deletion is invisible to this check. With `core.quotePath` on (the default),
a path containing non-ASCII bytes arrives C-quoted -- `".github/w\\303\\266rk.yml"`
-- whose first path component is `".github`, not `.github`.

Exit codes:
    0  no .github/ changes, or author is a repository administrator
    1  unauthorized changes to .github/ by a non-admin
    2  CI fault -- including "could not determine whether the author is an
       administrator". That still fails closed, but it is reported as our
       problem rather than as an accusation against the contributor.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Stdlib only + repo tools/
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ci_message import (
    EXIT_OK,
    Diagnostic,
    Doc,
    Severity,
    guard,
    infra_fault,
    report,
    report_infra_fault,
)

# `author_association` values that GitHub only assigns to someone with no
# write access to the repository, so the author cannot be an administrator.
# Used only as a fallback once the permission lookups have failed -- see
# check_is_admin. MEMBER and COLLABORATOR are deliberately absent: either may
# hold admin via a team or a direct grant, so neither resolves anything.
_NON_ADMIN_ASSOCIATIONS = frozenset(
    {
        "CONTRIBUTOR",
        "FIRST_TIMER",
        "FIRST_TIME_CONTRIBUTOR",
        "MANNEQUIN",
        "NONE",
    }
)


def _unquote_git_path(path: str) -> str:
    """Undo git's C-style quoting of a path, if present.

    `git diff --name-only` wraps any path containing non-ASCII bytes,
    backslashes or control characters in double quotes and escapes the
    contents -- `".github/workflows/w\\303\\266rk.yml"`. Left as-is the
    leading quote becomes part of the first path component and the file
    stops looking like it is under .github/ at all. Callers should also pass
    `-c core.quotePath=false`; this is the second line of defence behind it,
    and the only protection at all for paths fed in via --changed-files.
    """
    if len(path) < 2 or not (path.startswith('"') and path.endswith('"')):
        return path
    try:
        # latin-1 round-trips the raw bytes that the octal escapes encode,
        # so a multi-byte UTF-8 sequence survives unescaping intact.
        return (
            path[1:-1]
            .encode("latin-1", "backslashreplace")
            .decode("unicode_escape")
            .encode("latin-1")
            .decode("utf-8", "replace")
        )
    except (UnicodeDecodeError, UnicodeEncodeError):
        # Undecodable is still suspicious; strip the quotes and let the
        # prefix match decide rather than silently dropping the path.
        return path[1:-1]


def find_github_files(changed_files: list[str]) -> list[str]:
    """Return all unique file paths sitting under the .github/ directory.

    Matches whole path components so '.github/workflows/foo.yml' matches, but
    a hypothetical '.github-archive/bar.txt' does not.
    """
    github_files: list[str] = []
    seen: set[str] = set()

    for raw in changed_files:
        path = _unquote_git_path(raw.strip())
        if not path:
            continue
        # Normalize leading './' or '/'
        normalized = path
        if normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.lstrip("/")
        parts = normalized.split("/")
        if parts and parts[0] == ".github":
            if normalized not in seen:
                seen.add(normalized)
                github_files.append(normalized)

    return github_files


def _payload_says_admin(data: object) -> bool:
    """Read an admin verdict out of a collaborator-permission API payload."""
    if not isinstance(data, dict):
        return False
    permissions = data.get("permissions")
    return bool(
        data.get("permission") == "admin"
        or data.get("role_name") == "admin"
        or (isinstance(permissions, dict) and permissions.get("admin") is True)
    )


def _query_permission_api(
    repo: str, author: str, token: str | None
) -> bool | None:
    """Ask the REST API. True/False when answered, None when unanswerable."""
    api_url = (
        "https://api.github.com/repos/"
        f"{urllib.parse.quote(repo)}/collaborators/"
        f"{urllib.parse.quote(author)}/permission"
    )
    req = urllib.request.Request(api_url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "adk-recipes-ci")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _payload_says_admin(json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # Answered: the user has no collaborator access at all.
            return False
        # 401/403 (no token, or a token without the reach to ask), 5xx,
        # rate limiting -- all mean "we do not know", not "not an admin".
        return None
    except Exception:
        return None


def _query_permission_cli(repo: str, author: str) -> bool | None:
    """`gh` CLI fallback, for local runs where a developer is logged in."""
    if not shutil.which("gh"):
        return None
    try:
        res = subprocess.run(
            ["gh", "api", f"/repos/{repo}/collaborators/{author}/permission"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if res.returncode != 0:
            return None
        return _payload_says_admin(json.loads(res.stdout))
    except Exception:
        return None


def check_is_admin(
    author: str,
    repo: str | None = None,
    author_association: str | None = None,
    token: str | None = None,
    is_admin_override: bool | None = None,
) -> bool | None:
    """Determine whether the PR author is a repository administrator.

    Returns True (admin), False (definitively not an admin), or None --
    "could not determine". The third state is the point of this function.
    Collapsing it into False, as an earlier version did, means a rate-limited
    API or a workflow token without the reach to ask produces a blocking
    error telling a genuine administrator that they are not one. That is a CI
    fault wearing a contributor's face, and `tools/ci_message.py` exists
    specifically to keep those two apart; `main` turns None into an
    :class:`InfraFault`, which still fails closed but says whose problem it is.

    Resolution order:
      1. Explicit override (if provided, e.g. for testing).
      2. author_association == 'OWNER' -- repository / organization owner.
      3. GitHub REST API collaborator permission check.
      4. `gh` CLI, for local runs where a developer is logged in.
      5. author_association in _NON_ADMIN_ASSOCIATIONS -- a last-resort read
         of the trusted event payload, used ONLY once the lookups have come
         back empty. It sits here rather than ahead of them on purpose: it is
         an inference about what GitHub's label implies, and a real answer
         from the API must always be allowed to overrule an inference.
      6. None -- undetermined.
    """
    if is_admin_override is not None:
        return is_admin_override

    if author_association == "OWNER":
        return True

    if not repo or not author:
        # Nothing to query. This is the documented local-self-check shape
        # (`... | check_github_dir_changes.py --author me`), where the useful
        # answer is "here is what CI would flag", not a CI fault.
        return False

    tok = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    verdict = _query_permission_api(repo, author, tok)
    if verdict is None:
        verdict = _query_permission_cli(repo, author)
    if verdict is not None:
        return verdict

    # Both lookups failed. A fork PR's workflow token is the likeliest cause,
    # and it is also the case we can still answer: GitHub only hands out these
    # associations to someone with no write access, so they cannot be an
    # administrator. Answering here keeps the ordinary outside contributor on
    # the actionable "revert your .github/ changes" path instead of a CI fault.
    if author_association in _NON_ADMIN_ASSOCIATIONS:
        return False

    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify .github/ modifications are made only by repo admins."
    )
    parser.add_argument(
        "--author",
        default="",
        help="PR author GitHub username (login)",
    )
    parser.add_argument(
        "--author-association",
        default="",
        help="PR author association (e.g. OWNER, MEMBER, COLLABORATOR)",
    )
    parser.add_argument(
        "--repo",
        default="",
        help="Repository in owner/name format",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub API token (defaults to GITHUB_TOKEN or GH_TOKEN env var)",
    )
    parser.add_argument(
        "--changed-files",
        type=Path,
        default=None,
        help="Path to a file listing changed files (one per line). Reads stdin if omitted.",
    )
    parser.add_argument(
        "--is-admin",
        dest="is_admin_override",
        default=None,
        type=lambda v: v.lower() in ("true", "1", "yes"),
        help="Explicit boolean override for admin check (primarily for testing)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.changed_files:
        changed_files = args.changed_files.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    else:
        changed_files = sys.stdin.read().splitlines()

    github_files = find_github_files(changed_files)
    if not github_files:
        print("[PASS] No files under .github/ modified in this PR.")
        return EXIT_OK

    is_admin = check_is_admin(
        author=args.author,
        repo=args.repo,
        author_association=args.author_association,
        token=args.token,
        is_admin_override=args.is_admin_override,
    )

    author_desc = args.author or "unknown user"
    repo_desc = args.repo or "this repository"

    if is_admin is None:
        # Fail closed, but do not tell an administrator they are not one.
        return report_infra_fault(
            infra_fault(
                "check_github_dir_changes.py",
                f"Could not determine whether '{author_desc}' is an "
                f"administrator of {repo_desc}: the GitHub permission API and "
                f"the `gh` CLI fallback both failed to answer (no token, "
                f"insufficient token reach, rate limit, or network error). "
                f"{len(github_files)} file(s) under .github/ are changed, so "
                f"the check fails closed rather than guessing.",
            )
        )

    if is_admin:
        print(
            f"[PASS] .github/ changes authorized: PR author '{args.author or 'Author'}' is a repository administrator."
        )
        return EXIT_OK

    # Non-admin user modified files under .github/

    diagnostics = [
        Diagnostic(
            check="github-dir-admin-only",
            what=f"'{file_path}' is under .github/, which can only be modified by repository administrators.",
            why=(
                f"Only repository administrators are permitted to create, modify, or delete files in the "
                f".github/ directory (including GitHub Actions workflows, issue templates, scripts, and repository policy configuration). "
                f"PR author '{author_desc}' does not have administrator permissions on {repo_desc}."
            ),
            how=(
                "Restore .github/ to exactly what is on main. Both lines are needed: "
                "git checkout alone leaves behind any file your branch ADDED under .github/.\n"
                "  git rm -r --quiet --ignore-unmatch .github/\n"
                "  git checkout origin/main -- .github/\n"
                "If CI workflow or repository configuration changes are needed, please open an issue or ask a repository administrator."
            ),
            doc=Doc.GITHUB_DIR_ADMIN,
            file=file_path,
            severity=Severity.ERROR,
        )
        for file_path in github_files
    ]

    count = len(diagnostics)
    noun = "file" if count == 1 else "files"
    return report(
        diagnostics,
        header=f"{count} unauthorized {noun} modified under .github/",
        passed_message="No unauthorized changes to .github/ directory.",
        next_step=(
            "Revert changes to .github/ files in your branch, then push again.\n"
            "Only repository administrators are permitted to modify .github/."
        ),
    )


if __name__ == "__main__":
    sys.exit(guard("check_github_dir_changes.py", main))
