#!/usr/bin/env python3
"""Process the AI issue responder's output and determine ticket actions.

Used by .github/workflows/_ai-issue-response-core.yml. Parses the JSON output
from Antigravity CLI (agy), validates the selected core functional option
(clarify, simple solution, detailed solution, acknowledge and assign), resolves
developer assignment based on repository ownership paths when Option 4 is
selected, determines whether to close the issue when a complete solution is
provided, and prepares the response comment.

Routing rules for Option 4:
  Catch-all (docs, CI workflows, root configs, unmatched): @happyhuman
  /core/python/**      -> @eliasecchig
  /core/go/**          -> @ToniCorinne
  /core/java/**        -> @eliasecchig
  /core/typescript/**  -> @happyhuman
  /core/kotlin/**      -> @happyhuman
  /contrib/python/**   -> @happyhuman
  /contrib/go/**       -> @ToniCorinne
  /contrib/java/**     -> @happyhuman
  /contrib/typescript/** -> @happyhuman
  /contrib/kotlin/**   -> @happyhuman
  /skills/**           -> @happyhuman

Usage:
  python3 process_issue_response.py \\
    --result agy_result.json \\
    --issue-number 123 \\
    --comment-out comment.md \\
    --assignee-out assignee.txt \\
    [--close-out close.txt] \\
    [--github-output "$GITHUB_OUTPUT"]

Exit codes:
  0  success (comment and optional assignee/close flag written)
  2  CI fault (unreadable result or execution crash)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from enum import IntEnum
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ci_message import (
    EXIT_OK,
    guard,
    infra_fault,
    report_infra_fault,
)

CHECKER = "process_issue_response.py"


class Option(IntEnum):
    """The 4 core functional options for issue triage response."""

    CLARIFY = 1
    SIMPLE_SOLUTION = 2
    DETAILED_SOLUTION = 3
    ACKNOWLEDGE_AND_ASSIGN = 4


ROUTING_RULES: list[tuple[str, str]] = [
    # Core directory assignments
    ("core/python", "eliasecchig"),
    ("core/go", "ToniCorinne"),
    ("core/java", "eliasecchig"),
    ("core/typescript", "happyhuman"),
    ("core/kotlin", "happyhuman"),
    # Contrib directory assignments
    ("contrib/python", "happyhuman"),
    ("contrib/go", "ToniCorinne"),
    ("contrib/java", "happyhuman"),
    ("contrib/typescript", "happyhuman"),
    ("contrib/kotlin", "happyhuman"),
    # Skills directory assignments
    ("skills", "happyhuman"),
]

DEFAULT_ASSIGNEE = "happyhuman"

# Known developer usernames (without @ prefix)
VALID_ASSIGNEES = {"eliasecchig", "ToniCorinne", "happyhuman"}

FENCED_JSON = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE
)


def normalize_path(path: str) -> str:
    """Normalize a repository path for routing comparison."""
    cleaned = path.strip().replace("\\", "/")
    while cleaned.startswith("/"):
        cleaned = cleaned[1:]
    while cleaned.endswith("/"):
        cleaned = cleaned[:-1]
    return cleaned.lower()


def resolve_assignee_from_path(path: str | None) -> str:
    """Resolve the assigned developer username based on path ownership."""
    if not path:
        return DEFAULT_ASSIGNEE

    normalized = normalize_path(path)
    if not normalized:
        return DEFAULT_ASSIGNEE

    for prefix, assignee in ROUTING_RULES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return assignee

    return DEFAULT_ASSIGNEE


def parse_option(raw_option: Any) -> Option:
    """Parse raw option value into an Option enum member."""
    if isinstance(raw_option, int):
        try:
            return Option(raw_option)
        except ValueError:
            return Option.ACKNOWLEDGE_AND_ASSIGN

    if isinstance(raw_option, str):
        cleaned = raw_option.strip().lower()
        if cleaned in (
            "1",
            "option 1",
            "option_1",
            "option1",
            "clarify",
            "option_1_clarify",
        ):
            return Option.CLARIFY
        if cleaned in (
            "2",
            "option 2",
            "option_2",
            "option2",
            "simple",
            "quick",
            "simple_solution",
            "quick_solution",
            "option_2_quick_solution",
        ):
            return Option.SIMPLE_SOLUTION
        if cleaned in (
            "3",
            "option 3",
            "option_3",
            "option3",
            "detailed",
            "detailed_solution",
            "step_by_step",
            "option_3_detailed_solution",
        ):
            return Option.DETAILED_SOLUTION
        if cleaned in (
            "4",
            "option 4",
            "option_4",
            "option4",
            "acknowledge",
            "assign",
            "acknowledge_and_assign",
            "option_4_acknowledge_and_assign",
        ):
            return Option.ACKNOWLEDGE_AND_ASSIGN

        for opt in Option:
            if str(opt.value) in cleaned:
                return opt

    return Option.ACKNOWLEDGE_AND_ASSIGN


def _strip_trailing_commas(text: str) -> str:
    """Remove trailing commas before '}' or ']' outside of JSON string literals."""
    result: list[str] = []
    in_string = False
    escape = False
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if escape:
            escape = False
            result.append(char)
            i += 1
            continue
        if char == "\\":
            if in_string:
                escape = True
            result.append(char)
            i += 1
            continue
        if char == '"':
            in_string = not in_string
            result.append(char)
            i += 1
            continue
        if not in_string and char == ",":
            j = i + 1
            while j < length and text[j] in " \t\r\n":
                j += 1
            if j < length and text[j] in ("}", "]"):
                i += 1
                continue
        result.append(char)
        i += 1
    return "".join(result)


def _parse_json_dict(text: str) -> dict[str, Any] | None:
    """Attempt to parse a candidate string into a dict, handling strictness and trailing commas."""
    text = text.strip()
    if not text:
        return None

    cleaned = _strip_trailing_commas(text)
    candidates = (text, cleaned) if cleaned != text else (text,)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate, strict=False)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    return None


def find_json_objects(text: str) -> list[dict[str, Any]]:
    """Find all top-level balanced JSON objects `{...}` within text."""
    results: list[dict[str, Any]] = []
    text_len = len(text)
    i = 0
    while i < text_len:
        if text[i] == "{":
            depth = 0
            in_string = False
            escape = False
            start_idx = i
            end_idx = -1
            for j in range(start_idx, text_len):
                char = text[j]
                if escape:
                    escape = False
                    continue
                if char == "\\":
                    if in_string:
                        escape = True
                    continue
                if char == '"':
                    in_string = not in_string
                    continue
                if not in_string:
                    if char == "{":
                        depth += 1
                    elif char == "}":
                        depth -= 1
                        if depth == 0:
                            end_idx = j
                            break
            if end_idx != -1:
                candidate = text[start_idx : end_idx + 1]
                parsed_obj = _parse_json_dict(candidate)
                if parsed_obj is not None:
                    results.append(parsed_obj)
                i = end_idx + 1
                continue
        i += 1
    return results


def _score_decision_candidate(d: dict[str, Any]) -> int:
    """Score candidate dicts to identify the actual decision payload."""
    score = 0
    if "option" in d:
        score += 10
    if "response" in d and isinstance(d["response"], str):
        score += 5
    if "close_issue" in d or "close" in d:
        score += 2
    if "path" in d or "assignee" in d:
        score += 1
    if "status" in d:
        # Penalize outer agy envelopes {"status": "SUCCESS", "response": ...}
        score -= 20
    return score


def extract_decision_json(raw_text: str) -> dict[str, Any]:
    """Extract and parse the JSON decision from agy output."""
    raw_text = raw_text.strip()
    if not raw_text:
        raise ValueError("Empty output from AI agent.")

    text_to_search = raw_text

    # Check if raw_text is the agy envelope JSON: {"status": ..., "response": ...}
    envelope = _parse_json_dict(raw_text)
    if envelope is not None and "status" in envelope:
        if "response" in envelope:
            if isinstance(envelope["response"], str):
                text_to_search = envelope["response"].strip()
            elif isinstance(envelope["response"], dict):
                return envelope["response"]

    # Try direct parse of text_to_search first
    parsed = _parse_json_dict(text_to_search)
    if (
        parsed is not None
        and ("option" in parsed or "response" in parsed)
        and "status" not in parsed
    ):
        return parsed

    # Search for markdown code fences
    fenced_matches = FENCED_JSON.findall(text_to_search)
    fenced_candidates: list[dict[str, Any]] = []
    for match_str in fenced_matches:
        fenced_obj = _parse_json_dict(match_str)
        if fenced_obj is not None:
            fenced_candidates.append(fenced_obj)

    if fenced_candidates:
        fenced_candidates.sort(key=_score_decision_candidate, reverse=True)
        if _score_decision_candidate(fenced_candidates[0]) > 0:
            return fenced_candidates[0]

    # Search for balanced JSON objects in text
    candidates = find_json_objects(text_to_search)
    if candidates:
        candidates.sort(key=_score_decision_candidate, reverse=True)
        if _score_decision_candidate(candidates[0]) > 0:
            return candidates[0]

    # Fallback to search in raw_text if text_to_search was different
    if text_to_search != raw_text:
        raw_candidates = find_json_objects(raw_text)
        if raw_candidates:
            raw_candidates.sort(key=_score_decision_candidate, reverse=True)
            if _score_decision_candidate(raw_candidates[0]) > 0:
                return raw_candidates[0]

    raise ValueError(
        f"No valid JSON decision object found in AI output: {text_to_search[:200]}"
    )


def parse_close_issue(decision: dict[str, Any], option: Option) -> bool:
    """Determine whether to close the issue as completed.

    Only Options 2 and 3 can close an issue when the response provides a complete,
    definitive solution. Option 1 (clarify) and Option 4 (assign) must never close.
    """
    if option in (Option.CLARIFY, Option.ACKNOWLEDGE_AND_ASSIGN):
        return False

    raw = decision.get("close_issue")
    if raw is None:
        raw = decision.get("close")

    if isinstance(raw, bool):
        return raw

    if isinstance(raw, (int, float)):
        return bool(raw)

    if isinstance(raw, str):
        cleaned = raw.strip().lower()
        if cleaned in ("true", "1", "yes", "close", "closed"):
            return True
        if cleaned in ("false", "0", "no", "open", "keep_open"):
            return False

    return False


def process_response(
    decision: dict[str, Any],
) -> tuple[Option, str, str | None, bool]:
    """Process decision dict and return (option, response_body, assignee, close_issue)."""
    raw_opt = decision.get("option")
    option = parse_option(raw_opt)

    response_body = str(decision.get("response") or "").strip()
    if not response_body:
        response_body = (
            "Thank you for opening this issue! We have received your report "
            "and are looking into it."
        )

    path = decision.get("path")
    if path is not None:
        path = str(path).strip()

    raw_assignee = decision.get("assignee")
    if raw_assignee is not None:
        raw_assignee = str(raw_assignee).strip().lstrip("@")

    assignees_ci = {k.lower(): k for k in VALID_ASSIGNEES}

    if option == Option.ACKNOWLEDGE_AND_ASSIGN:
        # Determine assignee based on path routing
        if path:
            assignee = resolve_assignee_from_path(path)
        elif raw_assignee and raw_assignee.lower() in assignees_ci:
            assignee = assignees_ci[raw_assignee.lower()]
        else:
            assignee = DEFAULT_ASSIGNEE
    else:
        assignee = None

    close_issue = parse_close_issue(decision, option)

    return option, response_body, assignee, close_issue


def _safe_write_text(path: Path, content: str) -> None:
    """Write text to a destination file, wrapping OSError with filename."""
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise OSError(f"cannot write {path}: {exc}") from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description="Process AI issue responder decision and routing."
    )
    parser.add_argument(
        "--result",
        required=True,
        type=Path,
        help="Path to agy_result.json output file",
    )
    parser.add_argument(
        "--issue-number",
        required=True,
        type=int,
        help="The GitHub issue number",
    )
    parser.add_argument(
        "--comment-out",
        required=True,
        type=Path,
        help="Destination path for markdown comment file",
    )
    parser.add_argument(
        "--assignee-out",
        required=True,
        type=Path,
        help="Destination path for assignee text file",
    )
    parser.add_argument(
        "--close-out",
        type=Path,
        default=None,
        help="Destination path for close issue boolean text file ('true' or 'false')",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        default=None,
        help="Path to $GITHUB_OUTPUT file",
    )
    return parser


def main() -> int:
    """CLI entrypoint."""
    args = build_parser().parse_args()

    try:
        raw_content = args.result.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return report_infra_fault(
            infra_fault(CHECKER, f"cannot read {args.result}: {exc}")
        )

    try:
        decision = extract_decision_json(raw_content)
    except Exception as exc:
        return report_infra_fault(
            infra_fault(CHECKER, f"failed to parse JSON from agy output: {exc}")
        )

    option, response_body, assignee, close_issue = process_response(decision)

    try:
        _safe_write_text(args.comment_out, response_body + "\n")
        _safe_write_text(args.assignee_out, (assignee or "") + "\n")
        if args.close_out:
            _safe_write_text(
                args.close_out, ("true" if close_issue else "false") + "\n"
            )
    except OSError as exc:
        return report_infra_fault(infra_fault(CHECKER, str(exc)))

    print(
        f"Issue #{args.issue_number}: selected Option {option.value} ({option.name})"
    )
    if assignee:
        print(f"Assigned to developer: @{assignee}")
    else:
        print("No assignee specified (Options 1-3).")
    print(
        f"Close issue: {'yes (completed)' if close_issue else 'no (keep open)'}"
    )

    if args.github_output:
        outputs = [
            f"option={option.value}",
            f"option_name={option.name}",
            f"assignee={assignee or ''}",
            f"has_assignee={'true' if assignee else 'false'}",
            f"close_issue={'true' if close_issue else 'false'}",
        ]
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(outputs) + "\n")

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(guard(CHECKER, main))
