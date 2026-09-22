"""The fact-anchoring rule, held to real accept/reject decisions.

This is the skill's only ground truth about what the user will actually post:
20 drafted comments on google/adk-samples#2373, 16 posted and 4 cut.

The rule itself is a judgement an agent applies. What CAN be tested is the
`fact_anchor_lint` heuristic that approximates it — so if someone loosens the lint,
or the rule drifts, these fail instead of the skill quietly getting worse.

The fixture is small and the rule was fitted to it, so a perfect score here is
expected rather than impressive. Its value is as a *ratchet*: it can only get worse
silently if someone edits the fixture too.
"""

import json
from pathlib import Path

import pytest
import verify_findings as vf

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "pr2373_outcomes.json").read_text()
)
COMMENTS = FIXTURE["comments"]
CUT = [c for c in COMMENTS if c["verdict"] == "cut"]
KEPT = [c for c in COMMENTS if c["verdict"] == "keep"]


def test_fixture_matches_the_recorded_outcome():
    assert len(COMMENTS) == 20
    assert len(KEPT) == 16 and len(CUT) == 4


@pytest.mark.parametrize("c", CUT, ids=lambda c: f"cut-{c['n']}")
def test_every_cut_comment_has_a_recorded_rationale(c):
    """A cut with no reason teaches nothing."""
    assert c["rationale"] and "anchors on" not in c["rationale"]


def test_lint_flags_most_of_what_was_cut():
    """The heuristic should catch the rejected shapes, not just claim to."""
    flagged = [c for c in CUT if vf.fact_anchor_lint(c)]
    assert len(flagged) == 4, (
        f"lint caught {len(flagged)}/4 cut comments, expected 4: "
        f"{[c['n'] for c in CUT if not vf.fact_anchor_lint(c)]}"
    )


def test_lint_is_quiet_on_most_of_what_was_kept():
    """False positives are the real cost — they suppress comments you wanted."""
    noisy = [c for c in KEPT if vf.fact_anchor_lint(c)]
    assert len(noisy) <= 1, (
        f"lint fires on {len(noisy)}/{len(KEPT)} accepted comments: "
        f"{[c['n'] for c in noisy]} — false positives suppress wanted comments"
    )


def test_lint_separates_cut_from_kept_better_than_chance():
    cut_rate = sum(1 for c in CUT if vf.fact_anchor_lint(c)) / len(CUT)
    keep_rate = sum(1 for c in KEPT if vf.fact_anchor_lint(c)) / len(KEPT)
    assert cut_rate > keep_rate, (
        f"lint fires on {cut_rate:.0%} of cut vs {keep_rate:.0%} of kept — "
        "it is not discriminating"
    )


def test_every_fixture_comment_has_a_window():
    """Without a window the lint cannot check identifiers, and the user cannot
    settle the comment from the table."""
    missing = [c["n"] for c in COMMENTS if not c.get("window")]
    assert not missing, f"comments without a window: {missing}"


def test_the_four_cuts_are_the_expected_ones():
    """Pinned so a fixture regeneration cannot silently rewrite history."""
    assert {c["n"] for c in CUT} == {3, 10, 17, 20}
