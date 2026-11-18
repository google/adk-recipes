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
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ci_message import (
    EXIT_OK,
    guard,
)

CHECKER = "check_recipe_proposal.py"

PROPOSAL_MARKER = "[RECIPE PROPOSAL]"
MIN_TEMPLATE_SIGNALS = 3
TEMPLATE_URL = (
    "https://github.com/google/adk-recipes/blob/main/"
    ".github/ISSUE_TEMPLATE/propose-a-new-recipe.md"
)

GATE_A_REQUIRED_QUESTIONS: list[tuple[str, re.Pattern[str]]] = [
    (
        "1. Intent / purpose",
        re.compile(
            r"what\s+is\s+the\s+intent/purpose\s+of\s+your\s+new\s+recipe\?"
            r"(?:\s*in\s+what\s+way\s+will\s+it\s+help\s+the\s+adk\s+community\?)?",
            re.IGNORECASE,
        ),
    ),
    (
        "2. Language",
        re.compile(
            r"what\s+language\s+is\s+your\s+recipe(?:\s+is)?\s+implemented\s+in"
            r"(?:\s*\([^)]*\))?\??",
            re.IGNORECASE,
        ),
    ),
    (
        "3. Deployability / Dockerfile",
        re.compile(
            r"will\s+this\s+be\s+a\s+deployable\s+recipe[^\n?]*\?"
            r"(?:\s*note:\s*every\s+`?contrib/`?\s+recipe\s+requires\s+a\s+root\s+"
            r"`?dockerfile`?\s+and\s+`?deployable:\s*true`?\s+in\s+`?manifest\.yaml`?\.?)?",
            re.IGNORECASE,
        ),
    ),
    (
        "4. Required GCP services or third-party APIs",
        re.compile(
            r"what\s+gcp\s+services\s+or\s+third-party\s+apis"
            r"(?:\s*\([^)]*\))?\s*(?:are\s+required)?\??",
            re.IGNORECASE,
        ),
    ),
    (
        "5. Owner / maintainer",
        re.compile(
            r"who\s*(?:\([^)]*\)\s*)?will\s+own\s+and\s+maintain\s+this\??",
            re.IGNORECASE,
        ),
    ),
]

_GATE_A_OPTIONAL_Q6_RE = re.compile(
    r"please\s+include\s+any\s+additional\s+information[^\n.]*\.?",
    re.IGNORECASE,
)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

TEMPLATE_SIGNALS = [
    "What is the intent/purpose of your new recipe?",
    "What language is your recipe implemented in",
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
    re.compile(
        r"\b(add|create|propose|submit)\s+(?:a\s+)?(?:new\s+)?(?:[\w-]+\s+){0,2}(?:recipe|plugin)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bnew\s+(?:[\w-]+\s+){0,2}(?:recipe|plugin)\b", re.IGNORECASE),
    re.compile(
        r"\b(recipe|plugin)\b.*\bfor\s+(core|contrib|plugins)\b", re.IGNORECASE
    ),
    re.compile(
        r"\b(add|new|propose)\s+(?:a\s+)?(?:recipe|plugin)\s+(?:in|to|under)\s+(core|contrib|plugins)/",
        re.IGNORECASE,
    ),
]

TITLE_NEGATIVE_PATTERNS = re.compile(
    r"\b(bug|error|fail(?:ed|s|ing|ure)?|cannot|can't|unable\s+to|broken|crash(?:es|ed)?|missing\s+\.env)\b",
    re.IGNORECASE,
)

NEGATIVE_LABELS = {
    "bug",
    "type: bug",
    "question",
    "type: question",
    "documentation",
}

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

    # Explicit negative check for bug reports / questions before heuristics
    for prefix in BUG_OR_QUESTION_PREFIXES:
        if upper_title.startswith(prefix):
            return False, f"negative_prefix:{prefix}"

    if any(lbl in NEGATIVE_LABELS for lbl in clean_labels):
        return False, "negative_label"

    # Template body signals (ignore generic 'Scope Notice' unless paired)
    for signal in TEMPLATE_SIGNALS:
        if signal == "Scope Notice":
            continue
        if signal.lower() in clean_body.lower():
            return True, f"template_signal:{signal[:20]}"

    # Label 'contribute'
    if "contribute" in clean_labels:
        return True, "label_contribute"

    if TITLE_NEGATIVE_PATTERNS.search(clean_title):
        return False, "negative_title_keyword"

    # Title pattern checks
    for pat in TITLE_PROPOSAL_PATTERNS:
        if pat.search(clean_title):
            return True, f"title_pattern:{pat.pattern}"

    # Body pattern checks
    for pat in BODY_PROPOSAL_PATTERNS:
        if pat.search(clean_body):
            return True, f"body_pattern:{pat.pattern}"

    return False, "no_proposal_signals"


