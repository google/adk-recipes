#!/usr/bin/env python3
"""
Validates the structural shell of every recipe under core/ and contrib/
(and, once it lands, skills/) — the checks that are determined by folder
location and the recipe's declared language, NOT by anything language-
specific.

Checks performed, in order, for each recipe:
  1. manifest.yaml presence.
  2. manifest.yaml schema (delegated to validate_manifest).
  3. Folder name matches ^[a-z][a-z-]*$ and length <= policy limit.
  4. Folder size (bytes) and file count vs .github/policy.yml tiers
     (core vs contrib, default vs manifest.large=true), applying
     excluded_paths as a union across every language section.
  5. Required files present — the UNION of policy.required_files.always,
     policy.required_files.by_root[<root>], and
     policy.required_files.by_language[<manifest.language>].
     manifest.yaml itself is NOT listed in policy.required_files; check 1
     is authoritative for it, and duplicating the rule would double-report.
  6. Required directories present — the same three-way union over
     policy.required_dirs. Vertical skills use this for scripts/.
     An empty directory passes.

Name matching for checks 5 and 6 is done in Python rather than by asking
the filesystem, so a case-mismatched file fails identically on macOS and
on Linux CI. Entries listed in policy.case_insensitive_files may be
spelled in any case and report an advisory note instead.

Language detection: manifest.language is the ONLY source of truth. Path
convention (e.g. core/python/foo/) carries no meaning to this checker.
When manifest.yaml is missing, checks 2 and the language-derived slice of
check 5 are skipped for that recipe — the maintainer still sees the
folder-name, size, and always/by_root file-requirement failures in the
same run, so they can fix everything in one shot.

Usage:
  # Validate all recipes (both core/ and contrib/):
  uv run validate structure
  uv run validate structure all

  # Validate only core/ or contrib/:
  uv run validate structure core
  uv run validate structure contrib

  # Validate a single recipe:
  uv run validate structure core/rag-agent-search
  uv run validate structure core/python/some-recipe

Exit codes:
  0 — every checked recipe passed every applicable check
  1 — one or more recipes failed one or more checks
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import validate_manifest as vm
import yaml
from ci_message import EXIT_VIOLATIONS, Diagnostic, Doc, guard, report

REPO_ROOT = Path(__file__).parent.parent
POLICY_PATH = REPO_ROOT / ".github" / "policy.yml"

# Every folder that may hold recipes at the top level. Kept aligned with
# validate_manifest.RECIPE_ROOTS but extensible: adding a new root here
# (e.g. "skills") plus a matching `by_root:` entry in policy.yml is all
# it takes to bring the new root under structural validation.
RECIPE_ROOTS: list[str] = list(vm.RECIPE_ROOTS)

# Reused from the manifest tool so both checkers agree on what counts as
# a recipe directory (see is_recipe_dir there).
LANGUAGE_NAMESPACE_DIRS = vm.LANGUAGE_NAMESPACE_DIRS

# ---------------------------------------------------------------------------
# Regex for legal recipe folder names. Kept in code (not policy.yml)
# because it's shape-based, not a tunable number — same rationale as the
# comment in the recipe_naming section of policy.yml.
# ---------------------------------------------------------------------------
_FOLDER_NAME_RE = re.compile(r"^[a-z][a-z-]*$")


# ===========================================================================
# Policy loading
# ===========================================================================


def load_policy() -> dict:
    """Load .github/policy.yml as a plain dict. Raises on any I/O or YAML
    error — the caller has no useful recovery."""
    with open(POLICY_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _as_key(name: str | list[str]) -> str | tuple[str, ...]:
    return tuple(name) if isinstance(name, list) else name


def _resolve_required(
    policy: dict, section: str, root: str, language: str | None
) -> list[tuple[str | tuple[str, ...], str]]:
    """Union of `always`, `by_root[root]`, and `by_language[language]`
    from the named policy section, each entry paired with the rule that
    contributed it (`always`, `by_root.<root>`, `by_language.<language>`).

    The provenance travels with the entry because it is the answer to the
    only question the contributor actually has: not "is README.md
    required" but "why is it required of MY recipe". Recovering it later
    from the entry name alone is impossible — the same name can arrive
    from more than one rule.

    Missing sections and missing keys degrade silently to an empty list so
    partial policy configs remain usable (e.g. a root with no entries yet).
    """
    config = policy.get(section) or {}
    entries: list[tuple[str | tuple[str, ...], str]] = []
    for name in config.get("always") or []:
        entries.append((_as_key(name), "always"))
    for name in (config.get("by_root") or {}).get(root) or []:
        entries.append((_as_key(name), f"by_root.{root}"))
    if language:
        for name in (config.get("by_language") or {}).get(language) or []:
            entries.append((_as_key(name), f"by_language.{language}"))
    # Preserve order but drop duplicates — an entry could be listed under
    # multiple sources without meaning it's required twice. The FIRST
    # source wins, which keeps the reported rule stable as policy grows.
    seen: set[str | tuple[str, ...]] = set()
    deduped: list[tuple[str | tuple[str, ...], str]] = []
    for name, source in entries:
        if name not in seen:
            seen.add(name)
            deduped.append((name, source))
    return deduped


def required_files_for(
    policy: dict, root: str, language: str | None
) -> list[tuple[str | tuple[str, ...], str]]:
    """(file, rule) pairs every recipe under this root/language must ship."""
    return _resolve_required(policy, "required_files", root, language)


def required_dirs_for(
    policy: dict, root: str, language: str | None
) -> list[tuple[str | tuple[str, ...], str]]:
    """(directory, rule) pairs every recipe under this root/language ships.

    Separate from required_files because the two need different existence
    tests; a `scripts` entry in required_files could never be satisfied by
    a directory.
    """
    return _resolve_required(policy, "required_dirs", root, language)


def _provenance(
    section: str, source: str, root: str, language: str | None
) -> str:
    """Why THIS recipe has to ship the entry, naming the policy key."""
    rule = f"policy.{section}.{source}"
    if source.startswith("by_language."):
        return f"Required because manifest.language is '{language}' ({rule})."
    if source.startswith("by_root."):
        return f"Required for every recipe under {root}/ ({rule})."
    return f"Required for every recipe in the repository ({rule})."


# Concrete fixes, keyed by required entry. A message that says a file is
# missing and stops there leaves the contributor to discover that a
# generator exists at all — which is the difference between a two-minute
# fix and an afternoon of hand-writing a lockfile.
_FILE_REMEDIATION: dict[str, str] = {
    "manifest.yaml": "Run the `generate-manifest` AI skill.",
    ".env.example": (
        "Run the `extract-python-environment-variables` AI skill — it "
        "finds every os.environ/os.getenv read in the recipe and writes "
        "the file for you."
    ),
    "tests/test_runnability.py": (
        "Run the `generate-python-runnability-test` AI skill — it writes "
        "the import-and-assert-root_agent smoke test."
    ),
    "pyproject.toml": (
        "Run the `align-recipe-pyproject` AI skill — it writes a "
        "pyproject.toml that already satisfies the repo's checks."
    ),
    "README.md": (
        "Add a README.md covering three things: what the recipe does, how "
        "to set it up, and the exact command to run it (in a fenced code "
        "block)."
    ),
    "AGENTS.md": (
        "Add an AGENTS.md holding the maintainer-facing context an AI "
        "coding assistant needs: layout, conventions, and how to test."
    ),
    "SKILL.md": (
        "Add a SKILL.md — the conversational installer for a vertical "
        "skill: it runs the interview, writes the design spec, and "
        "performs setup."
    ),
    "EVAL.yaml": (
        "Add an EVAL.yaml holding the eval rubrics that prove the skill "
        "performs — see docs/recipe-handbook/anatomy.md."
    ),
    "go.mod": (
        "Every Go recipe is its own module: run `go mod init` in the "
        "recipe directory."
    ),
    "build.gradle.kts": "Add build.gradle.kts to the recipe.",
    "package.json": "Add package.json to the recipe.",
}


def _file_remediation(rel: str | tuple[str, ...], recipe_rel: str) -> str:
    """The fix for one missing required file."""
    if isinstance(rel, tuple):
        if "pom.xml" in rel:
            return (
                "Add a build configuration file to the recipe: pom.xml (Maven), "
                "build.gradle, or build.gradle.kts (Gradle)."
            )
        if "package-lock.json" in rel:
            return (
                "Add a lockfile (package-lock.json, pnpm-lock.yaml, yarn.lock, "
                "bun.lockb, or bun.lock) to the recipe by running your package "
                "manager's install/lock command."
            )
        return f"Add one of {', '.join(rel)} to the recipe."
    if rel == "uv.lock":
        # Needs the recipe path, so it cannot live in the static table.
        return f"cd {recipe_rel} && uv lock"
    return _FILE_REMEDIATION.get(
        rel,
        (
            f"Add {rel} to the recipe. There is no generator for this one — "
            f"docs/recipe-checklist.md lists what it has to contain."
        ),
    )


def _dir_remediation(rel: str | tuple[str, ...], recipe_rel: str) -> str:
    """The fix for a missing required directory.

    Always leads with the git detail: the overwhelmingly common report is
    "the folder is right there in my editor". It is, locally — git simply
    never committed it, because git tracks files and an empty directory
    has none.
    """
    target = rel[0] if isinstance(rel, tuple) else rel
    return (
        f"git cannot commit an empty directory, so a folder with nothing "
        f"in it never reaches CI. Add a placeholder and commit it:\n"
        f"  touch {recipe_rel}/{target}/.gitkeep && "
        f"git add {recipe_rel}/{target}/.gitkeep\n"
        f"If the directory should have content, add the content instead."
    )


def case_insensitive_entries(policy: dict) -> set[str]:
    """Required entries whose name may be spelled in any case.

    Opt-in per entry, deliberately. Most required files are read by other
    tools that demand an exact name — uv reads `pyproject.toml` and
    `uv.lock`, pytest collects `tests/test_runnability.py`, and our own
    tooling globs `manifest.yaml`. Accepting `PyProject.toml` would pass
    this check and then break uv, which is a worse failure than the one
    the leniency prevents. Only files nothing else parses belong here.
    """
    return {str(e) for e in (policy.get("case_insensitive_files") or [])}


# ===========================================================================
# Root & language detection
# ===========================================================================


def recipe_root_of(recipe_dir: Path) -> str | None:
    """Top-level folder the recipe lives in ('core', 'contrib', …) —
    the first path component relative to REPO_ROOT. Returns None if
    the recipe is somehow outside REPO_ROOT (shouldn't happen in normal
    use but guards against surprising callers)."""
    try:
        rel = recipe_dir.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        return None
    parts = rel.parts
    return parts[0] if parts else None


def detect_language(manifest_path: Path) -> str | None:
    """Return `manifest.language` as a lowercase string, or None if the
    manifest is missing, unreadable, or does not declare a language.
    Does NOT validate the value — schema validation is a separate check."""
    if not manifest_path.is_file():
        return None
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    lang = data.get("language")
    if not isinstance(lang, str):
        return None
    return lang.strip().lower() or None


def is_large_recipe(manifest_path: Path) -> bool:
    """Return True if manifest.large == true. Missing/invalid manifest
    is treated as `large: false` (the stricter default tier applies)."""
    if not manifest_path.is_file():
        return False
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError:
        return False
    return isinstance(data, dict) and data.get("large") is True


# ===========================================================================
# Individual checks (each returns list[Diagnostic]; empty list means pass)
# ===========================================================================


def _suggest_folder_name(name: str) -> str:
    """A legal name close to the one the author tried to use."""
    cleaned = re.sub(r"[^a-z-]+", "-", name.lower()).strip("-")
    return re.sub(r"-{2,}", "-", cleaned) or "my-recipe"


def check_folder_name(recipe_dir: Path, max_length: int) -> list[Diagnostic]:
    """Folder basename must match ^[a-z][a-z-]*$ and stay within the
    length limit. Two independent violations are reported independently
    so the maintainer sees both in one CI run."""
    name = recipe_dir.name
    rel = vm.repo_relative(recipe_dir, REPO_ROOT)
    parent = str(Path(rel).parent)
    diagnostics: list[Diagnostic] = []
    if not _FOLDER_NAME_RE.match(name):
        suggestion = _suggest_folder_name(name)
        diagnostics.append(
            Diagnostic(
                check="folder-name",
                what=f"Folder name '{name}' is invalid.",
                why=(
                    "Recipe folder names must match ^[a-z][a-z-]*$ — "
                    "lowercase letters and hyphens only, starting with a "
                    "letter. The name is used verbatim in URLs, package "
                    "names and shell commands, where underscores, digits "
                    "and capitals all behave differently across platforms."
                ),
                how=f"git mv {rel} {parent}/{suggestion}",
                doc=Doc.FOLDER_NAME,
                file=rel,
            )
        )
    if len(name) > max_length:
        diagnostics.append(
            Diagnostic(
                check="folder-name",
                what=(
                    f"Folder name '{name}' is {len(name)} characters; the "
                    f"maximum is {max_length}."
                ),
                why=(
                    f"policy.recipe_naming.max_folder_name_length is "
                    f"{max_length}, so the path stays readable in CI logs "
                    f"and short enough for every filesystem in play."
                ),
                how=(
                    f"Rename the folder to at most {max_length} characters, "
                    f"e.g. git mv {rel} {parent}/{name[:max_length]}"
                ),
                doc=Doc.FOLDER_NAME,
                file=rel,
            )
        )
    return diagnostics


def _iter_excluded_dirs_files(policy: dict) -> tuple[set[str], set[str]]:
    """Union of excluded_paths.<section>.{dirs,files} across every
    language section. Kept as sets for O(1) membership tests. Values
    are basename patterns (plain names or fnmatch globs)."""
    excluded_dirs: set[str] = set()
    excluded_files: set[str] = set()
    excluded = policy.get("excluded_paths") or {}
    for section in excluded.values():
        if not isinstance(section, dict):
            continue
        for d in section.get("dirs") or []:
            excluded_dirs.add(d)
        for f in section.get("files") or []:
            excluded_files.add(f)
    return excluded_dirs, excluded_files


def _matches_any(name: str, patterns: Iterable[str]) -> bool:
    """True if `name` matches any of the fnmatch patterns. Plain names
    (no glob metachars) still work because fnmatch treats them as
    literal matches."""
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _walk_counted_files(recipe_dir: Path, policy: dict) -> list[Path]:
    """Walk the recipe tree and return every file that COUNTS toward
    the size/count limits — i.e. everything not pruned by
    excluded_paths. Directory pruning is applied per basename, so a
    `build/` anywhere in the tree is excluded (mirrors the current
    bash `find -name X -prune` behaviour)."""
    excluded_dirs, excluded_files = _iter_excluded_dirs_files(policy)
    counted: list[Path] = []
    # Iterative walk so we can prune directories in place — pathlib's
    # rglob offers no prune hook, and os.walk gives us the mutable
    # `dirs` list we need.
    import os

    for dirpath, dirnames, filenames in os.walk(recipe_dir):
        # Prune excluded directories BEFORE descending into them.
        dirnames[:] = [
            d for d in dirnames if not _matches_any(d, excluded_dirs)
        ]
        for fname in filenames:
            if _matches_any(fname, excluded_files):
                continue
            counted.append(Path(dirpath) / fname)
    return counted


def _fmt_bytes(count: int) -> str:
    if count >= 1024 * 1024:
        return f"{count / (1024 * 1024):.1f} MB"
    if count >= 1024:
        return f"{count // 1024} KB"
    return f"{count} B"


def _worst_contributors(
    items: Iterable[tuple[str, int]], overage: int, limit: int = 5
) -> list[tuple[str, int]]:
    """The biggest contributors, cut off once removing them clears the
    overage.

    Always padding the list out to `limit` puts a 60 MB blob next to a
    7-byte README, which makes the culprit harder to spot, not easier.
    The truncated list is also a promise: delete exactly these and the
    check passes.
    """
    picked: list[tuple[str, int]] = []
    for name, weight in sorted(items, key=lambda kv: -kv[1])[:limit]:
        picked.append((name, weight))
        overage -= weight
        if overage <= 0:
            break
    return picked


def _describe_tier(tier_limits: dict | None) -> str:
    if not isinstance(tier_limits, dict):
        return "not defined for this root"
    return (
        f"max_files={tier_limits.get('max_files')}, "
        f"max_size_mb={tier_limits.get('max_size_mb')}"
    )


def check_size_and_count(
    recipe_dir: Path, root: str, policy: dict, manifest_path: Path
) -> list[Diagnostic]:
    """Enforce the size (MB) and file-count tiers from policy.yml.
    Tier is chosen by `root` (core vs contrib) then by manifest.large.
    Recipes under roots not covered by recipe_size_limits (e.g. a
    future 'skills' root without limits) skip these checks."""
    rel = vm.repo_relative(recipe_dir, REPO_ROOT)
    manifest_rel = vm.repo_relative(manifest_path, REPO_ROOT)
    limits_by_root = policy.get("recipe_size_limits") or {}
    root_limits = limits_by_root.get(root)
    if not isinstance(root_limits, dict):
        # No configured size limits for this root — nothing to enforce.
        return []

    is_large = is_large_recipe(manifest_path)
    tier = "large" if is_large else "default"
    tier_limits = root_limits.get(tier)

    # Fail loudly when `large: true` is set but the requested tier is not
    # defined for this root. Previously the code silently fell back to
    # `default` via a boolean chain, so the recipe author got no signal
    # that their opt-in was being ignored.
    if tier_limits is None:
        if tier != "default":
            return [
                Diagnostic(
                    check="size",
                    what=(
                        f"manifest.large is true, but .github/policy.yml "
                        f"defines no 'large' size tier for {root}/."
                    ),
                    why=(
                        f"recipe_size_limits.{root} defines only: "
                        f"{', '.join(sorted(root_limits)) or '(nothing)'}. "
                        f"The opt-in would be silently ignored, so it is "
                        f"reported instead of quietly falling back."
                    ),
                    how=(
                        f"Remove `large: true` from {vm.MANIFEST_FILENAME} "
                        f"and stay within the default tier, or ask a "
                        f"maintainer to add a `{root}.large` block "
                        f"(max_files / max_size_mb) to .github/policy.yml."
                    ),
                    doc=Doc.SIZE_LIMIT,
                    file=manifest_rel,
                )
            ]
        # No `default` tier and no `large` tier requested — nothing to
        # enforce, matches the no-root-limits case.
        return []

    max_files = tier_limits.get("max_files")
    max_size_mb = tier_limits.get("max_size_mb")

    counted = [
        p for p in _walk_counted_files(recipe_dir, policy) if p.is_file()
    ]
    sizes = {p: p.stat().st_size for p in counted}
    total_bytes = sum(sizes.values())
    file_count = len(counted)

    # The walk above already knows every offending path and its size.
    # Reporting only the total made the contributor re-derive that with
    # `du -a | sort -n`, on a tree whose exclusions they cannot see.
    tier_note = (
        f"The {root}/{tier} tier applies: the recipe is under {root}/ and "
        f"its manifest {'sets' if is_large else 'does not set'} "
        f"`large: true`"
    )
    escape_hatch = (
        "This recipe is already on the relaxed `large: true` tier — there "
        "is no higher one, so the content itself has to come down."
        if is_large
        else (
            f"If the recipe genuinely needs the room, set `large: true` in "
            f"{vm.MANIFEST_FILENAME} to opt into the relaxed tier "
            f"({_describe_tier(root_limits.get('large'))})."
        )
    )
    excluded_note = (
        "Paths listed under policy.excluded_paths (.venv/, __pycache__/, "
        "uv.lock, build output …) are not counted."
    )

    diagnostics: list[Diagnostic] = []
    if max_size_mb is not None:
        max_bytes = int(max_size_mb) * 1024 * 1024
        if total_bytes > max_bytes:
            listing = "\n".join(
                f"  {_fmt_bytes(size):>9}  {name}"
                for name, size in _worst_contributors(
                    (
                        (str(path.relative_to(recipe_dir)), size)
                        for path, size in sizes.items()
                    ),
                    total_bytes - max_bytes,
                )
            )
            diagnostics.append(
                Diagnostic(
                    check="size",
                    what=(
                        f"Recipe folder is {_fmt_bytes(total_bytes)}; the "
                        f"limit is {max_size_mb} MB."
                    ),
                    why=(
                        f"{tier_note} "
                        f"(policy.recipe_size_limits.{root}.{tier}"
                        f".max_size_mb = {max_size_mb}). {excluded_note}"
                    ),
                    how=(
                        f"Largest counted files:\n{listing}\n"
                        f"Move data and media out to a storage bucket and "
                        f"link them from README.md, or delete generated "
                        f"output that should not be committed.\n"
                        f"{escape_hatch}"
                    ),
                    doc=Doc.SIZE_LIMIT,
                    file=rel,
                )
            )
    if max_files is not None and file_count > int(max_files):
        # Directories, not individual filenames: when a recipe is 400 files
        # over the limit, five arbitrary paths say nothing, while the
        # directory holding 380 of them says everything.
        per_dir = Counter(
            str(path.parent.relative_to(recipe_dir)) for path in counted
        )
        listing = "\n".join(
            f"  {n:>5} file{'s' if n != 1 else ''}  "
            f"{name if name != '.' else '(recipe root)'}"
            for name, n in _worst_contributors(
                per_dir.items(), file_count - int(max_files)
            )
        )
        diagnostics.append(
            Diagnostic(
                check="size",
                what=(
                    f"Recipe folder contains {file_count} counted files; "
                    f"the limit is {max_files}."
                ),
                why=(
                    f"{tier_note} "
                    f"(policy.recipe_size_limits.{root}.{tier}.max_files = "
                    f"{max_files}). {excluded_note}"
                ),
                how=(
                    f"Directories contributing the most files:\n{listing}\n"
                    f"Delete generated output, or add the directory that "
                    f"holds it to policy.excluded_paths if it is tool "
                    f"output that every recipe produces.\n"
                    f"{escape_hatch}"
                ),
                doc=Doc.SIZE_LIMIT,
                file=rel,
            )
        )
    return diagnostics


def _find_entry(
    recipe_dir: Path, rel: str, *, case_insensitive: bool
) -> tuple[Path | None, bool]:
    """Resolve a recipe-relative path by comparing names in Python.

    Returns (path, exact). `path` is None when nothing matched; `exact`
    reports whether the match used the requested spelling.

    Deliberately does NOT use Path.is_file()/is_dir() to test for
    existence: those delegate to the filesystem, and macOS is normally
    case-insensitive, so `PyProject.toml` would satisfy `pyproject.toml`
    locally and then fail on Linux CI. Comparing names here makes the
    verdict identical on every platform, and makes leniency an explicit
    per-entry decision rather than a property of the contributor's laptop.
    """
    current = recipe_dir
    exact = True
    for segment in Path(rel).parts:
        if not current.is_dir():
            return None, False
        children = sorted(current.iterdir())
        match = next((c for c in children if c.name == segment), None)
        if match is None and case_insensitive:
            lowered = segment.lower()
            match = next(
                (c for c in children if c.name.lower() == lowered), None
            )
            if match is not None:
                exact = False
        if match is None:
            return None, False
        current = match
    return current, exact


def _note_inexact(rel: str, found: Path) -> None:
    print(
        f"[NOTE] Required entry '{rel}' matched as '{found.name}'. "
        f"Accepted, but '{rel}' is the canonical spelling — renaming keeps "
        "the tree consistent for tools that look for the exact name."
    )


def check_required_files(
    recipe_dir: Path, root: str, language: str | None, policy: dict
) -> list[Diagnostic]:
    """Every path in the union list must exist as a file (not a dir).
    Paths are recipe-relative and may include subdirectories
    (e.g. tests/test_runnability.py). If an entry is a tuple of
    alternatives, any ONE of the files is sufficient."""
    recipe_rel = vm.repo_relative(recipe_dir, REPO_ROOT)
    lenient = case_insensitive_entries(policy)
    diagnostics: list[Diagnostic] = []
    for rel, source in required_files_for(policy, root, language):
        why = _provenance("required_files", source, root, language)
        fix = _file_remediation(rel, recipe_rel)
        if isinstance(rel, tuple):
            matched_file = False
            for alt in rel:
                found, exact = _find_entry(
                    recipe_dir, alt, case_insensitive=alt in lenient
                )
                if found is not None and found.is_file():
                    matched_file = True
                    if not exact:
                        _note_inexact(alt, found)
                    break
            if not matched_file:
                alt_str = " OR ".join(rel)
                diagnostics.append(
                    Diagnostic(
                        check="required-files",
                        what=f"Required file '{alt_str}' is missing (any ONE is required).",
                        why=why,
                        how=fix,
                        doc=Doc.REQUIRED_FILES,
                        file=f"{recipe_rel}/{rel[0]}",
                    )
                )
        else:
            found, exact = _find_entry(
                recipe_dir, rel, case_insensitive=rel in lenient
            )
            if found is None:
                diagnostics.append(
                    Diagnostic(
                        check="required-files",
                        what=f"Required file '{rel}' is missing.",
                        why=why,
                        how=fix,
                        doc=Doc.REQUIRED_FILES,
                        file=f"{recipe_rel}/{rel}",
                    )
                )
            elif not found.is_file():
                diagnostics.append(
                    Diagnostic(
                        check="required-files",
                        what=(
                            f"Required file '{rel}' exists but is a "
                            f"{'directory' if found.is_dir() else 'non-file'}."
                        ),
                        why=why,
                        how=(
                            f"Remove or rename {recipe_rel}/{rel}, then add the "
                            f"file.\n{fix}"
                        ),
                        doc=Doc.REQUIRED_FILES,
                        file=f"{recipe_rel}/{rel}",
                    )
                )
            elif not exact:
                _note_inexact(rel, found)
    return diagnostics


def check_required_dirs(
    recipe_dir: Path, root: str, language: str | None, policy: dict
) -> list[Diagnostic]:
    """Every path in the union list must exist as a directory.

    An empty directory passes: some required folders legitimately have
    nothing in them for a given recipe, and git cannot commit an empty
    directory anyway, so in practice they carry a .gitkeep.
    """
    recipe_rel = vm.repo_relative(recipe_dir, REPO_ROOT)
    lenient = case_insensitive_entries(policy)
    diagnostics: list[Diagnostic] = []
    for rel, source in required_dirs_for(policy, root, language):
        why = _provenance("required_dirs", source, root, language)
        if isinstance(rel, tuple):
            matched_dir = False
            for alt in rel:
                found, exact = _find_entry(
                    recipe_dir, alt, case_insensitive=alt in lenient
                )
                if found is not None and found.is_dir():
                    matched_dir = True
                    if not exact:
                        _note_inexact(alt, found)
                    break
            if not matched_dir:
                alt_str = " OR ".join(f"{a}/" for a in rel)
                diagnostics.append(
                    Diagnostic(
                        check="required-dirs",
                        what=f"Required directory '{alt_str}' is missing.",
                        why=why,
                        how=_dir_remediation(rel, recipe_rel),
                        doc=Doc.REQUIRED_FILES,
                        file=f"{recipe_rel}/{rel[0]}",
                    )
                )
        else:
            found, exact = _find_entry(
                recipe_dir, rel, case_insensitive=rel in lenient
            )
            if found is None:
                diagnostics.append(
                    Diagnostic(
                        check="required-dirs",
                        what=f"Required directory '{rel}/' is missing.",
                        why=why,
                        how=_dir_remediation(rel, recipe_rel),
                        doc=Doc.REQUIRED_FILES,
                        file=f"{recipe_rel}/{rel}",
                    )
                )
            elif not found.is_dir():
                # Reporting this as "missing" would send the author hunting
                # for something that is right there under the wrong kind.
                diagnostics.append(
                    Diagnostic(
                        check="required-dirs",
                        what=f"Required directory '{rel}/' exists but is a file.",
                        why=why,
                        how=(
                            f"Rename or delete the file at {recipe_rel}/{rel}, "
                            f"then create a directory in its place:\n"
                            f"  mkdir -p {recipe_rel}/{rel}"
                        ),
                        doc=Doc.REQUIRED_FILES,
                        file=f"{recipe_rel}/{rel}",
                    )
                )
            elif not exact:
                _note_inexact(rel, found)
    return diagnostics


# ===========================================================================
# Per-recipe orchestration
# ===========================================================================


def validate_recipe(
    recipe_dir: Path, policy: dict, schema: dict
) -> list[Diagnostic]:
    """Run every applicable check on one recipe. Returns the list of
    diagnostics (empty list means everything passed)."""
    diagnostics: list[Diagnostic] = []
    rel = vm.repo_relative(recipe_dir, REPO_ROOT)

    root = recipe_root_of(recipe_dir)
    if root is None or root not in RECIPE_ROOTS:
        roots = ", ".join(f"{r}/" for r in RECIPE_ROOTS)
        return [
            Diagnostic(
                check="placement",
                what=f"'{rel}' is not inside a recipe root.",
                why=(
                    f"Recipes are only collected from {roots}, so a "
                    f"directory anywhere else is never validated, indexed "
                    f"or published — it would look fine and reach nobody."
                ),
                how=(
                    "Move it under one of them: core/ for curated recipes, "
                    "contrib/ for community ones, "
                    "skills/<vertical>/<solution> for a vertical skill."
                ),
                doc=Doc.PLACEMENT,
                file=rel,
            )
        ]

    # Check 1: manifest.yaml presence.
    manifest_path = recipe_dir / vm.MANIFEST_FILENAME
    manifest_present = manifest_path.is_file()
    if not manifest_present:
        diagnostics.append(vm.missing_manifest_diagnostic(manifest_path))
        # Continue with the checks that don't need the manifest —
        # folder-name and (best-effort) size — so the maintainer sees
        # every problem in one shot rather than fix-and-retry chains.

    # Check 2: manifest.yaml schema (only if the file exists — a missing
    # manifest is already reported by check 1).
    if manifest_present:
        diagnostics.extend(vm.validate_manifest(manifest_path, schema))

    # Check 3: folder name.
    naming = policy.get("recipe_naming") or {}
    max_length = int(naming.get("max_folder_name_length", 30))
    diagnostics.extend(check_folder_name(recipe_dir, max_length))

    # Check 4: folder size + file count.
    diagnostics.extend(
        check_size_and_count(recipe_dir, root, policy, manifest_path)
    )

    # Check 5: required files (union of always + by_root + by_language).
    # Language detection is intentionally decoupled from schema validation:
    # the schema check above already reports an invalid `language` value;
    # here we simply use whatever the manifest declares (or None) so this
    # check still runs even if the manifest has unrelated schema issues.
    language = detect_language(manifest_path) if manifest_present else None
    diagnostics.extend(check_required_files(recipe_dir, root, language, policy))

    # Check 6: required directories. Same union, different existence test —
    # a `scripts` entry under required_files could never be satisfied.
    diagnostics.extend(check_required_dirs(recipe_dir, root, language, policy))

    return diagnostics


# ===========================================================================
# CLI entry point (mirrors validate_manifest.main)
# ===========================================================================


def main(scope: str | None = None) -> int:
    policy = load_policy()
    schema = vm.load_schema()
    recipe_dirs = vm.collect_recipe_dirs(scope)
    if vm.report_empty_scope(scope, recipe_dirs):
        return EXIT_VIOLATIONS

    diagnostics: list[Diagnostic] = []
    for recipe_dir in recipe_dirs:
        diagnostics.extend(validate_recipe(recipe_dir, policy, schema))

    return report(
        diagnostics,
        header="Recipes with structural problems",
        passed_message=(
            f"All {len(recipe_dirs)} recipe(s) passed structural checks."
        ),
        next_step=(
            f"{vm.AUTHORING_DOCS}\n"
            f"\nRe-run locally:\n"
            f"  uv run validate structure <recipe-path>\n"
            f"\nThe rules themselves live in .github/policy.yml "
            f"(required_files, required_dirs, recipe_size_limits, "
            f"recipe_naming)."
        ),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Validate recipe folder structure."
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
    sys.exit(guard("validate_structure.py", lambda: main(args.scope)))
