"""
Generate a lightweight `tests/test_runnability.py` for a Python recipe.

What the generated test does — nothing more:
  1. Import the recipe's agent module.
  2. Assert `root_agent is not None`.
  3. If the module also defines a top-level `app`, assert it's non-None too.

The interesting work is generating the *right* test for THIS recipe. The
recipe's agent.py often has import-time side effects (`google.auth.default()`,
`vertexai.init()`) that would crash a naive `import`. We parse agent.py with
`ast` to figure out which mocks/env vars are actually needed and only include
those — no dead safety belt for recipes that don't need it, no missing mock
for recipes that do.

Detection scope:
  - `agent.py` plus every ancestor package `__init__.py` (all of which run
    when the test does `import a.b.agent`): top-level assignments
    (root_agent, app — from agent.py only, by convention), calls
    (vertexai.init, google.auth.default) and env-var reads. Call/import
    detection uses `ast.walk`, so it is deliberately broad — a match inside
    a function body still flags the recipe. A false positive is cheap (a
    no-op patch); a false negative would crash the generated test at import.
  - All `.py` files under the recipe directory tree: for the env-var
    convention scan (e.g. INTEGRATION_TEST, read by helpers that agent.py
    imports at module-load time — such helpers need not sit beside
    agent.py).

Usage:

    python generate_runnability_test.py --recipe-dir <RECIPE_DIR> --dry-run
    python generate_runnability_test.py --recipe-dir <RECIPE_DIR>
    python generate_runnability_test.py --recipe-dir <RECIPE_DIR> --overwrite
    python generate_runnability_test.py --recipe-dir <RECIPE_DIR> \\
        --agent-file path/to/entry.py

Output: JSON report on stdout, structured for a coding agent to render.

Exit codes:
  0  dry-run: always. apply: file written (or was already correct).
  1  apply mode refused to overwrite an existing file (re-run with
     --overwrite to accept clobbering).
  2  hard error: recipe-dir invalid, agent.py not found, or parse failure.
"""

# `from __future__ import annotations` keeps every annotation a lazy string,
# so the PEP 604 `X | None` fields on the Report/Detections dataclasses are
# NOT evaluated at class-definition time. Without it, defining those classes
# raises `TypeError: unsupported operand type(s) for |` on Python 3.9 (the
# `|` type-union operator is 3.10+), which the system `python3` on macOS may
# still be. The script uses no other 3.10+ runtime feature, so this alone
# lets it run anywhere 3.9+. (The SKILL.md also invokes it via
# `uv run --no-project python3` to guarantee a modern interpreter.)
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:  # tomllib is stdlib from 3.11; this script still supports 3.9.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter
    tomllib = None  # type: ignore[assignment]

# ---------- Constants ------------------------------------------------------

# Directories the recipe walker must never descend into.
#
# Two reasons for exclusion:
#   1. Virtualenvs, build artefacts, and tool caches contain third-party
#      code whose side-effects (INTEGRATION_TEST reads, vertexai calls) are
#      not the recipe's runtime code path — scanning them produces noise.
#   2. `tests/` is excluded so the generator does not read its own output
#      (a hand-written test that sets `INTEGRATION_TEST` via
#      `os.environ["INTEGRATION_TEST"] = "TRUE"` would otherwise be seen as
#      a recipe-source signal and self-perpetuate the env-var injection).
#      It also prevents a `tests/agent.py` test double from being picked
#      as the entry point instead of the real `app/agent.py`.
#
# Dot-directories (`.venv`, `.git`, `.tox`, `.pytest_cache`, `.ruff_cache`,
# `.mypy_cache`, `.idea`, `.vscode`, …) are pruned by a separate rule in
# `_walk_recipe_pyfiles`. `*.egg-info` suffix pruning is likewise there.
IGNORED_DIRS_NON_DOT = frozenset(
    {
        "venv",
        "env",
        "build",
        "dist",
        "__pycache__",
        "node_modules",
        "tests",
    }
)

LICENSE_HEADER = """\
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


CONFTEST_BODY = '''"""Put the recipe root on ``sys.path`` for the runnability test.

