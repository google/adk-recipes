"""
Make a Python recipe deployable: generate the serving files and configure them.

A deployable recipe is one that can be packaged into a container and run as a
service. This script writes the files that requires and patches the recipe's
metadata to match. It does NOT build an image, deploy anything, or provision
infrastructure.

The standard it implements lives in `.github/policy.yml` under `deployability:`
(minimum google-adk version, required dependencies, required files, the legacy
app_utils file list). Change the standard there, not here.

CHECKS, in the order they run
-----------------------------
Gates run first and can stop the whole script — there is no point generating
files for a recipe that cannot host them.

  - agent-package
        Locate the recipe's agent package (the directory holding agent.py).
        Everything downstream is written relative to it. ERROR if not found
        or if the recipe has no `root_agent`.

  - adk-version-floor  [GATE]
        [project].dependencies must carry a google-adk requirement whose
        specifier ACCEPTS `deployability.min_google_adk`.

        The floor is not arbitrary: the serving stack needs
        `a2a-sdk[http-server]>=1.0`, and google-adk's `a2a` extra caps a2a-sdk
        at <0.4 through 2.4.0 — only 2.5.0 widened it to <2.

        A recipe whose specifier excludes the floor (a `<2.0.0` ceiling, an
        `==1.31.0` pin) returns NEEDS_INPUT and the script STOPS. Crossing an
        ADK major is a code migration, not a metadata rewrite: widening the
        ceiling and re-locking would yield a recipe importing a major it was
        never ported to, a failure invisible here, in the lockfile, and in CI,
        surfacing only when someone runs the recipe. Same reasoning as
        align-recipe-pyproject's "DELIBERATELY NOT IMPLEMENTED HERE".

  - legacy-app-utils  [GATE]
        `<pkg>/app_utils/` must not contain the old ASP-era generation
        (telemetry.py, typing.py, deploy.py, memory_config.py). Filenames do
        not collide with the new set, but the two wire telemetry and services
        differently: the old fast_api_app.py imports the old modules and
        defines its own /feedback route, so overwriting the entrypoint either
        orphans them or double-wires telemetry. NEEDS_INPUT, script STOPS.

  - backing-infra  [GATE, advisory]
        Detects an existing infra/ or terraform/ directory, or imports of
        backing services this script cannot provision (Cloud SQL, Pub/Sub,
        a Vertex AI Search datastore, a vector index).

        This does not stop generation — it decides the OUTCOME. A recipe that
        needs infra can still be containerized; it just is not one-click
        deployable, so `manifest.deployable` is left alone. See "two outcomes"
        below.

  - serving-files
        The five files from `deployability.required_files`, copied from
        resources/templates/ with `__AGENT_PACKAGE__` / `__PROJECT_NAME__`
        substituted. An existing file is never overwritten without --overwrite.

  - dockerignore
        A real .dockerignore. Not cosmetic: every recipe in this repo has an
        in-tree .venv/, and `.env` must never be baked into an image.

  - required-dependencies
        Adds any missing entry from `deployability.required_dependencies`.

        For a package the recipe ALREADY declares, the two halves of the
        requirement are treated differently:
          * VERSION BOUND — left exactly as written, and reported. A recipe
            may carry a deliberate tighter pin, and silently widening it is
            how you break something that was working.
          * EXTRAS — merged in (`google-adk` -> `google-adk[gcp,otel-gcp]`,
            same bound). The generated serving code imports what those extras
            install, so omitting them ships a recipe that cannot start. An
            extra only pulls in more of that package's own optional
            dependencies; it cannot move the recipe onto a different version.

  - hatch-wheel-packages
        [tool.hatch.build.targets.wheel] must list the agent package, or
        `uv sync` fails.

  - app-object
        agent.py must define `app = App(root_agent=root_agent, name="<pkg>")`
        alongside root_agent. Appended (with its import) when absent.

  - manifest-deployable
        Sets `deployable: true` in manifest.yaml — but ONLY when the
        backing-infra gate found nothing. See below.

OUTCOMES, NEVER CONFLATED
-------------------------
Two independent questions decide the outcome, and collapsing them is how a
manifest ends up carrying a false claim:

  1. Does the recipe need infrastructure a human must provision?
     `deployable` in .github/schemas/manifest-schema.json means "can be
     deployed with ONE CLICK", so a recipe needing hand-written terraform is
     containerized but not deployable.
  2. Did we PROVE the container works, or only assume it?

Crossing the two gives the vocabulary:

  deployable-verified      No bespoke infra, and the image built and served.
                           `manifest.deployable` set to true, on evidence.
  deployable-unverified    No bespoke infra, but no container evidence exists
                           (no docker, or the owner declined). Flag still set
                           — see WHY UNVERIFIED STILL SETS THE FLAG below.
  containerized-verified   Image built and served, but backing infra is
                           needed. Flag NOT touched.
  containerized-unverified Same, without container evidence. Flag NOT touched.
  verification-failed      Docker was usable and the recipe FAILED — the build
                           broke, or the container never served. Flag NOT set,
                           whatever the static checks said, and a pre-existing
                           one is retracted.
  verification-inconclusive
                           Verification was ATTEMPTED and defeated by the
                           environment (network, registry, or a runtime that
                           could not exec the image on this host). It proves
                           nothing either way, so nothing is retracted; static
                           checks may still have earned the flag. Distinct
                           from `-unverified`, which means nobody tried: that
                           one says "go find a machine with docker", this one
                           says "you already did — retry".
  blocked                  The run stopped without a usable verdict, in one
                           of FOUR ways:
                             1. a GATE refused it up front (nothing written);
                             2. the recipe was DISQUALIFIED partway through —
                                a hard ERROR, or no usable `app` object in
                                agent.py. A stale flag is RETRACTED in both
                                cases, and the app-object case carries no
                                ERROR check, so do not use the presence of an
                                ERROR to infer whether the tree was touched;
                             3. the skill ITSELF faulted (bad policy, missing
                                template) — says nothing about the recipe and
                                leaves manifest.yaml exactly as found;
                             4. verification was DEFERRED pending `uv lock`
                                (files written, flag held back, exit 1) — a
                                pause, not a judgement. Re-invoke to finish.
                           Only case 1 guarantees a clean tree, so check
                           `files_written` rather than assuming one, and read
                           the `container-verify` check to tell 4 from the
                           rest.

A recipe proven broken is not deployable, so verification outranks every
static check that came before it.

WHY UNVERIFIED STILL SETS THE FLAG
----------------------------------
Because absence of evidence is not evidence of absence, and because this
skill's primary user has no container runtime at all. Withholding the flag
whenever docker is missing would silently regress every such run to "not
deployable" on the strength of the checker's environment rather than the
recipe's quality — punishing the recipe for the laptop it was checked on.

So static checks still earn the flag; the OUTCOME carries the distinction,
and a reader can always tell a proven claim from an assumed one. Only a
verification that actually FAILED withholds it. Silence and a failure are
very different signals and must never render alike.

CONTAINER VERIFICATION, AND THE ORDERING IT FORCES
--------------------------------------------------
The generated Dockerfile runs `uv sync --frozen`, which fails unless uv.lock
already matches pyproject.toml — and this script adds serving dependencies
without re-locking (see below). Two consequences, both handled by making
--verify-container a TWO-PHASE operation keyed on lockfile currency:

  stale lockfile    Files are written, the manifest flag is DEFERRED, and the
                    run returns NEEDS_INPUT telling the caller to run
                    `uv lock` and re-invoke. Building here would fail with a
                    lockfile-mismatch error that looks exactly like a broken
                    template and is not.
  current lockfile  Files are already in place (this script is idempotent, so
                    the second invocation rewrites nothing), the image is
                    built, run and probed, and the flag is written only if it
                    passed.

The deferral is not fussiness. `manifest.deployable` is written during the
apply pass, but `uv lock` can only run after apply and the build only after
that — so a flag written eagerly could never be gated by a result that does
not exist yet. Deferring it is what lets verification actually decide.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO
-----------------------------------------
  - DEPLOY. It builds an image only to verify its own output, then deletes
    it. Publishing to Artifact Registry and rolling out belongs to the
    owner's CI via Cloud Build. Docker here is an instrument, not a target.
  - Run a container that is not on `deployability.verification.run_allowlist`.
    Some recipes create real GCP resources at import; an unlisted recipe is
    built only, and the report says the serve step was skipped rather than
    implying it passed.
  - Migrate agent code across an ADK major (see adk-version-floor).
  - Merge a legacy app_utils generation (see legacy-app-utils).
  - Write terraform.
  - Remove [tool.ruff*] or fix requires-python — that is
    align-recipe-pyproject's job, and duplicating it guarantees drift.
  - Complete .env.example — that is extract-python-environment-variables.
  - Run `uv lock` or ruff. The calling skill does that after this script, so
    a failure is attributable to the right step. Verification READS lockfile
    currency (`uv lock --check`) but never writes the lockfile, for the same
    reason.

It reports which of those the owner still needs, rather than doing them.

A NOTE ON WHAT GENERATING a2a.py PROVES
---------------------------------------
Nothing, on its own. These files were designed assuming the project was
scaffolded by agents-cli, and a file named a2a.py does not make an agent
behave correctly over A2A. This script copies and configures; it does not
certify. The report says so, and so must the skill.
"""

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tomlkit
import tomllib
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version
from ruamel.yaml import YAML

# ---------------------------------------------------------------------------
# Status vocabulary — identical to align-recipe-pyproject so a caller can
# render both skills' reports with one code path.
# ---------------------------------------------------------------------------
CLEAN = "clean"  # already satisfies the rule
WOULD_FIX = "would_fix"  # dry-run: a fix is available and would be applied
FIXED = "fixed"  # apply mode: the fix was applied
NEEDS_INPUT = "needs_input"  # cannot proceed without a human decision
REPORT_ONLY = "report_only"  # informational, no auto-fix
ERROR = "error"  # unexpected failure

# Outcomes (see module docstring). The infra axis (deployable vs
# containerized) is crossed with the evidence axis (verified vs unverified),
# because "needs terraform" and "we never built it" are different facts and a
# reader must be able to tell a proven result from an assumed one.
OUTCOME_DEPLOYABLE_VERIFIED = "deployable-verified"
OUTCOME_DEPLOYABLE_UNVERIFIED = "deployable-unverified"
OUTCOME_CONTAINERIZED_VERIFIED = "containerized-verified"
OUTCOME_CONTAINERIZED_UNVERIFIED = "containerized-unverified"
OUTCOME_VERIFICATION_FAILED = "verification-failed"
# Tried, and could not tell. Distinct from `-unverified` (never tried),
# because the two call for opposite responses: `-unverified` means "run
# verification somewhere with docker", while this means "you already did, and
# the environment defeated it — retry, do not re-plan". Two independent
# reviewers flagged that collapsing them left a caller unable to choose.
OUTCOME_VERIFICATION_INCONCLUSIVE = "verification-inconclusive"
OUTCOME_BLOCKED = "blocked"


def outcome_for(
    *,
    infra_clean: bool,
    verified: bool | None,
    has_error: bool = False,
    skill_fault: bool = False,
    disqualified: bool = False,
    deferred: bool = False,
    inconclusive: bool = False,
) -> str:
    """Pick the outcome from the infra axis and the evidence axis.

    `verified` is tri-state on purpose: True (proved it works), False (proved
    it does NOT — outranks everything, since a recipe demonstrated broken is
    not deployable however clean its metadata), or None (no evidence either
    way, which is not a failure).
    """
    if verified is False:
        return OUTCOME_VERIFICATION_FAILED
    if inconclusive and not (has_error or skill_fault or disqualified):
        # Attempted and defeated by the environment. The flag may still have
        # been earned by static checks — see the manifest-deployable check;
        # what this outcome asserts is only that the CONTAINER proved nothing.
        #
        # It must NOT outrank a disqualification. Verification is not gated on
        # the static checks, so a broken recipe still gets built; without this
        # guard any passing network hiccup would mask the disqualification and
        # report "nothing is retracted" for a run that had just deleted the
        # flag. That is the outcome-contradicts-the-manifest defect twice
        # shipped already, re-entering through the newest branch.
        return OUTCOME_VERIFICATION_INCONCLUSIVE
    if deferred:
        # The flag is withheld pending a verdict, so no `-unverified` outcome
        # (which SKILL.md defines as "flag set") may be reported here.
        return OUTCOME_BLOCKED
    if has_error or skill_fault or disqualified:
        # An ERROR anywhere disqualifies the recipe, and the outcome must say
        # so. Without this the report could read `deployable-verified` while
        # the manifest flag had just been retracted for that same error —
        # two halves of one report contradicting each other.
        return OUTCOME_BLOCKED
    if infra_clean:
        return (
            OUTCOME_DEPLOYABLE_VERIFIED
            if verified
            else OUTCOME_DEPLOYABLE_UNVERIFIED
        )
    return (
        OUTCOME_CONTAINERIZED_VERIFIED
        if verified
        else OUTCOME_CONTAINERIZED_UNVERIFIED
    )


# Docker availability, three states rather than a boolean. A stopped daemon is
# the common case on a developer machine and is NOT an error — it is simply an
# absence of evidence, and must report identically to having no docker at all
# while still being distinguishable in the details for anyone debugging.
DOCKER_ABSENT = "absent"  # no `docker` binary on PATH
DOCKER_UNREACHABLE = "unreachable"  # binary present, `docker info` fails
DOCKER_USABLE = "usable"  # binary present and the daemon answers

# Template placeholders. Both are valid Python identifiers on purpose, so the
# vendored .py templates parse and can be linted in place rather than only
# after substitution.
PLACEHOLDER_PACKAGE = "__AGENT_PACKAGE__"
PLACEHOLDER_PROJECT = "__PROJECT_NAME__"

