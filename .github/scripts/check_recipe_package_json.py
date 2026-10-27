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
"""Validates a TypeScript recipe's package.json against repository metadata rules.

Rules enforced:

  - package-name: `name` in package.json MUST equal the recipe's expected name:
      * core/ and contrib/ — the folder basename (e.g.
        contrib/typescript/financial-advisor -> "financial-advisor").
      * plugins/ — "<vertical>-<solution>" (e.g.
        plugins/retail/store-ops -> "retail-store-ops").
  - description: `description` in package.json MUST equal `manifest.description`
    from the recipe's manifest.yaml (after .strip(), exact match). Optional;
    skipped when absent from package.json. If present but manifest.yaml is
    missing or unparseable, an error is reported.
  - engines-node: `engines.node` must be declared and must accept the Node 22
    CI pin (from .github/workflows/typescript-tests.yml).
  - npmrc-registry: If `.npmrc` exists, any `registry=` or `@<scope>:registry=`
    directive must point to the public npm registry (https://registry.npmjs.org/).
  - tsconfig: `tsconfig.json` is optional. If present, it must be valid JSON/JSONC.
    If absent, skipped silently.
  - Missing `.env.example` is not checked here (handled by structure validation).

Usage: python check_recipe_package_json.py <recipe-dir>

Exit codes:
  0  every rule passed (or package.json does not exist, which is owned by
     separate structure validation).
  1  contributor-fixable problems found; every one has been reported with
     a fix, both as a human block and as a ::error annotation.
  2  CI fault — the checker crashed, or its own environment is missing a
     dependency it needs. Never blamed on the contributor's files.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

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

CHECKER = "check_recipe_package_json.py"

# Node version pinned in CI (see .github/workflows/typescript-tests.yml).
CI_PINNED_NODE_VERSION = 22

NAMESPACED_ROOTS = {"plugins": "vertical"}

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class CheckerFault(Exception):
    """A failure in the checker's own environment, not in the recipe."""


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
    """Return the package name this recipe directory is required to declare.

    For core/ and contrib/ recipes, returns the folder basename.
    For plugins/<vertical>/<solution>, returns "<vertical>-<solution>".
    """
    parts = _repo_relative_parts(recipe_dir, repo_root)
    if len(parts) == 3 and parts[0] in NAMESPACED_ROOTS:
        return f"{parts[1]}-{parts[2]}"
    return recipe_dir.name


def _describe(value: object) -> str:
    """`list ['a', 'b']` — the type first, because the type is the bug."""
    text = repr(value)
    if len(text) > 60:
        text = text[:57] + "..."
    return f"{type(value).__name__} {text}"


def _is_public_npm_registry(url: str) -> bool:
    """Check if the given URL points to the public npm registry."""
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return False
    if cleaned.startswith("//"):
        cleaned = "https:" + cleaned
    try:
        parsed = urlparse(cleaned)
        return (
            parsed.scheme in ("https", "http")
            and parsed.netloc.lower()
            in ("registry.npmjs.org", "registry.npmjs.com")
            and parsed.path in ("", "/")
        )
    except Exception:
        return False


def _parse_semver_parts(
    v_str: str,
) -> tuple[int | None, int | None, int | None]:
    """Parse major, minor, patch digits from a semver string."""
    v_clean = re.sub(r"^[v=]+", "", v_str.strip())
    v_clean = re.split(r"[-+]", v_clean)[0]
    parts = v_clean.split(".")

    def to_int_or_none(p: str) -> int | None:
        return int(p) if p.isdigit() else None

    major = to_int_or_none(parts[0]) if len(parts) > 0 else None
    minor = to_int_or_none(parts[1]) if len(parts) > 1 else None
    patch = to_int_or_none(parts[2]) if len(parts) > 2 else None
    return major, minor, patch


