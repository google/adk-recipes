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
"""
Validates that a recipe does not contain recipe-local style or lint configuration.

Rules enforced:
  - TypeScript: forbid recipe-local biome.json, biome.jsonc, and .biomerc* files.
  - Go: forbid recipe-local .golangci.yml, .golangci.yaml, .golangci.toml and
    .golangci.json files.
  - Kotlin: forbid recipe-local .editorconfig files that configure Kotlin
    style: a Kotlin section (e.g. [*.{kt,kts}], [*.kt], [*.kts]), a ktlint_*
    or ij_kotlin_* property in any section, or, in a recipe with Kotlin
    sources, a property ktlint honours (indent_size, max_line_length, ...) in
    a section that matches every file, such as [*].

Style and lint configurations are centralized at the repository root. Recipe-local
configuration files override the repo-wide standards and are forbidden.

Usage: python3 check_recipe_lint_config.py <recipe-dir>

The rule is advisory during rollout: findings are reported as warnings
through report_advisories() and the checker still exits 0. To make it
blocking, change _SEVERITY to ERROR and report through report() instead.

Exit codes:
  0  checked; any findings were reported as non-blocking warnings
  1  contributor-fixable problems found (only once the rule is blocking)
  2  CI fault — the checker crashed or was invoked wrongly
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))
from ci_message import (  # noqa: E402
    EXIT_OK,
    Diagnostic,
    Doc,
    Severity,
    guard,
    infra_fault,
    report,
    report_advisories,
    report_infra_fault,
)

CHECKER = "check_recipe_lint_config.py"
CHECK = "recipe-lint-config"

# Advisory until existing recipes are cleaned up. An ERROR diagnostic that
# does not fail the job renders as a red annotation on a green run.
_SEVERITY = Severity.WARNING

_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".git",
        "node_modules",
        ".gradle",
        "build",
    }
)


_KOTLIN_SUFFIXES = (".kt", ".kts")

_SECTION_RE = re.compile(r"^\[(.+)\]$")
# A Kotlin extension in a glob: `*.kt`, `*.kts`, or `kt`/`kts` inside a
# brace list such as `*.{kt,kts}` or `*.{java,kt}`.
_KOTLIN_GLOB_RE = re.compile(r"\.kts?(?!\w)|[{,]\s*kts?\s*(?=[,}])")
# A section whose last path component is `*` or `**` matches every file,
# Kotlin sources included.
_MATCH_ALL_RE = re.compile(r"(?:.*/)?\*{1,2}")
_PROPERTY_RE = re.compile(r"^([\w.-]+)\s*[=:]")
_KOTLIN_PROPERTY_PREFIXES = ("ktlint_", "ij_kotlin_")
# Standard EditorConfig properties that ktlint applies to Kotlin files.
_KTLINT_STANDARD_PROPERTIES = frozenset(
    {
        "indent_size",
        "indent_style",
        "tab_width",
        "max_line_length",
        "insert_final_newline",
    }
)


def _editorconfig_kotlin_rule(
    path: Path, *, has_kotlin_sources: bool
) -> tuple[bool, int, str]:
    """Check if an .editorconfig file configures Kotlin style.

    Flags the first of: a section targeting Kotlin files; a ktlint_* or
    ij_kotlin_* property in any section; or, when the recipe has Kotlin
    sources, a standard property ktlint honours in a section that matches
    every file. ktlint applies [*] to Kotlin files too, so checking section
    headers alone misses those overrides. The last case is limited to Kotlin
    recipes because [*] indent_size in, say, a Python recipe changes nothing
    ktlint checks.

    Returns (found, line_number, description of what was found).
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False, 0, ""

    section = ""
    for lineno, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        m = _SECTION_RE.match(stripped)
        if m:
            section = m.group(1).strip()
            if _KOTLIN_GLOB_RE.search(section):
                return True, lineno, f"Kotlin style section [{section}]"
            continue
        m = _PROPERTY_RE.match(stripped)
        if not m:
            continue
        key = m.group(1).lower()
        where = f"section [{section}]" if section else "the preamble"
        if key.startswith(_KOTLIN_PROPERTY_PREFIXES):
            return True, lineno, f"Kotlin property {key} in {where}"
        if (
            has_kotlin_sources
            and key in _KTLINT_STANDARD_PROPERTIES
            and _MATCH_ALL_RE.fullmatch(section)
        ):
            return True, lineno, f"property {key} in {where}"
    return False, 0, ""


def _is_excluded(file_path: Path, recipe_dir: Path) -> bool:
    try:
        rel_to_recipe = file_path.relative_to(recipe_dir)
    except ValueError:
        rel_to_recipe = Path(file_path.name)
    return any(part in _EXCLUDED_DIRS for part in rel_to_recipe.parts)


