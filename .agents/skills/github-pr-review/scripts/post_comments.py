#!/usr/bin/env python3
"""Post PR review comments one at a time, paced, like a human typing them.

Each comment is posted via POST /repos/{owner}/{repo}/pulls/{n}/comments, which
creates a standalone review comment with its own implicit review id -- the exact
shape GitHub produces when a person types a comment on a line in the web UI.
(Verified against real manual comments: each carries a distinct
pull_request_review_id.)

Every comment is validated against the PR's diff hunks BEFORE anything is posted,
so an unaddressable line fails the whole run up front rather than halfway through
leaving a partial review behind.

Usage:
    post_comments.py --repo owner/name --pr 123 --file comments.json [--dry-run]
    post_comments.py --repo owner/name --pr 123 --file comments.json --min-gap 10 --max-gap 20

comments.json:
    [
      {"path": "src/api.py", "line": 42, "body": "missing await here"},
      {"path": "src/api.py", "line": 88, "body": "Can we use `subprocess.run()` here?"}
    ]

Optional per-comment keys:
    "side":        "RIGHT" (default) or "LEFT" for a deleted line
    "start_line":  for a multi-line comment (must also be in the diff)

State: writes <file>.posted alongside the input recording which comments landed,
so re-running after an interruption skips them instead of double-posting.
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def gh_api(path, method="GET", payload=None):
    """Call the GitHub API through gh, returning parsed JSON."""
    cmd = ["gh", "api", path]
    if method != "GET":
        cmd += ["--method", method, "--input", "-"]
    proc = subprocess.run(  # noqa: PLW1510 -- returncode is inspected below
        cmd,
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh api {path} failed")
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def fetch_pr(repo, pr):
    return gh_api(f"/repos/{repo}/pulls/{pr}")


def fetch_files(repo, pr):
    """All changed files, paginated."""
    files, page = [], 1
    while True:
        batch = gh_api(
            f"/repos/{repo}/pulls/{pr}/files?per_page=100&page={page}"
        )
        if not batch:
            break
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return files


def commentable_lines(patch):
    """Line numbers addressable by a review comment, from a unified diff patch.

    GitHub accepts a comment on any line that appears in the diff -- added lines
    and untouched context lines both count (RIGHT side). Deleted lines are only
    addressable on the LEFT side, tracked separately.
    """
    right, left = set(), set()
    if not patch:
        return right, left
    old_ln = new_ln = 0
    for raw in patch.split("\n"):
        m = HUNK_RE.match(raw)
        if m:
            old_ln, new_ln = int(m.group(1)), int(m.group(3))
            continue
        if not raw:
            continue
        tag = raw[0]
        if tag == "+":
            right.add(new_ln)
            new_ln += 1
        elif tag == "-":
            left.add(old_ln)
            old_ln += 1
        elif tag == " ":
            right.add(new_ln)
            left.add(old_ln)
            old_ln += 1
            new_ln += 1
        # '\' (no newline at EOF) and any other marker advance nothing
    return right, left


def load_state(state_path):
    if os.path.exists(state_path):
        with open(state_path) as fh:
            return json.load(fh)
    return {"posted": []}


def save_state(state_path, state):
    with open(state_path, "w") as fh:
        json.dump(state, fh, indent=2)


def key_of(c):
    return f"{c['path']}:{c.get('line')}:{hash(c['body']) & 0xFFFFFF}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--file", required=True, help="JSON array of comments")
    ap.add_argument(
        "--dry-run", action="store_true", help="validate only, post nothing"
    )
    ap.add_argument(
        "--min-gap", type=int, default=10, help="min seconds between posts"
    )
    ap.add_argument(
        "--max-gap", type=int, default=20, help="max seconds between posts"
    )
    args = ap.parse_args()

    with open(args.file) as fh:
        comments = json.load(fh)
    if not isinstance(comments, list) or not comments:
        sys.exit("comments file must be a non-empty JSON array")

    pr = fetch_pr(args.repo, args.pr)
    head_sha = pr["head"]["sha"]
    if pr.get("state") != "open":
        sys.exit(f"PR #{args.pr} is {pr.get('state')}, refusing to comment")

    files = fetch_files(args.repo, args.pr)
    index = {}
    for f in files:
        index[f["filename"]] = commentable_lines(f.get("patch"))

    # ---- validate everything before posting anything ----
    errors = []
    for i, c in enumerate(comments):
        where = f"comment {i + 1}"
        if not c.get("body", "").strip():
            errors.append(f"{where}: empty body")
            continue
        path = c.get("path")
        if path not in index:
            errors.append(
                f"{where}: '{path}' is not in this PR's changed files"
            )
            continue
        line, side = c.get("line"), c.get("side", "RIGHT")
        if not isinstance(line, int):
            errors.append(f"{where}: missing integer 'line'")
            continue
        right, left = index[path]
        valid = right if side == "RIGHT" else left
        if line not in valid:
            nearby = sorted(valid)[:1] + sorted(valid)[-1:] if valid else []
            hint = (
                f" (addressable {side} lines range {nearby})" if nearby else ""
            )
            errors.append(f"{where}: {path}:{line} is not in the diff{hint}")

    if errors:
        print("Validation failed -- nothing was posted:\n", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Validated {len(comments)} comment(s) against PR #{args.pr} @ {head_sha[:8]}"
    )

    # A pending (unsubmitted) review blocks every individual comment: POST
    # .../comments implicitly opens a pending review and GitHub permits only one
    # per user per PR. Catch it here rather than as a bare 422 on comment 1.
    try:
        me = gh_api("/user").get("login")
        reviews = gh_api(f"/repos/{args.repo}/pulls/{args.pr}/reviews")
        stuck = [
            r
            for r in reviews
            if r.get("state") == "PENDING"
            and (r.get("user") or {}).get("login") == me
        ]
    except Exception:
        stuck = []  # never block posting on a probe failure
    if stuck:
        rid = stuck[0]["id"]
        print(
            f"\nBlocked: you have a PENDING review on this PR (id {rid}).\n"
            "GitHub allows one pending review per user, and it prevents posting\n"
            "individual comments. Submit or discard it first:\n"
            f"  submit:  gh api -X POST /repos/{args.repo}/pulls/{args.pr}/reviews/{rid}/events "
            "-f event=COMMENT\n"
            f"  discard: gh api -X DELETE /repos/{args.repo}/pulls/{args.pr}/reviews/{rid}\n"
            "Then re-run this command.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.dry_run:
        for c in comments:
            print(f"\n--- {c['path']}:{c['line']}\n{c['body']}")
        print("\nDry run -- nothing posted.")
        return

    state_path = args.file + ".posted"
    state = load_state(state_path)
    already = set(state["posted"])

    pending = [c for c in comments if key_of(c) not in already]
    if len(pending) < len(comments):
        print(
            f"Resuming: {len(comments) - len(pending)} already posted, {len(pending)} to go"
        )

    for i, c in enumerate(pending):
        payload = {
            "body": c["body"],
            "commit_id": head_sha,
            "path": c["path"],
            "line": c["line"],
            "side": c.get("side", "RIGHT"),
        }
        if "start_line" in c:
            payload["start_line"] = c["start_line"]
            payload["start_side"] = c.get("start_side", payload["side"])

        try:
            res = gh_api(
                f"/repos/{args.repo}/pulls/{args.pr}/comments", "POST", payload
            )
        except RuntimeError as e:
            print(f"\nFailed on {c['path']}:{c['line']}: {e}", file=sys.stderr)
            print(
                f"Progress saved to {state_path}; re-run to resume.",
                file=sys.stderr,
            )
            save_state(state_path, state)
            sys.exit(1)

        state["posted"].append(key_of(c))
        save_state(state_path, state)
        print(
            f"[{i + 1}/{len(pending)}] {c['path']}:{c['line']} -> {res.get('html_url', 'posted')}"
        )

        if i < len(pending) - 1:
            gap = random.randint(args.min_gap, args.max_gap)
            print(f"      waiting {gap}s...")
            time.sleep(gap)

    print(f"\nDone. {len(pending)} comment(s) posted.")
    if os.path.exists(state_path):
        os.remove(state_path)


if __name__ == "__main__":
    main()
