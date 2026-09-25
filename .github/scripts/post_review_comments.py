#!/usr/bin/env python3
"""
Turn the AI PR reviewer's JSON findings into one GitHub review payload.

Used by .github/workflows/_ai-pr-review-core.yml. The reviewer agent used to
post its own comments through the GitHub MCP server, which took three chained
tool calls; the model kept getting that sequence wrong, retried, and re-sent
its whole context each time, so a single review burned ~1.4M input tokens
across ~80 round trips and then hit the wall clock. The agent now returns
findings as JSON and this script does the posting, which makes the reviewer a
single request/response.

Moving the posting here buys line validation as well. GitHub rejects an ENTIRE
review if one comment names a line outside the diff, and a model picking a
plausible-but-wrong line is routine. Anchors are recomputed from the same diff
the reviewer was shown, so a bad position is dropped on its own instead of
taking every other comment down with it.

It is also where the reviewer's output is checked rather than trusted. The
prompt's severity gate ("only critical or high") used to be the only thing
standing between a wrong finding and a contributor's PR, and it worked by
making the reviewer say almost nothing — across eight recent PRs the three
lanes posted one or two comments between them, all from Correctness. Trading
that gate for a wider one is only safe if something downstream can tell a real
finding from an invented one, so four checks live here:

  WINDOW      Each finding quotes the diff lines it sits on. Those are matched
              against the diff itself. No match means the finding was
              fabricated and it is dropped; a match a few lines off means the
              finding is real and only its arithmetic was wrong, so the anchor
              is corrected instead of the finding being lost.
  CHEAPNESS   A finding whose own stated verification procedure admits it
              needs a second file, or a traced value, or an assumed input,
              costs the author more to check than it is worth. Dropped.
  DUPLICATES  Anything already said on this PR — by an earlier run of this
              lane, by one of the other three, or by a human — is suppressed.
              Four lanes re-running on every push otherwise repeat themselves.
  CONTEXT     A verified finding on a line the PR does not add cannot be an
              inline comment, but it is still true. It goes in the review body
              rather than into the log where nobody reads it.

Usage:
  python3 post_review_comments.py \
    --result agy_result.json \
    --diff pr_diff_used.txt \
    --label Correctness \
    --out review_payload.json \
    [--repo owner/name --pr 123]

--repo/--pr enable duplicate suppression; without them the other three checks
still run. --out is written ONLY when there is something to post, so the caller
can treat "file absent" as "nothing to post".

Every failure here is a CI fault, never the contributor's: the reviewer agent
returned something unusable, or a file this workflow wrote is unreadable. None
of it is caused by, or fixable from, the pull request under review — so they go
through ci_message.infra_fault, which annotates this checker rather than the
contributor's code.

Exit codes:
  0  payload written, or nothing worth posting
  2  CI fault — the reviewer's output could not be read as findings
"""

import argparse
import itertools
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ci_message import (
    EXIT_OK,
    guard,
    infra_fault,
    report_infra_fault,
)

# Imported, not re-implemented: prepare_review_diff already fixed this exact
# defect on its side, with tests. Duplicating is how the two drifted apart.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_review_diff import (
    _strip_side_prefix,
    _unquote_git_path,
)

CHECKER = "post_review_comments.py"

# Findings carrying this `source` came from house_rules_lane.py rather than
# from a model, and skip the window check.
#
# A model's findings are its own JSON, so a model CAN emit this key — which
# would let a prompt-injected reviewer waive the check that catches invented
# source. main() strips `source` from everything that arrives via --result
# before it is read, so the flag can only enter through --findings, a file no
# model writes.
TRUSTED_SOURCE = "checker"

# ---------------------------------------------------------- severity tags
#
# Every posted comment is prefixed `[CRITICAL] `, `[MAJOR] ` or `[MINOR] `.
# An inline comment does not show which lane produced it -- the lane name is
# only in the review body header -- so without this the author cannot tell a
# merge blocker from a nit without opening the review.
#
# The tag is derived from the LANE, never from a model's opinion of its own
# finding. A model asked to grade its own severity inflates, and a confident,
# precise, false "this is critical" is the most expensive comment this system
# can produce. `review-voice.md` therefore still forbids the model to write a
# severity label: it writes the prose, and the tag is prepended here.
#
# CRITICAL is reserved for the deterministic checker, and only for a finding
# it marked CI-failing. Those are the only ones where "this blocks your merge"
# is a fact rather than a guess.
TAG_CRITICAL = "[CRITICAL]"
TAG_MAJOR = "[MAJOR]"
TAG_MINOR = "[MINOR]"

# Lane name -> tag for anything the lane reports. Security and Correctness are
# the `blocker_lanes` in policy.yml, so MAJOR here and blocker there are the
# same judgement written twice; keep them in step.
LANE_TAGS = {
    "Security": TAG_MAJOR,
    "Correctness": TAG_MAJOR,
    "Maintainability": TAG_MINOR,
    "Hygiene": TAG_MINOR,
    "House Rules": TAG_MINOR,
}
# An unrecognised lane gets the tag that claims least.
DEFAULT_TAG = TAG_MINOR

# Any tag at the head of a body, with the space after it. Used to strip a tag
# back off before comparison, so a comment posted BEFORE tagging shipped still
# suppresses the tagged form of the same finding on the next push. Without it
# every open PR collects one duplicate of every comment already on it.
TAG_PREFIX = re.compile(r"^\s*\[(?:CRITICAL|MAJOR|MINOR)\]\s*", re.IGNORECASE)

# The value a finding's `ci` field carries when the deterministic checker says
# CI will fail on it. Must match `CI_FAIL` in
# `.agents/skills/github-pr-review/scripts/check_house_rules.py`, which is
# where the findings this reads are written. The two run in separate jobs, so
# this is a literal rather than an import -- and a literal that drifts silently
# downgrades every CI failure to a nit, which is why it is named here and the
# coupling is written down.
CI_FAIL = "fail"


def severity_tag(label: str, ci: object = None) -> str:
    """The tag for a finding from `label`, given its CI signal if it has one.

    Only the deterministic lane sets `ci`; a model finding passes None and is
    graded by its lane alone.
    """
    if str(ci or "").strip().lower() == CI_FAIL:
        return TAG_CRITICAL
    return LANE_TAGS.get(str(label or "").strip(), DEFAULT_TAG)


def apply_tag(body: str, tag: str) -> str:
    """`body` prefixed with `tag`, without stacking a second one."""
    return f"{tag} {TAG_PREFIX.sub('', str(body or '')).lstrip()}"


def _tagged(findings: list[dict], label: str) -> list[dict]:
    """Each finding with its body tagged and the internal `_ci` dropped.

    Comments and notes go through the same function on purpose. They differ in
    where they are rendered -- a comment is posted as JSON, a note becomes a
    bullet in the review body -- but neither should carry internal state, and
    handling them separately is how one of them ends up keeping it.
    """
    return [
        {
            **{k: v for k, v in f.items() if k != "_ci"},
            "body": apply_tag(
                f.get("body", ""), severity_tag(label, f.get("_ci"))
            ),
        }
        for f in findings
    ]


# The invisible signature every review we post carries, so a later run can
# recognise its own work and know which round it is on. Defined in
# review_budget.py, which is the reader; duplicated as a literal rather than
# imported because these two scripts run in different jobs and one must not
# start importing the other for a single constant.
REVIEW_MARKER = "<!-- adk-ai-review -->"

# Group 2 is the old-side length, groups 3/4 the new-side start and length.
# A length is absent for a one-line side ("@@ -1 +1 @@"), which means 1.
# Digit runs bounded like every other int() on parsed text in this file: a
# longer one is not a line count, and int() past 4300 digits raises.
HUNK_HEADER = re.compile(
    r"^@@ -(\d{1,12})(?:,(\d{1,12}))? \+(\d{1,12})(?:,(\d{1,12}))? @@"
)

# The fenced block, captured whole from its opening "[" to the closing
# fence. Regex cannot find where the array ends: a "]" inside a comment body
# is indistinguishable from the one that closes it, and anchoring on the LAST
# "]" in the block swallowed anything the model appended after the array.
# json's own parser draws that boundary in extract_findings instead.
FENCED_BLOCK = re.compile(r"```(?:json)?\s*(\[.*?)```", re.DOTALL)

# An opening fence with NO closing fence: a response truncated mid-array.
# Without this, FENCED_BLOCK cannot match and the bare-"[" fallback does not
# fire either (the response starts with the fence), so extract_findings raised
# and exited 2 -- a red check, discarding findings that had parsed fine. The
# repair and salvage paths downstream already handle a block that stops
# mid-object; nothing was reaching them.
UNTERMINATED_FENCED_BLOCK = re.compile(r"```(?:json)?\s*(\[.*)", re.DOTALL)

# A window row: "  42: os.system(cmd)". Both ":" and "|" are seen as the
# separator, and the leading whitespace is the model aligning its numbers.
# The digit run is bounded for the same reason as MAX_LINE_DIGITS: a longer
# one is not a line number, and int() on it raises rather than returning.
WINDOW_LINE = re.compile(r"^\s*(\d{1,12})\s*[:|]\s?(.*)$")

# The keys a finding is built from. Used to find where a string value ENDS
# when the model has left raw quotes inside it — see _repair_string_values.
FINDING_KEYS = "path|line|body|window|verify_steps"

# The opener of a string-valued field: '"window": "'.
STRING_FIELD_OPEN = re.compile(rf'"(?:{FINDING_KEYS})"\s*:\s*"')

