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

# ruff: noqa: S101
"""
Tokenomics SDK - Testing & CI/CD Regression Assertions
Enables teams to enforce financial token budgets and cache optimization gates in CI/CD.
"""

import glob
import json
import math
import os
from collections import Counter
from typing import Any

from adk_tokenomics_plugin.core.tracker import TokenTracker


class InvalidThresholdError(AssertionError, ValueError):
    """Raised when an assertion threshold or summary payload is NaN, infinite, negative, or missing required keys."""


def _require_finite_number(
    val: Any,
    name: str,
    min_val: float | None = 0.0,
    max_val: float | None = None,
) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise InvalidThresholdError(
            f"{name} must be a finite number, got {val!r}."
        )
    f = float(val)
    if not math.isfinite(f):
        raise InvalidThresholdError(
            f"{name} must be finite (not NaN/Inf), got {val!r}."
        )
    if min_val is not None and f < min_val:
        raise InvalidThresholdError(
            f"{name} must be >= {min_val}, got {val!r}."
        )
    if max_val is not None and f > max_val:
        raise InvalidThresholdError(
            f"{name} must be <= {max_val}, got {val!r}."
        )
    return f


def _resolve_summary(
    tracker_or_summary: TokenTracker | dict[str, Any], required_key: str
) -> dict[str, Any]:
    if isinstance(tracker_or_summary, TokenTracker):
        summary = tracker_or_summary.get_summary()
        if int(summary.get("total_turns", 0)) == 0:
            raise InvalidThresholdError(
                "No turns recorded in TokenTracker; cannot assert against an empty session."
            )
        return summary
    if isinstance(tracker_or_summary, dict):
        if not tracker_or_summary or required_key not in tracker_or_summary:
            raise InvalidThresholdError(
                f"Summary dictionary is empty or missing required key {required_key!r}."
            )
        if (
            "total_turns" in tracker_or_summary
            and int(tracker_or_summary["total_turns"]) == 0
        ):
            raise InvalidThresholdError(
                "Summary reports total_turns == 0; cannot assert against an empty session."
            )
        return tracker_or_summary
    raise TypeError(
        f"Expected TokenTracker or summary dict, got {type(tracker_or_summary).__name__}."
    )


def assert_cost_under(
    tracker_or_summary: TokenTracker | dict[str, Any],
    max_dollars: float,
    msg: str = "",
):
    """Asserts that total accumulated dollar cost does not exceed (<=) the specified budget threshold."""
    limit = _require_finite_number(max_dollars, "max_dollars", min_val=0.0)
    summary = _resolve_summary(tracker_or_summary, "total_cost")
    actual_cost = _require_finite_number(
        summary["total_cost"], "summary['total_cost']", min_val=0.0
    )
    err_msg = (
        msg
        or f"Cost budget exceeded! Expected cost <= ${limit:.5f}, but was ${actual_cost:.5f}"
    )
    assert actual_cost <= limit, err_msg


def assert_cache_ratio_above(
    tracker_or_summary: TokenTracker | dict[str, Any],
    min_ratio_pct: float,
    msg: str = "",
):
    """Asserts that context caching read ratio meets or exceeds (>=) the required efficiency percentage (0-100)."""
    limit = _require_finite_number(
        min_ratio_pct, "min_ratio_pct", min_val=0.0, max_val=100.0
    )
    summary = _resolve_summary(tracker_or_summary, "cache_read_ratio_pct")
    actual_ratio = _require_finite_number(
        summary["cache_read_ratio_pct"],
        "summary['cache_read_ratio_pct']",
        min_val=0.0,
    )
    err_msg = (
        msg
        or f"Context caching efficiency too low! Expected >= {limit:.1f}%, but was {actual_ratio:.1f}%"
    )
    assert actual_ratio >= limit, err_msg


def assert_token_budget(
    tracker_or_summary: TokenTracker | dict[str, Any],
    max_tokens: int,
    msg: str = "",
):
    """Asserts that total tokens consumed is within (<=) the allocated token budget."""
    limit = _require_finite_number(max_tokens, "max_tokens", min_val=0.0)
    summary = _resolve_summary(tracker_or_summary, "total_tokens")
    actual_tokens = _require_finite_number(
        summary["total_tokens"], "summary['total_tokens']", min_val=0.0
    )
    err_msg = (
        msg
        or f"Token budget exceeded! Expected total tokens <= {int(limit)}, but was {int(actual_tokens)}"
    )
    assert actual_tokens <= limit, err_msg


