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
Validates a Go recipe's go.sum against its go.mod for supply-chain integrity.

Checks that:
  - When go.mod has require entries, a go.sum file must exist.
  - When go.mod has no require entries and no go.sum, it passes silently.
  - Every module in go.mod's require list has a matching h1: content hash in go.sum.
  - All lines in go.sum are well-formed: <module> <version>[/go.mod] h1:<base64>.
  - When go.mod has no require entries, go.sum must not contain stale checksums.

Usage: python3 check_lockfile_gosum.py <recipe-dir-or-go.mod>

Exit codes:
  0  all checks passed (or recipe has no dependencies and no go.sum)
  1  contributor-fixable problems found; reported with diagnostics and annotations
  2  CI fault — the checker crashed or was invoked wrongly.
"""

from __future__ import annotations

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

CHECKER = "check_lockfile_gosum.py"
CHECK = "lockfile-gosum"

# Standard Go module checksum: SHA-256 base64 digest with '=' padding.
_HASH_RE = re.compile(r"^h1:[A-Za-z0-9+/]{43}=$")

_REGENERATE_HINT = (
    "Regenerate go.sum from the recipe directory:\n"
    "  rm -f go.sum\n"
    "  go mod tidy\n"
    "Commit the updated go.sum."
)


def _strip_comment(line: str) -> str:
    """Strip // line comments outside quotes."""
    in_quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if in_quote is not None:
            if ch == "\\" and i + 1 < len(line):
                i += 2
                continue
            if ch == in_quote:
                in_quote = None
            i += 1
            continue

        if ch in ('"', "'"):
            in_quote = ch
            i += 1
            continue

        if ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
            return line[:i].strip()

        i += 1

    return line.strip()


def _record_require(
    tokens: list[str],
    requires: dict[str, str],
    lineno: int,
    errors: list[str],
) -> None:
    """Validate and record a require entry from tokens."""
    if len(tokens) >= 2:
        mod_path = tokens[0].strip("\"'")
        version = tokens[1].strip("\"'")
        requires[mod_path] = version
    elif tokens:
        raw = " ".join(tokens)
        errors.append(f"Line {lineno}: invalid require entry {raw!r}")


def parse_go_mod(content: str) -> tuple[dict[str, str], list[str]]:
    """Extract required modules and versions from go.mod content.

    Returns:
        tuple of (requires_dict, errors_list) where requires_dict maps
        module_path -> version.
    """
    requires: dict[str, str] = {}
    errors: list[str] = []
    in_require_block = False

    for lineno, raw_line in enumerate(content.splitlines(), start=1):
        line = _strip_comment(raw_line)
        if not line:
            continue

        if in_require_block:
            if line == ")" or line.startswith(")"):
                in_require_block = False
                continue
            tokens = line.split()
            _record_require(tokens, requires, lineno, errors)
            continue

        if (
            line == "require ("
            or line.startswith("require (")
            or line.startswith("require(")
        ):
            in_require_block = True
            rest = line.split("(", 1)[1].strip()
            if rest.endswith(")"):
                in_require_block = False
                rest = rest[:-1].strip()
            if rest:
                tokens = rest.split()
                _record_require(tokens, requires, lineno, errors)
            continue

        if line.startswith("require ") or line.startswith("require\t"):
            rest = line.removeprefix("require").strip()
            tokens = rest.split()
            _record_require(tokens, requires, lineno, errors)

    if in_require_block:
        errors.append("Unclosed require block in go.mod")

    return requires, errors


def parse_go_sum(
    content: str,
) -> tuple[dict[tuple[str, str], str], list[tuple[int, str]]]:
    """Parse go.sum lines into (module, version_token) -> hash mapping.

    Returns:
        tuple of (entries_dict, malformed_lines)
    """
    entries: dict[tuple[str, str], str] = {}
    malformed: list[tuple[int, str]] = []

    for lineno, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        tokens = line.split()
        if len(tokens) != 3:
            malformed.append((lineno, raw_line))
            continue
        mod, ver_token, hash_val = tokens
        if not _HASH_RE.match(hash_val):
            malformed.append((lineno, raw_line))
            continue
        base_ver = ver_token.removesuffix("/go.mod")
        if not base_ver.startswith("v"):
            malformed.append((lineno, raw_line))
            continue
        entries[(mod, ver_token)] = hash_val

    return entries, malformed


def _shape_error(file_path: str, what: str) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=what,
        why=(
            "go.mod must be a valid Go module definition file containing "
            "standard module, go, and require directives."
        ),
        how=f"Fix the syntax error in {file_path} or run 'go mod init'.",
        doc=Doc.REQUIRED_FILES,
        file=file_path,
    )


def _missing_gosum(
    go_sum_path: str, go_mod_path: str, count: int
) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=(
            f"{go_sum_path} is missing, but {go_mod_path} has {count} "
            f"require {'entry' if count == 1 else 'entries'}."
        ),
        why=(
            "go.sum records cryptographic checksums (h1 hashes) for all "
            "dependencies required by go.mod to ensure supply-chain integrity. "
            "A recipe with dependencies must commit a go.sum file."
        ),
        how=_REGENERATE_HINT,
        doc=Doc.LOCK_HASH,
        file=go_sum_path,
    )


