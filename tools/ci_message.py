#!/usr/bin/env python3
"""
One shared way to tell a contributor what went wrong.

Every user-facing CI failure in this repo is a :class:`Diagnostic`. The
class exists to make one specific failure mode impossible: a message that
says WHAT is wrong and stops there, leaving the contributor to reverse
engineer the rule from `.github/policy.yml`.

A Diagnostic must answer four questions, and the constructor rejects it if
any answer is missing:

    what   The problem, naming the exact file / field / value.
    why    Which rule fired, and why it applies to THIS recipe. Provenance,
           not restatement: "required because manifest.language is 'python'"
           rather than "this file is required".
    how    The concrete fix. A command to run, or the exact text to add.
           May be multi-line; formatting survives to both renderers.
    doc    Anchor into docs/recipe-handbook/troubleshooting.md, so there is
           always somewhere to go for more than one line of context.

The acid test each message must pass: *could a contributor who has never
opened policy.yml fix this in one attempt, without asking anyone?*

Two renderers, one Diagnostic:

    render_human()       Multi-line block for the job log, where there is
                         room to breathe.
    render_annotation()  A single ``::error`` line for the PR Files tab.
                         Newlines are percent-encoded as ``%0A``, which is
                         GitHub's own escape — multi-line `how` blocks stay
                         multi-line in the annotation instead of being
                         flattened into one unusable line.

CONTRIBUTOR FAULT vs CI FAULT
-----------------------------
:class:`Diagnostic` is for things the contributor can fix. When one of our
own checkers crashes — bad TOML we failed to guard, a missing CI dep, a
network blip — use :func:`infra_fault` instead. It annotates the *checker*,
never the contributor's file, and says plainly that the failure is not
theirs. Putting a red marker on someone's `.env.example` because our TOML
parser raised AttributeError is how you teach people to ignore CI.

Import from anywhere
--------------------
Stdlib only, and no imports from other repo modules, so this is safe to
import from `.github/scripts/*` (which run under `uv run --no-project`
with no repo dependencies available) as well as from `tools/*`:

    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
    from ci_message import Diagnostic, Doc
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

# ---------------------------------------------------------------------------
# Documentation anchors
#
# Every value is a real heading in docs/recipe-handbook/troubleshooting.md.
# tools/tests/test_ci_message.py asserts that — if you add a member here
# without adding the heading, that test fails rather than shipping a link
# that 404s onto the contributor's screen.
# ---------------------------------------------------------------------------

_TROUBLESHOOTING = "docs/recipe-handbook/troubleshooting.md"


class Doc(StrEnum):
    """Anchors into the troubleshooting handbook."""

    MANIFEST = "manifestyaml-missing-or-invalid"
    OWNERSHIP_PLACEHOLDER = "ownershipteam-or-poc-is-a-placeholder"
    FOLDER_NAME = "directory-name-too-long-or-invalid"
    SIZE_LIMIT = "recipe-exceeds-size-or-file-limit"
    REQUIRED_FILES = "required-file-or-directory-missing"
    PLACEMENT = "recipe-is-in-the-wrong-folder"
    RETIRED_FOLDER = "changes-inside-a-retired-folder"
    GITHUB_DIR_ADMIN = "only-repository-admins-may-modify-files-under-github"

    README_MISSING = "readmemd-is-missing-or-empty"
    README_TODO = "readmemd-contains-todo-placeholders"
    README_SHORT = "readmemd-is-too-short"
    README_SETUP = "readmemd-is-missing-a-setup-section"
    README_RUN = "readmemd-is-missing-a-run-section-or-code-block"

    RUFF_CONFIG = "pyprojecttoml-has-a-local-ruff-configuration"
    RUFF_STANDALONE = "standalone-ruff-config-file"
    PROJECT_NAME = "project-name-doesnt-match-the-required-name"
    PROJECT_DESCRIPTION = "project-description-doesnt-match-manifest"
    REQUIRES_PYTHON = "requires-python-below-311"
    NO_SIBLING_LOCK = "pyprojecttoml-has-no-sibling-uvlock"
    PYPI_INDEX = "missing-tooluvindex-block"

    ENV_VARS = "env-var-missing-from-envexample"

    LOCK_STALE = "uvlock-out-of-sync"
    LOCK_NON_PYPI = "lockfile-references-a-non-pypi-url"
    LOCK_VCS = "vcs-dependency-in-uvlock"
    LOCK_PATH = "local-path-dependency-in-uvlock"
    LOCK_HASH = "missing-package-hash-in-uvlock"

    RUNNABILITY = "runnability-test-missing"
    RUFF_FAILED = "ruff-format-or-check-failed"

    ADK_MAJOR = "core-recipe-is-behind-the-current-adk-major"
    RECIPE_INACTIVE = "recipe-is-marked-inactive"

    DOCKER_BUILD = "dockerfile-build-failed"
    DOCKER_SERVES = "recipe-container-does-not-serve"

    CI_FAULT = "ci-infrastructure-failure"

    @property
    def url(self) -> str:
        """Repo-relative path plus anchor, e.g. ``docs/…md#env-var-…``."""
        return f"{_TROUBLESHOOTING}#{self.value}"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    NOTICE = "notice"


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------


