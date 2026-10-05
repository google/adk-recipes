#!/usr/bin/env python3
"""
Entry point for all recipe validation tools.

Usage:
  uv run validate [subcommand] [scope]

Both arguments are optional:

  subcommand  — which check(s) to run; one of: manifest, structure, readme,
                placement, all (default: all)
  scope       — what to validate; one of:
                  all                  (default) validate core/, contrib/, plugins/
                  core                 validate core/ only
                  contrib              validate contrib/ only
                  plugins              validate plugins/ only
                  core/<lang>/<recipe> validate a single recipe directory

When only one argument is given and it looks like a path (contains '/') or
is a known root ('core', 'contrib', 'plugins'), it is treated as the scope and
all checks are run.

Examples:
  uv run validate core/python/rag-agent-search    # run all checks on one recipe
  uv run validate core                            # run all checks on core/ only
  uv run validate manifest                        # run manifest check on everything
  uv run validate manifest core                   # run manifest check on core/ only
  uv run validate structure                       # run structural check on everything
  uv run validate structure core/python/rag-agent-search
  uv run validate readme                          # run README check on everything
  uv run validate readme core/python/rag-agent-search

Exit codes:
  0 — all checks passed
  1 — one or more checks failed
"""

import sys

import validate_manifest
import validate_placement
import validate_readme
import validate_structure
from ci_message import EXIT_CI_FAULT, EXIT_OK, EXIT_VIOLATIONS, guard

SUBCOMMANDS = {
    "manifest": ("Manifest validation", validate_manifest.main),
    "structure": ("Structure validation", validate_structure.main),
    "readme": ("README validation", validate_readme.main),
    # Placement answers "is this recipe in the right folder", which the
    # per-recipe checkers above cannot: they only ever see recipes the
    # collector already found, and a misplaced one is missed by that
    # collector. CI runs it as its own job; registering it here is what
    # lets a contributor catch the problem locally before pushing.
    "placement": ("Placement validation", validate_placement.main),
}

VALID_SUBCOMMANDS = [*SUBCOMMANDS, "all"]
RECIPE_ROOTS = validate_manifest.RECIPE_ROOTS


def run_all(scope: str | None) -> int:
    results = []
    for label, tool_main in SUBCOMMANDS.values():
        print(f"\n{'=' * 60}")
        print(f"  {label}")
        print(f"{'=' * 60}")
        exit_code = tool_main(scope)
        results.append((label, exit_code))

    print(f"\n{'=' * 60}")
    print("  Summary")
    print(f"{'=' * 60}")
    statuses = {
        EXIT_OK: "[PASS]",
        EXIT_VIOLATIONS: "[FAIL]",
        EXIT_CI_FAULT: "[CI FAULT]",
    }
    for label, exit_code in results:
        print(f"  {statuses.get(exit_code, '[FAIL]')} {label}")

    # A CI fault outranks a violation. Collapsing 2 into 1 here would tell
    # the workflow "the recipe is invalid" when what actually happened is
    # that one of our own validators crashed — so the contributor gets
    # fix-it advice for a bug they cannot fix.
    if any(code == EXIT_CI_FAULT for _, code in results):
        return EXIT_CI_FAULT
    if any(code != EXIT_OK for _, code in results):
        return EXIT_VIOLATIONS
    return EXIT_OK


def looks_like_scope(arg: str) -> bool:
    """Return True if arg looks like a scope rather than a subcommand."""
    return "/" in arg or arg in RECIPE_ROOTS or arg == "all"


def _dispatch() -> int:
    args = sys.argv[1:]

    # Strip --help / -h and let Python handle it manually so we can keep
    # the argument parsing simple without argparse.
    if "-h" in args or "--help" in args:
        print(__doc__)
        return 0

    subcommand = None
    scope = None

    if len(args) == 0:
        subcommand = "all"
        scope = None
    elif len(args) == 1:
        if looks_like_scope(args[0]):
            # e.g. "uv run validate core/python/rag-agent-search"
            subcommand = "all"
            scope = args[0]
        elif args[0] in VALID_SUBCOMMANDS:
            subcommand = args[0]
            scope = None
        else:
            print(
                f"[ERROR] '{args[0]}' is not a valid subcommand or scope.\n"
                f"  Run 'uv run validate --help' for usage."
            )
            return 1
    elif len(args) == 2:
        if args[0] not in VALID_SUBCOMMANDS:
            print(
                f"[ERROR] '{args[0]}' is not a valid subcommand.\n"
                f"  Valid subcommands: {', '.join(VALID_SUBCOMMANDS)}\n"
                f"  Run 'uv run validate --help' for usage."
            )
            return 1
        subcommand = args[0]
        scope = args[1]
    else:
        print(
            "[ERROR] Too many arguments.\n"
            "  Run 'uv run validate --help' for usage."
        )
        return 1

    if subcommand == "all":
        return run_all(scope)

    _, tool_main = SUBCOMMANDS[subcommand]
    return tool_main(scope)


def main() -> int:
    """Guarded entrypoint.

    This is what `validate = "validate:main"` in pyproject.toml binds, so
    it — not the `__main__` block below — is the boundary CI actually
    crosses when it runs `uv run validate structure`. The guard has to live
    here or a crash in a sub-validator escapes as a bare traceback and the
    workflow reads it as the contributor's recipe being invalid.
    """
    return guard("validate", _dispatch)


if __name__ == "__main__":
    sys.exit(guard("validate", _dispatch))
