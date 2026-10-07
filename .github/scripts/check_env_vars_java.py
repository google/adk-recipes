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
"""Checks that every environment variable read by a recipe's Java source is
declared in the recipe's .env.example.

Detects calls to:
  - System.getenv("VAR")
  - System.getenv().get("VAR")
  - System.getProperty("VAR") and System.getProperty("VAR", "default")
  - env("VAR") / requireEnv("VAR") / getEnv("VAR")
  - Dotenv / dotenv / DOTENV .get("VAR")

An allowlist of well-known OS/CI variables suppresses false positives for
variables that legitimately do not belong in .env.example (HOME, PATH, CI,
GITHUB_*, etc.).

Usage: python3 check_env_vars_java.py <recipe-dir>

Exit codes:
  0  every variable read by the recipe's Java source is declared
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

CHECKER = "check_env_vars_java.py"
CHECK = "env-vars"

PLACEHOLDER = "<TODO: update-this-value>"

# ---------------------------------------------------------------------------
# Allowlist
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
        # ADK runnability-test sentinel
        "INTEGRATION_TEST",
    }
)

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
# Java source tokenization and extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    kind: str  # "IDENT", "STRING", "DOT", "LPAREN", "RPAREN", "LBRACKET", "RBRACKET", "COMMA", "OTHER"
    value: str
    lineno: int


def _tokenize_java(source: str) -> list[_Token]:
    """Tokenize Java source text into tokens for AST-like inspection."""
    tokens: list[_Token] = []
    i = 0
    n = len(source)
    lineno = 1

    while i < n:
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
            # Block comment (Java does not nest block comments)
            i += 2
            while i < n and source[i : i + 2] != "*/":
                if source[i] == "\n":
                    lineno += 1
                i += 1
            if i < n:
                i += 2
        elif source[i : i + 3] == '"""':
            # Text block (Java 15+)
            start_line = lineno
            i += 3
            chars: list[str] = []
            while i < n and source[i : i + 3] != '"""':
                if source[i] == "\n":
                    lineno += 1
                    chars.append("\n")
                    i += 1
                elif source[i : i + 2] == '\\"':
                    chars.append('"')
                    i += 2
                elif source[i : i + 2] == "\\\\":
                    chars.append("\\")
                    i += 2
                else:
                    chars.append(source[i])
                    i += 1
            if i < n:
                i += 3
            tokens.append(_Token("STRING", "".join(chars), start_line))
        elif c == '"':
            # String literal
            start_line = lineno
            i += 1
            chars = []
            while i < n and source[i] != '"':
                if source[i] == "\n":
                    lineno += 1
                    chars.append("\n")
                    i += 1
                elif source[i : i + 2] == '\\"':
                    chars.append('"')
                    i += 2
                elif source[i : i + 2] == "\\\\":
                    chars.append("\\")
                    i += 2
                elif source[i] == "\\":
                    i += 1
                    if i < n:
                        chars.append(source[i])
                        i += 1
                else:
                    chars.append(source[i])
                    i += 1
            if i < n:
                i += 1
            tokens.append(_Token("STRING", "".join(chars), start_line))
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
        elif c.isalpha() or c in ("_", "$"):
            start_line = lineno
            start = i
            while i < n and (source[i].isalnum() or source[i] in ("_", "$")):
                i += 1
            tokens.append(_Token("IDENT", source[start:i], start_line))
        else:
            tokens.append(_Token("OTHER", c, lineno))
            i += 1

    return tokens


_DECL_KEYWORDS: frozenset[str] = frozenset(
    {
        "class",
        "interface",
        "enum",
        "record",
        "import",
        "package",
        "public",
        "private",
        "protected",
        "static",
        "final",
        "void",
        "String",
        "int",
        "boolean",
    }
)

_ENV_HELPER_FUNCTIONS: frozenset[str] = frozenset(
    {"env", "requireEnv", "getEnv", "getenv"}
)

_DOTENV_NAMES: frozenset[str] = frozenset({"Dotenv", "dotenv", "DOTENV"})


@dataclass(frozen=True)
class VarRead:
    name: str
    lineno: int
    is_property: bool = False


