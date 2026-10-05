#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Applies per-check recipe exemptions defined in .github/policy.yml.

Enables CI workflows to exclude recipes from specific checks without modifying
the check scripts.

The `filter` command processes recipe paths from stdin:
1. Emits non-exempt recipe paths to stdout.
2. Emits `[SKIP]` notices with reasons to stderr.

Example:
    echo "$DOCKER_RECIPES" | uv run python tools/check_exemptions.py \\
        filter --check recipe-docker-build

Exit codes:
    0  Paths filtered successfully.
    2  CI configuration fault (e.g., invalid policy) or usage error.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import yaml
from ci_message import guard

REPO_ROOT = Path(__file__).parent.parent
POLICY_PATH = REPO_ROOT / ".github" / "policy.yml"
SECTION = "check_exemptions"
RECIPE_ROOTS = ("core", "contrib", "plugins")

_CHECK_ID = re.compile(r"^[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*)?$")
_ENTRY_KEYS = frozenset({"path", "reason"})


@dataclass(frozen=True)
class Exemption:
    """Exemption rule for a recipe path under a given check.

    Attributes:
        check: Check identifier (e.g., `recipe-docker-build`).
        path: Normalized repo-relative path prefix covering all subpaths.
        reason: Single-line rationale logged when the check is skipped.
    """

    check: str
    path: str
    reason: str


class ExemptionPolicyError(ValueError):
    """Raised when .github/policy.yml `check_exemptions` is invalid."""


def _normalize_path(path: str) -> str:
    return path.strip().strip("/")


def _validate_path(raw: object, where: str, problems: list[str]) -> str | None:
    """Validates and normalizes a recipe path from the policy configuration.

    Args:
        raw: Raw `path` entry from policy.yml.
        where: Entry location identifier for error reporting.
        problems: Problem accumulator updated if validation fails.

    Returns:
        Normalized repo-relative path, or None if validation fails.
    """
    if raw is None:
        problems.append(f"{where}: `path` is missing")
        return None
    if not isinstance(raw, str):
        problems.append(f"{where}: `path` must be a string, got {raw!r}")
        return None
    path = _normalize_path(raw)
    if not path:
        problems.append(f"{where}: `path` is empty")
        return None
    parts = path.split("/")
    if parts[0] not in RECIPE_ROOTS:
        problems.append(
            f"{where}: `path` {path!r} must be under one of "
            f"{', '.join(RECIPE_ROOTS)}"
        )
        return None
    if any(part in {"", ".", ".."} for part in parts):
        problems.append(
            f"{where}: `path` {path!r} must not contain empty, '.' or '..' "
            f"components"
        )
        return None
    return path


def _validate_reason(
    raw: object, where: str, problems: list[str]
) -> str | None:
    """Validates and normalizes an exemption reason from the policy.

    Args:
        raw: Raw `reason` entry from policy.yml.
        where: Entry location identifier for error reporting.
        problems: Problem accumulator updated if validation fails.

    Returns:
        Single-line normalized reason string, or None if validation fails.
    """
    if raw is None:
        problems.append(f"{where}: `reason` is missing")
        return None
    if not isinstance(raw, str):
        problems.append(f"{where}: `reason` must be a string, got {raw!r}")
        return None
    reason = " ".join(raw.split())
    if not reason:
        problems.append(f"{where}: `reason` is empty")
        return None
    return reason


def _parse_check_entries(
    check: str, entries: object, problems: list[str]
) -> list[Exemption]:
    """Parses and validates exemption entries for a specific check ID.

    Args:
        check: Check identifier for the entries.
        entries: Raw list of exemption mappings from policy.yml.
        problems: Problem accumulator updated with any validation issues.

    Returns:
        Parsed and validated Exemption instances for the check.
    """
    if entries is None:
        return []
    if not isinstance(entries, list):
        problems.append(
            f"{SECTION}.{check}: must be a list of {{path, reason}} entries"
        )
        return []
    exemptions: list[Exemption] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"{SECTION}.{check}[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where}: must be a mapping with path and reason")
            continue
        extra = sorted(str(k) for k in entry.keys() - _ENTRY_KEYS)
        if extra:
            problems.append(
                f"{where}: unknown keys {', '.join(extra)}; only path and "
                f"reason are allowed"
            )
        path = _validate_path(entry.get("path"), where, problems)
        reason = _validate_reason(entry.get("reason"), where, problems)
        if path is not None and path in seen:
            problems.append(f"{where}: duplicate path {path!r}")
            continue
        if path is not None:
            seen.add(path)
        if path is not None and reason is not None:
            exemptions.append(Exemption(check=check, path=path, reason=reason))
    return exemptions


