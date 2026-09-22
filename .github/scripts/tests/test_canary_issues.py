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
"""Unit tests for canary_issues.py.

This script @-mentions people and proposes deleting recipes, so its failure
modes are social as well as technical: a stage that fires twice is monthly
nagging, a stage that fires early is a deletion warning nobody had a chance to
act on, and treating an infrastructure blip as a recipe failure teaches
everyone to ignore the channel.

Every test below pins one of those.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import canary_issues as m
import pytest

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _issue(days_old: int, labels: set[str] | None = None) -> dict:
    return {
        "number": 42,
        "title": "t",
        "createdAt": (NOW - timedelta(days=days_old)).isoformat(),
        "labelNames": labels or set(),
    }


@pytest.fixture
def calls(monkeypatch):
    """Record what the script would do, without touching GitHub."""
    recorded: list[tuple] = []
    monkeypatch.setattr(
        m, "comment", lambda n, b, d: recorded.append(("comment", n, b))
    )
    monkeypatch.setattr(
        m, "add_label", lambda n, lbl, d: recorded.append(("label", n, lbl))
    )
    monkeypatch.setattr(m, "read_owner", lambda r, repo_root=None: "owner-x")
    return recorded


# ---------------------------------------------------------------------------
# Which recipes are considered failing
# ---------------------------------------------------------------------------


def test_infra_outcomes_never_count_as_a_recipe_failure():
    """A registry timeout is not the owner's fault. Filing against them for it
    is how a notification channel loses credibility."""
    results = [
        {"recipe": "a", "python": "3.11", "outcome": "infra"},
        {"recipe": "b", "python": "3.11", "outcome": "fail"},
        {"recipe": "c", "python": "3.11", "outcome": "pass"},
    ]
    assert set(m.failing_recipes(results)) == {"b"}


def test_one_failing_version_fails_the_recipe():
    results = [
        {"recipe": "a", "python": "3.11", "outcome": "pass"},
        {"recipe": "a", "python": "3.13", "outcome": "fail"},
    ]
    assert set(m.failing_recipes(results)) == {"a"}


# ---------------------------------------------------------------------------
# Which recipes are considered RECOVERED — the other half, and the one that
# closes issues, so it has to be a positive observation rather than "did not
# appear in the failing set".
# ---------------------------------------------------------------------------


def test_recovery_requires_an_actual_passing_job():
    results = [
        {"recipe": "a", "python": "3.11", "outcome": "pass"},
        {"recipe": "b", "python": "3.11", "outcome": "fail"},
        {"recipe": "c", "python": "3.11", "outcome": "infra"},
    ]
    assert m.recovered_recipes(results) == {"a"}


def test_an_infra_only_month_is_not_a_recovery():
    """The bug this function exists for. `failing_recipes` ignores `infra`, so
    a registry outage puts a recipe in NEITHER bucket. Reading that as
    recovery closes the tracking issue claiming the recipe passes — having
    tested nothing — and throws away the escalation history."""
    results = [
        {"recipe": "a", "python": "3.11", "outcome": "infra"},
        {"recipe": "a", "python": "3.13", "outcome": "infra"},
    ]
    assert m.failing_recipes(results) == {}
    assert m.recovered_recipes(results) == set()


def test_one_passing_version_is_not_recovery_if_another_failed():
    results = [
        {"recipe": "a", "python": "3.11", "outcome": "pass"},
        {"recipe": "a", "python": "3.13", "outcome": "fail"},
    ]
    assert m.recovered_recipes(results) == set()


def test_a_pass_alongside_infra_still_counts_as_recovery():
    """Nothing failed and something demonstrably worked, which is as much as
    the canary can ever observe."""
    results = [
        {"recipe": "a", "python": "3.11", "outcome": "pass"},
        {"recipe": "a", "python": "3.13", "outcome": "infra"},
    ]
    assert m.recovered_recipes(results) == {"a"}


# ---------------------------------------------------------------------------
# The escalation clock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("already", "expected"),
    [
        (set(), m.LABEL_REMINDED),
        ({m.LABEL_REMINDED}, m.LABEL_ROTTING),
        ({m.LABEL_REMINDED, m.LABEL_ROTTING}, m.LABEL_DELETION),
        (
            {m.LABEL_REMINDED, m.LABEL_ROTTING, m.LABEL_DELETION},
            m.LABEL_PROPOSED,
        ),
    ],
)
def test_each_run_advances_exactly_one_rung(calls, already, expected):
    """The labels are the state: the next rung is the first one the issue
    does not already carry."""
    issue = _issue(400, already)
    m.escalate("core/python/demo", issue, dry=False, now=NOW)
    assert [c[2] for c in calls if c[0] == "label"] == [expected]


def test_the_ladder_cannot_collapse_however_old_the_issue(calls):
    """The bug this rewrite exists for. Age used to gate each rung
    independently, so an issue that crossed two thresholds between runs fired
    both in one go — an owner got 'here is a reminder' and 'this is being
    marked inactive' in the same breath, with no grace between them. A very
    old issue must still advance one rung and no more."""
    m.escalate("core/python/demo", _issue(3650), dry=False, now=NOW)
    assert [c[2] for c in calls if c[0] == "label"] == [m.LABEL_REMINDED]
    assert len([c for c in calls if c[0] == "comment"]) == 1


def test_a_fully_escalated_issue_goes_quiet(calls):
    """Every rung fired: say nothing rather than nag forever."""
    m.escalate("core/python/demo", _issue(999, set(m.LADDER)), dry=False)
    assert calls == []


@pytest.mark.parametrize(
    ("rung_index", "already"),
    [
        (0, set()),
        (1, {m.LABEL_REMINDED}),
        (2, {m.LABEL_REMINDED, m.LABEL_ROTTING}),
    ],
)
def test_a_rung_is_held_below_its_minimum_age(calls, rung_index, already):
    """The floor. It never binds on the monthly cron, but it stops a more
    frequent cron from compressing a four-month ladder into four weeks."""
    just_under = (rung_index + 1) * m.MIN_DAYS_BETWEEN_STAGES - 1
    m.escalate(
        "core/python/demo", _issue(just_under, already), dry=False, now=NOW
    )
    assert calls == []


def test_a_manual_run_never_advances_the_ladder(calls):
    """`--no-escalate`. Re-running the canary by hand to check something must
    not move a recipe closer to deletion."""
    m.escalate(
        "core/python/demo", _issue(400), dry=False, now=NOW, advance=False
    )
    assert calls == []


def test_deletion_rungs_tag_a_maintainer_not_only_the_owner(calls):
    """An unresponsive owner is the reason we got here, so both deletion
    notices have to reach someone who can decide."""
    for already in (
        {m.LABEL_REMINDED, m.LABEL_ROTTING},
        {m.LABEL_REMINDED, m.LABEL_ROTTING, m.LABEL_DELETION},
    ):
        calls.clear()
        m.escalate("core/python/demo", _issue(400, already), dry=False, now=NOW)
        bodies = [c[2] for c in calls if c[0] == "comment"]
        assert bodies and all(f"@{m.MAINTAINER}" in b for b in bodies)


def test_a_recipe_with_no_poc_is_not_mentioned_twice(calls, monkeypatch):
    """`mention` already falls back to the maintainer, so naming them again
    rendered "@happyhuman @happyhuman" on the deletion-scheduled notice."""
    monkeypatch.setattr(m, "read_owner", lambda r, repo_root=None: None)
    already = {m.LABEL_REMINDED, m.LABEL_ROTTING}
    m.escalate("core/python/demo", _issue(400, already), dry=False, now=NOW)
    body = next(c[2] for c in calls if c[0] == "comment")
    assert body.count(f"@{m.MAINTAINER}") == 1


def test_a_recipe_with_a_poc_still_tags_both(calls):
    """The dedupe must not cost the maintainer ping when there IS an owner."""
    already = {m.LABEL_REMINDED, m.LABEL_ROTTING}
    m.escalate("core/python/demo", _issue(400, already), dry=False, now=NOW)
    body = next(c[2] for c in calls if c[0] == "comment")
    assert f"@{m.MAINTAINER}" in body
    assert "@owner-x" in body


# ---------------------------------------------------------------------------
# Honesty about what the canary can actually do
# ---------------------------------------------------------------------------


def test_the_canary_always_says_a_human_must_apply_file_changes():
    """GITHUB_TOKEN cannot push to a code-owner-protected branch, and nothing
    in this script writes files or opens a PR. Claiming otherwise on a public
    issue is worse than admitting the gap."""
    note = m._write_note("core/python/demo", "status: inactive")
    assert "by hand" in note
    assert "will not pretend" in note


def test_no_environment_variable_can_make_it_promise_a_pr(monkeypatch):
    """An earlier version promised "a pull request will be opened
    automatically" whenever CANARY_APP_TOKEN was set, and no code ever opened
    one — so setting that secret would have made the canary lie."""
    monkeypatch.setenv("CANARY_APP_TOKEN", "x")
    note = m._write_note("core/python/demo", "status: inactive")
    assert "automatically" not in note
    assert "by hand" in note


def test_the_script_cannot_write_files_at_all():
    """Structural, not stylistic: no YAML library is imported and no PR is
    created anywhere in the module, which is what makes the note above true.
    If the write side lands, this test is the one that should fail first."""
    source = Path(m.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert "import yaml" not in code
    assert "ruamel" not in code
    assert '"pr", "create"' not in code
    assert "pr create" not in code


def test_recovery_does_not_claim_the_manifest_was_changed(calls, monkeypatch):
    """The rotting label records that the canary ASKED for `status: inactive`,
    not that anyone applied it. Stating it as fact is wrong most of the
    time, since nothing writes the manifest."""
    closed: list[tuple] = []
    monkeypatch.setattr(m, "gh", lambda *a, **k: closed.append(a) or "")
    issue = _issue(120, {m.LABEL_ROTTING})
    m.close_recovered("core/python/demo", issue, dry=False)
    body = next(c[2] for c in calls if c[0] == "comment")
    assert "was marked" not in body
    assert "asked for" in body
    assert ("issue", "close", "42", "--repo", m.REPO) in closed


# ---------------------------------------------------------------------------
# Owner routing
# ---------------------------------------------------------------------------


def _manifest(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "core" / "python" / "demo"
    d.mkdir(parents=True)
    (d / "manifest.yaml").write_text(body, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "line",
    [
        '  poc: "someone"',
        "  poc: 'someone'",
        "  poc: someone",
        "  poc:   someone",
        "  poc: someone  # the owner",
    ],
)
def test_reads_quoted_and_unquoted_poc(tmp_path, line):
    """The name promised "quoted and unquoted" but only the double-quoted
    form was ever exercised. Real manifests use all of these."""
    root = _manifest(
        tmp_path, f"status: active\nownership:\n  team: t\n{line}\n"
    )
    assert m.read_owner("core/python/demo", root) == "someone"


def test_ignores_a_poc_outside_the_ownership_block(tmp_path):
    """`poc:` under a different key is not the owner. Mentioning whoever it
    names would notify an uninvolved person."""
    root = _manifest(
        tmp_path,
        "status: active\narchitecture:\n  poc: not-the-owner\n"
        "ownership:\n  team: t\n  poc: real-owner\n",
    )
    assert m.read_owner("core/python/demo", root) == "real-owner"


def test_missing_poc_falls_back_to_the_maintainer(tmp_path):
    root = _manifest(tmp_path, "status: active\nownership:\n  team: t\n")
    assert m.read_owner("core/python/demo", root) is None
    mention, note = m._mention(None)
    assert mention == f"@{m.MAINTAINER}"
    assert "no usable `ownership.poc`" in note


# ---------------------------------------------------------------------------
# The issue body
# ---------------------------------------------------------------------------


def test_body_names_the_passing_versions(monkeypatch):
    """The whole diagnostic value of a two-version matrix. An earlier version
    was handed only the failures and could never render this."""
    monkeypatch.setattr(m, "read_owner", lambda r, repo_root=None: "o")
    body = m.build_body(
        "core/python/demo",
        [
            {"recipe": "d", "python": "3.11", "outcome": "pass"},
            {
                "recipe": "d",
                "python": "3.13",
                "outcome": "fail",
                "step": "pytest",
                "detail": "boom",
            },
        ],
        "https://run",
    )
    assert "Python 3.13." in body
    assert "still passes on Python 3.11" in body
    # The table lists the failure and not the pass.
    assert "| 3.13 | pytest | boom |" in body
    assert "| 3.11 |" not in body


def test_body_states_this_is_not_a_dependency_bump_nag(monkeypatch):
    monkeypatch.setattr(m, "read_owner", lambda r, repo_root=None: "o")
    body = m.build_body(
        "core/python/demo",
        [{"recipe": "d", "python": "3.11", "outcome": "fail"}],
        "https://run",
    )
    assert "committed lockfile" in body
    assert "never opens version-bump PRs" in body


def test_title_is_stable_because_it_is_the_dedupe_key():
    """Anything varying in the title — a date, a version, the error text —
    stops the canary recognising its own issue, so it files a fresh one every
    month and no ladder ever advances.

    Pinned as an exact string. `issue_title(x) == issue_title(x)` was the
    assertion here before, and a function always equals itself: putting
    `date.today()` in the title left this green.
    """
    assert (
        m.issue_title("core/python/x")
        == "Recipe canary: core/python/x is failing"
    )


def test_the_title_carries_nothing_that_varies_between_runs():
    """The complement, as cheap insurance against a future f-string reaching
    for the clock or the interpreter version."""
    title = m.issue_title("core/python/x")
    for token in (str(datetime.now(timezone.utc).year), "3.11", "T00:"):
        assert token not in title


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------


def test_empty_results_refuse_to_act(tmp_path, capsys):
    """An empty result set and a run where every job died look identical.
    Acting would close every open issue as 'recovered'."""
    results = tmp_path / "r.json"
    results.write_text("[]", encoding="utf-8")
    assert m.main(["--results", str(results)]) == 1
    assert "Refusing to act" in capsys.readouterr().err


def _drive_main(tmp_path, monkeypatch, results, issue):
    """Run main() over `results` with one open issue, recording the verdict."""
    acted: list[tuple] = []
    monkeypatch.setattr(m, "ensure_labels", lambda: None)
    monkeypatch.setattr(m, "open_issues_by_title", dict)
    monkeypatch.setattr(m, "find_issue", lambda r, idx=None: issue)
    monkeypatch.setattr(
        m, "open_issue", lambda r, j, u, d: acted.append(("open", r))
    )
    monkeypatch.setattr(
        m,
        "escalate",
        lambda r, i, d, **kw: acted.append(("escalate", r, kw)),
    )
    monkeypatch.setattr(
        m, "close_recovered", lambda r, i, d: acted.append(("close", r))
    )
    path = tmp_path / "r.json"
    path.write_text(json.dumps(results), encoding="utf-8")
    assert m.main(["--results", str(path), "--dry-run"]) == 0
    return acted


def test_an_infra_only_month_leaves_the_issue_open(tmp_path, monkeypatch):
    """End to end: a registry outage must not close a rotting issue.

    Before the fix `main()` reached `close_recovered` for any recipe absent
    from `failing_recipes`, so this posted "installs from its lockfile and
    passes its tests again" on an issue whose recipe had not been tested at
    all, and reset an escalation clock up to 119 days old.
    """
    results = [
        {"recipe": "contrib/foo", "python": "3.11", "outcome": "infra"},
        {"recipe": "contrib/foo", "python": "3.13", "outcome": "infra"},
    ]
    acted = _drive_main(
        tmp_path, monkeypatch, results, _issue(95, {m.LABEL_DELETION})
    )
    assert acted == []


def test_a_genuine_pass_still_closes_the_issue(tmp_path, monkeypatch):
    """The other side of the same guard: recovery must still work."""
    results = [{"recipe": "contrib/foo", "python": "3.11", "outcome": "pass"}]
    acted = _drive_main(tmp_path, monkeypatch, results, _issue(95))
    assert acted == [("close", "contrib/foo")]


def test_a_still_failing_recipe_escalates(tmp_path, monkeypatch):
    results = [{"recipe": "contrib/foo", "python": "3.11", "outcome": "fail"}]
    acted = _drive_main(tmp_path, monkeypatch, results, _issue(35))
    assert acted == [("escalate", "contrib/foo", {"advance": True})]


def test_no_escalate_flag_reaches_escalate(tmp_path, monkeypatch):
    """The workflow passes `--no-escalate` on every non-scheduled run."""
    results = [{"recipe": "contrib/foo", "python": "3.11", "outcome": "fail"}]
    acted: list[tuple] = []
    monkeypatch.setattr(m, "ensure_labels", lambda: None)
    monkeypatch.setattr(m, "open_issues_by_title", dict)
    monkeypatch.setattr(m, "find_issue", lambda r, idx=None: _issue(35))
    monkeypatch.setattr(
        m,
        "escalate",
        lambda r, i, d, **kw: acted.append(("escalate", r, kw)),
    )
    path = tmp_path / "r.json"
    path.write_text(json.dumps(results), encoding="utf-8")
    assert m.main(["--results", str(path), "--no-escalate"]) == 0
    assert acted == [("escalate", "contrib/foo", {"advance": False})]


def test_gh_failure_is_raised_not_swallowed(monkeypatch):
    """A canary that cannot file issues must fail its run rather than report
    success having told nobody."""
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *a, **k: type(
            "R", (), {"returncode": 1, "stdout": "", "stderr": "nope"}
        )(),
    )
    with pytest.raises(m.GhError, match="nope"):
        m.gh("issue", "list")


def test_a_mass_failure_refuses_to_file_issues(tmp_path, monkeypatch, capsys):
    """Circuit breaker, mirroring close_orphan_dependabot_prs.py. Half the
    repo failing at once is a bad runner image or an ecosystem-wide yank, not
    that many independent rotting recipes — and filing against every owner
    for it is how the channel gets muted."""
    results = [
        {"recipe": f"core/python/r{i}", "python": "3.11", "outcome": "fail"}
        for i in range(m.DEFAULT_MAX_ISSUES + 1)
    ]
    called: list[str] = []
    monkeypatch.setattr(m, "open_issues_by_title", dict)
    monkeypatch.setattr(
        m, "find_issue", lambda r, idx=None: called.append(r) or None
    )
    path = tmp_path / "r.json"
    path.write_text(json.dumps(results), encoding="utf-8")
    assert m.main(["--results", str(path), "--dry-run"]) == 1
    assert called == []
    assert "Refusing to act" in capsys.readouterr().err


def test_the_breaker_allows_a_normal_month(tmp_path, monkeypatch):
    results = [
        {"recipe": f"core/python/r{i}", "python": "3.11", "outcome": "fail"}
        for i in range(m.DEFAULT_MAX_ISSUES)
    ]
    opened: list[str] = []
    monkeypatch.setattr(m, "ensure_labels", lambda: None)
    monkeypatch.setattr(m, "open_issues_by_title", dict)
    monkeypatch.setattr(m, "find_issue", lambda r, idx=None: None)
    monkeypatch.setattr(m, "open_issue", lambda r, j, u, d: opened.append(r))
    path = tmp_path / "r.json"
    path.write_text(json.dumps(results), encoding="utf-8")
    assert m.main(["--results", str(path), "--dry-run"]) == 0
    assert len(opened) == m.DEFAULT_MAX_ISSUES


def test_one_api_failure_does_not_skip_every_recipe_after_it(
    tmp_path, monkeypatch, capsys
):
    """The loop used to abort on the first GhError, leaving an arbitrary tail
    of the recipe list unexamined with nothing saying which."""
    names = ["core/python/a", "core/python/b", "core/python/c"]
    results = [
        {"recipe": n, "python": "3.11", "outcome": "fail"} for n in names
    ]
    seen: list[str] = []

    def flaky(recipe, idx=None):
        seen.append(recipe)
        if recipe == "core/python/b":
            raise m.GhError("rate limited")

    monkeypatch.setattr(m, "ensure_labels", lambda: None)
    monkeypatch.setattr(m, "open_issues_by_title", dict)
    monkeypatch.setattr(m, "find_issue", flaky)
    monkeypatch.setattr(m, "open_issue", lambda r, j, u, d: None)
    path = tmp_path / "r.json"
    path.write_text(json.dumps(results), encoding="utf-8")

    # Every recipe examined, and the run still fails so nobody reads the
    # month as clean.
    assert m.main(["--results", str(path), "--dry-run"]) == 1
    assert seen == names
    assert "could not be processed: core/python/b" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The write path: dedupe, closing, and dry-run suppression
# ---------------------------------------------------------------------------


def _gh_stub(monkeypatch, issues):
    """Stand in for `gh`, recording every invocation."""
    seen: list[tuple] = []

    def fake(*args, **kwargs):
        seen.append(args)
        if args[:2] == ("issue", "list"):
            return json.dumps(issues)
        return ""

    monkeypatch.setattr(m, "gh", fake)
    return seen


def test_find_issue_matches_the_title_exactly(monkeypatch):
    """A prefix match would hand `rag-agent-search` the issue belonging to
    `rag-agent-search-v2`, escalating one recipe's clock on another's
    failures. Making the comparison a prefix left every test green."""
    _gh_stub(
        monkeypatch,
        [
            {
                "number": 7,
                "title": m.issue_title("core/python/rag-agent-search-v2"),
                "createdAt": NOW.isoformat(),
                "labels": [],
            }
        ],
    )
    assert m.find_issue("core/python/rag-agent-search") is None
    assert m.find_issue("core/python/rag-agent-search-v2")["number"] == 7


def test_recovery_actually_closes_the_issue(monkeypatch):
    """Deleting the `gh issue close` call entirely left every test green."""
    seen = _gh_stub(monkeypatch, [])
    m.close_recovered("core/python/x", _issue(40), dry=False)
    assert ("issue", "close", "42", "--repo", m.REPO) in seen


@pytest.mark.parametrize(
    "action",
    [
        lambda: m.comment(42, "body", dry=True),
        lambda: m.add_label(42, "lbl", dry=True),
        lambda: m.close_recovered("core/python/x", _issue(40), dry=True),
        lambda: m.open_issue("core/python/x", [], "url", True),
    ],
)
def test_dry_run_performs_no_write(monkeypatch, action):
    """Every write path must be suppressed by --dry-run. Disabling the guard
    in `comment` and `open_issue` left every test green, so a dry run would
    have posted to real issues."""
    seen = _gh_stub(monkeypatch, [])
    monkeypatch.setattr(m, "read_owner", lambda r, repo_root=None: None)
    monkeypatch.setattr(m, "is_assignable", lambda u: False)
    action()
    # `gh api repos/...` passes the path as ONE argument, so match on the
    # verb rather than a two-element prefix that can never occur.
    writes = [a for a in seen if a[:2] != ("issue", "list") and a[0] != "api"]
    assert writes == [], f"dry run performed writes: {writes}"


def test_is_assignable_is_not_called_during_a_dry_run(monkeypatch):
    """It shells out to `gh api`, so a dry run was not offline and could fail
    or rate-limit on a run that is supposed to change nothing."""
    called: list[str] = []
    monkeypatch.setattr(m, "gh", lambda *a, **k: "")
    monkeypatch.setattr(m, "read_owner", lambda r, repo_root=None: "someone")
    monkeypatch.setattr(m, "is_assignable", lambda u: called.append(u) or True)
    m.open_issue("core/python/x", [], "url", True)
    assert called == []


def test_issues_are_fetched_once_per_run_not_once_per_recipe(
    tmp_path, monkeypatch
):
    """`find_issue` used to run its own `gh issue list` for every recipe: the
    same query, the same page of up to 200 issues, n times — and n chances to
    hit a rate limit on a run whose whole job is to be dependable."""
    recipes = [f"core/python/r{i}" for i in range(6)]
    results = [
        {"recipe": r, "python": "3.11", "outcome": "fail"} for r in recipes
    ]
    calls: list[tuple] = []

    def fake_gh(*args, **kwargs):
        calls.append(args)
        return json.dumps([]) if args[:2] == ("issue", "list") else ""

    monkeypatch.setattr(m, "gh", fake_gh)
    monkeypatch.setattr(m, "ensure_labels", lambda: None)
    monkeypatch.setattr(m, "open_issue", lambda r, j, u, d: None)
    path = tmp_path / "r.json"
    path.write_text(json.dumps(results), encoding="utf-8")

    assert m.main(["--results", str(path), "--dry-run"]) == 0
    listings = [c for c in calls if c[:2] == ("issue", "list")]
    assert len(listings) == 1, (
        f"{len(listings)} `gh issue list` calls for {len(recipes)} recipes; "
        "the snapshot should be taken once per run"
    )


def test_find_issue_still_works_standalone(monkeypatch):
    """Omitting the index fetches one, which is right for a one-off call and
    wrong inside a loop. Kept working so the function stays usable alone."""
    _gh_stub(
        monkeypatch,
        [
            {
                "number": 3,
                "title": m.issue_title("core/python/x"),
                "createdAt": NOW.isoformat(),
                "labels": [{"name": m.LABEL_ROTTING}],
            }
        ],
    )
    found = m.find_issue("core/python/x")
    assert found["number"] == 3
    assert found["labelNames"] == {m.LABEL_ROTTING}
