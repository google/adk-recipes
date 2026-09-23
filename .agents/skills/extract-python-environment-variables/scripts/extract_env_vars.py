#!/usr/bin/env python3
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
extract_env_vars.py

Scans a Python recipe directory and:
  1. Detects all environment variable accesses in non-test Python files
     — including ``os.environ.setdefault("V", "d")``, whose "d" would
     otherwise be a hidden default that a user editing .env.example has
     no way to discover.
  2. Creates or updates .env.example with any missing variables, writing
     the extracted default value when the author committed to one and
     falling back to a TODO placeholder otherwise (see rule below).
  3. Injects the load_dotenv() bootstrap snippet into the package __init__.py.
  4. Ensures python-dotenv>=1.0.0 is listed in pyproject.toml dependencies.
  5. Detects hardcoded model name strings and replaces them with
     bare os.getenv("MODEL_NAME") calls (no default argument).

Two hard rules the script NEVER breaks:

  (1) IDEMPOTENCY / USER-EDIT SAFETY FOR .env.example. Existing lines
      in .env.example are NEVER modified. If a variable is already
      declared — with any value, whether the skill wrote it on a previous
      run or the maintainer typed it — the skill skips it. Delete the
      line to force regeneration. This is what makes re-running the
      skill safe on a recipe whose maintainer has customised .env.example.

      When ADDING a new line, the writer uses the resolved default
      extracted from source (setdefault > getenv/environ.get fallback,
      alphabetical file tie-break). Placeholder-shaped values (e.g.
      "my-project-id", "changeme", "<...>") are downgraded to the TODO
      placeholder AND the source string is preserved in the marker
      comment so the maintainer sees what was in source and can fix it.
      Every generated line carries a ``# extracted-by:extract-env-vars``
      marker.

      Hardcoded model strings replaced in source ARE written as their
      actual value in .env.example (e.g. MODEL_NAME_GENERATED_1=
      gemini-3.5-flash) — same as before v2.

  (2) ADDITIVE-ONLY FOR PYTHON FILES. The script never writes new
      os.environ.setdefault(...) bootstrap lines into any Python file.
      Pre-existing os.environ.setdefault(...) or
      os.getenv("VAR", "default") calls that a recipe author wrote by
      hand are LEFT UNTOUCHED — the script's only writes to Python
      files are (a) the load_dotenv snippet, (b) trailing-import E402
      suppression, and (c) the model-literal replacement.

