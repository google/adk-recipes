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
"""Checks that every environment variable read by a recipe's TypeScript source is
declared in the recipe's .env.example.

Tree-sitter-based: understands process.env.VAR, process.env["VAR"],
import.meta.env.VAR, destructuring (const { VAR } = process.env), and default
fallbacks (process.env.VAR ?? "default", process.env.VAR || "default").
An allowlist of well-known OS/CI variables suppresses false positives for
variables that legitimately do not belong in .env.example (HOME, PATH, CI,
GITHUB_*, etc.).

Usage: python3 check_env_vars_typescript.py <recipe-dir>

Exit codes:
  0  every variable read by the recipe's TypeScript source is declared
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
import tree_sitter_typescript

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

CHECKER = "check_env_vars_typescript.py"
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
        # Node / JS runtime flags
        "NODE_ENV",
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
# Tree-sitter TypeScript AST parsing
# ---------------------------------------------------------------------------

_TS_LANGUAGE = tree_sitter.Language(
    tree_sitter_typescript.language_typescript()
)
_TSX_LANGUAGE = tree_sitter.Language(tree_sitter_typescript.language_tsx())


def _node_text(node: tree_sitter.Node | None) -> str:
    """Decode a tree-sitter node's text as UTF-8."""
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _extract_string_literal(node: tree_sitter.Node) -> str | None:
    """Extract string value from a string or template_string literal node."""
    if node.type == "string":
        raw = _node_text(node)
        if len(raw) >= 2 and (
            (raw.startswith('"') and raw.endswith('"'))
            or (raw.startswith("'") and raw.endswith("'"))
        ):
            return raw[1:-1]
    elif node.type == "template_string":
        for child in node.children:
            if child.type == "template_substitution":
                return None
        raw = _node_text(node)
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


def _is_env_object(node: tree_sitter.Node | None) -> bool:
    """True if node represents process.env or import.meta.env."""
    if node is None:
        return False
    if node.type != "member_expression":
        return False
    obj = node.child_by_field_name("object")
    prop = node.child_by_field_name("property")
    if obj is None or prop is None:
        return False
    if _node_text(prop) != "env":
        return False

    # 1. process.env (or process?.env)
    if obj.type == "identifier" and _node_text(obj) == "process":
        return True

    # 2. import.meta.env (or import.meta?.env)
    if obj.type == "meta_property" and _node_text(obj) == "import.meta":
        return True

    return False


def _extract_default_value(node: tree_sitter.Node | None) -> str | None:
    """Extract a default value from an AST node (string literal or raw text)."""
    if node is None:
        return None
    str_val = _extract_string_literal(node)
    if str_val is not None:
        return str_val
    return _node_text(node).strip() or None


def _extract_default_from_parent(node: tree_sitter.Node) -> str | None:
    """Extract fallback default if the read is part of a `??` or `||` binary expression."""
    parent = node.parent
    while parent and parent.type in (
        "parenthesized_expression",
        "non_null_expression",
        "as_expression",
        "type_assertion",
    ):
        parent = parent.parent

    if parent and parent.type == "binary_expression":
        left = parent.child_by_field_name("left")
        op = parent.child_by_field_name("operator")
        right = parent.child_by_field_name("right")

        curr: tree_sitter.Node | None = node
        while curr and left not in (curr, curr.parent):
            curr = curr.parent

        if (
            curr is not None
            and op is not None
            and _node_text(op) in ("??", "||")
            and right is not None
        ):
            return _extract_default_value(right)

    return None


def _extract_from_object_pattern(
    pattern_node: tree_sitter.Node,
) -> list[tuple[str, int, str | None]]:
    """Extract (var_name, lineno, default_val) from an object destructuring pattern."""
    extracted: list[tuple[str, int, str | None]] = []
    for child in pattern_node.children:
        if child.type == "shorthand_property_identifier_pattern":
            var_name = _node_text(child)
            extracted.append((var_name, child.start_point.row + 1, None))
        elif child.type == "object_assignment_pattern":
            left = child.child_by_field_name("left")
            right = child.child_by_field_name("right")
            if (
                left is not None
                and left.type == "shorthand_property_identifier_pattern"
            ):
                var_name = _node_text(left)
                def_val = _extract_default_value(right)
                extracted.append((var_name, left.start_point.row + 1, def_val))
        elif child.type == "pair_pattern":
            key = child.child_by_field_name("key")
            val = child.child_by_field_name("value")
            var_name = None
            if key is not None:
                if key.type in ("property_identifier", "identifier"):
                    var_name = _node_text(key)
                elif key.type in ("string", "template_string"):
                    var_name = _extract_string_literal(key)

            if var_name and key is not None:
                def_val = None
                if val is not None and val.type == "assignment_pattern":
                    right = val.child_by_field_name("right")
                    def_val = _extract_default_value(right)
                extracted.append((var_name, key.start_point.row + 1, def_val))
    return extracted


