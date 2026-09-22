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
"""Unit tests for recipe_canary_matrix.py.

The matrix decides what the canary looks at, so anything it drops is a recipe
nobody is watching — a silent gap, which is the failure mode the canary was
built to remove. These tests pin the dropping rules, not the happy path.
"""

from pathlib import Path

import pytest
import recipe_canary_matrix as m
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _recipe(
    tmp_path: Path,
    rel: str,
    requires_python: str | None = ">=3.11,<3.14",
    language: str = "python",
    status: str = "active",
) -> Path:
    d = tmp_path / rel
    d.mkdir(parents=True)
    (d / "manifest.yaml").write_text(
        f'language: "{language}"\nstatus: {status}\n', encoding="utf-8"
    )
    if requires_python is not None:
        (d / "pyproject.toml").write_text(
            f'[project]\nname = "x"\nrequires-python = "{requires_python}"\n',
            encoding="utf-8",
        )
    return d


# ---------------------------------------------------------------------------
# Which Python versions a recipe is tested on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requires_python", "expected"),
    [
        # The case that motivated the whole rule: a recipe claiming <3.14 is
        # broken on 3.13 today (google-genai 2.10.0, SyntaxError on import)
        # and passes on 3.11, so a floor-only canary reports it healthy.
        (">=3.11,<3.14", ["3.11", "3.13"]),
        (">=3.11,<3.13", ["3.11", "3.12"]),
        ("<=3.12,>=3.11", ["3.11", "3.12"]),
        # No upper bound claims everything; test up to what we can provision.
        (">=3.11", ["3.11", "3.13"]),
        # A ceiling at the floor means one job, not two identical ones.
        (">=3.11,<3.12", ["3.11"]),
        # Beyond what the runners have: clamped, never invented.
        (">=3.11,<3.99", ["3.11", "3.13"]),
    ],
)
def test_python_targets(tmp_path, requires_python, expected):
    d = _recipe(tmp_path, "core/python/demo", requires_python)
    assert m.python_targets(d) == expected


@pytest.mark.parametrize(
    "requires_python", ["", "not-a-specifier", "~=3.11", ">=3.11,!=3.12.*"]
)
def test_unparseable_upper_bound_tests_more_not_less(tmp_path, requires_python):
    """No readable ceiling falls back to the maximum, deliberately.

    A surplus job costs a few CI minutes. A skipped one costs a recipe that
    silently stops working on a Python it claims to support.
    """
    d = _recipe(tmp_path, "core/python/demo", requires_python)
    assert m.python_targets(d) == ["3.11", "3.13"]


def test_missing_or_broken_pyproject_still_yields_the_floor(tmp_path):
    """A recipe is never dropped from the matrix for a malformed pyproject —
    the validate workflow owns that complaint, and dropping it here would
    remove the recipe from the canary entirely."""
    d = _recipe(tmp_path, "core/python/demo", requires_python=None)
    assert m.python_targets(d) == ["3.11"]

    (d / "pyproject.toml").write_text("[project\nbroken", encoding="utf-8")
    assert m.python_targets(d) == ["3.11"]


# ---------------------------------------------------------------------------
# Which recipes are in the matrix at all
# ---------------------------------------------------------------------------


def test_discovers_python_recipes_across_all_three_roots(tmp_path):
    _recipe(tmp_path, "core/python/a")
    _recipe(tmp_path, "contrib/python/b")
    _recipe(tmp_path, "skills/retail/c")
    assert m.discover_recipes(tmp_path) == [
        "contrib/python/b",
        "core/python/a",
        "skills/retail/c",
    ]


def test_non_python_recipes_are_not_in_the_matrix(tmp_path):
    _recipe(tmp_path, "core/go/g", language="go")
    _recipe(tmp_path, "core/python/p")
    assert m.discover_recipes(tmp_path) == ["core/python/p"]


def test_inactive_recipes_are_still_tested(tmp_path):
    """Deliberate. A recipe on the retirement path is deleted after 120 days,
    so "fixed but nobody flipped status back" has to be detectable — and it
    only is if the canary keeps running it."""
    _recipe(tmp_path, "core/python/retired", status="inactive")
    assert m.discover_recipes(tmp_path) == ["core/python/retired"]


