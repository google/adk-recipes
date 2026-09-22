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
"""Unit tests for sweep_stale_branches.py.

Deleting a branch cannot be undone from the UI, so the classification order
and every protection are pinned here.

The dangerous case these tests exist for: this repository is squash-merge
only, so a merged branch is NEVER an ancestor of the default branch. An
implementation that detected merges by ancestry alone — the obvious one, and
what `git branch --merged` does — would classify provably merged branches as
unmerged orphans. At the time of writing that was 6 of 28 branches.
"""

from datetime import datetime, timedelta, timezone

import pytest
import sweep_stale_branches as s

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)

CFG = {
    "merged_after_days": 7,
    "closed_pr_after_days": 30,
    "orphan_after_days": 90,
    "max_delete_per_run": 20,
    "protected": ["main", "assets", "gh-pages", "release/*"],
}


def ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


def branch(name: str, days_since_commit: int = 200) -> s.Branch:
    return s.Branch(
        name=name, sha=f"sha-{name}", last_commit=ago(days_since_commit)
    )


def pr(
    number: int = 1,
    head: str = "feature",
    base: str = "main",
    state: str = "OPEN",
    merged_days_ago: int | None = None,
    closed_days_ago: int | None = None,
    fork: bool = False,
) -> s.PullRequest:
    return s.PullRequest(
        number=number,
        head_ref=head,
        base_ref=base,
        state=state,
        merged_at=None if merged_days_ago is None else ago(merged_days_ago),
        closed_at=None if closed_days_ago is None else ago(closed_days_ago),
        cross_repository=fork,
    )


NEVER_ANCESTOR = lambda _branch: False  # noqa: E731
ALWAYS_ANCESTOR = lambda _branch: True  # noqa: E731


# ---------------------------------------------------------------------------
# open_pr_protected_refs
# ---------------------------------------------------------------------------


def test_same_repo_open_pr_protects_its_head_and_base():
    prs = [pr(head="feature/x", base="develop")]
    assert s.open_pr_protected_refs(prs) == {"feature/x", "develop"}


def test_fork_pr_protects_its_base_but_not_its_head():
    """A fork's head ref lives in the fork; its NAME is chosen by an outsider.

    Several open PRs on this repo have head refs literally called `main` or
    `dev`. Honouring those would let an outsider's branch name shadow ours.
    The base ref is always a branch here, so it is protected regardless.
    """
    prs = [pr(head="dev", base="main", fork=True)]
    assert s.open_pr_protected_refs(prs) == {"main"}


def test_closed_and_merged_prs_protect_nothing():
    prs = [
        pr(number=1, head="a", state="MERGED", merged_days_ago=1),
        pr(number=2, head="b", state="CLOSED", closed_days_ago=1),
    ]
    assert s.open_pr_protected_refs(prs) == set()


# ---------------------------------------------------------------------------
# matches_any
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("main", True),
        ("assets", True),
        ("release/1.2", True),
        ("release", False),
        ("feature/assets", False),
        ("assets-old", False),
    ],
)
def test_protected_glob_matching(name, expected):
    assert s.matches_any(name, CFG["protected"]) is expected


# ---------------------------------------------------------------------------
# prs_for_branch
# ---------------------------------------------------------------------------


def test_prs_for_branch_ignores_fork_prs_with_the_same_head_name():
    prs = [
        pr(number=1, head="dev", fork=True),
        pr(number=2, head="dev", fork=False),
    ]
    assert [p.number for p in s.prs_for_branch("dev", prs)] == [2]


# ---------------------------------------------------------------------------
# classify — the resolution order
# ---------------------------------------------------------------------------


def test_squash_merged_branch_is_merged_not_orphan():
    """The regression this whole module exists to prevent.

    A squash-merged branch has a MERGED pull request but is not an ancestor
    of the default branch. It must land on the 7-day merged clock, not the
    90-day orphan clock, and `reason` must say so.
    """
    b = branch("chore/webp-doc-images", days_since_commit=17)
    prs = [
        pr(
            number=42,
            head="chore/webp-doc-images",
            state="MERGED",
            merged_days_ago=17,
            closed_days_ago=17,
        )
    ]
    verdict = s.classify(b, prs, CFG, NOW, NEVER_ANCESTOR)
    assert verdict.category == "merged"
    assert verdict.delete is True
    assert "#42 merged" in verdict.reason


