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
"""Process the AI recipe proposal triager's output and determine ticket actions.

Used by .github/workflows/_ai-recipe-proposal-triage-core.yml. Parses the JSON
output from Antigravity CLI (agy), validates the proposal decision
(ACCEPT, DECLINE, DEFER, REDIRECT, PARTIAL_ACCEPT), determines whether to close
the issue, and prepares the response comment.
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
    infra_fault,
    report_infra_fault,
)

CHECKER = "process_recipe_proposal_response.py"

VALID_DECISIONS = {
    "ACCEPT",
    "DECLINE",
    "DEFER",
    "REDIRECT",
    "PARTIAL_ACCEPT",
}

VALID_GATES = {
    "A",
    "B",
    "C",
    "D",
    "E",
    "F",
    "G",
}

ALLOWED_MENTIONS = {
    "pierpaolo28",
    "kgu-here",
    "happyhuman",
    "eliasecchig",
    "tommywagz",
    "tonicorinne",
}

FENCED_JSON = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)
TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)"
)


def _try_parse_dict(text: str) -> dict[str, Any] | None:
    """Attempt to parse a JSON object, tolerating trailing commas and raw newlines."""
    for candidate in (text, TRAILING_COMMA_RE.sub(r"\1", text)):
        try:
            data = json.loads(candidate, strict=False)
            if isinstance(data, dict) and "decision" in data:
                return data
        except json.JSONDecodeError:
            continue
    try:
        data = json.loads(text, strict=False)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return None


def _iter_balanced_objects(text: str) -> list[str]:
    """Extract balanced `{...}` substrings from `text`."""
    objs: list[str] = []
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        for j in range(i, n):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    objs.append(text[i : j + 1])
                    break
        i += 1
    return objs


def extract_json(raw: str | dict[str, Any]) -> dict[str, Any]:
    """Extract a JSON object from raw model output."""
    if isinstance(raw, dict):
        return raw

    raw_str = (raw or "").strip()
    if not raw_str:
        raise ValueError("Model output is empty.")

    if (data := _try_parse_dict(raw_str)) is not None:
        return data

    for match in FENCED_JSON.finditer(raw_str):
        snippet = match.group(1).strip()
        if (data := _try_parse_dict(snippet)) is not None:
            return data

    for candidate in reversed(_iter_balanced_objects(raw_str)):
        if (data := _try_parse_dict(candidate)) is not None:
            return data

    raise ValueError(
        f"Could not parse valid JSON object from model output:\n{raw_str[:300]}"
    )


def _sanitize_mentions(text: str, issue_author: str = "") -> str:
    """Neutralize @mentions that are not in the maintainer allowlist or the issue author."""
    allowed = set(ALLOWED_MENTIONS)
    if issue_author.strip():
        allowed.add(issue_author.strip().lstrip("@").lower())

    def _replace(match: re.Match[str]) -> str:
        handle = match.group(1)
        if handle.lower() in allowed:
            return f"@{handle}"
        return f"`@{handle}`"

    return MENTION_RE.sub(_replace, text)


def process_proposal_response(
    decision_dict: dict[str, Any],
    issue_author: str = "",
) -> tuple[str, str | None, str, bool]:
    """Validate decision dictionary and extract (decision, gate, response_body, close_issue)."""
    raw_decision = str(decision_dict.get("decision") or "").strip().upper()
    if raw_decision not in VALID_DECISIONS:
        raise ValueError(
            f"Invalid decision '{raw_decision}'. Must be one of {sorted(VALID_DECISIONS)}"
        )

    raw_gate = decision_dict.get("gate")
    gate: str | None = None
    if raw_gate is not None:
        clean_gate = str(raw_gate).strip().upper()
        if clean_gate.startswith("GATE "):
            clean_gate = clean_gate[5:].strip()
        if clean_gate in VALID_GATES:
            gate = clean_gate

    response_body = str(decision_dict.get("response") or "").strip()
    if not response_body:
        raise ValueError("Decision contains an empty response comment.")
    response_body = _sanitize_mentions(response_body, issue_author=issue_author)

    # Determine close_issue: ACCEPT, PARTIAL_ACCEPT, and DEFER must never close the issue.
    if raw_decision in {"ACCEPT", "PARTIAL_ACCEPT", "DEFER"}:
        close_issue = False
    else:
        raw_close = decision_dict.get("close_issue")
        if isinstance(raw_close, bool):
            close_issue = raw_close
        else:
            close_issue = raw_decision in {"DECLINE", "REDIRECT"}

    return raw_decision, gate, response_body, close_issue


def _safe_write_text(path: Path | None, content: str) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--result", required=True, type=Path, help="Path to agy_result.json"
    )
    parser.add_argument(
        "--issue-number", required=True, help="GitHub issue number"
    )
    parser.add_argument(
        "--comment-out",
        required=True,
        type=Path,
        help="Output comment markdown path",
    )
    parser.add_argument(
        "--close-out", required=False, type=Path, help="Output close flag path"
    )
    parser.add_argument(
        "--issue-author",
        required=False,
        default="",
        help="GitHub login of the issue author",
    )
    parser.add_argument(
        "--github-output",
        required=False,
        type=Path,
        help="Path to $GITHUB_OUTPUT",
    )
    args = parser.parse_args()

    if not args.result.exists():
        return report_infra_fault(
            infra_fault(CHECKER, f"Result file not found: {args.result}")
        )

    try:
        raw_text = args.result.read_text(encoding="utf-8", errors="replace")
        raw_envelope = json.loads(raw_text)
    except Exception as e:
        return report_infra_fault(
            infra_fault(CHECKER, f"Error reading result JSON: {e}")
        )

    # agy envelope output
    model_response = (
        raw_envelope.get("response", raw_text)
        if isinstance(raw_envelope, dict)
        else raw_text
    )
    try:
        decision_dict = extract_json(model_response)
        decision, gate, response_body, close_issue = process_proposal_response(
            decision_dict,
            issue_author=args.issue_author,
        )
    except Exception as e:
        return report_infra_fault(
            infra_fault(CHECKER, f"Error processing proposal response: {e}")
        )

    try:
        _safe_write_text(args.comment_out, response_body + "\n")
        if args.close_out:
            _safe_write_text(
                args.close_out, "true\n" if close_issue else "false\n"
            )

        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as f:
                f.write(f"decision={decision}\n")
                if gate:
                    f.write(f"gate={gate}\n")
                f.write(f"close_issue={'true' if close_issue else 'false'}\n")
    except Exception as e:
        return report_infra_fault(
            infra_fault(CHECKER, f"Error writing output files: {e}")
        )

    print(
        f"Issue #{args.issue_number}: Decision {decision} (Gate: {gate}, Close: {close_issue})"
    )
    print(f"Comment generated ({len(response_body)} chars).")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(guard(CHECKER, main))