def assert_thinking_budget(
    tracker_or_summary: TokenTracker | dict[str, Any],
    min_tokens: int = 0,
    max_tokens: int = 8192,
    msg: str = "",
):
    """Asserts that reasoning/thinking tokens fall within expected [min_tokens, max_tokens] bounds."""
    low = _require_finite_number(min_tokens, "min_tokens", min_val=0.0)
    high = _require_finite_number(max_tokens, "max_tokens", min_val=0.0)
    if low > high:
        raise InvalidThresholdError(
            f"min_tokens ({low}) cannot exceed max_tokens ({high})."
        )
    summary = _resolve_summary(tracker_or_summary, "thinking_tokens")
    actual_thinking = _require_finite_number(
        summary["thinking_tokens"], "summary['thinking_tokens']", min_val=0.0
    )
    err_msg = msg or (
        f"Thinking tokens out of bounds! Expected between {int(low)} and {int(high)}, but was {int(actual_thinking)}"
    )
    assert low <= actual_thinking <= high, err_msg


class Assertions:
    """Class wrapper for Assertions API."""

    assert_cost_under = staticmethod(assert_cost_under)
    assert_cache_ratio_above = staticmethod(assert_cache_ratio_above)
    assert_token_budget = staticmethod(assert_token_budget)
    assert_thinking_budget = staticmethod(assert_thinking_budget)


def _extract_turn_latency_ms(raw_lat: Any) -> float:
    """
    Extracts non-negative latency in milliseconds from int, float, numeric string, or BigQuery-style
    {'total_ms': ...} dicts. Note: 0 (or None) is treated as 'not reported' to match TokenTracker.record_turn,
    which coerces missing latency_ms=None to 0.
    """
    if raw_lat is None:
        return 0.0
    if isinstance(raw_lat, dict):
        inner = raw_lat.get("total_ms")
        if inner is None:
            inner = raw_lat.get("latency_ms")
        return _extract_turn_latency_ms(inner)
    if isinstance(raw_lat, bool):
        raise ValueError(f"Invalid boolean latency_ms value: {raw_lat!r}")
    if isinstance(raw_lat, str):
        stripped = raw_lat.strip()
        if not stripped:
            return 0.0
        if stripped.startswith("{"):
            parsed_obj = json.loads(stripped)
            if isinstance(parsed_obj, dict):
                return _extract_turn_latency_ms(parsed_obj)
        lat_val = float(stripped)
    else:
        lat_val = float(raw_lat)
    if not math.isfinite(lat_val) or lat_val < 0.0:
        raise ValueError(f"Invalid latency_ms value: {raw_lat!r}")
    return lat_val


def _validate_raw_turn_nonnegative(it: dict[str, Any]) -> None:
    """Rejects negative or non-finite raw token/cost fields so crafted negative turns cannot mask regressions."""
    for field_name in (
        "input_tokens",
        "prompt_tokens",
        "cached_tokens",
        "output_tokens",
        "completion_tokens",
        "thinking_tokens",
        "total_tokens",
        "total_cost",
        "estimated_cost",
        "cost",
    ):
        val = it.get(field_name)
        if val is None:
            continue
        if isinstance(val, bool):
            raise ValueError(f"Invalid boolean value for {field_name}: {val!r}")
        num = float(val)
        if not math.isfinite(num) or num < 0.0:
            raise ValueError(
                f"Negative or non-finite {field_name}={val!r} in trace turn"
            )


