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
"""Unit tests for post_review_comments.py.

This script decides where an automated review comment lands. Its failure
modes are quiet ones: an anchor computed one line off puts a comment on
innocent code, and a mis-parsed file header drops every finding in a file
while reporting only "not a line this PR adds". Neither shows up as a red
check — the review just says something wrong, or says nothing.

Every test below pins one of those.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import post_review_comments as m
import pytest

# Must come after post_review_comments: importing that is what puts tools/ on
# sys.path. isort keeps plain imports above from-imports so the order holds,
# and reordering it would fail loudly at collection rather than silently.
from ci_message import EXIT_CI_FAULT

SCRIPT = Path(m.__file__)


def _diff(*rows: str) -> str:
    return "\n".join(rows) + "\n"


# --------------------------------------------------------------------------
# added_line_anchors
# --------------------------------------------------------------------------


def test_anchors_track_new_file_line_numbers():
    diff = _diff(
        "diff --git a/x.py b/x.py",
        "index 111..222 100644",
        "--- a/x.py",
        "+++ b/x.py",
        "@@ -10,3 +10,4 @@",
        " context",
        "-removed",
        "+added_one",
        "+added_two",
        " trailing",
    )
    # Hunk starts at new-file line 10: " context" is 10, the two added
    # lines are 11 and 12 (the removed line consumes no new-side number).
    assert m.added_line_anchors(diff) == {"x.py": {11, 12}}


def test_added_line_that_looks_like_a_file_header_is_not_one():
    """An added line beginning "++ " renders as the row "+++ ...".

    Prefix-sniffing read that as a `+++ b/path` header, set the path to the
    line's own text, and silently lost every later anchor in the file.
    """
    diff = _diff(
        "--- a/doc.md",
        "+++ b/doc.md",
        "@@ -1,2 +1,4 @@",
        " intro",
        "++ this documents a diff marker",
        "+a real finding lands here",
        " outro",
    )
    assert m.added_line_anchors(diff) == {"doc.md": {2, 3}}


def test_removed_line_that_looks_like_a_file_header_is_not_one():
    diff = _diff(
        "--- a/doc.md",
        "+++ b/doc.md",
        "@@ -1,3 +1,2 @@",
        " intro",
        "-- this line is going away",
        "+replacement",
    )
    assert m.added_line_anchors(diff) == {"doc.md": {2}}


def test_one_line_hunk_without_explicit_lengths():
    diff = _diff("--- a/x.py", "+++ b/x.py", "@@ -1 +1 @@", "-old", "+new")
    assert m.added_line_anchors(diff) == {"x.py": {1}}


def test_multiple_hunks_and_multiple_files():
    diff = _diff(
        "--- a/a.py",
        "+++ b/a.py",
        "@@ -1,1 +1,2 @@",
        " keep",
        "+first",
        "@@ -20,1 +21,2 @@",
        " keep",
        "+second",
        "--- a/b.py",
        "+++ b/b.py",
        "@@ -5,0 +6,1 @@",
        "+only",
    )
    assert m.added_line_anchors(diff) == {"a.py": {2, 22}, "b.py": {6}}


def test_new_file_has_no_old_side():
    diff = _diff(
        "--- /dev/null",
        "+++ b/new.py",
        "@@ -0,0 +1,2 @@",
        "+line one",
        "+line two",
    )
    assert m.added_line_anchors(diff) == {"new.py": {1, 2}}


def test_deleted_file_contributes_no_anchors():
    diff = _diff(
        "--- a/gone.py",
        "+++ /dev/null",
        "@@ -1,2 +0,0 @@",
        "-line one",
        "-line two",
    )
    assert m.added_line_anchors(diff) == {}


def test_no_newline_marker_does_not_shift_numbering():
    diff = _diff(
        "--- a/x.py",
        "+++ b/x.py",
        "@@ -1,2 +1,2 @@",
        " first",
        "-second",
        "\\ No newline at end of file",
        "+second!",
        "\\ No newline at end of file",
    )
    assert m.added_line_anchors(diff) == {"x.py": {2}}


def test_unparseable_hunk_header_yields_no_anchors():
    """Guessing a start line anchors comments onto real but wrong lines.

    A wrong anchor still passes validation and gets posted, so a hunk we
    cannot place must contribute nothing.
    """
    diff = _diff("--- a/x.py", "+++ b/x.py", "@@ garbled @@", "+added", "+more")
    assert m.added_line_anchors(diff) == {}


def test_truncated_diff_keeps_the_anchors_it_did_see():
    """The workflow cuts the diff to fit the argv budget, mid-hunk."""
    diff = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,9 +1,9 @@\n"
        " keep\n"
        "+added\n"
        "+part"  # cut mid-hunk, no trailing newline
        "\n[... diff truncated — review only what is shown above ...]"
    )
    assert m.added_line_anchors(diff) == {"x.py": {2, 3}}


def test_empty_diff():
    assert m.added_line_anchors("") == {}


# --------------------------------------------------------------------------
# extract_findings
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        '```json\n[{"path": "x.py"}]\n```',
        '```\n[{"path": "x.py"}]\n```',
        'Here you go:\n\n```json\n[{"path": "x.py"}]\n```\n\nHope that helps.',
        '[{"path": "x.py"}]',
        '   [{"path": "x.py"}]   ',
    ],
)
def test_extract_findings_accepts_realistic_shapes(response):
    assert m.extract_findings(response) == [{"path": "x.py"}]


def test_extract_findings_keeps_brackets_inside_a_body():
    response = '```json\n[{"body": "index arr[0] is off by one"}]\n```'
    assert m.extract_findings(response) == [
        {"body": "index arr[0] is off by one"}
    ]


def test_extract_findings_accepts_an_empty_array():
    assert m.extract_findings("```json\n[]\n```") == []


def test_extract_findings_accepts_a_repeated_empty_array():
    # What PR #2547's reviewer actually returned.
    assert m.extract_findings("```json\n[]\n[]\n```") == []


def test_extract_findings_merges_several_arrays_in_one_block():
    response = '```json\n[{"path": "x.py"}]\n[{"path": "y.py"}]\n```'
    assert m.extract_findings(response) == [{"path": "x.py"}, {"path": "y.py"}]


def test_extract_findings_drops_a_repeated_finding():
    response = '```json\n[{"path": "x.py"}]\n[{"path": "x.py"}]\n```'
    assert m.extract_findings(response) == [{"path": "x.py"}]


def test_extract_findings_keeps_findings_despite_trailing_junk():
    response = '```json\n[{"path": "x.py"}]\nand also [oops\n```'
    assert m.extract_findings(response) == [{"path": "x.py"}]


@pytest.mark.parametrize(
    "response",
    [
        "I reviewed it and found nothing.",
        '```json\n{"path": "x.py"}\n```',
        "```json\n[{oops}\n```",
        "",
    ],
)
def test_extract_findings_rejects_unusable_output(response):
    with pytest.raises(m.ReviewerOutputError):
        m.extract_findings(response)


# --------------------------------------------------------------------------
# build_comments
# --------------------------------------------------------------------------

ANCHORS = {"x.py": {10, 11}}


def test_build_comments_emits_a_right_side_line_comment():
    comments, _notes, skipped = m.build_comments(
        [{"path": "x.py", "line": 10, "body": "Off by one."}], ANCHORS
    )
    assert comments == [
        {"path": "x.py", "line": 10, "side": "RIGHT", "body": "Off by one."}
    ]
    assert skipped == []


def test_build_comments_strips_a_stray_b_prefix():
    comments, _notes, _skipped = m.build_comments(
        [{"path": "b/x.py", "line": 10, "body": "note"}], ANCHORS
    )
    assert comments[0]["path"] == "x.py"


def test_a_real_path_starting_with_b_slash_is_not_stripped():
    """A repo with a top-level `b/` directory has real paths like this.

    Stripping unconditionally would turn `b/pkg.py` into `pkg.py`, match
    nothing, and silently drop every finding in that directory.
    """
    anchors = {"b/pkg.py": {7}}
    comments, _notes, skipped = m.build_comments(
        [{"path": "b/pkg.py", "line": 7, "body": "note"}], anchors
    )
    assert comments[0]["path"] == "b/pkg.py"
    assert skipped == []


def test_build_comments_accepts_a_stringified_line():
    comments, _notes, _skipped = m.build_comments(
        [{"path": "x.py", "line": "10", "body": "note"}], ANCHORS
    )
    assert comments[0]["line"] == 10


@pytest.mark.parametrize(
    "finding",
    [
        {"path": "x.py", "line": 99, "body": "note"},  # not an added line
        {"path": "other.py", "line": 10, "body": "note"},  # untouched file
        {"path": "x.py", "line": 10, "body": "   "},  # empty body
        {"path": "", "line": 10, "body": "note"},  # empty path
        {"path": "x.py", "line": "ten", "body": "note"},  # unparseable
        {"path": "x.py", "line": None, "body": "note"},
        {"path": "x.py", "line": 10.5, "body": "note"},  # lossy
        {"path": "x.py", "line": True, "body": "note"},  # bool is an int
        "not an object",
    ],
)
def test_build_comments_drops_unpostable_findings(finding):
    comments, _notes, skipped = m.build_comments([finding], ANCHORS)
    assert comments == []
    assert len(skipped) == 1


def test_one_bad_finding_does_not_take_the_good_ones_with_it():
    """GitHub rejects the whole review for one bad position."""
    comments, _notes, skipped = m.build_comments(
        [
            {"path": "x.py", "line": 10, "body": "good"},
            {"path": "x.py", "line": 9999, "body": "bad anchor"},
            {"path": "x.py", "line": 11, "body": "also good"},
        ],
        ANCHORS,
    )
    assert [c["line"] for c in comments] == [10, 11]
    assert len(skipped) == 1


# --------------------------------------------------------------------------
# implausible_body — the shape limits on what reaches a public PR
#
# The threat these answer is b/555419958: a body is model text derived from a
# diff a fork author wrote, and it is posted verbatim through the API, where
# log masking does not reach. The limits are not the boundary — the empty agy
# tool allowlist in the workflow is — so what these tests pin is the
# calibration, which is the part that rots. Both directions matter equally:
# too loose and the channel carries a credential, too tight and it silently
# drops real reviews.
# --------------------------------------------------------------------------

# The longest path in this repository at the time the cap was set. Quoting a
# path is what a legitimate finding does, so this is the shape
# MAX_UNBROKEN_RUN has to keep accepting. If a longer path ever lands, this
# fails here rather than by dropping a contributor's review comment.
LONGEST_REPO_PATH = (
    "java/agents/time-series-forecasting/src/main/java/com/google/adk/"
    "samples/agents/timeseriesforecasting/ForecastingAgent.java"
)


@pytest.mark.parametrize(
    "body",
    [
        "Off by one.",
        "This drops the error instead of raising it; re-raise after logging.",
        f"`{LONGEST_REPO_PATH}` is imported here but never used.",
        "See https://github.com/google/adk-samples/blob/main/AGENTS.md — "
        "gemini-2.5-flash is deprecated in this repo.",
        # Both caps exactly at their limit, which is where an off-by-one in
        # either comparison would show up.
        ("word " * m.MAX_BODY_CHARS)[: m.MAX_BODY_CHARS],
        "y" * m.MAX_UNBROKEN_RUN,
    ],
)
def test_a_real_review_comment_is_plausible(body):
    assert m.implausible_body(body) is None


def test_the_longest_repo_path_clears_the_run_cap():
    """Pins the calibration itself, not just its effect."""
    assert len(LONGEST_REPO_PATH) < m.MAX_UNBROKEN_RUN


def test_an_over_long_body_is_implausible():
    reason = m.implausible_body("x" * (m.MAX_BODY_CHARS + 1))
    assert reason is not None
    assert str(m.MAX_BODY_CHARS) in reason


def test_an_unbroken_run_past_the_cap_is_implausible():
    reason = m.implausible_body(
        "The value is " + "A" * (m.MAX_UNBROKEN_RUN + 1)
    )
    assert reason is not None
    assert "unbroken run" in reason


def test_a_credential_shaped_payload_is_implausible():
    """The concrete thing this exists to refuse.

    Shaped like the ADC file the runner holds: an external_account config
    whose credential_source embeds the runner's OIDC request token.
    """
    payload = json.dumps(
        {
            "type": "external_account",
            "audience": "//iam.googleapis.com/projects/123456789/locations/"
            "global/workloadIdentityPools/gh-pool/providers/gh-provider",
            "token_url": "https://sts.googleapis.com/v1/token",
            "credential_source": {
                "url": "https://pipelinesghubeus.actions.githubusercontent.com"
                "/abcdef/_apis/distributedtask/hubs/Actions/plans/0000/jobs"
                "/idtoken",
                "headers": {"Authorization": "bearer " + "e" * 400},
            },
        }
    )
    assert m.implausible_body(payload) is not None


def test_an_implausible_body_is_dropped_before_it_can_be_posted():
    comments, notes, skipped = m.build_comments(
        [{"path": "x.py", "line": 10, "body": "z" * (m.MAX_BODY_CHARS + 1)}],
        ANCHORS,
    )
    assert (comments, notes) == ([], [])
    assert len(skipped) == 1


def test_the_note_path_is_shape_checked_too():
    """A note is not posted inline, but it does reach the review body.

    Both are public, so a check covering only inline comments would leave half
    the channel open — and the note path is the easier half to reach, since it
    takes any window-verified line rather than only lines the PR adds.
    """
    diff = _diff(
        "--- a/x.py",
        "+++ b/x.py",
        "@@ -1,2 +1,3 @@",
        " def handler(req):",
        "-    return None",
        "+    return req",
    )
    anchors, text = m.walk_right_side(diff)
    comments, notes, skipped = m.build_comments(
        [
            {
                "path": "x.py",
                "line": 1,
                "body": "A" * (m.MAX_UNBROKEN_RUN + 1),
                "window": "  1: def handler(req):",
            }
        ],
        anchors,
        text,
    )
    assert (comments, notes) == ([], [])
    assert len(skipped) == 1


def test_one_implausible_body_does_not_take_the_good_ones_with_it():
    comments, _notes, skipped = m.build_comments(
        [
            {"path": "x.py", "line": 10, "body": "good"},
            {"path": "x.py", "line": 11, "body": "q" * (m.MAX_BODY_CHARS + 1)},
        ],
        ANCHORS,
    )
    assert [c["line"] for c in comments] == [10]
    assert len(skipped) == 1


# --------------------------------------------------------------------------
# main() — end to end, as the workflow invokes it
# --------------------------------------------------------------------------


def _run(tmp_path: Path, response: str, diff: str) -> tuple[int, Path, str]:
    result = tmp_path / "agy_result.json"
    result.write_text(json.dumps({"response": response}), encoding="utf-8")
    diff_file = tmp_path / "pr_diff_used.txt"
    diff_file.write_text(diff, encoding="utf-8")
    out = tmp_path / "review_payload.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--result",
            str(result),
            "--diff",
            str(diff_file),
            "--label",
            "Correctness",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, out, proc.stdout


DIFF = _diff("--- a/x.py", "+++ b/x.py", "@@ -1,1 +1,2 @@", " keep", "+added")


def test_main_writes_a_payload_the_rest_api_accepts(tmp_path):
    response = (
        '```json\n[{"path": "x.py", "line": 2, "body": "Off by one."}]\n```'
    )
    code, out, _ = _run(tmp_path, response, DIFF)
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["event"] == "COMMENT"
    assert payload["body"]  # required by the API for a COMMENT event
    assert payload["comments"] == [
        {
            "path": "x.py",
            "line": 2,
            "side": "RIGHT",
            # Tagged at post time. The lane decides it: this run is the
            # default Correctness lane, so MAJOR.
            "body": "[MAJOR] Off by one.",
        }
    ]


def test_main_writes_nothing_when_there_are_no_findings(tmp_path):
    code, out, _ = _run(tmp_path, "```json\n[]\n```", DIFF)
    assert code == 0
    assert not out.exists()


def test_main_writes_nothing_when_every_finding_is_dropped(tmp_path):
    response = '```json\n[{"path": "x.py", "line": 999, "body": "n"}]\n```'
    code, out, stdout = _run(tmp_path, response, DIFF)
    assert code == 0
    assert not out.exists()
    assert "dropped finding" in stdout
    # A dropped finding is not a contributor-facing annotation.
    assert "::warning::" not in stdout


def test_main_fails_loudly_on_unusable_reviewer_output(tmp_path):
    """A reviewer that returned nothing usable is a CI fault, not a verdict.

    It must not be reported as a problem with the pull request: exit with
    the dedicated CI-fault code and annotate this checker, never a file.
    """
    code, out, stdout = _run(tmp_path, "I found nothing.", DIFF)
    assert code == EXIT_CI_FAULT
    assert not out.exists()
    assert "[CI FAULT]" in stdout
    assert "post_review_comments.py" in stdout
    assert "file=" not in stdout  # never point at contributor code


def test_main_fails_when_the_result_file_is_not_json(tmp_path):
    result = tmp_path / "agy_result.json"
    result.write_text("", encoding="utf-8")
    diff_file = tmp_path / "d.txt"
    diff_file.write_text(DIFF, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--result",
            str(result),
            "--diff",
            str(diff_file),
            "--label",
            "Security",
            "--out",
            str(tmp_path / "out.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == EXIT_CI_FAULT
    assert "[CI FAULT]" in proc.stdout


def test_main_fails_when_the_result_is_not_an_object(tmp_path):
    result = tmp_path / "agy_result.json"
    result.write_text("[1, 2, 3]", encoding="utf-8")
    diff_file = tmp_path / "d.txt"
    diff_file.write_text(DIFF, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--result",
            str(result),
            "--diff",
            str(diff_file),
            "--label",
            "Security",
            "--out",
            str(tmp_path / "out.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == EXIT_CI_FAULT
    assert "not a JSON object" in proc.stdout


def test_diff_trimmed_mid_utf8_character_does_not_crash(tmp_path):
    """The workflow trims the diff with `head -c`, which cuts bytes.

    Sooner or later that cut lands inside a multi-byte character — any diff
    touching an em dash or an accent is a candidate. Strict decoding raised
    an uncaught UnicodeDecodeError and threw away the whole review.
    """
    whole = (
        "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,2 @@\n keep\n+caf\u00e9 au lait\n"
    )
    raw = whole.encode("utf-8")
    cut = raw[: raw.index("\u00e9".encode()) + 1]  # split the 2-byte é
    assert cut.decode("utf-8", "ignore") != cut.decode("utf-8", "replace")

    result = tmp_path / "agy_result.json"
    result.write_text(
        json.dumps(
            {
                "response": (
                    '```json\n[{"path": "x.py", "line": 2, '
                    '"body": "note"}]\n```'
                )
            }
        ),
        encoding="utf-8",
    )
    diff_file = tmp_path / "pr_diff_used.txt"
    diff_file.write_bytes(cut)
    out = tmp_path / "review_payload.json"

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--result",
            str(result),
            "--diff",
            str(diff_file),
            "--label",
            "Correctness",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr
    # The partial line is still an added line, so the finding survives.
    assert (
        json.loads(out.read_text(encoding="utf-8"))["comments"][0]["line"] == 2
    )


def test_restrict_to_pr_drops_lines_the_pr_does_not_add():
    # Incremental diff across a base-branch merge: line 2 came from base.
    incremental = {"x.py": {1, 2, 3}, "y.py": {5}}
    pr_diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,3 @@\n+one\n two\n+three\n"
    restricted = m.restrict_to_pr(incremental, pr_diff)
    assert restricted == {"x.py": {1, 3}, "y.py": set()}


def test_pr_diff_keeps_base_branch_lines_out_of_inline_comments(tmp_path):
    """#2677: every comment was on a line a merge from main brought in.

    The incremental diff showed that line as added, so it passed validation,
    and GitHub then rejected the comment with HTTP 422 because the line is
    not in the PR's own diff.
    """
    result = tmp_path / "agy_result.json"
    result.write_text(
        json.dumps(
            {
                "response": (
                    '```json\n[{"path": "x.py", "line": 2, "body": "from '
                    'base"}, {"path": "x.py", "line": 3, "body": "from pr"}]'
                    "\n```"
                )
            }
        ),
        encoding="utf-8",
    )
    incremental = tmp_path / "pr_diff_used.txt"
    incremental.write_text(
        "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,3 @@\n+one\n+two\n+three\n",
        encoding="utf-8",
    )
    whole = tmp_path / "pr_diff_full.txt"
    whole.write_text(
        "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,3 @@\n+one\n two\n+three\n",
        encoding="utf-8",
    )

    def run(*extra: str) -> list[int]:
        out = tmp_path / "review_payload.json"
        out.unlink(missing_ok=True)
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--result",
                str(result),
                "--diff",
                str(incremental),
                *extra,
                "--label",
                "Correctness",
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(out.read_text(encoding="utf-8"))
        return sorted(c["line"] for c in payload["comments"])

    assert run() == [2, 3]
    assert run("--pr-diff", str(whole)) == [3]


def test_unreadable_pr_diff_is_a_ci_fault(tmp_path):
    result = tmp_path / "agy_result.json"
    result.write_text(json.dumps({"response": "[]"}), encoding="utf-8")
    diff_file = tmp_path / "pr_diff_used.txt"
    diff_file.write_text("", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--result",
            str(result),
            "--diff",
            str(diff_file),
            "--pr-diff",
            str(tmp_path / "missing.txt"),
            "--label",
            "Correctness",
            "--out",
            str(tmp_path / "out.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "cannot read diff" in proc.stdout


def test_the_workflow_passes_the_whole_pr_diff_for_anchors():
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
    fetch = next(s for s in steps if s.get("id") == "fetch_diff")["run"]
    build = next(s for s in steps if s.get("id") == "build_review")["run"]
    assert "pr_diff_full.txt" in fetch
    assert "--pr-diff pr_diff_full.txt" in build
    assert "--pr-diff" in {
        opt
        for action in m.build_parser()._actions
        for opt in action.option_strings
    }


def test_workflow_invokes_this_script_with_the_flags_it_defines():
    """Pin the workflow -> CLI contract.

    post_review_comments.py is called from a shell block in
    _ai-pr-review-core.yml, so a renamed flag or a moved file is invisible to
    both ruff and pytest and only shows up as a failed review on a real PR.
    """
    # A hard import, not importorskip: pyyaml is a project dependency, so a
    # skip here could only ever mean this pin quietly stopped being enforced.
    import yaml

    workflow = (
        Path(__file__).resolve().parents[3]
        / ".github"
        / "workflows"
        / "_ai-pr-review-core.yml"
    )
    # The `review` job, not `post`: building the payload and posting it are
    # deliberately different jobs, and this script runs in the one that holds
    # no write token. If that ever moves back into `post`, this fails — which
    # is the point, because the split is a security boundary (b/555419958).
    steps = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"][
        "review"
    ]["steps"]
    build = next(s for s in steps if s.get("id") == "build_review")

    invocation = build["run"]
    assert "python3 .github/scripts/post_review_comments.py" in invocation
    for flag in ("--result", "--diff", "--label", "--out"):
        assert flag in invocation, f"workflow no longer passes {flag}"

    # ...and the script still defines exactly those flags.
    defined = {
        opt
        for action in m.build_parser()._actions
        for opt in action.option_strings
    }
    assert {"--result", "--diff", "--label", "--out"} <= defined


# --------------------------------------------------------------------------
# Window verification
#
# The prompt's severity gate ("only critical or high") used to be the only
# thing between a wrong finding and a contributor's PR. It has been replaced
# by a wider one, so these checks are what keeps precision up. Each test below
# pins one of them.
# --------------------------------------------------------------------------

WINDOW_DIFF = _diff(
    "--- a/x.py",
    "+++ b/x.py",
    "@@ -1,2 +1,4 @@",
    " def handler(req):",
    "-    return None",
    "+    name = req.args['n']",
    "+    os.system(f'echo {name}')",
    "+    return name",
)


def _one(finding, diff=WINDOW_DIFF, existing=None):
    anchors, text = m.walk_right_side(diff)
    return m.build_comments([finding], anchors, text, existing)


def test_a_matching_window_is_kept():
    comments, notes, skipped = _one(
        {
            "path": "x.py",
            "line": 3,
            "body": "shell injection here",
            "window": "  2:     name = req.args['n']\n  3:     os.system(f'echo {name}')",
        }
    )
    assert [c["line"] for c in comments] == [3]
    assert (notes, skipped) == ([], [])


def test_a_fabricated_window_is_dropped():
    """A lane that invents a finding invents the source under it too.

    This is the check that makes a wider filter safe: without it the only
    defence against a hallucinated finding is a real line number, which a
    model guesses correctly often enough to be no defence at all.
    """
    comments, _notes, skipped = _one(
        {
            "path": "x.py",
            "line": 3,
            "body": "sql injection here",
            "window": "  3:     cursor.execute('SELECT ' + name)",
        }
    )
    assert comments == []
    assert "window says" in skipped[0]


def test_a_window_off_by_one_corrects_the_anchor():
    """Real finding, wrong arithmetic — repair it rather than lose it.

    Counting new-file line numbers out of a unified diff by hand is the part
    of the job a model is worst at, and dropping those findings throws away
    correct work over an off-by-one.
    """
    comments, _notes, skipped = _one(
        {
            "path": "x.py",
            "line": 4,
            "body": "shell injection here",
            "window": "  3:     name = req.args['n']\n  4:     os.system(f'echo {name}')",
        }
    )
    assert [c["line"] for c in comments] == [3]
    assert skipped == []


def test_a_finding_with_no_window_still_needs_a_real_added_line():
    """No window means no verification, so the old rule stands unchanged."""
    good, _n1, _s1 = _one({"path": "x.py", "line": 3, "body": "note"})
    bad, _n2, skipped = _one({"path": "x.py", "line": 99, "body": "note"})
    assert len(good) == 1
    assert bad == []
    assert "not a line this PR adds" in skipped[0]


def test_a_verified_finding_on_an_unchanged_line_becomes_a_note():
    """Real, but GitHub will not take an inline comment there.

    These used to go to the job log and vanish. On PR #2373 that class held
    all three of the hard CI failures the review found.
    """
    comments, notes, skipped = _one(
        {
            "path": "x.py",
            "line": 1,
            "body": "no type hints on this signature",
            "window": "  1: def handler(req):",
        }
    )
    assert comments == []
    assert notes == [
        {"path": "x.py", "line": 1, "body": "no type hints on this signature"}
    ]
    assert skipped == []


def test_an_unverified_finding_on_an_unchanged_line_is_not_promoted():
    """An unverifiable line number is model arithmetic, not a finding.

    Promoting those to the review body would surface exactly the mistakes
    window verification exists to catch.
    """
    comments, notes, skipped = _one(
        {
            "path": "x.py",
            "line": 1,
            "body": "note",
            "window": "  1: something that is not in this diff at all",
        }
    )
    assert (comments, notes) == ([], [])
    assert len(skipped) == 1


# --------------------------------------------------------------------------
# The cheapness gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "steps",
    [
        "trace the value back to its caller",
        "assuming the input is user-controlled, read line 3",
        "consider the case where the list is empty",
        "grep the repo for other callers",
    ],
)
def test_a_finding_that_admits_it_is_expensive_is_dropped(steps):
    """Cost to check, not severity, is what a comment is filtered on.

    Cheap and wrong costs the author five seconds; expensive and wrong costs
    twenty minutes and the credibility of every other comment in the review.
    """
    comments, _notes, skipped = _one(
        {
            "path": "x.py",
            "line": 3,
            "body": "note",
            "window": "  3:     os.system(f'echo {name}')",
            "verify_steps": steps,
        }
    )
    assert comments == []
    assert "not cheap to verify" in skipped[0]


def test_a_finding_settled_at_the_anchor_survives_the_gate():
    comments, _notes, skipped = _one(
        {
            "path": "x.py",
            "line": 3,
            "body": "note",
            "window": "  3:     os.system(f'echo {name}')",
            "verify_steps": "read line 3 of this file",
        }
    )
    assert len(comments) == 1
    assert skipped == []


# --------------------------------------------------------------------------
# Duplicate suppression
#
# Four lanes review every push. Without this each one repeats itself on every
# `synchronize` and repeats whatever the other three found in the overlap
# between their remits.
# --------------------------------------------------------------------------


def _existing(path, line, body):
    return [
        {
            "kind": "inline",
            "path": path,
            "line": line,
            "original_line": line,
            "body": body,
        }
    ]


def test_a_comment_already_on_the_line_suppresses_the_finding():
    comments, _notes, skipped = _one(
        {"path": "x.py", "line": 3, "body": "shell injection here"},
        existing=_existing("x.py", 3, "anything at all"),
    )
    assert comments == []
    assert "already commented on this line" in skipped[0]


def test_suppression_reaches_two_lines_either_side():
    comments, _notes, _skipped = _one(
        {"path": "x.py", "line": 4, "body": "note"},
        existing=_existing("x.py", 2, "something"),
    )
    assert comments == []


def test_suppression_does_not_reach_three_lines_away():
    """Tight on purpose — widening it starts eating genuinely new findings."""
    comments, _notes, _skipped = _one(
        {"path": "x.py", "line": 3, "body": "note"},
        existing=_existing("x.py", 6, "something"),
    )
    assert len(comments) == 1


def test_a_similar_comment_elsewhere_suppresses_the_finding():
    """The other three lanes phrase the same defect differently."""
    comments, _notes, skipped = _one(
        {
            "path": "x.py",
            "line": 3,
            "body": "unsanitised filename interpolated into os.system",
        },
        existing=_existing(
            "other.py",
            99,
            "filename is interpolated into os.system unsanitised",
        ),
    )
    assert comments == []
    assert "very similar" in skipped[0]


def test_an_unrelated_existing_comment_does_not_suppress():
    comments, _notes, _skipped = _one(
        {"path": "x.py", "line": 3, "body": "shell injection in this handler"},
        existing=_existing("x.py", 40, "please rename this fixture"),
    )
    assert len(comments) == 1


# --------------------------------------------------------------------------
# Output parsing and payload shape
# --------------------------------------------------------------------------


def test_the_last_fenced_block_wins():
    """The reviewer now reasons in prose before answering.

    That scan quotes diff rows and sometimes fences them, so the first block
    in the response is no longer reliably the answer. The prompt puts the
    findings last; this reads them from the same end.
    """
    response = (
        "Working through the diff.\n\n"
        '```json\n[{"path": "nope.py", "line": 1, "body": "an example"}]\n```\n\n'
        "Now the real answer.\n\n"
        '```json\n[{"path": "x.py", "line": 2, "body": "the real one"}]\n```\n'
    )
    assert m.extract_findings(response) == [
        {"path": "x.py", "line": 2, "body": "the real one"}
    ]


def test_notes_are_listed_in_the_review_body():
    payload = m.build_payload(
        "Correctness",
        [{"path": "x.py", "line": 2, "side": "RIGHT", "body": "inline"}],
        [{"path": "y.toml", "line": 9, "body": "requires-python is 3.10"}],
    )
    assert "`y.toml:9` — [MAJOR] requires-python is 3.10" in payload["body"]
    assert len(payload["comments"]) == 1


def test_a_review_of_notes_alone_is_still_posted(tmp_path):
    """Nothing inline to say does not mean nothing to say."""
    response = json.dumps(
        [
            {
                "path": "x.py",
                "line": 1,
                "body": "no type hints here",
                "window": "  1: def handler(req):",
            }
        ]
    )
    code, out, _ = _run(tmp_path, f"```json\n{response}\n```", WINDOW_DIFF)
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["comments"] == []
    assert "no type hints here" in payload["body"]


def test_an_anchor_outside_its_own_window_is_pulled_into_it():
    """The window is checked against the diff; the anchor is not.

    A model that quotes the right code and then names the line underneath it
    would otherwise get a comment about a placeholder value posted on the
    `return` statement below it — right finding, wrong line, reads as
    carelessness.
    """
    comments, _notes, skipped = _one(
        {
            "path": "x.py",
            "line": 4,
            "body": "shell injection here",
            "window": "  3:     os.system(f'echo {name}')",
        }
    )
    assert [c["line"] for c in comments] == [3]
    assert skipped == []


def test_an_anchor_inside_its_window_is_left_alone():
    comments, _notes, _skipped = _one(
        {
            "path": "x.py",
            "line": 2,
            "body": "note",
            "window": "  2:     name = req.args['n']\n  3:     os.system(f'echo {name}')",
        }
    )
    assert [c["line"] for c in comments] == [2]


# --------------------------------------------------------------------------
# Salvaging a malformed findings block
#
# Findings quote source verbatim in `window`, and source is full of double
# quotes. A reviewer that forgets to escape one used to cost the whole review
# AND turn the check red. Caught by a dry run against a real PR, not by any
# test written beforehand — hence the recorded fixture.
# --------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def test_one_unescaped_quote_does_not_destroy_the_whole_review():
    """Regression: run 32896537749, Maintainability on PR #2545.

    The reviewer emitted `"window": "  211:     assert res["success"] is True"`
    — valid but for one unescaped pair. Three good findings were thrown away
    and the job failed with a CI-fault annotation on the contributor's PR.

    All three come back now. Salvage used to keep the two findings that were
    already well-formed and drop the third; repair rebuilds the block, so the
    malformed one is no longer a casualty either — and its window keeps the
    quotes that broke it.
    """
    response = (FIXTURES / "malformed_findings_response.txt").read_text(
        encoding="utf-8"
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(m.FENCED_BLOCK.findall(response)[-1])

    findings = m.extract_findings(response)
    assert len(findings) == 3
    paths = {f["path"].split("/")[-1] for f in findings}
    assert paths == {"test_routine_tool.py", "test_process_tool.py"}

    recovered = next(f for f in findings if f["line"] == 212)
    assert 'assert res["success"] is True' in recovered["window"]


def test_salvage_keeps_only_things_shaped_like_findings():
    """Advancing past a bad object can land on a `{` inside a string.

    Requiring path and body is what stops that debris becoming a comment.
    """
    block = (
        '[{"path": "a.py", "line": 1, "body": "real", "window": "x"broken"},'
        ' {"nested": {"not": "a finding"}},'
        ' {"path": "b.py", "line": 2, "body": "also real"}]'
    )
    salvaged = m._salvage_findings(block, json.JSONDecoder())
    assert [f["path"] for f in salvaged] == ["b.py"]


def test_a_block_that_salvages_nothing_is_still_a_ci_fault():
    """Silence must not be mistaken for a clean review.

    Nothing here carries a finding key, so there is no value for repair to
    delimit and nothing for salvage to keep. The fault must survive both.
    """
    with pytest.raises(m.ReviewerOutputError):
        m.extract_findings("```json\n[{totally broken}\n```")


def test_every_finding_malformed_still_yields_a_review():
    """Regression: the Correctness lane on PR #2566.

    Both findings quoted shell out of a workflow file, so both windows came
    back with raw quotes and salvage — which only keeps findings that were
    already well-formed — recovered nothing. The review died as a CI fault
    over output that was one escape away from usable.
    """
    response = (FIXTURES / "unescaped_quotes_response.txt").read_text(
        encoding="utf-8"
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(m.FENCED_BLOCK.findall(response)[-1])
    assert (
        m._salvage_findings(
            m.FENCED_BLOCK.findall(response)[-1], json.JSONDecoder()
        )
        == []
    )

    findings = m.extract_findings(response)
    assert len(findings) == 2
    assert all(
        f["path"] == ".github/workflows/typescript-tests.yml" for f in findings
    )
    assert '[ -f "yarn.lock" ]' in findings[0]["window"]
    assert '[ -f "bun.lockb" ]' in findings[1]["window"]


def test_a_bracket_after_a_stray_quote_is_not_a_value_end():
    """The trap that rules out the cheap way of finding a value's end.

    `[ -f "x.lock" ]` puts a `]` immediately after the stray quote. Ending a
    value at the next quote followed by `,`, `]` or `}` would stop there,
    truncating the array mid-string and losing every finding after it. Only a
    schema boundary — the next key, or the close of the object — ends a value.
    """
    block = '{"window": "  1: [ -f "x.lock" ]; then", "verify_steps": "x"}'
    start = block.index('"  1:') + 1

    end = m.STRING_FIELD_END.search(block, start)
    assert end is not None
    assert block[start : end.start()] == '  1: [ -f "x.lock" ]; then'


def test_repair_leaves_a_field_it_cannot_delimit_alone():
    """An unterminated value has no schema anchor after it.

    Rewriting on a guess would corrupt the one field still readable, so there
    is no reading to accept and the existing paths decide.
    """
    assert (
        m._repaired_findings('[{"path": "a.py", "body": "unterminated') is None
    )


def test_escaping_a_value_is_idempotent():
    """A value already escaped must not gain a second layer of backslashes.

    Repair normalises before it escapes, so re-reading a block that was only
    partly malformed cannot double up the quotes that were already correct.
    """
    assert m._escape_value('says \\"hi\\" loudly') == 'says \\"hi\\" loudly'
    assert m._escape_value('says "hi" loudly') == 'says \\"hi\\" loudly'


def test_repair_refuses_a_window_that_quotes_findings_shaped_source():
    """A second reading scrapes the finding's own fields out of the window.

    The window quotes source that is itself a findings array — which this
    repo's own tests contain — so one reading anchors at line 919 with the
    body "dup", and another stops the value early and takes `line` 1 and the
    body "real" from inside the quoted source. Both parse, so parsing cannot
    be the test. Posting the second would put source text on a contributor's
    PR as though it were a review, so the block is declined outright.
    """
    block = (
        '[{"path": "t.py", "line": 919, "body": "dup",'
        ' "window": "  919: [{"path": "a.py", "line": 1, "body": "real"}]"}]'
    )
    assert m._repaired_findings(block) is None


def test_repair_declines_rather_than_guessing_past_its_budget():
    """An exhausted search has not established that a reading is unique."""
    block = (
        '[{"path": "a.py", "line": 1, "body": "b",'
        ' "window": "' + '", "body": "x' * 40 + '"}]'
    )
    assert m._repaired_findings(block) is None


def _wide_malformed_block(findings: int) -> str:
    """A block with far more findings than a review ever asks for, one bad."""
    body = ",".join(
        f'{{"path": "f{i}.py", "line": 1, "body": "b", "window": "  1: x"}}'
        for i in range(findings)
    )
    return f"[{body}]".replace("  1: x", '  1: [ -f "a.lock" ]', 1)


def test_a_runaway_block_does_not_blow_the_stack():
    """Recursion made a wide block fatal instead of merely unreadable.

    One field was one frame, so a model answering with hundreds of findings
    raised RecursionError — and that escapes extract_findings entirely, so a
    block salvage could have read reached the CI fault instead. The walk is
    iterative for that reason; here it must simply decline.
    """
    assert m._repaired_findings(_wide_malformed_block(400)) is None


def test_a_runaway_block_is_bounded_in_time():
    """Charging only completed readings let a wide block run for minutes.

    Every later field boundary is a candidate end for every earlier field, so
    a 400-finding block pushed ~1600 copies of the reading per step and took
    over two minutes. Pushes are charged to the budget, and the ends weighed
    per field are capped, so the walk is bounded by shape as well as depth.
    """
    start = time.monotonic()
    assert m._repaired_findings(_wide_malformed_block(1000)) is None
    assert time.monotonic() - start < 5


def test_a_runaway_block_still_reaches_salvage():
    """Declining to repair must hand the block on, not end the review.

    The findings that were well-formed are still there to be kept, and before
    the walk was bounded they were lost with the rest.
    """
    findings = m.extract_findings(f"```json\n{_wide_malformed_block(400)}\n```")
    assert len(findings) == 399
    assert all(f["path"].endswith(".py") for f in findings)


# ------------------------------------------- the deterministic lane's input


def _diff_one_added_line(path="contrib/python/x/pyproject.toml"):
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,0 +1,2 @@\n"
        "+[tool.ruff]\n"
        "+line-length = 80\n"
    )


def _run_findings(tmp_path, findings, diff=None):
    findings_file = tmp_path / "findings.json"
    findings_file.write_text(json.dumps(findings), encoding="utf-8")
    diff_file = tmp_path / "diff.txt"
    diff_file.write_text(diff or _diff_one_added_line(), encoding="utf-8")
    out = tmp_path / "payload.json"
    rc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--findings",
            str(findings_file),
            "--diff",
            str(diff_file),
            "--label",
            "House Rules",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc.returncode == 0, rc.stderr
    return json.loads(out.read_text()) if out.exists() else None


def test_a_checker_finding_on_an_added_line_posts_inline(tmp_path):
    payload = _run_findings(
        tmp_path,
        [
            {
                "path": "contrib/python/x/pyproject.toml",
                "line": 1,
                "body": "declares a [tool.ruff] table; recipes must not",
                "verify_steps": "read line 1",
            }
        ],
    )
    assert payload["comments"][0]["line"] == 1
    assert "tool.ruff" in payload["comments"][0]["body"]


def test_a_checker_finding_off_the_diff_becomes_a_body_note(tmp_path):
    """A missing required file, a folder name, a lockfile source: real, and on
    no added line. Without the trusted-source path these were dropped."""
    payload = _run_findings(
        tmp_path,
        [
            {
                "path": "contrib/python/x/tests/test_runnability.py",
                "line": 1,
                "body": "required file missing: tests/test_runnability.py",
                "verify_steps": "check the file exists",
            }
        ],
    )
    assert payload["comments"] == []
    assert "required file missing" in payload["body"]


def test_a_model_cannot_claim_to_be_the_checker(tmp_path):
    """`source: checker` waives the window check. A model emitting it from a
    prompt-injected diff would waive the check that catches invented source."""
    result = tmp_path / "result.json"
    result.write_text(
        json.dumps(
            {
                "response": json.dumps(
                    [
                        {
                            "path": "contrib/python/x/pyproject.toml",
                            "line": 1,
                            "body": "something on a line that is not in the diff",
                            "verify_steps": "read it",
                            "source": "checker",
                            "window": "   1: this text is nowhere in the diff",
                        }
                    ]
                )
            }
        ),
        encoding="utf-8",
    )
    diff = tmp_path / "diff.txt"
    diff.write_text(_diff_one_added_line(), encoding="utf-8")
    out = tmp_path / "payload.json"
    rc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--result",
            str(result),
            "--diff",
            str(diff),
            "--label",
            "Correctness",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc.returncode == 0, rc.stderr
    assert not out.exists(), (
        "a fabricated window survived because the model claimed to be the "
        "deterministic checker"
    )


def test_result_and_findings_are_mutually_exclusive(tmp_path):
    rc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--result",
            "a.json",
            "--findings",
            "b.json",
            "--diff",
            "d.txt",
            "--label",
            "X",
            "--out",
            "o.json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc.returncode != 0
    assert "not allowed with" in rc.stderr


def test_one_of_them_is_required(tmp_path):
    rc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--diff",
            "d.txt",
            "--label",
            "X",
            "--out",
            "o.json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc.returncode != 0


def test_the_house_rules_workflow_invokes_the_flags_this_script_defines():
    """Same pin as the core workflow's, for the fifth lane's shell block."""
    import yaml

    workflow = (
        Path(__file__).resolve().parents[3]
        / ".github"
        / "workflows"
        / "ai-pr-review-house-rules.yml"
    )
    steps = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"][
        "check"
    ]["steps"]
    build = next(s for s in steps if s.get("id") == "payload")
    invocation = build["run"]
    assert "post_review_comments.py" in invocation
    for flag in ("--findings", "--diff", "--label", "--out", "--repo", "--pr"):
        assert flag in invocation, f"workflow no longer passes {flag}"


# ------------------------------------------- a maintainer's explicit verdict


def _texts(*comments):
    return [(m._tokens(c["body"]), c) for c in comments]


def test_a_verdict_names_itself_but_does_not_widen_suppression():
    """A verdict used to lower the threshold to 0.35. Anyone can react to a
    public comment and a PR author can resolve threads on their own PR, so
    that let the REVIEWED PARTY suppress findings about their own code. The
    verdict now only explains a suppression the ordinary bar already made."""
    body = "the retry loop never terminates once the request is cancelled"
    judged = {"body": body, "verdict": "resolved"}
    why = m.already_raised("p", 1, body, {}, _texts(judged))
    assert why and "resolved" in why

    # Below the ordinary bar, a verdict buys nothing.
    unrelated = "this upload has no timeout and will hang forever"
    assert not m.already_raised("p", 1, unrelated, {}, _texts(judged))


def test_the_verdict_is_named_in_the_reason():
    judged = {
        "body": "the retry loop never terminates once cancelled",
        "verdict": "thumbed this down",
    }
    why = m.already_raised(
        "p",
        1,
        "this retry loop never terminates when cancelled",
        {},
        _texts(judged),
    )
    assert "thumbed this down" in why


def test_an_unrelated_judged_comment_suppresses_nothing():
    judged = {
        "body": "the docstring here says milliseconds",
        "verdict": "resolved",
    }
    assert not m.already_raised(
        "p", 1, "this subprocess call has no timeout", {}, _texts(judged)
    )


def test_verdicts_are_best_effort(monkeypatch):
    """A review is worth having with a noisier duplicate filter; it is not
    worth losing to a GraphQL error."""

    class P:
        returncode = 1
        stderr = "boom"
        stdout = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: P())
    assert m.fetch_verdicts("o/r", 1) == {}


def test_a_thumbs_down_outranks_a_resolution(monkeypatch):
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "isResolved": True,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 1,
                                            "isMinimized": False,
                                            "reactions": {"totalCount": 2},
                                        },
                                        {
                                            "databaseId": 2,
                                            "isMinimized": False,
                                            "reactions": {"totalCount": 0},
                                        },
                                        {
                                            "databaseId": 3,
                                            "isMinimized": True,
                                            "reactions": {"totalCount": 0},
                                        },
                                    ]
                                },
                            }
                        ],
                    }
                }
            }
        }
    }

    class P:
        returncode = 0
        stdout = json.dumps(payload)
        stderr = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: P())
    assert m.fetch_verdicts("o/r", 1) == {
        1: "thumbed this down",
        2: "resolved",
        3: "hid",
    }


# ------------------------------------------------------ mechanical grouping


def _c(path, line, body):
    return {"path": path, "line": line, "side": "RIGHT", "body": body}


def test_three_of_a_kind_become_one_comment():
    comments, dropped = m.group_repeats(
        [
            _c("a.py", 1, "this import of os is never used anywhere below"),
            _c("b.py", 2, "the import of sys is never used anywhere below"),
            _c("c.py", 3, "the import of json is never used anywhere below"),
        ]
    )
    assert len(comments) == 1
    assert "2 other places" in comments[0]["body"]
    assert len(dropped) == 2


def test_two_of_a_kind_stay_two_comments():
    """The rule is three, not two — two instances are cheap to read and the
    second carries a location the first does not."""
    comments, dropped = m.group_repeats(
        [
            _c("a.py", 1, "this import of os is never used anywhere below"),
            _c("b.py", 2, "the import of sys is never used anywhere below"),
        ]
    )
    assert len(comments) == 2 and dropped == []


def test_distinct_findings_are_never_merged():
    comments, _ = m.group_repeats(
        [
            _c("a.py", 1, "this subprocess call has no timeout argument"),
            _c("b.py", 2, "the docstring says milliseconds but the code uses"),
            _c("c.py", 3, "this loop rebinds the variable it iterates over"),
        ]
    )
    assert len(comments) == 3


def test_the_kept_comment_keeps_its_own_anchor():
    """The grouped comment's claim is about the line it sits on; the count is
    context. An anchor moved to a 'representative' line would make the visible
    claim false."""
    comments, _ = m.group_repeats(
        [
            _c("a.py", 11, "this import of os is never used anywhere below"),
            _c("b.py", 22, "the import of sys is never used anywhere below"),
            _c("c.py", 33, "the import of json is never used anywhere below"),
        ]
    )
    assert comments[0]["path"] == "a.py"
    assert comments[0]["line"] == 11
    assert comments[0]["body"].startswith("this import of os")


def test_grouping_does_not_list_the_other_places():
    comments, _ = m.group_repeats(
        [
            _c("a.py", 1, "this import of os is never used anywhere below"),
            _c("b.py", 2, "the import of sys is never used anywhere below"),
            _c("c.py", 3, "the import of json is never used anywhere below"),
        ]
    )
    assert "b.py" not in comments[0]["body"]
    assert "c.py" not in comments[0]["body"]


def test_a_short_body_is_never_grouped():
    """Too few tokens to tell one defect class from another."""
    comments, _ = m.group_repeats(
        [
            _c("a.py", 1, "typo here"),
            _c("b.py", 2, "typo here"),
            _c("c.py", 3, "typo here"),
        ]
    )
    assert len(comments) == 3


# ------------------------------------------- what the reviewer did not see


def test_unreviewed_files_are_named_in_the_review_body():
    """Silence on a file reads as approval. When the diff did not fit, that
    reading is wrong and only the author can tell which files mattered."""
    payload = m.build_payload("Correctness", [], [], ["a.py", "b/c.py"])
    assert "were not looked at" in payload["body"]
    assert "`a.py`" in payload["body"] and "`b/c.py`" in payload["body"]


def test_a_long_unreviewed_list_is_summarised():
    payload = m.build_payload(
        "Correctness", [], [], [f"f{i}.py" for i in range(40)]
    )
    assert "…and 25 more" in payload["body"]
    assert payload["body"].count("- `f") == m.MAX_UNREVIEWED_LISTED


def test_nothing_is_said_when_the_whole_diff_was_reviewed():
    payload = m.build_payload("Correctness", [], [], [])
    assert "not looked at" not in payload["body"]


def test_notes_and_unreviewed_files_coexist():
    payload = m.build_payload(
        "Correctness",
        [],
        [{"path": "x.py", "line": 3, "body": "a note"}],
        ["y.py"],
    )
    assert "a note" in payload["body"]
    assert "`y.py`" in payload["body"]


def test_a_truncated_review_that_found_nothing_still_posts(tmp_path):
    """A lane that found nothing AND saw half the diff is not the same result
    as a lane that found nothing."""
    unreviewed = tmp_path / "unreviewed.txt"
    unreviewed.write_text("a.py\nb.py\n", encoding="utf-8")
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"response": "```json\n[]\n```"}))
    diff = tmp_path / "diff.txt"
    diff.write_text(_diff_one_added_line(), encoding="utf-8")
    out = tmp_path / "payload.json"
    rc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--result",
            str(result),
            "--diff",
            str(diff),
            "--label",
            "Correctness",
            "--unreviewed",
            str(unreviewed),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc.returncode == 0, rc.stderr
    assert out.exists(), "the unreviewed-files warning was never posted"
    assert "`a.py`" in json.loads(out.read_text())["body"]


def test_the_workflow_passes_the_unreviewed_list():
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
    build = next(s for s in steps if s.get("id") == "build_review")
    assert "--unreviewed unreviewed_files.txt" in build["run"]

    # And the step that writes that file must always write it, or the flag
    # points at nothing on the (common) path where everything fits. The
    # packer is what writes it now, and it writes it unconditionally --
    # `--unreviewed-out` truncates the file even when nothing was omitted, so
    # there is no branch on which it fails to exist.
    assemble = "\n".join(str(s.get("run", "")) for s in steps)
    assert "--unreviewed-out unreviewed_files.txt" in assemble, (
        "nothing creates the file that --unreviewed points at"
    )
    # Not behind an `if`: the guarantee is that it always runs, and the step
    # reads the file with `wc -l` immediately afterwards, so a conditional
    # packer means a missing file and a dead step under `set -e`.
    #
    # Measured by nesting depth rather than by reading the line itself. The
    # flag sits on a backslash continuation, which can never begin with
    # `if` — the first version of this assertion looked there and was
    # therefore true however the workflow was written.
    build_step = next(
        s
        for s in steps
        if "--unreviewed-out unreviewed_files.txt" in str(s.get("run", ""))
    )
    depth, depth_at_packer = 0, None
    for line in str(build_step["run"]).splitlines():
        stripped = line.strip()
        if stripped == "if" or stripped.startswith("if "):
            depth += 1
        elif stripped in ("fi", "fi;"):
            depth -= 1
        if "prepare_review_diff.py" in stripped:
            depth_at_packer = depth
    assert depth_at_packer == 0, (
        f"the packer runs at `if` nesting depth {depth_at_packer}; it must "
        "be unconditional or unreviewed_files.txt will not exist"
    )


# --------------------------------------------------- the enforced ceiling

# Deliberately unrelated vocabulary per finding: the grouping pass collapses
# three or more findings that share tokens, which would otherwise hide whether
# the CEILING did anything.
_SUBJECTS = [
    "subprocess call carries no timeout argument",
    "docstring claims milliseconds while seconds are passed",
    "loop rebinds the iteration variable inside itself",
    "regex compiles on every request rather than once",
    "boolean parameter defaults differently from its sibling",
    "exception swallows the original traceback silently",
    "sleep blocks the event loop for two seconds",
    "path joins with a slash instead of pathlib",
    "counter increments after the early return statement",
]


def _many_findings(n):
    # One per line: stacking them all on line 1 makes the within-run
    # duplicate check collapse them, and this fixture is for the CEILING.
    return [
        {
            "path": "contrib/python/x/pyproject.toml",
            "line": i + 1,
            "body": _SUBJECTS[i],
            "verify_steps": f"read line {i + 1}",
        }
        for i in range(n)
    ]


def _diff_n_added_lines(n, path="contrib/python/x/pyproject.toml"):
    body = "".join(f"+line {i + 1}\n" for i in range(n))
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{n} @@\n{body}"
    )


def test_the_budget_is_now_enforced_not_suggested(tmp_path):
    """It used to reach the model as prose and nothing downstream checked it."""
    findings = tmp_path / "f.json"
    findings.write_text(json.dumps(_many_findings(9)), encoding="utf-8")
    diff = tmp_path / "d.txt"
    diff.write_text(_diff_n_added_lines(9), encoding="utf-8")
    out = tmp_path / "p.json"
    rc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--findings",
            str(findings),
            "--diff",
            str(diff),
            "--label",
            "House Rules",
            "--max-comments",
            "3",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc.returncode == 0, rc.stderr
    assert len(json.loads(out.read_text())["comments"]) == 3


def test_the_most_serious_findings_are_the_ones_kept(tmp_path):
    """The model is told to emit most serious first, so the ceiling keeps the
    head of the list rather than an arbitrary slice."""
    findings = tmp_path / "f.json"
    findings.write_text(json.dumps(_many_findings(5)), encoding="utf-8")
    diff = tmp_path / "d.txt"
    diff.write_text(_diff_n_added_lines(5), encoding="utf-8")
    out = tmp_path / "p.json"
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--findings",
            str(findings),
            "--diff",
            str(diff),
            "--label",
            "House Rules",
            "--max-comments",
            "1",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    kept = json.loads(out.read_text())["comments"]
    # These findings carry no `ci`, so House Rules grades them advisory.
    assert kept[0]["body"] == f"[MINOR] {_SUBJECTS[0]}"


def test_no_ceiling_means_no_ceiling(tmp_path):
    findings = tmp_path / "f.json"
    findings.write_text(json.dumps(_many_findings(7)), encoding="utf-8")
    diff = tmp_path / "d.txt"
    diff.write_text(_diff_n_added_lines(7), encoding="utf-8")
    out = tmp_path / "p.json"
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--findings",
            str(findings),
            "--diff",
            str(diff),
            "--label",
            "House Rules",
            "--max-comments",
            "0",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert len(json.loads(out.read_text())["comments"]) == 7


# --------------------------------------------- the marker and the progress line


def test_every_review_carries_the_marker():
    """review_budget.py counts rounds by finding our own reviews. If the
    marker goes missing the round counter restarts at 1 on every push, and the
    author gets a full-size batch forever — the exact bug this all fixes."""
    body = m.build_payload("Correctness", [], [], [])["body"]
    assert body.startswith(m.REVIEW_MARKER)


def test_the_marker_matches_the_one_the_reader_looks_for():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import review_budget

    assert m.REVIEW_MARKER == review_budget.REVIEW_MARKER


def test_the_legacy_header_still_identifies_a_review():
    """PRs already under review when this ships have no marker, and must not
    all restart at round 1 on the same day."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import review_budget

    body = m.build_payload("Correctness", [], [], [])["body"]
    without_marker = body.replace(m.REVIEW_MARKER, "").lstrip()
    assert review_budget.is_ours(
        {"body": without_marker, "user": {"type": "Bot"}}
    )


