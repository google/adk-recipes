#!/usr/bin/env python3
"""Tests for the shared diagnostic helper.

Two jobs here. The first is ordinary unit testing of Diagnostic and
InfraFault. The second is the one that actually keeps message quality from
rotting: `test_every_doc_anchor_exists_in_troubleshooting` walks the `Doc`
enum and fails if any anchor is missing from the handbook, so a link can
never 404 onto a contributor's screen; and the standard tests below assert
that a Diagnostic cannot be constructed without an answer to what/why/how.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ci_message as m

REPO_ROOT = Path(__file__).resolve().parents[2]
TROUBLESHOOTING = REPO_ROOT / "docs" / "recipe-handbook" / "troubleshooting.md"


def _github_anchor(heading: str) -> str:
    """Slugify a Markdown heading the way GitHub does.

    Lowercase, strip anything that is not alphanumeric/space/hyphen, then
    spaces to hyphens. Good enough for the headings this repo actually
    uses; the test that consumes it compares against a set, so a wrong
    slug shows up as a failure rather than a silent pass.
    """
    text = heading.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text).strip("-")


def _anchors_in_troubleshooting() -> set[str]:
    anchors: set[str] = set()
    for line in TROUBLESHOOTING.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            anchors.add(_github_anchor(line.lstrip("#")))
    return anchors


def _valid_diagnostic(**overrides) -> m.Diagnostic:
    kwargs = {
        "check": "required-files",
        "what": "Required file '.env.example' is missing.",
        "why": "Required because manifest.language is 'python'.",
        "how": "Run the extract-python-environment-variables skill.",
        "doc": m.Doc.REQUIRED_FILES,
        "file": "skills/retail/product-search/.env.example",
    }
    kwargs.update(overrides)
    return m.Diagnostic(**kwargs)


# ---------------------------------------------------------------------------
# The contract: a message that only says WHAT cannot be built
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["check", "what", "why", "how"])
@pytest.mark.parametrize("empty", ["", "   ", "\n\t "])
def test_blank_required_field_is_rejected(field, empty):
    with pytest.raises(ValueError, match=field):
        _valid_diagnostic(**{field: empty})


def test_doc_must_be_an_enum_member_not_a_bare_string():
    """A bare string would let a caller invent an anchor that does not
    exist; the enum is what the anchor-existence test can enumerate."""
    with pytest.raises(TypeError, match="Doc member"):
        _valid_diagnostic(doc="some-anchor-i-made-up")


def test_fields_are_stripped():
    diag = _valid_diagnostic(what="  padded  ")
    assert diag.what == "padded"


def test_diagnostic_is_frozen():
    diag = _valid_diagnostic()
    with pytest.raises(dataclasses.FrozenInstanceError):
        diag.what = "mutated"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_human_render_contains_all_four_answers():
    out = _valid_diagnostic().render_human()
    assert "[required-files]" in out
    assert "Why:" in out
    assert "Fix:" in out
    assert "Docs:" in out
    assert "troubleshooting.md#required-file-or-directory-missing" in out


def test_human_render_indents_continuation_lines_of_a_multiline_fix():
    out = _valid_diagnostic(how="line one\nline two").render_human()
    lines = out.splitlines()
    fix_idx = next(i for i, ln in enumerate(lines) if "Fix:" in ln)
    # The continuation must be indented to align under the first line, not
    # left-aligned where it would read as a separate finding.
    assert lines[fix_idx + 1].startswith("          ")
    assert "line two" in lines[fix_idx + 1]


def test_annotation_is_exactly_one_line():
    """GitHub reads one annotation per line; a raw newline would truncate
    the message at the first break and silently drop the fix."""
    out = _valid_diagnostic(how="first\nsecond\nthird").render_annotation()
    assert "\n" not in out
    assert out.startswith("::error file=")


def test_annotation_encodes_newlines_as_percent_0a():
    out = _valid_diagnostic(how="first\nsecond").render_annotation()
    assert "%0A" in out
    assert "first%0Asecond" in out


def test_annotation_escapes_a_literal_percent_before_encoding_newlines():
    """A literal % in a message would otherwise corrupt the %0A escape."""
    out = _valid_diagnostic(what="100% broken").render_annotation()
    assert "100%25 broken" in out


def test_annotation_without_a_file_has_no_file_key():
    out = _valid_diagnostic(file=None).render_annotation()
    assert out.startswith("::error::")


def test_severity_is_reflected_in_the_annotation():
    out = _valid_diagnostic(severity=m.Severity.WARNING).render_annotation()
    assert out.startswith("::warning file=")


# ---------------------------------------------------------------------------
# CI faults never point at the contributor's file
# ---------------------------------------------------------------------------


def test_infra_fault_annotation_carries_no_file_location():
    """The whole point of the type: our crash must not put a red marker on
    someone else's file."""
    out = m.infra_fault(
        "check_env_vars.py", "AttributeError"
    ).render_annotation()
    assert out.startswith("::error::")
    assert "file=" not in out


