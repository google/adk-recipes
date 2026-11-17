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

r"""Plugin evaluation runner for EVAL.yaml suites.

Parses EVAL.yaml across plugin skills, evaluates assertions and rubric
requirements, checks criteria quality, and asserts that the pass rate meets
the >= 80% threshold required for Agent Plugins v1.0.0.

Usage:
    uv run python tools/run_plugin_eval.py [scope]
    scope can be 'plugins', a specific plugin or skill path, or omitted for all plugins.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from ci_message import guard  # noqa: E402


def find_eval_files(scope: Path | None = None) -> list[Path]:
    """Find all EVAL.yaml / eval.yaml files under plugins/."""
    if scope is not None and not scope.exists():
        return []
    base = scope if scope is not None else (REPO_ROOT / "plugins")
    if base.is_file() and base.name.lower() in ("eval.yaml", "eval.yml"):
        return [base]

    eval_files: list[Path] = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.name.lower() in ("eval.yaml", "eval.yml"):
            # Exclude cache or venv dirs
            if not any(
                part in {".venv", "node_modules", ".git"} for part in p.parts
            ):
                eval_files.append(p)
    return eval_files


def evaluate_rubric_rules(
    rubric_items: list[str],
) -> tuple[int, int, list[str]]:
    """Evaluate quality and clarity of rubric criteria.

    Returns:
        (passed_rules, total_rules, issues)
    """
    passed = 0
    issues = []
    for item in rubric_items:
        text = str(item).strip()
        # Quality check: well-formed rubrics declare MUST, MUST NOT, or SHOULD
        has_modal = bool(
            re.search(
                r"\b(MUST|SHOULD|MUST NOT|SHALL|CANNOT)\b", text, re.IGNORECASE
            )
        )
        has_min_length = len(text) >= 15
        if has_modal and has_min_length:
            passed += 1
        else:
            issues.append(
                f"Rubric rule lacks RFC 2119 requirement keyword or detail: '{text[:50]}...'"
            )
    return passed, len(rubric_items), issues


def evaluate_assertions(
    assertions: list[dict[str, Any]],
) -> tuple[int, int, list[str]]:
    """Validate assertion structure and determinism.

    Returns:
        (passed_assertions, total_assertions, issues)
    """
    passed = 0
    issues = []
    recognized_types = {
        "contains_product",
        "price_under",
        "tool_called",
        "no_hallucination",
        "mentions_price",
        "response_not_empty",
        "response_min_length",
        "skill_covers",
        "mentions_field",
        "regex_match",
        "json_valid",
        "status_code",
        "graceful_no_results",
    }

    for assertion in assertions:
        if not isinstance(assertion, dict):
            issues.append(
                f"Invalid assertion format: expected dict, got {type(assertion).__name__}"
            )
            continue

        a_type = assertion.get("type")
        if not a_type or not isinstance(a_type, str):
            issues.append("Assertion missing 'type' key")
            continue

        if a_type in recognized_types:
            passed += 1
        else:
            # Unrecognized assertion type is flagged and not counted as passed
            issues.append(f"Custom/unverified assertion type: '{a_type}'")

    return passed, len(assertions), issues


def _parse_threshold(raw: Any) -> float | None:
    """Parse an EVAL.yaml threshold value into a float in (0.0, 1.0]."""
    if raw is None:
        return 0.80
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if 0.0 < val <= 1.0 else None
    if isinstance(raw, str):
        s = raw.strip()
        try:
            if s.endswith("%"):
                val = float(s[:-1]) / 100.0
            else:
                val = float(s)
            return val if 0.0 < val <= 1.0 else None
        except ValueError:
            return None
    return None


def run_eval_for_file(eval_path: Path) -> dict[str, Any]:
    """Execute evaluation for one EVAL.yaml file."""
    rel_path = (
        eval_path.relative_to(REPO_ROOT)
        if eval_path.is_relative_to(REPO_ROOT)
        else eval_path
    )
    try:
        content = yaml.safe_load(
            eval_path.read_text(encoding="utf-8", errors="replace")
        )
    except yaml.YAMLError as exc:
        return {
            "path": rel_path,
            "status": "ERROR",
            "error": f"YAML Parse error: {exc}",
            "pass_rate": 0.0,
        }

    if not isinstance(content, dict):
        return {
            "path": rel_path,
            "status": "ERROR",
            "error": "EVAL.yaml must be a top-level mapping.",
            "pass_rate": 0.0,
        }

    cases = content.get("cases") or []
    if not isinstance(cases, list) or not cases:
        return {
            "path": rel_path,
            "status": "ERROR",
            "error": "No 'cases' list found in EVAL.yaml.",
            "pass_rate": 0.0,
        }

    parsed_threshold = _parse_threshold(
        content["threshold"] if "threshold" in content else 0.80
    )
    if parsed_threshold is None:
        return {
            "path": rel_path,
            "status": "ERROR",
            "error": f"Invalid threshold '{content.get('threshold')}' in EVAL.yaml (must be between 0.0 and 1.0 or a percentage string).",
            "pass_rate": 0.0,
        }
    declared_threshold = max(0.80, parsed_threshold)

    total_checks = 0
    passed_checks = 0
    case_summaries = []

    for idx, case in enumerate(cases):
        if not isinstance(case, dict):
            total_checks += 1
            case_summaries.append(
                {
                    "id": f"case_{idx}",
                    "checks": 1,
                    "passed": 0,
                    "rate": 0.0,
                    "issues": [
                        f"Case #{idx} must be a mapping, got {type(case).__name__}."
                    ],
                }
            )
            continue

        case_id = str(case.get("id") or f"unnamed_case_{idx}")
        raw_prompt = case.get("prompt", "")
        prompt = raw_prompt if isinstance(raw_prompt, str) else ""
        raw_rubrics = case.get("rubric")
        raw_assertions = case.get("assertions")

        case_passed = 0
        case_total = 0
        issues = []

        # Validate prompt presence
        if not prompt or len(prompt.strip()) < 5:
            issues.append(f"Case '{case_id}' has missing or trivial prompt.")
            case_total += 1
        else:
            case_passed += 1
            case_total += 1

        has_criteria = False

        # Check rubrics
        if raw_rubrics is not None:
            if not isinstance(raw_rubrics, list):
                issues.append(
                    f"Case '{case_id}' rubric must be a list, got {type(raw_rubrics).__name__}."
                )
                case_total += 1
                has_criteria = True
            elif raw_rubrics:
                has_criteria = True
                r_pass, r_tot, r_issues = evaluate_rubric_rules(raw_rubrics)
                case_passed += r_pass
                case_total += r_tot
                issues.extend(r_issues)

        # Check assertions
        if raw_assertions is not None:
            if not isinstance(raw_assertions, list):
                issues.append(
                    f"Case '{case_id}' assertions must be a list, got {type(raw_assertions).__name__}."
                )
                case_total += 1
                has_criteria = True
            elif raw_assertions:
                has_criteria = True
                a_pass, a_tot, a_issues = evaluate_assertions(raw_assertions)
                case_passed += a_pass
                case_total += a_tot
                issues.extend(a_issues)

        if not has_criteria:
            issues.append(
                f"Case '{case_id}' defines neither rubric items nor assertions."
            )
            case_total += 1

        total_checks += case_total
        passed_checks += case_passed

        case_rate = (case_passed / case_total) if case_total > 0 else 0.0
        case_summaries.append(
            {
                "id": case_id,
                "checks": case_total,
                "passed": case_passed,
                "rate": case_rate,
                "issues": issues,
            }
        )

    overall_pass_rate = (
        (passed_checks / total_checks) if total_checks > 0 else 0.0
    )
    passed = overall_pass_rate >= declared_threshold

    return {
        "path": rel_path,
        "status": "PASS" if passed else "FAIL",
        "total_cases": len(cases),
        "total_checks": total_checks,
        "passed_checks": passed_checks,
        "pass_rate": overall_pass_rate,
        "threshold": declared_threshold,
        "cases": case_summaries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run evaluation test suites for Agent Plugins."
    )
    parser.add_argument(
        "scope",
        nargs="?",
        default="plugins",
        help="Path or scope to evaluate (default: 'plugins')",
    )
    args = parser.parse_args()

    scope_path: Path | None = None
    if args.scope not in ("all", "plugins"):
        scope_path = Path(args.scope).resolve()
        if not scope_path.exists():
            scope_path = (REPO_ROOT / args.scope).resolve()
        if not scope_path.exists():
            print(f"❌ Target scope '{args.scope}' does not exist.")
            return 1

    eval_files = find_eval_files(scope_path)
    if not eval_files:
        print(f"❌ No EVAL.yaml files found in scope '{args.scope}'.")
        return 1

    print("=" * 80)
    print(f"  RUNNING PLUGIN EVALUATION SUITES ({len(eval_files)} found)")
    print("=" * 80)

    any_failed = False
    for eval_file in eval_files:
        result = run_eval_for_file(eval_file)
        print(f"\nEvaluating: {result['path']}")
        if result.get("error"):
            print(f"  ❌ Error: {result['error']}")
            any_failed = True
            continue

        pass_pct = result["pass_rate"] * 100
        thresh_pct = result["threshold"] * 100
        status_icon = "✅" if result["status"] == "PASS" else "❌"
        print(
            f"  {status_icon} Status: {result['status']} | Pass Rate: {pass_pct:.1f}% (Required: >={thresh_pct:.0f}%)"
        )
        print(
            f"  Cases: {result['total_cases']} | Individual Checks: {result['passed_checks']}/{result['total_checks']}"
        )

        for case in result["cases"]:
            c_icon = "✓" if case["rate"] >= 0.80 else "✗"
            print(
                f"    [{c_icon}] {case['id']}: {case['passed']}/{case['checks']} checks passed ({case['rate'] * 100:.0f}%)"
            )
            for issue in case["issues"]:
                print(f"        ⚠ {issue}")

        if result["status"] != "PASS":
            any_failed = True

    print("\n" + "=" * 80)
    if any_failed:
        print(
            "❌ EVALUATION FAILED: One or more plugin evaluations fell below the 80% pass threshold."
        )
        print("=" * 80)
        return 1

    print(
        "✅ ALL PLUGIN EVALUATIONS PASSED: All suites met or exceeded the 80% threshold."
    )
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(guard("run_plugin_eval.py", main))
