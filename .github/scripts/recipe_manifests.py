#!/usr/bin/env python3
"""
Locate dependency manifests across the recipe tree.

Answers one question: which directories under core/, contrib/, and skills/
contain a dependency manifest, and for which package ecosystem?

Why this exists as its own module
---------------------------------
`.github/dependabot.yml` cannot answer this question, so nothing here feeds
it. That file does carry an entry per recipe ecosystem — but each one exists
purely to suppress updates, and points at `directories: ["/", "**/*"]`. A
glob, not an enumeration.

close_orphan_dependabot_prs.py still needs the concrete list, to decide
whether an open Dependabot PR targets a directory that no longer exists. It
closes what it decides with --delete-branch, so the answer has to be right.

It used to recover the list by regex-scanning the enumerated `directory:`
keys out of dependabot.yml. Those enumerations are gone; the suppression
entries use `directories:` with a glob instead, so the old parser would
recover zero recipe directories, judge every open Dependabot PR an orphan,
and close the lot. The tree is the only source that knows which recipe
directories are live, which is exactly why liveness is derived from it.

STATIC_ENTRIES below covers the other half — entries configured
unconditionally rather than discovered. Nothing in the tree can turn them up,
so they are listed once here and asserted against the real config by
tests/test_dependabot_config.py. An entry present in dependabot.yml but
missing from that list would have its PRs closed as orphans.

Zero third-party dependencies, matching its callers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Top-level directories that hold recipes. Retired roots (python/agents,
# java/agents, ... — see `frozen_paths` in .github/policy.yml) are
# deliberately absent: they are closed to new work and are not dependency-
# managed.
SCAN_ROOTS = ["core", "contrib", "skills"]

# Directory names that are never recipe roots — pruned during the walk.
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
}


# ---------------------------------------------------------------------------
# Ecosystem detectors
# Each returns True if the given directory should be dependency-managed.
# ---------------------------------------------------------------------------


def _is_uv(d: Path) -> bool:
    return (d / "uv.lock").is_file()


def _is_gomod(d: Path) -> bool:
    return (d / "go.mod").is_file()


def _is_maven(d: Path) -> bool:
    return (d / "pom.xml").is_file()


def _is_gradle(d: Path) -> bool:
    # Only the root project has settings.gradle.kts; sub-modules do not.
    return (d / "build.gradle.kts").is_file() and (
        d / "settings.gradle.kts"
    ).is_file()


def _is_npm(d: Path) -> bool:
    pkg = d / "package.json"
    if not pkg.is_file():
        return False
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
        return bool(data.get("dependencies") or data.get("devDependencies"))
    except (json.JSONDecodeError, OSError):
        return False


DETECTORS: list[tuple[str, object]] = [
    ("uv", _is_uv),
    ("gomod", _is_gomod),
    ("maven", _is_maven),
    ("gradle", _is_gradle),
    ("npm", _is_npm),
]


# ---------------------------------------------------------------------------
# Static entries
# ---------------------------------------------------------------------------

# Entries that are configured unconditionally rather than discovered by
# scanning for recipe manifests.
#
# `github-actions` is not a recipe ecosystem: Dependabot reads
# /.github/workflows and any root action.yml, so its directory is always "/"
# and no amount of scanning the recipe tree would turn it up.
#
# close_orphan_dependabot_prs.py folds these into the set of live pairs so
# their PRs are never treated as orphans. Because they cannot be discovered,
# an entry that exists in dependabot.yml but not in this list has its PRs
# closed with --delete-branch — and since such an entry produces roughly one
# grouped PR a week, the --max-close circuit breaker would never trip on it.
# tests/test_dependabot_config.py asserts the two agree, in both directions.
#
# `extra_labels` is carried because dependabot.yml gives github-actions its
# own extra label; keeping it here means the list fully describes the entry
# rather than half of it.
#
# Each tuple is (package-ecosystem, directory, extra_labels).
STATIC_ENTRIES: list[tuple[str, str, list[str]]] = [
    ("github-actions", "/", ["github-actions"]),
]


def static_pairs() -> list[tuple[str, str]]:
    """STATIC_ENTRIES as plain (ecosystem, directory) pairs."""
    return [(eco, directory) for eco, directory, _ in STATIC_ENTRIES]


def scan(repo_root: Path | None = None) -> list[tuple[str, str]]:
    """Return sorted (ecosystem, "/repo/relative/dir") pairs, one per manifest.

    A single recipe can yield more than one pair — `core/python/
    rag-vector-search` and its nested `data_ingestion/` each carry a uv.lock,
    and `core/python/long-horizon-harness/web/server` is an npm manifest three
    levels below its recipe root. That is why this walks the whole tree rather
    than globbing a fixed depth.
    """
    root = REPO_ROOT if repo_root is None else repo_root
    pairs: set[tuple[str, str]] = set()

    for root_name in SCAN_ROOTS:
        root_path = root / root_name
        if not root_path.is_dir():
            continue

        for dirpath, dirnames, _ in os.walk(root_path):
            # Prune in-place so os.walk does not descend into them.
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            directory = Path(dirpath)

            for ecosystem, detector in DETECTORS:
                if detector(directory):  # type: ignore[operator]
                    rel = "/" + directory.relative_to(root).as_posix()
                    pairs.add((ecosystem, rel))

    return sorted(pairs)
