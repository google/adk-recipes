#!/usr/bin/env python3
"""
Trim a PR diff to what is worth reviewing, and size the review from it.

Used by .github/workflows/_ai-pr-review-core.yml, between fetching the diff
and building the reviewer's prompt.

Two jobs, both of which exist because the reviewer's prompt has a hard byte
budget (Linux caps one argv string at 131072 bytes, so the workflow allows
125000 and truncates the diff to fit).

1. DROP WHAT NOBODY REVIEWS. A regenerated `uv.lock` is thousands of lines
   that no reviewer reads and that no comment can usefully land on, and every
   one of those lines displaces a line of hand-written code from the prompt.
   On a PR that mixes the two, the lockfile wins purely by being longer, and
   the code it was generated from never reaches the model at all.

2. SIZE THE REVIEW. The reviewer was previously told nothing about how many
   findings a PR of this size should yield, and settled at one or two
   regardless. The budget below is computed from REVIEWABLE churn, so a PR
   that is 1400 lines of lockfile plus 80 lines of code is sized as the small
   PR it is.

Both numbers are deterministic, which is the point: a model asked to judge
its own thoroughness will not, and a model asked to ignore a lockfile it can
see in its context still spends attention on it.

Usage:
  python3 prepare_review_diff.py \
    --diff pr_diff.txt \
    --out pr_diff_reviewable.txt \
    --github-output "${GITHUB_OUTPUT}"

Outputs (to --github-output, or stdout when it is absent):
  reviewable        `false` when nothing is left to review
  reviewable_lines  added+removed lines across the kept files
  budget            comments expected from ONE lane
  kept_files        files surviving the filter
  skipped_files     files dropped, with a one-word reason in the log

Exit codes:
  0  filtered diff written (even when it is empty — `reviewable=false` says so)
  2  CI fault — the diff this workflow just fetched could not be read
"""

import argparse
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ci_message import (
    EXIT_OK,
    guard,
    infra_fault,
    report_infra_fault,
)

CHECKER = "prepare_review_diff.py"

# What never earns a review comment: (pattern, reason), matched
# case-insensitively against the full path. Kept deliberately in step with
# .agents/skills/github-pr-review/scripts/plan_review.py, which makes the same
# judgement for the interactive reviewer.
SKIP_PATTERNS = [
    (
        r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|poetry\.lock"
        r"|Cargo\.lock|Gemfile\.lock|composer\.lock|go\.sum|uv\.lock)$",
        "lockfile",
    ),
    (r"(^|/)(vendor|node_modules|third_party|external)/", "vendored"),
    (r"(^|/)(dist|build|out|target)/", "build output"),
    (r"(^|/)__snapshots__/", "snapshot"),
    (r"\.snap$", "snapshot"),
    (r"\.min\.(js|css)$", "minified"),
    (r"\.(pb|pb2)\.(go|py|js|ts|cc|h)$", "generated protobuf"),
    (r"_pb2(_grpc)?\.pyi?$", "generated protobuf"),
    (r"\.generated\.[a-z]+$", "generated"),
    (r"(^|/)generated/", "generated"),
    (
        r"\.(png|jpe?g|gif|svg|ico|webp|pdf|woff2?|ttf|eot|zip|tar|gz|jar"
        r"|so|dylib|dll)$",
        "binary asset",
    ),
    (r"(^|/)testdata/", "test fixture"),
]

# A data file this large is a dump whatever it is called. Small ones are
# hand-written config and very much worth reviewing, so the size is doing the
# work here, not the extension.
BULK_DATA_EXT = {
    ".json",
    ".csv",
    ".tsv",
    ".yaml",
    ".yml",
    ".xml",
    ".sql",
    ".txt",
    ".ndjson",
    ".jsonl",
}
BULK_DATA_CHURN = 500

# How many THROTTLED lanes run against one PR. Used only to divide the budget
# below; the lanes themselves never learn about each other. The deterministic
# house-rules lane is exempt from the budget and is not counted here.
# `test_review_budget.py` pins this against policy.yml's `lanes` list.
LANE_COUNT = 4

