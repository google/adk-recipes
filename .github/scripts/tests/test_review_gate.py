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
"""Unit tests for review_gate.py.

The two failure modes that matter most:
  - bouncing a PR whose checks are merely still running, which would hit
    nearly every PR since reviews are requested seconds after a push;
  - losing track of whom to re-request, which strands the PR.
"""

import pytest
import review_gate as g

CFG = g.Config(
    label="status/not-ready-for-review",
    ignored_workflows=("Review Gate", "*AI PR Review — Security"),
)


def run(name, workflow="CI", status="COMPLETED", conclusion="SUCCESS", at="1"):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "COMPLETED" else None,
        "detailsUrl": f"https://ci/{name}",
        "startedAt": at,
        "checkSuite": {"workflowRun": {"workflow": {"name": workflow}}},
    }


def status(context, state="SUCCESS"):
    return {
        "__typename": "StatusContext",
        "context": context,
        "state": state,
        "targetUrl": None,
        "createdAt": "1",
    }


def pr(contexts=(), requests=1, labels=(), number=7, draft=False):
    return {
        "number": number,
        "state": "OPEN",
        "isDraft": draft,
        "labels": {"nodes": [{"name": n} for n in labels]},
        "reviewRequests": {"totalCount": requests},
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {
                            "contexts": {"nodes": list(contexts)}
                        }
                    }
                }
            ]
        },
    }


def blocked_gate(users=("alice",), teams=()):
    stored = g.Reviewers(users=list(users), teams=list(teams))
    body = g.marker("blocked", stored)
    return g.GateComment(1, body, "blocked", stored)


# --- checks ----------------------------------------------------------------


def test_failed_and_errored_contexts_are_failing():
    failing, pending = g.evaluate_checks(
        [run("a", conclusion="FAILURE"), status("cla/google", "ERROR")], CFG
    )
    assert [c.name for c in failing] == ["CI / a", "cla/google"]
    assert pending is False


def test_skipped_neutral_and_cancelled_do_not_fail():
    failing, _ = g.evaluate_checks(
        [
            run("a", conclusion="SKIPPED"),
            run("b", conclusion="NEUTRAL"),
            run("c", conclusion="CANCELLED"),
        ],
        CFG,
    )
    assert failing == []


def test_running_checks_are_pending_not_failing():
    failing, pending = g.evaluate_checks(
        [run("a", status="IN_PROGRESS"), status("cla/google", "PENDING")], CFG
    )
    assert failing == []
    assert pending is True


def test_a_rerun_that_passed_supersedes_the_failed_attempt():
    failing, _ = g.evaluate_checks(
        [
            run("a", conclusion="FAILURE", at="2026-01-01T00:00:00Z"),
            run("a", conclusion="SUCCESS", at="2026-01-01T01:00:00Z"),
        ],
        CFG,
    )
    assert failing == []


def test_a_queued_rerun_supersedes_the_failed_attempt_it_retries():
    failing, pending = g.evaluate_checks(
        [
            run("a", conclusion="FAILURE", at="2026-01-01T00:00:00Z"),
            run("a", status="QUEUED", at=None),
        ],
        CFG,
    )
    assert failing == []
    assert pending is True


def test_ignored_workflows_are_skipped_including_by_glob():
    failing, pending = g.evaluate_checks(
        [
            run("x", workflow="Review Gate", status="IN_PROGRESS"),
            run(
                "y", workflow="🔒 AI PR Review — Security", conclusion="FAILURE"
            ),
        ],
        CFG,
    )
    assert failing == []
    assert pending is False


def test_house_rules_is_not_ignored_by_the_real_config():
    cfg = g.load_config()
    failing, _ = g.evaluate_checks(
        [
            run(
                "Check the house rules",
                workflow="📐 AI PR Review — House Rules",
                conclusion="FAILURE",
            ),
            run(
                "trigger / review",
                workflow="🔍 AI PR Review — Correctness",
                conclusion="FAILURE",
            ),
        ],
        cfg,
    )
    assert [c.name for c in failing] == [
        "📐 AI PR Review — House Rules / Check the house rules"
    ]