# Where such a value ends: a quote followed by the next key, or by the end of
# the object. Anything else that looks like a terminator is part of the value.
STRING_FIELD_END = re.compile(rf'"(?=\s*(?:,\s*"(?:{FINDING_KEYS})"\s*:|\}}))')

# How much work to spend reading a malformed block before giving up on it.
# The choices multiply per field, so this is a ceiling on a combinatorial walk.
# Proving a reading UNIQUE means exhausting the walk, so the cap has to clear
# a real block with room to spare: the three-finding block in
# fixtures/malformed_findings_response.txt spends 6091 of it and the
# two-finding #2566 block 383. Exhausting the cap is treated as "ambiguous",
# never as "unique" — see _repaired_findings.
MAX_REPAIR_STEPS = 16384

# How many candidate ends to weigh for a single field. Every later field
# boundary is a candidate end for an earlier field, so this is what stops a
# many-finding block fanning out by its own width. The correct end is the
# first in every real case seen; the rest are only there to expose ambiguity,
# and a reading only reachable past the eighth is one this declines to make.
MAX_ENDS_PER_FIELD = 8

# String fields past which a block is not a findings array a review produced,
# and is not worth reading. A lane is asked for about five findings of four
# string fields each; the two real blocks here carry 8 and 12. Checked before
# the walk starts, because the walk holds a copy of the reading so far per
# branch: a runaway thousand-finding block cost 400MB of them before this.
MAX_REPAIR_FIELDS = 64

# Literal escape text a model writes where the diff holds the real character.
ESCAPE_SEQ = re.compile(
    r"\\u[0-9a-fA-F]{4}|\\U[0-9a-fA-F]{8}|\\x[0-9a-fA-F]{2}"
)

# Phrases in verify_steps that admit the reader must leave the anchored lines.
# Ported from .agents/skills/github-pr-review/scripts/verify_findings.py, where
# they were tuned against two real reviews.
NOT_CHEAP_MARKERS = re.compile(
    r"\btrace\b|\bassum\w*\b|consider the case|if an attacker|"
    r"\bsimulat\w*\b|\bimagine\b|run the code|execute\b|another file|"
    r"\bgrep the repo\b|across (the )?(repo|codebase)",
    re.IGNORECASE,
)

# SECURITY (b/555419958). A finding's `body` is written by a model whose only
# input is the PR diff, and on a fork PR every byte of that diff is chosen by
# its author. The body is then posted verbatim to a public pull request through
# the API, where GitHub's log masking does not reach. So the body is an
# attacker-influenced string on a public channel, and something has to say what
# shape it is allowed to be.
#
# These two limits say it. They are NOT the boundary — the empty agy tool
# allowlist in the workflow is, and the credential scan there is the precise
# check. What they do is make the channel too narrow to carry a credential
# without the model first chopping it up, which is the difference between an
# exfiltration that works on the first try and one that has to be engineered.
# Treat them as a speed bump with a second job, not as a proof.
#
# The second job is the honest one: the prompt asks for "1-2 plain sentences"
# and nothing enforced it. A model that returns an essay is misbehaving whether
# or not anyone is attacking, and a review comment nobody will read is not
# worth posting either.
#
# 600 characters is roughly 100 words — four or five sentences, well clear of
# the two the prompt asks for, and clear too of a grouped finding that has to
# say how many instances it covers.
MAX_BODY_CHARS = 600

# No file has 10^12 lines. The bound exists because int() on a string of more
# than 4300 digits raises ValueError, which escapes as a CI fault and
# discards every finding in the lane.
MAX_LINE_DIGITS = 12

# The longest unbroken non-whitespace run a body may contain. Calibrated
# against real data, not guessed: the longest path in this repository is 123
# characters (java/agents/time-series-forecasting/...ForecastingAgent.java),
# and quoting a path is exactly what a legitimate finding does. So the cap has
# to clear that with room, or the check costs real reviews. 160 does, while
# still refusing a credential pasted in one piece — the ADC file this runner
# holds is ~1KB and its embedded token alone runs past 200.
MAX_UNBROKEN_RUN = 160

# How far an anchor may be wrong before a window match stops being believable.
MAX_DRIFT = 3

# Below this many characters, a window line must equal the diff line exactly.
# A short line is a prefix of almost anything and matches spuriously.
MIN_PREFIX = 8

# A duplicate is the same line or within this many of it. Tight on purpose:
# widening it starts eating genuinely new findings near an old comment.
PROXIMITY = 2

# Token overlap above which two comments are saying the same thing.
SIMILARITY = 0.55

# How much of a note's vocabulary must already appear in an earlier review
# body for it to count as already said. High, because a body carries every
# note from its round and a low bar would swallow unrelated findings.
NOTE_CONTAINMENT = 0.9

# A verdict — a resolved thread, a hidden comment, a 👎 — is recorded and named
# in the log, but it does NOT widen the suppression threshold.
#
# It used to, at 0.35. Two things were wrong with that. Anyone can react to a
# public comment and a PR author can resolve threads on their own PR, so
# "maintainer verdict" was not maintainer-only: the reviewed party could
# switch off findings about their own code, which is precisely backwards for
# the Security lane. And 0.35 against a containment metric is very wide — two
# shared tokens out of four — so it suppressed genuinely new findings.
#
# The verdict still earns its keep: the reason string tells whoever reads the
# log why a comment was dropped, and a resolved thread already blocks its own
# line through the proximity zones.
#
# What none of this can do: a DELETED comment leaves nothing behind in either
# API, so that finding can come back. Closing it needs stored state, which
# this system deliberately does not have.

_STOPWORDS = set(
    """a an the is are was were be been being this that these those it its of to
    in on for with from by at as and or not no any some all each every into out
    here there which what when where line lines file files code value values
    never only still also just even than then them they their should does did
    has have""".split()
)


class ReviewerOutputError(Exception):
    """The reviewer returned something this script cannot read as findings."""


