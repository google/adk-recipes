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
Verification Suite for the Beginner-Friendly Travel Concierge Example (`examples/`).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import examples.travel_optimized as travel_opt_pkg
import examples.travel_unoptimized as travel_unopt_pkg
from adk_tokenomics_plugin import TokenomicsTestCase
from examples._shared.adk_compat import (
    FLASH_MODEL,
    FRONTIER_MODEL,
    build_planner,
    genai_types,
    load_travel_skill_toolset,
    normalize_adk_model_id,
)
from examples._shared.travel_tools import (
    get_city_weather,
    search_flights,
    search_hotels,
)
from examples.run_travel_demo import run_demo
from examples.travel_optimized.agent import build_travel_optimized
from examples.travel_unoptimized.agent import build_travel_unoptimized


class TestTravelConciergeExample(TokenomicsTestCase):
    """Tests `examples/travel_optimized` and `examples/travel_unoptimized`."""

    def test_01_package_entrypoints_and_3_agent_topology(self) -> None:
        for pkg in (travel_unopt_pkg, travel_opt_pkg):
            self.assertTrue(hasattr(pkg, "agent"))
            self.assertTrue(hasattr(pkg, "root_agent"))
            self.assertTrue(hasattr(pkg, "app"))
            self.assertTrue(hasattr(pkg, "MODEL_NAME"))
            self.assertEqual(pkg.root_agent.name, "concierge_router")

        with tempfile.TemporaryDirectory() as tmpdir:
            unopt_app, unopt_root, _ = build_travel_unoptimized(
                auto_export_path=f"{tmpdir}/unopt.json"
            )
            opt_app, opt_root, _ = build_travel_optimized(
                auto_export_path=f"{tmpdir}/opt.json"
            )

            self.assertIsNone(unopt_app.context_cache_config)
            self.assertIsNone(unopt_app.events_compaction_config)
            self.assertEqual(unopt_root.model, FRONTIER_MODEL)
            self.assertEqual(unopt_root.tools[0].agent.model, FRONTIER_MODEL)
            self.assertEqual(unopt_root.sub_agents[0].model, FRONTIER_MODEL)

            self.assertIsNotNone(opt_app.context_cache_config)
            self.assertEqual(opt_app.context_cache_config.min_tokens, 1024)
            self.assertIsNotNone(opt_app.events_compaction_config)
            self.assertEqual(
                opt_app.events_compaction_config.compaction_interval, 4
            )
            self.assertEqual(opt_root.model, FLASH_MODEL)
            self.assertEqual(opt_root.tools[0].agent.model, FLASH_MODEL)
            self.assertEqual(opt_root.sub_agents[0].model, FRONTIER_MODEL)

    def test_02_planner_and_model_id_normalization(self) -> None:
        self.assertEqual(
            normalize_adk_model_id("publishers/google/models/gemini-3.8-flash"),
            "gemini-3.8-flash",
        )
        p_min, c_min = build_planner("gemini-3.8-flash", "minimal")
        self.assertIsNotNone(p_min)
        self.assertEqual(
            p_min.thinking_config.thinking_level,
            genai_types.ThinkingLevel.LOW,
        )
        self.assertFalse(p_min.thinking_config.include_thoughts)
        self.assertEqual(c_min.temperature, 1.0)

        p_g4, _ = build_planner("gemini-4-argon", "minimal")
        self.assertEqual(
            p_g4.thinking_config.thinking_level,
            genai_types.ThinkingLevel.LOW,
        )

        p_25, _ = build_planner("gemini-2.5-pro", "low")
        self.assertEqual(p_25.thinking_config.thinking_budget, 256)

        p_3p, c_3p = build_planner("anthropic/claude-3-5-haiku", "low")
        self.assertIsNone(p_3p)
        self.assertIsNone(c_3p)

    def test_03_travel_tools_and_skills_work_for_any_city(self) -> None:
        tokyo_flights = search_flights("Tokyo", "SFO", 3)
        self.assertEqual(tokyo_flights["destination"], "Tokyo")
        self.assertGreaterEqual(len(tokyo_flights["options"]), 2)

        fallback_flights = search_flights("Barcelona", "JFK", 5)
        self.assertEqual(fallback_flights["destination"], "Barcelona")
        self.assertGreaterEqual(len(fallback_flights["options"]), 1)

        hotels = search_hotels("Tokyo", max_nightly_usd=160)
        self.assertTrue(all(h["nightly_usd"] <= 160 for h in hotels["hotels"]))

        weather = get_city_weather("Paris")
        self.assertIn("temp_c", weather)

        skill_ts = load_travel_skill_toolset()
        self.assertEqual(len(skill_ts.skills), 2)

    def test_04_end_to_end_demo_fires_all_5_rules_then_passes_cleanly(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            res = run_demo(traces_dir=tmpdir)

            self.assertFalse(res["unopt_assert_passed"])
            self.assertTrue(res["opt_assert_passed"])

            unopt_recs = "\n".join(res["unopt_cli_recommendations"])
            self.assertIn("[Context Caching]", unopt_recs)
            self.assertIn("[History Compaction]", unopt_recs)
            self.assertIn("[Modular Skills]", unopt_recs)
            self.assertIn("[Thinking Budget Tuning]", unopt_recs)
            self.assertIn("[Sub-Agent Model Tiering]", unopt_recs)
            self.assertEqual(len(res["unopt_tower_recommendations"]), 6)

            self.assertEqual(res["opt_cli_recommendations"], [])
            self.assertTrue(Path(res["opt_trace"]).is_file())
            self.assertTrue(Path(res["unopt_trace"]).is_file())
            self.assertTrue(Path(res["helper_trace"]).is_file())

            opt_turns = res["optimized"]["turns"]
            self.assertEqual(len(opt_turns), 4)
            for t in opt_turns:
                self.assertEqual(t["session_id"], "travel_optimized")
            self.assertIn("japan-guide", opt_turns[0].get("skills_active", []))

            opt_summary = res["optimized"]["summary"]
            self.assertCostLessThan(opt_summary, max_dollars=0.020)
            self.assertCacheHitRatioGreaterThan(opt_summary, min_ratio_pct=75.0)
            self.assertTokenBudget(opt_summary, max_tokens=15000)
            self.assertThinkingTokensWithin(
                opt_summary, min_tokens=0, max_tokens=512
            )


if __name__ == "__main__":
    unittest.main()
