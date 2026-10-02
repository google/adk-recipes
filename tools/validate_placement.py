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

SKILL_FILENAME = "SKILL.md"

# Expected path component counts for each layout type:
#   Legacy:       plugins/<vertical>/<solution>/manifest.yaml
#   Spec plugin:  plugins/<plugin>/plugin.json
#   Spec skill:   plugins/<plugin>/skills/<skill>/SKILL.md
EXPECTED_LEGACY_PARTS = 4
EXPECTED_SPEC_PLUGIN_PARTS = 3
EXPECTED_SPEC_SKILL_PARTS = 5


def _find_files(root: Path, filename: str) -> list[Path]:
    """Every file matching `filename` beneath `root`, skipping vendored/build dirs."""
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(root.rglob(filename)):
        if any(part in PRUNED_DIRS for part in path.parts):
            continue
        found.append(path)
    return found


def find_manifests(root: Path) -> list[Path]:
    """Every manifest.yaml beneath `root`, skipping vendored/build dirs."""
    return _find_files(root, vm.MANIFEST_FILENAME)


def find_plugin_manifests(root: Path) -> list[Path]:
    """Every plugin.json beneath `root`, skipping vendored/build dirs."""
    return _find_files(root, vm.PLUGIN_FILENAME)


def find_skills(root: Path) -> list[Path]:
    """Every SKILL.md beneath `root`, skipping vendored/build dirs."""
    return _find_files(root, SKILL_FILENAME)


def describe_violation(rel_parts: list[str]) -> Diagnostic | None:
    """Return a diagnostic for a manifest or plugin path, or None if it is valid.

    `rel_parts` are the path components relative to the repo root, e.g.
    ["plugins", "retail", "store-ops", "manifest.yaml"] or
    ["plugins", "retail", "plugin.json"].
    """
    if not rel_parts:
        return None

    filename = rel_parts[-1]
    root = rel_parts[0]

    if filename == vm.PLUGIN_FILENAME:
        # plugins/<plugin>/plugin.json (depth 3)
        if len(rel_parts) == EXPECTED_SPEC_PLUGIN_PARTS:
            return None
        recipe_dir = "/".join(rel_parts[:-1])
        if len(rel_parts) < EXPECTED_SPEC_PLUGIN_PARTS:
            return Diagnostic(
                check="placement",
                what=f"'{recipe_dir}' sits directly under '{root}/' with no plugin directory.",
                why=f"In a spec-compliant plugin, {vm.PLUGIN_FILENAME} must sit directly at {root}/<plugin>/{vm.PLUGIN_FILENAME}.",
                how=f"Move it into a plugin directory:\n  git mv {recipe_dir}/{vm.PLUGIN_FILENAME} {root}/<plugin>/{vm.PLUGIN_FILENAME}",
                doc=Doc.PLACEMENT,
                file="/".join(rel_parts),
            )
        return Diagnostic(
            check="placement",
            what=f"'{recipe_dir}' is nested too deeply.",
            why=f"In a spec-compliant plugin, {vm.PLUGIN_FILENAME} must sit directly at {root}/<plugin>/{vm.PLUGIN_FILENAME}.",
            how=f"Move {vm.PLUGIN_FILENAME} up to the plugin root:\n  git mv {'/'.join(rel_parts)} {root}/{rel_parts[1]}/{vm.PLUGIN_FILENAME}",
            doc=Doc.PLACEMENT,
            file="/".join(rel_parts),
        )

    if filename == SKILL_FILENAME:
        # Spec skill: plugins/<plugin>/skills/<skill>/SKILL.md (depth 5)
        if (
            len(rel_parts) == EXPECTED_SPEC_SKILL_PARTS
            and rel_parts[2] == "skills"
        ):
            return None
        return Diagnostic(
            check="placement",
            what=f"'{'/'.join(rel_parts)}' is placed at an invalid location.",
            why="In a spec-compliant plugin, skills must live at plugins/<plugin>/skills/<skill-name>/SKILL.md.",
            how=f"Move {SKILL_FILENAME} to plugins/{rel_parts[1]}/skills/<skill-name>/SKILL.md.",
            doc=Doc.PLACEMENT,
            file="/".join(rel_parts),
        )

    if len(rel_parts) == EXPECTED_LEGACY_PARTS:
        return None

    recipe_dir = "/".join(rel_parts[:-1])
    expected = f"{root}/<vertical>/<solution>/{vm.MANIFEST_FILENAME}"
    why = (
        f"Every {root}/ recipe must live at {expected}. The vertical "
        f"(retail/, hr/, finance/) is mandatory: it is what surfaces "
        f"ownership and lets a team see its whole surface at a glance."
    )

    if len(rel_parts) < EXPECTED_LEGACY_PARTS:
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


