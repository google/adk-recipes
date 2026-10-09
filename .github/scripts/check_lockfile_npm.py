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
Validates a TypeScript recipe's npm lockfile for supply-chain integrity.

Checks that:
  - Every resolved package URL points at the public npm registry
    (registry.npmjs.org).
  - No Git/VCS dependencies (git://, git+, github:).
  - No local file: or link: dependencies.
  - Every entry with a resolved URL carries a well-formed Subresource
    Integrity (SRI) hash (sha512, sha384, sha256, sha1).
  - Root, workspace, and internal symlink entries without resolved URLs
    or integrity hashes are exempt.
  - Structural sanity: malformed or corrupted lockfiles are reported as
    actionable shape errors.

Supported lockfile formats:
  - package-lock.json (npm lockfileVersion 1, 2, and 3)
  - npm-shrinkwrap.json

Unsupported formats detected in v1:
  - pnpm-lock.yaml (pnpm)
  - yarn.lock (Yarn)
  - bun.lockb / bun.lock (Bun)
  Detected unsupported formats are reported with actionable instructions to
  generate a package-lock.json rather than passing silently.

Usage: python3 check_lockfile_npm.py <recipe-dir-or-lockfile-path>

Exit codes:
  0  all dependencies verified
  1  contributor-fixable problems found; reported with diagnostics and annotations
  2  CI fault — the checker crashed or was invoked wrongly. Never blamed
     on the contributor's files.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ci_message import (
    Diagnostic,
    Doc,
    guard,
    infra_fault,
    report,
    report_infra_fault,
)

CHECKER = "check_lockfile_npm.py"
CHECK = "lockfile-npm"

_PACKAGE_LOCK_JSON = "package-lock.json"
_SHRINKWRAP_JSON = "npm-shrinkwrap.json"
_REGENERATE_COMMAND = "npm install --package-lock-only"

_MAX_DESCRIBE_LEN = 60
_TRUNCATE_DESCRIBE_LEN = 57

# Public npm registry URL pattern anchored per-URL.
# Rejects subdomains/suffixes such as registry.npmjs.org.attacker.example.
_NPM_REGISTRY_RE = re.compile(
    r"^https?://([^/@]*@)?registry\.npmjs\.org([:/]|$)"
)

# Standard Subresource Integrity (SRI) hash format: <algo>-<base64>.
_INTEGRITY_PART_RE = re.compile(
    r"^(sha512|sha384|sha256|sha1)-[A-Za-z0-9+/]+={0,2}$"
)

_VCS_PREFIXES = ("git://", "git+", "github:")
_PATH_PREFIXES = ("file:", "link:")

_UNSUPPORTED_FORMATS: dict[str, str] = {
    "pnpm-lock.yaml": "pnpm (pnpm-lock.yaml)",
    "yarn.lock": "Yarn (yarn.lock)",
    "bun.lockb": "Bun (bun.lockb)",
    "bun.lock": "Bun (bun.lock)",
}

_REGENERATE_HINT = (
    f"Regenerate {_PACKAGE_LOCK_JSON} from the recipe directory:\n"
    f"  rm {_PACKAGE_LOCK_JSON}\n"
    f"  {_REGENERATE_COMMAND}\n"
    f"Commit the updated {_PACKAGE_LOCK_JSON}."
)


def _is_valid_integrity(integrity: str) -> bool:
    """Validate SRI integrity string (single or space-separated digests)."""
    parts = integrity.strip().split()
    if not parts:
        return False
    return all(_INTEGRITY_PART_RE.match(part) for part in parts)


def _describe(value: object) -> str:
    """`list ['a', 'b']` — the type first, because the type is the bug."""
    text = repr(value)
    if len(text) > _MAX_DESCRIBE_LEN:
        text = text[:_TRUNCATE_DESCRIBE_LEN] + "..."
    return f"{type(value).__name__} {text}"


def _shape_error(lockfile: str, what: str) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=what,
        why=(
            f"{_PACKAGE_LOCK_JSON} is a generated file. A malformed structure means "
            "this lockfile was hand-edited, truncated, or written by an "
            "incompatible tool, so its dependencies and integrity hashes "
            "cannot be verified."
        ),
        how=_REGENERATE_HINT,
        doc=Doc.LOCK_HASH,
        file=lockfile,
    )


def _non_npm_registry(lockfile: str, label: str, url: str) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=f"{label} resolves from a non-public registry: {url}",
        why=(
            f"Every dependency in {_PACKAGE_LOCK_JSON} must resolve from the public "
            "npm registry (registry.npmjs.org). Private registries, internal "
            "mirrors, and custom registry URLs are not allowed in recipes."
        ),
        how=_REGENERATE_HINT,
        doc=Doc.LOCK_NON_PYPI,
        file=lockfile,
    )


