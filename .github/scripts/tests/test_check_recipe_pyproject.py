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
"""Unit tests for check_recipe_pyproject.py.

This script gates real PRs. The naming tests pin the
`skills/<vertical>/<solution>` behaviour so a future refactor cannot
silently reintroduce the basename-only rule, which would force two verticals
that ship a same-named solution to declare the same distribution name.

The rule is implemented twice on purpose — the read-only validator here and
the comment-preserving auto-fixer in
.agents/skills/align-recipe-pyproject/scripts/align_pyproject.py. The two
implementations are cross-checked at the bottom of this file.

The rest of the file pins the exit-code contract the workflow reads, and the
malformed-input cases that used to escape as an AttributeError traceback —
which the workflow then reported as the contributor's error.
"""

import sys
from pathlib import Path

import check_recipe_pyproject as m
import pytest

# The numbers the workflow branches on. Spelled out rather than imported so
# a change to them has to be made deliberately in two places.
EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_CI_FAULT = 2

# Where actions/checkout puts the repo on a GitHub runner. The workflow hands
# the script a repo-relative path, but a developer debugging by hand may pass
# an absolute one, so both must derive the same name.
CI_CHECKOUT = "/home/runner/work/adk-samples/adk-samples"


def _text(diagnostic) -> str:
    """Everything the contributor reads, as one searchable string."""
    return diagnostic.render_human()


def _run(tmp_path: Path, monkeypatch, pyproject: str, manifest=None) -> int:
    """Invoke the script the way CI does, on a throwaway recipe."""
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    if manifest is not None:
        (tmp_path / "manifest.yaml").write_text(manifest, encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_pyproject.py", str(tmp_path)]
    )
    return m.main()


# ---------------------------------------------------------------------------
# expected_project_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # Language-namespaced roots — basename wins.
        ("core/python/deep-search", "deep-search"),
        ("contrib/python/financial-advisor", "financial-advisor"),
        # Legacy flat layout still present under core/.
        ("core/rag-vector-search", "rag-vector-search"),
        # Vertical-namespaced root — the vertical is joined in.
        ("skills/retail/product-search", "retail-product-search"),
        ("skills/hr/onboarding", "hr-onboarding"),
        ("skills/finance/month-end-close", "finance-month-end-close"),
        # A bare directory name has no root to inspect.
        ("product-search", "product-search"),
        # Deeper than <root>/<namespace>/<solution>. Not three segments from
        # the root, so it is not the namespaced shape.
        # tools/validate_placement.py rejects this layout anyway.
        ("skills/retail/deep/nested", "nested"),
    ],
)
def test_expected_project_name_for_repo_relative_paths(path, expected):
    """CI always passes repo-relative paths — see tools/affected_recipes.py."""
    assert m.expected_project_name(Path(path)) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            f"{CI_CHECKOUT}/skills/retail/product-search",
            "retail-product-search",
        ),
        (f"{CI_CHECKOUT}/core/python/deep-search", "deep-search"),
        (f"{CI_CHECKOUT}/core/rag-vector-search", "rag-vector-search"),
    ],
)
def test_absolute_paths_inside_the_repo_resolve_the_same(path, expected):
    assert (
        m.expected_project_name(Path(path), repo_root=Path(CI_CHECKOUT))
        == expected
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # The bug this guards: a checkout directory that merely happens to be
        # NAMED "skills". Matching the third-from-last segment made this
        # "core-rag-vector-search". The root has to be at position 0 of the
        # repo-relative path, not just present somewhere in it.
        ("/home/me/skills/core/rag-vector-search", "rag-vector-search"),
        ("/var/tmp/skills/retail/product-search", "product-search"),
        # Correctly shaped, but not under the repo root we were given.
        (f"{CI_CHECKOUT}-other/skills/retail/product-search", "product-search"),
    ],
)
def test_paths_outside_the_repo_fall_back_to_basename(path, expected):
    """A path we cannot locate within the repository gets the conservative
    answer. The auto-fixer mirror of this rule WRITES the value, so guessing
    a namespaced name from an unanchored path would corrupt a pyproject."""
    assert (
        m.expected_project_name(Path(path), repo_root=Path(CI_CHECKOUT))
        == expected
    )


def test_two_verticals_sharing_a_solution_name_do_not_collide():
    """The reason the rule exists: basenames are not unique under skills/."""
    retail = m.expected_project_name(Path("skills/retail/product-search"))
    grocery = m.expected_project_name(Path("skills/grocery/product-search"))
    assert retail != grocery


