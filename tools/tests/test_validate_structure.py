#!/usr/bin/env python3
"""Unit tests for tools/validate_structure.py.

Structure similar to test_validate_manifest.py: tiny per-function tests
plus a handful of end-to-end tests that monkeypatch REPO_ROOT to a fake
recipe tree so we exercise the real orchestration without touching the
committed recipes.
"""

import textwrap
from pathlib import Path

import pytest
import validate_manifest as vm
import validate_structure as m
from ci_message import Diagnostic, Doc

# Shared minimal-valid manifest — mirrors test_validate_manifest.VALID_MANIFEST.
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
    fired (provenance), an actionable fix, a doc link — rather than on one
    exact sentence, so wording can be improved without a test edit.
    """
    return "\n".join(
        f"{d.check}|{d.what}|{d.why}|{d.how}|{d.doc.url}|{d.file}"
        for d in diagnostics
    )


def _names(entries: list[tuple[str, str]]) -> list[str]:
    return [name for name, _ in entries]


def _make_python_recipe(
    root: Path,
    rel: str,
    *,
    manifest: str | None = VALID_MANIFEST,
    include_agents: bool = False,
    include_python_files: bool = True,
) -> Path:
    """Create a directory that looks like a real Python recipe: manifest
    + README + pyproject.toml + uv.lock + .env.example + tests/. Callers
    flip flags to remove pieces and exercise negative paths."""
    recipe = root / rel
    recipe.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        _write(recipe / "manifest.yaml", manifest)
    _write(recipe / "README.md", "# recipe\n")
    if include_python_files:
        _write(recipe / "pyproject.toml", "[project]\nname='x'\n")
        _write(recipe / "uv.lock", "# lockfile\n")
        _write(recipe / ".env.example", "FOO=1\n")
        _write(recipe / "tests" / "test_runnability.py", "def test(): pass\n")
    if include_agents:
        _write(recipe / "AGENTS.md", "# agents\n")
    return recipe


# ---------------------------------------------------------------------------
# required_files_for
# ---------------------------------------------------------------------------


def _base_policy() -> dict:
    # Mirrors the intended shape of .github/policy.yml — manifest.yaml is
    # NOT in `always`: check 1 in validate_recipe is authoritative for it,
    # and including it here would produce a duplicate error report.
    return {
        "required_files": {
            "always": ["README.md"],
            "by_root": {
                "core": ["AGENTS.md"],
                "contrib": [],
                "plugins": ["SKILL.md", "EVAL.yaml"],
            },
            "by_language": {
                "python": ["pyproject.toml", "uv.lock"],
                "java": [],
            },
        },
        "required_dirs": {
            "always": [],
            "by_root": {
                "core": [],
                "contrib": [],
                "plugins": ["scripts", "assets", "references", "tests/unit"],
            },
            "by_language": {"python": [], "java": []},
        },
        "case_insensitive_files": ["EVAL.yaml"],
    }


def test_required_files_for_core_python():
    # Each entry carries the rule that contributed it — that provenance is
    # the whole answer to "why does MY recipe need this file", and it
    # cannot be recovered from the name alone once the lists are merged.
    assert m.required_files_for(_base_policy(), "core", "python") == [
        ("README.md", "always"),
        ("AGENTS.md", "by_root.core"),
        ("pyproject.toml", "by_language.python"),
        ("uv.lock", "by_language.python"),
    ]


def test_required_files_for_contrib_python():
    # No AGENTS.md — contrib has no by_root files.
    assert m.required_files_for(_base_policy(), "contrib", "python") == [
        ("README.md", "always"),
        ("pyproject.toml", "by_language.python"),
        ("uv.lock", "by_language.python"),
    ]


def test_required_files_for_plugins_no_language():
    assert m.required_files_for(_base_policy(), "plugins", None) == [
        ("README.md", "always"),
        ("SKILL.md", "by_root.plugins"),
        ("EVAL.yaml", "by_root.plugins"),
    ]


def test_required_files_for_unknown_root_and_language():
    # Unknown language falls back to only always + by_root[core].
    assert m.required_files_for(_base_policy(), "core", "brainfuck") == [
        ("README.md", "always"),
        ("AGENTS.md", "by_root.core"),
    ]


def test_required_files_for_missing_config():
    # An empty policy shouldn't crash — it should return an empty list.
    assert m.required_files_for({}, "core", "python") == []


def test_required_files_for_deduplicates():
    # The dedup path is exercised even if the intended policy layout
    # avoids overlaps — someone editing policy.yml could reintroduce one.
    # The FIRST source wins, so the reported rule stays stable.
    policy = {
        "required_files": {
            "always": ["README.md"],
            "by_root": {"core": ["README.md", "AGENTS.md"]},
            "by_language": {"python": ["AGENTS.md", "pyproject.toml"]},
        }
    }
    assert m.required_files_for(policy, "core", "python") == [
        ("README.md", "always"),
        ("AGENTS.md", "by_root.core"),
        ("pyproject.toml", "by_language.python"),
    ]


# ---------------------------------------------------------------------------
# detect_language / is_large_recipe / recipe_root_of
# ---------------------------------------------------------------------------


def test_detect_language_valid(tmp_path):
    manifest = _write(tmp_path / "manifest.yaml", VALID_MANIFEST)
    assert m.detect_language(manifest) == "python"


def test_detect_language_missing_manifest(tmp_path):
    assert m.detect_language(tmp_path / "missing.yaml") is None


def test_detect_language_bad_yaml(tmp_path):
    manifest = _write(tmp_path / "manifest.yaml", "language: [unclosed\n")
    assert m.detect_language(manifest) is None


def test_detect_language_no_language_field(tmp_path):
    manifest = _write(tmp_path / "manifest.yaml", "type: standalone\n")
    assert m.detect_language(manifest) is None


def test_detect_language_lowercases(tmp_path):
    # Schema disallows uppercase, but detect_language should still
    # normalise — the schema check reports the violation separately.
    manifest = _write(tmp_path / "manifest.yaml", "language: PYTHON\n")
    assert m.detect_language(manifest) == "python"


def test_is_large_recipe_true(tmp_path):
    manifest = _write(
        tmp_path / "manifest.yaml",
        VALID_MANIFEST + "large: true\n",
    )
    assert m.is_large_recipe(manifest) is True


def test_is_large_recipe_false_by_default(tmp_path):
    manifest = _write(tmp_path / "manifest.yaml", VALID_MANIFEST)
    assert m.is_large_recipe(manifest) is False


def test_is_large_recipe_missing_manifest(tmp_path):
    assert m.is_large_recipe(tmp_path / "missing.yaml") is False


def test_recipe_root_of_core(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    (tmp_path / "core" / "recipe-a").mkdir(parents=True)
    assert m.recipe_root_of(tmp_path / "core" / "recipe-a") == "core"


def test_recipe_root_of_namespaced(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    (tmp_path / "contrib" / "python" / "recipe-b").mkdir(parents=True)
    assert (
        m.recipe_root_of(tmp_path / "contrib" / "python" / "recipe-b")
        == "contrib"
    )


# ---------------------------------------------------------------------------
# check_folder_name
# ---------------------------------------------------------------------------


def test_check_folder_name_valid(tmp_path):
    d = tmp_path / "my-recipe"
    d.mkdir()
    assert m.check_folder_name(d, max_length=30) == []


def test_check_folder_name_rejects_underscore(tmp_path):
    d = tmp_path / "my_recipe"
    d.mkdir()
    errs = m.check_folder_name(d, max_length=30)
    assert errs and "invalid" in errs[0].what.lower()
    # The fix is a runnable rename, not a restatement of the rule.
    assert "git mv" in errs[0].how and "my-recipe" in errs[0].how


def test_check_folder_name_rejects_uppercase(tmp_path):
    d = tmp_path / "MyRecipe"
    d.mkdir()
    errs = m.check_folder_name(d, max_length=30)
    assert errs and "invalid" in errs[0].what.lower()


def test_check_folder_name_rejects_leading_digit(tmp_path):
    d = tmp_path / "1recipe"
    d.mkdir()
    errs = m.check_folder_name(d, max_length=30)
    assert errs and "invalid" in errs[0].what.lower()


def test_check_folder_name_too_long(tmp_path):
    d = tmp_path / ("x" * 31)
    d.mkdir()
    errs = m.check_folder_name(d, max_length=30)
    assert errs and "31 characters" in errs[0].what
    assert "max_folder_name_length" in errs[0].why
    assert errs[0].doc is Doc.FOLDER_NAME


def test_check_folder_name_both_violations_reported(tmp_path):
    # Invalid AND too long → both errors surface at once so the maintainer
    # doesn't have to iterate twice.
    d = tmp_path / ("X" * 31)
    d.mkdir()
    errs = m.check_folder_name(d, max_length=30)
    assert len(errs) == 2


# ---------------------------------------------------------------------------
# check_required_files
# ---------------------------------------------------------------------------


def test_committed_policy_pairs_every_entry_with_a_rule():
    """Provenance is only useful if it is always there — an entry with a
    blank source would report "Required for every recipe" for a rule that
    is nothing of the kind."""
    policy = m.load_policy()
    for root in ("core", "contrib", "plugins"):
        entries = m.required_files_for(policy, root, "python")
        entries += m.required_dirs_for(policy, root, "python")
        for name, source in entries:
            assert name and source
            assert source in (
                "always",
                f"by_root.{root}",
                "by_language.python",
            ), (name, source)


def test_check_required_files_all_present(tmp_path):
    recipe = _make_python_recipe(tmp_path, "core/foo", include_agents=True)
    policy = _base_policy()
    assert m.check_required_files(recipe, "core", "python", policy) == []


def test_check_required_files_missing_agents_in_core(tmp_path):
    recipe = _make_python_recipe(tmp_path, "core/foo", include_agents=False)
    policy = _base_policy()
    errs = m.check_required_files(recipe, "core", "python", policy)
    assert any("AGENTS.md" in e.what for e in errs)


def test_missing_file_says_which_rule_required_it(tmp_path):
    """ "Required file X is missing" without the rule sends the author to
    policy.yml to work out whether it even applies to their recipe."""
    recipe = _make_python_recipe(tmp_path, "core/foo", include_agents=False)
    (recipe / ".env.example").unlink()
    policy = _full_policy()
    by_name = {
        d.what.split("'")[1]: d
        for d in m.check_required_files(recipe, "core", "python", policy)
    }

    assert "by_root.core" in by_name["AGENTS.md"].why
    assert "under core/" in by_name["AGENTS.md"].why

    env = by_name[".env.example"]
    assert "manifest.language is 'python'" in env.why
    assert "by_language.python" in env.why
    # …and a generator, so the fix is a command, not a research project.
    assert "extract-python-environment-variables" in env.how


def test_missing_uv_lock_gives_the_exact_command(isolated_repo):
    recipe = _make_python_recipe(isolated_repo, "core/foo", include_agents=True)
    (recipe / "uv.lock").unlink()
    errs = m.check_required_files(recipe, "core", "python", _full_policy())
    (lock,) = [d for d in errs if "uv.lock" in d.what]
    assert lock.how == "cd core/foo && uv lock"


def test_a_required_file_with_no_generator_points_at_the_checklist(tmp_path):
    recipe = _make_python_recipe(tmp_path, "core/foo", include_agents=True)
    policy = _full_policy()
    policy["required_files"]["always"] = ["ARCHITECTURE.md"]
    (diag,) = m.check_required_files(recipe, "core", None, policy)
    assert "docs/recipe-checklist.md" in diag.how


def test_check_required_files_contrib_does_not_require_agents(tmp_path):
    recipe = _make_python_recipe(tmp_path, "contrib/foo", include_agents=False)
    policy = _base_policy()
    assert m.check_required_files(recipe, "contrib", "python", policy) == []


def test_check_required_files_nested_path(tmp_path):
    # tests/test_runnability.py is a nested requirement — the checker
    # must look at the actual file, not just the directory.
    recipe = _make_python_recipe(tmp_path, "core/foo", include_agents=True)
    # Remove the file but keep tests/ as a directory.
    (recipe / "tests" / "test_runnability.py").unlink()
    policy = _base_policy()
    policy["required_files"]["by_language"]["python"].append(
        "tests/test_runnability.py"
    )
    errs = m.check_required_files(recipe, "core", "python", policy)
    assert any("tests/test_runnability.py" in e.what for e in errs)


def test_check_required_files_directory_does_not_satisfy(tmp_path):
    # If a required file exists as a DIRECTORY, that must still count as
    # missing (is_file() guards this). Use AGENTS.md so the check runs
    # even with manifest.yaml removed from `always` in the real policy.
    recipe = tmp_path / "core" / "foo"
    (recipe / "AGENTS.md").mkdir(parents=True)
    _write(recipe / "README.md", "# x\n")
    policy = _base_policy()
    errs = m.check_required_files(recipe, "core", None, policy)
    assert any("AGENTS.md" in e.what and "directory" in e.what for e in errs)


# ---------------------------------------------------------------------------
# check_size_and_count
# ---------------------------------------------------------------------------


def _size_policy(max_files: int = 10, max_size_mb: int = 1) -> dict:
    """Minimal policy with tight limits so tests can trip them with
    just a few files."""
    return {
        "recipe_size_limits": {
            "core": {
                "default": {"max_files": max_files, "max_size_mb": max_size_mb},
                "large": {"max_files": 1000, "max_size_mb": 100},
            },
            "contrib": {
                "default": {"max_files": max_files, "max_size_mb": max_size_mb},
            },
        },
        "excluded_paths": {
            "python": {"dirs": ["__pycache__"], "files": ["uv.lock"]},
        },
    }


def test_check_size_and_count_under_limits(tmp_path):
    recipe = _make_python_recipe(tmp_path, "core/foo", include_agents=True)
    manifest = recipe / "manifest.yaml"
    assert (
        m.check_size_and_count(recipe, "core", _size_policy(), manifest) == []
    )


def test_check_size_and_count_exceeds_file_count(tmp_path):
    recipe = _make_python_recipe(tmp_path, "core/foo", include_agents=True)
    manifest = recipe / "manifest.yaml"
    # Add enough files to exceed max_files=10 (recipe already has ~6).
    for i in range(20):
        _write(recipe / "generated" / f"extra_{i}.py", "# noop\n")
    (diag,) = m.check_size_and_count(recipe, "core", _size_policy(), manifest)
    assert "the limit is 10" in diag.what
    # Which directory is responsible — a total alone leaves the author to
    # go and find that out with `find | wc -l`.
    assert "generated" in diag.how
    assert "20 files" in diag.how
    assert "core/default tier" in diag.why
    assert "large: true" in diag.how


def test_check_size_and_count_exceeds_size(tmp_path):
    recipe = _make_python_recipe(tmp_path, "core/foo", include_agents=True)
    manifest = recipe / "manifest.yaml"
    # Write a single 2 MiB blob; limit is 1 MiB.
    _write(recipe / "blob.bin", "x" * (2 * 1024 * 1024))
    (diag,) = m.check_size_and_count(recipe, "core", _size_policy(), manifest)
    assert "2.0 MB" in diag.what and "limit is 1 MB" in diag.what
    # The walk already knew the offending path and its size.
    assert "blob.bin" in diag.how
    assert "recipe_size_limits.core.default.max_size_mb = 1" in diag.why
    assert "large: true" in diag.how
    assert diag.doc is Doc.SIZE_LIMIT


def test_size_message_names_the_five_largest_and_no_more(tmp_path):
    recipe = _make_python_recipe(tmp_path, "core/foo", include_agents=True)
    manifest = recipe / "manifest.yaml"
    for i in range(8):
        _write(recipe / f"blob_{i}.bin", "x" * (300 * 1024))
    (diag,) = m.check_size_and_count(
        recipe, "core", _size_policy(max_files=100), manifest
    )
    listed = [f"blob_{i}.bin" for i in range(8) if f"blob_{i}.bin" in diag.how]
    assert len(listed) == 5


def test_large_tier_message_says_there_is_no_higher_tier(tmp_path):
    recipe = _make_python_recipe(
        tmp_path,
        "core/foo",
        manifest=VALID_MANIFEST + "large: true\n",
        include_agents=True,
    )
    manifest = recipe / "manifest.yaml"
    policy = _size_policy()
    policy["recipe_size_limits"]["core"]["large"] = {
        "max_files": 1000,
        "max_size_mb": 1,
    }
    _write(recipe / "blob.bin", "x" * (2 * 1024 * 1024))
    (diag,) = m.check_size_and_count(recipe, "core", policy, manifest)
    assert "core/large tier" in diag.why
    assert "no higher one" in diag.how


def test_check_size_and_count_excludes_uv_lock(tmp_path):
    recipe = _make_python_recipe(tmp_path, "core/foo", include_agents=True)
    manifest = recipe / "manifest.yaml"
    # Write a 2 MiB uv.lock (excluded) — should NOT trip the size limit.
    _write(recipe / "uv.lock", "y" * (2 * 1024 * 1024))
    assert (
        m.check_size_and_count(recipe, "core", _size_policy(), manifest) == []
    )


def test_check_size_and_count_excludes_pycache_dir(tmp_path):
    recipe = _make_python_recipe(tmp_path, "core/foo", include_agents=True)
    manifest = recipe / "manifest.yaml"
    # A pruned dir with many files must not count toward the file limit.
    for i in range(50):
        _write(recipe / "app" / "__pycache__" / f"m_{i}.pyc", "x")
    assert (
        m.check_size_and_count(recipe, "core", _size_policy(), manifest) == []
    )


def test_check_size_and_count_large_tier_relaxes(tmp_path):
    # A recipe that would fail under `default` should pass under `large`
    # when the manifest opts in.
    recipe = _make_python_recipe(
        tmp_path,
        "core/foo",
        manifest=VALID_MANIFEST + "large: true\n",
        include_agents=True,
    )
    manifest = recipe / "manifest.yaml"
    for i in range(50):
        _write(recipe / f"extra_{i}.py", "x")
    assert (
        m.check_size_and_count(recipe, "core", _size_policy(), manifest) == []
    )


def test_check_size_and_count_root_without_limits_skips(tmp_path):
    # A root with no section in recipe_size_limits → nothing to enforce.
    # Uses a fictional root because core, contrib and plugins all have real
    # limits in .github/policy.yml now, so none of them demonstrates this.
    recipe = _make_python_recipe(
        tmp_path, "playground/foo", include_agents=False
    )
    manifest = recipe / "manifest.yaml"
    assert (
        m.check_size_and_count(recipe, "playground", _size_policy(), manifest)
        == []
    )


def test_check_size_and_count_missing_large_tier_fails(tmp_path):
    # If a recipe opts into `large: true` but the root has no `large` tier
    # in policy.yml, the check must FAIL with a clear message (previously
    # it silently fell back to `default`, leaving the author unaware).
    recipe = _make_python_recipe(
        tmp_path,
        "core/foo",
        manifest=VALID_MANIFEST + "large: true\n",
        include_agents=True,
    )
    manifest = recipe / "manifest.yaml"
    # Policy with `default` but no `large` tier for core.
    policy = {
        "recipe_size_limits": {
            "core": {"default": {"max_files": 50, "max_size_mb": 5}}
        }
    }
    (diag,) = m.check_size_and_count(recipe, "core", policy, manifest)
    assert "manifest.large is true" in diag.what
    assert "no 'large' size tier" in diag.what
    # Both ways out, named: drop the opt-in, or have policy.yml grow one.
    assert "Remove `large: true`" in diag.how
    assert "core.large" in diag.how


def test_check_size_and_count_missing_both_tiers_is_a_noop(tmp_path):
    # Edge case: root_limits exists but has neither `default` nor `large`.
    # A `large: true` request without a matching tier still fails; a
    # default (no manifest.large) request finds nothing to enforce and
    # returns [] — same shape as "no root_limits at all".
    recipe = _make_python_recipe(tmp_path, "core/foo", include_agents=True)
    manifest = recipe / "manifest.yaml"
    policy = {"recipe_size_limits": {"core": {}}}
    assert m.check_size_and_count(recipe, "core", policy, manifest) == []


# ---------------------------------------------------------------------------
# validate_recipe (end-to-end orchestration on a fake tree)
# ---------------------------------------------------------------------------


def _full_policy() -> dict:
    """Realistic policy combining required_files + size limits +
    excluded paths — mirrors the shape of the committed policy.yml."""
    p = _base_policy()
    p.update(_size_policy(max_files=200, max_size_mb=50))
    p["recipe_naming"] = {"max_folder_name_length": 30}
    p["required_files"]["by_language"]["python"] = [
        "pyproject.toml",
        "uv.lock",
        ".env.example",
        "tests/test_runnability.py",
    ]
    return p


@pytest.fixture
def isolated_repo(tmp_path, monkeypatch):
    """Point validate_structure's REPO_ROOT at a scratch dir so the
    recipe_root_of() calls used by validate_recipe/main resolve
    correctly. Independent of the `fake_repo` fixture below, which
    additionally writes a policy.yml for main() to load."""
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    return tmp_path


def test_validate_recipe_full_pass(isolated_repo):
    recipe = _make_python_recipe(
        isolated_repo, "core/good", include_agents=True
    )
    schema = vm.load_schema()
    assert m.validate_recipe(recipe, _full_policy(), schema) == []


def test_validate_recipe_missing_manifest_reports_and_skips_schema(
    isolated_repo,
):
    recipe = _make_python_recipe(
        isolated_repo, "core/bad", manifest=None, include_agents=True
    )
    schema = vm.load_schema()
    errs = m.validate_recipe(recipe, _full_policy(), schema)
    # Missing manifest is reported…
    assert any(e.check == "manifest-missing" for e in errs)
    # …and required files that depend on the manifest's language are
    # NOT reported (we don't know the language, so we skip that source).
    # But `always` + `by_root[core]` files still apply.
    # AGENTS.md is present, README is present, so no other errors expected
    # beyond the manifest itself.
    assert ".env.example" not in _blob(errs)


def test_validate_recipe_invalid_manifest_still_runs_other_checks(
    isolated_repo,
):
    # Manifest exists but is schema-invalid (bad type). detect_language
    # still returns "python", so by_language files are still enforced.
    bad_manifest = textwrap.dedent(
        """\
        type: not_a_valid_type
        status: active
        language: python
        description: A valid description here.
        ownership:
          team: My Team
          poc: my-github-id
        """
    )
    recipe = _make_python_recipe(
        isolated_repo, "core/foo", manifest=bad_manifest, include_agents=True
    )
    # Remove .env.example so we can assert the language-based check ran.
    (recipe / ".env.example").unlink()
    schema = vm.load_schema()
    errs = m.validate_recipe(recipe, _full_policy(), schema)
    assert any(e.check.startswith("manifest") for e in errs)
    assert ".env.example" in _blob(errs)


def test_validate_recipe_bad_folder_name(isolated_repo):
    recipe = _make_python_recipe(
        isolated_repo, "core/Bad_Name", include_agents=True
    )
    schema = vm.load_schema()
    errs = m.validate_recipe(recipe, _full_policy(), schema)
    assert any(e.check == "folder-name" for e in errs)


def test_validate_recipe_missing_agents_in_core(isolated_repo):
    recipe = _make_python_recipe(
        isolated_repo, "core/foo", include_agents=False
    )
    schema = vm.load_schema()
    errs = m.validate_recipe(recipe, _full_policy(), schema)
    assert "AGENTS.md" in _blob(errs)


def test_validate_recipe_outside_known_roots(isolated_repo):
    # A recipe under some other top-level dir the checker doesn't recognise.
    recipe = _make_python_recipe(
        isolated_repo, "playground/foo", include_agents=True
    )
    schema = vm.load_schema()
    errs = m.validate_recipe(recipe, _full_policy(), schema)
    assert len(errs) == 1
    assert errs[0].check == "placement"
    assert "core/" in errs[0].how and "plugins/" in errs[0].how


def test_every_diagnostic_carries_a_working_doc_anchor(isolated_repo):
    """Every path out of validate_recipe must hand back somewhere to go
    for more than one line of context."""
    recipe = _make_python_recipe(
        isolated_repo, "core/Bad_Name", manifest=None, include_agents=False
    )
    (recipe / "uv.lock").unlink()
    errs = m.validate_recipe(recipe, _full_policy(), vm.load_schema())
    assert errs
    for diag in errs:
        assert isinstance(diag.doc, Doc)
        assert diag.doc.url.startswith("docs/recipe-handbook/")


# ---------------------------------------------------------------------------
# main (end-to-end against a fake tree)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """A fake repo with a mix of good and bad recipes so main() has
    something to iterate over."""
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(m, "POLICY_PATH", tmp_path / ".github" / "policy.yml")
    monkeypatch.setattr(vm, "REPO_ROOT", tmp_path)
    # Write a policy.yml matching _full_policy() for main() to load.
    import yaml as _yaml

    _write(tmp_path / ".github" / "policy.yml", _yaml.safe_dump(_full_policy()))
    return tmp_path


def test_main_all_recipes_pass(fake_repo):
    _make_python_recipe(fake_repo, "core/good-a", include_agents=True)
    _make_python_recipe(fake_repo, "core/good-b", include_agents=True)
    assert m.main("core") == 0


def test_main_returns_one_on_failure(fake_repo, capsys):
    _make_python_recipe(fake_repo, "core/good", include_agents=True)
    _make_python_recipe(fake_repo, "core/broken", include_agents=False)
    assert m.main("core") == 1
    out = capsys.readouterr().out
    # The annotation points at the missing file itself, not the recipe root.
    assert "::error file=core/broken/AGENTS.md::" in out
    assert "AGENTS.md" in out


def test_main_annotates_every_problem_not_just_the_first(fake_repo, capsys):
    """The old output collapsed a recipe's problems into one annotation
    plus "(+N more)", so the Files tab was a teaser for the job log."""
    recipe = _make_python_recipe(fake_repo, "core/broken", include_agents=False)
    (recipe / "uv.lock").unlink()
    (recipe / ".env.example").unlink()

    assert m.main("core") == 1

    out = capsys.readouterr().out
    assert "(+" not in out
    for missing in ("AGENTS.md", "uv.lock", ".env.example"):
        assert f"::error file=core/broken/{missing}::" in out


def test_main_footer_leads_with_the_authoring_docs(fake_repo, capsys):
    """policy.yml and the JSON schema are enforcement artifacts — the
    wrong audience for someone who just wants to fix their recipe."""
    _make_python_recipe(fake_repo, "core/broken", include_agents=False)
    assert m.main("core") == 1
    out = capsys.readouterr().out
    handbook = out.index("docs/recipe-handbook/README.md")
    checklist = out.index("docs/recipe-checklist.md")
    policy = out.index(".github/policy.yml")
    assert handbook < policy and checklist < policy


def test_main_single_recipe_scope(fake_repo):
    _make_python_recipe(fake_repo, "core/only", include_agents=True)
    assert m.main("core/only") == 0


# ---------------------------------------------------------------------------
# required_dirs_for — same three-way union as required_files_for
# ---------------------------------------------------------------------------


def test_required_dirs_for_plugins():
    assert m.required_dirs_for(_base_policy(), "plugins", "python") == [
        ("scripts", "by_root.plugins"),
        ("assets", "by_root.plugins"),
        ("references", "by_root.plugins"),
        ("tests/unit", "by_root.plugins"),
    ]


def test_required_dirs_for_core_is_empty():
    assert m.required_dirs_for(_base_policy(), "core", "python") == []


def test_required_dirs_for_missing_section_degrades():
    assert m.required_dirs_for({}, "plugins", "python") == []


def test_required_dirs_for_dedupes():
    policy = {
        "required_dirs": {
            "always": ["scripts"],
            "by_root": {"plugins": ["scripts", "assets"]},
            "by_language": {"python": ["assets"]},
        }
    }
    assert m.required_dirs_for(policy, "plugins", "python") == [
        ("scripts", "always"),
        ("assets", "by_root.plugins"),
    ]


# ---------------------------------------------------------------------------
# Vertical plugins — the full file + directory contract
# ---------------------------------------------------------------------------

PLUGIN_MANIFEST = VALID_MANIFEST


def _make_plugin(root: Path, rel: str = "plugins/retail/store-ops") -> Path:
    """A complete, valid Python vertical plugin: every required file and
    every required directory."""
    plugin = root / rel
    plugin.mkdir(parents=True, exist_ok=True)
    _write(plugin / "manifest.yaml", PLUGIN_MANIFEST)
    _write(plugin / "README.md", "# plugin\n")
    _write(plugin / "SKILL.md", "# installer\n")
    _write(plugin / "EVAL.yaml", "rubrics: []\n")
    _write(plugin / "pyproject.toml", "[project]\nname='x'\n")
    _write(plugin / "uv.lock", "# lockfile\n")
    _write(plugin / ".env.example", "FOO=1\n")
    _write(plugin / "tests" / "test_runnability.py", "def test(): pass\n")
    for d in ("scripts", "assets", "references", "tests/unit"):
        (plugin / d).mkdir(parents=True, exist_ok=True)
    return plugin


def test_complete_plugin_passes(isolated_repo):
    plugin = _make_plugin(isolated_repo)
    schema = vm.load_schema()
    assert m.validate_recipe(plugin, _full_policy(), schema) == []


def test_plugin_missing_a_required_dir_fails(isolated_repo):
    plugin = _make_plugin(isolated_repo)
    (plugin / "assets").rmdir()
    errors = m.validate_recipe(plugin, _full_policy(), vm.load_schema())
    (diag,) = [d for d in errors if d.check == "required-dirs"]
    assert "assets/" in diag.what
    # The overwhelmingly common report is "the folder is right there" —
    # so the fix has to lead with git's inability to commit an empty one.
    assert "git cannot commit an empty directory" in diag.how
    assert (
        "touch plugins/retail/store-ops/assets/.gitkeep && "
        "git add plugins/retail/store-ops/assets/.gitkeep" in diag.how
    )


def test_missing_dir_says_which_rule_required_it(isolated_repo):
    plugin = _make_plugin(isolated_repo)
    (plugin / "scripts").rmdir()
    (diag,) = m.check_required_dirs(plugin, "plugins", "python", _full_policy())
    assert "under plugins/" in diag.why
    assert "policy.required_dirs.by_root.plugins" in diag.why


def test_plugin_missing_eval_yaml_fails(isolated_repo):
    plugin = _make_plugin(isolated_repo)
    (plugin / "EVAL.yaml").unlink()
    errors = m.validate_recipe(plugin, _full_policy(), vm.load_schema())
    assert "EVAL.yaml" in _blob(errors)


def test_empty_required_dirs_pass(isolated_repo):
    """assets/ and references/ are legitimately empty for some plugins."""
    plugin = _make_plugin(isolated_repo)
    assert list((plugin / "assets").iterdir()) == []
    assert (
        m.check_required_dirs(plugin, "plugins", "python", _full_policy()) == []
    )


def test_required_dir_that_is_actually_a_file_is_reported_precisely(
    isolated_repo,
):
    """'missing' would send the author hunting for something that is
    right there under the wrong kind."""
    plugin = _make_plugin(isolated_repo)
    (plugin / "scripts").rmdir()
    _write(plugin / "scripts", "oops\n")
    errors = m.check_required_dirs(plugin, "plugins", "python", _full_policy())
    assert len(errors) == 1
    assert "exists but is a file" in errors[0].what


# ---------------------------------------------------------------------------
# Case handling — must not depend on the host filesystem
# ---------------------------------------------------------------------------


def test_wrong_case_fails_for_a_strict_entry(isolated_repo):
    """pyproject.toml is read by uv, which resolves it by exact name.
    Accepting PyProject.toml would pass here and then break uv. This must
    fail on macOS too, where the filesystem alone would accept it."""
    plugin = _make_plugin(isolated_repo)
    (plugin / "pyproject.toml").unlink()
    _write(plugin / "PyProject.toml", "[project]\nname='x'\n")
    errors = m.check_required_files(plugin, "plugins", "python", _full_policy())
    assert any(
        "pyproject.toml" in e.what and "missing" in e.what for e in errors
    )


def test_wrong_case_passes_for_eval_yaml_with_a_note(isolated_repo, capsys):
    plugin = _make_plugin(isolated_repo)
    (plugin / "EVAL.yaml").unlink()
    _write(plugin / "eval.yaml", "rubrics: []\n")
    assert (
        m.check_required_files(plugin, "plugins", "python", _full_policy())
        == []
    )
    out = capsys.readouterr().out
    assert "[NOTE]" in out
    assert "eval.yaml" in out
    assert "EVAL.yaml" in out


def test_exact_case_produces_no_note(isolated_repo, capsys):
    plugin = _make_plugin(isolated_repo)
    assert (
        m.check_required_files(plugin, "plugins", "python", _full_policy())
        == []
    )
    assert "[NOTE]" not in capsys.readouterr().out


def test_find_entry_compares_names_in_python(tmp_path):
    """Pins the platform-independence: the match is decided by comparing
    names, never by asking the filesystem, so the verdict is the same on a
    case-insensitive macOS volume and on Linux CI."""
    _write(tmp_path / "EVAL.yaml", "x")
    assert m._find_entry(tmp_path, "EVAL.yaml", case_insensitive=False)[0]
    assert m._find_entry(tmp_path, "eval.yaml", case_insensitive=False) == (
        None,
        False,
    )
    found, exact = m._find_entry(tmp_path, "eval.yaml", case_insensitive=True)
    assert found is not None and exact is False


def test_find_entry_walks_nested_segments(tmp_path):
    _write(tmp_path / "tests" / "unit" / "test_x.py", "x")
    found, exact = m._find_entry(tmp_path, "tests/unit", case_insensitive=False)
    assert found is not None and found.is_dir() and exact is True
    assert m._find_entry(tmp_path, "tests/Unit", case_insensitive=False) == (
        None,
        False,
    )


def test_case_insensitive_entries_reads_policy():
    assert m.case_insensitive_entries(_full_policy()) == {"EVAL.yaml"}
    assert m.case_insensitive_entries({}) == set()


# ---------------------------------------------------------------------------
# Guards on the committed policy.yml
# ---------------------------------------------------------------------------


def test_committed_policy_declares_the_plugin_contract():
    """A future edit must not silently drop part of the contract."""
    policy = m.load_policy()
    files = _names(m.required_files_for(policy, "plugins", "python"))
    dirs = _names(m.required_dirs_for(policy, "plugins", "python"))
    for f in (
        "README.md",
        "SKILL.md",
        "EVAL.yaml",
        "pyproject.toml",
        "uv.lock",
        "tests/test_runnability.py",
    ):
        assert f in files, f
    # scripts/ is the only required DIRECTORY: an empty directory passes,
    # so a rule satisfied by a .gitkeep enforces nothing. assets/,
    # references/ and tests/unit/ remain the convention and are still
    # created by the scaffold — see the comment above required_dirs in
    # .github/policy.yml.
    assert dirs == ["scripts"]


def test_committed_policy_keeps_tool_read_files_case_strict():
    """These are resolved by exact name by uv, pytest and our own tooling;
    relaxing them would pass CI and then break the tool that reads them."""
    lenient = m.case_insensitive_entries(m.load_policy())
    for f in (
        "pyproject.toml",
        "uv.lock",
        "manifest.yaml",
        "tests/test_runnability.py",
    ):
        assert f not in lenient, f
