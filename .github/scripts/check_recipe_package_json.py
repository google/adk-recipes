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
"""Validates a TypeScript recipe's package.json against metadata rules.

Rules enforced:
  - name: MUST equal the recipe folder basename exactly. Suffixes (like -ts)
    are forbidden.
  - description: If present, MUST equal manifest.description exactly after
    .strip(). If manifest.yaml is missing while package.json specifies a
    description, reports an error. Optional in package.json (skipped if absent).
  - engines.node: MUST be declared and MUST accept Node 22 (pinned in CI
    workflow .github/workflows/typescript-tests.yml).
  - .npmrc: If .npmrc exists, any registry= directive must point at the public
    npm registry (https://registry.npmjs.org/).
  - tsconfig.json is optional; skipped silently if absent.

Usage: python3 check_recipe_package_json.py <recipe-dir>

Exit codes:
  0  every rule passed (or package.json does not exist, which is owned by
     structure validation).
  1  contributor-fixable problems found; every one has been reported with
     a fix, both as a human block and as a ::error annotation.
  2  CI fault — the checker crashed or its environment is missing a dependency.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ci_message import (
    Diagnostic,
    Doc,
    guard,
    infra_fault,
    report,
    report_infra_fault,
)

CHECKER = "check_recipe_package_json.py"
CI_PINNED_NODE_VERSION = 22
ALLOWED_NPM_REGISTRY_HOSTS = frozenset({"registry.npmjs.org"})

_SEMVER_PART_RE = re.compile(
    r"^([<>=~^]*)\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-[\w.-]+)?(?:\+[\w.-]+)?$"
)


def _parse_semver_part(
    part: str,
) -> tuple[str, int, int | None, int | None] | None:
    part = part.strip()
    if part in ("*", "x", "X", "latest", ""):
        return ("*", 0, None, None)
    m = _SEMVER_PART_RE.match(part)
    if not m:
        return None
    op, major, minor, patch = m.groups()
    return (
        op or "=",
        int(major),
        int(minor) if minor is not None else None,
        int(patch) if patch is not None else None,
    )


def _eval_single_condition(
    cond: str, target: tuple[int, int, int] = (CI_PINNED_NODE_VERSION, 0, 0)
) -> bool | None:
    cond = cond.strip()
    if not cond or cond in ("*", "x", "X", "latest"):
        return True

    # Wildcards like 22.x or 22.* or 20.x
    if re.match(r"^v?(\d+)\.[xX*](?:\.[xX*])?$", cond):
        m = re.match(r"^v?(\d+)", cond)
        return int(m.group(1)) == target[0] if m else False

    if re.match(r"^v?(\d+)\.(\d+)\.[xX*]$", cond):
        m = re.match(r"^v?(\d+)\.(\d+)", cond)
        if m:
            return int(m.group(1)) == target[0] and int(m.group(2)) == target[1]
        return False

    parsed = _parse_semver_part(cond)
    if not parsed:
        return None

    op, major, minor, patch = parsed
    v_minor = minor if minor is not None else 0
    v_patch = patch if patch is not None else 0
    version_tuple = (major, v_minor, v_patch)

    if op == "*":
        return True
    if op in ("=", "=="):
        if minor is None:
            return target[0] == major
        if patch is None:
            return target[0] == major and target[1] == minor
        return target == version_tuple
    if op == ">=":
        return target >= version_tuple
    if op == ">":
        return target > version_tuple
    if op == "<=":
        return target <= version_tuple
    if op == "<":
        return target < version_tuple
    if op == "^":
        if major > 0:
            return target[0] == major and target >= version_tuple
        if v_minor > 0:
            return (
                target[0] == 0
                and target[1] == v_minor
                and target >= version_tuple
            )
        return target == version_tuple
    if op == "~":
        if minor is not None:
            return (
                target[0] == major
                and target[1] == minor
                and target >= version_tuple
            )
        return target[0] == major and target >= version_tuple

    return None


def node_engine_accepts_node_22(engine_str: str) -> bool:
    """Evaluate whether an npm engines.node range string accepts Node 22."""
    cleaned = engine_str.strip()
    if not cleaned or cleaned in ("*", "x", "X", "latest"):
        return True

    # Split on logical OR: ||
    or_clauses = [c.strip() for c in cleaned.split("||") if c.strip()]
    if not or_clauses:
        return True

    for clause in or_clauses:
        # Split on whitespace for logical AND
        and_conditions = clause.split()
        clause_ok = True
        for cond in and_conditions:
            res = _eval_single_condition(
                cond, target=(CI_PINNED_NODE_VERSION, 0, 0)
            )
            if res is False:
                clause_ok = False
                break
            if res is None:
                # If unrecognized operator, fail safe
                clause_ok = False
                break
        if clause_ok:
            return True

    return False


def _check_name(
    package_json: dict,
    expected_name: str,
    package_path: Path,
) -> list[Diagnostic]:
    raw_name = package_json.get("name")
    if not raw_name or not isinstance(raw_name, str):
        return [
            Diagnostic(
                check="package-json-name",
                what=f"{package_path} is missing a `name` field.",
                why=(
                    "Every TypeScript recipe's package.json must declare a `name` "
                    "matching the recipe folder basename."
                ),
                how=(f'Add `"name": "{expected_name}"` to {package_path}.'),
                doc=Doc.PROJECT_NAME,
                file=str(package_path),
            )
        ]

    name = raw_name.strip()
    if name != expected_name:
        return [
            Diagnostic(
                check="package-json-name",
                what=(
                    f"package.json `name` ({name!r}) does not match the "
                    f"recipe folder name ({expected_name!r})."
                ),
                why=(
                    "The recipe directory name and package.json `name` must "
                    "be identical so package identifiers remain canonical across "
                    "the catalogue. Suffixes (like `-ts`) are not permitted."
                ),
                how=(f'Set `"name": "{expected_name}"` in {package_path}.'),
                doc=Doc.PROJECT_NAME,
                file=str(package_path),
            )
        ]
    return []


def _check_description(
    package_json: dict,
    package_path: Path,
    recipe_dir: Path,
) -> list[Diagnostic]:
    description = package_json.get("description")
    if description is None:
        return []

    pkg_desc = str(description).strip()
    manifest_path = recipe_dir / "manifest.yaml"

    if not manifest_path.is_file():
        return [
            Diagnostic(
                check="description-matches-manifest",
                what=(
                    f"`description` is set in {package_path}, but "
                    f"there is no {manifest_path} to check it against."
                ),
                why=(
                    "Every recipe ships a manifest.yaml, and its "
                    "`description` is the single source of truth that "
                    "package.json `description` has to match. With no manifest "
                    "the rule cannot be evaluated."
                ),
                how=(
                    f"Create {manifest_path} carrying the same description:\n"
                    f"  description: {pkg_desc}\n"
                    f"If the recipe does not need a description in package.json, "
                    f"delete the field — it is optional."
                ),
                doc=Doc.MANIFEST,
                file=str(package_path),
            )
        ]

    try:
        import yaml
    except ImportError as exc:
        raise infra_fault(
            CHECKER,
            f"pyyaml is not importable ({exc}), so package.json description "
            f"cannot be compared with manifest.description.",
        ) from exc

    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = yaml.safe_load(f) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        return [
            Diagnostic(
                check="description-matches-manifest",
                what=f"{manifest_path} could not be read as YAML: {e}",
                why=(
                    "manifest.yaml is parsed by this check and by "
                    "tools/validate_manifest.py; a file neither can read "
                    "blocks manifest-derived rules."
                ),
                how="Fix the YAML syntax in manifest.yaml.",
                doc=Doc.MANIFEST,
                file=str(manifest_path),
            )
        ]

    if not isinstance(manifest, dict):
        return [
            Diagnostic(
                check="description-matches-manifest",
                what=(
                    f"{manifest_path} is valid YAML but its top level is not "
                    f"a mapping of fields."
                ),
                why="manifest.yaml must be a mapping with top-level keys.",
                how="Rewrite manifest.yaml as `key: value` pairs.",
                doc=Doc.MANIFEST,
                file=str(manifest_path),
            )
        ]

    mf_desc = str(manifest.get("description") or "").strip()
    if pkg_desc != mf_desc:
        return [
            Diagnostic(
                check="description-matches-manifest",
                what=(
                    f"package.json `description` does not match manifest.description.\n"
                    f"  package.json:  {pkg_desc!r}\n"
                    f"  manifest.yaml: {mf_desc!r}"
                ),
                why=(
                    "The two files describe the same recipe in two places; when "
                    "they disagree, the description shown in the catalogue "
                    "depends on which file the reader opens."
                ),
                how=(
                    "Update package.json or manifest.yaml so both read identically, "
                    "or delete `description` from package.json — it is optional."
                ),
                doc=Doc.PROJECT_DESCRIPTION,
                file=str(package_path),
            )
        ]
    return []


def _check_engines(
    package_json: dict,
    package_path: Path,
) -> list[Diagnostic]:
    engines = package_json.get("engines")
    if not isinstance(engines, dict) or "node" not in engines:
        return [
            Diagnostic(
                check="package-json-engines-node",
                what=(f"{package_path} does not declare `engines.node`."),
                why=(
                    f"TypeScript recipes must explicitly declare an `engines.node` "
                    f"range in package.json that accepts the CI pinned Node "
                    f"version (Node {CI_PINNED_NODE_VERSION})."
                ),
                how=(
                    f'Add `"engines": {{ "node": ">={CI_PINNED_NODE_VERSION}" }}` '
                    f"to {package_path}."
                ),
                doc=Doc.PROJECT_NAME,
                file=str(package_path),
            )
        ]

    node_engine = str(engines["node"]).strip()
    if not node_engine_accepts_node_22(node_engine):
        return [
            Diagnostic(
                check="package-json-engines-node",
                what=(
                    f"{package_path} declares `engines.node`: {node_engine!r}, "
                    f"which does not accept CI's Node {CI_PINNED_NODE_VERSION} runtime."
                ),
                why=(
                    f"CI builds and tests TypeScript recipes using Node "
                    f"{CI_PINNED_NODE_VERSION}. If `engines.node` excludes Node "
                    f"{CI_PINNED_NODE_VERSION}, CI cannot execute the recipe."
                ),
                how=(
                    f"Update `engines.node` in {package_path} to accept Node "
                    f'{CI_PINNED_NODE_VERSION}, e.g. `">={CI_PINNED_NODE_VERSION}"`.'
                ),
                doc=Doc.PROJECT_NAME,
                file=str(package_path),
            )
        ]
    return []


def _check_npmrc(recipe_dir: Path) -> list[Diagnostic]:
    npmrc_path = recipe_dir / ".npmrc"
    if not npmrc_path.is_file():
        return []

    try:
        content = npmrc_path.read_text(encoding="utf-8")
    except Exception as exc:
        return [
            Diagnostic(
                check="npmrc-public-registry",
                what=f"{npmrc_path} could not be read: {exc}",
                why="The .npmrc configuration file must be valid UTF-8 text.",
                how=f"Fix encoding or permissions for {npmrc_path}.",
                doc=Doc.PYPI_INDEX,
                file=str(npmrc_path),
            )
        ]

    diagnostics: list[Diagnostic] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue

        match = re.match(r"^(?:@[\w-]+:)?registry\s*=\s*(.+)$", stripped)
        if match:
            registry_url = match.group(1).strip().strip("\"'")
            parsed = urlparse(registry_url)
            if (
                parsed.scheme not in ("http", "https")
                or parsed.netloc.lower() not in ALLOWED_NPM_REGISTRY_HOSTS
            ):
                diagnostics.append(
                    Diagnostic(
                        check="npmrc-public-registry",
                        what=(
                            f"{npmrc_path}:{lineno} configures a non-public npm registry: "
                            f"{registry_url!r}"
                        ),
                        why=(
                            "Recipe dependencies must resolve from the public npm "
                            "registry (https://registry.npmjs.org/) to maintain supply-chain "
                            "security and hermetic builds."
                        ),
                        how=(
                            "Point the registry configuration to https://registry.npmjs.org/ "
                            "or remove .npmrc if default npm resolution is sufficient."
                        ),
                        doc=Doc.PYPI_INDEX,
                        file=str(npmrc_path),
                    )
                )

    return diagnostics


def validate_package_json(
    recipe_dir: Path,
) -> list[Diagnostic]:
    package_path = recipe_dir / "package.json"
    if not package_path.is_file():
        return []

    try:
        content = package_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [
            Diagnostic(
                check="package-json-readable",
                what=f"{package_path} is not valid UTF-8: {exc}",
                why="package.json must be encoded in UTF-8.",
                how=f"Re-save {package_path} using UTF-8 encoding.",
                doc=Doc.PROJECT_NAME,
                file=str(package_path),
            )
        ]

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return [
            Diagnostic(
                check="package-json-valid-json",
                what=f"{package_path}:{exc.lineno}:{exc.colno} is not valid JSON: {exc.msg}",
                why="package.json must be valid JSON.",
                how=f"Fix JSON syntax in {package_path}.",
                doc=Doc.PROJECT_NAME,
                file=str(package_path),
            )
        ]

    if not isinstance(data, dict):
        return [
            Diagnostic(
                check="package-json-valid-json",
                what=f"{package_path} top level must be a JSON object.",
                why="package.json must be a JSON object containing package fields.",
                how=f"Wrap top-level contents in {package_path} with {{}}.",
                doc=Doc.PROJECT_NAME,
                file=str(package_path),
            )
        ]

    expected_name = recipe_dir.name
    diagnostics: list[Diagnostic] = []
    diagnostics.extend(_check_name(data, expected_name, package_path))
    diagnostics.extend(_check_description(data, package_path, recipe_dir))
    diagnostics.extend(_check_engines(data, package_path))
    diagnostics.extend(_check_npmrc(recipe_dir))

    return diagnostics


def main() -> int:
    if len(sys.argv) != 2:
        return report_infra_fault(
            infra_fault(
                CHECKER,
                f"invoked with {len(sys.argv) - 1} argument(s); expected "
                f"exactly one recipe directory.",
            )
        )

    raw_path = Path(sys.argv[1])
    recipe_dir = (
        raw_path.parent if raw_path.name == "package.json" else raw_path
    )
    if not recipe_dir.is_dir():
        return report_infra_fault(
            infra_fault(
                CHECKER,
                f"{recipe_dir} is not a directory. The path came from the "
                f"workflow's recipe discovery step, not from the "
                f"contributor.",
            )
        )

    diagnostics = validate_package_json(recipe_dir)
    return report(
        diagnostics,
        header=f"{recipe_dir}: package.json metadata",
        passed_message=(
            f"{recipe_dir}/package.json: name, description, engines.node, "
            f"and .npmrc registry satisfy the repo rules."
        ),
        next_step=(
            "Update package.json with matching name, description, and "
            f"engines.node (>= {CI_PINNED_NODE_VERSION})."
        ),
    )


if __name__ == "__main__":
    sys.exit(guard(CHECKER, main))