def test_vendored_directories_are_pruned(tmp_path):
    """A manifest.yaml inside .venv or node_modules belongs to a dependency,
    not to this repo."""
    _recipe(tmp_path, "core/python/real")
    _recipe(tmp_path, "core/python/real/.venv/lib/pkg/vendored")
    _recipe(tmp_path, "core/python/real/node_modules/thing")
    assert m.discover_recipes(tmp_path) == ["core/python/real"]


def test_a_skipped_recipe_is_dropped_from_discovery(tmp_path, monkeypatch):
    """SKIP_RECIPES is empty today, so the mechanism has no live entry to
    exercise it. Pin the behaviour anyway: the next entry added must still
    remove exactly its own recipe and nothing else."""
    monkeypatch.setattr(m, "SKIP_RECIPES", {"core/legacy-dupe"})
    _recipe(tmp_path, "core/legacy-dupe")
    _recipe(tmp_path, "core/python/legacy-dupe")
    assert m.discover_recipes(tmp_path) == ["core/python/legacy-dupe"]


def test_every_skip_entry_still_names_a_real_recipe():
    """A skip that matches nothing is dead config; a skip that quietly starts
    matching a live recipe removes it from every canary run with no signal.

    Fails once the legacy duplicates are deleted — which is the intended
    prompt to delete the SKIP_RECIPES entries in the same change.
    """
    for rel in sorted(m.SKIP_RECIPES):
        assert (REPO_ROOT / rel / "manifest.yaml").is_file(), (
            f"SKIP_RECIPES lists {rel}, which no longer exists. Remove the "
            f"entry from recipe_canary_matrix.py."
        )


# ---------------------------------------------------------------------------
# The matrix as the workflow consumes it
# ---------------------------------------------------------------------------


def test_matrix_is_one_entry_per_recipe_and_version(tmp_path):
    _recipe(tmp_path, "core/python/a", ">=3.11,<3.14")
    _recipe(tmp_path, "core/python/b", ">=3.11,<3.12")
    assert m.build_matrix(tmp_path) == [
        {"recipe": "core/python/a", "python": "3.11"},
        {"recipe": "core/python/a", "python": "3.13"},
        {"recipe": "core/python/b", "python": "3.11"},
    ]