def test_infra_fault_says_it_is_not_the_contributors_fault():
    out = m.infra_fault("check_env_vars.py", "boom").render_annotation()
    assert "NOT caused by your changes" in out


def test_report_infra_fault_returns_the_dedicated_exit_code(capsys):
    code = m.report_infra_fault(m.infra_fault("x.py", "boom"))
    assert code == m.EXIT_CI_FAULT
    assert code != m.EXIT_VIOLATIONS
    assert "[ci-fault]" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# report()
# ---------------------------------------------------------------------------


def test_report_emits_one_annotation_per_diagnostic(capsys):
    """No "first error (+N more)" collapse: a contributor reading the Files
    tab should see every problem, not a teaser for the job log."""
    diags = [
        _valid_diagnostic(what=f"Problem {i}.", file=f"a/f{i}.toml")
        for i in range(4)
    ]
    code = m.report(
        diags, header="h", passed_message="p", next_step="do the thing"
    )
    out = capsys.readouterr().out
    assert code == m.EXIT_VIOLATIONS
    assert out.count("::error file=") == 4
    assert "more)" not in out


def test_report_groups_human_output_by_file(capsys):
    m.report(
        [
            _valid_diagnostic(file="a/one.toml", what="First."),
            _valid_diagnostic(file="a/one.toml", what="Second."),
            _valid_diagnostic(file="b/two.toml", what="Third."),
        ],
        header="h",
        passed_message="p",
        next_step="n",
    )
    out = capsys.readouterr().out
    assert out.count("a/one.toml") >= 1
    assert out.count("b/two.toml") >= 1


def test_report_with_no_diagnostics_passes_quietly(capsys):
    code = m.report([], header="h", passed_message="all good", next_step="n")
    assert code == m.EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS] all good" in out
    assert "::error" not in out


def test_report_always_points_at_the_troubleshooting_handbook(capsys):
    m.report(
        [_valid_diagnostic()], header="h", passed_message="p", next_step="n"
    )
    assert "troubleshooting.md" in capsys.readouterr().out


def test_exit_codes_are_distinct():
    codes = {m.EXIT_OK, m.EXIT_VIOLATIONS, m.EXIT_CI_FAULT}
    assert len(codes) == 3


# ---------------------------------------------------------------------------
# The anti-rot test
# ---------------------------------------------------------------------------


def test_every_doc_anchor_exists_in_troubleshooting():
    """Every Doc member must resolve to a real heading.

    This is the test that keeps the handbook honest: adding a Doc member
    without writing the section fails here rather than shipping a link
    that 404s for a contributor who is already stuck.
    """
    present = _anchors_in_troubleshooting()
    missing = sorted(d.value for d in m.Doc if d.value not in present)
    assert not missing, (
        f"Doc members with no matching heading in {TROUBLESHOOTING.name}: "
        f"{missing}. Add a '## <heading>' section for each, or remove the "
        f"enum member."
    )


def test_no_contributor_checker_hand_builds_an_annotation():
    """Hand-rolled `::error` strings are how message quality drifted in the
    first place: each one is a separate author deciding for themselves
    whether to include a why, a fix or a doc link. Routing everything
    through Diagnostic makes those three mandatory. This test is the
    regression guard.

    Two maintenance bots are exempt, on the same grounds: they run on a
    schedule, and their output is read by a maintainer inspecting a run
    rather than by a contributor trying to get a pull request green.
    Diagnostic's mandatory doc link points into the contributor
    troubleshooting handbook, which has nothing to say about either.

      close_orphan_dependabot_prs.py  closes stranded Dependabot PRs
      sweep_stale_branches.py         deletes dead branches

    The bar for adding to this set is "no contributor will ever read this",
    not "writing a Diagnostic is inconvenient".
    """
    exempt = {
        "ci_message.py",
        "close_orphan_dependabot_prs.py",
        "sweep_stale_branches.py",
    }
    pattern = re.compile(r'["\']::(error|warning|notice)')
    offenders: list[str] = []

    for directory in ("tools", ".github/scripts"):
        for path in sorted((REPO_ROOT / directory).glob("*.py")):
            if path.name in exempt:
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if pattern.search(line):
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "These build a GitHub annotation by hand instead of going through "
        "ci_message.Diagnostic, which means nothing forces them to explain "
        "WHY the rule applies or HOW to fix it:\n  " + "\n  ".join(offenders)
    )