Rationale for the v2 change (extracting setdefault/getenv defaults instead
of always writing TODO): a value hidden inside a Python file that a user
must know about but has no reason to look at is, in practice, undocumented.
.env.example is the documented configuration surface. Surfacing what the
author already committed to in code makes the configuration discoverable
without inventing anything new — the value in source WAS the value; we're
only lifting it into view.
"""

import argparse
import ast
import re
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

if sys.version_info < (3, 11):
    sys.exit(
        "extract_env_vars.py requires Python 3.11+ (it uses the stdlib "
        "`tomllib`, added in 3.11). Re-run it with a newer interpreter, "
        "e.g. `uv run --no-project python3 <this-script> ...`."
    )

import tomllib

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLACEHOLDER = "<TODO: update-this-value>"

# Stable substring embedded in every .env.example line the skill writes.
# Used by the writer to distinguish skill-authored lines from user-authored
# ones on subsequent runs. Also useful for maintainers grepping to find
# "what did the skill do?".
EXTRACTED_MARKER = "extracted-by:extract-env-vars"

# Kinds of AST-level env var access, in descending order of "how strongly
# does the author commit to this default value?".
#
#   'setdefault' — os.environ.setdefault("V", "x"). Mutates the process
#      environment; the value becomes visible to every subsequent read.
#      Strongest signal that the author has chosen this as the canonical
#      default.
#   'getenv' / 'environ_get' — os.getenv("V", "x") / os.environ.get(...).
#      Per-read fallback; does not mutate os.environ. Weaker commitment,
#      but still an authored value.
#   'environ_sub' — os.environ["V"]. Never carries a default.
_KIND_PRIORITY: dict[str, int] = {
    "setdefault": 0,
    "getenv": 1,
    "environ_get": 1,
    "environ_sub": 2,
}


class Source(NamedTuple):
    """One AST-level read/write of an env var, with provenance for reporting.

    ``default`` is the string literal passed as the second argument, or None
    when the call had no default (or the default was not a string literal —
    non-string defaults cannot be surfaced in ``.env.example``).
    """

    file: Path
    kind: str  # one of _KIND_PRIORITY's keys
    default: str | None
    line: int


# Values that look like placeholders the author never intended as real
# defaults (e.g. `os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "my-project-id")`
# used as a "reminder to fill in" bootstrap). When the extracted value matches
# any of these, the writer downgrades the .env.example entry to the TODO
# placeholder AND includes the source string in the marker comment so the
# maintainer sees what was found and can fix the source too.
#
# Kept CONSERVATIVE on purpose: false-downgrading a real default is more
# damaging than letting a placeholder through (the placeholder still ends up
# in .env.example, just without the shape warning — same as the pre-v2
# behaviour).
_PLACEHOLDER_EXACT: frozenset[str] = frozenset(
    {"changeme", "change-me", "todo", "xxx", "xxxxx", "foo", "bar", "baz"}
)
_PLACEHOLDER_PREFIXES: tuple[str, ...] = (
    "your-",
    "your_",
    "my-",
    "my_",
)
# Compiled once; case-insensitive because placeholders often mix casing.
_ANGLE_WRAPPED_RE = re.compile(r"^<.*>$")


def looks_like_placeholder(value: str) -> str | None:
    """Return a short reason string if ``value`` looks like a placeholder.

    Returns None when the value looks like a real default and should be
    written to .env.example as-is. See ``_PLACEHOLDER_EXACT`` /
    ``_PLACEHOLDER_PREFIXES`` for the (deliberately conservative) rule set.
    """
    if value == "":
        return "empty string"
    lowered = value.lower()
    if _ANGLE_WRAPPED_RE.match(value):
        return "angle-wrapped placeholder"
    if lowered in _PLACEHOLDER_EXACT:
        return "generic placeholder token"
    if any(lowered.startswith(p) for p in _PLACEHOLDER_PREFIXES):
        return "starts with your-/my- (looks like a stub)"
    if "example.com" in lowered:
        return "contains example.com"
    return None


# Model name prefixes that indicate a hardcoded model identifier.
# Kept in sync with python-validate-recipe.yml.
MODEL_PREFIXES: tuple[str, ...] = (
    "gemini-",
    "gemini-exp-",
    "imagen-",
    "claude-",
    "llama-",
    "meta/llama-",
    "mistral-",
    "codestral-",
    "phi-",
    "grok-",
    "command-",
    "jamba-",
)

LOAD_DOTENV_IMPORT = "from dotenv import load_dotenv"

LOAD_DOTENV_SNIPPET = """\
# Load variables from .env if present. In production the environment is
# already populated by the platform (Cloud Run, GKE, etc.), so a missing
# .env is expected and not an error.
load_dotenv()"""


# ---------------------------------------------------------------------------
# Safe I/O helpers
# ---------------------------------------------------------------------------


def _write_text_atomic(
    path: Path, content: str, encoding: str = "utf-8"
) -> None:
    """Write *content* to *path* atomically via a sibling temp file.

    Writes to a temporary file in the same directory as *path*, then renames
    it over the target.  Because the rename is atomic on POSIX (same
    filesystem), a crash or disk-full error mid-write leaves the original
    file untouched — the worst outcome is an orphaned ``.tmp`` file.

    ``newline=""`` disables Python's universal-newlines translation on
    write, so any ``\\r\\n`` sequences in *content* land on disk verbatim
    rather than being expanded by the platform. Callers that want to
    preserve a file's original line endings must also disable universal
    newlines on read (see :func:`_read_text_preserving_newlines`).

    Raises ``OSError`` on any I/O failure; callers are responsible for
    catching and reporting it.
    """
    dir_ = path.parent
    tmp_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        tmp_path = Path(tmp_name)
        with open(fd, "w", encoding=encoding, newline="") as fh:
            fh.write(content)
        tmp_path.replace(path)
    except OSError:
        # Best-effort cleanup of the temp file before re-raising.
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _read_text_preserving_newlines(path: Path) -> str:
    """Read *path* WITHOUT converting CRLF/CR to LF.

    ``Path.read_text()`` uses universal-newlines mode by default, which
    silently rewrites ``\\r\\n`` to ``\\n`` in memory. That's fine for
    parsing but catastrophic if the caller round-trips the file back to
    disk — it will replace every ``\\r\\n`` with ``\\n``, mutating the
    user's file in a way they didn't ask for. This helper opens with
    ``newline=""`` so the bytes come through unchanged.
    """
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Step 1: Find Python files
# ---------------------------------------------------------------------------

# Directories that never contain first-party recipe source and MUST be skipped
# during scanning. Virtualenvs are the biggest hazard: a local `.venv/`
# containing installed third-party packages would otherwise pollute
# `.env.example` with unrelated env vars and match hundreds of hardcoded model
# strings inside third-party code.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        # Test directories (not part of the recipe's runtime code path).
        "tests",
        # Python virtualenvs (all common names).
        ".venv",
        "venv",
        "env",
        # Bytecode + tool caches.
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".eggs",
        # Build artifacts.
        "build",
        "dist",
        # VCS metadata.
        ".git",
        ".hg",
        ".svn",
        # JS toolchains (uncommon here but cheap to skip).
        "node_modules",
    }
)

# Reserved filenames that always represent test / tooling infrastructure,
# regardless of where they live in the recipe. These are skipped in addition
# to the ``SKIP_DIRS`` check — needed because pytest's ``conftest.py`` is
# commonly placed at the repo/recipe ROOT (not inside a ``tests/`` dir), so
# the directory-based exclusion misses it. Surfacing env vars from
# ``conftest.py`` (e.g. ``INTEGRATION_TEST`` gating integration-test
# collection) into ``.env.example`` would silently invite users to set
# test-mode toggles they should not touch.
SKIP_FILES: frozenset[str] = frozenset(
    {
        "conftest.py",  # pytest reserved name — always test infrastructure
    }
)


def find_python_files(recipe_dir: Path) -> list[Path]:
    """
    Return all .py files under recipe_dir, skipping:
      * directories that are not part of the recipe's own source (see
        ``SKIP_DIRS``); and
      * files whose basename is a reserved test/tooling filename (see
        ``SKIP_FILES``) regardless of directory.
    """
    return [
        p
        for p in sorted(recipe_dir.rglob("*.py"))
        if not SKIP_DIRS.intersection(p.relative_to(recipe_dir).parts)
        and p.name not in SKIP_FILES
    ]


# ---------------------------------------------------------------------------
# Step 2: Extract environment variable reads via AST
# ---------------------------------------------------------------------------


def _extract_var_from_node(
    node: ast.AST,
) -> tuple[str | None, str | None, str | None]:
    """
    Return ``(var_name, kind, default)`` if node is an env-var access,
    else ``(None, None, None)``.

    Handles four forms:
      * ``os.getenv("V")`` / ``os.getenv("V", "d")``               → kind='getenv'
      * ``os.environ.get("V")`` / ``os.environ.get("V", "d")``     → kind='environ_get'
      * ``os.environ["V"]``                                        → kind='environ_sub'
      * ``os.environ.setdefault("V", "d")``                        → kind='setdefault'

    Note that ``setdefault`` is included even though it MUTATES the process
    environment rather than reading from it — semantically it declares
    "if VAR is not set, VAR IS d". That declaration is the author's
    committed default, and the whole point of surfacing it in .env.example
    is that a user cannot discover it otherwise.
    """

    def _str_const(n: ast.expr) -> str | None:
        return (
            n.value
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            else None
        )

    def _default_from_call(call: ast.Call) -> str | None:
        """Extract the ``default`` argument from a getenv/get/setdefault call.

        Checks the second positional argument first, then falls back to
        the ``default=`` keyword form (``os.getenv("X", default="y")``).
        Non-string-literal defaults return None so the caller emits TODO.
        """
        if len(call.args) > 1:
            return _str_const(call.args[1])
        for kw in call.keywords:
            if kw.arg == "default":
                return _str_const(kw.value)
        return None

    # os.getenv("VAR") / os.getenv("VAR", "default") / os.getenv("VAR", default="d")
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "getenv"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
        and node.args
    ):
        var_name = _str_const(node.args[0])
        default = _default_from_call(node)
        return var_name, "getenv", default

    # os.environ.get("VAR") / os.environ.get("VAR", "default")
    # os.environ.setdefault("VAR", "default")
    # Keyword-form default= is also honoured for both.
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("get", "setdefault")
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "environ"
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "os"
        and node.args
    ):
        var_name = _str_const(node.args[0])
        default = _default_from_call(node)
        kind = "environ_get" if node.func.attr == "get" else "setdefault"
        return var_name, kind, default

    # os.environ["VAR"]
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "environ"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "os"
    ):
        return _str_const(node.slice), "environ_sub", None

    return None, None, None


def extract_env_vars(py_files: list[Path]) -> dict[str, list[Source]]:
    """
    Walk each file's AST and collect every env-var access site.

    Returns:
      {VAR_NAME: [Source(file, kind, default, line), ...]}

    The returned list preserves every occurrence — the resolver
    (:func:`resolve_default`) is responsible for picking a winning default
    when a variable has multiple sources with different defaults.
    """
    found: dict[str, list[Source]] = {}
    skipped: set[str] = set()

    for py_file in py_files:
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            print(
                f"[WARN] Could not parse {py_file} — skipping.", file=sys.stderr
            )
            continue

        for node in ast.walk(tree):
            var_name, kind, default = _extract_var_from_node(node)
            if not var_name or not kind:
                continue
            if re.match(r"^[A-Z_][A-Z0-9_]*$", var_name):
                lineno = getattr(node, "lineno", 0)
                found.setdefault(var_name, []).append(
                    Source(
                        file=py_file, kind=kind, default=default, line=lineno
                    )
                )
            else:
                # Not UPPER_SNAKE_CASE — dropped, but surfaced so the
                # maintainer isn't left wondering why it never appeared in
                # .env.example (silent drops were a reported footgun).
                skipped.add(var_name)

    if skipped:
        print(
            "[WARN] Ignored env var name(s) that are not UPPER_SNAKE_CASE "
            "(only names matching ^[A-Z_][A-Z0-9_]*$ are captured; rename "
            "them in source, or add them to .env.example by hand): "
            + ", ".join(sorted(skipped)),
            file=sys.stderr,
        )

    return found


# ---------------------------------------------------------------------------
# Default resolution + .env.example line formatting
# ---------------------------------------------------------------------------


class ResolvedDefault(NamedTuple):
    """The winning default for an env var, chosen from its Source list.

    ``value`` is None when no source supplied a string-literal default;
    the caller should emit a TODO placeholder in that case.

    ``winner`` is the specific ``Source`` the value came from — used to
    build the provenance comment in .env.example.

    ``conflict_count`` is the number of OTHER sources that supplied a
    DIFFERENT string-literal default. Zero means every default agreed
    (or there was only one default at all). A non-zero conflict count is
    surfaced in the provenance comment so the maintainer can reconcile.
    """

    value: str | None
    winner: Source | None
    conflict_count: int


def resolve_default(sources: list[Source]) -> ResolvedDefault:
    """Pick the winning default for a variable from all its access sites.

    Priority: ``setdefault`` beats ``getenv`` / ``environ_get`` fallbacks
    (see the ``_KIND_PRIORITY`` docstring for why). Within a tier, the
    source whose file sorts first alphabetically wins, then the earliest
    line within that file — both purely for determinism.

    Sources that have no string-literal default (``environ_sub``, bare
    ``getenv("V")``, or a non-string default like ``os.getenv("N", 5)``)
    do not contribute a value but DO count against the total when
    computing conflicts — actually no, they don't: they didn't propose a
    default at all, so they cannot conflict with the winner. Only
    string-literal defaults that DIFFER from the winner are conflicts.
    """
    with_defaults = [s for s in sources if s.default is not None]
    if not with_defaults:
        return ResolvedDefault(value=None, winner=None, conflict_count=0)

    winner = min(
        with_defaults,
        key=lambda s: (_KIND_PRIORITY.get(s.kind, 99), str(s.file), s.line),
    )
    conflicts = sum(1 for s in with_defaults if s.default != winner.default)
    return ResolvedDefault(
        value=winner.default, winner=winner, conflict_count=conflicts
    )


def _relpath_or_name(path: Path, base: Path | None) -> str:
    """Return ``path`` relative to ``base`` if possible, else its name.

    Used only for human-readable provenance comments — never for logic.
    """
    if base is None:
        return path.name
    try:
        return str(path.relative_to(base))
    except ValueError:
        return path.name


def format_env_entry(
    var_name: str,
    sources: list[Source],
    recipe_dir: Path | None = None,
) -> str:
    """Return one ``.env.example`` line (no trailing newline) for ``var_name``.

    Format:
      ``VAR=<value>  # <EXTRACTED_MARKER>; <provenance>``

    Where ``<value>`` is one of:
      * the resolved default, if a source supplied one and it doesn't look
        like a placeholder;
      * :data:`PLACEHOLDER` (``<TODO: update-this-value>``) otherwise.

    The provenance section always names the source (file + kind) when a
    default was found — including the case where we downgraded a
    placeholder-shaped value to TODO, so the maintainer can find and fix
    the source too. If multiple sources supplied conflicting defaults, the
    conflict count is included.
    """
    resolved = resolve_default(sources)

    if resolved.value is None:
        # No string-literal default anywhere — safest to ask the maintainer.
        return (
            f"{var_name}={PLACEHOLDER}"
            f"  # {EXTRACTED_MARKER}; no default in source"
        )

    assert resolved.winner is not None  # narrow for type-checkers
    where = _relpath_or_name(resolved.winner.file, recipe_dir)
    provenance = f"from {resolved.winner.kind} in {where}"
    if resolved.conflict_count:
        provenance += (
            f" (differs from {resolved.conflict_count} other source(s))"
        )

    reason = looks_like_placeholder(resolved.value)
    if reason is not None:
        # Downgrade to TODO but keep the source string visible so the
        # maintainer can find and fix the source too.
        return (
            f"{var_name}={PLACEHOLDER}  # {EXTRACTED_MARKER}; {provenance};"
            f' source had "{resolved.value}" — {reason}, please fix source too'
        )

    return f"{var_name}={resolved.value}  # {EXTRACTED_MARKER}; {provenance}"


# ---------------------------------------------------------------------------
# Step 3: Create / update .env.example
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Classification of existing entries in .env.example
#
# When re-running the skill against a recipe whose .env.example already has
# entries, we can't apply a blanket "if present, skip" rule — that would
# leave v1-era TODO placeholders in place forever, even after source-side
# defaults become available. But we also can't blanket-overwrite, because
# any line the maintainer typed by hand is theirs.
#
# So every existing entry is classified into one of these buckets. The
# writer's action table (see `update_env_example`) depends on the bucket
# AND on what the current source scan produced.
#
# Golden rule: fail closed. If we can't confidently prove a line was
# skill-authored (has our marker) OR is a bare v1 TODO with no user
# annotations, we treat it as USER_OWNED and leave it alone. The cost
# of a missed upgrade is small; the cost of clobbering user intent
# is unrecoverable without a git checkout.
# ---------------------------------------------------------------------------


class EntryClassification:
    """String tags for the five states an existing .env.example entry can be in."""

    USER_OWNED = "user_owned"  # never touch
    V1_TODO = "v1_todo"  # unmarked bare TODO from pre-v2 skill; upgradable
    SKILL_TODO = "skill_todo"  # marker present + value is v2 PLACEHOLDER
    SKILL_REAL = "skill_real"  # marker present + non-placeholder value
    # (MISSING is not a state of an existing entry — it's the absence of one.)


class ExistingEntry(NamedTuple):
    """One env-var entry parsed out of an existing .env.example file.

    ``line_index`` is 0-based into the raw ``lines`` list (splitlines with
    keepends), so the rewriter can address the exact physical line.

    ``raw_line`` is the untouched line as it appeared on disk (including
    trailing newline), preserved so a refusal-to-rewrite leaves everything
    byte-identical.

    ``value`` is the value portion (after ``=``, before any inline
    comment), stripped of surrounding whitespace. Quoted values are
    returned WITH their quotes so the classifier / rewriter can detect
    and refuse them.
    """

    line_index: int
    raw_line: str
    value: str
    classification: str  # one of EntryClassification's constants


# Regex for a well-formed bare (unquoted) env line. Multiple things must
# ALL hold for us to consider a line safely rewritable later:
#   * Optional `export ` prefix
#   * VAR name is UPPER_SNAKE_CASE
#   * Single `=` (the rest of the line goes to the value + optional comment)
#   * Value up to the first `#` (comment start) or end-of-line
#
# Anything that doesn't match this shape is treated as USER_OWNED — the
# skill only rewrites lines whose structure it understands unambiguously.
_ENV_LINE_RE = re.compile(
    r"""
    ^
    [ \t]*                                  # optional leading whitespace
                                            # (python-dotenv permits it;
                                            # the old parser did too)
    (?:export[ \t]+)?                       # optional 'export '
    (?P<name>[A-Z_][A-Z0-9_]*)              # VAR name
    [ \t]*=[ \t]*
    (?P<value>[^#\n]*?)                     # value (up to first `#` or EOL)
    [ \t]*
    (?P<comment>\#.*)?                      # optional inline comment
    $
    """,
    re.VERBOSE,
)


def _classify_entry(raw_line: str, value: str, comment: str | None) -> str:
    """Classify one parsed entry.

    ``value`` is the value portion with surrounding whitespace stripped;
    ``comment`` is the inline comment (including its leading ``#``) or
    None if the line had no inline comment.

    Fail-closed: anything we can't confidently prove is skill-authored or
    a bare v1 TODO becomes USER_OWNED.
    """
    has_marker = comment is not None and EXTRACTED_MARKER in comment

    if has_marker:
        # Skill wrote this line; we own it and can rewrite it.
        return (
            EntryClassification.SKILL_TODO
            if value == PLACEHOLDER
            else EntryClassification.SKILL_REAL
        )

    # No marker. To qualify as an upgradable v1 TODO the line must be
    # BOTH the exact PLACEHOLDER value AND have NO inline comment at
    # all — because a user inline comment (e.g. "# my project is prj-foo,
    # use gcloud config get project") is real user intent even next to
    # a TODO literal, and we must respect it.
    if value == PLACEHOLDER and comment is None:
        return EntryClassification.V1_TODO

    return EntryClassification.USER_OWNED


def _parse_env_content(content: str) -> dict[str, ExistingEntry]:
    """Parse .env.example content into name -> ExistingEntry.

    Lines that don't look like a well-formed env declaration (blank,
    comment-only, non-matching, malformed) are skipped and not returned —
    they'll be preserved as-is when the writer rewrites the file, because
    the writer touches lines by index, never by text-replace.

    If the same var appears more than once in the file, only the FIRST
    occurrence is returned. This mirrors dotenv precedence (first wins)
    and means the writer will only ever attempt to rewrite that first
    occurrence — safer than trying to handle duplicates.
    """
    entries: dict[str, ExistingEntry] = {}
    lines = content.splitlines(keepends=True)
    for i, raw in enumerate(lines):
        stripped_for_match = raw.rstrip("\n").rstrip("\r")
        m = _ENV_LINE_RE.match(stripped_for_match)
        if not m:
            continue
        name = m.group("name")
        if name in entries:
            continue  # first-occurrence wins
        value = m.group("value") or ""
        comment = m.group("comment")  # includes leading '#' or None
        classification = _classify_entry(raw, value, comment)
        entries[name] = ExistingEntry(
            line_index=i,
            raw_line=raw,
            value=value,
            classification=classification,
        )
    return entries


def parse_env_example(env_example: Path) -> dict[str, ExistingEntry]:
    """Parse a .env.example file into name -> ExistingEntry.

    Returns an empty dict if the file does not exist.
    """
    if not env_example.exists():
        return {}
    return _parse_env_content(env_example.read_text(encoding="utf-8"))


def read_defined_vars(env_example: Path) -> set[str]:
    """Return the set of variable names already declared in .env.example.

    Thin wrapper around :func:`parse_env_example` kept for existing
    callers that only need names (e.g. ``assign_model_var_names``).
    """
    return set(parse_env_example(env_example).keys())


def _rewrite_var_line(
    lines: list[str], var_name: str, new_line_body: str
) -> bool:
    """Rewrite the ONE line in ``lines`` that declares ``var_name``.

    ``new_line_body`` is the desired line WITHOUT trailing newline and
    WITHOUT any leading ``export `` prefix (the rewriter preserves the
    original prefix if there was one).

    Returns True on success, False on any of the following (fail-closed):
      * zero or multiple physical lines declare ``var_name``
      * the matched line has a value that starts with a quote (``"`` or
        ``'``) — quoted / multi-line values are ambiguous to rewrite
      * the matched line ends with a backslash continuation
      * the matched line's structure doesn't parse as a well-formed env
        line (``_ENV_LINE_RE`` doesn't match)

    These refusals are the golden-rule enforcement — better to leave a
    stale TODO in place than to rewrite a line whose semantics we don't
    understand.
    """
    # Regex to capture the "left prefix" — everything before the VAR
    # name (leading whitespace + optional 'export '). We preserve this
    # verbatim on rewrite so an indented line stays indented and an
    # `export`-ed line stays exported.
    left_prefix_re = re.compile(
        rf"^(?P<prefix>[ \t]*(?:export[ \t]+)?){re.escape(var_name)}[ \t]*="
    )

    matches: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n").rstrip("\r")
        env_m = _ENV_LINE_RE.match(stripped)
        if not env_m or env_m.group("name") != var_name:
            continue
        prefix_m = left_prefix_re.match(stripped)
        if prefix_m is None:
            # Line was recognised by the env regex but the more specific
            # prefix regex can't extract a prefix — refuse rather than
            # guess. Should be impossible in practice.
            return False
        matches.append((i, prefix_m.group("prefix")))

    if len(matches) != 1:
        return False  # unique-match requirement failed
    idx, left_prefix = matches[0]
    original = lines[idx]
    stripped = original.rstrip("\n").rstrip("\r")

    # Refuse quoted values — the value part might span multiple physical
    # lines, contain escaped quotes, or otherwise be more nuanced than
    # our regex handles safely.
    env_m = _ENV_LINE_RE.match(stripped)
    if env_m is None:  # defensive; should be impossible given the loop above
        return False
    value = env_m.group("value") or ""
    if value.startswith(('"', "'")):
        return False

    # Refuse continuation-line values.
    if stripped.endswith("\\"):
        return False

    # Preserve the original line's newline (\n, \r\n, or none).
    if original.endswith("\r\n"):
        newline = "\r\n"
    elif original.endswith("\n"):
        newline = "\n"
    else:
        newline = ""

    # left_prefix captures everything before the VAR name (leading
    # whitespace + optional 'export '); new_line_body is "VAR=value  #
    # comment". Concatenation preserves the original indentation and
    # export status while replacing the value + comment.
    lines[idx] = left_prefix + new_line_body + newline
    return True


def _plan_updates(
    existing: dict[str, ExistingEntry],
    env_vars: dict[str, list[Source]],
    recipe_dir: Path | None,
) -> tuple[dict[str, str], dict[str, tuple[int, str]]]:
    """Decide, per var, whether to append or upgrade in place (or skip).

    Returns ``(to_append, to_upgrade)``:
      * ``to_append``: {var: new_line_body} for missing entries.
      * ``to_upgrade``: {var: (line_index, new_line_body)} for existing
        entries that are safely upgradable AND whose upgrade would
        produce a REAL (non-placeholder) value.

    Anything not returned in either dict is being intentionally skipped
    (user-owned, or upgrade would just churn TODO → TODO, etc.).
    """
    to_append: dict[str, str] = {}
    to_upgrade: dict[str, tuple[int, str]] = {}

    for var, sources in env_vars.items():
        new_body = format_env_entry(var, sources, recipe_dir)
        entry = existing.get(var)

        if entry is None:
            # Missing → always append.
            to_append[var] = new_body
            continue

        if entry.classification == EntryClassification.USER_OWNED:
            continue  # ironclad: never touch user-authored lines
        if entry.classification == EntryClassification.SKILL_REAL:
            continue  # already has a real value; drift handled elsewhere

        # entry is V1_TODO or SKILL_TODO. Only upgrade if the new body
        # would actually be a real value — otherwise we'd churn TODO to
        # TODO, changing the comment but not the value. Detect via the
        # resolver rather than string-matching the new body.
        resolved = resolve_default(sources)
        if resolved.value is None:
            continue  # no default at all
        if looks_like_placeholder(resolved.value) is not None:
            continue  # source has a placeholder-shape value; skip

        to_upgrade[var] = (entry.line_index, new_body)

    return to_append, to_upgrade


def _build_updated_content(
    env_example: Path,
    to_append: dict[str, str],
    to_upgrade: dict[str, tuple[int, str]],
    added_names: list[str],
    upgraded_names: list[str],
) -> tuple[str, list[str]]:
    """Assemble the new ``.env.example`` content in memory.

    Reads the existing file (CRLF-preservingly), applies in-place upgrades
    line-by-line via :func:`_rewrite_var_line` (with WARN on refusal),
    ensures a trailing newline, and appends new entries in a marker block.

    Returns ``(new_content, actually_upgraded)`` where ``actually_upgraded``
    is the subset of ``upgraded_names`` that the fail-closed rewriter did
    not refuse. The caller runs the round-trip safety check on the result
    and decides whether to actually write.
    """
    if env_example.exists():
        current = _read_text_preserving_newlines(env_example)
    else:
        current = ""
    lines = current.splitlines(keepends=True)

    actually_upgraded: list[str] = []
    for var in upgraded_names:
        _line_idx, new_body = to_upgrade[var]
        if _rewrite_var_line(lines, var, new_body):
            actually_upgraded.append(var)
        else:
            print(
                f"[WARN] Refused to upgrade {var} in .env.example — "
                "the existing line's structure was ambiguous (quoted "
                "value, multiple matches, or line continuation). "
                "Leaving it as-is. Delete the line and re-run to force "
                "regeneration.",
                file=sys.stderr,
            )

    # Ensure a trailing newline before appending fresh entries.
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"

    if to_append:
        block_header = (
            "\n# Environment variables extracted by"
            " extract-python-environment-variables\n"
        )
        lines.append(block_header)
        for var in added_names:
            lines.append(to_append[var] + "\n")

    return "".join(lines), actually_upgraded


def _passes_round_trip_check(
    new_content: str,
    actually_upgraded: list[str],
    added_names: list[str],
) -> bool:
    """Re-parse ``new_content`` and verify every planned change looks right.

    Every var in ``actually_upgraded`` must now classify as ``SKILL_REAL``;
    every var in ``added_names`` must be present. Any deviation means our
    rewrite produced something the classifier doesn't understand — a
    rewriter bug — and the caller must REFUSE to write to preserve the
    file's current on-disk contents.

    Emits ``[ERROR]`` on stderr for the first mismatch it finds and
    returns False. Returns True when everything re-parses cleanly.
    """
    reparsed = _parse_env_content(new_content)
    for var in actually_upgraded:
        entry = reparsed.get(var)
        if (
            entry is None
            or entry.classification != EntryClassification.SKILL_REAL
        ):
            print(
                f"[ERROR] Round-trip safety check failed for {var} — the "
                "rewritten line would not re-parse as a skill-authored real "
                "value. Refusing to write; .env.example was NOT modified.",
                file=sys.stderr,
            )
            return False
    for var in added_names:
        if var not in reparsed:
            print(
                f"[ERROR] Round-trip safety check failed for appended {var} "
                "— entry did not re-parse. Refusing to write; .env.example "
                "was NOT modified.",
                file=sys.stderr,
            )
            return False
    return True


def update_env_example(
    env_example: Path,
    env_vars: dict[str, list[Source]],
    dry_run: bool = False,
    recipe_dir: Path | None = None,
) -> tuple[list[str], list[str]]:
    """
    Update .env.example — appending new entries AND upgrading stale TODOs.

    Returns ``(added, upgraded)``:
      * ``added``   — names appended as new entries.
      * ``upgraded`` — names whose existing TODO line was rewritten in
        place with a real default from source.

    SAFETY CONTRACT (the golden rule for this function):

      * A line without our ``EXTRACTED_MARKER`` marker AND whose value
        is not exactly the v1 :data:`PLACEHOLDER` is considered
        USER_OWNED — NEVER touched.
      * A line that is a v1 PLACEHOLDER but has an inline comment on
        it is treated as USER_OWNED (the comment is real user intent).
      * A skill-authored line whose value is a real (non-placeholder)
        value is NEVER re-written on drift — the maintainer may have
        relied on the persisted value.
      * A TODO line is only upgraded when source can supply a REAL
        (non-placeholder-shape) default. TODO → TODO would be pure
        churn.
      * Before writing, the assembled content is re-parsed and every
        planned upgrade must appear as ``SKILL_REAL`` in the result.
        If not, the write is REFUSED and the file is left untouched.

    When ``dry_run`` is True, no file is written; the return value still
    reports which variables would be added and upgraded.
    """
    existing = parse_env_example(env_example)
    to_append, to_upgrade = _plan_updates(existing, env_vars, recipe_dir)

    if not to_append and not to_upgrade:
        return [], []

    added_names = sorted(to_append.keys())
    upgraded_names = sorted(to_upgrade.keys())

    if dry_run:
        return added_names, upgraded_names

    # Build new content in memory (line-by-line rewrites + trailing
    # appends), then run the round-trip safety check before writing.
    new_content, actually_upgraded = _build_updated_content(
        env_example, to_append, to_upgrade, added_names, upgraded_names
    )
    if not _passes_round_trip_check(
        new_content, actually_upgraded, added_names
    ):
        return [], []

    try:
        _write_text_atomic(env_example, new_content)
    except OSError as exc:
        print(
            f"[ERROR] Could not write {env_example}: {exc} — "
            ".env.example was NOT modified.",
            file=sys.stderr,
        )
        return [], []

    return added_names, actually_upgraded


def write_model_vars_to_env_example(
    env_example: Path,
    var_to_model: dict[str, str],
    dry_run: bool = False,
) -> list[str]:
    """
    Append generated model variable entries to .env.example, skipping any
    already declared. Unlike update_env_example, writes the actual model
    string as the value — it was hardcoded in source so it is the known
    correct value, not an inferred default. Adds a comment reminding the
    maintainer to rename the auto-generated variable names.

    Returns the list of variable names that were (or would be) added.
    """
    existing = read_defined_vars(env_example)
    to_add = {k: v for k, v in var_to_model.items() if k not in existing}

    if not to_add:
        return []

    if dry_run:
        return sorted(to_add.keys())

    if env_example.exists():
        current = env_example.read_text(encoding="utf-8")
        if not current.endswith("\n"):
            current += "\n"
    else:
        current = ""

    block = (
        "\n# Renamed from hardcoded string in source."
        " Rename the variable if needed.\n"
    )
    for var in sorted(to_add):
        block += f"{var}={to_add[var]}\n"

    try:
        _write_text_atomic(env_example, current + block)
    except OSError as exc:
        print(
            f"[ERROR] Could not write {env_example}: {exc} — "
            ".env.example was NOT modified.",
            file=sys.stderr,
        )
        return []
    return sorted(to_add.keys())


# ---------------------------------------------------------------------------
# Shared helpers: file structure analysis
# ---------------------------------------------------------------------------


# Opening quotes of a module docstring, allowing any legal string prefix.
# Python permits r/u/b (and Rb/bR/... pairs) before the quote; a docstring in
# practice is r"""...""" or plain, but the scan must not be fooled by any of
# them. Historical bug: this was a bare startswith('"""') test, so a file whose
# docstring was written as r\"\"\"...\"\"\" (common when the text contains
# backslashes) had `import os` injected ABOVE the docstring, demoting it to a
# dead string expression and losing __doc__.
_DOCSTRING_OPEN_RE = re.compile(r'^[rRuUbB]{0,2}("""|\'\'\'|"|\')')


def _post_header_index(lines: list[str]) -> int:
    """
    Return the line index after which new top-level code should be inserted.

    Skips (in order):
      1. Leading license / comment block and blank lines.
      2. An optional module-level docstring, whatever its quote style or
         string prefix.

    This prevents imports from being injected before the module docstring,
    which would cause documentation tools to miss it.

    The docstring boundary is resolved with the AST wherever the source
    parses, since that is the only way to be right about every quote style,
    prefix, and implicit string concatenation. The textual scan below is a
    fallback for sources that do not parse (the caller may be mid-edit).
    """
    n = len(lines)

    # AST path — authoritative when the source is syntactically valid.
    try:
        tree = ast.parse("".join(lines))
    except (SyntaxError, ValueError):
        tree = None
    if tree is not None and tree.body:
        first = tree.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            # end_lineno is the 1-based last line of the docstring, which is
            # exactly the 0-based index of the line after it.
            return min(first.value.end_lineno or first.value.lineno, n)

    # Fallback: skip the license header (comment and blank lines)...
    i = 0
    while i < n and (lines[i].strip().startswith("#") or not lines[i].strip()):
        i += 1

    # ...then an optional module docstring.
    if i < n:
        match = _DOCSTRING_OPEN_RE.match(lines[i].strip())
        if match:
            quote = match.group(1)
            rest = lines[i].strip()[match.end() :]
            if rest.endswith(quote) and len(rest) >= len(quote):
                i += 1  # single-line docstring
            else:
                i += 1  # multi-line: scan for closing quotes
                while i < n and quote not in lines[i]:
                    i += 1
                i += 1  # include the line that contains the closing quotes

    return min(i, n)


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """
    Return the id() of every ast.Constant that is a docstring.

    A docstring is the first statement of a module, class, or function body
    when that statement is a bare string expression.  Excluding these prevents
    the model-name replacement from corrupting documentation text.
    """
    ids: set[int] = set()

    def _mark(stmts: list[ast.stmt]) -> None:
        if (
            stmts
            and isinstance(stmts[0], ast.Expr)
            and isinstance(stmts[0].value, ast.Constant)
            and isinstance(stmts[0].value.value, str)
        ):
            ids.add(id(stmts[0].value))

    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef)):
            _mark(node.body)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _mark(node.body)

    return ids


def _getenv_default_node_ids(tree: ast.AST) -> set[int]:
    """
    Return the id() of every ast.Constant that is the DEFAULT argument of
    an ``os.getenv``, ``os.environ.get``, or ``os.environ.setdefault`` call.

    Excluding these is a correctness requirement for the model-replacement
    path. A string like ``"gemini-3.5-flash"`` inside
    ``os.getenv("MODEL_NAME_GENERATED_1", "gemini-3.5-flash")`` is NOT a
    "raw hardcoded literal that needs promotion" — it's already serving
    as the default value of an env-var read. Replacing it with a bare
    ``os.getenv("MODEL_NAME")`` (which returns ``None`` when unset) would:

      * Break the type contract (``str`` becomes ``str | None``).
      * Cause runtime failures for callers that assume a string
        (``Gemini(model=None)``, ``.startswith(...)``, etc.).
      * Add nested-getenv complexity for zero benefit — the var was
        already environment-configurable.

    Both positional (``getenv("V", "default")``) and keyword
    (``getenv("V", default="d")``) default forms are recognised, in
    every function shape our extractor supports.
    """
    ids: set[int] = set()

    def _mark_default(call: ast.Call) -> None:
        """Add the id() of a string-literal default argument, if present."""
        default_node: ast.expr | None = None
        if len(call.args) > 1 and isinstance(call.args[1], ast.Constant):
            default_node = call.args[1]
        else:
            for kw in call.keywords:
                if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                    default_node = kw.value
                    break
        if default_node is not None and isinstance(default_node.value, str):
            ids.add(id(default_node))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # os.getenv(...)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "getenv"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and node.args
        ):
            _mark_default(node)
            continue
        # os.environ.get(...) / os.environ.setdefault(...)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in ("get", "setdefault")
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "environ"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "os"
            and node.args
        ):
            _mark_default(node)

    return ids


def _structural_exclusion_node_ids(tree: ast.AST) -> set[int]:
    """
    Return the id() of every ast.Constant that must NOT be rewritten because
    of WHERE it sits, regardless of what it looks like.

    A model string is only a candidate for promotion to an env var when it is
    a *configurable constant*. Three positions mean it is something else, and
    rewriting them corrupts working code:

    1. **An element of a collection literal** (dict key or value, list, set,
       or tuple entry). These are lookup tables and enumerations of supported
       models, not a single configurable choice::

           IMAGE_MODELS = {
               "flash": "gemini-2.5-flash-image",
               "pro":   "gemini-2.5-pro-image",
           }

       Rewriting the values makes the table resolve at import time from a
       single env var; rewriting the KEYS is worse still, because
       ``IMAGE_MODELS.get("flash")`` then never matches anything. A dict key
       must stay a static literal.

    2. **A subscript index** — ``IMAGE_MODELS["gemini-3.1"]`` is a key INTO
       such a table. Replacing it looks up a different (probably absent)
       entry.

    3. **An operand of a comparison** — ``if "gemini-3.1" in model_id`` tests
       a value rather than configuring one. Replacing it changes the
       predicate's meaning.

    Skipping a legitimate extraction is cheap (the user can lift the value by
    hand); silently breaking a lookup table is not. When in doubt this errs
    towards skipping, and the skipped literals are reported so nothing is
    hidden — see :func:`extract_skipped_model_literals`.
    """
    ids: set[int] = set()
    for parent in ast.walk(tree):
        if isinstance(parent, ast.Dict):
            # `keys` holds None for a `**expansion` entry — guard for it.
            for node in list(parent.keys) + list(parent.values):
                if isinstance(node, ast.Constant):
                    ids.add(id(node))
        elif isinstance(parent, (ast.List, ast.Set, ast.Tuple)):
            for node in parent.elts:
                if isinstance(node, ast.Constant):
                    ids.add(id(node))
        elif isinstance(parent, ast.Subscript):
            if isinstance(parent.slice, ast.Constant):
                ids.add(id(parent.slice))
        elif isinstance(parent, ast.Compare):
            for node in [parent.left, *parent.comparators]:
                if isinstance(node, ast.Constant):
                    ids.add(id(node))
    return ids


def _model_exclusion_node_ids(tree: ast.AST) -> set[int]:
    """Every Constant the model-replacement path must leave alone."""
    return (
        _docstring_node_ids(tree)
        | _getenv_default_node_ids(tree)
        | _structural_exclusion_node_ids(tree)
    )


def _flat_offset(lines: list[str], lineno: int, col: int) -> int:
    """Convert a 1-based lineno + 0-based col_offset to a flat char offset."""
    return sum(len(ln) for ln in lines[: lineno - 1]) + col


def _imports_os(tree: ast.AST) -> bool:
    """Return True if the module actually imports ``os`` (binds ``os``).

    A plain substring search for "import os" gives false positives when the
    text appears in a comment or docstring, so we inspect real import nodes.
    ``import os`` and ``import os.path`` both bind ``os``; ``import os as o``
    does not.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                if name == "os":
                    return True
    return False


_NOQA_E402_SUFFIX = "  # noqa: E402 -- must come after load_dotenv()"


def _suppress_e402_on_late_relative_imports(
    tree: ast.Module | None, lines: list[str]
) -> tuple[list[str], int]:
    """Append `# noqa: E402` to top-level `from .x import y` statements that
    sit AFTER a non-import module-level statement — those are the only ones
    Ruff actually flags as E402. A relative import at the top of the module
    (before any non-import) is fine and left alone.

    Returns ``(new_lines, count_of_lines_newly_marked)``. Idempotent: a line
    that already carries an E402 noqa comment is not touched again.
    """
    if tree is None:
        return lines, 0

    # 1. Find the line of the first non-import module-level statement.
    #    A module docstring (bare string expression at the top) is allowed
    #    to precede imports and does NOT count as a non-import.
    first_non_import_line: int | None = None
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue
        first_non_import_line = stmt.lineno
        break

    if first_non_import_line is None:
        return lines, 0  # No non-imports → no E402 possible.

    # 2. Collect line numbers of trailing relative imports (level > 0) that
    #    fall past that first-non-import boundary.
    late_lines: set[int] = set()
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.ImportFrom)
            and stmt.level > 0
            and stmt.lineno > first_non_import_line
        ):
            for lineno in range(
                stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1
            ):
                late_lines.add(lineno)

    if not late_lines:
        return lines, 0

    new_lines: list[str] = []
    marked = 0
    for i, line in enumerate(lines, start=1):
        if i in late_lines and not ("noqa" in line and "E402" in line):
            stripped = line.rstrip("\n")
            newline = line[len(stripped) :]
            new_lines.append(stripped + _NOQA_E402_SUFFIX + newline)
            marked += 1
        else:
            new_lines.append(line)
    return new_lines, marked


def _has_load_dotenv(tree: ast.AST) -> bool:
    """Return True if the module already imports load_dotenv / dotenv.

    AST-based so that a mention of "load_dotenv" in a docstring or comment does
    not suppress a legitimate injection.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "dotenv"
            and any(a.name == "load_dotenv" for a in node.names)
        ):
            return True
        if isinstance(node, ast.Import) and any(
            a.name == "dotenv" for a in node.names
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Step 4: Inject load_dotenv() into package __init__.py
# ---------------------------------------------------------------------------


def find_package_init(recipe_dir: Path) -> Path | None:
    """
    Return the __init__.py of the top-level Python package inside recipe_dir.

    Looks for immediate subdirectories that contain __init__.py, skipping
    test and hidden directories. Without this guard a ``tests/`` package that
    sorts before the real agent package would receive the load_dotenv()
    bootstrap by mistake.
    """
    for candidate in sorted(recipe_dir.iterdir()):
        if not candidate.is_dir():
            continue
        if candidate.name == "tests" or candidate.name.startswith("."):
            continue
        if (candidate / "__init__.py").exists():
            return candidate / "__init__.py"
    return None


def _last_top_level_absolute_import_line(tree: ast.Module) -> int:
    """Return the 1-based end line of the last top-level absolute import.

    Only ``tree.body`` (module-level statements) is considered, so imports
    nested inside strings, function bodies, or ``if``/``try`` blocks are
    ignored — a text scan cannot make that distinction and would place the
    bootstrap inside a docstring or a conditional block (silently disabling
    it, or producing an IndentationError). Relative imports (``from . import
    x``, ``level > 0``) are excluded on purpose: load_dotenv() must run
    before them so the environment is populated first.

    Returns 0 when there is no top-level absolute import.
    """
    last = 0
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            last = max(last, stmt.end_lineno or stmt.lineno)
        elif isinstance(stmt, ast.ImportFrom) and stmt.level == 0:
            last = max(last, stmt.end_lineno or stmt.lineno)
    return last


def inject_load_dotenv(
    init_py: Path, dry_run: bool = False
) -> tuple[bool, int]:
    """
    Ensure ``__init__.py`` bootstraps env-var loading correctly.

    Does two things, in order:

      1. Injects the ``from dotenv import load_dotenv`` + ``load_dotenv()``
         bootstrap block if not already present. Insertion goes AFTER the
         last top-level absolute import so env vars are populated before any
         downstream package code runs.

      2. Appends ``# noqa: E402`` to any trailing relative import
         (``from .x import y``) that comes AFTER a non-import statement.
         Runs BOTH when we just injected AND when the recipe author already
         wrote a bootstrap (``load_dotenv()`` + ``os.environ.setdefault(...)``)
         with trailing relative imports that were never marked — Ruff would
         otherwise flag those as E402. The ordering is deliberate (env must
         be populated before importing agent submodules), so suppression is
         the correct fix, not reordering.

    Returns ``(injected, noqa_added)``:
      * ``injected``  — True if the bootstrap block was added.
      * ``noqa_added`` — Number of trailing relative import lines newly
        marked with ``# noqa: E402``.

    Both operations are idempotent — calling again on the same file returns
    ``(False, 0)``. When ``dry_run`` is True, the file is NOT written but
    the return value still reports what would happen.
    """
    content = init_py.read_text(encoding="utf-8")

    try:
        tree: ast.Module | None = ast.parse(content)
    except SyntaxError:
        tree = None

    if tree is not None:
        already_present = _has_load_dotenv(tree)
    else:
        # Fall back to a conservative substring check on unparseable files.
        already_present = "load_dotenv" in content

    lines = content.splitlines(keepends=True)
    injected = False

    if not already_present:
        # Build the block to inject (import + blank line + snippet + blank line)
        inject_block = f"\n{LOAD_DOTENV_IMPORT}\n\n{LOAD_DOTENV_SNIPPET}\n"

        # Insert AFTER the last top-level absolute import (AST-based, so imports
        # inside docstrings/functions/conditionals are never mistaken for one).
        # Relative imports (from .something) must come AFTER load_dotenv() so
        # the env is populated before any package module-level code runs.
        last_import_line = (
            _last_top_level_absolute_import_line(tree)
            if tree is not None
            else 0
        )

        if last_import_line > 0:
            # end_lineno is 1-based; index == end_lineno inserts on the next
            # line.
            lines.insert(last_import_line, inject_block)
        else:
            # No top-level imports — insert after license header + docstring.
            lines.insert(_post_header_index(lines), inject_block)
        injected = True

        # Re-split so each list element is exactly one physical line —
        # inject_block spans multiple lines, so leaving it as one element
        # would misalign indices with AST line numbers. Re-parse so the AST
        # reflects the post-injection state (line numbers of trailing imports
        # shift).
        joined = "".join(lines)
        lines = joined.splitlines(keepends=True)
        try:
            tree = ast.parse(joined)
        except SyntaxError:
            tree = None

    # Suppress E402 on any trailing relative import that comes after a
    # non-import statement — see docstring for the two cases this covers.
    lines, noqa_added = _suppress_e402_on_late_relative_imports(tree, lines)

    if (injected or noqa_added > 0) and not dry_run:
        try:
            _write_text_atomic(init_py, "".join(lines))
        except OSError as exc:
            print(
                f"[ERROR] Could not write {init_py}: {exc} — "
                "the file was NOT modified.",
                file=sys.stderr,
            )
            return False, 0

    return injected, noqa_added


# ---------------------------------------------------------------------------
# Step 5: Ensure python-dotenv in pyproject.toml
# ---------------------------------------------------------------------------


_PYPROJECT_HEADER_RE = re.compile(r"(?m)^\[project\][ \t]*(#.*)?$")
_NEXT_SECTION_RE = re.compile(r"(?m)^\[[^\]]+\]")
_DEPS_START_RE = re.compile(r"(?m)^\s*dependencies\s*=\s*\[")
_DEP_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)")


