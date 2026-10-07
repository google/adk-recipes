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

  - artifactId: <artifactId> MUST equal the recipe folder basename (or
    "<vertical>-<solution>" for plugins).
  - java-release-floor: <maven.compiler.release> in <properties> is canonical.
    A recipe setting only maven.compiler.source / maven.compiler.target fails,
    with a message telling it to use release instead. The declared release
    must be <= the JDK CI pins in .github/workflows/java-tests.yml
    (currently 17). Lower versions (e.g. 11) are permitted. Higher versions
    (e.g. 21) fail because CI cannot compile them.
  - description: <description> MUST equal manifest.description after .strip().
    Optional; skipped if absent from pom.xml.
  - repositories-and-mirrors: If <repositories>, <pluginRepositories>, or
    <mirrors> are declared, all URLs must point to Maven Central
    (https://repo.maven.apache.org/maven2). Custom or private repositories
    are forbidden.

Usage: python check_recipe_pom.py <recipe-dir>

Exit codes:
  0  every rule passed (or pom.xml does not exist, which is owned by separate
     structure validation).
  1  contributor-fixable problems found; every one has been reported with
     a fix, both as a human block and as a ::error annotation.
  2  CI fault — the checker crashed, or its own environment is missing a
     dependency it needs. Never blamed on the contributor's files.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException

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

REPO_ROOT = Path(__file__).resolve().parents[2]

MAVEN_CENTRAL_HOSTS = {
    "repo.maven.apache.org",
    "repo1.maven.org",
    "repo.maven.org",
}


class CheckerFault(Exception):
    """A failure in the checker's own environment, not in the recipe."""


def _repo_relative_parts(
    recipe_dir: Path, repo_root: Path | None = None
) -> tuple[str, ...]:
    """Path segments of `recipe_dir` relative to the repository root.

    An absolute or relative path is resolved against `repo_root` so that
    leading `./` or parent references are normalized before inspection.
    """
    root = (REPO_ROOT if repo_root is None else repo_root).resolve()
    resolved_dir = recipe_dir.resolve()
    try:
        return resolved_dir.relative_to(root).parts
    except ValueError:
        if not recipe_dir.is_absolute():
            try:
                return (root / recipe_dir).resolve().relative_to(root).parts
            except ValueError:
                pass
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


def _local_tag(elem: Element) -> str:
    """Return local tag name stripped of XML namespace."""
    if elem.tag.startswith("{") and "}" in elem.tag:
        return elem.tag.split("}", 1)[1]
    return elem.tag


def _find_child(parent: Element, tag_name: str) -> Element | None:
    """Find first direct child matching tag_name (ignoring XML namespace)."""
    for child in parent:
        if _local_tag(child) == tag_name:
            return child
    return None


def _find_children(parent: Element, tag_name: str) -> list[Element]:
    """Find all direct children matching tag_name (ignoring XML namespace)."""
    return [child for child in parent if _local_tag(child) == tag_name]


def _is_maven_central_url(url: str) -> bool:
    """Check if the given URL points to Maven Central."""
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return False
    try:
        parsed = urlparse(cleaned)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.hostname and parsed.hostname.lower() in MAVEN_CENTRAL_HOSTS:
        return parsed.path.rstrip("/") in ("/maven2", "")
    return False


def check_artifact_id(
    root: Element,
    pom_path: Path,
    recipe_dir: Path,
    repo_root: Path | None = None,
) -> list[Diagnostic]:
    """Check that top-level <artifactId> matches the expected project name."""
    expected = expected_project_name(recipe_dir, repo_root)
    elem = _find_child(root, "artifactId")
    if elem is None or not (elem.text and elem.text.strip()):
        return [
            Diagnostic(
                check="pom-artifact-id",
                what=f"<artifactId> is missing from {pom_path}.",
                why=(
                    f"Every Java recipe must declare an <artifactId> matching "
                    f"its expected recipe name ('{expected}')."
                ),
                how=f"Add to {pom_path.name}:\n  <artifactId>{expected}</artifactId>",
                doc=Doc.PROJECT_NAME,
                file=str(pom_path),
            )
        ]

    declared_id = elem.text.strip()
    if declared_id != expected:
        return [
            Diagnostic(
                check="pom-artifact-id",
                what=(
                    f"<artifactId> = '{declared_id}' in {pom_path}, but "
                    f"this recipe must declare '{expected}'."
                ),
                why=(
                    f"<artifactId> in pom.xml must match the expected "
                    f"recipe name ('{expected}')."
                ),
                how=(
                    f"Update <artifactId> in {pom_path.name}:\n"
                    f"  <artifactId>{expected}</artifactId>"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(pom_path),
            )
        ]

    return []


def check_java_version(
    root: Element,
    pom_path: Path,
) -> list[Diagnostic]:
    """Check that <maven.compiler.release> is declared in <properties> and <= CI pin."""
    props_elem = _find_child(root, "properties")
    if props_elem is None:
        return [
            Diagnostic(
                check="pom-java-release",
                what=f"<maven.compiler.release> in <properties> is missing from {pom_path}.",
                why=(
                    f"Every Java recipe must declare <maven.compiler.release> "
                    f"in <properties> <= {CI_PINNED_JDK_VERSION} (pinned in "
                    f".github/workflows/java-tests.yml)."
                ),
                how=(
                    f"Add <properties> to {pom_path.name}:\n"
                    f"  <properties>\n"
                    f"    <maven.compiler.release>{CI_PINNED_JDK_VERSION}</maven.compiler.release>\n"
                    f"  </properties>"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(pom_path),
            )
        ]

    release_elem = _find_child(props_elem, "maven.compiler.release")
    if (
        release_elem is not None
        and release_elem.text
        and release_elem.text.strip()
    ):
        val_str = release_elem.text.strip()
        try:
            val_int = int(val_str)
        except ValueError:
            return [
                Diagnostic(
                    check="pom-java-release",
                    what=(
                        f"<maven.compiler.release> '{val_str}' in {pom_path} "
                        f"is not a valid integer."
                    ),
                    why=(
                        "maven.compiler.release must be an integer Java "
                        "release version (e.g. 17)."
                    ),
                    how=(
                        f"Set <maven.compiler.release>{CI_PINNED_JDK_VERSION}</maven.compiler.release> "
                        f"in {pom_path.name}."
                    ),
                    doc=Doc.PROJECT_NAME,
                    file=str(pom_path),
                )
            ]

        if val_int > CI_PINNED_JDK_VERSION:
            return [
                Diagnostic(
                    check="pom-java-release",
                    what=(
                        f"Declared Java release ({val_int}) in {pom_path} "
                        f"exceeds CI's pinned JDK version ({CI_PINNED_JDK_VERSION})."
                    ),
                    why=(
                        f"CI pins JDK {CI_PINNED_JDK_VERSION} (in "
                        f".github/workflows/java-tests.yml). A recipe "
                        f"declaring release {val_int} cannot be compiled in CI."
                    ),
                    how=(
                        f"Lower <maven.compiler.release> in {pom_path.name} "
                        f"to {CI_PINNED_JDK_VERSION} or below:\n"
                        f"  <properties>\n"
                        f"    <maven.compiler.release>{CI_PINNED_JDK_VERSION}</maven.compiler.release>\n"
                        f"  </properties>"
                    ),
                    doc=Doc.PROJECT_NAME,
                    file=str(pom_path),
                )
            ]
        return []

    source_elem = _find_child(props_elem, "maven.compiler.source")
    target_elem = _find_child(props_elem, "maven.compiler.target")
    if (
        source_elem is not None
        and source_elem.text
        and source_elem.text.strip()
    ) or (
        target_elem is not None
        and target_elem.text
        and target_elem.text.strip()
    ):
        return [
            Diagnostic(
                check="pom-java-release",
                what=(
                    f"<maven.compiler.source> / <maven.compiler.target> declared "
                    f"without <maven.compiler.release> in {pom_path}."
                ),
                why=(
                    "Java recipes must use `maven.compiler.release` in <properties> "
                    "as the canonical version floor instead of source/target. "
                    "`release` compiles against that JDK's API, not merely its "
                    "bytecode level."
                ),
                how=(
                    f"Replace <maven.compiler.source> and <maven.compiler.target> "
                    f"with <maven.compiler.release> in {pom_path.name}:\n"
                    f"  <properties>\n"
                    f"    <maven.compiler.release>{CI_PINNED_JDK_VERSION}</maven.compiler.release>\n"
                    f"  </properties>"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(pom_path),
            )
        ]

    return [
        Diagnostic(
            check="pom-java-release",
            what=f"<maven.compiler.release> is not declared in {pom_path}.",
            why=(
                f"Every Java recipe must declare <maven.compiler.release> "
                f"in <properties> <= {CI_PINNED_JDK_VERSION} (pinned in "
                f".github/workflows/java-tests.yml)."
            ),
            how=(
                f"Add <maven.compiler.release> to <properties> in {pom_path.name}:\n"
                f"  <properties>\n"
                f"    <maven.compiler.release>{CI_PINNED_JDK_VERSION}</maven.compiler.release>\n"
                f"  </properties>"
            ),
            doc=Doc.PROJECT_NAME,
            file=str(pom_path),
        )
    ]


def check_description(
    root: Element,
    pom_path: Path,
    recipe_dir: Path,
) -> list[Diagnostic]:
    """Check that top-level <description> equals manifest.description if present."""
    desc_elem = _find_child(root, "description")
    if desc_elem is None:
        return []

    pom_desc = desc_elem.text.strip() if desc_elem.text else ""
    manifest_path = recipe_dir / "manifest.yaml"
    if not manifest_path.is_file():
        return [
            Diagnostic(
                check="pom-description",
                what=(
                    f"<description> is set in {pom_path}, but "
                    f"there is no {manifest_path} to check it against."
                ),
                why=(
                    "Every recipe ships a manifest.yaml, and its `description` "
                    "is the single source of truth that <description> in "
                    "pom.xml has to match. With no manifest the rule cannot "
                    "be evaluated, and the recipe is missing a required file besides."
                ),
                how=(
                    f"Create {manifest_path} carrying the same description:\n"
                    f"  description: {pom_desc}\n"
                    f"Or delete <description> from {pom_path.name} — it is "
                    "optional, and manifest.yaml is the source of truth."
                ),
                doc=Doc.MANIFEST,
                file=str(pom_path),
            )
        ]

    try:
        import yaml
    except ImportError as exc:
        raise CheckerFault(
            f"pyyaml is not importable ({exc}), so <description> cannot "
            f"be compared with manifest.description."
        ) from exc

    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = yaml.safe_load(f) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        return [
            Diagnostic(
                check="pom-description",
                what=f"{manifest_path} could not be read as YAML: {e}",
                why=(
                    "manifest.yaml is parsed by this check and by "
                    "tools/validate_manifest.py; a file neither can read "
                    "blocks every manifest-derived rule."
                ),
                how=(
                    "Fix the YAML syntax in manifest.yaml, or regenerate the "
                    "file."
                ),
                doc=Doc.MANIFEST,
                file=str(manifest_path),
            )
        ]

    if not isinstance(manifest, dict):
        return [
            Diagnostic(
                check="pom-description",
                what=(
                    f"{manifest_path} is valid YAML but its top level is "
                    f"not a mapping."
                ),
                why=(
                    "manifest.yaml must be a mapping with top-level keys "
                    "(name, description, language, ...)."
                ),
                how=(
                    f"Rewrite manifest.yaml as `key: value` pairs, for example:\n"
                    f"  description: {pom_desc}"
                ),
                doc=Doc.MANIFEST,
                file=str(manifest_path),
            )
        ]

    manifest_desc = str(manifest.get("description") or "").strip()
    if pom_desc != manifest_desc:
        return [
            Diagnostic(
                check="pom-description",
                what=(
                    f"<description> in {pom_path} does not match manifest.description.\n"
                    f"  pom.xml:       {pom_desc!r}\n"
                    f"  manifest.yaml: {manifest_desc!r}"
                ),
                why=(
                    "The two files describe the same recipe in two places; "
                    "when they disagree, the description shown in the catalogue "
                    "depends on which file the reader happens to open."
                ),
                how=(
                    "Update whichever is out of date so both read the same, or "
                    f"delete <description> from {pom_path.name} — it is optional, "
                    "and manifest.yaml is the source of truth."
                ),
                doc=Doc.PROJECT_DESCRIPTION,
                file=str(pom_path),
            )
        ]

    return []


def _check_repo_entries(
    root: Element,
    container_tag: str,
    entry_tag: str,
    label: str,
    pom_path: Path,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for container in _find_children(root, container_tag):
        for entry in _find_children(container, entry_tag):
            id_elem = _find_child(entry, "id")
            entry_id = (
                id_elem.text.strip()
                if id_elem is not None and id_elem.text
                else "unknown"
            )
            url_elem = _find_child(entry, "url")
            url_text = (
                url_elem.text.strip()
                if url_elem is not None and url_elem.text
                else ""
            )

            if not _is_maven_central_url(url_text):
                diagnostics.append(
                    Diagnostic(
                        check="pom-repositories",
                        what=(
                            f"{label} '{entry_id}' with URL '{url_text}' in "
                            f"{pom_path} does not point to Maven Central."
                        ),
                        why=(
                            f"Java recipes must not declare custom or private "
                            f"Maven {label.lower()}s. All public dependencies "
                            f"must resolve from Maven Central "
                            f"(https://repo.maven.apache.org/maven2)."
                        ),
                        how=(
                            f"Remove custom {label.lower()} declarations from "
                            f"{pom_path.name} or use Maven Central "
                            f"(https://repo.maven.apache.org/maven2)."
                        ),
                        doc=Doc.PROJECT_NAME,
                        file=str(pom_path),
                    )
                )
    return diagnostics


def check_repositories_and_mirrors(
    root: Element,
    pom_path: Path,
) -> list[Diagnostic]:
    """Check that declared repositories, pluginRepositories, and mirrors point to Maven Central."""
    diagnostics: list[Diagnostic] = []
    diagnostics += _check_repo_entries(
        root, "repositories", "repository", "Repository", pom_path
    )
    diagnostics += _check_repo_entries(
        root,
        "pluginRepositories",
        "pluginRepository",
        "Plugin repository",
        pom_path,
    )
    diagnostics += _check_repo_entries(
        root, "mirrors", "mirror", "Mirror", pom_path
    )
    return diagnostics


def _report(diagnostics: list[Diagnostic], recipe_dir: Path) -> int:
    return report(
        diagnostics,
        header=f"{recipe_dir}: pom.xml metadata",
        passed_message=(
            f"{recipe_dir}/pom.xml: artifactId, compiler release, description "
            f"and repositories all satisfy the repo rules."
        ),
        next_step=(
            "Update pom.xml to declare matching <artifactId>, valid "
            "<maven.compiler.release> in <properties>, matching <description>, "
            "and Maven Central repositories."
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
                    check="pom-parse",
                    what=f"{pom_path} is not valid UTF-8: {exc}",
                    why=(
                        "The build toolchain requires pom.xml to be "
                        "valid UTF-8 text."
                    ),
                    how=f"Re-encode {pom_path.name} as UTF-8.",
                    doc=Doc.PROJECT_NAME,
                    file=str(pom_path),
                )
            ],
            recipe_dir,
        )

    try:
        root = ET.fromstring(content)
    except (ET.ParseError, DefusedXmlException) as exc:
        return _report(
            [
                Diagnostic(
                    check="pom-parse",
                    what=f"{pom_path} is not valid XML: {exc}",
                    why="The build toolchain requires pom.xml to be valid XML.",
                    how=f"Fix XML syntax errors in {pom_path.name}.",
                    doc=Doc.PROJECT_NAME,
                    file=str(pom_path),
                )
            ],
            recipe_dir,
        )

    diagnostics: list[Diagnostic] = []
    diagnostics += check_artifact_id(root, pom_path, recipe_dir, repo_root)
    diagnostics += check_java_version(root, pom_path)
    diagnostics += check_description(root, pom_path, recipe_dir)
    diagnostics += check_repositories_and_mirrors(root, pom_path)

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