# Modules whose presence means the recipe talks to a backing service this
# script cannot provision. Matched against the recipe's import statements.
BACKING_SERVICE_IMPORTS = {
    "google.cloud.sql": "Cloud SQL",
    "google.cloud.pubsub": "Pub/Sub",
    "google.cloud.pubsub_v1": "Pub/Sub",
    "google.cloud.discoveryengine": "Vertex AI Search datastore",
    "google.cloud.alloydb": "AlloyDB",
    "asyncpg": "PostgreSQL",
    "psycopg": "PostgreSQL",
    "psycopg2": "PostgreSQL",
    "pymysql": "MySQL",
    "sqlalchemy": "a SQL database",
}

# Environment variables that likewise imply provisioned infrastructure.
BACKING_SERVICE_ENV_VARS = {
    "INSTANCE_CONNECTION_NAME": "Cloud SQL",
    "SESSION_DB_URL": "a SQL session store",
    "TASK_DB_URL": "a SQL task store",
    "DATASTORE_ID": "a Vertex AI Search datastore",
    "VECTOR_INDEX_ID": "a vector index",
    "INDEX_ENDPOINT_ID": "a vector index endpoint",
}

DOCKERIGNORE = """\
# Build context exclusions. Keeping .venv/ out is not optional: every recipe
# in this repo has one in-tree, and copying it into the image both bloats it
# and can shadow the container's own site-packages.
.venv/
venv/
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/

# Never bake credentials into an image. .env.example is safe and stays.
.env

.git/
.gitignore
.DS_Store
"""


@dataclass
class Check:
    """One entry in the report — one rule's outcome for this recipe."""

    id: str
    status: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Report:
    recipe_dir: str
    mode: str  # "dry-run" or "apply"
    outcome: str = OUTCOME_BLOCKED
    agent_package: str | None = None
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    todos: list[str] = field(default_factory=list)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def todo(self, text: str) -> None:
        self.todos.append(text)

    def to_json(self) -> str:
        return json.dumps(
            {
                "recipe_dir": self.recipe_dir,
                "mode": self.mode,
                "outcome": self.outcome,
                "agent_package": self.agent_package,
                "checks": [asdict(c) for c in self.checks],
                "files_written": self.files_written,
                "todos": self.todos,
                "notes": self.notes,
            },
            indent=2,
        )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def find_repo_root(start: Path) -> Path | None:
    """Walk up from `start` looking for the directory holding .github/policy.yml."""
    for candidate in [start.resolve(), *start.resolve().parents]:
        if (candidate / ".github" / "policy.yml").is_file():
            return candidate
    return None


def load_policy(repo_root: Path) -> dict[str, Any]:
    """Read the `deployability:` section of .github/policy.yml.

    Read rather than hardcoded so the standard (ADK floor, dependency list,
    file list) can move without a code change — that was an explicit design
    requirement, not a convenience.
    """
    yaml = YAML(typ="safe")
    with open(repo_root / ".github" / "policy.yml", encoding="utf-8") as f:
        data = yaml.load(f)
    policy = (data or {}).get("deployability")
    if not policy:
        raise RuntimeError(
            "`deployability:` section missing from .github/policy.yml — the "
            "skill cannot know what the standard requires."
        )
    return policy


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

SKIP_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    "node_modules",
    "build",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "tests",
    "test",
    "frontend",
    "deployment",
    "infra",
    "terraform",
}


def _walk_py_files(root: Path):
    """Yield .py files under `root`, skipping build output and virtualenvs."""
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        yield path


def find_agent_package(recipe_dir: Path) -> tuple[Path, Path] | None:
    """Return (agent_py_path, package_dir) for the shallowest agent.py.

    The package directory is agent.py's parent — `app`, `horizon`,
    `expense_agent`. Shallowest wins so a sub-agent nested under the real
    package (e.g. `app/subagents/agent.py`) never shadows it; alphabetical
    tie-break keeps the choice deterministic.
    """
    candidates = []
    for path in _walk_py_files(recipe_dir):
        if path.name == "agent.py":
            depth = len(path.relative_to(recipe_dir).parts)
            candidates.append((depth, str(path), path))
    if not candidates:
        return None
    candidates.sort()
    agent_py = candidates[0][2]
    return agent_py, agent_py.parent


def module_has_name(tree: ast.Module, name: str) -> bool:
    """True if `name` is bound at module level by an assignment or a def."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return True
    return False


def imported_modules(tree: ast.Module) -> set[str]:
    """Every dotted module path imported anywhere in the file."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def check_adk_version_floor(
    deps: list[str], min_adk: str, migration_is_manual: bool
) -> Check:
    """Verify the recipe's google-adk specifier accepts the required floor."""
    floor = Version(min_adk)
    for raw in deps:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            continue
        if req.name.lower() != "google-adk":
            continue

        # prereleases=True so an alpha floor like `>=2.0.0a0` is judged on
        # what it admits, not silently treated as excluding everything.
        if req.specifier.contains(floor, prereleases=True):
            return Check(
                id="adk-version-floor",
                status=CLEAN,
                message=f"google-adk specifier accepts {min_adk}.",
                details={"requirement": raw, "min_google_adk": min_adk},
            )

        if not migration_is_manual:
            return Check(
                id="adk-version-floor",
                status=REPORT_ONLY,
                message=(
                    f"google-adk specifier `{raw}` excludes {min_adk}, and "
                    "policy allows an automatic bump — not implemented."
                ),
                details={"requirement": raw, "min_google_adk": min_adk},
            )

        return Check(
            id="adk-version-floor",
            status=NEEDS_INPUT,
            message=(
                f"google-adk is pinned `{raw}`, which excludes {min_adk}. The "
                "serving stack needs a2a-sdk>=1.0, and only google-adk 2.5.0+ "
                "permits it (2.0.0-2.4.0 cap a2a-sdk at <0.4). Crossing an ADK "
                "major is a code migration, not a pin edit — widening the "
                "ceiling here would produce a recipe importing a major it was "
                "never ported to. Migrate the agent first, then re-run."
            ),
            details={"requirement": raw, "min_google_adk": min_adk},
        )

    return Check(
        id="adk-version-floor",
        status=NEEDS_INPUT,
        message=(
            "No google-adk requirement found in [project].dependencies. Add "
            f"one accepting >={min_adk} before making this recipe deployable."
        ),
        details={"min_google_adk": min_adk},
    )


def locked_version(lock_path: Path, package: str) -> Version | None:
    """Return the version of `package` currently pinned in uv.lock, if any."""
    if not lock_path.is_file():
        return None
    try:
        data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    for entry in data.get("package", []):
        if entry.get("name") == package:
            try:
                return Version(str(entry["version"]))
            except (KeyError, InvalidVersion):
                return None
    return None


def _requirement_name(raw: str) -> str | None:
    """Canonical package name from a requirement string, or None if unparseable."""
    try:
        return Requirement(raw).name.lower().replace("_", "-")
    except InvalidRequirement:
        return None


def _spec_still_admits(declared_spec: str | None, version: Version) -> bool:
    """Would uv keep `version` when re-locking against `declared_spec`?

    This is what decides whether a plain `uv lock` is a no-op. uv preserves an
    already-locked version only while it still satisfies the declared
    specifier; once the specifier has moved past it, the lockfile is simply
    stale and a bare re-lock does raise it.

    Unknown or unparseable specifier -> True, the conservative answer: it
    sends the owner to `--upgrade-package`, which is correct in both cases,
    rather than to a command that might do nothing.
    """
    if not declared_spec:
        return True
    try:
        req = Requirement(declared_spec)
    except InvalidRequirement:
        return True
    if not str(req.specifier):
        return True
    return req.specifier.contains(version, prereleases=True)


def check_adk_locked_version(
    lock_path: Path, min_adk: str, declared_spec: str | None = None
) -> Check:
    """Judge the ADK migration risk on what the recipe RESOLVES to today.

    The declared specifier is the wrong signal on its own. A recipe pinning
    `google-adk>=1.0.0` accepts 2.6 on paper, so a specifier-only check passes
    it — but if its lockfile says 1.28.0, that recipe has only ever run on ADK
    1.x, and adding the serving dependencies silently drags it across a major
    version. That is a code migration wearing a metadata disguise, which is
    exactly the failure align-recipe-pyproject warns about.

    Conversely a loose specifier already locked to 2.6.2 (financial-advisor)
    is genuinely fine, and blocking it on the specifier alone would be wrong.

    So: compare MAJORS. Crossing one stops the run; a minor bump inside the
    same major is reported but allowed.

    A property of uv that this check must not misstate: `uv lock` is STICKY.
    It preserves an already-locked version that still satisfies the declared
    specifier, so re-locking does NOT raise a pin — only
    `uv lock --upgrade-package <name>` does. This check previously told owners
    the opposite, and core/python/rag-agent-search is what it cost: locked at
    google-adk 2.3.0 under `>=2.0.0`, re-locked as instructed, still 2.3.0,
    and the container then died on `cannot import name 'TextPart' from
    'a2a.types'` because adk <=2.4 predates the a2a-sdk <2 widening. Only a
    container build caught it.
    """
    floor = Version(min_adk)
    have = locked_version(lock_path, "google-adk")

    if have is None:
        return Check(
            id="adk-locked-version",
            status=REPORT_ONLY,
            message=(
                "No uv.lock, or google-adk absent from it — cannot tell which "
                "ADK version this recipe actually runs on. Verify the agent "
                f"works on {min_adk} before relying on the generated files."
            ),
        )

    if have.major < floor.major:
        return Check(
            id="adk-locked-version",
            status=NEEDS_INPUT,
            message=(
                f"uv.lock resolves google-adk to {have}, but deployability "
                f"needs {min_adk}. "
                + (
                    "The declared specifier still admits it, so re-locking "
                    f"IN PLACE keeps {have.major}.x (uv preserves a locked "
                    "version that satisfies the specifier) and the recipe "
                    "would ship the new serving dependencies against an ADK "
                    "major that cannot support them; resolving FRESH moves "
                    "it to "
                    if _spec_still_admits(declared_spec, have)
                    else "The declared specifier no longer admits it, so even "
                    "a plain re-lock moves this to "
                )
                + f"{floor.major}.x, which is code the agent has never run "
                f"against. Either way, port the agent to {floor.major}.x "
                "first, then re-lock and re-run. This script only rewrites "
                "metadata; it cannot migrate code."
            ),
            details={"locked": str(have), "min_google_adk": min_adk},
        )

    if have < floor:
        # Stickiness only bites when the locked version STILL SATISFIES the
        # declared specifier. If the recipe declares `>=2.6.0` while the lock
        # says 2.3.0, the lock is simply stale and a plain `uv lock` does
        # raise it — telling that owner "uv lock will not raise it" would be
        # just as false as the claim this replaced.
        sticky = _spec_still_admits(declared_spec, have)
        consequence = (
            " Then confirm the resolved pair — google-adk 2.5+ is what "
            "widened a2a-sdk to <2, and an older ADK alongside a2a-sdk 1.x "
            "fails at import with `cannot import name 'TextPart' from "
            "'a2a.types'`, which no static check can see."
        )
        if sticky:
            return Check(
                id="adk-locked-version",
                status=REPORT_ONLY,
                message=(
                    f"uv.lock resolves google-adk to {have}, below the "
                    f"{min_adk} floor, and the recipe's own specifier still "
                    f"admits {have}. A PLAIN `uv lock` WILL NOT RAISE IT: uv "
                    "preserves an already-locked version that satisfies the "
                    "specifier. Run `uv lock --upgrade-package google-adk` "
                    "instead." + consequence
                ),
                details={
                    "locked": str(have),
                    "min_google_adk": min_adk,
                    "sticky": True,
                    "remedy": "uv lock --upgrade-package google-adk",
                },
            )
        return Check(
            id="adk-locked-version",
            status=REPORT_ONLY,
            message=(
                f"uv.lock resolves google-adk to {have}, below the {min_adk} "
                f"floor, but the declared specifier no longer admits {have}, "
                "so the lockfile is merely stale and a plain `uv lock` will "
                "raise it." + consequence
            ),
            details={
                "locked": str(have),
                "min_google_adk": min_adk,
                "sticky": False,
            },
        )

    return Check(
        id="adk-locked-version",
        status=CLEAN,
        message=f"uv.lock already resolves google-adk to {have}.",
        details={"locked": str(have)},
    )


def check_already_deployable(
    recipe_dir: Path,
    package_dir: Path,
    *,
    generated_by_us: bool = False,
) -> Check:
    """Warn when the recipe already serves, by its own arrangement.

    `long-horizon-harness` is the case this exists for: it ships a Dockerfile,
    `deployable: true`, and a bespoke ~400-line `fast_api_app.py` that wires
    its own A2A routes from a `horizon/a2a/` package. Nothing here is
    "missing", but because it has no `app_utils/`, a naive run would happily
    add three modules that the recipe's own entrypoint never imports — dead
    code that looks load-bearing.

    Advisory, not a gate: a recipe may legitimately want to migrate onto the
    standard layout. But the owner should choose that, not discover it.
    """
    has_dockerfile = (recipe_dir / "Dockerfile").is_file()
    has_entrypoint = (package_dir / "fast_api_app.py").is_file()
    if has_dockerfile and has_entrypoint and generated_by_us:
        # These are OUR files, from an earlier invocation. The two-phase
        # --verify-container flow makes that the normal case: phase one
        # writes them, phase two re-runs after `uv lock` and finds them
        # present. Calling them "the recipe's own arrangement" would advise
        # the owner to protect a bespoke entrypoint that does not exist.
        return Check(
            id="already-deployable",
            status=CLEAN,
            message=(
                "A Dockerfile and entrypoint are present, but they are the "
                "ones this skill generated (content matches the templates), "
                "not a bespoke arrangement. Nothing to protect."
            ),
            details={"generated_by_us": True},
        )
    if not (has_dockerfile and has_entrypoint):
        return Check(
            id="already-deployable",
            status=CLEAN,
            message="Recipe does not already ship a container entrypoint.",
        )
    return Check(
        id="already-deployable",
        status=REPORT_ONLY,
        message=(
            "This recipe ALREADY has a Dockerfile and a "
            f"{package_dir.name}/fast_api_app.py, so it serves by its own "
            "arrangement. Neither will be replaced without --overwrite, but "
            "any app_utils/ modules generated alongside a bespoke entrypoint "
            "will be dead code unless someone wires them in. Confirm the "
            "owner actually wants to migrate onto the standard layout before "
            "applying."
        ),
        details={"dockerfile": has_dockerfile, "entrypoint": has_entrypoint},
    )