# ---------------------------------------------------------------------------
# check_name
# ---------------------------------------------------------------------------


def test_skill_with_vertical_prefixed_name_passes():
    assert (
        m.check_name(
            {"name": "retail-product-search"},
            Path("skills/retail/product-search/pyproject.toml"),
            Path("skills/retail/product-search"),
        )
        == []
    )


def test_skill_with_bare_basename_fails_and_explains_why():
    (diag,) = m.check_name(
        {"name": "product-search"},
        Path("skills/retail/product-search/pyproject.toml"),
        Path("skills/retail/product-search"),
    )
    text = _text(diag)
    assert "retail-product-search" in text
    # The contributor should not have to go read the script to understand it.
    assert "<vertical>-<solution>" in text
    # And should not have to work out where to type it.
    assert 'name = "retail-product-search"' in diag.how


def test_core_recipe_message_omits_the_vertical_explanation():
    """core/ and contrib/ messages stay as terse as they were pre-change."""
    (diag,) = m.check_name(
        {"name": "wrong"},
        Path("core/python/deep-search/pyproject.toml"),
        Path("core/python/deep-search"),
    )
    text = _text(diag)
    assert "deep-search" in text
    assert "vertical" not in text


def test_core_recipe_with_matching_basename_passes():
    assert (
        m.check_name(
            {"name": "deep-search"},
            Path("core/python/deep-search/pyproject.toml"),
            Path("core/python/deep-search"),
        )
        == []
    )


def test_missing_name_reports_the_expected_value():
    (diag,) = m.check_name(
        {},
        Path("skills/retail/product-search/pyproject.toml"),
        Path("skills/retail/product-search"),
    )
    assert "is missing" in diag.what
    assert "retail-product-search" in _text(diag)


# ---------------------------------------------------------------------------
# Exit-code contract
#
# The workflow no longer parses records out of stdout; it branches on the
# exit code alone. 0/1/2 have to mean exactly one thing each.
# ---------------------------------------------------------------------------

_GOOD_INDEX = """
[[tool.uv.index]]
url = "https://pypi.org/simple/"
default = true
"""


def _good_pyproject(name: str) -> str:
    return (
        f'[project]\nname = "{name}"\nversion = "0.1.0"\n'
        f'requires-python = ">=3.11"\n{_GOOD_INDEX}'
    )


def test_conforming_recipe_exits_zero(tmp_path, monkeypatch, capsys):
    code = _run(tmp_path, monkeypatch, _good_pyproject(tmp_path.name))
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("[PASS]")
    assert "::error" not in out


def test_violation_exits_one_and_annotates_the_file(
    tmp_path, monkeypatch, capsys
):
    code = _run(tmp_path, monkeypatch, _good_pyproject("wrong-name"))
    assert code == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert f"::error file={tmp_path}/pyproject.toml::" in out


def test_every_violation_gets_its_own_annotation(tmp_path, monkeypatch, capsys):
    """No "first error (+N more)" collapse: the Files tab shows them all."""
    code = _run(
        tmp_path,
        monkeypatch,
        '[project]\nname = "wrong-name"\nrequires-python = ">=3.9"\n',
    )
    assert code == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    # name, requires-python, missing index.
    assert out.count("::error file=") == 3


def test_missing_pyproject_is_not_this_checkers_failure(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_pyproject.py", str(tmp_path)]
    )
    assert m.main() == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_a_path_that_is_not_a_directory_is_a_ci_fault(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_pyproject.py", str(tmp_path / "nope")]
    )
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    # A CI fault must never put a marker on a contributor's file.
    assert "::error file=" not in out


def test_an_unexpected_crash_is_a_ci_fault_not_the_contributors_fault(
    tmp_path, monkeypatch, capsys
):
    def boom(*_args, **_kwargs):
        raise RuntimeError("checker bug")

    monkeypatch.setattr(m, "check_name", boom)
    code = _run(tmp_path, monkeypatch, _good_pyproject(tmp_path.name))
    assert code == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "RuntimeError: checker bug" in out
    assert "::error file=" not in out


# ---------------------------------------------------------------------------
# Malformed input that used to raise AttributeError
#
# Each of these produced a traceback, which the workflow then reported as
# "checker crashed" against the contributor's pyproject.toml.
# ---------------------------------------------------------------------------