def _record_var(
    env_vars: dict[str, VarRead],
    var_name: str,
    lineno: int,
    is_property: bool = False,
) -> None:
    cleaned = var_name.strip()
    if cleaned and cleaned not in env_vars:
        env_vars[cleaned] = VarRead(cleaned, lineno, is_property=is_property)


def _is_call_site(tokens: list[_Token], idx: int) -> bool:
    if idx <= 0:
        return True
    prev = tokens[idx - 1]
    return prev.kind != "IDENT" or prev.value not in _DECL_KEYWORDS


def _parse_java_source(source_text: str) -> dict[str, VarRead]:
    """Parse Java source text and extract environment variable reads."""
    tokens = _tokenize_java(source_text)
    env_vars: dict[str, VarRead] = {}
    n = len(tokens)

    for i in range(n):
        tok = tokens[i]
        if tok.kind != "IDENT":
            continue

        lineno = tok.lineno

        # 1. System.getenv(...) or System.getProperty(...)
        if tok.value == "System" and i + 4 < n:
            if tokens[i + 1].kind == "DOT" and tokens[i + 2].kind == "IDENT":
                method = tokens[i + 2].value
                # System.getenv("VAR")
                if (
                    method == "getenv"
                    and tokens[i + 3].kind == "LPAREN"
                    and tokens[i + 4].kind == "STRING"
                ):
                    _record_var(
                        env_vars, tokens[i + 4].value, lineno, is_property=False
                    )
                # System.getenv().get("VAR")
                elif (
                    method == "getenv"
                    and i + 8 < n
                    and tokens[i + 3].kind == "LPAREN"
                    and tokens[i + 4].kind == "RPAREN"
                    and tokens[i + 5].kind == "DOT"
                    and tokens[i + 6].kind == "IDENT"
                    and tokens[i + 6].value in ("get", "getOrDefault")
                    and tokens[i + 7].kind == "LPAREN"
                    and tokens[i + 8].kind == "STRING"
                ):
                    _record_var(
                        env_vars, tokens[i + 8].value, lineno, is_property=False
                    )
                # System.getProperty("VAR") or System.getProperty("VAR", "default")
                elif (
                    method == "getProperty"
                    and tokens[i + 3].kind == "LPAREN"
                    and tokens[i + 4].kind == "STRING"
                ):
                    _record_var(
                        env_vars, tokens[i + 4].value, lineno, is_property=True
                    )

        # 2. env("VAR") / requireEnv("VAR") / getEnv("VAR") / getenv("VAR")
        elif tok.value in _ENV_HELPER_FUNCTIONS and _is_call_site(tokens, i):
            if (
                i + 2 < n
                and tokens[i + 1].kind == "LPAREN"
                and tokens[i + 2].kind == "STRING"
            ):
                _record_var(
                    env_vars, tokens[i + 2].value, lineno, is_property=False
                )

        # 3. Dotenv / dotenv / DOTENV .get("VAR")
        elif tok.value in _DOTENV_NAMES and _is_call_site(tokens, i):
            # Scan forward in the Dotenv chain for .get("VAR")
            j = i + 1
            while j + 3 < n and tokens[j].kind in (
                "DOT",
                "IDENT",
                "LPAREN",
                "RPAREN",
            ):
                if (
                    tokens[j].kind == "DOT"
                    and tokens[j + 1].kind == "IDENT"
                    and tokens[j + 1].value in ("get", "getOrDefault")
                    and tokens[j + 2].kind == "LPAREN"
                    and tokens[j + 3].kind == "STRING"
                ):
                    _record_var(
                        env_vars, tokens[j + 3].value, lineno, is_property=False
                    )
                    break
                j += 1

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
        ".agent-tmp",
        ".git",
    }
)


def _is_test_path(path: Path, recipe_dir: Path) -> bool:
    """Return True if path is under a test directory or is a test file."""
    rel = path.relative_to(recipe_dir)
    for part in rel.parts:
        if part in _EXCLUDED_DIRS or "test" in part.lower():
            return True
    name = path.name
    return (
        name.endswith("Test.java")
        or name.endswith("Tests.java")
        or name.endswith("TestCase.java")
    )


