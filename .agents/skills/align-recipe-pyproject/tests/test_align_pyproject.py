# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for the align-recipe-pyproject skill script.

Covers the two checks added after a vertical plugin under plugins/ showed that
raising `requires-python` silently desynced the recipe's own bootstrap script,
and that a narrow `testpaths` can exclude the required runnability test.
"""

from pathlib import Path

import align_pyproject as m
import pytest
import tomlkit


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _check(recipe_dir: Path) -> m.Check:
    return m.check_stale_python_version_refs(recipe_dir)


# ---------------------------------------------------------------------------
# stale-python-version-refs
# ---------------------------------------------------------------------------


def test_clean_recipe_reports_ok(tmp_path):
    _write(tmp_path / "README.md", "- Python 3.11+\n")
    _write(tmp_path / "pyproject.toml", 'requires-python = ">=3.11"\n')
    assert _check(tmp_path).status == m.OK


def test_flags_prose_version_claim(tmp_path):
    _write(tmp_path / "README.md", "# R\n\n- Python 3.10+\n")
    check = _check(tmp_path)
    assert check.status == m.REPORT_ONLY
    assert check.details["files"] == ["README.md"]


def test_flags_interpreter_allowlist_in_shell_script(tmp_path):
    # The case that actually broke a recipe: bootstrap.sh would still build a
    # 3.10 venv that the recipe then refuses to install into.
    _write(
        tmp_path / "scripts" / "bootstrap.sh",
        "for py in python3.12 python3.11 python3.10 python3; do\n",
    )
    check = _check(tmp_path)
    assert check.status == m.REPORT_ONLY
    assert check.details["files"] == ["scripts/bootstrap.sh"]


def test_flags_version_specifier_syntax(tmp_path):
    _write(tmp_path / "TROUBLE.md", "not in '>=3.10'\n")
    assert _check(tmp_path).status == m.REPORT_ONLY


def test_does_not_flag_model_names(tmp_path):
    # "gemini-3.5-flash" contains "3.5" but is not a Python version claim.
    # A context-free digit scan would drown the report in these.
    _write(
        tmp_path / "agent.py",
        'MODEL = "gemini-3.5-flash"\nOTHER = "gemini-2.0-pro"\n',
    )
    assert _check(tmp_path).status == m.OK


def test_does_not_flag_versions_at_or_above_floor(tmp_path):
    _write(
        tmp_path / "bootstrap.sh",
        "for py in python3.13 python3.12 python3.11; do\n",
    )
    assert _check(tmp_path).status == m.OK


def test_ignores_requires_python_line_in_pyproject(tmp_path):
    # Owned by the python-version-floor check, which rewrites it in memory
    # and persists at the end of run(); reporting it here would be a false
    # positive in apply mode.
    _write(tmp_path / "pyproject.toml", 'requires-python = ">=3.10"\n')
    assert _check(tmp_path).status == m.OK


def test_still_flags_other_pyproject_lines(tmp_path):
    _write(
        tmp_path / "pyproject.toml",
        'requires-python = ">=3.10"\n'
        'classifiers = ["Programming Language :: Python :: 3.10"]\n',
    )
    check = _check(tmp_path)
    assert check.status == m.REPORT_ONLY
    assert check.details["hits"]["pyproject.toml"][0]["line"] == 2


def test_skips_lockfiles_and_venvs(tmp_path):
    _write(tmp_path / "uv.lock", 'requires-python = ">=3.10"\n')
    _write(tmp_path / ".venv" / "x.py", "# python3.9\n")
    _write(tmp_path / "__pycache__" / "y.txt", "python3.9\n")
    assert _check(tmp_path).status == m.OK


def test_binary_file_does_not_crash_the_scan(tmp_path):
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    _write(tmp_path / "README.md", "- Python 3.10+\n")
    check = _check(tmp_path)
    assert check.status == m.REPORT_ONLY
    assert check.details["files"] == ["README.md"]


def test_hits_are_truncated_but_file_list_is_not(tmp_path):
    body = "\n".join(f"python3.10 line {i}" for i in range(60))
    _write(tmp_path / "many.txt", body)
    check = _check(tmp_path)
    assert check.details["truncated"] is True
    assert check.details["total"] == 60
    reported = sum(len(v) for v in check.details["hits"].values())
    assert reported == m._MAX_REPORTED_REFS
    assert check.details["files"] == ["many.txt"]


# ---------------------------------------------------------------------------
# runnability-test-in-testpaths
# ---------------------------------------------------------------------------


def _testpaths_check(toml: str) -> m.Check:
    return m.check_runnability_test_in_testpaths(tomlkit.parse(toml))


def test_no_testpaths_is_ok():
    assert _testpaths_check('[project]\nname = "x"\n').status == m.OK


def test_narrow_testpaths_is_reported():
    check = _testpaths_check(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests/unit", "tests/integration"]\n'
    )
    assert check.status == m.REPORT_ONLY
    assert check.details["testpaths"] == ["tests/unit", "tests/integration"]


def test_tests_dir_in_testpaths_is_ok():
    check = _testpaths_check(
        '[tool.pytest.ini_options]\ntestpaths = ["tests", "docs"]\n'
    )
    assert check.status == m.OK


def test_trailing_slash_is_normalised():
    check = _testpaths_check(
        '[tool.pytest.ini_options]\ntestpaths = ["tests/"]\n'
    )
    assert check.status == m.OK


def test_dot_testpath_is_ok():
    check = _testpaths_check('[tool.pytest.ini_options]\ntestpaths = ["."]\n')
    assert check.status == m.OK


def test_explicit_runnability_path_is_ok():
    check = _testpaths_check(
        '[tool.pytest.ini_options]\ntestpaths = ["tests/test_runnability.py"]\n'
    )
    assert check.status == m.OK


def test_string_testpaths_is_handled():
    check = _testpaths_check(
        '[tool.pytest.ini_options]\ntestpaths = "tests/unit"\n'
    )
    assert check.status == m.REPORT_ONLY


# ---------------------------------------------------------------------------
# Both checks are wired into run()
# ---------------------------------------------------------------------------


def test_new_checks_appear_in_report(tmp_path):
    _write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "alrec"\ndescription = "d"\n'
        'requires-python = ">=3.11"\n'
        '[tool.pytest.ini_options]\ntestpaths = ["tests/unit"]\n',
    )
    _write(tmp_path / "README.md", "- Python 3.10+\n")

    report = m.run(tmp_path, dry_run=True, description_source=None)
    by_id = {c.id: c for c in report.checks}

    assert by_id["stale-python-version-refs"].status == m.REPORT_ONLY
    assert by_id["runnability-test-in-testpaths"].status == m.REPORT_ONLY


# ---------------------------------------------------------------------------
# project-name-matches-folder — expected name derivation
#
# core/ and contrib/ derive the name from the folder basename. plugins/ joins
# the vertical namespace to it, because plugins/<vertical>/<solution> makes the
# basename non-unique across verticals (plugins/retail/product-search and
# plugins/grocery/product-search would otherwise both want "product-search").
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # Language-namespaced roots — basename wins.
        ("core/python/deep-search", "deep-search"),
        ("contrib/python/financial-advisor", "financial-advisor"),
        # Legacy flat layout still present under core/.
        ("core/rag-vector-search", "rag-vector-search"),
        # Vertical-namespaced root — vertical is joined in.
        ("plugins/retail/product-search", "retail-product-search"),
        ("plugins/hr/onboarding", "hr-onboarding"),
        ("plugins/finance/month-end-close", "finance-month-end-close"),
        # A bare directory name has no root to inspect.
        ("product-search", "product-search"),
        # Deeper than <root>/<namespace>/<solution>, so not the namespaced
        # shape. validate_placement.py rejects this layout anyway.
        ("plugins/retail/deep/nested", "nested"),
    ],
)
def test_expected_project_name_for_repo_relative_paths(path, expected):
    assert m.expected_project_name(Path(path)) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "/w/adk-samples/plugins/retail/product-search",
            "retail-product-search",
        ),
        ("/w/adk-samples/core/python/deep-search", "deep-search"),
    ],
)
def test_absolute_paths_inside_the_repo_resolve_the_same(path, expected):
    root = Path("/w/adk-samples")
    assert m.expected_project_name(Path(path), repo_root=root) == expected


@pytest.mark.parametrize(
    "path",
    [
        # A checkout directory that merely happens to be NAMED "plugins".
        # Matching the third-from-last segment made this script WRITE
        # [project].name = "core-rag-vector-search" into a real pyproject.
        "/home/me/plugins/core/rag-vector-search",
        # Right shape, wrong repository.
        "/elsewhere/plugins/retail/rag-vector-search",
    ],
)
def test_paths_outside_the_repo_fall_back_to_basename(path):
    root = Path("/w/adk-samples")
    assert (
        m.expected_project_name(Path(path), repo_root=root)
        == "rag-vector-search"
    )


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Treat tmp_path as the repository root.

    The rule is a POSITION test against the repo root, so a recipe built
    under tmp_path is outside the real repo and would correctly fall back to
    its basename. Re-pointing REPO_ROOT is what lets these tests exercise the
    namespaced branch at all.
    """
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    return tmp_path


