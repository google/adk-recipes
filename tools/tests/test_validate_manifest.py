#!/usr/bin/env python3
"""Unit tests for tools/validate_manifest.py.

The `validate` / `validate_manifest` modules are importable directly because
the repo root is installed as the `adk-recipes-tools` package (see the hatch
`sources` mapping in pyproject.toml), so no sys.path shim is needed here.
"""

import textwrap
from pathlib import Path

import pytest
import validate_manifest as m
from ci_message import Diagnostic, Doc

VALID_MANIFEST = textwrap.dedent(
    """\
    type: standalone
    status: active
    language: python
    description: A valid recipe description that is comfortably long.
    ownership:
      team: My Team
      poc: my-github-id
    """
)


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _blob(diagnostics: list[Diagnostic]) -> str:
    """Every field of every diagnostic, flattened for substring checks.

    The tests assert on the CONTRACT — the offending name, the rule that
    fired, an actionable fix, a doc link — not on one exact sentence, so
    wording can be improved without a test edit.
    """
    return "\n".join(
        f"{d.check}|{d.what}|{d.why}|{d.how}|{d.doc.url}|{d.file}"
        for d in diagnostics
    )


def _make_recipe(
    root: Path, rel: str, manifest: str | None = VALID_MANIFEST
) -> Path:
    """Create a recipe dir at root/rel. If manifest is None, omit manifest.yaml
    but still add a file so the dir qualifies as a recipe."""
    recipe = root / rel
    recipe.mkdir(parents=True, exist_ok=True)
    if manifest is None:
        _write(recipe / "agent.py", "# placeholder\n")
    else:
        _write(recipe / "manifest.yaml", manifest)
    return recipe


# ---------------------------------------------------------------------------
# is_recipe_dir
# ---------------------------------------------------------------------------


def test_is_recipe_dir_true_with_content(tmp_path):
    d = tmp_path / "my-recipe"
    _write(d / "manifest.yaml", VALID_MANIFEST)
    assert m.is_recipe_dir(d) is True


def test_is_recipe_dir_false_readme_only(tmp_path):
    d = tmp_path / "docs-only"
    _write(d / "README.md", "# only readme")
    assert m.is_recipe_dir(d) is False


def test_is_recipe_dir_false_for_language_namespace(tmp_path):
    d = tmp_path / "python"
    _write(d / "some-recipe" / "manifest.yaml", VALID_MANIFEST)
    assert m.is_recipe_dir(d) is False


def test_is_recipe_dir_false_for_hidden_dir(tmp_path):
    d = tmp_path / ".hidden"
    _write(d / "manifest.yaml", VALID_MANIFEST)
    assert m.is_recipe_dir(d) is False


def test_is_recipe_dir_false_for_file(tmp_path):
    f = _write(tmp_path / "afile.txt", "x")
    assert m.is_recipe_dir(f) is False


# ---------------------------------------------------------------------------
# validate_manifest (uses the real schema)
# ---------------------------------------------------------------------------


def test_validate_manifest_valid(tmp_path):
    schema = m.load_schema()
    manifest = _write(tmp_path / "manifest.yaml", VALID_MANIFEST)
    assert m.validate_manifest(manifest, schema) == []


def test_validate_manifest_empty_file(tmp_path):
    schema = m.load_schema()
    manifest = _write(tmp_path / "manifest.yaml", "")
    (diag,) = m.validate_manifest(manifest, schema)
    assert diag.check == "manifest-empty"
    # The fix has to name the fields the author is expected to write; a
    # bare "manifest.yaml is empty" left them to go and find the schema.
    for field in ("type", "status", "language", "description", "ownership"):
        assert field in diag.how
    assert diag.doc is Doc.MANIFEST


def test_validate_manifest_comments_only_is_named_as_such(tmp_path):
    """The confusing half of the empty case: the author can see text."""
    manifest = _write(
        tmp_path / "manifest.yaml", "# type: standalone\n# TODO fill in\n"
    )
    (diag,) = m.validate_manifest(manifest, m.load_schema())
    assert "only comments" in diag.what