# The interactive skill budgets 2-20 comments for a whole review, scaled by
# churn, and caps it at 20. Here four lanes run as separate concurrent jobs
# and cannot coordinate, so each is given a share that keeps the total at that
# same cap however many findings the others come back with.
GLOBAL_CAP = 20

# (max_reviewable_churn, comments expected from one lane).
BUDGET_TABLE = [
    (50, 2),
    (200, 3),
    (math.inf, GLOBAL_CAP // LANE_COUNT),
]

FILE_HEADER = re.compile(r"^diff --git ")
# The b-side of a `diff --git` header. Quoted when the path holds a space or a
# non-ASCII byte, which git escapes C-style.
GIT_HEADER_PATHS = re.compile(r'^diff --git (?:"?a/(.+?)"?) (?:"?b/(.+?)"?)$')


def budget_for(churn: int) -> int:
    """Comments one lane is expected to produce for this much churn."""
    for ceiling, budget in BUDGET_TABLE:
        if churn <= ceiling:
            return budget
    return BUDGET_TABLE[-1][1]


def skip_reason(path: str, churn: int) -> str | None:
    """Why this file earns no review comment, or None if it does."""
    for pattern, reason in SKIP_PATTERNS:
        if re.search(pattern, path, re.IGNORECASE):
            return reason
    suffix = Path(path).suffix.lower()
    if suffix in BULK_DATA_EXT and churn >= BULK_DATA_CHURN:
        return "bulk data"
    return None


# A C-style escape inside a quoted git path: \\ \" \t \n \r, or \nnn octal.
C_ESCAPE = re.compile(r"\\(?:([\\\"abfnrtv])|([0-7]{3}))")
_C_SIMPLE = {
    "\\": b"\\",
    '"': b'"',
    "a": b"\a",
    "b": b"\b",
    "f": b"\f",
    "n": b"\n",
    "r": b"\r",
    "t": b"\t",
    "v": b"\v",
}


def _unquote_git_path(raw: str) -> str:
    """A path as git printed it, turned back into the real name.

    With `core.quotePath` on -- the default -- git wraps any path holding a
    space, a quote or a non-ASCII byte in double quotes and C-escapes it, so
    `café.py` prints as `"caf\\303\\251.py"`. The GitHub API reports the real
    name. Comparing one against the other silently fails to match, and since
    that comparison now decides whether a file is reviewed at all, every path
    with a space or an accent in it would drop out of the review unnoticed.
    """
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return raw
    body = raw[1:-1]
    # Anything undecodable comes back exactly as written -- never mangled and
    # never raised. A wrong name is worse than a quoted one, and this parses
    # text nobody here controls. `\400` to `\777` are why the whole build sits
    # inside the try rather than just the decode: the escape grammar admits
    # them and `bytes()` rejects them, so an octal git would never emit took
    # the lane down with a ValueError from four frames away.
    try:
        out = bytearray()
        index = 0
        for match in C_ESCAPE.finditer(body):
            out.extend(body[index : match.start()].encode("utf-8"))
            simple, octal = match.group(1), match.group(2)
            out.extend(_C_SIMPLE[simple] if simple else bytes([int(octal, 8)]))
            index = match.end()
        out.extend(body[index:].encode("utf-8"))
        return out.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return raw


def _strip_side_prefix(path: str) -> str:
    """`a/x` or `b/x` -> `x`. Applied AFTER unquoting, never before: in
    `"b/x"` the prefix is inside the quotes, so testing the raw string for a
    leading `b/` sees the quote and leaves the whole thing untouched."""
    return path[2:] if path.startswith(("a/", "b/")) else path


def _section_path(section: list[str]) -> str | None:
    """The new-side path a diff section is about, or None when it is deleted.

    `+++ b/x` is preferred because it is where the reviewer's own findings are
    anchored. It is absent for a deletion (`+++ /dev/null`) and for a
    rename or mode change carrying no hunks, so the `diff --git` header is the
    fallback — it is the only line every section is guaranteed to have.
    """
    for row in section:
        if row.startswith("+++ "):
            target = row[4:].strip()
            if target == "/dev/null":
                return None
            return _strip_side_prefix(_unquote_git_path(target))
    header = GIT_HEADER_PATHS.match(section[0]) if section else None
    if header:
        # The regex peels the quotes off the outside but leaves any escape
        # inside them, so the same unquoting has to run here too.
        return _unquote_git_path(f'"{header.group(2)}"')
    return None


def _section_churn(section: list[str]) -> int:
    """Added plus removed lines, ignoring the `---`/`+++` file headers."""
    churn = 0
    for row in section:
        if row.startswith(("+++ ", "--- ")):
            continue
        if row.startswith(("+", "-")):
            churn += 1
    return churn


def split_sections(diff: str) -> tuple[list[str], list[list[str]]]:
    """(preamble, per-file sections) — sections start at `diff --git`.

    A diff with no `diff --git` at all is returned whole as the preamble, so
    an unexpected format degrades to "review everything" rather than to
    "review nothing". Silently discarding a diff we failed to parse would
    turn a format change into a green check on an unreviewed PR.
    """
    preamble: list[str] = []
    sections: list[list[str]] = []
    current: list[str] | None = None

    for row in diff.split("\n"):
        if FILE_HEADER.match(row):
            if current is not None:
                sections.append(current)
            current = [row]
        elif current is None:
            preamble.append(row)
        else:
            current.append(row)

    if current is not None:
        sections.append(current)
    return preamble, sections


# ------------------------------------------------------- fitting the budget
#
# The prompt has a hard byte budget (agy takes it as one argv string, and
# Linux caps that at 131072). Until this existed the workflow met the budget
# with `head -c`, which cuts at a byte offset in git's own order -- roughly
# alphabetical. That is not a review policy, it is an accident, and on PR
# #2626 it produced this: 662KB of diff, ~100KB of budget, and the budget went
# to the six paths that sorted first. Four of the six were markdown. Not one
# of the fourteen source files was reviewed in full, 31 files were invisible,
# and the author was told to split the PR up.
#
# So pack the budget deliberately instead:
#
#   1. SOURCE before tests before prose. A defect in shipped code costs more
#      than one in a test, which costs more than one in documentation, and
#      when there is not room for everything that ranking is the decision.
#   2. Smallest first WITHIN a tier, which maximises how many files are seen
#      whole. One 79KB file would otherwise take four fifths of the budget.
#   3. Whole files, with at most one partial at the end. A 2.7KB slice of a
#      74KB file is not a review of anything -- an equal split across 37 files
#      sounds fairer and tells the model nothing about any of them.
#
# Reordering is safe: a hunk header carries its own line numbers, so an anchor
# does not depend on where its file sits in the diff.
TEST_PATH = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*$|_test\.[a-z]+$", re.I)
PROSE_EXT = {".md", ".markdown", ".rst", ".txt", ".adoc"}

# Below this a partial file is a fragment rather than a sample, so it goes to
# the unreviewed list instead, which at least names it honestly.
MIN_PARTIAL_BYTES = 3000

# Appended to a file that had to be cut short, so the model knows the silence
# after it is a budget boundary and not the end of the change. Shared with the
# tests, which would otherwise assert against their own copy of the wording
# and keep passing after someone edited this one.
TRUNCATED_MARKER = "[... this file was truncated here ...]"

# Bytes held back when cutting a file, so TRUNCATED_MARKER and its newline
# always fit inside the room the caller measured.
TRUNCATED_MARKER_BYTES = len(TRUNCATED_MARKER.encode()) + 2

# The path reported for a section whose own header does not name one. Should
# not survive `filter_diff`, which drops deletions, but `pack_to_budget` is
# callable on its own and an omission list is worse than useless if an entry
# in it is blank.
UNKNOWN_PATH = "<unknown>"

# How many omitted paths to print in the job log. The count above the list is
# the complete figure; this only bounds the log on a PR with hundreds.
MAX_OMITTED_LOGGED = 20

# `@@ -old[,count] +new[,count] @@`. An absent count means 1.
HUNK_COUNTS = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _hunk_is_complete(rows: list[str]) -> bool:
    """Did every line the header of `rows` promises actually survive?

    `rows[0]` is the `@@` header and the rest is its body. A hunk header
    declares how many lines each side spans, so completeness is a fact the
    diff states about itself rather than something to infer from where the
    bytes happened to stop.
    """
    header = HUNK_COUNTS.match(rows[0]) if rows else None
    if not header:
        return False
    want_old = int(header.group(1) or 1)
    want_new = int(header.group(2) or 1)
    old = new = 0
    for row in rows[1:]:
        if row.startswith("\\"):  # "\ No newline at end of file"
            continue
        if row.startswith("+"):
            new += 1
        elif row.startswith("-"):
            old += 1
        else:
            old += 1
            new += 1
    return old >= want_old and new >= want_new


def review_tier(path: str) -> int:
    """0 source, 1 test, 2 prose. Lower is packed first."""
    if Path(path).suffix.lower() in PROSE_EXT:
        return 2
    if TEST_PATH.search(path):
        return 1
    return 0


def _truncate_at_hunk(text: str, limit: int) -> str:
    """`text` cut to at most `limit` bytes, on a hunk boundary where possible.

    `head -c` cut mid-line, so the last file the model saw ended in half a
    statement and it had to guess the rest. Cutting at a `@@` keeps every hunk
    it does see intact.
    """
    rows = text.split("\n")
    kept: list[str] = []
    size = 0
    for row in rows:
        cost = len(row.encode()) + 1
        if size + cost > limit:
            break
        kept.append(row)
        size += cost
    if len(kept) == len(rows):
        return text

    # Drop the trailing hunk only when it is INCOMPLETE. Backing off to the
    # last `@@` unconditionally threw away a whole hunk whenever the byte
    # limit happened to land on a hunk boundary -- the one case where the
    # trailing hunk is perfectly good -- so the reader lost content that fit.
    for index in range(len(kept) - 1, 0, -1):
        if kept[index].startswith("@@"):
            if index > 1 and not _hunk_is_complete(kept[index:]):
                kept = kept[:index]
            break
    return "\n".join(kept)


def _hard_cut(text: str, max_bytes: int) -> str:
    """`text` cut to at most `max_bytes`, on a line boundary.

    The fallback for a diff that cannot be packed at all. It exists because
    the CALLER asserts the assembled prompt fits and exits non-zero when it
    does not (`_ai-pr-review-core.yml`, "Assembled prompt is N bytes"), so
    returning something oversized here does not degrade the review, it
    deletes it -- a red check on a PR nobody reviewed. `head -c` could never
    do that, and neither may this.
    """
    kept: list[str] = []
    size = 0
    for row in text.split("\n"):
        cost = len(row.encode()) + 1
        if size + cost > max_bytes:
            break
        kept.append(row)
        size += cost
    return "\n".join(kept)


# Reserved so the notice below cannot itself push the prompt over budget.
# Only spent when something was actually withheld, and by then the diff is
# over budget by definition, so the reservation costs nothing real.
OMISSION_NOTICE_BYTES = 256


def _omission_notice(omitted: list[str], partial: list[str]) -> str:
    """What to tell the MODEL about the files it is not being shown.

    Not the same audience as the unreviewed list in the review body, which is
    for the author. The model is handed the PR's full changed-file list, so
    without this it sees 37 names and 10 diffs and has every reason to reason
    about the other 27 from their filenames alone. `head -c` said "review
    only what is shown above" for exactly this reason; packing must not drop
    the instruction along with the mechanism.
    """
    if not omitted and not partial:
        return ""
    parts = []
    if omitted:
        parts.append(f"{len(omitted)} file(s) omitted entirely")
    if partial:
        parts.append(f"{len(partial)} shown only in part")
    return (
        f"\n[... {', '.join(parts)} to fit the prompt budget. Review ONLY "
        f"what is shown above; say nothing about code you were not shown "
        f"...]"
    )


def _fallback_cut(
    diff: str, sections: list[list[str]], max_bytes: int
) -> tuple[str, list[str], list[str]]:
    """Pack's last resort: cut to fit, and say what the cut cost.

    Reached when there is nothing to choose between (no `diff --git` at all)
    or the preamble alone fills the budget. Two things have to hold even
    here. The result must FIT, because the caller rejects an oversized prompt
    outright. And whatever was lost must be NAMED: an empty omitted list
    leaves unreviewed_files.txt empty and the review body silent, so a PR
    whose diff could not be shown collects a green check, and a review of
    nothing looks exactly like a clean bill of health.
    """
    # Leave room for the notice, as the packing path does. A budget too small
    # to hold even that spends the bytes on diff instead: a sentence of
    # explanation is worth less than the only lines the reader will get.
    notice_room = (
        OMISSION_NOTICE_BYTES if max_bytes > OMISSION_NOTICE_BYTES * 2 else 0
    )
    cut = _hard_cut(diff, max_bytes - notice_room)

    # A file survives only if CHANGED LINES of it made the cut. Matching the
    # `diff --git` header called a file reviewed when all the reader got was
    # its name; matching `@@` called it reviewed when all the reader got was
    # a hunk header with nothing under it. Churn is the question actually
    # being asked -- was there anything here to review -- so ask that, with
    # the function that already answers it everywhere else in this file.
    _preamble, cut_sections = split_sections(cut)
    survived = {
        _section_path(s) or UNKNOWN_PATH
        for s in cut_sections
        if _section_churn(s) > 0
    }
    lost = [
        path
        for path in (_section_path(s) or UNKNOWN_PATH for s in sections)
        if path not in survived
    ]
    notice = _omission_notice(lost, []) if notice_room else ""
    if notice:
        cut = cut.rstrip("\n") + "\n" + notice.lstrip("\n") + "\n"
    return cut, [], lost


def pack_to_budget(
    diff: str, max_bytes: int
) -> tuple[str, list[str], list[str]]:
    """(diff that fits, partially shown paths, omitted paths).

    `max_bytes <= 0` means no budget was given and the diff is returned as it
    came. Otherwise the result NEVER exceeds `max_bytes`, including on every
    path where packing is impossible -- see `_hard_cut`.
    """
    if max_bytes <= 0 or len(diff.encode()) <= max_bytes:
        return diff, [], []

    preamble, sections = split_sections(diff)
    head = "\n".join(preamble)
    room = max_bytes - len(head.encode()) - 1 - OMISSION_NOTICE_BYTES
    if not sections or room <= 0:
        # Nothing to choose between (no `diff --git` at all), or the preamble
        # alone fills the budget.
        return _fallback_cut(diff, sections, max_bytes)

    by_path = [
        (_section_path(s) or UNKNOWN_PATH, "\n".join(s)) for s in sections
    ]
    order = sorted(
        range(len(by_path)),
        key=lambda i: (
            review_tier(by_path[i][0]),
            len(by_path[i][1].encode()),
            by_path[i][0],
        ),
    )

    chosen: dict[int, str] = {}
    partial: list[str] = []
    dropped: list[int] = []
    for i in order:
        path, text = by_path[i]
        cost = len(text.encode()) + 1
        if cost <= room:
            chosen[i] = text
            room -= cost
        elif room >= MIN_PARTIAL_BYTES and not partial:
            cut = _truncate_at_hunk(text, room - TRUNCATED_MARKER_BYTES)
            if len(cut.encode()) >= MIN_PARTIAL_BYTES:
                chosen[i] = f"{cut}\n{TRUNCATED_MARKER}"
                partial.append(path)
                room = 0
            else:
                dropped.append(i)
        else:
            dropped.append(i)

    # Report omissions in DIFF order, not pack order. The author reads this
    # list against their own PR, and "tier, then size ascending" is an
    # internal detail that makes it look arbitrary to them.
    omitted = [by_path[i][0] for i in sorted(dropped)]

    # Emit in the diff's ORIGINAL order too. The packing decides what is
    # shown; the model should still read the PR in the shape git describes.
    rows = [head] if head else []
    rows.extend(chosen[i] for i in sorted(chosen))
    out = "\n".join(rows).strip("\n")
    out = (out + "\n") if out else ""
    notice = _omission_notice(omitted, partial)
    if notice:
        out += notice.lstrip("\n") + "\n"
    return out, partial, omitted


def filter_diff(diff: str, only: set[str] | None = None) -> tuple[str, dict]:
    """Drop unreviewable file sections. Returns (diff, stats).

    `only` is the set of paths the PULL REQUEST itself changes. Anything
    outside it is discarded before any other rule runs -- see FOREIGN_REASON.
    None means no list was supplied and nothing is filtered on that basis.
    """
    preamble, sections = split_sections(diff)
    kept: list[list[str]] = []
    skipped: list[tuple[str, str, int]] = []
    foreign: list[str] = []
    reviewable = 0

    for section in sections:
        path = _section_path(section)
        churn = _section_churn(section)

        # The name to report this section by. `_section_path` is None for a
        # deletion (`+++ /dev/null`), so recover it from the `diff --git`
        # header — the one line every section has. Resolved BEFORE the
        # ownership test below, because a deletion has an owner too: computing
        # it afterwards meant a file deleted on the base branch was reported
        # as one the author had deleted.
        #
        # Slicing off "diff --git " left the raw "a/x b/x" pair here, which
        # reads as a path containing a space and hides which file was dropped.
        header = GIT_HEADER_PATHS.match(section[0]) if section else None
        name = path
        if name is None:
            name = (
                _unquote_git_path(f'"{header.group(2)}"')
                if header
                else UNKNOWN_PATH
            )

        # FIRST, before every other rule: is this file even part of the PR?
        #
        # The lanes review `compare/<last reviewed>...<head>` so a push that
        # only fixes earlier comments has almost nothing new to say. But when
        # the author merges the BASE branch in, that compare also contains
        # everything that landed on base in the meantime -- other people's
        # merged work. On #2628 the PR changed 10 files and the lanes read
        # 37, the extra 27 belonging to #2626. They spent the prompt budget
        # on code the author never wrote, then told them 31 files "were not
        # looked at".
        #
        # Dropped SILENTLY, not added to `skipped`: a file outside the PR is
        # not something the author declined to have reviewed, and naming it
        # would be the same confusion with the sign flipped. The job log
        # records the count.
        #
        # A section whose name could not be recovered at all is NOT called
        # foreign. "I could not parse this" and "this belongs to somebody
        # else" are different answers, and only one of them is a fact.
        if only is not None and name != UNKNOWN_PATH and name not in only:
            foreign.append(name)
            continue

        if path is None:
            skipped.append((name, "deleted", churn))
            continue
        # A section with no churn is a pure rename or a mode change. There is
        # nothing in it to comment on, and on a migration PR it can be most of
        # the diff — 65 of 123 files on #2373.
        if churn == 0:
            skipped.append((path, "no content change", 0))
            continue
        reason = skip_reason(path, churn)
        if reason:
            skipped.append((path, reason, churn))
            continue

        kept.append(section)
        reviewable += churn

    rows = list(preamble)
    for section in kept:
        rows.extend(section)
    out = "\n".join(rows).strip("\n")
    if out:
        out += "\n"

    return out, {
        "reviewable_lines": reviewable,
        "kept_files": len(kept),
        "skipped": skipped,
        "foreign": foreign,
    }


def build_parser() -> argparse.ArgumentParser:
    """The CLI, exposed so a test can pin it against the workflow's call."""
    parser = argparse.ArgumentParser(
        description="Trim a PR diff to the files worth reviewing."
    )
    parser.add_argument(
        "--diff", required=True, type=Path, help="the fetched PR diff"
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="filtered diff destination"
    )
    parser.add_argument(
        "--budget-ceiling",
        type=int,
        default=0,
        help="hard ceiling for this lane from review_budget.py; the budget "
        "this script prints is the smaller of that and its own churn-based "
        "number, so the figure the model aims at cannot exceed the figure "
        "the poster enforces. 0 means no ceiling",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=0,
        help="byte budget for the diff in the prompt. The script packs whole "
        "files into it, source before tests before prose; 0 means no budget "
        "and the filtered diff is written as-is",
    )
    parser.add_argument(
        "--only-files",
        type=Path,
        default=None,
        help="the paths this PR actually changes, one per line. Anything in "
        "the diff and not in this list is dropped: an incremental diff taken "
        "across a merge of the base branch also contains whatever landed on "
        "base in between, which is not this PR's code to review",
    )
    parser.add_argument(
        "--unreviewed-out",
        type=Path,
        default=None,
        help="write the paths that did not fit here, one per line",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        default=None,
        help="$GITHUB_OUTPUT; printed to stdout when omitted",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    # errors="replace" for the same reason post_review_comments.py needs it:
    # nothing here depends on the bytes of any individual line, and a diff
    # touching an em dash must not take the whole review down.
    try:
        diff = args.diff.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return report_infra_fault(
            infra_fault(CHECKER, f"cannot read diff {args.diff}: {exc}")
        )

    # FAIL OPEN. An unreadable or empty list means "filter on nothing", never
    # "filter out everything": the second would leave the lane reviewing an
    # empty diff and reporting no findings, which is indistinguishable from a
    # clean PR. Reviewing too much is a bug; reviewing nothing and saying so
    # is a lie.
    # Each outcome reports itself and then stops. Falling through from the
    # OSError branch into a shared empty-check printed both "cannot read" and
    # "is empty" for one unreadable file -- two messages, the second of them
    # untrue, in the log someone reads to find out why nothing was filtered.
    only: set[str] | None = None
    if args.only_files:
        try:
            listed = {
                line.strip()
                for line in args.only_files.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                if line.strip()
            }
        except OSError as exc:
            print(f"  cannot read {args.only_files} ({exc}); not filtering")
        else:
            if listed:
                only = listed
            else:
                print(
                    f"  {args.only_files} is empty; not filtering by PR files"
                )

    filtered, stats = filter_diff(diff, only)
    filtered, partial, omitted = pack_to_budget(filtered, args.max_bytes)

    try:
        args.out.write_text(filtered, encoding="utf-8")
    except OSError as exc:
        return report_infra_fault(
            infra_fault(CHECKER, f"cannot write {args.out}: {exc}")
        )

    if args.unreviewed_out:
        try:
            args.unreviewed_out.write_text(
                "".join(f"{p}\n" for p in omitted), encoding="utf-8"
            )
        except OSError as exc:
            return report_infra_fault(
                infra_fault(
                    CHECKER, f"cannot write {args.unreviewed_out}: {exc}"
                )
            )

    if stats["foreign"]:
        # Worth its own line in the log: a large number here means the diff
        # was taken across a merge of the base branch, and before this filter
        # existed every one of these was reviewed as if the author wrote it.
        print(
            f"  {len(stats['foreign'])} file(s) in the diff are not part of "
            "this PR (base-branch merge?); dropped before review:"
        )
        for path in stats["foreign"][:MAX_OMITTED_LOGGED]:
            print(f"    not in this PR: {path}")
        if len(stats["foreign"]) > MAX_OMITTED_LOGGED:
            hidden = len(stats["foreign"]) - MAX_OMITTED_LOGGED
            print(f"    ...and {hidden} more")
    for path, reason, churn in stats["skipped"]:
        print(f"  skipped {path} ({reason}, {churn} lines)")
    for path in partial:
        print(f"  partial {path} (did not fit whole)")
    if omitted:
        print(
            f"  {len(omitted)} file(s) did not fit the "
            f"{args.max_bytes}-byte prompt budget:"
        )
        for path in omitted[:MAX_OMITTED_LOGGED]:
            print(f"    unreviewed: {path}")
        if len(omitted) > MAX_OMITTED_LOGGED:
            hidden = len(omitted) - MAX_OMITTED_LOGGED
            print(f"    ...and {hidden} more (all of them in --unreviewed-out)")

    lines = stats["reviewable_lines"]
    budget = budget_for(lines)
    # Later rounds shrink. Telling the model to aim for 5 while the poster
    # will keep 1 wastes four findings and makes the log a puzzle.
    if args.budget_ceiling > 0:
        budget = min(budget, args.budget_ceiling)
    reviewable = "true" if stats["kept_files"] and lines else "false"
    print(
        f"{stats['kept_files']} file(s) / {lines} lines reviewable, "
        f"{len(stats['skipped'])} file(s) skipped; "
        f"budget {budget} comment(s) per lane."
    )

    outputs = "\n".join(
        [
            f"reviewable={reviewable}",
            f"reviewable_lines={lines}",
            f"budget={budget}",
            f"kept_files={stats['kept_files']}",
            f"skipped_files={len(stats['skipped'])}",
        ]
    )
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(outputs + "\n")
    else:
        print(outputs)

    return EXIT_OK


if __name__ == "__main__":
    # guard(): an unhandled exception must surface as a CI fault naming this
    # checker, not as a bare traceback under a "problem with your PR" banner.
    sys.exit(guard(CHECKER, main))
