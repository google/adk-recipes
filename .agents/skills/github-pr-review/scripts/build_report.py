#!/usr/bin/env python3
"""Render reviewed candidates as a markdown report.

The point of the report is that the reader can settle a comment WITHOUT
opening a file, so the code window travels next to the comment everywhere.

    build_report.py --candidates cands.json --repo owner/name --pr 123 \
        --out-md <dir>/pr-123-review.md

`--out-csv` is optional and off by default: it carried the same rows as the
markdown tables, so writing both left two copies of one report on disk.

Input JSON: an array of candidate objects.

    path          repo-relative path (required)
    line          int (required)
    comment       the exact text that will be posted (required; `body` also accepted)
    severity      critical | no_critical
    window        source text at the anchor, one "NNN: code" per line
    verify_steps  what the author does to settle it
    what          the blunt technical finding behind the comment
    evidence      how the lane knew

Markdown layout is summary-table-first, detail-sections-below: the table is for
scanning and settling the easy ones, the sections are for when it isn't enough.
"""

import argparse
import csv
import json
import os
import re
import sys

TABLE_CODE_MAX = 68  # chars of source shown in a table cell
SEV_SHORT = {"critical": "crit", "no_critical": "no_crit"}


def common_prefix(paths):
    """Longest shared directory prefix, so the table isn't 60 chars of boilerplate."""
    if not paths:
        return ""
    parts = [p.split("/")[:-1] for p in paths]
    shared = []
    # strict=False is the point: paths of different depth stop at the
    # shortest shared prefix, which is exactly the common directory.
    for chunk in zip(*parts, strict=False):
        if len(set(chunk)) != 1:
            break
        shared.append(chunk[0])
    return "/".join(shared) + "/" if shared else ""


