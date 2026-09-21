"""
Validates a recipe's pyproject.toml against the repo's metadata rules.

Rules enforced (see .github/workflows/python-validate-recipe.yml):

  - project-name-matches-folder: [project].name must equal the recipe's
    expected name, which depends on the root the recipe lives under:
      * core/ and contrib/ — the folder basename (e.g. core/python/
        deep-search -> "deep-search").
      * skills/ — "<vertical>-<solution>", because skills/ interposes a
        mandatory vertical namespace (skills/<vertical>/<solution>) and the
        basename alone is not unique across verticals. See
        expected_project_name() for the full rationale.
  - python-version-floor: [project].requires-python must ACCEPT Python
    3.11 exactly — it must neither permit anything below (loose floor) nor
    exclude 3.11 by requiring higher (e.g. `>=3.12`). CI pins Python 3.11
    and any recipe that can't lock under 3.11 breaks the lockfile check.
  - description-matches-manifest: if [project].description is set, it must
    equal manifest.description from the recipe's manifest.yaml (after
    .strip(), exact match). Optional; skipped when absent.
  - default-pypi-index: [[tool.uv.index]] must have an entry with
    default=true whose url is public PyPI (https://pypi.org/simple[/]).
    Required so `uv sync` works on Google corp workstations without
    Airlock auth (see the block comment in the root pyproject.toml for
    the full rationale).

Reported as a WARNING, never as a failure:

  - adk-major-current: a recipe under core/ should resolve to the current
    google-adk major. Read from the LOCK as well as the specifier, because
    `google-adk>=1.8.0` permits 2.x while still installing 1.28.0. Warn-only
    because crossing an ADK major is a code migration, so a blocking check
    would wedge unrelated contributors behind that work. Advisories do not
    affect the exit code.

Note: no-local-ruff-config (forbid [tool.ruff*] blocks in recipe
pyproject.toml) is enforced by a grep in the workflow itself, not here.

MAINTENANCE NOTE — keep in sync with the align skill. The four BLOCKING
rules above are also implemented (as auto-fixes) by
.agents/skills/align-recipe-pyproject/scripts/align_pyproject.py. This script
only READS/validates (stdlib tomllib + pyyaml); that one REWRITES
(comment-preserving tomlkit + ruamel.yaml). They are intentionally separate
but MUST stay semantically in sync — if you change a rule's meaning here,
mirror it there (and vice versa).

Usage: python check_recipe_pyproject.py <recipe-dir>

Exit codes:
  0  every blocking rule passed. A warn-only advisory may still have been
     printed — those never affect the exit code.
  1  contributor-fixable problems found; every one has been reported with
     a fix, both as a human block and as a ::error annotation
  2  CI fault — the checker crashed, or its own environment is missing a
     dependency it needs. Never blamed on the contributor's files.

A missing pyproject.toml is not this script's failure to report (a separate
required-files check owns it), so that case exits 0 with a note.
"""

import re
import sys
from pathlib import Path

import tomllib
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ci_message import (
    EXIT_OK,
    Diagnostic,
    Doc,
    Severity,
    guard,
    infra_fault,
    report,
    report_advisories,
    report_infra_fault,
)

CHECKER = "check_recipe_pyproject.py"

# The google-adk major version that curated recipes are expected to teach.
#
# This is a FRESHNESS rule, not a correctness one, which is why it is
# warn-only (see check_adk_major). A recipe pinned to an older major still
# runs; it just demonstrates an API surface that no longer reflects how you
# would write the agent today. That is worse than a broken recipe in one
# specific way — broken is loud, out-of-date is silent.
#
# Scope is core/ only. contrib/ is community-contributed and its authors set
# their own pace; skills/ likewise.
#
# TO BUMP THIS: set the number, then expect the notice to fire on every core
# recipe still on the previous major. Moving a recipe across an ADK major is
# a code migration, not a version bump, so do not treat a green run as the
# goal — the goal is a deliberate migration per recipe.
CURRENT_ADK_MAJOR = 2

# Distribution name to look for, normalised per PEP 503. Extras
# (`google-adk[gcp]`) are stripped before comparison.
ADK_DIST = "google-adk"