def _parse_typescript_file(
    source_bytes: bytes,
    is_tsx: bool = False,
) -> tuple[dict[str, tuple[int, str | None]], list[tuple[int, str]]]:
    """Parse a single TypeScript/JavaScript source file using tree-sitter.

    Returns:
        tuple of (env_vars_dict, errors_list) where env_vars_dict maps
        var_name -> (line_number, default_val).
    """
    lang = _TSX_LANGUAGE if is_tsx else _TS_LANGUAGE
    parser = tree_sitter.Parser(lang)
    tree = parser.parse(source_bytes)
    root = tree.root_node

    if root.has_error and not is_tsx:
        # Retry with TSX grammar in case the file contains JSX syntax
        parser_tsx = tree_sitter.Parser(_TSX_LANGUAGE)
        tree_tsx = parser_tsx.parse(source_bytes)
        if not tree_tsx.root_node.has_error:
            tree = tree_tsx
            root = tree.root_node

    if root.has_error:
        err_node = _find_first_error_node(root)
        lineno = (err_node.start_point.row + 1) if err_node else 1
        return {}, [(lineno, "syntax error")]

    env_vars: dict[str, tuple[int, str | None]] = {}

    def record_var(name: str, lineno: int, def_val: str | None) -> None:
        cleaned = name.strip()
        if not cleaned:
            return
        if cleaned not in env_vars:
            env_vars[cleaned] = (lineno, def_val)
        elif env_vars[cleaned][1] is None and def_val is not None:
            # Upgrade to include default and point to the read site carrying it
            env_vars[cleaned] = (lineno, def_val)

    def visit(node: tree_sitter.Node) -> None:
        if node.type == "member_expression":
            obj = node.child_by_field_name("object")
            prop = node.child_by_field_name("property")
            if obj is not None and _is_env_object(obj) and prop is not None:
                if prop.type in ("property_identifier", "identifier"):
                    var_name = _node_text(prop)
                    def_val = _extract_default_from_parent(node)
                    record_var(var_name, node.start_point.row + 1, def_val)
        elif node.type == "subscript_expression":
            obj = node.child_by_field_name("object")
            index = node.child_by_field_name("index")
            if obj is not None and _is_env_object(obj) and index is not None:
                var_name = _extract_string_literal(index)
                if var_name:
                    def_val = _extract_default_from_parent(node)
                    record_var(var_name, node.start_point.row + 1, def_val)
        elif node.type in (
            "variable_declarator",
            "assignment_expression",
            "required_parameter",
            "optional_parameter",
        ):
            pattern = None
            val = None
            if node.type == "variable_declarator":
                pattern = node.child_by_field_name("name")
                val = node.child_by_field_name("value")
            elif node.type == "assignment_expression":
                pattern = node.child_by_field_name("left")
                val = node.child_by_field_name("right")
            elif node.type in ("required_parameter", "optional_parameter"):
                pattern = node.child_by_field_name("pattern")
                val = node.child_by_field_name("value")

            if (
                pattern is not None
                and pattern.type == "object_pattern"
                and val is not None
                and _is_env_object(val)
            ):
                for v, line, d in _extract_from_object_pattern(pattern):
                    record_var(v, line, d)

        for child in node.children:
            visit(child)

    visit(root)
    return env_vars, []


# ---------------------------------------------------------------------------
# File-level helpers
# ---------------------------------------------------------------------------

_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        "out",
        ".next",
        ".nuxt",
        ".output",
        "coverage",
        "tests",
        "test",
        "__tests__",
        ".agent-tmp",
        ".git",
    }
)

_SOURCE_EXTENSIONS: tuple[str, ...] = (
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".mts",
    ".cts",
)


def _is_test_or_ignored_file(path: Path) -> bool:
    name = path.name.lower()
    for ext in _SOURCE_EXTENSIONS:
        if name.endswith(ext):
            base = name[: -len(ext)]
            if (
                base.endswith(".test")
                or base.endswith(".spec")
                or base.endswith("_test")
                or base.endswith("_spec")
            ):
                return True
    return name.endswith(".d.ts")


