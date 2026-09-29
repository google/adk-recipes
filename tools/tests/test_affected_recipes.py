#!/usr/bin/env python3
"""Unit tests for tools/affected_recipes.py.

Two layers of tests:
  - `recipe_dir_for` is pure string logic; a table of path → expected
    covers the layout matrix (flat / language-namespaced / not-a-recipe).
  - `compute_affected_recipes` composes the mapping with an on-disk
    existence filter and deduplication, exercised against a temporary
    fake repo tree.
"""

from pathlib import Path

import affected_recipes as m
import pytest

# ---------------------------------------------------------------------------
# recipe_dir_for — pure string mapping, no filesystem access
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        # Flat recipes under core/ and contrib/.
        ("core/my-recipe/agent.py", "core/my-recipe"),
        ("core/my-recipe/tests/foo.py", "core/my-recipe"),
        ("contrib/other/README.md", "contrib/other"),
        # Language-namespaced recipes.
        ("core/python/foo/agent.py", "core/python/foo"),
        ("contrib/python/bar/pyproject.toml", "contrib/python/bar"),
        ("core/java/hello/pom.xml", "core/java/hello"),
        ("contrib/go/example/main.go", "contrib/go/example"),
        # Plugins use a MANDATORY vertical namespace,
        # plugins/<vertical>/<solution>. The vertical is free-form, so the
        # solution is identified by position rather than by name.
        ("plugins/retail/store-ops/SKILL.md", "plugins/retail/store-ops"),
        ("plugins/hr/onboarding/scripts/run.py", "plugins/hr/onboarding"),
        ("plugins/finance/close/eval/cases.jsonl", "plugins/finance/close"),
        # A solution placed directly under plugins/, with no vertical, must
        # NOT be promoted to a recipe — mapping it would validate it at the
        # wrong depth and hide the misplacement. validate_placement.py is
        # what reports it.
        ("plugins/foo/SKILL.md", None),
        ("plugins/foo/manifest.yaml", None),
        # A vertical-level file belongs to no solution.
        ("plugins/retail/README.md", None),
        # Paths that don't sit under a recipe root.
        ("README.md", None),
        (".github/workflows/x.yml", None),
        ("tools/validate.py", None),
        # A root followed by nothing else (empty basename after the slash,
        # from a trailing-slash input like "core/") does NOT identify a
        # recipe — it must map to None, otherwise the candidate "core/"
        # slips through the existence filter (core/ IS a real dir) and
        # gets treated as a recipe.
        ("core/", None),
    ],
)
def test_recipe_dir_for_table(path, expected):
    assert m.recipe_dir_for(path) == expected


def test_recipe_dir_for_empty_string_returns_none():
    assert m.recipe_dir_for("") is None


def test_recipe_dir_for_single_component_returns_none():
    # Only one path segment → no recipe component to identify.
    assert m.recipe_dir_for("core") is None


# ---------------------------------------------------------------------------
# compute_affected_recipes — composition + dedup + existence filter
# ---------------------------------------------------------------------------


def _make_recipe(root: Path, rel: str) -> Path:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    # A file must exist so is_dir() returns True even after cleanup.
    (d / ".keep").write_text("")
    return d


def test_compute_deduplicates(tmp_path):
    _make_recipe(tmp_path, "core/foo")
    changed = [
        "core/foo/agent.py",
        "core/foo/tests/test_foo.py",
        "core/foo/README.md",
    ]
    assert m.compute_affected_recipes(changed, repo_root=tmp_path) == [
        "core/foo",
    ]


def test_compute_sorts(tmp_path):
    _make_recipe(tmp_path, "core/b-recipe")
    _make_recipe(tmp_path, "core/a-recipe")
    _make_recipe(tmp_path, "contrib/python/x-recipe")
    changed = [
        "contrib/python/x-recipe/foo.py",
        "core/b-recipe/foo.py",
        "core/a-recipe/foo.py",
    ]
    assert m.compute_affected_recipes(changed, repo_root=tmp_path) == [
        "contrib/python/x-recipe",
        "core/a-recipe",
        "core/b-recipe",
    ]


def test_compute_filters_nonexistent_by_default(tmp_path):
    # Only core/real exists — the other two must be dropped.
    _make_recipe(tmp_path, "core/real")
    changed = [
        "core/real/agent.py",
        "core/deleted-recipe/agent.py",  # dir no longer exists
        "core/README.md",  # yields "core/README.md", not a dir
    ]
    assert m.compute_affected_recipes(changed, repo_root=tmp_path) == [
        "core/real",
    ]


def test_compute_no_filter_keeps_candidates(tmp_path):
    # With filter_existing=False the tool becomes pure syntax mapping.
    changed = [
        "core/does-not-exist/agent.py",
        "core/README.md",
    ]
    assert m.compute_affected_recipes(
        changed, filter_existing=False, repo_root=tmp_path
    ) == ["core/README.md", "core/does-not-exist"]