def is_template_proposal(
    title: str,
    body: str = "",
) -> tuple[bool, str]:
    """Check if an issue was filed specifically using propose-a-new-recipe.md format.

    Requires the [RECIPE PROPOSAL] title marker (case-insensitive) or a high
    concentration of template questions if the title was modified.
    """
    clean_title = (title or "").strip()
    upper_title = clean_title.upper()
    clean_body = (body or "").strip()
    lower_body = clean_body.lower()

    # Negative check for bug reports / questions / chores
    for prefix in BUG_OR_QUESTION_PREFIXES:
        if upper_title.startswith(prefix):
            return False, f"negative_prefix:{prefix}"

    has_marker = PROPOSAL_MARKER in upper_title

    matched_signals = [s for s in TEMPLATE_SIGNALS if s.lower() in lower_body]
    if (
        "what language is your recipe is implemented in" in lower_body
        and "What language is your recipe implemented in" not in matched_signals
    ):
        matched_signals.append("What language is your recipe is implemented in")

    # If title has [RECIPE PROPOSAL], always route to proposal triage so
    # Gate A can verify template consistency and alert the contributor when
    # the body is empty or does not match the template.
    if has_marker:
        return (
            True,
            f"template_format:marker_and_{len(matched_signals)}_signals",
        )

    # Or if body contains at least MIN_TEMPLATE_SIGNALS distinct template questions/sections even if user altered title:
    if len(matched_signals) >= MIN_TEMPLATE_SIGNALS:
        return True, f"template_format:{len(matched_signals)}_signals"

    return False, "not_template_format"


def _line_start(text: str, pos: int) -> int:
    """Return the index of the start of the line containing `pos`."""
    idx = text.rfind("\n", 0, pos)
    return 0 if idx == -1 else idx + 1


def _strip_answer_noise(raw_slice: str) -> str:
    """Remove HTML comments and pure formatting punctuation from an answer."""
    without_comments = _HTML_COMMENT_RE.sub("", raw_slice)
    cleaned = re.sub(r"[\s*>`_~-]+", " ", without_comments).strip()
    return cleaned


def check_gate_a(title: str, body: str = "") -> dict[str, Any]:
    """Run deterministic Gate A check: verify `[RECIPE PROPOSAL]` body matches template.

    Returns a dictionary with:
      - passed (bool): True when all 5 required template questions are present and answered
      - has_marker (bool): Whether `[RECIPE PROPOSAL]` is in the title
      - missing_sections (list[str]): Required template questions not found in the body
      - unanswered_sections (list[str]): Found questions with empty answers
      - comment (str): Neutral first-pass bot alert when `passed` is False, else ""
    """
    clean_title = (title or "").strip()
    has_marker = PROPOSAL_MARKER in clean_title.upper()
    clean_body = (body or "").replace("\r\n", "\n").strip()

    if not clean_body:
        empty_lead = (
            "This `[RECIPE PROPOSAL]` issue has an empty body and does not yet include the "
            if has_marker
            else "This recipe proposal issue has an empty body and does not yet include the "
        )
        comment = (
            "**Automated first-pass triage (Gate A — Template check):** "
            f"{empty_lead}"
            f"[Propose a New Recipe template]({TEMPLATE_URL}).\n\n"
            "Please update the issue description with answers to questions 1-5 "
            "(intent/purpose, language, deployability/Dockerfile plan, required GCP "
            "services/APIs, and owner/maintainer) so automated first-pass checks "
            "(Gates B-F) and maintainer review can proceed.\n\n"
            "Leaving this issue open for @pierpaolo28 and @kgu-here — final review "
            "and closing are handled by the maintainers."
        )
        return {
            "passed": False,
            "has_marker": has_marker,
            "missing_sections": [
                label for label, _ in GATE_A_REQUIRED_QUESTIONS
            ],
            "unanswered_sections": [],
            "comment": comment,
        }

    found: list[tuple[int, int, str | None]] = []
    missing_sections: list[str] = []

    for label, pattern in GATE_A_REQUIRED_QUESTIONS:
        match = pattern.search(clean_body)
        if match is None:
            missing_sections.append(label)
        else:
            found.append((match.start(), match.end(), label))

    q6_match = _GATE_A_OPTIONAL_Q6_RE.search(clean_body)
    if q6_match is not None:
        found.append((q6_match.start(), q6_match.end(), None))

    found.sort(key=lambda item: item[0])

    unanswered_sections: list[str] = []
    for idx, (_start, end, label) in enumerate(found):
        if label is None:
            continue
        if idx + 1 < len(found):
            candidate_start = _line_start(clean_body, found[idx + 1][0])
            next_start = (
                candidate_start if candidate_start >= end else found[idx + 1][0]
            )
        else:
            next_start = len(clean_body)
        answer_text = _strip_answer_noise(clean_body[end:next_start])
        if not answer_text:
            unanswered_sections.append(label)

    passed = not missing_sections and not unanswered_sections
    if passed:
        return {
            "passed": True,
            "has_marker": has_marker,
            "missing_sections": [],
            "unanswered_sections": [],
            "comment": "",
        }

    details: list[str] = []
    if missing_sections:
        details.append(
            "- **Missing template questions:** " + ", ".join(missing_sections)
        )
    if unanswered_sections:
        details.append(
            "- **Unanswered template questions:** "
            + ", ".join(unanswered_sections)
        )
    details_block = "\n".join(details)

    lead = (
        "This issue is tagged with `[RECIPE PROPOSAL]`, but the body is not yet "
        if has_marker
        else "This issue looks like a recipe proposal, but the body is not yet "
    )
    comment = (
        "**Automated first-pass triage (Gate A — Template check):** "
        f"{lead}"
        f"consistent with the [Propose a New Recipe template]({TEMPLATE_URL}):\n\n"
        f"{details_block}\n\n"
        "Please update the issue description to fill in questions 1-5 so "
        "automated first-pass checks (Gates B-F) and maintainer review can proceed.\n\n"
        "Leaving this issue open for @pierpaolo28 and @kgu-here — final review "
        "and closing are handled by the maintainers."
    )
    return {
        "passed": False,
        "has_marker": has_marker,
        "missing_sections": missing_sections,
        "unanswered_sections": unanswered_sections,
        "comment": comment,
    }