def _missing_hash(
    go_sum_path: str, go_mod_path: str, module: str, version: str
) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=(
            f"Module '{module} {version}' required in {go_mod_path} has no "
            f"matching h1: content hash in {go_sum_path}."
        ),
        why=(
            "Every module version listed in go.mod's require list must have a "
            "corresponding h1: content hash in go.sum to guarantee that the "
            "downloaded source code matches the locked checksum."
        ),
        how=_REGENERATE_HINT,
        doc=Doc.LOCK_HASH,
        file=go_sum_path,
    )


def _malformed_gosum_line(
    go_sum_path: str, lineno: int, line_content: str
) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=f"{go_sum_path}:{lineno} has a malformed entry: {line_content!r}",
        why=(
            "Each line in go.sum must have the format "
            "'<module> <version>[/go.mod] h1:<base64-hash>' with a valid "
            "base64-encoded SHA-256 hash."
        ),
        how=_REGENERATE_HINT,
        doc=Doc.LOCK_HASH,
        file=go_sum_path,
    )


def _stale_gosum(go_sum_path: str, go_mod_path: str) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=(
            f"{go_sum_path} contains checksum entries, but {go_mod_path} has "
            f"no require entries."
        ),
        why=(
            "go.sum should only contain checksums for dependencies required "
            "by go.mod. A recipe with no dependencies does not need a go.sum file."
        ),
        how=f"Remove the unnecessary go.sum file:\n  rm {go_sum_path}",
        doc=Doc.LOCK_STALE,
        file=go_sum_path,
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
        go_mod_path = target / "go.mod"
        go_sum_path = target / "go.sum"
    elif target.name == "go.mod":
        go_mod_path = target
        go_sum_path = target.parent / "go.sum"
    elif target.name == "go.sum":
        go_mod_path = target.parent / "go.mod"
        go_sum_path = target
    else:
        return report_infra_fault(
            infra_fault(
                CHECKER,
                f"Expected a recipe directory or a go.mod/go.sum file, got {target_str}.",
            )
        )

    if not go_mod_path.exists():
        return report_infra_fault(
            infra_fault(
                CHECKER,
                f"{go_mod_path} does not exist in {target_str}.",
            )
        )

    try:
        mod_content = go_mod_path.read_text(encoding="utf-8")
    except Exception as exc:
        return _report(
            [
                _shape_error(
                    str(go_mod_path), f"Cannot read {go_mod_path}: {exc}"
                )
            ],
            str(go_mod_path),
        )

    requires, mod_errors = parse_go_mod(mod_content)
    if mod_errors:
        return _report(
            [
                _shape_error(str(go_mod_path), f"{go_mod_path}: {err}")
                for err in mod_errors
            ],
            str(go_mod_path),
        )

    has_gosum = go_sum_path.exists()

    # Rule: go.mod has no require entries and no go.sum -> pass silently
    if not requires and not has_gosum:
        return report(
            [],
            header=f"{target_str} — go.sum checks",
            passed_message=(
                f"{go_mod_path}: no dependencies required and no go.sum present (pass)."
            ),
            next_step="",
        )

    # Rule: go.mod has require entries but no go.sum -> report
    if requires and not has_gosum:
        return _report(
            [_missing_gosum(str(go_sum_path), str(go_mod_path), len(requires))],
            str(go_sum_path),
        )

    # Rule: go.mod has no require entries but go.sum exists -> stale
    if not requires and has_gosum:
        return _report(
            [_stale_gosum(str(go_sum_path), str(go_mod_path))],
            str(go_sum_path),
        )

    # go.sum exists with dependencies in go.mod: parse it
    try:
        sum_content = go_sum_path.read_text(encoding="utf-8")
    except Exception as exc:
        return _report(
            [
                _shape_error(
                    str(go_sum_path), f"Cannot read {go_sum_path}: {exc}"
                )
            ],
            str(go_sum_path),
        )

    sum_entries, malformed_lines = parse_go_sum(sum_content)

    diagnostics: list[Diagnostic] = []

    # Rule: malformed lines in go.sum
    for lineno, line_text in malformed_lines:
        diagnostics.append(
            _malformed_gosum_line(str(go_sum_path), lineno, line_text)
        )

    # Rule: every required module has matching h1: hash in go.sum
    for mod, ver in requires.items():
        if (mod, ver) not in sum_entries:
            diagnostics.append(
                _missing_hash(str(go_sum_path), str(go_mod_path), mod, ver)
            )

    return _report(diagnostics, str(go_sum_path))


def _report(diagnostics: list[Diagnostic], target_label: str) -> int:
    return report(
        diagnostics,
        header=f"{target_label} — go.sum supply-chain integrity problems",
        passed_message=(
            f"{target_label}: all required module checksums are verified in go.sum."
        ),
        next_step=_REGENERATE_HINT,
    )


def main() -> int:
    if len(sys.argv) != 2:
        return report_infra_fault(
            infra_fault(
                CHECKER,
                f"invoked with {len(sys.argv) - 1} argument(s); expected "
                f"exactly one recipe directory or go.mod path.",
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