def test_compute_ignores_non_recipe_paths(tmp_path):
    _make_recipe(tmp_path, "core/foo")
    changed = [
        "README.md",
        ".github/policy.yml",
        "tools/validate.py",
        "core/foo/agent.py",
    ]
    assert m.compute_affected_recipes(changed, repo_root=tmp_path) == [
        "core/foo",
    ]


def test_compute_skips_blank_and_whitespace_lines(tmp_path):
    _make_recipe(tmp_path, "core/foo")
    changed = [
        "",
        "   ",
        "core/foo/agent.py",
        "\n",
    ]
    assert m.compute_affected_recipes(changed, repo_root=tmp_path) == [
        "core/foo",
    ]


def test_compute_handles_plugins_root(tmp_path):
    _make_recipe(tmp_path, "plugins/retail/store-ops")
    changed = ["plugins/retail/store-ops/SKILL.md"]
    assert m.compute_affected_recipes(changed, repo_root=tmp_path) == [
        "plugins/retail/store-ops",
    ]


def test_compute_ignores_a_plugin_with_no_vertical(tmp_path):
    """A solution dropped straight under plugins/ is not collected here — it
    would otherwise be validated at the vertical's depth. Reporting it is
    tools/validate_placement.py's job."""
    _make_recipe(tmp_path, "plugins/no-vertical")
    changed = ["plugins/no-vertical/SKILL.md"]
    assert m.compute_affected_recipes(changed, repo_root=tmp_path) == []


def test_compute_returns_empty_on_no_changes(tmp_path):
    assert m.compute_affected_recipes([], repo_root=tmp_path) == []


# ---------------------------------------------------------------------------
# main — end-to-end CLI with stdin plumbing
# ---------------------------------------------------------------------------


def test_main_reads_stdin_and_writes_stdout(tmp_path, monkeypatch, capsys):
    _make_recipe(tmp_path, "core/foo")
    _make_recipe(tmp_path, "contrib/python/bar")

    import io

    stdin = io.StringIO(
        "core/foo/agent.py\n"
        "contrib/python/bar/README.md\n"
        "core/does-not-exist/x.py\n"
        "README.md\n"
    )
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr(
        "sys.argv", ["affected_recipes", "--repo-root", str(tmp_path)]
    )

    assert m.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["contrib/python/bar", "core/foo"]


def test_main_no_filter_flag(tmp_path, monkeypatch, capsys):
    import io

    stdin = io.StringIO("core/does-not-exist/x.py\n")
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr(
        "sys.argv",
        [
            "affected_recipes",
            "--no-filter-existing",
            "--repo-root",
            str(tmp_path),
        ],
    )

    assert m.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["core/does-not-exist"]


# ---------------------------------------------------------------------------
# recipe_language_namespace — pure string, no filesystem access
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "recipe_dir,expected",
    [
        # Flat recipes have no language namespace.
        ("core/foo", None),
        ("contrib/bar", None),
        ("plugins/baz", None),
        # Standard language namespace layer.
        ("core/python/foo", "python"),
        ("contrib/java/bar", "java"),
        ("contrib/typescript/x", "typescript"),
        # A directory named like a language but at the wrong depth
        # doesn't count.
        ("core", None),
        ("core/", None),
    ],
)
def test_recipe_language_namespace(recipe_dir, expected):
    assert m.recipe_language_namespace(recipe_dir) == expected


# ---------------------------------------------------------------------------
# --language filter — reads manifest.yaml for flat recipes
# ---------------------------------------------------------------------------


_MANIFEST_PYTHON = "language: python\n"
_MANIFEST_JAVA = "language: java\n"


def _make_recipe_with_manifest(
    root: Path, rel: str, manifest: str | None = _MANIFEST_PYTHON
) -> Path:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (d / "manifest.yaml").write_text(manifest, encoding="utf-8")
    else:
        # Still needs SOMETHING so the dir exists after cleanup.
        (d / ".keep").write_text("")
    return d


def test_language_filter_includes_namespaced_python(tmp_path):
    _make_recipe_with_manifest(tmp_path, "core/python/foo", manifest=None)
    changed = ["core/python/foo/agent.py"]
    assert m.compute_affected_recipes(
        changed, language="python", repo_root=tmp_path
    ) == ["core/python/foo"]


def test_language_filter_excludes_wrong_namespace(tmp_path):
    # A recipe under core/java/ must be excluded when filtering for
    # python, EVEN IF its manifest declares language: python. Path is
    # authoritative for namespaced recipes.
    _make_recipe_with_manifest(
        tmp_path, "core/java/foo", manifest=_MANIFEST_PYTHON
    )
    changed = ["core/java/foo/pom.xml"]
    assert (
        m.compute_affected_recipes(
            changed, language="python", repo_root=tmp_path
        )
        == []
    )


