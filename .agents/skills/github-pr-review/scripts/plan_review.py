#!/usr/bin/env python3
"""Turn a PR into a review plan: what to skip, how many comments, how to shard.

Produces the same plan every run, so the checkpoint the user sees is consistent
instead of hand-rolled from raw JSON each time.

    plan_review.py --repo owner/name --pr 123
    plan_review.py --repo owner/name --pr 123 --json

Human output is a size table plus the lane assignments. --json emits:

    {
      "repo": ..., "pr": ..., "head_sha": ...,
      "churn": {"total": 1430, "reviewable": 1180, "skipped": 250},
      "budget": {"low": 8, "high": 12, "mid": 10},
      "fan_out": true,
      "lanes": [{"id": 1, "churn": 320, "files": ["src/a.py", ...]}, ...],
      "skipped": [{"path": "pnpm-lock.yaml", "churn": 250, "reason": "lockfile"}],
      "post_eta_seconds": 250
    }

The budget is computed on REVIEWABLE churn only -- a PR that is 1,400 lines of
regenerated lockfile and 80 lines of hand-written code earns a small-PR budget.
"""

import argparse
import json
import math
import re
import subprocess
import sys

# --- what never earns a review comment -------------------------------------
# (pattern, reason) -- matched against the full path, case-insensitively.
SKIP_PATTERNS = [
    (
        r"(^|/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|poetry\.lock|Cargo\.lock|Gemfile\.lock|composer\.lock|go\.sum|uv\.lock)$",
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
        r"\.(png|jpe?g|gif|svg|ico|webp|pdf|woff2?|ttf|eot|zip|tar|gz|jar|so|dylib|dll)$",
        "binary asset",
    ),
    (r"(^|/)testdata/", "test fixture"),
    (r"(^|/)fixtures?/", "test fixture"),
]

# A data file this large is a fixture dump whatever it is named.
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

# Comment budget by reviewable churn: (max_churn, low, high). Mirrors SKILL.md.
BUDGET_TABLE = [
    (50, 2, 3),
    (200, 3, 5),
    (600, 5, 8),
    (1500, 8, 12),
    (math.inf, 12, 20),
]

# Fan out only when the work is big enough to pay for the spawn overhead.
FAN_OUT_MIN_CHURN = 400
FAN_OUT_MIN_FILES = 8
CHURN_PER_LANE = 500
# 12, not 5: a 100k-line PR at 5 lanes gives each worker 20k lines, which is not a
# review, it is a skim. Lanes are cheap; unread code is not.
MAX_LANES = 12

# post_comments.py sleeps a random 10-20s between posts.
AVG_POST_GAP_SECONDS = 15


