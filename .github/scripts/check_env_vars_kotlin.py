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
"""Checks that every environment variable read by a recipe's Kotlin source is
declared in the recipe's .env.example.

Detects calls to System.getenv(), System.getProperty(), env(), and dotenv reads,
including when followed by the Elvis operator (?:) or a default value argument.
Variable names constructed dynamically at runtime (e.g. System.getenv(myVar) or
System::getenv method references) are not matched statically.

An allowlist of well-known OS/CI variables suppresses false positives for
variables that legitimately do not belong in .env.example (HOME, PATH, CI,
GITHUB_*, etc.).

Usage: python3 check_env_vars_kotlin.py <recipe-dir>

Exit codes:
  0  every variable read by the recipe's Kotlin source is declared
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
from dataclasses import dataclass
from pathlib import Path

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

CHECKER = "check_env_vars_kotlin.py"
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
# Kotlin source tokenization and extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    kind: str  # "IDENT", "STRING", "DOT", "LPAREN", "RPAREN", "LBRACKET", "RBRACKET", "COMMA", "OTHER"
    value: str
    lineno: int


def _handle_string_interpolation(
    chars: list[str],
    start_line: int,
    lineno: int,
    tokens: list[_Token],
    mode_stack: list[tuple[str, int]],
) -> None:
    if chars:
        tokens.append(_Token("STRING", "".join(chars), start_line))
    tokens.append(_Token("DOLLAR_LBRACE", "${", lineno))
    mode_stack.append(("CODE", 0))


def _tokenize_kotlin(source: str) -> list[_Token]:
    """Tokenize Kotlin source text into relevant tokens for AST-like inspection.

    Handles single-line and nested block comments, string literals (standard and
    triple-quoted), string template interpolation (${...}), and char literals.
    """
    tokens: list[_Token] = []
    i = 0
    n = len(source)
    lineno = 1
    # Stack stores (mode, brace_depth) context
    mode_stack: list[tuple[str, int]] = [("CODE", 0)]

    while i < n:
        mode, depth = mode_stack[-1]

        if mode == "CODE":
            c = source[i]
            if c == "\n":
                lineno += 1
                i += 1
            elif c in " \t\r":
                i += 1
            elif source[i : i + 2] == "//":
                # Line comment
                i += 2
                while i < n and source[i] != "\n":
                    i += 1
            elif source[i : i + 2] == "/*":
                # Block comment (supports nesting in Kotlin)
                i += 2
                comment_depth = 1
                while i < n and comment_depth > 0:
                    if source[i : i + 2] == "/*":
                        comment_depth += 1
                        i += 2
                    elif source[i : i + 2] == "*/":
                        comment_depth -= 1
                        i += 2
                    elif source[i] == "\n":
                        lineno += 1
                        i += 1
                    else:
                        i += 1
            elif source[i : i + 3] == '"""':
                i += 3
                mode_stack.append(("TRIPLE_STRING", 0))
            elif c == '"':
                i += 1
                mode_stack.append(("STRING", 0))
            elif c == "'":
                # Char literal
                i += 1
                if i < n and source[i] == "\\":
                    i += 2
                elif i < n:
                    i += 1
                if i < n and source[i] == "'":
                    i += 1
            elif c == ".":
                tokens.append(_Token("DOT", ".", lineno))
                i += 1
            elif c == "(":
                tokens.append(_Token("LPAREN", "(", lineno))
                i += 1
            elif c == ")":
                tokens.append(_Token("RPAREN", ")", lineno))
                i += 1
            elif c == "[":
                tokens.append(_Token("LBRACKET", "[", lineno))
                i += 1
            elif c == "]":
                tokens.append(_Token("RBRACKET", "]", lineno))
                i += 1
            elif c == ",":
                tokens.append(_Token("COMMA", ",", lineno))
                i += 1
            elif c == "{":
                mode_stack[-1] = (mode, depth + 1)
                tokens.append(_Token("LBRACE", "{", lineno))
                i += 1
            elif c == "}":
                if depth > 0:
                    mode_stack[-1] = (mode, depth - 1)
                    tokens.append(_Token("RBRACE", "}", lineno))
                    i += 1
                elif len(mode_stack) > 1:
                    # Closing brace of ${...} inside string
                    mode_stack.pop()
                    tokens.append(_Token("RBRACE", "}", lineno))
                    i += 1
                else:
                    tokens.append(_Token("RBRACE", "}", lineno))
                    i += 1
            elif c.isalpha() or c == "_":
                start_line = lineno
                start = i
                while i < n and (source[i].isalnum() or source[i] == "_"):
                    i += 1
                tokens.append(_Token("IDENT", source[start:i], start_line))
            else:
                tokens.append(_Token("OTHER", c, lineno))
                i += 1

        elif mode == "STRING":
            # Inside "..."
            start_line = lineno
            chars: list[str] = []
            while i < n:
                if source[i] == "\n":
                    lineno += 1
                    chars.append("\n")
                    i += 1
                elif source[i : i + 2] == "\\\\":
                    chars.append("\\")
                    i += 2
                elif source[i : i + 2] == '\\"':
                    chars.append('"')
                    i += 2
                elif source[i : i + 2] == "\\$":
                    chars.append("$")
                    i += 2
                elif source[i : i + 2] == "\\n":
                    chars.append("\n")
                    i += 2
                elif source[i : i + 2] == "\\t":
                    chars.append("\t")
                    i += 2
                elif source[i : i + 2] == "\\r":
                    chars.append("\r")
                    i += 2
                elif source[i] == "\\":
                    i += 1
                    if i < n:
                        chars.append(source[i])
                        i += 1
                elif source[i : i + 2] == "${":
                    _handle_string_interpolation(
                        chars, start_line, lineno, tokens, mode_stack
                    )
                    i += 2
                    break
                elif source[i] == '"':
                    tokens.append(_Token("STRING", "".join(chars), start_line))
                    mode_stack.pop()
                    i += 1
                    break
                else:
                    chars.append(source[i])
                    i += 1

        elif mode == "TRIPLE_STRING":
            # Inside """..."""
            start_line = lineno
            chars: list[str] = []
            while i < n:
                if source[i : i + 3] == '"""':
                    tokens.append(_Token("STRING", "".join(chars), start_line))
                    mode_stack.pop()
                    i += 3
                    break
                elif source[i : i + 2] == "${":
                    _handle_string_interpolation(
                        chars, start_line, lineno, tokens, mode_stack
                    )
                    i += 2
                    break
                elif source[i] == "\n":
                    lineno += 1
                    chars.append("\n")
                    i += 1
                else:
                    chars.append(source[i])
                    i += 1

    return tokens


