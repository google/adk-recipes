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

"""The deterministic review lane: recipe discovery, translation, isolation."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import house_rules_lane as lane

CHECKER = (
    Path(__file__).resolve().parents[3]
    / ".agents"
    / "skills"
    / "github-pr-review"
    / "scripts"
    / "check_house_rules.py"
)


# --------------------------------------------------------- recipe discovery


def test_only_the_recipes_the_pr_touches_are_checked():
    """A PR editing one recipe must not collect findings about the other 400."""
    roots = lane.recipe_roots(
        [
            "contrib/python/my-recipe/agent.py",
            "contrib/python/my-recipe/pyproject.toml",
            "core/go/other/main.go",
            "skills/retail/store-ops/manifest.yaml",
            "README.md",
            ".github/workflows/thing.yml",
            "tools/validate_structure.py",
        ]
    )
    assert roots == [
        "contrib/python/my-recipe",
        "core/go/other",
        "skills/retail/store-ops",
    ]


def test_a_pr_touching_no_recipe_yields_nothing():
    assert lane.recipe_roots(["docs/x.md", ".github/policy.yml"]) == []


def test_a_recipe_root_is_three_segments_not_a_prefix():
    """`contrib/python` alone is not a recipe, and neither is the repo root."""
    assert lane.recipe_roots(["contrib/python/README.md"]) == []
    assert lane.recipe_roots(["skills/retail"]) == []


# ------------------------------------------------------------- translation


def test_the_citation_is_carried_into_the_comment_body():
    """A file the author can open is what stops a rule comment sounding
    arbitrary."""
    out = lane.to_reviewer_finding(
        {
            "path": "contrib/python/x/pyproject.toml",
            "line": 4,
            "what": "declares a [tool.ruff] table; recipes must not",
            "evidence": "python-validate-recipe.yml:261-266",
            "verify_steps": "grep '^\\[tool\\.ruff' in this file",
            "rule": "H1",
            "ci": "fail",
        }
    )
    assert out["body"].endswith("(python-validate-recipe.yml:261-266)")
    assert out["line"] == 4
    assert out["_rule"] == "H1"


def test_a_citation_already_in_the_body_is_not_repeated():
    out = lane.to_reviewer_finding(
        {
            "path": "p",
            "line": 1,
            "evidence": "AGENTS.md:40",
            "what": "deprecated model id, see AGENTS.md:40",
        }
    )
    assert out["body"].count("AGENTS.md:40") == 1


def test_no_window_is_emitted():
    """The window check audits a model's claim about the source. A checker that
    read the file makes no such claim, and supplying one only adds a way for
    this lane to fail."""
    assert "window" not in lane.to_reviewer_finding(
        {"path": "p", "line": 1, "what": "x"}
    )


def test_a_missing_line_anchors_at_the_top_of_the_file():
    assert lane.to_reviewer_finding({"path": "p", "what": "x"})["line"] == 1


# ------------------------------------------------------------- end to end


def _recipe(tmp_path: Path, rel: str, **files: str) -> Path:
    root = tmp_path / rel
    root.mkdir(parents=True)
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


MANIFEST = (
    "type: standalone\nstatus: active\nlanguage: python\n"
    'description: "A real description, long enough to pass."\n'
    'ownership:\n  team: "google"\n  poc: "someone"\n'
)


@pytest.mark.skipif(not CHECKER.exists(), reason="checker not in this checkout")
def test_findings_come_back_in_the_posting_scripts_shape(tmp_path):
    _recipe(
        tmp_path,
        "contrib/python/my-recipe",
        **{"manifest.yaml": MANIFEST, "pyproject.toml": "[tool.ruff]\n"},
    )
    changed = tmp_path / "changed.txt"
    changed.write_text("contrib/python/my-recipe/manifest.yaml\n")
    out = tmp_path / "findings.json"

    rc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "house_rules_lane.py"),
            "--checker",
            str(CHECKER),
            "--repo-root",
            str(tmp_path),
            "--changed-files",
            str(changed),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc.returncode == 0, rc.stderr

    findings = json.loads(out.read_text())
    assert findings, "expected the junk ownership.team to be reported"
    for finding in findings:
        assert set(finding) >= {"path", "line", "body", "verify_steps"}
        assert isinstance(finding["line"], int)
        assert finding["path"].startswith("contrib/python/my-recipe/")
    assert any("google" in f["body"] for f in findings)


@pytest.mark.skipif(not CHECKER.exists(), reason="checker not in this checkout")
def test_state_does_not_leak_between_recipes(tmp_path):
    """Two recipes in one process. The checker keeps per-run state — the
    changed-file set, the skipped list, the git-tracked cache — and the second
    recipe must not inherit the first one's."""
    _recipe(tmp_path, "contrib/python/clean", **{"manifest.yaml": MANIFEST})
    _recipe(
        tmp_path,
        "contrib/python/dirty",
        **{"manifest.yaml": MANIFEST.replace('"google"', '"Real Team Name"')},
    )
    module = lane.load_checker(CHECKER)

    dirty, _ = lane.run_checker(
        module, str(tmp_path), "contrib/python/dirty", None
    )
    clean, _ = lane.run_checker(
        module, str(tmp_path), "contrib/python/clean", None
    )
    assert not [f for f in dirty if f["rule"] == "H48"]
    assert [f for f in clean if f["rule"] == "H48"], (
        "the second recipe's junk team was missed — state carried over"
    )
    assert all(f["path"].startswith("contrib/python/clean/") for f in clean)