def test_merged_clock_measures_from_merged_at_not_last_commit():
    """Commits can predate the merge by months; only the merge date counts."""
    b = branch("long-lived", days_since_commit=300)
    prs = [
        pr(
            number=7,
            head="long-lived",
            state="MERGED",
            merged_days_ago=3,
            closed_days_ago=3,
        )
    ]
    verdict = s.classify(b, prs, CFG, NOW, NEVER_ANCESTOR)
    assert verdict.category == "merged"
    assert verdict.age_days == 3
    assert verdict.delete is False  # 3d < 7d


def test_ancestor_with_no_pull_request_is_merged():
    b = branch("pushed-straight-to-main", days_since_commit=40)
    verdict = s.classify(b, [], CFG, NOW, ALWAYS_ANCESTOR)
    assert verdict.category == "merged"
    assert verdict.delete is True


def test_closed_unmerged_pr_uses_the_close_date_not_the_commit_date():
    """A PR closed recently may still be revived, however old its commits.

    Modelled on the real case: `fix/rag-agent-model-and-config`, whose last
    commit is 156 days old but whose PR was closed 21 days ago. Under the
    orphan clock it would be deleted; under the correct closed-PR clock it
    has 9 days left.
    """
    b = branch("fix/rag-agent-model-and-config", days_since_commit=156)
    prs = [
        pr(
            number=1217,
            head="fix/rag-agent-model-and-config",
            state="CLOSED",
            closed_days_ago=21,
        )
    ]
    verdict = s.classify(b, prs, CFG, NOW, NEVER_ANCESTOR)
    assert verdict.category == "closed-pr"
    assert verdict.age_days == 21
    assert verdict.delete is False


def test_closed_unmerged_pr_past_its_clock_is_deleted():
    b = branch("abandoned", days_since_commit=400)
    prs = [pr(number=9, head="abandoned", state="CLOSED", closed_days_ago=31)]
    verdict = s.classify(b, prs, CFG, NOW, NEVER_ANCESTOR)
    assert verdict.category == "closed-pr"
    assert verdict.delete is True


def test_merged_pr_wins_over_an_older_closed_pr_on_the_same_branch():
    """A branch can be PR'd, closed, re-opened and merged. Merge wins."""
    b = branch("reworked", days_since_commit=10)
    prs = [
        pr(number=1, head="reworked", state="CLOSED", closed_days_ago=60),
        pr(
            number=2,
            head="reworked",
            state="MERGED",
            merged_days_ago=10,
            closed_days_ago=10,
        ),
    ]
    verdict = s.classify(b, prs, CFG, NOW, NEVER_ANCESTOR)
    assert verdict.category == "merged"


def test_most_recent_close_is_used_when_several_prs_were_closed():
    b = branch("retried", days_since_commit=400)
    prs = [
        pr(number=1, head="retried", state="CLOSED", closed_days_ago=200),
        pr(number=2, head="retried", state="CLOSED", closed_days_ago=5),
    ]
    verdict = s.classify(b, prs, CFG, NOW, NEVER_ANCESTOR)
    assert verdict.age_days == 5
    assert verdict.delete is False


def test_unmerged_branch_with_no_pull_request_is_an_orphan():
    b = branch("experiment", days_since_commit=95)
    verdict = s.classify(b, [], CFG, NOW, NEVER_ANCESTOR)
    assert verdict.category == "orphan"
    assert verdict.delete is True


@pytest.mark.parametrize(
    ("days", "expected"),
    [(89, False), (90, True), (91, True)],
)
def test_orphan_threshold_is_inclusive(days, expected):
    b = branch("experiment", days_since_commit=days)
    verdict = s.classify(b, [], CFG, NOW, NEVER_ANCESTOR)
    assert verdict.delete is expected


# ---------------------------------------------------------------------------
# evaluate — protections
# ---------------------------------------------------------------------------


