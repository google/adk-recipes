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
"""Checks that every environment variable read by a recipe's Go source is
declared in the recipe's .env.example.

Tree-sitter-based: understands os.Getenv() and os.LookupEnv(), including import
aliases (import goos "os") and dot imports (import . "os") regardless of how the
calls are formatted or split across lines. An allowlist of well-known OS/CI
variables suppresses false positives for variables that legitimately do not
belong in .env.example (HOME, PATH, CI, GITHUB_*, etc.).

Usage: python3 check_env_vars_go.py <recipe-dir>

Exit codes:
  0  every variable read by the recipe's Go source is declared
  1  contributor-fixable problems found; every one has been reported with
     a fix, both as a human block and as a ::error annotation
  2  CI fault — the checker crashed or was invoked wrongly. Never blamed
     on the contributor's files.

A missing .env.example is not this script's failure to report (a separate
required-files check owns it), so that case exits 0 with a note.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import tree_sitter
import tree_sitter_go

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

CHECKER = "check_env_vars_go.py"
CHECK = "env-vars"

PLACEHOLDER = "<TODO: update-this-value>"

# ---------------------------------------------------------------------------
# Allowlist
#
# Variables in this set (or matching a prefix below) are provided by the OS,
# the CI runtime, or the execution environment.  They should NOT appear in
# .env.example because their values vary per machine and are never
# recipe-specific secrets or configuration.  Adding a name here suppresses
# the FAIL that would otherwise fire when a recipe reads it but does not
# declare it.
#
# Keep this list conservative.  When in doubt, do NOT allowlist — let the
# recipe declare the variable and explain why it exists.
# ---------------------------------------------------------------------------
_ALLOWLIST: frozenset[str] = frozenset(
    {
        # POSIX core
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "PWD",
        "OLDPWD",
        "PATH",
        "TMPDIR",
        "TEMP",
        "TMP",
        # Locale / terminal
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "TERM",
        "TERM_PROGRAM",
        "COLORTERM",
        "TZ",
        "EDITOR",
        "VISUAL",
        "PAGER",
        # Common CI / test flags
        "CI",
        "CONTINUOUS_INTEGRATION",
        "DEBUG",
        "PORT",
        "HOST",
        "HOSTNAME",
        # ADK runnability-test sentinel — set by the test harness, not the
        # developer, so it should not appear in .env.example.
        "INTEGRATION_TEST",
    }
)

# Variable-name prefixes that are automatically allowed without being listed
# individually above.
_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "GITHUB_",  # GitHub Actions context variables (GITHUB_TOKEN, etc.)
    "RUNNER_",  # GitHub Actions runner variables
    "ACTIONS_",  # GitHub Actions built-ins
)


def _is_allowed(name: str) -> bool:
    return name in _ALLOWLIST or any(
        name.startswith(p) for p in _ALLOWLIST_PREFIXES
    )


# ---------------------------------------------------------------------------
# Tree-sitter Go AST parsing
# ---------------------------------------------------------------------------

_GO_LANGUAGE = tree_sitter.Language(tree_sitter_go.language())


def _node_text(node: tree_sitter.Node | None) -> str:
    """Decode a tree-sitter node's text as UTF-8."""
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _extract_string_literal(node: tree_sitter.Node) -> str | None:
    """Extract string value from an interpreted or raw string literal node."""
    raw = _node_text(node)
    if node.type == "interpreted_string_literal":
        if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
            # Basic unescaping of standard quotes and backslashes
            return raw[1:-1]
    elif node.type == "raw_string_literal":
        if len(raw) >= 2 and raw.startswith("`") and raw.endswith("`"):
            return raw[1:-1]
    return None


def _find_first_error_node(node: tree_sitter.Node) -> tree_sitter.Node | None:
    """Recursively search for the first syntax error node in the AST."""
    if node.is_error or node.is_missing:
        return node
    for child in node.children:
        err = _find_first_error_node(child)
        if err is not None:
            return err
    return None