def _comparator_accepts_node(
    comp: str, node_major: int = CI_PINNED_NODE_VERSION
) -> bool:
    """Check if a single semver comparator accepts the given Node major version."""
    comp = comp.strip()
    if not comp or comp in ("*", "x", "X"):
        return True

    # Hyphen range: A - B
    if " - " in comp:
        low_str, high_str = comp.split(" - ", 1)
        l_maj, _, _ = _parse_semver_parts(low_str)
        h_maj, h_min, h_pat = _parse_semver_parts(high_str)
        if l_maj is None:
            return False
        if l_maj > node_major:
            return False
        if h_maj is not None:
            if h_maj < node_major:
                return False
            if h_maj == node_major and h_min is not None and h_pat is not None:
                # e.g. 20.0.0 - 22.0.0 accepts 22.0.0
                return True
        return True

    m = re.match(r"^(\^|~|>=|<=|>|<|==|=|v)?\s*([v=]?\d.*|\*|x|X)$", comp)
    if not m:
        return False
    op, ver_str = m.groups()
    op = op or ""
    if ver_str in ("*", "x", "X"):
        return True

    maj, minor, _ = _parse_semver_parts(ver_str)
    if maj is None:
        return False

    if op == "^":
        return maj == node_major
    if op == "~":
        return maj == node_major
    if op == ">=":
        return maj <= node_major
    if op == ">":
        if maj < node_major:
            return True
        if maj == node_major:
            # >22 or >22.x means >=23.0.0 (excludes 22); >22.0.0 accepts 22.x
            return minor is not None
        return False
    if op == "<=":
        return maj >= node_major
    if op == "<":
        return maj > node_major
    if op in ("", "=", "==", "v"):
        return maj == node_major
    return False


def accepts_node_version(
    range_str: str, node_major: int = CI_PINNED_NODE_VERSION
) -> bool:
    """Evaluate whether an npm semver range string accepts the target Node major version."""
    if not range_str or not isinstance(range_str, str):
        return False
    raw_branches = range_str.split("||")
    for raw_branch in raw_branches:
        branch = raw_branch.strip()
        if not branch:
            continue
        if " - " in branch:
            comps = [branch]
        else:
            comps = [c for c in re.split(r"[\s,]+", branch) if c]
        if comps and all(
            _comparator_accepts_node(c, node_major) for c in comps
        ):
            return True
    return False


def _skip_whitespace_and_comments(text: str, i: int, n: int) -> int:
    """Skip whitespace and JSONC comments starting from index i."""
    while i < n:
        if text[i] in " \t\r\n":
            i += 1
        elif text[i] == "/" and i + 1 < n and text[i + 1] == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
        elif text[i] == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            break
    return i


def _strip_jsonc(text: str) -> str:
    """Strip comments and trailing commas from JSON with comments (JSONC)."""
    result: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    string_char = ""
    escape = False

    while i < n:
        c = text[i]
        if in_string:
            result.append(c)
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == string_char:
                in_string = False
            i += 1
        elif c in ('"', "'"):
            in_string = True
            string_char = c
            result.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] in ("/", "*"):
            i = _skip_whitespace_and_comments(text, i, n)
        elif c == ",":
            next_idx = _skip_whitespace_and_comments(text, i + 1, n)
            if next_idx >= n or text[next_idx] not in ("}", "]"):
                result.append(c)
            i += 1
        else:
            result.append(c)
            i += 1

    return "".join(result)


# ---------------------------------------------------------------------------
# Individual check rules
# ---------------------------------------------------------------------------


def check_name(
    package_json: dict, package_json_path: Path, recipe_dir: Path
) -> list[Diagnostic]:
    """Check that package.json "name" equals expected_project_name(recipe_dir)."""
    expected = expected_project_name(recipe_dir)
    parts = _repo_relative_parts(recipe_dir)
    if (
        expected != recipe_dir.name
        and len(parts) == 3
        and parts[0] in NAMESPACED_ROOTS
    ):
        root = parts[0]
        term = NAMESPACED_ROOTS[root]
        derivation = (
            f"Recipes under {root}/ are named '<{term}>-<solution>' — the "
            f"{term} '{parts[1]}' joined to the folder name "
            f"'{parts[2]}' — because a solution basename is not "
            f"unique across {term}s."
        )
    else:
        derivation = (
            f"A recipe's package name must equal its folder name, "
            f"'{recipe_dir.name}'."
        )

    how = f'Set "name" in {package_json_path.name}:\n  "name": "{expected}"'
    name = package_json.get("name")
    if not name or not isinstance(name, str):
        return [
            Diagnostic(
                check="package-name",
                what=f"`name` is missing from {package_json_path}.",
                why=f"It must be '{expected}'. {derivation}",
                how=how,
                doc=Doc.PROJECT_NAME,
                file=str(package_json_path),
            )
        ]

    if name != expected:
        return [
            Diagnostic(
                check="package-name",
                what=(
                    f"`name` = '{name}', but this recipe must "
                    f"declare '{expected}'."
                ),
                why=derivation,
                how=how,
                doc=Doc.PROJECT_NAME,
                file=str(package_json_path),
            )
        ]
    return []