def _dependencies_has_python_dotenv(content: str) -> bool | None:
    """
    Report whether [project].dependencies already lists python-dotenv.

    Uses tomllib (stdlib since Python 3.11) so it correctly handles extras
    (`google-adk[gcp]>=2.0.0`), single-line vs. multi-line arrays, and any
    other TOML surface syntax — avoiding the false negatives the previous
    regex-based check produced when an earlier dep contained a `]`.

    Returns:
      True  — python-dotenv is present in [project].dependencies.
      False — [project].dependencies exists and does NOT include it.
      None  — [project] or [project].dependencies is missing, or the file
              is not valid TOML.
    """
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return None
    project = data.get("project") if isinstance(data, dict) else None
    if not isinstance(project, dict):
        return None
    deps = project.get("dependencies")
    if not isinstance(deps, list):
        return None
    for dep in deps:
        if not isinstance(dep, str):
            continue
        name = _DEP_NAME_RE.match(dep)
        if name and name.group(1).lower() == "python-dotenv":
            return True
    return False


def _scan_matching_close_bracket(content: str, open_idx: int) -> int | None:
    """
    Given the offset of a '[' in `content`, return the offset of its
    matching ']'. Depth-aware (skips nested brackets) and quote-aware
    (skips brackets inside string literals). Returns None if not matched.
    """
    depth = 0
    in_str: str | None = None  # None, or the currently-open quote char.
    i = open_idx
    while i < len(content):
        c = content[i]
        if in_str is not None:
            if c == "\\":
                i += 2  # Skip the escaped character.
                continue
            if c == in_str:
                in_str = None
        elif c in ('"', "'"):
            in_str = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _find_dependencies_close_bracket(content: str) -> int | None:
    """
    Return the character offset of the closing ']' of the top-level
    [project].dependencies array, or None if the array is not present.

    Extras brackets (`google-adk[gcp]`) and any `]` inside a dep string
    are correctly ignored — see `_scan_matching_close_bracket`.
    """
    proj = _PYPROJECT_HEADER_RE.search(content)
    if not proj:
        return None
    # Bound the [project] table: up to the next top-level section header.
    remainder = content[proj.end() :]
    next_sec = _NEXT_SECTION_RE.search(remainder)
    proj_end = proj.end() + (next_sec.start() if next_sec else len(remainder))
    deps_start = _DEPS_START_RE.search(content, proj.end(), proj_end)
    if not deps_start:
        return None
    # Position of '[' is deps_start.end() - 1 (regex ends just past it).
    return _scan_matching_close_bracket(content, deps_start.end() - 1)