The test imports ``{module_name}`` from the recipe root, but this recipe
declares no ``[build-system]``, so nothing installs it as a package. Under
pytest's default ``prepend`` import mode only the test file's own directory
goes on ``sys.path``, so the import would fail with ``ModuleNotFoundError``.

Delete this file once the recipe declares a ``[build-system]`` and is
installed with ``uv sync`` — the shim is then redundant.
"""

import sys
from pathlib import Path

RECIPE_ROOT = Path(__file__).resolve().parent.parent
if str(RECIPE_ROOT) not in sys.path:
    sys.path.insert(0, str(RECIPE_ROOT))
'''


# ---------- Report dataclasses ---------------------------------------------


@dataclass
class Detections:
    has_root_agent: bool = False
    has_app: bool = False
    needs_vertexai_patch: bool = False
    needs_gcp_project_env: bool = False
    needs_integration_test_env: bool = False


@dataclass
class Report:
    recipe_dir: str
    mode: str  # "dry-run" or "apply"
    agent_file: str | None = None
    module_name: str | None = None
    target_path: str | None = None
    detections: Detections = field(default_factory=Detections)
    test_content: str | None = None
    action: str | None = None  # would_write / wrote / refused_overwrite / error
    message: str = ""
    # How `import <module_name>` is expected to resolve when pytest runs.
    # One of: installable / pythonpath-ini / existing-conftest /
    # generated-conftest / unresolved.
    import_support: str | None = None
    conftest_path: str | None = None
    conftest_action: str | None = None  # would_write / wrote / skipped / none
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "recipe_dir": self.recipe_dir,
                "mode": self.mode,
                "agent_file": self.agent_file,
                "module_name": self.module_name,
                "target_path": self.target_path,
                "detections": asdict(self.detections),
                "import_support": self.import_support,
                "conftest_path": self.conftest_path,
                "conftest_action": self.conftest_action,
                "test_content": self.test_content,
                "action": self.action,
                "message": self.message,
                "warnings": self.warnings,
            },
            indent=2,
        )


# ---------- agent.py discovery ---------------------------------------------


def _walk_recipe_pyfiles(recipe_dir: Path) -> list[Path]:
    """Yield every .py file under recipe_dir, safely pruning venv/build/etc."""
    found: list[Path] = []
    for root, dirs, files in os.walk(recipe_dir):
        dirs[:] = [
            d
            for d in dirs
            if d not in IGNORED_DIRS_NON_DOT
            and not d.startswith(".")
            and not d.endswith(".egg-info")
        ]
        for name in files:
            if name.endswith(".py"):
                found.append(Path(root) / name)
    return sorted(found)


def find_agent_file(recipe_dir: Path, override: Path | None) -> Path | None:
    """Return the agent.py to generate a test for.

    If override is given, resolve it (relative to recipe_dir if not absolute).
    Otherwise, prefer the shallowest agent.py under recipe_dir.
    """
    if override:
        path = override if override.is_absolute() else recipe_dir / override
        return path if path.is_file() else None

    candidates = []
    for p in _walk_recipe_pyfiles(recipe_dir):
        if p.name == "agent.py":
            depth = len(p.relative_to(recipe_dir).parts)
            candidates.append((depth, str(p), p))
    if not candidates:
        return None
    # Shallowest first, then alphabetical for a deterministic tie-break.
    candidates.sort()
    return candidates[0][2]


def module_path_from_file(agent_file: Path, recipe_dir: Path) -> str:
    """Convert `<recipe>/app/agent.py` to `app.agent`."""
    rel = agent_file.relative_to(recipe_dir)
    parts = list(rel.parts)
    parts[-1] = parts[-1].removesuffix(".py")
    return ".".join(parts)


# ---------- Import-support detection ---------------------------------------
#
# The generated test does `import <module_name>`, which only resolves if the
# RECIPE ROOT is on sys.path. That is not automatic: under pytest's default
# `prepend` import mode, only the test file's own basedir (`<recipe>/tests`)
# is inserted. The recipe root gets there when the project is installed
# (`uv sync`, which needs a [build-system]) or when something explicitly puts
# it there.
#
# Historical gap: this was assumed rather than checked, so a recipe without a
# [build-system] — common for vertical plugins under plugins/, where code lives
# in a plain scripts/ directory — got a test that always died with
# ModuleNotFoundError, while the pipeline's py_compile check still reported
# success.


_BUILD_SYSTEM_RE = re.compile(r"^\s*\[build-system\]", re.MULTILINE)


def _load_pyproject(pyproject: Path) -> dict[str, Any] | None:
    """Parse pyproject.toml, or None when unavailable/unparseable."""
    if tomllib is None or not pyproject.is_file():
        return None
    try:
        return tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _has_build_system(pyproject: Path) -> bool:
    """Whether pyproject.toml declares a [build-system] table."""
    data = _load_pyproject(pyproject)
    if data is not None:
        return isinstance(data.get("build-system"), dict)
    # tomllib unavailable (< 3.11) or unparseable — fall back to a text scan
    # rather than silently claiming the recipe is not installable.
    try:
        return bool(_BUILD_SYSTEM_RE.search(pyproject.read_text("utf-8")))
    except (OSError, UnicodeDecodeError):
        return False


def _pytest_pythonpath_covers_root(pyproject: Path) -> bool:
    """Whether [tool.pytest.ini_options].pythonpath puts the root on sys.path."""
    data = _load_pyproject(pyproject)
    if not data:
        return False
    ini = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    entries = ini.get("pythonpath")
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, list):
        return False
    return any(str(e).strip() in {".", "./", ""} for e in entries)


def _extends_syspath(path: Path) -> bool:
    """Whether a file actually touches ``sys.path`` in executable code.

    AST-based on purpose: a substring search counts a comment or docstring
    that merely *mentions* sys.path (``# does not touch sys.path``), which
    would wrongly certify a conftest as providing import support. Comments
    never appear in the AST and a prose mention is an ``ast.Constant``, not
    an attribute access, so both are excluded for free.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        # `sys.path...`
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "path"
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
        ):
            return True
        # `from sys import path`
        if isinstance(node, ast.ImportFrom) and node.module == "sys":
            if any(alias.name == "path" for alias in node.names):
                return True
    return False


