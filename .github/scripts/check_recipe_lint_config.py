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
  - Go: forbid recipe-local .golangci.yml, .golangci.yaml, and .golangci.toml files.
  - Kotlin: forbid recipe-local .editorconfig files containing Kotlin sections
    (e.g., [*.{kt,kts}], [*.kt], [*.kts]).

Style and lint configurations are centralized at the repository root. Recipe-local
configuration files override the repo-wide standards and are forbidden.

Usage: python3 check_recipe_lint_config.py <recipe-dir>

Exit codes:
  0  no forbidden recipe-local lint or style configurations found
  1  contributor-fixable problems found; every one reported with a fix
  2  CI fault — the checker crashed or was invoked wrongly
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))
from ci_message import (  # noqa: E402
    Diagnostic,
    Doc,
    guard,
    infra_fault,
    report,
    report_infra_fault,
)

CHECKER = "check_recipe_lint_config.py"
CHECK = "recipe-lint-config"

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


def _editorconfig_has_kotlin_section(
    path: Path,
) -> tuple[bool, int, str]:
    """Check if an .editorconfig file declares a section targeting Kotlin files.

    Returns (found, line_number, section_header).
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False, 0, ""

    section_re = re.compile(r"^\s*\[([^\]]+)\]")
    kotlin_pattern_re = re.compile(
        r"(?:^|[^\w])(?:kt|kts)(?:$|[^\w])|\*\.kt|\*\.kts|\.kt\b|\.kts\b"
    )

    for lineno, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        m = section_re.match(stripped)
        if m:
            section = m.group(1).strip()
            if kotlin_pattern_re.search(section):
                return True, lineno, section
    return False, 0, ""


def _collect_violations(
    recipe_dir: Path, repo_root: Path | None = None
) -> list[Diagnostic]:
    root = REPO_ROOT if repo_root is None else repo_root
    violations: list[Diagnostic] = []

    for file_path in sorted(recipe_dir.rglob("*")):
        try:
            rel_to_recipe = file_path.relative_to(recipe_dir)
        except ValueError:
            rel_to_recipe = file_path
        if any(part in _EXCLUDED_DIRS for part in rel_to_recipe.parts):
            continue
        if not file_path.is_file():
            continue

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
                )
            )

        # Go: .golangci.yml, .golangci.yaml, .golangci.toml
        elif name in (".golangci.yml", ".golangci.yaml", ".golangci.toml"):
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
                )
            )

        # Kotlin: .editorconfig containing a Kotlin section
        elif name == ".editorconfig":
            has_kt, lineno, section = _editorconfig_has_kotlin_section(
                file_path
            )
            if has_kt:
                violations.append(
                    Diagnostic(
                        check=CHECK,
                        what=(
                            f"Recipe contains a recipe-local .editorconfig "
                            f"declaring Kotlin style section [{section}] at "
                            f"line {lineno}: {rel_path}."
                        ),
                        why=(
                            "Kotlin style is governed entirely by the pinned "
                            "ktlint version in CI. There is no .editorconfig "
                            "in the repository, and recipe-local editorconfig "
                            "rules are forbidden because they override the "
                            "style contract."
                        ),
                        how=(
                            f"Remove the [{section}] section from {rel_path}, "
                            f"or delete {name} if it only configures Kotlin."
                        ),
                        doc=Doc.LINT_CONFIG,
                        file=str(rel_path),
                    )
                )

    return violations


def _run(recipe_dir: Path) -> int:
    violations = _collect_violations(recipe_dir)
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
