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
Validates a Java recipe's pom.xml against the repository's metadata rules.

Rules enforced:

  - artifact-id: <artifactId> must match the recipe folder basename (or
    "<vertical>-<solution>" for plugins).
  - java-release-floor: <maven.compiler.release> in <properties> is CANONICAL.
    A recipe setting only maven.compiler.source / maven.compiler.target fails,
    with a message telling it to use release instead. The declared release must
    be <= the JDK version CI pins in .github/workflows/java-tests.yml
    (currently 17). Release <= 17 passes (e.g. 17 or 11); release > 17 fails.
  - description: If <description> is set in pom.xml, it must match
    manifest.description in manifest.yaml (after .strip()). Optional; skipped
    if <description> or manifest.yaml is absent.
  - repositories: <repositories>, <pluginRepositories>, and <mirrors> blocks,
    if present, must point to Maven Central and must not reference custom or
    private repositories.

Usage: python3 check_recipe_pom.py <recipe-dir>

Exit codes:
  0  every rule passed (or pom.xml does not exist, which is skipped here).
  1  contributor-fixable problems found; every one has been reported with
     a fix, both as a human block and as a ::error annotation.
  2  CI fault — the checker crashed, or was invoked wrongly. Never blamed on
     the contributor's files.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

import yaml

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

CHECKER = "check_recipe_pom.py"

# Maximum JDK version supported by CI (see .github/workflows/java-tests.yml).
CI_PINNED_JDK_VERSION = 17

NAMESPACED_ROOTS = {"plugins"}

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

MAVEN_CENTRAL_HOSTS = frozenset(
    {
        "repo.maven.apache.org",
        "repo1.maven.org",
        "repo.maven.org",
        "central.maven.org",
        "repo2.maven.org",
    }
)


def _repo_relative_parts(
    recipe_dir: Path, repo_root: Path | None = None
) -> tuple[str, ...]:
    """Path segments of `recipe_dir` relative to the repository root."""
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
    """Return the expected project/artifact name for the recipe directory.

    For core/ and contrib/ recipes, returns the folder basename.
    For plugins/<vertical>/<solution>, returns "<vertical>-<solution>".
    """
    parts = _repo_relative_parts(recipe_dir, repo_root)
    if len(parts) == 3 and parts[0] in NAMESPACED_ROOTS:
        return f"{parts[1]}-{parts[2]}"
    return recipe_dir.name


def _local_tag(elem: ET.Element) -> str:
    """Return the local XML tag name stripped of any XML namespace."""
    tag = elem.tag
    return tag.split("}")[-1] if "}" in tag else tag


def _find_child(parent: ET.Element, tag_name: str) -> ET.Element | None:
    for child in parent:
        if _local_tag(child) == tag_name:
            return child
    return None


def _find_all_children(parent: ET.Element, tag_name: str) -> list[ET.Element]:
    return [child for child in parent if _local_tag(child) == tag_name]