def test_the_progress_line_is_last_and_set_apart():
    body = m.build_payload(
        "Correctness", [], [], [], "Round 3 · 4 of 25 used."
    )["body"]
    assert body.rstrip().endswith("_Round 3 · 4 of 25 used._")


def test_no_progress_line_when_there_is_nothing_to_say():
    body = m.build_payload("Correctness", [], [], [], "")["body"]
    assert "---" not in body


def test_the_review_records_the_commit_it_reviewed():
    """Without commit_id GitHub stamps the review with head AT POST TIME. A
    review job takes minutes, so a push landing inside that window records a
    commit nothing looked at — and review_budget.py reads exactly that field
    to decide what has already been reviewed, so the next run skips every lane
    and that commit is never reviewed by anyone."""
    payload = m.build_payload("Correctness", [], [], [], "", "abc123")
    assert payload["commit_id"] == "abc123"


def test_no_commit_id_is_sent_when_none_is_known():
    """An empty string would be rejected by the API; omitting the key keeps
    GitHub's own default."""
    assert "commit_id" not in m.build_payload("Correctness", [], [], [], "", "")


def test_both_workflows_pass_the_commit_they_reviewed():
    import yaml

    workflows = Path(__file__).resolve().parents[3] / ".github" / "workflows"
    for name, job, step_id in (
        ("_ai-pr-review-core.yml", "review", "build_review"),
        ("ai-pr-review-house-rules.yml", "check", "payload"),
    ):
        data = yaml.safe_load((workflows / name).read_text(encoding="utf-8"))
        step = next(
            s for s in data["jobs"][job]["steps"] if s.get("id") == step_id
        )
        assert "--commit-id" in step["run"], f"{name} does not pass --commit-id"
        assert "HEAD_SHA" in step["env"], f"{name} does not define HEAD_SHA"