def _collect_violations(
    recipe_dir: Path, repo_root: Path | None = None
) -> list[Diagnostic]:
    root = REPO_ROOT if repo_root is None else repo_root
    violations: list[Diagnostic] = []

    files = [
        f
        for f in sorted(recipe_dir.rglob("*"))
        if not _is_excluded(f, recipe_dir) and f.is_file()
    ]
    has_kotlin_sources = any(f.suffix in _KOTLIN_SUFFIXES for f in files)

    for file_path in files:
        # If the file is directly at the repo root, it is not recipe-local.
        try:
            if file_path.resolve() == (root / file_path.name).resolve():
                continue
        except (ValueError, OSError):
            pass

        name = file_path.name
        try:
            rel_path = file_path.relative_to(root)
        except ValueError:
            rel_path = file_path

        # TypeScript: biome.json, biome.jsonc, .biomerc*
        if name in ("biome.json", "biome.jsonc") or name.startswith(".biomerc"):
            violations.append(
                Diagnostic(
                    check=CHECK,
                    what=(
                        f"Recipe contains a recipe-local Biome configuration "
                        f"file: {rel_path}."
                    ),
                    why=(
                        "Biome style and lint configuration is centralized in "
                        "the repo root biome.json / biome.jsonc. Recipe-local "
                        "Biome configuration files are forbidden because "
                        "nested configs silently override the repository "
                        "standard."
                    ),
                    how=(
                        f"Delete {name} from the recipe. If repository-wide "
                        f"style changes are needed, update the root biome.json."
                    ),
                    doc=Doc.LINT_CONFIG,
                    file=str(rel_path),
                    severity=_SEVERITY,
                )
            )

        # Go: .golangci.yml, .golangci.yaml, .golangci.toml, .golangci.json
        elif name in (
            ".golangci.yml",
            ".golangci.yaml",
            ".golangci.toml",
            ".golangci.json",
        ):
            violations.append(
                Diagnostic(
                    check=CHECK,
                    what=(
                        f"Recipe contains a recipe-local golangci-lint "
                        f"configuration file: {rel_path}."
                    ),
                    why=(
                        "Go lint configuration is centralized in the repo "
                        "root .golangci.yml. golangci-lint walks up from the "
                        "module directory, so a recipe-level file silently "
                        "overrides the repository standard."
                    ),
                    how=(
                        f"Delete {name} from the recipe. If repository-wide "
                        f"lint rules need updating, update the root "
                        f".golangci.yml."
                    ),
                    doc=Doc.LINT_CONFIG,
                    file=str(rel_path),
                    severity=_SEVERITY,
                )
            )

        # Kotlin: .editorconfig that configures Kotlin style
        elif name == ".editorconfig":
            has_kt, lineno, found = _editorconfig_kotlin_rule(
                file_path, has_kotlin_sources=has_kotlin_sources
            )
            if has_kt:
                violations.append(
                    Diagnostic(
                        check=CHECK,
                        what=(
                            f"Recipe contains a recipe-local .editorconfig "
                            f"that configures Kotlin style ({found}, line "
                            f"{lineno}): {rel_path}."
                        ),
                        why=(
                            "Kotlin style is governed by the ktlint version "
                            "pinned in CI. Recipe-local editorconfig rules "
                            "are forbidden because ktlint reads them and "
                            "they override that style contract."
                        ),
                        how=(
                            f"Remove the {found} (line {lineno}) from "
                            f"{rel_path}, or delete {name} if it only "
                            f"configures Kotlin. ktlint also applies a "
                            f"section such as [*] to Kotlin files."
                        ),
                        doc=Doc.LINT_CONFIG,
                        file=str(rel_path),
                        severity=_SEVERITY,
                    )
                )

    return violations


def _run(recipe_dir: Path) -> int:
    violations = _collect_violations(recipe_dir)
    if violations:
        report_advisories(
            violations,
            header=f"{recipe_dir}: recipe-local lint configuration",
        )
        return EXIT_OK
    return report(
        violations,
        header=f"{recipe_dir}: recipe-local lint configuration",
        passed_message=(
            f"{recipe_dir}: carries no forbidden recipe-local lint or style "
            f"configurations."
        ),
        next_step=(
            "Style and lint configurations are centralized at the repository "
            "root. Delete recipe-local config files to use the repo standard."
        ),
    )


def main() -> int:
    if len(sys.argv) != 2:
        return report_infra_fault(
            infra_fault(
                CHECKER,
                f"invoked with {len(sys.argv) - 1} argument(s); expected "
                f"exactly one recipe directory.",
            )
        )

    recipe_dir = Path(sys.argv[1])
    if not recipe_dir.is_dir():
        return report_infra_fault(
            infra_fault(
                CHECKER,
                f"{recipe_dir} is not a directory. The path came from the "
                f"workflow's recipe discovery step, not from the "
                f"contributor.",
            )
        )

    try:
        return _run(recipe_dir)
    except Exception as exc:
        return report_infra_fault(
            infra_fault(CHECKER, f"{type(exc).__name__}: {exc}")
        )


if __name__ == "__main__":
    sys.exit(guard(CHECKER, main))