_DECL_KEYWORDS: frozenset[str] = frozenset(
    {"fun", "val", "var", "class", "object", "interface", "import", "package"}
)
_GET_METHODS: frozenset[str] = frozenset(
    {"get", "getOrNull", "getOrDefault", "getOrElse"}
)


def _is_call_site(tokens: list[_Token], idx: int) -> bool:
    """True if tokens[idx] is a call site and not preceded by a declaration keyword."""
    if idx <= 0:
        return True
    prev = tokens[idx - 1]
    return prev.kind != "IDENT" or prev.value not in _DECL_KEYWORDS


def _record_var(env_vars: dict[str, int], var_name: str, lineno: int) -> None:
    cleaned = var_name.strip()
    if cleaned:
        env_vars.setdefault(cleaned, lineno)


def _parse_kotlin_source(source_text: str) -> dict[str, int]:
    """Parse Kotlin source text and extract environment variable reads.

    Detects:
      - System.getenv("VAR")
      - System.getProperty("VAR") / System.getProperty("VAR", "default")
      - System.getenv()["VAR"] / System.getenv().get("VAR")
      - env("VAR")
      - dotenv["VAR"] / dotenv.get("VAR")
      - Dotenv.load()["VAR"] / Dotenv.load().get("VAR") / dotenv()["VAR"]

    Returns:
        dict mapping var_name -> line_number of the first read in the file.
    """
    tokens = _tokenize_kotlin(source_text)
    env_vars: dict[str, int] = {}
    n = len(tokens)

    for i in range(n):
        tok = tokens[i]
        if tok.kind != "IDENT":
            continue

        lineno = tok.lineno

        # 1. System.getenv(...) or System.getProperty(...)
        if tok.value == "System" and i + 4 < n:
            if (
                tokens[i + 1].kind == "DOT"
                and tokens[i + 2].kind == "IDENT"
                and tokens[i + 2].value in ("getenv", "getProperty")
            ):
                # System.getenv("VAR") or System.getProperty("VAR")
                if (
                    tokens[i + 3].kind == "LPAREN"
                    and tokens[i + 4].kind == "STRING"
                ):
                    _record_var(env_vars, tokens[i + 4].value, lineno)
                # System.getenv()["VAR"]
                elif (
                    tokens[i + 2].value == "getenv"
                    and i + 6 < n
                    and tokens[i + 3].kind == "LPAREN"
                    and tokens[i + 4].kind == "RPAREN"
                    and tokens[i + 5].kind == "LBRACKET"
                    and tokens[i + 6].kind == "STRING"
                ):
                    _record_var(env_vars, tokens[i + 6].value, lineno)
                # System.getenv().get("VAR")
                elif (
                    tokens[i + 2].value == "getenv"
                    and i + 8 < n
                    and tokens[i + 3].kind == "LPAREN"
                    and tokens[i + 4].kind == "RPAREN"
                    and tokens[i + 5].kind == "DOT"
                    and tokens[i + 6].kind == "IDENT"
                    and tokens[i + 6].value in _GET_METHODS
                    and tokens[i + 7].kind == "LPAREN"
                    and tokens[i + 8].kind == "STRING"
                ):
                    _record_var(env_vars, tokens[i + 8].value, lineno)

        # 2. env("VAR") function / parameter call
        elif tok.value == "env" and _is_call_site(tokens, i):
            if (
                i + 2 < n
                and tokens[i + 1].kind == "LPAREN"
                and tokens[i + 2].kind == "STRING"
            ):
                _record_var(env_vars, tokens[i + 2].value, lineno)

        # 3. dotenv["VAR"] / Dotenv["VAR"] / dotenv.get("VAR")
        elif tok.value in ("dotenv", "Dotenv") and _is_call_site(tokens, i):
            # dotenv["VAR"]
            if (
                i + 2 < n
                and tokens[i + 1].kind == "LBRACKET"
                and tokens[i + 2].kind == "STRING"
            ):
                _record_var(env_vars, tokens[i + 2].value, lineno)
            # dotenv.get("VAR")
            elif (
                i + 4 < n
                and tokens[i + 1].kind == "DOT"
                and tokens[i + 2].kind == "IDENT"
                and tokens[i + 2].value in _GET_METHODS
                and tokens[i + 3].kind == "LPAREN"
                and tokens[i + 4].kind == "STRING"
            ):
                _record_var(env_vars, tokens[i + 4].value, lineno)
            # dotenv()["VAR"]
            elif (
                i + 4 < n
                and tokens[i + 1].kind == "LPAREN"
                and tokens[i + 2].kind == "RPAREN"
                and tokens[i + 3].kind == "LBRACKET"
                and tokens[i + 4].kind == "STRING"
            ):
                _record_var(env_vars, tokens[i + 4].value, lineno)
            # dotenv().get("VAR")
            elif (
                i + 6 < n
                and tokens[i + 1].kind == "LPAREN"
                and tokens[i + 2].kind == "RPAREN"
                and tokens[i + 3].kind == "DOT"
                and tokens[i + 4].kind == "IDENT"
                and tokens[i + 4].value in _GET_METHODS
                and tokens[i + 5].kind == "LPAREN"
                and tokens[i + 6].kind == "STRING"
            ):
                _record_var(env_vars, tokens[i + 6].value, lineno)
            # Dotenv.load()["VAR"]
            elif (
                i + 6 < n
                and tokens[i + 1].kind == "DOT"
                and tokens[i + 2].kind == "IDENT"
                and tokens[i + 2].value == "load"
                and tokens[i + 3].kind == "LPAREN"
                and tokens[i + 4].kind == "RPAREN"
                and tokens[i + 5].kind == "LBRACKET"
                and tokens[i + 6].kind == "STRING"
            ):
                _record_var(env_vars, tokens[i + 6].value, lineno)
            # Dotenv.load().get("VAR")
            elif (
                i + 8 < n
                and tokens[i + 1].kind == "DOT"
                and tokens[i + 2].kind == "IDENT"
                and tokens[i + 2].value == "load"
                and tokens[i + 3].kind == "LPAREN"
                and tokens[i + 4].kind == "RPAREN"
                and tokens[i + 5].kind == "DOT"
                and tokens[i + 6].kind == "IDENT"
                and tokens[i + 6].value in _GET_METHODS
                and tokens[i + 7].kind == "LPAREN"
                and tokens[i + 8].kind == "STRING"
            ):
                _record_var(env_vars, tokens[i + 8].value, lineno)

    return env_vars


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
        ".gradle",
        "build",
        "target",
        "tests",
        "test",
        ".agent-tmp",
        ".git",
    }
)