def test_project_as_array_of_tables_is_reported_not_crashed(
    tmp_path, monkeypatch, capsys
):
    code = _run(
        tmp_path,
        monkeypatch,
        f'[[project]]\nname = "{tmp_path.name}"\n{_GOOD_INDEX}',
    )
    assert code == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "AttributeError" not in out
    assert "not a table" in out
    assert "[project]   ← not [[project]]" in out


def test_index_as_a_list_of_strings_is_reported_not_crashed(
    tmp_path, monkeypatch, capsys
):
    code = _run(
        tmp_path,
        monkeypatch,
        f'[project]\nname = "{tmp_path.name}"\n'
        f'requires-python = ">=3.11"\n\n[tool.uv]\n'
        f'index = ["https://pypi.org/simple/"]\n',
    )
    assert code == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "AttributeError" not in out
    assert "not a table" in out
    assert "[[tool.uv.index]]" in out


def test_index_as_a_single_table_is_reported_not_crashed(
    tmp_path, monkeypatch, capsys
):
    code = _run(
        tmp_path,
        monkeypatch,
        f'[project]\nname = "{tmp_path.name}"\n'
        f'requires-python = ">=3.11"\n\n[tool.uv.index]\n'
        f'url = "https://pypi.org/simple/"\ndefault = true\n',
    )
    assert code == EXIT_VIOLATIONS
    assert "array of tables" in capsys.readouterr().out


def test_the_index_fix_survives_as_multiple_lines_in_the_annotation(
    tmp_path, monkeypatch, capsys
):
    """The reason the annotation is built here and not in shell: the old
    demux collapsed newlines, rendering the TOML fix as one invalid line."""
    _run(
        tmp_path,
        monkeypatch,
        f'[project]\nname = "{tmp_path.name}"\nrequires-python = ">=3.11"\n',
    )
    annotation = next(
        ln
        for ln in capsys.readouterr().out.splitlines()
        if ln.startswith("::error file=")
    )
    assert "%0A" in annotation
    assert 'url = "https://pypi.org/simple/"' in annotation


def test_the_index_message_carries_no_google_internal_jargon(
    tmp_path, monkeypatch, capsys
):
    _run(
        tmp_path,
        monkeypatch,
        f'[project]\nname = "{tmp_path.name}"\nrequires-python = ">=3.11"\n',
    )
    assert "Airlock" not in capsys.readouterr().out


def test_list_shaped_manifest_is_reported_not_crashed(
    tmp_path, monkeypatch, capsys
):
    pytest.importorskip("yaml")
    code = _run(
        tmp_path,
        monkeypatch,
        f'[project]\nname = "{tmp_path.name}"\n'
        f'requires-python = ">=3.11"\ndescription = "d"\n{_GOOD_INDEX}',
        manifest="- a\n- b\n",
    )
    assert code == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "AttributeError" not in out
    assert "not a mapping" in out


def test_missing_manifest_says_what_to_do_about_it(
    tmp_path, monkeypatch, capsys
):
    code = _run(
        tmp_path,
        monkeypatch,
        f'[project]\nname = "{tmp_path.name}"\n'
        f'requires-python = ">=3.11"\ndescription = "A recipe."\n'
        f"{_GOOD_INDEX}",
    )
    assert code == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    # A symptom ("cannot verify consistency") is not an action.
    assert "description: A recipe." in out


def test_missing_pyyaml_is_a_ci_fault_not_a_contributor_error(
    tmp_path, monkeypatch, capsys
):
    """The contributor cannot install a dependency into our runner."""
    # A None entry in sys.modules makes `import yaml` raise ImportError.
    monkeypatch.setitem(sys.modules, "yaml", None)
    code = _run(
        tmp_path,
        monkeypatch,
        f'[project]\nname = "{tmp_path.name}"\n'
        f'requires-python = ">=3.11"\ndescription = "d"\n{_GOOD_INDEX}',
        manifest="description: d\n",
    )
    assert code == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "::error file=" not in out


def test_invalid_toml_is_reported_against_the_file(
    tmp_path, monkeypatch, capsys
):
    code = _run(tmp_path, monkeypatch, "[project\nname = broken\n")
    assert code == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "not valid TOML" in out
    assert "Traceback" not in out