def test_validate_manifest_bad_yaml(tmp_path):
    schema = m.load_schema()
    manifest = _write(tmp_path / "manifest.yaml", "type: [unclosed\n")
    (diag,) = m.validate_manifest(manifest, schema)
    assert diag.check == "manifest-yaml"
    assert "YAML parse error" in diag.how
    assert "line" in diag.how


def test_yaml_parse_error_keeps_line_and_column_in_the_annotation(tmp_path):
    """The parser's report is multi-line. An annotation is one line, so
    the detail used to be truncated away at the first newline — leaving
    "YAML parse error: while parsing a flow node" and nothing else."""
    manifest = _write(tmp_path / "manifest.yaml", "type: [unclosed\n")
    (diag,) = m.validate_manifest(manifest, m.load_schema())
    annotation = diag.render_annotation()
    assert "\n" not in annotation
    assert "%0A" in annotation
    assert "line 1" in annotation


def test_validate_manifest_missing_required_field(tmp_path):
    schema = m.load_schema()
    # Missing 'ownership'
    content = textwrap.dedent(
        """\
        type: standalone
        status: active
        language: python
        description: A valid recipe description that is long enough.
        """
    )
    manifest = _write(tmp_path / "manifest.yaml", content)
    diagnostics = m.validate_manifest(manifest, schema)
    blob = _blob(diagnostics)
    assert "'ownership'" in blob
    assert "generate-manifest" in blob
    assert Doc.MANIFEST.url in blob


def test_unknown_field_lists_the_allowed_ones_and_suggests(tmp_path):
    """A closed schema turns a typo into a hard failure, so the message
    has to carry the correction — not just the rejection."""
    manifest = _write(tmp_path / "manifest.yaml", VALID_MANIFEST + "tag: []\n")
    diagnostics = m.validate_manifest(manifest, m.load_schema())
    blob = _blob(diagnostics)
    assert "'tag'" in blob
    assert "Did you mean 'tags'?" in blob
    # The allowed set is in the schema we already loaded; withholding it
    # buys the contributor a second CI round trip.
    assert "ownership" in blob and "description" in blob


def test_top_level_json_path_is_named_in_english(tmp_path):
    manifest = _write(tmp_path / "manifest.yaml", VALID_MANIFEST + "tag: []\n")
    diagnostics = m.validate_manifest(manifest, m.load_schema())
    blob = _blob(diagnostics)
    assert "top level" in blob
    assert "[$]" not in blob


def test_too_short_prints_the_threshold_and_the_actual_length(tmp_path):
    content = VALID_MANIFEST.replace(
        "description: A valid recipe description that is comfortably long.",
        "description: short",
    )
    manifest = _write(tmp_path / "manifest.yaml", content)
    blob = _blob(m.validate_manifest(manifest, m.load_schema()))
    assert "5 characters" in blob
    assert "at least 10" in blob
    assert "minLength: 10" in blob


def test_enum_violation_lists_the_allowed_values(tmp_path):
    content = VALID_MANIFEST.replace(
        "type: standalone", "type: not_a_valid_type"
    )
    manifest = _write(tmp_path / "manifest.yaml", content)
    blob = _blob(m.validate_manifest(manifest, m.load_schema()))
    assert "standalone" in blob and "module" in blob


def test_validate_manifest_placeholder_team_and_poc(tmp_path):
    schema = m.load_schema()
    content = textwrap.dedent(
        f"""\
        type: standalone
        status: active
        language: python
        description: A valid recipe description that is long enough.
        ownership:
          team: "{m.OWNERSHIP_TEAM_PLACEHOLDER}"
          poc: "{m.OWNERSHIP_POC_PLACEHOLDER}"
        """
    )
    manifest = _write(tmp_path / "manifest.yaml", content)
    diagnostics = m.validate_manifest(manifest, schema)
    blob = _blob(diagnostics)
    assert "ownership.team" in blob
    assert "ownership.poc" in blob
    assert all(
        d.doc is Doc.OWNERSHIP_PLACEHOLDER
        for d in diagnostics
        if d.check == "ownership-placeholder"
    )


