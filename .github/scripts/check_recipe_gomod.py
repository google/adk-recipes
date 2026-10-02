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
Validates a Go recipe's go.mod against the repository's metadata rules.

Rules enforced:

  - module-path: module path MUST be
    github.com/google/adk-recipes/<root>/go/<recipe>, where <root> is core
    or contrib (derivable from the recipe location).
  - go-version: the `go` directive must declare a Go version <= the version
    CI pins in .github/workflows/go-tests.yml (currently 1.26). A recipe
    declaring a higher version than CI provides fails because CI cannot
    build it. Lower versions are permitted.
  - no-local-replace: replace directives pointing to local filesystem paths
    (e.g. `replace ... => ../something`) are forbidden because they cannot
    resolve outside the local author's workstation. Replacements pointing to
    published modules are permitted.
  - No description rule (go.mod has no description field) and no registry
    rule (GOPROXY is an environment variable, not a go.mod directive).

Usage: python check_recipe_gomod.py <recipe-dir>

Exit codes:
  0  every rule passed.
  1  contributor-fixable problems found; every one has been reported with
     a fix, both as a human block and as a ::error annotation.
  2  CI fault — the checker crashed, or its own environment is missing a
     dependency it needs. Never blamed on the contributor's files.

A missing go.mod is not this script's failure to report (the required-files
check in validate_structure.py owns it), so that case exits 0 with a note.
"""

from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from packaging.version import InvalidVersion, Version

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

CHECKER = "check_recipe_gomod.py"

# Maximum Go version supported by CI (see .github/workflows/go-tests.yml).
CI_PINNED_GO_VERSION = "1.26"

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


def expected_module_path(
    recipe_dir: Path, repo_root: Path | None = None
) -> str:
    """Return the canonical Go module path this recipe is required to declare.

    Canonical form: github.com/google/adk-recipes/<root>/go/<recipe>
    where <root> is 'core' or 'contrib' (derivable from the recipe location).
    """
    parts = _repo_relative_parts(recipe_dir, repo_root)
    if "core" in parts:
        root = "core"
    elif "contrib" in parts:
        root = "contrib"
    else:
        root = "contrib"
    recipe = parts[-1] if parts else recipe_dir.name
    return f"github.com/google/adk-recipes/{root}/go/{recipe}"


@dataclass
class ReplaceDirective:
    """A single parsed replace directive in go.mod."""

    old_path: str
    old_version: str | None
    new_target: str
    new_version: str | None
    line_number: int
    raw_line: str
    is_local_path: bool


@dataclass
class ParsedGoMod:
    """Structured representation of a parsed go.mod file."""

    module: str | None = None
    module_line: int | None = None
    go_version: str | None = None
    go_line: int | None = None
    replaces: list[ReplaceDirective] = field(default_factory=list)


def _strip_comment(line: str) -> str:
    """Strip // comments from a line, respecting quoted strings and escapes."""
    in_quote = False
    quote_char = ""
    escaped = False
    i = 0
    while i < len(line):
        c = line[i]
        if escaped:
            escaped = False
            i += 1
            continue

        if c == "\\" and in_quote and quote_char == '"':
            escaped = True
            i += 1
            continue

        if c in ('"', "`"):
            if not in_quote:
                in_quote = True
                quote_char = c
            elif quote_char == c:
                in_quote = False
        elif (
            not in_quote
            and c == "/"
            and i + 1 < len(line)
            and line[i + 1] == "/"
        ):
            return line[:i].strip()
        i += 1
    return line.strip()


def _tokenize(line: str) -> list[str]:
    """Split line into tokens, stripping surrounding quotes including backticks."""
    try:
        lexer = shlex.shlex(line, posix=True)
        lexer.quotes += "`"
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return line.split()


def _parse_replace_tokens(
    tokens: list[str],
    lineno: int,
    gomod_path: Path,
    clean_line: str,
) -> tuple[ReplaceDirective | None, Diagnostic | None]:
    """Parse a single replace directive token list into ReplaceDirective or Diagnostic."""
    if "=>" not in tokens:
        return None, Diagnostic(
            check="gomod-parse",
            what=(
                f"Malformed replace directive at line {lineno} in "
                f"{gomod_path}: '{clean_line}'."
            ),
            why=(
                "Replace directives must follow the syntax "
                "'<old> [version] => <new> [version]'."
            ),
            how=f"Fix the syntax on line {lineno} of {gomod_path.name}.",
            doc=Doc.PROJECT_NAME,
            file=str(gomod_path),
        )

    arrow_idx = tokens.index("=>")
    left = tokens[:arrow_idx]
    right = tokens[arrow_idx + 1 :]
    old_path = left[0] if left else ""
    old_version = left[1] if len(left) > 1 else None
    new_target = right[0] if right else ""
    new_version = right[1] if len(right) > 1 else None

    is_local = (
        new_target.startswith(("./", "../", "/", "~", "\\"))
        or new_target in (".", "..")
        or new_version is None
    )
    return (
        ReplaceDirective(
            old_path=old_path,
            old_version=old_version,
            new_target=new_target,
            new_version=new_version,
            line_number=lineno,
            raw_line=clean_line,
            is_local_path=is_local,
        ),
        None,
    )