MIN_PYTHON = (3, 11)
MIN_PYTHON_STR = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
# Representative Python versions strictly below MIN_PYTHON, used to probe
# whether a requires-python specifier admits anything under the floor. For
# each minor series below MIN_PYTHON we include BOTH the .0 release and a
# very-high micro (`.9999`): the .0 catches plain floors like `>=3.10`, while
# the high micro catches micro-version floors like `>=3.10.5` / `~=3.10.2`
# whose lower bound sits above X.Y.0 — a single `.0` probe would sail past
# them and wrongly report OK. The trailing 2.99 catches Python 2.x.
# Known residual: an exact micro pin (e.g. `==3.10.5`) is not detected, since
# no finite probe set can hit an arbitrary pinned micro; such pins are
# effectively nonexistent in real requires-python declarations.
# NOTE: mirrored in .agents/skills/align-recipe-pyproject/scripts/
# align_pyproject.py — keep in sync.
_PROBE_MICRO = 9999  # synthetic "very high" micro (see note above)
_BELOW_MIN_MINORS = range(MIN_PYTHON[1])  # 3.0, 3.1, ..., 3.(min-1)
BELOW_MIN = (
    [Version(f"{MIN_PYTHON[0]}.{m}") for m in _BELOW_MIN_MINORS]
    + [
        Version(f"{MIN_PYTHON[0]}.{m}.{_PROBE_MICRO}")
        for m in _BELOW_MIN_MINORS
    ]
    + [Version(f"{MIN_PYTHON[0] - 1}.99")]
)

# Acceptable URLs for the required default `[[tool.uv.index]]` entry.
# Both trailing-slash and non-trailing-slash forms are legal.
PYPI_URLS = frozenset(
    {
        "https://pypi.org/simple",
        "https://pypi.org/simple/",
    }
)

# Recipe roots whose layout interposes a mandatory NAMESPACE between the root
# and the solution folder: <root>/<namespace>/<solution>.
#
# Only `skills/` does this today. Its middle segment is a VERTICAL, not a
# language (see AGENTS.md and the `recipe_size_limits` comment in
# .github/policy.yml), and tools/validate_placement.py rejects a solution
# dropped directly under skills/.
#
# core/ and contrib/ look superficially similar (core/<language>/<recipe>)
# but are NOT listed here: their middle segment is a language, and their
# basenames are already unique repo-wide.
#
# Maps the root name to what its namespace segment is CALLED, purely so error
# messages can say "<vertical>-<solution>" rather than mechanically
# depluralising "skills" into something meaningless.
# NOTE: mirrored in .agents/skills/align-recipe-pyproject/scripts/
# align_pyproject.py — keep in sync.
NAMESPACED_ROOTS = {"skills": "vertical"}

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _repo_relative_parts(
    recipe_dir: Path, repo_root: Path | None = None
) -> tuple[str, ...]:
    """Path segments of `recipe_dir` relative to the repository root.

    An absolute path is made relative to `repo_root` so that the position of
    a segment is meaningful. A relative path is taken as already
    repo-relative, which is what CI passes. A path outside the repository
    yields its own segments, which will not match the three-segment shape the
    caller looks for — the safe outcome.
    """
    root = REPO_ROOT if repo_root is None else repo_root
    if recipe_dir.is_absolute():
        try:
            return recipe_dir.resolve().relative_to(root.resolve()).parts
        except ValueError:
            return recipe_dir.parts
    return recipe_dir.parts


def expected_project_name(
    recipe_dir: Path, repo_root: Path | None = None
) -> str:
    """Return the [project].name this recipe directory is required to declare.

    For most recipes this is just the folder basename. For a recipe under a
    namespaced root it is "<namespace>-<solution>", for two reasons:

    1. The basename is not unique. `skills/retail/product-search` and a future
       `skills/grocery/product-search` would both be forced to declare
       [project].name = "product-search" — two distribution packages with the
       same name. The vertical exists precisely to namespace solutions, so the
       project name has to carry it.
    2. It matches the skill's own identity. A vertical skill's SKILL.md
       frontmatter `name:`, its slash command, and its installed directory all
       use "<vertical>-<solution>"; the Python distribution name should not be
       the odd one out.

    The namespaced form requires the root to sit at the START of the
    repo-relative path with exactly two segments after it — a position test,
    not a name test. Matching on the third-from-last segment alone would fire
    on any path that merely happens to contain a directory called "skills":
    a hand-run `/home/me/skills/core/rag-vector-search` would yield
    "core-rag-vector-search". CI only ever passes repo-relative paths, but
    the auto-fixer mirror of this rule WRITES the value, so a human running
    it by hand on an absolute path must not get a corrupted name.

    Paths outside `repo_root` (and relative paths that are not exactly
    <root>/<namespace>/<solution>) fall back to the basename.
    """
    parts = _repo_relative_parts(recipe_dir, repo_root)
    if len(parts) == 3 and parts[0] in NAMESPACED_ROOTS:
        return f"{parts[1]}-{parts[2]}"
    return recipe_dir.name


class CheckerFault(Exception):
    """A failure in the checker's own environment, not in the recipe.

    Raised instead of reported so it can never be rendered as a Diagnostic
    against a contributor's file.
    """