def _unreadable_source(
    java_file: Path,
    exc: Exception | None = None,
    lineno: int = 1,
    detail: str = "",
) -> Diagnostic:
    if isinstance(exc, UnicodeDecodeError):
        what = f"{java_file} could not be decoded as UTF-8: {exc}."
        how = (
            "Re-save the file as UTF-8 (every Java source file in this "
            "repo is UTF-8):\n"
            "  iconv -f <current-encoding> -t utf-8 <file> > <file>.utf8\n"
            "  mv <file>.utf8 <file>"
        )
    elif detail:
        what = f"{java_file}:{lineno} is not valid Java: {detail}."
        how = "Fix the syntax error, then re-run the build."
    elif exc is not None:
        what = f"{java_file}:{lineno} could not be read as Java source: {exc}."
        how = "Open the file and check it is plain UTF-8 Java source."
    else:
        what = f"{java_file}:{lineno} could not be read as Java source."
        how = "Open the file and check it is plain UTF-8 Java source."
    return Diagnostic(
        check=CHECK,
        what=what,
        why=(
            "This check scans every non-test Java file in the recipe "
            "to find environment-variable reads. A file it cannot read is "
            "invisible to it, so a variable read there could be missing "
            "from .env.example and still pass CI."
        ),
        how=how,
        doc=Doc.ENV_VARS,
        file=str(java_file),
    )


def _collect_used_vars(
    recipe_dir: Path,
) -> tuple[dict[str, tuple[Path, int, bool]], list[Diagnostic]]:
    """Scan all non-test, non-build Java files.

    Returns the variables read (name -> (file, line, is_property))
    and a diagnostic for every file that could not be parsed.
    """
    used: dict[str, tuple[Path, int, bool]] = {}
    unreadable: list[Diagnostic] = []

    for java_file in sorted(recipe_dir.rglob("*.java")):
        if _is_test_path(java_file, recipe_dir):
            continue

        try:
            source_bytes = java_file.read_bytes()
            source_text = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            unreadable.append(_unreadable_source(java_file, exc=exc))
            continue
        except Exception as exc:
            unreadable.append(_unreadable_source(java_file, exc=exc))
            continue

        try:
            vars_found = _parse_java_source(source_text)
        except Exception as exc:
            unreadable.append(_unreadable_source(java_file, exc=exc))
            continue

        for name, var_read in vars_found.items():
            if name not in used:
                used[name] = (java_file, var_read.lineno, var_read.is_property)

    return used, unreadable


def _parse_env_example(
    env_example: Path,
) -> tuple[set[str], Diagnostic | None]:
    """Return the variable names declared in .env.example."""
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
    var: str, source: Path, lineno: int, env_example: Path, is_property: bool
) -> Diagnostic:
    if is_property:
        why = (
            ".env.example is the only place someone running this recipe can "
            "discover what they have to configure; a variable that is read "
            "but not listed there makes the recipe fail with an empty value "
            "and no explanation. The read was found by scanning the "
            "recipe's Java source for System.getProperty access (note that "
            "while a system property is not strictly an environment variable, "
            "recipes often mirror environment variables into properties)."
        )
    else:
        why = (
            ".env.example is the only place someone running this recipe can "
            "discover what they have to configure; a variable that is read "
            "but not listed there makes the recipe fail with an empty value "
            "and no explanation. The read was found by scanning the "
            "recipe's Java source, so it is a real System.getenv / Dotenv "
            "access."
        )

    return Diagnostic(
        check=CHECK,
        what=(
            f"{var} is read at {source}:{lineno} but is not declared in "
            f"{env_example}."
        ),
        why=why,
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
        print(
            f"[SKIP] {env_example} does not exist — the required-files "
            f"check reports that separately."
        )
        return EXIT_OK

    defined_vars, encoding_problem = _parse_env_example(env_example)
    used_vars, diagnostics = _collect_used_vars(recipe_dir)

    if encoding_problem is not None:
        diagnostics.insert(0, encoding_problem)
    else:
        for var, (source, lineno, is_property) in sorted(used_vars.items()):
            if var in defined_vars or _is_allowed(var):
                continue
            diagnostics.append(
                _undeclared_var(var, source, lineno, env_example, is_property)
            )

    return report(
        diagnostics,
        header=f"{recipe_dir}: environment variable declarations",
        passed_message=(
            f"{env_example}: every environment variable read by this "
            f"recipe is declared."
        ),
        next_step=(
            "Add the missing variable(s) to .env.example with a placeholder "
            f"value like `{PLACEHOLDER}`."
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