def check_legacy_app_utils(package_dir: Path, legacy_files: list[str]) -> Check:
    """Stop if the recipe carries the old ASP-era app_utils generation."""
    app_utils = package_dir / "app_utils"
    if not app_utils.is_dir():
        return Check(
            id="legacy-app-utils",
            status=CLEAN,
            message="No existing app_utils/ — nothing to collide with.",
        )
    present = sorted(f for f in legacy_files if (app_utils / f).is_file())
    if not present:
        return Check(
            id="legacy-app-utils",
            status=CLEAN,
            message="app_utils/ exists but carries no legacy-generation files.",
        )
    return Check(
        id="legacy-app-utils",
        status=NEEDS_INPUT,
        message=(
            f"{package_dir.name}/app_utils/ carries the old generation "
            f"({', '.join(present)}). Those wire telemetry and services "
            "differently from services.py, and the existing fast_api_app.py "
            "imports them. Generating over the top would orphan them or "
            "double-wire telemetry. A human needs to decide how to migrate."
        ),
        details={"legacy_files_present": present},
    )


def check_backing_infra(recipe_dir: Path, package_dir: Path) -> Check:
    """Detect infrastructure this script cannot provision.

    Advisory: it selects the OUTCOME (containerized vs deployable) rather than
    stopping the run.
    """
    reasons: list[str] = []

    for dirname in ("infra", "terraform"):
        if (recipe_dir / dirname).is_dir():
            reasons.append(f"has a {dirname}/ directory")

    found_services: set[str] = set()
    for path in _walk_py_files(package_dir):
        try:
            tree = ast.parse(
                path.read_text(encoding="utf-8"), filename=str(path)
            )
        except (SyntaxError, UnicodeDecodeError):
            continue
        for module in imported_modules(tree):
            for prefix, label in BACKING_SERVICE_IMPORTS.items():
                if module == prefix or module.startswith(prefix + "."):
                    found_services.add(label)
    for label in sorted(found_services):
        reasons.append(f"imports a client for {label}")

    env_example = recipe_dir / ".env.example"
    if env_example.is_file():
        text = env_example.read_text(encoding="utf-8")
        for var, label in BACKING_SERVICE_ENV_VARS.items():
            if re.search(rf"^\s*{re.escape(var)}\s*=", text, re.MULTILINE):
                reasons.append(f"declares {var}, implying {label}")

    if not reasons:
        return Check(
            id="backing-infra",
            status=CLEAN,
            message="No backing infrastructure detected — one-click deployable.",
        )
    return Check(
        id="backing-infra",
        status=REPORT_ONLY,
        message=(
            "Recipe needs infrastructure this skill cannot provision: "
            + "; ".join(reasons)
            + ". It can still be containerized, but it is not one-click "
            "deployable, so this skill will not SET manifest.deployable."
        ),
        details={"reasons": reasons},
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def render_template(text: str, package: str, project: str) -> str:
    return text.replace(PLACEHOLDER_PACKAGE, package).replace(
        PLACEHOLDER_PROJECT, project
    )


def inject_data_dir_copies(
    dockerfile: str, package: str, data_dirs: list[str]
) -> str:
    """Add `COPY ./<dir> ./<dir>` lines for runtime data the agent reads.

    Inserted immediately after the agent-package COPY so they land before
    `uv sync`, matching the layer ordering of every hand-written Dockerfile in
    the repo. A missing data directory does not fail the build — it fails at
    request time — which is exactly why they are confirmed by a human rather
    than guessed.
    """
    if not data_dirs:
        return dockerfile
    anchor = f"COPY ./{package} ./{package}"
    if anchor not in dockerfile:
        return dockerfile
    extra = "\n".join(f"COPY ./{d} ./{d}" for d in data_dirs)
    return dockerfile.replace(anchor, f"{anchor}\n\n{extra}", 1)


def set_python_base_image(dockerfile: str, python_version: str | None) -> str:
    """Rewrite `FROM python:X-slim` to the recipe's own floor.

    The vendored template hardcodes 3.12. Recipes in this repo target 3.11,
    3.12 and 3.13, and building on a different minor than the recipe declares
    is a real source of resolution failures.
    """
    if not python_version:
        return dockerfile
    return re.sub(
        r"^FROM python:[\d.]+-slim$",
        f"FROM python:{python_version}-slim",
        dockerfile,
        count=1,
        flags=re.MULTILINE,
    )


def python_floor_from_requires(requires_python: str | None) -> str | None:
    """Extract `3.11` from a specifier like `>=3.11,<3.14`."""
    if not requires_python:
        return None
    match = re.search(r">=\s*(\d+\.\d+)", requires_python)
    return match.group(1) if match else None


def generate_serving_files(
    *,
    templates_dir: Path,
    recipe_dir: Path,
    package_dir: Path,
    project_name: str,
    python_version: str | None,
    data_dirs: list[str],
    apply: bool,
    overwrite: bool,
    report: Report,
    required_files: list[str] | None = None,
) -> None:
    """Write the required serving files plus .dockerignore.

    The file list comes from `deployability.required_files` so the standard
    stays editable in policy.yml, which is what SKILL.md and this module's
    docstring both promise. It used to be hardcoded here, which made adding a
    file to policy.yml a silent no-op — the worst kind of configuration,
    because it looks like it worked.

    Each entry is a destination relative to the recipe, with `<pkg>` standing
    for the agent package; the template is found at the same path with the
    `<pkg>/` prefix removed.
    """
    package = package_dir.name
    entries = required_files or []
    if not entries:
        # A policy that lists nothing must be an error, never a quiet no-op.
        # Silently generating zero serving files and then flagging the recipe
        # deployable is precisely the failure this list is supposed to
        # prevent, and an empty or commented-out list looks like it worked.
        report.add(
            Check(
                id="serving-files",
                status=ERROR,
                message=(
                    "`deployability.required_files` is empty or missing from "
                    ".github/policy.yml, so no serving files would be "
                    "generated. Refusing to continue: a recipe with no "
                    "Dockerfile and no entrypoint cannot be deployable, and "
                    "reporting success here would be a false claim. This is "
                    "a fault in the SKILL's configuration, not in the recipe."
                ),
                details={SKILL_FAULT: True},
            )
        )
        return
    jobs: list[tuple[Path, Path]] = []
    for entry in entries:
        rel_entry = str(entry).strip().lstrip("/")
        # Never let a policy entry escape the recipe. `..` in a path would
        # otherwise write into a sibling recipe or the repo itself:
        # `dst.relative_to()` resolves it lexically instead of objecting.
        if ".." in Path(rel_entry).parts:
            report.add(
                Check(
                    id=f"file:{rel_entry}",
                    status=ERROR,
                    message=(
                        f"`deployability.required_files` entry `{entry}` "
                        "escapes the recipe directory. Refusing to write "
                        "outside the recipe. This is a fault in the SKILL's "
                        "policy, not in the recipe, so no deployable flag "
                        "was retracted."
                    ),
                    details={SKILL_FAULT: True},
                )
            )
            continue
        if rel_entry.startswith("<pkg>/"):
            tail = rel_entry[len("<pkg>/") :]
            dst = package_dir / tail
        else:
            tail = rel_entry
            dst = recipe_dir / rel_entry
        src = templates_dir / tail
        if not src.is_file():
            report.add(
                Check(
                    id=f"file:{rel_entry}",
                    status=ERROR,
                    message=(
                        f"`deployability.required_files` lists `{entry}` but "
                        f"no template exists at {src.relative_to(templates_dir.parent.parent)}. "
                        "Add the template or correct the policy entry — the "
                        "recipe would be missing a file the standard "
                        "requires. This is a fault in the SKILL, not in the "
                        "recipe, so no deployable flag was retracted."
                    ),
                    details={SKILL_FAULT: True},
                )
            )
            continue
        jobs.append((src, dst))

    for src, dst in jobs:
        rel = dst.relative_to(recipe_dir)
        if dst.exists() and not overwrite:
            report.add(
                Check(
                    id=f"file:{rel}",
                    status=REPORT_ONLY,
                    message=(
                        f"{rel} already exists — left untouched. Re-run with "
                        "--overwrite to replace it, after checking what it does."
                    ),
                )
            )
            continue

        content = render_template(
            src.read_text(encoding="utf-8"), package, project_name
        )
        if dst.name == "Dockerfile":
            content = set_python_base_image(content, python_version)
            content = inject_data_dir_copies(content, package, data_dirs)

        if apply:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content, encoding="utf-8")
            report.files_written.append(str(rel))
        report.add(
            Check(
                id=f"file:{rel}",
                status=FIXED if apply else WOULD_FIX,
                message=f"{'Wrote' if apply else 'Would write'} {rel}.",
            )
        )

    dockerignore = recipe_dir / ".dockerignore"
    if dockerignore.exists() and not overwrite:
        report.add(
            Check(
                id="file:.dockerignore",
                status=REPORT_ONLY,
                message=(
                    ".dockerignore already exists — left untouched. Re-run "
                    "with --overwrite to replace it."
                ),
            )
        )
    else:
        if apply:
            dockerignore.write_text(DOCKERIGNORE, encoding="utf-8")
            report.files_written.append(".dockerignore")
        report.add(
            Check(
                id="file:.dockerignore",
                status=FIXED if apply else WOULD_FIX,
                message=(
                    f"{'Wrote' if apply else 'Would write'} .dockerignore "
                    "(excludes .venv/, .env, .git/)."
                ),
            )
        )


# ---------------------------------------------------------------------------
# pyproject.toml patching
# ---------------------------------------------------------------------------


def _with_extras(req: Requirement, extras: list[str]) -> str:
    """Re-render `req` carrying `extras`, preserving everything else.

    Rebuilt from the parsed parts rather than string-patched so a marker or a
    URL survives: `foo>=1; python_version<"3.12"` must not lose its marker on
    the way through.
    """
    spec = str(req.specifier)
    url = f" @ {req.url}" if req.url else ""
    marker = f" ; {req.marker}" if req.marker else ""
    return f"{req.name}[{','.join(extras)}]{url}{spec}{marker}"


def _replace_requirement(deps: Any, old: str, new: str) -> None:
    """Swap one entry of the dependencies array in place.

    Index assignment keeps the entry's position (and tomlkit's surrounding
    formatting) rather than removing and appending, which would silently
    reorder a hand-curated, commented dependency list.
    """
    for i, raw in enumerate(deps):
        if str(raw) == old:
            deps[i] = new
            return


def patch_dependencies(
    doc: tomlkit.TOMLDocument, required: list[str], apply: bool
) -> Check:
    """Add missing required dependencies and merge in any missing extras.

    Version specifiers the recipe already declares are never rewritten — see
    the extras comment inside for why those two edits carry different risk.
    """
    project = doc.get("project")
    if project is None:
        return Check(
            id="required-dependencies",
            status=ERROR,
            message="pyproject.toml has no [project] table.",
        )
    deps = project.get("dependencies")
    if deps is None:
        deps = tomlkit.array()
        deps.multiline(True)
        project["dependencies"] = deps

    # Map normalised name -> the recipe's OWN requirement string. Reporting
    # the recipe's actual spec rather than the policy's is the whole point of
    # the "left alone" message: telling an owner that
    # `google-adk[gcp,otel-gcp]>=2.6.0,<3.0.0` is already present, when the
    # recipe really pins `>=2.2.0`, hides exactly the mismatch they were being
    # asked to check.
    existing: dict[str, str] = {}
    for raw in deps:
        try:
            existing[Requirement(str(raw)).name.lower()] = str(raw)
        except InvalidRequirement:
            continue

    missing: list[str] = []
    kept: list[dict[str, Any]] = []
    for spec in required:
        req = Requirement(spec)
        name = req.name.lower()
        if name not in existing:
            missing.append(spec)
            continue
        # Version compatibility is not the only thing that matters. Extras
        # carry real code: `google-adk[gcp,otel-gcp]` is what installs the
        # OTel exporters and GCP clients the generated services.py and
        # fast_api_app.py import. A recipe pinning bare `google-adk>=2.2.0`
        # satisfies the VERSION floor and still cannot run the serving stack,
        # so leaving it alone would knowingly emit a recipe that fails to
        # import.
        #
        # Extras are therefore MERGED IN, while the version specifier is left
        # exactly as the recipe wrote it. The two are not equivalent risks:
        # adding an extra only ever pulls in more of the same package's own
        # optional dependencies, whereas rewriting a version bound overrides a
        # deliberate pin and can move the recipe onto code it was never tested
        # against. So `google-adk>=2.2.0` becomes
        # `google-adk[gcp,otel-gcp]>=2.2.0` — never `>=2.6.0`.
        try:
            have = Requirement(existing[name])
            lacking = sorted(req.extras - have.extras)
        except InvalidRequirement:
            have, lacking = None, []
        merged = None
        if lacking and have is not None:
            merged = _with_extras(have, sorted(have.extras | req.extras))
            if apply:
                _replace_requirement(deps, existing[name], merged)
        kept.append(
            {
                "required": spec,
                "recipe_has": existing[name],
                "missing_extras": lacking,
                "rewritten_to": merged,
            }
        )

    kept_note = ""
    unchanged = [k for k in kept if not k["rewritten_to"]]
    if unchanged:
        pairs = "; ".join(
            f"{k['recipe_has']} (required: {k['required']})" for k in unchanged
        )
        kept_note = (
            f" Left {len(unchanged)} existing requirement(s) alone — {pairs}. "
            "A recipe may carry a deliberate tighter pin, so confirm each "
            "still admits the required version."
        )
    extras_gaps = [k for k in kept if k["rewritten_to"]]
    if extras_gaps:
        detail = "; ".join(
            f"{k['recipe_has']} -> {k['rewritten_to']}" for k in extras_gaps
        )
        kept_note += (
            f" {'Added' if apply else 'Would add'} missing extras — {detail}. "
            "Version bounds were NOT touched; only the extras the generated "
            "serving code imports were merged in."
        )

    if not missing:
        return Check(
            id="required-dependencies",
            status=CLEAN,
            message=(
                "All required serving dependencies are already declared."
                + kept_note
            ),
            details={"kept": kept},
        )

    if apply:
        for spec in missing:
            deps.append(spec)
        deps.multiline(True)

    return Check(
        id="required-dependencies",
        status=FIXED if apply else WOULD_FIX,
        message=(
            f"{'Added' if apply else 'Would add'} {len(missing)} required "
            f"dependency/ies: {', '.join(missing)}." + kept_note
        ),
        details={"added": missing, "kept": kept},
    )