# --- the scenarios, plus the in-flight case --------------------------------


def red(n):
    return [run(f"job{i}", conclusion="FAILURE") for i in range(n)]


@pytest.mark.parametrize(
    ("contexts", "failing", "action"),
    [
        pytest.param(red(2), 2, "bounce", id="broken"),
        pytest.param([run("ok")], 0, "none", id="clean"),
    ],
)
def test_scenarios(contexts, failing, action):
    ready = g.readiness(pr(contexts), CFG)
    assert len(ready.failing) == failing
    assert g.decide(ready, has_requests=True, gate=None).action == action


def test_unresolved_review_threads_never_hold_a_review():
    # GitHub still returns review threads; the gate must ignore them.
    unresolved = {
        "isResolved": False,
        "path": "a.py",
        "comments": {
            "nodes": [{"author": {"login": "github-actions"}, "url": "u"}]
        },
    }
    ready = g.readiness(
        {**pr([run("ok")]), "reviewThreads": {"nodes": [unresolved]}}, CFG
    )
    assert not ready.blocked
    assert g.decide(ready, has_requests=True, gate=None).action == "none"


def test_checks_still_running_hold_rather_than_bounce():
    ready = g.readiness(pr([run("a", status="QUEUED")]), CFG)
    assert g.decide(ready, has_requests=True, gate=None).action == "none"


def test_a_definite_failure_bounces_even_while_others_run():
    ready = g.readiness(
        pr([run("a", status="QUEUED"), run("b", conclusion="FAILURE")]), CFG
    )
    assert g.decide(ready, has_requests=True, gate=None).action == "bounce"


def test_a_bounced_pr_stays_bounced_while_the_fix_is_checked():
    ready = g.readiness(pr([run("a", status="IN_PROGRESS")], requests=0), CFG)
    assert g.decide(ready, False, blocked_gate()).action == "none"


def test_a_bounced_pr_is_released_once_green():
    ready = g.readiness(pr([run("a")], requests=0), CFG)
    assert g.decide(ready, False, blocked_gate()).action == "release"


def test_a_still_red_bounced_pr_only_updates_its_comment():
    ready = g.readiness(pr(red(1), requests=0), CFG)
    assert g.decide(ready, False, blocked_gate()).action == "update"


def test_red_pr_nobody_asked_about_is_left_alone():
    ready = g.readiness(pr(red(1), requests=0), CFG)
    assert g.decide(ready, False, None).action == "none"


# --- comment ---------------------------------------------------------------


def test_marker_round_trips():
    stored = g.Reviewers(users=["alice", "bob"], teams=["devex"])
    body = g.blocked_body(
        g.Readiness([g.Check("CI / a", None)], False), stored, CFG.label
    )
    assert g.parse_marker(body) == ("blocked", stored)


def test_the_comment_never_mentions_the_removed_reviewers():
    stored = g.Reviewers(users=["alice"], teams=["devex"])
    body = g.blocked_body(
        g.Readiness([g.Check("CI / a", "u")], False), stored, CFG.label
    )
    assert "@alice" not in body and "@devex" not in body
    assert "`alice`" in body


# --- orchestration, with the API faked ------------------------------------


@pytest.fixture
def api(monkeypatch):
    calls = []

    def rest_json(method, path, payload=None):
        calls.append((method, path.split("/", 3)[-1], payload))

    monkeypatch.setattr(g, "rest_json", rest_json)
    monkeypatch.setattr(g, "REPO", "o/r")
    return calls


def test_bounce_removes_reviewers_comments_and_labels(api, monkeypatch):
    monkeypatch.setattr(g, "fetch_gate_comment", lambda n: None)
    monkeypatch.setattr(
        g, "fetch_requested", lambda n: g.Reviewers(users=["alice"])
    )
    g.process(pr(red(1)), CFG, dry_run=False)

    methods = [(m, p) for m, p, _ in api]
    assert ("DELETE", "pulls/7/requested_reviewers") in methods
    assert ("POST", "issues/7/comments") in methods
    assert ("POST", "issues/7/labels") in methods
    body = next(p["body"] for m, path, p in api if path == "issues/7/comments")
    assert g.parse_marker(body) == ("blocked", g.Reviewers(users=["alice"]))