# ------------------------------------------------- notes must converge too


def _body_text(*notes):
    return {
        "kind": "review-body",
        "body": "Automated **House Rules** review — 0 finding(s).\n\n"
        "Also, on lines this PR does not change:\n\n"
        + "\n".join(f"- `x.py:1` — {n}" for n in notes),
    }


def test_a_note_already_said_in_an_earlier_review_is_suppressed():
    """The House Rules lane is never capped and never skipped, and produces
    mostly notes. Review BODIES were not fetched, so nothing could see a note
    it posted last round — the identical body went up on every push forever,
    which is the exact non-convergence this branch exists to end."""
    previous = _body_text(
        "required file missing: tests/test_runnability.py",
        "required file missing: uv.lock",
    )
    # The path the bullet names. Passing a different one used to "pass"
    # because the leg ignored the path entirely — which is the bug that
    # silenced every recipe after the first.
    _zones, texts = m.build_exclusions([previous])
    why = m.already_raised(
        "x.py",
        1,
        "required file missing: tests/test_runnability.py",
        {},
        texts,
        trusted=True,
    )
    assert why and "already" in why


def test_a_new_note_is_not_swallowed_by_an_old_review_body():
    previous = _body_text("required file missing: uv.lock")
    texts = [(m._tokens(previous["body"]), previous)]
    assert not m.already_raised(
        "p", 1, "ownership.team names an organisation, not a team", {}, texts
    )