def gh_api(path):
    proc = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise SystemExit(f"gh api {path} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def fetch_files(repo, pr):
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


# Optional scope carve-outs, driven by the Step 2 question. Tests are the richest
# source of cheaply-verifiable defects, so they are IN by default; --no-tests is
# for a security-focused complete review where the production surface matters more.
TEST_DIR_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/", re.IGNORECASE)
WEB_DIR_RE = re.compile(r"(^|/)(web|frontend|client|ui)/", re.IGNORECASE)


def skip_reason(path, churn, *, include_tests=True, include_web=True):
    for pattern, reason in SKIP_PATTERNS:
        if re.search(pattern, path, re.IGNORECASE):
            return reason
    if not include_tests and TEST_DIR_RE.search(path):
        return "tests excluded"
    if not include_web and WEB_DIR_RE.search(path):
        return "web excluded"
    ext = (
        path[path.rfind(".") :].lower()
        if "." in path.rsplit("/", 1)[-1]
        else ""
    )
    if ext in BULK_DATA_EXT and churn >= BULK_DATA_CHURN:
        return "bulk data"
    return None


def budget_for(churn):
    for ceiling, low, high in BUDGET_TABLE:
        if churn <= ceiling:
            return low, high
    return 12, 20


def lane_count(churn, file_count):
    """1 means review inline; >1 means fan out."""
    if churn < FAN_OUT_MIN_CHURN and file_count < FAN_OUT_MIN_FILES:
        return 1
    n = max(2, math.ceil(churn / CHURN_PER_LANE))
    return max(1, min(n, MAX_LANES, file_count))


# Directory components that describe layout rather than subject matter, so a test
# and the thing it tests resolve to the same affinity key.
STRUCTURAL_DIRS = {
    "tests",
    "test",
    "__tests__",
    "spec",
    "specs",
    "src",
    "lib",
    "scripts",
}
TEST_AFFIXES = [
    (r"^test_", ""),
    (r"_test$", ""),
    (r"^Test", ""),
    (r"\.test$", ""),
    (r"\.spec$", ""),
    (r"_spec$", ""),
]


def affinity_key(path):
    """Group a source file with its tests, so one lane reviews both together.

    `tools/validate.py` and `tools/tests/test_validate.py` both key to
    `tools/validate`. A reviewer holding only one of the pair cannot tell whether
    the test still covers the code.
    """
    parts = path.split("/")
    name = parts[-1]
    stem = name[: name.rfind(".")] if "." in name else name
    for pattern, repl in TEST_AFFIXES:
        stem = re.sub(pattern, repl, stem)
    dirs = [d for d in parts[:-1] if d.lower() not in STRUCTURAL_DIRS]
    return "/".join([*dirs, stem.lower()])


def pack(files, n):
    """Longest-processing-time-first bin packing over affinity groups.

    Groups keep related files in one lane; LPT keeps the lanes finishing together.
    """
    groups = {}
    for f in files:
        groups.setdefault(affinity_key(f["path"]), []).append(f)

    # A group heavier than twice an even share would wreck the balance on its own;
    # cohesion is worth less than a lane that never finishes.
    ideal = sum(f["churn"] for f in files) / n if n else 0
    units = []
    for members in groups.values():
        churn = sum(f["churn"] for f in members)
        if churn > 2 * ideal and len(members) > 1:
            units.extend([{"churn": f["churn"], "files": [f]} for f in members])
        else:
            units.append({"churn": churn, "files": members})

    lanes = [{"id": i + 1, "churn": 0, "files": []} for i in range(n)]
    for unit in sorted(units, key=lambda u: -u["churn"]):
        lane = min(lanes, key=lambda x: x["churn"])
        # Heaviest file first, so the lane's prompt leads with the substantive work.
        lane["files"].extend(
            f["path"] for f in sorted(unit["files"], key=lambda f: -f["churn"])
        )
        lane["churn"] += unit["churn"]
    return [x for x in lanes if x["files"]]


def build_plan(repo, pr, *, include_tests=True, include_web=True):
    meta = gh_api(f"/repos/{repo}/pulls/{pr}")
    raw = fetch_files(repo, pr)

    reviewable, skipped = [], []
    for f in raw:
        churn = f.get("additions", 0) + f.get("deletions", 0)
        entry = {
            "path": f["filename"],
            "churn": churn,
            "status": f.get("status"),
        }
        reason = skip_reason(
            f["filename"],
            churn,
            include_tests=include_tests,
            include_web=include_web,
        )
        if reason:
            skipped.append({**entry, "reason": reason})
        elif f.get("status") == "removed":
            skipped.append({**entry, "reason": "file deleted"})
        elif f.get("status") == "renamed" and churn == 0:
            # Byte-identical, just moved. Nobody re-reviews code that didn't
            # change; on a migration PR this is most of the diff.
            skipped.append({**entry, "reason": "pure rename"})
        else:
            reviewable.append(entry)

    r_churn = sum(f["churn"] for f in reviewable)
    s_churn = sum(f["churn"] for f in skipped)
    low, high = budget_for(r_churn)
    n = lane_count(r_churn, len(reviewable))

    return {
        "repo": repo,
        "pr": pr,
        "title": meta.get("title"),
        "head_sha": meta.get("head", {}).get("sha"),
        "state": meta.get("state"),
        "churn": {
            "total": r_churn + s_churn,
            "reviewable": r_churn,
            "skipped": s_churn,
        },
        "budget": {"low": low, "high": high, "mid": (low + high) // 2},
        "fan_out": n > 1,
        "lanes": pack(reviewable, n),
        "skipped": sorted(skipped, key=lambda f: -f["churn"]),
        "post_eta_seconds": ((low + high) // 2) * AVG_POST_GAP_SECONDS,
    }


def render(plan):
    out = []
    churn = plan["churn"]
    n_files = sum(len(lane["files"]) for lane in plan["lanes"])
    out.append(f"PR #{plan['pr']} - {plan['title']}")
    out.append(
        f"  {churn['total']} changed lines; {churn['reviewable']} reviewable "
        f"across {n_files} file(s)"
    )

    if plan["skipped"]:
        by_reason = {}
        for f in plan["skipped"]:
            by_reason.setdefault(f["reason"], 0)
            by_reason[f["reason"]] += 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(by_reason.items()))
        out.append(
            f"  skipped {len(plan['skipped'])} file(s) / {churn['skipped']} lines: {summary}"
        )

    b = plan["budget"]
    out.append(
        f"  budget: {b['low']}-{b['high']} comments "
        f"(post run ~{plan['post_eta_seconds'] // 60} min)"
    )
    out.append("")

    if plan["fan_out"]:
        out.append(
            f"Fan out: {len(plan['lanes'])} file lanes + 1 cross-cutting lane"
        )
        for lane in plan["lanes"]:
            out.append(
                f"  lane {lane['id']}  {lane['churn']:>5} lines  "
                f"{len(lane['files'])} file(s)"
            )
            for p in lane["files"]:
                out.append(f"           {p}")
    else:
        out.append("Below fan-out threshold: review inline, no sub-agents.")
        for lane in plan["lanes"]:
            for p in lane["files"]:
                out.append(f"  {p}")

    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--json", action="store_true", help="emit the plan as JSON")
    ap.add_argument(
        "--no-tests",
        action="store_true",
        help="exclude test directories from review scope",
    )
    ap.add_argument(
        "--no-web",
        action="store_true",
        help="exclude web/frontend directories from review scope",
    )
    args = ap.parse_args()

    plan = build_plan(
        args.repo,
        args.pr,
        include_tests=not args.no_tests,
        include_web=not args.no_web,
    )
    if plan["state"] != "open":
        print(f"warning: PR #{args.pr} is {plan['state']}", file=sys.stderr)
    if not plan["lanes"]:
        sys.exit(
            "Nothing reviewable in this PR -- every changed file was skipped."
        )

    print(json.dumps(plan, indent=2) if args.json else render(plan))


if __name__ == "__main__":
    main()
