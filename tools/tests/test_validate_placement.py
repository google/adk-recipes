#!/usr/bin/env python3
"""Unit tests for tools/validate_placement.py.

`describe_violation` is pure string logic and gets a table. The scanning
layer is exercised against temporary trees, since the whole point of the
checker is what it finds on disk.
"""

from pathlib import Path

import pytest
import validate_placement as m
from ci_message import Doc

MANIFEST = "manifest.yaml"


def _make_manifest(root: Path, rel_dir: str) -> Path:
    """Create <root>/<rel_dir>/manifest.yaml and return the manifest path."""
    directory = root / rel_dir
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / MANIFEST
    manifest.write_text("type: recipe\n", encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# describe_violation — pure string logic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel_parts",
    [
        ["plugins", "retail", "store-ops", MANIFEST],
        ["plugins", "hr", "onboarding", MANIFEST],
        ["plugins", "finance", "month-end-close", MANIFEST],
    ],
)
def test_correctly_placed_returns_none(rel_parts):
    assert m.describe_violation(rel_parts) is None


def test_flat_skill_is_rejected():
    """The case that matters: a solution dropped straight under plugins/."""
    diag = m.describe_violation(["plugins", "foo", MANIFEST])
    assert diag is not None
    assert "no vertical" in diag.what
    assert "plugins/<vertical>/foo" in diag.how
    # The fix is the command, not a description of the destination.
    assert "git mv plugins/foo" in diag.how
    assert diag.doc is Doc.PLACEMENT
    assert diag.file == f"plugins/foo/{MANIFEST}"


def test_flat_skill_message_names_the_offending_dir():
    diag = m.describe_violation(["plugins", "store-ops", MANIFEST])
    assert "'plugins/store-ops'" in diag.what
    assert "plugins/retail/store-ops" in diag.how


def test_manifest_at_root_of_plugins_is_rejected():
    diag = m.describe_violation(["plugins", MANIFEST])
    assert diag is not None
    assert "no vertical" in diag.what


def test_too_deep_is_rejected():
    diag = m.describe_violation(
        ["plugins", "retail", "store-ops", "subproject", MANIFEST]
    )
    assert diag is not None
    assert "too deeply" in diag.what
    assert "plugins/retail/store-ops/subproject" in diag.what
    # The suggested destination must be a path that does not already
    # exist — moving onto its own ancestor is not a fix.
    assert (
        "git mv plugins/retail/store-ops/subproject plugins/retail/subproject"
        in diag.how
    )


def test_every_violation_explains_why_the_vertical_exists():
    """ "Move it" without the reason invites the next contributor to make
    the same choice again."""
    for parts in (
        ["plugins", "foo", MANIFEST],
        ["plugins", "retail", "store-ops", "deep", MANIFEST],
    ):
        diag = m.describe_violation(parts)
        assert "ownership" in diag.why
        assert "mandatory" in diag.why


# ---------------------------------------------------------------------------
# find_manifests — scanning and pruning
# ---------------------------------------------------------------------------


def test_find_manifests_on_missing_root_is_empty(tmp_path):
    assert m.find_manifests(tmp_path / "plugins") == []


def test_find_manifests_prunes_vendored_dirs(tmp_path):
    _make_manifest(tmp_path, "plugins/retail/store-ops")
    _make_manifest(tmp_path, "plugins/retail/store-ops/.venv/lib/pkg")
    _make_manifest(tmp_path, "plugins/retail/store-ops/node_modules/thing")
    found = m.find_manifests(tmp_path / "plugins")
    assert [str(p.relative_to(tmp_path)) for p in found] == [
        f"plugins/retail/store-ops/{MANIFEST}"
    ]


# ---------------------------------------------------------------------------
# check_root — scanning plus classification
# ---------------------------------------------------------------------------


def test_check_root_accepts_valid_tree(tmp_path, capsys):
    _make_manifest(tmp_path, "plugins/retail/store-ops")
    _make_manifest(tmp_path, "plugins/hr/onboarding")
    assert m.check_root("plugins", repo_root=tmp_path) == []
    assert "::error" not in capsys.readouterr().out


