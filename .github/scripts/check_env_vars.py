"""
Checks that every environment variable read by a recipe's Python source is
declared in the recipe's .env.example.

AST-based: understands os.getenv(), os.environ.get(), os.environ[...],
and their import-alias forms (from os import getenv; from os.environ import
get) regardless of how the calls are formatted or split across lines.
An allowlist of well-known OS/CI variables suppresses false positives for
variables that legitimately do not belong in .env.example (HOME, PATH, CI,
GITHUB_*, etc.).

Usage: python3 check_env_vars.py <recipe-dir>

Exit codes:
  0  every variable read by the recipe's Python source is declared
  1  contributor-fixable problems found; every one has been reported with
     a fix, both as a human block and as a ::error annotation
  2  CI fault — the checker crashed or was invoked wrongly. Never blamed
     on the contributor's files.

A missing .env.example is not this script's failure to report (a separate
required-files check owns it), so that case exits 0 with a note.

MAINTENANCE NOTE — keep in sync with the extract-python-environment-variables
skill (.agents/skills/extract-python-environment-variables/scripts/
extract_env_vars.py).  This script READS/validates; that one REWRITES.  They
are intentionally separate tools but must stay semantically aligned: if you
add a detection pattern or modify the allowlist here, mirror the change there.
"""

import ast
import re
import sys
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

CHECKER = "check_env_vars.py"
CHECK = "env-vars"

# The value extract_env_vars.py writes for a variable it cannot infer a
# default for. Suggesting the same token keeps a hand-fixed .env.example
# indistinguishable from a skill-fixed one.
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
        # Python runtime flags
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "PYTHONSTARTUP",
        # Virtual-environment managers
        "VIRTUAL_ENV",
        "CONDA_DEFAULT_ENV",
        "CONDA_PREFIX",
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
        "PYTEST_CURRENT_TEST",
        # Container / platform runtime sentinels injected by Cloud Run, GKE,
        # Agent Engine, or Docker build-args rather than user .env files.
        "K_SERVICE",
        "K_REVISION",
        "K_CONFIGURATION",
        "KUBERNETES_SERVICE_HOST",
        "KUBERNETES_SERVICE_PORT",
        "GOOGLE_CLOUD_AGENT_ENGINE_ID",
        "GOOGLE_CLOUD_AGENT_ENGINE_LOCATION",
        "ADK_DEPLOY_CHANNEL",
        "AGENT_VERSION",
        "COMMIT_SHA",
        "USAGE_ANALYTICS_ENABLED",
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
# AST visitor
# ---------------------------------------------------------------------------


class _EnvVarCollector(ast.NodeVisitor):
    """Collects env-variable names from canonical os/os.environ read patterns.

    Detected patterns
    -----------------
    os.getenv("VAR")                  Attribute call on 'os'
    os.environ.get("VAR")             Chained attribute call
    os.environ["VAR"]                 Subscript on os.environ
    getenv("VAR")                     After: from os import getenv [as ...]
    get("VAR")                        After: from os.environ import get [as ...]

    Intentionally NOT detected (to avoid false positives)
    -------------------------------------------------------
    os.environ.setdefault("VAR", ...) — write/default, not a pure read
    os.environ.pop("VAR")             — deletion
    Dynamic keys: os.getenv(some_var) — cannot resolve statically
    Keyword-only calls: os.getenv(key="VAR") — extremely uncommon
    """

    def __init__(self) -> None:
        # name -> line of the FIRST read, so a diagnostic can point at a
        # source location instead of naming a variable and leaving the
        # contributor to grep for it.
        self.env_vars: dict[str, int] = {}
        # Aliases introduced by `from os import getenv [as foo]`
        self._getenv_aliases: set[str] = set()
        # Aliases introduced by `from os.environ import get [as foo]`
        self._environ_get_aliases: set[str] = set()

    # ------------------------------------------------------------------
    # Import tracking
    # ------------------------------------------------------------------

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "os":
            for alias in node.names:
                if alias.name == "getenv":
                    self._getenv_aliases.add(alias.asname or alias.name)
        elif node.module == "os.environ":
            for alias in node.names:
                if alias.name == "get":
                    self._environ_get_aliases.add(alias.asname or alias.name)
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Call patterns
    # ------------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        # os.getenv("VAR")
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "getenv"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ):
            self._capture_first_str(node)

        # os.environ.get("VAR")
        elif (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "environ"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "os"
        ):
            self._capture_first_str(node)

        # getenv("VAR")  after `from os import getenv`
        elif (
            isinstance(node.func, ast.Name)
            and node.func.id in self._getenv_aliases
        ):
            self._capture_first_str(node)

        # get("VAR")  after `from os.environ import get`
        elif (
            isinstance(node.func, ast.Name)
            and node.func.id in self._environ_get_aliases
        ):
            self._capture_first_str(node)

        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Subscript pattern
    # ------------------------------------------------------------------

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # os.environ["VAR"]
        if (
            isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
        ):
            # Python 3.9+: node.slice is the value directly.
            # Python 3.8:  node.slice is ast.Index(value=...).
            slice_node = node.slice
            if isinstance(slice_node, ast.Index):  # type: ignore[attr-defined]
                slice_node = slice_node.value  # type: ignore[attr-defined]
            if isinstance(slice_node, ast.Constant) and isinstance(
                slice_node.value, str
            ):
                self.env_vars.setdefault(slice_node.value, node.lineno)
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _capture_first_str(self, node: ast.Call) -> None:
        """Add the first positional string-literal argument to env_vars."""
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            self.env_vars.setdefault(node.args[0].value, node.lineno)