def _strip_diff_prefix(path: str) -> str:
    """Drop git's `a/`/`b/` diff prefix from a path."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _header_path(target: str) -> str:
    """A `+++ ` header's path, as GitHub will report it.

    With `core.quotePath` on -- the default -- git quotes and C-escapes any
    path holding a space, a quote or a non-ASCII byte: `+++ "b/my recipe/x.py"`.
    The quotes then stay in the key AND the `b/` prefix sits inside them, so
    the prefix strip never fires, and every finding in that file is dropped as
    unanchorable. Unquote first, strip second -- the order is load-bearing.
    """
    return _strip_side_prefix(_unquote_git_path(target))


def _resolve_path(reported: str, anchors: dict[str, set[int]]) -> str:
    """Match the model's path against the paths the diff actually names.

    The prompt asks for the path without git's `b/` prefix and models
    sometimes leave it on. Stripping unconditionally is not safe either: a
    repository with a top-level `b/` directory has real paths beginning that
    way, and stripping would turn `b/pkg/x.py` into `pkg/x.py` and drop every
    finding in it. Prefer the path as given, fall back to the stripped form.
    """
    stripped = _strip_diff_prefix(reported)
    if reported not in anchors and stripped in anchors:
        return stripped
    return reported


def walk_right_side(
    diff: str,
) -> tuple[dict[str, set[int]], dict[str, dict[int, str]]]:
    """Read the diff's RIGHT side once: (added anchors, line text).

    The first return value is the set of new-file lines each file ADDS, which
    is what GitHub will accept as an inline anchor. The second is the text of
    every new-file line the diff shows — added and context alike — which is
    what a finding's quoted window is checked against.

    Both come from one walk because they are the same traversal, and two
    traversals that disagree about where a hunk starts would put a comment on
    a line whose text was read from somewhere else.

    Only `+` lines are valid anchors on the RIGHT side of a review, so the
    first value doubles as enforcement of the prompt's "added lines only"
    rule. Context lines are still worth indexing: a finding anchored to one is
    real but unpostable inline, and belongs in the review body.

    Hunk lengths are tracked rather than sniffing line prefixes, because the
    two are not distinguishable by prefix alone. An added line whose text
    begins with "++ " produces the row "+++ ...", identical in shape to a
    file header; prefix-sniffing mistook it for one, set the path to the
    line's own text, and silently lost every remaining anchor in that file.
    Inside a hunk the line counts say exactly how many rows belong to it, so
    a header can only be recognised when we are between hunks.

    A hunk header that does not parse leaves us outside any hunk, so its
    lines contribute no anchors at all. That is deliberate: guessing a start
    line would anchor comments onto real but WRONG lines, which is worse than
    dropping them, because a wrong anchor still passes validation and gets
    posted.
    """
    anchors: dict[str, set[int]] = {}
    text: dict[str, dict[int, str]] = {}
    path: str | None = None
    new_line = 0
    old_remaining = 0
    new_remaining = 0

    for row in diff.splitlines():
        if old_remaining <= 0 and new_remaining <= 0:
            # Between hunks: the only place a row can be a file header.
            if row.startswith("+++ "):
                target = row[4:].strip()
                path = None if target == "/dev/null" else _header_path(target)
                continue
            header = HUNK_HEADER.match(row)
            if header:
                old_remaining = int(header.group(2) or 1)
                new_line = int(header.group(3))
                new_remaining = int(header.group(4) or 1)
            # Everything else between hunks ("diff --git", "index", "--- a/x",
            # "Binary files ... differ") carries no line numbering.
            continue

        # Inside a hunk. "\ No newline at end of file" annotates the previous
        # row and belongs to neither side's count.
        if row.startswith("\\"):
            continue
        if row.startswith("+"):
            if path is not None:
                anchors.setdefault(path, set()).add(new_line)
                text.setdefault(path, {})[new_line] = row[1:]
            new_line += 1
            new_remaining -= 1
        elif row.startswith("-"):
            old_remaining -= 1
        else:
            if path is not None:
                text.setdefault(path, {})[new_line] = row[1:]
            new_line += 1
            new_remaining -= 1
            old_remaining -= 1

    return anchors, text


def added_line_anchors(diff: str) -> dict[str, set[int]]:
    """The new-file lines each path adds. See walk_right_side."""
    return walk_right_side(diff)[0]


def restrict_to_pr(
    anchors: dict[str, set[int]], pr_diff: str
) -> dict[str, set[int]]:
    """Keep only the anchors the PR's own base...head diff also adds.

    An incremental diff (last reviewed commit...head) taken across a merge of
    the base branch marks everything that landed on base in between as
    added. Those lines are not in the PR's diff, so GitHub rejects a comment
    on one with HTTP 422, and when every comment in a run is such a line the
    post job fails. Both diffs end at head, so new-file line numbers agree
    and a plain intersection is enough. A dropped anchor is not lost: its
    finding still verifies against the line text and goes in the review body.

    Every path keeps its key, even with no anchors left, so path resolution
    against the reviewed diff behaves as before.
    """
    pr_anchors = added_line_anchors(pr_diff)
    return {
        path: lines & pr_anchors.get(path, set())
        for path, lines in anchors.items()
    }


def extract_findings(response: str) -> list:
    """Pull the findings array out of the reviewer's text response.

    The prompt asks for a bare fenced block and nothing else, but a stray
    sentence either side is the likeliest way for the model to drift, and
    re-prompting costs another full model call.

    The inside of the block drifts too: a reviewer answered PR #2547 with
    "[]\\n[]" — two arrays in one fence — which is not a JSON document, so
    parsing the block as one value failed the run as a CI fault over a review
    that had simply found nothing. Each top-level array is decoded in turn and
    their findings concatenated, and a decode failure after the first array
    has parsed ends the scan rather than the review, because trailing prose is
    drift and not a reason to throw away findings already in hand. Only the
    first array failing to parse is fatal — at that point there is nothing to
    post and nothing to infer.

    The LAST fenced block wins, not the first. The prompt now asks the
    reviewer to work through the diff in plain text before answering, because
    a model told to emit nothing but JSON does no reasoning and finds
    correspondingly little. That scan quotes diff rows and sometimes brackets
    them, so the first fenced array in the response is no longer reliably the
    answer — the prompt says the findings block comes last, and this reads it
    from the same end.

    A block that will not parse AT ALL is repaired, and failing that salvaged
    finding by finding, rather than thrown away whole. Findings quote source
    verbatim in `window`, and source is full of double quotes: a reviewer
    emitted

        "window": "  211:     assert res["success"] is True\\n ..."

    which is one unescaped pair away from valid and killed an entire review
    of three good findings.

    Repair runs first because it recovers the WHOLE block, where salvage keeps
    only the findings that happened to be well-formed already. Salvage alone
    is not enough: the same quoting habit corrupts every window drawn from the
    same file, so on a quote-dense diff there are no well-formed siblings left
    to keep. Repair declines anything it cannot read one single way, so salvage
    remains the fallback for a block that is malformed in some other way, and
    one malformed finding still costs only that finding.
    """
    matches = list(FENCED_BLOCK.finditer(response))
    # All three branches guarantee a block starting at "[", so the scan below
    # always decodes at least once.
    block = matches[-1].group(1) if matches else None
    if block is None and response.strip().startswith("["):
        block = response.strip()
    if block is None:
        # No closing fence: hand the unterminated remainder to the same
        # salvage path a malformed block takes.
        truncated = UNTERMINATED_FENCED_BLOCK.search(response)
        if truncated is not None:
            block = truncated.group(1)
            print(
                "  findings block has no closing fence (output was likely "
                "truncated); attempting to recover complete findings from it"
            )
    if block is None:
        raise ReviewerOutputError("response contained no JSON findings array")

    decoder = json.JSONDecoder()
    findings: list = []
    parsed_any = False
    index = 0

    while (start := block.find("[", index)) != -1:
        try:
            array, index = decoder.raw_decode(block, start)
        except ValueError as exc:
            # ValueError, not JSONDecodeError: a bare integer literal of more
            # than 4300 digits makes json's own number parser raise the base
            # class, which slipped past the narrower handler and escaped as a
            # CI fault before any validation had run. JSONDecodeError is a
            # ValueError, so this still catches everything it did.
            if parsed_any:
                break
            # Repair before salvage: it recovers every finding in the block,
            # where salvage keeps only the ones that were already well-formed.
            repaired = _repaired_findings(block)
            if repaired is not None:
                print(
                    f"  findings block is malformed JSON ({exc}); "
                    f"repaired {len(repaired)} finding(s) in it"
                )
                return _dedupe(repaired)
            salvaged = _salvage_findings(block, decoder)
            if salvaged:
                print(
                    f"  findings block is malformed JSON ({exc}); "
                    f"salvaged {len(salvaged)} finding(s) from it"
                )
                return _dedupe(salvaged)
            raise ReviewerOutputError(
                f"findings block is not valid JSON: {exc}"
            ) from exc

        parsed_any = True
        findings.extend(array)

    return _dedupe(findings)


def _dedupe(findings: list) -> list:
    """A repeated block repeats its findings, and posting the same note twice
    on the same line is worse than posting it once."""
    seen: set[str] = set()
    unique: list = []
    for finding in findings:
        key = json.dumps(finding, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def _escape_value(value: str) -> str:
    """Re-escape a value's double quotes, normalising first so it is idempotent."""
    return value.replace('\\"', '"').replace('"', '\\"')


def _repair_readings(block: str, budget: list[int]):
    """Every way of escaping the block, one per choice of where values end.

    A value's end is looked for from the SCHEMA — the quote that precedes the
    next known key, or the one that closes the object — because the obvious
    cheap rule, "a quote followed by `,`, `]` or `}`", is wrong on the very
    input this exists for: `[ -f "yarn.lock" ]` puts a `]` right after the
    stray quote and would end the array mid-string.

    The schema rule is not unambiguous either, so the first MAX_ENDS_PER_FIELD
    candidate ends are branched on rather than just the first, and the caller
    decides between the readings they produce.

    Depth-first over an explicit stack, not recursion: one field is one frame,
    and a runaway model answering with hundreds of findings would exceed the
    interpreter's limit. That mattered — a RecursionError escapes the caller
    entirely, so a block salvage could have read went to a CI fault instead.
    `budget` is charged for every reading completed and every branch taken, so
    the walk is capped whatever shape it takes: the combinations multiply per
    field, and a block nobody can read is not worth an unbounded search.
    """
    stack: list[tuple[int, str]] = [(0, "")]

    while stack:
        if budget[0] <= 0:
            return
        index, prefix = stack.pop()

        opener = STRING_FIELD_OPEN.search(block, index)
        if opener is None:
            budget[0] -= 1
            yield prefix + block[index:]
            continue

        head = prefix + block[index : opener.end()]
        ends = list(
            itertools.islice(
                STRING_FIELD_END.finditer(block, opener.end()),
                MAX_ENDS_PER_FIELD,
            )
        )
        if not ends:
            # No end to find, so there is nothing to repair past this point.
            budget[0] -= 1
            yield head + block[opener.end() :]
            continue

        # Reversed, so the earliest end is popped first and the cheapest
        # reading is the one a budget-limited walk is most likely to reach.
        #
        # Each push copies the reading so far, so pushes are charged to the
        # budget as well. Counting only the steps that reached a leaf let a
        # wide block spend minutes building readings it never finished: every
        # later field boundary is a candidate end for every earlier field, so
        # a 400-finding block pushed ~1600 copies per step and ran for over
        # two minutes before this.
        for end in reversed(ends):
            budget[0] -= 1
            value = block[opener.end() : end.start()]
            stack.append((end.end(), f'{head}{_escape_value(value)}"'))


def _repaired_findings(block: str) -> list | None:
    """The findings a malformed block yields, when exactly one reading fits.

    Findings quote source verbatim in `window`, and source is full of double
    quotes, so the model regularly emits

        "window": "  252:   elif [ -f "yarn.lock" ]; then\\n ..."

    which is not JSON. `_salvage_findings` cannot help: it drops a finding it
    cannot decode, and one quoting habit corrupts EVERY window drawn from the
    same file, so a quote-dense diff loses the whole review rather than one
    finding of it. That is what happened on #2566, where both findings quoted
    shell out of a workflow file and the run died as a CI fault.

    Escaping alone is not enough, because where a value ENDS can be genuinely
    ambiguous. A window that quotes findings-shaped source — which this repo's
    own tests contain — offers a second reading in which the value stops early
    and the source's own `"line":` and `"body":` become the finding's fields:

        "window": "  919: [{"path": "a.py", "line": 1, "body": "real"}]"

    reads just as well as a finding anchored at line 1 with the body "real".
    That parses, so parsing cannot be the test. Posting source scraped out of
    a window as though it were a review comment is worse than posting nothing,
    so a block is repaired ONLY when exactly one reading survives; anything
    else falls through to salvage and, failing that, to the CI fault. The two
    real regressions this exists for both have exactly one reading.
    """
    fields = sum(1 for _ in STRING_FIELD_OPEN.finditer(block))
    if fields > MAX_REPAIR_FIELDS:
        return None

    readings: dict[str, list] = {}
    decoder = json.JSONDecoder()
    budget = [MAX_REPAIR_STEPS]

    for candidate in _repair_readings(block, budget):
        try:
            array, _ = decoder.raw_decode(candidate, candidate.find("["))
        except ValueError:
            continue
        if isinstance(array, list) and array:
            readings.setdefault(json.dumps(array, sort_keys=True), array)
            if len(readings) > 1:
                return None

    # An exhausted budget means readings beyond the ones seen may exist, so
    # "exactly one" has not actually been established.
    if budget[0] <= 0 or len(readings) != 1:
        return None
    return next(iter(readings.values()))