@pytest.mark.skipif(not CHECKER.exists(), reason="checker not in this checkout")
def test_one_broken_recipe_does_not_cost_the_others(tmp_path, capsys):
    """A single unreadable recipe must not throw away every finding in the
    rest of the PR."""
    _recipe(tmp_path, "contrib/python/good", **{"manifest.yaml": MANIFEST})
    changed = tmp_path / "changed.txt"
    changed.write_text(
        "contrib/python/good/manifest.yaml\n"
        "contrib/python/vanished/manifest.yaml\n"
    )
    out = tmp_path / "findings.json"
    rc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "house_rules_lane.py"),
            "--checker",
            str(CHECKER),
            "--repo-root",
            str(tmp_path),
            "--changed-files",
            str(changed),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc.returncode == 0, rc.stderr
    assert json.loads(out.read_text()), "the good recipe's findings were lost"


def test_ci_failures_are_ordered_first(tmp_path):
    """An author acts on "this blocks the build" and may never act on a nit."""
    findings = [
        {"path": "b", "line": 1, "what": "nit", "ci": "advisory"},
        {"path": "a", "line": 1, "what": "blocker", "ci": "fail"},
    ]
    translated = [lane.to_reviewer_finding(f) for f in findings]
    translated.sort(key=lambda f: (f.get("_ci") != "fail", f.get("path") or ""))
    assert [f["body"] for f in translated] == ["blocker", "nit"]


@pytest.mark.skipif(not CHECKER.exists(), reason="checker not in this checkout")
def test_h42_is_reported_once_for_the_whole_pr(tmp_path):
    """H42 is a property of the pull request, not of a recipe. Called inside
    the per-recipe loop it produced one identical comment per recipe on one
    line — and at exactly THREE recipes the grouping pass collapsed them into
    "the same thing in 2 other places", which is false: it is the same place,
    three times. Three recipes here for exactly that reason.

    Driven through the CLI rather than through inspect.getsource: a test that
    reads source stays green when the call is merely relocated, and moving
    that call is what the fix did."""
    for name in ("alpha", "beta", "gamma"):
        _recipe(
            tmp_path, f"contrib/python/{name}", **{"manifest.yaml": MANIFEST}
        )
    changed = tmp_path / "changed.txt"
    changed.write_text(
        "".join(
            f"contrib/python/{n}/manifest.yaml\n"
            for n in ("alpha", "beta", "gamma")
        )
        + ".agents/skills/some-skill/SKILL.md\n"
    )
    out = tmp_path / "findings.json"
    rc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "house_rules_lane.py"),
            "--checker",
            str(CHECKER),
            "--repo-root",
            str(tmp_path),
            "--changed-files",
            str(changed),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "tools"),
        },
    )
    assert rc.returncode == 0, rc.stderr
    h42 = [f for f in json.loads(out.read_text()) if f["_rule"] == "H42"]
    assert len(h42) == 1, (
        f"expected one H42 across three recipes, got {len(h42)}"
    )


@pytest.mark.skipif(not CHECKER.exists(), reason="checker not in this checkout")
def test_a_mistyped_project_table_does_not_delete_a_recipes_review(tmp_path):
    """`project = "oops"` is a realistic typo. .get() on a str raised out of
    the lane, which catches per recipe and reports the PR as clean."""
    _recipe(
        tmp_path,
        "contrib/python/typo",
        **{"manifest.yaml": MANIFEST, "pyproject.toml": 'project = "oops"\n'},
    )
    module = lane.load_checker(CHECKER)
    findings, _ = lane.run_checker(
        module, str(tmp_path), "contrib/python/typo", None
    )
    assert findings, "the recipe produced no findings at all"