def test_review_bodies_are_fetched():
    import inspect

    source = inspect.getsource(m.fetch_existing_comments)
    assert "/reviews" in source, (
        "review bodies are not fetched, so notes can never be deduplicated"
    )


def test_the_note_list_is_bounded():
    """GitHub rejects a body over ~65k characters, and the fallback then
    re-posts the same oversized body once per comment, so every retry fails
    too and the contributor gets a red check."""
    notes = [
        {"path": f"f{i}.py", "line": i, "body": "x" * 400} for i in range(200)
    ]
    body = m.build_payload("House Rules", [], notes, [])["body"]
    assert "…and 180 more" in body
    assert len(body) < 20000


def test_a_backtick_in_a_path_cannot_break_out_of_its_code_span():
    """Paths are fork-author-chosen text. A backtick closes the span and lets
    arbitrary markdown into a body the bot signs."""
    body = m.build_payload("Correctness", [], [], ["evil`](http://x)`.py"])[
        "body"
    ]
    assert "`](http" not in body
    assert "evil](http://x).py" in body


def test_a_newline_in_a_path_cannot_forge_a_list_item():
    body = m.build_payload("Correctness", [], [], ["a.py\n- `fake finding`"])[
        "body"
    ]
    assert body.count("- `") == 1


def test_only_our_own_review_bodies_are_used_for_containment():
    """A review body is large, so containment against an arbitrary one is easy
    to satisfy. Reading everyone's bodies let a PR author paste a wall of
    plausible text into a review of their own PR and suppress most of what the
    next round would say — the hole the 0.35 verdict threshold was removed
    for, rebuilt wider."""
    ours = {
        "body": f"{m.REVIEW_MARKER}\nAutomated **House Rules** review — 1.",
        "user": {"type": "Bot"},
    }
    theirs = {
        "body": "I think the timeout here is fine, and the retry loop too, "
        "and the import ordering, and the missing test file.",
        "user": {"type": "User"},
    }
    bot_but_not_ours = {
        "body": "Dependabot could not update this dependency.",
        "user": {"type": "Bot"},
    }
    assert m._our_review(ours)
    assert not m._our_review(theirs)
    assert not m._our_review(bot_but_not_ours)
    assert not m._our_review({"body": "   ", "user": {"type": "Bot"}})