def _matches_scope(rel_str: str, scope: str | None) -> bool:
    if not scope:
        return True
    return rel_str == scope or rel_str.startswith(f"{scope}/")


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
    root_path = repo_root / root_name
    if not root_path.is_dir():
        return []

    # Check for manifest at root of plugins/
    root_manifest = root_path / vm.MANIFEST_FILENAME
    if root_manifest.is_file():
        rel = root_manifest.relative_to(repo_root)
        if _matches_scope(str(rel), scope):
            diag = describe_violation(list(rel.parts))
            if diag is not None:
                diagnostics.append(diag)

    root_plugin = root_path / vm.PLUGIN_FILENAME
    if root_plugin.is_file():
        rel = root_plugin.relative_to(repo_root)
        if _matches_scope(str(rel), scope):
            diag = describe_violation(list(rel.parts))
            if diag is not None:
                diagnostics.append(diag)

    for child in sorted(root_path.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        rel_child = child.relative_to(repo_root)
        child_str = str(rel_child)

        if scope and not (
            child_str == scope
            or child_str.startswith(f"{scope}/")
            or scope.startswith(f"{child_str}/")
        ):
            continue

        manifests = find_manifests(child)
        plugins = find_plugin_manifests(child)
        skills = find_skills(child)

        # Check for mixed pattern
        if manifests and plugins:
            diagnostics.append(
                Diagnostic(
                    check="placement",
                    what=f"'{child_str}' mixes legacy and spec-compliant plugin layouts.",
                    why=(
                        "A plugin directory under plugins/ must use either the "
                        "legacy layout (plugins/<vertical>/<solution>/manifest.yaml) "
                        "or the spec-compliant layout (plugins/<plugin>/plugin.json "
                        "with skills/<skill>/SKILL.md), not both."
                    ),
                    how=(
                        "Choose one layout: migrate fully to the spec-compliant "
                        "layout (remove manifest.yaml and use plugin.json with skills/) "
                        "or keep the legacy layout."
                    ),
                    doc=Doc.PLACEMENT,
                    file=child_str,
                )
            )
            continue

        if plugins:
            for p in plugins:
                rel = p.relative_to(repo_root)
                if not _matches_scope(str(rel), scope):
                    continue
                diag = describe_violation(list(rel.parts))
                if diag is not None:
                    diagnostics.append(diag)
            for s in skills:
                rel = s.relative_to(repo_root)
                if not _matches_scope(str(rel), scope):
                    continue
                diag = describe_violation(list(rel.parts))
                if diag is not None:
                    diagnostics.append(diag)
        elif manifests:
            for m_path in manifests:
                rel = m_path.relative_to(repo_root)
                if not _matches_scope(str(rel), scope):
                    continue
                diag = describe_violation(list(rel.parts))
                if diag is not None:
                    diagnostics.append(diag)
        else:
            for s in skills:
                rel = s.relative_to(repo_root)
                if not _matches_scope(str(rel), scope):
                    continue
                diagnostics.append(
                    Diagnostic(
                        check="placement",
                        what=f"'{child_str}' contains skills ({rel}) but is missing plugin.json.",
                        why=(
                            "A spec-compliant plugin container must have a "
                            f"plugin.json file at plugins/<plugin>/{vm.PLUGIN_FILENAME} "
                            f"(or a legacy manifest.yaml at plugins/<vertical>/<solution>/{vm.MANIFEST_FILENAME})."
                        ),
                        how=f"Add {vm.PLUGIN_FILENAME} to {child_str}/{vm.PLUGIN_FILENAME}.",
                        doc=Doc.PLACEMENT,
                        file=child_str,
                    )
                )
                break

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
