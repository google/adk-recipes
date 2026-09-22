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
"""Unit tests for prepare_review_diff.py.

This script decides what the AI reviewer is allowed to see and how many
comments it is asked for. Both failure modes are quiet: dropping a file the
reviewer should have read produces a green check on unreviewed code, and a
budget computed off the wrong churn produces a review that is too thin or too
noisy for the PR it is on.

Every test below pins one of those.
"""

import re
import subprocess
import sys
from pathlib import Path

import post_review_comments
import prepare_review_diff as m
import pytest

SCRIPT = Path(m.__file__)


def _section(path: str, *rows: str) -> str:
    return "\n".join(
        [
            f"diff --git a/{path} b/{path}",
            "index 111..222 100644",
            f"--- a/{path}",
            f"+++ b/{path}",
            *rows,
        ]
    )


CODE = _section("pkg/agent.py", "@@ -1,1 +1,2 @@", " keep", "+added")


# --------------------------------------------------------------------------
# What gets dropped
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "recipe/uv.lock",
        "package-lock.json",
        "vendor/dep/thing.go",
        "web/node_modules/pkg/index.js",
        "dist/bundle.js",
        "app/__snapshots__/view.snap",
        "static/logo.png",
        "proto/service_pb2.py",
        "tests/testdata/big.json",
    ],
)
def test_unreviewable_files_are_dropped(path):
    """Every one of these displaces hand-written code from the prompt.

    The prompt has a hard byte budget, so on a PR that mixes a regenerated
    lockfile with real code the lockfile wins by being longer and the code it
    was generated from never reaches the model.
    """
    diff = _section(path, "@@ -1,1 +1,2 @@", " keep", "+added") + "\n" + CODE
    filtered, stats = m.filter_diff(diff)
    assert path not in filtered
    assert "pkg/agent.py" in filtered
    assert stats["kept_files"] == 1


def test_a_small_yaml_file_is_kept():
    """Size is doing the work, not the extension.

    A 20-line manifest.yaml is hand-written and very much worth reviewing; a
    5000-line one is a data dump.
    """
    diff = _section(
        "recipe/manifest.yaml", "@@ -1,1 +1,2 @@", " keep", "+added"
    )
    filtered, stats = m.filter_diff(diff)
    assert "manifest.yaml" in filtered
    assert stats["kept_files"] == 1


def test_a_large_data_file_is_dropped_whatever_it_is_called():
    rows = ["@@ -1,1 +1,600 @@"] + [f"+row {n}" for n in range(600)]
    diff = _section("eval/cases.json", *rows)
    _filtered, stats = m.filter_diff(diff)
    assert stats["kept_files"] == 0
    assert stats["skipped"][0][1] == "bulk data"


def test_a_deleted_file_is_dropped():
    """Nothing to fix in code the PR removes, and no RIGHT side to anchor to."""
    diff = "\n".join(
        [
            "diff --git a/old.py b/old.py",
            "deleted file mode 100644",
            "--- a/old.py",
            "+++ /dev/null",
            "@@ -1,2 +0,0 @@",
            "-gone",
            "-also gone",
        ]
    )
    _filtered, stats = m.filter_diff(diff)
    assert stats["kept_files"] == 0
    assert stats["reviewable_lines"] == 0


def test_a_pure_rename_is_dropped():
    """A file moved with no content change has nothing to review.

    On a migration PR this is most of the diff — 65 of 123 files on #2373.
    """
    diff = "\n".join(
        [
            "diff --git a/a/x.py b/b/x.py",
            "similarity index 100%",
            "rename from a/x.py",
            "rename to b/x.py",
        ]
    )
    _filtered, stats = m.filter_diff(diff)
    assert stats["kept_files"] == 0
    assert stats["skipped"][0][1] == "no content change"


def test_an_unparseable_diff_is_passed_through_whole():
    """Degrade to reviewing everything, never to reviewing nothing.

    Silently discarding a diff we failed to parse would turn a format change
    into a green check on an unreviewed PR.
    """
    diff = "something that is not a git diff at all\n"
    filtered, stats = m.filter_diff(diff)
    assert "not a git diff" in filtered
    assert stats["kept_files"] == 0


# --------------------------------------------------------------------------
# Churn and budget
# --------------------------------------------------------------------------


def test_churn_counts_both_sides_but_not_the_file_headers():
    diff = _section(
        "pkg/agent.py", "@@ -1,2 +1,2 @@", " keep", "-removed", "+added"
    )
    _filtered, stats = m.filter_diff(diff)
    assert stats["reviewable_lines"] == 2


def test_a_lockfile_does_not_inflate_the_budget():
    """The whole reason the budget is computed after filtering.

    A PR that is 1400 lines of regenerated lockfile plus 80 lines of code is
    a small PR, and asking for a large-PR number of comments on it produces
    padding.
    """
    lock_rows = ["@@ -1,1 +1,1400 @@"] + [f"+dep {n}" for n in range(1400)]
    diff = _section("uv.lock", *lock_rows) + "\n" + CODE
    _filtered, stats = m.filter_diff(diff)
    assert stats["reviewable_lines"] == 1
    assert m.budget_for(stats["reviewable_lines"]) == 2


