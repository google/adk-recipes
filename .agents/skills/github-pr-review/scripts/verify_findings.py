#!/usr/bin/env python3
"""Machine-verify lane findings before any of them becomes a comment.

    verify_findings.py --findings raw.json --repo-root /tmp/pr-123 \
        --repo owner/name --pr 123 --out verified.json

Every check here is one I have done by hand during a review and would otherwise
forget. Prose in SKILL.md saying "verify the windows" is not a control; this is.

Four checks:

1. **Window vs file.** A lane reports the source lines at its anchor. If those lines
   do not match the real file, the finding is fabricated -- a lane that invents a
   finding invents the source too. REJECTED, not downgraded.

2. **Addressability.** GitHub only accepts an inline comment on a line inside a diff
   hunk. Findings outside are marked `anchorable: false` and kept for a single
   top-level comment. On PR #2373 that was a third of the pool.

3. **`verify_steps` gate.** If the stated check procedure names a second file or says
   "trace" / "assuming" / "consider the case where", the finding is not cheaply
   verifiable whatever the lane labelled it. Forces `cheap: not_cheap`.

4. **Schema.** Missing path/line/what, or a line outside the file, is a broken
   finding, not a judgement call.

Exit code is 0 even when findings are rejected -- rejection is a normal result. It
is non-zero only when the input itself cannot be processed.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata

# Phrases in verify_steps that mean the reader must leave the anchored lines.
NOT_CHEAP_MARKERS = re.compile(
    r"\btrace\b|\bassum\w*\b|consider the case|if an attacker|"
    r"\bsimulat\w*\b|\bimagine\b|run the code|execute\b|another file|"
    r"\bgrep the repo\b|across (the )?(repo|codebase)",
    re.IGNORECASE,
)
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
WINDOW_LINE_RE = re.compile(r"^\s*(\d+)\s*[:|]\s?(.*)$")


ESCAPE_SEQ = re.compile(
    r"\\u[0-9a-fA-F]{4}|\\U[0-9a-fA-F]{8}|\\x[0-9a-fA-F]{2}"
)


def norm(s):
    """Reduce a source line to a comparable ASCII skeleton.

    A lane emits an emoji as the literal escape text `\\u26a0`; the file holds the
    real character. Round-tripping through unicode_escape does NOT reconcile these
    -- it mojibakes the file side (`⚠` becomes `â\\x9a\\xa0`) and every emoji line
    then looks fabricated. Three false rejections on PR #2373 came from exactly that.

    So compare the ASCII skeleton instead: drop escape sequences, drop non-ASCII,
    drop whitespace. Real fabrication still differs in the ASCII text, which is
    where the substance of a source line lives.
    """
    s = ESCAPE_SEQ.sub("", s)
    s = unicodedata.normalize("NFKC", s)
    return "".join(ch for ch in s if ch.isascii() and not ch.isspace())


def addressable_lines(repo, pr):
    """Map path -> set of RIGHT-side line numbers GitHub will accept."""
    out, page = {}, 1
    while True:
        proc = subprocess.run(
            [
                "gh",
                "api",
                f"/repos/{repo}/pulls/{pr}/files?per_page=100&page={page}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemExit(f"gh api failed: {proc.stderr.strip()[:200]}")
        batch = json.loads(proc.stdout or "[]")
        if not batch:
            break
        for f in batch:
            right, new = set(), 0
            for raw in (f.get("patch") or "").split("\n"):
                m = HUNK_RE.match(raw)
                if m:
                    new = int(m.group(1))
                    continue
                if not raw:
                    continue
                if raw[0] in "+ ":
                    right.add(new)
                    new += 1
            out[f["filename"]] = right
        if len(batch) < 100:
            break
        page += 1
    return out


def _window_pairs(window):
    """[(claimed_line_no, text)] from a window block."""
    out = []
    for raw in str(window or "").split("\n"):
        m = WINDOW_LINE_RE.match(raw)
        if not m:
            continue
        # Lanes abbreviate long lines. A Unicode ellipsis vanishes in norm() as
        # non-ASCII, but an ASCII "..." survives and turns a valid prefix into a
        # mismatch -- which rejected 6 of 146 real findings on PR #2302.
        out.append(
            (int(m.group(1)), re.sub(r"(\.{3}|\u2026)\s*$", "", m.group(2)))
        )
    return out


def _matches_at(pairs, lines, offset):
    """Do all window lines match the file when shifted by `offset`?"""
    checked = 0
    for n, claimed in pairs:
        i = n - 1 + offset
        if not (0 <= i < len(lines)):
            return False, 0
        a, b = norm(claimed), norm(lines[i])
        if not a:
            continue
        # Prefix, not substring: a one-character file line is a substring of
        # almost anything and matched spuriously. Below MIN_PREFIX require
        # equality, so short lines cannot carry a false match.
        if a == b:
            pass
        elif min(len(a), len(b)) >= MIN_PREFIX and (
            a.startswith(b) or b.startswith(a)
        ):
            pass
        else:
            return False, 0
        checked += 1
    return checked > 0, checked


def check_window(finding, repo_root, max_drift=3):
    """(ok, reason). False means the window contradicts the file on disk.

    A window that matches at a CONSISTENT offset is line drift, not fabrication --
    the finding is real and only its anchor is wrong, so repair it rather than
    discard it. On PR #2302 four findings were off by exactly one line.
    """
    path = os.path.join(repo_root, finding["path"])
    if not os.path.exists(path):
        return False, "file does not exist in the checkout"
    try:
        lines = (
            open(path, encoding="utf-8", errors="replace").read().split("\n")
        )
    except OSError as e:
        return False, f"unreadable: {e}"

    line = finding.get("line")
    if not isinstance(line, int) or not (1 <= line <= len(lines)):
        return False, f"line {line} outside file (1..{len(lines)})"

    pairs = _window_pairs(finding.get("window"))
    if not pairs:
        return True, "no window supplied"  # tolerated, not verified

    ok, _ = _matches_at(pairs, lines, 0)
    if ok:
        return True, "window matches file"

    for offset in [d for k in range(1, max_drift + 1) for d in (k, -k)]:
        ok, n = _matches_at(pairs, lines, offset)
        if ok and n >= 2:  # one line could match by chance
            new_line = line + offset
            if 1 <= new_line <= len(lines):
                finding["line"] = new_line
                finding["_line_corrected"] = f"{line} -> {new_line}"
                return (
                    True,
                    f"window matched at offset {offset:+d}; anchor corrected",
                )

    n, claimed = pairs[0]
    actual = lines[n - 1] if 1 <= n <= len(lines) else ""
    return False, (
        f"window line {n} says {claimed.strip()[:40]!r}, "
        f"file has {actual.strip()[:40]!r}"
    )


# ---------------------------------------------------------------- existing work

PROXIMITY = 2  # same line, or within 2 -- tight, to avoid eating fresh findings
MIN_PREFIX = 8  # below this, a window line must equal the file line exactly

_STOPWORDS = set(
    """a an the is are was were be been being this that these those it