def _parse_labels(raw_labels: str | list[Any]) -> list[str]:
    """Parse labels from a list, JSON list string, or comma-separated string."""
    if isinstance(raw_labels, list):
        return [
            str(x.get("name") if isinstance(x, dict) else x)
            for x in raw_labels
            if x
        ]
    stripped = (raw_labels or "").strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return [
                    str(x.get("name") if isinstance(x, dict) else x)
                    for x in parsed
                    if x
                ]
        except json.JSONDecodeError:
            pass
    return [x.strip() for x in stripped.split(",") if x.strip()]


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
        "--stdin",
        action="store_true",
        help="Read issue JSON ({title, body, labels}) from stdin",
    )
    parser.add_argument(
        "--github-output",
        default="",
        help="Path to GITHUB_OUTPUT file",
    )
    parser.add_argument(
        "--template-only",
        action="store_true",
        help="Only match issues that use the [RECIPE PROPOSAL] marker or template format",
    )
    parser.add_argument(
        "--check-gate-a",
        action="store_true",
        help="Run deterministic Gate A template consistency check and output result",
    )
    parser.add_argument(
        "--comment-out",
        default="",
        help="Optional path to write the Gate A alert comment when --check-gate-a fails",
    )
    args = parser.parse_args()

    title = args.title
    body = args.body
    labels = _parse_labels(args.labels)

    if args.stdin:
        raw_stdin = sys.stdin.read().strip()
        if raw_stdin:
            data = json.loads(raw_stdin)
            if isinstance(data, dict):
                title = str(data.get("title") or "")
                body = str(data.get("body") or "")
                if "labels" in data:
                    labels = _parse_labels(data["labels"])

    if args.check_gate_a:
        gate_a = check_gate_a(title, body)
        if args.comment_out and not gate_a["passed"]:
            out_comment = Path(args.comment_out)
            out_comment.parent.mkdir(parents=True, exist_ok=True)
            out_comment.write_text(gate_a["comment"] + "\n", encoding="utf-8")
        if args.github_output:
            out_path = Path(args.github_output)
            with out_path.open("a", encoding="utf-8") as f:
                f.write(
                    f"gate_a_passed={'true' if gate_a['passed'] else 'false'}\n"
                )
                if not gate_a["passed"]:
                    f.write("decision=DECLINE\n")
                    f.write("gate=A\n")
                    f.write("close_issue=false\n")
        print(json.dumps(gate_a))
        return EXIT_OK

    if args.template_only:
        is_prop, reason = is_template_proposal(title, body)
    else:
        is_prop, reason = is_recipe_proposal(title, body, labels)

    if args.github_output:
        out_path = Path(args.github_output)
        with out_path.open("a", encoding="utf-8") as f:
            f.write(f"is_proposal={'true' if is_prop else 'false'}\n")
            f.write(f"reason={reason}\n")

    print(json.dumps({"is_proposal": is_prop, "reason": reason}))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(guard(CHECKER, main))