def _salvage_findings(block: str, decoder: json.JSONDecoder) -> list[dict]:
    """Decode findings one object at a time, skipping ones that will not parse.

    Only objects carrying the fields a finding must have are kept. Advancing
    past a failure means the next `{` tried may be one inside a string, so
    something has to reject the resulting debris; requiring `path` and `body`
    does, and every real finding has both.
    """
    salvaged: list[dict] = []
    index = 0
    while (start := block.find("{", index)) != -1:
        try:
            candidate, index = decoder.raw_decode(block, start)
        except ValueError:
            # ValueError, like its two siblings: a >4300-digit integer
            # literal makes json's number parser raise the base class, and
            # this is the fallback path the other two hand off to.
            index = start + 1
            continue
        if (
            isinstance(candidate, dict)
            and str(candidate.get("path") or "").strip()
            and str(candidate.get("body") or "").strip()
        ):
            salvaged.append(candidate)
    return salvaged


def _coerce_line(value: object) -> int | None:
    """Read a line number, accepting 42 and "42" but nothing lossy.

    `bool` is an `int` subclass, so a stray `"line": true` would otherwise
    become line 1 — a real anchor on an unrelated line.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if (
        isinstance(value, str)
        and value.strip().isdecimal()
        # Python 3.11 caps int(str) at 4300 digits and raises ValueError past
        # it. A line number is never more than a handful; anything longer is
        # not a line number, and letting int() decide costs the whole review.
        and len(value.strip()) <= MAX_LINE_DIGITS
    ):
        return int(value.strip())
    return None


def _norm(text: str) -> str:
    """Reduce a source line to a comparable ASCII skeleton.

    A model writes an emoji as the literal escape text `\\u26a0` where the diff
    holds the real character, and round-tripping through unicode_escape does
    not reconcile the two — it mojibakes the diff side instead, so every line
    holding a non-ASCII character then looks fabricated.

    Comparing ASCII skeletons sidesteps that: drop escape sequences, drop
    non-ASCII, drop whitespace. Real fabrication still differs in the ASCII
    text, which is where the substance of a line of code lives.
    """
    text = ESCAPE_SEQ.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text if c.isascii() and not c.isspace())


def _window_rows(window: object) -> list[tuple[int, str]]:
    """[(claimed_line, text)] from a finding's quoted window."""
    rows: list[tuple[int, str]] = []
    for raw in str(window or "").split("\n"):
        match = WINDOW_LINE.match(raw)
        if not match:
            continue
        # Models abbreviate a long line with an ellipsis. A Unicode one
        # vanishes in _norm as non-ASCII, but an ASCII "..." survives and
        # would turn a valid prefix into a mismatch.
        rows.append(
            (
                int(match.group(1)),
                re.sub(r"(\.{3}|\u2026)\s*$", "", match.group(2)),
            )
        )
    return rows


def _window_matches(
    rows: list[tuple[int, str]], lines: dict[int, str], offset: int
) -> int:
    """How many window rows match the diff at this offset; 0 if any conflicts.

    A row whose line the diff does not show is not a conflict — the model may
    quote a couple of lines either side of a hunk boundary — but it does not
    count towards the match either.
    """
    matched = 0
    for number, claimed in rows:
        actual = lines.get(number + offset)
        if actual is None:
            continue
        want, have = _norm(claimed), _norm(actual)
        if not want:
            continue
        if want == have:
            matched += 1
        elif min(len(want), len(have)) >= MIN_PREFIX and (
            want.startswith(have) or have.startswith(want)
        ):
            # Prefix, not substring: a one-character line is a substring of
            # almost anything and matches spuriously.
            matched += 1
        else:
            return 0
    return matched


def _snap_to_window(line: int, rows: list[tuple[int, str]], offset: int) -> int:
    """Pull an anchor that sits outside its own verified window back into it.

    The prompt requires the anchored line to be one of the window's rows, so
    the two disagreeing means the model quoted the right code and then named a
    different line beside it. The window has been checked against the diff and
    the anchor has not, so the verified side wins.

    Without this the comment lands a line or two from the thing it is about —
    a placeholder value flagged on the `return` statement underneath it, say —
    which reads as carelessness even when the finding itself is right.
    """
    claimed = [number + offset for number, _text in rows]
    if line in claimed:
        return line
    return min(
        claimed, key=lambda candidate: (abs(candidate - line), candidate)
    )


def implausible_body(body: str) -> str | None:
    """Why this body is not shaped like a review comment, or None if it is.

    See MAX_BODY_CHARS. Deliberately says nothing about what a body may
    CONTAIN: a keyword deny-list ("external_account", "Bearer", ...) reads as
    security and is not, because the thing being filtered is written by a model
    that the same input can instruct to reword, encode or spell out whatever
    the list names. Shape is the property an attacker cannot negotiate away —
    a credential is long, or it is chopped into pieces small enough that
    reassembling it is its own problem.
    """
    if len(body) > MAX_BODY_CHARS:
        return (
            f"body is {len(body)} chars, over the {MAX_BODY_CHARS}-char limit"
        )

    longest = max((len(run) for run in body.split()), default=0)
    if longest > MAX_UNBROKEN_RUN:
        return (
            f"body contains a {longest}-char unbroken run, over the "
            f"{MAX_UNBROKEN_RUN}-char limit"
        )

    return None


def check_window(
    finding: dict, line: int, lines: dict[int, str]
) -> tuple[bool, int, str]:
    """(verified, line, reason) for a finding's quoted window.

    A window matching at a CONSISTENT offset is arithmetic drift, not
    fabrication: the finding is real and only the model's line counting was
    wrong, so the anchor is corrected rather than the finding thrown away.
    Requiring two matching rows before accepting a shift keeps a single
    coincidental match from moving a comment onto unrelated code.

    Counting new-file line numbers out of a unified diff by hand is the part
    of this job a model is worst at, and it is the part that decides whether a
    correct finding is postable at all. Both repairs here exist because
    dropping a finding over its arithmetic throws away work that was right.
    """
    rows = _window_rows(finding.get("window"))
    if not rows:
        return False, line, "no window supplied"

    if _window_matches(rows, lines, 0):
        snapped = _snap_to_window(line, rows, 0)
        if snapped != line:
            return (
                True,
                snapped,
                f"anchor {line} was outside its own window; moved to {snapped}",
            )
        return True, line, "window matches the diff"

    for step in range(1, MAX_DRIFT + 1):
        for offset in (step, -step):
            if _window_matches(rows, lines, offset) >= 2:
                return (
                    True,
                    _snap_to_window(line + offset, rows, offset),
                    f"window matched {offset:+d} lines away; anchor corrected",
                )

    number, claimed = rows[0]
    actual = lines.get(number, "")
    return (
        False,
        line,
        f"window says {claimed.strip()[:40]!r}, diff has {actual.strip()[:40]!r}",
    )


def _similarity(a: set[str], b: set[str]) -> float:
    """Jaccard, not containment.

    `len(a & b) / min(len(a), len(b))` is containment: a short comment fully
    contained in a longer one scores 1.0 however much more the longer one
    says. That made a 5-defect comment "the same" as a 1-defect one, and let a
    4-token finding be suppressed by any comment sharing two of its words.
    Dividing by the union asks the question actually intended — are these two
    comments about the same thing.
    """
    union = a | b
    return len(a & b) / len(union) if union else 0.0


# The suffix group_repeats appends. Stripped before any comparison: grouping
# runs after the duplicate check, so the body that gets STORED carries five
# extra tokens the next round's body does not, which dropped Jaccard under
# the bar and re-posted the grouped comment on every push.
_GROUP_NOTE = re.compile(
    r"\n*\(Same thing in \d+ other places? in this review\.\)\s*$"
)


def _base_body(text: str) -> str:
    """A comment body reduced to what the finding actually said.

    Strips the grouping suffix and any leading severity tag. The tag matters
    because it is added at post time, so the SAME finding is untagged in a
    comment posted before tagging shipped and tagged in one posted after.
    Comparing those two forms as written makes every comment already on an
    open PR look new, and each one is posted a second time -- the convergence
    failure the whole comment budget exists to prevent.
    """
    text = _GROUP_NOTE.sub("", str(text or ""))
    return TAG_PREFIX.sub("", text).strip()


def _tokens(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z_][a-z_0-9]{3,}", str(text).lower())
        if word not in _STOPWORDS
    }


# A bullet in one of our review bodies, as build_payload renders it:
#     - `path/to/file.py:42` — the finding text
_NOTE_BULLET = re.compile(r"^- `([^`]+):(\d{1,12})` — (.*)$")