def test_validate_manifest_placeholder_description(tmp_path):
    # A TODO placeholder description is long enough to pass the schema's
    # minLength, so it must be caught by the explicit placeholder guard.
    schema = m.load_schema()
    content = textwrap.dedent(
        """\
        type: standalone
        status: active
        language: python
        description: "TODO: Replace with a clear description of what this recipe demonstrates (min 10 characters)."
        ownership:
          team: My Team
          poc: my-github-id
        """
    )
    manifest = _write(tmp_path / "manifest.yaml", content)
    blob = _blob(m.validate_manifest(manifest, schema))
    assert "description" in blob


def test_validate_manifest_with_valid_license(tmp_path):
    schema = m.load_schema()
    content = VALID_MANIFEST + 'license: "Apache-2.0"\n'
    manifest = _write(tmp_path / "manifest.yaml", content)
    assert m.validate_manifest(manifest, schema) == []


def test_validate_manifest_with_empty_license(tmp_path):
    schema = m.load_schema()
    content = VALID_MANIFEST + 'license: ""\n'
    manifest = _write(tmp_path / "manifest.yaml", content)
    blob = _blob(m.validate_manifest(manifest, schema))
    assert "license" in blob
    assert "minLength: 1" in blob


# ---------------------------------------------------------------------------
# collect_recipe_dirs (monkeypatch REPO_ROOT to a fake tree)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """A fake repo with flat and namespaced recipes under core/ and contrib/."""
    _make_recipe(tmp_path, "core/recipe-a")
    _make_recipe(tmp_path, "core/python/recipe-b")
    _make_recipe(tmp_path, "contrib/recipe-c")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    return tmp_path


def _rel(paths, root):
    return {str(p.relative_to(root)) for p in paths}


def test_collect_all(fake_repo):
    dirs = m.collect_recipe_dirs(None)
    assert _rel(dirs, fake_repo) == {
        "core/recipe-a",
        "core/python/recipe-b",
        "contrib/recipe-c",
    }


def test_collect_single_root(fake_repo):
    dirs = m.collect_recipe_dirs("core")
    assert _rel(dirs, fake_repo) == {"core/recipe-a", "core/python/recipe-b"}


def test_collect_language_namespace(fake_repo):
    dirs = m.collect_recipe_dirs("core/python")
    assert _rel(dirs, fake_repo) == {"core/python/recipe-b"}


def test_collect_single_flat_recipe(fake_repo):
    dirs = m.collect_recipe_dirs("core/recipe-a")
    assert _rel(dirs, fake_repo) == {"core/recipe-a"}


def test_collect_single_namespaced_recipe(fake_repo):
    dirs = m.collect_recipe_dirs("core/python/recipe-b")
    assert _rel(dirs, fake_repo) == {"core/python/recipe-b"}


def test_collect_nonexistent_scope_returns_nothing(fake_repo):
    """A collector must not kill the process from inside a helper: only
    the caller knows enough to say WHY nothing matched."""
    assert m.collect_recipe_dirs("core/does-not-exist") == []


def test_nonexistent_scope_is_explained_not_silently_passed(fake_repo, capsys):
    assert m.main("core/does-not-exist") == 1
    out = capsys.readouterr().out
    assert "does not exist" in out
    assert "troubleshooting.md" in out


# ---------------------------------------------------------------------------
# skills/ — mandatory vertical namespace (skills/<vertical>/<solution>)
# ---------------------------------------------------------------------------


@pytest.fixture
def skills_repo(tmp_path, monkeypatch):
    """A fake repo laid out as skills/<vertical>/<solution>."""
    _make_recipe(tmp_path, "skills/retail/store-ops")
    _make_recipe(tmp_path, "skills/hr/onboarding")
    _make_recipe(tmp_path, "skills/finance/month-end-close")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    return tmp_path


def test_collect_skills_returns_solutions_not_verticals(skills_repo):
    """The regression this whole layout change exists to prevent: before
    NAMESPACE_REQUIRED_ROOTS, this returned the VERTICALS (skills/retail,
    skills/hr), so every check ran against the wrong directory and the
    real solutions were never validated at all."""
    dirs = m.collect_recipe_dirs("skills")
    assert _rel(dirs, skills_repo) == {
        "skills/retail/store-ops",
        "skills/hr/onboarding",
        "skills/finance/month-end-close",
    }


