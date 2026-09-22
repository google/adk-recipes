#!/usr/bin/env python3
"""Remember which drafted comments the user cut, so they don't come back.

    # after the user approves a subset at Step 6
    rejections.py --record --repo owner/name --pr 123 \
        --candidates all-drafted.json --approved what-they-said-yes-to.json

    rejections.py --show --repo owner/name --pr 123

Suppression of *posted* comments (existing_comments.py) only covers what made it
onto the PR. A comment the user looked at and cut was never posted, so GitHub has
no record of it -- and on the next review of that PR the same finding comes back
and has to be rejected again.

Observed on the PR #2373 re-review: two findings the user had already cut
reappeared, because nothing remembered the decision.

The ledger lives outside the skill directory on purpose. The skill may be kept
untracked inside a repo checkout, where `git clean -xfd` would delete it; losing
the skill is recoverable, losing a record of the user's decisions is not.
"""

import argparse
import json
import os
import time
from pathlib import Path


def ledger_dir():
    return (
        Path(
            os.environ.get(
                "GH_PR_REVIEW_STATE",
                Path.home() / ".local" / "state" / "github-pr-review",
            )
        )
        / "rejected"
    )


def ledger_path(repo, pr):
    return ledger_dir() / f"{repo.replace('/', '__')}__{pr}.json"


def load(repo, pr):
    p = ledger_path(repo, pr)
    if not p.exists():
        return []
    try:
        return json.load(open(p))
    except (json.JSONDecodeError, OSError):
        return []


def record(repo, pr, rejected):
    """Append, de-duplicating on (path, line, comment)."""
    p = ledger_path(repo, pr)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = load(repo, pr)
    seen = {(e.get("path"), e.get("line"), e.get("comment")) for e in existing}
    added = 0
    for r in rejected:
        key = (r.get("path"), r.get("line"), r.get("comment"))
        if key in seen:
            continue
        existing.append(
            {
                "path": r.get("path"),
                "line": r.get("line"),
                "comment": r.get("comment") or r.get("body") or "",
                "what": r.get("what", ""),
                "rejected_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
            }
        )
        seen.add(key)
        added += 1
    json.dump(existing, open(p, "w"), indent=1)
    return added, p


def _key(c):
    return (
        c.get("path"),
        c.get("line"),
        (c.get("comment") or c.get("body") or ""),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--candidates", help="everything that was drafted")
    ap.add_argument(
        "--approved", help="what the user approved; the rest is rejected"
    )
    args = ap.parse_args()

    if args.show or not args.record:
        entries = load(args.repo, args.pr)
        if not entries:
            print(f"no recorded rejections for {args.repo}#{args.pr}")
            return
        print(f"{len(entries)} previously rejected on {args.repo}#{args.pr}:")
        for e in entries:
            print(f"  {e['path']}:{e['line']}  {e['comment'][:70]}")
        print(f"\n{ledger_path(args.repo, args.pr)}")
        return

    if not args.candidates:
        raise SystemExit("--record needs --candidates")
    cands = json.load(open(args.candidates))
    approved = json.load(open(args.approved)) if args.approved else []
    ok = {_key(c) for c in approved}
    rejected = [c for c in cands if _key(c) not in ok]

    if not rejected:
        print("nothing rejected; ledger unchanged")
        return
    added, path = record(args.repo, args.pr, rejected)
    print(f"recorded {added} rejection(s) for {args.repo}#{args.pr}")
    for r in rejected:
        print(
            f"  {r.get('path')}:{r.get('line')}  "
            f"{(r.get('comment') or r.get('body') or '')[:70]}"
        )
    print(f"-> {path}")


if __name__ == "__main__":
    main()
