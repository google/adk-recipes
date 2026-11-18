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

"""In-container collector (`snapshot`, `inspect`, `usage`) executed via `Environment.exec`."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from typing import Any

HOME = os.path.expanduser("~")
FOLDERS = ("code", "projects", "scratch", "inbox")
SKIP_DIRS = {".git", ".worktrees", "__pycache__"}
MAX_READ = 8000


def _sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _git(repo: str, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, check=False
    )
    return out.stdout


def files() -> dict[str, str]:
    found = {}
    for name in sorted(os.listdir(HOME)):
        path = os.path.join(HOME, name)
        if not name.startswith(".") and os.path.isfile(path):
            found[name] = _sha(path)
    for top in FOLDERS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(HOME, top)):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                if os.path.isfile(path):
                    found[os.path.relpath(path, HOME)] = _sha(path)
    return found


def repos() -> dict[str, dict]:
    found = {}
    for top in FOLDERS:
        base = os.path.join(HOME, top)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            repo = os.path.join(base, name)
            if not os.path.exists(os.path.join(repo, ".git")):
                continue
            log = _git(repo, "log", "--all", "--format=%H%x09%P%x09%s")
            branches = _git(
                repo,
                "for-each-ref",
                "--format=%(refname:short) %(objectname)",
                "refs/heads",
            )
            found[f"{top}/{name}"] = {
                "commits": [
                    dict(
                        zip(
                            ("sha", "parents", "subject"),
                            line.split("\t", 2),
                            strict=False,
                        )
                    )
                    for line in log.splitlines()
                    if line
                ],
                "branches": dict(
                    line.split(" ", 1)
                    for line in branches.splitlines()
                    if " " in line
                ),
                "head": _git(
                    repo, "symbolic-ref", "--short", "-q", "HEAD"
                ).strip(),
                "status": _git(repo, "status", "--porcelain").splitlines(),
                "worktrees": sorted(
                    os.listdir(os.path.join(repo, ".worktrees"))
                )
                if os.path.isdir(os.path.join(repo, ".worktrees"))
                else [],
            }
    return found


def remotes() -> dict[str, dict[str, str]]:
    base = os.path.join(HOME, ".remotes")
    found = {}
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            refs = _git(
                os.path.join(base, name),
                "for-each-ref",
                "--format=%(refname) %(objectname)",
            )
            found[name] = dict(
                line.split(" ", 1) for line in refs.splitlines() if " " in line
            )
    return found


def run_tests(repo: str, branch: str) -> dict:
    with tempfile.TemporaryDirectory(prefix="eval-tests-") as tmp:
        clone = subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--branch",
                branch,
                os.path.join(HOME, repo),
                tmp + "/r",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if clone.returncode != 0:
            return {"ok": False, "output": clone.stderr[-500:]}
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-t",
                ".",
            ],
            cwd=tmp + "/r",
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        return {
            "ok": run.returncode == 0,
            "output": (run.stdout + run.stderr)[-1200:],
        }


def inspect(request: dict) -> dict:
    texts = {}
    for rel in request.get("read") or []:
        path = os.path.join(HOME, rel)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                texts[rel] = f.read(MAX_READ)
        except OSError as exc:
            texts[rel] = f"<unreadable: {exc}>"
    tests = {
        f"{repo}@{branch}": run_tests(repo, branch)
        for repo, branch in request.get("tests") or []
    }
    diffs = {
        f"{repo}@{branch}": _git(
            os.path.join(HOME, repo), "diff", "--name-only", f"{base}..{branch}"
        ).splitlines()
        for repo, base, branch in request.get("diffs") or []
    }
    return {"texts": texts, "tests": tests, "diffs": diffs}


def usage() -> list[dict]:
    runs = os.path.join(HOME, ".voice-agent", "runs")
    found: list[dict[str, Any]] = []
    if not os.path.isdir(runs):
        return found
    for run_id in sorted(os.listdir(runs)):
        path = os.path.join(runs, run_id, "stdout.jsonl")
        last: dict[str, Any] | None = None
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"result"' in line:
                        try:
                            payload = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(payload, dict) and (
                            payload.get("type") == "result"
                            or "result" in payload
                        ):
                            last = payload
        except OSError:
            continue
        # A run stopped at a gate has no result line; it still counts as a run.
        if last is not None:
            for key in ("result", "response"):
                if isinstance(last.get(key), str):
                    last[key] = last[key][:200]
        found.append({"run_id": run_id, "result": last})
    return found


def main() -> None:
    command = sys.argv[1]
    out: Any
    if command == "snapshot":
        out = {"files": files(), "repos": repos(), "remotes": remotes()}
    elif command == "inspect":
        out = inspect(json.loads(sys.argv[2]))
    elif command == "usage":
        out = usage()
    else:
        raise SystemExit(f"unknown command {command}")
    print(json.dumps(out))


if __name__ == "__main__":
    main()