def _handle_replace(
    tokens: list[str],
    lineno: int,
    gomod_path: Path,
    clean_line: str,
    parsed: ParsedGoMod,
    diagnostics: list[Diagnostic],
) -> None:
    rep, diag = _parse_replace_tokens(tokens, lineno, gomod_path, clean_line)
    if diag:
        diagnostics.append(diag)
    elif rep:
        parsed.replaces.append(rep)


def parse_gomod(
    content: str, gomod_path: Path
) -> tuple[ParsedGoMod, list[Diagnostic]]:
    """Parse go.mod content into a ParsedGoMod structure."""
    parsed = ParsedGoMod()
    diagnostics: list[Diagnostic] = []
    in_block: str | None = None

    for lineno, raw_line in enumerate(content.splitlines(), start=1):
        clean_line = _strip_comment(raw_line)
        if not clean_line:
            continue

        tokens = _tokenize(clean_line)
        if not tokens:
            continue

        if in_block is not None:
            if tokens[0] == ")":
                in_block = None
                tokens = tokens[1:]
                if not tokens:
                    continue

        if in_block == "replace":
            _handle_replace(
                tokens, lineno, gomod_path, clean_line, parsed, diagnostics
            )
            continue

        if in_block == "module":
            if not parsed.module and tokens:
                parsed.module = tokens[0]
                parsed.module_line = lineno
            continue

        if in_block in ("require", "exclude", "retract"):
            continue

        directive = tokens[0]
        if len(tokens) > 1 and tokens[1] == "(":
            in_block = directive
            continue

        if directive == "module":
            if len(tokens) > 1:
                if tokens[1] == "(":
                    in_block = "module"
                else:
                    parsed.module = tokens[1]
                    parsed.module_line = lineno
            elif len(tokens) == 1 and "(" in clean_line:
                in_block = "module"
        elif directive == "go":
            if len(tokens) > 1:
                parsed.go_version = tokens[1]
                parsed.go_line = lineno
        elif directive == "replace":
            if len(tokens) > 1 and tokens[1] == "(":
                in_block = "replace"
                continue
            _handle_replace(
                tokens[1:], lineno, gomod_path, clean_line, parsed, diagnostics
            )
        elif directive in ("require", "exclude", "retract"):
            if len(tokens) > 1 and tokens[1] == "(":
                in_block = directive
            elif "(" in clean_line:
                in_block = directive

    return parsed, diagnostics


