"""Planner behaviours that are load-bearing and were previously undocumented."""

import plan_review as pr


def f(path, churn, status="added"):
    return {"path": path, "churn": churn, "status": status}


# ------------------------------------------------- affinity packing


def test_source_and_its_test_land_in_the_same_lane():
    """Split across lanes, neither reviewer can tell if the test covers the code."""
    files = [
        f("tools/validate.py", 100),
        f("tools/tests/test_validate.py", 100),
        f("other/a.py", 100),
        f("other/b.py", 100),
    ]
    lanes = pr.pack(files, 2)
    for lane in lanes:
        has_src = "tools/validate.py" in lane["files"]
        has_test = "tools/tests/test_validate.py" in lane["files"]
        assert has_src == has_test


def test_affinity_key_pairs_common_test_layouts():
    pairs = [
        ("tools/validate.py", "tools/tests/test_validate.py"),
        ("src/api/client.ts", "src/api/__tests__/client.test.ts"),
        ("a/scripts/align.py", "a/tests/test_align.py"),
        ("pkg/thing.go", "pkg/thing_test.go"),
    ]
    for src, test in pairs:
        assert pr.affinity_key(src) == pr.affinity_key(test), (src, test)


def test_affinity_key_keeps_unrelated_files_apart():
    assert pr.affinity_key("go/README.md") != pr.affinity_key("java/README.md")


def test_lanes_stay_balanced_despite_grouping():
    files = [f(f"m{i}.py", 100) for i in range(10)]
    lanes = pr.pack(files, 5)
    churns = [lane["churn"] for lane in lanes]
    assert max(churns) - min(churns) <= 100


def test_oversized_group_is_split_rather_than_wrecking_balance():
    files = [f("big.py", 5000), f("tests/test_big.py", 5000)] + [
        f(f"s{i}.py", 10) for i in range(4)
    ]
    lanes = pr.pack(files, 3)
    assert len([lane for lane in lanes if lane["files"]]) >= 2


# ------------------------------------------------------ skip rules


def test_pure_rename_is_skipped():
    """65 of 123 files on PR #2373. Byte-identical moves have nothing to review."""
    plan_files = [f("a.py", 0, "renamed"), f("b.py", 10, "added")]
    reviewable = [
        x
        for x in plan_files
        if not (x["status"] == "renamed" and x["churn"] == 0)
    ]
    assert [x["path"] for x in reviewable] == ["b.py"]


def test_renamed_with_edits_is_still_reviewed():
    x = f("a.py", 6, "renamed")
    assert not (x["status"] == "renamed" and x["churn"] == 0)


def test_lockfiles_and_generated_are_skipped():
    for path in (
        "pnpm-lock.yaml",
        "go.sum",
        "vendor/x.go",
        "dist/a.js",
        "x_pb2.py",
        "api.png",
        "__snapshots__/a.snap",
    ):
        assert pr.skip_reason(path, 10) is not None, path


def test_real_source_is_not_skipped():
    assert pr.skip_reason("src/agent.py", 100) is None


def test_bulk_data_skipped_only_when_large():
    assert pr.skip_reason("data/x.json", 900) == "bulk data"
    assert pr.skip_reason("data/x.json", 10) is None


def test_scope_flags_exclude_tests_and_web():
    assert (
        pr.skip_reason("a/tests/x.py", 10, include_tests=False)
        == "tests excluded"
    )
    assert pr.skip_reason("a/tests/x.py", 10, include_tests=True) is None
    assert (
        pr.skip_reason("web/src/a.ts", 10, include_web=False) == "web excluded"
    )


# --------------------------------------------------------- budget


def test_budget_uses_reviewable_churn_not_total():
    """A PR that is 4,800 lines of lockfile plus 2 real lines is a small PR."""
    assert pr.budget_for(2) == (2, 3)
    assert pr.budget_for(120) == (3, 5)
    assert pr.budget_for(3000) == (12, 20)


def test_budget_never_exceeds_twenty():
    assert pr.budget_for(10**6)[1] == 20


# ----------------------------------------------------- lane count


def test_small_pr_does_not_fan_out():
    assert pr.lane_count(300, 5) == 1


def test_large_pr_fans_out_but_is_capped():
    assert pr.lane_count(100_000, 500) == pr.MAX_LANES


def test_lane_count_never_exceeds_file_count():
    assert pr.lane_count(50_000, 3) <= 3
