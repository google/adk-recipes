#!/usr/bin/env python3
"""
Validates manifest.yaml files in recipe directories under core/ and contrib/.

Checks:
  - Every recipe directory has a manifest.yaml file.
  - Every manifest.yaml is valid YAML.
  - Every manifest.yaml conforms to the schema at
    .github/schemas/manifest-schema.json.
  - ownership.team and ownership.poc are not left as placeholder values.

Usage:
  # Validate all recipes (both core/ and contrib/):
  python3 tools/validate_manifest.py
  python3 tools/validate_manifest.py all

  # Validate only core/ or contrib/:
  python3 tools/validate_manifest.py core
  python3 tools/validate_manifest.py contrib

  # Validate a single recipe:
  python3 tools/validate_manifest.py core/rag-agent-search

  Dependencies are managed in pyproject.toml. Run `uv sync` once before using.

Exit codes:
  0 — all manifests present and valid
  1 — one or more manifests missing or invalid
"""

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

import jsonschema
import yaml
from ci_message import (
    EXIT_VIOLATIONS,
    Diagnostic,
    Doc,
    Severity,
    guard,
    report,
)

REPO_ROOT = Path(__file__).parent.parent
SCHEMA_PATH = REPO_ROOT / ".github" / "schemas" / "manifest-schema.json"
MANIFEST_FILENAME = "manifest.yaml"
# Top-level directories that may hold recipes. `skills/` is scaffolded ahead
# of that folder actually existing on disk — _collect_root() prints a
# harmless [SKIP] line when the directory is missing, so listing it here is
# safe today and lets the validation tooling pick up skills the moment they
# land without another code change.
RECIPE_ROOTS = ["core", "contrib", "skills"]

OWNERSHIP_TEAM_PLACEHOLDER = "TODO: Replace with your team name"
OWNERSHIP_POC_PLACEHOLDER = "TODO: Replace with your GitHub user ID"

LANGUAGE_NAMESPACE_DIRS = {"python", "java", "go", "typescript", "kotlin"}

# Every validator's footer opens with these. A contributor who has just
# failed a check needs to know how a recipe is meant to be BUILT; .github/
# policy.yml and the JSON schema answer "what exactly is the rule", which
# is the maintainer's question. Leading with the enforcement artifacts sent
# people to read YAML they had never seen to work out what to type.
AUTHORING_DOCS = (
    "Start here:\n"
    "  docs/recipe-handbook/README.md  how a recipe is put together\n"
    "  docs/recipe-checklist.md        the pre-PR checklist"
)

# Roots whose second path component is ALWAYS a namespace, whatever it is
# called. core/ and contrib/ take an OPTIONAL language namespace, matched by
# name against LANGUAGE_NAMESPACE_DIRS. skills/ takes a MANDATORY vertical
# (retail/, hr/, finance/ …) whose name is free-form, so it can only be
# recognised by position:
#
#     core/<language>/<recipe>   or   core/<recipe>
#     skills/<vertical>/<solution>
#
# The vertical surfaces ownership — it lets a team see its whole surface at
# a glance — so it is part of the layout rather than a value we enumerate.
NAMESPACE_REQUIRED_ROOTS = {"skills"}


def is_namespace_path(parts: list[str]) -> bool:
    """True if these repo-relative path components identify a namespace
    directory — a container of recipes — rather than a recipe itself.

    Depth matters: only the component directly under a recipe root can be a
    namespace, which is what keeps `skills/retail` (a vertical) distinct
    from `skills/retail/store-ops` (a solution).
    """
    if len(parts) != 2:
        return False
    root, name = parts
    if root not in RECIPE_ROOTS:
        return False
    if root in NAMESPACE_REQUIRED_ROOTS:
        return True
    return name in LANGUAGE_NAMESPACE_DIRS