def _name_check(recipe_dir: Path, name: str | None, apply: bool) -> m.Check:
    body = "[project]\n" + (f'name = "{name}"\n' if name is not None else "")
    doc = tomlkit.parse(body)
    return m.check_project_name_matches_folder(recipe_dir, doc, apply), doc


def test_skill_with_vertical_prefixed_name_is_ok(fake_repo):
    recipe = fake_repo / "plugins" / "retail" / "product-search"
    check, _ = _name_check(recipe, "retail-product-search", apply=False)
    assert check.status == m.OK


def test_skill_with_bare_basename_is_rewritten(fake_repo):
    """The pre-fix behaviour (bare basename) is now a violation, and the
    auto-fix promotes it to the vertical-prefixed form rather than the other
    way around."""
    recipe = fake_repo / "plugins" / "retail" / "product-search"
    check, doc = _name_check(recipe, "product-search", apply=True)
    assert check.status == m.FIXED
    assert check.details == {
        "from": "product-search",
        "to": "retail-product-search",
    }
    assert doc["project"]["name"] == "retail-product-search"


def test_skill_dry_run_message_explains_the_vertical_rule(fake_repo):
    recipe = fake_repo / "plugins" / "retail" / "product-search"
    check, _ = _name_check(recipe, "product-search", apply=False)
    assert check.status == m.WOULD_FIX
    assert "<vertical>-<solution>" in check.message


def test_core_recipe_message_still_says_basename(fake_repo):
    recipe = fake_repo / "core" / "python" / "deep-search"
    check, _ = _name_check(recipe, "wrong", apply=False)
    assert check.status == m.WOULD_FIX
    assert "recipe folder basename" in check.message
    assert "<vertical>" not in check.message


def test_missing_name_is_added_with_vertical_prefix(fake_repo):
    recipe = fake_repo / "plugins" / "retail" / "product-search"
    check, doc = _name_check(recipe, None, apply=True)
    assert check.status == m.FIXED
    assert doc["project"]["name"] == "retail-product-search"
