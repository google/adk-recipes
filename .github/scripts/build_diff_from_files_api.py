#!/usr/bin/env python3
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
"""
Rebuild a PR's unified diff from the paginated files endpoint.

Used by .github/workflows/_ai-pr-review-core.yml when `gh pr diff` cannot
serve one.

THE PROBLEM. `gh pr diff` reads GET /repos/{owner}/{repo}/pulls/{n} with the
diff media type. That endpoint does not paginate, so it answers HTTP 406
"PullRequest.diff too_large" above 300 changed files or 20000 diff lines, and
the answer is deterministic — retrying is dead time. #2666 (216 files,
561225 lines) got nothing reviewed because three CSVs and a handful of
lockfiles took the whole pull request over the line limit.

THE WAY ROUND IT. GET /repos/{owner}/{repo}/pulls/{n}/files DOES paginate, and
each record carries that file's own `patch` — a real unified diff body with
real `@@` headers. Concatenating those with reconstructed file headers gives
back a diff that every downstream reader here already understands, and no
single request has to carry the whole pull request.

WHAT COMES BACK IS NOT THE WHOLE DIFF, AND THAT IS THE POINT. GitHub omits
`patch` for binary files, for files with no textual change, and for very
large ones. On #2666 that was 18 of 216 records: the .webp images, five
uv.lock files, and the three enormous CSV/SQL fixtures. Every one of those is
already discarded by prepare_review_diff.py, whose SKIP_PATTERNS cover
lockfiles and whose BULK_DATA_EXT covers the data files. So the set the API
will not send is almost exactly the set the pipeline throws away, and what
survives is the reviewable part of the pull request.

The omissions are still reported rather than silently dropped: a reviewer
shown a partial diff and told it is whole will describe a change that is not
the change under review.

Usage:
  gh api "repos/${REPO}/pulls/${N}/files" --paginate \
    --jq '.[] | {filename,previous_filename,status,patch,changes} | @json' \
    | python3 build_diff_from_files_api.py --out pr_diff.txt

Inputs:
  JSONL on stdin (or --files PATH) — one file record per line. `--jq '.[]'`
  is what makes it one-record-per-line: `gh api --paginate` concatenates a
  JSON ARRAY per page, and those cannot simply be parsed as one document.

Outputs (to --github-output, or stdout when absent):
  files_total     records read
  files_patched   records that carried a patch and reached the diff
  files_omitted   records with no patch, listed in the log

Exit codes:
  0  diff written (even when empty — `files_patched=0` says so)
  2  CI fault — stdin unreadable, malformed JSON, or the output unwritable
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ci_message import (  # noqa: E402
    EXIT_OK,
    guard,
    infra_fault,
    report_infra_fault,
)

CHECKER = "build_diff_from_files_api.py"

# How many omitted paths to print. The count above the list is complete; this
# only bounds the log on a PR with hundreds of them. Matches
# prepare_review_diff.MAX_OMITTED_LOGGED so the two logs read alike.
MAX_OMITTED_LOGGED = 20

# Characters that oblige git to emit a path in its quoted C-string form.
# Anything outside printable ASCII, plus the two characters that would
# otherwise be ambiguous inside the quotes.
_NEEDS_QUOTING = set('"\\') | {chr(c) for c in range(0x20)} | {chr(0x7F)}

_C_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\a": "\\a",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\v": "\\v",
}


def quote_path(path: str) -> str:
    """A path as git would write it in a diff header.

    Git leaves an ordinary path bare and wraps anything else in quotes with
    C-style escapes. The distinction matters because the readers downstream
    (prepare_review_diff._unquote_git_path, post_review_comments._header_path)
    implement git's rule, so a path quoted differently from the way git would
    quote it round-trips into a path that names no file — and a finding
    anchored to it is silently dropped.
    """
    if not any(ch in _NEEDS_QUOTING for ch in path) and path.isascii():
        return path

    out = ['"']
    for ch in path:
        if ch in _C_ESCAPES:
            out.append(_C_ESCAPES[ch])
        elif ch.isascii() and (ord(ch) < 0x20 or ord(ch) == 0x7F):
            out.append(f"\\{ord(ch):03o}")
        elif not ch.isascii():
            # Git escapes non-ASCII byte by byte, in octal, from UTF-8.
            out.extend(f"\\{byte:03o}" for byte in ch.encode("utf-8"))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def section_for(record: dict) -> str | None:
    """One file's diff section, or None when the API sent no patch.

    The header is rebuilt rather than taken from the API, which does not
    supply one. Three lines are load-bearing downstream:

      `diff --git`  splits the diff into per-file sections
      `--- a/...`   distinguishes an addition (/dev/null) from a change
      `+++ b/...`   is where every anchor's path comes from

    An `index` line is deliberately NOT emitted. Nothing here reads it, and
    inventing blob SHAs that do not exist would be worse than omitting it.
    """
    patch = record.get("patch")
    if not patch:
        return None

    new_path = record.get("filename")
    if not new_path:
        return None

    status = record.get("status") or "modified"
    # A rename carries the path it came from; everything else is its own
    # old path. `previous_filename` is absent unless the status is a rename.
    old_path = record.get("previous_filename") or new_path

    if status == "added":
        left = "/dev/null"
    else:
        left = f"a/{quote_path(old_path)}"

    if status == "removed":
        right = "/dev/null"
    else:
        right = f"b/{quote_path(new_path)}"

    lines = [
        f"diff --git a/{quote_path(old_path)} b/{quote_path(new_path)}",
        f"--- {left}",
        f"+++ {right}",
    ]

    # The patch body arrives without a trailing newline. Normalise it here so
    # the next section's `diff --git` starts on its own line; two headers
    # sharing a line would collapse two files into one section.
    body = patch.rstrip("\n")
    lines.append(body)
    return "\n".join(lines) + "\n"


def build(records: list[dict]) -> tuple[str, list[str]]:
    """(reconstructed diff, paths the API sent no patch for)."""
    sections: list[str] = []
    omitted: list[str] = []

    for record in records:
        section = section_for(record)
        if section is None:
            omitted.append(record.get("filename") or "<unknown>")
            continue
        sections.append(section)

    return "".join(sections), omitted


def read_records(stream) -> list[dict]:
    """Parse JSONL, skipping blank lines.

    A malformed line is a CI fault rather than a record to skip: it means the
    `--jq` shape changed, and silently dropping files would hand the reviewer
    a diff that is missing part of the pull request without saying so.
    """
    records: list[dict] = []
    for number, line in enumerate(stream, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {number} is not JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(
                f"line {number} is {type(record).__name__}, expected an object"
            )
        records.append(record)
    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rebuild a PR diff from the paginated files endpoint.",
    )
    parser.add_argument(
        "--files",
        type=Path,
        help="JSONL of file records; defaults to stdin.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Where to write the reconstructed unified diff.",
    )
    parser.add_argument(
        "--omitted-out",
        type=Path,
        help="Where to write the paths the API sent no patch for, one per line.",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        help="$GITHUB_OUTPUT; counts go to stdout when absent.",
    )
    return parser


def _write(path: Path, text: str, *, append: bool = False) -> None:
    """Write a file, or raise OSError naming the path that failed.

    `guard` would turn a bare OSError into a CI fault anyway, but it can only
    report what the exception says — and "Permission denied: '/x/y'" in a job
    that writes three different files does not say which write was the one
    that mattered. Naming the path here is the difference between a fault
    someone can act on and one they have to reproduce.
    """
    try:
        with path.open("a" if append else "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        raise OSError(f"cannot write {path}: {exc}") from exc


def run() -> int:
    args = build_parser().parse_args()

    try:
        if args.files:
            text = args.files.read_text(encoding="utf-8", errors="replace")
            records = read_records(text.splitlines())
        else:
            records = read_records(sys.stdin)
    except (OSError, ValueError) as exc:
        return report_infra_fault(
            infra_fault(CHECKER, f"cannot read the file records: {exc}")
        )

    diff, omitted = build(records)

    try:
        _write(args.out, diff)
        if args.omitted_out:
            _write(
                args.omitted_out, "".join(f"{path}\n" for path in omitted)
            )
    except OSError as exc:
        return report_infra_fault(infra_fault(CHECKER, str(exc)))

    patched = len(records) - len(omitted)
    print(
        f"  rebuilt {patched} of {len(records)} file(s) "
        f"into {len(diff.encode('utf-8'))} bytes of diff"
    )
    if omitted:
        # Named, not just counted. These are the files the reviewer will not
        # see, and "18 files omitted" does not tell anyone whether that was
        # three lockfiles or three hand-written modules.
        print(
            f"  {len(omitted)} file(s) carried no patch "
            "(binary, unchanged, or too large for the API):"
        )
        for path in omitted[:MAX_OMITTED_LOGGED]:
            print(f"    {path}")
        if len(omitted) > MAX_OMITTED_LOGGED:
            print(f"    ... and {len(omitted) - MAX_OMITTED_LOGGED} more")

    lines = [
        f"files_total={len(records)}",
        f"files_patched={patched}",
        f"files_omitted={len(omitted)}",
    ]
    # Appended, never truncated: $GITHUB_OUTPUT is shared with every other
    # step in the job, and opening it with "w" would silently discard
    # everything written before this ran.
    if args.github_output:
        try:
            _write(
                args.github_output,
                "".join(f"{line}\n" for line in lines),
                append=True,
            )
        except OSError as exc:
            return report_infra_fault(infra_fault(CHECKER, str(exc)))
    else:
        print("\n".join(lines))

    return EXIT_OK


def main() -> int:
    return guard(CHECKER, run)


if __name__ == "__main__":
    raise SystemExit(main())