def _notes_in(review_body) -> list[dict]:
    """Our own notes, parsed back out of a review body we posted.

    Each becomes an ordinary entry with its own path and text, so the
    duplicate check compares like with like instead of hunting substrings in
    one large string. That substring approach was the root of four
    consecutive rounds of defects: the path and the body could be matched by
    two DIFFERENT bullets, and every transformation applied on the way in
    (defanging, whitespace flattening, path truncation) had to be replayed
    exactly on the way out or a note repeated on every push forever.
    """
    notes = []
    for raw in str(review_body or "").split("\n"):
        match = _NOTE_BULLET.match(raw.strip())
        if match:
            notes.append(
                {
                    "kind": "note",
                    "path": match.group(1),
                    "line": int(match.group(2)),
                    "body": match.group(3).strip(),
                }
            )
    return notes


def _our_review(item: dict) -> bool:
    """A non-empty review body that WE posted.

    The author check is the whole point. Review bodies are used for
    containment matching — "did we already say this in an earlier round" — and
    a body is large, so containment against an arbitrary one is easy to
    satisfy. Without this filter a PR author could paste a wall of plausible
    text into a review of their own PR and suppress most of what the next
    round would have said: the same hole the 0.35 verdict threshold was
    removed for, rebuilt wider.

    Only an App or Actions token can post as an account of type Bot.
    """
    if not (item.get("body") or "").strip():
        return False
    if str((item.get("user") or {}).get("type") or "") != "Bot":
        return False
    body = str(item["body"]).lstrip()
    return REVIEW_MARKER in body or body.startswith("Automated **")


def fetch_existing_comments(repo: str, pr: int) -> list[dict]:
    """Everything already said on this PR, inline and top-level.

    Four lanes review every push, so without this the same observation is
    re-posted on every `synchronize` and each lane repeats whatever the other
    three found in the overlap between their remits.

    A failure here is not fatal. Suppression improves the review; it is not a
    precondition for having one, and losing a whole review to a transient API
    error is a worse outcome than posting a comment twice.

    Those failures are logged as plain lines, not `::warning::` annotations,
    for the same reason the dropped findings in main() are: a contributor
    reading their PR can neither act on this nor be helped by seeing it.
    """
    existing: list[dict] = []
    for endpoint, kind in (
        (f"repos/{repo}/pulls/{pr}/comments", "inline"),
        (f"repos/{repo}/issues/{pr}/comments", "top-level"),
        # Review BODIES, which is where notes live — findings on lines the PR
        # does not change. Without this endpoint `already_raised` cannot see a
        # note it posted last round, so a lane that is never capped and never
        # skipped (House Rules) repeats the identical body on every push
        # forever. That is the non-convergence this whole change exists to
        # end, hiding in the one class of finding nobody was deduplicating.
        (f"repos/{repo}/pulls/{pr}/reviews", "review-body"),
    ):
        try:
            proc = subprocess.run(
                ["gh", "api", "--paginate", f"{endpoint}?per_page=100"],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"  could not read {kind} comments: {exc}")
            continue
        if proc.returncode != 0:
            print(
                f"  could not read {kind} comments: {proc.stderr.strip()[:200]}"
            )
            continue
        # --paginate concatenates one JSON array per page, so decode them in
        # sequence rather than parsing the output as a single document.
        decoder = json.JSONDecoder()
        text, index = proc.stdout, 0
        while (start := text.find("[", index)) != -1:
            try:
                batch, index = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                break
            if kind == "review-body":
                for item in batch:
                    if not _our_review(item):
                        continue
                    existing.append(
                        {"kind": "review-body", "body": item.get("body")}
                    )
                continue
            for item in batch:
                # OUR review bodies only. This call site has now been the
                # bug three times running: a review body is large, so
                # containment against an arbitrary one is easy to satisfy,
                # and a PR author pasting a wall of plausible text into a
                # self-review suppresses most of what the next round would
                # say. The helper without this line is decoration.
                if kind == "review-body" and not _our_review(item):
                    continue
                existing.append(
                    {
                        "kind": kind,
                        "id": item.get("id"),
                        "user": item.get("user") or {},
                        "path": item.get("path"),
                        "line": item.get("line"),
                        "original_line": item.get("original_line"),
                        "body": item.get("body") or "",
                    }
                )

    verdicts = fetch_verdicts(repo, pr)
    if verdicts:
        judged = 0
        for comment in existing:
            verdict = verdicts.get(comment.get("id"))
            if verdict:
                comment["verdict"] = verdict
                judged += 1
        print(f"  {judged} of them carry a maintainer's verdict.")
    return existing


# Resolution, minimisation and reactions are GraphQL-only: none of the three
# appears on a REST review comment. Each is a maintainer saying something about
# a comment rather than about the code, and that is the only durable record of
# a rejected finding this system can read.
_VERDICT_QUERY = """
query($owner:String!, $name:String!, $pr:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$pr) {
      reviewThreads(first:100) {
        nodes {
          isResolved
          comments(first:50) {
            nodes {
              databaseId
              isMinimized
              reactions(first:1, content:THUMBS_DOWN) { totalCount }
            }
          }
        }
      }
    }
  }
}
"""


