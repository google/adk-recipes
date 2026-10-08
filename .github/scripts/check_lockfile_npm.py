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
"""Validates a TypeScript recipe's npm package-lock.json for supply-chain integrity.

Checks that:
  - Supported lockfile format is used (package-lock.json v2/v3). Other lockfile
    formats (pnpm-lock.yaml, yarn.lock, bun.lockb, bun.lock) are reported as
    unsupported in v1.
  - Every resolved package URL points at the public npm registry (https://registry.npmjs.org/).
  - No git://, git+ssh://, git+https://, or github: dependencies.
  - No file: or link: local path dependencies.
  - Every resolved dependency carries a well-formed integrity hash (sha512-/sha384-/sha256-).
  - Root entry, workspace entries, and link dependencies with no resolved URL are exempted.
  - Malformed or truncated lockfiles produce a single structural shape error.

Usage: python3 check_lockfile_npm.py <recipe-dir>

Exit codes:
  0  all checks passed (or recipe has no dependencies and no lockfile)
  1  contributor-fixable problems found; reported with diagnostics and annotations
  2  CI fault — the checker crashed or was invoked wrongly.
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

CHECKER = "check_lockfile_npm.py"
CHECK = "lockfile-npm"

ALLOWED_NPM_HOSTS = frozenset({"registry.npmjs.org"})
_HASH_RE = re.compile(r"^(sha512|sha384|sha256)-[A-Za-z0-9+/=]+$")

_OTHER_LOCKFILES = (
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "bun.lock",
)

_REGENERATE_HINT = (
    "Regenerate package-lock.json from the recipe directory:\n"
    "  rm -f package-lock.json\n"
    "  npm install --package-lock-only\n"
    "Commit the updated package-lock.json."
)


def _shape_error(lockfile: Path, what: str) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=what,
        why=(
            "package-lock.json is a generated file. A malformed structure means "
            "the lockfile was hand-edited, corrupted, or written by an incompatible "
            "tool, so its dependencies and hashes cannot be verified."
        ),
        how=_REGENERATE_HINT,
        doc=Doc.LOCK_HASH,
        file=str(lockfile),
    )


def _check_entry(
    label: str,
    entry: dict,
    lockfile: Path,
    is_root: bool = False,
) -> list[Diagnostic]:
    if is_root or not isinstance(entry, dict):
        return []

    if entry.get("link") is True:
        # Link dependency
        return [
            Diagnostic(
                check=CHECK,
                what=f"{label} is a local link dependency.",
                why=(
                    "Recipes cannot use local link dependencies because local target "
                    "paths do not exist outside the author's workstation."
                ),
                how=_REGENERATE_HINT,
                doc=Doc.LOCK_PATH,
                file=str(lockfile),
            )
        ]

    resolved = entry.get("resolved")
    integrity = entry.get("integrity")

    # If neither resolved nor integrity is present (e.g. workspace or bundled package)
    if resolved is None and integrity is None:
        return []

    diagnostics: list[Diagnostic] = []

    if resolved is not None:
        resolved_str = str(resolved).strip()

        # Check for local path / file:
        if resolved_str.startswith(("file:", "link:")):
            diagnostics.append(
                Diagnostic(
                    check=CHECK,
                    what=f"{label} references a local path dependency: {resolved_str!r}",
                    why=(
                        "Local file/path dependencies cannot resolve in CI or on "
                        "end-user machines."
                    ),
                    how=_REGENERATE_HINT,
                    doc=Doc.LOCK_PATH,
                    file=str(lockfile),
                )
            )
            return diagnostics

        # Check for VCS / git
        parsed = urlparse(resolved_str)
        if (
            resolved_str.startswith(
                (
                    "git:",
                    "git://",
                    "git+ssh://",
                    "git+https://",
                    "git+http://",
                    "github:",
                )
            )
            or parsed.netloc.lower() == "github.com"
        ):
            diagnostics.append(
                Diagnostic(
                    check=CHECK,
                    what=f"{label} references a Git/VCS dependency: {resolved_str!r}",
                    why=(
                        "Dependencies must resolve from the public npm registry "
                        "with cryptographic integrity hashes, not unpinned Git references."
                    ),
                    how=_REGENERATE_HINT,
                    doc=Doc.LOCK_VCS,
                    file=str(lockfile),
                )
            )
            return diagnostics

        # Check registry host
        host = parsed.netloc.lower().split(":")[0]
        if (
            parsed.scheme not in ("http", "https")
            or host not in ALLOWED_NPM_HOSTS
        ):
            diagnostics.append(
                Diagnostic(
                    check=CHECK,
                    what=(
                        f"{label} resolves from a non-public npm registry: {resolved_str!r}"
                    ),
                    why=(
                        "All dependencies in package-lock.json must resolve from the "
                        "public npm registry (https://registry.npmjs.org/)."
                    ),
                    how=_REGENERATE_HINT,
                    doc=Doc.LOCK_NON_PYPI,
                    file=str(lockfile),
                )
            )

    # Check integrity hash
    if resolved is not None:
        if integrity is None or not str(integrity).strip():
            diagnostics.append(
                Diagnostic(
                    check=CHECK,
                    what=f"{label} has a resolved URL but is missing an `integrity` hash.",
                    why=(
                        "Every downloadable artifact in package-lock.json must declare "
                        "an integrity checksum (sha512/sha384/sha256) so downloaded "
                        "packages are proven identical to locked versions."
                    ),
                    how=_REGENERATE_HINT,
                    doc=Doc.LOCK_HASH,
                    file=str(lockfile),
                )
            )
        else:
            integrity_str = str(integrity).strip()
            if not _HASH_RE.match(integrity_str):
                diagnostics.append(
                    Diagnostic(
                        check=CHECK,
                        what=f"{label} has an unrecognised or malformed integrity hash: {integrity_str!r}",
                        why=(
                            "Integrity hashes must use a standard algorithm prefix "
                            "(sha512-, sha384-, or sha256-) followed by base64-encoded digest."
                        ),
                        how=_REGENERATE_HINT,
                        doc=Doc.LOCK_HASH,
                        file=str(lockfile),
                    )
                )

    return diagnostics


def _check_dependencies_v1(
    deps: dict,
    lockfile: Path,
    prefix: str = "",
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if not isinstance(deps, dict):
        return [
            _shape_error(lockfile, "dependencies table is not a JSON object.")
        ]

    for name, info in sorted(deps.items()):
        label = f"{prefix}{name}" if prefix else f"package {name!r}"
        if isinstance(info, dict):
            diagnostics.extend(_check_entry(label, info, lockfile))
            nested = info.get("dependencies")
            if isinstance(nested, dict):
                diagnostics.extend(
                    _check_dependencies_v1(
                        nested, lockfile, prefix=f"{label} -> "
                    )
                )
    return diagnostics


def validate_lockfile_npm(recipe_dir: Path) -> list[Diagnostic]:
    lockfile = recipe_dir / "package-lock.json"

    # Check if other unsupported lockfiles exist
    for alt in _OTHER_LOCKFILES:
        alt_path = recipe_dir / alt
        if alt_path.is_file() and not lockfile.is_file():
            return [
                Diagnostic(
                    check=CHECK,
                    what=f"{alt_path} uses an unsupported lockfile format.",
                    why=(
                        "TypeScript recipe CI currently enforces supply-chain policies "
                        "against npm package-lock.json v2/v3. Other lockfile formats "
                        f"({alt}) are not supported in v1."
                    ),
                    how=(
                        f"Generate package-lock.json with `npm install --package-lock-only` "
                        f"and remove {alt}."
                    ),
                    doc=Doc.LOCK_HASH,
                    file=str(alt_path),
                )
            ]

    if not lockfile.is_file():
        # Check if package.json has dependencies
        pkg_json_path = recipe_dir / "package.json"
        if pkg_json_path.is_file():
            try:
                pkg_data = json.loads(pkg_json_path.read_text(encoding="utf-8"))
                if isinstance(pkg_data, dict):
                    has_deps = bool(
                        pkg_data.get("dependencies")
                        or pkg_data.get("devDependencies")
                    )
                    if has_deps:
                        return [
                            Diagnostic(
                                check=CHECK,
                                what=f"{recipe_dir} declares dependencies in package.json but has no package-lock.json.",
                                why=(
                                    "Every TypeScript recipe declaring dependencies must check in "
                                    "a corresponding package-lock.json to guarantee reproducible, "
                                    "tamper-proof builds."
                                ),
                                how=_REGENERATE_HINT,
                                doc=Doc.NO_SIBLING_LOCK,
                                file=str(pkg_json_path),
                            )
                        ]
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                pass
        return []

    try:
        content = lockfile.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return [
            _shape_error(
                lockfile, f"package-lock.json is not valid UTF-8: {exc}"
            )
        ]

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return [
            _shape_error(
                lockfile,
                f"package-lock.json:{exc.lineno}:{exc.colno} could not be parsed as JSON: {exc.msg}",
            )
        ]

    if not isinstance(data, dict):
        return [
            _shape_error(
                lockfile, "package-lock.json root must be a JSON object."
            )
        ]

    lock_version = data.get("lockfileVersion")
    if lock_version not in (1, 2, 3):
        return [
            _shape_error(
                lockfile,
                f"package-lock.json declares unsupported lockfileVersion: {lock_version!r} (expected 1, 2, or 3).",
            )
        ]

    diagnostics: list[Diagnostic] = []

    # Format v2/v3 has "packages"
    if "packages" in data:
        packages = data["packages"]
        if not isinstance(packages, dict):
            return [
                _shape_error(lockfile, "`packages` field must be an object.")
            ]

        for pkg_path, entry in sorted(packages.items()):
            is_root = pkg_path == ""
            label = f"package {pkg_path!r}" if pkg_path else "root package"
            if isinstance(entry, dict):
                diagnostics.extend(
                    _check_entry(label, entry, lockfile, is_root=is_root)
                )
            else:
                return [
                    _shape_error(
                        lockfile, f"`packages[{pkg_path!r}]` must be an object."
                    )
                ]

    # Format v1 (or v2 fallback) has "dependencies"
    elif "dependencies" in data:
        diagnostics.extend(
            _check_dependencies_v1(data["dependencies"], lockfile)
        )

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
        raw_path.parent if raw_path.name == "package-lock.json" else raw_path
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

    diagnostics = validate_lockfile_npm(recipe_dir)
    return report(
        diagnostics,
        header=f"{recipe_dir}: npm lockfile integrity",
        passed_message=(
            f"{recipe_dir}/package-lock.json: all dependencies resolve from "
            f"registry.npmjs.org with valid integrity hashes."
        ),
        next_step="Regenerate package-lock.json with npm install --package-lock-only.",
    )


if __name__ == "__main__":
    sys.exit(guard(CHECKER, main))