@pytest.mark.parametrize(
    ("churn", "expected"),
    [(0, 2), (49, 2), (50, 2), (51, 3), (200, 3), (201, 5), (100_000, 5)],
)
def test_budget_scales_with_reviewable_churn(churn, expected):
    assert m.budget_for(churn) == expected


def test_the_per_lane_budget_never_exceeds_the_global_cap():
    """Four lanes run concurrently and cannot coordinate.

    Each is given a share rather than the whole budget, so the total stays at
    the cap however many findings the other three come back with.
    """
    largest = max(budget for _ceiling, budget in m.BUDGET_TABLE)
    assert largest * m.LANE_COUNT <= m.GLOBAL_CAP


# --------------------------------------------------------------------------
# main() — as the workflow invokes it
# --------------------------------------------------------------------------


def _run(tmp_path: Path, diff: str) -> tuple[int, str, dict]:
    src = tmp_path / "pr_diff.txt"
    src.write_text(diff, encoding="utf-8")
    out = tmp_path / "pr_diff_reviewable.txt"
    gho = tmp_path / "github_output"
    gho.touch()
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--diff",
            str(src),
            "--out",
            str(out),
            "--github-output",
            str(gho),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    outputs = dict(
        row.split("=", 1)
        for row in gho.read_text(encoding="utf-8").splitlines()
        if "=" in row
    )
    return proc.returncode, out.read_text(encoding="utf-8"), outputs


def test_main_writes_the_filtered_diff_and_its_outputs(tmp_path):
    code, filtered, outputs = _run(tmp_path, CODE)
    assert code == 0
    assert "pkg/agent.py" in filtered
    assert outputs["reviewable"] == "true"
    assert outputs["reviewable_lines"] == "1"
    assert outputs["budget"] == "2"


def test_main_reports_a_lockfile_only_pr_as_unreviewable(tmp_path):
    """This is what stops the workflow burning a model call on nothing.

    It also has to be a clean skip rather than a failure: a Dependabot PR is
    not a broken PR.
    """
    lock = _section("uv.lock", "@@ -1,1 +1,2 @@", " keep", "+added")
    code, filtered, outputs = _run(tmp_path, lock)
    assert code == 0
    assert filtered.strip() == ""
    assert outputs["reviewable"] == "false"


def test_the_filtered_diff_still_parses_as_a_diff(tmp_path):
    """The reviewer's anchors are computed from whatever survives here.

    A filter that corrupted the diff structure would put comments on real but
    wrong lines, which passes validation and gets posted.
    """
    diff = (
        _section("uv.lock", "@@ -1,1 +1,2 @@", " keep", "+dep")
        + "\n"
        + _section("pkg/agent.py", "@@ -10,1 +10,2 @@", " keep", "+added")
    )
    _code, filtered, _outputs = _run(tmp_path, diff)
    assert post_review_comments.added_line_anchors(filtered) == {
        "pkg/agent.py": {11}
    }


def test_workflow_invokes_this_script_with_the_flags_it_defines():
    """Pin the workflow -> CLI contract.

    The script is called from a shell block in _ai-pr-review-core.yml, so a
    renamed flag or a moved file is invisible to both ruff and pytest and only
    shows up as a review that never runs.
    """
    import yaml

    workflow = (
        Path(__file__).resolve().parents[3]
        / ".github"
        / "workflows"
        / "_ai-pr-review-core.yml"
    )
    steps = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"][
        "review"
    ]["steps"]
    prepare = next(s for s in steps if s.get("id") == "prepare_diff")

    invocation = prepare["run"]
    assert "python3 .github/scripts/prepare_review_diff.py" in invocation
    for flag in ("--diff", "--out", "--github-output"):
        assert flag in invocation, f"workflow no longer passes {flag}"

    defined = {
        opt
        for action in m.build_parser()._actions
        for opt in action.option_strings
    }
    assert {"--diff", "--out", "--github-output"} <= defined


def test_the_budget_the_workflow_reads_is_the_one_this_script_writes():
    """The prompt interpolates ${{ steps.prepare_diff.outputs.budget }}.

    An output renamed here and not there yields an empty budget line in the
    prompt, which reads as "no budget" and puts the reviewer straight back to
    the one-or-two comments this change exists to fix.
    """
    core = (
        Path(__file__).resolve().parents[3]
        / ".github"
        / "workflows"
        / "_ai-pr-review-core.yml"
    ).read_text(encoding="utf-8")
    for name in ("budget", "reviewable_lines", "reviewable"):
        assert f"steps.prepare_diff.outputs.{name}" in core