its of to in on for with from by at as and or not no any some all each every into
out here there which what when where line lines file files code value values never
only still also just even than then them they their should does did has have""".split()
)


def _tokens(text):
    return {
        w
        for w in re.findall(r"[a-z_][a-z_0-9]{3,}", str(text).lower())
        if w not in _STOPWORDS
    }


def rejected_as_exclusions(entries):
    """Previously-cut comments, shaped like existing comments for reuse.

    Marked `kind: inline` so they create line-zones, and never `outdated` -- a
    decision the user already made does not expire because the code moved.
    """
    return [
        {
            "kind": "inline",
            "path": e.get("path"),
            "line": e.get("line"),
            "original_line": e.get("line"),
            "body": e.get("comment", ""),
            "author": "you",
            "is_bot": False,
            "resolved": False,
            "outdated": False,
            "_was_rejected": True,
        }
        for e in entries or []
    ]


def build_exclusions(existing, proximity=PROXIMITY):
    """(line_zones, texts) from comments already on the PR.

    An OUTDATED thread contributes no line-zone: the code moved out from under it,
    so that spot deserves a fresh look. Its text still counts, so the same
    observation is not repeated verbatim somewhere else.

    A RESOLVED thread does block -- it was already discussed, and re-raising a
    settled point is worse than missing it.

    Bots block exactly like humans: if a linter already flagged line 42, saying it
    again adds nothing regardless of who said it first.
    """
    zones, texts = {}, []
    for c in existing or []:
        body = c.get("body") or ""
        if body.strip():
            texts.append((_tokens(body), c))
        if c.get("kind") != "inline" or c.get("outdated"):
            continue
        path = c.get("path")
        for ln in (c.get("line"), c.get("original_line")):
            if not path or not ln:
                continue
            for d in range(-proximity, proximity + 1):
                zones.setdefault(path, {}).setdefault(ln + d, c)
    return zones, texts


def already_raised(finding, zones, texts, sim=0.55):
    """(True, reason) if this finding repeats something already on the PR."""
    hit = zones.get(finding.get("path"), {}).get(finding.get("line"))
    if hit:
        if hit.get("_was_rejected"):
            return True, "you cut this comment on a previous review"
        who = "a bot" if hit.get("is_bot") else (hit.get("author") or "someone")
        at = hit.get("line") or hit.get("original_line")
        state = " (resolved)" if hit.get("resolved") else ""
        where = "this line" if at == finding.get("line") else f"line {at}"
        return True, f"{who} already commented on {where}{state}"

    mine = _tokens(f"{finding.get('what', '')} {finding.get('comment', '')}")
    if len(mine) >= 4:
        for toks, c in texts:
            if len(toks) < 4:
                continue
            if len(mine & toks) / min(len(mine), len(toks)) >= sim:
                if c.get("_was_rejected"):
                    return True, "very similar to a comment you cut previously"
                who = (
                    "a bot"
                    if c.get("is_bot")
                    else (c.get("author") or "someone")
                )
                return True, f"{who} already said something very similar"
    return False, ""


# ------------------------------------------------------------ fact anchoring

# Vocabulary that appears when a comment asks the reader to derive, not to see.
# Bare "pattern" was here and fired on legitimate GROUPED findings ("the same
# silent-swallow pattern appears twice more"), which are encouraged. Only the
# "breaks the pattern" shape demands that the reader infer the pattern first.
INFERENCE_WORDS = re.compile(
    r"\bbreaks? the pattern\b|"
    r"\benough\b|\bwould\b|\brestored?\b|\bimpl(y|ies)\b|"
    r"\bpresumably\b|\beffectively\b|\bin practice\b|\bends up\b",
    re.IGNORECASE,
)


def fact_anchor_lint(finding):
    """Advisory. Flags comments that likely ask the reader to derive the defect.

    It cannot decide the rule -- that is a judgement -- but it catches the two
    shapes behind every comment rejected on PR #2373: a claim about identifiers not
    present in the window, and inference vocabulary.
    """
    text = finding.get("comment") or finding.get("what") or ""
    window = str(finding.get("window") or "")
    notes = []

    # Only single-token identifiers. A multi-word backticked phrase is prose
    # quoting code (`make streamlit`, `except Exception: continue`) and will not
    # appear verbatim in the window -- flagging those was pure noise on #2373.
    ticked = [
        tok
        for tok in re.findall(r"`([^`]+)`", text)
        if not re.search(r"[\s:(){}\[\]]", tok)
    ]
    if window and ticked:
        flat = re.sub(r"\W", "", window)
        missing = [
            tok
            for tok in ticked
            if re.sub(r"\W", "", tok) and re.sub(r"\W", "", tok) not in flat
        ]
        if missing:
            notes.append(
                "names "
                + ", ".join(f"`{m}`" for m in missing[:3])
                + " which is not in the window"
            )

    m = INFERENCE_WORDS.search(text)
    if m:
        notes.append(f"inference word {m.group(0)!r}")
    return notes


def red_workflows(repo, sha):
    """Basenames of the workflow files that FAILED on this head commit.

    The rules doc keeps a hand-maintained "Already enforced" table so a reviewer
    does not repeat a red check. A table drifts; the PR's own results do not.
    Only `failure` counts -- a run still queued tells us nothing, and suppressing
    on it would silently drop a real finding.
    """
    proc = subprocess.run(
        [
            "gh",
            "api",
            f"/repos/{repo}/actions/runs?head_sha={sha}&per_page=100",
            "--jq",
            ".workflow_runs[] | [.path, .conclusion] | @tsv",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None  # cannot tell; caller must not suppress
    red = set()
    for line in proc.stdout.splitlines():
        path, _, conclusion = line.partition("\t")
        if conclusion.strip() == "failure" and path.strip():
            red.add(os.path.basename(path.strip()))
    return red


# A finding already cites the check that enforces it in `evidence`, e.g.
# "python-validate-recipe.yml:261-266". Reusing that citation means no second
# rule-to-check mapping table has to be kept in step with anything.
_EVIDENCE_WORKFLOW = re.compile(r"([A-Za-z0-9_.\-]+\.ya?ml)")


def already_red(finding, red):
    """Is a check already failing this PR for exactly this? (reason | None)

    Advisory findings are never suppressed: nothing is failing for them, so the
    comment is the only way the author hears it at all.
    """
    if not red or finding.get("ci") != "fail":
        return None
    for wf in _EVIDENCE_WORKFLOW.findall(str(finding.get("evidence") or "")):
        if wf in red:
            return (
                f"{wf} is already failing on this PR with a precise message; "
                "a comment repeating it lands on an author who is already "
                "looking at a red check"
            )
    return None


def verify(findings, repo_root, addr, existing=None, red=None):
    zones, texts = build_exclusions(existing or [])
    verified, rejected, suppressed = [], [], []
    for f in findings:
        if not f.get("path") or not f.get("what"):
            rejected.append({**f, "_reason": "missing path or what"})
            continue

        ok, reason = check_window(f, repo_root)
        if not ok:
            rejected.append({**f, "_reason": reason})
            continue
        f["_window_check"] = reason

        # Addressability: unknown file means no patch, so not addressable.
        f["anchorable"] = f["line"] in addr.get(f["path"], set())

        # Compute cheapness from verify_steps. Single source of truth: workers
        # no longer assign this, prose no longer re-scores it.
        steps = str(f.get("verify_steps") or "")
        if not steps.strip():
            f["cheap"] = "not_cheap"
            f["_cheap_reason"] = "no verify_steps supplied"
        elif NOT_CHEAP_MARKERS.search(steps):
            f["cheap"] = "not_cheap"
            f["_cheap_reason"] = NOT_CHEAP_MARKERS.search(steps).group(0)
        else:
            f["cheap"] = "cheap"

        dup, why = already_raised(f, zones, texts)
        if dup:
            suppressed.append({**f, "_reason": why})
            continue

        why_red = already_red(f, red)
        if why_red:
            suppressed.append({**f, "_reason": why_red})
            continue

        notes = fact_anchor_lint(f)
        if notes:
            f["_anchor_warnings"] = notes

        verified.append(f)
    return verified, rejected, suppressed


# Words too generic to identify a defect class.
_STOP = set(
    """a an the is are was were be been being this that these those it its