def _split_trailing_comment(line: str) -> tuple[str, str]:
    """
    Split a single TOML line into ``(code, comment)``.

    A ``#`` only opens a comment when it sits outside a string, so the scan
    tracks basic strings (``"..."``, backslash escapes honoured) and literal
    strings (``'...'``, no escapes). ``comment`` starts at the ``#`` and is
    ``""`` when the line has none; any whitespace between the code and the
    ``#`` stays on the ``code`` side so callers can preserve the layout.
    """
    quote: str | None = None
    escaped = False
    for idx, ch in enumerate(line):
        if quote is not None:
            if escaped:
                escaped = False
            elif quote == '"' and ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[:idx], line[idx:]
    return line, ""


def _array_body_has_entries(array_body: str) -> bool:
    """Whether an array body holds a real entry (not just blanks/comments)."""
    code_only = "".join(
        _split_trailing_comment(ln)[0] for ln in array_body.split("\n")
    )
    return bool(code_only.strip())


def _ensure_trailing_comma(trimmed: str) -> str:
    """
    Append a ``,`` to the last array entry in ``trimmed`` if it lacks one.

    The comma must land on the ENTRY, never inside a trailing comment.
    Historical bug: a plain ``trimmed.endswith(",")`` test inspected the end
    of the *comment* text, so an array ending in::

        "pyOpenSSL>=23.0",  # mTLS during auth in some envs

    grew a stray comma inside the comment, and one ending in::

        "pyOpenSSL>=23.0"  # no comma

    produced invalid TOML — the separating comma was commented out, which
    the round-trip guard in :func:`ensure_python_dotenv_dependency` then
    rejected, silently skipping the dependency insertion altogether.
    """
    lines = trimmed.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        code, comment = _split_trailing_comment(lines[i])
        if not code.strip():
            continue  # blank or comment-only line — keep looking backwards
        stripped = code.rstrip()
        if stripped.endswith(",") or stripped.endswith("["):
            return trimmed  # already terminated, or the array is empty
        gap = code[len(stripped) :]
        lines[i] = stripped + "," + gap + comment
        return "\n".join(lines)
    return trimmed


