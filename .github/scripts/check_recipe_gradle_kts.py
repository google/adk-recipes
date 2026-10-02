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
Validates a Kotlin recipe's build.gradle.kts (and optional settings.gradle.kts)
against the repository's metadata rules.

Rules enforced:

  - jvm-toolchain: The JVM toolchain must be explicitly declared and must be
    <= the JDK version CI pins in .github/workflows/kotlin-tests.yml
    (currently 17). Both standard Gradle Kotlin DSL declaration forms are
    supported:
      * jvmToolchain(17)
      * languageVersion.set(JavaLanguageVersion.of(17))
    A recipe declaring a higher version than CI provides fails because CI
    cannot build or test it. Lower versions are permitted. If neither form is
    present, an error is reported.
  - repositories: A top-level `repositories { }` block, if present, must
    declare `mavenCentral()` to resolve public dependencies, and must not
    reference custom or private maven repositories (e.g. `maven(url = ...)` or
    `maven { ... }`).
  - root-project-name: If `settings.gradle.kts` exists, `rootProject.name` must
    equal the recipe folder basename (or "<vertical>-<solution>" for plugins).
    The file is optional; if absent, this check is skipped.
  - No description rule: Gradle has no conventional description field.

Limitation note:
  This checker uses regex-based source validation rather than evaluating
  Gradle KTS. A recipe computing its toolchain version dynamically will not
  be matched, and that is accepted. A missed advisory is cheap; a wrong one
  is not.

Usage: python check_recipe_gradle_kts.py <recipe-dir>

Exit codes:
  0  every rule passed (or build.gradle.kts does not exist, which is owned
     by separate structure validation).
  1  contributor-fixable problems found; every one has been reported with
     a fix, both as a human block and as a ::error annotation.
  2  CI fault — the checker crashed, or its own environment is missing a
     dependency it needs. Never blamed on the contributor's files.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ci_message import (
    EXIT_OK,
    Diagnostic,
    Doc,
    guard,
    infra_fault,
    report,
    report_infra_fault,
)

CHECKER = "check_recipe_gradle_kts.py"

# Maximum JDK version supported by CI (see .github/workflows/kotlin-tests.yml).
CI_PINNED_JDK_VERSION = 17

NAMESPACED_ROOTS = {"plugins"}

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _repo_relative_parts(
    recipe_dir: Path, repo_root: Path | None = None
) -> tuple[str, ...]:
    """Path segments of `recipe_dir` relative to the repository root.

    An absolute or relative path is resolved against `repo_root` so that
    leading `./` or parent references are normalized before inspection.
    """
    root = (REPO_ROOT if repo_root is None else repo_root).resolve()
    resolved_dir = (
        (root / recipe_dir).resolve()
        if not recipe_dir.is_absolute()
        else recipe_dir.resolve()
    )
    try:
        return resolved_dir.relative_to(root).parts
    except ValueError:
        return recipe_dir.parts


def expected_project_name(
    recipe_dir: Path, repo_root: Path | None = None
) -> str:
    """Return the expected project name for the recipe directory.

    For core/ and contrib/ recipes, returns the folder basename.
    For plugins/<vertical>/<solution>, returns "<vertical>-<solution>".
    """
    parts = _repo_relative_parts(recipe_dir, repo_root)
    if len(parts) == 3 and parts[0] in NAMESPACED_ROOTS:
        return f"{parts[1]}-{parts[2]}"
    return recipe_dir.name


def _scan_string(content: str, start: int, quote_char: str) -> int:
    """Scan a single-line string and return the index after the closing quote."""
    i = start + 1
    n = len(content)
    while i < n:
        if content[i] == "\\":
            i += 2
            continue
        if content[i] == quote_char:
            return i + 1
        if content[i] == "\n":
            break
        i += 1
    return i


def _skip_string(content: str, i: int) -> int:
    """If index `i` is at a string literal, return the index after it; else `i`."""
    if content.startswith('"""', i):
        end = content.find('"""', i + 3)
        return len(content) if end == -1 else end + 3
    if content[i] in ('"', "'"):
        return _scan_string(content, i, content[i])
    return i


def _strip_comments(content: str) -> str:
    """Strip single-line (//) and multi-line (/* */) comments from Kotlin code."""
    result: list[str] = []
    i = 0
    n = len(content)
    while i < n:
        next_i = _skip_string(content, i)
        if next_i > i:
            result.append(content[i:next_i])
            i = next_i
            continue
        if content.startswith("//", i):
            end = content.find("\n", i + 2)
            if end == -1:
                break
            result.append("\n")
            i = end + 1
            continue
        if content.startswith("/*", i):
            end = content.find("*/", i + 2)
            if end == -1:
                break
            newlines = content[i : end + 2].count("\n")
            result.append("\n" * newlines)
            result.append(" ")
            i = end + 2
            continue
        result.append(content[i])
        i += 1
    return "".join(result)


