"""verify_findings is the control that replaces my manual diligence.

Every test here is a mistake actually made during a real review.
"""

import pytest
import verify_findings as vf


def write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def finding(**kw):
    base = {
        "path": "src/a.py",
        "line": 2,
        "what": "something",
        "cheap": "cheap",
        "verify_steps": "read line 2",
        "window": "",
    }
    base.update(kw)
    return base


# --------------------------------------------------------------- window check


def test_matching_window_verifies(tmp_path):
    write(tmp_path, "src/a.py", "one\ntwo\nthree\n")
    f = finding(window="   1: one\n   2: two\n   3: three")
    ok, _ = vf.check_window(f, str(tmp_path))
    assert ok


def test_fabricated_window_is_rejected(tmp_path):
    """A lane that invents a finding invents the source line with it."""
    write(tmp_path, "src/a.py", "one\ntwo\nthree\n")
    f = finding(window="   2: os.system(user_input)")
    ok, reason = vf.check_window(f, str(tmp_path))
    assert not ok
    assert "window line 2" in reason


def test_emoji_escape_is_not_a_fabrication(tmp_path):
    """Regression: three false rejections on PR #2373.

    The lane writes the escape text; the file holds the real character. Decoding
    via unicode_escape mojibakes the file side and flags every emoji line.
    """
    write(
        tmp_path, "src/a.py", 'x = 1\nprint("\u26a0\ufe0f [Quota] exhausted")\n'
    )
    f = finding(line=2, window=r'   2: print("\u26a0\ufe0f [Quota] exhausted")')
    ok, reason = vf.check_window(f, str(tmp_path))
    assert ok, reason


def test_line_outside_file_is_rejected(tmp_path):
    write(tmp_path, "src/a.py", "one\ntwo\n")
    ok, reason = vf.check_window(finding(line=99), str(tmp_path))
    assert not ok
    assert "outside file" in reason


def test_missing_file_is_rejected(tmp_path):
    ok, reason = vf.check_window(finding(path="nope.py"), str(tmp_path))
    assert not ok
    assert "does not exist" in reason


def test_absent_window_is_tolerated_not_rejected(tmp_path):
    """No window is a gap, not evidence of fabrication."""
    write(tmp_path, "src/a.py", "one\ntwo\n")
    ok, _ = vf.check_window(finding(window=""), str(tmp_path))
    assert ok


# ------------------------------------------------------------ addressability


def test_unaddressable_line_is_kept_but_flagged(tmp_path):
    """PR #2373: a third of findings sat outside every hunk. Real, uncommentable."""
    write(tmp_path, "src/a.py", "one\ntwo\nthree\n")
    addr = {"src/a.py": {1, 3}}
    verified, rejected, _ = vf.verify([finding(line=2)], str(tmp_path), addr)
    assert not rejected
    assert verified[0]["anchorable"] is False


def test_addressable_line_is_marked_true(tmp_path):
    write(tmp_path, "src/a.py", "one\ntwo\nthree\n")
    verified, _, _ = vf.verify(
        [finding(line=2)], str(tmp_path), {"src/a.py": {1, 2, 3}}
    )
    assert verified[0]["anchorable"] is True


def test_file_absent_from_diff_is_unanchorable(tmp_path):
    write(tmp_path, "src/a.py", "one\ntwo\n")
    verified, _, _ = vf.verify([finding()], str(tmp_path), {})
    assert verified[0]["anchorable"] is False


# ------------------------------------------------------- verify_steps gating


@pytest.mark.parametrize(
    "steps",
    [
        "trace the value through build_client",
        "assuming the allowlist is empty",
        "consider the case where input is empty",
        "if an attacker supplies a crafted path",
        "grep the repo for other callers",
        "open another file and compare",
        "simulate the regex with two ids",
    ],
)
def test_expensive_verify_steps_force_not_cheap(tmp_path, steps):
    write(tmp_path, "src/a.py", "one\ntwo\n")
    verified, _, _ = vf.verify(
        [finding(verify_steps=steps)], str(tmp_path), {"src/a.py": {2}}
    )
    assert verified[0]["cheap"] == "not_cheap"
    assert verified[0]["_cheap_reason"]