def dependencies_changed(deps_check: Check) -> bool:
    """Did this run alter [project].dependencies?

    Read BOTH signals, because the check's status is not sufficient on its
    own. When every required package is already declared but one of them
    lacks an extra, `patch_dependencies` rewrites that requirement in place
    and still reports CLEAN — so a status test alone would conclude nothing
    changed for a run that just edited pyproject.toml.
    """
    details = deps_check.details
    if details.get("added"):
        return True
    return any(k.get("rewritten_to") for k in details.get("kept", []))


def patch_hatch_packages(
    doc: tomlkit.TOMLDocument, package: str, apply: bool
) -> Check:
    """Ensure [tool.hatch.build.targets.wheel].packages lists the agent package."""
    tool = doc.get("tool")
    build_backend = (doc.get("build-system") or {}).get("build-backend", "")
    if "hatchling" not in str(build_backend):
        return Check(
            id="hatch-wheel-packages",
            status=REPORT_ONLY,
            message=(
                f"Build backend is `{build_backend or 'unset'}`, not hatchling — "
                "skipping the hatch wheel-packages check. Confirm the recipe's "
                f"backend installs the `{package}` package, or `uv sync` will "
                "fail in the image."
            ),
            details={"build_backend": str(build_backend)},
        )

    wheel = None
    if tool is not None:
        wheel = (
            tool.get("hatch", {})
            .get("build", {})
            .get("targets", {})
            .get("wheel")
        )
    if wheel is not None and package in [
        str(p) for p in wheel.get("packages", [])
    ]:
        return Check(
            id="hatch-wheel-packages",
            status=CLEAN,
            message=f"hatch wheel packages already includes `{package}`.",
        )

    if apply:
        if tool is None:
            tool = tomlkit.table(is_super_table=True)
            doc["tool"] = tool
        node: Any = tool
        for key in ("hatch", "build", "targets"):
            if key not in node:
                node[key] = tomlkit.table(is_super_table=True)
            node = node[key]
        if "wheel" not in node:
            node["wheel"] = tomlkit.table()
        wheel_tbl = node["wheel"]
        packages = wheel_tbl.get("packages")
        if packages is None:
            packages = tomlkit.array()
            wheel_tbl["packages"] = packages
        if package not in [str(p) for p in packages]:
            packages.append(package)

    return Check(
        id="hatch-wheel-packages",
        status=FIXED if apply else WOULD_FIX,
        message=(
            f"{'Added' if apply else 'Would add'} `{package}` to "
            "[tool.hatch.build.targets.wheel].packages — uv sync fails without it."
        ),
    )


# ---------------------------------------------------------------------------
# agent.py patching
# ---------------------------------------------------------------------------


def patch_app_object(agent_py: Path, package: str, apply: bool) -> Check:
    """Ensure agent.py defines `app = App(root_agent=root_agent, name=...)`.

    Appended at end of file rather than inserted, because `root_agent` must
    already be bound. The import goes after the last top-level import so the
    file keeps a conventional shape and ruff's import rules stay happy.
    """
    try:
        source = agent_py.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(agent_py))
    except (SyntaxError, UnicodeDecodeError) as e:
        return Check(
            id="app-object",
            status=ERROR,
            message=f"Could not parse {agent_py.name}: {e}",
        )

    if not module_has_name(tree, "root_agent"):
        return Check(
            id="app-object",
            status=NEEDS_INPUT,
            message=(
                f"{agent_py.name} defines no module-level `root_agent`, so an "
                "`App` cannot be wired to it. The serving entrypoint imports "
                "both by name."
            ),
        )

    if module_has_name(tree, "app"):
        return Check(
            id="app-object",
            status=CLEAN,
            message=f"{agent_py.name} already defines a module-level `app`.",
        )

    lines = source.splitlines()
    needs_import = "google.adk.apps" not in imported_modules(tree)

    if apply:
        if needs_import:
            last_import_line = 0
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    last_import_line = max(
                        last_import_line, node.end_lineno or 0
                    )
            lines.insert(last_import_line, "from google.adk.apps import App")
        body = "\n".join(lines).rstrip("\n")
        body += f'\n\napp = App(root_agent=root_agent, name="{package}")\n'
        agent_py.write_text(body, encoding="utf-8")

    return Check(
        id="app-object",
        status=FIXED if apply else WOULD_FIX,
        message=(
            f"{'Added' if apply else 'Would add'} "
            f'`app = App(root_agent=root_agent, name="{package}")` to '
            f"{agent_py.name}"
            + (" (plus the App import)." if needs_import else ".")
        ),
    )


# ---------------------------------------------------------------------------
# manifest.yaml
# ---------------------------------------------------------------------------


# Marks a Check whose failure is OUR fault, not the recipe's.
SKILL_FAULT = "skill_fault"


def _manifest_claims_deployable(manifest_path: Path) -> bool:
    """Does manifest.yaml already assert `deployable: true`?

    Shared by every branch that wants to say something about the flag. Two
    branches previously asserted "not set" without looking, and this repo has
    recipes that ship the flag — a claim the reader falsifies by opening the
    file.

    Only the spellings ruamel actually resolves to boolean true. YAML 1.2,
    which ruamel defaults to, parses `yes`/`on` as STRINGS, so accepting them
    here would disagree with the parse used elsewhere in this module.
    """
    if not manifest_path.is_file():
        return False
    try:
        text = manifest_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(
        re.match(r"^deployable[ \t]*:[ \t]*true[ \t]*(#.*)?$", ln, re.I)
        for ln in text.splitlines()
    )


def runtime_platform_failure(logs: str) -> bool:
    """Did the CONTAINER RUNTIME refuse to exec the image's binary?

    Deliberately far narrower than failure_is_environmental, and separate from
    it, because this one reads the container's log — and an earlier attempt to
    run that log through the general infrastructure vocabulary was a disaster:
    phrases like "connection reset by peer" and "timed out" appear constantly
    in ordinary Python tracebacks, so a genuine boot crash was laundered into
    a pass.

    This matches ONE signature, as a whole line:

        exec /usr/local/bin/uv: exec format error

    which the containerd shim writes BEFORE any application code runs, when
    the image's architecture does not match the host and no emulation is
    registered. An application cannot reach the point of printing its own
    tracebacks and also be the thing that failed to exec. That is what makes
    it safe to read here where the general vocabulary was not.

    A shebang-less script inside a bespoke Dockerfile produces the same errno,
    but we only build a foreign Dockerfile for an allowlisted recipe, so the
    exposure is bounded and the alternative — deleting a contributor's flag
    because our host lacks binfmt — is worse.
    """
    lines = [ln for ln in (logs or "").splitlines() if ln.strip()]
    # Require it to be the ONLY output. The justification above is precisely
    # that no application code ran; a log that also contains application
    # output contradicts that, and would let a recipe whose crash happens to
    # echo a subprocess's exec error escape its verdict.
    return len(lines) == 1 and bool(
        re.match(r"^\s*exec /\S+: exec format error\s*$", lines[0])
    )


def _verification_was_inconclusive(report: Report) -> bool:
    """Was verification ATTEMPTED but defeated by the environment?

    Distinguished from "never attempted" by the presence of a container-*
    check that reported an environmental or platform problem. Telling those
    apart is the whole point: one asks the owner to go find docker, the other
    asks them to retry the command they just ran.
    """
    return any(
        c.id in ("container-build", "container-serves")
        and (
            c.details.get("environmental") or c.details.get("platform_failure")
        )
        for c in report.checks
    )


def _has_error(report: Report) -> bool:
    """Did anything fail in a way that disqualifies THE RECIPE?

    A blanket rule, because listing the specific disqualifying conditions
    proved fragile — each new check is one someone can forget to add, and the
    failure mode is a false one-click-deploy claim rather than a visible
    crash.

    But it deliberately ignores checks flagged SKILL_FAULT. A policy entry
    naming a template nobody vendored is a packaging bug in this skill; if
    that deleted a contributor's `deployable: true`, our own broken release
    would be destroying their data. Skill faults must stop the run loudly
    without passing judgement on the recipe.
    """
    return any(
        c.status == ERROR and not c.details.get(SKILL_FAULT)
        for c in report.checks
    )


def _has_skill_fault(report: Report) -> bool:
    """Did the SKILL itself fail (bad policy, missing template)?

    Such a run says nothing about the recipe, so it must neither set a
    deployable flag nor retract an existing one: leave the manifest exactly
    as found and report loudly.
    """
    return any(
        c.status == ERROR and c.details.get(SKILL_FAULT) for c in report.checks
    )


def clear_manifest_deployable(manifest_path: Path, apply: bool) -> Check:
    """Remove a `deployable: true` that verification has just disproved.

    Declining to WRITE the flag is not enough. The ordinary sequence is: run
    once with no docker (flag written on static checks, outcome
    `-unverified`), then later run with --verify-container on a machine that
    has one. If that verification fails and we merely skip the write, the
    earlier `true` survives while the report says "NOT set" — the report lies,
    and the manifest keeps exactly the false one-click claim this feature
    exists to prevent.

    The key is removed rather than set to false because the schema treats it
    as optional and false by default ("omit if false"), so removal is how a
    manifest says "not deployable" in this repo's own convention.
    """
    if not manifest_path.is_file():
        return Check(
            id="manifest-deployable",
            status=REPORT_ONLY,
            message=(
                "NOT set: container verification failed. No manifest.yaml "
                "exists, so there was no claim to retract."
            ),
        )
    # newline="" so line terminators survive verbatim rather than being
    # translated to \n by universal-newline mode.
    with open(manifest_path, encoding="utf-8", newline="") as f:
        text = f.read()
    # keepends preserves each line's own terminator, so a CRLF file is not
    # silently rewritten to LF — that turns a one-line removal into an
    # every-line diff on a Windows checkout.
    lines = text.splitlines(keepends=True)
    # Require a SCALAR value on the same line. `deployable:` followed by an
    # indented block, a list, or a nested mapping is off-schema, and deleting
    # only its first line splices the orphaned body onto the previous key —
    # silent semantic corruption rather than a removal. Leave those alone and
    # say so.
    scalar_flag = re.compile(r"^deployable[ \t]*:[ \t]*\S[^\r\n]*[\r\n]*$")
    kept = [ln for ln in lines if not scalar_flag.match(ln)]

    if len(kept) == len(lines):
        # Nothing removable. Either there was no flag, or it is an off-schema
        # non-scalar we will not touch — flag that case rather than implying
        # the manifest is clean.
        odd = any(re.match(r"^deployable[ \t]*:", ln) for ln in lines)
        return Check(
            id="manifest-deployable",
            status=REPORT_ONLY,
            message=(
                (
                    "A `deployable:` key exists but its value is not a simple "
                    "scalar, so it was left untouched to avoid corrupting the "
                    "document. REVIEW IT BY HAND — the recipe did not pass "
                    "verification. "
                )
                if odd
                else "NOT set. "
            )
            + "A recipe that did not pass is not deployable, whatever the "
            "static checks said. Fix the failure above and re-run.",
        )

    if apply:
        with open(manifest_path, "w", encoding="utf-8", newline="") as f:
            f.write("".join(kept))
    return Check(
        id="manifest-deployable",
        status=FIXED if apply else WOULD_FIX,
        message=(
            f"{'REMOVED' if apply else 'Would remove'} the existing "
            "`deployable` flag: the recipe did not pass, so the claim already "
            "in manifest.yaml is false. Re-verify successfully to restore it."
        ),
    )


