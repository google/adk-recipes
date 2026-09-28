#!/usr/bin/env python3
# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0

import argparse
import os
import re
import shutil
import sys

# Junk that can accumulate in the templates directory locally (e.g. from
# running tooling there) and must never be copied into a scaffolded recipe.
COPY_IGNORE_PATTERNS = (
    ".ruff_cache",
    ".pytest_cache",
    "__pycache__",
    "*.pyc",
    ".DS_Store",
)

# Documented recipe-name rules (kept in sync with SKILL.md's "Recipe Name"
# section). A name matches this pattern iff it is lowercase letters and
# hyphens only and does not start or end with a hyphen.
MAX_RECIPE_NAME_LENGTH = 30
_RECIPE_NAME_RE = re.compile(r"[a-z](?:[a-z-]*[a-z])?")


def recipe_name_error(name: str) -> str | None:
    """Return an error message if ``name`` violates the documented naming
    rules, or ``None`` if the name is valid.

    The rules (mirrored from SKILL.md so direct callers — developers, CI,
    other scripts — get the same guarantees as the agent's pre-validation):
      - lowercase letters and hyphens only (``a-z``, ``-``)
      - 30 characters or fewer
      - does not start or end with a hyphen
    """
    if not name:
        return "the name must not be empty"
    if len(name) > MAX_RECIPE_NAME_LENGTH:
        return (
            f"the name must be {MAX_RECIPE_NAME_LENGTH} characters or fewer "
            f"(got {len(name)})"
        )
    if not _RECIPE_NAME_RE.fullmatch(name):
        return (
            "the name may contain only lowercase letters and hyphens "
            "(a-z, -) and must not start or end with a hyphen"
        )
    return None


def is_safe_recipe_name(name: str) -> bool:
    """Return True if ``name`` is a single, safe path component.

    Rejects empty names, ``.``/``..``, absolute paths, and any name containing
    a path separator, so scaffolding can never escape the output directory.
    """
    if not name or name in (".", ".."):
        return False
    if os.path.isabs(name):
        return False
    return "/" not in name and "\\" not in name


def is_safe_output_dir(output_dir: str) -> bool:
    """Return True if ``output_dir`` contains no ``..`` traversal segments.

    Blocks relative escapes like ``../../other-repo`` for any caller. Absolute
    destinations are allowed here so the function stays usable as a testable
    primitive; the CLI additionally restricts ``--output-dir`` to the
    documented locations (see ``ALLOWED_OUTPUT_DIRS``).
    """
    return ".." not in re.split(r"[\\/]", output_dir)


# Locations the CLI will scaffold into. `contrib` (flat) stays accepted
# because recipes already live there, but `contrib/python` is the documented
# home for new Python recipes.
#
# Vertical skills (skills/<vertical>/<solution>) are deliberately absent:
# they need a different template — SKILL.md, EVAL.yaml, scripts/, assets/,
# references/, tests/unit/ — which this skill does not ship. Refusing is
# better than scaffolding a recipe-shaped tree into a skills path.
ALLOWED_OUTPUT_DIRS = ("contrib", "contrib/python", "core/python")


def is_allowed_output_dir(output_dir: str) -> bool:
    """Return True if ``output_dir`` is one of the documented locations.

    Trailing slashes are tolerated to match the paths shown in the skill's
    instructions.
    """
    return output_dir.rstrip("/") in ALLOWED_OUTPUT_DIRS


def replace_in_file(filepath: str, replacements: dict[str, str]) -> None:
    """Reads a file, replaces placeholder tokens, and writes it back."""
    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    for placeholder, value in replacements.items():
        content = content.replace(placeholder, value)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


def scaffold(
    name: str,
    output_dir: str,
) -> bool:
    # Enforce the documented naming rules (SKILL.md) here, not just in the
    # agent layer, so direct script/CI callers can't create out-of-spec names.
    name_error = recipe_name_error(name)
    if name_error:
        print(f"Error: Invalid recipe name '{name}': {name_error}.")
        return False

    # Defense in depth: the naming rule above already excludes path separators
    # and dot-refs, but keep the explicit traversal guard so the "never escape
    # output_dir" security invariant is checked independently of the style rules.
    if not is_safe_recipe_name(name):
        print(
            f"Error: Invalid recipe name '{name}'. The name must be a single "
            "directory component (no '/', '\\', '..', or absolute path)."
        )
        return False

    # Guard output_dir against relative traversal (e.g. "../../other-repo") so
    # a direct or programmatic call can't drop the recipe outside the intended
    # tree. (The CLI further restricts it to the two documented locations.)
    if not is_safe_output_dir(output_dir):
        print(
            f"Error: Invalid output directory '{output_dir}': must not "
            "contain '..' path segments."
        )
        return False

    # Setup paths relative to the script location
    script_dir = os.path.dirname(os.path.abspath(__file__))
    skill_dir = os.path.dirname(script_dir)
    templates_dir = os.path.join(skill_dir, "resources", "templates")

    if not os.path.isdir(templates_dir):
        print(f"Error: Templates directory not found: {templates_dir}")
        return False

    # Target directory under the workspace
    target_dir = os.path.abspath(os.path.join(output_dir, name))

    if os.path.exists(target_dir):
        print(f"Error: Target directory {target_dir} already exists.")
        return False

    # Copy templates, skipping local caches / junk artifacts.
    shutil.copytree(
        templates_dir,
        target_dir,
        ignore=shutil.ignore_patterns(*COPY_IGNORE_PATTERNS),
    )
    print(f"Copied templates to {target_dir}")

    # Define placeholder replacements
    replacements = {
        "<RECIPE_NAME>": name,
        "<OUTPUT_DIRECTORY>": output_dir.rstrip("/"),
    }

    # Walk through the target directory and apply replacements in all files
    for root, _, files in os.walk(target_dir):
        for file in files:
            filepath = os.path.join(root, file)
            try:
                replace_in_file(filepath, replacements)
            except Exception as e:
                print(f"Warning: Could not process placeholders in {file}: {e}")

    print(f"Successfully scaffolded recipe '{name}' at {target_dir}")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scaffold a new Python ADK recipe."
    )
    parser.add_argument("--name", required=True, help="Name of the recipe")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Output directory inside the repo (e.g. contrib or core/python)",
    )

    args = parser.parse_args()

    # Enforce the documented, valid locations (SKILL.md) at the CLI so a direct
    # invocation can't scaffold outside the repo (e.g. --output-dir
    # ../../other-repo) or into an unintended location.
    if not is_allowed_output_dir(args.output_dir):
        print(
            "Error: --output-dir must be one of "
            f"{ALLOWED_OUTPUT_DIRS} (got '{args.output_dir}')."
        )
        sys.exit(2)

    result = scaffold(
        name=args.name,
        output_dir=args.output_dir,
    )
    # Exit non-zero on failure so CI / automation can detect it (previously
    # every invocation exited 0 regardless of the outcome).
    sys.exit(0 if result else 1)