def evaluate(
    branches,
    open_prs,
    default="main",
    api_protected=frozenset(),
    history=None,
):
    """`open_prs` decides protection; `history` is the per-branch lookup."""
    by_branch = history or {}
    return {
        v.branch.name: v
        for v in s.evaluate(
            branches,
            open_prs,
            CFG,
            default,
            set(api_protected),
            NOW,
            lambda name: by_branch.get(name, []),
            NEVER_ANCESTOR,
        )
    }


def test_default_branch_is_never_deleted():
    result = evaluate([branch("main", 500)], [pr(head="x")])
    assert result["main"].delete is False
    assert result["main"].category == "protected"


def test_branch_protection_rule_wins_over_every_clock():
    result = evaluate(
        [branch("locked", 500)],
        [pr(head="x")],
        api_protected={"locked"},
    )
    assert result["locked"].delete is False
    assert "branch-protection" in result["locked"].reason


def test_assets_content_branch_is_protected_by_policy():
    """`assets` carries documentation images, no PR, and no merges.

    Every clock in the policy would eventually consider it dead.
    """
    result = evaluate([branch("assets", 500)], [pr(head="x")])
    assert result["assets"].delete is False
    assert result["assets"].category == "protected"


def test_release_glob_is_protected():
    result = evaluate([branch("release/2.0", 500)], [pr(head="x")])
    assert result["release/2.0"].delete is False


def test_open_pr_head_protects_its_branch():
    result = evaluate(
        [branch("feature/live", 500)],
        [pr(head="feature/live")],
    )
    assert result["feature/live"].delete is False
    assert result["feature/live"].category == "open-pr"


def test_open_pr_base_protects_a_stacked_branch():
    """A fork PR targeting `feature/base` keeps `feature/base` alive."""
    result = evaluate(
        [branch("feature/base", 500)],
        [pr(head="contributor-work", base="feature/base", fork=True)],
    )
    assert result["feature/base"].delete is False
    assert result["feature/base"].category == "open-pr"


def test_fork_head_ref_does_not_shadow_a_local_branch_of_the_same_name():
    """`dev` here is a dead local branch; the open PR's `dev` is in a fork."""
    result = evaluate(
        [branch("dev", 500)],
        [pr(head="dev", base="main", fork=True)],
    )
    assert result["dev"].delete is True
    assert result["dev"].category == "orphan"


def test_protections_short_circuit_before_any_lookup():
    """A protected branch must trigger neither a history nor a compare call.

    Both are one API request each, and a protected branch's verdict cannot
    depend on either.
    """

    def explode(_arg):
        raise AssertionError("lookup ran for a protected branch")

    verdicts = s.evaluate(
        [branch("assets", 500), branch("main", 500)],
        [pr(head="x")],
        CFG,
        "main",
        set(),
        NOW,
        explode,
        explode,
    )
    assert all(v.delete is False for v in verdicts)


def test_branch_history_is_looked_up_per_branch_not_shared():
    """The regression that the first live dry run exposed.

    An earlier version fetched every pull request in one `--state all` call
    and filtered in memory. That listing truncates at its limit, dropping the
    OLDEST pull requests — which belong to the oldest branches, the very ones
    being judged. Five branches with OPEN pull requests were classified as
    unmerged orphans and queued for deletion.

    Pinning the shape here: a branch's verdict must come from a lookup keyed
    on that branch, so nothing can fall off the end of a shared list.
    """
    asked: list[str] = []

    def lookup(name):
        asked.append(name)
        return [pr(number=1217, head=name, state="CLOSED", closed_days_ago=21)]

    verdicts = s.evaluate(
        [branch("old-branch", 156)],
        [pr(head="unrelated")],
        CFG,
        "main",
        set(),
        NOW,
        lookup,
        NEVER_ANCESTOR,
    )
    assert asked == ["old-branch"]
    assert verdicts[0].category == "closed-pr"
    assert verdicts[0].delete is False


def test_open_pr_on_an_ancient_branch_protects_it():
    """`am/doc-update`: 181 days old, but its pull request is still OPEN."""
    result = evaluate(
        [branch("am/doc-update", 181)],
        [pr(number=1086, head="am/doc-update")],
    )
    assert result["am/doc-update"].delete is False
    assert result["am/doc-update"].category == "open-pr"