def _describe(value: object) -> str:
    """`list ['a', 'b']` — the type first, because the type is the bug."""
    text = repr(value)
    if len(text) > 60:
        text = text[:57] + "..."
    return f"{type(value).__name__} {text}"


def check_name(
    project: dict, pyproject_path: Path, recipe_dir: Path
) -> list[Diagnostic]:
    """B2-name: [project].name must equal expected_project_name(recipe_dir)."""
    expected = expected_project_name(recipe_dir)
    # Only spell out the namespace derivation when the answer is not simply
    # the basename, so the common core/contrib message stays terse.
    if expected != recipe_dir.name:
        root = recipe_dir.parts[-3]
        term = NAMESPACED_ROOTS[root]
        derivation = (
            f"Recipes under {root}/ are named '<{term}>-<solution>' — the "
            f"{term} '{recipe_dir.parts[-2]}' joined to the folder name "
            f"'{recipe_dir.name}' — because a solution basename is not "
            f"unique across {term}s."
        )
    else:
        derivation = (
            f"A recipe's distribution name must equal its folder name, "
            f"'{recipe_dir.name}'."
        )

    how = f'Set the name in [project]:\n  name = "{expected}"'
    name = project.get("name")
    if not name:
        return [
            Diagnostic(
                check="project-name",
                what=f"[project].name is missing from {pyproject_path}.",
                why=f"It must be '{expected}'. {derivation}",
                how=how,
                doc=Doc.PROJECT_NAME,
                file=str(pyproject_path),
            )
        ]
    if name != expected:
        return [
            Diagnostic(
                check="project-name",
                what=(
                    f"[project].name = '{name}', but this recipe must "
                    f"declare '{expected}'."
                ),
                why=derivation,
                how=how,
                doc=Doc.PROJECT_NAME,
                file=str(pyproject_path),
            )
        ]
    return []


def check_requires_python(
    project: dict, pyproject_path: Path
) -> list[Diagnostic]:
    """B1: [project].requires-python must ACCEPT Python == MIN_PYTHON.

    Interpretation B: every recipe must be lockable/runnable under the
    version CI pins in .github/workflows/python-dependency-policy.yml (3.11).
    Two failure modes:

      * ``permits_older`` — spec accepts a Python below MIN_PYTHON (e.g.
        ``>=3.10``, ``~=3.10``, ``!=3.11``, ``<=3.12``, unpinned).
      * ``excludes_min`` — spec rejects MIN_PYTHON by requiring higher
        (e.g. ``>=3.12``, ``>=3.12,<3.14``, ``~=3.12``). This used to be
        permitted under a "floor is 3.11 but higher is fine" reading, but
        it makes CI fail with a confusing "lockfile is out of date" error
        whose real cause is that uv can't resolve a 3.11 interpreter
        against a >=3.12 requirement. Recipes that genuinely need 3.12+
        features must instead update CI's pinned interpreter (or add a
        per-recipe override) — silently allowing them here just moves the
        failure to PR-time.

    Uses packaging.specifiers.SpecifierSet (the PEP 440 reference
    implementation) so every legal operator (>=, >, ~=, ==, !=, <, <=, and
    combinations) is handled correctly.
    """
    floor = f'  requires-python = ">={MIN_PYTHON_STR}"'
    requires_python = project.get("requires-python")
    if not requires_python:
        return [
            Diagnostic(
                check="requires-python",
                what=(
                    f"[project].requires-python is missing from "
                    f"{pyproject_path}."
                ),
                why=(
                    f"AGENTS.md sets {MIN_PYTHON_STR} as the minimum for "
                    f"every recipe, and CI pins {MIN_PYTHON_STR} when it "
                    f"locks and runs this one. Without a specifier, uv is "
                    f"free to resolve against any interpreter."
                ),
                how=f"Add to [project]:\n{floor}",
                doc=Doc.REQUIRES_PYTHON,
                file=str(pyproject_path),
            )
        ]

    try:
        spec = SpecifierSet(str(requires_python))
    except (InvalidSpecifier, TypeError) as e:
        return [
            Diagnostic(
                check="requires-python",
                what=(
                    f"[project].requires-python = '{requires_python}' is not "
                    f"a valid PEP 440 version specifier ({e})."
                ),
                why=(
                    "uv and pip both parse this field as a PEP 440 "
                    "specifier set; a value they cannot parse makes the "
                    "recipe uninstallable."
                ),
                how=f"Use a specifier such as:\n{floor}",
                doc=Doc.REQUIRES_PYTHON,
                file=str(pyproject_path),
            )
        ]

    permits_older = [v for v in BELOW_MIN if v in spec]
    excludes_min = Version(MIN_PYTHON_STR) not in spec

    if permits_older:
        # Only surface a real witness version, never a synthetic `.9999`
        # probe (which is the only match for a micro floor like `>=3.10.5`).
        real = [v for v in permits_older if v.micro != _PROBE_MICRO]
        example = f" (for example {real[0]})" if real else ""
        return [
            Diagnostic(
                check="requires-python",
                what=(
                    f"[project].requires-python = '{requires_python}' "
                    f"accepts Python below {MIN_PYTHON_STR}{example}."
                ),
                why=(
                    f"AGENTS.md sets {MIN_PYTHON_STR} as the minimum for "
                    f"every recipe in this repo."
                ),
                how=(
                    f"Raise the lower bound to {MIN_PYTHON_STR}, keeping any "
                    f"upper bound you already have:\n{floor}"
                ),
                doc=Doc.REQUIRES_PYTHON,
                file=str(pyproject_path),
            )
        ]

    if excludes_min:
        return [
            Diagnostic(
                check="requires-python",
                what=(
                    f"[project].requires-python = '{requires_python}' "
                    f"excludes Python {MIN_PYTHON_STR}."
                ),
                why=(
                    f"CI pins Python {MIN_PYTHON_STR}. A specifier that "
                    f"rejects it makes uv fail to resolve an interpreter, "
                    f"which surfaces later as a misleading 'lockfile is out "
                    f"of date' error."
                ),
                how=(
                    f"Lower the floor, preserving any upper bound:\n{floor}\n"
                    f"If the recipe genuinely needs newer language features, "
                    f"ask the repo maintainers to raise CI's pinned "
                    f"interpreter instead of raising it here."
                ),
                doc=Doc.REQUIRES_PYTHON,
                file=str(pyproject_path),
            )
        ]

    return []