def patch_manifest_deployable(
    manifest_path: Path, infra_clean: bool, apply: bool
) -> Check:
    """Set `deployable: true`, but only when no backing infra was detected."""
    if not infra_clean:
        # Read before asserting. Two recipes in this repo already ship
        # `deployable: true`, and reporting "left unset" about a file that
        # says otherwise is a statement the reader can immediately falsify.
        already = _manifest_claims_deployable(manifest_path)
        if already:
            return Check(
                id="manifest-deployable",
                status=REPORT_ONLY,
                message=(
                    "NOT changed. This recipe needs backing infrastructure, "
                    "so this skill would not set `deployable`, but the "
                    "manifest ALREADY says `deployable: true` — pre-existing, "
                    "not written here. It is left alone: the infra detector "
                    "fires on signals as broad as an `import sqlalchemy`, so "
                    "overriding an owner's explicit claim on that basis would "
                    "be presumptuous. Confirm it is accurate."
                ),
                details={"pre_existing_flag": True},
            )
        return Check(
            id="manifest-deployable",
            status=REPORT_ONLY,
            message=(
                "Left manifest.deployable unset: the recipe needs backing "
                "infrastructure, so it is containerized but not one-click "
                "deployable. Claiming otherwise would put a false flag in the "
                "manifest."
            ),
        )
    if not manifest_path.is_file():
        return Check(
            id="manifest-deployable",
            status=REPORT_ONLY,
            message="No manifest.yaml found — nothing to flag.",
        )

    text = manifest_path.read_text(encoding="utf-8")

    # Read with ruamel, but WRITE textually. A round-trip dump reflows the
    # whole document — it re-indents every sequence to its own default and
    # hard-wraps long scalars — turning "add one boolean key" into a 30-line
    # diff that silently restyles a file the owner did not ask us to touch.
    # Adding a top-level scalar is a one-line edit, so make it one.
    yaml = YAML()
    yaml.preserve_quotes = True
    try:
        data = yaml.load(text) or {}
    except Exception as e:
        return Check(
            id="manifest-deployable",
            status=ERROR,
            message=f"Failed to parse manifest.yaml: {e}",
        )

    if data.get("deployable") is True:
        return Check(
            id="manifest-deployable",
            status=CLEAN,
            message="manifest.deployable is already true.",
        )

    lines = text.splitlines()
    existing = next(
        (i for i, ln in enumerate(lines) if re.match(r"^deployable\s*:", ln)),
        None,
    )
    if existing is not None:
        # Present but false/commented-out value — replace in place so the key
        # keeps its position and any trailing comment context around it.
        new_lines = list(lines)
        new_lines[existing] = "deployable: true"
    else:
        # Insert after `language:` (where the schema examples put it), else
        # after `type:`, else append. Anchoring beats appending because a
        # manifest often ends inside a nested block, where a top-level key
        # appended at EOF reads as belonging to that block.
        anchor = next(
            (
                i
                for key in ("language", "type", "status")
                for i, ln in enumerate(lines)
                if re.match(rf"^{key}\s*:", ln)
            ),
            None,
        )
        new_lines = list(lines)
        new_lines.insert(
            len(lines) if anchor is None else anchor + 1, "deployable: true"
        )

    if apply:
        manifest_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    return Check(
        id="manifest-deployable",
        status=FIXED if apply else WOULD_FIX,
        message=f"{'Set' if apply else 'Would set'} manifest.deployable = true.",
    )


# ---------------------------------------------------------------------------
# agents-cli-manifest.yaml
# ---------------------------------------------------------------------------

# Fields DELIBERATELY omitted, and why omission is more correct than a value.
# agents-cli's ProjectConfig.from_dict gives every field a default, so nothing
# here is required — the question is only which values would be TRUE.
#
#   acli_version   No agents-cli scaffold produced this recipe, so there is no
#                  honest value. Absence is handled explicitly: check_cli_version
#                  returns early on a falsy version and scaffold_older_than
#                  returns False, so both callers fall back to generic guidance.
#                  A FABRICATED version is actively harmful — it makes the CLI
#                  print "Upgrade the project: agents-cli scaffold upgrade" for
#                  a project that was never scaffolded and cannot be upgraded.
#   generated_at   Never read by from_dict, and would assert a generation event
#                  that did not happen.
#   base_template  Only consumed by upgrade/enhance (metadata_to_cli_args), which
#                  do not apply here. It already defaults to "adk".
#   description    Never read by from_dict; manifest.yaml already carries it.
_ACLI_MANIFEST = """\
# agents-cli project manifest (agents-cli 1.x reads this file).
#
# Written by the make-python-recipe-deployable repo skill — NOT by an
# agents-cli scaffold. `acli_version`, `generated_at` and `base_template` are
# deliberately omitted rather than invented: no scaffold produced this recipe,
# and a fabricated version makes agents-cli offer a `scaffold upgrade` that
# cannot work. agents-cli treats an absent version as "unknown" and falls back
# to generic guidance, which is the accurate outcome here.
#
# `agents-cli deploy` uses this file as the project-root marker and reads
# `deployment_target` to choose how to deploy.
name: "{name}"
agent_directory: "{agent_directory}"
language: "python"
region: "{region}"
create_params:
  deployment_target: "cloud_run"
  session_type: "in_memory"
  is_a2a: true
  cicd_runner: "skip"
  agent_gateway: false
  agent_guidance_filename: "{guidance}"
"""


def detect_guidance_filename(recipe_dir: Path) -> str:
    """Which agent-guidance file the recipe actually ships.

    Every core/ recipe is required to carry AGENTS.md (policy.yml
    required_files.by_root.core), while agents-cli's own default is GEMINI.md.
    Reporting the wrong one would send a reader to a file that is not there.
    """
    for candidate in ("AGENTS.md", "GEMINI.md", "CLAUDE.md"):
        if (recipe_dir / candidate).is_file():
            return candidate
    return "AGENTS.md"


def write_agents_cli_manifest(
    recipe_dir: Path,
    *,
    project_name: str,
    agent_directory: str,
    region: str,
    apply: bool,
    report: Report | None = None,
) -> Check:
    """Write agents-cli-manifest.yaml with only fields that are actually true."""
    path = recipe_dir / "agents-cli-manifest.yaml"
    if path.is_file():
        return Check(
            id="agents-cli-manifest",
            status=REPORT_ONLY,
            message=(
                "agents-cli-manifest.yaml already exists — left untouched. If it "
                "predates the current agents-cli (check `acli_version`), its "
                "create_params may not describe what this skill just generated."
            ),
        )

    content = _ACLI_MANIFEST.format(
        name=project_name,
        agent_directory=agent_directory,
        region=region,
        guidance=detect_guidance_filename(recipe_dir),
    )
    if apply:
        path.write_text(content, encoding="utf-8")
        # Record it: a crash-recovery reader is told to trust files_written,
        # and this file landing unrecorded made that list quietly incomplete.
        if report is not None:
            report.files_written.append("agents-cli-manifest.yaml")

    return Check(
        id="agents-cli-manifest",
        status=FIXED if apply else WOULD_FIX,
        message=(
            f"{'Wrote' if apply else 'Would write'} agents-cli-manifest.yaml "
            f"(region {region}). This is what lets `agents-cli deploy` find the "
            "project root and pick a deployment target. acli_version / "
            "generated_at / base_template are omitted on purpose — see the "
            "file's header."
        ),
        details={"region": region},
    )


# ---------------------------------------------------------------------------
# Container verification
# ---------------------------------------------------------------------------
#
# The one step that turns "we generated a Dockerfile" into evidence. Every
# subprocess call goes through _docker() so tests can replace exactly one
# seam instead of patching subprocess globally.


def _docker(
    args: list[str], timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    """Run a docker command, capturing output and never raising on failure.

    Failure is data here, not an exception: a non-zero `docker info` is how a
    stopped daemon announces itself, and that is a normal condition rather
    than an error. A timeout or a vanished binary is normalised into the same
    CompletedProcess shape so every caller has one thing to inspect.
    """
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=["docker", *args],
            returncode=124,  # conventional timeout code
            stdout="",
            stderr=f"docker {' '.join(args)} timed out after {timeout}s",
        )
    except (OSError, ValueError) as e:
        return subprocess.CompletedProcess(
            args=["docker", *args], returncode=127, stdout="", stderr=str(e)
        )


def detect_docker() -> tuple[str, str]:
    """Classify docker into one of three states. Returns (state, detail).

    Three states, not two, because "no binary" and "daemon down" need
    different words to the user even though both mean "cannot verify":

      absent       nothing named docker on PATH
      unreachable  the CLI exists but `docker info` fails — a stopped daemon,
                   a socket the user lacks permission for, a broken context
      usable       the daemon answered

    `docker info`'s exit code is the honest probe. Checking whether a dockerd
    PROCESS is alive would be wrong: a running daemon whose socket the caller
    cannot open is still unusable, and reporting it as available would send
    the run into a build that cannot start.
    """
    if shutil.which("docker") is None:
        return DOCKER_ABSENT, "No `docker` on PATH."
    proc = _docker(["info", "--format", "{{.ServerVersion}}"], timeout=30)
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "").strip().splitlines()
        return DOCKER_UNREACHABLE, (
            reason[-1] if reason else "`docker info` failed."
        )
    return DOCKER_USABLE, f"Docker daemon {proc.stdout.strip()} responding."


def check_docker(state: str, detail: str) -> Check:
    """Report docker availability. Never an error, whatever the state.

    REPORT_ONLY across the board is deliberate. The skill's primary user has
    no container runtime, and surfacing that as an error would turn the
    normal case into a red herring on every single run.
    """
    if state == DOCKER_USABLE:
        return Check(
            id="docker",
            status=REPORT_ONLY,
            message=(
                f"Docker is usable — {detail} Container verification can run; "
                "ask the owner before starting one."
            ),
            details={"docker_state": state},
        )
    why = (
        "no `docker` binary on PATH"
        if state == DOCKER_ABSENT
        else f"the daemon is not responding ({detail})"
    )
    return Check(
        id="docker",
        status=REPORT_ONLY,
        message=(
            f"Docker is unavailable — {why}. Skipping container "
            "verification; this is not a failure. The recipe is assessed on "
            "static checks alone, and the outcome says `-unverified` so the "
            "claim is not mistaken for a proven one."
        ),
        details={"docker_state": state},
    )