def fetch_verdicts(repo: str, pr: int) -> dict[int, str]:
    """{comment id: what the maintainer did to it}.

    Best-effort, exactly like fetch_existing_comments: an unreadable verdict
    costs a slightly noisier review, and failing the whole run over it would
    cost the review entirely.
    """
    owner, _, name = repo.partition("/")
    try:
        proc = subprocess.run(
            [
                "gh",
                "api",
                "graphql",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"pr={pr}",
                "-f",
                f"query={_VERDICT_QUERY}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  could not read review verdicts: {exc}")
        return {}
    if proc.returncode != 0:
        print(f"  could not read review verdicts: {proc.stderr.strip()[:200]}")
        return {}
    try:
        threads = json.loads(proc.stdout)["data"]["repository"]["pullRequest"][
            "reviewThreads"
        ]["nodes"]
    except (ValueError, KeyError, TypeError) as exc:
        print(f"  could not read review verdicts: {exc}")
        return {}

    verdicts: dict[int, str] = {}
    for thread in threads or []:
        # GraphQL returns a null node for anything the token cannot see. The
        # docstring above promises this is best-effort; an AttributeError here
        # escapes to guard() and turns "slightly noisier review" into a CI
        # fault on the contributor's PR.
        if not isinstance(thread, dict):
            continue
        resolved = bool(thread.get("isResolved"))
        for comment in (thread.get("comments") or {}).get("nodes") or []:
            if not isinstance(comment, dict):
                continue
            cid = comment.get("databaseId")
            if not cid:
                continue
            # Most specific verdict wins: a 👎 is someone saying the comment
            # was wrong, where resolving can also mean it was acted on.
            if (comment.get("reactions") or {}).get("totalCount"):
                verdicts[cid] = "thumbed this down"
            elif comment.get("isMinimized"):
                verdicts[cid] = "hid"
            elif resolved:
                verdicts[cid] = "resolved"
    return verdicts


def build_exclusions(
    existing: list[dict],
) -> tuple[dict[str, dict[int, dict]], list[tuple[set[str], dict]]]:
    """(line zones, tokenised bodies) from comments already on the PR."""
    zones: dict[str, dict[int, dict]] = {}
    texts: list[tuple[set[str], dict]] = []
    # Expand any review body into the notes it carries, so a caller that
    # hands one over whole gets the same treatment as the fetch path. One
    # place does the parsing; everything downstream sees individual notes
    # with their own path and text.
    expanded: list[dict] = []
    for comment in existing or []:
        if comment.get("kind") == "review-body":
            expanded.extend(_notes_in(comment.get("body")))
        else:
            expanded.append(comment)
    for comment in expanded:
        if (comment.get("body") or "").strip():
            # Compared without the grouping suffix: grouping runs AFTER the
            # duplicate check, so the stored body carries five tokens the
            # next round's body will not, and the difference was enough to
            # drop a short body under the bar and re-post it every push.
            stripped = {**comment, "body": _base_body(comment["body"])}
            texts.append((_tokens(stripped["body"]), stripped))
        if comment.get("kind") != "inline":
            continue
        path = comment.get("path")
        if not path:
            continue
        # `line` is nulled by GitHub once a thread goes outdated, and only
        # `original_line` survives. Both are claimed so a moved comment still
        # blocks the place it was originally made about.
        for anchor in (comment.get("line"), comment.get("original_line")):
            if not anchor:
                continue
            for delta in range(-PROXIMITY, PROXIMITY + 1):
                zones.setdefault(path, {}).setdefault(anchor + delta, comment)
    return zones, texts


def _same_path(comment: dict, path: str) -> bool:
    """Is this comment about `path`?

    Compares the rendered spelling too: a note's path is recovered from the
    bullet we wrote, which went through `_safe_span` — so a path carrying a
    backtick, or longer than the 160-character cap, never matched itself and
    its note repeated on every push.
    """
    stored = str(comment.get("path") or "")
    return stored in (path, _safe_span(path))


def already_raised(
    path: str,
    line: int,
    body: str,
    zones: dict[str, dict[int, dict]],
    texts: list[tuple[set[str], dict]],
    trusted: bool = False,
) -> str:
    """Why this finding repeats something already on the PR, or ""."""
    # Both sides of every comparison below go through `_base_body`, so the
    # two are reduced the same way by construction rather than by two pieces
    # of code that have to be kept in agreement.
    #
    # Callers pass an untagged body today, because tagging is the last step
    # before the payload is written -- but that is an ordering guarantee, and
    # if it ever stops holding the failure is silent: every comment already on
    # the PR is posted a second time.
    body = _base_body(body)
    if zones.get(path, {}).get(line) and not trusted:
        # Position is good evidence of repetition for a MODEL finding: two
        # comments on one line are usually the same observation restated.
        # For the deterministic lane it is not — several rules anchor at
        # line 1 when they cannot locate their subject, so one posted
        # comment there would silence every other rule for that file on
        # every later round. Those findings are distinguished by their text
        # below, which is exact for a checker.
        return "already commented on this line"

    mine = _tokens(body)
    if len(mine) < 4:
        # Too few distinctive words for a similarity comparison, but an exact
        # repeat is still a repeat. Both shapes have to be checked:
        # `"api_key" is not UPPER_SNAKE_CASE` and `a committed private key`
        # tokenise to three, and the exempt House Rules lane re-posts them on
        # every push unless something stops it.
        for _tokens_unused, comment in texts:
            if not _same_path(comment, path):
                continue
            # Both spellings. A note is stored flattened by `_safe_line`; an
            # inline comment is stored as written. Comparing only one of them
            # fixed notes and broke comments, and a short body carrying a
            # newline or a double space then repeated on every push.
            if _base_body(comment.get("body")) in (
                body.strip(),
                _safe_line(body).strip(),
            ):
                return "identical to a comment already on this PR"
        return ""
    for tokens, comment in texts:
        if len(tokens) < 4:
            continue
        # For a CHECKER finding, same file only. Its bodies come from one
        # format string per rule, so `[build-system] missing or lacks
        # requires / build-backend` is byte-identical for every recipe and
        # CI-failing: path-blind, the first author was told and every one
        # after them silenced.
        #
        # For a MODEL finding the opposite is wanted, and is why this leg
        # exists: four lanes review the same PR with overlapping remits and
        # phrase one defect four ways, so the same observation elsewhere
        # SHOULD suppress. Prose bodies do not collide by construction the
        # way a format string does.
        if trusted and not _same_path(comment, path):
            # Note the missing `comment.get("path") and`. An issue comment
            # carries no path, so that clause skipped the guard for the one
            # class an arbitrary user controls: a single top-level comment
            # quoting a checker's message -- they are format strings in a
            # public file -- silenced that rule on every file of every later
            # push. A checker finding is only ever a repeat of a comment
            # about the same file.
            continue
        if _similarity(mine, tokens) >= SIMILARITY:
            verdict = comment.get("verdict")
            if verdict:
                return f"already on this PR, and somebody {verdict} it"
            return "very similar to a comment already on this PR"
    return ""


# Three or more of one defect class is ONE comment. The prompt says so, and a
# model that has just found five unused imports writes five comments anyway —
# on PR #2373 twenty findings were five real classes. Five comments spend five
# slots to say one thing; the other four buy distinct defects.
GROUP_AT = 3
# Jaccard overlap at which two findings are the same defect class.
#
# 0.5 under Jaccard, not the 0.6 this was under containment: for two bodies of
# the same length, containment 0.6 is Jaccard ~0.43, so the old number was far
# looser than it looked in one direction and far tighter in the other. Being
# wrong here collapses real information rather than merely suppressing noise,
# which is why it sits above the duplicate bar in spirit and is measured
# symmetrically.
# Equal to SIMILARITY on purpose. Below it, `group_repeats` collapses pairs
# that `already_raised` cannot recognise next round -- the grouped comment
# goes out again and the members it swallowed arrive one per push, each round
# re-claiming "same thing in N other places" about the places it is about to
# comment on. Anything grouped must be recognisable later.
GROUP_SIMILARITY = SIMILARITY


# Where a path's recipe begins. Two findings in different recipes are never
# "the same thing in another place" — they are two recipes each needing a fix,
# and collapsing them tells one author and silences the rest.
_RECIPE_KEY = re.compile(r"^((?:core|contrib|skills)/[^/]+(?:/[^/]+)?)")


def _group_scope(path: str, trusted: bool = False) -> str:
    """The recipe a comment belongs to, for grouping purposes.

    Outside a recipe — repo tooling, a workflow, a root-level file — the scope
    is the top-level directory, so ordinary grouping still works. Keying those
    on the filename would give every file a scope of its own and disable
    grouping wherever recipes are not involved.
    """
    text = str(path)
    if trusted:
        # A checker finding groups only with others in the SAME FILE.
        # Grouping across files drops the other members with no record of
        # them anywhere, and the path-aware suppression a checker gets cannot
        # recognise them next round — so the class dripped one comment per
        # push for N-1 pushes, each round re-claiming "same thing in N other
        # places" about the places it was about to comment on. The
        # deterministic lane is exempt from the comment budget, so there is
        # nothing to save by collapsing them.
        return text
    match = _RECIPE_KEY.match(text)
    if match:
        return match.group(1)
    return text.split("/", maxsplit=1)[0] if "/" in text else ""


def _said_in_this_run(
    path: str,
    line: int,
    body: str,
    texts: list[tuple[set[str], dict]],
) -> bool:
    """Has this run already accepted this finding, for THIS file?

    Deliberately narrower than `already_raised`: same path, and either the
    same line or near-identical wording. Cross-file repetition inside one run
    is what `group_repeats` is for, and it says "same thing in N other
    places" rather than silently dropping the others.
    """
    # No line-zone leg. Many house rules fall back to line 1 when they cannot
    # locate their subject, so distinct rules collide there constantly: a stub
    # README produces four separate CI-failing H20 findings, all at line 1,
    # and blocking on position alone told the author about one of them. What
    # makes two findings the same finding is what they SAY.
    # The exact-match leg runs FIRST, above the token floor: a two-token body
    # repeated verbatim on one line is the clearest duplicate there is, and
    # putting it below the floor made it unreachable for exactly the bodies
    # the removed line-zone leg used to cover.
    for _tokens_unused, accepted in texts:
        if (
            accepted.get("path") == path
            and accepted.get("line") == line
            and accepted.get("body") == body
        ):
            return True
    # Exact repeats only. A similarity leg here dropped findings that are
    # genuinely different and merely worded alike -- two H39 stub values in
    # one .env.example differ only in the quoted value and score 0.77 -- and
    # it dropped them SILENTLY, with no "(Same thing in N other places)" note
    # and no way for the author to learn the others exist. Near-duplicates
    # within one run are group_repeats' job, and grouping announces itself.
    # The repo's own rule is that two instances stay two comments.
    return False


def group_repeats(comments: list[dict]) -> tuple[list[dict], list[str]]:
    """Collapse 3+ comments of one class onto the first instance.

    The kept comment's claim stays a claim about ITS OWN anchor; the count is
    context the reader can ignore. That is why the note says "N other places"
    instead of listing them — a list the reader must go and check is exactly
    the expensive comment shape the rest of this file exists to prevent.
    """
    tokenised = [(c, _tokens(c["body"])) for c in comments]
    kept: list[dict] = []
    dropped: list[str] = []
    used: set[int] = set()

    for i, (comment, tokens) in enumerate(tokenised):
        if i in used:
            continue
        members = [i]
        if len(tokens) >= 4:
            for j, (_other, other) in enumerate(
                tokenised[i + 1 :], start=i + 1
            ):
                if j in used or len(other) < 4:
                    continue
                # Same recipe only. The deterministic lane builds each rule's
                # body from one format string, so two recipes' findings for
                # one rule are near-identical by construction and always
                # cleared the threshold: three recipes with a deprecated model
                # id produced ONE comment on the first of them, and the other
                # two authors were told nothing.
                if _group_scope(
                    _other["path"], _other.get("trusted", False)
                ) != _group_scope(
                    comment["path"], comment.get("trusted", False)
                ):
                    continue
                # Never group two findings at the SAME position. The note
                # says "in N other places", and for these there is no other
                # place -- it is the same place, N times. Several house rules
                # fall back to line 1 when they cannot locate their subject,
                # so three unknown manifest keys all land on manifest.yaml:1,
                # and collapsing them posted one comment with a false count
                # and dropped two real CI-failing findings.
                # Against every member already in the group, not just the
                # anchor: two members sharing a line with each OTHER still
                # inflated "(Same thing in N other places)" and collapsed a
                # real finding. Same class as the bug this guard fixed, one
                # step removed.
                if any(
                    _other["path"] == tokenised[k][0]["path"]
                    and _other["line"] == tokenised[k][0]["line"]
                    for k in members
                ):
                    continue
                if _similarity(tokens, other) >= GROUP_SIMILARITY:
                    members.append(j)

        if len(members) >= GROUP_AT:
            others = len(members) - 1
            grouped_body = (
                f"{comment['body'].rstrip()}\n\n"
                f"(Same thing in {others} other place"
                f"{'s' if others != 1 else ''} in this review.)"
            )
            # The shape gate ran BEFORE grouping, and grouping is the one path
            # that makes a body longer. A 600-char body plus the note is 643,
            # over the cap that keeps this public channel narrow, and GitHub
            # would reject the whole review for it.
            #
            # `used` is marked only once the group is going ahead. Marking it
            # first and then bailing out left the other members flagged as
            # consumed while nothing had consumed them: they were skipped by
            # the outer loop and silently vanished. Found by the test written
            # for the cap itself, which is the only reason it is not still
            # here.
            if implausible_body(grouped_body):
                kept.append(comment)
                continue
            used.update(members)
            for index in members[1:]:
                victim = tokenised[index][0]
                dropped.append(
                    f"{victim['path']}:{victim['line']}: grouped into "
                    f"{comment['path']}:{comment['line']}"
                )
            kept.append({**comment, "body": grouped_body})
            continue
        kept.append(comment)

    return kept, dropped


def build_comments(
    findings: list,
    anchors: dict[str, set[int]],
    line_text: dict[str, dict[int, str]] | None = None,
    existing: list[dict] | None = None,
) -> tuple[list[dict], list[dict], list[str]]:
    """Sort findings into inline comments, body notes, and the discarded.

    Returns (comments, notes, skipped-with-reasons). Dropping beats failing:
    one bad anchor would otherwise cost the whole review.

    `notes` are findings whose window verified against a line the PR does not
    ADD — real, but with nowhere to hang inline. They used to go into the job
    log and be lost; they go in the review body instead. Only window-verified
    findings qualify, because an unverifiable line number is a model
    arithmetic error and promoting those would surface exactly the mistakes
    this function exists to catch.
    """
    line_text = line_text or {}
    comments: list[dict] = []
    notes: list[dict] = []
    skipped: list[str] = []
    zones, texts = build_exclusions(existing or [])
    # Accumulated as this run accepts findings.
    run_texts: list[tuple[set[str], dict]] = []

    for finding in findings:
        if not isinstance(finding, dict):
            skipped.append(f"not a JSON object: {finding!r}")
            continue

        path = _resolve_path(str(finding.get("path") or "").strip(), anchors)
        body = str(finding.get("body") or "").strip()
        line = _coerce_line(finding.get("line"))

        if line is None:
            skipped.append(f"{path or '<no path>'}: line is not a whole number")
            continue
        if not path or not body:
            skipped.append(f"{path or '<no path>'}:{line}: empty path or body")
            continue

        # Defang FIRST, so every comparison below -- and the duplicate legs
        # in particular -- sees the same string that will be stored and
        # posted. Cleaning it later meant a short body containing image
        # markup matched neither exact-match leg.
        body = _defang_images(body)

        # Before anything that could promote this body onto the PR — inline or
        # as a note in the review body, both of which are public.
        malformed = implausible_body(body)
        if malformed:
            skipped.append(f"{path}:{line}: {malformed}")
            continue

        steps = str(finding.get("verify_steps") or "")
        marker = NOT_CHEAP_MARKERS.search(steps)
        if marker:
            skipped.append(
                f"{path}:{line}: not cheap to verify ({marker.group(0)!r} "
                "in verify_steps)"
            )
            continue

        # Window first: it can move the anchor, and everything below depends
        # on the anchor being the one the finding is really about.
        declared = line
        verified, line, reason = check_window(
            finding, line, line_text.get(path, {})
        )
        # A finding from the deterministic checker read the file itself, out
        # of the same checkout this diff describes. The window check exists to
        # catch a MODEL that invented a finding and invented the source to
        # match it; there is no such claim to audit here. This has to be
        # decided BEFORE the gate below, not after it — waiving a check that
        # has already dropped the finding waives nothing.
        trusted = finding.get("source") == TRUSTED_SOURCE
        if not verified and finding.get("window") and not trusted:
            skipped.append(f"{path}:{line}: {reason}")
            continue
        if not verified and not finding.get("window") and not trusted:
            # DELIBERATE, and worth naming because it looks like a hole: a
            # finding with no window at all is not dropped for fabrication.
            # It still has to land on a line this PR adds, so the model
            # cannot choose where it goes, and dropping these instead would
            # discard a real finding whenever the model omits one field.
            # Logged so the trade is visible in the job output rather than
            # silent. test_a_finding_with_no_window_still_needs_a_real_added_line
            # pins the behaviour.
            print(f"  {path}:{line}: no window supplied; anchor not verified")
        if line != declared:
            print(f"  {path}: {reason}")

        # Without this, every house-rule finding whose subject is not an added
        # line — a required file that is missing, a folder name, a lockfile
        # source — is dropped as "not a line this PR adds" instead of reaching
        # the author in the review body.
        if trusted:
            verified = True

        duplicate = already_raised(path, line, body, zones, texts, trusted)
        if duplicate:
            skipped.append(f"{path}:{line}: {duplicate}")
            continue

        # ...and against what THIS run has already accepted.
        #
        # A dedicated check, NOT already_raised: that helper's similarity leg
        # ignores the path, which is right for "did we say this on the PR
        # before" and catastrophic here. The deterministic lane builds each
        # rule's body from one format string, so two recipes' findings for
        # one rule are byte-identical -- and reusing it dropped 13 of 21
        # findings on a three-recipe PR, telling two of the three authors
        # nothing about their own recipe. Same file only.
        if _said_in_this_run(path, line, body, run_texts):
            skipped.append(f"{path}:{line}: already said in this review")
            continue

        accepted = {"kind": "inline", "path": path, "line": line, "body": body}

        # Internal, and only ever present on a deterministic-lane finding: it
        # is what separates CRITICAL from MAJOR, because "CI will fail on
        # this" is a fact there and a guess anywhere else. Attached only when
        # it has a value, so a model lane's comments keep exactly the shape
        # they had before tagging existed. build_payload reads it and drops
        # it; it never reaches GitHub.
        _ci_value = finding.get("ci") or finding.get("_ci")
        ci_key = {"_ci": _ci_value} if _ci_value else {}

        if line in anchors.get(path, frozenset()):
            comments.append(
                {
                    "path": path,
                    "line": line,
                    "side": "RIGHT",
                    "body": body,
                    # Internal, stripped before the payload is written: tells
                    # group_repeats that this body came from a format string
                    # rather than from prose.
                    "trusted": trusted,
                    **ci_key,
                }
            )
        elif verified:
            notes.append({"path": path, "line": line, "body": body, **ci_key})
        else:
            skipped.append(f"{path}:{line}: not a line this PR adds")
            # NOT recorded, and this really is the ordering now: the previous
            # commit claimed it and added only a comment saying so. Recorded
            # before the classification, a finding dropped here suppressed a
            # later one under the reason "already said in this review", when
            # nothing had been said.
            continue

        run_texts.append((_tokens(body), accepted))

    # Last, so grouping sees only what actually survived every filter above.
    # Grouping first would collapse a class onto an instance that is then
    # dropped as a duplicate, taking the whole class with it.
    comments, grouped = group_repeats(comments)
    skipped.extend(grouped)

    # `trusted` is ours, not GitHub's.
    comments = [
        {k: v for k, v in c.items() if k != "trusted"} for c in comments
    ]
    return comments, notes, skipped


def _non_negative(value: str) -> int:
    """An int >= 0. A negative ceiling is `comments[:-1]`, which drops ONE
    comment and logs it as "over budget" — the opposite of what was asked."""
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"must be zero or more, got {number}")
    return number