def check_description(
    project: dict, pyproject_path: Path, manifest_path: Path
) -> list[Diagnostic]:
    """B2-desc: if [project].description is set, must equal manifest.description.

    The field is optional; skipped entirely when absent.
    """
    description = project.get("description")
    if description is None:
        return []
    py_desc = str(description).strip()

    if not manifest_path.is_file():
        return [
            Diagnostic(
                check="description-matches-manifest",
                what=(
                    f"[project].description is set in {pyproject_path}, but "
                    f"there is no {manifest_path} to check it against."
                ),
                why=(
                    "Every recipe ships a manifest.yaml, and its "
                    "`description` is the single source of truth that "
                    "[project].description has to match. With no manifest "
                    "the rule cannot be evaluated, and the recipe is "
                    "missing a required file besides."
                ),
                how=(
                    f"Create {manifest_path} carrying the same description:\n"
                    f"  description: {py_desc}\n"
                    f"The generate-manifest AI skill writes the whole file "
                    f"from the recipe. If the recipe genuinely has no "
                    f"manifest, delete [project].description instead — the "
                    f"field is optional."
                ),
                doc=Doc.MANIFEST,
                file=str(pyproject_path),
            )
        ]

    # Imported lazily so a recipe with no [project].description does not
    # require pyyaml at all.
    try:
        import yaml
    except ImportError as exc:
        raise CheckerFault(
            f"pyyaml is not importable ({exc}), so [project].description "
            f"cannot be compared with manifest.description. The workflow "
            f"must invoke this script with pyyaml available: "
            f"`uv run --no-project --with pyyaml --with packaging`."
        ) from exc

    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = yaml.safe_load(f) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        return [
            Diagnostic(
                check="description-matches-manifest",
                what=f"{manifest_path} could not be read as YAML: {e}",
                why=(
                    "manifest.yaml is parsed by this check and by "
                    "tools/validate_manifest.py; a file neither can read "
                    "blocks every manifest-derived rule."
                ),
                how=(
                    "Fix the YAML (indentation and unquoted colons are the "
                    "usual culprits), or regenerate the file with the "
                    "generate-manifest AI skill."
                ),
                doc=Doc.MANIFEST,
                file=str(manifest_path),
            )
        ]

    if not isinstance(manifest, dict):
        return [
            Diagnostic(
                check="description-matches-manifest",
                what=(
                    f"{manifest_path} is valid YAML but its top level is a "
                    f"{_describe(manifest)}, not a mapping of fields."
                ),
                why=(
                    "manifest.yaml must be a mapping with top-level keys "
                    "(name, description, language, …). A document that "
                    "starts with `- ` is a list, so it has no `description` "
                    "field for [project].description to match."
                ),
                how=(
                    "Rewrite manifest.yaml as `key: value` pairs, for "
                    "example:\n"
                    f"  description: {py_desc}\n"
                    "The generate-manifest AI skill writes a correctly "
                    "shaped file from the recipe."
                ),
                doc=Doc.MANIFEST,
                file=str(manifest_path),
            )
        ]

    mf_desc = str(manifest.get("description") or "").strip()
    if py_desc == mf_desc:
        return []
    return [
        Diagnostic(
            check="description-matches-manifest",
            what=(
                f"[project].description does not match "
                f"manifest.description.\n"
                f"  pyproject.toml: {py_desc!r}\n"
                f"  manifest.yaml:  {mf_desc!r}"
            ),
            why=(
                "The two files describe the same recipe in two places; when "
                "they disagree, the description shown in the catalogue "
                "depends on which file the reader happens to open."
            ),
            how=(
                "Update whichever is out of date so both read the same, or "
                "delete [project].description from pyproject.toml — it is "
                "optional, and manifest.yaml is the source of truth."
            ),
            doc=Doc.PROJECT_DESCRIPTION,
            file=str(pyproject_path),
        )
    ]