def test_collect_scoped_to_one_vertical(skills_repo):
    dirs = m.collect_recipe_dirs("skills/retail")
    assert _rel(dirs, skills_repo) == {"skills/retail/store-ops"}


def test_collect_single_solution(skills_repo):
    dirs = m.collect_recipe_dirs("skills/retail/store-ops")
    assert _rel(dirs, skills_repo) == {"skills/retail/store-ops"}


def test_collect_skips_a_solution_with_no_vertical(tmp_path, monkeypatch):
    """A solution directly under skills/ is treated as an (empty) vertical
    and contributes nothing, rather than being validated at the wrong
    depth. validate_placement.py is what reports the misplacement."""
    _make_recipe(tmp_path, "skills/retail/store-ops")
    _make_recipe(tmp_path, "skills/no-vertical")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    assert _rel(m.collect_recipe_dirs("skills"), tmp_path) == {
        "skills/retail/store-ops"
    }


def test_a_vertical_named_like_a_language_is_still_a_vertical(
    tmp_path, monkeypatch
):
    """`skills/python/foo` is vertical `python` + solution `foo`, not a
    language namespace. The result matches what the old language-based
    rule produced, but for a different reason — pinned so a future
    refactor cannot quietly reintroduce language semantics under skills/."""
    _make_recipe(tmp_path, "skills/python/foo")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    assert _rel(m.collect_recipe_dirs("skills"), tmp_path) == {
        "skills/python/foo"
    }


@pytest.mark.parametrize(
    "parts,expected",
    [
        # core/contrib: namespace recognised by NAME.
        (["core", "python"], True),
        (["contrib", "java"], True),
        (["core", "my-recipe"], False),
        # skills: namespace recognised by POSITION, whatever it is called.
        (["skills", "retail"], True),
        (["skills", "anything-at-all"], True),
        # Depth matters — only the component directly under a root.
        (["skills", "retail", "store-ops"], False),
        (["core", "python", "foo"], False),
        (["core"], False),
        # Not a recipe root.
        (["python", "agents"], False),
    ],
)
def test_is_namespace_path(parts, expected):
    assert m.is_namespace_path(parts) is expected


def test_collect_invalid_recipe_dir_says_what_a_recipe_is(
    tmp_path, monkeypatch, capsys
):
    """A dir with only README.md is not a recipe dir. The old message was
    "[ERROR] Not a valid recipe directory: /abs/path" — it named neither
    the rule nor the fix, and it was an absolute path."""
    readme_only = tmp_path / "core" / "readme-only"
    _write(readme_only / "README.md", "# just docs")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)

    assert m.collect_recipe_dirs("core/readme-only") == []
    assert m.main("core/readme-only") == 1

    out = capsys.readouterr().out
    assert "'core/readme-only' is not a recipe directory" in out
    assert str(tmp_path) not in out
    assert "manifest.yaml" in out
    assert "generate-manifest" in out


# ---------------------------------------------------------------------------
# main (end-to-end against a fake tree)
# ---------------------------------------------------------------------------


def test_main_all_valid_returns_zero(tmp_path, monkeypatch):
    _make_recipe(tmp_path, "core/good-a")
    _make_recipe(tmp_path, "core/good-b")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    assert m.main("core") == 0


def test_main_missing_manifest_returns_one(tmp_path, monkeypatch):
    _make_recipe(tmp_path, "core/no-manifest", manifest=None)
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    assert m.main("core") == 1


def test_main_invalid_manifest_returns_one(tmp_path, monkeypatch):
    _make_recipe(tmp_path, "core/bad", manifest="type: not_a_valid_enum\n")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    assert m.main("core/bad") == 1