# --------------------------------------------------------------------------
# Packing the diff into the prompt's byte budget
#
# The budget is real (agy takes the prompt as one argv string, Linux caps that
# at 131072) and it is routinely smaller than the diff. What matters is how it
# is SPENT. `head -c` spent it in git's order, which is roughly alphabetical:
# on PR #2626 that meant 662KB of diff, ~100KB of budget, four markdown files
# eating most of it, and not one of the fourteen source files reviewed.
# --------------------------------------------------------------------------


def _big(path: str, n: int) -> str:
    """A diff section for `path` with roughly `n` bytes of added lines."""
    rows = [f"@@ -1,1 +1,{n} @@", " context"]
    rows += ["+" + "x" * 58 for _ in range(max(1, n // 60))]
    return _section(path, *rows)


def _many_hunks(path: str, hunks: int) -> str:
    """A section of `hunks` small complete hunks.

    `_big` emits a single enormous hunk, and cutting that always destroys the
    only hunk it has, so such a file is omitted rather than shown in part.
    Exercising the partial path needs a file that can be cut BETWEEN hunks.
    """
    rows = []
    for h in range(hunks):
        rows += [
            f"@@ -{h * 20 + 1},2 +{h * 20 + 1},3 @@",
            " ctx",
            "+" + "w" * 120,
        ]
    return _section(path, *rows)


def test_no_budget_leaves_the_diff_alone():
    diff = CODE + "\n"
    assert m.pack_to_budget(diff, 0) == (diff, [], [])
    assert m.pack_to_budget(diff, -1) == (diff, [], [])


def test_a_diff_that_already_fits_is_untouched():
    diff = CODE + "\n"
    assert m.pack_to_budget(diff, 10**6) == (diff, [], [])


@pytest.mark.parametrize(
    "path,tier",
    [
        ("pkg/agent.py", 0),
        ("src/main.go", 0),
        (".github/workflows/ci.yml", 0),
        ("tests/test_agent.py", 1),
        ("pkg/tests/helpers.py", 1),
        ("pkg/test_agent.py", 1),
        ("pkg/agent_test.go", 1),
        ("README.md", 2),
        ("docs/design.rst", 2),
        ("notes.txt", 2),
    ],
)
def test_source_outranks_tests_which_outrank_prose(path, tier):
    assert m.review_tier(path) == tier


def test_source_is_packed_before_prose_of_the_same_size():
    """The regression in one assertion. Alphabetically `a_docs.md` sorts
    first and used to take the budget; the source file must win regardless."""
    diff = _big("a_docs.md", 40000) + "\n" + _big("z_code.py", 40000) + "\n"
    packed, _partial, omitted = m.pack_to_budget(diff, 45000)
    assert "z_code.py" in packed
    assert omitted == ["a_docs.md"]


def test_source_is_packed_before_tests():
    diff = _big("a_tests/test_x.py", 40000) + "\n" + _big("z_impl.py", 40000)
    packed, _partial, omitted = m.pack_to_budget(diff + "\n", 45000)
    assert "z_impl.py" in packed
    assert omitted == ["a_tests/test_x.py"]


def test_a_file_too_big_for_the_budget_does_not_take_it_all():
    diff = "\n".join(
        [_big("huge.py", 80000)]
        + [_big(f"small{i}.py", 4000) for i in range(8)]
    )
    packed, _partial, omitted = m.pack_to_budget(diff + "\n", 45000)
    shown = re.findall(r"^diff --git a/(\S+)", packed, re.M)
    assert "huge.py" not in shown, "the huge file starved the others again"
    assert len([p for p in shown if p.startswith("small")]) >= 7
    assert omitted == ["huge.py"]


def test_a_big_file_that_fits_still_does_not_starve_the_small_ones():
    """The sharper case, and the one the first version of this test missed:
    `big.py` FITS inside the budget, so a largest-first packer takes it and
    has room for one small file after. Smallest-first spends the same budget
    on eight files instead of two. On PR #2626 that difference was ten source
    files reviewed against none."""
    diff = "\n".join(
        [_big("big.py", 40000)] + [_big(f"small{i}.py", 4000) for i in range(8)]
    )
    packed, _partial, _omitted = m.pack_to_budget(diff + "\n", 45000)
    shown = re.findall(r"^diff --git a/(\S+)", packed, re.M)
    smalls = [p for p in shown if p.startswith("small")]
    assert len(smalls) == 8, (
        f"expected all eight small files, got {len(smalls)}: {shown}"
    )
    assert "big.py" not in shown


def test_every_shown_file_is_whole_or_explicitly_marked_truncated():
    diff = "\n".join(_big(f"f{i}.py", 20000) for i in range(6)) + "\n"
    packed, partial, _omitted = m.pack_to_budget(diff, 50000)
    for path in re.findall(r"^diff --git a/(\S+)", packed, re.M):
        if path in partial:
            continue
        body = packed.split(f"diff --git a/{path} ")[1]
        assert "truncated" not in body.split("diff --git")[0]


def test_a_partial_file_is_cut_on_a_hunk_boundary():
    """`head -c` cut mid-line, so the model's last file ended in half a
    statement and it reasoned about code it could not see the end of."""
    rows = []
    for h in range(40):
        rows += [f"@@ -{h * 10},2 +{h * 10},3 @@", " ctx", "+" + "y" * 200]
    diff = _section("one.py", *rows) + "\n"
    packed, partial, _omitted = m.pack_to_budget(diff, 9000)
    assert partial == ["one.py"]
    body = packed[: packed.index("[... this file was truncated here ...]")]
    # Nothing after the final complete hunk header may be a dangling fragment:
    # every retained line is a whole line from the original.
    original = set(diff.split("\n"))
    assert all(line in original for line in body.split("\n") if line)


def test_the_packed_diff_never_exceeds_the_budget():
    diff = "\n".join(_big(f"f{i}.py", 13000) for i in range(12)) + "\n"
    for budget in (8000, 20000, 45000, 90000):
        packed, _partial, _omitted = m.pack_to_budget(diff, budget)
        assert len(packed.encode()) <= budget, f"overran at {budget}"


def test_every_file_is_either_shown_or_named_unreviewed():
    """Silence on a file reads as approval. A file must never simply vanish."""
    diff = "\n".join(_big(f"f{i}.py", 15000) for i in range(10)) + "\n"
    packed, partial, omitted = m.pack_to_budget(diff, 40000)
    shown = set(re.findall(r"^diff --git a/(\S+)", packed, re.M))
    assert shown | set(omitted) == {f"f{i}.py" for i in range(10)}
    assert not (shown & set(omitted)), "a file was both shown and reported"
    assert set(partial) <= shown


def test_the_packed_diff_keeps_the_original_file_order():
    """Packing decides WHAT is shown, not what order the PR is read in."""
    diff = (
        "\n".join([_big("z.py", 4000), _big("a.py", 4000), _big("m.py", 4000)])
        + "\n"
    )
    packed, _partial, _omitted = m.pack_to_budget(diff, 10**6)
    assert re.findall(r"^diff --git a/(\S+)", packed, re.M) == [
        "z.py",
        "a.py",
        "m.py",
    ]


def test_an_unparseable_diff_is_cut_to_budget_not_handed_back_whole():
    """It has no `diff --git` sections, so there is nothing to pack -- but
    the result still has to FIT. The caller asserts the assembled prompt is
    within budget and exits non-zero when it is not, so returning the diff
    whole here does not degrade the review, it deletes it: a red check on a
    PR that nobody reviewed. `head -c` could never do that."""
    junk = "not a diff at all\njust some text\n" * 500
    packed, partial, omitted = m.pack_to_budget(junk, 100)
    assert len(packed.encode()) <= 100, "handed the caller an oversized diff"
    assert packed and junk.startswith(packed.rstrip("\n"))
    assert partial == [] and omitted == []


def test_a_preamble_larger_than_the_budget_is_cut_not_returned_whole():
    """Same failure by the other route: `diff --git` sections exist, but the
    preamble alone already fills the budget, so there is no room to pack
    into."""
    diff = "preamble line\n" * 4000 + CODE + "\n"
    packed, _partial, _omitted = m.pack_to_budget(diff, 500)
    assert len(packed.encode()) <= 500


@pytest.mark.parametrize("budget", [80, 500, 5000, 20000, 60000])
def test_the_result_never_exceeds_the_budget_on_any_shape_of_input(budget):
    """One assertion over every packing path: normal, unparseable, huge
    preamble, single oversized file. Whatever route the code takes, the
    caller's contract is the same and it is absolute."""
    shapes = {
        "normal": "\n".join(_big(f"f{i}.py", 9000) for i in range(6)) + "\n",
        "unparseable": "just text\n" * 3000,
        "huge preamble": "preamble\n" * 3000 + CODE + "\n",
        "one giant file": _big("one.py", 200000) + "\n",
        "empty": "",
    }
    for name, diff in shapes.items():
        packed, _partial, _omitted = m.pack_to_budget(diff, budget)
        assert len(packed.encode()) <= budget, f"{name} overran at {budget}"


def test_the_model_is_told_when_files_were_withheld():
    """The prompt carries the PR's full changed-file list, so a model shown
    ten diffs out of thirty-seven will otherwise reason about the other
    twenty-seven from their names. `head -c` appended "review only what is
    shown above" for that reason; the packer has to say it too."""
    diff = "\n".join(_big(f"f{i}.py", 15000) for i in range(10)) + "\n"
    packed, _partial, omitted = m.pack_to_budget(diff, 40000)
    assert omitted
    assert "Review ONLY what is shown above" in packed
    assert f"{len(omitted)} file(s) omitted entirely" in packed


def test_no_withholding_notice_when_everything_fits():
    diff = "\n".join(_big(f"f{i}.py", 2000) for i in range(3)) + "\n"
    packed, _partial, omitted = m.pack_to_budget(diff, 10**6)
    assert omitted == []
    assert "Review ONLY" not in packed


def test_omitted_files_are_listed_in_diff_order():
    """The author reads this list against their own PR. Pack order is tier
    then size ascending, which looks arbitrary from outside."""
    diff = (
        "\n".join(
            [
                _big("z_last.py", 30000),
                _big("a_first.py", 30000),
                _big("m_mid.py", 30000),
            ]
        )
        + "\n"
    )
    _packed, _partial, omitted = m.pack_to_budget(diff, 35000)
    assert omitted == sorted(
        omitted, key=["z_last.py", "a_first.py", "m_mid.py"].index
    )


def test_the_hard_cut_still_reports_what_it_dropped():
    """The fallback path used to return an empty omitted list. That makes
    unreviewed_files.txt empty, the review body silent, and a PR whose diff
    could not be shown collects a green check — a review of nothing looking
    exactly like a clean bill of health."""
    diff = (
        "preamble line\n" * 4000
        + _big("a.py", 9000)
        + "\n"
        + _big("b.py", 9000)
    )
    packed, _partial, omitted = m.pack_to_budget(diff + "\n", 500)
    assert len(packed.encode()) <= 500
    assert set(omitted) == {"a.py", "b.py"}, (
        f"files vanished without being reported: {omitted}"
    )


def test_a_diff_cut_to_nothing_still_names_every_file():
    """One line longer than the whole budget leaves no diff at all. The
    author must still be told, or the lane reports silence on everything."""
    diff = _section("giant.py", "@@ -1 +1,2 @@", "+" + "z" * 200000) + "\n"
    packed, _partial, omitted = m.pack_to_budget(diff, 100)
    assert len(packed.encode()) <= 100
    assert omitted == ["giant.py"]


def test_the_omission_notice_fits_the_bytes_reserved_for_it():
    """The reservation is what stops the notice pushing the prompt back over
    budget. If the wording grows past it, every packed diff overruns."""
    worst = m._omission_notice(["x"] * 10**6, ["y"] * 10**6)
    assert len(worst.encode()) <= m.OMISSION_NOTICE_BYTES


def test_the_hard_cut_path_also_tells_the_model_what_it_cannot_see():
    """The fallback is the path where the model sees LEAST, so it is the one
    that most needs the instruction. Adding the notice on the packing path
    and not this one left the worst case as the unguarded one."""
    diff = "preamble line\n" * 4000 + _big("a.py", 9000) + "\n"
    packed, _partial, omitted = m.pack_to_budget(diff, 2000)
    assert len(packed.encode()) <= 2000
    assert omitted == ["a.py"]
    assert "Review ONLY what is shown above" in packed


def test_a_budget_too_small_for_the_notice_spends_it_on_diff_instead():
    """A sentence of explanation is worth less than the only diff lines the
    reader is going to get, and the byte ceiling is absolute either way."""
    diff = "preamble line\n" * 4000 + _big("a.py", 9000) + "\n"
    packed, _partial, _omitted = m.pack_to_budget(diff, 300)
    assert len(packed.encode()) <= 300


# ------------------------------- review comments on PR #2632


def test_a_complete_trailing_hunk_is_kept_when_the_cut_lands_on_it():
    """Backing off to the last `@@` unconditionally threw away a whole hunk
    whenever the byte limit happened to land exactly on a hunk boundary --
    the one case where the trailing hunk is perfectly good. Reported as
    "unconditionally discards the last complete hunk"."""
    rows = [
        "diff --git a/x.py b/x.py",
        "index 1..2 100644",
        "--- a/x.py",
        "+++ b/x.py",
        "@@ -1,1 +1,2 @@",
        " ctx",
        "+one",
        "@@ -9,1 +9,2 @@",
        " ctx",
        "+two",
        "@@ -20,1 +20,2 @@",
        " ctx",
        "+three",
    ]
    text = "\n".join(rows)
    exact = len("\n".join(rows[:10]).encode()) + 1
    out = m._truncate_at_hunk(text, exact)
    assert "+two" in out, "a complete hunk that fitted was discarded"
    assert "+three" not in out, "kept a hunk that did not fit"


def test_an_incomplete_trailing_hunk_is_still_dropped():
    """The original behaviour has to survive: a hunk cut in half mid-body is
    worse than no hunk, because the model reasons about code whose end it
    cannot see."""
    rows = [
        "diff --git a/x.py b/x.py",
        "index 1..2 100644",
        "--- a/x.py",
        "+++ b/x.py",
        "@@ -1,1 +1,2 @@",
        " ctx",
        "+one",
        "@@ -9,1 +9,5 @@",
        " ctx",
        "+two",
    ]
    text = "\n".join(rows) + "\n+three\n+four\n+five"
    cut_to = len("\n".join(rows).encode()) + 1
    out = m._truncate_at_hunk(text, cut_to)
    assert "+one" in out
    assert "@@ -9,1 +9,5 @@" not in out, "kept a half-finished hunk"


def test_truncate_returns_the_text_unchanged_when_it_all_fits():
    text = _big("x.py", 100)
    assert m._truncate_at_hunk(text, 10**6) == text


@pytest.mark.parametrize(
    "rows,complete",
    [
        (["@@ -1,1 +1,2 @@", " ctx", "+a"], True),
        (["@@ -1,1 +1,2 @@", " ctx"], False),
        (["@@ -1 +1 @@", " ctx"], True),  # absent counts mean 1
        (["@@ -1,0 +1,2 @@", "+a", "+b"], True),
        (["@@ -1,2 +1,2 @@", " ctx"], False),
        (["not a hunk header"], False),
        ([], False),
        # "\ No newline at end of file" belongs to neither side's count.
        (["@@ -1,0 +1,1 @@", "+a", "\\ No newline at end of file"], True),
    ],
)
def test_hunk_completeness_is_read_off_the_header(rows, complete):
    assert m._hunk_is_complete(rows) is complete


def test_the_truncation_marker_fits_the_bytes_reserved_for_it():
    """`TRUNCATED_MARKER_BYTES` is what the packer holds back before cutting.
    If the marker outgrows it, every partial file overruns the budget."""
    assert len(m.TRUNCATED_MARKER.encode()) + 2 <= m.TRUNCATED_MARKER_BYTES


def test_the_partial_marker_in_the_output_is_the_shared_constant():
    """The test file used to carry its own copy of the wording, so editing
    the real one left the assertion passing against a string nothing emits."""
    diff = "\n".join(_many_hunks(f"f{i}.py", 60) for i in range(6)) + "\n"
    packed, partial, _omitted = m.pack_to_budget(diff, 50000)
    assert partial, "no file was truncated, so the marker proves nothing"
    assert m.TRUNCATED_MARKER in packed


# --------------------------------------------------------------------------
# Only this PR's files (the #2628 contamination)
#
# The lanes review `compare/<last reviewed>...<head>` so a push that only
# fixes earlier comments has little new to say. Merge the BASE branch in and
# that compare also carries everything that landed on base in between. On
# #2628 the PR changed 10 files and the lanes read 37; the other 27 were
# #2626's, and the author was told 31 files "were not looked at".
# --------------------------------------------------------------------------


def test_files_outside_the_pr_are_dropped():
    diff = "\n".join(
        [
            _section("mine/a.py", "@@ -1 +1,2 @@", " ctx", "+mine"),
            _section("theirs/b.py", "@@ -1 +1,2 @@", " ctx", "+theirs"),
        ]
    )
    out, stats = m.filter_diff(diff, {"mine/a.py"})
    assert "mine/a.py" in out
    assert "theirs/b.py" not in out
    assert stats["foreign"] == ["theirs/b.py"]
    assert stats["kept_files"] == 1


def test_a_foreign_file_is_not_reported_as_skipped_or_unreviewed():
    """It is not something the author declined to have reviewed. Listing it
    is the same confusion with the sign flipped -- #2628's author was handed
    31 filenames they had never touched."""
    diff = _section("theirs/b.py", "@@ -1 +1,2 @@", " ctx", "+theirs")
    _out, stats = m.filter_diff(diff, {"mine/a.py"})
    assert stats["foreign"] == ["theirs/b.py"]
    assert stats["skipped"] == [], "a foreign file was reported to the author"


def test_no_list_means_no_filtering():
    diff = _section("anything.py", "@@ -1 +1,2 @@", " ctx", "+x")
    out, stats = m.filter_diff(diff, None)
    assert "anything.py" in out
    assert stats["foreign"] == []


def test_the_pr_file_list_is_applied_before_every_other_rule():
    """A foreign lockfile must be dropped as foreign, not counted as a skip:
    the two mean different things and only one of them is the author's."""
    diff = _section("theirs/uv.lock", "@@ -1 +1,2 @@", " ctx", "+dep")
    _out, stats = m.filter_diff(diff, {"mine/a.py"})
    assert stats["foreign"] == ["theirs/uv.lock"]
    assert stats["skipped"] == []


def test_an_empty_list_fails_open_rather_than_reviewing_nothing(tmp_path):
    """Reviewing too much is a bug. Reviewing nothing and reporting no
    findings is indistinguishable from a clean PR, which is a lie."""
    diff = tmp_path / "d.txt"
    diff.write_text(_section("a.py", "@@ -1 +1,2 @@", " ctx", "+x"))
    empty = tmp_path / "none.txt"
    empty.write_text("")
    out = tmp_path / "o.txt"
    rc = subprocess.run(
        [
            sys.executable,
            str(Path(m.__file__)),
            "--diff",
            str(diff),
            "--out",
            str(out),
            "--only-files",
            str(empty),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc.returncode == 0, rc.stderr
    assert "a.py" in out.read_text(), "an empty list silenced the whole review"


def test_a_missing_list_file_fails_open(tmp_path):
    diff = tmp_path / "d.txt"
    diff.write_text(_section("a.py", "@@ -1 +1,2 @@", " ctx", "+x"))
    out = tmp_path / "o.txt"
    rc = subprocess.run(
        [
            sys.executable,
            str(Path(m.__file__)),
            "--diff",
            str(diff),
            "--out",
            str(out),
            "--only-files",
            str(tmp_path / "does-not-exist.txt"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc.returncode == 0, rc.stderr
    assert "a.py" in out.read_text()


def test_the_2628_shape_end_to_end():
    """The real case, reduced: one file the PR owns, many it does not,
    because the author merged the base branch in."""
    pr_files = {"docs/guide.md"}
    diff = "\n".join(
        [_section("docs/guide.md", "@@ -1 +1,2 @@", " ctx", "+theirs")]
        + [
            _section(f"other/f{i}.py", "@@ -1 +1,2 @@", " ctx", "+x")
            for i in range(27)
        ]
    )
    out, stats = m.filter_diff(diff, pr_files)
    shown = re.findall(r"^diff --git a/(\S+)", out, re.M)
    assert shown == ["docs/guide.md"]
    assert len(stats["foreign"]) == 27


def test_the_workflow_passes_the_pr_file_list_to_the_filter():
    """The flag is useless unless the workflow actually supplies it, and the
    list has to be PAGED — the unpaged endpoint stops at 100 silently."""
    import yaml

    workflow = (
        Path(__file__).resolve().parents[3]
        / ".github"
        / "workflows"
        / "_ai-pr-review-core.yml"
    )
    steps = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"][
        "review"
    ]["steps"]
    prepare = next(s for s in steps if s.get("id") == "prepare_diff")
    run = str(prepare["run"])
    assert "--only-files pr_files.txt" in run
    assert "pulls/${PR_NUMBER}/files" in run
    assert "--paginate" in run, "an unpaged list silently stops at 100 files"


# ------------------------------------- paths as git prints them vs the API
#
# With `core.quotePath` on (the default) git wraps any path holding a space,
# a quote or a non-ASCII byte in double quotes and C-escapes it. The API
# reports the real name. Now that the two are COMPARED to decide whether a
# file is reviewed at all, a mismatch is not cosmetic: the file drops out of
# the review and nothing says so.


@pytest.mark.parametrize(
    "plus_line,expected",
    [
        ("+++ b/plain.py", "plain.py"),
        ('+++ "b/my recipe/agent.py"', "my recipe/agent.py"),
        ('+++ "b/caf\\303\\251.py"', "café.py"),
        ('+++ "b/tab\\there.py"', "tab\there.py"),
        ('+++ "b/quote\\".py"', 'quote".py'),
        ('+++ "b/back\\\\slash.py"', "back\\slash.py"),
    ],
)
def test_a_quoted_path_is_read_as_the_name_the_api_reports(plus_line, expected):
    section = [
        "diff --git a/x b/x",
        "index 1..2 100644",
        "--- a/x",
        plus_line,
        "@@ -1 +1,2 @@",
        " ctx",
        "+x",
    ]
    assert m._section_path(section) == expected


def test_a_quoted_rename_falls_back_to_the_header_and_still_unquotes():
    """A pure rename carries no `+++` line, so the `diff --git` header is the
    only source — and it is quoted too."""
    section = [
        'diff --git "a/old name.py" "b/new name.py"',
        "similarity index 100%",
        "rename from old name.py",
        "rename to new name.py",
    ]
    assert m._section_path(section) == "new name.py"


def test_a_path_with_a_space_is_not_dropped_as_foreign():
    """The failure this guards: parsed as `\"b/my recipe/a.py\"`, compared
    against the API's `my recipe/a.py`, never equal, silently unreviewed."""
    diff = "\n".join(
        [
            'diff --git "a/my recipe/a.py" "b/my recipe/a.py"',
            "index 1..2 100644",
            '--- "a/my recipe/a.py"',
            '+++ "b/my recipe/a.py"',
            "@@ -1 +1,2 @@",
            " ctx",
            "+x = 1",
        ]
    )
    out, stats = m.filter_diff(diff, {"my recipe/a.py"})
    assert stats["foreign"] == [], "a real PR file was dropped as foreign"
    assert stats["kept_files"] == 1
    assert "my recipe/a.py" in out


@pytest.mark.parametrize(
    "raw",
    [
        '"b/\\377\\376.py"',  # valid bytes, not valid UTF-8
        '"b/bad\\777.py"',  # 511: the grammar allows it, bytes() will not
        '"b/hi\\400.py"',  # 256: the first value off the end of a byte
    ],
)
def test_an_undecodable_escape_is_left_alone_rather_than_mangled(raw):
    """A wrong name is worse than a quoted one: it would match nothing AND
    read as though it were the real path. And it must not RAISE -- `\\400`
    upward are inside the escape grammar and outside `bytes()`, so catching
    only UnicodeDecodeError let a malformed path kill the whole lane."""
    assert m._unquote_git_path(raw) == raw


def test_a_malformed_path_does_not_take_the_filter_down_with_it():
    diff = "\n".join(
        [
            'diff --git "a/bad\\777.py" "b/bad\\777.py"',
            "index 1..2 100644",
            '--- "a/bad\\777.py"',
            '+++ "b/bad\\777.py"',
            "@@ -1 +1,2 @@",
            " ctx",
            "+x",
        ]
    )
    out, stats = m.filter_diff(diff, {"something/else.py"})
    assert stats["foreign"], "expected it dropped, but the point is no crash"
    assert out is not None


def test_unquoting_leaves_an_unquoted_path_untouched():
    assert m._unquote_git_path("b/plain.py") == "b/plain.py"
    assert m._unquote_git_path("") == ""
    assert m._unquote_git_path('"') == '"'


def test_the_workflow_retries_the_file_list_and_fails_open():
    """This step made no network call before, so an unretried blip would turn
    a transient API hiccup into a failed lane on a good PR. And a list that
    could not be fetched must filter NOTHING — treated as authoritative it
    would review nothing and report a clean PR."""
    import yaml

    workflow = (
        Path(__file__).resolve().parents[3]
        / ".github"
        / "workflows"
        / "_ai-pr-review-core.yml"
    )
    steps = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"][
        "review"
    ]["steps"]
    run = str(next(s for s in steps if s.get("id") == "prepare_diff")["run"])
    assert "for attempt in 1 2 3; do" in run, "no retry on the file list"
    # Written aside and moved only on success: a run that dies mid-pagination
    # must not leave a SHORT list behind, which filters silently.
    assert "pr_files.partial" in run
    assert "mv pr_files.partial pr_files.txt" in run
    assert ": > pr_files.txt" in run, "no fail-open branch"


# ------------------------------------------- review comments on PR #2635


def test_a_deletion_from_the_base_branch_is_foreign_not_the_authors():
    """`_section_path` is None for a deletion, so the ownership test used to
    be reached with nothing to test and the file fell through to `skipped` as
    one the AUTHOR deleted. It is dropped either way -- deletions carry
    nothing to review -- but the log is what someone reads to work out why a
    review looks wrong, and it was naming the wrong person."""
    deletion = "\n".join(
        [
            "diff --git a/theirs/gone.py b/theirs/gone.py",
            "deleted file mode 100644",
            "index 1..0000000",
            "--- a/theirs/gone.py",
            "+++ /dev/null",
            "@@ -1,2 +0,0 @@",
            "-was",
            "-here",
        ]
    )
    _out, stats = m.filter_diff(deletion, {"mine/a.py"})
    assert stats["foreign"] == ["theirs/gone.py"]
    assert stats["skipped"] == [], "reported as the author's own deletion"


def test_the_authors_own_deletion_is_still_reported_as_skipped():
    """The other half: a deletion the PR really does own is not foreign."""
    deletion = "\n".join(
        [
            "diff --git a/mine/gone.py b/mine/gone.py",
            "deleted file mode 100644",
            "--- a/mine/gone.py",
            "+++ /dev/null",
            "@@ -1 +0,0 @@",
            "-was",
        ]
    )
    _out, stats = m.filter_diff(deletion, {"mine/gone.py"})
    assert stats["foreign"] == []
    assert [(p, r) for p, r, _ in stats["skipped"]] == [
        ("mine/gone.py", "deleted")
    ]


def test_a_quoted_deletion_matches_the_api_spelling_too():
    deletion = "\n".join(
        [
            'diff --git "a/my recipe/gone.py" "b/my recipe/gone.py"',
            "deleted file mode 100644",
            '--- "a/my recipe/gone.py"',
            "+++ /dev/null",
            "@@ -1 +0,0 @@",
            "-was",
        ]
    )
    _out, stats = m.filter_diff(deletion, {"my recipe/gone.py"})
    assert stats["foreign"] == []


def test_an_unparseable_section_is_not_called_foreign():
    """ "I could not parse this" and "this belongs to someone else" are
    different answers, and only one of them is a fact."""
    junk = "\n".join(["diff --git nonsense", "+++ /dev/null", "@@ -1 +0,0 @@"])
    _out, stats = m.filter_diff(junk, {"mine/a.py"})
    assert stats["foreign"] == []
    assert stats["skipped"] == [(m.UNKNOWN_PATH, "deleted", 0)]


def test_an_unreadable_file_list_reports_once_and_does_not_claim_empty(
    tmp_path, capsys
):
    """The OSError branch fell through into the shared empty-check, so one
    unreadable file produced both "cannot read" and "is empty" -- two
    messages, the second untrue."""
    diff = tmp_path / "d.txt"
    diff.write_text(_section("a.py", "@@ -1 +1,2 @@", " ctx", "+x"))
    out = tmp_path / "o.txt"
    unreadable = tmp_path / "nope.txt"
    rc = subprocess.run(
        [
            sys.executable,
            str(Path(m.__file__)),
            "--diff",
            str(diff),
            "--out",
            str(out),
            "--only-files",
            str(unreadable),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc.returncode == 0, rc.stderr
    assert "cannot read" in rc.stdout
    assert "is empty" not in rc.stdout, "claimed an unreadable file was empty"
    assert "a.py" in out.read_text(), "should still fail open"