def _vcs_dependency(lockfile: str, label: str, source: str) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=f"{label} is a Git/VCS dependency: {source}",
        why=(
            "Git dependencies (git://, github:, git+https://) are not allowed "
            "in lockfiles because they are non-reproducible when refs change "
            "and bypass registry security guarantees."
        ),
        how=(
            f"Replace the VCS dependency {label} with a published npm package, "
            f"then regenerate the lockfile:\n"
            f"  {_REGENERATE_COMMAND}\n"
            f"Commit the updated {_PACKAGE_LOCK_JSON}."
        ),
        doc=Doc.LOCK_VCS,
        file=lockfile,
    )


def _path_dependency(lockfile: str, label: str, source: str) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=f"{label} is a local file/link dependency: {source}",
        why=(
            "Local file: and link: dependencies resolve only on the machine "
            "where they were created. Recipes must depend only on published "
            "npm packages."
        ),
        how=(
            f"Replace the local dependency {label} with a published npm "
            f"package, then regenerate the lockfile:\n"
            f"  {_REGENERATE_COMMAND}\n"
            f"Commit the updated {_PACKAGE_LOCK_JSON}."
        ),
        doc=Doc.LOCK_PATH,
        file=lockfile,
    )


def _missing_integrity(lockfile: str, label: str, url: str) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=f"{label} has a resolved URL but no `integrity` hash: {url}",
        why=(
            f"Every resolved package entry in {_PACKAGE_LOCK_JSON} must carry an "
            "integrity hash (sha512 or sha256) so downloaded artifacts can be "
            "verified against supply-chain tampering."
        ),
        how=_REGENERATE_HINT,
        doc=Doc.LOCK_HASH,
        file=lockfile,
    )


def _bad_integrity(lockfile: str, label: str, integrity: str) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=f"{label} has an unrecognised integrity hash: {integrity!r}",
        why=(
            "Integrity hashes must use standard Subresource Integrity format: "
            "<algorithm>-<base64-digest> (e.g. sha512-... or sha256-...). "
            "Anything else cannot be verified at install time."
        ),
        how=_REGENERATE_HINT,
        doc=Doc.LOCK_HASH,
        file=lockfile,
    )


def _unsupported_format(lockfile: str, format_name: str) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=f"{lockfile} uses an unsupported lockfile format: {format_name}.",
        why=(
            f"Supply-chain policy currently verifies npm {_PACKAGE_LOCK_JSON}. "
            f"{format_name} is detected but cannot be verified yet."
        ),
        how=(
            f"Generate an npm {_PACKAGE_LOCK_JSON} for this recipe:\n"
            f"  {_REGENERATE_COMMAND}\n"
            f"Commit {_PACKAGE_LOCK_JSON}."
        ),
        doc=Doc.LOCK_HASH,
        file=lockfile,
    )


def _missing_lockfile(recipe_dir: str) -> Diagnostic:
    target_lock = str(Path(recipe_dir) / _PACKAGE_LOCK_JSON)
    return Diagnostic(
        check=CHECK,
        what=f"No lockfile found in {recipe_dir}.",
        why=(
            "Every TypeScript recipe with dependencies must commit a lockfile "
            f"({_PACKAGE_LOCK_JSON}) so dependency versions and hashes can be "
            "verified for supply-chain integrity."
        ),
        how=(
            f"Generate and commit a {_PACKAGE_LOCK_JSON} from the recipe directory:\n"
            f"  cd {recipe_dir}\n"
            f"  {_REGENERATE_COMMAND}\n"
            f"Commit the {_PACKAGE_LOCK_JSON} file."
        ),
        doc=Doc.REQUIRED_FILES,
        file=target_lock,
    )


def _get_pkg_label(pkg_path: str, pkg_info: dict) -> str:
    name = pkg_info.get("name", "")
    version = pkg_info.get("version", "")
    if name and version:
        return f"{name}@{version}"
    if pkg_path.startswith("node_modules/"):
        derived = pkg_path.rsplit("node_modules/", maxsplit=1)[-1]
        return f"{derived}@{version}" if version else derived
    return pkg_path or "<root>"


def _validate_integrity_value(
    label: str,
    integrity_val: object,
    lockfile: str,
) -> Diagnostic | None:
    """Validate format and type of an integrity value."""
    if not isinstance(integrity_val, str) or not _is_valid_integrity(
        integrity_val
    ):
        return _bad_integrity(lockfile, label, str(integrity_val))
    return None