def test_results_are_sorted_by_branch_name():
    verdicts = s.evaluate(
        [branch("zeta"), branch("alpha"), branch("mid")],
        [pr(head="x")],
        CFG,
        "main",
        set(),
        NOW,
        lambda _name: [],
        NEVER_ANCESTOR,
    )
    assert [v.branch.name for v in verdicts] == ["alpha", "mid", "zeta"]


# ---------------------------------------------------------------------------
# delete_branch — the only call that destroys anything
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected_ref",
    [
        ("feature/x", "feature/x"),  # slashes must survive
        ("fix#1", "fix%231"),
        ("100%done", "100%25done"),
        ("a b", "a%20b"),
    ],
)
def test_delete_branch_percent_encodes_the_ref(monkeypatch, name, expected_ref):
    """`gh api` does not encode the path it is given.

    `#` is legal in a git ref name and opens a URL fragment, so an unencoded
    `fix#1` would send DELETE .../heads/fix and destroy a different, live
    branch. Slashes must NOT be encoded — `feature/x` is one ref, not a
    branch called `feature%2Fx`.
    """
    seen: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **_kwargs):
        seen.append(argv)
        return Result()

    monkeypatch.setattr(s.subprocess, "run", fake_run)
    ok, _ = s.delete_branch(s.Branch(name, "deadbeef", NOW))

    assert ok
    assert seen[0][-1] == f"repos/{s.REPO}/git/refs/heads/{expected_ref}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_parse_ts_handles_github_zulu_timestamps():
    assert s.parse_ts("2026-07-23T23:45:43Z") == datetime(
        2026, 7, 23, 23, 45, 43, tzinfo=timezone.utc
    )


def test_parse_ts_of_none_is_none():
    assert s.parse_ts(None) is None
    assert s.parse_ts("") is None


def test_real_policy_file_declares_the_branch_thresholds():
    """Loads the REAL .github/policy.yml, not a fixture.

    The script reads its defaults from that file, so a rename or a dropped
    key there breaks the sweep at runtime rather than here. tools-tests.yml
    lists .github/policy.yml in its `paths` so this runs on such a change.
    """
    cfg = s.load_config()
    for key in (
        "merged_after_days",
        "closed_pr_after_days",
        "orphan_after_days",
        "max_delete_per_run",
        "protected",
    ):
        assert key in cfg, f"stale_policy.branches.{key} is missing"
    assert cfg["orphan_after_days"] >= cfg["closed_pr_after_days"]
    assert cfg["closed_pr_after_days"] >= cfg["merged_after_days"]
    # `assets` is a live content branch; losing this entry deletes it.
    assert "assets" in cfg["protected"]
    assert "main" in cfg["protected"]


# ---------------------------------------------------------------------------
# The stacked-PR base ref, and the hour-long window the Monday ordering opens.
#
# stale-sweep.yml closes stale PRs at 05:00; this sweep runs at 06:00. In
# between, a branch that was only ever a stack's BASE loses the protection
# `open_pr_protected_refs` gave it, and nothing else was crediting it.
# ---------------------------------------------------------------------------


def test_base_of_just_closed_pr_is_not_orphaned():
    """The 05:00 close must not make the 06:00 sweep delete the stack's base.

    `prs_for_branch` filters on head_ref, so a base-only branch used to get no
    credit at all once its PR closed and fell to the orphan clock, measured
    from a last commit that is necessarily old — that is why the PR went stale.
    It could be deleted within the hour of losing protection, falsifying the
    close notice's promise that reopening the PR restores the work.
    """
    base = branch("feature/stack-base", days_since_commit=200)
    just_closed = pr(
        number=77,
        head="feature/stack-child",
        base="feature/stack-base",
        state="CLOSED",
        closed_days_ago=0,
    )

    verdict = s.classify(base, [just_closed], CFG, NOW, NEVER_ANCESTOR)

    assert verdict.category == "closed-pr"
    assert not verdict.delete, "base of a PR closed today must survive"
    assert "#77" in verdict.reason


