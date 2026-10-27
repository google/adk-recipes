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

FENCED_JSON = re.compile(
    r"```(?:json)?\s*\n?(.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)


def extract_json(raw: str) -> dict[str, Any]:
    """Extract a JSON object from raw model output."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Model output is empty.")

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    match = FENCED_JSON.search(raw)
    if match:
        snippet = match.group(1).strip()
        try:
            data = json.loads(snippet)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        snippet = raw[first_brace : last_brace + 1].strip()
        try:
            data = json.loads(snippet)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    raise ValueError(
        f"Could not parse valid JSON object from model output:\n{raw[:300]}"
    )


def process_proposal_response(
    decision_dict: dict[str, Any],
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
        if clean_gate in VALID_GATES:
            gate = clean_gate

    response_body = str(decision_dict.get("response") or "").strip()
    if not response_body:
        raise ValueError("Decision contains an empty response comment.")

    # Determine close_issue
    raw_close = decision_dict.get("close_issue")
    if isinstance(raw_close, bool):
        close_issue = raw_close
    else:
        # Default behavior: DECLINE and REDIRECT close as not planned
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
        "--github-output",
        required=False,
        type=Path,
        help="Path to $GITHUB_OUTPUT",
    )
    args = parser.parse_args()

    if not args.result.exists():
        print(f"Error: Result file not found: {args.result}", file=sys.stderr)
        return 2

    try:
        raw_text = args.result.read_text(encoding="utf-8")
        raw_envelope = json.loads(raw_text)
    except Exception as e:
        print(f"Error reading result JSON: {e}", file=sys.stderr)
        return 2

    # agy envelope output
    model_response = raw_envelope.get("response", raw_text)
    try:
        decision_dict = extract_json(model_response)
        decision, gate, response_body, close_issue = process_proposal_response(
            decision_dict
        )
    except Exception as e:
        print(f"Error processing proposal response: {e}", file=sys.stderr)
        return 2

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
        print(f"Error writing output files: {e}", file=sys.stderr)
        return 2

    print(
        f"Issue #{args.issue_number}: Decision {decision} (Gate: {gate}, Close: {close_issue})"
    )
    print(f"Comment generated ({len(response_body)} chars).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