def test_local_verify_steps_stay_cheap(tmp_path):
    write(tmp_path, "src/a.py", "one\ntwo\n")
    verified, _, _ = vf.verify(
        [finding(verify_steps="read lines 1-3 of this file")],
        str(tmp_path),
        {"src/a.py": {2}},
    )
    assert verified[0]["cheap"] == "cheap"
    assert "_cheap_reason" not in verified[0]


def test_lane_cannot_relabel_its_way_past_the_gate(tmp_path):
    """Workers no longer assign `cheap`; the gate computes it from verify_steps.

    Even a worker that smuggles the label in loses to its own procedure.
    """
    write(tmp_path, "src/a.py", "one\ntwo\n")
    f = finding(cheap="cheap", verify_steps="trace it through two modules")
    verified, _, _ = vf.verify([f], str(tmp_path), {"src/a.py": {2}})
    assert verified[0]["cheap"] == "not_cheap"


def test_missing_verify_steps_is_not_cheap(tmp_path):
    """No stated procedure means no evidence it is checkable."""
    write(tmp_path, "src/a.py", "one\ntwo\n")
    verified, _, _ = vf.verify(
        [finding(verify_steps="")], str(tmp_path), {"src/a.py": {2}}
    )
    assert verified[0]["cheap"] == "not_cheap"


# ------------------------------------------------------------------ clustering


def test_repeated_class_is_clustered(tmp_path):
    """Five 'unused import' findings are one comment, not five."""
    fs = [
        finding(
            path=f"src/m{i}.py",
            line=1,
            what=f"the logging import is created but never used in m{i}",
        )
        for i in range(4)
    ]
    groups = vf.cluster(fs)
    assert len(groups) == 1 and len(groups[0]) == 4


def test_distinct_findings_are_not_clustered():
    fs = [
        finding(what="the licence header stops mid-sentence"),
        finding(what="ipv4_enabled is true on the database"),
        finding(what="city_clean is assigned and never read"),
    ]
    assert vf.cluster(fs) == []


def test_two_of_a_kind_stays_below_the_grouping_threshold():
    fs = [
        finding(what="the logging import is created but never used here"),
        finding(what="the logging import is created but never used there"),
    ]
    assert vf.cluster(fs) == []


# --------------------------------------------------------------------- schema


@pytest.mark.parametrize(
    "bad", [{"line": 2, "what": "x"}, {"path": "src/a.py", "line": 2}]
)
def test_incomplete_findings_are_rejected(tmp_path, bad):
    verified, rejected, _ = vf.verify([bad], str(tmp_path), {})
    assert not verified and len(rejected) == 1


def test_norm_ignores_whitespace_and_indentation():
    assert vf.norm("    x = 1") == vf.norm("x=1")


def test_norm_keeps_ascii_substance():
    assert vf.norm("os.system(x)") != vf.norm("subprocess.run(x)")


# ------------------------------------------------- suppressing existing work


def existing(**kw):
    base = {
        "kind": "inline",
        "path": "src/a.py",
        "line": 2,
        "original_line": 2,
        "body": "already said this",
        "author": "someone",
        "is_bot": False,
        "resolved": False,
        "outdated": False,
    }
    base.update(kw)
    return base


def test_exact_line_collision_is_suppressed(tmp_path):
    """The re-review case: my own prior comments must exclude themselves."""
    write(tmp_path, "src/a.py", "one\ntwo\nthree\n")
    v, _, sup = vf.verify(
        [finding()], str(tmp_path), {"src/a.py": {2}}, [existing()]
    )
    assert not v and len(sup) == 1
    assert "already commented on this line" in sup[0]["_reason"]


def test_within_two_lines_is_suppressed(tmp_path):
    write(tmp_path, "src/a.py", "\n".join(f"l{i}" for i in range(10)))
    v, _, sup = vf.verify(
        [finding(line=4)], str(tmp_path), {"src/a.py": {4}}, [existing(line=2)]
    )
    assert not v and "line 2" in sup[0]["_reason"]


def test_three_lines_away_survives(tmp_path):
    """Proximity is 2, deliberately tight so fresh findings aren't eaten."""
    write(tmp_path, "src/a.py", "\n".join(f"l{i}" for i in range(10)))
    v, _, sup = vf.verify(
        [finding(line=5)], str(tmp_path), {"src/a.py": {5}}, [existing(line=2)]
    )
    assert len(v) == 1 and not sup