def detect_import_support(recipe_dir: Path) -> str:
    """Classify how the recipe root reaches sys.path under pytest.

    Returns one of ``installable`` / ``pythonpath-ini`` /
    ``existing-conftest`` / ``unresolved``.
    """
    pyproject = recipe_dir / "pyproject.toml"
    if _has_build_system(pyproject):
        return "installable"
    if _pytest_pythonpath_covers_root(pyproject):
        return "pythonpath-ini"
    # A conftest.py AT THE RECIPE ROOT is sufficient whatever it contains:
    # under prepend import mode pytest puts each conftest's own directory on
    # sys.path, and for this one that directory IS the recipe root. Verified
    # empirically — an entirely empty root conftest.py makes `import pkg.mod`
    # resolve.
    if (recipe_dir / "conftest.py").is_file():
        return "existing-conftest"
    # A tests/conftest.py, by the same rule, only adds `<recipe>/tests` — which
    # does NOT help. It counts only if it explicitly extends sys.path itself.
    tests_conftest = recipe_dir / "tests" / "conftest.py"
    if tests_conftest.is_file() and _extends_syspath(tests_conftest):
        return "existing-conftest"
    return "unresolved"


# ---------- AST detection helpers ------------------------------------------


def _call_target(call: ast.Call) -> str | None:
    """Dotted attribute chain for a Call's callee. `vertexai.init` etc."""
    parts: list[str] = []
    node: Any = call.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _collect_target_names(target: ast.expr) -> set[str]:
    """Return every `Name` that this assignment target binds.

    Handles the four forms the Python grammar allows on the left of `=`:
      - `x = ...`                          -> ast.Name
      - `x, y = ...` / `[x, y] = ...`      -> ast.Tuple / ast.List
      - `*rest = ...`                      -> ast.Starred
      - `obj.attr = ...` / `obj[key] = ...` -> ast.Attribute / ast.Subscript
        (ignored — these don't bind a module-level name)
    """
    names: set[str] = set()
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            names |= _collect_target_names(elt)
    elif isinstance(target, ast.Starred):
        names |= _collect_target_names(target.value)
    # Attribute / Subscript targets don't create module-level names — skip.
    return names