def _check_resolved_and_integrity(
    label: str,
    pkg_entry: dict,
    lockfile: str,
) -> list[Diagnostic]:
    """Validate the resolved URL and integrity hash of a package entry."""
    diagnostics: list[Diagnostic] = []
    has_resolved = "resolved" in pkg_entry
    has_integrity = "integrity" in pkg_entry

    if has_resolved:
        resolved_val = pkg_entry["resolved"]
        if not isinstance(resolved_val, str):
            diagnostics.append(
                _shape_error(
                    lockfile,
                    f"{label} has a `resolved` value that is a "
                    f"{_describe(resolved_val)}, not a string.",
                )
            )
        elif resolved_val.startswith(_VCS_PREFIXES):
            diagnostics.append(_vcs_dependency(lockfile, label, resolved_val))
        elif resolved_val.startswith(_PATH_PREFIXES):
            diagnostics.append(_path_dependency(lockfile, label, resolved_val))
        elif resolved_val.startswith(("http://", "https://")):
            if not _NPM_REGISTRY_RE.match(resolved_val):
                diagnostics.append(
                    _non_npm_registry(lockfile, label, resolved_val)
                )
            elif not has_integrity or not pkg_entry["integrity"]:
                diagnostics.append(
                    _missing_integrity(lockfile, label, resolved_val)
                )
            else:
                diag = _validate_integrity_value(
                    label, pkg_entry["integrity"], lockfile
                )
                if diag is not None:
                    diagnostics.append(diag)
        else:
            diagnostics.append(_non_npm_registry(lockfile, label, resolved_val))
    elif has_integrity:
        diag = _validate_integrity_value(
            label, pkg_entry["integrity"], lockfile
        )
        if diag is not None:
            diagnostics.append(diag)

    return diagnostics


def _check_package_entry(
    pkg_path: str, pkg_info: object, lockfile: str
) -> list[Diagnostic]:
    """Validate a single package entry in package-lock.json v2/v3 packages map."""
    if not isinstance(pkg_info, dict):
        return [
            _shape_error(
                lockfile,
                f"Package entry {pkg_path!r} is a {_describe(pkg_info)}, "
                f"not an object.",
            )
        ]

    # Root entry (""): exempt from missing resolved / integrity checks.
    if pkg_path == "":
        resolved_val = pkg_info.get("resolved")
        if (
            resolved_val is not None
            and isinstance(resolved_val, str)
            and resolved_val.startswith(_VCS_PREFIXES)
        ):
            return [_vcs_dependency(lockfile, "<root>", resolved_val)]
        return []

    label = _get_pkg_label(pkg_path, pkg_info)

    # Internal workspace symlink entries (link: true)
    if pkg_info.get("link") is True:
        resolved_val = pkg_info.get("resolved")
        if isinstance(resolved_val, str):
            if resolved_val.startswith(_VCS_PREFIXES):
                return [_vcs_dependency(lockfile, label, resolved_val)]
            if resolved_val.startswith(_PATH_PREFIXES):
                return [_path_dependency(lockfile, label, resolved_val)]
        return []

    diagnostics: list[Diagnostic] = []

    # Check version field for git or local path source
    ver_val = pkg_info.get("version")
    if isinstance(ver_val, str):
        if ver_val.startswith(_VCS_PREFIXES):
            diagnostics.append(_vcs_dependency(lockfile, label, ver_val))
        elif ver_val.startswith(_PATH_PREFIXES):
            diagnostics.append(_path_dependency(lockfile, label, ver_val))

    diagnostics.extend(
        _check_resolved_and_integrity(
            label,
            pkg_info,
            lockfile,
        )
    )

    return diagnostics


def _check_v1_dependencies(
    deps: object, lockfile: str, prefix: str = ""
) -> list[Diagnostic]:
    """Recursively validate dependencies object in package-lock.json v1."""
    if not isinstance(deps, dict):
        return [
            _shape_error(
                lockfile,
                f"`dependencies` is a {_describe(deps)}, not an object.",
            )
        ]

    diagnostics: list[Diagnostic] = []
    for name, info in deps.items():
        label = f"{prefix}{name}"
        if not isinstance(info, dict):
            diagnostics.append(
                _shape_error(
                    lockfile,
                    f"Dependency {label!r} is a {_describe(info)}, not an object.",
                )
            )
            continue

        ver = info.get("version", "")
        pkg_label = f"{label}@{ver}" if ver else label

        if isinstance(ver, str):
            if ver.startswith(_VCS_PREFIXES):
                diagnostics.append(_vcs_dependency(lockfile, pkg_label, ver))
            elif ver.startswith(_PATH_PREFIXES):
                diagnostics.append(_path_dependency(lockfile, pkg_label, ver))

        diagnostics.extend(
            _check_resolved_and_integrity(
                pkg_label,
                info,
                lockfile,
            )
        )

        if "dependencies" in info:
            diagnostics.extend(
                _check_v1_dependencies(
                    info["dependencies"], lockfile, prefix=f"{label} > "
                )
            )

    return diagnostics