def test_outdated_thread_does_not_block(tmp_path):
    """The code moved out from under it, so the spot gets a fresh look."""
    write(tmp_path, "src/a.py", "one\ntwo\nthree\n")
    v, _, sup = vf.verify(
        [finding()],
        str(tmp_path),
        {"src/a.py": {2}},
        [existing(outdated=True, body="unrelated words entirely")],
    )
    assert len(v) == 1 and not sup


def test_resolved_thread_still_blocks(tmp_path):
    """Already discussed. Re-raising a settled point is worse than missing it."""
    write(tmp_path, "src/a.py", "one\ntwo\nthree\n")
    v, _, sup = vf.verify(
        [finding()], str(tmp_path), {"src/a.py": {2}}, [existing(resolved=True)]
    )
    assert not v and "(resolved)" in sup[0]["_reason"]


def test_bot_comments_suppress_like_humans(tmp_path):
    write(tmp_path, "src/a.py", "one\ntwo\nthree\n")
    v, _, sup = vf.verify(
        [finding()],
        str(tmp_path),
        {"src/a.py": {2}},
        [existing(is_bot=True, author="lint[bot]")],
    )
    assert not v and "a bot" in sup[0]["_reason"]


def test_similar_text_suppresses_even_on_a_different_line(tmp_path):
    """Top-level comments have no line at all, so text is the only signal."""
    write(tmp_path, "src/a.py", "\n".join(f"l{i}" for i in range(40)))
    f = finding(
        line=30, what="hardcoded project identifier committed in the config"
    )
    prior = existing(
        kind="issue",
        path=None,
        line=None,
        original_line=None,
        body="hardcoded project identifier committed in the config file",
    )
    v, _, sup = vf.verify([f], str(tmp_path), {"src/a.py": {30}}, [prior])
    assert not v and "similar" in sup[0]["_reason"]


def test_unrelated_text_is_not_suppressed(tmp_path):
    write(tmp_path, "src/a.py", "\n".join(f"l{i}" for i in range(40)))
    f = finding(line=30, what="the licence header stops mid sentence")
    prior = existing(
        kind="issue",
        path=None,
        line=None,
        original_line=None,
        body="database has a public ipv4 address enabled",
    )
    v, _, sup = vf.verify([f], str(tmp_path), {"src/a.py": {30}}, [prior])
    assert len(v) == 1 and not sup


def test_no_existing_comments_suppresses_nothing(tmp_path):
    write(tmp_path, "src/a.py", "one\ntwo\n")
    v, _, sup = vf.verify([finding()], str(tmp_path), {"src/a.py": {2}}, [])
    assert len(v) == 1 and not sup


# ------------------------------------------------------- fact-anchoring lint


def test_lint_flags_identifier_absent_from_window():
    notes = vf.fact_anchor_lint(
        {"comment": "does `strip_wrapper` handle this?", "window": "  3: x = 1"}
    )
    assert notes and "strip_wrapper" in notes[0]


def test_lint_passes_identifier_present_in_window():
    assert not vf.fact_anchor_lint(
        {
            "comment": "`city_clean` isn't used below",
            "window": "  3: city_clean = city.split(',')[0]",
        }
    )


def test_lint_flags_inference_vocabulary():
    notes = vf.fact_anchor_lint(
        {
            "comment": "Is 200 characters enough to stay clear of the keys?",
            "window": "  37: print(cmd[:200])",
        }
    )
    assert any("inference word" in n for n in notes)


def test_lint_is_quiet_on_a_plain_observation():
    assert not vf.fact_anchor_lint(
        {"comment": "this runs as root", "window": '  35: CMD ["uvicorn"]'}
    )


def test_ascii_ellipsis_abbreviation_is_not_a_fabrication(tmp_path):
    """PR #2302 regression: lanes shorten long lines with a trailing ellipsis.

    A Unicode ellipsis disappears in norm() as non-ASCII; an ASCII '...' does not,
    and turned a valid prefix into a mismatch. Six real findings were rejected.
    """
    write(
        tmp_path,
        "src/a.py",
        "x = 1\nthis is a very long line with lots of detail after it\n",
    )
    f = finding(line=2, window="   2: this is a very long line with lots...")
    ok, reason = vf.check_window(f, str(tmp_path))
    assert ok, reason