def find_top_level_assignments(module: ast.Module) -> set[str]:
    """Names bound at module top level by Assign or AnnAssign.

    Handles tuple / list / starred unpacking so patterns like
    `root_agent, app = _build()` still surface both names.
    """
    names: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                names |= _collect_target_names(target)
        elif isinstance(node, ast.AnnAssign):
            # PEP 526: AnnAssign has a single simple target — no unpacking
            # allowed at the grammar level, but the target may still be a
            # non-Name (Attribute/Subscript). Reuse the same helper.
            names |= _collect_target_names(node.target)
    return names


def find_call_targets(module: ast.Module) -> set[str]:
    """Dotted names of every Call anywhere in the module (any depth)."""
    names: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Call):
            n = _call_target(node)
            if n:
                names.add(n)
    return names


def find_env_var_reads(module: ast.Module) -> set[str]:
    """Env var names read via os.getenv/os.environ.get/os.environ[...]."""
    vars: set[str] = set()
    for node in ast.walk(module):
        # os.getenv("VAR", ...) / os.environ.get("VAR", ...)
        if isinstance(node, ast.Call) and node.args:
            target = _call_target(node)
            if target in ("os.getenv", "os.environ.get"):
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(
                    first.value, str
                ):
                    vars.add(first.value)
        # os.environ["VAR"] — only a READ (ast.Load) counts. A write
        # (`os.environ["VAR"] = ...`, ast.Store) or delete (ast.Del) means
        # the recipe SETS the variable itself and does not depend on the
        # test providing one, so it must not trigger the env-var setdefault
        # (or, for GOOGLE_CLOUD_PROJECT, the google.auth patch).
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            base = node.value
            if (
                isinstance(base, ast.Attribute)
                and isinstance(base.value, ast.Name)
                and base.value.id == "os"
                and base.attr == "environ"
            ):
                sl = node.slice
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                    vars.add(sl.value)
    return vars


