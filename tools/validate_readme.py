#!/usr/bin/env python3
"""
Validates README.md files in recipe directories under core/ and contrib/.

Checks:
  - Every recipe directory has a README.md file.
  - README.md is not empty.
  - README.md contains no TODO: placeholders.
  - README.md meets a minimum word count (description proxy).
  - README.md has a setup section (heading keyword match).
  - README.md has a run section (heading keyword match).
  - README.md has at least one fenced code block (run command proxy).

Usage:
  # Validate all recipes (both core/ and contrib/):
  python3 tools/validate_readme.py
  python3 tools/validate_readme.py all

  # Validate only core/ or contrib/:
  python3 tools/validate_readme.py core
  python3 tools/validate_readme.py contrib

  # Validate a single recipe:
  python3 tools/validate_readme.py core/python/rag-agent-search

  Dependencies are managed in pyproject.toml. Run `uv sync` once before using.

Exit codes:
  0 — all READMEs present and valid
  1 — one or more READMEs missing or invalid
"""

import argparse
import re
import sys
from pathlib import Path

import validate_manifest
from ci_message import EXIT_VIOLATIONS, Diagnostic, Doc, guard, report

REPO_ROOT = validate_manifest.REPO_ROOT
README_FILENAME = "README.md"
MIN_WORD_COUNT = 100

# Keywords checked against each heading (case-insensitive, word-boundary match).
# Broad enough to accept common synonyms without requiring exact names.
_SETUP_KEYWORDS = frozenset(
    [
        "setup",
        "prerequisites",
        "prerequisite",
        "installation",
        "install",
        "requirements",
        "requirement",
        "configuration",
        "getting started",
        "before you begin",
        "environment",
    ]
)

_RUN_KEYWORDS = frozenset(
    [
        "run",
        "running",
        "usage",
        "quickstart",
        "quick start",
        "start",
        "deploy",
        "deployment",
        "how to run",
        "how to use",
        "launch",
        "launching",
    ]
)

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)


def _has_section(content: str, keywords: frozenset[str]) -> bool:
    """Return True if any heading matches any keyword (word-boundary, case-insensitive)."""
    headings = [m.group(1).lower() for m in _HEADING_RE.finditer(content)]
    for heading in headings:
        for kw in keywords:
            pattern = r"\b" + re.escape(kw) + r"\b"
            if re.search(pattern, heading):
                return True
    return False


def _keyword_list(keywords: frozenset[str]) -> str:
    """Render a keyword set the way it should be typed into a heading."""
    return ", ".join(sorted(kw.title() for kw in keywords))