def test_bounce_labels_before_it_removes_anyone(api, monkeypatch):
    # The label is how the sweep finds a bounced PR again once its reviewers
    # are gone, so it must land before they are removed.
    monkeypatch.setattr(g, "fetch_gate_comment", lambda n: None)
    monkeypatch.setattr(
        g, "fetch_requested", lambda n: g.Reviewers(users=["alice"])
    )
    g.process(pr(red(1)), CFG, dry_run=False)

    order = [(m, p) for m, p, _ in api]
    assert order.index(("POST", "issues/7/labels")) < order.index(
        ("DELETE", "pulls/7/requested_reviewers")
    )
    assert order.index(("POST", "issues/7/comments")) < order.index(
        ("DELETE", "pulls/7/requested_reviewers")
    )


def test_a_second_bounce_remembers_the_first_reviewers(api, monkeypatch):
    monkeypatch.setattr(g, "fetch_gate_comment", lambda n: blocked_gate())
    monkeypatch.setattr(
        g, "fetch_requested", lambda n: g.Reviewers(users=["bob"])
    )
    g.process(pr(red(1), labels=[CFG.label]), CFG, dry_run=False)

    body = next(p["body"] for m, path, p in api if m == "PATCH")
    assert g.parse_marker(body)[1].users == ["alice", "bob"]
    assert not any(path == "issues/7/labels" for _, path, _ in api)


def test_release_re_requests_and_drops_the_label(api, monkeypatch):
    monkeypatch.setattr(
        g, "fetch_gate_comment", lambda n: blocked_gate(teams=["devex"])
    )
    g.process(
        pr([run("ok")], requests=0, labels=[CFG.label]), CFG, dry_run=False
    )

    assert (
        "POST",
        "pulls/7/requested_reviewers",
        {"reviewers": ["alice"], "team_reviewers": ["devex"]},
    ) in api
    assert any(
        m == "DELETE" and path.startswith("issues/7/labels/")
        for m, path, _ in api
    )
    body = next(p["body"] for m, path, p in api if m == "PATCH")
    assert g.parse_marker(body)[0] == "released"


def test_dry_run_changes_nothing(api, monkeypatch):
    monkeypatch.setattr(g, "fetch_gate_comment", lambda n: None)
    g.process(pr(red(1)), CFG, dry_run=True)
    assert api == []


def test_drafts_are_skipped(api):
    assert "skipped" in g.process(pr(red(1), draft=True), CFG, dry_run=False)
    assert api == []


@pytest.mark.parametrize("value", [None, "", "not-a-repo"])
def test_refuses_to_run_without_a_target_repository(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    else:
        monkeypatch.setenv("GITHUB_REPOSITORY", value)
    monkeypatch.setattr("sys.argv", ["review_gate.py", "--sweep", "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        g.main()
    assert exc.value.code == 2


def test_one_bad_reviewer_does_not_strand_the_rest(monkeypatch):
    monkeypatch.setattr(g, "REPO", "o/r")
    posted = []

    def rest_json(method, path, payload=None):
        if len(payload["reviewers"]) + len(payload["team_reviewers"]) > 1:
            raise g.GhError("batch rejected")
        if payload["reviewers"] == ["gone"]:
            raise g.GhError("not a collaborator")
        posted.append(payload)

    monkeypatch.setattr(g, "rest_json", rest_json)
    failed = g.request_reviewers(
        7, g.Reviewers(users=["alice", "gone"], teams=["devex"])
    )
    assert failed == g.Reviewers(users=["gone"])
    assert {"reviewers": ["alice"], "team_reviewers": []} in posted
    assert {"reviewers": [], "team_reviewers": ["devex"]} in posted