def _parse_go_file(
    source_bytes: bytes,
) -> tuple[dict[str, int], list[tuple[int, str]]]:
    """Parse a single Go source file using tree-sitter.

    Returns:
        tuple of (env_vars_dict, errors_list) where env_vars_dict maps
        var_name -> line_number of the first read.
    """
    parser = tree_sitter.Parser(_GO_LANGUAGE)
    tree = parser.parse(source_bytes)
    root = tree.root_node

    if root.has_error:
        err_node = _find_first_error_node(root)
        lineno = (err_node.start_point.row + 1) if err_node else 1
        return {}, [(lineno, "syntax error")]

    os_aliases: set[str] = set()
    os_dot_imported = False

    def check_import_spec(spec_node: tree_sitter.Node) -> None:
        nonlocal os_dot_imported
        path_node = spec_node.child_by_field_name("path")
        name_node = spec_node.child_by_field_name("name")

        if path_node is None:
            for child in spec_node.children:
                if child.type in (
                    "interpreted_string_literal",
                    "raw_string_literal",
                ):
                    path_node = child
                elif child.type in (
                    "package_identifier",
                    "dot",
                    "blank_identifier",
                ):
                    name_node = child

        if path_node is None:
            return

        path_val = _extract_string_literal(path_node)
        if path_val != "os":
            return

        if name_node is None:
            os_aliases.add("os")
        elif name_node.type == "package_identifier":
            name = _node_text(name_node)
            if name:
                os_aliases.add(name)
        elif name_node.type == "dot" or name_node.text == b".":
            os_dot_imported = True

    env_vars: dict[str, int] = {}

    def visit(node: tree_sitter.Node) -> None:
        if node.type == "import_spec":
            check_import_spec(node)
        elif node.type == "call_expression":
            fn_node = node.child_by_field_name("function")
            if fn_node is None and node.children:
                fn_node = node.children[0]

            args_node = node.child_by_field_name("arguments")
            if args_node is None:
                for child in node.children:
                    if child.type == "argument_list":
                        args_node = child
                        break

            if fn_node is not None and args_node is not None:
                first_arg_str: str | None = None
                for arg in args_node.children:
                    if arg.type in (
                        "interpreted_string_literal",
                        "raw_string_literal",
                    ):
                        first_arg_str = _extract_string_literal(arg)
                        break
                    elif arg.type in ("comment", "(", ")", ","):
                        continue
                    else:
                        break

                if first_arg_str is not None:
                    is_env_call = False
                    if fn_node.type == "selector_expression":
                        operand = fn_node.child_by_field_name("operand")
                        if operand is None and fn_node.children:
                            operand = fn_node.children[0]
                        field = fn_node.child_by_field_name("field")
                        if field is None and len(fn_node.children) > 2:
                            field = fn_node.children[2]

                        op_name = _node_text(operand)
                        field_name = _node_text(field)
                        if op_name in os_aliases and field_name in (
                            "Getenv",
                            "LookupEnv",
                        ):
                            is_env_call = True
                    elif os_dot_imported and fn_node.type == "identifier":
                        fn_name = _node_text(fn_node)
                        if fn_name in ("Getenv", "LookupEnv"):
                            is_env_call = True

                    if is_env_call:
                        env_vars.setdefault(
                            first_arg_str, node.start_point.row + 1
                        )

        for child in node.children:
            visit(child)

    visit(root)
    return env_vars, []


# ---------------------------------------------------------------------------
# File-level helpers
# ---------------------------------------------------------------------------

_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "tests",
        "test",
        "vendor",
    }
)


def _unreadable_source(
    go_file: Path,
    exc: Exception | None = None,
    lineno: int = 1,
    detail: str = "",
) -> Diagnostic:
    """A Go file this check could not read or parse is a hole in the check."""
    if isinstance(exc, UnicodeDecodeError):
        what = f"{go_file} could not be decoded as UTF-8: {exc}."
        how = (
            "Re-save the file as UTF-8 (every Go source file in this "
            "repo is UTF-8):\n"
            "  iconv -f <current-encoding> -t utf-8 <file> > <file>.utf8\n"
            "  mv <file>.utf8 <file>"
        )
    elif detail:
        what = f"{go_file}:{lineno} is not valid Go: {detail}."
        how = "Fix the syntax error, then re-run:\n  go vet ./..."
    else:
        what = f"{go_file}:{lineno} could not be parsed as Go."
        how = "Fix the syntax error, then re-run:\n  go vet ./..."
    return Diagnostic(
        check=CHECK,
        what=what,
        why=(
            "This check parses every non-test Go file in the recipe "
            "to find environment-variable reads. A file it cannot parse is "
            "invisible to it, so a variable read there could be missing "
            "from .env.example and still pass CI."
        ),
        how=how,
        doc=Doc.ENV_VARS,
        file=str(go_file),
    )