def is_recipe_dir(path: Path) -> bool:
    """A recipe directory is any immediate subdirectory that contains
    more than just a README.md, and is not a language namespace directory."""
    if not path.is_dir() or path.name.startswith("."):
        return False
    # Language namespace dirs (e.g. core/python/) are not recipes themselves;
    # they are containers whose children are the actual recipes.
    if path.name in LANGUAGE_NAMESPACE_DIRS:
        return False
    children = [
        p
        for p in path.iterdir()
        if not p.name.startswith(".") and p.name != "README.md"
    ]
    if not children:
        return False
    # Defence-in-depth: a directory whose non-hidden, non-README children are
    # ALL language namespace dirs (`python/`, `java/`, …) is treated as an
    # organisational container, not a recipe. A real recipe always has at
    # least one file (manifest.yaml, README.md, agent code) at its root, so
    # this exclusion never fires for a valid recipe — it only guards against
    # accidental structures being promoted to recipes.
    if all(p.is_dir() and p.name in LANGUAGE_NAMESPACE_DIRS for p in children):
        return False
    return True


def load_schema() -> dict:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def repo_relative(path: Path, repo_root: Path | None = None) -> str:
    """Repo-relative spelling of `path`, for a diagnostic's `file=`.

    An absolute path is noise on a contributor's screen and GitHub cannot
    anchor an annotation to one. Falls back to the path as given when it
    lies outside the repo — only reachable from tests that point the
    tooling at a scratch tree.
    """
    base = Path(repo_root) if repo_root is not None else REPO_ROOT
    try:
        return str(Path(path).resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Turning jsonschema errors into something a contributor can act on
#
# jsonschema speaks JSONPath and its own vocabulary: "[$.description] 'short'
# is too short" names neither the YAML field nor the threshold it failed.
# The threshold is sitting in the schema we already loaded, so withholding
# it only buys the contributor a second CI round trip to discover it.
# ---------------------------------------------------------------------------


def _field_label(json_path: str) -> str:
    """`$` -> "top level"; `$.ownership.team` -> "ownership.team"."""
    if json_path in ("", "$"):
        return "top level"
    return json_path.removeprefix("$.").removeprefix("$")


def _quote(value: object, limit: int = 60) -> str:
    """Render an offending value short enough to sit inside a message."""
    text = str(value)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return repr(text) if isinstance(value, str) else text


def _did_you_mean(name: str, candidates: list[str]) -> str:
    match = difflib.get_close_matches(name, candidates, n=1, cutoff=0.7)
    return f"Did you mean '{match[0]}'? " if match else ""


def _additional_properties(err) -> tuple[str, str, str]:
    allowed = sorted((err.schema or {}).get("properties") or {})
    instance = err.instance if isinstance(err.instance, dict) else {}
    extras = [k for k in instance if k not in allowed] or ["(unknown)"]
    where = _field_label(err.json_path)
    listed = ", ".join(f"'{k}'" for k in extras)
    what = (
        f"Unrecognised field{'s' if len(extras) > 1 else ''} "
        f"{listed} at the {where} of {MANIFEST_FILENAME}."
    )
    why = (
        f"The manifest schema is closed (additionalProperties: false at "
        f"{err.json_path}), so an unknown key is never ignored — it is "
        f"either a typo or a field that does not exist."
    )
    suggestion = "".join(_did_you_mean(k, allowed) for k in extras)
    how = (
        f"{suggestion}Rename or remove it.\n"
        f"Fields allowed at the {where}: {', '.join(allowed) or '(none)'}"
    )
    return what, why, how


def _bound(err) -> tuple[str, str, str] | None:
    """Threshold-style failures — the ones worth printing the number for."""
    label = _field_label(err.json_path)
    limit = err.validator_value
    kind = err.validator
    if kind in ("minLength", "maxLength"):
        actual = len(err.instance) if isinstance(err.instance, str) else "?"
        direction = "at least" if kind == "minLength" else "at most"
        what = (
            f"Field '{label}' is {actual} characters long; the schema "
            f"requires {direction} {limit}."
        )
    elif kind in ("minItems", "maxItems"):
        actual = len(err.instance) if isinstance(err.instance, list) else "?"
        direction = "at least" if kind == "minItems" else "at most"
        what = (
            f"Field '{label}' has {actual} entries; the schema requires "
            f"{direction} {limit}."
        )
    elif kind in ("minimum", "maximum"):
        direction = "at least" if kind == "minimum" else "at most"
        what = (
            f"Field '{label}' is {_quote(err.instance)}; the schema "
            f"requires {direction} {limit}."
        )
    else:
        return None

    why = (
        f"{SCHEMA_PATH.name} sets {kind}: {limit} on {label}. Current "
        f"value: {_quote(err.instance)}."
    )
    purpose = (err.schema or {}).get("description")
    how = f"Replace the value so it satisfies {kind}: {limit}."
    if purpose:
        how += f"\nWhat this field is for: {purpose}"
    return what, why, how


def _enum(err) -> tuple[str, str, str]:
    label = _field_label(err.json_path)
    allowed = [str(v) for v in (err.validator_value or [])]
    what = (
        f"Field '{label}' is {_quote(err.instance)}, which is not one of "
        f"the values the schema allows."
    )
    why = (
        f"{SCHEMA_PATH.name} restricts {label} to an enum: "
        f"{', '.join(allowed)}."
    )
    suggestion = (
        _did_you_mean(str(err.instance), allowed)
        if isinstance(err.instance, str)
        else ""
    )
    how = f"{suggestion}Set {label} to one of: {', '.join(allowed)}."
    return what, why, how


def _required(err, schema: dict) -> tuple[str, str, str]:
    instance = err.instance if isinstance(err.instance, dict) else {}
    missing = [p for p in (err.validator_value or []) if p not in instance]
    label = _field_label(err.json_path)
    where = "top level" if label == "top level" else f"'{label}' block"
    named = ", ".join(f"'{p}'" for p in missing) or err.message
    verb = "are" if len(missing) > 1 else "is"
    what = (
        f"Required field{'s' if len(missing) > 1 else ''} {named} {verb} "
        f"missing from the {where} of {MANIFEST_FILENAME}."
    )
    why = (
        f"{SCHEMA_PATH.name} lists {', '.join(err.validator_value or [])} "
        f"as required at {err.json_path}."
    )
    props = (err.schema or schema).get("properties") or {}
    lines = [
        f"  {name}: {props[name].get('description', '')[:70]}"
        for name in missing
        if name in props
    ]
    how = "\n".join(["Add the missing field(s):", *lines] if lines else [])
    return (
        what,
        why,
        (how or "Add the missing field(s).")
        + "\nOr run the `generate-manifest` AI skill to write a complete one.",
    )


def _type(err) -> tuple[str, str, str]:
    label = _field_label(err.json_path)
    expected = err.validator_value
    if isinstance(expected, list):
        expected = " or ".join(str(e) for e in expected)
    what = (
        f"Field '{label}' is {_quote(err.instance)}; the schema requires "
        f"a {expected}."
    )
    why = f"{SCHEMA_PATH.name} declares {label} as type: {expected}."
    how = f"Change the value of {label} to a {expected}."
    return what, why, how


def _schema_diagnostic(err, schema: dict, file: str) -> Diagnostic:
    """One jsonschema error, translated out of JSONPath and into YAML."""
    handlers = {
        "additionalProperties": lambda: _additional_properties(err),
        "enum": lambda: _enum(err),
        "required": lambda: _required(err, schema),
        "type": lambda: _type(err),
    }
    parts = _bound(err)
    if parts is None:
        handler = handlers.get(err.validator)
        parts = (
            handler()
            if handler
            else (
                f"Field '{_field_label(err.json_path)}' in "
                f"{MANIFEST_FILENAME} is invalid: {err.message}",
                f"{SCHEMA_PATH.name} applies the '{err.validator}' rule to "
                f"{err.json_path}.",
                f"Compare the field against {SCHEMA_PATH.name} and correct "
                f"the value.",
            )
        )
    what, why, how = parts
    return Diagnostic(
        check="manifest-schema",
        what=what,
        why=why,
        how=how,
        doc=Doc.MANIFEST,
        file=file,
    )


def validate_manifest(manifest_path: Path, schema: dict) -> list[Diagnostic]:
    """Returns a list of diagnostics. Empty list means valid."""
    file = repo_relative(manifest_path)
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        # The parser's own report is multi-line and carries the line and
        # column. That detail used to be truncated at the first newline on
        # its way into an annotation; the Diagnostic renderer percent-
        # encodes newlines, so it survives to the Files tab intact.
        # PyYAML names the stream by the absolute path it was opened with,
        # which on a CI runner is a temp path the reader does not
        # recognise — swap it for the repo-relative one.
        detail = str(e).replace(str(manifest_path), file)
        return [
            Diagnostic(
                check="manifest-yaml",
                what=f"{MANIFEST_FILENAME} is not valid YAML.",
                why=(
                    "Nothing else can be checked until the file parses — "
                    "schema validation, language detection and the "
                    "required-file rules all read the parsed document."
                ),
                how=(
                    f"Fix the syntax the parser reports below, then re-run "
                    f"`uv run validate manifest`.\n"
                    f"YAML parse error: {detail}"
                ),
                doc=Doc.MANIFEST,
                file=file,
            )
        ]

    if data is None:
        # yaml.safe_load returns None for a zero-byte file AND for one that
        # holds nothing but comments — the second is the confusing case,
        # because the author can see text in their editor.
        required = ", ".join(schema.get("required") or [])
        return [
            Diagnostic(
                check="manifest-empty",
                what=(
                    f"{MANIFEST_FILENAME} has no content — it is either "
                    f"empty or contains only comments."
                ),
                why=(
                    "A manifest is how the tooling learns the recipe's "
                    "language, type and owner; a commented-out manifest "
                    "declares none of them."
                ),
                how=(
                    f"Run the `generate-manifest` AI skill, or write the "
                    f"required top-level fields by hand: {required}."
                ),
                doc=Doc.MANIFEST,
                file=file,
            )
        ]

    validator = jsonschema.Draft7Validator(schema)
    diagnostics = [
        _schema_diagnostic(err, schema, file)
        for err in sorted(validator.iter_errors(data), key=str)
    ]

    # Placeholder values satisfy the schema but mean nobody has claimed the
    # recipe, so they are checked separately from it.
    if isinstance(data, dict):
        ownership = data.get("ownership")
        if isinstance(ownership, dict):
            for field, placeholder, fix in (
                (
                    "team",
                    OWNERSHIP_TEAM_PLACEHOLDER,
                    "the name of the team that owns this recipe",
                ),
                (
                    "poc",
                    OWNERSHIP_POC_PLACEHOLDER,
                    "the GitHub user ID of the person accountable for it",
                ),
            ):
                if ownership.get(field) != placeholder:
                    continue
                diagnostics.append(
                    Diagnostic(
                        check="ownership-placeholder",
                        what=(
                            f"ownership.{field} is still the scaffold "
                            f'placeholder "{placeholder}".'
                        ),
                        why=(
                            "Ownership is how a question about this recipe "
                            "reaches someone who can answer it; a "
                            "placeholder routes it nowhere."
                        ),
                        how=f"Set ownership.{field} to {fix}.",
                        doc=Doc.OWNERSHIP_PLACEHOLDER,
                        file=file,
                    )
                )

        # A "TODO ..." description is long enough to satisfy the schema's
        # minLength, so it would otherwise slip through. A prefix match
        # (rather than an exact string) keeps this robust to wording changes
        # and catches any hand-written "TODO ..." description too.
        description = data.get("description")
        if isinstance(
            description, str
        ) and description.strip().upper().startswith("TODO"):
            diagnostics.append(
                Diagnostic(
                    check="description-placeholder",
                    what=(
                        "manifest.description is still a TODO placeholder: "
                        f"{_quote(description)}."
                    ),
                    why=(
                        "The description is what the recipe catalogue shows "
                        "to someone deciding whether to use this recipe."
                    ),
                    how=(
                        "Replace it with one or two sentences saying what "
                        "the recipe demonstrates and what it is good for."
                    ),
                    doc=Doc.MANIFEST,
                    file=file,
                )
            )

    return diagnostics


def _collect_scoped_path(scope: str) -> list[Path]:
    """Resolve a scope that points at a specific path (not a bare root).

    Handles a language namespace dir (recurse one level) or a single recipe
    directory. Returns [] for anything it cannot resolve rather than
    calling sys.exit: killing the process from inside a collector meant the
    contributor got a bare "[ERROR] Not a valid recipe directory:
    /Users/…/core/foo" with no statement of what a recipe directory IS.
    empty_scope_diagnostic answers that, and can only do so if it is
    reached.
    """
    target = REPO_ROOT / scope
    if not target.exists():
        return []
    # Namespace directory (e.g. core/python, skills/retail) — recurse one
    # level. Matched on the scope's own components rather than just the
    # basename, so `skills/retail` is a namespace while the solution beneath
    # it, `skills/retail/store-ops`, is not.
    if is_namespace_path(scope.strip("/").split("/")):
        return sorted(c for c in target.iterdir() if is_recipe_dir(c))
    if not is_recipe_dir(target):
        return []
    return [target]


def _collect_root(root_name: str) -> list[Path]:
    """Return the recipe directories directly under a top-level root.

    Recognised layouts:
        <root>/<recipe>              — flat (core/, contrib/)
        <root>/<language>/<recipe>   — language-namespaced (core/, contrib/)
        skills/<vertical>/<solution> — vertical-namespaced (skills/)

    Under a NAMESPACE_REQUIRED_ROOTS root every child is a namespace, so a
    solution placed directly at `skills/<solution>` is not collected here.
    That misplacement is reported by tools/validate_placement.py rather
    than silently validated at the wrong depth.
    """
    root_path = REPO_ROOT / root_name
    if not root_path.exists():
        print(f"[SKIP] '{root_name}/' does not exist.")
        return []

    recipe_dirs: list[Path] = []
    for p in sorted(root_path.iterdir()):
        if not p.is_dir():
            continue
        if is_namespace_path([root_name, p.name]):
            # <root>/<language>/<recipe> or skills/<vertical>/<solution>
            recipe_dirs.extend(
                sorted(c for c in p.iterdir() if is_recipe_dir(c))
            )
        elif is_recipe_dir(p):
            # <root>/<recipe> (flat)
            recipe_dirs.append(p)

    if not recipe_dirs:
        print(f"[INFO] No recipe directories found under '{root_name}/'.")
    return recipe_dirs


def collect_recipe_dirs(scope: str | None) -> list[Path]:
    """Return the list of recipe directories to validate.

    scope can be:
      None / "all"              — all roots in RECIPE_ROOTS
      "core" / "contrib"        — a single top-level root
      "core/python"             — a language namespace dir (recurse one level)
      "core/some-recipe"        — a single flat recipe directory
      "core/python/some-recipe" — a single namespaced recipe directory
    """
    if scope is None or scope == "all":
        roots_to_scan = RECIPE_ROOTS
    elif scope in RECIPE_ROOTS:
        roots_to_scan = [scope]
    else:
        return _collect_scoped_path(scope)

    dirs: list[Path] = []
    for root_name in roots_to_scan:
        dirs.extend(_collect_root(root_name))
    return dirs


def empty_scope_diagnostic(
    scope: str | None, recipe_dirs: list
) -> Diagnostic | None:
    """Diagnostic for an EXPLICIT scope that matched no recipes, else None.

    Without this, `uv run validate structure skills` prints
    "[PASS] All 0 recipe(s) passed structural checks." and exits 0 — a
    green check for a run that validated nothing. A typo'd scope does the
    same. Both are far likelier to be a mistake than a deliberate request
    to check nothing, and a silent pass is the worst possible answer to
    either.

    An unscoped (or "all") run is exempt: scanning a root that is
    legitimately empty — `skills/` before the first vertical skill lands —
    is normal, and `_collect_root` already prints an [INFO] for it.
    """
    if recipe_dirs or scope is None or scope == "all":
        return None

    rel = scope.strip("/")
    target = REPO_ROOT / rel
    shape = (
        f"A scope names a root ({', '.join(RECIPE_ROOTS)}), a namespace "
        f"inside one (core/python, skills/retail), or one recipe directory."
    )
    drop_the_scope = (
        "Check the path for a typo, or drop the scope to run against the "
        "whole tree:\n  uv run validate"
    )

    if not target.exists():
        what = f"Scope '{rel}' does not exist."
        why = f"Nothing was validated. {shape}"
        how = drop_the_scope
    elif not target.is_dir():
        what = f"Scope '{rel}' is a file, not a directory."
        why = f"Nothing was validated. {shape}"
        how = drop_the_scope
    elif rel in RECIPE_ROOTS or is_namespace_path(rel.split("/")):
        what = f"'{rel}/' holds no recipe directories."
        why = (
            f"'{rel}/' is a container, and every child of it was either "
            f"empty or not a recipe. Nothing was validated."
        )
        how = drop_the_scope
    elif not is_recipe_dir(target):
        what = (
            f"'{rel}' is not a recipe directory — it contains no "
            f"{MANIFEST_FILENAME}."
        )
        why = (
            f"{MANIFEST_FILENAME} is what makes a directory a recipe: it "
            f"declares the language, type and owner that every other check "
            f"reads. Without it the directory is a container."
        )
        how = (
            f"If '{rel}' is meant to be a recipe, add {MANIFEST_FILENAME} — "
            f"run the `generate-manifest` AI skill.\n"
            f"If it is a container, scope to a recipe inside it instead."
        )
    else:
        what = f"Scope '{rel}' matched no recipe directories."
        why = f"Nothing was validated. {shape}"
        how = drop_the_scope

    return Diagnostic(
        check="scope",
        what=what,
        why=why,
        how=how,
        doc=Doc.MANIFEST,
    )


def report_empty_scope(scope: str | None, recipe_dirs: list) -> bool:
    """Report the empty-scope problem if it applies. True means 'stop'."""
    diagnostic = empty_scope_diagnostic(scope, recipe_dirs)
    if diagnostic is None:
        return False
    report(
        [diagnostic],
        header=f"Scope '{scope}' validated nothing.",
        passed_message="",
        next_step=f"{AUTHORING_DOCS}\n\nUsage:\n  uv run validate --help",
    )
    return True


def missing_manifest_diagnostic(manifest_path: Path) -> Diagnostic:
    """The one check every other check depends on.

    Shared with validate_structure so both tools word it identically —
    a contributor who sees it twice in one CI run should not have to work
    out whether they are two different problems.
    """
    return Diagnostic(
        check="manifest-missing",
        what=f"{MANIFEST_FILENAME} is missing.",
        why=(
            "Every recipe is required to ship one: it declares the "
            "language, type, status and owner, and the language-specific "
            "required-file rules are resolved from manifest.language. "
            "Without it those checks cannot run at all."
        ),
        how=(
            "Run the `generate-manifest` AI skill, or copy the minimum "
            "example from docs/recipe-handbook/anatomy.md#manifestyaml."
        ),
        doc=Doc.MANIFEST,
        file=repo_relative(manifest_path),
    )


def inactive_recipes(recipe_dirs: list[Path]) -> list[str]:
    """Recipes declaring `status: inactive`, as repo-relative paths.

    Read with a regex rather than the YAML parser because this runs after
    validation and must not raise on a manifest that failed it — a recipe
    with a broken manifest already has a diagnostic and does not need a
    traceback on top.
    """
    found = []
    for recipe_dir in recipe_dirs:
        manifest_path = recipe_dir / MANIFEST_FILENAME
        try:
            text = manifest_path.read_text(encoding="utf-8")
        except OSError:
            continue
        # No `\r` in the trailing class, and none is needed: read_text()
        # opens in text mode, so universal-newline translation has already
        # turned any CRLF into LF before the regex sees it. A test pins that,
        # because it looks like a bug and invites the same "fix" twice.
        #
        # IGNORECASE is deliberate even though the schema enum is lowercase-
        # only. `status: INACTIVE` fails validation on its own, and matching
        # it here too tells the author both things at once rather than only
        # the schema complaint.
        if re.search(
            r"""^[ \t]*status:[ \t]*["']?inactive["']?[ \t]*(?:\#.*)?$""",
            text,
            re.IGNORECASE | re.MULTILINE,
        ):
            found.append(repo_relative(manifest_path.parent))
    return sorted(found)


def report_inactive(recipe_dirs: list[Path]) -> None:
    """Surface `status: inactive` recipes. Never affects the exit code.

    `status` was a field nothing read: validated for presence and enum, and
    then ignored by every workflow and generator. That made "retired" a
    cosmetic edit, and made a recipe that was fixed but never flipped back to
    `active` indistinguishable from one nobody touched.

    It matters now because inactive is a waypoint to deletion — the recipe
    canary asks for it on the third consecutive monthly run a recipe fails,
    and proposes removing the recipe two runs after that. A recipe sitting
    inactive is on a clock, so anyone running the validator should be told,
    and a PR touching one gets an annotation asking whether it is being
    revived.

    Nothing clears the field automatically. The canary has no write access —
    it asks on the issue and a human applies it, or nobody does — so this
    notice is the only thing that will ever point out a recipe that was
    fixed but never flipped back.
    """
    inactive = inactive_recipes(recipe_dirs)
    if not inactive:
        return

    print("")
    print(f"[NOTICE] {len(inactive)} recipe(s) marked status: inactive")
    for rel in inactive:
        # Routed through Diagnostic rather than printing a `::warning` by
        # hand: the type is what forces a why, a fix and a doc link to exist,
        # and tools/tests/test_ci_message.py enforces that nothing bypasses
        # it. Severity.WARNING keeps it out of the exit code.
        #
        # WARNING under a `[NOTICE]` header is not a mismatch: `[NOTICE]` is
        # the name of the non-blocking channel — see report_advisories() in
        # ci_message.py, which prints the same header for WARNING-severity
        # advisories — while the severity picks the annotation colour. A
        # `::notice` annotation is easy to miss, and a recipe sliding toward
        # deletion should not be.
        notice = Diagnostic(
            check="recipe-inactive",
            severity=Severity.WARNING,
            what=f"{rel} is marked `status: inactive`.",
            why=(
                "Inactive is a waypoint to deletion, not a parking space. "
                "The recipe canary asks for it on the third consecutive "
                "monthly run a recipe fails and proposes removal two runs "
                "later. Nothing resets the field on its own, so a recipe "
                "left inactive after it has been fixed still reads as "
                "abandoned to everyone who looks."
            ),
            how=(
                f"If you are reviving this recipe, set `status: active` in "
                f"{rel}/{MANIFEST_FILENAME} in the same PR. If you are not, "
                f"no action is needed — this does not fail the check."
            ),
            doc=Doc.RECIPE_INACTIVE,
            file=f"{rel}/{MANIFEST_FILENAME}",
        )
        print(notice.render_human(indent="    "))
        print(notice.render_annotation())
    print("")


def main(scope: str | None = None) -> int:
    schema = load_schema()
    recipe_dirs = collect_recipe_dirs(scope)
    if report_empty_scope(scope, recipe_dirs):
        return EXIT_VIOLATIONS

    diagnostics: list[Diagnostic] = []
    for recipe_dir in recipe_dirs:
        manifest_path = recipe_dir / MANIFEST_FILENAME
        if not manifest_path.exists():
            diagnostics.append(missing_manifest_diagnostic(manifest_path))
        else:
            diagnostics.extend(validate_manifest(manifest_path, schema))

    report_inactive(recipe_dirs)

    return report(
        diagnostics,
        header="manifest.yaml problems",
        passed_message=(
            f"All {len(recipe_dirs)} recipe manifest(s) are present and valid."
        ),
        next_step=(
            f"{AUTHORING_DOCS}\n"
            f"\nRe-run locally:\n"
            f"  uv run validate manifest <recipe-path>\n"
            f"\nThe rule itself lives in "
            f".github/schemas/manifest-schema.json."
        ),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Validate recipe manifest.yaml files."
    )
    parser.add_argument(
        "scope",
        nargs="?",
        default=None,
        help=(
            "What to validate: 'all' (default), 'core', 'contrib', "
            "or a path to a single recipe (e.g. core/rag-agent-search)."
        ),
    )
    args = parser.parse_args()
    sys.exit(guard("validate_manifest.py", lambda: main(args.scope)))