def check_module_path(
    parsed: ParsedGoMod,
    gomod_path: Path,
    recipe_dir: Path,
    repo_root: Path | None = None,
) -> list[Diagnostic]:
    """Check that module path matches canonical form."""
    expected = expected_module_path(recipe_dir, repo_root)
    if not parsed.module:
        return [
            Diagnostic(
                check="gomod-module-path",
                what=f"`module` directive is missing from {gomod_path}.",
                why=(
                    f"Every Go recipe must declare a canonical module path "
                    f"matching its location ('{expected}')."
                ),
                how=f"Add to {gomod_path.name}:\n  module {expected}",
                doc=Doc.PROJECT_NAME,
                file=str(gomod_path),
            )
        ]

    if parsed.module != expected:
        return [
            Diagnostic(
                check="gomod-module-path",
                what=(
                    f"module path = '{parsed.module}' in {gomod_path}, but "
                    f"this recipe must declare '{expected}'."
                ),
                why=(
                    "Go recipes under core/ and contrib/ must declare a "
                    "canonical module path matching their location "
                    "('github.com/google/adk-recipes/<root>/go/<recipe>')."
                ),
                how=(
                    f"Update the module directive in {gomod_path.name}:\n"
                    f"  module {expected}"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(gomod_path),
            )
        ]

    return []


def _parse_go_version(version_str: str) -> Version | None:
    """Parse a Go version string (e.g. '1.26', '1.26.0', '1.25.1')."""
    try:
        return Version(version_str)
    except InvalidVersion:
        return None


def check_go_version(parsed: ParsedGoMod, gomod_path: Path) -> list[Diagnostic]:
    """Check that the `go` directive is <= CI's pinned Go version."""
    if not parsed.go_version:
        return [
            Diagnostic(
                check="gomod-go-version",
                what=f"`go` directive is missing from {gomod_path}.",
                why=(
                    f"Every Go recipe must declare the Go language version it "
                    f"requires. CI pins Go {CI_PINNED_GO_VERSION} (in "
                    f".github/workflows/go-tests.yml)."
                ),
                how=f"Add to {gomod_path.name}:\n  go {CI_PINNED_GO_VERSION}",
                doc=Doc.PROJECT_NAME,
                file=str(gomod_path),
            )
        ]

    parsed_ver = _parse_go_version(parsed.go_version)
    if parsed_ver is None:
        return [
            Diagnostic(
                check="gomod-go-version",
                what=(
                    f"`go` directive '{parsed.go_version}' in {gomod_path} "
                    f"is not a valid version."
                ),
                why=(
                    f"Go requires a valid version format (e.g. "
                    f"'{CI_PINNED_GO_VERSION}') in the go directive."
                ),
                how=(
                    f"Set a valid version in {gomod_path.name}:\n"
                    f"  go {CI_PINNED_GO_VERSION}"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(gomod_path),
            )
        ]

    ci_ver = Version(CI_PINNED_GO_VERSION)
    if parsed_ver > ci_ver:
        return [
            Diagnostic(
                check="gomod-go-version",
                what=(
                    f"`go` directive = '{parsed.go_version}' in {gomod_path} "
                    f"exceeds CI's pinned Go version ({CI_PINNED_GO_VERSION})."
                ),
                why=(
                    f"CI pins Go {CI_PINNED_GO_VERSION} (in "
                    f".github/workflows/go-tests.yml). A recipe declaring a "
                    f"higher version cannot be built or tested in CI."
                ),
                how=(
                    f"Lower the `go` directive in {gomod_path.name} to "
                    f"{CI_PINNED_GO_VERSION} or below:\n"
                    f"  go {CI_PINNED_GO_VERSION}"
                ),
                doc=Doc.PROJECT_NAME,
                file=str(gomod_path),
            )
        ]

    return []


def check_replaces(parsed: ParsedGoMod, gomod_path: Path) -> list[Diagnostic]:
    """Check that no replace directives point to local filesystem paths."""
    diagnostics: list[Diagnostic] = []
    for rep in parsed.replaces:
        if rep.is_local_path:
            diagnostics.append(
                Diagnostic(
                    check="gomod-no-local-replace",
                    what=(
                        f"replace directive '{rep.raw_line}' in {gomod_path} "
                        f"points to a local path ('{rep.new_target}')."
                    ),
                    why=(
                        "Local path replacements cannot be resolved outside "
                        "the author's local environment and break CI builds. "
                        "Replacements must point to published modules."
                    ),
                    how=(
                        f"Remove the local replace directive from "
                        f"{gomod_path.name} or replace it with a published "
                        f"module version:\n"
                        f"  // Remove: {rep.raw_line}"
                    ),
                    doc=Doc.LOCK_PATH,
                    file=str(gomod_path),
                )
            )
    return diagnostics


def _report(diagnostics: list[Diagnostic], recipe_dir: Path) -> int:
    return report(
        diagnostics,
        header=f"{recipe_dir}: go.mod metadata",
        passed_message=(
            f"{recipe_dir}/go.mod: module path, go directive and replace "
            f"directives all satisfy the repo rules."
        ),
        next_step=(
            "Update go.mod with the canonical module path and ensure no "
            "local replace directives are used."
        ),
    )


def _run(recipe_dir: Path) -> int:
    gomod_path = recipe_dir / "go.mod"

    if not gomod_path.is_file():
        print(
            f"[SKIP] {gomod_path} does not exist — the required-files "
            f"check reports that separately."
        )
        return EXIT_OK

    try:
        content = gomod_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        return _report(
            [
                Diagnostic(
                    check="gomod-parse",
                    what=f"{gomod_path} is not valid UTF-8: {e}",
                    why=(
                        "The Go toolchain and this check require go.mod to be "
                        "valid UTF-8 text."
                    ),
                    how=f"Re-encode {gomod_path.name} as UTF-8.",
                    doc=Doc.PROJECT_NAME,
                    file=str(gomod_path),
                )
            ],
            recipe_dir,
        )

    parsed, parse_diags = parse_gomod(content, gomod_path)
    if parse_diags:
        return _report(parse_diags, recipe_dir)

    diagnostics: list[Diagnostic] = []
    diagnostics += check_module_path(parsed, gomod_path, recipe_dir)
    diagnostics += check_go_version(parsed, gomod_path)
    diagnostics += check_replaces(parsed, gomod_path)

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
