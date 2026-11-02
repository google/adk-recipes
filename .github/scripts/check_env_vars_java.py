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

Token-based scanner: understands System.getenv(), System.getenv().get(),
System.getProperty(), static imports (import static java.lang.System.getenv),
and Dotenv library reads, regardless of how the calls are formatted or split
across lines. An allowlist of well-known OS/CI variables suppresses false
positives for variables that legitimately do not belong in .env.example
(HOME, PATH, CI, GITHUB_*, etc.).

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
_DOTENV_LOOKAHEAD_LIMIT = 20

_PUNCTUATION: dict[str, str] = {
    ".": "DOT",
    "(": "LPAREN",
    ")": "RPAREN",
    "[": "LBRACKET",
    "]": "RBRACKET",
    ",": "COMMA",
    ";": "SEMI",
    "{": "LBRACE",
    "}": "RBRACE",
    "=": "EQUALS",
}

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
# Java source tokenization and extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    kind: str  # "IDENT", "STRING", "DOT", "LPAREN", "RPAREN", "COMMA", "SEMI", etc.
    value: str
    lineno: int


def _tokenize_java(source: str) -> list[_Token]:
    """Tokenize Java source text into relevant tokens for AST inspection.

    Handles single-line and block comments, string literals (standard and
    text blocks), char literals, identifiers, and punctuation.
    """
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
            i += 2
            while i < n and source[i] != "\n":
                i += 1
        elif source[i : i + 2] == "/*":
            start_line = lineno
            i += 2
            while i < n and source[i : i + 2] != "*/":
                if source[i] == "\n":
                    lineno += 1
                i += 1
            if i >= n:
                raise ValueError(
                    f"unclosed block comment starting at line {start_line}"
                )
            i += 2
        elif source[i : i + 3] == '"""':
            start_line = lineno
            i += 3
            chars: list[str] = []
            while i < n and source[i : i + 3] != '"""':
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
                elif source[i : i + 2] == "\\n":
                    chars.append("\n")
                    i += 2
                elif source[i : i + 2] == "\\t":
                    chars.append("\t")
                    i += 2
                else:
                    chars.append(source[i])
                    i += 1
            if i >= n:
                raise ValueError(
                    f"unclosed text block starting at line {start_line}"
                )
            i += 3
            tokens.append(_Token("STRING", "".join(chars).strip(), start_line))
        elif c == '"':
            start_line = lineno
            i += 1
            chars = []
            while i < n and source[i] != '"':
                if source[i] == "\n":
                    raise ValueError(
                        f"unclosed string literal at line {start_line}"
                    )
                elif source[i : i + 2] == "\\\\":
                    chars.append("\\")
                    i += 2
                elif source[i : i + 2] == '\\"':
                    chars.append('"')
                    i += 2
                elif source[i : i + 2] == "\\n":
                    chars.append("\n")
                    i += 2
                elif source[i : i + 2] == "\\t":
                    chars.append("\t")
                    i += 2
                else:
                    chars.append(source[i])
                    i += 1
            if i >= n:
                raise ValueError(
                    f"unclosed string literal at line {start_line}"
                )
            i += 1
            tokens.append(_Token("STRING", "".join(chars), start_line))
        elif c == "'":
            i += 1
            if i < n and source[i] == "\\":
                i += 2
            elif i < n:
                i += 1
            if i < n and source[i] == "'":
                i += 1
        elif c in _PUNCTUATION:
            tokens.append(_Token(_PUNCTUATION[c], c, lineno))
            i += 1
        elif c.isalpha() or c in "_$":
            start_line = lineno
            start = i
            while i < n and (source[i].isalnum() or source[i] in "_$"):
                i += 1
            tokens.append(_Token("IDENT", source[start:i], start_line))
        else:
            tokens.append(_Token("OTHER", c, lineno))
            i += 1

    return tokens


def _is_direct_call(tokens: list[_Token], idx: int) -> bool:
    """Return True if tokens[idx] is a direct call site not preceded by a dot."""
    return (
        (idx == 0 or tokens[idx - 1].kind != "DOT")
        and idx + 1 < len(tokens)
        and tokens[idx + 1].kind == "LPAREN"
    )


