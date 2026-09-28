#!/usr/bin/env python3
"""
Map a list of changed file paths to the recipe directories those files
belong to. Reads paths from stdin (one per line), writes recipe
directories to stdout (one per line, sorted, deduplicated).

Called by CI workflows to translate a `git diff --name-only` result into
the set of recipes that need re-validation.

Layout rules (kept in sync with tools/validate_manifest.py):
    <root>/<recipe>/…              flat            (core / contrib)
    <root>/<language>/<recipe>/…   language-nsed   (core / contrib)
    plugins/<vertical>/<solution>/… vertical-nsed   (plugins)

where:
    <root>      ∈ RECIPE_ROOTS               (core / contrib / plugins)
    <language>  ∈ LANGUAGE_NAMESPACE_DIRS    (python / java / go / …)
    <vertical>  is free-form (retail / hr / finance / …) and mandatory —
                see NAMESPACE_REQUIRED_ROOTS in validate_manifest.py

A path whose <recipe> component doesn't correspond to a real directory
on disk is dropped by default — this filters out top-level files like
`core/README.md` and deleted recipes. Pass `--no-filter-existing` to
keep them (useful for tests).

Usage:
    # Every affected recipe, any language:
    git diff --name-only origin/main...HEAD | uv run python tools/affected_recipes.py

    # Only recipes whose language is Python (used by python-validate-recipe.yml):
    git diff --name-only origin/main...HEAD | \\
        uv run python tools/affected_recipes.py --language python

Exit codes:
    0  success (recipe list written to stdout; may be empty)
    2  usage error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import validate_manifest as vm
import yaml

REPO_ROOT = Path(__file__).parent.parent

# Kept aligned with validate_manifest so all consumers of the recipe
# taxonomy — path→recipe mapping here, full-tree collection there —
# agree on which folders are recipe roots and language namespaces.
# If either set grows, only the corresponding line in validate_manifest.py
# needs to change.
RECIPE_ROOTS = vm.RECIPE_ROOTS
LANGUAGE_NAMESPACE_DIRS = vm.LANGUAGE_NAMESPACE_DIRS


def recipe_dir_for(path: str) -> str | None:
    """Return the recipe directory the given path belongs to, or None
    if the path doesn't sit under a recognised recipe root.

    Purely string-based; does no filesystem lookup. Callers can filter
    the results against the working tree separately (see main's
    --filter-existing flag)."""
    parts = path.split("/")
    if len(parts) < 2:
        return None
    root = parts[0]
    if root not in RECIPE_ROOTS:
        return None

    part1 = parts[1] if len(parts) > 1 else ""
    part2 = parts[2] if len(parts) > 2 else ""

    # A trailing-slash or empty second component (e.g. "core/" splits to
    # ["core", ""]) doesn't identify a recipe — bail out rather than
    # returning a bogus candidate like "core/" that the filesystem
    # existence filter would then accept because `core/` is a real dir.
    if not part1:
        return None

    # Roots where the namespace is mandatory (plugins/<vertical>/<solution>).
    # The vertical is free-form, so it can only be recognised by position:
    # a path identifies a solution only when there is something BELOW it,
    # i.e. at least root/vertical/solution/<file>. Anything shallower —
    # `plugins/foo/SKILL.md`, a solution placed directly under the root —
    # deliberately maps to None rather than inventing a recipe at the wrong
    # depth. tools/validate_placement.py reports those as misplaced.
    if root in vm.NAMESPACE_REQUIRED_ROOTS:
        if len(parts) >= 4 and part2:
            return f"{root}/{part1}/{part2}"
        return None

    # Language-namespaced: root/language/recipe/…
    if part1 in LANGUAGE_NAMESPACE_DIRS and part2:
        return f"{root}/{part1}/{part2}"

    # Flat: root/recipe/…  — falls through for anything else.
    return f"{root}/{part1}"


def recipe_language_namespace(recipe_dir: str) -> str | None:
    """If the given recipe directory sits under a language namespace,
    return that language; otherwise None (flat recipe). Purely
    string-based, no filesystem access.

    Layouts:
        <root>/<language>/<recipe>   → returns <language>
        <root>/<recipe>              → returns None (flat)
    """
    parts = recipe_dir.split("/")
    if len(parts) >= 3 and parts[1] in LANGUAGE_NAMESPACE_DIRS:
        return parts[1]
    return None


def _flat_recipe_manifest_language(
    recipe_dir: str, repo_root: Path
) -> str | None:
    """Read manifest.yaml for a FLAT recipe and return its declared
    language (lowercased), or None on missing/unreadable/no-language.
    Callers should not invoke this on namespaced recipes — the path
    already answers the language question for those."""
    manifest = repo_root / recipe_dir / vm.MANIFEST_FILENAME
    if not manifest.is_file():
        return None
    try:
        with open(manifest, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    lang = data.get("language")
    if not isinstance(lang, str):
        return None
    return lang.strip().lower() or None


def _matches_language(recipe_dir: str, language: str, repo_root: Path) -> bool:
    """Path-first language match, mirroring the current
    python-validate-recipe.yml semantics:
      - Namespaced recipe (<root>/<lang>/…): path IS authoritative; the
        manifest is not consulted.
      - Flat recipe (<root>/<recipe>): match on manifest.language.
        Missing/malformed manifest → excluded (nothing to match against)."""
    ns = recipe_language_namespace(recipe_dir)
    if ns is not None:
        return ns == language
    return _flat_recipe_manifest_language(recipe_dir, repo_root) == language


def compute_affected_recipes(
    changed_files: list[str],
    *,
    filter_existing: bool = True,
    language: str | None = None,
    repo_root: Path = REPO_ROOT,
) -> list[str]:
    """Return a sorted, deduplicated list of recipe directories affected
    by the given changed files.

    filter_existing: when True, drop candidates that don't exist as
        directories under repo_root. This removes false positives from
        top-level files (e.g. `core/README.md`) and deleted recipes.
    language: if set, keep only recipes whose language matches.
        Namespaced recipes match on their path segment; flat recipes
        match on manifest.language. Implies filter_existing (the manifest
        must exist to be read)."""
    candidates: set[str] = set()
    for line in changed_files:
        path = line.strip()
        if not path:
            continue
        rd = recipe_dir_for(path)
        if rd is None:
            continue
        candidates.add(rd)

    if filter_existing or language is not None:
        candidates = {c for c in candidates if (repo_root / c).is_dir()}

    if language is not None:
        candidates = {
            c
            for c in candidates
            if _matches_language(c, language.lower(), repo_root)
        }

    return sorted(candidates)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Map changed file paths (from stdin) to affected recipe "
            "directories (to stdout)."
        )
    )
    parser.add_argument(
        "--no-filter-existing",
        dest="filter_existing",
        action="store_false",
        default=True,
        help=(
            "Emit every candidate recipe dir even if it doesn't exist "
            "on disk. Useful for tests; in CI leave the default so "
            "top-level files and deleted recipes drop out."
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help=(
            "Root directory to resolve candidate recipes against when "
            "--filter-existing is on. Defaults to the repo root inferred "
            "from this script's location."
        ),
    )
    parser.add_argument(
        "--language",
        default=None,
        help=(
            "Only emit recipes whose language matches. Namespaced "
            "recipes match on their path segment (e.g. core/python/foo); "
            "flat recipes match on manifest.language. Implies "
            "--filter-existing."
        ),
    )
    args = parser.parse_args()

    changed = sys.stdin.read().splitlines()
    recipes = compute_affected_recipes(
        changed,
        filter_existing=args.filter_existing,
        language=args.language,
        repo_root=Path(args.repo_root),
    )
    for r in recipes:
        print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