def check_description(
    package_json: dict, package_json_path: Path, manifest_path: Path
) -> list[Diagnostic]:
    """Check that package.json "description" equals manifest.description if present."""
    description = package_json.get("description")
    if description is None:
        return []
    pkg_desc = str(description).strip()

    if not manifest_path.is_file():
        return [
            Diagnostic(
                check="description-matches-manifest",
                what=(
                    f"`description` is set in {package_json_path}, but "
                    f"there is no {manifest_path} to check it against."
                ),
                why=(
                    "Every recipe ships a manifest.yaml, and its "
                    "`description` is the single source of truth that "
                    "package.json description has to match. With no manifest "
                    "the rule cannot be evaluated, and the recipe is "
                    "missing a required file besides."
                ),
                how=(
                    f"Create {manifest_path} carrying the same description:\n"
                    f"  description: {pkg_desc}\n"
                    f"The generate-manifest AI skill writes the whole file "
                    f"from the recipe. If the recipe genuinely has no "
                    f"manifest, delete `description` from package.json instead — the "
                    f"field is optional."
                ),
                doc=Doc.MANIFEST,
                file=str(package_json_path),
            )
        ]

    try:
        import yaml
    except ImportError as exc:
        raise CheckerFault(
            f"pyyaml is not importable ({exc}), so package.json description "
            f"cannot be compared with manifest.description."
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
                    "blocks every manifest-derived rule."
                ),
                how=(
                    "Fix the YAML (indentation and unquoted colons are the "
                    "usual culprits), or regenerate the file with the "
                    "generate-manifest AI skill."
                ),
                doc=Doc.MANIFEST,
                file=str(manifest_path),
            )
        ]

    if not isinstance(manifest, dict):
        return [
            Diagnostic(
                check="description-matches-manifest",
                what=(
                    f"{manifest_path} is valid YAML but its top level is a "
                    f"{_describe(manifest)}, not a mapping of fields."
                ),
                why=(
                    "manifest.yaml must be a mapping with top-level keys "
                    "(name, description, language, …). A document that "
                    "starts with `- ` is a list, so it has no `description` "
                    "field for package.json description to match."
                ),
                how=(
                    "Rewrite manifest.yaml as `key: value` pairs, for "
                    "example:\n"
                    f"  description: {pkg_desc}\n"
                    "The generate-manifest AI skill writes a correctly "
                    "shaped file from the recipe."
                ),
                doc=Doc.MANIFEST,
                file=str(manifest_path),
            )
        ]

    mf_desc = str(manifest.get("description") or "").strip()
    if pkg_desc == mf_desc:
        return []
    return [
        Diagnostic(
            check="description-matches-manifest",
            what=(
                f"package.json `description` does not match "
                f"manifest.description.\n"
                f"  package.json:  {pkg_desc!r}\n"
                f"  manifest.yaml: {mf_desc!r}"
            ),
            why=(
                "The two files describe the same recipe in two places; when "
                "they disagree, the description shown in the catalogue "
                "depends on which file the reader happens to open."
            ),
            how=(
                "Update whichever is out of date so both read the same, or "
                "delete `description` from package.json — it is "
                "optional, and manifest.yaml is the source of truth."
            ),
            doc=Doc.PROJECT_DESCRIPTION,
            file=str(package_json_path),
        )
    ]