def _recipes_via_real_yaml(root: Path) -> set[str]:
    """Which recipes are Python recipes, according to a real YAML parser.

    A SECOND OPINION, and deliberately a different implementation from the
    one under test: `recipe_canary_matrix` matches `language:` with a regex
    so it can run under `uv run --no-project` with no dependencies, and this
    parses the document properly. Agreement between two implementations is
    the signal; a helper that re-used the regex would assert nothing.

    A manifest that will not parse counts as NOT a Python recipe. If the
    regex accepted it, the two disagree and the caller fails — which is the
    right answer for a malformed manifest, because a recipe the canary picks
    up from a file nobody can parse is not a recipe anyone is really
    watching.
    """
    found: set[str] = set()
    for root_name in m.SCAN_ROOTS:
        root_path = root / root_name
        if not root_path.is_dir():
            continue
        for manifest in root_path.rglob("manifest.yaml"):
            rel = manifest.parent.relative_to(root).as_posix()
            if any(part in m.SKIP_DIRS for part in Path(rel).parts):
                continue
            try:
                data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
            except (yaml.YAMLError, OSError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            language = data.get("language")
            if isinstance(language, str) and language.strip().lower() == (
                "python"
            ):
                found.add(rel)
    return found


def test_the_canary_sees_every_python_recipe_in_the_tree():
    """The canary must watch every Python recipe that exists on disk.

    This used to be an EXACT frozen list of the eleven recipes present when
    the canary was written, which made adding a recipe a change to this
    file — a file under .github/, which a recipe contributor has no business
    editing and which routes review to the CODEOWNERS catch-all. Deriving
    the expected set from the tree costs nothing and removes that entirely.

    It is also a STRICTLY STRONGER guard, because the frozen list could not
    see the failure that matters most to a contributor. A new recipe whose
    `language:` line the regex cannot read is never discovered, so the
    discovered set stays equal to the frozen eleven and the assertion passes
    — while the recipe is silently never canaried, which is the exact
    outcome the list was written to prevent. Comparing against the tree
    catches it, in both directions: a recipe the parser sees and the canary
    misses, and a recipe the canary picks up that is not a Python recipe at
    all.
    """
    # discover_recipes(), not build_matrix(): build_matrix drops a recipe
    # whose python_targets() is empty, which is the deliberate decline for
    # one needing an interpreter newer than the canary provisions
    # (recipe_canary_matrix.py:236). Comparing the matrix against "is a
    # Python recipe" fails on such a recipe and blames its `language:` line.
    covered = set(m.discover_recipes())
    expected = _recipes_via_real_yaml(REPO_ROOT) - m.SKIP_RECIPES

    # Two empty sets are equal, so without a floor this reads as agreement
    # when discovery has collapsed entirely — a bad REPO_ROOT, an emptied
    # SCAN_ROOTS or a broken rglob empties BOTH sides. The frozen list this
    # replaced could not miss that (11 != 0). Set well under the real count
    # so that adding or deleting a recipe never touches it.
    assert len(expected) >= 8, (
        f"only {len(expected)} Python recipes found under {m.SCAN_ROOTS} in "
        f"{REPO_ROOT}. That is a broken scan, not a shrunken repo."
    )

    assert covered == expected, (
        "the canary's recipe set disagrees with the recipe tree. Not "
        f"canaried but present on disk: {sorted(expected - covered)}; "
        f"canaried but not a Python recipe: {sorted(covered - expected)}.\n"
        "\n"
        "This is NOT fixed by editing this file. A recipe missing from the "
        "canary almost always has a `language:` line in its manifest.yaml "
        "that a real YAML parser reads as Python but the matcher in "
        "recipe_canary_matrix.py does not — most often because the value is "
        "on the following line. Put `language: python` on one line in the "
        "recipe's own manifest.yaml."
    )


# The recipes the canary is allowed not to watch.
#
# This is the one thing the tree cannot tell us. A skipped recipe and a
# recipe that was never there look identical from outside the canary, so
# `test_the_canary_sees_every_python_recipe_in_the_tree` subtracts
# SKIP_RECIPES from both sides and is blind to the list growing — as is
# `test_discovery_agrees_with_python_tests_yml`, which unions it back in.
# Guarding that residual is the entire remaining job of a frozen literal,
# and it needs two lines rather than one entry per recipe in the repo.
#
# Maintainer-owned. A recipe contributor never touches this; it held the two
# legacy flat-path duplicates until #2653 deleted them, and is empty now.
ALLOWED_SKIPS: set[str] = set()


def test_the_skip_list_has_not_grown():
    """Adding a live recipe to SKIP_RECIPES removes it from every canary run,
    permanently and silently. It must be a deliberate act with a reviewer
    attached."""
    assert m.SKIP_RECIPES == ALLOWED_SKIPS, (
        "SKIP_RECIPES changed. Added: "
        f"{sorted(m.SKIP_RECIPES - ALLOWED_SKIPS)}; removed: "
        f"{sorted(ALLOWED_SKIPS - m.SKIP_RECIPES)}. A skipped recipe is "
        "never canaried again and nothing else in this file can detect it, "
        "so update ALLOWED_SKIPS in the same change and say why in the PR."
    )


def test_the_second_opinion_is_genuinely_a_second_opinion(tmp_path):
    """The differential check is only worth running if its two sides can
    actually disagree. A helper that re-implemented the regex would agree
    with the regex unconditionally and assert nothing.

    `language:` with the value on the next line is valid YAML and reads as
    `{"language": "python"}`, but the matcher in recipe_canary_matrix
    requires the value on the same line. So the parser sees a Python recipe
    and the canary does not — the precise shape of "a new recipe silently
    never canaried" that the old frozen list could not detect.
    """
    recipe = tmp_path / "core" / "python" / "folded"
    recipe.mkdir(parents=True)
    (recipe / "manifest.yaml").write_text(
        "language:\n  python\nstatus: active\n", encoding="utf-8"
    )

    assert _recipes_via_real_yaml(tmp_path) == {"core/python/folded"}
    assert m.discover_recipes(tmp_path) == []


def test_a_skip_swallowing_a_live_recipe_is_caught(tmp_path, monkeypatch):
    """The failure the skip-list pin exists for, exercised rather than
    assumed: a live recipe added to SKIP_RECIPES vanishes from the matrix,
    and the tree-derived check cannot see it because it subtracts
    SKIP_RECIPES from both sides."""
    _recipe(tmp_path, "core/python/watched")
    monkeypatch.setattr(m, "SKIP_RECIPES", {"core/python/watched"})

    # Gone from the matrix, and invisible to the differential check.
    assert m.discover_recipes(tmp_path) == []
    assert not _recipes_via_real_yaml(tmp_path) - m.SKIP_RECIPES
    # Only the pin catches it.
    assert m.SKIP_RECIPES != ALLOWED_SKIPS


def test_empty_scan_is_an_error_not_a_pass(tmp_path, monkeypatch, capsys):
    """An empty matrix must never read as success. Every recipe vanishing and
    the scanner breaking look identical from the outside."""
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    assert m.main([]) == 1
    assert "refuses to report success" in capsys.readouterr().err


def test_discovery_agrees_with_python_tests_yml():
    """The canary and python-tests.yml discover recipes independently — one in
    Python here, one in bash there — and must not drift.

    They were deliberately NOT unified: python-tests.yml is a required check
    whose discovery also does diff-to-recipe mapping, and rewriting it to
    share this code is a change to the gate every PR passes through. Pinning
    agreement is the cheaper half of that trade, and it is the half that
    catches the failure that matters — a recipe visible to one and invisible
    to the other.

    The bash is EXTRACTED FROM THE WORKFLOW, not retyped here. A hand-copied
    version was what this test ran before, and it had already drifted: the
    copy pruned `.venv` and `node_modules` with `-not -path`, while the real
    `find` in python-tests.yml prunes nothing. So it compared the canary
    against a script that does not exist, and could never have caught the
    difference it was written to catch.

    Two differences are legitimate and accounted for below:
      * SKIP_RECIPES — the canary deliberately skips the legacy duplicates;
      * SKIP_DIRS — the canary prunes vendored trees and the workflow does
        not, so a manifest the bash finds inside one is not a disagreement.
    """
    import subprocess
    import textwrap

    workflow = (
        REPO_ROOT / ".github" / "workflows" / "python-tests.yml"
    ).read_text(encoding="utf-8")

    # Pull the two shell functions straight out of the workflow's `run:`
    # block, so this exercises the real discovery rather than a copy of it.
    blocks = []
    for name in ("is_python_recipe", "all_python_recipes"):
        start = workflow.index(f"{name}() {{")
        depth, i = 0, start
        while True:
            if workflow[i] == "{":
                depth += 1
            elif workflow[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        blocks.append(textwrap.dedent(workflow[start : i + 1]))

    out = subprocess.run(
        ["bash", "-c", "\n".join(blocks) + "\nall_python_recipes\n"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    from_bash = {line for line in out.stdout.split() if line}
    from_bash = {
        r
        for r in from_bash
        if not any(part in m.SKIP_DIRS for part in Path(r).parts)
    }
    from_python = set(m.discover_recipes()) | set(m.SKIP_RECIPES)

    assert from_python == from_bash, (
        "recipe_canary_matrix.discover_recipes() and the discovery in "
        "python-tests.yml disagree. Symmetric difference: "
        f"{from_python ^ from_bash}"
    )


# ---------------------------------------------------------------------------
# Guards added after review
# ---------------------------------------------------------------------------


def test_skip_dirs_is_matched_against_the_repo_relative_path(tmp_path):
    """SKIP_DIRS used to be tested against `manifest.parts`, the ABSOLUTE
    path. Any checkout living under a directory named `build`, `dist` or
    `.venv` — which is every self-hosted runner with a `build/` workspace —
    therefore excluded every recipe in the repo and produced an empty
    matrix."""
    checkout = tmp_path / "build" / "adk-samples"
    recipe = checkout / "core" / "python" / "demo"
    recipe.mkdir(parents=True)
    (recipe / "manifest.yaml").write_text(
        'language: "python"\n', encoding="utf-8"
    )
    assert m.discover_recipes(checkout) == ["core/python/demo"]


def test_a_vendored_manifest_is_still_skipped(tmp_path):
    """The other direction: SKIP_DIRS must still work on path components
    inside the repo."""
    recipe = tmp_path / "core" / "python" / "demo"
    (recipe / "node_modules" / "pkg").mkdir(parents=True)
    (recipe / "manifest.yaml").write_text(
        'language: "python"\n', encoding="utf-8"
    )
    (recipe / "node_modules" / "pkg" / "manifest.yaml").write_text(
        'language: "python"\n', encoding="utf-8"
    )
    assert m.discover_recipes(tmp_path) == ["core/python/demo"]


def test_an_unreadable_manifest_is_reported_not_swallowed(tmp_path, capsys):
    """A recipe silently dropped is never tested, so it never fails, so it
    looks healthy forever. Dropping it is acceptable; doing so in silence is
    not."""
    recipe = tmp_path / "core" / "python" / "broken"
    recipe.mkdir(parents=True)
    (recipe / "manifest.yaml").write_bytes(b"language: \xff\xfe python\n")
    assert m.discover_recipes(tmp_path) == []
    captured = capsys.readouterr()
    assert "not valid UTF-8" in captured.err
    # Deliberately not a `::warning` annotation: GitHub collects those from
    # stdout, and stdout here carries the matrix JSON the workflow parses.
    assert "::warning" not in captured.err
    assert captured.out == ""


def test_an_unknown_recipe_argument_is_rejected():
    """`--recipe` was taken on trust, so a typo produced a matrix pointing at
    a directory that does not exist and every job failed for a reason that
    was not the recipe's fault."""
    with pytest.raises(m.MatrixError, match="not a Python recipe"):
        m.build_matrix(only="core/python/does-not-exist")


def test_a_skipped_recipe_cannot_be_canaried_by_hand(monkeypatch):
    """`--recipe` also bypassed SKIP_RECIPES entirely, so a maintainer could
    hand-run the canary against exactly the recipe the skip list exists to
    keep quiet. Skipping a real recipe here, since SKIP_RECIPES is empty."""
    skipped = m.discover_recipes()[0]
    monkeypatch.setattr(m, "SKIP_RECIPES", {skipped})
    with pytest.raises(m.MatrixError):
        m.build_matrix(only=skipped)


def test_a_known_recipe_argument_is_accepted():
    known = m.discover_recipes()[0]
    matrix = m.build_matrix(only=known)
    assert matrix and {e["recipe"] for e in matrix} == {known}


def test_a_recipe_declaring_a_higher_floor_is_not_tested_below_it(tmp_path):
    """Running a `>=3.12` recipe on 3.11 is a guaranteed install failure that
    the canary would file against its owner as rot. The repo's
    python-version-floor rule should prevent this ever arising, but a false
    accusation is the one outcome that costs the channel its credibility."""
    recipe = tmp_path / "demo"
    recipe.mkdir()
    (recipe / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.12,<3.14"\n', encoding="utf-8"
    )
    assert m.python_targets(recipe) == ["3.12", "3.13"]


def test_the_ordinary_floor_is_still_used(tmp_path):
    recipe = tmp_path / "demo"
    recipe.mkdir()
    (recipe / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11,<3.14"\n', encoding="utf-8"
    )
    assert m.python_targets(recipe) == [m.FLOOR, "3.13"]


def test_an_oversized_matrix_is_refused(monkeypatch):
    """GitHub rejects a matrix above 256 jobs with a scheduling error that
    names no cause."""
    monkeypatch.setattr(m, "MAX_MATRIX_JOBS", 3)
    with pytest.raises(m.MatrixError, match="above GitHub's"):
        m.build_matrix()