def test_base_of_long_closed_pr_is_eventually_deleted():
    """The grace period is bounded — this defers deletion, it does not block it."""
    base = branch("feature/stack-base", days_since_commit=200)
    long_closed = pr(
        number=78,
        head="feature/stack-child",
        base="feature/stack-base",
        state="CLOSED",
        closed_days_ago=CFG["closed_pr_after_days"] + 1,
    )

    verdict = s.classify(base, [long_closed], CFG, NOW, NEVER_ANCESTOR)

    assert verdict.category == "closed-pr"
    assert verdict.delete


def test_fork_pr_still_protects_the_base_it_targets():
    """A base ref names a branch HERE even when the head lives in a fork."""
    base = branch("feature/stack-base", days_since_commit=200)
    fork_pr = pr(
        number=79,
        head="patch-1",
        base="feature/stack-base",
        state="CLOSED",
        closed_days_ago=1,
        fork=True,
    )

    verdict = s.classify(base, [fork_pr], CFG, NOW, NEVER_ANCESTOR)

    assert verdict.category == "closed-pr"
    assert not verdict.delete


def test_plain_orphan_is_unaffected():
    """A branch no PR ever referenced still takes the orphan clock."""
    verdict = s.classify(
        branch("feature/nobody", days_since_commit=200),
        [
            pr(
                number=80,
                head="other",
                base="main",
                state="CLOSED",
                closed_days_ago=1,
            )
        ],
        CFG,
        NOW,
        NEVER_ANCESTOR,
    )
    assert verdict.category == "orphan"
    assert verdict.delete


# ---------------------------------------------------------------------------
# make_pr_lookup
#
# classify()'s base-ref rule is only reachable if the list it receives can
# actually contain a PR whose BASE is the branch. That is a property of the
# lookup, not of classify, and testing classify alone cannot show it: those
# tests build the list by hand and can express an input the real lookup could
# never produce.
# ---------------------------------------------------------------------------


def test_lookup_queries_both_head_and_base(monkeypatch):
    """Without the --base query the stacked-base rule is dead code.

    This is the test that was missing. classify() gained a rule keyed on
    base_ref while the lookup still asked only for --head, so in production
    the rule never fired and a stack's base could still be deleted, with a
    green unit-test suite saying otherwise.
    """
    calls: list[tuple[str, str]] = []

    def fake_gh(*args: str) -> str:
        argv = list(args)
        selector = next(a for a in argv if a in ("--head", "--base"))
        branch = argv[argv.index(selector) + 1]
        calls.append((selector, branch))
        if selector == "--head":
            return "[]"
        return (
            '[{"number": 77, "headRefName": "feature/child",'
            ' "baseRefName": "feature/stack-base", "state": "CLOSED",'
            ' "mergedAt": null, "closedAt": "2026-08-13T00:00:00Z",'
            ' "isCrossRepository": false}]'
        )

    monkeypatch.setattr(s, "gh", fake_gh)
    prs = s.make_pr_lookup()("feature/stack-base")

    assert ("--head", "feature/stack-base") in calls
    assert ("--base", "feature/stack-base") in calls, (
        "lookup must ask for PRs targeting this branch, or classify's "
        "base-ref rule can never fire"
    )
    assert [p.number for p in prs] == [77]


def test_lookup_deduplicates_across_the_two_queries(monkeypatch):
    """The same PR returned by both halves must be counted once."""
    payload = (
        '[{"number": 5, "headRefName": "a", "baseRefName": "a",'
        ' "state": "CLOSED", "mergedAt": null,'
        ' "closedAt": "2026-08-13T00:00:00Z", "isCrossRepository": false}]'
    )
    monkeypatch.setattr(s, "gh", lambda *a: payload)
    assert [p.number for p in s.make_pr_lookup()("a")] == [5]


def test_lookup_is_cached_per_branch(monkeypatch):
    """Two selectors per branch, not two per call."""
    n = {"count": 0}

    def fake_gh(*args: str) -> str:
        n["count"] += 1
        return "[]"

    monkeypatch.setattr(s, "gh", fake_gh)
    lookup = s.make_pr_lookup()
    lookup("b")
    lookup("b")
    assert n["count"] == 2, "expected one --head and one --base, then cache"