def build_parser() -> argparse.ArgumentParser:
    """The CLI, exposed so a test can pin it against the workflow's call."""
    parser = argparse.ArgumentParser(
        description="Build a GitHub review payload from AI reviewer findings."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--result",
        type=Path,
        help="agy result file (--output-format json)",
    )
    source.add_argument(
        "--findings",
        type=Path,
        help="a JSON array of findings from house_rules_lane.py, in place of "
        "a model response; these skip the window check (see TRUSTED_SOURCE)",
    )
    parser.add_argument(
        "--diff",
        required=True,
        type=Path,
        help="the exact diff the reviewer was shown",
    )
    parser.add_argument(
        "--pr-diff",
        type=Path,
        default=None,
        help="the PR's whole base...head diff, when --diff is an incremental "
        "one. Inline anchors are limited to lines this diff adds, because "
        "GitHub rejects a comment on any other line",
    )
    parser.add_argument(
        "--label", required=True, help="review type, e.g. Correctness"
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="payload destination; written only when there is something to post",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="owner/name; with --pr, suppresses comments already on the PR",
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="PR number; with --repo, suppresses comments already on the PR",
    )
    parser.add_argument(
        "--max-comments",
        type=_non_negative,
        default=0,
        help="hard ceiling on inline comments, from review_budget.py. 0 means "
        "no ceiling. Until this existed the budget reached the model as prose "
        "and nothing downstream checked it",
    )
    parser.add_argument(
        "--commit-id",
        default="",
        help="the commit these findings are about. Recorded on the review so "
        "a later run knows exactly what was reviewed, rather than inferring "
        "it from whatever head happened to be at post time",
    )
    parser.add_argument(
        "--progress",
        default="",
        help="one line telling the author which review round this is and how "
        "much budget the PR has left",
    )
    parser.add_argument(
        "--unreviewed",
        type=Path,
        default=None,
        help="file of paths that fell outside the prompt budget; named in the "
        "review body so silence on them is not read as approval",
    )
    return parser


# How many unreviewed paths to name before summarising the rest. Long enough
# to be actionable, short enough not to bury the findings above it.
MAX_UNREVIEWED_LISTED = 15

# Notes are bullets in ONE review body, so they cannot be capped by
# --max-comments without changing what that flag means. They still need a
# bound: GitHub rejects a review body over ~65k characters, and the fallback
# path then re-posts the same oversized body once per comment, so every retry
# fails too and the contributor gets a red check. The House Rules lane is
# exempt from the comment budget and produces mostly notes, which is exactly
# the combination that gets there.
MAX_NOTES_LISTED = 20


# A path is fork-author-chosen text. Everything else on this channel goes
# through a shape rule; this is the one part that did not, and a backtick in a
# filename closes the code span and lets arbitrary markdown — a link, an
# image, a fake instruction — into a body the review bot signs.
_UNSAFE_IN_SPAN = re.compile(r"[`\r\n]")