def _parse_java_source(source_text: str) -> dict[str, tuple[int, bool]]:
    """Parse Java source text and extract environment variable reads.

    Returns:
        dict mapping var_name -> (line_number of first read, is_property).
    """
    tokens = _tokenize_java(source_text)
    n = len(tokens)

    brace_diff = sum(1 for t in tokens if t.kind == "LBRACE") - sum(
        1 for t in tokens if t.kind == "RBRACE"
    )
    if brace_diff != 0:
        raise ValueError(f"syntax error: unmatched braces ({brace_diff})")
    paren_diff = sum(1 for t in tokens if t.kind == "LPAREN") - sum(
        1 for t in tokens if t.kind == "RPAREN"
    )
    if paren_diff != 0:
        raise ValueError(f"syntax error: unmatched parentheses ({paren_diff})")

    # 1. Static imports
    static_getenv = False
    static_getproperty = False
    for i in range(n - 4):
        if (
            tokens[i].kind == "IDENT"
            and tokens[i].value == "import"
            and tokens[i + 1].kind == "IDENT"
            and tokens[i + 1].value == "static"
        ):
            j = i + 2
            imp_parts: list[str] = []
            while j < n and tokens[j].kind != "SEMI":
                imp_parts.append(tokens[j].value)
                j += 1
            imp_str = "".join(imp_parts)
            if (
                "java.lang.System.getenv" in imp_str
                or "java.lang.System.*" in imp_str
            ):
                static_getenv = True
            if (
                "java.lang.System.getProperty" in imp_str
                or "java.lang.System.*" in imp_str
            ):
                static_getproperty = True

    # 2. String constants (e.g. String KEY = "VAL"; or final String KEY = "VAL";)
    constants: dict[str, str] = {}
    for i in range(n - 4):
        if (
            tokens[i].kind == "IDENT"
            and tokens[i].value == "String"
            and tokens[i + 1].kind == "IDENT"
            and tokens[i + 2].kind == "EQUALS"
            and tokens[i + 3].kind == "STRING"
        ):
            constants[tokens[i + 1].value] = tokens[i + 3].value

    def resolve_token(tok: _Token | None) -> str | None:
        if tok is None:
            return None
        if tok.kind == "STRING":
            return tok.value
        if tok.kind == "IDENT":
            return constants.get(tok.value)
        return None

    # 3. Method calls
    env_vars: dict[str, tuple[int, bool]] = {}

    def record_var(var_name: str | None, lineno: int, is_prop: bool) -> None:
        if var_name and var_name.strip():
            env_vars.setdefault(var_name.strip(), (lineno, is_prop))

    for i in range(n):
        tok = tokens[i]
        if tok.kind != "IDENT":
            continue

        lineno = tok.lineno

        # Check for System.getenv(...) or java.lang.System.getenv(...)
        if tok.value == "System" and i + 4 < n:
            if tokens[i + 1].kind == "DOT" and tokens[i + 2].kind == "IDENT":
                method_name = tokens[i + 2].value
                # System.getenv("VAR") or System.getProperty("VAR")
                if (
                    method_name in ("getenv", "getProperty")
                    and tokens[i + 3].kind == "LPAREN"
                ):
                    var_val = resolve_token(
                        tokens[i + 4] if i + 4 < n else None
                    )
                    if var_val:
                        record_var(
                            var_val,
                            lineno,
                            is_prop=(method_name == "getProperty"),
                        )
                    # System.getenv().get("VAR") or System.getenv().getOrDefault("VAR", ...)
                    elif (
                        method_name == "getenv"
                        and tokens[i + 4].kind == "RPAREN"
                        and i + 8 < n
                        and tokens[i + 5].kind == "DOT"
                        and tokens[i + 6].kind == "IDENT"
                        and tokens[i + 6].value in ("get", "getOrDefault")
                        and tokens[i + 7].kind == "LPAREN"
                    ):
                        inner_val = resolve_token(tokens[i + 8])
                        if inner_val:
                            record_var(inner_val, lineno, is_prop=False)

        # Static imports: getenv("VAR") or getProperty("VAR")
        elif (
            tok.value == "getenv"
            and static_getenv
            and _is_direct_call(tokens, i)
        ):
            var_val = resolve_token(tokens[i + 2] if i + 2 < n else None)
            if var_val:
                record_var(var_val, lineno, is_prop=False)

        elif (
            tok.value == "getProperty"
            and static_getproperty
            and _is_direct_call(tokens, i)
        ):
            var_val = resolve_token(tokens[i + 2] if i + 2 < n else None)
            if var_val:
                record_var(var_val, lineno, is_prop=True)

        # Helper methods: env("VAR"), requireEnv("VAR"), getEnv("VAR")
        elif tok.value in ("env", "requireEnv", "getEnv") and _is_direct_call(
            tokens, i
        ):
            var_val = resolve_token(tokens[i + 2] if i + 2 < n else None)
            if var_val:
                record_var(var_val, lineno, is_prop=False)

        # Dotenv calls: dotenv.get("VAR"), DOTENV.get("VAR"), Dotenv.load().get("VAR")
        elif tok.value in ("dotenv", "DOTENV", "Dotenv") and i + 4 < n:
            # DOTENV.get("VAR")
            if (
                tokens[i + 1].kind == "DOT"
                and tokens[i + 2].kind == "IDENT"
                and tokens[i + 2].value in ("get", "getOrDefault")
                and tokens[i + 3].kind == "LPAREN"
            ):
                var_val = resolve_token(tokens[i + 4])
                if var_val:
                    record_var(var_val, lineno, is_prop=False)
            # Dotenv.load().get("VAR") or Dotenv.configure().load().get("VAR")
            elif tokens[i + 1].kind == "DOT":
                j = i + 1
                while j < min(i + _DOTENV_LOOKAHEAD_LIMIT, n - 3):
                    if (
                        tokens[j].kind == "DOT"
                        and tokens[j + 1].kind == "IDENT"
                        and tokens[j + 1].value in ("get", "getOrDefault")
                        and tokens[j + 2].kind == "LPAREN"
                    ):
                        var_val = resolve_token(tokens[j + 3])
                        if var_val:
                            record_var(var_val, lineno, is_prop=False)
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
        ".m2",
        "tests",
        "test",
        ".agent-tmp",
        ".git",
    }
)


