#!/usr/bin/env python3
"""
Build the job matrix for .github/workflows/recipe-canary.yml.

Emits one entry per (recipe, python-version) pair the canary should exercise,
as a JSON array on stdout:

    [{"recipe": "core/python/deep-search", "python": "3.11"}, ...]

Why two Python versions per recipe
----------------------------------
`python-tests.yml` pins Python 3.11 and nothing else, so a recipe declaring
`requires-python = ">=3.11,<3.14"` has its claim to support 3.12 and 3.13
tested nowhere. That is not theoretical: `core/python/rag-agent-search`
advertises `<3.14` and cannot be imported on 3.13 at all — the google-genai
2.10.0 it pins ships a module with a misplaced `from __future__` import, a
hard SyntaxError. It passes cleanly on 3.11.

So the canary tests the floor AND the ceiling of what each recipe claims:
the version everyone actually uses, plus the highest one it promises to work
on. A recipe that only claims 3.11 gets a single job.

This is the rot the canary exists for. A frozen lockfile does not decay on
its own — the interpreter moves underneath it.

Scope
-----
Python recipes under core/, contrib/ and skills/, discovered by manifest.yaml
declaring `language: python`. Recipes marked `status: inactive` are INCLUDED:
a recipe on the retirement path still needs to be noticed if it starts
passing again, and skipping it would make "fixed but never reactivated"
invisible until deletion.

Usage:
    python recipe_canary_matrix.py            # every recipe
    python recipe_canary_matrix.py --recipe core/python/deep-search
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SCAN_ROOTS = ["core", "contrib", "skills"]

SKIP_DIRS = {
    ".venv",
    "node_modules",
    ".git",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    "dist",
    "build",
}

# The floor every recipe must accept (AGENTS.md: "Minimum python version:
# 3.11", and CI pins it).
FLOOR = "3.11"

# The highest minor the canary is willing to provision. A recipe with no
# upper bound declares support for everything, which cannot be tested, so the
# claim is checked up to here and no further. Raise it when the runners and
# the ecosystem have caught up with a new release — and expect new failures,
# because that is the point.
MAX_TESTABLE_MINOR = 13

# Recipes the canary deliberately does not run.
#
# Empty since #2653 deleted the legacy flat-path duplicates it held. A stale
# skip that silently matched a live recipe would hide it from the canary
# forever, so the accompanying test asserts every entry still exists.
SKIP_RECIPES: set[str] = set()

# GitHub fails a workflow outright above 256 matrix jobs.
MAX_MATRIX_JOBS = 256


class MatrixError(RuntimeError):
    """The matrix cannot be built as asked. Always fatal: a canary that
    cannot say which recipes to test must not report success."""


def _is_python_recipe(manifest_path: Path) -> bool:
    """`language: python` in manifest.yaml, without requiring a YAML parser.

    Mirrors the tolerant matcher in python-tests.yml: optional quotes, any
    case, an optional trailing comment. Kept regex-based so the canary matrix
    can be produced with the standard library alone.

    An unreadable manifest is reported on stderr rather than swallowed. A
    recipe silently dropped from the matrix is the worst outcome this script
    has: it is never tested, so it never fails, so it looks healthy forever.
    """
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Not UTF-8. Say so and move on rather than taking the whole month's
        # run down with an uncaught exception.
        #
        # Plain stderr, not a `::warning` annotation: GitHub only collects
        # annotations from stdout, and stdout here is the matrix JSON the
        # workflow parses. tools/tests/test_ci_message.py also forbids
        # hand-built annotations outside Diagnostic, which this stdlib-only
        # script deliberately cannot import.
        print(
            f"WARNING: {manifest_path} is not valid UTF-8; the canary "
            f"cannot read it and is skipping this recipe",
            file=sys.stderr,
        )
        return False
    except OSError as exc:
        print(
            f"WARNING: cannot read {manifest_path} ({exc}); the canary is "
            f"skipping this recipe",
            file=sys.stderr,
        )
        return False
    return bool(
        re.search(
            r"""^[ \t]*language:[ \t]*["']?python["']?[ \t]*(?:\#.*)?$""",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
    )


def discover_recipes(repo_root: Path | None = None) -> list[str]:
    """Repo-relative paths of every Python recipe root, sorted."""
    root = REPO_ROOT if repo_root is None else repo_root
    found: list[str] = []
    for root_name in SCAN_ROOTS:
        root_path = root / root_name
        if not root_path.is_dir():
            continue
        for manifest in sorted(root_path.rglob("manifest.yaml")):
            rel = manifest.parent.relative_to(root).as_posix()
            # Match SKIP_DIRS against the REPO-RELATIVE path. Testing
            # `manifest.parts` matched the absolute path, so a checkout under
            # any directory happening to be named `build`, `dist` or `.venv`
            # — which is every self-hosted runner with a `build/` workspace —
            # excluded every recipe in the repo and produced an empty matrix.
            # recipe_manifests.scan() gets this right; the two now agree.
            if any(part in SKIP_DIRS for part in Path(rel).parts):
                continue
            if not _is_python_recipe(manifest):
                continue
            if rel in SKIP_RECIPES:
                continue
            found.append(rel)
    return sorted(found)