# ---------------------------------------------------------------------------
# File-level helpers
# ---------------------------------------------------------------------------


_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        # Generated / tool directories — contain third-party code, not recipe
        # source, and scanning them produces enormous false-positive floods.
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        # Test directory — checked separately; runnability tests intentionally
        # use stub env vars that shouldn't be declared in .env.example.
        "tests",
    }
)


def _unreadable_source(py_file: Path, exc: Exception) -> Diagnostic:
    """A Python file this check could not read is a hole in the check.

    Skipping it silently is worse than reporting it: the recipe passes with
    "all env vars declared" while an unknown number of reads inside the
    unreadable file were never looked at.
    """
    if isinstance(exc, SyntaxError):
        what = f"{py_file}:{exc.lineno or 1} is not valid Python: {exc.msg}."
        how = (
            "Fix the syntax error, then re-run. Ruff reports the same "
            "problem locally:\n"
            "  uv run ruff check <recipe-dir>"
        )
    elif isinstance(exc, UnicodeDecodeError):
        what = f"{py_file} could not be decoded as UTF-8: {exc}."
        how = (
            "Re-save the file as UTF-8 (every Python source file in this "
            "repo is UTF-8):\n"
            "  iconv -f <current-encoding> -t utf-8 <file> > <file>.utf8\n"
            "  mv <file>.utf8 <file>"
        )
    else:
        what = f"{py_file} could not be parsed as Python: {exc}."
        how = (
            "Open the file and check it is plain UTF-8 Python source with "
            "no embedded null bytes, then re-run:\n"
            "  uv run ruff check <recipe-dir>"
        )
    return Diagnostic(
        check=CHECK,
        what=what,
        why=(
            "This check AST-parses every non-test Python file in the recipe "
            "to find environment-variable reads. A file it cannot parse is "
            "invisible to it, so a variable read there could be missing "
            "from .env.example and still pass CI."
        ),
        how=how,
        doc=Doc.ENV_VARS,
        file=str(py_file),
    )


def _collect_used_vars(
    recipe_dir: Path,
) -> tuple[dict[str, tuple[Path, int]], list[Diagnostic]]:
    """AST-parse all non-test, non-venv Python files.

    Returns the variables read (name -> the file and line of the first read)
    and a diagnostic for every file that could not be parsed.
    """
    used: dict[str, tuple[Path, int]] = {}
    unreadable: list[Diagnostic] = []
    for py_file in sorted(recipe_dir.rglob("*.py")):
        # Exclude anything under a directory that appears in the exclusion set.
        if any(part in _EXCLUDED_DIRS for part in py_file.parts):
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            unreadable.append(_unreadable_source(py_file, exc))
            continue
        collector = _EnvVarCollector()
        collector.visit(tree)
        for name, lineno in collector.env_vars.items():
            used.setdefault(name, (py_file, lineno))
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
                "check, by python-dotenv at runtime, and by every editor "
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
            "and no explanation. The read was found by AST-parsing the "
            "recipe's source, so it is a real os.getenv/os.environ access, "
            "not a string match."
        ),
        how=(
            f"Add this line to {env_example}:\n"
            f"  {var}={PLACEHOLDER}\n"
            f"and make sure the recipe loads it (load_dotenv() in the "
            f"package __init__.py).\n"
            f"Or ask your AI coding assistant to run the "
            f"extract-python-environment-variables skill on this recipe — "
            f"it finds every read and writes the missing declarations, "
            f"including defaults hidden in os.environ.setdefault() calls."
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
        f"{env_example}: no environment-variable reads detected in Python "
        f"source."
        if not used_vars
        else (
            f"{env_example}: every environment variable read in Python "
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
    # guard(): a traceback escaping to the runner is reported by the
    # workflow as the contributor's failure, which is exactly the confusion
    # infra_fault exists to prevent.
    sys.exit(guard(CHECKER, main))