_INDEX_BLOCK = (
    '  [[tool.uv.index]]\n  url = "https://pypi.org/simple/"\n  default = true'
)

# Contributor-facing wording deliberately omits the Google-internal reason.
# The real one: on a corp workstation the system-wide /etc/uv/uv.toml
# redirects to the Airlock proxy, which the developer would then have to
# authenticate to. Per uv's config-file merge rules project-level indexes are
# concatenated ahead of system-level ones, so declaring public PyPI here as
# default puts it first in the merged list. External contributors have no
# Airlock, so naming it in the error only confuses them.
_INDEX_WHY = (
    "Every recipe must pin public PyPI as its default index so `uv sync` "
    "resolves the same way on every machine. Without it, a machine-wide uv "
    "configuration can silently redirect installs to a different index that "
    "the next person to run the recipe cannot reach."
)


def check_default_pypi_index(
    pyproject: dict, pyproject_path: Path
) -> list[Diagnostic]:
    """default-pypi-index: [[tool.uv.index]] with default=true → public PyPI.

    Fail modes:
      - No [[tool.uv.index]] at all.
      - `index` written in a shape uv does not accept (single-bracket table,
        or a bare list of URL strings).
      - Entries exist but none has default=true.
      - A default entry exists but its url is not public PyPI (the recipe
        author may have a legitimate reason but must justify it; the default
        here is strictness so CI protects the repo standard).
    """
    tool = pyproject.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    indexes = uv.get("index") if isinstance(uv, dict) else None
    if not indexes:
        return [
            Diagnostic(
                check="default-pypi-index",
                what=(f"{pyproject_path} has no `[[tool.uv.index]]` block."),
                why=_INDEX_WHY,
                how=f"Add to pyproject.toml:\n{_INDEX_BLOCK}",
                doc=Doc.PYPI_INDEX,
                file=str(pyproject_path),
            )
        ]

    # `[tool.uv.index]` (single brackets) parses as a dict; `[[tool.uv.index]]`
    # (double brackets, array of tables) parses as a list of dicts. Only the
    # latter is what uv accepts.
    if not isinstance(indexes, list):
        return [
            Diagnostic(
                check="default-pypi-index",
                what=(
                    f"`tool.uv.index` is a {_describe(indexes)}, but uv "
                    f"requires an array of tables."
                ),
                why=(
                    "Single brackets declare one table; uv reads "
                    "`tool.uv.index` as a list of index definitions. "
                    f"{_INDEX_WHY}"
                ),
                how=(
                    "Use double brackets — `[[tool.uv.index]]`, not "
                    f"`[tool.uv.index]`:\n{_INDEX_BLOCK}"
                ),
                doc=Doc.PYPI_INDEX,
                file=str(pyproject_path),
            )
        ]

    # An inline `index = ["https://…"]` is a list, so it survives the check
    # above and then has no `.get()`. Each element must be a table.
    non_tables = [e for e in indexes if not isinstance(e, dict)]
    if non_tables:
        return [
            Diagnostic(
                check="default-pypi-index",
                what=(
                    f"`tool.uv.index` contains an entry that is a "
                    f"{_describe(non_tables[0])}, not a table."
                ),
                why=(
                    '`index = ["https://pypi.org/simple/"]` is a list of '
                    "strings. uv needs an array of TABLES, because an index "
                    "carries more than a url — `default`, and optionally "
                    f"`name` and `explicit`. {_INDEX_WHY}"
                ),
                how=(
                    "Replace the list of strings with an array of tables:\n"
                    f"{_INDEX_BLOCK}"
                ),
                doc=Doc.PYPI_INDEX,
                file=str(pyproject_path),
            )
        ]

    for entry in indexes:
        if entry.get("default") is True:
            url = entry.get("url")
            if url and str(url).lower() in PYPI_URLS:
                return []
            return [
                Diagnostic(
                    check="default-pypi-index",
                    what=(
                        f"The `[[tool.uv.index]]` entry with default=true "
                        f"has url='{url}', which is not public PyPI."
                    ),
                    why=_INDEX_WHY,
                    how=(
                        "Point the default entry at public PyPI (both the "
                        "trailing-slash and non-trailing-slash forms are "
                        f"accepted):\n{_INDEX_BLOCK}\n"
                        "A private index may be added as an ADDITIONAL "
                        "non-default entry."
                    ),
                    doc=Doc.PYPI_INDEX,
                    file=str(pyproject_path),
                )
            ]

    return [
        Diagnostic(
            check="default-pypi-index",
            what=(
                f"{pyproject_path} declares {len(indexes)} "
                f"`[[tool.uv.index]]` entr"
                f"{'y' if len(indexes) == 1 else 'ies'}, but none is marked "
                f"`default = true`."
            ),
            why=_INDEX_WHY,
            how=(
                "Mark the public-PyPI entry as the default, or add one:\n"
                f"{_INDEX_BLOCK}"
            ),
            doc=Doc.PYPI_INDEX,
            file=str(pyproject_path),
        )
    ]