def check_artifact_id(
    root_elem: ET.Element,
    pom_path: Path,
    recipe_dir: Path,
    repo_root: Path | None = None,
) -> list[Diagnostic]:
    """Check that <artifactId> matches the expected recipe directory name."""
    expected = expected_project_name(recipe_dir, repo_root)
    artifact_id_elem = _find_child(root_elem, "artifactId")
    if artifact_id_elem is None or not (artifact_id_elem.text or "").strip():
        return [
            Diagnostic(
                check="pom-artifact-id",
                what=f"<artifactId> is not declared in {pom_path}.",
                why=(
                    f"Every Java recipe must declare <artifactId> matching "
                    f"its directory name ('{expected}')."
                ),
                how=(
                    f"Add <artifactId> to {pom_path.name}:\n"
                    f"  <artifactId>{expected}</artifactId>"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(pom_path),
            )
        ]

    declared = (artifact_id_elem.text or "").strip()
    if declared != expected:
        return [
            Diagnostic(
                check="pom-artifact-id",
                what=(
                    f"<artifactId> in {pom_path} ('{declared}') does not match "
                    f"expected recipe name ('{expected}')."
                ),
                why=(
                    f"Every Java recipe must declare an <artifactId> matching "
                    f"its folder name ('{expected}') for consistent build "
                    f"artifact naming."
                ),
                how=(
                    f"Update <artifactId> in {pom_path.name} to:\n"
                    f"  <artifactId>{expected}</artifactId>"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(pom_path),
            )
        ]

    return []


def check_java_release(
    root_elem: ET.Element,
    pom_path: Path,
) -> list[Diagnostic]:
    """Check Java version floor: maven.compiler.release in <properties> is canonical."""
    properties_elem = _find_child(root_elem, "properties")
    if properties_elem is None:
        return [
            Diagnostic(
                check="pom-java-release",
                what=f"<properties> block is missing from {pom_path}.",
                why=(
                    f"Every Java recipe must declare <maven.compiler.release> "
                    f"in <properties> to specify its target Java version "
                    f"(<= {CI_PINNED_JDK_VERSION}, pinned in "
                    f".github/workflows/java-tests.yml)."
                ),
                how=(
                    f"Add a <properties> block to {pom_path.name}:\n"
                    f"  <properties>\n"
                    f"    <maven.compiler.release>{CI_PINNED_JDK_VERSION}</maven.compiler.release>\n"
                    f"  </properties>"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(pom_path),
            )
        ]

    release_elem = _find_child(properties_elem, "maven.compiler.release")
    if release_elem is None or not (release_elem.text or "").strip():
        source_elem = _find_child(properties_elem, "maven.compiler.source")
        target_elem = _find_child(properties_elem, "maven.compiler.target")
        if source_elem is not None or target_elem is not None:
            return [
                Diagnostic(
                    check="pom-java-release",
                    what=(
                        f"{pom_path} declares maven.compiler.source/target "
                        f"without maven.compiler.release in <properties>."
                    ),
                    why=(
                        "maven.compiler.release in <properties> is canonical "
                        "for Java recipes because it compiles against the target "
                        "JDK API, not merely its bytecode level. Setting only "
                        "maven.compiler.source / maven.compiler.target fails."
                    ),
                    how=(
                        f"Replace <maven.compiler.source> and "
                        f"<maven.compiler.target> in <properties> with:\n"
                        f"  <maven.compiler.release>{CI_PINNED_JDK_VERSION}</maven.compiler.release>"
                    ),
                    doc=Doc.PROJECT_NAME,
                    file=str(pom_path),
                )
            ]

        return [
            Diagnostic(
                check="pom-java-release",
                what=(
                    f"<maven.compiler.release> is not declared in <properties> "
                    f"in {pom_path}."
                ),
                why=(
                    f"Every Java recipe must declare <maven.compiler.release> "
                    f"in <properties> to specify its target Java version "
                    f"(<= {CI_PINNED_JDK_VERSION}, pinned in "
                    f".github/workflows/java-tests.yml)."
                ),
                how=(
                    f"Add <maven.compiler.release> to <properties> in "
                    f"{pom_path.name}:\n"
                    f"  <maven.compiler.release>{CI_PINNED_JDK_VERSION}</maven.compiler.release>"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(pom_path),
            )
        ]

    raw_val = (release_elem.text or "").strip()
    try:
        release_ver = int(raw_val)
    except ValueError:
        return [
            Diagnostic(
                check="pom-java-release",
                what=(
                    f"<maven.compiler.release> in {pom_path} ('{raw_val}') is "
                    f"not a valid integer."
                ),
                why=(
                    f"The declared Java release must be an integer version "
                    f"(<= {CI_PINNED_JDK_VERSION})."
                ),
                how=(
                    f"Set <maven.compiler.release> to an integer version like "
                    f"{CI_PINNED_JDK_VERSION}."
                ),
                doc=Doc.PROJECT_NAME,
                file=str(pom_path),
            )
        ]

    if release_ver > CI_PINNED_JDK_VERSION:
        return [
            Diagnostic(
                check="pom-java-release",
                what=(
                    f"Declared maven.compiler.release ({release_ver}) in "
                    f"{pom_path} exceeds CI's pinned JDK version "
                    f"({CI_PINNED_JDK_VERSION})."
                ),
                why=(
                    f"CI pins JDK {CI_PINNED_JDK_VERSION} (in "
                    f".github/workflows/java-tests.yml). A recipe declaring a "
                    f"higher Java release version cannot be built or tested in CI."
                ),
                how=(
                    f"Lower <maven.compiler.release> in {pom_path.name} to "
                    f"{CI_PINNED_JDK_VERSION} or below:\n"
                    f"  <maven.compiler.release>{CI_PINNED_JDK_VERSION}</maven.compiler.release>"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(pom_path),
            )
        ]

    return []


def check_description(
    root_elem: ET.Element,
    pom_path: Path,
    recipe_dir: Path,
) -> list[Diagnostic]:
    """If <description> is declared in pom.xml, check that it matches manifest.yaml."""
    description_elem = _find_child(root_elem, "description")
    if description_elem is None:
        return []

    pom_desc = (description_elem.text or "").strip()
    manifest_path = recipe_dir / "manifest.yaml"
    if not manifest_path.is_file():
        return []

    try:
        manifest_data = yaml.safe_load(
            manifest_path.read_text(encoding="utf-8")
        )
    except Exception:
        return []

    if not isinstance(manifest_data, dict):
        return []

    manifest_desc = manifest_data.get("description")
    if not isinstance(manifest_desc, str):
        return []

    if pom_desc != manifest_desc.strip():
        return [
            Diagnostic(
                check="pom-description",
                what=(
                    f"<description> in {pom_path} ('{pom_desc}') does not "
                    f"match manifest.description ('{manifest_desc.strip()}')."
                ),
                why=(
                    "When <description> is declared in pom.xml, it must match "
                    "manifest.description in manifest.yaml exactly."
                ),
                how=(
                    f"Update <description> in {pom_path.name} to match "
                    f"manifest.yaml:\n"
                    f"  <description>{manifest_desc.strip()}</description>"
                ),
                doc=Doc.PROJECT_DESCRIPTION,
                file=str(pom_path),
            )
        ]

    return []


def _is_maven_central_url(url: str) -> bool:
    """Return True if url points to Maven Central."""
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False

    host = (parsed.hostname or "").lower()
    return host in MAVEN_CENTRAL_HOSTS


def check_repositories(
    root_elem: ET.Element,
    pom_path: Path,
) -> list[Diagnostic]:
    """Check that <repositories>, <pluginRepositories>, and <mirrors> point to Maven Central."""
    diagnostics: list[Diagnostic] = []

    containers = [
        ("repositories", "repository"),
        ("pluginRepositories", "pluginRepository"),
        ("mirrors", "mirror"),
    ]

    for container_name, item_name in containers:
        container_elem = _find_child(root_elem, container_name)
        if container_elem is None:
            continue

        items = _find_all_children(container_elem, item_name)
        for item in items:
            url_elem = _find_child(item, "url")
            if url_elem is None:
                continue

            url = (url_elem.text or "").strip()
            if not url:
                continue

            if not _is_maven_central_url(url):
                diagnostics.append(
                    Diagnostic(
                        check="pom-repositories",
                        what=(
                            f"<{container_name}> in {pom_path} declares a "
                            f"custom or private repository: '{url}'."
                        ),
                        why=(
                            "Java recipes must not declare custom or private "
                            "maven repositories. All dependencies must be "
                            "resolvable from Maven Central."
                        ),
                        how=(
                            f"Remove custom repository '{url}' from "
                            f"{pom_path.name}."
                        ),
                        doc=Doc.PROJECT_NAME,
                        file=str(pom_path),
                    )
                )

    return diagnostics


def _report(diagnostics: list[Diagnostic], recipe_dir: Path) -> int:
    return report(
        diagnostics,
        header=f"{recipe_dir}: pom.xml metadata",
        passed_message=(
            f"{recipe_dir}/pom.xml: artifactId, release version, "
            f"description, and repositories all satisfy the repo rules."
        ),
        next_step=(
            "Update pom.xml to declare matching artifactId, "
            "maven.compiler.release (<= 17), matching description, and "
            "Maven Central repositories."
        ),
    )


def _run(recipe_dir: Path, repo_root: Path | None = None) -> int:
    pom_path = recipe_dir / "pom.xml"
    if not pom_path.is_file():
        print(
            f"[SKIP] {pom_path} does not exist — the required-files "
            f"check reports that separately."
        )
        return EXIT_OK

    try:
        content = pom_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return _report(
            [
                Diagnostic(
                    check="pom-xml-parse",
                    what=f"{pom_path} is not valid UTF-8: {exc}",
                    why=(
                        "The build toolchain requires pom.xml to be valid UTF-8 text."
                    ),
                    how=f"Re-encode {pom_path.name} as UTF-8.",
                    doc=Doc.PROJECT_NAME,
                    file=str(pom_path),
                )
            ],
            recipe_dir,
        )

    try:
        root_elem = ET.fromstring(content)  # noqa: S314
    except ET.ParseError as exc:
        return _report(
            [
                Diagnostic(
                    check="pom-xml-parse",
                    what=f"{pom_path} is not valid XML: {exc}",
                    why="pom.xml must be valid XML for Maven and CI builds.",
                    how=f"Fix XML syntax errors in {pom_path.name}.",
                    doc=Doc.PROJECT_NAME,
                    file=str(pom_path),
                )
            ],
            recipe_dir,
        )

    diagnostics: list[Diagnostic] = []
    diagnostics += check_artifact_id(root_elem, pom_path, recipe_dir, repo_root)
    diagnostics += check_java_release(root_elem, pom_path)
    diagnostics += check_description(root_elem, pom_path, recipe_dir)
    diagnostics += check_repositories(root_elem, pom_path)

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