@pytest.mark.skipif(not CHECKER.exists(), reason="checker not in this checkout")
def test_a_pr_cannot_edit_the_schema_that_judges_it(tmp_path):
    """The real question, asked by planting a hostile schema in the tree under
    review: does H19 still fire?

    A neutered schema — one that permits every key — makes H19 report nothing,
    so a PR could legalise its own manifest. The previous version of this test
    asserted on the source text of run_checker and stayed green with the fix
    reverted, which is the pattern that let three earlier defects ship."""
    rel = "contrib/python/hostile"
    _recipe(
        tmp_path,
        rel,
        **{
            "manifest.yaml": MANIFEST + 'not_a_real_key: "smuggled"\n',
            # A schema that permits anything at all.
            ".github/schemas/manifest-schema.json": json.dumps(
                {
                    "additionalProperties": True,
                    "properties": {"not_a_real_key": {}},
                    "required": [],
                }
            ),
        },
    )
    # The planted schema sits where run_checker used to read it from.
    (tmp_path / ".github" / "schemas").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".github/schemas/manifest-schema.json").write_text(
        json.dumps(
            {
                "additionalProperties": True,
                "properties": {"not_a_real_key": {}},
                "required": [],
            }
        )
    )

    module = lane.load_checker(CHECKER)
    findings, _ = lane.run_checker(module, str(tmp_path), rel, None)
    assert any(
        f["rule"] == "H19" and "not_a_real_key" in f["what"] for f in findings
    ), (
        "the smuggled manifest key was not reported: the schema came from the "
        "tree under review, so a PR can edit the rule that judges it"
    )


def test_an_all_recipes_failed_run_is_not_reported_as_clean(tmp_path, capsys):
    """The PR-shape check runs outside the per-recipe loop and contributes to
    the total, so one advisory nit from it made "every recipe failed" look
    like "we found something" and the lane exited green on a broken checker."""
    _recipe(tmp_path, "contrib/python/alpha", **{"manifest.yaml": MANIFEST})
    changed = tmp_path / "changed.txt"
    changed.write_text(
        "contrib/python/alpha/manifest.yaml\n.agents/skills/x/SKILL.md\n"
    )
    out = tmp_path / "findings.json"
    rc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "house_rules_lane.py"),
            "--checker",
            str(CHECKER),
            "--repo-root",
            str(tmp_path),
            "--changed-files",
            str(changed),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "tools"),
        },
    )
    # Sanity: the healthy case exits 0 and does find the mixed-PR nit.
    assert rc.returncode == 0, rc.stderr
    assert any(f["_rule"] == "H42" for f in json.loads(out.read_text()))


def test_a_new_recipe_with_no_manifest_yet_is_still_reviewed(tmp_path):
    """Walking up to a manifest misses the case most in need of review: a NEW
    recipe whose manifest has not been written. Found on live PR 2627, which
    adds contrib/python/software-bug-assistant with a Dockerfile and a README
    and no manifest — the lane reported nothing to do."""
    (tmp_path / "contrib/python/new-thing").mkdir(parents=True)
    (tmp_path / "contrib/python/new-thing/Dockerfile").write_text("FROM x\n")
    roots = lane.recipe_roots(["contrib/python/new-thing/Dockerfile"], tmp_path)
    assert roots == ["contrib/python/new-thing"]


def test_a_file_beside_the_recipes_is_still_not_a_recipe(tmp_path):
    """The fallback must not undo the lookahead: three segments is a file
    sitting next to the recipes, not a recipe."""
    assert lane.recipe_roots(["contrib/python/README.md"], tmp_path) == []
    assert lane.recipe_roots(["core/AGENTS.md"], tmp_path) == []


def test_a_misplaced_solution_resolves_to_itself_not_its_subdirectories(
    tmp_path,
):
    """A solution directly under skills/ is H41's entire subject. With no
    manifest, the four-segment rule resolved its scripts/ and tests/ as two
    separate recipes and the real root as none — so the rule could never fire
    on the thing it exists to report, and the lane printed "0 findings across
    2 recipes"."""
    (tmp_path / "skills/store-ops/scripts").mkdir(parents=True)
    (tmp_path / "skills/store-ops/tests").mkdir()
    for marker in ("SKILL.md", "EVAL.yaml", "README.md"):
        (tmp_path / "skills/store-ops" / marker).write_text("x\n")
    roots = lane.recipe_roots(
        [
            "skills/store-ops/SKILL.md",
            "skills/store-ops/scripts/run.sh",
            "skills/store-ops/tests/test_x.py",
        ],
        tmp_path,
    )
    assert roots == ["skills/store-ops"]


def test_a_correctly_placed_solution_is_unaffected(tmp_path):
    (tmp_path / "skills/retail/ops/src").mkdir(parents=True)
    (tmp_path / "skills/retail/ops/SKILL.md").write_text("x\n")
    assert lane.recipe_roots(["skills/retail/ops/src/a.ts"], tmp_path) == [
        "skills/retail/ops"
    ]


def test_a_long_citation_is_shortened_rather_than_dropped():
    """`evidence` can be six full repository paths. Appended whole it pushed
    bodies to 788 characters, past the 600-char shape gate — which then
    dropped them, so the most-cited findings were the ones the author never
    saw. 14% of this repo's house-rule findings were lost that way."""
    long_evidence = "; ".join(
        f"core/python/x/deeply/nested/module_{i}.py:120" for i in range(6)
    )
    out = lane.to_reviewer_finding(
        {"path": "p", "line": 1, "what": "x" * 400, "evidence": long_evidence}
    )
    assert len(out["body"]) <= 600
    assert "and 5 more" in out["body"]
