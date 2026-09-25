# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Checks recipe source files across all supported languages for hardcoded model
identifier string literals and emits GitHub Actions ::notice annotations.

Regex-based prefix matcher across .py, .ts, .tsx, .java, .kt, .kts, .go files,
with per-language test file exclusions.

Usage: python3 check_model_literals.py <recipe-dir>

Exit codes:
  0  all scanned files checked; notices emitted if model literals found
  2  CI fault — the checker crashed or was invoked wrongly. Never blamed
     on the contributor's files.
"""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ci_message import (
    EXIT_OK,
    guard,
    infra_fault,
    report_infra_fault,
)

CHECKER = "check_model_literals.py"

MODEL_PREFIXES: str = (
    "gemini-|imagen-|claude-|llama-|meta/llama-|mistral-|"
    "codestral-|phi-|grok-|command-|jamba-"
)

MODEL_PATTERN: re.Pattern[str] = re.compile(
    rf"""(['"])({MODEL_PREFIXES})[^ '"]{{1,35}}\1"""
)

TARGET_EXTENSIONS: frozenset[str] = frozenset(
    {".py", ".ts", ".tsx", ".java", ".kt", ".kts", ".go"}
)

_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".agent-tmp",
        "build",
        "dist",
        "target",
        ".gradle",
    }
)


def is_test_file(rel_path: Path) -> bool:
    """Determine whether a file is a test file for its respective language.

    Per-language test exclusions:
      python      */tests/*
      go          *_test.go
      java/kotlin */src/test/*
      typescript  *.test.ts *.test.tsx *.spec.ts *.spec.tsx
    """
    name = rel_path.name
    # Python: */tests/*
    if name.endswith(".py"):
        return "tests" in rel_path.parent.parts

    # Go: *_test.go
    if name.endswith(".go"):
        return name.endswith("_test.go")

    # Java/Kotlin: */src/test/*
    if name.endswith((".java", ".kt", ".kts")):
        return fnmatch.fnmatch(f"/{rel_path.as_posix()}", "*/src/test/*")

    # TypeScript: *.test.ts *.test.tsx *.spec.ts *.spec.tsx
    if name.endswith((".ts", ".tsx")):
        return name.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))

    return False


def collect_candidate_files(recipe_dir: Path) -> list[Path]:
    """Find all candidate source files in recipe_dir excluding test files and ignored dirs."""
    candidates: list[Path] = []
    for file_path in recipe_dir.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            rel_path = file_path.relative_to(recipe_dir)
        except ValueError:
            rel_path = file_path
        if any(part in _EXCLUDED_DIRS for part in rel_path.parent.parts):
            continue
        if any(rel_path.name.endswith(ext) for ext in TARGET_EXTENSIONS):
            if not is_test_file(rel_path):
                candidates.append(file_path)
    return sorted(candidates)


def check_file(file_path: Path) -> list[tuple[int, str]]:
    """Scan a single source file for model literal matches.

    Returns a list of (lineno, match_str) tuples.
    """
    matches: list[tuple[int, str]] = []
    content = file_path.read_text(encoding="utf-8", errors="replace")
    for lineno, line in enumerate(content.splitlines(), start=1):
        for m in MODEL_PATTERN.finditer(line):
            matches.append((lineno, m.group(0)))
    return matches


def _run(recipe_dir: Path) -> int:
    for file_path in collect_candidate_files(recipe_dir):
        matches = check_file(file_path)
        for lineno, match_str in matches:
            print(
                f"::notice file={file_path},line={lineno}::Possible hardcoded "
                f"model name {match_str} detected. If this is a model identifier, "
                f"consider using an environment variable instead."
            )
            print(
                f"[NOTICE] {file_path}:{lineno} \u2014 possible hardcoded "
                f"model name: {match_str}"
            )
    return EXIT_OK


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