def test_a_hostile_self_review_cannot_suppress_the_next_round(monkeypatch):
    """Through fetch_existing_comments and already_raised, NOT through the
    helper. The previous version of this test called `_our_review` directly
    and stayed green while the helper was orphaned and the hole wide open —
    which is how the same defect survived three rounds of review."""
    hostile = {
        "id": 1,
        "user": {"type": "User", "login": "pr-author"},
        "body": "required file missing tests test_runnability py uv lock "
        "pyproject toml env example ownership team names an organisation",
    }
    pages = {"comments": "[]", "reviews": json.dumps([hostile])}

    def fake_run(cmd, **kwargs):
        path = cmd[3].split("?")[0]

        class P:
            returncode = 0
            stdout = pages[path.rsplit("/", 1)[-1]]
            stderr = ""

        return P()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr(m, "fetch_verdicts", lambda repo, pr: {})

    existing = m.fetch_existing_comments("o/r", 1)
    assert existing == [], "a stranger's review body reached the exclusions"

    zones, texts = m.build_exclusions(existing)
    assert not m.already_raised(
        "x.py",
        1,
        "required file missing: tests/test_runnability.py",
        zones,
        texts,
    )


def test_our_own_review_body_still_suppresses_its_own_repeat(monkeypatch):
    ours = {
        "id": 2,
        "user": {"type": "Bot", "login": "adk-bot[bot]"},
        "body": f"{m.REVIEW_MARKER}\nAutomated **House Rules** review — 1.\n\n"
        "- `a/b.py:1` — required file missing: tests/test_runnability.py",
    }
    pages = {"comments": "[]", "reviews": json.dumps([ours])}

    def fake_run(cmd, **kwargs):
        path = cmd[3].split("?")[0]

        class P:
            returncode = 0
            stdout = pages[path.rsplit("/", 1)[-1]]
            stderr = ""

        return P()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr(m, "fetch_verdicts", lambda repo, pr: {})

    zones, texts = m.build_exclusions(m.fetch_existing_comments("o/r", 1))
    # The SAME path the earlier review named. This test used to pass "x.py"
    # against a note about "a/b.py" and so encoded the path-blindness that
    # silenced every recipe after the first.
    # trusted=True: the checker's bodies are one format string per rule, so
    # they are byte-identical across recipes and the path is what separates
    # them. A model finding keeps the path-blind behaviour, which is how the
    # four overlapping lanes avoid saying one thing four ways.
    assert m.already_raised(
        "a/b.py",
        1,
        "required file missing: tests/test_runnability.py",
        zones,
        texts,
        trusted=True,
    )
    assert not m.already_raised(
        "other/c.py",
        1,
        "required file missing: tests/test_runnability.py",
        zones,
        texts,
        trusted=True,
    ), "a note about one file suppressed the same finding about another"


def test_a_grouped_body_never_exceeds_the_shape_cap():
    """Grouping is the one path that makes a body LONGER, and the shape gate
    runs before it. A 600-char body plus the note is 643, over the cap that
    keeps this public channel narrow — and GitHub rejects the whole review for
    it. Too long to annotate means keep it ungrouped, never drop it."""
    # Real words: a long run of one character trips the unbroken-run rule
    # instead, which would make this test pass for the wrong reason.
    prefix = "the retry loop never terminates when cancelled "
    filler = "and the socket stays open until the process exits. "
    long_body = (prefix + filler * 20)[: m.MAX_BODY_CHARS]
    assert len(long_body) == m.MAX_BODY_CHARS
    assert not m.implausible_body(long_body), "the fixture is not a valid body"
    comments = [
        {"path": f"f{i}.py", "line": 1, "side": "RIGHT", "body": long_body}
        for i in range(3)
    ]
    kept, dropped = m.group_repeats(comments)
    assert len(kept) == 3, "the group note pushed a body over the cap"
    for c in kept:
        assert not m.implausible_body(c["body"]), m.implausible_body(c["body"])
    assert dropped == []


def test_a_short_group_still_gets_its_note():
    """The cap guard must not disable grouping for ordinary bodies."""
    body = "this import of os is never used anywhere below"
    comments = [
        {"path": f"f{i}.py", "line": 1, "side": "RIGHT", "body": body}
        for i in range(3)
    ]
    kept, dropped = m.group_repeats(comments)
    assert len(kept) == 1 and "2 other places" in kept[0]["body"]
    assert len(dropped) == 2


def test_a_group_that_cannot_be_annotated_loses_no_comment():
    """When the note will not fit, the members must stay as separate comments.
    `used` was marked before the bail-out, so the other members were flagged
    as consumed while nothing had consumed them: the outer loop skipped them
    and they vanished from the review with no log line. Three findings went in
    and one came out."""
    prefix = "the retry loop never terminates when cancelled "
    filler = "and the socket stays open until the process exits. "
    body = (prefix + filler * 20)[: m.MAX_BODY_CHARS]
    comments = [
        {"path": f"f{i}.py", "line": 1, "side": "RIGHT", "body": body}
        for i in range(3)
    ]
    kept, dropped = m.group_repeats(comments)
    assert len(kept) + len(dropped) == 3, "a comment disappeared entirely"
    assert len(kept) == 3


def test_no_comment_is_ever_lost_by_grouping():
    """The invariant, over a mixed set: everything is either kept or recorded
    as grouped-into. Nothing may simply vanish."""
    bodies = [
        "this import of os is never used anywhere below",
        "the import of sys is never used anywhere below",
        "import json is never used anywhere in this module",
        "this subprocess call has no timeout argument at all",
        "the docstring claims milliseconds but seconds are passed",
    ]
    comments = [
        {"path": f"f{i}.py", "line": i + 1, "side": "RIGHT", "body": b}
        for i, b in enumerate(bodies)
    ]
    kept, dropped = m.group_repeats(comments)
    assert len(kept) + len(dropped) == len(comments)


def test_grouping_never_reaches_across_recipes():
    """The deterministic lane builds each rule's body from one format string,
    so two recipes' findings for one rule are near-identical by construction
    and always cleared the threshold. Three recipes with a deprecated model id
    produced ONE comment on the first of them; the other two authors were told
    nothing about their own recipe."""
    body = "deprecated model id (use gemini-3.5-flash); 1 occurrence(s)"
    comments = [
        {
            "path": f"core/python/{n}/agent.py",
            "line": 1,
            "side": "RIGHT",
            "body": body,
        }
        for n in ("alpha", "beta", "gamma")
    ]
    kept, dropped = m.group_repeats(comments)
    assert len(kept) == 3, "one recipe's author was told and two were not"
    assert dropped == []


def test_grouping_still_works_within_one_recipe():
    body = "this import of os is never used anywhere below"
    comments = [
        {
            "path": f"core/python/alpha/{n}.py",
            "line": 1,
            "side": "RIGHT",
            "body": body,
        }
        for n in ("a", "b", "c")
    ]
    kept, _ = m.group_repeats(comments)
    assert len(kept) == 1


def test_a_note_body_cannot_forge_markdown_in_the_review():
    """The path beside it is sanitised; the body is the wider channel — 600
    characters of model text derived from a fork-authored diff. A newline plus
    --- renders a horizontal rule, an inline image fires a remote request when
    the page renders, and an italic line forges a second progress footer."""
    note = {
        "path": "a.py",
        "line": 1,
        "body": "Looks fine.\n\n---\n\n![](https://attacker.example/p.png)\n\n"
        "_Round 1 - 0 of this PR's 25 automated comments used._",
    }
    body = m.build_payload("Correctness", [], [note], [])["body"]
    bullet = next(ln for ln in body.split("\n") if ln.startswith("- `a.py"))
    assert "\n" not in bullet
    assert "![](" not in bullet
    # A `---` only renders as a horizontal rule on a line of its own. The
    # flattening is what prevents that; inline it is literal text.
    assert "---" not in [ln.strip() for ln in body.split("\n")]


def test_the_same_finding_twice_in_one_run_is_posted_once():
    """Everything else compares against comments already ON the PR, so the
    system suppressed a near-duplicate from a previous round two lines away
    and cheerfully posted an exact duplicate on the same line within one
    run."""
    diff = _diff_n_added_lines(3)
    anchors, line_text = m.walk_right_side(diff)
    path = "contrib/python/x/pyproject.toml"
    body = "this subprocess call has no timeout argument at all"
    findings = [
        {"path": path, "line": 1, "body": body, "verify_steps": "read it"},
        {"path": path, "line": 1, "body": body, "verify_steps": "read it"},
    ]
    comments, _notes, skipped = m.build_comments(findings, anchors, line_text)
    assert len(comments) == 1
    assert any("already said in this review" in s for s in skipped)


def test_two_distinct_defects_near_each_other_are_both_posted():
    """The within-run check must not inherit the ±2 proximity zone: inside one
    run, two different defects a line apart are both worth saying."""
    diff = _diff_n_added_lines(3)
    anchors, line_text = m.walk_right_side(diff)
    path = "contrib/python/x/pyproject.toml"
    findings = [
        {
            "path": path,
            "line": 1,
            "verify_steps": "read it",
            "body": "this subprocess call has no timeout argument at all",
        },
        {
            "path": path,
            "line": 2,
            "verify_steps": "read it",
            "body": "the docstring claims milliseconds but seconds are passed",
        },
    ]
    comments, _notes, _skipped = m.build_comments(findings, anchors, line_text)
    assert len(comments) == 2