def _unreadable_source(
    java_file: Path,
    exc: Exception | None = None,
    lineno: int = 1,
    detail: str = "",
) -> Diagnostic:
    """A Java file this check could not read or parse is a hole in the check."""
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
        what = f"{java_file}:{lineno} is not valid Java: {exc}."
        how = "Fix the syntax error, then re-run the build."
    else:
        what = f"{java_file}:{lineno} could not be parsed as Java."
        how = "Fix the syntax error, then re-run the build."
    return Diagnostic(
        check=CHECK,
        what=what,
        why=(
            "This check parses every non-test Java file in the recipe "
            "to find environment-variable reads. A file it cannot parse is "
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
    """Parse all non-test, non-build Java files.

    Returns the variables read (name -> (file, line of first read, is_property))
    and a diagnostic for every file that could not be parsed.
    """
    used: dict[str, tuple[Path, int, bool]] = {}
    unreadable: list[Diagnostic] = []

    for java_file in sorted(recipe_dir.rglob("*.java")):
        rel = java_file.relative_to(recipe_dir)
        if any(part in _EXCLUDED_DIRS for part in rel.parts):
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

        for name, (lineno, is_prop) in vars_found.items():
            used.setdefault(name, (java_file, lineno, is_prop))

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
    is_property: bool = False,
) -> Diagnostic:
    prop_note = (
        " (System.getProperty reads a system property, which is not strictly "
        "an environment variable, but is reported so it is documented in .env.example)"
        if is_property
        else ""
    )
    return Diagnostic(
        check=CHECK,
        what=(
            f"{var} is read at {source}:{lineno}{prop_note} but is not declared in "
            f"{env_example}."
        ),
        why=(
            ".env.example is the only place someone running this recipe can "
            "discover what they have to configure; a variable that is read "
            "but not listed there makes the recipe fail with an empty value "
            "and no explanation. The read was found by parsing the "
            "recipe's Java source, so it is a real System.getenv / "
            "System.getProperty access, not a string match."
            + (
                " Note that System.getProperty reads a Java system property "
                "rather than strictly an OS environment variable."
                if is_property
                else ""
            )
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
        for var, (source, lineno, is_prop) in sorted(used_vars.items()):
            if var in defined_vars or _is_allowed(var):
                continue
            diagnostics.append(
                _undeclared_var(
                    var, source, lineno, env_example, is_property=is_prop
                )
            )

    n_checked = len(used_vars)
    n_allowed = sum(1 for v in used_vars if _is_allowed(v))
    passed_message = (
        f"{env_example}: no environment-variable reads detected in Java source."
        if not used_vars
        else (
            f"{env_example}: every environment variable read in Java "
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