def _normalise_dist(name: str) -> str:
    """PEP 503 normalisation, so `Google_ADK` and `google-adk` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _adk_requirement(project: dict) -> Requirement | None:
    """The `google-adk` entry from `[project].dependencies`, if declared.

    Malformed entries are skipped rather than reported: the resolver fails
    loudly on those already (`uv lock --check`, in the dependency-policy
    workflow), and a freshness notice is the wrong place to first learn your
    dependency list does not parse.
    """
    for raw in project.get("dependencies") or []:
        if not isinstance(raw, str):
            continue
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            continue
        if _normalise_dist(req.name) == ADK_DIST:
            return req
    return None


def _locked_adk_version(recipe_dir: Path) -> Version | None:
    """The google-adk version pinned in the sibling uv.lock, if resolvable.

    Read because the DECLARED specifier does not tell you what the recipe
    teaches. `google-adk>=1.0.0` admits 2.x, yet several recipes carrying that
    exact specifier resolve to 1.28.0 — the constraint is satisfied and the
    recipe still demonstrates the previous major. The lock is what `uv sync`
    installs, so the lock is what a reader ends up running.

    Every failure path returns None (no notice) rather than guessing. A
    missing or unparseable lock is reported by the dependency-policy workflow,
    which owns that question.
    """
    lock_path = recipe_dir / "uv.lock"
    if not lock_path.is_file():
        return None
    try:
        with open(lock_path, "rb") as f:
            lock = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        return None

    for package in lock.get("package") or []:
        if not isinstance(package, dict):
            continue
        if _normalise_dist(str(package.get("name", ""))) != ADK_DIST:
            continue
        try:
            return Version(str(package.get("version", "")))
        except InvalidVersion:
            return None
    return None


def _specifier_admits_major(specifier: SpecifierSet, major: int) -> bool:
    """Does `specifier` allow ANY release in the given major series?

    The obvious implementation — `specifier.contains(Version(f"{major}.0.0"))`
    — is wrong, and wrong in the direction that produces false accusations:
    `>=2.5.0,<3.0.0` sits squarely on major 2 but excludes 2.0.0 exactly. Two
    recipes were wrongly flagged by that during development.

    So probe instead, the same technique (and the same class of caveat) as
    BELOW_MIN above:

      * `major.0.0` catches caps inside the series, e.g. `<2.1`;
      * `major.9999.9999` catches floors inside it, e.g. `>=2.5.0`;
      * each clause version that is itself in the series catches exact pins
        such as `==2.3.0`, which no fixed probe set can hit.

    Prereleases are admitted deliberately: `>=2.0.0a0` is a real way to sit on
    the current major, and the prerelease itself gets its own better-targeted
    notice rather than being conflated with being a major behind.
    """
    probes = [Version(f"{major}.0.0"), Version(f"{major}.9999.9999")]
    for clause in specifier:
        try:
            candidate = Version(clause.version.rstrip(".*"))
        except InvalidVersion:
            continue
        if candidate.major == major:
            probes.append(candidate)
    return any(specifier.contains(p, prereleases=True) for p in probes)


def check_adk_major(
    project: dict, pyproject_path: Path, recipe_dir: Path
) -> list[Diagnostic]:
    """WARN-ONLY: is a core/ recipe still teaching an older google-adk major?

    Deliberately not an error. Six of the nine core/python recipes were on the
    previous major when this rule landed, and each one needs a real migration
    rather than a specifier edit — making it blocking would have wedged every
    unrelated PR behind that work.

    Two distinct problems, reported separately because the fixes differ:

      * the specifier EXCLUDES the current major (`<2.0.0`, `==1.31.0`) —
        pyproject.toml has to change before a relock can do anything;
      * the specifier admits it but the LOCK is behind — the declaration is
        already fine and only the lock needs regenerating.

    A prerelease lock (`2.0.0a3`) is on the current major and so passes the
    version test, but is called out on its own: an alpha in a curated recipe
    is a different kind of "do not copy this" than being a major behind.
    """
    parts = _repo_relative_parts(recipe_dir)
    if not parts or parts[0] != "core":
        return []

    req = _adk_requirement(project)
    if req is None:
        # Not every core recipe depends on the ADK directly (a nested
        # sub-project such as data_ingestion/ does not), and inventing a
        # notice for those would train people to ignore the channel.
        return []

    advisories: list[Diagnostic] = []
    locked = _locked_adk_version(recipe_dir)

    # The lock is consulted FIRST and settles the question when it is already
    # on the current major: whatever the specifier looks like, it demonstrably
    # admits the version that is installed. Reasoning from the specifier alone
    # produced false accusations against two recipes on `>=2.5.0,<3.0.0`.
    if locked is not None and locked.major >= CURRENT_ADK_MAJOR:
        if locked.is_prerelease:
            advisories.append(
                _adk_prerelease_notice(recipe_dir, pyproject_path, locked)
            )
        return advisories

    excludes_current = bool(req.specifier) and not _specifier_admits_major(
        req.specifier, CURRENT_ADK_MAJOR
    )

    if excludes_current:
        advisories.append(
            Diagnostic(
                check="adk-major-current",
                severity=Severity.WARNING,
                what=(
                    f"{pyproject_path} declares `{req}`, which cannot resolve "
                    f"to google-adk {CURRENT_ADK_MAJOR}.x."
                ),
                why=(
                    f"A curated recipe is read as the current way to write an "
                    f"agent. Pinned below {CURRENT_ADK_MAJOR}.x it keeps "
                    f"demonstrating an API surface that has moved on — and "
                    f"unlike a broken recipe, nothing about running it says "
                    f"so."
                ),
                how=(
                    f"Migrate the recipe to google-adk "
                    f"{CURRENT_ADK_MAJOR}.x, then widen the specifier:\n"
                    f'  "google-adk>={CURRENT_ADK_MAJOR}.0.0,'
                    f'<{CURRENT_ADK_MAJOR + 1}.0.0"\n'
                    f"and re-lock with: uv lock --project {recipe_dir}\n"
                    f"This is a code migration, not a version bump — widening "
                    f"the specifier without porting the code will fail at "
                    f"runtime, not here."
                ),
                doc=Doc.ADK_MAJOR,
                file=str(pyproject_path),
            )
        )
        return advisories

    if locked is None:
        return advisories

    # Only one case is left. The `>=` lock returned at the top and an
    # excluding specifier returned just above, so `locked.major` is
    # necessarily below the current one while the declaration already permits
    # it — the pyproject is fine and only the lock is behind.
    advisories.append(
        Diagnostic(
            check="adk-major-current",
            severity=Severity.WARNING,
            what=(
                f"{recipe_dir}/uv.lock pins google-adk {locked}, but "
                f"{pyproject_path} declares `{req.specifier or 'no bound'}`"
                f" which already permits {CURRENT_ADK_MAJOR}.x."
            ),
            why=(
                f"The specifier is not what a reader installs — the lock "
                f"is. `uv sync` resolves this recipe to {locked}, so the "
                f"recipe demonstrates google-adk {locked.major}.x however "
                f"permissive the declaration looks."
            ),
            how=(
                f"Port the recipe to {CURRENT_ADK_MAJOR}.x if it is not "
                f"already, then:\n"
                f"  uv lock --upgrade-package google-adk "
                f"--project {recipe_dir}\n"
                f"Check the recipe still runs afterwards; crossing a "
                f"major is not expected to be a no-op."
            ),
            doc=Doc.ADK_MAJOR,
            file=str(pyproject_path),
        )
    )

    return advisories


def _adk_prerelease_notice(
    recipe_dir: Path, pyproject_path: Path, locked: Version
) -> Diagnostic:
    """A core recipe locked to a prerelease of the current major.

    Separate from the behind-a-major notice because the reader's problem is
    different: the API is current, but it is not stable, so the recipe can
    stop working without a deprecation cycle.
    """
    return Diagnostic(
        check="adk-major-current",
        severity=Severity.WARNING,
        what=f"{recipe_dir}/uv.lock pins google-adk {locked}, a prerelease.",
        why=(
            "A curated recipe is copied as a starting point. Shipping it "
            "against an alpha hands everyone who clones it a dependency that "
            "can change under them with no deprecation path."
        ),
        how=(
            f"Move to a stable release once one is available:\n"
            f"  uv lock --upgrade-package google-adk --project {recipe_dir}\n"
            f"If the recipe genuinely needs prerelease-only behaviour, say so "
            f"in its README so a reader knows it is deliberate."
        ),
        doc=Doc.ADK_MAJOR,
        file=str(pyproject_path),
    )


def _project_table(
    pyproject: dict, pyproject_path: Path, recipe_dir: Path
) -> tuple[dict, list[Diagnostic]]:
    """The `[project]` table, or a shape diagnostic if it is not a table."""
    project = pyproject.get("project")
    if project is None:
        return {}, []
    if isinstance(project, dict):
        return project, []
    return {}, [
        Diagnostic(
            check="project-table",
            what=(
                f"`project` in {pyproject_path} is a {_describe(project)}, "
                f"not a table."
            ),
            why=(
                "`[[project]]` (double brackets) declares an array of "
                "tables. PEP 621 requires a single `[project]` table, and "
                "every metadata rule reads its fields from there — so as "
                "written, name, requires-python and description are all "
                "invisible to uv and to this check."
            ),
            how=(
                "Use single brackets:\n"
                "  [project]   ← not [[project]]\n"
                f'  name = "{expected_project_name(recipe_dir)}"\n'
                f'  requires-python = ">={MIN_PYTHON_STR}"'
            ),
            doc=Doc.PROJECT_NAME,
            file=str(pyproject_path),
        )
    ]


def _run(recipe_dir: Path) -> int:
    pyproject_path = recipe_dir / "pyproject.toml"
    manifest_path = recipe_dir / "manifest.yaml"

    if not pyproject_path.is_file():
        # A separate required-files check in the workflow reports this; not
        # repeating it here keeps one missing file from producing two errors.
        print(
            f"[SKIP] {pyproject_path} does not exist — the required-files "
            f"check reports that separately."
        )
        return EXIT_OK

    try:
        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        return _report(
            [
                Diagnostic(
                    check="pyproject-parse",
                    what=f"{pyproject_path} is not valid TOML: {e}",
                    why=(
                        "uv, pip and this check all parse pyproject.toml "
                        "before anything else; while it does not parse, no "
                        "other rule about the recipe can even be evaluated."
                    ),
                    how=(
                        "Fix the syntax at the position reported above. To "
                        "see the same error locally:\n"
                        "  python3 -c 'import tomllib,sys; "
                        'tomllib.load(open(sys.argv[1],"rb"))\' '
                        f"{pyproject_path}"
                    ),
                    doc=Doc.PROJECT_NAME,
                    file=str(pyproject_path),
                )
            ],
            recipe_dir,
        )

    project, diagnostics = _project_table(pyproject, pyproject_path, recipe_dir)
    advisories: list[Diagnostic] = []
    if not diagnostics:
        diagnostics += check_name(project, pyproject_path, recipe_dir)
        diagnostics += check_requires_python(project, pyproject_path)
        diagnostics += check_description(project, pyproject_path, manifest_path)
        advisories += check_adk_major(project, pyproject_path, recipe_dir)
    diagnostics += check_default_pypi_index(pyproject, pyproject_path)

    # Printed before the verdict so a passing run still surfaces them, and so
    # they never read as part of the failure when the verdict is FAIL.
    report_advisories(
        advisories, header=f"{recipe_dir}: pyproject.toml freshness"
    )
    return _report(diagnostics, recipe_dir)


def _report(diagnostics: list[Diagnostic], recipe_dir: Path) -> int:
    return report(
        diagnostics,
        header=f"{recipe_dir}: pyproject.toml metadata",
        passed_message=(
            f"{recipe_dir}/pyproject.toml: name, requires-python, "
            f"description and default index all satisfy the repo rules."
        ),
        next_step=(
            "The align-recipe-pyproject AI skill applies all of these fixes "
            "in place, preserving comments."
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
    except CheckerFault as exc:
        return report_infra_fault(infra_fault(CHECKER, str(exc)))
    except Exception as exc:
        return report_infra_fault(
            infra_fault(CHECKER, f"{type(exc).__name__}: {exc}")
        )


if __name__ == "__main__":
    # guard(): a traceback escaping to the runner is reported by the
    # workflow as the contributor's failure, which is exactly the confusion
    # infra_fault exists to prevent.
    sys.exit(guard(CHECKER, main))