def _unreadable_source(
    ts_file: Path,
    exc: Exception | None = None,
    lineno: int = 1,
    detail: str = "",
) -> Diagnostic:
    """A TypeScript file this check could not read or parse is a hole in the check."""
    if isinstance(exc, UnicodeDecodeError):
        what = f"{ts_file} could not be decoded as UTF-8: {exc}."
        how = (
            "Re-save the file as UTF-8 (every TypeScript source file in this "
            "repo is UTF-8):\n"
            "  iconv -f <current-encoding> -t utf-8 <file> > <file>.utf8\n"
            "  mv <file>.utf8 <file>"
        )
    elif detail:
        what = f"{ts_file}:{lineno} is not valid TypeScript: {detail}."
        how = "Fix the syntax error, then re-run the TypeScript typecheck or build."
    else:
        what = f"{ts_file}:{lineno} could not be parsed as TypeScript."
        how = "Fix the syntax error, then re-run the TypeScript typecheck or build."
    return Diagnostic(
        check=CHECK,
        what=what,
        why=(
            "This check parses every non-test TypeScript/JavaScript file in the "
            "recipe to find environment-variable reads. A file it cannot parse is "
            "invisible to it, so a variable read there could be missing "
            "from .env.example and still pass CI."
        ),
        how=how,
        doc=Doc.ENV_VARS,
        file=str(ts_file),
    )


def _collect_used_vars(
    recipe_dir: Path,
) -> tuple[dict[str, tuple[Path, int, str | None]], list[Diagnostic]]:
    """Parse all non-test, non-build TypeScript/JavaScript files.

    Returns the variables read (name -> (file, line, default_val))
    and a diagnostic for every file that could not be parsed.
    """
    used: dict[str, tuple[Path, int, str | None]] = {}
    unreadable: list[Diagnostic] = []

    for file_path in sorted(recipe_dir.rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(recipe_dir)
        if any(part in _EXCLUDED_DIRS for part in rel.parts):
            continue
        if file_path.suffix.lower() not in _SOURCE_EXTENSIONS:
            continue
        if _is_test_or_ignored_file(file_path):
            continue

        try:
            source_bytes = file_path.read_bytes()
            source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            unreadable.append(_unreadable_source(file_path, exc=exc))
            continue

        is_tsx = file_path.suffix.lower() in (".tsx", ".jsx")
        try:
            vars_found, parse_errors = _parse_typescript_file(
                source_bytes, is_tsx=is_tsx
            )
        except Exception as exc:
            unreadable.append(_unreadable_source(file_path, exc=exc))
            continue

        if parse_errors:
            for lineno, detail in parse_errors:
                unreadable.append(
                    _unreadable_source(file_path, lineno=lineno, detail=detail)
                )
            continue

        for name, (lineno, def_val) in vars_found.items():
            if name not in used:
                used[name] = (file_path, lineno, def_val)
            elif used[name][2] is None and def_val is not None:
                used[name] = (file_path, lineno, def_val)

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
                "check, by dotenv loaders at runtime, and by every editor "
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
    var: str,
    source: Path,
    lineno: int,
    env_example: Path,
    default_val: str | None = None,
) -> Diagnostic:
    if default_val is not None:
        what = (
            f"{var} is read at {source}:{lineno} (default: {default_val!r}) "
            f"but is not declared in {env_example}."
        )
        how = (
            f"Add this line to {env_example}:\n"
            f"  {var}={default_val}\n"
            f"and make sure the recipe loads it (e.g. via dotenv or ADK)."
        )
    else:
        what = (
            f"{var} is read at {source}:{lineno} but is not declared in "
            f"{env_example}."
        )
        how = (
            f"Add this line to {env_example}:\n"
            f"  {var}={PLACEHOLDER}\n"
            f"and make sure the recipe loads it (e.g. via dotenv or ADK)."
        )
    return Diagnostic(
        check=CHECK,
        what=what,
        why=(
            ".env.example is the only place someone running this recipe can "
            "discover what they have to configure; a variable that is read "
            "but not listed there makes the recipe fail with an empty value "
            "and no explanation. The read was found by parsing the "
            "recipe's TypeScript source, so it is a real process.env / "
            "import.meta.env access, not a string match."
        ),
        how=how,
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
        for var, (source, lineno, def_val) in sorted(used_vars.items()):
            if var in defined_vars or _is_allowed(var):
                continue
            diagnostics.append(
                _undeclared_var(var, source, lineno, env_example, def_val)
            )

    n_checked = len(used_vars)
    n_allowed = sum(1 for v in used_vars if _is_allowed(v))
    passed_message = (
        f"{env_example}: no environment-variable reads detected in "
        f"TypeScript source."
        if not used_vars
        else (
            f"{env_example}: every environment variable read in TypeScript "
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