def test_the_within_run_check_never_reaches_across_files():
    """`already_raised`'s similarity leg ignores the path, which is right for
    "did we say this on the PR before" and catastrophic within one run: the
    deterministic lane builds each rule's body from one format string, so two
    recipes' findings for one rule are byte-identical. Reusing it dropped 13
    of 21 findings on a three-recipe PR and told two of the three authors
    nothing about their own recipe."""
    body = "deprecated model id (use gemini-3.5-flash); 1 occurrence(s)"
    paths = [f"core/python/{n}/agent.py" for n in ("alpha", "beta", "gamma")]
    diff = "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -0,0 +1,2 @@\n"
        "+import os\n+x = 1\n"
        for p in paths
    )
    anchors, line_text = m.walk_right_side(diff)
    findings = [
        {"path": p, "line": 1, "body": body, "verify_steps": "read it"}
        for p in paths
    ]
    comments, _notes, _skipped = m.build_comments(findings, anchors, line_text)
    assert len(comments) == 3, "a recipe's author was silently told nothing"


def test_the_within_run_check_still_catches_a_repeat_in_one_file():
    """Exact repeats only, on the same line. The same body on a DIFFERENT
    line is a second instance of the defect, and the repo's rule is that two
    instances stay two comments — a similarity leg here dropped genuinely
    distinct findings (two stub values in one .env.example score 0.84) with
    no note saying anything had been dropped."""
    diff = _diff_n_added_lines(5)
    anchors, line_text = m.walk_right_side(diff)
    path = "contrib/python/x/pyproject.toml"
    body = "this subprocess call has no timeout argument at all"
    findings = [
        {"path": path, "line": 1, "body": body, "verify_steps": "read it"},
        {"path": path, "line": 1, "body": body, "verify_steps": "read it"},
        {"path": path, "line": 4, "body": body, "verify_steps": "read it"},
    ]
    comments, _notes, skipped = m.build_comments(findings, anchors, line_text)
    assert len(comments) == 2, "the second LINE is a second instance"
    assert any("already said in this review" in s for s in skipped)


@pytest.mark.parametrize(
    "attack",
    [
        "![](https://evil.example/pixel.png)",
        '<img src="https://evil.example/pixel.png">',
    ],
)
def test_neither_image_spelling_survives_into_the_review_body(attack):
    """An image is a remote request fired by rendering the page. `<img>` is in
    GitHub's markdown sanitiser allowlist, so closing only the `![]()` form
    left the same request one tag away."""
    note = {"path": "a.py", "line": 1, "body": f"see {attack} here"}
    body = m.build_payload("Correctness", [], [note], [])["body"]
    assert "![](" not in body
    assert "<img" not in body


@pytest.mark.parametrize(
    "attack",
    [
        "![](https://evil.example/pixel.png)",
        '<img src="https://evil.example/pixel.png">',
    ],
)
def test_an_inline_comment_cannot_carry_an_image_either(attack):
    """Inline bodies went out with nothing applied to them at all."""
    diff = _diff_n_added_lines(3)
    anchors, line_text = m.walk_right_side(diff)
    findings = [
        {
            "path": "contrib/python/x/pyproject.toml",
            "line": 1,
            "body": f"the timeout here looks short {attack}",
            "verify_steps": "read it",
        }
    ]
    comments, _notes, _skipped = m.build_comments(findings, anchors, line_text)
    assert comments, "the finding was dropped instead of cleaned"
    assert "![](" not in comments[0]["body"]
    assert "<img" not in comments[0]["body"]


def test_a_superscript_digit_line_number_does_not_crash():
    """ "²".isdigit() is True and int("²") raises, which escapes as a CI fault."""
    assert m._coerce_line("²") is None
    assert m._coerce_line("42") == 42


# ------------------------------------------- int() has a 4300-digit ceiling


def test_an_absurdly_long_line_number_is_rejected_not_fatal():
    """Python 3.11 caps int(str) at 4300 digits and raises ValueError past it.
    "9"*5000 is `isdecimal()`, so the guard let it through to int() — which
    escaped as a CI fault and discarded every finding in the lane."""
    assert m._coerce_line("9" * 5000) is None
    assert m._coerce_line("42") == 42


def test_an_absurdly_long_window_line_number_is_not_fatal():
    rows = m._window_rows("  " + "9" * 5000 + ": import os")
    assert rows == [] or all(isinstance(n, int) for n, _ in rows)


def test_a_giant_integer_literal_in_the_response_is_handled():
    """json's own number parser raises a bare ValueError, not
    JSONDecodeError, so it slipped past the handler before any validation."""
    block = (
        '```json\n[{"path": "a.py", "line": '
        + "9" * 5000
        + ', "body": "x"}]\n```'
    )
    try:
        m.extract_findings(block)
    except m.ReviewerOutputError:
        pass  # a reported, handled failure is the correct outcome
    except Exception as exc:
        raise AssertionError(f"escaped as {type(exc).__name__}") from exc


@pytest.mark.parametrize(
    "spelling",
    ['<IMG SRC="https://evil/p.png">', "<Img src=x>", "<iMG src=x>"],
)
def test_the_image_defang_is_case_insensitive(spelling):
    """HTML tag names are case-insensitive — the parser lowercases the node
    before GitHub's sanitiser allowlist is consulted — so <IMG SRC=...> is the
    same element and rendered the same remote request."""
    assert "<img" not in m._defang_images(spelling).lower()


def test_two_distinct_rules_on_one_line_are_both_reported():
    """Many house rules fall back to line 1 when they cannot locate their
    subject, so distinct CI-failing rules collide there routinely. Blocking on
    position alone told the author about one of four stub-README findings, and
    the posted one then occupied the proximity zone so the other three were
    never told on any later round."""
    diff = _diff_n_added_lines(3, path="contrib/python/x/README.md")
    anchors, line_text = m.walk_right_side(diff)
    findings = [
        {
            "path": "contrib/python/x/README.md",
            "line": 1,
            "source": "checker",
            "verify_steps": "read it",
            "body": "README is 40 words, minimum is 100",
        },
        {
            "path": "contrib/python/x/README.md",
            "line": 1,
            "source": "checker",
            "verify_steps": "read it",
            "body": "README has no setup or prerequisites heading",
        },
        {
            "path": "contrib/python/x/README.md",
            "line": 1,
            "source": "checker",
            "verify_steps": "read it",
            "body": "README has no fenced code block anywhere in it",
        },
    ]
    comments, _notes, _skipped = m.build_comments(findings, anchors, line_text)
    assert len(comments) == 3, "distinct rules were collapsed by their anchor"


def test_a_checker_finding_is_not_blocked_by_an_unrelated_comment_on_its_line():
    """Across rounds, too: one comment on line 1 silenced every other rule
    that anchors there, permanently."""
    existing = [
        {
            "kind": "inline",
            "path": "contrib/python/x/README.md",
            "line": 1,
            "body": "README is 40 words, minimum is 100",
        }
    ]
    zones, texts = m.build_exclusions(existing)
    assert not m.already_raised(
        "contrib/python/x/README.md",
        1,
        "README has no setup or prerequisites heading",
        zones,
        texts,
        trusted=True,
    )
    # A model finding keeps the positional rule: two comments on one line are
    # usually the same observation restated.
    assert m.already_raised(
        "contrib/python/x/README.md",
        1,
        "something else entirely here now",
        zones,
        texts,
        trusted=False,
    )


# --------------------------- short bodies: below the similarity token floor

SHORT_BODIES = [
    '"api_key" is not UPPER_SNAKE_CASE (extract_env_vars.py:444)',  # H13
    "a committed private key (.gitignore)",  # H43
]


@pytest.mark.parametrize("body", SHORT_BODIES)
def test_a_short_finding_is_not_repeated_on_every_push(body):
    """These tokenise to three distinctive words, and the similarity check
    returns early below four — so nothing suppressed them in either leg. The
    House Rules lane is exempt from the budget and never hits the same-commit
    guard, so it re-posted them on every push forever: the precise
    non-convergence this branch exists to end. Both are real checker bodies,
    and H43's are the security-relevant ones."""
    assert len(m._tokens(body)) < 4, "fixture no longer exercises the floor"
    existing = [{"kind": "inline", "path": "a/b.py", "line": 1, "body": body}]
    zones, texts = m.build_exclusions(existing)
    assert m.already_raised("a/b.py", 1, body, zones, texts, trusted=True)


@pytest.mark.parametrize("body", SHORT_BODIES)
def test_a_short_finding_is_not_posted_twice_in_one_run(body):
    """The exact-match leg sat BELOW the same token floor, so it was
    unreachable for exactly the bodies the removed line-zone leg covered, and
    two of them on one line both went out."""
    diff = _diff_n_added_lines(3, path="a/b.py")
    anchors, line_text = m.walk_right_side(diff)
    findings = [
        {
            "path": "a/b.py",
            "line": 1,
            "body": body,
            "source": "checker",
            "verify_steps": "read it",
            "window": "",
        },
        {
            "path": "a/b.py",
            "line": 1,
            "body": body,
            "source": "checker",
            "verify_steps": "read it",
            "window": " ",
        },
    ]
    comments, _notes, _skipped = m.build_comments(findings, anchors, line_text)
    assert len(comments) == 1


def test_distinct_short_findings_on_one_line_both_survive():
    """The floor fix must not reinstate the collision it replaced."""
    diff = _diff_n_added_lines(3, path="a/b.py")
    anchors, line_text = m.walk_right_side(diff)
    findings = [
        {
            "path": "a/b.py",
            "line": 1,
            "source": "checker",
            "verify_steps": "read it",
            "body": SHORT_BODIES[0],
        },
        {
            "path": "a/b.py",
            "line": 1,
            "source": "checker",
            "verify_steps": "read it",
            "body": SHORT_BODIES[1],
        },
    ]
    comments, _notes, _skipped = m.build_comments(findings, anchors, line_text)
    assert len(comments) == 2


@pytest.mark.parametrize(
    "block",
    [
        '```json\n[{"line": ' + "9" * 5000 + "}]\n```",
        '```json\n[{"body":"x","path":"a.py","line":' + "9" * 5000 + "}]\n```",
        '```json\n[{"path":"a.py","line":' + "9" * 5000 + '},{"path":"b.py",'
        '"line":3,"body":"y"}]\n```',
    ],
)
def test_every_decode_site_survives_a_giant_integer(block):
    """Three sites decode JSON; round 7 widened two. The third is the
    fallback the other two hand off to, so the shapes that reach salvage
    still escaped as a CI fault."""
    try:
        m.extract_findings(block)
    except m.ReviewerOutputError:
        pass
    except Exception as exc:
        raise AssertionError(f"escaped as {type(exc).__name__}") from exc


def test_a_hunk_header_with_absurd_counts_does_not_crash():
    diff = f"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,{'9' * 5000} +1,2 @@\n+x\n"
    anchors, _text = m.walk_right_side(diff)
    assert isinstance(anchors, dict)


def test_a_short_note_is_not_repeated_on_every_push():
    """A note lives as one bullet inside a much larger review body, so the
    exact-equality leg never matched it and the containment leg sat below the
    four-token floor. The House Rules lane is exempt from the budget and
    produces mostly notes, so its short findings — `"api_key" is not
    UPPER_SNAKE_CASE`, `a committed private key` — went out on every push
    forever, which is the non-convergence this branch exists to end."""
    for short in SHORT_BODIES:
        assert len(m._tokens(short)) < 4, (
            "fixture no longer exercises the floor"
        )
        previous = {
            "kind": "review-body",
            "body": f"{m.REVIEW_MARKER}\nAutomated **House Rules** review — 0 "
            f"finding(s).\n\nAlso, on lines this PR does not change:\n\n"
            f"- `a/b.py:2` — {short}",
        }
        zones, texts = m.build_exclusions([previous])
        assert m.already_raised(
            "a/b.py", 2, short, zones, texts, trusted=True
        ), f"{short!r} would be re-posted on the next push"


def test_an_unrelated_short_note_is_not_suppressed():
    previous = {
        "kind": "review-body",
        "body": f"{m.REVIEW_MARKER}\nAutomated **House Rules** review.\n\n"
        "- `a/b.py:2` — a committed private key (.gitignore)",
    }
    zones, texts = m.build_exclusions([previous])
    assert not m.already_raised(
        "a/b.py",
        2,
        '"api_key" is not UPPER_SNAKE_CASE',
        zones,
        texts,
        trusted=True,
    )


def test_a_short_body_with_image_markup_is_still_deduplicated():
    """The comparison legs saw the raw body while what is stored and posted is
    the defanged one, so a short body carrying image markup matched neither."""
    diff = _diff_n_added_lines(3, path="a/b.py")
    anchors, line_text = m.walk_right_side(diff)
    body = "a remote <img> in the README"
    findings = [
        {
            "path": "a/b.py",
            "line": 1,
            "body": body,
            "source": "checker",
            "verify_steps": "read it",
            "window": "",
        },
        {
            "path": "a/b.py",
            "line": 1,
            "body": body,
            "source": "checker",
            "verify_steps": "read it",
            "window": " ",
        },
    ]
    comments, _notes, _skipped = m.build_comments(findings, anchors, line_text)
    assert len(comments) == 1
    assert "<img" not in comments[0]["body"]


def test_a_short_body_about_another_file_is_not_suppressed():
    """`a committed private key` is byte-identical for every recipe, so an
    equality leg that ignores the path tells one author and silences the
    rest — the shape _group_scope and _said_in_this_run both exist to stop."""
    existing = [
        {
            "kind": "inline",
            "path": "core/python/alpha/x.pem",
            "line": 1,
            "body": "a committed private key (.gitignore)",
        }
    ]
    zones, texts = m.build_exclusions(existing)
    assert not m.already_raised(
        "core/python/beta/y.pem",
        1,
        "a committed private key (.gitignore)",
        zones,
        texts,
        trusted=True,
    )


@pytest.mark.parametrize(
    "body",
    [
        "Unused import.  Remove it.",  # two spaces
        "Unused import.\nRemove it.",  # a newline
        "a committed private key (.gitignore)",
    ],
)
def test_a_note_is_compared_as_it_was_written(body):
    """A note is rendered through `_safe_line`, which flattens whitespace. The
    comparison used the RAW body, so anything with a newline or a double
    space never matched itself and repeated on every push — notes being the
    channel with no cap and no counter, so one occurrence is unbounded."""
    rendered = m.build_payload(
        "House Rules", [], [{"path": "a/b.py", "line": 2, "body": body}], []
    )["body"]
    previous = {"kind": "review-body", "body": rendered}
    zones, texts = m.build_exclusions([previous])
    assert m.already_raised("a/b.py", 2, body, zones, texts, trusted=True), (
        f"{body!r} would be posted again next push"
    )