def _unreadable_source(
    kt_file: Path,
    exc: Exception | None = None,
    lineno: int = 1,
    detail: str = "",
) -> Diagnostic:
    """A Kotlin file this check could not read is a hole in the check."""
    if isinstance(exc, UnicodeDecodeError):
        what = f"{kt_file} could not be decoded as UTF-8: {exc}."
        how = (
            "Re-save the file as UTF-8 (every Kotlin source file in this "
            "repo is UTF-8):\n"
            "  iconv -f <current-encoding> -t utf-8 <file> > <file>.utf8\n"
            "  mv <file>.utf8 <file>"
        )
    elif detail:
        what = f"{kt_file}:{lineno} is not valid Kotlin: {detail}."
        how = "Fix the syntax error, then re-run the build."
    elif exc is not None:
        what = f"{kt_file}:{lineno} could not be read as Kotlin source: {exc}."
        how = "Open the file and check it is plain UTF-8 Kotlin source."
    else:
        what = f"{kt_file}:{lineno} could not be read as Kotlin source."
        how = "Open the file and check it is plain UTF-8 Kotlin source."
    return Diagnostic(
        check=CHECK,
        what=what,
        why=(
            "This check scans every non-test Kotlin file in the recipe "
            "to find environment-variable reads. A file it cannot read is "
            "invisible to it, so a variable read there could be missing "
            "from .env.example and still pass CI."
        ),
        how=how,
        doc=Doc.ENV_VARS,
        file=str(kt_file),
    )