def test_language_filter_flat_recipe_matches_manifest(tmp_path):
    _make_recipe_with_manifest(
        tmp_path, "core/flat-py", manifest=_MANIFEST_PYTHON
    )
    _make_recipe_with_manifest(
        tmp_path, "core/flat-java", manifest=_MANIFEST_JAVA
    )
    changed = ["core/flat-py/agent.py", "core/flat-java/Main.java"]
    assert m.compute_affected_recipes(
        changed, language="python", repo_root=tmp_path
    ) == ["core/flat-py"]


def test_language_filter_flat_recipe_missing_manifest_excluded(tmp_path):
    # No manifest → no language info → excluded from the language filter.
    _make_recipe_with_manifest(tmp_path, "core/orphan", manifest=None)
    changed = ["core/orphan/agent.py"]
    assert (
        m.compute_affected_recipes(
            changed, language="python", repo_root=tmp_path
        )
        == []
    )


def test_language_filter_flat_recipe_malformed_manifest_excluded(tmp_path):
    _make_recipe_with_manifest(
        tmp_path, "core/broken", manifest="language: [unclosed\n"
    )
    changed = ["core/broken/agent.py"]
    assert (
        m.compute_affected_recipes(
            changed, language="python", repo_root=tmp_path
        )
        == []
    )


def test_language_filter_flat_recipe_manifest_without_language_excluded(
    tmp_path,
):
    _make_recipe_with_manifest(
        tmp_path, "core/no-lang", manifest="type: standalone\n"
    )
    changed = ["core/no-lang/agent.py"]
    assert (
        m.compute_affected_recipes(
            changed, language="python", repo_root=tmp_path
        )
        == []
    )


def test_language_filter_case_insensitive_manifest(tmp_path):
    # Schema disallows uppercase, but the filter should normalise so
    # the manifest-schema check remains the single reporter of that.
    _make_recipe_with_manifest(
        tmp_path, "core/loud", manifest="language: PYTHON\n"
    )
    changed = ["core/loud/agent.py"]
    assert m.compute_affected_recipes(
        changed, language="python", repo_root=tmp_path
    ) == ["core/loud"]


def test_language_filter_case_insensitive_argument(tmp_path):
    # Callers should be free to pass --language Python (uppercase);
    # the filter lowercases before comparing.
    _make_recipe_with_manifest(
        tmp_path, "core/flat-py", manifest=_MANIFEST_PYTHON
    )
    changed = ["core/flat-py/agent.py"]
    assert m.compute_affected_recipes(
        changed, language="Python", repo_root=tmp_path
    ) == ["core/flat-py"]


def test_language_filter_plugins_uses_the_manifest_not_the_path(tmp_path):
    # A plugin's namespace is a VERTICAL, not a language, so the path cannot
    # answer the language question the way core/python/<recipe> can. The
    # filter must fall through to manifest.language — which works because
    # `language` stays a required manifest field for plugins.
    _make_recipe_with_manifest(
        tmp_path, "plugins/retail/store-ops", manifest="language: python\n"
    )
    changed = ["plugins/retail/store-ops/SKILL.md"]
    assert m.compute_affected_recipes(
        changed, language="python", repo_root=tmp_path
    ) == ["plugins/retail/store-ops"]


def test_language_filter_plugins_excludes_other_languages(tmp_path):
    _make_recipe_with_manifest(
        tmp_path, "plugins/retail/store-ops", manifest="language: java\n"
    )
    changed = ["plugins/retail/store-ops/SKILL.md"]
    assert (
        m.compute_affected_recipes(
            changed, language="python", repo_root=tmp_path
        )
        == []
    )


def test_language_filter_mixes_flat_and_namespaced(tmp_path):
    _make_recipe_with_manifest(tmp_path, "core/python/ns-py", manifest=None)
    _make_recipe_with_manifest(
        tmp_path, "core/flat-py", manifest=_MANIFEST_PYTHON
    )
    _make_recipe_with_manifest(
        tmp_path, "core/flat-java", manifest=_MANIFEST_JAVA
    )
    _make_recipe_with_manifest(tmp_path, "core/java/ns-java", manifest=None)
    changed = [
        "core/python/ns-py/x.py",
        "core/flat-py/x.py",
        "core/flat-java/x.java",
        "core/java/ns-java/x.java",
    ]
    assert m.compute_affected_recipes(
        changed, language="python", repo_root=tmp_path
    ) == ["core/flat-py", "core/python/ns-py"]


def test_language_filter_via_cli(tmp_path, monkeypatch, capsys):
    _make_recipe_with_manifest(
        tmp_path, "core/flat-py", manifest=_MANIFEST_PYTHON
    )
    _make_recipe_with_manifest(
        tmp_path, "core/flat-java", manifest=_MANIFEST_JAVA
    )

    import io

    stdin = io.StringIO("core/flat-py/agent.py\ncore/flat-java/Main.java\n")
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr(
        "sys.argv",
        [
            "affected_recipes",
            "--language",
            "python",
            "--repo-root",
            str(tmp_path),
        ],
    )

    assert m.main() == 0
    assert capsys.readouterr().out.splitlines() == ["core/flat-py"]
