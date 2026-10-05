"""The rejection ledger: don't re-propose what the user already cut.

Suppression of posted comments cannot see a cut comment -- it was never posted, so
GitHub has no record of it. Observed on the PR #2373 re-review: three findings the
user had already rejected came back.
"""

import pytest
import rejections
import verify_findings as vf


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_PR_REVIEW_STATE", str(tmp_path))


def c(path="src/a.py", line=2, comment="something"):
    return {"path": path, "line": line, "comment": comment}


def test_record_then_load_roundtrip():
    rejections.record("o/r", "1", [c()])
    got = rejections.load("o/r", "1")
    assert len(got) == 1 and got[0]["path"] == "src/a.py"


def test_recording_is_idempotent():
    rejections.record("o/r", "1", [c()])
    added, _ = rejections.record("o/r", "1", [c()])
    assert added == 0
    assert len(rejections.load("o/r", "1")) == 1


def test_ledgers_are_per_pr():
    rejections.record("o/r", "1", [c()])
    assert rejections.load("o/r", "2") == []


def test_ledgers_are_per_repo():
    rejections.record("o/r", "1", [c()])
    assert rejections.load("other/repo", "1") == []


def test_missing_ledger_is_empty_not_an_error():
    assert rejections.load("never/seen", "99") == []


def test_corrupt_ledger_degrades_quietly(tmp_path):
    p = rejections.ledger_path("o/r", "1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    assert rejections.load("o/r", "1") == []


def test_entries_carry_a_timestamp():
    rejections.record("o/r", "1", [c()])
    assert rejections.load("o/r", "1")[0]["rejected_at"].endswith("Z")


# ------------------------------------------------- suppression on re-review


def write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def finding(**kw):
    base = {
        "path": "src/a.py",
        "line": 2,
        "what": "x",
        "comment": "something",
        "verify_steps": "read line 2",
        "window": "   2: two",
    }
    base.update(kw)
    return base


def test_a_cut_comment_does_not_come_back(tmp_path):
    write(tmp_path, "src/a.py", "one\ntwo\nthree\n")
    prior = vf.rejected_as_exclusions([c()])
    v, _, sup = vf.verify([finding()], str(tmp_path), {"src/a.py": {2}}, prior)
    assert not v
    assert "you cut this comment on a previous review" in sup[0]["_reason"]


def test_a_cut_comment_blocks_nearby_lines_too(tmp_path):
    write(tmp_path, "src/a.py", "\n".join(f"l{i}" for i in range(10)))
    prior = vf.rejected_as_exclusions([c(line=2)])
    v, _, sup = vf.verify(
        [finding(line=4, window="   4: l3")],
        str(tmp_path),
        {"src/a.py": {4}},
        prior,
    )
    assert not v and sup


def test_a_rejection_never_expires_as_outdated(tmp_path):
    """A decision the user made does not lapse because the code moved."""
    write(tmp_path, "src/a.py", "one\ntwo\n")
    entry = vf.rejected_as_exclusions([c()])[0]
    assert entry["outdated"] is False


def test_unrelated_finding_survives_the_ledger(tmp_path):
    write(tmp_path, "src/a.py", "\n".join(f"l{i}" for i in range(30)))
    prior = vf.rejected_as_exclusions(
        [c(line=2, comment="licence header truncated")]
    )
    f = finding(
        line=25, what="ipv4_enabled is true on the database", window="  25: l24"
    )
    v, _, sup = vf.verify([f], str(tmp_path), {"src/a.py": {25}}, prior)
    assert len(v) == 1 and not sup


def test_empty_ledger_suppresses_nothing(tmp_path):
    write(tmp_path, "src/a.py", "one\ntwo\n")
    v, _, sup = vf.verify(
        [finding()],
        str(tmp_path),
        {"src/a.py": {2}},
        vf.rejected_as_exclusions([]),
    )
    assert len(v) == 1 and not sup
