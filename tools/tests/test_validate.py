#!/usr/bin/env python3
"""Unit tests for tools/validate.py (argument routing).

These tests stub out the actual validation tools via SUBCOMMANDS so we verify
routing/argument parsing, not the underlying manifest checks (covered by
test_validate_manifest.py).
"""

import pytest
import validate as m


class _Recorder:
    """A stand-in tool `main` that records the scope it was called with."""

    def __init__(self, return_code: int = 0):
        self.calls: list[str | None] = []
        self.return_code = return_code

    def __call__(self, scope=None):
        self.calls.append(scope)
        return self.return_code


@pytest.fixture
def stub_subcommands(monkeypatch):
    """Replace SUBCOMMANDS/VALID_SUBCOMMANDS with a single recording tool."""
    recorder = _Recorder()
    subs = {"manifest": ("Manifest validation", recorder)}
    monkeypatch.setattr(m, "SUBCOMMANDS", subs)
    monkeypatch.setattr(m, "VALID_SUBCOMMANDS", [*subs, "all"])
    return recorder


def _run(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["validate", *argv])
    return m.main()


# ---------------------------------------------------------------------------
# looks_like_scope
# ---------------------------------------------------------------------------


def test_looks_like_scope_path():
    assert m.looks_like_scope("core/rag-agent-search") is True


def test_looks_like_scope_roots():
    assert m.looks_like_scope("core") is True
    assert m.looks_like_scope("contrib") is True
    assert m.looks_like_scope("plugins") is True
    assert m.looks_like_scope("all") is True


def test_looks_like_scope_subcommand_is_false():
    assert m.looks_like_scope("manifest") is False


# ---------------------------------------------------------------------------
# main routing
# ---------------------------------------------------------------------------


def test_main_no_args_runs_all(monkeypatch, stub_subcommands):
    assert _run(monkeypatch, []) == 0
    # run_all iterates every subcommand with scope=None
    assert stub_subcommands.calls == [None]


def test_main_single_scope_arg_runs_all_with_scope(
    monkeypatch, stub_subcommands
):
    assert _run(monkeypatch, ["core"]) == 0
    assert stub_subcommands.calls == ["core"]


def test_main_single_subcommand_arg(monkeypatch, stub_subcommands):
    assert _run(monkeypatch, ["manifest"]) == 0
    assert stub_subcommands.calls == [None]


def test_main_unknown_single_arg_returns_one(monkeypatch, stub_subcommands):
    assert _run(monkeypatch, ["bogus"]) == 1
    assert stub_subcommands.calls == []


def test_main_subcommand_with_scope(monkeypatch, stub_subcommands):
    assert _run(monkeypatch, ["manifest", "core"]) == 0
    assert stub_subcommands.calls == ["core"]


def test_main_all_subcommand_with_scope(monkeypatch, stub_subcommands):
    # Explicit "all" subcommand plus a scope routes to run_all(scope).
    assert _run(monkeypatch, ["all", "core"]) == 0
    assert stub_subcommands.calls == ["core"]


def test_main_all_alone_uses_all_scope(monkeypatch, stub_subcommands):
    # A lone "all" is treated as a scope (looks_like_scope), so run_all("all").
    assert _run(monkeypatch, ["all"]) == 0
    assert stub_subcommands.calls == ["all"]


def test_main_invalid_subcommand_with_scope_returns_one(
    monkeypatch, stub_subcommands
):
    assert _run(monkeypatch, ["bogus", "core"]) == 1
    assert stub_subcommands.calls == []


def test_main_too_many_args_returns_one(monkeypatch, stub_subcommands):
    assert _run(monkeypatch, ["a", "b", "c"]) == 1
    assert stub_subcommands.calls == []


def test_main_help_returns_zero(monkeypatch, stub_subcommands, capsys):
    assert _run(monkeypatch, ["--help"]) == 0
    assert "Usage" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# run_all aggregation
# ---------------------------------------------------------------------------


