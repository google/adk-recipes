#!/usr/bin/env python3
"""
Decide which recipes get a container image built, and what to call it.

Answers one question: which recipe directories under the live roots carry a
Dockerfile at their root, and what image path should each one publish to?

Why this exists as its own module
---------------------------------
`.agents/skills/make-python-recipe-deployable` writes the serving files a
recipe needs — Dockerfile, fast_api_app.py, app_utils/ — and then stops. Its
own words: "Image builds happen later via Cloud Build → Artifact Registry;
this skill's job ends when the files are correct." This module is the front
half of that later step: it turns the tree into a build matrix.

Recipe-root Dockerfiles only
----------------------------
A recipe can contain more than one Dockerfile. contrib/python/multiformat-
hybrid-rag ships four — one for the recipe, three for data-ingestion
sub-services it stands up. Only the file at the recipe root serves the agent
and satisfies the Agent Engine container contract; the others are backing
infrastructure with their own lifecycles. Publishing those under a recipe
image name would misrepresent what the image is, so the walk stops at the
first Dockerfile it finds down each path.

A Dockerfile is not by itself evidence of a recipe
--------------------------------------------------
skills/retail/virtual-tryon/assets/export-template carries one, and it is a
build asset shipped by a skill rather than an agent anybody deploys. The
manifest is what makes a directory a recipe — the schema at
.github/schemas/manifest-schema.json is the same thing `deployable` is
declared in — so a root Dockerfile earns an image only when a manifest.yaml
sits beside it. Without that test the walk publishes whatever happens to be
containerized, and derives a nonsense language ("retail") from the path while
doing it.

Live roots only
---------------
SCAN_ROOTS and SKIP_DIRS are imported from recipe_manifests rather than
restated, because the retired roots (python/agents, java/agents, ... — see
`frozen_paths` in .github/policy.yml) still contain Dockerfiles. Those paths
are closed to new work; images built from them would be published from code
nobody is allowed to fix. Importing means this module cannot drift out of
agreement with the rest of the tooling about what "live" means.

Image naming: <language>/<recipe>, not <root>/<language>/<recipe>
-----------------------------------------------------------------
The root is deliberately dropped. A recipe promoted from contrib/ to core/ is
the same recipe, and its published image name should not change underneath
consumers who have pinned it. Language is kept because it disambiguates:
core/kotlin/llm-auditor and contrib/python/llm-auditor coexist today, and the
leaf name alone would collide.

Zero third-party dependencies, matching its callers.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from recipe_manifests import REPO_ROOT, SCAN_ROOTS, SKIP_DIRS

# Every image lands here. The repository is public-read (allUsers has
# roles/artifactregistry.reader), which is what lets Agent Engine pull an
# image into a customer tenant without a per-consumer IAM grant.
REGISTRY = (
    "us-west1-docker.pkg.dev/adk-samples-repo-gcp-support/adk-recipes-registry"
)

# Changing the matrix or the workflow that consumes it invalidates the
# incremental answer: a push touching only these files matches no recipe
# prefix, so filtering would return nothing and the change would merge having
# never built anything. They rebuild the full set instead.
SELF_PATHS = frozenset(
    {
        ".github/scripts/recipe_images_matrix.py",
        ".github/workflows/recipe-images.yml",
    }
)


def _category_and_name(recipe: Path) -> tuple[str, str]:
    """Split a recipe path into (category, name).

    Recipes live at <root>/<category>/<name>. The segment is a language under
    core/ and contrib/ (python, kotlin) but a vertical under skills/ (retail),
    so it is named for what it is positionally rather than for what it usually
    holds. Anything shallower has no such segment, and the root stands in.
    """
    parts = recipe.parts
    if len(parts) >= 3:
        return parts[1], parts[-1]
    return parts[0], parts[-1]


def discover(repo_root: Path) -> list[dict[str, str]]:
    """Every live recipe with a Dockerfile at its root, shallowest first."""
    found: list[dict[str, str]] = []
    for root in SCAN_ROOTS:
        base = repo_root / root
        if not base.is_dir():
            continue
        # Depth first, not lexicographic. The nested-Dockerfile test below
        # only works if a recipe is recorded before anything beneath it, and
        # sorting by name does not guarantee that: 'foo/Bar/Dockerfile' sorts
        # ahead of 'foo/Dockerfile' because 'B' < 'D'. Depth does guarantee
        # it, and the secondary sort keeps the output stable.
        for dockerfile in sorted(
            base.rglob("Dockerfile"), key=lambda p: (len(p.parts), p)
        ):
            recipe = dockerfile.parent
            rel = recipe.relative_to(repo_root)
            if any(part in SKIP_DIRS for part in rel.parts):
                continue
            if not (recipe / "manifest.yaml").is_file():
                continue
            # A Dockerfile nested under another recipe belongs to a
            # sub-service, not to a recipe of its own.
            if any(str(rel).startswith(entry["path"] + "/") for entry in found):
                continue
            category, name = _category_and_name(rel)
            found.append(
                {
                    "path": str(rel),
                    "name": name,
                    "category": category,
                    "image": f"{REGISTRY}/{category}/{name}",
                }
            )
    return found


def _changed_paths(ref: str, repo_root: Path) -> set[str] | None:
    """Files touched between `ref` and HEAD, or None if the diff failed.

    None and the empty set mean different things and the caller acts on the
    difference: an empty set is a successful diff that found nothing, so
    nothing needs rebuilding, while None means the question could not be
    answered and every recipe should be rebuilt rather than silently skipped.
    """
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{ref}...HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        # A missing or unrelated ref is not worth failing the build over:
        # falling back to "everything changed" is wasteful but never leaves a
        # recipe published from stale source.
        print(
            f"warning: git diff against {ref} failed, treating all recipes as changed",
            file=sys.stderr,
        )
        return None
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--changed-from",
        metavar="REF",
        help="Only include recipes with files touched since REF. Omit to include all.",
    )
    parser.add_argument(
        "--recipe",
        metavar="PATH",
        help="Limit to one recipe path (e.g. core/python/ambient-expense-agent).",
    )
    args = parser.parse_args()

    recipes = discover(REPO_ROOT)

    if args.recipe:
        wanted = args.recipe.rstrip("/")
        recipes = [r for r in recipes if r["path"] == wanted]
        if not recipes:
            print(
                f"error: no recipe with a root Dockerfile at {wanted!r}",
                file=sys.stderr,
            )
            return 1

    if args.changed_from:
        changed = _changed_paths(args.changed_from, REPO_ROOT)
        # None is a failed diff: rebuild everything rather than skip silently.
        # An empty set is a successful diff that found nothing, which
        # correctly narrows the matrix to nothing.
        if changed is not None and not (changed & SELF_PATHS):
            recipes = [
                r
                for r in recipes
                if any(f.startswith(r["path"] + "/") for f in changed)
            ]

    print(json.dumps({"include": recipes}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