def test_a_note_about_one_recipe_does_not_silence_another():
    """`a committed private key` is byte-identical for every recipe, and it is
    CI-failing. Both review-body legs ignored the path, so recipe beta's
    author was told nothing once alpha had been told."""
    short = "a committed private key (.gitignore)"
    long_body = "required file missing: tests/test_runnability.py"
    for body in (short, long_body):
        rendered = m.build_payload(
            "House Rules",
            [],
            [{"path": "core/python/alpha/x.py", "line": 1, "body": body}],
            [],
        )["body"]
        zones, texts = m.build_exclusions(
            [{"kind": "review-body", "body": rendered}]
        )
        assert m.already_raised(
            "core/python/alpha/x.py", 1, body, zones, texts, trusted=True
        )
        assert not m.already_raised(
            "core/python/beta/x.py", 1, body, zones, texts, trusted=True
        ), f"beta's author was silenced by alpha's note: {body!r}"


def test_a_note_is_matched_bullet_by_bullet_not_by_substring():
    """The path and the text used to be tested as two independent substrings
    of the whole review body, so they could be satisfied by two DIFFERENT
    bullets: a body naming alpha's path and (separately) beta's finding text
    suppressed alpha's genuinely new finding. Review bodies are now parsed
    back into individual notes, which removes the whole class."""
    previous = {
        "kind": "review-body",
        "body": f"{m.REVIEW_MARKER}\nAutomated **House Rules** review.\n\n"
        "Also, on lines this PR does not change:\n\n"
        '- `alpha/.env.example:2` — "api_key" is not UPPER_SNAKE_CASE\n'
        "- `beta/.env.example:5` — placeholder should be the exact string",
    }
    _zones, texts = m.build_exclusions([previous])
    # alpha's NEW finding, whose text belongs to beta's bullet.
    assert not m.already_raised(
        "alpha/.env.example",
        9,
        "placeholder should be the exact string",
        {},
        texts,
        trusted=True,
    ), "two different bullets combined to suppress a new finding"
    # Each bullet still suppresses its own repeat.
    assert m.already_raised(
        "alpha/.env.example",
        2,
        '"api_key" is not UPPER_SNAKE_CASE',
        {},
        texts,
        trusted=True,
    )


def test_notes_are_parsed_back_out_of_a_review_body():
    body = (
        f"{m.REVIEW_MARKER}\nAutomated **House Rules** review — 0 finding(s).\n"
        "\nAlso, on lines this PR does not change:\n\n"
        "- `a/b.py:12` — required file missing: uv.lock\n"
        "- `c/d.py:1` — a committed private key\n"
        "\n---\n\n_Round 2 · 4 of 25 used._"
    )
    notes = m._notes_in(body)
    assert [(n["path"], n["line"]) for n in notes] == [
        ("a/b.py", 12),
        ("c/d.py", 1),
    ]
    assert notes[0]["body"] == "required file missing: uv.lock"


def test_a_checker_finding_is_not_silenced_by_the_same_text_elsewhere():
    """The checker's bodies come from one format string per rule, so
    `[build-system] missing or lacks requires / build-backend` is identical
    for every recipe and CI-failing. Path-blind, the first author was told
    and every one after them silenced."""
    body = "[build-system] missing or lacks requires / build-backend"
    existing = [
        {
            "kind": "inline",
            "path": "core/python/alpha/pyproject.toml",
            "line": 1,
            "body": body,
        }
    ]
    zones, texts = m.build_exclusions(existing)
    assert not m.already_raised(
        "core/python/beta/pyproject.toml", 1, body, zones, texts, trusted=True
    )
    assert m.already_raised(
        "core/python/alpha/pyproject.toml", 1, body, zones, texts, trusted=True
    )


def test_a_model_finding_keeps_cross_file_suppression():
    """The opposite is wanted for the four overlapping model lanes: they
    phrase one defect four ways, and the second phrasing should not be
    posted just because it is about a different file."""
    existing = [
        {
            "kind": "inline",
            "path": "other.py",
            "line": 99,
            "body": "filename is interpolated into os.system unsanitised",
        }
    ]
    zones, texts = m.build_exclusions(existing)
    assert m.already_raised(
        "x.py",
        3,
        "unsanitised filename interpolated into os.system",
        zones,
        texts,
        trusted=False,
    )


def test_a_grouped_comment_suppresses_its_own_repeat_next_round():
    """grouping runs AFTER the duplicate check and appends five distinctive
    tokens, so the stored body was not the compared body: a short one fell
    under the bar against its own grouped form and went out every push."""
    # Enough distinctive tokens to reach the Jaccard leg: the short-body
    # branch has its own stripping, so a two-token fixture tests the wrong
    # one. Five tokens against the note's five is 0.5, under SIMILARITY.
    base = "the subprocess timeout retry socket handler is never configured"
    stored = f"{base}\n\n(Same thing in 2 other places in this review.)"
    zones, texts = m.build_exclusions(
        [{"kind": "inline", "path": "a.py", "line": 1, "body": stored}]
    )
    assert m.already_raised("a.py", 1, base, zones, texts, trusted=True)


def _round_trip(findings, existing, diff):
    """One review round: what gets posted, given what is already on the PR."""
    anchors, line_text = m.walk_right_side(diff)
    comments, notes, _skipped = m.build_comments(
        findings, anchors, line_text, existing
    )
    return comments, notes


def test_a_grouped_checker_class_does_not_drip_one_comment_per_push():
    """group_repeats drops the other members with no record of them, and the
    path-aware suppression a checker gets cannot recognise them next round —
    so round 2 posted exactly the places round 1 claimed it had covered, and
    the class dripped one comment per push for N-1 pushes, each round
    re-claiming "same thing in N other places"."""
    paths = [f"core/python/alpha/{n}" for n in ("a.tsx", "b.js", "c.ts")]
    diff = "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -0,0 +1,2 @@\n+x\n+y\n"
        for p in paths
    )
    body = "no licence header on this file; 3 files have none while 9 carry it"
    findings = [
        {
            "path": p,
            "line": 1,
            "body": body,
            "source": "checker",
            "verify_steps": "read it",
        }
        for p in paths
    ]
    posted, _notes = _round_trip(findings, [], diff)
    assert len(posted) == 3, "the class was collapsed across files"

    # Next push, nothing new: every one is recognised and nothing re-posts.
    existing = [
        {
            "kind": "inline",
            "path": c["path"],
            "line": c["line"],
            "body": c["body"],
        }
        for c in posted
    ]
    again, _notes = _round_trip(findings, existing, diff)
    assert again == [], f"round 2 re-posted {[c['path'] for c in again]}"


def test_a_model_class_still_groups_across_files_in_one_recipe():
    """Grouping exists to stop one defect spending N of a bounded budget, and
    the model lanes are the ones with the budget."""
    paths = [f"core/python/alpha/{n}.py" for n in ("a", "b", "c")]
    diff = "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -0,0 +1,2 @@\n+x\n+y\n"
        for p in paths
    )
    body = "this import of os is never used anywhere below"
    findings = [
        {"path": p, "line": 1, "body": body, "verify_steps": "read it"}
        for p in paths
    ]
    posted, _notes = _round_trip(findings, [], diff)
    assert len(posted) == 1
    assert "2 other places" in posted[0]["body"]


def test_the_internal_trusted_flag_never_reaches_github():
    """Through build_comments, which is what adds the key. Asserting on
    build_payload alone proves nothing: it never sees the flag, so the test
    passed with the stripping removed."""
    diff = _diff_n_added_lines(3, path="a.py")
    anchors, line_text = m.walk_right_side(diff)
    comments, _notes, _skipped = m.build_comments(
        [
            {
                "path": "a.py",
                "line": 1,
                "source": "checker",
                "verify_steps": "read it",
                "body": "required file missing: uv.lock",
            }
        ],
        anchors,
        line_text,
    )
    assert comments, "nothing was posted, so the assertion below is vacuous"
    assert all("trusted" not in c for c in comments), (
        "an internal flag would be sent to GitHub as a comment field"
    )


@pytest.mark.parametrize(
    "path",
    [
        "core/python/alpha/we`ird.py",
        "core/python/alpha/" + "d" * 200 + ".py",
    ],
)
def test_a_path_the_renderer_rewrites_still_matches_its_own_note(path):
    """A note's path is recovered from the bullet we wrote, which went through
    _safe_span — so a path carrying a backtick, or past the 160-char cap,
    never matched itself and repeated on every push."""
    body = "required file missing: uv.lock"
    rendered = m.build_payload(
        "House Rules", [], [{"path": path, "line": 1, "body": body}], []
    )["body"]
    _zones, texts = m.build_exclusions(
        [{"kind": "review-body", "body": rendered}]
    )
    assert m.already_raised(path, 1, body, {}, texts, trusted=True), (
        f"{path!r} would be posted again next push"
    )


@pytest.mark.parametrize(
    "body",
    ["committed  private  key", "committed\nprivate key", "a short one here"],
)
def test_a_whitespace_lossy_inline_body_matches_its_own_comment(body):
    """Notes are stored flattened and inline comments are not; comparing only
    the flattened spelling fixed one and broke the other."""
    existing = [{"kind": "inline", "path": "a.py", "line": 1, "body": body}]
    zones, texts = m.build_exclusions(existing)
    assert m.already_raised("a.py", 1, body, zones, texts, trusted=True)


def test_a_bullet_with_an_absurd_line_number_is_not_fatal():
    body = f"- `a.py:{'9' * 5000}` — something"
    assert m._notes_in(body) == []


def test_a_top_level_comment_cannot_silence_a_checker_rule():
    """An issue comment carries no path, so the same-file guard was skipped
    for the one comment class any user can post. The checker's messages are
    format strings in a public file, so one comment quoting one — verbatim or
    paraphrased — silenced that rule on every file, on every later push."""
    body = "required file missing: tests/test_runnability.py"
    hostile = [
        {"kind": "top-level", "path": None, "line": None, "body": body},
        {
            "kind": "top-level",
            "path": None,
            "line": None,
            "body": "the missing required file tests test_runnability is fine",
        },
    ]
    zones, texts = m.build_exclusions(hostile)
    for recipe_path in ("core/python/alpha/x.py", "core/python/beta/x.py"):
        assert not m.already_raised(
            recipe_path, 1, body, zones, texts, trusted=True
        ), "a top-level comment silenced the deterministic lane"
    # A model finding still defers to it: that is what the leg is for.
    assert m.already_raised(
        "core/python/alpha/x.py", 1, body, zones, texts, trusted=False
    )


def test_two_findings_worded_alike_are_both_reported():
    """Two H39 stub values in one .env.example differ only in the quoted
    value and score 0.77. The within-run similarity leg dropped the second
    silently — no group note, nothing telling the author it existed."""
    path = "contrib/python/x/.env.example"
    diff = _diff_n_added_lines(3, path=path)
    anchors, line_text = m.walk_right_side(diff)
    # The checker's REAL wording. The two bodies differ only in the quoted
    # value and score 0.769 — above the bar the removed leg used. A
    # paraphrase of them scores 0.5 and so cannot fail.
    stub = (
        " is a stub committed as if it were a real value. Someone copying "
        "this file has no way to tell it needs replacing; use "
        "<TODO: update-this-value>"
    )
    findings = [
        {
            "path": path,
            "line": 1,
            "source": "checker",
            "verify_steps": "read",
            "body": '"my-project-id"' + stub,
        },
        {
            "path": path,
            "line": 2,
            "source": "checker",
            "verify_steps": "read",
            "body": '"us-central1-placeholder"' + stub,
        },
    ]
    comments, _notes, _skipped = m.build_comments(findings, anchors, line_text)
    assert len(comments) == 2, "a distinct finding was dropped with no trace"


def test_anything_grouped_is_recognisable_next_round():
    """GROUP_SIMILARITY below SIMILARITY leaves a band where grouping
    collapses members that suppression cannot then recognise, so the class
    drips one comment per push."""
    assert m.GROUP_SIMILARITY >= m.SIMILARITY, (
        "a pair can be grouped and then not recognised, which drips"
    )


def test_a_finding_that_is_never_posted_does_not_suppress_a_later_one():
    """run_texts was fed before the classification, so a finding dropped as
    "not a line this PR adds" still suppressed a later one — and the log said
    "already said in this review" when nothing had been said.

    Removing the within-run similarity leg made that unreachable as well:
    only an exact (path, line, body) match suppresses now, and the dropped
    finding is on a different line. Both the ordering and this test are kept
    as the invariant they assert, not as the last line of defence.
    """
    path = "contrib/python/x/a.py"
    diff = _diff_n_added_lines(3, path=path)
    anchors, line_text = m.walk_right_side(diff)
    body = "this subprocess call has no timeout argument at all"
    findings = [
        {"path": path, "line": 500, "body": body, "verify_steps": "read it"},
        {"path": path, "line": 1, "body": body, "verify_steps": "read it"},
    ]
    comments, _notes, _skipped = m.build_comments(findings, anchors, line_text)
    assert len(comments) == 1, "the unpostable finding suppressed a real one"


def test_two_long_paths_sharing_a_prefix_do_not_collide():
    """Paths differ at the END. Head-truncation mapped two files in one long
    directory onto the same span, and one file's note then suppressed the
    other's."""
    prefix = "core/python/alpha/" + "deep/" * 30
    a, b = prefix + "first.py", prefix + "second.py"
    assert m._safe_span(a) != m._safe_span(b)
    assert not m._same_path({"path": m._safe_span(a)}, b)


def test_findings_at_one_position_are_never_grouped():
    """The note says "in N other places", and for these there is no other
    place — it is the same place, N times. Several house rules fall back to
    line 1 when they cannot locate their subject, so three unknown manifest
    keys all land on manifest.yaml:1: one comment went out with a false count
    and two real CI-failing findings were dropped."""
    path = "contrib/python/x/manifest.yaml"
    body = '"{}" is not a key in manifest-schema.json'
    comments = [
        {
            "path": path,
            "line": 1,
            "side": "RIGHT",
            "trusted": True,
            "body": body.format(k),
        }
        for k in ("owner", "author", "maintainer")
    ]
    kept, dropped = m.group_repeats(comments)
    assert len(kept) == 3, "three findings at one line were collapsed"
    assert dropped == []
    assert all("other place" not in c["body"] for c in kept)