def _insert_before_close(content: str, close_idx: int) -> str:
    """
    Insert a `"python-dotenv>=1.0.0",` line into the dependencies array
    immediately before the closing ']' at `close_idx`.

    Ensures the preceding entry ends with a comma. The insertion is always
    placed on its own line with a 4-space indent — valid TOML in every case,
    and it stays readable whether the source array was multi-line or
    single-line.
    """
    prefix = content[:close_idx]
    suffix = content[close_idx:]

    # Trim trailing whitespace between the last entry and ']' so we control
    # the layout of the insertion.
    trimmed = prefix.rstrip()

    # If the array holds any real entry, make sure the last one has a
    # trailing comma before we append our own.
    array_open = trimmed.rfind("[")
    array_body = trimmed[array_open + 1 :] if array_open >= 0 else ""
    if _array_body_has_entries(array_body):
        trimmed = _ensure_trailing_comma(trimmed)

    return trimmed + '\n    "python-dotenv>=1.0.0",\n' + suffix


def _compute_pyproject_with_dotenv(content: str) -> str | None:
    """
    Return `content` with `python-dotenv>=1.0.0` added to
    `[project].dependencies`, or None if nothing should be written
    (already present, no `[project]` table, or no safe place to insert).
    """
    status = _dependencies_has_python_dotenv(content)
    if status is True:
        return None  # Already present — nothing to do.
    if status is False:
        # [project].dependencies exists but is missing python-dotenv.
        close_idx = _find_dependencies_close_bracket(content)
        if close_idx is None:
            return None  # Defensive: shouldn't happen given status.
        return _insert_before_close(content, close_idx)
    # status is None. Create the array if [project] exists, otherwise bail.
    project_header = _PYPROJECT_HEADER_RE.search(content)
    if not project_header:
        return None
    insert_at = project_header.end()
    block = '\ndependencies = [\n    "python-dotenv>=1.0.0",\n]'
    return content[:insert_at] + block + content[insert_at:]


