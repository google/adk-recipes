#!/usr/bin/env python3
"""
Validate that every recipe sits at the path its root requires.

Recipes are located by their manifest.yaml, and the manifest's depth below
the recipe root tells you whether the recipe is in the right place:

    plugins/<vertical>/<solution>/manifest.yaml     valid
    plugins/<solution>/manifest.yaml                too shallow — no vertical
    plugins/<vertical>/<solution>/x/manifest.yaml   too deep

The vertical (retail/, hr/, finance/) is mandatory under plugins/: it
surfaces ownership and lets a team reason about its whole surface at a
glance. A solution dropped directly under plugins/ has no owning vertical,
so it is rejected.

Scope: only roots listed in validate_manifest.NAMESPACE_REQUIRED_ROOTS are
checked. core/ and contrib/ still permit a flat <root>/<recipe> layout and
are deliberately left alone here — widen CHECKED_ROOTS once that layout is
retired.

This is a whole-tree scan rather than a diff, so it also catches a
misplacement that arrives some other way (a rename, a bad merge). A missing
root is not an error: plugins/ need not exist yet.

Usage:
    uv run python tools/validate_placement.py

Exit codes:
    0 — every recipe is correctly placed
    1 — one or more recipes are misplaced
"""

from __future__ import annotations

import sys
from pathlib import Path

import validate_manifest as vm
from ci_message import Diagnostic, Doc, guard, report

REPO_ROOT = Path(__file__).parent.parent

# Roots this checker enforces. Kept deliberately narrow: these are the roots
# where a namespace is mandatory, so the expected depth is unambiguous.
CHECKED_ROOTS = sorted(vm.NAMESPACE_REQUIRED_ROOTS)

# Directory names never descended into while looking for manifests.
PRUNED_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".ruff_cache"}

# <root>/<namespace>/<recipe>/manifest.yaml
EXPECTED_PARTS = 4


def find_manifests(root: Path) -> list[Path]:
    """Every manifest.yaml beneath `root`, skipping vendored/build dirs."""
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(root.rglob(vm.MANIFEST_FILENAME)):
        if any(part in PRUNED_DIRS for part in path.parts):
            continue
        found.append(path)
    return found


def describe_violation(rel_parts: list[str]) -> Diagnostic | None:
    """Return a diagnostic for a manifest path, or None if it is valid.

    `rel_parts` are the manifest's path components relative to the repo
    root, e.g. ["plugins", "retail", "store-ops", "manifest.yaml"].
    """
    if len(rel_parts) == EXPECTED_PARTS:
        return None

    root = rel_parts[0]
    recipe_dir = "/".join(rel_parts[:-1])
    expected = f"{root}/<vertical>/<solution>/{vm.MANIFEST_FILENAME}"
    why = (
        f"Every {root}/ recipe must live at {expected}. The vertical "
        f"(retail/, hr/, finance/) is mandatory: it is what surfaces "
        f"ownership and lets a team see its whole surface at a glance."
    )

    if len(rel_parts) < EXPECTED_PARTS:
        solution = rel_parts[-2] if len(rel_parts) > 1 else "<solution>"
        return Diagnostic(
            check="placement",
            what=(
                f"'{recipe_dir}' sits directly under '{root}/' with no "
                f"vertical."
            ),
            why=why,
            how=(
                f"Move it one level down, into the vertical that owns it:\n"
                f"  git mv {recipe_dir} {root}/<vertical>/{solution}\n"
                f"e.g. {root}/retail/{solution}"
            ),
            doc=Doc.PLACEMENT,
            file="/".join(rel_parts),
        )

    # rel_parts[1] is the vertical (the only component whose role is fixed
    # by position at this depth); rel_parts[-2] is the directory actually
    # holding the manifest, which is the thing that has to move.
    correct = f"{root}/{rel_parts[1]}/{rel_parts[-2]}"
    return Diagnostic(
        check="placement",
        what=f"'{recipe_dir}' is nested too deeply.",
        why=f"{why} It must sit exactly one directory below its vertical.",
        how=(
            f"Move it up to the solution level:\n"
            f"  git mv {recipe_dir} {correct}\n"
            f"…or, if it belongs to a different vertical, move it to "
            f"{root}/<vertical>/{rel_parts[-2]} instead."
        ),
        doc=Doc.PLACEMENT,
        file="/".join(rel_parts),
    )


def check_root(
    root_name: str,
    repo_root: Path = REPO_ROOT,
    scope: str | None = None,
) -> list[Diagnostic]:
    """Return a diagnostic for every misplaced recipe under a root.

    `scope`, when given, restricts reporting to manifests beneath that
    repo-relative path, so `validate placement core/python/foo` cannot fail
    on an unrelated problem elsewhere in the tree.
    """
    diagnostics: list[Diagnostic] = []
    for manifest in find_manifests(repo_root / root_name):
        rel = manifest.relative_to(repo_root)
        if scope and not (
            str(rel) == scope or str(rel).startswith(f"{scope}/")
        ):
            continue
        diagnostic = describe_violation(list(rel.parts))
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    return diagnostics


def main(scope: str | None = None) -> int:
    """Check every root in CHECKED_ROOTS, or just the part under `scope`.

    Placement is a whole-tree property, so the useful invocation is the
    unscoped one that CI runs. A scope is honoured anyway — it is what the
    other `validate` subcommands accept, and narrowing keeps
    `uv run validate <a-core-recipe>` from failing on an unrelated skill.

    `"all"` means the same thing as no scope at all. It has to be handled
    explicitly: `validate.py` classifies the literal string "all" as a
    SCOPE (looks_like_scope) rather than a subcommand, so `uv run validate
    all` reaches here as scope="all". Treating that as a path prefix made
    every root fail the prefix test below, skip its scan, and return 0 —
    `validate.py` then printed "[PASS] Placement validation" for a check
    that had not run. `validate_manifest.collect_recipe_dirs` already
    folds "all" into None the same way.
    """
    prefix = None if scope in (None, "all") else scope.strip("/")
    diagnostics: list[Diagnostic] = []
    for root_name in CHECKED_ROOTS:
        # A scope pointing outside this root (e.g. core/python/foo) means
        # there is nothing here for it to say.
        if prefix and not (
            prefix == root_name or prefix.startswith(f"{root_name}/")
        ):
            continue
        root_path = REPO_ROOT / root_name
        if not root_path.is_dir():
            print(f"[SKIP] '{root_name}/' does not exist.")
            continue
        # Pass REPO_ROOT explicitly: check_root's default argument is bound
        # at import time, so relying on it would ignore any later rebinding
        # of the module global (which is how the tests point at a fixture).
        diagnostics.extend(
            check_root(root_name, repo_root=REPO_ROOT, scope=prefix)
        )

    return report(
        diagnostics,
        header="Misplaced recipes",
        passed_message=(
            f"Every recipe under "
            f"{', '.join(f'{r}/' for r in CHECKED_ROOTS)} is correctly "
            f"placed."
        ),
        next_step=(
            f"{vm.AUTHORING_DOCS}\n"
            f"\nMove each recipe to the path shown above, then re-run:\n"
            f"  uv run validate placement\n"
            f"\nThe layout itself is described in "
            f"docs/recipe-handbook/anatomy.md."
        ),
    )


if __name__ == "__main__":
    sys.exit(guard("validate_placement.py", main))