def test_findings_at_different_lines_still_group():
    path = "contrib/python/x/.env.example"
    # The checker's real H39 wording. A short paraphrase shares too few
    # tokens to clear the bar (0.5 against 0.55), so a synthetic fixture
    # asserts nothing about grouping.
    body = (
        '"{}" is a stub committed as if it were a real value. Someone '
        "copying this file has no way to tell it needs replacing; use "
        "<TODO: update-this-value>"
    )
    comments = [
        {
            "path": path,
            "line": i + 1,
            "side": "RIGHT",
            "trusted": True,
            "body": body.format(v),
        }
        for i, v in enumerate(("my-project-id", "changeme", "your-bucket"))
    ]
    kept, dropped = m.group_repeats(comments)
    assert len(kept) == 1 and len(dropped) == 2
    assert "2 other places" in kept[0]["body"]


def test_an_unpostable_finding_does_not_enter_the_dedupe_pool():
    """Recorded before the classification, a finding dropped as "not a line
    this PR adds" suppressed a later one under the reason "already said in
    this review", when nothing had been said. The previous commit claimed
    this ordering and added only a comment saying so."""
    # A CONTEXT line: in the diff, so a window can verify against it, but not
    # an ADDED line — which is exactly what becomes a note rather than an
    # inline comment.
    path = "contrib/python/x/a.py"
    diff = (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        "@@ -1,3 +1,3 @@\n context_line = 1\n+added = 2\n context_tail = 3\n"
    )
    anchors, line_text = m.walk_right_side(diff)
    body = "this subprocess call has no timeout argument at all"
    findings = [
        # No window, on a line the PR does not add: dropped, and it must
        # leave no trace in the pool.
        {"path": path, "line": 1, "body": body, "verify_steps": "read it"},
        # Same text and line, but with a window that verifies. This one
        # belongs in the review body as a note.
        {
            "path": path,
            "line": 1,
            "body": body,
            "verify_steps": "read it",
            "window": "   1: context_line = 1",
        },
    ]
    comments, notes, skipped = m.build_comments(findings, anchors, line_text)
    assert not any("already said in this review" in s for s in skipped), (
        f"an unpostable finding poisoned the pool: {skipped}"
    )
    assert len(notes) == 1, f"the note was lost: {skipped}"
    assert comments == []


def test_the_same_position_guard_covers_every_member_not_just_the_anchor():
    """Two members sharing a line with each OTHER still counted toward
    "(Same thing in N other places)" and one of them was collapsed away:
    lines 7, 8, 1, 1 produced a claim of 3 other places when there are 2."""
    path = "contrib/python/x/manifest.yaml"
    body = (
        '"{}" is a stub committed as if it were a real value. Someone '
        "copying this file has no way to tell it needs replacing; use "
        "<TODO: update-this-value>"
    )
    comments = [
        {
            "path": path,
            "line": ln,
            "side": "RIGHT",
            "trusted": True,
            "body": body.format(v),
        }
        for ln, v in (
            (7, "a-value"),
            (8, "b-value"),
            (1, "c-value"),
            (1, "d-value"),
        )
    ]
    kept, _dropped = m.group_repeats(comments)
    grouped = [c for c in kept if "other place" in c["body"]]
    assert grouped, "nothing grouped at all"
    import re as _re

    claimed = int(
        _re.search(
            r"Same thing in (\d+) other place", grouped[0]["body"]
        ).group(1)
    )
    # Against DISTINCT positions, not against the number dropped: those two
    # are equal whether or not the guard covers every member, so comparing
    # them asserts nothing. "N other places" is a claim about places.
    anchor = (grouped[0]["path"], grouped[0]["line"])
    other_positions = {
        (c["path"], c["line"])
        for c in comments
        if (c["path"], c["line"]) != anchor
    }
    assert claimed <= len(other_positions), (
        f"claimed {claimed} other places; there are {len(other_positions)}"
    )
    positions = {(c["path"], c["line"]) for c in kept}
    assert len(positions) == len(kept), "two kept comments share a position"


# --------------------------------------------------------- severity tags
#
# An inline comment does not show which lane produced it -- the lane name is
# only in the review body header -- so the tag is the author's only signal at
# the line of whether something blocks the merge.


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Security", "[MAJOR]"),
        ("Correctness", "[MAJOR]"),
        ("Maintainability", "[MINOR]"),
        ("Hygiene", "[MINOR]"),
        ("House Rules", "[MINOR]"),
        # A lane nobody mapped must claim the least, not the most.
        ("Some New Lane", "[MINOR]"),
        ("", "[MINOR]"),
    ],
)
def test_a_model_lane_is_graded_by_its_lane_alone(label, expected):
    assert m.severity_tag(label) == expected


def test_only_a_failing_deterministic_check_earns_critical():
    """CRITICAL means "this blocks your merge", which is a fact only when a
    deterministic check says so. A model lane never reaches it, however
    serious it believes its finding to be."""
    assert m.severity_tag("House Rules", "fail") == "[CRITICAL]"
    assert m.severity_tag("House Rules", "advisory") == "[MINOR]"
    assert m.severity_tag("House Rules", None) == "[MINOR]"
    assert m.severity_tag("House Rules", "") == "[MINOR]"


def test_the_ci_signal_outranks_the_lane_whatever_the_lane_is():
    assert m.severity_tag("Hygiene", "fail") == "[CRITICAL]"


def test_apply_tag_does_not_stack_a_second_prefix():
    """build_payload runs once per review, but a body that already carries a
    tag -- a note parsed back out of a review we posted last round -- must not
    come back as `[MINOR] [MINOR] ...`."""
    assert m.apply_tag("plain", "[MINOR]") == "[MINOR] plain"
    assert m.apply_tag("[MINOR] plain", "[MINOR]") == "[MINOR] plain"
    assert m.apply_tag("[MAJOR] plain", "[CRITICAL]") == "[CRITICAL] plain"


def test_the_payload_is_tagged_and_carries_no_internal_keys():
    payload = m.build_payload(
        "Security",
        [
            {
                "path": "x.py",
                "line": 2,
                "side": "RIGHT",
                "body": "shell=True with a user-controlled value",
                "_ci": None,
            }
        ],
        [],
    )
    comment = payload["comments"][0]
    assert comment["body"] == "[MAJOR] shell=True with a user-controlled value"
    assert "_ci" not in comment, "internal state must not reach GitHub"


def test_a_ci_failing_finding_is_tagged_critical_end_to_end():
    payload = m.build_payload(
        "House Rules",
        [
            {
                "path": "x/pyproject.toml",
                "line": 1,
                "side": "RIGHT",
                "body": "[build-system] missing",
                "_ci": "fail",
            },
            {
                "path": "x/manifest.yaml",
                "line": 4,
                "side": "RIGHT",
                "body": "ownership.team names a company",
                "_ci": "advisory",
            },
        ],
        [],
    )
    bodies = [c["body"] for c in payload["comments"]]
    assert bodies[0].startswith("[CRITICAL] ")
    assert bodies[1].startswith("[MINOR] ")


def test_notes_in_the_review_body_are_tagged_too():
    """A note is a finding that had nowhere to anchor, not a lesser one. On
    PR #2373 this class held all three hard CI failures."""
    payload = m.build_payload(
        "House Rules",
        [],
        [
            {
                "path": "y.toml",
                "line": 9,
                "body": "uv.lock missing",
                "_ci": "fail",
            }
        ],
    )
    assert "`y.toml:9` — [CRITICAL] uv.lock missing" in payload["body"]


# ------------------------------- the migration: tagged vs untagged bodies


def test_an_untagged_comment_still_suppresses_its_tagged_successor():
    """THE regression this feature could cause. Every comment already on an
    open PR was posted before tagging existed, so it has no prefix. The next
    push produces the same finding, now tagged. Compared as written the two
    differ, every one of them looks new, and the whole review is posted a
    second time -- the non-convergence the comment budget exists to end."""
    previous = {
        "kind": "inline",
        "path": "x.py",
        "line": 2,
        "body": "subprocess call carries no timeout argument",
    }
    _zones, texts = m.build_exclusions([previous])
    why = m.already_raised(
        "x.py",
        2,
        "[MINOR] subprocess call carries no timeout argument",
        {},
        texts,
        trusted=True,
    )
    assert why, "a tagged repeat of an untagged comment was not suppressed"


def test_a_tagged_comment_suppresses_the_same_tagged_finding():
    previous = {
        "kind": "inline",
        "path": "x.py",
        "line": 2,
        "body": "[CRITICAL] [build-system] missing",
    }
    _zones, texts = m.build_exclusions([previous])
    assert m.already_raised(
        "x.py", 2, "[CRITICAL] [build-system] missing", {}, texts, trusted=True
    )


def test_a_retagged_finding_is_still_the_same_finding():
    """A rule reclassified from advisory to CI-failing changes its tag. It is
    the same sentence about the same line and must not be posted again."""
    previous = {
        "kind": "inline",
        "path": "x.py",
        "line": 2,
        "body": "[MINOR] ownership.team names a company",
    }
    _zones, texts = m.build_exclusions([previous])
    assert m.already_raised(
        "x.py",
        2,
        "[CRITICAL] ownership.team names a company",
        {},
        texts,
        trusted=True,
    )


def test_stripping_the_tag_does_not_swallow_an_unrelated_finding():
    """The tag is dropped before comparison, which must not make two
    different findings compare equal."""
    previous = {
        "kind": "inline",
        "path": "x.py",
        "line": 2,
        "body": "[MINOR] ownership.team names a company",
    }
    _zones, texts = m.build_exclusions([previous])
    assert not m.already_raised(
        "x.py",
        7,
        "[MINOR] requires-python is 3.10, the repo minimum is 3.11",
        {},
        texts,
        trusted=True,
    )


def test_base_body_strips_a_tag_and_the_grouping_suffix_together():
    body = "[CRITICAL] uv.lock missing\n(Same thing in 3 other places in this review.)"
    assert m._base_body(body) == "uv.lock missing"


def test_tagging_drops_internal_state_from_notes_as_well_as_comments():
    """Notes are rendered into the review body rather than posted as JSON, so
    a stray `_ci` on one is invisible rather than harmful -- which is exactly
    why it would survive. Both go through one function so neither can drift."""
    tagged = m._tagged(
        [
            {"path": "a.py", "line": 1, "body": "one", "_ci": "fail"},
            {"path": "b.py", "line": 2, "body": "two"},
        ],
        "House Rules",
    )
    assert [t["body"] for t in tagged] == ["[CRITICAL] one", "[MINOR] two"]
    assert not any("_ci" in t for t in tagged)


def test_tagging_preserves_every_other_field():
    tagged = m._tagged(
        [{"path": "a.py", "line": 1, "side": "RIGHT", "body": "x"}], "Security"
    )
    assert tagged[0] == {
        "path": "a.py",
        "line": 1,
        "side": "RIGHT",
        "body": "[MAJOR] x",
    }


def test_already_raised_also_sees_past_a_grouping_suffix():
    """Both sides now reduce through `_base_body`, so an incoming body that
    somehow carries the grouping suffix matches the stored form too. It does
    not happen today -- grouping runs after this check -- and that is the
    point: the comparison no longer depends on that ordering holding."""
    previous = {
        "kind": "inline",
        "path": "x.py",
        "line": 2,
        "body": "uv.lock is missing",
    }
    _zones, texts = m.build_exclusions([previous])
    incoming = (
        "[CRITICAL] uv.lock is missing\n"
        "(Same thing in 3 other places in this review.)"
    )
    assert m.already_raised("x.py", 2, incoming, {}, texts, trusted=True)


def test_the_ci_fail_signal_matches_the_checker_that_writes_it():
    """`CI_FAIL` here and `CI_FAIL` in check_house_rules.py are one value
    written in two files, because the two run in separate jobs. If they drift,
    every CI failure is silently downgraded to a nit."""
    checker = (
        Path(__file__).resolve().parents[3]
        / ".agents"
        / "skills"
        / "github-pr-review"
        / "scripts"
        / "check_house_rules.py"
    )
    if not checker.exists():
        pytest.skip("checker not present in this checkout")
    found = re.search(
        r"^CI_FAIL\s*=\s*[\"'](\w+)[\"']", checker.read_text(), re.M
    )
    assert found, "check_house_rules.py no longer defines CI_FAIL"
    assert found.group(1) == m.CI_FAIL


# --------------------------------------------------------------------------
# Truncated model output, and git-quoted paths. Both were silent losses: the
# first turned a recoverable response into a red check, the second dropped
# every finding in any file whose name git quotes.
# --------------------------------------------------------------------------

TRUNCATED_RESPONSE = (
    "Here are my findings:\n\n"
    "```json\n"
    "[\n"
    '  {"path": "a.py", "line": 10, "body": "first", "window": "10: x = 1"},\n'
    '  {"path": "b.py", "line": 20, "body": "second find'
)


def test_truncated_response_salvages_complete_findings():
    """An output-limit hit must not cost a red check.

    FENCED_BLOCK cannot match without its closing fence, and the bare-"["
    fallback does not fire because the response opens with the fence. That
    combination raised ReviewerOutputError -> exit 2 -> a failure comment on
    the contributor's PR, discarding findings that had parsed cleanly.
    """
    findings = m.extract_findings(TRUNCATED_RESPONSE)
    assert [f["path"] for f in findings] == ["a.py"]


def test_response_with_no_array_at_all_still_raises():
    """The salvage path must not swallow a genuinely empty response."""
    with pytest.raises(m.ReviewerOutputError):
        m.extract_findings("I reviewed the PR and found no issues.")


def _one_file_diff(header: str) -> str:
    return f"diff --git x y\n--- a/x\n{header}\n@@ -0,0 +1,1 @@\n+import os\n"


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ('+++ "b/my recipe/agent.py"', "my recipe/agent.py"),
        ('+++ "b/caf\\303\\251.py"', "café.py"),
        ("+++ b/app/agent.py", "app/agent.py"),
        # A real top-level `b/` directory: only git's own prefix comes off.
        ("+++ b/b/thing.py", "b/thing.py"),
    ],
)
def test_walk_right_side_reads_quoted_paths(header, expected):
    """core.quotePath is on by default, so this is the normal shape.

    Unquoting has to happen BEFORE the side prefix is stripped: in
    `"b/x"` the `b/` sits inside the quotes, so a raw startswith() test
    never fires and the key keeps both the quotes and the prefix.
    """
    anchors, _ = m.walk_right_side(_one_file_diff(header))
    assert list(anchors.keys()) == [expected]