def _lower_bound_minor(requires_python: str) -> int | None:
    """Lowest 3.x minor admitted by a lower bound, if any.

    The mirror of `_upper_bound_minor`, and the same scope: only `>=` and
    `>` are interpreted, and anything else yields None so the caller falls
    back to the repo floor. `>3.11` admits 3.12; `>=3.11` admits 3.11.
    """
    best: int | None = None
    for op, ver in re.findall(r"(>=|>)\s*3\.(\d+)(?:\.\d+)?", requires_python):
        minor = int(ver)
        lowest = minor + 1 if op == ">" else minor
        best = lowest if best is None else max(best, lowest)
    return best


def _upper_bound_minor(requires_python: str) -> int | None:
    """Highest 3.x minor admitted by an upper bound, if any.

    Only `<` and `<=` are interpreted, because they are the only forms the
    repo's recipes use and the only ones with an unambiguous "highest minor I
    claim" reading. Anything else yields None and falls back to
    MAX_TESTABLE_MINOR — the conservative direction is to test MORE, since a
    passing extra job costs a few minutes and a missed one costs a broken
    recipe nobody hears about.
    """
    best: int | None = None
    for op, ver in re.findall(r"(<=|<)\s*3\.(\d+)(?:\.\d+)?", requires_python):
        minor = int(ver)
        # `<3.14` admits 3.13; `<=3.13` admits 3.13.
        highest = minor - 1 if op == "<" else minor
        best = highest if best is None else min(best, highest)
    return best


def python_targets(recipe_dir: Path) -> list[str]:
    """The Python versions the canary should run for one recipe.

    The floor the recipe actually accepts, plus the ceiling it claims when
    that is higher and we can provision it.
    """
    targets = [FLOOR]
    pyproject = recipe_dir / "pyproject.toml"
    if not pyproject.is_file():
        return targets

    try:
        with open(pyproject, "rb") as handle:
            data = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError):
        # A pyproject that will not parse is reported by the validate
        # workflow, which owns that complaint. Fall back to the floor.
        return targets

    requires = (data.get("project") or {}).get("requires-python")
    if not isinstance(requires, str):
        return targets

    # Respect a recipe that declares a floor above the repo's. The
    # python-version-floor rule in check_recipe_pyproject.py requires every
    # recipe to accept 3.11, so this should be unreachable — but if one ever
    # declares `>=3.12`, running it on 3.11 produces a guaranteed install
    # failure that the canary would file against its owner as rot. A false
    # accusation is the one outcome that costs this channel its credibility.
    floor_minor = int(FLOOR.split(".")[1])
    declared_floor = _lower_bound_minor(requires)
    if declared_floor is not None and declared_floor > floor_minor:
        if declared_floor > MAX_TESTABLE_MINOR:
            # The recipe needs an interpreter newer than any this canary
            # provisions. Clamping to MAX_TESTABLE_MINOR would hand back a
            # floor BELOW the one the recipe declared — testing a `>=3.15`
            # recipe on 3.13, guaranteeing an install failure, and filing it
            # against the owner as rot. That is the exact false accusation
            # this block exists to prevent, so decline to test it instead.
            print(
                f"WARNING: {recipe_dir} requires >=3.{declared_floor}, above "
                f"the newest interpreter the canary provisions "
                f"(3.{MAX_TESTABLE_MINOR}); not testing it",
                file=sys.stderr,
            )
            return []
        floor_minor = declared_floor
        targets = [f"3.{floor_minor}"]

    declared = _upper_bound_minor(requires)
    ceiling = MAX_TESTABLE_MINOR if declared is None else declared
    ceiling = min(ceiling, MAX_TESTABLE_MINOR)

    if ceiling > floor_minor:
        targets.append(f"3.{ceiling}")
    return targets


def build_matrix(
    repo_root: Path | None = None, only: str | None = None
) -> list[dict[str, str]]:
    root = REPO_ROOT if repo_root is None else repo_root
    discovered = discover_recipes(root)

    if only is None:
        recipes = discovered
    elif only in discovered:
        recipes = [only]
    else:
        # `--recipe` used to be taken on trust, so a typo produced a matrix
        # pointing at a directory that does not exist (every job failing for
        # a reason that is not the recipe's fault), and a path in
        # SKIP_RECIPES bypassed the skip entirely — canarying by hand exactly
        # the duplicate the skip list exists to keep quiet.
        raise MatrixError(
            f"{only!r} is not a Python recipe the canary tests. Known "
            f"recipes:\n  " + "\n  ".join(discovered)
        )

    matrix: list[dict[str, str]] = []
    for recipe in recipes:
        for version in python_targets(root / recipe):
            matrix.append({"recipe": recipe, "python": version})

    if len(matrix) > MAX_MATRIX_JOBS:
        # GitHub rejects a matrix above 256 jobs with a scheduling error that
        # names no cause. Failing here, with the count, is readable.
        raise MatrixError(
            f"matrix has {len(matrix)} jobs, above GitHub's "
            f"{MAX_MATRIX_JOBS}-job cap, from {len(recipes)} recipes. "
            f"Shard the canary or reduce the versions tested per recipe."
        )
    return matrix


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--recipe",
        help="Limit the matrix to one repo-relative recipe path.",
    )
    args = parser.parse_args(argv)

    try:
        matrix = build_matrix(only=args.recipe)
    except MatrixError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    if not matrix:
        print(
            "No Python recipes found. The canary refuses to report success "
            "on an empty scan — that is indistinguishable from every recipe "
            "having vanished.",
            file=sys.stderr,
        )
        return 1

    json.dump(matrix, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