def find_imported_modules(module: ast.Module) -> set[str]:
    """Return the dotted module names referenced by any import statement.

    Both `Import` and `ImportFrom` forms are covered so aliased imports
    (`from vertexai import init`, `import vertexai as vai`) still contribute
    their canonical module name. Used as a fallback signal for detectors
    that would otherwise miss non-standard import styles: e.g. a call site
    `init()` after `from vertexai import init` is parsed as a bare `Name`
    and cannot be resolved by dotted-path matching alone.

    Values are the dotted module names — NOT the local aliases.
    """
    modules: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b.c` -> "a.b.c"; `import a.b.c as x` -> "a.b.c"
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            # `from a.b import x` -> "a.b"; also record the fully qualified
            # `a.b.x` so a caller can match either the parent or the leaf.
            modules.add(node.module)
            for alias in node.names:
                if alias.name != "*":
                    modules.add(f"{node.module}.{alias.name}")
    return modules


def _module_referenced(modules: set[str], prefix: str) -> bool:
    """True if any imported module equals `prefix` or starts with `prefix.`.

    Example: `_module_referenced({"vertexai.preview"}, "vertexai")` → True.
    """
    return prefix in modules or any(m.startswith(f"{prefix}.") for m in modules)


def detect_features(agent_file: Path, recipe_dir: Path) -> Detections:
    """Run every AST/scan detector against agent.py + every ancestor package
    __init__.py (all of which run when the test does `import a.b.agent`), and
    return the flags the generator needs to emit the right test."""
    agent_module = ast.parse(
        agent_file.read_text(encoding="utf-8"),
        filename=str(agent_file),
    )

    # Every ancestor package's __init__.py runs BEFORE agent.py's own
    # top-level code when Python resolves `import a.b.agent`: a/__init__.py
    # first, then a/b/__init__.py, then the module. So a side effect in ANY
    # ancestor package (google.auth.default(), vertexai.init(), env-var
    # reads) matters for the test's ability to import cleanly — not just the
    # immediate parent. Historical bug: cross-session-memory has
    # `_, project_id = google.auth.default()` in __init__.py; scanning only
    # agent.py missed it and the generated test crashed at import time in CI
    # without ADC. Walking only the immediate parent reopened the same gap
    # for recipes nested two or more levels deep (e.g. app/agents/agent.py).
    side_effect_modules: list[ast.Module] = [agent_module]
    for pkg_dir in agent_file.parents:
        if pkg_dir == recipe_dir:
            break  # recipe root is on sys.path, not an imported package.
        package_init = pkg_dir / "__init__.py"
        if not package_init.is_file():
            continue
        if package_init.resolve() == agent_file.resolve():
            continue  # entry point IS an __init__.py; don't double-parse.
        try:
            side_effect_modules.append(
                ast.parse(
                    package_init.read_text(encoding="utf-8"),
                    filename=str(package_init),
                )
            )
        except (SyntaxError, UnicodeDecodeError):
            # Broken __init__.py isn't ours to fix; skip it and let the
            # generated test surface the problem when run.
            pass

    # Top-level assignments (`root_agent`, `app`) come from agent.py by
    # convention — even if a package re-exports them via __init__.py, the
    # test does `import <module>` which is agent.py, not the package.
    assignments = find_top_level_assignments(agent_module)

    # Side-effect signals: union across agent.py AND __init__.py.
    calls: set[str] = set()
    imports: set[str] = set()
    env_reads: set[str] = set()
    for m in side_effect_modules:
        calls |= find_call_targets(m)
        imports |= find_imported_modules(m)
        env_reads |= find_env_var_reads(m)

    d = Detections()
    d.has_root_agent = "root_agent" in assignments
    d.has_app = "app" in assignments

    # Primary signal: `vertexai.init` shows up in call targets. Fallback:
    # any import of a vertexai module — catches `from vertexai import init`
    # (where the call site is a bare `Name` and cannot be resolved by
    # dotted-path matching). Same idea for google.auth. False positives
    # (importing vertexai but never calling init) are cheap: a no-op patch.
    d.needs_vertexai_patch = "vertexai.init" in calls or _module_referenced(
        imports, "vertexai"
    )
    d.needs_gcp_project_env = (
        "google.auth.default" in calls
        or "GOOGLE_CLOUD_PROJECT" in env_reads
        or _module_referenced(imports, "google.auth")
    )
    # INTEGRATION_TEST is a per-package convention (agent.py imports helpers
    # like retrievers.py, and THOSE read the env var at module-load time via
    # top-level function calls). Scan every recipe .py file for it, not just
    # agent.py — otherwise we miss it for rag-*-style recipes. tests/ is
    # excluded by IGNORED_DIRS_NON_DOT so hand-written test files that set
    # INTEGRATION_TEST themselves don't cause the detector to self-perpetuate.
    d.needs_integration_test_env = _any_pyfile_reads_env_var(
        recipe_dir, "INTEGRATION_TEST"
    )
    return d


def _any_pyfile_reads_env_var(recipe_dir: Path, var: str) -> bool:
    """True if any .py file under recipe_dir (safely walked) reads env var
    `var` via os.getenv / os.environ.get / os.environ[...]."""
    for p in _walk_recipe_pyfiles(recipe_dir):
        try:
            module = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except (SyntaxError, UnicodeDecodeError):
            continue
        if var in find_env_var_reads(module):
            return True
    return False


# ---------- Test-file generation -------------------------------------------


def generate_test_source(
    module_name: str,
    detections: Detections,
) -> str:
    """Emit the test_runnability.py contents for THIS recipe.

    Chooses between two shapes:
      * Minimal:  module-level `import <module>` + assertions.
      * Guarded:  env-var setup + optional `with patch(...)` around the
                  import, done inside the test function.

    Post-processes through `ruff format` when available so long emitted
    lines (e.g. multi-patch `with (...):` blocks) come out already wrapped
    per the repo's ruff config, and no reformat is needed downstream.
    """
    needs_env = (
        detections.needs_gcp_project_env
        or detections.needs_integration_test_env
    )
    has_side_effects = needs_env or detections.needs_vertexai_patch

    if not has_side_effects:
        raw = _emit_minimal(module_name, detections)
    else:
        raw = _emit_guarded(module_name, detections)
    return _ruff_format_if_available(raw)


def _ruff_format_if_available(source: str) -> str:
    """Best-effort: pipe `source` through `ruff format --stdin-filename=... -`.

    Returns the raw source unchanged if `ruff` isn't on PATH, if the format
    subprocess errors, or if it takes too long. The generator's contract is
    still "emit valid Python" — this is a nicety that removes the need for
    the user to run `ruff format` on the generated file separately.
    """
    try:
        result = subprocess.run(
            ["ruff", "format", "--stdin-filename=test_runnability.py", "-"],
            input=source,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return source
    return result.stdout


def _emit_minimal(module_name: str, detections: Detections) -> str:
    """Simplest test: import the module, assert the globals."""
    lines = [
        LICENSE_HEADER.rstrip(),
        '"""Runnability tests for the recipe."""',
        "",
        f"import {module_name}",
        "",
        "",
        "def test_agent_runnability() -> None:",
        '    """Verify agent.py imports and defines the expected globals."""',
        f"    assert {module_name}.root_agent is not None",
    ]
    if detections.has_app:
        lines.append(f"    assert {module_name}.app is not None")
    return "\n".join(lines) + "\n"


def _emit_guarded(module_name: str, detections: Detections) -> str:
    """Full-safety-belt test: env-var setup + optional import-time patches.

    Import happens inside a `with patch(...)` context whenever the recipe
    has import-time side effects that would fail without live credentials
    (vertexai.init, google.auth.default). The patches are only needed while
    the module loads; assertions run AFTER the with block so the test file
    honestly reflects what actually needs mocking (the load, not the
    checks).
    """
    # ---- build the list of patches (each is a single with-item string) --
    patches: list[str] = []
    needs_magicmock = False
    if detections.needs_vertexai_patch:
        patches.append('patch("vertexai.init")')
    if detections.needs_gcp_project_env:
        # `google.auth.default()` requires ADC to be discoverable at call
        # time. In CI or a fresh clone that's usually not set up, so any
        # unconditional call at import time crashes the test. Patch it to
        # return a plausible (credentials, project_id) tuple. Env-var
        # setdefault below still helps for recipes that gate the call on
        # GOOGLE_CLOUD_PROJECT — for those the patch is a no-op fallback.
        patches.append(
            'patch("google.auth.default", '
            'return_value=(MagicMock(), "test-project"))'
        )
        needs_magicmock = True

    # ---- imports ----
    imports = ["import os"]
    if patches:
        mock_names = ["MagicMock", "patch"] if needs_magicmock else ["patch"]
        imports.append(f"from unittest.mock import {', '.join(mock_names)}")

    # ---- explanatory comment (matches the hand-written recipe tests) ----
    comment_lines = _build_setup_comment(detections)

    # ---- env-var setup lines ----
    env_lines: list[str] = []
    if detections.needs_gcp_project_env:
        env_lines.append(
            '    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")'
        )
    if detections.needs_integration_test_env:
        env_lines.append(
            '    os.environ.setdefault("INTEGRATION_TEST", "TRUE")'
        )

    # ---- import block (with-patch context OR plain function-level import) --
    if patches:
        import_lines = _emit_with_block(patches, module_name)
    else:
        import_lines = [f"    import {module_name}"]

    # ---- assertions (always at function body level, not inside with) -----
    assertion_lines = [f"    assert {module_name}.root_agent is not None"]
    if detections.has_app:
        assertion_lines.append(f"    assert {module_name}.app is not None")

    # ---- assemble ----
    lines: list[str] = [
        LICENSE_HEADER.rstrip(),
        '"""Runnability tests for the recipe."""',
        "",
        *imports,
        "",
        "",
        "def test_agent_runnability() -> None:",
        '    """Verify agent.py imports and defines the expected globals."""',
        *comment_lines,
        *env_lines,
        "",
        *import_lines,
        "",
        *assertion_lines,
    ]
    return "\n".join(lines) + "\n"


def _emit_with_block(patches: list[str], module_name: str) -> list[str]:
    """Return the lines for a `with <patches>: import <module>` block.

    Single-patch form uses one line: `with patch(...):`. Multi-patch form
    uses the PEP 8 parenthesised style (`with (\\n <patch>,\\n ...):`)
    which ruff format prefers once the combined line exceeds 79 chars —
    emitting it that way up front avoids a mandatory reformat after
    generation.
    """
    if len(patches) == 1:
        return [
            f"    with {patches[0]}:",
            f"        import {module_name}",
        ]
    body = [f"        {p}," for p in patches]
    return [
        "    with (",
        *body,
        "    ):",
        f"        import {module_name}",
    ]


def _build_setup_comment(detections: Detections) -> list[str]:
    """Return the `# ...` comment lines that explain why env-var setup and
    the patches are needed. Each returned line already includes its
    leading 4-space indent and `# ` marker, wrapped to fit under 80 chars.
    """
    bits: list[str] = []
    if detections.needs_gcp_project_env:
        # Two things happen for this signal: env-var setdefault (helps
        # recipes that gate google.auth.default() on GOOGLE_CLOUD_PROJECT)
        # AND a google.auth.default() patch (needed when the recipe calls
        # it unconditionally at import time). Comment mentions both.
        bits.append(
            "provide a dummy GCP project and patch google.auth.default() "
            "so import-time credential lookups don't need ADC"
        )
    if detections.needs_vertexai_patch:
        bits.append("mock vertexai.init to avoid a real GCP call")
    if not bits and detections.needs_integration_test_env:
        bits.append(
            "set INTEGRATION_TEST so helpers imported by agent.py take "
            "their mock path"
        )
    prose = ", and ".join(bits) if bits else "Import-time setup"

    wrapped = textwrap.wrap(
        f"{prose} — the setup must happen before the import.",
        width=76,
        break_long_words=False,
    )
    return [f"    # {line}" for line in wrapped]


# ---------- Orchestration --------------------------------------------------


def _analyze(
    recipe_dir: Path, agent_file_override: Path | None, report: Report
) -> bool:
    """Populate report.{agent_file, module_name, detections}.

    Returns True on success, False if a hard error was written to the report
    (in which case the caller should return the report as-is).
    """
    agent_file = find_agent_file(recipe_dir, agent_file_override)
    if agent_file is None:
        report.action = "error"
        if agent_file_override is not None:
            # The user passed --agent-file but it didn't resolve to a file;
            # tell them exactly that instead of pointing them back to the
            # flag they already used.
            resolved = (
                agent_file_override
                if agent_file_override.is_absolute()
                else recipe_dir / agent_file_override
            )
            report.message = (
                f"The path passed with --agent-file ({resolved}) does not "
                f"exist or is not a file."
            )
        else:
            report.message = (
                f"No agent.py found under {recipe_dir}. If your recipe uses "
                f"a different entry-point file, pass --agent-file <path> "
                f"(relative to the recipe dir or absolute)."
            )
        return False
    report.agent_file = str(agent_file)

    try:
        report.module_name = module_path_from_file(agent_file, recipe_dir)
    except ValueError as e:
        report.action = "error"
        report.message = (
            f"agent-file {agent_file} is not inside recipe-dir "
            f"{recipe_dir}: {e}"
        )
        return False

    try:
        report.detections = detect_features(agent_file, recipe_dir)
    except SyntaxError as e:
        report.action = "error"
        report.message = f"Failed to parse {agent_file}: {e}"
        return False
    except UnicodeDecodeError as e:
        # A non-UTF-8 source file is the user's problem to fix, not a bug in
        # this skill — say so plainly instead of letting the generic
        # top-level safety net blame the skill.
        report.action = "error"
        report.message = (
            f"{agent_file} is not valid UTF-8 ({e}). Fix the file's "
            f"encoding and re-run."
        )
        return False

    if not report.detections.has_root_agent:
        report.message = (
            f"{agent_file} does not appear to have a top-level "
            f"`root_agent = ...` assignment; the generated test will fail "
            f"unless the module defines one when imported."
        )
    return True


def run(
    recipe_dir: Path,
    agent_file_override: Path | None,
    dry_run: bool,
    overwrite: bool,
) -> Report:
    mode = "dry-run" if dry_run else "apply"
    report = Report(recipe_dir=str(recipe_dir), mode=mode)

    if not _analyze(recipe_dir, agent_file_override, report):
        return report

    report.test_content = generate_test_source(
        report.module_name, report.detections
    )
    tests_dir = recipe_dir / "tests"
    target = tests_dir / "test_runnability.py"
    report.target_path = str(target)

    # Will `import <module_name>` actually resolve? If not, the test compiles
    # fine but dies at run time — so provide the missing sys.path shim rather
    # than emitting a test that cannot pass.
    report.import_support = detect_import_support(recipe_dir)
    conftest = tests_dir / "conftest.py"
    if report.import_support == "unresolved":
        if conftest.exists():
            # Never clobber a conftest we did not write; it may already do
            # something we would break.
            report.conftest_action = "skipped"
            report.conftest_path = str(conftest)
            report.warnings.append(
                f"{recipe_dir / 'pyproject.toml'} declares no [build-system], "
                f"so the recipe is not installed and the recipe root is not "
                f"on sys.path. {conftest} already exists and was left alone — "
                f"confirm it puts the recipe root on sys.path, or "
                f"`import {report.module_name}` will fail at run time."
            )
        else:
            report.conftest_action = "would_write" if dry_run else "wrote"
            report.conftest_path = str(conftest)
            report.import_support = "generated-conftest"
            report.warnings.append(
                f"No [build-system] in pyproject.toml, so nothing installs "
                f"the recipe and `import {report.module_name}` would fail "
                f"under pytest. Generated {conftest} to put the recipe root "
                f"on sys.path. Adding a [build-system] is the better fix; "
                f"delete the conftest once you do."
            )

    if dry_run:
        report.action = "would_write"
        return report

    if target.exists() and not overwrite:
        report.action = "refused_overwrite"
        report.message = (
            f"{target} already exists. Re-run with --overwrite to accept "
            f"clobbering."
        )
        return report

    try:
        tests_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(report.test_content, encoding="utf-8")
    except OSError as e:
        report.action = "error"
        report.message = f"Failed to write {target}: {e}"
        return report

    if report.conftest_action == "wrote":
        content = LICENSE_HEADER + CONFTEST_BODY.format(
            module_name=report.module_name
        )
        try:
            conftest.write_text(content, encoding="utf-8")
        except OSError as e:
            # The test itself landed; report the shim failure without
            # pretending the whole run failed.
            report.conftest_action = "error"
            report.warnings.append(f"Failed to write {conftest}: {e}")

    report.action = "wrote"
    written = [str(target)]
    if report.conftest_action == "wrote":
        written.append(str(conftest))
    report.message = f"Wrote {', '.join(written)}."
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=("Generate tests/test_runnability.py for a Python recipe."),
    )
    parser.add_argument(
        "--recipe-dir",
        required=True,
        type=Path,
        help="Path to the recipe root (e.g. core/python/foo).",
    )
    parser.add_argument(
        "--agent-file",
        type=Path,
        default=None,
        help=(
            "Override auto-detection of the entry-point file "
            "(relative to --recipe-dir or absolute). "
            "Default: shallowest agent.py under --recipe-dir."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the JSON report (including generated content) without "
        "writing any file.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing tests/test_runnability.py. Default: "
        "refuse and exit 1.",
    )
    args = parser.parse_args()

    if not args.recipe_dir.is_dir():
        print(
            f"Error: --recipe-dir {args.recipe_dir} is not a directory.",
            file=sys.stderr,
        )
        return 2

    try:
        report = run(
            recipe_dir=args.recipe_dir,
            agent_file_override=args.agent_file,
            dry_run=args.dry_run,
            overwrite=args.overwrite,
        )
    except Exception as e:  # final safety net for the CLI
        report = Report(
            recipe_dir=str(args.recipe_dir),
            mode="dry-run" if args.dry_run else "apply",
        )
        report.action = "error"
        report.message = (
            f"Unhandled {type(e).__name__}: {e}. This is a bug in the "
            f"generate-python-runnability-test skill; please report it."
        )

    print(report.to_json())

    if report.action == "error":
        return 2
    if report.action == "refused_overwrite":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