def load_exemptions(
    policy_path: Path = POLICY_PATH,
) -> dict[str, list[Exemption]]:
    """Loads and validates recipe exemptions from the policy configuration.

    Args:
        policy_path: Path to the policy YAML file.

    Returns:
        Mapping of check IDs to their corresponding Exemption lists. Returns
        an empty mapping if the section is absent.

    Raises:
        ExemptionPolicyError: If the section structure, check IDs, or entries
            are invalid.
    """
    with open(policy_path, encoding="utf-8") as f:
        policy = yaml.safe_load(f) or {}
    section = policy.get(SECTION)
    if not section:
        return {}
    if not isinstance(section, dict):
        raise ExemptionPolicyError(
            f"policy.yml {SECTION} must be a mapping of check id to entries"
        )

    problems: list[str] = []
    exemptions: dict[str, list[Exemption]] = {}
    for check, entries in section.items():
        if not isinstance(check, str) or not _CHECK_ID.fullmatch(check):
            problems.append(
                f"{SECTION}: invalid check id {check!r}; expected "
                f"<workflow> or <workflow>/<check> in lowercase letters, "
                f"digits and hyphens"
            )
            continue
        exemptions[check] = _parse_check_entries(check, entries, problems)

    if problems:
        raise ExemptionPolicyError(
            f"policy.yml {SECTION} is invalid:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )
    return exemptions


def find_exemption(
    exemptions: dict[str, list[Exemption]], check: str, path: str
) -> Exemption | None:
    """Finds an exemption rule matching a path under the specified check.

    Matches whole path components so an exempt recipe directory covers all
    nested files and subdirectories (e.g., `core/python/a` covers
    `core/python/a/x`, but not `core/python/a-b`).

    Args:
        exemptions: Exemption mappings indexed by check ID from
            `load_exemptions()`.
        check: Target check identifier.
        path: Repo-relative path to evaluate.

    Returns:
        Matching Exemption if found; otherwise None.
    """
    parts = _normalize_path(path).split("/")
    for exemption in exemptions.get(check, []):
        prefix = exemption.path.split("/")
        if parts[: len(prefix)] == prefix:
            return exemption
    return None


def render_skip_line(exemption: Exemption, path: str) -> str:
    """Formats the skip log line for an exempt recipe path.

    Args:
        exemption: Exemption rule that matched the path.
        path: Repo-relative path being skipped.

    Returns:
        Formatted `[SKIP]` log message for stderr.
    """
    return (
        f"[SKIP] {path}: exempt from {exemption.check} by policy.yml "
        f"{SECTION}: {exemption.reason}"
    )


def print_non_exempt_paths(
    check: str, policy_path: Path, paths_out: TextIO
) -> int:
    """Filters stdin paths against exemptions for a check.

    Processes incoming paths line-by-line:
    1. Writes non-exempt paths to `paths_out`.
    2. Writes formatted `[SKIP]` messages to stderr.

    Args:
        check: Check identifier to filter against.
        policy_path: Path to the policy YAML file.
        paths_out: Output stream receiving non-exempt paths.

    Returns:
        Exit code 0 on completion.
    """
    exemptions = load_exemptions(policy_path)
    for line in sys.stdin.read().splitlines():
        path = line.strip()
        if not path:
            continue
        exemption = find_exemption(exemptions, check, path)
        if exemption is None:
            print(path, file=paths_out)
        else:
            print(render_skip_line(exemption, path), file=sys.stderr)
    return 0


def main(argv: list[str] | None = None, paths_out: TextIO | None = None) -> int:
    """Parses CLI arguments and executes the requested command.

    Args:
        argv: Command-line arguments excluding program name; defaults to
            `sys.argv[1:]`.
        paths_out: Output stream receiving non-exempt paths; defaults to
            `sys.stdout`.

    Returns:
        Process exit code (0 on success, non-zero on error).
    """
    parser = argparse.ArgumentParser(
        description="Apply policy.yml check_exemptions to a list of paths."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    filter_parser = commands.add_parser(
        "filter", help="Print the stdin paths not exempt from a check."
    )
    filter_parser.add_argument(
        "--check", required=True, help="Check id to filter for."
    )
    args = parser.parse_args(argv)
    return print_non_exempt_paths(
        args.check, POLICY_PATH, paths_out or sys.stdout
    )


if __name__ == "__main__":
    # Redirect stdout to stderr so guard() diagnostic messages do not pollute
    # the downstream recipe path list captured by callers.
    stdout = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        sys.exit(guard("check_exemptions.py", lambda: main(paths_out=stdout)))