JVM_TOOLCHAIN_PATTERNS = [
    re.compile(r"jvmToolchain\(\s*(\d+)\s*\)"),
    re.compile(
        r"languageVersion\.set\(\s*JavaLanguageVersion\.of\(\s*(\d+)\s*\)\s*\)"
    ),
]


def check_jvm_toolchain(
    clean_content: str,
    build_gradle_path: Path,
) -> list[Diagnostic]:
    """Check that a JVM toolchain is declared and <= CI's pinned JDK version."""
    found_versions: list[int] = []
    for pattern in JVM_TOOLCHAIN_PATTERNS:
        for match in pattern.finditer(clean_content):
            found_versions.append(int(match.group(1)))

    if not found_versions:
        return [
            Diagnostic(
                check="gradle-kts-jvm-toolchain",
                what=f"JVM toolchain is not declared in {build_gradle_path}.",
                why=(
                    f"Every Kotlin recipe must declare a JVM toolchain "
                    f"version <= {CI_PINNED_JDK_VERSION} (pinned in "
                    f".github/workflows/kotlin-tests.yml)."
                ),
                how=(
                    f"Add the JVM toolchain declaration to "
                    f"{build_gradle_path.name}:\n"
                    f"  kotlin {{\n"
                    f"      jvmToolchain({CI_PINNED_JDK_VERSION})\n"
                    f"  }}"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(build_gradle_path),
            )
        ]

    diagnostics: list[Diagnostic] = []
    for ver in found_versions:
        if ver > CI_PINNED_JDK_VERSION:
            diagnostics.append(
                Diagnostic(
                    check="gradle-kts-jvm-toolchain",
                    what=(
                        f"Declared JVM toolchain ({ver}) in {build_gradle_path} "
                        f"exceeds CI's pinned JDK version "
                        f"({CI_PINNED_JDK_VERSION})."
                    ),
                    why=(
                        f"CI pins JDK {CI_PINNED_JDK_VERSION} (in "
                        f".github/workflows/kotlin-tests.yml). A recipe "
                        f"declaring a higher toolchain version cannot be "
                        f"built or tested in CI."
                    ),
                    how=(
                        f"Lower the JVM toolchain in {build_gradle_path.name} "
                        f"to {CI_PINNED_JDK_VERSION} or below:\n"
                        f"  kotlin {{\n"
                        f"      jvmToolchain({CI_PINNED_JDK_VERSION})\n"
                        f"  }}"
                    ),
                    doc=Doc.PROJECT_NAME,
                    file=str(build_gradle_path),
                )
            )
    return diagnostics


def _extract_top_level_repositories_blocks(content: str) -> list[str]:
    """Extract contents of top-level `repositories { ... }` blocks.

    Tracks string literals and braces so string contents with braces do not
    corrupt depth, and blocks inside nested contexts (e.g. `publishing { ... }`)
    are not treated as top-level dependency repositories.
    """
    blocks: list[str] = []
    i = 0
    n = len(content)
    depth = 0

    while i < n:
        next_i = _skip_string(content, i)
        if next_i > i:
            i = next_i
            continue

        if depth == 0:
            match = re.match(r"repositories\s*\{", content[i:])
            if match:
                start = i + match.end()
                block_depth = 1
                curr = start
                while curr < n and block_depth > 0:
                    next_curr = _skip_string(content, curr)
                    if next_curr > curr:
                        curr = next_curr
                        continue
                    if content[curr] == "{":
                        block_depth += 1
                    elif content[curr] == "}":
                        block_depth -= 1
                    curr += 1
                if block_depth == 0:
                    blocks.append(content[start : curr - 1])
                i = curr
                continue

        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth = max(0, depth - 1)
        i += 1

    return blocks


MAVEN_CENTRAL_PATTERN = re.compile(r"\bmavenCentral\s*\(\s*\)")
PRIVATE_MAVEN_PATTERN = re.compile(r"\bmaven\s*(\(|\{)")


def check_repositories(
    clean_content: str,
    build_gradle_path: Path,
) -> list[Diagnostic]:
    """Check that top-level repositories blocks reference mavenCentral() and no private maven repos."""
    blocks = _extract_top_level_repositories_blocks(clean_content)
    if not blocks:
        return []

    diagnostics: list[Diagnostic] = []
    for block in blocks:
        if not MAVEN_CENTRAL_PATTERN.search(block):
            diagnostics.append(
                Diagnostic(
                    check="gradle-kts-repositories",
                    what=(
                        f"`repositories` block in {build_gradle_path} does "
                        f"not declare `mavenCentral()`."
                    ),
                    why=(
                        "Kotlin recipes must declare `mavenCentral()` in "
                        "their repositories block to resolve public "
                        "dependencies."
                    ),
                    how=(
                        f"Add `mavenCentral()` inside the `repositories {{ }}` "
                        f"block in {build_gradle_path.name}:\n"
                        f"  repositories {{\n"
                        f"      mavenCentral()\n"
                        f"  }}"
                    ),
                    doc=Doc.PROJECT_NAME,
                    file=str(build_gradle_path),
                )
            )

        if PRIVATE_MAVEN_PATTERN.search(block):
            diagnostics.append(
                Diagnostic(
                    check="gradle-kts-repositories",
                    what=(
                        f"`repositories` block in {build_gradle_path} "
                        f"references a custom maven repository."
                    ),
                    why=(
                        "Kotlin recipes must not declare custom or private "
                        "maven repositories. Only public dependencies from "
                        "mavenCentral() are permitted."
                    ),
                    how=(
                        f"Remove custom maven repository declarations from "
                        f"{build_gradle_path.name}."
                    ),
                    doc=Doc.PROJECT_NAME,
                    file=str(build_gradle_path),
                )
            )

    return diagnostics


ROOT_PROJECT_NAME_PATTERN = re.compile(
    r'rootProject\.name\s*(?:=\s*|\.set\(\s*)["\']([^"\']+)["\'](?:\s*\))?'
)


def check_root_project_name(
    recipe_dir: Path,
    repo_root: Path | None = None,
) -> list[Diagnostic]:
    """Check rootProject.name in settings.gradle.kts if present."""
    settings_path = recipe_dir / "settings.gradle.kts"
    if not settings_path.is_file():
        return []

    try:
        content = settings_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [
            Diagnostic(
                check="gradle-kts-parse",
                what=f"{settings_path} is not valid UTF-8: {exc}",
                why=(
                    "The build toolchain requires settings.gradle.kts to be "
                    "valid UTF-8 text."
                ),
                how=f"Re-encode {settings_path.name} as UTF-8.",
                doc=Doc.PROJECT_NAME,
                file=str(settings_path),
            )
        ]

    clean_content = _strip_comments(content)
    expected = expected_project_name(recipe_dir, repo_root)
    match = ROOT_PROJECT_NAME_PATTERN.search(clean_content)

    if not match:
        return [
            Diagnostic(
                check="gradle-kts-root-project-name",
                what=f"`rootProject.name` is not declared in {settings_path}.",
                why=(
                    f"When settings.gradle.kts is present, it must declare "
                    f"`rootProject.name` matching the recipe name "
                    f"('{expected}')."
                ),
                how=(
                    f"Add to {settings_path.name}:\n"
                    f'  rootProject.name = "{expected}"'
                ),
                doc=Doc.PROJECT_NAME,
                file=str(settings_path),
            )
        ]

    declared_name = match.group(1).strip()
    if declared_name != expected:
        return [
            Diagnostic(
                check="gradle-kts-root-project-name",
                what=(
                    f"rootProject.name = '{declared_name}' in "
                    f"{settings_path}, but this recipe must declare "
                    f"'{expected}'."
                ),
                why=(
                    f"rootProject.name in settings.gradle.kts must match the "
                    f"expected recipe name ('{expected}')."
                ),
                how=(
                    f"Update rootProject.name in {settings_path.name}:\n"
                    f'  rootProject.name = "{expected}"'
                ),
                doc=Doc.PROJECT_NAME,
                file=str(settings_path),
            )
        ]

    return []


def _report(diagnostics: list[Diagnostic], recipe_dir: Path) -> int:
    return report(
        diagnostics,
        header=f"{recipe_dir}: build.gradle.kts metadata",
        passed_message=(
            f"{recipe_dir}/build.gradle.kts: JVM toolchain, repositories "
            f"and project name all satisfy the repo rules."
        ),
        next_step=(
            "Update build.gradle.kts (and settings.gradle.kts if present) "
            "to declare a valid JVM toolchain, mavenCentral() repository, "
            "and matching rootProject.name."
        ),
    )


def _run(recipe_dir: Path, repo_root: Path | None = None) -> int:
    build_gradle_path = recipe_dir / "build.gradle.kts"
    if not build_gradle_path.is_file():
        print(
            f"[SKIP] {build_gradle_path} does not exist — the required-files "
            f"check reports that separately."
        )
        return EXIT_OK

    try:
        content = build_gradle_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return _report(
            [
                Diagnostic(
                    check="gradle-kts-parse",
                    what=f"{build_gradle_path} is not valid UTF-8: {exc}",
                    why=(
                        "The build toolchain requires build.gradle.kts to be "
                        "valid UTF-8 text."
                    ),
                    how=f"Re-encode {build_gradle_path.name} as UTF-8.",
                    doc=Doc.PROJECT_NAME,
                    file=str(build_gradle_path),
                )
            ],
            recipe_dir,
        )

    clean_content = _strip_comments(content)
    diagnostics: list[Diagnostic] = []
    diagnostics += check_jvm_toolchain(clean_content, build_gradle_path)
    diagnostics += check_repositories(clean_content, build_gradle_path)
    diagnostics += check_root_project_name(recipe_dir, repo_root)

    return _report(diagnostics, recipe_dir)


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