def cell(text):
    """Make a string safe inside a markdown table cell."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("|", "\\|")  # an unescaped pipe silently eats the row
        .replace("\n", " ")
        .strip()
    )


def anchor_line(window, line):
    """The single source line at the anchor, pulled out of the window block."""
    if not window:
        return ""
    for raw in str(window).split("\n"):
        m = re.match(r"\s*(\d+)\s*[:|]\s?(.*)$", raw)
        if m and int(m.group(1)) == line:
            return m.group(2).strip()
    # No numbered match — fall back to the longest non-empty line.
    lines = [ln.strip() for ln in str(window).split("\n") if ln.strip()]
    return max(lines, key=len) if lines else ""


def truncate(code):
    code = code.strip()
    return (
        code
        if len(code) <= TABLE_CODE_MAX
        else code[: TABLE_CODE_MAX - 1] + "…"
    )


def code_block_cell(text):
    """A multi-line code window inside one table cell.

    Markdown fences can't live in a cell, so use <code> with <br>. Everything
    must be HTML-escaped first or a stray < eats the rest of the row.
    """
    if not text:
        return ""
    esc = (
        str(text)
        .rstrip()
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")  # pipe would split the cell
        .replace("`", "&#96;")  # source containing ``` would open a fence
    )
    lines = [ln.replace(" ", "&nbsp;") for ln in esc.split("\n")]
    return "<code>" + "<br>".join(lines) + "</code>"


def csv_safe(value):
    """Defuse a spreadsheet formula before it reaches the triage CSV.

    Every string in these rows is derived from the PR's own diff, and the CSV
    exists to be opened in a spreadsheet. Excel and Sheets both execute a cell
    beginning with = + - or @, so a contributor could land a line of source
    that runs a formula on the reviewer's machine. Quoting does not stop it --
    the leading tab does, and it survives a round-trip as the same text.
    """
    s = "" if value is None else str(value)
    return "\t" + s if s[:1] in ("=", "+", "-", "@") else s


def group_key(c):
    return c.get("severity", "no_critical")


def render_table(rows, prefix, start_index):
    out = [
        "| # | file | line | sev | code at that line | comment |",
        "|---|---|---|---|---|---|",
    ]
    for i, c in enumerate(rows, start_index):
        short = (
            c["path"][len(prefix) :]
            if c["path"].startswith(prefix)
            else c["path"]
        )
        code = truncate(anchor_line(c.get("window", ""), c["line"]))
        out.append(
            f"| {i} | `{cell(short)}` | {c['line']} "
            f"| {SEV_SHORT.get(c.get('severity'), '?')} "
            # <code>, not backticks: source lines contain backticks of their own
            # (RST double-backticks especially) and would close the span early.
            f"| {code_block_cell(code)} | {cell(c['comment'])} |"
        )
    return out


def render_detail_table(rows, prefix, start_index):
    """Second table: the full window and how to check it. Still a table."""
    out = [
        "| # | location | code window | to check | underlying finding |",
        "|---|---|---|---|---|",
    ]
    for i, c in enumerate(rows, start_index):
        short = (
            c["path"][len(prefix) :]
            if c["path"].startswith(prefix)
            else c["path"]
        )
        finding = cell(c.get("what", ""))
        if c.get("unchecked"):
            finding += f" **Not checked:** {cell(c['unchecked'])}"
        out.append(
            f"| {i} | `{cell(short)}:{c['line']}` "
            f"| {code_block_cell(c.get('window', ''))} "
            f"| {cell(c.get('verify_steps', ''))} "
            f"| {finding} |"
        )
    return out


def counts(rows):
    from collections import Counter

    return Counter(group_key(c) for c in rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--out-md", required=True)
    ap.add_argument(
        "--out-csv",
        default=None,
        help="optional; same rows as the markdown, for spreadsheet triage",
    )
    ap.add_argument(
        "--dropped",
        type=int,
        default=0,
        help="how many findings the gate discarded",
    )
    ap.add_argument("--title", default="")
    args = ap.parse_args()

    with open(args.candidates, encoding="utf-8") as fh:
        cands = json.load(fh)
    for c in cands:
        if "comment" not in c and "body" in c:
            c["comment"] = c["body"]
        # Candidates come from model-authored JSON, and a lane that omits
        # `line` used to take the whole report down with a KeyError six
        # frames from the cause. Normalise once, here, so every `c["line"]`
        # below is safe and the un-anchorable row still names the file.
        try:
            c["line"] = int(c.get("line") or 0)
        except (TypeError, ValueError):
            c["line"] = 0
    missing = [c for c in cands if not c.get("comment") or not c.get("path")]
    if missing:
        sys.exit(f"{len(missing)} candidate(s) missing path or comment")

    cands.sort(key=lambda c: (group_key(c) != "critical", c["path"], c["line"]))

    # Un-anchorable findings are real but sit outside every diff hunk, so they can
    # never be inline comments; they are offered as one top-level comment instead.
    unanchorable = [c for c in cands if c.get("anchorable") is False]
    inline = [c for c in cands if c.get("anchorable") is not False]

    prefix = common_prefix([c["path"] for c in cands])

    # ---------------- markdown ----------------
    m = []
    m.append(f"# PR #{args.pr} review")
    m.append("")
    m.append(f"**Repo** {args.repo}  ")
    if args.title:
        m.append(f"**PR** {args.title}  ")
    n_crit = sum(1 for c in inline if c.get("severity") == "critical")
    m.append(
        f"**Comments** {len(inline)} ({n_crit} critical)"
        + (f", plus {len(unanchorable)} un-anchorable" if unanchorable else "")
        + "  "
    )
    if args.dropped:
        m.append(
            f"**Gated out** {args.dropped} findings, not cheaply verifiable  "
        )
    m.append("**Status** nothing posted")
    m.append("")
    if prefix:
        m.append(f"Paths below are relative to `{prefix}`")
        m.append("")
    m.append(
        "Every row carries the source line it is anchored to. If you cannot "
        "settle a row from the table alone, that is a defect in the comment, "
        "not in you — say so and it gets cut."
    )
    m.append("")
    if unanchorable:
        nfail = sum(1 for c in unanchorable if c.get("ci") == "fail")
        m.append(
            f"The {len(unanchorable)} un-anchorable "
            f"({nfail} CI-failing) can only go up as one top-level comment — "
            "see the last table."
        )
        m.append("")
    m.append("---")
    m.append("")

    if inline:
        m.append("## Comments")
        m.append("")
        m += render_table(inline, prefix, 1)
        m.append("")

    if unanchorable:
        m.append("## Un-anchorable — one top-level comment")
        m.append("")
        m.append(
            "Real findings whose lines sit outside every diff hunk, so GitHub "
            "cannot take them as inline comments. CI-failing first. Do **not** "
            "pin these to a nearby line to force them through."
        )
        m.append("")
        ua = sorted(
            unanchorable,
            key=lambda c: (c.get("ci") != "fail", c["path"], c["line"]),
        )
        m.append("| # | file | line | CI | rule | finding |")
        m.append("|---|---|---|---|---|---|")
        for i, c in enumerate(ua, 1):
            short = (
                c["path"][len(prefix) :]
                if c["path"].startswith(prefix)
                else c["path"]
            )
            ci = "**FAIL**" if c.get("ci") == "fail" else "advisory"
            m.append(
                f"| {i} | `{cell(short)}` | {c['line']} | {ci} "
                f"| {cell(c.get('rule', ''))} | {cell(c.get('what') or c.get('comment', ''))} |"
            )
        m.append("")

    m.append("---")
    m.append("")
    m.append("## Detail")
    m.append("")
    m.append("Same rows, with the full window and the check procedure.")
    m.append("")
    if inline:
        m += render_detail_table(inline, prefix, 1)
        m.append("")

    os.makedirs(
        os.path.dirname(os.path.expanduser(args.out_md)) or ".", exist_ok=True
    )
    with open(os.path.expanduser(args.out_md), "w", encoding="utf-8") as fh:
        fh.write("\n".join(m) + "\n")

    # ---------------- csv (opt-in) ----------------
    if args.out_csv:
        os.makedirs(
            os.path.dirname(os.path.expanduser(args.out_csv)) or ".",
            exist_ok=True,
        )
        with open(
            os.path.expanduser(args.out_csv),
            "w",
            newline="",
            encoding="utf-8",
        ) as fh:
            wr = csv.writer(fh, quoting=csv.QUOTE_ALL)
            wr.writerow(
                [
                    "path",
                    "line",
                    "severity",
                    "anchorable",
                    "ci",
                    "rule",
                    "comment",
                    "code_at_line",
                    "verify_steps",
                    "code_window",
                ]
            )
            for c in inline + unanchorable:
                wr.writerow(
                    [
                        csv_safe(c["path"]),
                        c["line"],
                        csv_safe(c.get("severity", "")),
                        "no" if c.get("anchorable") is False else "yes",
                        csv_safe(c.get("ci", "")),
                        csv_safe(c.get("rule", "")),
                        csv_safe(c["comment"]),
                        csv_safe(anchor_line(c.get("window", ""), c["line"])),
                        csv_safe(c.get("verify_steps", "")),
                        csv_safe(c.get("window", "")),
                    ]
                )

    print(f"markdown -> {args.out_md}")
    if args.out_csv:
        print(f"csv      -> {args.out_csv}")
    print(
        f"{len(inline)} comments"
        + (f", {len(unanchorable)} un-anchorable" if unanchorable else "")
    )


if __name__ == "__main__":
    main()