def test_doc_url_is_repo_relative_and_anchored():
    url = m.Doc.ENV_VARS.url
    assert url.startswith("docs/recipe-handbook/troubleshooting.md#")
    assert not url.startswith("/")


def test_troubleshooting_contents_list_covers_every_anchor_used_by_code():
    """The Contents block at the top is how people navigate the page; an
    anchor reachable only by scrolling is half-documented."""
    text = TROUBLESHOOTING.read_text(encoding="utf-8")
    contents = text.split("---", 1)[0]
    missing = [d.value for d in m.Doc if f"(#{d.value})" not in contents]
    assert not missing, (
        f"Anchors referenced from code but absent from the Contents list: "
        f"{missing}"
    )


def test_every_checker_entrypoint_is_crash_guarded():
    """An unguarded entrypoint turns our bug into the contributor's verdict.

    A crash exits non-zero, and the calling workflow cannot tell that apart
    from EXIT_VIOLATIONS — so it prints "ACTION REQUIRED, here is how to fix
    your recipe" underneath a traceback nobody can act on. Every checker
    therefore has to route its `sys.exit` through `guard()` (or handle the
    fault itself via `report_infra_fault`).

    Scoped to modules that actually import ci_message: those are the
    contributor-facing checkers. A helper or maintenance script that never
    reports to a contributor has nothing to guard.
    """
    offenders: list[str] = []

    for directory in ("tools", ".github/scripts"):
        for path in sorted((REPO_ROOT / directory).glob("*.py")):
            if path.name == "ci_message.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "ci_message" not in text:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if not stripped.startswith("sys.exit("):
                    continue
                if "guard(" in stripped or "report_infra_fault(" in stripped:
                    continue
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {stripped}")

    assert not offenders, (
        "These exit without a crash guard, so an unhandled exception is "
        "reported to the contributor as a problem with their recipe. Wrap "
        "the call in ci_message.guard():\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# guard()
# ---------------------------------------------------------------------------


def test_guard_returns_the_exit_code_of_a_clean_run():
    """The guard must be transparent when nothing goes wrong."""
    assert m.guard("checker.py", lambda: m.EXIT_OK) == m.EXIT_OK
    assert m.guard("checker.py", lambda: m.EXIT_VIOLATIONS) == m.EXIT_VIOLATIONS


def test_guard_turns_a_crash_into_exit_ci_fault():
    def boom() -> int:
        raise RuntimeError("kaboom")

    assert m.guard("checker.py", boom) == m.EXIT_CI_FAULT


def test_guard_crash_output_names_the_checker_not_a_file(capsys):
    """A CI fault must never carry `file=`.

    `::error file=x` is what paints a red marker on a contributor's source
    in the Files tab. Doing that for our own crash is the single behaviour
    InfraFault exists to prevent.
    """

    def boom() -> int:
        raise ValueError("bad internals")

    m.guard("check_env_vars.py", boom)
    out = capsys.readouterr().out
    assert "::error file=" not in out
    assert "check_env_vars.py" in out
    assert "ValueError: bad internals" in out
    assert "NOT caused by your changes" in out
    assert m.Doc.CI_FAULT.url in out


def test_guard_lets_systemexit_through():
    """`argparse --help` raises SystemExit; swallowing it would turn a
    help request into a spurious CI fault."""

    def helped() -> int:
        raise SystemExit(0)

    with pytest.raises(SystemExit):
        m.guard("checker.py", helped)


def test_guard_lets_keyboardinterrupt_through():
    """Ctrl-C locally must stay a Ctrl-C, not become 'CI tooling failure'."""

    def interrupted() -> int:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        m.guard("checker.py", interrupted)


def test_guard_reports_an_exception_with_an_empty_message():
    """`raise RuntimeError()` has no str(); the detail must still render.

    A bare type name is thin, but an empty `Detail:` line would read as if
    the checker had nothing to say about its own failure.
    """

    def boom() -> int:
        raise RuntimeError

    assert m.guard("checker.py", boom) == m.EXIT_CI_FAULT
