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
"""The arithmetic that makes automated review converge.

Every number here is the difference between an author seeing the reviewer wind
down and an author feeling harassed by it, so the edges are tested rather than
assumed: the round after a round that found nothing, the round that exhausts
the cap, the allowance too small to give every lane a share.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review_budget as rb

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY = yaml.safe_load(
    (REPO_ROOT / ".github" / "policy.yml").read_text(encoding="utf-8")
)["pr_review_budget"]


def policy(**overrides):
    return {**rb.DEFAULTS, **POLICY, **overrides}


def review(commit, when, lane="Correctness", rid=None, body=None, bot=True):
    return {
        "id": rid if rid is not None else hash((commit, lane)) % 100000,
        "commit_id": commit,
        "submitted_at": when,
        # Only a GitHub App or Actions token can post as type Bot, so this is
        # what separates our reviews from a forgery. See is_ours.
        "user": {"login": "adk-bot[bot]", "type": "Bot" if bot else "User"},
        "body": body
        if body is not None
        else f"{rb.REVIEW_MARKER}\nAutomated **{lane}** review — 1 finding(s).",
    }


def comment(review_id):
    return {"pull_request_review_id": review_id}


# --------------------------------------------------------------- the policy


def test_the_shipped_policy_is_internally_consistent():
    """blocker_lanes must be a PREFIX of lanes, or the lanes that survive a
    shrinking allowance are not the ones that survive the round limit — and
    the reviewer would narrow to one set while allocating to another."""
    lanes = POLICY["lanes"]
    blockers = POLICY["blocker_lanes"]
    assert lanes[: len(blockers)] == blockers, (
        f"blocker_lanes {blockers} is not a prefix of lanes {lanes}"
    )
    assert 0 < POLICY["decay"] < 1
    assert POLICY["lifetime_cap"] > 0
    assert POLICY["min_allowance"] >= 1


def test_the_policy_lanes_are_the_real_lane_labels():
    """A label that matches no workflow silently drops that lane out of the
    allocation, giving every other lane a bigger share than intended."""
    labels = set()
    for path in (REPO_ROOT / ".github" / "workflows").glob(
        "ai-pr-review-*.yml"
    ):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "review_label:" in line:
                labels.add(line.split("review_label:")[1].strip().strip("'\""))
            if "--label '" in line:
                labels.add(line.split("--label '")[1].split("'")[0])
    for lane in POLICY["lanes"] + POLICY["exempt_lanes"]:
        assert lane in labels, (
            f"policy.yml names a lane {lane!r} that no workflow posts as; "
            f"workflows post as {sorted(labels)}"
        )


def test_defaults_match_the_shipped_policy():
    """The fallbacks are for an unreadable policy.yml, not a second opinion."""
    for key, value in rb.DEFAULTS.items():
        assert POLICY[key] == value, (
            f"policy.yml {key}={POLICY[key]!r} but the in-code default is "
            f"{value!r}; one of them is stale"
        )


# ------------------------------------------------------------ round history


def test_four_lanes_reviewing_one_push_is_one_round():
    """The lanes post four reviews against a single commit. Counting those as
    four rounds runs the decay four times per push and silences the reviewer
    on the second one."""
    reviews = [
        review("aaa", "2026-01-01T00:00:00Z", lane, rid=i)
        for i, lane in enumerate(POLICY["lanes"])
    ]
    state = rb.summarise_history(reviews, [], POLICY["exempt_lanes"])
    assert state["round"] == 2
    assert state["last_reviewed_sha"] == "aaa"


def test_the_round_number_counts_commits_reviewed():
    reviews = [
        review("aaa", "2026-01-01T00:00:00Z", rid=1),
        review("bbb", "2026-01-02T00:00:00Z", rid=2),
        review("ccc", "2026-01-03T00:00:00Z", rid=3),
    ]
    state = rb.summarise_history(reviews, [], POLICY["exempt_lanes"])
    assert state["round"] == 4
    assert state["last_reviewed_sha"] == "ccc"


def test_the_previous_round_count_is_only_the_last_commits_comments():
    reviews = [
        review("aaa", "2026-01-01T00:00:00Z", rid=1),
        review("bbb", "2026-01-02T00:00:00Z", rid=2),
    ]
    comments = [comment(1)] * 9 + [comment(2)] * 4
    state = rb.summarise_history(reviews, comments, POLICY["exempt_lanes"])
    assert state["posted_total"] == 13
    assert state["previous_round_count"] == 4


def test_a_humans_review_is_not_ours():
    reviews = [
        {
            "id": 1,
            "commit_id": "aaa",
            "submitted_at": "2026-01-01T00:00:00Z",
            "body": "Looks good, just one thing.",
        }
    ]
    state = rb.summarise_history(reviews, [comment(1)], POLICY["exempt_lanes"])
    assert state["round"] == 1
    assert state["posted_total"] == 0


def test_a_review_from_before_the_marker_still_counts():
    """Otherwise every PR already under review restarts at round 1 and gets a
    full-size batch the day this ships."""
    legacy = review(
        "aaa",
        "2026-01-01T00:00:00Z",
        body="Automated **Security** review — 3 finding(s).",
    )
    state = rb.summarise_history([legacy], [], POLICY["exempt_lanes"])
    assert state["round"] == 2


def test_the_exempt_lane_neither_advances_the_round_nor_spends_the_cap():
    reviews = [review("aaa", "2026-01-01T00:00:00Z", "House Rules", rid=7)]
    state = rb.summarise_history(
        reviews, [comment(7)] * 6, POLICY["exempt_lanes"]
    )
    assert state["round"] == 1, "the deterministic lane started the clock"
    assert state["posted_total"] == 0, "its comments consumed the model budget"


# ---------------------------------------------------------------- the decay


def test_the_documented_decay_curve():
    """20 -> 12 -> 7 -> 4 -> 2 -> 1 at 0.6. This is the shape an author sees."""
    seen, previous = [], 20
    for _ in range(5):
        state = {
            "round": 2,
            "last_reviewed_sha": "x",
            "posted_total": 0,
            "previous_round_count": previous,
        }
        previous = rb.decide(state, policy(), "Security", 5)["allowance"]
        seen.append(previous)
    assert seen == [12, 7, 4, 2, 1]


def test_the_allowance_never_reaches_zero():
    """A PR that grows a large new commit in round 9 still deserves one
    comment; the lifetime cap is what produces silence, not the decay."""
    state = {
        "round": 9,
        "last_reviewed_sha": "x",
        "posted_total": 0,
        "previous_round_count": 1,
    }
    assert rb.decide(state, policy(), "Security", 5)["allowance"] == 1


def test_a_round_that_found_nothing_does_not_silence_the_next():
    state = {
        "round": 3,
        "last_reviewed_sha": "x",
        "posted_total": 4,
        "previous_round_count": 0,
    }
    assert rb.decide(state, policy(), "Security", 5)["allowance"] == 1


def test_the_first_round_uses_the_churn_budget():
    state = {
        "round": 1,
        "last_reviewed_sha": "",
        "posted_total": 0,
        "previous_round_count": 0,
    }
    decision = rb.decide(state, policy(), "Security", 5)
    assert decision["allowance"] == 5 * len(POLICY["lanes"])
    assert decision["max_comments"] == 5


# ----------------------------------------------------------- the lifetime cap


def test_the_cap_stops_everything():
    state = {
        "round": 4,
        "last_reviewed_sha": "x",
        "posted_total": 25,
        "previous_round_count": 8,
    }
    decision = rb.decide(state, policy(), "Security", 5)
    assert decision["skip"] is True
    assert "at the 25 limit" in decision["reason"]


def test_the_cap_clips_a_round_that_would_overshoot():
    state = {
        "round": 2,
        "last_reviewed_sha": "x",
        "posted_total": 22,
        "previous_round_count": 20,
    }
    assert rb.decide(state, policy(), "Security", 5)["allowance"] == 3


def test_the_first_round_is_capped_too():
    state = {
        "round": 1,
        "last_reviewed_sha": "",
        "posted_total": 0,
        "previous_round_count": 0,
    }
    decision = rb.decide(state, policy(lifetime_cap=6), "Security", 5)
    assert decision["allowance"] == 6


# ------------------------------------------------------------ the allocation


def test_an_allowance_of_one_goes_to_exactly_one_lane():
    """Divide-and-round-up would turn 1 into 4 — at the tail of the decay,
    which is precisely where being exact matters."""
    got = {
        lane: rb.allocate(1, POLICY["lanes"], lane) for lane in POLICY["lanes"]
    }
    assert sum(got.values()) == 1
    assert got["Security"] == 1


@pytest.mark.parametrize("allowance", range(0, 21))
def test_the_shares_always_sum_to_the_allowance(allowance):
    total = sum(
        rb.allocate(allowance, POLICY["lanes"], lane)
        for lane in POLICY["lanes"]
    )
    assert total == allowance


def test_the_remainder_goes_to_the_higher_priority_lanes():
    shares = [rb.allocate(6, POLICY["lanes"], lane) for lane in POLICY["lanes"]]
    assert shares == [2, 2, 1, 1]


def test_a_lane_with_no_share_is_skipped_not_given_zero_comments():
    state = {
        "round": 2,
        "last_reviewed_sha": "x",
        "posted_total": 0,
        "previous_round_count": 2,
    }
    decision = rb.decide(state, policy(), "Hygiene", 5)
    assert decision["allowance"] == 1
    assert decision["skip"] is True
    assert "higher-priority lanes" in decision["reason"]


# ----------------------------------------------------------- the narrowing


def test_the_nit_lanes_stop_after_round_two():
    state = {
        "round": 3,
        "last_reviewed_sha": "x",
        "posted_total": 5,
        "previous_round_count": 8,
    }
    assert rb.decide(state, policy(), "Hygiene", 5)["skip"] is True
    assert rb.decide(state, policy(), "Maintainability", 5)["skip"] is True
    assert rb.decide(state, policy(), "Security", 5)["skip"] is False


def test_round_two_still_runs_everything():
    state = {
        "round": 2,
        "last_reviewed_sha": "x",
        "posted_total": 5,
        "previous_round_count": 20,
    }
    for lane in POLICY["lanes"]:
        assert rb.decide(state, policy(), lane, 5)["skip"] is False, lane


# -------------------------------------------------------------- exempt lane


def test_the_exempt_lane_is_never_skipped_or_capped():
    state = {
        "round": 12,
        "last_reviewed_sha": "x",
        "posted_total": 99,
        "previous_round_count": 0,
    }
    decision = rb.decide(state, policy(), "House Rules", 5)
    assert decision["skip"] is False
    assert decision["exempt"] is True
    assert decision["max_comments"] == 0  # no ceiling
    assert rb.progress_line(decision) == ""


# ------------------------------------------------------------- the author's view


def test_the_progress_line_names_the_round_and_what_is_left():
    state = {
        "round": 3,
        "last_reviewed_sha": "x",
        "posted_total": 18,
        "previous_round_count": 7,
    }
    line = rb.progress_line(rb.decide(state, policy(), "Security", 5))
    assert "Round 3" in line
    assert "18 of this PR's 25" in line
    assert "capped at 4" in line
    assert "Security and Correctness" in line


def test_the_first_round_does_not_threaten_the_author_with_narrowing():
    state = {
        "round": 1,
        "last_reviewed_sha": "",
        "posted_total": 0,
        "previous_round_count": 0,
    }
    line = rb.progress_line(rb.decide(state, policy(), "Security", 5))
    assert "from here only" not in line


# ------------------------------------------------------------------ the CLI


def test_a_full_review_clears_the_diff_scope_but_not_the_caps(monkeypatch):
    """`@ai-review` means "look at all of it", not "give me another 25"."""
    reviews = [review("aaa", "2026-01-01T00:00:00Z", rid=1)]
    monkeypatch.setattr(
        rb,
        "gh_json",
        lambda path: reviews if "reviews" in path else [comment(1)] * 20,
    )
    argv = [
        "review_budget.py",
        "--repo",
        "o/r",
        "--pr",
        "1",
        "--lane",
        "Security",
        "--full-review",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    rb.main()
    # Scope reset, history intact.
    state = rb.summarise_history(
        reviews, [comment(1)] * 20, POLICY["exempt_lanes"]
    )
    assert state["posted_total"] == 20
    assert rb.decide(state, policy(), "Security", 5)["allowance"] == 5


def test_an_unreadable_history_degrades_to_silence_not_a_full_batch(
    tmp_path, monkeypatch
):
    """Every number here comes from the review history, so without it the
    honest options are "assume round 1" and "say nothing".

    Assuming round 1 hands a fresh batch to a PR that has already had six --
    the exact failure this script exists to prevent. Failing the job is no
    better: a transient API error is not the contributor's problem, and a red
    check they cannot act on is worse than a quiet round they can recover
    with one more push.
    """
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(rb, "gh_json", lambda path: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "review_budget.py",
            "--repo",
            "o/r",
            "--pr",
            "1",
            "--lane",
            "Security",
            "--github-output",
        ],
    )
    assert rb.main() == 0
    written = dict(
        line.split("=", 1) for line in out.read_text().strip().splitlines()
    )
    assert written["skip"] == "true"
    assert written["max_comments"] == "0"
    assert written["progress"] == ""


def test_the_decision_reaches_github_output(tmp_path, monkeypatch):
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(
        rb,
        "gh_json",
        lambda path: (
            [review("aaa", "2026-01-01T00:00:00Z", rid=1)]
            if "reviews" in path
            else [comment(1)] * 10
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "review_budget.py",
            "--repo",
            "o/r",
            "--pr",
            "1",
            "--lane",
            "Security",
            "--github-output",
        ],
    )
    rb.main()
    written = dict(
        line.split("=", 1) for line in out.read_text().strip().splitlines()
    )
    assert written["round"] == "2"
    assert written["skip"] == "false"
    assert written["last_reviewed_sha"] == "aaa"
    assert written["max_comments"] == "2"  # floor(0.6 * 10) = 6, /4 -> 2


def test_no_output_value_can_span_two_lines(tmp_path, monkeypatch):
    """A newline in a $GITHUB_OUTPUT value forges a second output. Nothing
    here is built from PR content, but the guard is cheap and permanent."""
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    decision = {
        "round": 2,
        "last_reviewed_sha": "a\nb=evil",
        "posted_total": 0,
        "max_comments": 1,
        "skip": False,
        "exempt": False,
        "allowance": 1,
        "lifetime_cap": 25,
        "blocker_lanes": ["Security"],
        "narrow_after": 2,
        "reason": "one\ntwo",
    }
    rb.emit(decision, True)
    for line in out.read_text().strip().splitlines():
        assert line.count("=") >= 1
    assert "evil" not in out.read_text()


def test_the_script_runs_end_to_end():
    """Catches an import error or a syntax slip that unit tests importing the
    module would not, since the workflow shells out to it."""
    proc = subprocess.run(
        [sys.executable, str(Path(rb.__file__)), "--help"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "tools")},
    )
    assert proc.returncode == 0
    assert "--full-review" in proc.stdout


# ------------------------------------------------------- the workflow wiring

CORE = REPO_ROOT / ".github" / "workflows" / "_ai-pr-review-core.yml"


@pytest.fixture(scope="module")
def core():
    return yaml.safe_load(CORE.read_text(encoding="utf-8"))


def _step(core, step_id):
    for step in core["jobs"]["review"]["steps"]:
        if step.get("id") == step_id:
            return step
    raise AssertionError(f"no step with id {step_id!r}")


def test_the_budget_is_decided_before_the_diff_is_fetched(core):
    """The fetch scopes itself to `last_reviewed_sha`, which the budget step
    produces. The wrong order silently gives every round a full diff."""
    ids = [s.get("id") for s in core["jobs"]["review"]["steps"]]
    assert ids.index("budget") < ids.index("fetch_diff")


def test_the_budget_step_passes_the_flags_the_script_defines(core):
    run = _step(core, "budget")["run"]
    assert "review_budget.py" in run
    for flag in ("--repo", "--pr", "--lane", "--head-sha", "--github-output"):
        assert flag in run, f"the workflow no longer passes {flag}"


def test_a_manual_invoke_widens_the_scope_but_not_the_volume(core):
    """`--full-review` re-reads the whole PR. It must not also reset the caps,
    or `@ai-review` becomes a way to re-flood a pull request."""
    run = _step(core, "budget")["run"]
    assert "--full-review" in run
    assert "issue_comment" in run and "workflow_dispatch" in run
    assert "lifetime" not in run.lower(), (
        "the workflow appears to be overriding the cap on a manual invoke"
    )


def test_the_ceiling_and_the_progress_line_reach_the_poster(core):
    build = _step(core, "build_review")
    assert "--max-comments" in build["run"]
    assert "--progress" in build["run"]
    # prepare_diff's number, not the budget step's raw allowance: it is the
    # SMALLER of the round allowance and what a PR of this size warrants, and
    # it is the figure the model was told to aim at. Enforcing the larger one
    # lets a ten-line PR collect five comments a lane whenever the round
    # allowance happens to be generous.
    assert build["env"]["MAX_COMMENTS"].endswith(
        "prepare_diff.outputs.budget }}"
    )
    assert build["env"]["PROGRESS"].endswith("budget.outputs.progress }}")


def test_every_expensive_step_stops_when_the_lane_is_skipped(core):
    """The model call is the expensive one, and a skipped lane must not make
    it. Each of these gates on prepare_diff, which itself gates on the budget."""
    for step_id in ("auth", "agy_pr_review", "build_review"):
        condition = " ".join(_step(core, step_id)["if"].split())
        assert "reviewable == 'true'" in condition, (
            f"{step_id} tests reviewable for inequality; a SKIPPED "
            "prepare_diff leaves that output empty, and '' != 'false' is "
            "true — so the step would run after the budget stopped the lane"
        )


def test_the_fetch_falls_back_to_the_whole_pr_when_it_must(core):
    run = _step(core, "fetch_diff")["run"]
    assert "gh pr diff" in run, "there is no full-diff fallback left"
    assert "force-push" in run, "the orphaned-sha case is not handled"
    assert "compare/${LAST_REVIEWED_SHA}...${HEAD_SHA}" in run


def test_an_empty_incremental_diff_stops_rather_than_re_reviewing(core):
    run = _step(core, "fetch_diff")["run"]
    assert "empty=true" in run
    prepare = " ".join(_step(core, "prepare_diff")["if"].split())
    assert "fetch_diff.outputs.empty != 'true'" in prepare


def test_the_lane_count_matches_the_policy_lane_list():
    """prepare_review_diff.py divides the round-1 budget by a hardcoded lane
    count, because it runs without PyYAML. Adding a fifth throttled lane to
    policy.yml without changing it would silently give every lane a share
    that no longer adds up."""
    sys.path.insert(0, str(REPO_ROOT / ".github" / "scripts"))
    import prepare_review_diff

    assert prepare_review_diff.LANE_COUNT == len(POLICY["lanes"]), (
        f"policy.yml lists {len(POLICY['lanes'])} throttled lanes but "
        f"prepare_review_diff.LANE_COUNT is {prepare_review_diff.LANE_COUNT}"
    )


def test_the_prompt_target_never_exceeds_the_enforced_ceiling(tmp_path):
    """Telling the model to aim for 5 while the poster keeps 1 wastes four
    findings and makes the job log a puzzle."""
    diff = tmp_path / "d.txt"
    diff.write_text(
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -0,0 +1,3 @@\n+x = 1\n+y = 2\n+z = 3\n",
        encoding="utf-8",
    )
    out = tmp_path / "o.txt"
    gh_out = tmp_path / "gh.txt"
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / ".github/scripts/prepare_review_diff.py"),
            "--diff",
            str(diff),
            "--out",
            str(out),
            "--budget-ceiling",
            "1",
            "--github-output",
            str(gh_out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    written = dict(
        line.split("=", 1) for line in gh_out.read_text().strip().splitlines()
    )
    assert written["budget"] == "1"


def test_a_zero_reaches_github_output_as_zero(tmp_path, monkeypatch):
    """Truthiness is the wrong test for an integer output. Written as an empty
    string, `max_comments` becomes `--max-comments ""` in the workflow, which
    argparse rejects — turning a lane that simply had no budget into a failed
    job."""
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    rb.emit(
        {
            "round": 0,
            "last_reviewed_sha": "",
            "posted_total": 0,
            "max_comments": 0,
            "skip": True,
            "exempt": False,
            "reason": "none",
        },
        True,
    )
    written = dict(
        line.split("=", 1) for line in out.read_text().strip().splitlines()
    )
    assert written["max_comments"] == "0"
    assert written["posted_total"] == "0"
    assert written["round"] == "0"


def test_the_round_cap_is_named_as_a_shared_number():
    """The same line goes on each lane's review. Four reviews each saying
    "capped at 20" read as a threat of eighty comments."""
    state = {
        "round": 2,
        "last_reviewed_sha": "x",
        "posted_total": 4,
        "previous_round_count": 14,
    }
    line = rb.progress_line(rb.decide(state, policy(), "Security", 5))
    assert "across all reviewers" in line


def test_a_skipped_lane_says_nothing_at_all():
    """It posts no review, so a progress line would be a review body with no
    findings in it — the reviewer announcing that it has nothing to say."""
    state = {
        "round": 4,
        "last_reviewed_sha": "x",
        "posted_total": 25,
        "previous_round_count": 2,
    }
    assert rb.progress_line(rb.decide(state, policy(), "Hygiene", 5)) == ""


# ------------------------------------------------- forging our own identity


def test_a_human_cannot_pose_as_the_reviewer():
    """The attack this guards against, in full: a PR author submits a one-line
    review whose body is the marker. GitHub stamps it with the current head,
    every lane then reads `last_reviewed_sha == head_sha` and skips with
    "already reviewed". Repeat after each push and AI review is off for that
    pull request permanently. Twenty-five forged inline comments does the same
    thing through the lifetime cap."""
    forged = review("aaa", "2026-01-01T00:00:00Z", bot=False)
    assert not rb.is_ours(forged)

    state = rb.summarise_history(
        [forged], [comment(forged["id"])] * 25, POLICY["exempt_lanes"]
    )
    assert state["round"] == 1, "a forged review advanced the round counter"
    assert state["posted_total"] == 0, "forged comments consumed the cap"
    assert state["last_reviewed_sha"] == "", (
        "a forged review would make every lane skip as 'already reviewed'"
    )


def test_a_review_with_no_user_field_is_not_ours():
    assert not rb.is_ours({"body": rb.REVIEW_MARKER, "commit_id": "a"})


def test_our_own_bot_review_is_still_recognised():
    assert rb.is_ours(review("aaa", "2026-01-01T00:00:00Z"))


# ------------------------------------------------------- hostile / broken policy


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("decay", "sixty"),
        ("decay", 0),
        ("decay", 5),
        ("decay", None),
        ("lifetime_cap", "lots"),
        ("lifetime_cap", -3),
        ("min_allowance", None),
        ("lanes", None),
        ("lanes", []),
        ("lanes", "Security"),
        ("lanes", [1, 2]),
        ("blocker_lanes", ["Hygiene"]),  # not a prefix of lanes
        ("blocker_only_after_round", "two"),
    ],
)
def test_a_broken_policy_value_falls_back_instead_of_reddening_every_pr(
    key, value
):
    """`decide` coerces these with float()/int()/list(), so a typo in a config
    file used to escape as a CI fault — four red checks per push, on every PR,
    from a one-word edit."""
    broken = rb._validated({**rb.DEFAULTS, key: value})
    state = {
        "round": 3,
        "last_reviewed_sha": "x",
        "posted_total": 4,
        "previous_round_count": 6,
    }
    decision = rb.decide(state, broken, "Security", 5)
    assert isinstance(decision["max_comments"], int)
    assert decision["max_comments"] >= 0


def test_a_blocker_list_that_is_not_a_prefix_is_rejected():
    """Otherwise the lanes that survive a shrinking allowance are not the ones
    that survive the round limit."""
    fixed = rb._validated({**rb.DEFAULTS, "blocker_lanes": ["Hygiene"]})
    assert fixed["blocker_lanes"] == rb.DEFAULTS["blocker_lanes"]


# --------------------------------------------------- allocation misconfiguration


def test_an_unlisted_lane_gets_nothing_rather_than_everything():
    """It used to get the WHOLE round allowance, so four mislabelled lanes
    could each post the entire budget."""
    assert rb.allocate(12, POLICY["lanes"], "Performance") == 0
    assert rb.allocate(12, POLICY["lanes"], "security") == 0  # case slip
    assert rb.allocate(12, [], "Security") == 0


# ------------------------------------------------ narrowing and allocation agree


def test_past_the_narrowing_round_the_allowance_is_not_half_discarded():
    """Dividing by four while two lanes skip threw half the allowance away,
    and told the author a number twice what could actually be spent."""
    state = {
        "round": 3,
        "last_reviewed_sha": "x",
        "posted_total": 4,
        "previous_round_count": 8,
    }
    shares = {
        lane: rb.decide(state, policy(), lane, 5) for lane in POLICY["lanes"]
    }
    allowance = shares["Security"]["allowance"]
    spendable = sum(d["max_comments"] for d in shares.values() if not d["skip"])
    assert spendable == allowance, (
        f"{allowance} allowed but only {spendable} spendable"
    )


# ------------------------------------------------------ a garbled API response


@pytest.mark.parametrize(
    "stdout", ["", "<html>rate limited</html>", '{"message":"Not Found"}']
)
def test_a_success_with_no_json_array_is_not_an_empty_history(
    stdout, monkeypatch
):
    """Reading it as [] means "never reviewed", which hands a fresh batch to a
    PR that has already had five rounds."""

    class P:
        returncode = 0
        stdout_text = stdout
        stderr = ""

    P.stdout = stdout
    monkeypatch.setattr(rb.subprocess, "run", lambda *a, **k: P())
    assert rb.gh_json("repos/o/r/pulls/1/reviews") is None


def test_a_partial_page_is_not_a_partial_history(monkeypatch):
    class P:
        returncode = 0
        stdout = '[{"id":1}]\n[{"id":2},{"id":'
        stderr = ""

    monkeypatch.setattr(rb.subprocess, "run", lambda *a, **k: P())
    assert rb.gh_json("repos/o/r/pulls/1/reviews") is None


def test_a_genuine_multi_page_response_still_decodes(monkeypatch):
    class P:
        returncode = 0
        stdout = '[{"id":1}]\n[{"id":2}]'
        stderr = ""

    monkeypatch.setattr(rb.subprocess, "run", lambda *a, **k: P())
    assert rb.gh_json("x") == [{"id": 1}, {"id": 2}]


def test_a_genuinely_empty_history_is_still_empty(monkeypatch):
    class P:
        returncode = 0
        stdout = "[]"
        stderr = ""

    monkeypatch.setattr(rb.subprocess, "run", lambda *a, **k: P())
    assert rb.gh_json("x") == []


# ------------------------------------------------------------- force-push


def test_the_last_reviewed_commit_is_the_newest_review_not_the_newest_commit():
    """After a force-push back to an already-reviewed commit A the history is
    [A, B] but the newest review is against A. Naming B points the compare at
    a commit that no longer exists, which silently reverts to a full
    re-review."""
    reviews = [
        review("aaa", "2026-01-01T00:00:00Z", rid=1),
        review("bbb", "2026-01-02T00:00:00Z", rid=2),
        review("aaa", "2026-01-03T00:00:00Z", rid=3),
    ]
    state = rb.summarise_history(reviews, [], POLICY["exempt_lanes"])
    assert state["last_reviewed_sha"] == "aaa"


# ------------------------------------------- repeated manual re-review


def test_repeated_invokes_on_one_commit_do_not_grow_the_allowance():
    """`@ai-review` twice on the same commit decayed off a count that included
    the comments the first invocation had just posted, so the allowance GREW
    — 2, 3, 5, 8 — and five invocations spent the whole lifetime budget on an
    unchanged commit while the round counter stayed at 2."""
    reviews = [
        review("aaa", "2026-01-01T00:00:00Z", rid=1),
        review("bbb", "2026-01-02T00:00:00Z", rid=2),
    ]
    comments = [comment(1)] * 8 + [comment(2)] * 4

    seen = []
    for i in range(4):
        state = rb.summarise_history(
            reviews, comments, POLICY["exempt_lanes"], head_sha="bbb"
        )
        decision = rb.decide(state, policy(), "Security", 5)
        seen.append(decision["allowance"])
        rid = 10 + i
        reviews.append(review("bbb", f"2026-01-02T0{i}:00:00Z", rid=rid))
        comments += [comment(rid)] * decision["max_comments"]

    assert len(set(seen)) == 1, f"the allowance moved across invokes: {seen}"


def test_the_decay_still_measures_the_previous_commit():
    """The exclusion must not swallow the ordinary case."""
    reviews = [
        review("aaa", "2026-01-01T00:00:00Z", rid=1),
        review("bbb", "2026-01-02T00:00:00Z", rid=2),
    ]
    comments = [comment(1)] * 8 + [comment(2)] * 4
    state = rb.summarise_history(
        reviews, comments, POLICY["exempt_lanes"], head_sha="ccc"
    )
    assert state["previous_round_count"] == 4


# --------------------------------------------------- hostile policy values


def test_an_infinite_cap_does_not_escape_as_a_ci_fault():
    """`lifetime_cap: .inf` is valid YAML and int() of it raises OverflowError
    — out of the very function written to stop a policy typo reddening CI."""
    fixed = rb._validated({**rb.DEFAULTS, "lifetime_cap": float("inf")})
    assert fixed["lifetime_cap"] == rb.DEFAULTS["lifetime_cap"]
    assert (
        rb._validated({**rb.DEFAULTS, "decay": float("nan")})["decay"]
        == (rb.DEFAULTS["decay"])
    )


def test_an_empty_blocker_list_does_not_silence_every_lane():
    """From the narrowing round on, every lane would skip — the reviewer goes
    quiet on every PR in the repo, from one deleted line of config."""
    assert (
        rb._validated({**rb.DEFAULTS, "blocker_lanes": []})["blocker_lanes"]
        == rb.DEFAULTS["blocker_lanes"]
    )


def test_a_lane_cannot_be_budgeted_and_exempt_at_once():
    """The exempt branch returns before any ceiling is applied, so listing a
    model lane there makes it unbounded."""
    fixed = rb._validated({**rb.DEFAULTS, "exempt_lanes": ["Hygiene"]})
    assert "Hygiene" not in fixed["exempt_lanes"]


def test_the_narrowing_is_announced_in_the_right_tense():
    """At the narrowing round the nit lanes are still running, so a Hygiene
    review saying "only Security and Correctness run" contradicts itself."""
    base = {"last_reviewed_sha": "x", "posted_total": 2}
    at = rb.progress_line(
        rb.decide(
            {**base, "round": 2, "previous_round_count": 9},
            policy(),
            "Hygiene",
            5,
        )
    )
    after = rb.progress_line(
        rb.decide(
            {**base, "round": 3, "previous_round_count": 9},
            policy(),
            "Security",
            5,
        )
    )
    assert "after this round only" in at
    assert "only Security and Correctness still run" in after


def test_the_decay_basis_is_the_last_round_by_review_order():
    """`rounds` is deduped on first sight, so after A, B, A the last non-head
    entry is B — a round two pushes ago. Same trap the last_sha comment warns
    about, made ten lines below it."""
    reviews = [
        review("A", "2026-01-01T00:00:00Z", rid=1),
        review("B", "2026-01-02T00:00:00Z", rid=2),
        review("C", "2026-01-03T00:00:00Z", rid=3),
        review("A", "2026-01-04T00:00:00Z", rid=4),
        review("C", "2026-01-05T00:00:00Z", rid=5),
    ]
    comments = [comment(2)] * 9 + [comment(4)] * 7
    state = rb.summarise_history(
        reviews, comments, POLICY["exempt_lanes"], head_sha="C"
    )
    assert state["previous_round_count"] == 7


def test_an_exempt_lane_that_is_also_budgeted_loses_the_exemption():
    """The exempt branch returns before any ceiling, so a budgeted lane listed
    there is unbounded. Falling back to the DEFAULT exempt list could itself
    overlap a hand-edited `lanes`; removing the offenders cannot."""
    fixed = rb._validated(
        {**rb.DEFAULTS, "lanes": ["Security", "Correctness", "House Rules"]}
    )
    assert not set(fixed["lanes"]) & set(fixed["exempt_lanes"])


def test_no_overlap_survives_the_prefix_fallback():
    """The guard ran before the prefix rule could reassign `lanes`, so a
    fallback to the defaults reintroduced an overlap it had just cleared."""
    fixed = rb._validated(
        {
            **rb.DEFAULTS,
            "lanes": ["Security", "Correctness"],
            "blocker_lanes": ["Hygiene"],
            "exempt_lanes": ["Hygiene"],
        }
    )
    assert not set(fixed["lanes"]) & set(fixed["exempt_lanes"])


def test_a_re_review_with_no_earlier_round_says_so_honestly():
    """Deliberately stingy: there is no earlier round to decay from, and
    counting the current commit's own comments is what made repeated
    invocations grow the allowance. The reason must not claim otherwise."""
    state = {
        "round": 2,
        "last_reviewed_sha": "",
        "posted_total": 8,
        "previous_round_count": 0,
    }
    decision = rb.decide(state, policy(), "Security", 5)
    assert "no earlier round" in decision["reason"]
    assert decision["allowance"] == POLICY["min_allowance"]


def test_the_budget_step_resolves_the_head_sha_for_every_trigger(core):
    """`github.event.pull_request` is null on issue_comment and
    workflow_dispatch — which are exactly the triggers the same-commit guard
    exists for. Empty there, the guard could not fire and repeated
    `@ai-review` grew the allowance instead of shrinking it."""
    run = _step(core, "budget")["run"]
    assert "head_sha=" in run and "--jq '.head.sha'" in run
    for step_id in ("fetch_diff", "build_review"):
        env = _step(core, step_id)["env"]
        assert env["HEAD_SHA"].endswith("budget.outputs.head_sha }}"), (
            f"{step_id} reads the event payload, which is empty on "
            "issue_comment"
        )


def test_an_exempt_lane_is_not_skipped_by_another_lanes_review():
    """`last_reviewed_sha` comes from the BUDGETED lanes only — an exempt
    lane's reviews are excluded so they cannot advance the round counter — so
    testing an exempt lane against it asks whether some OTHER lane has seen
    this commit. Found on live PR 2627, where the deterministic lane skipped
    because Hygiene had already reviewed that commit."""
    state = {
        "round": 2,
        "last_reviewed_sha": "abc123",
        "posted_total": 1,
        "previous_round_count": 1,
    }
    decision = rb.decide(state, policy(), "House Rules", 5, head_sha="abc123")
    assert decision["skip"] is False
    assert decision["exempt"] is True

    # A budgeted lane on the same commit still skips.
    assert rb.decide(state, policy(), "Security", 5, head_sha="abc123")["skip"]


def test_an_amended_commit_does_not_make_the_decay_run_backwards():
    """`git commit --amend` lands back on a sha that was already reviewed.
    Keying the per-round tally on commit_id alone pooled every round that ever
    saw it, so the basis GREW: the allowance ran 20, 12, 7, 16, 16, 25, 31, 44
    instead of decaying, and the lifetime cap went two pushes early."""
    reviews, comments, rid = [], [], 0
    allowances = []
    # A ping-pong between two shas, as an amend-and-force cycle produces.
    for index, sha in enumerate(["A", "B", "A", "B", "A", "B"]):
        state = rb.summarise_history(
            reviews, comments, POLICY["exempt_lanes"], head_sha=sha
        )
        decision = rb.decide(state, policy(), "Security", 5)
        allowances.append(decision["allowance"])
        rid += 1
        reviews.append(
            review(sha, f"2026-01-{index + 1:02d}T00:00:00Z", rid=rid)
        )
        comments += [comment(rid)] * (8 if index == 0 else 2)

    assert allowances == sorted(allowances, reverse=True), (
        f"the allowance grew across rounds: {allowances}"
    )
    # The values, not just the shape. A tally keyed on the wrong space makes
    # every lookup miss, which floors the allowance at 1 from round 2 on --
    # monotonically non-increasing, and wrong. Round 2 must decay off the 8
    # comments round 1 actually posted: floor(0.6 * 8) == 4.
    assert allowances[1] == 4, (
        f"round 2 did not decay off round 1's real count: {allowances}"
    )
    assert allowances[-1] <= POLICY["min_allowance"] + 1
