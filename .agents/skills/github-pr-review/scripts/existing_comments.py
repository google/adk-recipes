#!/usr/bin/env python3
"""Fetch everything already said on a PR, so the review doesn't repeat it.

    existing_comments.py --repo owner/name --pr 123 --out existing.json

This exists so a PR can be re-reviewed -- by you again, or after a bot or a
colleague has been through it -- and still produce fresh comments.

Three sources, because no single endpoint has all of it:

  GET  /pulls/{n}/comments    inline comments. Carries `line` AND `original_line`;
                              GitHub nulls `line` once a comment goes outdated, and
                              only `original_line` survives.
  GET  /issues/{n}/comments   top-level comments. No line anchor at all, so they can
                              only ever match on text.
  GraphQL reviewThreads       the ONLY source of isResolved / isOutdated. The REST
                              comment payload does not carry either flag.

Emitted records are consumed by verify_findings.py --existing.
"""

import argparse
import json
import subprocess
import sys

GRAPHQL = """
query($owner:String!, $name:String!, $pr:Int!, $cursor:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$pr) {
      reviewThreads(first:100, after:$cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          isOutdated
          comments(first:1) { nodes { path line originalLine body } }
        }
      }
    }
  }
}
"""


def gh(args):
    proc = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"gh {' '.join(args[:2])} failed: {proc.stderr.strip()[:200]}"
        )
    return json.loads(proc.stdout or "null")


def paged(path):
    out, page = [], 1
    while True:
        batch = gh(
            [
                "api",
                f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}",
            ]
        )
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out


def thread_state(repo, pr):
    """(path, line) -> {resolved, outdated}, keyed on the thread's first comment."""
    owner, name = repo.split("/")
    state, cursor = {}, None
    while True:
        data = gh(
            [
                "api",
                "graphql",
                "-f",
                f"query={GRAPHQL}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
                "-F",
                f"pr={pr}",
            ]
            + (["-F", f"cursor={cursor}"] if cursor else [])
        )
        rt = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        for node in rt["nodes"]:
            c = (node.get("comments") or {}).get("nodes") or [{}]
            c = c[0]
            key = (c.get("path"), c.get("line") or c.get("originalLine"))
            state[key] = {
                "resolved": bool(node.get("isResolved")),
                "outdated": bool(node.get("isOutdated")),
            }
        if not rt["pageInfo"]["hasNextPage"]:
            break
        cursor = rt["pageInfo"]["endCursor"]
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()

    inline = paged(f"/repos/{args.repo}/pulls/{args.pr}/comments")
    issues = paged(f"/repos/{args.repo}/issues/{args.pr}/comments")
    try:
        states = thread_state(args.repo, args.pr)
    except SystemExit as e:
        print(
            f"warning: thread state unavailable ({e}); "
            "treating all threads as open",
            file=sys.stderr,
        )
        states = {}

    out = []
    for c in inline:
        line = c.get("line")
        orig = c.get("original_line")
        st = states.get((c.get("path"), line or orig), {})
        user = c.get("user") or {}
        out.append(
            {
                "kind": "inline",
                "path": c.get("path"),
                "line": line,
                "original_line": orig,
                "side": c.get("side") or "RIGHT",
                "body": c.get("body") or "",
                "author": user.get("login"),
                # A bot may be typed as Bot, or be a User account with a [bot] suffix.
                "is_bot": user.get("type") == "Bot"
                or str(user.get("login", "")).endswith("[bot]"),
                "resolved": st.get("resolved", False),
                "outdated": st.get("outdated", False),
            }
        )
    for c in issues:
        user = c.get("user") or {}
        out.append(
            {
                "kind": "issue",
                "path": None,
                "line": None,
                "original_line": None,
                "side": None,
                "body": c.get("body") or "",
                "author": user.get("login"),
                "is_bot": user.get("type") == "Bot"
                or str(user.get("login", "")).endswith("[bot]"),
                "resolved": False,
                "outdated": False,
            }
        )

    if args.out:
        json.dump(out, open(args.out, "w"), indent=1)

    n_inline = sum(1 for c in out if c["kind"] == "inline")
    n_bot = sum(1 for c in out if c["is_bot"])
    n_res = sum(1 for c in out if c["resolved"])
    n_out = sum(1 for c in out if c["outdated"])
    print(
        f"{len(out)} existing comment(s): {n_inline} inline, "
        f"{len(out) - n_inline} top-level"
    )
    print(f"  {n_bot} from bots, {n_res} resolved, {n_out} outdated")
    if n_out:
        print(
            "  outdated threads do NOT block new findings (the code moved), "
            "but their text still counts"
        )
    if args.out:
        print(f"-> {args.out}")


if __name__ == "__main__":
    main()