def _collect_used_vars(
    recipe_dir: Path,
) -> tuple[dict[str, tuple[Path, int]], list[Diagnostic]]:
    """Parse all non-test, non-vendor Go files.

    Returns the variables read (name -> the file and line of the first read)
    and a diagnostic for every file that could not be parsed.
    """
    used: dict[str, tuple[Path, int]] = {}
    unreadable: list[Diagnostic] = []

    for go_file in sorted(recipe_dir.rglob("*.go")):
        if any(part in _EXCLUDED_DIRS for part in go_file.parts):
            continue
        if go_file.name.endswith("_test.go"):
            continue

        try:
            source_bytes = go_file.read_bytes()
            source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            unreadable.append(_unreadable_source(go_file, exc=exc))
            continue

        try:
            vars_found, parse_errors = _parse_go_file(source_bytes)
        except Exception as exc:
            unreadable.append(_unreadable_source(go_file, exc=exc))
            continue

        if parse_errors:
            for lineno, detail in parse_errors:
                unreadable.append(
                    _unreadable_source(go_file, lineno=lineno, detail=detail)
                )
            continue

        for name, lineno in vars_found.items():
            used.setdefault(name, (go_file, lineno))

    return used, unreadable


def _parse_env_example(
    env_example: Path,
) -> tuple[set[str], Diagnostic | None]:
    """Return the variable names declared in .env.example.

    A file that is not UTF-8 yields no names and a diagnostic: the encoding
    is the contributor's to fix, and comparing against an empty set would
    otherwise report every variable in the recipe as undeclared.
    """
    defined: set[str] = set()
    try:
        text = env_example.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return defined, Diagnostic(
            check=CHECK,
            what=f"{env_example} is not valid UTF-8: {exc}.",
            why=(
                "The declarations in .env.example are read as UTF-8 by this "
                "check, by godotenv at runtime, and by every editor "
                "that opens the file. A byte that is not valid UTF-8 makes "
                "the file unreadable to all three."
            ),
            how=(
                "Re-save the file as UTF-8:\n"
                "  iconv -f <current-encoding> -t utf-8 .env.example "
                "> .env.example.utf8\n"
                "  mv .env.example.utf8 .env.example\n"
                "Values in .env.example are placeholders, so plain ASCII is "
                "usually the simplest fix."
            ),
            doc=Doc.ENV_VARS,
            file=str(env_example),
        )
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=", line)
        if m:
            defined.add(m.group(1))
    return defined, None


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def _undeclared_var(
    var: str, source: Path, lineno: int, env_example: Path
) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=(
            f"{var} is read at {source}:{lineno} but is not declared in "
            f"{env_example}."
        ),
        why=(
            ".env.example is the only place someone running this recipe can "
            "discover what they have to configure; a variable that is read "
            "but not listed there makes the recipe fail with an empty value "
            "and no explanation. The read was found by parsing the "
            "recipe's source, so it is a real os.Getenv/os.LookupEnv access, "
            "not a string match."
        ),
        how=(
            f"Add this line to {env_example}:\n"
            f"  {var}={PLACEHOLDER}\n"
            f"and make sure the recipe loads it (godotenv.Load() in main.go)."
        ),
        doc=Doc.ENV_VARS,
        file=str(env_example),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _run(recipe_dir: Path) -> int:
    env_example = recipe_dir / ".env.example"
    if not env_example.is_file():
        # A separate required-files check already reports this; staying
        # quiet here keeps one missing file from producing two errors.
        print(
            f"[SKIP] {env_example} does not exist — the required-files "
            f"check reports that separately."
        )
        return EXIT_OK

    defined_vars, encoding_problem = _parse_env_example(env_example)
    used_vars, diagnostics = _collect_used_vars(recipe_dir)

    if encoding_problem is not None:
        # Without a readable .env.example every variable would look
        # undeclared, so report the encoding and stop comparing.
        diagnostics.insert(0, encoding_problem)
    else:
        for var, (source, lineno) in sorted(used_vars.items()):
            if var in defined_vars or _is_allowed(var):
                continue
            diagnostics.append(
                _undeclared_var(var, source, lineno, env_example)
            )

    n_checked = len(used_vars)
    n_allowed = sum(1 for v in used_vars if _is_allowed(v))
    passed_message = (
        f"{env_example}: no environment-variable reads detected in Go source."
        if not used_vars
        else (
            f"{env_example}: every environment variable read in Go "
            f"source is declared ({n_checked} detected, {n_allowed} in the "
            f"OS allowlist, {n_checked - n_allowed} declared)."
        )
    )
    return report(
        diagnostics,
        header=f"{recipe_dir}: environment variables",
        passed_message=passed_message,
        next_step=(
            "Declaring a variable does not mean committing a secret: "
            f"'{PLACEHOLDER}' is a valid value. The file exists to say "
            "WHICH variables the recipe needs."
        ),
    )


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