# ---------------------------------------------------------------------------
# Cross-implementation consistency
#
# check_recipe_pyproject.py (validate) and align_pyproject.py (auto-fix) each
# implement this rule independently and both carry a "keep in sync" note. A
# drift between them is worse than either being wrong alone: the skill would
# cheerfully rewrite [project].name to a value CI then rejects.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "core/python/deep-search",
        "contrib/python/financial-advisor",
        "core/rag-vector-search",
        "skills/retail/product-search",
        "skills/hr/onboarding",
        "product-search",
        "skills/retail/deep/nested",
    ],
)
def test_validator_and_autofixer_agree(align_pyproject, path):
    assert m.expected_project_name(
        Path(path)
    ) == align_pyproject.expected_project_name(Path(path))


def test_validator_and_autofixer_share_the_same_namespaced_roots(
    align_pyproject,
):
    """The two could agree on every sampled path and still diverge on a root
    only one of them knows about."""
    assert m.NAMESPACED_ROOTS == align_pyproject.NAMESPACED_ROOTS


# ---------------------------------------------------------------------------
# adk-major-current (WARN-ONLY)
#
# The rule that must never fail a PR. Six of nine core/python recipes did not
# satisfy it on the day it landed, so a regression that made it blocking would
# wedge every unrelated contributor behind an ADK migration. The exit-code
# assertions below are the guard against exactly that, and are the reason this
# block exists at all — the message wording matters far less.
# ---------------------------------------------------------------------------


def _adk_pyproject(spec: str, name: str = "demo") -> str:
    return (
        "[project]\n"
        f'name = "{name}"\n'
        'requires-python = ">=3.11,<3.14"\n'
        f'dependencies = ["{spec}"]\n'
        "\n[[tool.uv.index]]\n"
        'url = "https://pypi.org/simple/"\n'
        "default = true\n"
    )


def _adk_lock(version: str) -> str:
    return f'[[package]]\nname = "google-adk"\nversion = "{version}"\n'


def _make_recipe(
    tmp_path: Path,
    monkeypatch,
    spec: str,
    locked: str | None,
    root: str = "core/python/demo",
) -> Path:
    """Build a throwaway recipe at <fake repo>/<root> and point the module's
    REPO_ROOT at it.

    Both halves matter and an earlier version of these tests got both wrong:
    the rule scopes itself on the path RELATIVE TO THE REPO ROOT, and it reads
    `uv.lock` from the recipe directory it is handed. Writing the lock
    somewhere else while passing a hand-written `Path("core/python/demo")`
    silently produced "no lock", so the tests passed against a code path they
    were not aiming at.
    """
    recipe_dir = tmp_path / root
    recipe_dir.mkdir(parents=True)
    (recipe_dir / "pyproject.toml").write_text(
        _adk_pyproject(spec, name=recipe_dir.name), encoding="utf-8"
    )
    if locked is not None:
        (recipe_dir / "uv.lock").write_text(_adk_lock(locked), encoding="utf-8")
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    return recipe_dir


def _advise(tmp_path, monkeypatch, spec, locked, root="core/python/demo"):
    recipe_dir = _make_recipe(tmp_path, monkeypatch, spec, locked, root)
    return m.check_adk_major(
        {"dependencies": [spec]}, recipe_dir / "pyproject.toml", recipe_dir
    )


@pytest.mark.parametrize(
    ("spec", "locked"),
    [
        ("google-adk[gcp]>=2.5.0,<3.0.0", "2.5.0"),
        ("google-adk[mcp,otel-gcp,a2a]>=2.5.0,<3.0.0", "2.5.0"),
        ("google-adk[gcp]>=2.0.0,<3.0.0", "2.3.0"),
        ("google-adk>=1.0.0", "2.6.2"),
        ("google-adk==2.3.0", "2.3.0"),
    ],
)
def test_recipes_on_the_current_major_are_silent(
    tmp_path, monkeypatch, spec, locked
):
    """Every case here locks on the current major, so what is pinned is the
    LOCK-FIRST ordering: the specifier is never consulted, however unusual it
    looks, because the lock demonstrably admitted what got installed.

    These do NOT reach `_specifier_admits_major` — the test below covers it.
    """
    assert _advise(tmp_path, monkeypatch, spec, locked) == []


@pytest.mark.parametrize(
    "spec",
    [
        "google-adk>=2.5.0,<3.0.0",  # floor inside the current series
        "google-adk==2.3.0",  # exact pin inside the current series
    ],
)
def test_specifier_inside_the_current_major_without_a_lock_is_silent(
    tmp_path, monkeypatch, spec
):
    """The regression `_specifier_admits_major` exists for, on the only path
    that reaches it: with no lock to settle the question, the specifier is all
    there is.

    A naive `contains(Version("2.0.0"))` calls both of these stale, because a
    floor inside the series and an exact pin inside it each exclude X.0.0
    exactly. Two recipes were wrongly flagged that way during development.
    """
    assert _advise(tmp_path, monkeypatch, spec, None) == []