def ensure_python_dotenv_dependency(
    pyproject: Path, dry_run: bool = False
) -> bool:
    """
    Add `python-dotenv>=1.0.0` to `[project].dependencies` if it is not
    already present there. Returns True if the file was (or would be)
    modified, False otherwise.

    Detection uses `tomllib` (parse the file, walk `[project].dependencies`)
    so extras like `google-adk[gcp]` and non-standard formatting don't
    produce false negatives — the root cause of the earlier bug that
    corrupted pyproject.toml files.

    Insertion is bracket-depth-aware and quote-aware. After building the new
    content, we round-trip it through `_dependencies_has_python_dotenv` and
    refuse to write anything that isn't valid TOML with python-dotenv now
    present under `[project].dependencies`.

    When `dry_run` is True, no file is written; the return value still
    reflects whether the dependency would be added.
    """
    if not pyproject.exists():
        return False
    content = pyproject.read_text(encoding="utf-8")
    new_content = _compute_pyproject_with_dotenv(content)
    if new_content is None:
        return False
    # Round-trip check: refuse to write output that is not valid TOML with
    # python-dotenv now under [project].dependencies. Hard backstop against
    # any future insertion bug producing invalid TOML.
    if _dependencies_has_python_dotenv(new_content) is not True:
        return False
    if not dry_run:
        try:
            _write_text_atomic(pyproject, new_content)
        except OSError as exc:
            print(
                f"[ERROR] Could not write {pyproject}: {exc} — "
                "pyproject.toml was NOT modified.",
                file=sys.stderr,
            )
            return False
    return True


# ---------------------------------------------------------------------------
# Step 6: Detect and extract hardcoded model names
# ---------------------------------------------------------------------------