def _safe_span(text: str, limit: int = 160) -> str:
    """A path, safe to drop inside a markdown code span.

    Over-long paths are cut in the MIDDLE, not at the end. Paths differ at
    the end -- that is where the filename is -- so head-truncation mapped two
    files sharing a long directory prefix onto one span, and `_same_path`
    then let one file's note suppress the other's.
    """
    cleaned = _UNSAFE_IN_SPAN.sub("", str(text))
    if len(cleaned) <= limit:
        return cleaned
    head = (limit - 1) // 2
    return cleaned[:head] + "…" + cleaned[-(limit - 1 - head) :]


# Case-insensitive: HTML tag names are, and the parser lowercases the node
# before GitHub's sanitiser allowlist is consulted, so `<IMG SRC=...>` is the
# same element as `<img src=...>` and rendered the same remote request.
_IMG_TAG = re.compile(r"<img", re.IGNORECASE)


def _defang_images(text: str) -> str:
    """Neutralise an inline image without touching anything else.

    An image is a request the reader's browser makes to a URL the pull
    request chose, fired merely by rendering the page. A link is fine; an
    image is not. BOTH spellings have to go: `<img src=...>` is in GitHub's
    markdown sanitiser allowlist, so closing only the `![]()` form left the
    same request one tag away.
    """
    return _IMG_TAG.sub("&lt;img", text.replace("![", "!\u200b["))


def _safe_line(text: str) -> str:
    """A finding body, safe to splice into a bullet in the review body.

    The path beside it is sanitised; this was not, and it is the wider
    channel — 600 characters of model text derived from a fork-authored diff.
    A newline plus `---` renders a horizontal rule, `![](url)` fires a remote
    request on render, and an italic line forges a second progress footer
    above the real one. All three reproduced.
    """
    return _defang_images(" ".join(str(text).split()))


def build_payload(
    label: str,
    comments: list[dict],
    notes: list[dict],
    unreviewed: list[str] | None = None,
    progress: str = "",
    commit_id: str = "",
) -> dict:
    """The review payload. `body` is required whenever `event` is COMMENT."""
    # Tagging happens HERE, last, and deliberately so. Everything upstream --
    # the duplicate check, grouping, the comment budget -- compares bodies,
    # and a prefix on one side of those comparisons makes a finding look new.
    # Tag at the end and every one of them still sees the prose alone.
    comments = _tagged(comments, label)
    notes = _tagged(notes, label)
    header = f"Automated **{label}** review — {len(comments)} finding(s)."
    # An invisible signature, so a later run can recognise its own reviews and
    # work out which round it is on. The header below is the fallback for
    # reviews posted before this existed, but prose gets edited and a marker
    # nobody reads does not. review_budget.py is the reader.
    lines = [REVIEW_MARKER, header]

    if notes:
        # These sit on lines the PR does not add, so GitHub will not take them
        # inline. Listing them here is the only way they reach the author at
        # all, and on PR #2373 this class held all three hard CI failures.
        lines += ["", "Also, on lines this PR does not change:", ""]
        lines += [
            f"- `{_safe_span(n['path'])}:{n['line']}` — {_safe_line(n['body'])}"
            for n in notes[:MAX_NOTES_LISTED]
        ]
        if len(notes) > MAX_NOTES_LISTED:
            lines.append(f"- …and {len(notes) - MAX_NOTES_LISTED} more")

    if unreviewed:
        # Silence on a file reads as approval of it. When the diff did not fit
        # the prompt, that reading is wrong, and only the author can tell which
        # of these actually needed looking at.
        lines += [
            "",
            f"⚠️ This PR's diff was too large to review in full, so "
            f"**{len(unreviewed)} file(s) were not looked at** by this lane:",
            "",
        ]
        lines += [
            f"- `{_safe_span(p)}`" for p in unreviewed[:MAX_UNREVIEWED_LISTED]
        ]
        if len(unreviewed) > MAX_UNREVIEWED_LISTED:
            lines.append(
                f"- …and {len(unreviewed) - MAX_UNREVIEWED_LISTED} more"
            )
        lines += ["", "Splitting the PR up would get them reviewed."]

    if progress:
        # Last, and set apart: the author has just read the findings and this
        # is the answer to "is this ever going to stop".
        lines += ["", "---", "", f"_{progress}_"]

    payload = {
        "event": "COMMENT",
        "body": "\n".join(lines),
        "comments": comments,
    }
    if commit_id:
        # Without this GitHub records the review against whatever is head AT
        # POST TIME, not the commit that was reviewed. A review job takes
        # minutes, so a push landing inside that window makes the recorded
        # commit one that nothing looked at — and review_budget.py reads that
        # field to decide what has already been reviewed, so the next run
        # skips every lane and that commit is never reviewed by anyone.
        payload["commit_id"] = commit_id
    return payload


def _findings_from_file(path: Path) -> list:
    """The deterministic lane's findings, tagged as trusted."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("findings file is not a JSON array")
    return [
        {**f, "source": TRUSTED_SOURCE} for f in data if isinstance(f, dict)
    ]


def main() -> int:
    args = build_parser().parse_args()

    if args.findings is not None:
        try:
            findings = _findings_from_file(args.findings)
        except (OSError, ValueError) as exc:
            return report_infra_fault(
                infra_fault(
                    CHECKER, f"cannot read findings {args.findings}: {exc}"
                )
            )
        return _post(args, findings)

    try:
        result = json.loads(args.result.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return report_infra_fault(
            infra_fault(
                CHECKER, f"cannot read reviewer result {args.result}: {exc}"
            )
        )
    if not isinstance(result, dict):
        return report_infra_fault(
            infra_fault(
                CHECKER, f"reviewer result {args.result} is not a JSON object"
            )
        )

    response = result.get("response") or ""
    try:
        findings = extract_findings(response)
    except ReviewerOutputError as exc:
        print("Reviewer response was:")
        print(response[:2000])
        return report_infra_fault(
            infra_fault(CHECKER, f"reviewer output unusable: {exc}")
        )

    # A model must not be able to waive its own window check by claiming to be
    # the deterministic checker. The key is stripped here, before anything
    # reads it, rather than trusted not to appear.
    for finding in findings:
        if isinstance(finding, dict):
            finding.pop("source", None)

    return _post(args, findings)


def _post(args, findings: list) -> int:
    """Anchor, filter and write the payload. Shared by both input paths."""
    # errors="replace", because the workflow trims the diff to a byte budget
    # with `head -c` and that cut lands inside a multi-byte character sooner
    # or later — any diff touching an em dash or an accent is a candidate.
    # Strict decoding turned that into an uncaught UnicodeDecodeError that
    # threw away the entire review. Nothing here needs the mangled bytes:
    # anchors are computed from line structure and ASCII prefixes, and comment
    # bodies come from the model, not from the diff.
    try:
        diff = args.diff.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return report_infra_fault(
            infra_fault(CHECKER, f"cannot read diff {args.diff}: {exc}")
        )

    existing = (
        fetch_existing_comments(args.repo, args.pr)
        if args.repo and args.pr
        else []
    )
    if existing:
        print(f"{len(existing)} comment(s) already on this PR.")

    anchors, line_text = walk_right_side(diff)
    if args.pr_diff is not None:
        try:
            pr_diff = args.pr_diff.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return report_infra_fault(
                infra_fault(CHECKER, f"cannot read diff {args.pr_diff}: {exc}")
            )
        anchors = restrict_to_pr(anchors, pr_diff)
    comments, notes, skipped = build_comments(
        findings, anchors, line_text, existing
    )
    # A plain log line, not a ::warning:: annotation. Which findings were
    # dropped is debugging detail for whoever is looking at this job, and
    # nothing a contributor reading their PR could act on.
    for reason in skipped:
        print(f"  dropped finding — {reason}")
    print(
        f"{len(findings)} finding(s) returned, {len(comments)} postable, "
        f"{len(notes)} on unchanged lines."
    )

    # The ceiling, applied last. Until this existed the per-run budget reached
    # the model as prose ("Aim for that number") and nothing downstream ever
    # checked it, so a lane that felt talkative simply was. The model is told
    # to emit findings most serious first, so keeping the head of the list
    # keeps the most serious ones.
    #
    # Notes are deliberately NOT capped: they are lines in one review body
    # rather than separate comments, and they carry the findings that have
    # nowhere else to go.
    limit = getattr(args, "max_comments", 0) or 0
    if limit and len(comments) > limit:
        print(
            f"  budget is {limit} comment(s); dropping "
            f"{len(comments) - limit} past it"
        )
        for dropped in comments[limit:]:
            print(f"  over budget — {dropped['path']}:{dropped['line']}")
        comments = comments[:limit]

    unreviewed: list[str] = []
    if getattr(args, "unreviewed", None) and args.unreviewed.exists():
        unreviewed = [
            line.strip()
            for line in args.unreviewed.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            if line.strip()
        ]
        if unreviewed:
            print(f"{len(unreviewed)} file(s) were outside the prompt budget.")

    # An unreviewed list is worth posting even with nothing else to say: a
    # lane that found nothing AND saw only half the diff is not the same
    # result as a lane that found nothing.
    if not comments and not notes and not unreviewed:
        return EXIT_OK

    args.out.write_text(
        json.dumps(
            build_payload(
                args.label,
                comments,
                notes,
                unreviewed,
                args.progress,
                getattr(args, "commit_id", ""),
            )
        ),
        encoding="utf-8",
    )
    return EXIT_OK


if __name__ == "__main__":
    # guard(): an unhandled exception must surface as a CI fault naming this
    # checker, not as a bare traceback under a "problem with your PR" banner.
    sys.exit(guard(CHECKER, main))