def test_unicode_ellipsis_abbreviation_also_passes(tmp_path):
    write(
        tmp_path,
        "src/a.py",
        "x = 1\nthis is a very long line with lots of detail after it\n",
    )
    f = finding(line=2, window="   2: this is a very long line with lots\u2026")
    ok, reason = vf.check_window(f, str(tmp_path))
    assert ok, reason


def test_truncation_without_a_marker_is_still_rejected(tmp_path):
    """Silent divergence is indistinguishable from fabrication, so it must fail."""
    write(tmp_path, "src/a.py", "x = 1\nalpha beta gamma\n")
    f = finding(line=2, window="   2: alpha beta DELTA")
    ok, _ = vf.check_window(f, str(tmp_path))
    assert not ok


def test_consistent_line_drift_is_repaired_not_rejected(tmp_path):
    """PR #2302: four findings were off by exactly one line. The content was real;
    only the numbering was wrong, so discarding them lost real findings."""
    write(
        tmp_path,
        "src/a.py",
        "\n".join(["zero", "alpha", "beta", "gamma", "delta"]),
    )
    f = finding(line=2, window="   2: alpha\n   3: beta\n   4: gamma")
    # file has alpha at 2, beta at 3, gamma at 4 -> offset 0; shift the claim by 1
    f = finding(line=3, window="   3: alpha\n   4: beta\n   5: gamma")
    ok, reason = vf.check_window(f, str(tmp_path))
    assert ok and "corrected" in reason
    assert f["line"] == 2 and f["_line_corrected"] == "3 -> 2"


def test_single_line_match_is_not_enough_to_claim_drift(tmp_path):
    """One line can coincide by chance; two is evidence."""
    write(
        tmp_path, "src/a.py", "\n".join(["x = 1", "x = 1", "totally different"])
    )
    f = finding(line=3, window="   3: x = 1")
    ok, _ = vf.check_window(f, str(tmp_path))
    assert not ok


def test_drift_beyond_the_search_window_is_still_rejected(tmp_path):
    write(tmp_path, "src/a.py", "\n".join(["a"] * 20 + ["alpha", "beta"]))
    f = finding(line=2, window="   2: alpha\n   3: beta")
    ok, _ = vf.check_window(f, str(tmp_path))
    assert not ok


# ------------------------------------------------- CI-status awareness


def _fail_finding(**kw):
    f = {
        "path": "contrib/python/x/pyproject.toml",
        "line": 4,
        "ci": "fail",
        "what": "declares a [tool.ruff] table",
        "evidence": "python-validate-recipe.yml:261-266",
        "verify_steps": "read line 4",
    }
    f.update(kw)
    return f


def test_a_finding_whose_workflow_is_already_red_is_suppressed():
    """The author is already looking at that red check; the comment adds noise
    and spends a budget slot."""
    why = vf.already_red(_fail_finding(), {"python-validate-recipe.yml"})
    assert why and "already failing" in why


def test_a_green_workflow_does_not_suppress():
    assert vf.already_red(_fail_finding(), {"go-format.yml"}) is None


def test_an_advisory_finding_is_never_suppressed_by_ci_status():
    """Nothing is failing for an advisory rule, so the comment is the only way
    the author hears it -- even if some unrelated workflow is red."""
    f = _fail_finding(ci="advisory")
    assert vf.already_red(f, {"python-validate-recipe.yml"}) is None


def test_unknown_check_status_suppresses_nothing():
    """gh failed, or the checks have not run. Dropping a real finding because
    we could not read a status is the wrong way to be wrong."""
    assert vf.already_red(_fail_finding(), None) is None
    assert vf.already_red(_fail_finding(), set()) is None


def test_only_a_failure_conclusion_counts(monkeypatch):
    rows = "\n".join(
        [
            ".github/workflows/python-validate-recipe.yml\tfailure",
            ".github/workflows/python-tests.yml\tsuccess",
            ".github/workflows/go-format.yml\t",  # still running
            ".github/workflows/tools-tests.yml\tcancelled",
        ]
    )

    class P:
        returncode = 0
        stdout = rows

    monkeypatch.setattr(vf.subprocess, "run", lambda *a, **k: P())
    assert vf.red_workflows("o/r", "abc") == {"python-validate-recipe.yml"}


def test_red_workflows_returns_none_when_gh_fails(monkeypatch):
    class P:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(vf.subprocess, "run", lambda *a, **k: P())
    assert vf.red_workflows("o/r", "abc") is None