def _validate_cli_thresholds(
    max_cost_per_turn: float | None,
    min_cache_rate: float | None,
    max_tokens_per_turn: int | None,
    max_latency_ms: int | None,
) -> list[str]:
    """Validates all CLI/SDK gate thresholds for finiteness and valid ranges (fail-closed on NaN/Inf/out-of-range)."""
    violations: list[str] = []
    if max_cost_per_turn is not None:
        if (
            isinstance(max_cost_per_turn, bool)
            or not isinstance(max_cost_per_turn, (int, float))
            or not math.isfinite(float(max_cost_per_turn))
            or float(max_cost_per_turn) < 0.0
        ):
            violations.append(
                f"❌ [Invalid Threshold]: --max-cost-per-turn must be a finite non-negative number, got {max_cost_per_turn!r}"
            )
    if min_cache_rate is not None:
        if (
            isinstance(min_cache_rate, bool)
            or not isinstance(min_cache_rate, (int, float))
            or not math.isfinite(float(min_cache_rate))
            or not (0.0 <= float(min_cache_rate) <= 1.0)
        ):
            violations.append(
                f"❌ [Invalid Threshold]: --min-cache-rate must be a finite ratio in [0.0, 1.0], got {min_cache_rate!r}"
            )
    if max_tokens_per_turn is not None:
        if (
            isinstance(max_tokens_per_turn, bool)
            or not isinstance(max_tokens_per_turn, (int, float))
            or not math.isfinite(float(max_tokens_per_turn))
            or float(max_tokens_per_turn) < 0.0
        ):
            violations.append(
                f"❌ [Invalid Threshold]: --max-tokens-per-turn must be a finite non-negative integer, got {max_tokens_per_turn!r}"
            )
    if max_latency_ms is not None:
        if (
            isinstance(max_latency_ms, bool)
            or not isinstance(max_latency_ms, (int, float))
            or not math.isfinite(float(max_latency_ms))
            or float(max_latency_ms) < 0.0
        ):
            violations.append(
                f"❌ [Invalid Threshold]: --max-latency-ms must be a finite non-negative integer, got {max_latency_ms!r}"
            )
    return violations


def _is_non_result_adk_config_file(file_path: str) -> bool:
    base = os.path.basename(file_path).lower()
    return base.endswith(".evalset.json") or base == "test_config.json"


def _discover_directory_trace_artifacts(dir_path: str) -> list[str]:
    direct = [
        p
        for p in sorted(glob.glob(os.path.join(dir_path, "*.json")))
        if not _is_non_result_adk_config_file(p)
    ]
    nested_eval_dir = os.path.join(dir_path, ".adk", "eval_history")
    nested = (
        sorted(
            glob.glob(os.path.join(nested_eval_dir, "*.evalset_result.json"))
        )
        if os.path.isdir(nested_eval_dir)
        else []
    )
    seen_abs: set[str] = set()
    ordered: list[str] = []
    for candidate in direct + nested:
        ap = os.path.abspath(candidate)
        if ap not in seen_abs:
            seen_abs.add(ap)
            ordered.append(candidate)
    return ordered