def extract_hardcoded_models(
    py_files: list[Path],
) -> dict[Path, list[tuple[int, str]]]:
    """
    Find string literals that look like hardcoded model names in Python files.

    Uses AST to walk string constants and checks whether the value starts with
    any known model prefix (same list as python-validate-recipe.yml).

    Two classes of nodes are excluded to prevent false positives / incorrect
    rewrites:

      * Docstrings (see :func:`_docstring_node_ids`) — a model name
        mentioned in prose must not be replaced with an ``os.getenv`` call.
      * String literals sitting inside an ``os.getenv``/``os.environ.get``/
        ``os.environ.setdefault`` call as the DEFAULT argument (see
        :func:`_getenv_default_node_ids`) — those are already serving as
        env-var defaults; replacing them would silently regress the type
        from ``str`` to ``str | None`` and could break at runtime.
      * String literals whose POSITION means they aren't a configurable
        constant — collection-literal entries, subscript indices, and
        comparison operands (see :func:`_structural_exclusion_node_ids`).

    Returns:
      {file_path: [(line_number, model_string), ...]}
    """
    hits: dict[Path, list[tuple[int, str]]] = {}

    for py_file in py_files:
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        excluded_ids = _model_exclusion_node_ids(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if id(node) in excluded_ids:
                continue
            if not isinstance(node.value, str):
                continue
            if any(node.value.startswith(prefix) for prefix in MODEL_PREFIXES):
                hits.setdefault(py_file, []).append((node.lineno, node.value))

    return hits


def extract_skipped_model_literals(
    py_files: list[Path],
) -> dict[Path, list[tuple[int, str]]]:
    """
    Find model strings deliberately NOT rewritten because of their position.

    These are the literals :func:`_structural_exclusion_node_ids` protects —
    lookup-table entries, subscript keys, comparison operands. They are
    reported rather than silently dropped so the maintainer knows the recipe
    still hardcodes a model somewhere, and can decide whether that table
    ought to be configurable.

    Docstring mentions are excluded (prose, not code). Identical
    (line, string) pairs are de-duplicated: ``{"gemini-x": "gemini-x"}``
    holds two distinct nodes on one line, and listing it twice only makes
    the report harder to read.

    Returns:
      {file_path: [(line_number, model_string), ...]}
    """
    skipped: dict[Path, list[tuple[int, str]]] = {}

    for py_file in py_files:
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        structural = _structural_exclusion_node_ids(tree)
        docstrings = _docstring_node_ids(tree)
        seen: set[tuple[int, str]] = set()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if id(node) not in structural or id(node) in docstrings:
                continue
            if not isinstance(node.value, str):
                continue
            if not any(node.value.startswith(p) for p in MODEL_PREFIXES):
                continue
            key = (node.lineno, node.value)
            if key in seen:
                continue
            seen.add(key)
            skipped.setdefault(py_file, []).append(key)

    if skipped:
        for hits in skipped.values():
            hits.sort()

    return skipped


def _assignment_target_name(node: ast.AST) -> str | None:
    """Return the single ``Name`` target of an assignment, else None."""
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node.targets[0].id
    if (
        isinstance(node, ast.AnnAssign)
        and node.value is not None
        and isinstance(node.target, ast.Name)
    ):
        return node.target.id
    return None


def _var_name_from_target(target: str) -> str | None:
    """
    Derive an env-var name from the assignment target holding a model string.

    ``DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"`` tells us far more
    about the variable's role than the generic ``MODEL_NAME`` fallback does —
    especially in a recipe that already reads a *different* model var (an LLM)
    from the environment, where a second bare ``MODEL_NAME`` is actively
    misleading.

    Only targets that name a model are used; anything else returns None so the
    caller falls back to the generic scheme. The leading ``DEFAULT_`` is
    dropped because it describes the constant, not the model.
    """
    name = target.strip().lstrip("_").upper()
    if name.startswith("DEFAULT_"):
        name = name[len("DEFAULT_") :]
    if not name.isidentifier():
        return None
    if "MODEL" not in name:
        return None
    if name == "MODEL":
        return "MODEL_NAME"
    return name


def extract_model_var_hints(py_files: list[Path]) -> dict[str, str]:
    """
    Suggest an env-var name for each hardcoded model string, from the
    assignment target that holds it.

    Both ``DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"`` and
    ``embedding_model = cfg.get("embedding_model", "gemini-embedding-001")``
    yield the hint ``EMBEDDING_MODEL``. The same exclusions as
    :func:`extract_hardcoded_models` apply, so a model name mentioned in a
    docstring or already serving as a getenv default never votes.

    When a model string is assigned to differently-named targets across the
    recipe, the most frequent target wins, ties broken alphabetically, so the
    result is deterministic.

    Returns:
      {model_string: suggested_var_name} — only for strings with a usable hint.
    """
    votes: dict[str, dict[str, int]] = {}

    for py_file in py_files:
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        excluded_ids = _model_exclusion_node_ids(tree)

        for node in ast.walk(tree):
            target = _assignment_target_name(node)
            if target is None:
                continue
            derived = _var_name_from_target(target)
            if derived is None:
                continue
            for child in ast.walk(node.value):
                if not isinstance(child, ast.Constant):
                    continue
                if id(child) in excluded_ids:
                    continue
                if not isinstance(child.value, str):
                    continue
                if any(
                    child.value.startswith(prefix) for prefix in MODEL_PREFIXES
                ):
                    tally = votes.setdefault(child.value, {})
                    tally[derived] = tally.get(derived, 0) + 1

    return {
        model_str: sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        for model_str, tally in votes.items()
    }


def assign_model_var_names(
    model_strings: set[str],
    existing_vars: set[str] | None = None,
    hints: dict[str, str] | None = None,
) -> dict[str, str]:
    """
    Assign a standardised env var name to each unique model string.

    Rules (applied to the sorted list for determinism):
      - If ``hints`` supplies a name derived from the assignment target (see
        :func:`extract_model_var_hints`) and that name is free, use it. A
        recipe that writes ``DEFAULT_EMBEDDING_MODEL`` gets ``EMBEDDING_MODEL``
        rather than a generic ``MODEL_NAME`` that collides conceptually with
        an LLM model var it may already read.
      - Otherwise, if exactly one model is left unnamed → MODEL_NAME (no
        suffix), unless MODEL_NAME is already taken.
      - Otherwise → MODEL_NAME_GENERATED_1, MODEL_NAME_GENERATED_2, … skipping
        any index whose name is already present in existing_vars (e.g. from a
        prior run or a manually added entry in .env.example).
        The model string itself is intentionally NOT embedded in the var name —
        model identifiers and version numbers change over time, so encoding them
        in the variable name would require renaming the var (and every reference
        to it) whenever the model is upgraded. The _GENERATED_ infix signals to
        the maintainer that this name was auto-assigned and should be renamed to
        something meaningful before the recipe ships.

    Args:
      model_strings: the set of unique hardcoded model strings found in source.
      existing_vars: names already declared in .env.example (or anywhere else
        that should be treated as taken). Defaults to empty set.
      hints: optional {model_string: suggested_name} from the assignment
        targets in source.

    Returns:
      {model_string: env_var_name}
    """
    taken = set(existing_vars) if existing_vars else set()
    hints = hints or {}
    sorted_strings = sorted(model_strings)

    mapping: dict[str, str] = {}
    unresolved: list[str] = []

    # Pass 1 — honour a hint derived from the assignment target.
    for model_str in sorted_strings:
        hint = hints.get(model_str)
        if hint and hint not in taken:
            mapping[model_str] = hint
            taken.add(hint)
        else:
            unresolved.append(model_str)

    if not unresolved:
        return mapping

    # Pass 2 — a lone unnamed model gets the unsuffixed MODEL_NAME.
    if len(unresolved) == 1 and "MODEL_NAME" not in taken:
        mapping[unresolved[0]] = "MODEL_NAME"
        return mapping

    # Pass 3 — counter scheme for everything still unnamed.
    counter = 1
    for model_str in unresolved:
        while f"MODEL_NAME_GENERATED_{counter}" in taken:
            counter += 1
        var_name = f"MODEL_NAME_GENERATED_{counter}"
        mapping[model_str] = var_name
        taken.add(var_name)  # Reserve it for subsequent iterations.
        counter += 1

    return mapping


def _py_string_literal(value: str) -> str:
    """Render ``value`` as a one-line, double-quoted Python string literal."""
    body = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{body}"'


def _model_replacement(
    node: ast.AST,
    excluded_ids: set[int],
    name_map: dict[str, str],
    lines: list[str],
    keep_defaults: bool = False,
) -> tuple[int, int, str, str, str] | None:
    """
    Return (start, end, new_text, model_str, var_name) if node is a
    replaceable hardcoded model string, else None.

    ``excluded_ids`` combines docstring nodes AND string literals that
    are already serving as ``os.getenv``/``environ.get``/``setdefault``
    default arguments. The latter must NOT be replaced — see
    :func:`_getenv_default_node_ids` for the correctness rationale.

    ``keep_defaults`` controls the shape of the emitted call:

      * False (the default) → BARE ``os.getenv("VAR")``. This is the skill's
        normal rule: we do NOT write inferred defaults into Python source.
        Default values are the maintainer's decision; the skill's job is to
        lift the constant out of the source, not to also decide what fallback
        the maintainer wants. It is only SAFE because a ``load_dotenv()``
        bootstrap guarantees ``.env`` has been read by the time the call runs.

      * True → ``os.getenv("VAR", "<original literal>")``. Used when no
        bootstrap could be installed (see :func:`run_step_load_dotenv`), so
        nothing loads ``.env`` and a bare lookup would evaluate to ``None``
        at runtime — silently breaking the recipe. Preserving the literal as
        the fallback keeps behaviour identical to before the rewrite while
        still lifting the value into the environment.
    """
    if not isinstance(node, ast.Constant):
        return None
    if id(node) in excluded_ids:
        return None
    if not isinstance(node.value, str):
        return None
    var_name = name_map.get(node.value)
    if not var_name:
        return None
    start = _flat_offset(lines, node.lineno, node.col_offset)
    end = _flat_offset(lines, node.end_lineno, node.end_col_offset)
    if keep_defaults:
        new_text = f'os.getenv("{var_name}", {_py_string_literal(node.value)})'
    else:
        new_text = f'os.getenv("{var_name}")'
    return start, end, new_text, node.value, var_name


def _collect_model_replacements(
    tree: ast.AST,
    source: str,
    name_map: dict[str, str],
    keep_defaults: bool = False,
) -> tuple[list[tuple[int, int, str]], dict[str, str]]:
    """Walk ``tree`` and collect model-string replacement plans.

    Returns ``(replacements, file_substituted)`` where:
      * ``replacements`` is a list of ``(start_offset, end_offset,
        replacement_text)`` triples suitable for a reverse-order splice.
      * ``file_substituted`` maps ``{model_string: env_var_name}`` for
        every replacement planned in this file — merged into the caller's
        aggregate ONLY after the write succeeds, so a failed syntax check
        or OSError never causes ``.env.example`` to record a substitution
        that didn't actually land.

    The combined exclusion set (docstrings + getenv defaults) protects
    against corrupting documentation OR silently regressing the return
    type of a call like ``os.getenv("V", "gemini-3.5-flash")`` from
    ``str`` to ``str | None``.
    """
    excluded_ids = _model_exclusion_node_ids(tree)
    lines = source.splitlines(keepends=True)
    replacements: list[tuple[int, int, str]] = []
    file_substituted: dict[str, str] = {}
    for node in ast.walk(tree):
        replacement = _model_replacement(
            node, excluded_ids, name_map, lines, keep_defaults=keep_defaults
        )
        if replacement is None:
            continue
        start, end, new_text, model_str, var_name = replacement
        replacements.append((start, end, new_text))
        file_substituted[model_str] = var_name
    return replacements, file_substituted


def _splice_and_ensure_os_import(
    source: str, replacements: list[tuple[int, int, str]]
) -> str:
    """Apply the collected ``replacements`` to ``source`` and inject
    ``import os`` if missing.

    Splices in reverse order so earlier offsets stay valid. Injects
    ``import os`` (AST-checked so a stray occurrence in a comment or
    docstring doesn't suppress the real import) after any leading license
    + module-docstring header, with a trailing blank line for readability.
    """
    replacements.sort(key=lambda x: x[0], reverse=True)
    chars = list(source)
    for start, end, new_text in replacements:
        chars[start:end] = list(new_text)
    modified = "".join(chars)

    try:
        already_imports_os = _imports_os(ast.parse(modified))
    except SyntaxError:
        already_imports_os = "import os" in modified
    if not already_imports_os:
        mod_lines = modified.splitlines(keepends=True)
        idx = _post_header_index(mod_lines)
        # Avoid double blank lines if the line at idx is already blank.
        suffix = "\n" if idx < len(mod_lines) and mod_lines[idx].strip() else ""
        mod_lines.insert(idx, f"import os\n{suffix}")
        modified = "".join(mod_lines)
    return modified


def replace_hardcoded_models(
    py_files: list[Path],
    hits: dict[Path, list[tuple[int, str]]],
    name_map: dict[str, str],
    dry_run: bool = False,
    keep_defaults: bool = False,
) -> dict[str, str]:
    """
    Replace each hardcoded model string with the correct
    os.getenv(...) call in-place, using the mapping produced by
    assign_model_var_names().

    ``keep_defaults`` preserves the original literal as the ``os.getenv``
    fallback — see :func:`_model_replacement` for when and why.

    Replacement is AST-position-based, which means:
      - All quote styles (single, double, triple, raw) are handled correctly
        because the AST abstracts away quoting entirely.
      - Only actual string-literal AST nodes are replaced — comments,
        docstrings, and f-string fragments are never touched.

    Also ensures `import os` is present in every modified file.

    When dry_run is True, no file is written; the returned mapping still
    reports which substitutions would be made.

    Returns a dict of {model_string: env_var_name} for the substitutions made.
    """
    substituted: dict[str, str] = {}

    for py_file in hits:
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        replacements, file_substituted = _collect_model_replacements(
            tree, source, name_map, keep_defaults=keep_defaults
        )
        if not replacements:
            continue

        if dry_run:
            # In dry-run, report what would be substituted without writing.
            substituted.update(file_substituted)
            continue

        modified = _splice_and_ensure_os_import(source, replacements)

        # Syntax check before writing — refuse to write if the modified
        # source no longer parses. This catches edge cases like a model
        # string used as a constant inside an f-string expression, where
        # the replacement can produce mismatched quotes and invalid Python.
        try:
            ast.parse(modified)
        except SyntaxError as exc:
            print(
                f"[ERROR] Replacement would produce invalid Python in "
                f"{py_file} ({exc}) — skipping this file. "
                "Replace the hardcoded model string manually.",
                file=sys.stderr,
            )
            continue  # Do NOT merge file_substituted — source not modified.

        try:
            _write_text_atomic(py_file, modified)
        except OSError as exc:
            print(
                f"[ERROR] Could not write {py_file}: {exc} — "
                "the file was NOT modified.",
                file=sys.stderr,
            )
            continue  # Do NOT merge file_substituted — source not modified.

        # Write succeeded — now it is safe to record these substitutions.
        substituted.update(file_substituted)

    return substituted


# ---------------------------------------------------------------------------
# Main — step runners
# ---------------------------------------------------------------------------


def _tag(dry_run: bool) -> str:
    """Status prefix used in log lines: DRY-RUN when nothing is written."""
    return "DRY-RUN" if dry_run else "PASS"


def run_step_env_vars(
    recipe_dir: Path, py_files: list[Path], dry_run: bool = False
) -> Path:
    """Steps 2 + 3: extract env var reads and update .env.example.

    Returns the path to .env.example (created or pre-existing).
    """
    env_vars = extract_env_vars(py_files)
    if env_vars:
        print(f"\n[INFO] Detected {len(env_vars)} environment variable(s):")
        for var in sorted(env_vars):
            resolved = resolve_default(env_vars[var])
            if resolved.value is None:
                suffix = ""
            else:
                reason = looks_like_placeholder(resolved.value)
                assert resolved.winner is not None
                where = _relpath_or_name(resolved.winner.file, recipe_dir)
                if reason is None:
                    suffix = (
                        f"  (default: {resolved.value!r}"
                        f" from {resolved.winner.kind} in {where})"
                    )
                else:
                    suffix = (
                        f"  (default: {resolved.value!r}"
                        f" from {resolved.winner.kind} in {where}"
                        f" — downgraded to TODO: {reason})"
                    )
                if resolved.conflict_count:
                    suffix = suffix[:-1] + (
                        f"; differs from {resolved.conflict_count}"
                        " other source(s))"
                    )
            print(f"       {var}{suffix}")
    else:
        print("\n[INFO] No environment variable reads detected.")

    env_example = recipe_dir / ".env.example"
    added, upgraded = update_env_example(
        env_example, env_vars, dry_run=dry_run, recipe_dir=recipe_dir
    )
    if added:
        verb = "Would add" if dry_run else "Added"
        print(
            f"\n[{_tag(dry_run)}] {verb} {len(added)} variable(s) to "
            ".env.example: " + ", ".join(added)
        )
    if upgraded:
        verb = "Would upgrade" if dry_run else "Upgraded"
        # Show the resolved value + provenance so the maintainer can
        # eyeball each upgrade — this is a stronger action than an
        # append and deserves visible detail.
        lines_out = [
            f"\n[{_tag(dry_run)}] {verb} {len(upgraded)} stale TODO"
            " entr(y/ies) with real default(s) from source:"
        ]
        for var in upgraded:
            resolved = resolve_default(env_vars[var])
            assert resolved.winner is not None  # planner guarantees this
            where = _relpath_or_name(resolved.winner.file, recipe_dir)
            lines_out.append(
                f"       {var} -> {resolved.value!r}"
                f" (from {resolved.winner.kind} in {where})"
            )
        print("\n".join(lines_out))
    if not added and not upgraded:
        print(
            "\n[PASS] .env.example is already up to date — no variables added"
            " or upgraded."
        )

    return env_example


def run_step_load_dotenv(recipe_dir: Path, dry_run: bool = False) -> bool:
    """Step 4: inject load_dotenv() bootstrap into the package __init__.py,
    and suppress Ruff E402 on any trailing relative imports that come after
    non-import statements (whether we just injected them or the author had
    already written a bootstrap by hand).

    Returns whether a ``load_dotenv()`` bootstrap is in place afterwards.
    Step 6 needs this: without a bootstrap nothing reads ``.env``, so the
    bare ``os.getenv("VAR")`` calls it would otherwise emit evaluate to
    ``None`` at runtime. See :func:`_model_replacement`.
    """
    init_py = find_package_init(recipe_dir)
    if not init_py:
        print(
            "[WARN] No Python package (subdirectory with __init__.py) found. "
            "load_dotenv() injection skipped."
        )
        return False
    rel = init_py.relative_to(recipe_dir)
    injected, noqa_added = inject_load_dotenv(init_py, dry_run=dry_run)
    if injected:
        verb = "Would inject" if dry_run else "Injected"
        print(f"[{_tag(dry_run)}] {verb} load_dotenv() bootstrap into {rel}")
    else:
        print(f"[PASS] load_dotenv() already present in {rel} — skipped.")
    if noqa_added > 0:
        verb = "Would add" if dry_run else "Added"
        print(
            f"[{_tag(dry_run)}] {verb} '# noqa: E402' to {noqa_added} "
            f"trailing relative import(s) in {rel} — they come after "
            "load_dotenv()/env-bootstrap and would otherwise trigger Ruff "
            "E402 in Phase 4 (ordering is intentional: env must be "
            "populated before importing agent submodules)."
        )
    return True


def run_step_pyproject(recipe_dir: Path, dry_run: bool = False) -> None:
    """Step 5: ensure python-dotenv>=1.0.0 is in pyproject.toml."""
    pyproject = recipe_dir / "pyproject.toml"
    if not pyproject.exists():
        print("[WARN] pyproject.toml not found — skipped.")
        return

    if ensure_python_dotenv_dependency(pyproject, dry_run=dry_run):
        verb = "Would add" if dry_run else "Added"
        print(
            f"[{_tag(dry_run)}] {verb} python-dotenv>=1.0.0 to pyproject.toml "
            "dependencies."
        )
        return

    # Not modified. Distinguish "already present" from "should be added but
    # the insertion could not be made safely" (no [project] table, unparseable
    # TOML, or the round-trip check rejected the result). The latter used to
    # print a false "[PASS] already includes", letting the user believe the
    # dependency was there when it was actually still missing.
    content = pyproject.read_text(encoding="utf-8")
    if _dependencies_has_python_dotenv(content) is True:
        print("[PASS] pyproject.toml already includes python-dotenv — skipped.")
    else:
        print(
            "[WARN] Could not safely add python-dotenv to pyproject.toml — "
            'add "python-dotenv>=1.0.0" to [project].dependencies by hand.'
        )


def run_step_model_names(
    recipe_dir: Path,
    py_files: list[Path],
    env_example: Path,
    dry_run: bool = False,
    dotenv_active: bool = True,
) -> None:
    """Step 6: detect hardcoded model strings, replace with os.getenv().

    ``dotenv_active`` comes from :func:`run_step_load_dotenv`. When it is
    False nothing in the recipe reads ``.env``, so the original literal is
    preserved as the ``os.getenv`` fallback rather than emitting a bare
    lookup that would evaluate to ``None``.
    """
    model_hits = extract_hardcoded_models(py_files)
    skipped = extract_skipped_model_literals(py_files)

    if skipped:
        total = sum(len(v) for v in skipped.values())
        print(
            f"\n[INFO] Left {total} model literal(s) in place — their position "
            "means they are not a configurable constant (lookup-table entry, "
            "subscript key, or comparison operand). Rewriting them would "
            "break the code:"
        )
        for py_file, file_hits in skipped.items():
            for lineno, model_str in file_hits:
                print(
                    f"       {py_file.relative_to(recipe_dir)}:{lineno}"
                    f' — "{model_str}"'
                )

    if not model_hits:
        print("\n[PASS] No hardcoded model names detected.")
        return

    all_model_strings: set[str] = {
        model_str
        for file_hits in model_hits.values()
        for _lineno, model_str in file_hits
    }
    existing_vars = read_defined_vars(env_example)
    hints = extract_model_var_hints(py_files)
    name_map = assign_model_var_names(all_model_strings, existing_vars, hints)

    print("\n[INFO] Detected hardcoded model name(s):")
    for py_file, file_hits in model_hits.items():
        for lineno, model_str in file_hits:
            print(
                f"       {py_file.relative_to(recipe_dir)}:{lineno}"
                f' — "{model_str}" → {name_map[model_str]}'
            )

    keep_defaults = not dotenv_active
    if keep_defaults:
        print(
            "[INFO] No load_dotenv() bootstrap is in place, so nothing would "
            "read .env at runtime. Keeping the original literal as the "
            "os.getenv() fallback instead of emitting a bare lookup that "
            "would evaluate to None."
        )

    substituted = replace_hardcoded_models(
        py_files,
        model_hits,
        name_map,
        dry_run=dry_run,
        keep_defaults=keep_defaults,
    )
    if not substituted:
        return

    var_to_model = {
        var_name: model_str for model_str, var_name in substituted.items()
    }
    added_models = write_model_vars_to_env_example(
        env_example,
        var_to_model,
        dry_run=dry_run,
    )

    replace_verb = "Would replace" if dry_run else "Replaced"
    for model_str, var_name in substituted.items():
        call = (
            f'os.getenv("{var_name}", "{model_str}")'
            if keep_defaults
            else f'os.getenv("{var_name}")'
        )
        print(
            f'[{_tag(dry_run)}] {replace_verb} hardcoded "{model_str}" with'
            f" {call} in source."
        )
    if added_models:
        add_verb = "Would add" if dry_run else "Added"
        for var in added_models:
            print(
                f"[{_tag(dry_run)}] {add_verb} {var}={var_to_model[var]}"
                f" to .env.example (rename the variable if needed)."
            )
    else:
        print("[PASS] All MODEL_NAME_* vars already in .env.example — skipped.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Scan a Python recipe and ensure all env vars are declared in "
            ".env.example, loaded via load_dotenv(), and python-dotenv is "
            "listed in pyproject.toml."
        )
    )
    parser.add_argument(
        "--recipe-dir",
        required=True,
        help="Path to the root of the Python recipe (e.g. contrib/python/my-recipe)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report what would change; do not modify any files.",
    )
    args = parser.parse_args()
    dry_run = args.dry_run

    recipe_dir = Path(args.recipe_dir).resolve()
    if not recipe_dir.is_dir():
        print(
            f"[ERROR] Recipe directory not found: {recipe_dir}", file=sys.stderr
        )
        sys.exit(1)

    print(f"\n{'=' * 50}")
    print("  extract-python-environment-variables")
    print(f"  Recipe: {recipe_dir}")
    if dry_run:
        print("  MODE: dry-run (no files will be modified)")
    print(f"{'=' * 50}\n")

    py_files = find_python_files(recipe_dir)
    print(f"[INFO] Scanning {len(py_files)} Python file(s) (tests/ excluded):")
    for f in py_files:
        print(f"       {f.relative_to(recipe_dir)}")

    env_example = run_step_env_vars(recipe_dir, py_files, dry_run=dry_run)
    dotenv_active = run_step_load_dotenv(recipe_dir, dry_run=dry_run)
    run_step_pyproject(recipe_dir, dry_run=dry_run)
    run_step_model_names(
        recipe_dir,
        py_files,
        env_example,
        dry_run=dry_run,
        dotenv_active=dotenv_active,
    )

    print(f"\n{'=' * 50}")
    if dry_run:
        print("  Done (dry-run — no files were modified).")
    else:
        print("  Done.")
    print(f"{'=' * 50}\n")


if __name__ == "__main__":
    main()
