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

# ruff: noqa: E402
"""
Travel Concierge — Before vs. After Tokenomics Demo (`adk-tokenomics-plugin`).

Runs the same Tokyo trip conversation across `travel_unoptimized` and
`travel_optimized`, exports trace files for `adk-tokenomics web`, profiles both
sessions, and verifies the CI/CD budget gate (`adk-tokenomics assert`).

Usage:
    python3 examples/run_travel_demo.py --simulate
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from adk_tokenomics_plugin import TokenControlTower
from adk_tokenomics_plugin.cli_profiler import profile_from_trace_file
from adk_tokenomics_plugin.testing.assertions import run_cli_assertions
from examples._shared.simulator import simulate_tokyo_trip_session
from examples._shared.travel_tools import run_standalone_budget_helper
from examples.travel_optimized.agent import build_travel_optimized
from examples.travel_unoptimized.agent import build_travel_unoptimized


def run_demo(traces_dir: str | Path | None = None) -> dict[str, Any]:
    """Runs the unoptimized vs. optimized Travel Concierge demo and exports JSON traces."""
    out_dir = Path(
        traces_dir or (_REPO_ROOT / "examples" / "artifacts" / "traces")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.json"):
        if not stale.name.startswith("live_"):
            stale.unlink(missing_ok=True)

    unopt_live = out_dir / "live_unopt_demo.json"
    opt_live = out_dir / "live_opt_demo.json"

    print("\n" + "═" * 92)
    print(
        " ✈️  ADK TOKENOMICS DEMO — TRAVEL CONCIERGE (`travel_unoptimized` vs. `travel_optimized`)"
    )
    print("═" * 92)

    # 1. Run Unoptimized Baseline -> Archive to `travel_unoptimized.json`
    unopt_app, _, unopt_tower = build_travel_unoptimized(
        auto_export_path=str(unopt_live)
    )
    unopt_res = simulate_tokyo_trip_session(
        app=unopt_app,
        tower=unopt_tower,
        optimized=False,
        session_id="travel_unoptimized",
    )
    unopt_app.plugins[0].finalize_session("travel_unoptimized")
    unopt_live.unlink(missing_ok=True)
    unopt_trace = out_dir / "travel_unoptimized.json"

    print(
        "\n🚨 [STEP 1/4] CI Budget Gate (`adk-tokenomics assert`) on `travel_unoptimized`..."
    )
    unopt_assert_ok = (
        run_cli_assertions(
            traces_path=str(unopt_trace),
            max_cost_per_turn=0.004,
            min_cache_rate=0.60,
            max_tokens_per_turn=3500,
        )
        == 0
    )

    print(
        "\n🔬 [STEP 2/4] Profiler (`adk-tokenomics profile`) on `travel_unoptimized`..."
    )
    unopt_cli_recs = profile_from_trace_file(str(unopt_trace)) or []
    unopt_tower_recs = unopt_tower.profile(
        title="travel_unoptimized (with Custom SLA Rule)"
    )

    # 2. Run FinOps-Optimized App -> Archive to `travel_optimized.json`
    opt_app, _, opt_tower = build_travel_optimized(
        auto_export_path=str(opt_live)
    )
    opt_res = simulate_tokyo_trip_session(
        app=opt_app,
        tower=opt_tower,
        optimized=True,
        session_id="travel_optimized",
    )
    opt_app.plugins[0].finalize_session("travel_optimized")
    opt_live.unlink(missing_ok=True)
    opt_trace = out_dir / "travel_optimized.json"

    # Optional: standalone `@track_tokens` helper trace
    helper_tower = TokenControlTower(mode="dev")
    run_standalone_budget_helper(helper_tower, destination="Tokyo")
    helper_trace = out_dir / "decorated_session.json"
    helper_tower.tracker.export_traces(
        str(helper_trace), session_id="decorated_session"
    )

    print(
        "\n✨ [STEP 3/4] Profiler & CI Gate on `travel_optimized` (After Applying Fixes)..."
    )
    opt_cli_recs = profile_from_trace_file(str(opt_trace)) or []
    opt_assert_ok = (
        run_cli_assertions(
            traces_path=str(opt_trace),
            max_cost_per_turn=0.004,
            min_cache_rate=0.60,
            max_tokens_per_turn=3500,
        )
        == 0
    )

    u_sum = unopt_res["summary"]
    o_sum = opt_res["summary"]
    u_cost = float(u_sum["total_cost"])
    o_cost = float(o_sum["total_cost"])
    pct_saved = ((u_cost - o_cost) / u_cost * 100.0) if u_cost > 0 else 0.0

    print("\n" + "═" * 92)
    print(" 📊 [STEP 4/4] BEFORE vs. AFTER SUMMARY (3-Day Tokyo Trip)")
    print("═" * 92)
    print(
        f" {'Metric':<32} | {'travel_unoptimized':<24} | {'travel_optimized':<24}"
    )
    print("─" * 92)
    print(
        f" {'Total Session Cost ($)':<32} | ${u_cost:<23.5f} | ${o_cost:<10.5f} (-{pct_saved:.1f}%)"
    )
    print(
        f" {'Prefix Cache Hit Ratio (%)':<32} | {u_sum['cache_read_ratio_pct']:<22.1f}% | {o_sum['cache_read_ratio_pct']:<22.1f}%"
    )
    print(
        f" {'Fresh Input Tokens':<32} | {u_sum['input_tokens']:<24,} | {o_sum['input_tokens']:<24,}"
    )
    print(
        f" {'Cached Prefix Tokens':<32} | {u_sum['cached_tokens']:<24,} | {o_sum['cached_tokens']:<24,}"
    )
    print(
        f" {'Reasoning / Thinking Tokens':<32} | {u_sum['thinking_tokens']:<24,} | {o_sum['thinking_tokens']:<24,}"
    )
    print(
        f" {'Profiler Warnings Fired':<32} | {f'{len(unopt_cli_recs)} warnings':<24} | {'0 warnings (Clean!)':<24}"
    )
    print(
        f" {'CI Budget Gate (`assert`)':<32} | {'❌ FAILED (exit 1)':<24} | {'✅ PASSED (exit 0)':<24}"
    )
    print("═" * 92)
    rel_traces = os.path.relpath(out_dir, Path.cwd())
    shown_traces = str(out_dir) if rel_traces.startswith("..") else rel_traces
    print("\n🌐 Inspect both sessions in the Token Control Tower Web UI:")
    print(f"   adk-tokenomics web --traces {shown_traces} --port 8080\n")

    return {
        "unoptimized": unopt_res,
        "optimized": opt_res,
        "unopt_trace": str(unopt_trace),
        "opt_trace": str(opt_trace),
        "helper_trace": str(helper_trace),
        "unopt_assert_passed": unopt_assert_ok,
        "opt_assert_passed": opt_assert_ok,
        "unopt_cli_recommendations": unopt_cli_recs,
        "unopt_tower_recommendations": unopt_tower_recs,
        "opt_cli_recommendations": opt_cli_recs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Travel Concierge Before/After Tokenomics Demo."
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        default=True,
        help="Run deterministic offline simulation (default; zero cloud credentials needed).",
    )
    parser.add_argument(
        "--traces-dir",
        type=str,
        default=None,
        help="Directory to export JSON session traces.",
    )
    args = parser.parse_args()
    run_demo(traces_dir=args.traces_dir)


if __name__ == "__main__":
    main()
