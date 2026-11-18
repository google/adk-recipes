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
"""Extract Gates B-F from `.agents/skills/triage-recipe-proposal/SKILL.md`.

Used by `.github/workflows/_ai-recipe-proposal-triage-core.yml` so the
triage-recipe-proposal skill is the single source of truth for Gates B-F
without keeping `.agents/` on disk while `agy` executes.
"""

from __future__ import annotations

import argparse
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

CHECKER = "extract_triage_skill_rubric.py"
DEFAULT_SKILL_PATH = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "triage-recipe-proposal"
    / "SKILL.md"
)

REQUIRED_GATES = ("B", "C", "D", "E", "F")
_GATE_B_RE = re.compile(r"^###\s+Gate\s+B\b", re.MULTILINE)
_NEXT_BOUNDARY_RE = re.compile(
    r"^(?:###\s+Gate\s+G\b|###\s+Accept\b|##\s+3\.)", re.MULTILINE
)


def extract_gates_b_to_f(skill_markdown: str) -> str:
    """Return the Gates B-F rubric slice from `SKILL.md`."""
    start_match = _GATE_B_RE.search(skill_markdown)
    if not start_match:
        raise ValueError("Could not find '### Gate B' heading in SKILL.md.")

    tail = skill_markdown[start_match.start() :]
    end_match = _NEXT_BOUNDARY_RE.search(tail)
    section = tail[: end_match.start()] if end_match else tail
    section = section.strip()

    for gate in REQUIRED_GATES:
        if not re.search(rf"^###\s+Gate\s+{gate}\b", section, re.MULTILINE):
            raise ValueError(
                f"Expected '### Gate {gate}' heading inside extracted Gates B-F section."
            )

    return section + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skill",
        type=Path,
        default=DEFAULT_SKILL_PATH,
        help="Path to .agents/skills/triage-recipe-proposal/SKILL.md",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional output file path (prints to stdout if omitted)",
    )
    args = parser.parse_args()

    if not args.skill.is_file():
        return report_infra_fault(
            infra_fault(CHECKER, f"Skill file not found: {args.skill}")
        )

    try:
        raw = args.skill.read_text(encoding="utf-8")
        extracted = extract_gates_b_to_f(raw)
    except Exception as exc:
        return report_infra_fault(
            infra_fault(
                CHECKER, f"Failed to extract Gates B-F from {args.skill}: {exc}"
            )
        )

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(extracted, encoding="utf-8")
    else:
        sys.stdout.write(extracted)

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(guard(CHECKER, main))
