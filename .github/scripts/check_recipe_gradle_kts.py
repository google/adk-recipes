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
  - repositories: A `repositories { }` block, if present, must declare
    `mavenCentral()` to resolve public dependencies, and must not reference
    custom or private maven repositories (e.g. `maven(url = ...)` or
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

NAMESPACED_ROOTS = {"plugins": "vertical"}

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _repo_relative_parts(
    recipe_dir: Path, repo_root: Path | None = None
) -> tuple[str, ...]:
    """Path segments of `recipe_dir` relative to the repository root.

    An absolute path is made relative to `repo_root` so that the position of
    a segment is meaningful. A relative path is taken as already
    repo-relative, which is what CI passes.
    """
    root = REPO_ROOT if repo_root is None else repo_root
    if recipe_dir.is_absolute():
        try:
            return recipe_dir.resolve().relative_to(root.resolve()).parts
        except ValueError:
            return recipe_dir.parts
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


def _strip_comments(content: str) -> str:
    """Strip single-line (//) and multi-line (/* */) comments from Kotlin code."""
    result: list[str] = []
    i = 0
    n = len(content)
    while i < n:
        if content.startswith('"""', i):
            end = content.find('"""', i + 3)
            if end == -1:
                result.append(content[i:])
                break
            result.append(content[i : end + 3])
            i = end + 3
            continue
        if content[i] == '"':
            start = i
            i += 1
            while i < n:
                if content[i] == "\\":
                    i += 2
                    continue
                if content[i] == '"':
                    i += 1
                    break
                if content[i] == "\n":
                    break
                i += 1
            result.append(content[start:i])
            continue
        if content[i] == "'":
            start = i
            i += 1
            while i < n:
                if content[i] == "\\":
                    i += 2
                    continue
                if content[i] == "'":
                    i += 1
                    break
                if content[i] == "\n":
                    break
                i += 1
            result.append(content[start:i])
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


def _extract_repositories_blocks(content: str) -> list[str]:
    """Extract the contents of all `repositories { ... }` blocks."""
    blocks: list[str] = []
    pattern = re.compile(r"\brepositories\s*\{")
    for match in pattern.finditer(content):
        start = match.end()
        depth = 1
        i = start
        n = len(content)
        while i < n and depth > 0:
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
            i += 1
        if depth == 0:
            blocks.append(content[start : i - 1])
    return blocks


MAVEN_CENTRAL_PATTERN = re.compile(r"\bmavenCentral\s*\(\s*\)")
PRIVATE_MAVEN_PATTERN = re.compile(r"\bmaven\s*(\(|\{)")


def check_repositories(
    clean_content: str,
    build_gradle_path: Path,
) -> list[Diagnostic]:
    """Check that repositories blocks reference mavenCentral() and no private maven repos."""
    blocks = _extract_repositories_blocks(clean_content)
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
