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
"""Detect if an issue is requesting to add a recipe to core/, contrib/, or plugins/.

Used by .github/workflows/recipe-proposal-intake.yml to route recipe proposals
to repository owners (@pierpaolo28, @kgu-here) and trigger the first-pass
triage response.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROPOSAL_MARKER = "[RECIPE PROPOSAL]"

TEMPLATE_SIGNALS = [
    "What is the intent/purpose of your new recipe?",
    "What language is your recipe is implemented in",
    "Will this be a deployable recipe",
    "What GCP services or third-party APIs",
    "Who (individual/team) will own and maintain this?",
    "Scope Notice",
    "For `contrib/` recipes only",
]

TITLE_PROPOSAL_PATTERNS = [
    re.compile(r"\[recipe\s+proposal\]", re.IGNORECASE),
    re.compile(r"\bpropos(e|ing|al)\b.*\brecipe\b", re.IGNORECASE),
    re.compile(r"\brecipe\b.*\bpropos(e|ing|al)\b", re.IGNORECASE),
    re.compile(r"\b(add|create|new)\b.*\brecipe\b", re.IGNORECASE),
    re.compile(r"\b(add|create|new)\b.*\bplugin\b", re.IGNORECASE),
    re.compile(
        r"\b(recipe|plugin)\b.*\bfor\s+(core|contrib|plugins)\b", re.IGNORECASE
    ),
    re.compile(
        r"\b(add|new|propose)\b.*\b(core|contrib|plugins)/", re.IGNORECASE
    ),
]

BODY_PROPOSAL_PATTERNS = [
    re.compile(
        r"\b(propose|proposing|propose\s+to\s+add)\b.*\brecipe\b", re.IGNORECASE
    ),
    re.compile(
        r"\b(submit|submitting|add)\s+a\s+new\s+recipe\b", re.IGNORECASE
    ),
    re.compile(r"\bnew\s+recipe\s+proposal\b", re.IGNORECASE),
    re.compile(
        r"\badd\s+(a\s+)?(new\s+)?recipe\s+to\s+(core|contrib|plugins)\b",
        re.IGNORECASE,
    ),
]

BUG_OR_QUESTION_PREFIXES = [
    "[BUG]",
    "[BUG REPORT]",
    "[QUESTION]",
    "[DOCS]",
    "[CI]",
    "[CHORE]",
]


def is_recipe_proposal(
    title: str,
    body: str = "",
    labels: list[str] | None = None,
) -> tuple[bool, str]:
    """Check if an issue is requesting to add a recipe to core/, contrib/, or plugins/.

    Returns (is_proposal, reason).
    """
    clean_title = (title or "").strip()
    upper_title = clean_title.upper()
    clean_body = (body or "").strip()
    clean_labels = [lbl.strip().lower() for lbl in (labels or [])]

    # Explicit marker takes highest precedence
    if PROPOSAL_MARKER in upper_title:
        return True, "title_marker"

    # Template body signals
    for signal in TEMPLATE_SIGNALS:
        if signal.lower() in clean_body.lower():
            return True, f"template_signal:{signal[:20]}"

    # Label 'contribute'
    if "contribute" in clean_labels:
        return True, "label_contribute"

    # Explicit negative check for bug reports / questions
    for prefix in BUG_OR_QUESTION_PREFIXES:
        if upper_title.startswith(prefix):
            return False, f"negative_prefix:{prefix}"

    # Title pattern checks
    for pat in TITLE_PROPOSAL_PATTERNS:
        if pat.search(clean_title):
            return True, f"title_pattern:{pat.pattern}"

    # Body pattern checks
    for pat in BODY_PROPOSAL_PATTERNS:
        if pat.search(clean_body):
            return True, f"body_pattern:{pat.pattern}"

    return False, "no_proposal_signals"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", default="", help="Issue title")
    parser.add_argument("--body", default="", help="Issue body")
    parser.add_argument(
        "--labels",
        default="",
        help="Comma-separated labels or JSON list of labels",
    )
    parser.add_argument(
        "--github-output",
        default="",
        help="Path to GITHUB_OUTPUT file",
    )
    args = parser.parse_args()

    labels: list[str] = []
    if args.labels:
        stripped = args.labels.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    labels = [str(x) for x in parsed]
            except json.JSONDecodeError:
                labels = [x.strip() for x in stripped.split(",") if x.strip()]
        else:
            labels = [x.strip() for x in stripped.split(",") if x.strip()]

    is_prop, reason = is_recipe_proposal(args.title, args.body, labels)

    if args.github_output:
        out_path = Path(args.github_output)
        with out_path.open("a", encoding="utf-8") as f:
            f.write(f"is_proposal={'true' if is_prop else 'false'}\n")
            f.write(f"reason={reason}\n")

    print(json.dumps({"is_proposal": is_prop, "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