def _collect_used_vars(
    recipe_dir: Path,
) -> tuple[dict[str, tuple[Path, int]], list[Diagnostic]]:
    """Scan all non-test, non-build Kotlin files.

    Returns the variables read (name -> the file and line of the first read)
    and a diagnostic for every file that could not be parsed.
    """
    used: dict[str, tuple[Path, int]] = {}
    unreadable: list[Diagnostic] = []

    for kt_file in sorted(recipe_dir.rglob("*.kt")):
        rel = kt_file.relative_to(recipe_dir)
        if any(part in _EXCLUDED_DIRS for part in rel.parts):
            continue

        try:
            source_bytes = kt_file.read_bytes()
            source_text = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            unreadable.append(_unreadable_source(kt_file, exc=exc))
            continue
        except Exception as exc:
            unreadable.append(_unreadable_source(kt_file, exc=exc))
            continue

        try:
            vars_found = _parse_kotlin_source(source_text)
        except Exception as exc:
            unreadable.append(_unreadable_source(kt_file, exc=exc))
            continue

        for name, lineno in vars_found.items():
            used.setdefault(name, (kt_file, lineno))

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
            "and no explanation. The read was found by scanning the "
            "recipe's Kotlin source, so it is a real System.getenv / "
            "System.getProperty access."
        ),
        how=(
            f"Add this line to {env_example}:\n"
            f"  {var}={PLACEHOLDER}\n"
            f"and make sure the recipe loads it."
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
        f"{env_example}: no environment-variable reads detected in Kotlin source."
        if not used_vars
        else (
            f"{env_example}: every environment variable read in Kotlin "
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