def test_main_missing_manifest_emits_github_annotation(
    tmp_path, monkeypatch, capsys
):
    # CI relies on the ::error file=...:: annotation format for PR feedback.
    _make_recipe(tmp_path, "core/no-manifest", manifest=None)
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)

    assert m.main("core") == 1

    out = capsys.readouterr().out
    assert "::error file=core/no-manifest/manifest.yaml::" in out
    assert "is missing" in out


def test_main_invalid_manifest_emits_github_annotation(
    tmp_path, monkeypatch, capsys
):
    _make_recipe(tmp_path, "core/bad", manifest="type: not_a_valid_enum\n")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)

    assert m.main("core/bad") == 1

    out = capsys.readouterr().out
    assert "::error file=core/bad/manifest.yaml::" in out


# ---------------------------------------------------------------------------
# inactive_recipes / report_inactive
#
# `status` gained its first consumer with the recipe canary, and all 85 lines
# of it shipped untested.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "status: inactive",
        'status: "inactive"',
        "status: 'inactive'",
        "status:   inactive",
        "status: inactive  # sunsetting",
        "status: INACTIVE",
    ],
)
def test_inactive_is_detected_in_every_spelling(tmp_path, monkeypatch, line):
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    d = _make_recipe(tmp_path, "core/python/a", f"type: standalone\n{line}\n")
    assert m.inactive_recipes([d]) == ["core/python/a"]


def test_inactive_is_detected_in_a_crlf_manifest(tmp_path, monkeypatch):
    """A Windows-authored manifest is detected like any other.

    This looks like it should need `\\r` in the regex's trailing character
    class, and does not: `read_text()` opens in text mode, so universal-
    newline translation turns CRLF into LF before the regex ever runs.
    Pinned because the apparent gap is inviting to "fix", and the added
    `\\r` would be dead characters nothing can reach.
    """
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    d = tmp_path / "core/python/a"
    d.mkdir(parents=True)
    (d / "manifest.yaml").write_bytes(
        b"type: standalone\r\nstatus: inactive\r\n"
    )
    assert m.inactive_recipes([d]) == ["core/python/a"]


@pytest.mark.parametrize(
    "line",
    [
        "status: active",
        "# status: inactive",
        "old_status: inactive",
        "status: inactive-ish",
    ],
)
def test_active_and_near_misses_are_not_reported(tmp_path, monkeypatch, line):
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    d = _make_recipe(tmp_path, "core/python/a", f"type: standalone\n{line}\n")
    assert m.inactive_recipes([d]) == []


def test_a_missing_manifest_is_skipped_not_raised(tmp_path, monkeypatch):
    """This runs AFTER validation, so a recipe whose manifest is absent or
    unreadable must not produce a traceback on top of its diagnostic."""
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    d = _make_recipe(tmp_path, "core/python/a", manifest=None)
    assert m.inactive_recipes([d]) == []


def test_results_are_sorted(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    body = "type: standalone\nstatus: inactive\n"
    dirs = [
        _make_recipe(tmp_path, "core/python/c", body),
        _make_recipe(tmp_path, "core/python/a", body),
        _make_recipe(tmp_path, "core/python/b", body),
    ]
    assert m.inactive_recipes(dirs) == [
        "core/python/a",
        "core/python/b",
        "core/python/c",
    ]


def test_report_inactive_prints_nothing_when_all_active(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    d = _make_recipe(
        tmp_path, "core/python/a", "type: standalone\nstatus: active\n"
    )
    m.report_inactive([d])
    assert capsys.readouterr().out == ""


def test_report_inactive_annotates_without_failing(
    tmp_path, monkeypatch, capsys
):
    """Non-blocking by construction: it returns None, so no caller can fold
    it into an exit code by accident."""
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    d = _make_recipe(
        tmp_path, "core/python/a", "type: standalone\nstatus: inactive\n"
    )
    assert m.report_inactive([d]) is None
    out = capsys.readouterr().out
    assert "[NOTICE]" in out
    assert "core/python/a" in out
    assert "::warning file=core/python/a/manifest.yaml" in out
    assert "::error" not in out
    # The notice has to say what to do about it, not just that it is true.
    assert "status: active" in out
    assert Doc.RECIPE_INACTIVE.value in out