def _clean(value: str, field: str) -> str:
    """Reject the empty-ish values that make a message useless."""
    if not isinstance(value, str):
        raise TypeError(f"Diagnostic.{field} must be a string, got {value!r}")
    stripped = value.strip()
    if not stripped:
        raise ValueError(
            f"Diagnostic.{field} is empty. Every diagnostic must answer "
            f"what/why/how — a message that only says WHAT sends the "
            f"contributor to policy.yml to work out the rest."
        )
    return stripped


@dataclass(frozen=True)
class Diagnostic:
    """One contributor-facing problem, with the fix attached.

    Construct it and hand it to a reporter; do not build ``::error``
    strings by hand. The point of the type is that the four questions are
    answered at the callsite, where the answers are actually known.
    """

    check: str
    what: str
    why: str
    how: str
    doc: Doc
    file: str | None = None
    severity: Severity = Severity.ERROR

    def __post_init__(self) -> None:
        object.__setattr__(self, "check", _clean(self.check, "check"))
        object.__setattr__(self, "what", _clean(self.what, "what"))
        object.__setattr__(self, "why", _clean(self.why, "why"))
        object.__setattr__(self, "how", _clean(self.how, "how"))
        if not isinstance(self.doc, Doc):
            raise TypeError(
                f"Diagnostic.doc must be a Doc member, got {self.doc!r}. "
                f"Add an anchor to Doc (and a heading to {_TROUBLESHOOTING}) "
                f"rather than passing a bare string."
            )

    # -- rendering ---------------------------------------------------------

    def render_human(self, indent: str = "  ") -> str:
        """Multi-line block for the job log."""
        lines = [f"{indent}[{self.check}] {self.what}"]
        lines.append(f"{indent}  Why:  {self.why}")
        how_lines = self.how.splitlines()
        lines.append(f"{indent}  Fix:  {how_lines[0]}")
        lines.extend(f"{indent}        {ln}" for ln in how_lines[1:])
        lines.append(f"{indent}  Docs: {self.doc.url}")
        return "\n".join(lines)

    def render_annotation(self) -> str:
        """One ``::error`` line, with newlines preserved via ``%0A``.

        GitHub decodes ``%0A`` back into a line break when it renders the
        annotation, so a multi-line `how` block survives the trip. Encoding
        the other two reserved characters (``%25``, ``%0D``) matters because
        a literal ``%`` in a message would otherwise corrupt the escape.
        """
        body = (
            f"{self.what}\n"
            f"Why:  {self.why}\n"
            f"Fix:  {self.how}\n"
            f"Docs: {self.doc.url}"
        )
        encoded = (
            body.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        )
        location = f" file={self.file}" if self.file else ""
        return f"::{self.severity.value}{location}::{encoded}"


# ---------------------------------------------------------------------------
# CI faults — our bug, not theirs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InfraFault:
    """A failure in the checker itself, or in the CI environment.

    Deliberately NOT a Diagnostic: it carries no `file`, because pointing
    at a contributor's file for our crash is the specific behaviour this
    type exists to prevent.
    """

    checker: str
    detail: str
    contributor_action: str = (
        "Nothing — re-run the job. If it fails again, this needs a repo "
        "maintainer, not a change to your PR."
    )

    def render_human(self, indent: str = "  ") -> str:
        return "\n".join(
            [
                f"{indent}[ci-fault] {self.checker} failed to run.",
                f"{indent}  This is a problem with the repo's CI tooling, "
                f"NOT with your changes.",
                f"{indent}  Detail: {self.detail}",
                f"{indent}  You:    {self.contributor_action}",
                f"{indent}  Docs:   {Doc.CI_FAULT.url}",
            ]
        )

    def render_annotation(self) -> str:
        """Annotate the checker, never the contributor's file."""
        body = (
            f"CI tooling failure in {self.checker} — this is NOT caused by "
            f"your changes.\n"
            f"Detail: {self.detail}\n"
            f"You: {self.contributor_action}"
        )
        encoded = (
            body.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        )
        return f"::error::{encoded}"


