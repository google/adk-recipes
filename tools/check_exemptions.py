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

"""Loads and applies per-check path exemptions defined in .github/policy.yml.

Python checkers evaluate exemptions in three steps:
1. Call `load_exemptions()` once to parse and validate policy exemptions.
2. Call `find_exemption()` for each path to check for an applicable rule.
3. Print `render_skip_line()` to report skipped paths.

Workflow steps and checkers without PyYAML access run the CLI `filter`
command. It reads newline-delimited paths from stdin and prints non-exempt
paths to stdout in input order. Skip notices are routed to stderr so stdout
remains clean:

    printf 'core/python/a\\ncore/python/b\\n' | \\
        uv run --with pyyaml python3 tools/check_exemptions.py \\
        filter --check docker-serves

Exit codes:
    0  Paths filtered successfully.
    2  policy.yml `check_exemptions` configuration is invalid.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import yaml
from ci_message import guard

REPO_ROOT = Path(__file__).parent.parent
POLICY_PATH = REPO_ROOT / ".github" / "policy.yml"
SECTION = "check_exemptions"

# Only register check IDs backed by an active checker to prevent silently
# ignored policy entries.
KNOWN_CHECKS: frozenset[str] = frozenset({"docker-build", "docker-serves"})

RECIPE_ROOTS = ("core", "contrib", "plugins")

_ENTRY_KEYS = frozenset({"path", "reason"})


@dataclass(frozen=True)
class Exemption:
    """Represents an exemption rule for a path from a specific check.

    Attributes:
        check: Identifier of the check the path is exempt from.
        path: Normalized repo-relative path prefix covering all child paths.
        reason: Rationale for the exemption, displayed when the check skips
            the path.
    """

    check: str
    path: str
    reason: str


class ExemptionPolicyError(ValueError):
    """Raised when policy.yml `check_exemptions` contains validation errors."""


def _normalize_path(path: str) -> str:
    """Normalizes a repo-relative path for prefix comparison.

    Args:
        path: Path string from policy configuration or check invocation.

    Returns:
        Path stripped of leading and trailing whitespace and slashes.
    """
    return path.strip().strip("/")


def _validate_path(raw: object, where: str, problems: list[str]) -> str | None:
    """Validates that an exemption path targets an allowed recipe location.

    Enforces non-empty string paths rooted under `core/`, `contrib/`, or
    `plugins/` without directory traversal segments.

    Args:
        raw: Raw path value parsed from policy.yml.
        where: Policy location label used in error messages.
        problems: Accumulator for validation error messages.

    Returns:
        Normalized path string, or None if validation failed.
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
    """Validates and normalizes an exemption reason string.

    Ensures a non-empty string is provided and collapses internal whitespace
    so skip logs format cleanly on a single line.

    Args:
        raw: Raw reason value parsed from policy.yml.
        where: Policy location label used in error messages.
        problems: Accumulator for validation error messages.

    Returns:
        Single-line reason string with collapsed whitespace, or None if invalid.
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
    """Parses and validates exemption entries configured for a check ID.

    Validates mapping keys, checks path and reason syntax, and prevents
    duplicate paths within the same check.

    Args:
        check: Check identifier corresponding to the entries.
        entries: Raw entry list parsed from policy.yml.
        problems: Accumulator for validation error messages.

    Returns:
        Valid Exemption instances for the check, preserving policy order.
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
    """Loads and validates the `check_exemptions` configuration from policy.yml.

    Verifies check IDs against KNOWN_CHECKS and validates each exemption entry.
    All validation errors across the section are accumulated so authors can fix
    all issues in a single pass.

    Args:
        policy_path: Path to the repository policy configuration file.

    Returns:
        Dictionary mapping each check ID to its list of Exemption instances.
        Returns an empty dict if the section is absent or empty.

    Raises:
        ExemptionPolicyError: If the configuration contains syntax or schema
            errors, listing every problem discovered.
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
        if check not in KNOWN_CHECKS:
            problems.append(
                f"{SECTION}: unknown check id {check!r}; known ids: "
                f"{', '.join(sorted(KNOWN_CHECKS))}"
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
    """Finds the exemption covering a path for a given check ID.

    Matches by whole path components so a recipe directory exemption covers
    all nested files and subdirectories without matching sibling prefixes (e.g.
    `core/python/a` matches `core/python/a/x`, but not `core/python/a-b`).

    Args:
        exemptions: Mapping of check IDs to exemptions from load_exemptions().
        check: Identifier of the check querying exemptions.
        path: Repo-relative path to evaluate.

    Returns:
        Matching Exemption instance, or None if the path is not exempt.

    Raises:
        ValueError: If check is not registered in KNOWN_CHECKS, indicating an
            unregistered caller rather than a policy configuration error.
    """
    if check not in KNOWN_CHECKS:
        raise ValueError(
            f"check id {check!r} is not in check_exemptions.KNOWN_CHECKS"
        )
    parts = _normalize_path(path).split("/")
    for exemption in exemptions.get(check, []):
        prefix_parts = exemption.path.split("/")
        if parts[: len(prefix_parts)] == prefix_parts:
            return exemption
    return None


def render_skip_line(exemption: Exemption, path: str) -> str:
    """Formats a standardized skip log line for an exempt path.

    Args:
        exemption: Exemption rule matched for the path.
        path: Path being skipped.

    Returns:
        Formatted `[SKIP]` log line containing path, check ID, and exemption
        reason.
    """
    return (
        f"[SKIP] {path}: exempt from {exemption.check} by policy.yml "
        f"{SECTION}: {exemption.reason}"
    )


def print_non_exempt_paths(
    check: str, policy_path: Path, paths_out: TextIO
) -> int:
    """Filters stdin paths against check exemptions and writes retained paths.

    Routes skip notifications to stderr so paths_out receives only non-exempt
    paths suitable for command pipelines.

    Args:
        check: Check identifier used to filter paths.
        policy_path: Path to the policy.yml configuration file.
        paths_out: Stream receiving non-exempt paths.

    Returns:
        0 on success.
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
    """Parses command-line arguments and executes the requested subcommand.

    Args:
        argv: Command-line arguments excluding program name; defaults to
            sys.argv[1:].
        paths_out: Output stream for non-exempt paths; defaults to sys.stdout.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        description="Apply policy.yml check_exemptions to a list of paths."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    filter_parser = commands.add_parser(
        "filter",
        help="Print the stdin paths that are not exempt from a check.",
    )
    filter_parser.add_argument(
        "--check",
        required=True,
        choices=sorted(KNOWN_CHECKS),
        help="Check id to filter for.",
    )
    args = parser.parse_args(argv)
    return print_non_exempt_paths(
        args.check, POLICY_PATH, paths_out or sys.stdout
    )


if __name__ == "__main__":
    # Redirect stdout so guard() emits CI faults to stderr, keeping stdout
    # clean for paths.
    stdout = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        sys.exit(guard("check_exemptions.py", lambda: main(paths_out=stdout)))
