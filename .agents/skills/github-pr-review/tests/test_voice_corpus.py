"""The voice corpus is the skill's only ground truth. Guard it from rotting.

The 16-for-16 fact-anchoring split and the four remedy edits are real accept/reject
decisions on live comments, not a rating session. If someone trims voice.md these
tests fail loudly rather than the skill quietly losing its calibration.
"""

import re
from pathlib import Path

import pytest

VOICE = Path(__file__).parent.parent / "reference" / "voice.md"
TEXT = VOICE.read_text(encoding="utf-8")


def test_fact_anchoring_is_the_first_filter():
    """It predicts acceptance better than anything else in the file."""
    assert "## The first filter: anchor on a fact" in TEXT
    i_filter = TEXT.index("anchor on a fact")
    i_hard = TEXT.index("## The hard rule")
    assert i_filter < i_hard, "fact-anchoring must precede the register rules"


def test_all_four_rejected_examples_are_present():
    """Each was cut by the user on PR #2373 for demanding reasoning."""
    for fragment in (
        "Is 200 characters enough",
        "break the pattern the rest of the map follows",
        "is in the accepted list",
        "restored afterwards",
    ):
        assert fragment in TEXT, f"rejected example missing: {fragment}"


def test_accepted_examples_are_present():
    """Posted verbatim, so they are the closest thing to a target."""
    for fragment in (
        "hardcoded project name here",
        "isn't used below",
        "has nothing to interpolate",
        "no licence header on this one",
        "looks like three files got concatenated",
    ):
        assert fragment in TEXT, f"accepted example missing: {fragment}"


def test_the_rejected_table_explains_what_each_demanded():
    section = TEXT[
        TEXT.index("## The first filter") : TEXT.index("## The hard rule")
    ]
    for demand in ("arithmetic", "infer a pattern", "reason about"):
        assert demand in section, f"missing rationale: {demand}"


def test_remedy_rule_exists_and_cites_the_real_edits():
    assert "A remedy is welcome" in TEXT
    for fragment in (
        "I suggest cleaning up all the headers",
        "You may add these default values",
    ):
        assert fragment in TEXT, f"remedy evidence missing: {fragment}"


def test_remedy_rule_does_not_contradict_the_ban_list():
    """`I suggest` / `You may` are attested; `Consider …` is still banned."""
    assert "Consider …" in TEXT
    assert "I suggest" in TEXT and "You may" in TEXT
    ban = TEXT[TEXT.index("## Banned outright") :]
    assert "not because suggesting a fix is banned" in ban


def test_absence_claims_require_an_inventory():
    assert "Never assert an absence you did not inventory" in TEXT
    assert "load_dotenv" in TEXT, "the real incident should stay as the example"


def test_example_20_is_still_rejected():
    """The remedy rule must not be read as licence for the dense chain."""
    assert "20" in TEXT
    assert "one sentence carrying mechanism" in TEXT or "too dense" in TEXT


@pytest.mark.parametrize(
    "section",
    [
        "## The two registers",
        "## The hard rule",
        "## Say what you are pointing at",
        "## Plausibility — could the reviewer have known this?",
        "## Severity does not change tone",
        "## Banned outright",
    ],
)
def test_core_sections_survive(section):
    assert section in TEXT


def test_no_stale_mode_or_confidence_vocabulary():
    """Modes and the confidence label are gone; the corpus must not resurrect them."""
    for dead in ("cheap mode", "complete mode", "not_high"):
        assert dead not in TEXT, f"stale vocabulary in voice.md: {dead}"


# ------------------------------------------- structural, not string-presence

import json as _json  # noqa: E402 -- the corpus below is the module docstring's subject
from pathlib import Path as _Path  # noqa: E402

_FIXTURE = _json.loads(
    (_Path(__file__).parent / "fixtures" / "pr2373_outcomes.json").read_text()
)
_BY_N = {c["n"]: c for c in _FIXTURE["comments"]}


def _table_rows(heading):
    """Rows of the first markdown table under a heading."""
    section = TEXT[TEXT.index(heading) :]
    rows = []
    for line in section.split("\n"):
        if line.startswith("|") and not re.match(r"^\|[\s|:-]+\|$", line):
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows.append(cells)
        elif rows and not line.startswith("|"):
            break
    return rows[1:]  # drop the header row


def test_rejected_table_is_structurally_complete():
    """Four rejected examples, each with an id, the comment, and what it demands."""
    rows = _table_rows("## The first filter: anchor on a fact")
    ids = [r[0] for r in rows if r and r[0].startswith("R")]
    assert ids == ["R1", "R2", "R3", "R4"], ids
    for r in rows:
        if r and r[0].startswith("R"):
            assert len(r) >= 3 and r[2], f"{r[0]} has no stated demand"


def test_every_rejected_example_is_a_real_cut_in_the_fixture():
    """The corpus must not invent rejections that never happened."""
    cut_text = " ".join(
        c["comment"] for c in _FIXTURE["comments"] if c["verdict"] == "cut"
    )
    for frag in (
        "200 characters",
        "break the pattern",
        "accepted list",
        "restored afterwards",
    ):
        assert frag in cut_text, (
            f"{frag!r} is in voice.md but was not actually cut"
        )


def test_every_accepted_example_is_a_real_keep_in_the_fixture():
    kept_text = " ".join(
        c["comment"] for c in _FIXTURE["comments"] if c["verdict"] == "keep"
    )
    for frag in (
        "hardcoded project name here",
        "isn't used below",
        "has nothing to interpolate",
        "no licence header on this one",
        "looks like three files got concatenated",
    ):
        assert frag in kept_text, (
            f"{frag!r} is in voice.md but was not actually kept"
        )


def test_remedy_examples_are_real_edits_not_invented():
    """All four remedy examples must be things the user actually wrote."""
    for frag in (
        "I suggest cleaning up all the headers",
        "You may add these default values",
    ):
        assert frag in TEXT
    # and the pre-edit form must match what the skill drafted
    drafted = " ".join(c["comment"] for c in _FIXTURE["comments"])
    assert "the licence header stops mid-sentence" in drafted
    assert "same default as the advisor module" in drafted


def test_corpus_counts_stated_in_prose_match_the_fixture():
    """'16 accepted / 4 rejected' must not drift from the data."""
    kept = sum(1 for c in _FIXTURE["comments"] if c["verdict"] == "keep")
    cut = sum(1 for c in _FIXTURE["comments"] if c["verdict"] == "cut")
    assert f"all {kept} accepted" in TEXT or f"{kept} accepted" in TEXT
    assert f"all {cut} rejected" in TEXT or f"{cut} rejected" in TEXT