def infra_fault(checker: str, detail: str) -> InfraFault:
    return InfraFault(checker=checker, detail=detail)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

# Exit codes. Kept distinct so a workflow can tell "your recipe is wrong"
# from "our checker broke" and word its own output accordingly.
EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_CI_FAULT = 2


def _rule() -> str:
    width = min(shutil.get_terminal_size((72, 20)).columns, 72)
    return "=" * width


def report(
    diagnostics: list[Diagnostic],
    *,
    header: str,
    passed_message: str,
    next_step: str,
) -> int:
    """Print every diagnostic (human block + annotation) and return an exit
    code.

    One annotation per diagnostic — never a "first error (+N more)"
    collapse. A contributor reading the Files tab should see every problem
    anchored to the file it belongs to, not a teaser for the job log.
    """
    if not diagnostics:
        print(f"[PASS] {passed_message}")
        return EXIT_OK

    print(f"\n[FAIL] {header}\n")
    by_file: dict[str, list[Diagnostic]] = {}
    for diag in diagnostics:
        by_file.setdefault(diag.file or "(repository)", []).append(diag)

    for path, group in by_file.items():
        print(f"  {path}")
        for diag in group:
            print(diag.render_human(indent="    "))
            print("")

    for diag in diagnostics:
        print(diag.render_annotation())

    count = len(diagnostics)
    noun = "problem" if count == 1 else "problems"
    print("")
    print(_rule())
    print(f"  ACTION REQUIRED: {count} {noun} to fix")
    print(_rule())
    print("")
    print(next_step)
    print("")
    print(f"Every fix above is documented in {_TROUBLESHOOTING}")
    print("")
    return EXIT_VIOLATIONS


def report_advisories(advisories: list[Diagnostic], *, header: str) -> None:
    """Print warn-only findings. Returns nothing, on purpose.

    A rule reported here is one the repo wants VISIBLE but is not willing to
    fail a PR over — typically because the existing tree does not satisfy it
    yet, so blocking would wedge every contributor behind a migration they did
    not cause.

    Returning ``None`` rather than an exit code is the whole design: a caller
    physically cannot fold this into its verdict by accident. The day a rule
    graduates to blocking, it moves into :func:`report` and the change is
    visible in the diff instead of hiding in a status code.

    Raises:
        ValueError: if an advisory is ERROR severity. That combination — an
            error the exit code ignores — is the failure mode this channel
            would otherwise introduce: a red annotation on a green run, which
            teaches contributors to disregard annotations.
    """
    if not advisories:
        return

    bad = [d.check for d in advisories if d.severity is Severity.ERROR]
    if bad:
        raise ValueError(
            f"advisories must not be ERROR severity: {sorted(bad)}. Use "
            f"Severity.WARNING or Severity.NOTICE, or report it through "
            f"report() so the exit code reflects it."
        )

    count = len(advisories)
    noun = "notice" if count == 1 else "notices"
    print(f"\n[NOTICE] {header} — {count} non-blocking {noun}\n")
    for diag in advisories:
        print(diag.render_human(indent="    "))
        print("")
    for diag in advisories:
        print(diag.render_annotation())
    print("")
    print("These do NOT fail the check. Fix them when convenient.")
    print("")


def report_infra_fault(fault: InfraFault) -> int:
    """Print a CI fault and return :data:`EXIT_CI_FAULT`."""
    print(f"\n[CI FAULT] {fault.checker} could not complete.\n")
    print(fault.render_human(indent="  "))
    print("")
    print(fault.render_annotation())
    return EXIT_CI_FAULT


def guard(checker: str, run: Callable[[], int]) -> int:
    """Run a checker's entrypoint so a crash becomes a CI fault, not a verdict.

    Every entrypoint in this repo must be wrapped. Without it an unhandled
    exception exits non-zero, and the calling workflow cannot tell that
    apart from :data:`EXIT_VIOLATIONS` — so it prints "ACTION REQUIRED, here
    is how to fix your recipe" underneath a traceback the contributor did
    not cause and cannot act on. That is the exact confusion
    :class:`InfraFault` exists to prevent, and it has to be enforced out
    here because a crash by definition escaped the checker's own handling.

    ``SystemExit`` and ``KeyboardInterrupt`` derive from BaseException, so
    ``argparse --help`` and Ctrl-C pass through untouched.
    """
    try:
        return run()
    except Exception as exc:
        return report_infra_fault(
            infra_fault(checker, f"{type(exc).__name__}: {exc}")
        )
