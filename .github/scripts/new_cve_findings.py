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
Report the vulnerabilities a pull request adds to its lockfiles.

Invoked by .github/workflows/dependency-cve-scan.yml with two osv-scanner
JSON reports: one for the PR's changed lockfiles, one for the same lockfiles
as they are on the base branch. A finding is NEW when the base report has no
finding for the same lockfile and package that shares any of its IDs or
aliases.

Why this exists
---------------
A lockfile a PR changes still carries every vulnerability it already had on
the base branch. Gating on the whole lockfile fails any PR that re-locks a
recipe on vulnerabilities the PR did not introduce, often ones published
after it was opened. #2702 bumped litellm to a version with fewer advisories
and failed on seven, every one of them already on main.

Matching ignores the package VERSION on purpose. A bump that leaves an
advisory unfixed is not new exposure, and without that the fewer-advisories
bump above would still fail. IDs and aliases are pooled per osv-scanner
group, because one advisory can surface as a GHSA in one report and a PYSEC
in another.

Exit codes
----------
  0  no new vulnerabilities
  1  at least one new vulnerability (listed on stdout)
  2  a report could not be read; the caller must not treat this as a pass

Python also exits 1 on a crash that escapes before guard() runs (a failed
import, say), which would read as "new vulnerabilities". So `--new-count`
names a file the script writes only when it completes, holding the number of
new findings. The workflow trusts an exit code only when that file agrees.

Usage
-----
  new_cve_findings.py --head head.json --head-root . \\
      --base base.json --base-root /tmp/base

`--base` may be omitted when no changed lockfile exists on the base branch;
every head finding is then new.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ci_message import guard, infra_fault, report_infra_fault

CHECKER = "new_cve_findings.py"


@dataclass(frozen=True)
class Finding:
    lockfile: str
    ecosystem: str
    package: str
    version: str
    ids: frozenset[str]

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.lockfile, self.ecosystem, self.package)

    @property
    def primary_id(self) -> str:
        return sorted(self.ids)[0]


def _relative(path: str, root: str) -> str:
    return os.path.relpath(os.path.realpath(path), os.path.realpath(root))


def findings(report: dict, root: str) -> list[Finding]:
    """Flatten an osv-scanner JSON report into one Finding per group."""
    found: list[Finding] = []
    for result in report.get("results") or []:
        lockfile = _relative(result["source"]["path"], root)
        for pkg in result.get("packages") or []:
            info = pkg.get("package") or {}
            groups = [
                set(g.get("ids") or []) | set(g.get("aliases") or [])
                for g in pkg.get("groups") or []
            ]
            if not groups:
                # Older reports, or a finding osv-scanner did not group:
                # fall back to each vulnerability with its own aliases.
                groups = [
                    {v["id"], *(v.get("aliases") or [])}
                    for v in pkg.get("vulnerabilities") or []
                ]
            for ids in groups:
                if ids:
                    found.append(
                        Finding(
                            lockfile=lockfile,
                            ecosystem=info.get("ecosystem", ""),
                            package=info.get("name", ""),
                            version=info.get("version", ""),
                            ids=frozenset(ids),
                        )
                    )
    return found


def new_findings(head: list[Finding], base: list[Finding]) -> list[Finding]:
    known: defaultdict[tuple[str, str, str], set[str]] = defaultdict(set)
    for f in base:
        known[f.key].update(f.ids)
    return [f for f in head if not f.ids & known[f.key]]


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--head", required=True)
    parser.add_argument("--head-root", required=True)
    parser.add_argument("--base")
    parser.add_argument("--base-root")
    parser.add_argument(
        "--new-count", help="file to write the number of new findings to"
    )
    args = parser.parse_args()
    if bool(args.base) != bool(args.base_root):
        parser.error("--base and --base-root go together")

    try:
        head = findings(_load(args.head), args.head_root)
        base = findings(_load(args.base), args.base_root) if args.base else []
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return report_infra_fault(
            infra_fault(
                CHECKER,
                f"could not read an osv-scanner report: {exc!r}",
            )
        )

    added = new_findings(head, base)
    if args.new_count:
        Path(args.new_count).write_text(f"{len(added)}\n", encoding="utf-8")
    existing = len(head) - len(added)
    if existing:
        print(
            f"{existing} vulnerability group(s) in the changed lockfiles "
            "already exist on the base branch; not failing on those."
        )
    if not added:
        print("No vulnerabilities introduced by this pull request.")
        return 0
    print(
        f"{len(added)} vulnerability group(s) introduced by this pull request:"
    )
    for f in sorted(added, key=lambda f: (f.lockfile, f.package, f.primary_id)):
        print(
            f"  https://osv.dev/{f.primary_id}  {f.ecosystem} {f.package} "
            f"{f.version}  ({f.lockfile})"
        )
    return 1


if __name__ == "__main__":
    # guard(): any other crash is reported as a CI fault (exit 2), which the
    # workflow treats as "could not compare", never as a pass.
    sys.exit(guard(CHECKER, main))