def run_cli_assertions(
    traces_path: str | None = None,
    results_path: str | None = None,
    max_cost_per_turn: float | None = None,
    min_cache_rate: float | None = None,
    max_tokens_per_turn: int | None = None,
    max_latency_ms: int | None = None,
    allow_empty: bool = False,
    output_format: str = "text",
) -> int:
    """Runs automated CI/CD assertions against trace files or ADK eval outputs. Returns exit code (0 = pass, 1 = fail)."""
    if output_format != "json":
        print("\n" + "=" * 80)
        print(" 🚦 ADK TOKENOMICS CI/CD FINANCIAL ASSERTIONS RUNNER")
        print("=" * 80)

    threshold_violations = _validate_cli_thresholds(
        max_cost_per_turn=max_cost_per_turn,
        min_cache_rate=min_cache_rate,
        max_tokens_per_turn=max_tokens_per_turn,
        max_latency_ms=max_latency_ms,
    )
    if threshold_violations:
        if output_format == "json":
            print(
                json.dumps(
                    {
                        "passed": False,
                        "total_turns": 0,
                        "failed_artifacts": [],
                        "violations": threshold_violations,
                    },
                    indent=2,
                )
            )
        else:
            for v in threshold_violations:
                print(f"[ERROR] {v}")
        return 1

    raw_export = os.environ.get("TOKENOMICS_AUTO_EXPORT_PATH")
    env_export_base = os.path.basename(raw_export) if raw_export else ""
    known_live_names = {"live_adk_session.json", "session.json", "ci_eval.json"}
    if env_export_base:
        known_live_names.add(env_export_base)

    effective_path = traces_path or results_path
    target_files: list[str] = []
    if effective_path:
        if os.path.isdir(effective_path):
            target_files.extend(
                _discover_directory_trace_artifacts(effective_path)
            )
        elif os.path.exists(effective_path):
            target_files.append(effective_path)
        elif (
            os.path.basename(effective_path) in known_live_names
            and os.path.dirname(effective_path)
            and os.path.isdir(os.path.dirname(effective_path))
        ):
            rotated = sorted(
                [
                    p
                    for p in glob.glob(
                        os.path.join(os.path.dirname(effective_path), "*.json")
                    )
                    if not _is_non_result_adk_config_file(p)
                ],
                key=os.path.getmtime,
                reverse=True,
            )
            if rotated:
                target_files.append(rotated[0])
    else:
        if os.path.isdir("artifacts/traces"):
            target_files.extend(
                p
                for p in sorted(glob.glob("artifacts/traces/*.json"))
                if not _is_non_result_adk_config_file(p)
            )
        if not target_files and os.path.isdir(
            os.path.join(".adk", "eval_history")
        ):
            target_files.extend(
                sorted(
                    glob.glob(
                        os.path.join(
                            ".adk", "eval_history", "*.evalset_result.json"
                        )
                    )
                )
            )

    if not target_files:
        if allow_empty:
            if output_format == "json":
                print(
                    json.dumps(
                        {
                            "passed": True,
                            "total_turns": 0,
                            "failed_artifacts": [],
                            "violations": [],
                        }
                    )
                )
            else:
                print(
                    "[WARN] No trace files found to assert (--allow-empty enabled)."
                )
            return 0
        msg = (
            "❌ [Missing Telemetry]: No trace or eval files found to assert. "
            "Checked --traces, ./artifacts/traces/, and ./.adk/eval_history/"
        )
        if output_format == "json":
            print(
                json.dumps(
                    {
                        "passed": False,
                        "total_turns": 0,
                        "failed_artifacts": [],
                        "violations": [msg],
                    },
                    indent=2,
                )
            )
        else:
            print(f"[ERROR] {msg}")
        return 1

    if output_format != "json":
        print(f"📁 Analyzing {len(target_files)} trace artifact(s)...")

    total_turns = 0
    total_cost = 0.0
    total_prompt = 0
    total_cached = 0
    total_tokens = 0
    total_latency_ms = 0.0
    latency_samples = 0
    parse_errors: list[str] = []
    failed_artifacts: list[str] = []
    artifact_stats: list[dict[str, Any]] = []

    def _add_failed_artifact(path: str) -> None:
        if path and path not in failed_artifacts:
            failed_artifacts.append(path)

    from adk_tokenomics_plugin.cli_profiler import parse_trace_or_eval_payload
    from adk_tokenomics_plugin.core.pricing import (
        PricingEngine,
        normalize_turn_tokens,
    )

    pricing = PricingEngine()
    has_live_export_file = len(target_files) > 1 and any(
        os.path.basename(p) in known_live_names for p in target_files
    )
    seen_cross_file_counts: Counter[tuple[Any, ...]] = Counter()

    for tf in target_files:
        try:
            with open(tf, encoding="utf-8") as f:
                data = json.load(f)
            parsed_turns = parse_trace_or_eval_payload(
                data,
                default_model=str(pricing.config.default_model),
                allow_estimated_tokens=False,
            )

            tf_turns = 0
            tf_cost = 0.0
            tf_prompt = 0
            tf_cached = 0
            tf_tokens = 0
            tf_lat_ms = 0.0
            tf_lat_samples = 0
            file_key_counts: Counter[tuple[Any, ...]] = Counter()

            for it in parsed_turns:
                _validate_raw_turn_nonnegative(it)
                # Note: normalize_turn_tokens handles both Google GenAI (prompt_tokens includes cached_tokens)
                # and Anthropic/LiteLLM semantics (when cached_tokens > prompt_tokens, fresh_tokens = prompt_tokens).
                norm = normalize_turn_tokens(it)
                fresh_p = norm["fresh_tokens"]
                c = norm["cached_tokens"]
                prompt_total = norm["prompt_tokens"]
                o = norm["output_tokens"]
                th = norm["thinking_tokens"]
                m = str(
                    it.get("model_name")
                    or it.get("model")
                    or pricing.config.default_model
                )
                lat = _extract_turn_latency_ms(it.get("latency_ms"))

                if (fresh_p + c + o + th) > 0:
                    cost = float(pricing.calculate_cost(m, fresh_p, c, o, th))
                else:
                    cost = float(
                        it.get("total_cost")
                        or it.get("estimated_cost")
                        or it.get("cost")
                        or 0.0
                    )

                tf_turns += 1
                tf_cost += cost
                tf_prompt += prompt_total
                tf_cached += c
                tf_tokens += prompt_total + o + th
                tf_lat_ms += lat
                if lat > 0:
                    tf_lat_samples += 1

                dedup_eligible = len(target_files) > 1 and (
                    has_live_export_file
                    or bool(
                        it.get("session_id")
                        or it.get("invocation_id")
                        or it.get("timestamp")
                    )
                )
                if dedup_eligible:
                    key_dict = {
                        **it,
                        "model_name": m,
                        "input_tokens": fresh_p,
                        "cached_tokens": c,
                        "output_tokens": o,
                        "thinking_tokens": th,
                    }
                    t_key = TokenTracker._turn_dedup_key(key_dict)
                    file_key_counts[t_key] += 1
                    if file_key_counts[t_key] <= seen_cross_file_counts[t_key]:
                        continue

                total_turns += 1
                total_cost += cost
                total_prompt += prompt_total
                total_cached += c
                total_tokens += prompt_total + o + th
                total_latency_ms += lat
                # Latency 0 is treated as 'not reported' (matching TokenTracker.record_turn's None -> 0 coercion).
                if lat > 0:
                    latency_samples += 1

            for t_key, cnt in file_key_counts.items():
                seen_cross_file_counts[t_key] = max(
                    seen_cross_file_counts[t_key], cnt
                )

            artifact_stats.append(
                {
                    "file": tf,
                    "turns": tf_turns,
                    "cost": tf_cost,
                    "prompt": tf_prompt,
                    "cached": tf_cached,
                    "tokens": tf_tokens,
                    "lat_ms": tf_lat_ms,
                    "lat_samples": tf_lat_samples,
                }
            )

        except Exception as e:
            err_str = (
                f"❌ [Invalid Trace Artifact]: Failed to parse '{tf}': {e}"
            )
            parse_errors.append(err_str)
            _add_failed_artifact(tf)
            if output_format != "json":
                print(f"[ERROR] {err_str}")

    if total_turns == 0 and not parse_errors:
        if allow_empty:
            if output_format == "json":
                print(
                    json.dumps(
                        {
                            "passed": True,
                            "total_turns": 0,
                            "failed_artifacts": [],
                            "violations": [],
                        }
                    )
                )
            else:
                print(
                    "[INFO] No turns found in traces (--allow-empty enabled).\n"
                )
            return 0
        parse_errors.append(
            "❌ [Empty Trace]: 0 turns found across provided trace/eval artifacts."
        )
        for tf in target_files:
            _add_failed_artifact(tf)

    avg_cost_turn = (total_cost / total_turns) if total_turns > 0 else 0.0
    cache_rate = (
        (total_cached / total_prompt * 100) if total_prompt > 0 else 0.0
    )
    avg_tokens_turn = (total_tokens / total_turns) if total_turns > 0 else 0.0
    avg_latency_ms = (
        (total_latency_ms / latency_samples) if latency_samples > 0 else 0.0
    )

    if output_format != "json" and total_turns > 0:
        print(f"\n📊 Evaluated Metrics across {total_turns} turn(s):")
        print(f" • Avg Cost / Turn   : ${avg_cost_turn:.5f}")
        print(f" • Cache Hit Rate    : {cache_rate:.1f}%")
        print(f" • Avg Tokens / Turn : {avg_tokens_turn:,.0f}")
        if latency_samples > 0:
            print(
                f" • Avg Latency / Turn: {avg_latency_ms:,.0f} ms ({latency_samples}/{total_turns} turns report latency)"
            )

    violations = list(parse_errors)
    cost_breach = False
    cache_breach = False
    tokens_breach = False
    latency_breach = False
    missing_latency_breach = False

    if total_turns > 0:
        if max_cost_per_turn is not None and avg_cost_turn > max_cost_per_turn:
            cost_breach = True
            violations.append(
                f"❌ [Cost Regression]: Avg cost per turn (${avg_cost_turn:.5f}) exceeded max budget threshold (${max_cost_per_turn:.5f})"
            )

        if min_cache_rate is not None and (cache_rate / 100.0) < min_cache_rate:
            cache_breach = True
            violations.append(
                f"❌ [Cache Degradation]: Cache hit rate ({cache_rate:.1f}%) fell below minimum required ({min_cache_rate * 100:.1f}%)"
            )

        if (
            max_tokens_per_turn is not None
            and avg_tokens_turn > max_tokens_per_turn
        ):
            tokens_breach = True
            violations.append(
                f"❌ [Context Bloat]: Avg tokens per turn ({avg_tokens_turn:,.0f}) exceeded max threshold ({max_tokens_per_turn:,})"
            )

        if max_latency_ms is not None:
            if latency_samples == 0:
                missing_latency_breach = True
                violations.append(
                    "❌ [Missing Latency Telemetry]: --max-latency-ms was set, but none of the provided artifacts "
                    "report latency_ms, so the latency budget cannot be evaluated."
                )
            elif avg_latency_ms > max_latency_ms:
                latency_breach = True
                violations.append(
                    f"❌ [Latency Regression]: Avg latency per turn ({avg_latency_ms:,.0f} ms) exceeded max threshold ({max_latency_ms:,} ms)"
                )

    if (
        cost_breach
        or cache_breach
        or tokens_breach
        or latency_breach
        or missing_latency_breach
    ):
        for a_stat in artifact_stats:
            a_turns = a_stat["turns"]
            if a_turns <= 0:
                continue
            a_file = a_stat["file"]
            if (
                cost_breach
                and max_cost_per_turn is not None
                and (a_stat["cost"] / a_turns) > max_cost_per_turn
            ):
                _add_failed_artifact(a_file)
            if cache_breach and min_cache_rate is not None:
                a_cache_ratio = (
                    (a_stat["cached"] / a_stat["prompt"])
                    if a_stat["prompt"] > 0
                    else 0.0
                )
                if a_cache_ratio < min_cache_rate:
                    _add_failed_artifact(a_file)
            if (
                tokens_breach
                and max_tokens_per_turn is not None
                and (a_stat["tokens"] / a_turns) > max_tokens_per_turn
            ):
                _add_failed_artifact(a_file)
            if missing_latency_breach and a_stat["lat_samples"] == 0:
                _add_failed_artifact(a_file)
            if (
                latency_breach
                and max_latency_ms is not None
                and a_stat["lat_samples"] > 0
                and (a_stat["lat_ms"] / a_stat["lat_samples"]) > max_latency_ms
            ):
                _add_failed_artifact(a_file)

        if not failed_artifacts:
            for tf in target_files:
                _add_failed_artifact(tf)

    if output_format == "json":
        print(
            json.dumps(
                {
                    "passed": len(violations) == 0,
                    "total_turns": total_turns,
                    "avg_cost_per_turn": round(avg_cost_turn, 6),
                    "cache_hit_rate_pct": round(cache_rate, 2),
                    "avg_tokens_per_turn": round(avg_tokens_turn, 1),
                    "avg_latency_ms": round(avg_latency_ms, 1),
                    "latency_samples": latency_samples,
                    "failed_artifacts": failed_artifacts,
                    "violations": violations,
                },
                indent=2,
            )
        )
        return 1 if violations else 0

    print("\n" + "─" * 80)
    if violations:
        print("🚨 CI/CD ASSERTION GATE FAILED:")
        for v in violations:
            print(f"  {v}")
        for fa in failed_artifacts:
            print(f"  -> Run: adk-tokenomics profile -t {fa}")
        print(
            "\n💥 Pull request or deployment should be blocked until token regression is fixed.\n"
        )
        return 1
    else:
        print("✅ ALL TOKENOMICS & FINOPS BUDGET GATES PASSED!")
        print("🎉 No cost, token, or caching regressions detected.\n")
        return 0