def test_run_all_returns_one_if_any_fails(monkeypatch):
    passing = _Recorder(return_code=0)
    failing = _Recorder(return_code=1)
    monkeypatch.setattr(
        m,
        "SUBCOMMANDS",
        {
            "pass": ("Passing tool", passing),
            "fail": ("Failing tool", failing),
        },
    )
    assert m.run_all(None) == 1


def test_run_all_returns_zero_if_all_pass(monkeypatch):
    a = _Recorder(return_code=0)
    b = _Recorder(return_code=0)
    monkeypatch.setattr(
        m,
        "SUBCOMMANDS",
        {"a": ("A", a), "b": ("B", b)},
    )
    assert m.run_all(None) == 0


def test_run_all_ci_fault_outranks_a_violation(monkeypatch):
    """A crashed validator must not be reported as an invalid recipe.

    run_all used to `return 0 if all_passed else 1`, which flattened
    EXIT_CI_FAULT into EXIT_VIOLATIONS. The workflow then printed "ACTION
    REQUIRED: here is how to fix your recipe" for a bug in our own code.
    """
    violation = _Recorder(return_code=1)
    crashed = _Recorder(return_code=2)
    monkeypatch.setattr(
        m,
        "SUBCOMMANDS",
        {
            "violation": ("Violating tool", violation),
            "faulted": ("Faulting tool", crashed),
        },
    )
    assert m.run_all(None) == 2


def test_run_all_reports_a_ci_fault_distinctly(monkeypatch, capsys):
    """The summary line has to name the CI fault, not label it [FAIL]."""
    monkeypatch.setattr(
        m,
        "SUBCOMMANDS",
        {"faulted": ("Faulting tool", _Recorder(return_code=2))},
    )
    m.run_all(None)
    out = capsys.readouterr().out
    assert "[CI FAULT] Faulting tool" in out
    assert "[FAIL] Faulting tool" not in out


def test_run_all_still_returns_one_for_a_pure_violation(monkeypatch):
    monkeypatch.setattr(
        m,
        "SUBCOMMANDS",
        {"violation": ("Violating tool", _Recorder(return_code=1))},
    )
    assert m.run_all(None) == 1


# ---------------------------------------------------------------------------
# Crash containment at the console-script boundary
#
# `validate = "validate:main"` in pyproject.toml means main() — not the
# __main__ block — is what CI actually calls. A guard on __main__ alone
# would never fire for `uv run validate structure`.
# ---------------------------------------------------------------------------


def _exploding(scope=None):
    raise RuntimeError("simulated validator crash")


def test_main_converts_a_validator_crash_into_a_ci_fault(monkeypatch):
    subs = {"manifest": ("Manifest validation", _exploding)}
    monkeypatch.setattr(m, "SUBCOMMANDS", subs)
    monkeypatch.setattr(m, "VALID_SUBCOMMANDS", [*subs, "all"])
    assert _run(monkeypatch, ["manifest"]) == 2


def test_main_crash_never_blames_a_contributor_file(monkeypatch, capsys):
    subs = {"manifest": ("Manifest validation", _exploding)}
    monkeypatch.setattr(m, "SUBCOMMANDS", subs)
    monkeypatch.setattr(m, "VALID_SUBCOMMANDS", [*subs, "all"])
    _run(monkeypatch, ["manifest"])
    out = capsys.readouterr().out
    # `::error file=...` is what puts a red marker on someone's source.
    assert "::error file=" not in out
    assert "NOT caused by your changes" in out
    assert "simulated validator crash" in out


def test_main_passes_a_clean_run_through_unchanged(monkeypatch):
    """The guard must not mask ordinary exit codes."""
    subs = {"manifest": ("Manifest validation", _Recorder(return_code=0))}
    monkeypatch.setattr(m, "SUBCOMMANDS", subs)
    monkeypatch.setattr(m, "VALID_SUBCOMMANDS", [*subs, "all"])
    assert _run(monkeypatch, ["manifest"]) == 0