def lockfile_is_current(
    recipe_dir: Path, timeout: int = 300
) -> tuple[bool, str]:
    """Is uv.lock in sync with pyproject.toml? Read-only; never writes.

    `uv lock --check` rather than `uv lock`: this script does not own the
    lockfile. The calling skill re-locks as its own step so a resolution
    failure is attributable to resolution, not to a container build that
    happened to be downstream of it.
    """
    if not (recipe_dir / "uv.lock").is_file():
        return False, "No uv.lock in the recipe."
    try:
        proc = subprocess.run(
            ["uv", "lock", "--check"],
            cwd=recipe_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"Could not check lockfile currency: {e}"
    if proc.returncode == 0:
        return True, "uv.lock matches pyproject.toml."
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, (tail[-1] if tail else "uv.lock is out of date.")


def _sanitise_tag(name: str) -> str:
    """Docker tags are lowercase and a restricted alphabet."""
    return re.sub(r"[^a-z0-9_.-]+", "-", name.lower()).strip("-.") or "recipe"


def dockerfile_is_generated(
    *,
    recipe_dir: Path,
    templates_dir: Path,
    package: str,
    project_name: str,
    python_version: str | None,
    data_dirs: list[str],
) -> bool:
    """Is the Dockerfile on disk byte-identical to what we would generate?

    Content comparison rather than "did this invocation write it". The
    two-phase flow makes the latter wrong: phase one writes the Dockerfile,
    phase two re-invokes after `uv lock` and finds it already present, so the
    generation step reports "left untouched" for a file this skill authored
    moments earlier. Trusting that signal would label our own output foreign,
    and — because a foreign Dockerfile is not built unattended — would skip
    verification entirely for every recipe not on the allowlist, silently
    disabling the feature in its normal mode.

    Comparing content answers the question actually being asked: is executing
    this file's RUN instructions equivalent to executing our own template?
    """
    dockerfile = recipe_dir / "Dockerfile"
    if not dockerfile.is_file():
        return False
    template = templates_dir / "Dockerfile"
    if not template.is_file():
        return False
    expected = render_template(
        template.read_text(encoding="utf-8"), package, project_name
    )
    expected = set_python_base_image(expected, python_version)
    expected = inject_data_dir_copies(expected, package, data_dirs)
    return dockerfile.read_text(encoding="utf-8") == expected


def entrypoint_is_generated(
    *,
    package_dir: Path,
    templates_dir: Path,
    package: str,
    project_name: str,
) -> bool:
    """Is `<pkg>/fast_api_app.py` byte-identical to the rendered template?

    Separate from the Dockerfile check because the two can disagree, and the
    already-deployable advisory is about the ENTRYPOINT. Deciding it on the
    Dockerfile alone suppressed the advisory on the second run for a recipe
    whose entrypoint is bespoke and whose Dockerfile we had just written —
    and the two-phase flow makes that second run mandatory.
    """
    existing = package_dir / "fast_api_app.py"
    template = templates_dir / "fast_api_app.py"
    if not existing.is_file() or not template.is_file():
        return False
    expected = render_template(
        template.read_text(encoding="utf-8"), package, project_name
    )
    return existing.read_text(encoding="utf-8") == expected


def parse_env_example(path: Path) -> dict[str, str]:
    """Read a recipe's .env.example into a plain dict.

    Deliberately simple: `KEY=value`, skipping blanks, comments and any
    trailing inline comment. Not a dotenv implementation — quoting and
    interpolation are not used by the .env.example files in this repo, and
    guessing at them would invent values the recipe never declared.
    """
    if not path.is_file():
        return {}
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.isidentifier():
            continue
        env[key] = value.split(" #")[0].strip().strip("'\"")
    return env


def _probe(url: str, timeout: int = 10) -> tuple[int, str]:
    """GET a URL. Returns (status, body-or-error). stdlib only, on purpose.

    urllib rather than httpx so verification adds no dependency to the
    script's documented `uv run --with ...` invocation.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read(2048).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.reason or ""
    except Exception as e:  # connection refused while still booting, etc.
        return 0, str(e)


def verify_container(
    *,
    recipe_dir: Path,
    package: str,
    settings: dict[str, Any],
    may_run: bool,
    report: Report,
    dockerfile_is_ours: bool = True,
) -> bool | None:
    """Build the generated Dockerfile, run it, and probe it.

    Returns True (built and served), False (proved broken — the caller must
    withhold manifest.deployable) or None (no verdict: built but deliberately
    not run). None is NOT a pass; build success alone does not prove the app
    serves, and claiming otherwise is the overclaim this whole step exists to
    prevent.

    Cleans up after itself in a finally: the container and the image it
    built. The `python:*-slim` base is left in the cache — it is shared, and
    evicting another user's base layer to tidy up after ourselves is rude.
    """
    port = int(settings.get("probe_port", 8080))
    build_timeout = int(settings.get("build_timeout_seconds", 900))
    ready_timeout = int(settings.get("ready_timeout_seconds", 120))
    # The recipe's declared environment first, policy overrides second: a
    # value the standard names explicitly beats the recipe's placeholder.
    env_map: dict[str, str] = {}
    if settings.get("load_env_example"):
        env_map.update(parse_env_example(recipe_dir / ".env.example"))
    env_map.update(dict(settings.get("container_env") or {}))
    paths = [
        str(p).replace("<pkg>", package)
        for p in (settings.get("probe_paths") or ["/list-apps"])
    ]

    tag = f"adk-recipe-verify/{_sanitise_tag(recipe_dir.name)}:verify"
    name = f"adk-verify-{_sanitise_tag(recipe_dir.name)}"

    # --- build -------------------------------------------------------------
    # --platform linux/amd64 because Cloud Run runs amd64. A no-op on an x86
    # host; on arm it is the difference between verifying the image the
    # target will actually run and verifying a different one.
    build = _docker(
        [
            "build",
            "--platform",
            "linux/amd64",
            "-t",
            tag,
            "-f",
            str(recipe_dir / "Dockerfile"),
            str(recipe_dir),
        ],
        timeout=build_timeout,
    )
    if build.returncode != 0:
        # An environmental failure is not evidence about the recipe. Returning
        # False here would retract a `deployable: true` the contributor wrote,
        # because our DNS was down — data loss caused by our own machine.
        environmental = failure_is_environmental(build)
        which = (
            "the recipe's OWN Dockerfile"
            if not dockerfile_is_ours
            else "the generated Dockerfile"
        )
        report.add(
            Check(
                id="container-build",
                status=REPORT_ONLY if environmental else ERROR,
                message=(
                    (
                        f"Build of {which} could not be completed for an "
                        "ENVIRONMENTAL reason, so nothing was concluded about "
                        "the recipe and nothing was RETRACTED (an inconclusive run cannot disprove a recipe). Static checks may still earn the flag on their own — see the manifest-deployable check for what actually happened. "
                    )
                    if environmental
                    else (
                        f"{which.upper()} DOES NOT BUILD. The recipe is not "
                        "deployable, and any existing manifest.deployable "
                        "flag has been retracted. "
                    )
                )
                + "Build log tail:\n"
                + _tail(build.stderr or build.stdout, 25),
                details={
                    "image": tag,
                    "hint": _build_failure_hint(build),
                    "environmental": environmental,
                },
            )
        )
        # Still clean up: a build that fails at a late layer leaves the tag
        # and its completed layers behind just as a successful one does.
        _cleanup(name, tag)
        return None if environmental else False
    # Say which Dockerfile was built. Claiming "the generated Dockerfile"
    # when an existing one was left in place would attribute a pass to code
    # this skill did not write.
    provenance = (
        "the generated Dockerfile"
        if dockerfile_is_ours
        else "the recipe's OWN pre-existing Dockerfile (not generated by "
        "this skill)"
    )
    report.add(
        Check(
            id="container-build",
            status=CLEAN,
            message=f"Image built from {provenance} ({tag}).",
            details={"image": tag, "dockerfile_is_ours": dockerfile_is_ours},
        )
    )

    if not may_run:
        report.add(
            Check(
                id="container-serves",
                status=REPORT_ONLY,
                message=(
                    "Built but NOT run: this recipe is not on "
                    "`deployability.verification.run_allowlist`, and some "
                    "recipes create real cloud resources at import. The image "
                    "compiles; whether it SERVES is unproven, so the outcome "
                    "stays `-unverified`. Add the recipe to the allowlist "
                    "after reading its package for import-time side effects."
                ),
            )
        )
        _cleanup(name, tag)
        return None

    # --- run and probe -----------------------------------------------------
    try:
        _docker(["rm", "-f", name], timeout=60)  # any stale leftover
        env_args: list[str] = []
        for key, value in env_map.items():
            env_args += ["-e", f"{key}={value}"]
        # Ephemeral host port: a fixed one collides with whatever the
        # developer already has on 8080.
        started = _docker(
            [
                "run",
                "-d",
                "--name",
                name,
                "-p",
                f"127.0.0.1::{port}",
                *env_args,
                tag,
            ],
            timeout=120,
        )
        if started.returncode != 0:
            # "port is already allocated" is a leftover container, not a
            # broken recipe — inconclusive, so no verdict and no retraction.
            environmental = failure_is_environmental(started)
            report.add(
                Check(
                    id="container-serves",
                    status=REPORT_ONLY if environmental else ERROR,
                    message=(
                        (
                            "Image built, but the container could not be "
                            "started for an ENVIRONMENTAL reason. Nothing was "
                            "concluded and nothing was RETRACTED (an inconclusive run cannot disprove a recipe). Static checks may still earn the flag on their own — see the manifest-deployable check for what actually happened. \n"
                        )
                        if environmental
                        else (
                            "Image built but the container would not start. "
                            "Any existing manifest.deployable was retracted.\n"
                        )
                    )
                    + _tail(started.stderr or started.stdout, 20),
                    details={"environmental": environmental},
                )
            )
            return None if environmental else False

        mapped = _docker(["port", name, str(port)], timeout=30)
        host_port = _parse_host_port(mapped.stdout)
        if host_port is None:
            report.add(
                Check(
                    id="container-serves",
                    status=ERROR,
                    message=(
                        f"Container started but port {port} is not published "
                        f"— cannot probe it. `docker port` said: "
                        f"{mapped.stdout.strip() or mapped.stderr.strip()}"
                    ),
                )
            )
            return False

        base = f"http://127.0.0.1:{host_port}"
        ready, boot_detail, exited = _await_ready(
            base + paths[0], name, ready_timeout
        )
        if not ready:
            # A verdict exists if the container exited OR it answered with a
            # non-200 — both are facts about the app. Total silence from a
            # still-running container is the only inconclusive case.
            answered = "answered but never returned 200" in boot_detail
            logs = _container_logs(name)
            # The ONE case where the container's own log is trustworthy
            # evidence about the host: the runtime refused to exec the binary,
            # so no application code ran at all. Everything else in that log
            # was written by the recipe and must never override `exited`.
            platform_failure = exited and runtime_platform_failure(logs)
            # `exited` is otherwise the strongest evidence this tool has, and
            # nothing else in the log may override it.
            verdict = (exited or answered) and not platform_failure
            if platform_failure:
                headline = (
                    "The container runtime could not exec the image's binary "
                    "(architecture mismatch with no emulation registered on "
                    "this host). No application code ran, so nothing was "
                    "concluded about the recipe and nothing was RETRACTED. "
                    "Verify on an amd64 host, or register binfmt. "
                )
            elif exited:
                headline = "Container never served: it EXITED during startup. "
            elif answered:
                headline = (
                    "Container is running but does NOT serve the expected "
                    "endpoint (it answered with a non-200 for the whole "
                    "window; it did not crash). "
                )
            else:
                headline = (
                    "Container did not answer at all in time. This is "
                    "INCONCLUSIVE, so nothing was RETRACTED (an inconclusive run cannot disprove a recipe). Static checks may still earn the flag on their own — see the manifest-deployable check for what actually happened. "
                )
            report.add(
                Check(
                    id="container-serves",
                    status=ERROR if verdict else REPORT_ONLY,
                    message=headline
                    + (
                        "Any existing manifest.deployable was retracted. "
                        if verdict
                        else ""
                    )
                    + f"{boot_detail}\nContainer log tail:\n"
                    + logs,
                    details={
                        "exited": exited,
                        "answered": answered,
                        "platform_failure": platform_failure,
                    },
                )
            )
            return False if verdict else None

        # --- the probe contract --------------------------------------------
        results: dict[str, int] = {}
        for path in paths:
            status, _body = _probe(base + path)
            results[path] = status

        list_apps = paths[0]
        card_paths = [p for p in paths if "well-known" in p]
        ok = results.get(list_apps) == 200

        if not ok:
            report.add(
                Check(
                    id="container-serves",
                    status=ERROR,
                    message=(
                        f"{list_apps} returned {results.get(list_apps)}, not "
                        "200. The container runs but does not serve the "
                        "recipe. No deployable flag was set on the strength "
                        "of this build, and any pre-existing one was "
                        "retracted."
                    ),
                    details={"probes": results},
                )
            )
            return False

        report.add(
            Check(
                id="container-serves",
                status=CLEAN,
                message=(
                    f"Container serves: {list_apps} -> 200. Verified against "
                    "a real image, not inferred."
                ),
                details={"probes": results},
            )
        )

        # A2A is reported separately and never rounded up: a 404 here means
        # the wiring did not take effect, which is a real finding about the
        # recipe rather than a failure of the image.
        for card in card_paths:
            code = results.get(card)
            if code == 200:
                report.add(
                    Check(
                        id="container-a2a",
                        status=CLEAN,
                        message=f"A2A agent card served: {card} -> 200.",
                    )
                )
            else:
                report.add(
                    Check(
                        id="container-a2a",
                        status=REPORT_ONLY,
                        message=(
                            f"A2A agent card {card} returned {code}, not 200. "
                            "The A2A wiring did NOT take effect. Do not "
                            "describe this recipe as A2A-capable. The image "
                            "still builds and serves."
                        ),
                        details={"probes": results},
                    )
                )
        return True
    finally:
        _cleanup(name, tag)


def _await_ready(url: str, name: str, timeout: int) -> tuple[bool, str, bool]:
    """Poll until the app answers. Returns (ready, detail, exited).

    The early abort matters: a container that crashes on import would
    otherwise hold the run hostage for the whole timeout before reporting
    something we already knew within a second.

    `exited` separates a verdict from a non-verdict. A container that EXITED
    is proven broken. One still running that has not answered in time may
    simply be on a loaded machine, and treating that as proof would retract a
    contributor's deployable flag because our host was busy.
    """
    deadline = time.monotonic() + timeout
    answered_at_all = False
    last_status = 0
    while time.monotonic() < deadline:
        status, _ = _probe(url, timeout=5)
        if status == 200:
            return True, "", False
        if status:
            # An HTTP status — any status — means the server is up and
            # answering. That is evidence about the APP, not about the host.
            answered_at_all = True
            last_status = status
        alive = _docker(
            ["inspect", "-f", "{{.State.Running}}", name], timeout=30
        )
        if alive.stdout.strip() == "false":
            return False, "The container exited before it began serving.", True
        time.sleep(2)

    if answered_at_all:
        # Serving, but not serving THIS. A bespoke entrypoint left in place
        # without --overwrite routinely 404s /list-apps. Calling that
        # "inconclusive" would let a recipe that demonstrably does not serve
        # the expected API keep a `deployable: true`.
        #
        # Reported as a verdict WITHOUT claiming the container exited: it did
        # not, and saying so sends the owner hunting for a crash in a healthy
        # process.
        return (
            False,
            f"The server answered but never returned 200 (last status "
            f"{last_status}), so it runs without serving this endpoint.",
            False,
        )
    return (
        False,
        f"No HTTP response at all within {timeout}s and the container is "
        "still running — inconclusive rather than proof of a defect, since a "
        "loaded host looks identical.",
        False,
    )


def _cleanup(name: str, tag: str) -> None:
    """Remove the container and the image this check created.

    Best-effort: a cleanup failure must never turn a passing verification
    into a reported error.
    """
    _docker(["rm", "-f", name], timeout=120)
    _docker(["rmi", "-f", tag], timeout=120)


def _parse_host_port(text: str) -> str | None:
    """Pull the host port out of `docker port` output.

    Docker prints one mapping per line and may publish both families:

        0.0.0.0:54321
        [::]:54321

    We bind to 127.0.0.1 and probe over IPv4, so prefer an IPv4 line and fall
    back to any line with a trailing port. Taking the first line blindly would
    pick the IPv6 mapping on a dual-stack host and, where the two families get
    different host ports, probe a port nothing is listening on — reported as
    "container never served" for a container that is serving perfectly.
    """
    lines = [
        ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()
    ]
    ipv4 = [ln for ln in lines if re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", ln)]
    for candidate in (*ipv4, *lines):
        match = re.search(r":(\d+)$", candidate)
        if match:
            return match.group(1)
    return None


def _container_logs(name: str, lines: int = 30) -> str:
    """Both streams of a container's log tail, in one call.

    uvicorn writes startup to stderr and access lines to stdout, so a failure
    diagnosis needs both or it reads as an empty log.
    """
    proc = _docker(["logs", "--tail", str(lines), name], timeout=60)
    return _tail((proc.stdout or "") + (proc.stderr or ""), lines)


def _tail(text: str, lines: int) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])


def failure_is_environmental(proc: subprocess.CompletedProcess[str]) -> bool:
    """Did this fail because of the machine rather than the recipe?

    The distinction decides whether we have a VERDICT or merely an absence of
    one, and that in turn decides whether an existing `deployable: true` is
    retracted. Deleting a contributor's flag because a DNS lookup failed, or
    because a leftover container was still holding the port, is data loss
    caused by our own environment — and it would happen while the report tells
    the owner "re-run before concluding anything".

    A network blip is not evidence about the recipe, so it maps to "no
    verdict" (None), never to "proven broken" (False).
    """
    blob = ((proc.stderr or "") + (proc.stdout or "")).lower()
    return bool(
        re.search(
            # Every pattern is anchored to infrastructure phrasing. A bare
            # `timeout` would be catastrophic: `async-timeout` is a
            # transitive dependency of aiohttp and appears in most lockfiles
            # in this repo, so "Failed to build async-timeout==4.0.3" — a
            # genuine recipe fault — would be excused as environmental, the
            # recipe would keep a false `deployable: true`, and the run would
            # exit 0. That is the original bug arriving from the opposite
            # direction. Match the machine's vocabulary, not a word that
            # happens to appear in package names.
            r"temporary failure in name resolution|could not resolve host"
            r"|could not resolve proxy|network is unreachable"
            r"|connection reset by peer|failed to fetch oauth token"
            r"|i/o timeout|tls handshake timeout|context deadline exceeded"
            r"|timeout exceeded while awaiting headers"
            # uv's own wording for a download that timed out. Caught live:
            # a real rag-agent-search build died on
            # "Failed to download distribution due to network timeout" and was
            # classified a RECIPE fault, retracting the contributor's flag.
            r"|due to network timeout|uv_http_timeout"
            r"|i/o operation failed during extraction"
            r"|failed to download distribution"
            r"|timed out after \d+s"
            r"|port is already allocated|address already in use"
            r"|no space left on device|toomanyrequests|rate limit exceeded"
            r"|error pulling image|failed to copy: httpreadseeker"
            # `no such host` is the standard Go DNS failure for registry
            # lookups and the single most common infra failure of all.
            # Anchored to the RUNTIME form. A bare `exec format error` also
            # appears when a recipe ships a script with no shebang — the
            # recipe's bug, not the host's.
            # Only the interpreters OUR generated Dockerfile invokes. A
            # shebang-less script at some other path produces the identical
            # errno and is the RECIPE's bug, so it must stay a verdict. This
            # is reached only from build/`docker run` failures, where the
            # text is docker's own output rather than anything the recipe
            # wrote.
            r"|exec /bin/sh: exec format error"
            r"|exec /usr/local/bin/(uv|python[\d.]*): exec format error"
            r"|failed to create endpoint"
            r"|no such host|error getting credentials"
            r"|cannot connect to the docker daemon"
            r"|error during connect|unexpected eof",
            blob,
        )
    )


def _build_failure_hint(proc: subprocess.CompletedProcess[str]) -> str:
    """Name the likely structural cause instead of only dumping the log.

    Without this a recipe that was never a container candidate reports as
    "your recipe is broken", when the truth is usually one specific missing
    precondition. Cheap triage until a real compatibility preflight exists.
    """
    # Match ONLY against lines that carry a diagnostic, never the whole log.
    # BuildKit echoes each Dockerfile instruction as it runs, so `COPY
    # ./README.md` and `RUN uv sync --frozen` appear in the output of every
    # build — including ones that failed for an unrelated reason like a
    # network timeout. Substring-matching the full blob therefore reports a
    # confident, specific and wrong cause, which is worse than saying nothing:
    # it sends the owner to fix a file that was never the problem.
    blob = (proc.stderr or "") + (proc.stdout or "")
    # Drop a line only when it is PURELY an echoed instruction. BuildKit
    # reprints every step as it runs, so a naive substring match over the whole
    # log reports `RUN uv sync --frozen` as a lockfile fault even when the real
    # error was a DNS timeout — confident, specific and wrong.
    #
    # But the filter must not be greedy either: `=> ERROR [5/5] RUN ...` and
    # `COPY failed: ... README.md ...` both BEGIN like echoes while actually
    # carrying the diagnosis. So a line survives if it contains any error
    # token, and is dropped only if, once decoration is stripped, nothing is
    # left but the instruction itself.
    decoration = re.compile(
        r"^[\s=>|#\d-]*(\[[\d/\s]+\]\s*)?(Step\s+\d+/\d+\s*:\s*)?"
    )
    instruction = re.compile(
        r"^(RUN|COPY|FROM|WORKDIR|CMD|ENV|ARG|EXPOSE|ADD|ENTRYPOINT)\s", re.I
    )
    error_token = re.compile(
        r"\berror\b|\bfailed\b|cannot|unable|denied|not found|non-zero", re.I
    )

    def is_pure_echo(line: str) -> bool:
        if error_token.search(line):
            return False
        return bool(instruction.match(decoration.sub("", line)))

    diagnostics = "\n".join(
        ln for ln in blob.splitlines() if not is_pure_echo(ln)
    ).lower()

    # Environmental first. `=> ERROR [5/5] RUN uv sync --frozen` legitimately
    # survives the echo filter (it carries the ERROR token), so the string
    # `--frozen` is present in the log of a build that died of a DNS timeout.
    # Testing the lockfile pattern first would blame the lockfile every time.
    if failure_is_environmental(proc):
        return (
            "Looks like a network/registry/host failure rather than a defect "
            "in the recipe. Nothing was concluded about the recipe and no "
            "manifest flag was changed. Re-run before drawing conclusions."
        )
    if re.search(
        r"lock.*(out of date|needs to be updated)"
        r"|frozen.*(out of date|mismatch)"
        r"|the lockfile.*(is not|does not)",
        diagnostics,
    ):
        return (
            "uv.lock does not match pyproject.toml — run `uv lock` and "
            "re-verify. This is a lockfile problem, not a template problem."
        )
    if "readme" in diagnostics:
        return (
            "The build backend reads `readme` from pyproject.toml and the "
            "file is missing from the build context. The Dockerfile's "
            "`COPY ./README.md` is functional, not decorative."
        )
    if re.search(r"hatch|editable|build backend|build-backend", diagnostics):
        return (
            "Build-backend/packaging failure. Check that the agent package "
            "is declared to the backend the recipe actually uses "
            "(hatchling's wheel packages, or uv_build's module settings)."
        )
    return "No known structural cause matched; read the log tail above."


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    *,
    recipe_dir: Path,
    apply: bool,
    overwrite: bool,
    data_dirs: list[str],
    region: str,
    verify_container_requested: bool = False,
    report: Report | None = None,
) -> Report:
    # The caller may supply the Report so that a crash mid-run does not lose
    # what already happened. Reporting `blocked` with `files_written: []`
    # after six files landed on disk tells the owner nothing was written when
    # their working tree says otherwise.
    if report is None:
        report = Report(
            recipe_dir=str(recipe_dir), mode="apply" if apply else "dry-run"
        )

    repo_root = find_repo_root(recipe_dir)
    if repo_root is None:
        report.add(
            Check(
                id="policy",
                status=ERROR,
                message="Could not locate .github/policy.yml above the recipe.",
            )
        )
        return report
    policy = load_policy(repo_root)

    templates_dir = (
        Path(__file__).resolve().parent.parent / "resources" / "templates"
    )

    # --- agent package -----------------------------------------------------
    found = find_agent_package(recipe_dir)
    if found is None:
        report.add(
            Check(
                id="agent-package",
                status=ERROR,
                message=(
                    "No agent.py found. This does not look like a Python ADK "
                    "recipe, or the agent lives somewhere unexpected."
                ),
            )
        )
        return report
    agent_py, package_dir = found
    report.agent_package = package_dir.name
    report.add(
        Check(
            id="agent-package",
            status=CLEAN,
            message=f"Agent package is `{package_dir.name}` ({agent_py.relative_to(recipe_dir)}).",
            details={"agent_file": str(agent_py.relative_to(recipe_dir))},
        )
    )

    # --- pyproject ---------------------------------------------------------
    pyproject_path = recipe_dir / "pyproject.toml"
    if not pyproject_path.is_file():
        report.add(
            Check(
                id="pyproject",
                status=ERROR,
                message="No pyproject.toml — cannot check dependencies or build config.",
            )
        )
        return report
    doc = tomlkit.parse(pyproject_path.read_text(encoding="utf-8"))
    project_tbl = doc.get("project") or {}
    deps = [str(d) for d in project_tbl.get("dependencies", [])]
    project_name = str(project_tbl.get("name") or recipe_dir.name)
    python_version = python_floor_from_requires(
        str(project_tbl.get("requires-python") or "")
    )

    # --- GATE: ADK floor ---------------------------------------------------
    adk_check = check_adk_version_floor(
        deps,
        str(policy["min_google_adk"]),
        bool(policy.get("adk_major_migration_is_manual", True)),
    )
    report.add(adk_check)
    if adk_check.status == NEEDS_INPUT:
        report.outcome = OUTCOME_BLOCKED
        report.note(
            "Stopped before generating anything. Nothing on disk was changed."
        )
        return report

    # --- GATE: what the recipe actually resolves to today ------------------
    declared_adk = next(
        (d for d in deps if _requirement_name(d) == "google-adk"),
        None,
    )
    locked_check = check_adk_locked_version(
        recipe_dir / "uv.lock",
        str(policy["min_google_adk"]),
        declared_spec=declared_adk,
    )
    report.add(locked_check)
    if locked_check.status == NEEDS_INPUT:
        report.outcome = OUTCOME_BLOCKED
        report.note(
            "Stopped before generating anything. Nothing on disk was changed."
        )
        return report

    # --- GATE: legacy app_utils -------------------------------------------
    legacy_check = check_legacy_app_utils(
        package_dir, list(policy.get("legacy_app_utils_files", []))
    )
    report.add(legacy_check)
    if legacy_check.status == NEEDS_INPUT:
        report.outcome = OUTCOME_BLOCKED
        report.note(
            "Stopped before generating anything. Nothing on disk was changed."
        )
        return report

    # --- advisory: recipe already serves by its own arrangement ------------
    report.add(
        check_already_deployable(
            recipe_dir,
            package_dir,
            # Content-based, so phase two recognises phase one's own output
            # instead of mistaking it for a contributor's bespoke entrypoint.
            # BOTH must be ours before the advisory is suppressed. Either
            # file being the contributor's is reason to warn: a bespoke
            # entrypoint means generated app_utils/ is dead code, and a
            # bespoke Dockerfile means we would be describing their build.
            generated_by_us=(
                dockerfile_is_generated(
                    recipe_dir=recipe_dir,
                    templates_dir=templates_dir,
                    package=package_dir.name,
                    project_name=project_name,
                    python_version=python_version,
                    data_dirs=data_dirs,
                )
                and entrypoint_is_generated(
                    package_dir=package_dir,
                    templates_dir=templates_dir,
                    package=package_dir.name,
                    project_name=project_name,
                )
            ),
        )
    )

    # --- GATE (advisory): backing infra ------------------------------------
    infra_check = check_backing_infra(recipe_dir, package_dir)
    report.add(infra_check)
    infra_clean = infra_check.status == CLEAN

    # --- docker availability -----------------------------------------------
    # Reported in EVERY mode, including dry-run, because the calling skill
    # needs to know whether verification is even offerable before it asks the
    # owner. The script never prompts; it states the fact and takes a flag.
    docker_state, docker_detail = detect_docker()
    report.add(check_docker(docker_state, docker_detail))

    # No evidence yet. Set provisionally so an early return still carries a
    # coherent outcome.
    verified: bool | None = None
    report.outcome = outcome_for(infra_clean=infra_clean, verified=None)

    # --- generate ----------------------------------------------------------
    generate_serving_files(
        templates_dir=templates_dir,
        recipe_dir=recipe_dir,
        package_dir=package_dir,
        project_name=project_name,
        python_version=python_version,
        data_dirs=data_dirs,
        apply=apply,
        overwrite=overwrite,
        report=report,
        required_files=[str(f) for f in (policy.get("required_files") or [])],
    )

    # --- patch -------------------------------------------------------------
    deps_check = patch_dependencies(
        doc, list(policy.get("required_dependencies", [])), apply
    )
    report.add(deps_check)
    for kept in deps_check.details.get("kept", []):
        if kept.get("missing_extras"):
            report.todo(
                f"Add the [{','.join(kept['missing_extras'])}] extra(s) to "
                f"`{kept['recipe_has']}` — the generated serving code imports "
                "what they install. Left alone here because widening an "
                "existing requirement changes resolution for the whole recipe."
            )
    report.add(patch_hatch_packages(doc, package_dir.name, apply))
    if apply:
        pyproject_path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    app_check = patch_app_object(agent_py, package_dir.name, apply)
    report.add(app_check)
    # The generated entrypoint does `from <pkg>.agent import app`. If that
    # symbol could not be established the container cannot start, so the
    # recipe is not deployable however clean everything else looks — the same
    # standard applied to the ADK and legacy-app_utils gates.
    app_object_ok = app_check.status not in (NEEDS_INPUT, ERROR)

    # --- verify -------------------------------------------------------------
    # Runs before the manifest flag is written, because the flag is what it
    # gates. See "CONTAINER VERIFICATION, AND THE ORDERING IT FORCES".
    defer_manifest = False
    if verify_container_requested:
        if docker_state != DOCKER_USABLE:
            report.add(
                Check(
                    id="container-verify",
                    status=REPORT_ONLY,
                    message=(
                        "Verification was requested but docker is "
                        f"{docker_state} — skipped, not failed. The recipe "
                        "keeps its static assessment and the outcome stays "
                        "`-unverified`."
                    ),
                    details={"docker_state": docker_state},
                )
            )
        elif not apply:
            report.add(
                Check(
                    id="container-verify",
                    status=REPORT_ONLY,
                    message=(
                        "Verification needs the generated files on disk, so "
                        "it only runs with --apply. Nothing was built."
                    ),
                )
            )
        else:
            current, lock_detail = lockfile_is_current(recipe_dir)
            if not current:
                # Phase one. The files are now written but the lockfile they
                # need does not exist yet, so building would fail for a
                # reason that has nothing to do with the template.
                defer_manifest = True
                report.add(
                    Check(
                        id="container-verify",
                        status=NEEDS_INPUT,
                        message=(
                            "Deferred manifest.deployable pending "
                            f"verification: {lock_detail} The Dockerfile runs "
                            "`uv sync --frozen`, which cannot succeed until "
                            "the lockfile matches. Run `uv lock --python "
                            "3.11` in the recipe, then re-invoke this script "
                            "with the same flags to build and probe. The flag "
                            "is written only once verification passes."
                            + (
                                " NOTE: this recipe is ALSO disqualified on "
                                "static grounds (see above), so a "
                                "pre-existing flag was RETRACTED rather than "
                                "merely held back — re-locking alone will not "
                                "restore it."
                                if (not app_object_ok or _has_error(report))
                                else ""
                            )
                        ),
                        details={
                            "phase": "awaiting-lock",
                            "also_disqualified": (
                                not app_object_ok or _has_error(report)
                            ),
                        },
                    )
                )
            else:
                settings = dict(policy.get("verification") or {})
                allowlist = [
                    str(p) for p in (settings.get("run_allowlist") or [])
                ]
                allowlisted = any(
                    str(recipe_dir)
                    .replace("\\", "/")
                    .rstrip("/")
                    .endswith(entry.rstrip("/"))
                    for entry in allowlist
                )
                # Is the Dockerfile about to be built one WE wrote? An
                # existing one is left untouched without --overwrite, and
                # `docker build` executes every RUN in whatever it is given.
                # Building a bespoke contributor Dockerfile is arbitrary code
                # execution, so it needs the same allowlist the run step has —
                # while our own generated file (pip install uv; uv sync) does
                # not. Decided on CONTENT, not on whether this invocation
                # wrote it: see dockerfile_is_generated.
                dockerfile_is_ours = dockerfile_is_generated(
                    recipe_dir=recipe_dir,
                    templates_dir=templates_dir,
                    package=package_dir.name,
                    project_name=project_name,
                    python_version=python_version,
                    data_dirs=data_dirs,
                )
                if not dockerfile_is_ours and not allowlisted:
                    report.add(
                        Check(
                            id="container-verify",
                            status=REPORT_ONLY,
                            message=(
                                "Skipped: the Dockerfile in this recipe is "
                                "not the one this skill generated (an "
                                "existing one is never replaced without "
                                "--overwrite), and the recipe is not on "
                                "`run_allowlist`. Building it would execute "
                                "its RUN instructions, which this skill will "
                                "not do unattended for a file it did not "
                                "write. Outcome stays `-unverified`."
                            ),
                        )
                    )
                else:
                    verified = verify_container(
                        recipe_dir=recipe_dir,
                        package=package_dir.name,
                        settings=settings,
                        may_run=allowlisted,
                        report=report,
                        dockerfile_is_ours=dockerfile_is_ours,
                    )

    report.outcome = outcome_for(
        infra_clean=infra_clean,
        verified=verified,
        has_error=_has_error(report),
        skill_fault=_has_skill_fault(report),
        disqualified=not app_object_ok,
        deferred=defer_manifest,
        inconclusive=_verification_was_inconclusive(report),
    )

    if defer_manifest and app_object_ok and not _has_error(report):
        report.add(
            Check(
                id="manifest-deployable",
                status=NEEDS_INPUT,
                message=(
                    "Held back until container verification has a verdict; "
                    "re-invoke after `uv lock`."
                    + (
                        " NOTE: manifest.yaml ALREADY carries a `deployable` "
                        "flag from before this run. It has been left exactly "
                        "as found — this run neither earned it nor disproved "
                        "it."
                        if _manifest_claims_deployable(
                            recipe_dir / "manifest.yaml"
                        )
                        else " Nothing was written to manifest.yaml."
                    )
                ),
            )
        )
    # Any ERROR recorded anywhere disqualifies the recipe. Enumerating
    # individual disqualifying conditions has repeatedly missed one — the
    # app-object gate and an empty required_files list both reached
    # `deployable: true` while the report carried an ERROR. A blanket rule
    # cannot be outflanked by the next new check.
    elif _has_skill_fault(report):
        report.add(
            Check(
                id="manifest-deployable",
                status=REPORT_ONLY,
                message=(
                    "manifest.yaml was left EXACTLY as found. This run hit a "
                    "fault in the skill itself (see the ERROR above), so it "
                    "learned nothing about the recipe: setting the flag would "
                    "be an unearned claim, and retracting one would destroy a "
                    "contributor's assertion over our bug. Fix the skill and "
                    "re-run."
                ),
            )
        )
    elif verified is False or not app_object_ok or _has_error(report):
        # RETRACTION COMES FIRST, and covers both disqualifying conditions.
        # Ordering the app-object branch ahead of this one meant the MORE
        # broken recipe — no `root_agent` AND a failed container — kept its
        # stale `deployable: true`, because control never reached the
        # retraction. Any condition that disqualifies a recipe must also
        # withdraw a claim already on disk.
        if verified is False:
            reason = "container verification failed"
        elif not app_object_ok:
            reason = (
                "the `app` object could not be established in agent.py, "
                "which the generated entrypoint imports"
            )
        else:
            reason = "the run recorded an ERROR (see the checks above)"
        check = clear_manifest_deployable(recipe_dir / "manifest.yaml", apply)
        check.message = f"{check.message} Cause: {reason}."
        report.add(check)
    else:
        report.add(
            patch_manifest_deployable(
                recipe_dir / "manifest.yaml", infra_clean, apply
            )
        )

    if policy.get("emit_agents_cli_manifest"):
        report.add(
            write_agents_cli_manifest(
                recipe_dir,
                project_name=project_name,
                agent_directory=package_dir.name,
                region=region,
                apply=apply,
                report=report,
            )
        )

    # --- follow-up the owner must do --------------------------------------
    # Which lock command depends on whether the recipe is already pinned below
    # the floor. Recommending a plain `uv lock` there would be recommending a
    # no-op, which is how a recipe ends up shipping an ADK too old for the
    # serving dependencies this script just added.
    #
    # And when this run changed no dependency, the plain form's stated reason
    # — "dependencies changed and the lockfile is now stale" — is false.
    # Emitting it anyway teaches owners to discount the todo list and sends
    # them re-locking for nothing.
    #
    # Staying silent instead would only trade that for a blind spot: a recipe
    # whose dependencies were added by an EARLIER run still has a stale
    # lockfile, and nothing here would say so. So the unchanged case ASKS uv
    # rather than guessing. `uv lock --check` resolves without writing and
    # costs about 80ms, which is cheap enough to buy an accurate answer.
    if locked_check.details.get("remedy"):
        report.todo(
            "Run `uv lock --upgrade-package google-adk --python 3.11` in the "
            f"recipe. A plain `uv lock` will NOT move google-adk off "
            f"{locked_check.details.get('locked')} — uv keeps a locked "
            "version that still satisfies the specifier — and the serving "
            "dependencies need the floor. Confirm the resolved google-adk / "
            "a2a-sdk pair afterwards."
        )
    elif dependencies_changed(deps_check):
        report.todo(
            "Run `uv lock --python 3.11` in the recipe — dependencies changed "
            "and the lockfile is now stale."
        )
    else:
        # `lockfile_is_current` reports False both for a stale lockfile and
        # for a check it could not run, so the todo quotes its reason verbatim
        # instead of restating it as staleness. Re-locking is the right move
        # either way; claiming to know which one it was would not be.
        lock_ok, lock_why = lockfile_is_current(recipe_dir)
        if not lock_ok:
            report.todo(
                f"Run `uv lock --python 3.11` in the recipe — {lock_why}"
            )
    report.todo(
        "Run ruff format + check from the REPO ROOT so the root config wins."
    )
    report.todo(
        "Declare any new environment variables in .env.example (the "
        "extract-python-environment-variables skill does this)."
    )
    if not infra_clean:
        if _manifest_claims_deployable(recipe_dir / "manifest.yaml"):
            report.todo(
                "Provision the backing infrastructure and write its "
                "terraform — this skill cannot. NOTE: manifest.yaml already "
                "claims `deployable: true` from before this run, which this "
                "skill has left alone. Confirm that claim is accurate, "
                "because the recipe needs infrastructure a human provisions."
            )
        else:
            report.todo(
                "Provision the backing infrastructure and write its "
                "terraform — this skill cannot, and manifest.deployable "
                "stays unset until it exists."
            )
    report.note(
        "Generating a2a.py does not prove the recipe supports A2A. These files "
        "assume an agents-cli-scaffolded project; they were copied and "
        "configured, not verified. Exercise the agent over A2A before claiming "
        "support."
    )
    report.note(
        "reasoning_engine_adapter.py is generated but is DEAD CODE in a "
        "cloud_run recipe — settled by verification, not speculation. "
        "Nothing imports it: fast_api_app.py pulls in only app_utils.services "
        "and app_utils.a2a, and its own "
        "`from agentplatform...import AdkApp` would raise ModuleNotFoundError "
        "if anything did, because `agentplatform` is not among the required "
        "dependencies and does not appear in a resolved uv.lock. A verified "
        "container builds and serves without it. It is emitted because the "
        "Recipe Deployability doc lists it unconditionally while agents-cli "
        "ships it only under agent_runtime; whether the doc should change is "
        "a standards decision, not a code one."
    )
    # Recomputed: the manifest patch above can itself record an ERROR, and it
    # runs after the first computation. Leaving the earlier value would report
    # a success outcome beside a check that just failed.
    report.outcome = outcome_for(
        infra_clean=infra_clean,
        verified=verified,
        has_error=_has_error(report),
        skill_fault=_has_skill_fault(report),
        disqualified=not app_object_ok,
        deferred=defer_manifest,
        inconclusive=_verification_was_inconclusive(report),
    )

    if verified is True:
        report.note(
            "Container verification PASSED: the generated Dockerfile was "
            "built and the running image served the recipe. This outcome is "
            "evidence, not inference."
        )
    elif verified is None and not defer_manifest:
        # "We never tried" and "we tried and could not tell" are different
        # facts, and telling someone whose build just died of a DNS failure to
        # "run with --verify-container" is advice to repeat what they did.
        # Only an INCONCLUSIVE attempt, not any attempt. A clean build of a
        # non-allowlisted recipe also produces container-* checks, and telling
        # that owner to "re-run when the environment is healthy" is advice to
        # repeat a run that already succeeded and will never change.
        attempted = any(
            c.id in ("container-build", "container-serves")
            and (
                c.details.get("environmental")
                or c.details.get("platform_failure")
                or c.status == ERROR
                or "INCONCLUSIVE" in c.message
            )
            for c in report.checks
        )
        if attempted:
            report.note(
                "Container verification was ATTEMPTED but reached no verdict "
                "— see the container-* checks for why (typically a network, "
                "registry or host problem rather than the recipe). Nothing "
                "was concluded, so the outcome is "
                "`verification-inconclusive`. Nothing was RETRACTED — an "
                "inconclusive run cannot disprove a recipe — and static "
                "checks may still have earned the flag on their own; see the "
                "manifest-deployable check for what actually happened. Re-run "
                "when the environment is healthy. Do not read this as a pass "
                "or a failure, and do not go looking for another machine: "
                "this one built the image, it just could not finish."
            )
        else:
            report.note(
                "No container evidence was gathered, so the outcome ends "
                "`-unverified`. The files are correct by static inspection; "
                "nobody has yet built the image. Run with --apply "
                "--verify-container on a machine with docker to upgrade this "
                "to a proven result."
            )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate and configure the files a Python recipe needs to be "
            "deployable. With --verify-container it also builds the "
            "generated Dockerfile and probes the running container to prove "
            "the claim; it never deploys or publishes an image."
        )
    )
    parser.add_argument(
        "--recipe-dir",
        required=True,
        type=Path,
        help="Path to the recipe root (e.g. contrib/python/foo).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this the script only reports.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Replace serving files that already exist. Off by default: an "
            "existing fast_api_app.py is usually bespoke."
        ),
    )
    parser.add_argument(
        "--data-dirs",
        default="",
        help=(
            "Comma-separated runtime data directories to COPY into the image "
            "(e.g. 'assets,sample_data'). Confirmed by a human — a missing one "
            "fails at request time, not build time."
        ),
    )
    parser.add_argument(
        "--region",
        default="us-east1",
        help=(
            "GCP region recorded in agents-cli-manifest.yaml. Nothing in a "
            "recipe declares one, so this is a deployment decision the owner "
            "makes; us-east1 matches agents-cli's own default."
        ),
    )
    parser.add_argument(
        "--verify-container",
        action="store_true",
        help=(
            "Build the generated Dockerfile, run it and probe it, then let "
            "the result gate manifest.deployable. Requires --apply and a "
            "current uv.lock; without a reachable docker daemon it skips "
            "cleanly. The skill asks the owner before passing this — the "
            "script itself never prompts."
        ),
    )
    args = parser.parse_args()

    if not args.recipe_dir.is_dir():
        print(
            f"Error: --recipe-dir {args.recipe_dir} is not a directory.",
            file=sys.stderr,
        )
        return 2

    data_dirs = [d.strip() for d in args.data_dirs.split(",") if d.strip()]

    # Any unforeseen exception is surfaced as one ERROR check inside the normal
    # JSON envelope, so the calling agent renders it like any other outcome
    # instead of parsing a stack trace off stderr.
    # Owned here, not inside run(), so a crash keeps every check and every
    # filename already recorded.
    report = Report(
        recipe_dir=str(args.recipe_dir),
        mode="apply" if args.apply else "dry-run",
    )
    try:
        run(
            recipe_dir=args.recipe_dir,
            apply=args.apply,
            overwrite=args.overwrite,
            data_dirs=data_dirs,
            region=args.region,
            verify_container_requested=args.verify_container,
            report=report,
        )
    except Exception as e:  # final safety net for the CLI
        report.add(
            Check(
                id="internal", status=ERROR, message=f"{type(e).__name__}: {e}"
            )
        )
        # A crash must never leave a success outcome in the envelope. run()
        # sets `outcome` partway through, so without this the report would
        # read `deployable-unverified` beside an internal ERROR, and a caller
        # switching on `outcome` would treat the crash as a pass.
        report.outcome = OUTCOME_BLOCKED
        report.note(
            f"CRASHED partway through ({type(e).__name__}). Outcome forced to "
            "`blocked`. "
            + (
                f"{len(report.files_written)} file(s) had already been "
                "written, so the recipe is in a PARTIALLY MODIFIED state — "
                "see files_written, and check pyproject.toml, agent.py and "
                "manifest.yaml before re-running."
                if report.files_written
                else "No files had been written at the point of failure."
            )
        )

    print(report.to_json())

    statuses = {c.status for c in report.checks}
    if ERROR in statuses:
        return 2
    if NEEDS_INPUT in statuses:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