def _check_file(lockfile_path: Path) -> int:
    unsupported_format = _UNSUPPORTED_FORMATS.get(lockfile_path.name)
    if unsupported_format:
        return _report(
            [_unsupported_format(str(lockfile_path), unsupported_format)],
            str(lockfile_path),
        )

    try:
        content = lockfile_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return _report(
            [
                _shape_error(
                    str(lockfile_path),
                    f"{lockfile_path} is not valid UTF-8: {exc}",
                )
            ],
            str(lockfile_path),
        )
    except Exception as exc:
        return report_infra_fault(
            infra_fault(CHECKER, f"Cannot read {lockfile_path}: {exc}")
        )

    if not content.strip():
        return _report(
            [_shape_error(str(lockfile_path), f"{lockfile_path} is empty.")],
            str(lockfile_path),
        )

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return _report(
            [
                _shape_error(
                    str(lockfile_path),
                    f"{lockfile_path} is not valid JSON: {exc}",
                )
            ],
            str(lockfile_path),
        )

    if not isinstance(data, dict):
        return _report(
            [
                _shape_error(
                    str(lockfile_path),
                    f"Top-level structure is a {_describe(data)}, "
                    f"not a JSON object.",
                )
            ],
            str(lockfile_path),
        )

    diagnostics: list[Diagnostic] = []

    if "lockfileVersion" in data:
        lv = data["lockfileVersion"]
        if not isinstance(lv, int) or isinstance(lv, bool):
            diagnostics.append(
                _shape_error(
                    str(lockfile_path),
                    f"`lockfileVersion` is a {_describe(lv)}, not an integer.",
                )
            )
        elif lv not in (1, 2, 3):
            diagnostics.append(
                _shape_error(
                    str(lockfile_path),
                    f"Unrecognised lockfileVersion: {lv}. Expected 1, 2, or 3.",
                )
            )

    packages = data.get("packages")
    if packages is not None:
        if not isinstance(packages, dict):
            diagnostics.append(
                _shape_error(
                    str(lockfile_path),
                    f"`packages` is a {_describe(packages)}, not an object.",
                )
            )
        else:
            for pkg_path, pkg_info in packages.items():
                diagnostics.extend(
                    _check_package_entry(pkg_path, pkg_info, str(lockfile_path))
                )
    elif "dependencies" in data:
        deps = data.get("dependencies")
        if not isinstance(deps, dict):
            diagnostics.append(
                _shape_error(
                    str(lockfile_path),
                    f"`dependencies` is a {_describe(deps)}, not an object.",
                )
            )
        else:
            diagnostics.extend(_check_v1_dependencies(deps, str(lockfile_path)))

    return _report(diagnostics, str(lockfile_path))


def _report(diagnostics: list[Diagnostic], target_label: str) -> int:
    return report(
        diagnostics,
        header=f"{target_label} — npm lockfile supply-chain problems",
        passed_message=(
            f"{target_label}: all dependencies point to the public npm "
            f"registry and carry valid integrity hashes."
        ),
        next_step=_REGENERATE_HINT,
    )


def _run(target_str: str) -> int:
    target = Path(target_str)
    if not target.exists():
        return report_infra_fault(
            infra_fault(
                CHECKER,
                f"{target_str} does not exist. The path came from CI workflow "
                f"discovery, not from the contributor.",
            )
        )

    if target.is_dir():
        pkg_lock = target / _PACKAGE_LOCK_JSON
        shrinkwrap = target / _SHRINKWRAP_JSON

        if pkg_lock.exists():
            return _check_file(pkg_lock)
        if shrinkwrap.exists():
            return _check_file(shrinkwrap)

        for filename, format_label in _UNSUPPORTED_FORMATS.items():
            candidate = target / filename
            if candidate.exists():
                return _report(
                    [_unsupported_format(str(candidate), format_label)],
                    str(candidate),
                )

        pkg_json = target / "package.json"
        if pkg_json.exists():
            return _report([_missing_lockfile(str(target))], str(target))

        return report_infra_fault(
            infra_fault(
                CHECKER,
                f"No package.json or lockfile found in directory {target_str}.",
            )
        )

    return _check_file(target)


def main() -> int:
    if len(sys.argv) != 2:
        return report_infra_fault(
            infra_fault(
                CHECKER,
                f"invoked with {len(sys.argv) - 1} argument(s); expected "
                f"exactly one recipe directory or lockfile path.",
            )
        )
    try:
        return _run(sys.argv[1])
    except Exception as exc:
        return report_infra_fault(
            infra_fault(CHECKER, f"{type(exc).__name__}: {exc}")
        )


if __name__ == "__main__":
    sys.exit(guard(CHECKER, main))