@pytest.mark.parametrize(
    "spec",
    [
        "google-adk>=1.15.0,<2.0.0",  # capped below the current major
        "google-adk==1.31.0",  # hard pin on the previous major
    ],
)
def test_specifier_that_excludes_the_current_major_is_flagged(
    tmp_path, monkeypatch, spec
):
    (advisory,) = _advise(tmp_path, monkeypatch, spec, "1.31.0")
    assert advisory.check == "adk-major-current"
    assert "cannot resolve" in advisory.what
    # The fix has to say "migrate", not "relock": widening the specifier
    # without porting the code fails at runtime, where it is expensive.
    assert "code migration" in advisory.how


def test_permissive_specifier_with_a_stale_lock_is_flagged(
    tmp_path, monkeypatch
):
    """`>=1.8.0` admits 2.x, yet the recipe installs 1.28.0 and therefore
    teaches 1.x. Reading the specifier alone would call this compliant."""
    (advisory,) = _advise(tmp_path, monkeypatch, "google-adk>=1.8.0", "1.28.0")
    assert "uv.lock pins google-adk 1.28.0" in advisory.what
    assert "--upgrade-package google-adk" in advisory.how


def test_prerelease_on_the_current_major_gets_its_own_notice(
    tmp_path, monkeypatch
):
    (advisory,) = _advise(
        tmp_path, monkeypatch, "google-adk>=2.0.0a0", "2.0.0a3"
    )
    assert "prerelease" in advisory.what
    assert "cannot resolve" not in advisory.what


@pytest.mark.parametrize("root", ["contrib/python/demo", "skills/retail/demo"])
def test_rule_is_scoped_to_core(tmp_path, monkeypatch, root):
    """contrib/ and skills/ set their own pace. Paired with the stale-lock
    test above, which uses identical inputs under core/ and DOES fire — so
    this proves scoping, not merely that the inputs were inert."""
    assert (
        _advise(tmp_path, monkeypatch, "google-adk>=1.8.0", "1.28.0", root)
        == []
    )


def test_recipe_without_an_adk_dependency_is_silent(tmp_path, monkeypatch):
    """A nested sub-project (data_ingestion/) has no ADK dependency. Inventing
    a notice for it would train people to ignore the channel."""
    recipe_dir = _make_recipe(tmp_path, monkeypatch, "pandas<3", "1.28.0")
    assert (
        m.check_adk_major(
            {"dependencies": ["pandas<3", "swifter>=1.4.0"]},
            recipe_dir / "pyproject.toml",
            recipe_dir,
        )
        == []
    )


def test_missing_lock_falls_back_to_the_specifier_and_never_guesses(
    tmp_path, monkeypatch
):
    """No lock: a permissive specifier must stay silent rather than assume the
    worst. The dependency-policy workflow owns the missing-lock complaint."""
    assert _advise(tmp_path, monkeypatch, "google-adk>=1.0.0", None) == []


def test_missing_lock_still_catches_an_excluding_specifier(
    tmp_path, monkeypatch
):
    assert len(_advise(tmp_path, monkeypatch, "google-adk<2.0.0", None)) == 1


def test_advisories_never_change_the_exit_code(tmp_path, monkeypatch, capsys):
    """The whole point of the rule. A recipe that is otherwise clean but a
    major behind still exits 0."""
    recipe_dir = _make_recipe(
        tmp_path, monkeypatch, "google-adk>=1.0.0", "1.28.0"
    )
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_pyproject.py", str(recipe_dir)]
    )
    code = m.main()
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "adk-major-current" in out
    assert "::warning file=" in out
    assert "::error" not in out


def test_advisory_severity_cannot_be_error():
    """report_advisories() refuses an ERROR-severity finding: a red annotation
    on a green run teaches contributors to ignore annotations."""
    sys.path.insert(0, str(Path(m.__file__).resolve().parents[2] / "tools"))
    import ci_message

    bad = ci_message.Diagnostic(
        check="x",
        what="x",
        why="x",
        how="x",
        doc=ci_message.Doc.ADK_MAJOR,
        severity=ci_message.Severity.ERROR,
    )
    with pytest.raises(ValueError, match="must not be ERROR"):
        ci_message.report_advisories([bad], header="h")