of to in on for with from by at as and or not no any some all each every into out
here there which what when where line lines file files code value values never
only still also just even than then them they their there's is-a""".split()
)


def cluster(findings, min_size=3):
    """Group findings whose `what` shares a distinctive vocabulary.

    Crude on purpose -- it flags candidates for the human to group, it does not
    group anything itself. Five separate "unused import" findings is one comment,
    and on PR #2373 twenty findings collapsed to five classes that way, freeing
    fifteen of twenty budget slots.
    """

    def sig(f):
        words = re.findall(r"[a-z_]{4,}", str(f.get("what", "")).lower())
        return frozenset(w for w in words if w not in _STOP)

    sigs = [(f, sig(f)) for f in findings]
    groups, used = [], set()
    for i, (_fa, sa) in enumerate(sigs):
        if i in used or not sa:
            continue
        members = [i]
        for j, (_fb, sb) in enumerate(sigs[i + 1 :], start=i + 1):
            if j in used or not sb:
                continue
            overlap = len(sa & sb) / max(1, min(len(sa), len(sb)))
            if overlap >= 0.5:
                members.append(j)
        if len(members) >= min_size:
            used.update(members)
            groups.append([sigs[k][0] for k in members])
    return groups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--findings", required=True)
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--repo", help="owner/name; omit to skip addressability")
    ap.add_argument("--pr")
    ap.add_argument("--out", help="write verified findings here")
    ap.add_argument(
        "--existing", help="existing_comments.py output; suppress repeats"
    )
    ap.add_argument(
        "--no-ledger",
        action="store_true",
        help="ignore previously-rejected comments for this PR",
    )
    ap.add_argument(
        "--head-sha",
        help="PR head commit; with --repo, suppresses CI-FAIL "
        "findings whose enforcing workflow is already red",
    )
    ap.add_argument(
        "--no-ci-status",
        action="store_true",
        help="do not read the PR's check results",
    )
    ap.add_argument(
        "--json", action="store_true", help="machine-readable summary"
    )
    args = ap.parse_args()

    raw = json.load(open(args.findings))
    if isinstance(raw, dict):
        raw = raw.get("findings", [])

    addr = {}
    if args.repo and args.pr:
        addr = addressable_lines(args.repo, args.pr)

    existing = []
    if args.existing:
        existing = json.load(open(args.existing))

    # Previously-cut comments load automatically -- the user should not have to
    # remember to pass them, and forgetting means re-proposing rejected findings.
    n_rejected = 0
    if args.repo and args.pr and not args.no_ledger:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import rejections

            prior = rejections.load(args.repo, args.pr)
            existing = existing + rejected_as_exclusions(prior)
            n_rejected = len(prior)
        except Exception as e:  # never block a review on this
            print(
                f"warning: could not load rejection ledger: {e}",
                file=sys.stderr,
            )

    red = None
    if args.repo and args.head_sha and not args.no_ci_status:
        red = red_workflows(args.repo, args.head_sha)
        if red is None:
            print(
                "warning: could not read check results; not suppressing "
                "anything as already-red",
                file=sys.stderr,
            )

    verified, rejected, suppressed = verify(
        raw, os.path.expanduser(args.repo_root), addr, existing, red
    )

    n_unanchor = sum(1 for f in verified if not f.get("anchorable"))
    n_down = sum(1 for f in verified if f.get("cheap") == "not_cheap")
    n_warn = sum(1 for f in verified if f.get("_anchor_warnings"))
    n_fixed = sum(1 for f in verified if f.get("_line_corrected"))
    summary = {
        "input": len(raw),
        "verified": len(verified),
        "rejected": len(rejected),
        "suppressed_as_duplicate": len(suppressed),
        "line_corrected": n_fixed,
        "unanchorable": n_unanchor,
        "not_cheap": n_down,
        "anchor_warnings": n_warn,
    }

    if args.out:
        json.dump(verified, open(args.out, "w"), indent=1)

    if args.json:
        print(
            json.dumps(
                {
                    "summary": summary,
                    "rejected": rejected,
                    "suppressed": suppressed,
                },
                indent=1,
            )
        )
        return

    print(
        f"{summary['input']} findings in, {summary['verified']} verified, "
        f"{summary['rejected']} REJECTED"
    )
    if n_rejected:
        print(
            f"  ({n_rejected} previously-cut comment(s) loaded from the ledger)"
        )
    if rejected:
        print(
            "\nrejected (window contradicts the file -- treat as fabricated):"
        )
        for f in rejected:
            print(f"  {f.get('path')}:{f.get('line')}  {f['_reason']}")
    if n_unanchor:
        print(
            f"\n{n_unanchor} not addressable (outside every diff hunk) -- "
            "these can only go up as one top-level comment:"
        )
        for f in verified:
            if not f.get("anchorable"):
                print(f"  {f['path']}:{f['line']}")
    if n_fixed:
        print(f"\n{n_fixed} anchor(s) corrected for line drift:")
        for f in verified:
            if f.get("_line_corrected"):
                print(f"  {f['path']}  {f['_line_corrected']}")
    if suppressed:
        print(f"\n{len(suppressed)} suppressed — already raised on this PR:")
        for f in suppressed:
            print(f"  {f['path']}:{f['line']}  {f['_reason']}")
    if n_down:
        print(
            f"\n{n_down} not cheaply verifiable (from their own verify_steps):"
        )
        for f in verified:
            if f.get("cheap") == "not_cheap":
                print(
                    f"  {f['path']}:{f['line']}  ({f.get('_cheap_reason', '')})"
                )
    if n_warn:
        print(
            f"\n{n_warn} fact-anchoring warning(s) — check these read as "
            "pointing at something visible:"
        )
        for f in verified:
            for note in f.get("_anchor_warnings", []):
                print(f"  {f['path']}:{f['line']}  {note}")
    groups = cluster(verified)
    if groups:
        print(
            f"\n{len(groups)} repeated class(es) — group each into ONE comment:"
        )
        for g in groups:
            where = ", ".join(
                f"{x['path'].rsplit('/', 1)[-1]}:{x['line']}" for x in g[:3]
            )
            print(f"  {len(g)}x  {g[0]['what'][:70]}")
            print(f"      {where}{' …' if len(g) > 3 else ''}")

    if args.out:
        print(f"\nverified findings -> {args.out}")


if __name__ == "__main__":
    main()