def check_engines_node(
    package_json: dict, package_json_path: Path
) -> list[Diagnostic]:
    """Check that engines.node is declared and accepts the Node 22 CI pin."""
    engines = package_json.get("engines")
    if engines is None:
        return [
            Diagnostic(
                check="engines-node",
                what=f"`engines.node` is missing from {package_json_path}.",
                why=(
                    f"CI pins Node {CI_PINNED_NODE_VERSION} to build and test "
                    f"TypeScript recipes (see .github/workflows/typescript-tests.yml). "
                    f"Declaring engines.node ensures the recipe specifies compatibility "
                    f"with the CI runtime."
                ),
                how=(
                    f"Add to {package_json_path.name}:\n"
                    f'  "engines": {{\n'
                    f'    "node": ">={CI_PINNED_NODE_VERSION}"\n'
                    f"  }}"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(package_json_path),
            )
        ]

    if not isinstance(engines, dict):
        return [
            Diagnostic(
                check="engines-node",
                what=(
                    f"`engines` in {package_json_path} is a "
                    f"{_describe(engines)}, but package.json requires an object."
                ),
                why=(
                    "The `engines` field in package.json must be an object "
                    "mapping engine names (e.g. 'node') to semver version ranges."
                ),
                how=(
                    f"Set engines in {package_json_path.name}:\n"
                    f'  "engines": {{\n'
                    f'    "node": ">={CI_PINNED_NODE_VERSION}"\n'
                    f"  }}"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(package_json_path),
            )
        ]

    node_val = engines.get("node")
    if node_val is None:
        return [
            Diagnostic(
                check="engines-node",
                what=f"`engines.node` is missing from {package_json_path}.",
                why=(
                    f"CI pins Node {CI_PINNED_NODE_VERSION} to build and test "
                    f"TypeScript recipes (see .github/workflows/typescript-tests.yml). "
                    f"Declaring engines.node ensures the recipe specifies compatibility "
                    f"with the CI runtime."
                ),
                how=(
                    f"Set engines.node in {package_json_path.name}:\n"
                    f'  "engines": {{\n'
                    f'    "node": ">={CI_PINNED_NODE_VERSION}"\n'
                    f"  }}"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(package_json_path),
            )
        ]

    if not isinstance(node_val, str) or not node_val.strip():
        return [
            Diagnostic(
                check="engines-node",
                what=(
                    f"`engines.node` in {package_json_path} is "
                    f"{_describe(node_val)}, expected a semver range string."
                ),
                why=(
                    "engines.node must be a semver version range string (e.g. '>=22')."
                ),
                how=(
                    f"Set engines.node in {package_json_path.name}:\n"
                    f'  "engines": {{\n'
                    f'    "node": ">={CI_PINNED_NODE_VERSION}"\n'
                    f"  }}"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(package_json_path),
            )
        ]

    node_str = node_val.strip()
    if not accepts_node_version(node_str, CI_PINNED_NODE_VERSION):
        return [
            Diagnostic(
                check="engines-node",
                what=(
                    f"`engines.node` = '{node_str}' in {package_json_path} "
                    f"does not accept Node {CI_PINNED_NODE_VERSION}."
                ),
                why=(
                    f"CI pins Node {CI_PINNED_NODE_VERSION} to build and test "
                    f"TypeScript recipes (see .github/workflows/typescript-tests.yml). "
                    f"A recipe declaring an incompatible engines.node cannot run in CI."
                ),
                how=(
                    f"Update engines.node in {package_json_path.name} to accept "
                    f"Node {CI_PINNED_NODE_VERSION} (for example, '>={CI_PINNED_NODE_VERSION}'):\n"
                    f'  "engines": {{\n'
                    f'    "node": ">={CI_PINNED_NODE_VERSION}"\n'
                    f"  }}"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(package_json_path),
            )
        ]

    return []


def check_npmrc(recipe_dir: Path) -> list[Diagnostic]:
    """Check that .npmrc (if present) points registry lines to the public npm registry."""
    npmrc_path = recipe_dir / ".npmrc"
    if not npmrc_path.is_file():
        return []

    try:
        content = npmrc_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        return [
            Diagnostic(
                check="npmrc-registry",
                what=f"{npmrc_path} is not valid UTF-8: {e}",
                why="The npm toolchain and this check require .npmrc to be valid UTF-8 text.",
                how=f"Re-encode {npmrc_path.name} as UTF-8.",
                doc=Doc.PROJECT_NAME,
                file=str(npmrc_path),
            )
        ]

    diagnostics: list[Diagnostic] = []
    for line_num, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue

        if "=" not in line:
            continue

        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")

        if key == "registry" or key.endswith(":registry"):
            if not _is_public_npm_registry(val):
                diagnostics.append(
                    Diagnostic(
                        check="npmrc-registry",
                        what=(
                            f"{npmrc_path}:{line_num} configures a non-public "
                            f"npm registry: '{val}'."
                        ),
                        why=(
                            "Every recipe must use the public npm registry "
                            "(https://registry.npmjs.org/) so dependencies "
                            "resolve identically on all machines without "
                            "private registry authentication."
                        ),
                        how=(
                            f"Point {key} to the public npm registry in {npmrc_path.name}:\n"
                            f"  {key}=https://registry.npmjs.org/"
                        ),
                        doc=Doc.PYPI_INDEX,
                        file=str(npmrc_path),
                    )
                )

    return diagnostics


def check_tsconfig(recipe_dir: Path) -> list[Diagnostic]:
    """Check that tsconfig.json (if present) is valid JSON/JSONC."""
    tsconfig_path = recipe_dir / "tsconfig.json"
    if not tsconfig_path.is_file():
        return []

    try:
        content = tsconfig_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        return [
            Diagnostic(
                check="tsconfig-parse",
                what=f"{tsconfig_path} is not valid UTF-8: {e}",
                why="TypeScript compiler requires tsconfig.json to be valid UTF-8 text.",
                how=f"Re-encode {tsconfig_path.name} as UTF-8.",
                doc=Doc.PROJECT_NAME,
                file=str(tsconfig_path),
            )
        ]

    cleaned = _strip_jsonc(content)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return [
            Diagnostic(
                check="tsconfig-parse",
                what=f"{tsconfig_path} could not be parsed as JSON: {e}",
                why="TypeScript compiler requires tsconfig.json to be valid JSON.",
                how=f"Fix JSON syntax errors in {tsconfig_path.name}.",
                doc=Doc.PROJECT_NAME,
                file=str(tsconfig_path),
            )
        ]

    if not isinstance(parsed, dict):
        return [
            Diagnostic(
                check="tsconfig-parse",
                what=(
                    f"{tsconfig_path} is valid JSON but its top level is a "
                    f"{_describe(parsed)}, not a JSON object."
                ),
                why="tsconfig.json must be a JSON object mapping compiler options.",
                how=f"Rewrite {tsconfig_path.name} as a JSON object ({{ ... }}).",
                doc=Doc.PROJECT_NAME,
                file=str(tsconfig_path),
            )
        ]

    return []


# ---------------------------------------------------------------------------
# Runner & CLI
# ---------------------------------------------------------------------------


def _report(diagnostics: list[Diagnostic], recipe_dir: Path) -> int:
    return report(
        diagnostics,
        header=f"{recipe_dir}: package.json metadata",
        passed_message=(
            f"{recipe_dir}/package.json: name, description, engines.node and "
            f"npmrc all satisfy the repo rules."
        ),
        next_step=(
            "Update package.json with the required metadata and ensure "
            "engines.node accepts Node 22."
        ),
    )


def _run(recipe_dir: Path) -> int:
    package_json_path = recipe_dir / "package.json"

    if not package_json_path.is_file():
        print(
            f"[SKIP] {package_json_path} does not exist — the required-files "
            f"check reports that separately."
        )
        return EXIT_OK

    try:
        content = package_json_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        return _report(
            [
                Diagnostic(
                    check="package-json-parse",
                    what=f"{package_json_path} is not valid UTF-8: {e}",
                    why=(
                        "The Node toolchain and this check require package.json "
                        "to be valid UTF-8 text."
                    ),
                    how=f"Re-encode {package_json_path.name} as UTF-8.",
                    doc=Doc.PROJECT_NAME,
                    file=str(package_json_path),
                )
            ],
            recipe_dir,
        )

    try:
        package_json = json.loads(content)
    except json.JSONDecodeError as e:
        return _report(
            [
                Diagnostic(
                    check="package-json-parse",
                    what=f"{package_json_path} is not valid JSON: {e}",
                    why="package.json must be valid JSON for npm and tooling to parse.",
                    how=f"Fix the JSON syntax errors in {package_json_path.name}.",
                    doc=Doc.PROJECT_NAME,
                    file=str(package_json_path),
                )
            ],
            recipe_dir,
        )

    if not isinstance(package_json, dict):
        return _report(
            [
                Diagnostic(
                    check="package-json-parse",
                    what=(
                        f"{package_json_path} is valid JSON but its top level "
                        f"is a {_describe(package_json)}, not a JSON object."
                    ),
                    why="package.json must be a JSON object mapping fields to values.",
                    how=f"Rewrite {package_json_path.name} as a JSON object ({{ ... }}).",
                    doc=Doc.PROJECT_NAME,
                    file=str(package_json_path),
                )
            ],
            recipe_dir,
        )

    manifest_path = recipe_dir / "manifest.yaml"

    diagnostics: list[Diagnostic] = []
    diagnostics += check_name(package_json, package_json_path, recipe_dir)
    diagnostics += check_description(
        package_json, package_json_path, manifest_path
    )
    diagnostics += check_engines_node(package_json, package_json_path)
    diagnostics += check_npmrc(recipe_dir)
    diagnostics += check_tsconfig(recipe_dir)

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