def test_check_root_flags_flat_skill(tmp_path):
    _make_manifest(tmp_path, "plugins/retail/store-ops")
    _make_manifest(tmp_path, "plugins/oops")
    (diag,) = m.check_root("plugins", repo_root=tmp_path)
    # check_root collects; main() renders. Emitting the annotation from
    # inside the scan is what made "one annotation per problem" hard to
    # guarantee in the first place.
    assert diag.file == f"plugins/oops/{MANIFEST}"
    assert f"::error file=plugins/oops/{MANIFEST}::" in diag.render_annotation()


def test_check_root_flags_every_offender(tmp_path):
    _make_manifest(tmp_path, "plugins/a")
    _make_manifest(tmp_path, "plugins/b")
    _make_manifest(tmp_path, "plugins/retail/ok")
    assert len(m.check_root("plugins", repo_root=tmp_path)) == 2


def test_check_root_flags_too_deep(tmp_path):
    _make_manifest(tmp_path, "plugins/retail/store-ops/inner")
    (diag,) = m.check_root("plugins", repo_root=tmp_path)
    assert "too deeply" in diag.what


def test_check_root_on_empty_root(tmp_path):
    (tmp_path / "plugins").mkdir()
    assert m.check_root("plugins", repo_root=tmp_path) == []


# ---------------------------------------------------------------------------
# main — exit codes against the real repository
# ---------------------------------------------------------------------------


def test_main_passes_on_the_real_repo():
    """The repository must always be placement-clean on main."""
    assert m.main() == 0


def test_main_tolerates_a_missing_plugins_root(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    assert m.main() == 0
    assert "[SKIP]" in capsys.readouterr().out


def test_main_reports_failure(monkeypatch, tmp_path, capsys):
    _make_manifest(tmp_path, "plugins/oops")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    assert m.main() == 1
    out = capsys.readouterr().out
    assert "ACTION REQUIRED" in out
    assert "1 problem to fix" in out
    assert f"::error file=plugins/oops/{MANIFEST}::" in out


def test_main_annotates_every_offender(monkeypatch, tmp_path, capsys):
    _make_manifest(tmp_path, "plugins/a")
    _make_manifest(tmp_path, "plugins/b")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    assert m.main() == 1
    out = capsys.readouterr().out
    assert "(+" not in out
    assert f"::error file=plugins/a/{MANIFEST}::" in out
    assert f"::error file=plugins/b/{MANIFEST}::" in out


def test_main_footer_leads_with_the_authoring_docs(
    monkeypatch, tmp_path, capsys
):
    _make_manifest(tmp_path, "plugins/oops")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    assert m.main() == 1
    out = capsys.readouterr().out
    assert "docs/recipe-handbook/README.md" in out
    assert "docs/recipe-checklist.md" in out
    assert "docs/recipe-handbook/troubleshooting.md" in out


def test_main_narrows_to_the_given_scope(monkeypatch, tmp_path):
    """A scope must not surface violations from elsewhere in the tree, or
    `uv run validate <a-core-recipe>` would fail on an unrelated skill."""
    _make_manifest(tmp_path, "plugins/oops")
    _make_manifest(tmp_path, "plugins/retail/store-ops")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    assert m.main("plugins/retail") == 0
    assert m.main("plugins") == 1
    assert m.main() == 1


def test_main_ignores_a_scope_outside_the_checked_roots(
    monkeypatch, tmp_path, capsys
):
    _make_manifest(tmp_path, "plugins/oops")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    assert m.main("core/python/foo") == 0
    assert "::error" not in capsys.readouterr().out


def test_registered_as_a_validate_subcommand():
    import validate

    assert "placement" in validate.SUBCOMMANDS
    assert validate.SUBCOMMANDS["placement"][1] is m.main
    assert "placement" in validate.VALID_SUBCOMMANDS


# ---------------------------------------------------------------------------
# Configuration guards
# ---------------------------------------------------------------------------


def test_checked_roots_tracks_the_shared_constant():
    import validate_manifest as vm

    assert set(m.CHECKED_ROOTS) == set(vm.NAMESPACE_REQUIRED_ROOTS)
    assert "plugins" in m.CHECKED_ROOTS


def test_core_and_contrib_are_not_checked_yet():
    """Flat core/<recipe> is still legal, so those roots stay out of scope
    until that layout is retired."""
    assert "core" not in m.CHECKED_ROOTS
    assert "contrib" not in m.CHECKED_ROOTS