def validate_readme(readme_path: Path) -> list[Diagnostic]:
    """Returns a list of diagnostics. Empty list means valid."""
    file = validate_manifest.repo_relative(readme_path)
    diagnostics: list[Diagnostic] = []

    try:
        content = readme_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        # UnicodeDecodeError is a ValueError, NOT an OSError, so the
        # `except OSError` below never saw it: a latin-1 README used to
        # abort the whole run with a traceback, taking every other recipe's
        # results with it.
        return [
            Diagnostic(
                check="readme-encoding",
                what=(
                    f"README.md is not valid UTF-8 — byte 0x"
                    f"{e.object[e.start]:02x} at offset {e.start} "
                    f"({e.reason})."
                ),
                why=(
                    "Every file in this repo is read as UTF-8, by CI and by "
                    "GitHub's own renderer. A README saved as latin-1 or "
                    "cp1252 cannot be read by either."
                ),
                how=(
                    "Re-save README.md as UTF-8. From the recipe "
                    "directory:\n"
                    "  iconv -f latin1 -t utf-8 README.md > README.utf8.md "
                    "&& mv README.utf8.md README.md"
                ),
                doc=Doc.README_MISSING,
                file=file,
            )
        ]
    except OSError as e:
        return [
            Diagnostic(
                check="readme-unreadable",
                what=f"README.md could not be read: {e}.",
                why=(
                    "The README is a required file, so a recipe whose "
                    "README cannot be opened cannot be validated."
                ),
                how=(
                    "Check the file's permissions and that it is a regular "
                    "file, not a broken symlink."
                ),
                doc=Doc.README_MISSING,
                file=file,
            )
        ]

    if not content.strip():
        return [
            Diagnostic(
                check="readme-empty",
                what="README.md is empty.",
                why=(
                    "The README is the recipe's front door: it is what a "
                    "reader sees first and the only place the setup and run "
                    "steps are written down."
                ),
                how=(
                    "Write at least a description, a setup section, and a "
                    "run section with the exact command in a fenced code "
                    f"block. Minimum length is {MIN_WORD_COUNT} words."
                ),
                doc=Doc.README_MISSING,
                file=file,
            )
        ]

    # No TODO: placeholders — catches unfilled scaffold or draft text.
    todos = sorted({m.group(0) for m in re.finditer(r"TODO:.*", content)})
    if todos:
        preview = "\n".join(f"  {line[:70]}" for line in todos[:5])
        diagnostics.append(
            Diagnostic(
                check="readme-todo",
                what=(
                    f"README.md still contains {len(todos)} TODO: "
                    f"placeholder(s)."
                ),
                why=(
                    "A TODO: line is scaffold text nobody replaced, so the "
                    "reader is being shown an instruction meant for the "
                    "author."
                ),
                how=f"Replace each of these with real content:\n{preview}",
                doc=Doc.README_TODO,
                file=file,
            )
        )

    # Minimum word count — proxy for a real description being present.
    word_count = len(content.split())
    if word_count < MIN_WORD_COUNT:
        diagnostics.append(
            Diagnostic(
                check="readme-length",
                what=(
                    f"README.md is {word_count} words; the minimum is "
                    f"{MIN_WORD_COUNT}."
                ),
                why=(
                    "Word count is a proxy for a real description being "
                    "present — a README this short has never yet turned out "
                    "to explain what the recipe does."
                ),
                how=(
                    f"Add roughly {MIN_WORD_COUNT - word_count} more words: "
                    f"what the recipe demonstrates, what it needs to run, "
                    f"and what a successful run looks like. Aim for 200-300."
                ),
                doc=Doc.README_SHORT,
                file=file,
            )
        )

    # Setup section — must have a heading matching a setup keyword.
    if not _has_section(content, _SETUP_KEYWORDS):
        diagnostics.append(
            Diagnostic(
                check="readme-setup",
                what="README.md has no setup section.",
                why=(
                    "No heading in the file contains any of the words that "
                    "mark one, so a reader has nowhere to look for the "
                    "prerequisites."
                ),
                how=(
                    f"Add a heading whose text contains one of: "
                    f"{_keyword_list(_SETUP_KEYWORDS)}."
                ),
                doc=Doc.README_SETUP,
                file=file,
            )
        )

    # Run section — must have a heading matching a run keyword.
    if not _has_section(content, _RUN_KEYWORDS):
        diagnostics.append(
            Diagnostic(
                check="readme-run",
                what="README.md has no run section.",
                why=(
                    "No heading in the file contains any of the words that "
                    "mark one, so a reader cannot find how to start the "
                    "agent."
                ),
                how=(
                    f"Add a heading whose text contains one of: "
                    f"{_keyword_list(_RUN_KEYWORDS)}."
                ),
                doc=Doc.README_RUN,
                file=file,
            )
        )

    # At least one fenced code block — the run command must be shown.
    if "```" not in content:
        diagnostics.append(
            Diagnostic(
                check="readme-run",
                what="README.md has no fenced code block.",
                why=(
                    "The exact command to start the agent has to be "
                    "copy-pasteable; prose describing it is not."
                ),
                how=(
                    "Put the run command in a fenced block:\n"
                    "  ```bash\n"
                    "  uv run adk web\n"
                    "  ```"
                ),
                doc=Doc.README_RUN,
                file=file,
            )
        )

    return diagnostics


def main(scope: str | None = None) -> int:
    recipe_dirs = validate_manifest.collect_recipe_dirs(scope)
    if validate_manifest.report_empty_scope(scope, recipe_dirs):
        return EXIT_VIOLATIONS

    diagnostics: list[Diagnostic] = []
    for recipe_dir in recipe_dirs:
        if validate_manifest.is_plugin_container(recipe_dir):
            continue
        readme_path = recipe_dir / README_FILENAME
        if not readme_path.exists():
            diagnostics.append(
                Diagnostic(
                    check="readme-missing",
                    what=f"{README_FILENAME} is missing.",
                    why=(
                        "README.md is required of every recipe "
                        "(policy.required_files.always) — it is the only "
                        "place a reader learns what the recipe does and how "
                        "to run it."
                    ),
                    how=(
                        "Add a README.md with three sections: a description "
                        "of what the recipe demonstrates, a setup section "
                        "(prerequisites, auth, env vars), and a run section "
                        "with the exact command in a fenced code block. "
                        f"Minimum length is {MIN_WORD_COUNT} words."
                    ),
                    doc=Doc.README_MISSING,
                    file=validate_manifest.repo_relative(readme_path),
                )
            )
        else:
            diagnostics.extend(validate_readme(readme_path))

    return report(
        diagnostics,
        header="README.md problems",
        passed_message=(
            f"All {len(recipe_dirs)} recipe README(s) are present and valid."
        ),
        next_step=(
            f"{validate_manifest.AUTHORING_DOCS}\n"
            f"\nRe-run locally:\n"
            f"  uv run validate readme <recipe-path>\n"
            f"\nWhat a README must contain: "
            f"docs/recipe-handbook/anatomy.md."
        ),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Validate recipe README.md files."
    )
    parser.add_argument(
        "scope",
        nargs="?",
        default=None,
        help=(
            "What to validate: 'all' (default), 'core', 'contrib', "
            "or a path to a single recipe (e.g. core/python/rag-agent-search)."
        ),
    )
    args = parser.parse_args()
    sys.exit(guard("validate_readme.py", lambda: main(args.scope)))
