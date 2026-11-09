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

# ruff: noqa: PLW1510
import os
import sys
import unittest
from typing import Any

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.append(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
    )
)

from adk_tokenomics_plugin import (
    ADKTokenomicsPlugin,
    FinOpsAggregator,
    InMemorySink,
    PricingEngine,
    TokenControlTower,
    TokenomicsConfig,
    TokenomicsTestCase,
    TokenTracker,
    track_tokens,
)


class TestTokenomicsSDK(TokenomicsTestCase):
    def test_pricing_engine_calculations(self):
        engine = PricingEngine()
        res = engine.calculate_turn_cost(
            model_id="publishers/google/models/gemini-3.8-flash",
            input_tokens=10000,
            cached_tokens=40000,
            output_tokens=2000,
            thinking_tokens=1000,
        )
        # 10k * 0.75/1M (0.0075) + 40k * 0.075/1M (0.0030) + 3k * 3.75/1M (0.01125) = 0.02175
        self.assertAlmostEqual(res["input_cost"], 0.0075, places=8)
        self.assertAlmostEqual(res["cached_cost"], 0.0030, places=8)
        self.assertAlmostEqual(res["output_cost"], 0.01125, places=8)
        self.assertAlmostEqual(res["total_cost"], 0.02175, places=8)
        self.assertAlmostEqual(
            res["hypothetical_uncached_cost"], 0.04875, places=8
        )
        self.assertAlmostEqual(res["savings_dollars"], 0.0270, places=8)
        self.assertAlmostEqual(
            res["savings_pct"], (0.0270 / 0.04875) * 100.0, places=4
        )

    def test_token_tracker_and_assertions(self):
        with self.track_tokens() as tracker:
            tracker.record_turn(
                session_id="s1",
                app_name="caching_app",
                model_name="publishers/google/models/gemini-3.8-flash",
                user_query="Hello",
                agent_response="Hi there!",
                input_tokens=5000,
                cached_tokens=20000,
                output_tokens=500,
                thinking_tokens=100,
            )
            self.assertCostLessThan(tracker, max_dollars=0.01)
            self.assertCacheHitRatioGreaterThan(tracker, min_ratio_pct=70.0)
            self.assertTokenBudget(tracker, max_tokens=30000)
            self.assertThinkingTokensWithin(
                tracker, min_tokens=50, max_tokens=1000
            )

    def test_finops_aggregator(self):
        records = [
            {
                "model_name": "publishers/google/models/gemini-3.8-flash",
                "app_name": "caching_app",
                "total_cost": 0.01,
                "input_tokens": 1000,
                "cached_tokens": 5000,
                "output_tokens": 200,
                "thinking_tokens": 50,
            },
            {
                "model_name": "claude-3-5-sonnet-v2@20241022",
                "app_name": "compaction_app",
                "total_cost": 0.05,
                "input_tokens": 2000,
                "cached_tokens": 0,
                "output_tokens": 400,
                "thinking_tokens": 0,
            },
        ]
        agg = FinOpsAggregator.aggregate_records(records)
        self.assertEqual(agg["status"], "success")
        self.assertEqual(agg["kpis"]["total_turns"], 2)
        self.assertEqual(agg["kpis"]["top_provider"], "Anthropic Claude")
        self.assertEqual(len(agg["matrix_rows"]), 2)

    def test_high_level_tower_init(self):
        tower = TokenControlTower(mode="dev")
        self.assertIsNotNone(tower.tracker)
        plugin = tower.create_adk_plugin(app_name="test_bot")
        self.assertEqual(plugin.app_name, "test_bot")

    def test_adk_plugin_lifecycle_callbacks_and_latency(self):
        tower = TokenControlTower(mode="dev")
        plugin = tower.create_adk_plugin(app_name="adk_native_bot")

        # Simulate ADK before_model_callback
        dummy_part = type("Part", (), {"text": "Book a flight to Tokyo"})()
        dummy_content = type(
            "Content", (), {"role": "user", "parts": [dummy_part]}
        )()
        dummy_req = type("LlmRequest", (), {"contents": [dummy_content]})()
        plugin.before_model_callback(llm_request=dummy_req)

        # Simulate ADK after_tool_callback
        dummy_tool = type("Tool", (), {"name": "activate_skill"})()
        plugin.after_tool_callback(
            tool=dummy_tool, tool_args={"skill_name": "tokyo-travel"}
        )

        # Simulate ADK after_model_callback
        dummy_usage = type(
            "UsageMetadata",
            (),
            {
                "prompt_token_count": 3000,
                "cached_content_token_count": 2000,
                "candidates_token_count": 400,
                "thoughts_token_count": 100,
            },
        )()
        resp_part = type("Part", (), {"text": "Flight booked!"})()
        resp_content = type("Content", (), {"parts": [resp_part]})()
        dummy_cand = type("Candidate", (), {"content": resp_content})()
        dummy_resp = type(
            "LlmResponse",
            (),
            {
                "usage_metadata": dummy_usage,
                "model_version": "publishers/google/models/gemini-3.8-flash",
                "candidates": [dummy_cand],
            },
        )()
        dummy_ctx = type(
            "CallbackContext",
            (),
            {"session_id": "sess_tokyo_1", "invocation_id": "inv_99"},
        )()
        plugin.after_model_callback(
            callback_context=dummy_ctx, llm_response=dummy_resp
        )

        summary = tower.get_summary()
        self.assertEqual(summary["total_turns"], 1)
        self.assertEqual(summary["input_tokens"], 1000)
        self.assertEqual(summary["cached_tokens"], 2000)
        self.assertEqual(summary["output_tokens"], 400)
        self.assertEqual(summary["thinking_tokens"], 100)

        last_turn = tower.tracker.turns_history[-1]
        self.assertEqual(last_turn["user_query"], "Book a flight to Tokyo")
        self.assertIn("activate_skill", last_turn["tools_called"])
        self.assertIn("tokyo-travel", last_turn["skills_active"])
        self.assertGreaterEqual(last_turn["latency_ms"], 1)

    def test_custom_pricing_and_json_rate_keys(self):
        cfg = TokenomicsConfig(
            custom_pricing={
                "custom-enterprise-llm": {
                    "input": 2.00,
                    "cached": 0.20,
                    "output": 8.00,
                }
            }
        )
        engine = PricingEngine(cfg)
        rate = engine.get_rate("custom-enterprise-llm")
        self.assertEqual(rate["input_per_million"], 2.00)
        self.assertEqual(rate["cached_per_million"], 0.20)
        self.assertEqual(rate["output_per_million"], 8.00)

        # Verify claude-sonnet-5 loaded from models_config.json uses 3.00 / 0.30 / 15.00
        claude_rate = engine.get_rate("claude-sonnet-5")
        self.assertEqual(claude_rate["input_per_million"], 3.00)
        self.assertEqual(claude_rate["cached_per_million"], 0.30)
        self.assertEqual(claude_rate["output_per_million"], 15.00)

    def test_custom_optimization_prompts_and_rules(self):
        tower = TokenControlTower(
            mode="dev",
            optimization_rules={
                "min_cache_tokens": 2048,
                "max_thinking_to_output_ratio": 0.50,
            },
            optimization_prompts={
                "context_caching": "CUSTOM CACHE ALERT: {turns_str} exceeded {min_cache_tokens} tokens!",
            },
        )
        tower.add_recommendation_rule(
            lambda ctx: (
                f"CUSTOM BUDGET RULE: Total cost ${ctx['total_cost']:.5f} evaluated."
            )
        )
        tower.track_turn(
            session_id="s_opt",
            app_name="pitch_studio",
            model_name="publishers/google/models/gemini-3.8-flash",
            user_query="[researcher] Gather Ada Lovelace dossier",
            agent_response="Biographical notes...",
            input_tokens=4096,
            cached_tokens=0,
            output_tokens=500,
            thinking_tokens=1024,
        )
        recs = tower.profile(title="Custom Rules Test")
        self.assertTrue(
            any(
                "CUSTOM CACHE ALERT: Turn #1 (4,096 tokens) exceeded 2048 tokens!"
                in r
                for r in recs
            )
        )
        self.assertTrue(any("Thinking Budget Tuning" in r for r in recs))
        self.assertTrue(any("CUSTOM BUDGET RULE:" in r for r in recs))

        advisor_payload = tower.get_finops_advisor_prompt(
            title="Custom Rules Test",
            custom_system_prompt="You are a custom FinOps reviewer.",
        )
        self.assertEqual(
            advisor_payload["system_instruction"],
            "You are a custom FinOps reviewer.",
        )
        self.assertIn("Ada Lovelace", advisor_payload["user_prompt"])

    def test_3p_and_open_weight_models_support(self):
        tower = TokenControlTower(mode="dev")

        # 1. Built-in 3P & Open-Weight prefix normalization (Vertex Model Garden, LiteLLM, Ollama)
        claude_rate = tower.pricing.get_rate(
            "publishers/anthropic/models/claude-3-5-sonnet-v2@20241022"
        )
        self.assertEqual(claude_rate["input_per_million"], 3.00)
        self.assertEqual(claude_rate["cached_per_million"], 0.30)
        self.assertEqual(claude_rate["output_per_million"], 15.00)

        haiku_rate = tower.pricing.get_rate(
            "anthropic/claude-3-5-haiku-20241022"
        )
        self.assertEqual(haiku_rate["input_per_million"], 0.80)
        self.assertEqual(haiku_rate["output_per_million"], 4.00)

        llama_rate = tower.pricing.get_rate(
            "together_ai/meta-llama/Llama-3.3-70B-Instruct"
        )
        self.assertEqual(llama_rate["input_per_million"], 0.60)
        self.assertEqual(llama_rate["output_per_million"], 0.60)

        deepseek_rate = tower.pricing.get_rate(
            "fireworks_ai/deepseek/deepseek-r1"
        )
        self.assertEqual(deepseek_rate["input_per_million"], 0.55)
        self.assertEqual(deepseek_rate["output_per_million"], 2.19)

        ollama_rate = tower.pricing.get_rate("ollama/qwen2.5-coder:32b")
        self.assertEqual(ollama_rate["input_per_million"], 0.0)
        self.assertEqual(ollama_rate["output_per_million"], 0.0)

        # 2. Registering a custom 3P / Open-Weight model at runtime
        tower.register_model(
            model_id="together_ai/NousResearch/Hermes-3-Llama-3.1-405B",
            input_per_million=3.50,
            cached_per_million=0.85,
            output_per_million=3.50,
            display_name="Hermes 3 405B (Together AI)",
            tier="Open-Weight Frontier",
        )
        hermes_rate = tower.pricing.get_rate(
            "NousResearch/Hermes-3-Llama-3.1-405B"
        )
        self.assertEqual(hermes_rate["input_per_million"], 3.50)
        self.assertEqual(hermes_rate["cached_per_million"], 0.85)

        # 3. ADKTokenomicsPlugin with LiteLLM / Anthropic usage_metadata dict
        plugin = ADKTokenomicsPlugin(tower=tower, app_name="multi_model_agent")
        user_part = type(
            "Part", (), {"text": "Summarize architecture with Claude"}
        )()
        user_msg = type("Content", (), {"role": "user", "parts": [user_part]})()
        llm_req = type(
            "LlmRequest",
            (),
            {
                "model": "anthropic/claude-3-7-sonnet",
                "contents": [user_msg],
            },
        )()
        plugin.before_model_callback(callback_context=None, llm_request=llm_req)

        # Anthropic / LiteLLM style usage dict (prompt_tokens, cache_read_input_tokens, completion_tokens, reasoning_tokens)
        litellm_usage = {
            "prompt_tokens": 5000,
            "cache_read_input_tokens": 2000,
            "completion_tokens": 800,
            "reasoning_tokens": 200,
        }
        resp_part = type(
            "Part", (), {"text": "Architecture summary complete."}
        )()
        resp_content = type("Content", (), {"parts": [resp_part]})()
        dummy_cand = type("Candidate", (), {"content": resp_content})()
        # Note: model_version omitted on response to verify fallback to llm_request.model
        dummy_resp = type(
            "LlmResponse",
            (),
            {
                "usage_metadata": litellm_usage,
                "model_version": None,
                "candidates": [dummy_cand],
            },
        )()
        dummy_ctx = type(
            "CallbackContext",
            (),
            {"session_id": "sess_3p_1", "invocation_id": "inv_3p_1"},
        )()
        plugin.after_model_callback(
            callback_context=dummy_ctx, llm_response=dummy_resp
        )

        last_turn = tower.tracker.turns_history[-1]
        self.assertEqual(last_turn["model_name"], "anthropic/claude-3-7-sonnet")
        self.assertEqual(last_turn["input_tokens"], 3000)
        self.assertEqual(last_turn["cached_tokens"], 2000)
        self.assertEqual(last_turn["output_tokens"], 800)
        self.assertEqual(last_turn["thinking_tokens"], 200)

        # 4. FinOpsAggregator provider classification for 3P and Open-Weight models
        self.assertEqual(
            FinOpsAggregator.classify_provider("anthropic/claude-3-7-sonnet"),
            "Anthropic Claude",
        )
        self.assertEqual(
            FinOpsAggregator.classify_provider("openai/gpt-4o"), "OpenAI"
        )
        self.assertEqual(
            FinOpsAggregator.classify_provider("meta/llama-3.3-70b-instruct"),
            "Open-Weight / MaaS",
        )
        self.assertEqual(
            FinOpsAggregator.classify_provider("deepseek/deepseek-r1"),
            "Open-Weight / MaaS",
        )
        self.assertEqual(
            FinOpsAggregator.classify_provider("ollama/local-open-weight"),
            "Open-Weight / MaaS",
        )

    def test_async_awaitable_plugin_callbacks(self):
        import asyncio

        tower = TokenControlTower(mode="dev")
        plugin = tower.create_adk_plugin(app_name="async_test_agent")

        async def _run_plugin_manager_hooks():
            res1 = await plugin.before_model_callback(
                callback_context=None, llm_request=None
            )
            res2 = await plugin.after_tool_callback(
                tool=type("T", (), {"name": "search_flights"})(), tool_args={}
            )
            usage = type(
                "U",
                (),
                {
                    "prompt_token_count": 1200,
                    "cached_content_token_count": 400,
                    "candidates_token_count": 200,
                    "thoughts_token_count": 50,
                },
            )()
            resp = type(
                "R",
                (),
                {
                    "usage_metadata": usage,
                    "model_version": "gemini-3.8-flash",
                    "candidates": [],
                },
            )()
            res3 = await plugin.after_model_callback(
                callback_context=None, llm_response=resp
            )
            # Calling on_event_callback with the same usage object should be deduplicated
            res4 = await plugin.on_event_callback(
                invocation_context=None, event=resp
            )
            return res1, res2, res3, res4

        r1, r2, r3, r4 = asyncio.run(_run_plugin_manager_hooks())
        self.assertIsNone(r1)
        self.assertIsNone(r2)
        self.assertIsNone(r3)
        self.assertIsNone(r4)
        self.assertEqual(tower.tracker.total_turns, 1)

    def test_p0_streaming_partial_chunks_and_copied_event_dedup(self):
        tower = TokenControlTower(mode="dev")
        plugin = tower.create_adk_plugin(app_name="streaming_agent")

        ctx = type(
            "CallbackContext",
            (),
            {
                "session_id": "sess_stream_1",
                "invocation_id": "inv_stream_1",
                "agent_name": "planner_agent",
            },
        )()
        user_part = type("Part", (), {"text": "Stream a 3-step plan"})()
        user_msg = type("Content", (), {"role": "user", "parts": [user_part]})()
        req = type(
            "LlmRequest",
            (),
            {"model": "gemini-3.8-flash", "contents": [user_msg]},
        )()
        plugin.before_model_callback(callback_context=ctx, llm_request=req)

        # Intermediate streaming chunks (partial=True) with distinct usage_metadata objects must be ignored
        for chunk_idx in range(1, 4):
            chunk_usage = {
                "prompt_token_count": 2000,
                "cached_content_token_count": 1500,
                "candidates_token_count": chunk_idx * 50,
                "thoughts_token_count": 40,
            }
            partial_resp = type(
                "LlmResponse",
                (),
                {
                    "partial": True,
                    "usage_metadata": chunk_usage,
                    "model_version": "gemini-3.8-flash",
                    "candidates": [],
                },
            )()
            plugin.after_model_callback(
                callback_context=ctx, llm_response=partial_resp
            )
            plugin.on_event_callback(invocation_context=ctx, event=partial_resp)

        self.assertEqual(tower.tracker.total_turns, 0)

        # Final streaming chunk (partial=False)
        final_usage_model = {
            "prompt_token_count": 2000,
            "cached_content_token_count": 1500,
            "candidates_token_count": 200,
            "thoughts_token_count": 40,
        }
        final_resp = type(
            "LlmResponse",
            (),
            {
                "partial": False,
                "usage_metadata": final_usage_model,
                "model_version": "gemini-3.8-flash",
                "candidates": [],
            },
        )()
        plugin.after_model_callback(
            callback_context=ctx, llm_response=final_resp
        )
        self.assertEqual(tower.tracker.total_turns, 1)

        # ADK Runner then emits an Event with a NEW copy of usage_metadata to on_event_callback
        copied_usage_event = dict(final_usage_model)
        runner_event = type(
            "Event",
            (),
            {
                "partial": False,
                "author": "planner_agent",
                "invocation_id": "inv_stream_1",
                "usage_metadata": copied_usage_event,
                "model_version": "gemini-3.8-flash",
            },
        )()
        plugin.on_event_callback(invocation_context=ctx, event=runner_event)

        # Must still be 1 turn (not double-counted)
        self.assertEqual(tower.tracker.total_turns, 1)
        turn = tower.tracker.turns_history[-1]
        self.assertEqual(turn["input_tokens"], 500)
        self.assertEqual(turn["cached_tokens"], 1500)
        self.assertEqual(turn["output_tokens"], 200)
        self.assertEqual(turn["thinking_tokens"], 40)
        self.assertIsNotNone(turn["latency_ms"])

    def test_p0_parallel_subagent_state_isolation(self):
        tower = TokenControlTower(mode="dev")
        plugin = tower.create_adk_plugin(app_name="parallel_orchestrator")

        ctx_a = type(
            "CallbackContext",
            (),
            {
                "session_id": "sess_par",
                "invocation_id": "inv_shared_1",
                "agent_name": "researcher_subagent",
            },
        )()
        ctx_b = type(
            "CallbackContext",
            (),
            {
                "session_id": "sess_par",
                "invocation_id": "inv_shared_1",
                "agent_name": "coder_subagent",
            },
        )()

        # Both sub-agents start before either finishes (interleaved execution)
        req_a = type(
            "LlmRequest",
            (),
            {
                "model": "gemini-3.8-flash",
                "contents": [
                    type(
                        "Content",
                        (),
                        {
                            "role": "user",
                            "parts": [
                                type("Part", (), {"text": "Search papers"})()
                            ],
                        },
                    )()
                ],
            },
        )()
        req_b = type(
            "LlmRequest",
            (),
            {
                "model": "claude-3-7-sonnet",
                "contents": [
                    type(
                        "Content",
                        (),
                        {
                            "role": "user",
                            "parts": [
                                type("Part", (), {"text": "Write kernel"})()
                            ],
                        },
                    )()
                ],
            },
        )()
        plugin.before_model_callback(callback_context=ctx_a, llm_request=req_a)
        plugin.before_model_callback(callback_context=ctx_b, llm_request=req_b)

        # Each sub-agent calls different tools and skills concurrently
        plugin.after_tool_callback(
            tool=type("Tool", (), {"name": "load_skill"})(),
            tool_args={"skill_name": "arxiv-search"},
            tool_context=ctx_a,
        )
        plugin.after_tool_callback(
            tool=type("Tool", (), {"name": "activate_skill"})(),
            tool_args={"skill_name": "rust-codegen"},
            tool_context=ctx_b,
        )

        # Sub-agent A finishes first (model_version=None on response to test isolated request_model lookup)
        resp_a = type(
            "LlmResponse",
            (),
            {
                "usage_metadata": {
                    "prompt_token_count": 1000,
                    "cached_content_token_count": 200,
                    "candidates_token_count": 150,
                },
                "model_version": None,
                "candidates": [],
            },
        )()
        plugin.after_model_callback(callback_context=ctx_a, llm_response=resp_a)

        # Sub-agent B finishes second
        resp_b = type(
            "LlmResponse",
            (),
            {
                "usage_metadata": {
                    "prompt_token_count": 4000,
                    "cached_content_token_count": 1000,
                    "candidates_token_count": 600,
                },
                "model_version": None,
                "candidates": [],
            },
        )()
        plugin.after_model_callback(callback_context=ctx_b, llm_response=resp_b)

        self.assertEqual(tower.tracker.total_turns, 2)
        turn_a, turn_b = (
            tower.tracker.turns_history[-2],
            tower.tracker.turns_history[-1],
        )

        self.assertEqual(turn_a["model_name"], "gemini-3.8-flash")
        self.assertEqual(
            turn_a["user_query"], "[researcher_subagent] Search papers"
        )
        self.assertEqual(turn_a["tools_called"], ["load_skill"])
        self.assertEqual(turn_a["skills_active"], ["arxiv-search"])

        self.assertEqual(turn_b["model_name"], "claude-3-7-sonnet")
        self.assertEqual(turn_b["user_query"], "[coder_subagent] Write kernel")
        self.assertEqual(turn_b["tools_called"], ["activate_skill"])
        self.assertEqual(turn_b["skills_active"], ["rust-codegen"])

    def test_p0_camelcase_usage_metadata_and_bq_ddl_safe_cast(self):
        from adk_tokenomics_plugin.finops.bq_schema import (
            ADK_LLM_RESPONSE_VIEW_DDL,
        )

        tower = TokenControlTower(mode="dev")
        plugin = tower.create_adk_plugin(app_name="camelcase_adapter_agent")

        camel_event = type(
            "Event",
            (),
            {
                "usage_metadata": {
                    "promptTokens": 5000,
                    "cacheReadTokens": 3500,
                    "candidatesTokenCount": 400,
                    "thinkingOutputTokens": 250,
                },
                "model_version": "gemini-3.8-flash",
            },
        )()
        rec = plugin.on_event(
            camel_event, session_id="s_camel", user_query="Test camelCase"
        )
        self.assertIsNotNone(rec)
        self.assertEqual(rec["input_tokens"], 1500)
        self.assertEqual(rec["cached_tokens"], 3500)
        self.assertEqual(rec["output_tokens"], 400)
        self.assertEqual(rec["thinking_tokens"], 250)

        self.assertIn("SAFE_CAST", ADK_LLM_RESPONSE_VIEW_DDL)
        self.assertIn("cacheReadTokens", ADK_LLM_RESPONSE_VIEW_DDL)
        self.assertIn("thinkingOutputTokens", ADK_LLM_RESPONSE_VIEW_DDL)

    def test_p0_end_to_end_pipeline_integration(self):
        import tempfile

        from adk_tokenomics_plugin.cli_profiler import profile_from_trace_file
        from adk_tokenomics_plugin.testing.assertions import run_cli_assertions

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False
        ) as tf:
            trace_path = tf.name

        try:
            tower = TokenControlTower(mode="dev")
            plugin = tower.create_adk_plugin(
                app_name="e2e_agent", auto_export_path=trace_path
            )

            ctx = type(
                "CallbackContext",
                (),
                {
                    "session_id": "sess_e2e_1",
                    "invocation_id": "inv_e2e_1",
                    "agent_name": "orchestrator",
                },
            )()
            req = type(
                "LlmRequest",
                (),
                {
                    "model": "gemini-3.8-flash",
                    "contents": [
                        type(
                            "Content",
                            (),
                            {
                                "role": "user",
                                "parts": [
                                    type(
                                        "Part",
                                        (),
                                        {"text": "Analyze Q3 cloud spend"},
                                    )()
                                ],
                            },
                        )()
                    ],
                },
            )()
            plugin.before_model_callback(callback_context=ctx, llm_request=req)
            plugin.after_tool_callback(
                tool=type("Tool", (), {"name": "load_skill"})(),
                tool_args={"skill_name": "finops-analyzer"},
                tool_context=ctx,
            )

            # Emit a partial streaming chunk followed by final response
            plugin.after_model_callback(
                callback_context=ctx,
                llm_response=type(
                    "LlmResponse",
                    (),
                    {
                        "partial": True,
                        "usage_metadata": {
                            "promptTokens": 10000,
                            "cacheReadTokens": 8000,
                            "candidatesTokenCount": 50,
                        },
                    },
                )(),
            )
            plugin.after_model_callback(
                callback_context=ctx,
                llm_response=type(
                    "LlmResponse",
                    (),
                    {
                        "partial": False,
                        "usage_metadata": {
                            "promptTokens": 10000,
                            "cacheReadTokens": 8000,
                            "candidatesTokenCount": 300,
                            "thinkingOutputTokens": 100,
                        },
                        "candidates": [],
                    },
                )(),
            )

            # Verify exported trace file exists and passes CLI profiler, assertions, and FinOps aggregator
            self.assertTrue(os.path.exists(trace_path))
            profile_from_trace_file(trace_path)
            exit_code = run_cli_assertions(
                traces_path=trace_path,
                max_cost_per_turn=0.05,
                min_cache_rate=0.70,
                max_tokens_per_turn=15000,
                max_latency_ms=5000,
            )
            self.assertEqual(exit_code, 0)

            agg = FinOpsAggregator.aggregate_records(
                tower.tracker.turns_history
            )
            self.assertEqual(agg["kpis"]["total_turns"], 1)
            self.assertEqual(agg["kpis"]["total_input"], 2000)
            self.assertEqual(agg["kpis"]["total_cached"], 8000)
        finally:
            if os.path.exists(trace_path):
                os.remove(trace_path)

    def test_session_trace_rotation_and_content_function_call(self):
        import json
        import shutil
        import tempfile

        tmp_dir = tempfile.mkdtemp()
        try:
            live_path = os.path.join(tmp_dir, "live_adk_session.json")
            tower = TokenControlTower(mode="dev")
            plugin = tower.create_adk_plugin(
                app_name="travel_planner", auto_export_path=live_path
            )

            # Session 1: emits function_call on LlmResponse.content.parts (where candidates is None)
            ctx_1 = type(
                "CallbackContext",
                (),
                {
                    "session_id": "sess_oslo_001",
                    "invocation_id": "inv_1",
                    "agent_name": "flight_hotel_researcher",
                },
            )()
            fc_part = type(
                "Part",
                (),
                {
                    "text": None,
                    "function_call": type(
                        "FunctionCall", (), {"name": "search_flights"}
                    )(),
                },
            )()
            resp_1 = type(
                "LlmResponse",
                (),
                {
                    "usage_metadata": {
                        "prompt_token_count": 1200,
                        "cached_content_token_count": 0,
                        "candidates_token_count": 80,
                    },
                    "model_version": "gemini-3.8-flash",
                    "candidates": None,
                    "content": type("Content", (), {"parts": [fc_part]})(),
                },
            )()
            plugin.after_model_callback(
                callback_context=ctx_1, llm_response=resp_1
            )

            self.assertTrue(os.path.exists(live_path))
            with open(live_path) as f:
                data_1 = json.load(f)
            self.assertEqual(data_1["session_id"], "sess_oslo_001")
            self.assertEqual(len(data_1["turns"]), 1)
            self.assertIn("search_flights", data_1["turns"][0]["tools_called"])

            # Session 2 starts: live_adk_session.json from Session 1 is rotated to sess_oslo_001.json
            ctx_2 = type(
                "CallbackContext",
                (),
                {
                    "session_id": "sess_kyoto_002",
                    "invocation_id": "inv_2",
                    "agent_name": "concierge_router",
                },
            )()
            resp_2 = type(
                "LlmResponse",
                (),
                {
                    "usage_metadata": {
                        "prompt_token_count": 900,
                        "cached_content_token_count": 0,
                        "candidates_token_count": 60,
                    },
                    "model_version": "gemini-3.8-flash",
                    "candidates": None,
                    "content": type(
                        "Content",
                        (),
                        {
                            "parts": [
                                type(
                                    "Part",
                                    (),
                                    {
                                        "text": "Planning Kyoto!",
                                        "function_call": None,
                                    },
                                )()
                            ]
                        },
                    )(),
                },
            )()
            plugin.after_model_callback(
                callback_context=ctx_2, llm_response=resp_2
            )

            archived_oslo = os.path.join(tmp_dir, "sess_oslo_001.json")
            self.assertTrue(os.path.exists(archived_oslo))
            with open(archived_oslo) as f:
                oslo_archive = json.load(f)
            self.assertEqual(len(oslo_archive["turns"]), 1)
            self.assertEqual(
                oslo_archive["turns"][0]["session_id"], "sess_oslo_001"
            )

            # Live file now has ONLY Session 2
            with open(live_path) as f:
                kyoto_live = json.load(f)
            self.assertEqual(len(kyoto_live["turns"]), 1)
            self.assertEqual(
                kyoto_live["turns"][0]["session_id"], "sess_kyoto_002"
            )

            # Session 2 terminates: live_adk_session.json is renamed to sess_kyoto_002.json
            final_path = plugin.finalize_session()
            archived_kyoto = os.path.join(tmp_dir, "sess_kyoto_002.json")
            self.assertEqual(final_path, archived_kyoto)
            self.assertTrue(os.path.exists(archived_kyoto))
            self.assertFalse(os.path.exists(live_path))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_live_adk_tool_call_lifecycle_no_carryover(self):
        tower = TokenControlTower(mode="dev")
        plugin = tower.create_adk_plugin(app_name="travel_planner_agent")

        ctx = type(
            "CallbackContext",
            (),
            {
                "session_id": "sess_england_1",
                "invocation_id": "inv_england_1",
                "agent_name": "flight_hotel_researcher",
            },
        )()
        user_msg = type(
            "Content",
            (),
            {
                "role": "user",
                "parts": [
                    type("Part", (), {"text": "5 day trip to England"})()
                ],
            },
        )()
        req = type(
            "LlmRequest",
            (),
            {"model": "gemini-3.8-flash", "contents": [user_msg]},
        )()

        # Turn 1 (e.g. #8): before_model -> after_model (emits search_flights, search_hotels) -> after_tool
        plugin.before_model_callback(callback_context=ctx, llm_request=req)
        fc1 = type(
            "FunctionCall",
            (),
            {"name": "search_flights", "args": {"destination": "London"}},
        )()
        fc2 = type(
            "FunctionCall",
            (),
            {
                "name": "search_hotels",
                "args": {"destination": "London", "nights": 5},
            },
        )()
        resp1 = type(
            "LlmResponse",
            (),
            {
                "usage_metadata": {
                    "prompt_token_count": 1809,
                    "cached_content_token_count": 0,
                    "candidates_token_count": 40,
                    "thoughts_token_count": 98,
                },
                "model_version": "gemini-3.8-flash",
                "candidates": [
                    type(
                        "Candidate",
                        (),
                        {
                            "content": type(
                                "Content",
                                (),
                                {
                                    "parts": [
                                        type(
                                            "Part",
                                            (),
                                            {
                                                "text": None,
                                                "function_call": fc1,
                                            },
                                        )(),
                                        type(
                                            "Part",
                                            (),
                                            {
                                                "text": None,
                                                "function_call": fc2,
                                            },
                                        )(),
                                    ]
                                },
                            )()
                        },
                    )()
                ],
            },
        )()
        plugin.after_model_callback(callback_context=ctx, llm_response=resp1)

        # In live ADK, after_tool_callback fires AFTER after_model_callback for Turn 1
        plugin.after_tool_callback(
            tool=type("Tool", (), {"name": "search_flights"})(),
            tool_args={"destination": "London"},
            tool_context=ctx,
        )
        plugin.after_tool_callback(
            tool=type("Tool", (), {"name": "search_hotels"})(),
            tool_args={"destination": "London", "nights": 5},
            tool_context=ctx,
        )

        # Turn 2 (e.g. #10): before_model -> after_model (emits ONLY save_trip_state) -> after_tool
        plugin.before_model_callback(callback_context=ctx, llm_request=req)
        fc3 = type(
            "FunctionCall",
            (),
            {"name": "save_trip_state", "args": {"key": "lodging_and_transit"}},
        )()
        resp2 = type(
            "LlmResponse",
            (),
            {
                "usage_metadata": {
                    "prompt_token_count": 2234,
                    "cached_content_token_count": 0,
                    "candidates_token_count": 516,
                    "thoughts_token_count": 122,
                },
                "model_version": "gemini-3.8-flash",
                "candidates": [
                    type(
                        "Candidate",
                        (),
                        {
                            "content": type(
                                "Content",
                                (),
                                {
                                    "parts": [
                                        type(
                                            "Part",
                                            (),
                                            {
                                                "text": None,
                                                "function_call": fc3,
                                            },
                                        )(),
                                    ]
                                },
                            )()
                        },
                    )()
                ],
            },
        )()
        plugin.after_model_callback(callback_context=ctx, llm_response=resp2)
        plugin.after_tool_callback(
            tool=type("Tool", (), {"name": "save_trip_state"})(),
            tool_args={"key": "lodging_and_transit"},
            tool_context=ctx,
        )

        # Turn 3 (e.g. #13): final text turn (no tools called)
        plugin.before_model_callback(callback_context=ctx, llm_request=req)
        resp3 = type(
            "LlmResponse",
            (),
            {
                "usage_metadata": {
                    "prompt_token_count": 2878,
                    "cached_content_token_count": 0,
                    "candidates_token_count": 446,
                    "thoughts_token_count": 14,
                },
                "model_version": "gemini-3.8-flash",
                "candidates": [
                    type(
                        "Candidate",
                        (),
                        {
                            "content": type(
                                "Content",
                                (),
                                {
                                    "parts": [
                                        type(
                                            "Part",
                                            (),
                                            {
                                                "text": "I have completed the flight and accommodation research.",
                                                "function_call": None,
                                            },
                                        )(),
                                    ]
                                },
                            )()
                        },
                    )()
                ],
            },
        )()
        plugin.after_model_callback(callback_context=ctx, llm_response=resp3)

        self.assertEqual(len(tower.tracker.turns_history), 3)
        t1, t2, t3 = tower.tracker.turns_history
        self.assertEqual(
            t1["tools_called"], ["search_flights", "search_hotels"]
        )
        self.assertIn(
            "search_flights(destination='London')", t1["agent_response"]
        )
        self.assertEqual(t2["tools_called"], ["save_trip_state"])
        self.assertEqual(t3["tools_called"], [])

    def test_usage_dedupe_survives_python_id_reuse(self):
        """Regression: freed usage objects whose id() gets recycled must not be dropped as duplicates."""
        import gc

        plugin = ADKTokenomicsPlugin(app_name="id_reuse_bot")
        Usage = type("UsageMetadata", (), {})
        for i in range(50):
            usage = Usage()
            usage.prompt_token_count = 1000 + i
            usage.cached_content_token_count = 0
            usage.candidates_token_count = 10
            usage.thoughts_token_count = 0
            resp = type(
                "LlmResponse",
                (),
                {"usage_metadata": usage, "model_version": "gemini-3.8-flash"},
            )()
            ctx = type(
                "CallbackContext",
                (),
                {
                    "session_id": "sess_ids",
                    "invocation_id": f"inv_{i}",
                    "agent_name": "agent_a",
                },
            )()
            plugin.before_model_callback(callback_context=ctx, llm_request=None)
            plugin.after_model_callback(callback_context=ctx, llm_response=resp)
            del usage, resp
            gc.collect()
        self.assertEqual(len(plugin.tracker.turns_history), 50)

        # The same live usage object seen twice (after_model + on_event) is still recorded only once.
        shared_usage = {"prompt_token_count": 500, "candidates_token_count": 5}
        shared_resp = type(
            "LlmResponse", (), {"usage_metadata": shared_usage}
        )()
        plugin.on_event(event=shared_resp, session_id="sess_ids")
        plugin.on_event(event=shared_resp, session_id="sess_ids")
        self.assertEqual(len(plugin.tracker.turns_history), 51)

    def test_travel_planner_profiler_precision_and_atexit_safety(self):
        """Verifies profiler avoids false positives on intra-invocation tool steps / sub-256 thinking and atexit does not clobber untouched files."""
        import json
        import tempfile

        from adk_tokenomics_plugin.cli_profiler import TokenProfiler

        with tempfile.TemporaryDirectory() as tmp_dir:
            live_file = os.path.join(tmp_dir, "live_adk_session.json")
            with open(live_file, "w") as f:
                json.dump(
                    {
                        "session_id": "existing_sess_99",
                        "turns": [
                            {
                                "session_id": "existing_sess_99",
                                "input_tokens": 100,
                            }
                        ],
                    },
                    f,
                )

            # Instantiating a plugin without recording turns must not rename existing live_adk_session.json on _atexit_finalize_session
            idle_plugin = ADKTokenomicsPlugin(
                app_name="travel_planner_agent", auto_export_path=live_file
            )
            idle_plugin._atexit_finalize_session()
            self.assertTrue(os.path.exists(live_file))
            self.assertFalse(
                os.path.exists(os.path.join(tmp_dir, "existing_sess_99.json"))
            )

        # Profile a 2-invocation travel planner trace where tool steps share invocation_id and tool-call thinking is 124 (<256)
        turns = [
            {
                "session_id": "s_paris",
                "invocation_id": "inv_1",
                "sub_agent": "concierge_router",
                "user_query": "[concierge_router] Hi",
                "input_tokens": 426,
                "cached_tokens": 0,
                "output_tokens": 112,
                "thinking_tokens": 0,
                "total_tokens": 538,
                "model_name": "gemini-3.8-flash",
            },
            {
                "session_id": "s_paris",
                "invocation_id": "inv_2",
                "sub_agent": "flight_hotel_researcher",
                "user_query": "[flight_hotel_researcher] Plan Paris",
                "input_tokens": 1174,
                "cached_tokens": 0,
                "output_tokens": 36,
                "thinking_tokens": 124,
                "total_tokens": 1334,
                "model_name": "gemini-3.8-flash",
            },
            {
                "session_id": "s_paris",
                "invocation_id": "inv_2",
                "sub_agent": "itinerary_architect",
                "user_query": "[itinerary_architect] Plan Paris",
                "input_tokens": 5221,
                "cached_tokens": 0,
                "output_tokens": 1335,
                "thinking_tokens": 0,
                "total_tokens": 6556,
                "model_name": "gemini-3.8-flash",
            },
            {
                "session_id": "s_paris",
                "invocation_id": "inv_2",
                "sub_agent": "itinerary_architect",
                "user_query": "[itinerary_architect] Plan Paris",
                "input_tokens": 6549,
                "cached_tokens": 0,
                "output_tokens": 1349,
                "thinking_tokens": 0,
                "total_tokens": 7898,
                "model_name": "gemini-3.8-flash",
            },
        ]
        profiler = TokenProfiler()
        recs = profiler.profile_session(
            turns, session_title="Travel Planner Precision Test"
        )
        self.assertFalse(any("Thinking Budget Tuning" in r for r in recs))
        self.assertFalse(any("History Compaction" in r for r in recs))
        self.assertTrue(any("Context Caching" in r for r in recs))
        self.assertTrue(any("Modular Skills" in r for r in recs))

    def test_agent_tool_sub_agent_session_linking_and_flow_attribution(self):
        """Verifies AgentTool-wrapped sub-agents with ephemeral child sessions map to parent session_id and 'AgentTool' flow."""
        tower = TokenControlTower(mode="dev")
        plugin = tower.create_adk_plugin(app_name="financial_advisor")

        data_analyst = type(
            "LlmAgent",
            (),
            {"name": "data_analyst_agent", "parent_agent": None, "tools": []},
        )()
        agent_tool = type(
            "AgentTool",
            (),
            {"name": "data_analyst_agent", "agent": data_analyst},
        )()
        coordinator = type(
            "LlmAgent",
            (),
            {
                "name": "financial_coordinator",
                "parent_agent": None,
                "tools": [agent_tool],
                "sub_agents": [],
            },
        )()

        # 1. Parent coordinator turn starts in parent_session_01
        parent_ctx = type(
            "CallbackContext",
            (),
            {
                "agent": coordinator,
                "agent_name": "financial_coordinator",
                "session_id": "parent_session_01",
                "invocation_id": "inv_parent_01",
            },
        )()
        plugin.before_model_callback(
            callback_context=parent_ctx,
            llm_request=type(
                "Req", (), {"model": "gemini-3.8-flash", "contents": []}
            )(),
        )
        parent_usage = type(
            "Usage",
            (),
            {
                "prompt_token_count": 1000,
                "cached_content_token_count": 0,
                "candidates_token_count": 200,
                "thoughts_token_count": 100,
            },
        )()
        plugin.after_model_callback(
            callback_context=parent_ctx,
            llm_response=type(
                "Resp",
                (),
                {
                    "usage_metadata": parent_usage,
                    "model_version": "gemini-3.8-flash",
                    "candidates": [],
                },
            )(),
        )

        # 2. Parent invokes AgentTool(agent=data_analyst_agent), which spins up an ephemeral child session UUID
        plugin.before_tool_callback(
            tool=agent_tool,
            tool_args={"request": "Analyze GOOGL"},
            tool_context=parent_ctx,
        )
        child_ctx = type(
            "CallbackContext",
            (),
            {
                "agent": data_analyst,
                "agent_name": "data_analyst_agent",
                "session_id": "ephemeral_child_uuid_999",
                "invocation_id": "inv_child_999",
            },
        )()
        plugin.before_model_callback(
            callback_context=child_ctx,
            llm_request=type(
                "Req", (), {"model": "gemini-3.8-flash", "contents": []}
            )(),
        )
        child_usage = type(
            "Usage",
            (),
            {
                "prompt_token_count": 1500,
                "cached_content_token_count": 0,
                "candidates_token_count": 800,
                "thoughts_token_count": 300,
            },
        )()
        plugin.after_model_callback(
            callback_context=child_ctx,
            llm_response=type(
                "Resp",
                (),
                {
                    "usage_metadata": child_usage,
                    "model_version": "gemini-3.8-flash",
                    "candidates": [],
                },
            )(),
        )
        plugin.after_tool_callback(
            tool=agent_tool,
            tool_args={"request": "Analyze GOOGL"},
            tool_context=parent_ctx,
            result="Market data report",
        )

        self.assertEqual(len(tower.tracker.turns_history), 2)
        t1, t2 = tower.tracker.turns_history[0], tower.tracker.turns_history[1]
        self.assertEqual(t1["session_id"], "parent_session_01")
        self.assertEqual(t1["raw_json"]["orchestration_flow"], "Root")
        self.assertEqual(t2["session_id"], "parent_session_01")
        self.assertEqual(t2["invocation_id"], "inv_parent_01")
        self.assertEqual(t2["raw_json"]["sub_agent"], "data_analyst_agent")
        self.assertEqual(t2["raw_json"]["orchestration_flow"], "AgentTool")

    def test_get_rate_versioned_ids_and_vertex_endpoints_use_configured_rates(
        self,
    ):
        from adk_tokenomics_plugin.core.pricing import normalize_turn_tokens

        engine = PricingEngine()
        self.assertEqual(
            engine.get_rate("gpt-5-2025-08-07")["input_per_million"], 1.25
        )
        self.assertEqual(
            engine.get_rate("gpt-5-2025-08-07")["cached_per_million"], 0.125
        )
        self.assertEqual(
            engine.get_rate("openai/gpt-5-2025-08-07")["input_per_million"],
            1.25,
        )
        self.assertEqual(
            engine.get_rate("claude-3-7-sonnet@20250219")["input_per_million"],
            3.00,
        )
        self.assertEqual(
            engine.get_rate("claude-opus-4@20250514")["input_per_million"],
            15.00,
        )
        self.assertEqual(
            engine.get_rate("gemini-4-argon-001")["input_per_million"], 2.00
        )
        self.assertEqual(
            engine.get_rate(
                "projects/p/locations/us-central1/publishers/google/models/gemini-3.8-flash"
            )["input_per_million"],
            0.75,
        )

        # Vertex deployed endpoint path containing 'projects/' must not false-match 'pro'
        endpoint_rate = engine.get_rate(
            "projects/p/locations/us-central1/endpoints/1234567890"
        )
        self.assertNotEqual(endpoint_rate["name"], "Gemini 4 Argon")
        self.assertEqual(endpoint_rate["input_per_million"], 0.75)

        # OpenAI o4-mini resolves to OpenAI Mini instead of falling through to Google Flash
        o4_rate = engine.get_rate("o4-mini")
        self.assertEqual(o4_rate["provider"], "openai")
        self.assertEqual(o4_rate["input_per_million"], 0.15)

        # Registered custom model matches versioned suffixes via boundary prefix match
        engine.register_model(
            "acme-llm",
            input_per_million=10.0,
            output_per_million=40.0,
            provider="custom",
        )
        self.assertEqual(
            engine.get_rate("acme-llm-v2")["input_per_million"], 10.0
        )
        self.assertEqual(
            engine.get_rate("acme-llm@20260101")["output_per_million"], 40.0
        )

        # Registering prefixed custom/gemini-3.8-flash must not hijack canonical short key gemini-3.8-flash
        engine.register_model(
            "custom/gemini-3.8-flash",
            input_per_million=0.01,
            output_per_million=0.02,
            provider="custom",
        )
        self.assertEqual(
            engine.get_rate("gemini-3.8-flash")["input_per_million"], 0.75
        )
        self.assertEqual(
            engine.get_rate("custom/gemini-3.8-flash")["input_per_million"],
            0.01,
        )

        # Negative and None token counts clamp safely to >= 0
        neg_cost = engine.calculate_turn_cost(
            "gemini-3.8-flash", -500, -100, 1000, None
        )  # type: ignore[arg-type]
        self.assertEqual(neg_cost["input_cost"], 0.0)
        self.assertEqual(neg_cost["cached_cost"], 0.0)
        self.assertAlmostEqual(neg_cost["total_cost"], 0.00375, places=8)

        # Seam contract C2: normalize_turn_tokens
        norm_sdk = normalize_turn_tokens(
            {
                "input_tokens": 1200,
                "cached_tokens": 800,
                "total_tokens": 2100,
                "output_tokens": 100,
            }
        )
        self.assertEqual(
            norm_sdk,
            {
                "fresh_tokens": 1200,
                "cached_tokens": 800,
                "prompt_tokens": 2000,
                "output_tokens": 100,
                "thinking_tokens": 0,
            },
        )
        norm_bq = normalize_turn_tokens(
            {
                "prompt_tokens": 2000,
                "cached_tokens": 800,
                "completion_tokens": 150,
                "thinking_tokens": 50,
            }
        )
        self.assertEqual(
            norm_bq,
            {
                "fresh_tokens": 1200,
                "cached_tokens": 800,
                "prompt_tokens": 2000,
                "output_tokens": 150,
                "thinking_tokens": 50,
            },
        )

    def test_get_cheapest_model_by_provider_and_unknown_provider_fallback(self):
        engine = PricingEngine()
        self.assertEqual(
            engine.get_cheapest_model("google")["short_id"], "gemini-3.8-flash"
        )
        self.assertEqual(
            engine.get_cheapest_model("google")["input_per_million"], 0.75
        )
        self.assertEqual(
            engine.get_cheapest_model("anthropic")["short_id"],
            "claude-3-5-haiku",
        )
        self.assertEqual(
            engine.get_cheapest_model("openai")["short_id"], "gpt-4o-mini"
        )

        # Open-weight & global cheapest skip $0 self-hosted entries by default (include_self_hosted=False)
        cheapest_ow = engine.get_cheapest_model("open_weight")
        self.assertNotEqual(cheapest_ow["id"], "ollama/local-open-weight")
        self.assertEqual(cheapest_ow["id"], "meta/llama-3.1-8b-instruct")
        self.assertEqual(cheapest_ow["input_per_million"], 0.05)

        cheapest_any = engine.get_cheapest_model()
        self.assertNotEqual(cheapest_any["id"], "ollama/local-open-weight")
        self.assertEqual(cheapest_any["id"], "meta/llama-3.1-8b-instruct")

        cheapest_with_local = engine.get_cheapest_model(
            "open_weight", include_self_hosted=True
        )
        self.assertEqual(cheapest_with_local["id"], "ollama/local-open-weight")
        self.assertEqual(cheapest_with_local["input_per_million"], 0.0)

        # Unknown explicit provider must fall back to default_model rather than $0 ollama
        unknown = engine.get_cheapest_model("nonexistent_cloud_vendor")
        self.assertNotEqual(unknown["id"], "ollama/local-open-weight")
        self.assertEqual(unknown["short_id"], "gemini-3.8-flash")
        self.assertEqual(unknown["input_per_million"], 0.75)

    def test_prod_mode_honors_explicit_memory_sink(self):
        import tempfile
        from unittest.mock import patch

        from adk_tokenomics_plugin.core.config import (
            resolve_models_config_path,
            resolve_writable_models_config_path,
        )

        tower = TokenControlTower(mode="prod", sink="memory")
        self.assertEqual(tower.config.mode, "prod")
        self.assertEqual(tower.config.sink, "memory")
        self.assertIsInstance(tower.sink, InMemorySink)

        # Explicit constructor args must NOT be overridden by TOKENOMICS_MODE / TOKENOMICS_SINK env vars
        with patch.dict(
            os.environ,
            {"TOKENOMICS_MODE": "prod", "TOKENOMICS_SINK": "bigquery"},
            clear=False,
        ):
            explicit_tower = TokenControlTower(mode="dev", sink="memory")
            self.assertEqual(explicit_tower.config.mode, "dev")
            self.assertEqual(explicit_tower.config.sink, "memory")
            self.assertIsInstance(explicit_tower.sink, InMemorySink)

            # Unset defaults DO honor env vars
            env_cfg = TokenomicsConfig()
            self.assertEqual(env_cfg.mode, "prod")
            self.assertEqual(env_cfg.sink, "bigquery")

        # Invalid or missing prompts_config_path raises ValueError
        with self.assertRaises(ValueError):
            TokenomicsConfig(
                prompts_config_path="/nonexistent/path/prompts.json"
            )

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False
        ) as tf:
            tf.write('["not", "a", "dict"]')
            bad_prompts_path = tf.name
        try:
            with self.assertRaises(ValueError):
                TokenomicsConfig(prompts_config_path=bad_prompts_path)
        finally:
            os.remove(bad_prompts_path)

        # Seam contract C1: writable models config never points to packaged models_config.json
        with tempfile.TemporaryDirectory() as tmp_xdg:
            with patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": tmp_xdg,
                    "TOKENOMICS_MODELS_CONFIG_PATH": "",
                },
                clear=False,
            ):
                writable = resolve_writable_models_config_path()
                pkg_cfg = os.path.abspath(
                    os.path.join(
                        os.path.dirname(
                            os.path.dirname(os.path.abspath(__file__))
                        ),
                        "adk_tokenomics_plugin",
                        "models_config.json",
                    )
                )
                self.assertNotEqual(os.path.abspath(writable), pkg_cfg)
                self.assertTrue(writable.startswith(tmp_xdg))
                with open(writable, "w", encoding="utf-8") as wf:
                    wf.write('{"models": []}')
                self.assertEqual(
                    os.path.abspath(resolve_models_config_path()),
                    os.path.abspath(writable),
                )

    def test_track_tokens_async_function_records_usage(self):
        import asyncio

        tr = TokenTracker(sink=InMemorySink())

        @track_tokens(
            app_name="async_rag", model_name="gemini-3.8-flash", tracker=tr
        )
        async def ask(prompt: str):
            await asyncio.sleep(0.01)
            return (
                "async answer",
                {
                    "input_tokens": 1000,
                    "cached_tokens": 250,
                    "output_tokens": 200,
                    "thinking_tokens": 50,
                },
            )

        res = asyncio.run(ask("hi"))
        self.assertEqual(res[0], "async answer")
        self.assertEqual(len(tr.turns_history), 1)
        rec = tr.turns_history[-1]
        self.assertEqual(rec["input_tokens"], 1000)
        self.assertEqual(rec["cached_tokens"], 250)
        self.assertEqual(rec["output_tokens"], 200)
        self.assertEqual(rec["thinking_tokens"], 50)
        self.assertEqual(rec["agent_response"], "async answer")
        self.assertGreaterEqual(rec["latency_ms"], 1)

        # Exceptions raised by wrapped async/sync functions propagate cleanly without recording bogus turns
        @track_tokens(tracker=tr)
        async def fail_async(prompt: str):
            raise RuntimeError("upstream model timeout")

        with self.assertRaises(RuntimeError):
            asyncio.run(fail_async("boom"))
        self.assertEqual(len(tr.turns_history), 1)

    def test_track_tokens_telemetry_failure_returns_result(self):
        import asyncio

        tr2 = TokenTracker(sink=InMemorySink())

        @track_tokens(tracker=tr2)
        def ask2(prompt: str):
            return ("real answer", {"input_tokens": None, "output_tokens": 10})

        out = ask2("q")
        self.assertEqual(out[0], "real answer")
        self.assertEqual(len(tr2.turns_history), 1)
        self.assertEqual(tr2.turns_history[-1]["input_tokens"], 0)
        self.assertEqual(tr2.turns_history[-1]["output_tokens"], 10)

        # Even if tracker.record_turn raises unexpectedly, host call return value is never lost
        class BrokenTracker:
            def record_turn(self, **kwargs):
                raise RuntimeError("sink connection broken")

        @track_tokens(tracker=BrokenTracker())  # type: ignore[arg-type]
        def ask_sync_broken(prompt: str):
            return "sync_survived"

        @track_tokens(tracker=BrokenTracker())  # type: ignore[arg-type]
        async def ask_async_broken(prompt: str):
            return "async_survived"

        self.assertEqual(ask_sync_broken("q"), "sync_survived")
        self.assertEqual(asyncio.run(ask_async_broken("q")), "async_survived")

    def test_bigquery_sink_retries_transient_insert_failure(self):
        from adk_tokenomics_plugin.core.sinks import BigQuerySink

        class FakeBQClient:
            def __init__(self, fail_times: int = 1):
                self.fail_times = fail_times
                self.calls = 0
                self.batch_sizes: list[int] = []
                self.rows: list[Any] = []

            def insert_rows_json(self, table: str, rows):
                self.calls += 1
                self.batch_sizes.append(len(rows))
                if self.calls <= self.fail_times:
                    raise RuntimeError("503 Service Unavailable")
                self.rows.extend(rows)
                return []

        cfg = TokenomicsConfig(
            mode="prod",
            sink="bigquery",
            project_id="my-test-project",
            flush_interval_seconds=0.02,
            batch_size=5,
        )
        sink = BigQuerySink(cfg, max_retries=3, retry_base_delay=0.002)
        fake = FakeBQClient(fail_times=1)
        sink._client = fake

        for i in range(12):
            sink.write_turn(
                {
                    "session_id": "s_bq",
                    "input_tokens": 100 + i,
                    "cached_tokens": 0,
                    "output_tokens": 10,
                    "thinking_tokens": 0,
                    "model_name": "gemini-3.8-flash",
                    "latency_ms": 5,
                }
            )
        sink.close()
        self.assertEqual(len(fake.rows), 12)
        self.assertEqual(sink.dropped_rows_count, 0)
        self.assertGreaterEqual(fake.calls, 2)

        # Partial row-level errors retry ONLY failed row indices without duplicating succeeded rows
        class PartialErrorBQClient:
            def __init__(self):
                self.calls = 0
                self.persisted = []

            def insert_rows_json(self, table: str, rows):
                self.calls += 1
                if self.calls == 1:
                    self.persisted.extend([rows[0], rows[2]])
                    return [{"index": 1, "errors": [{"reason": "timeout"}]}]
                self.persisted.extend(rows)
                return []

        sink_partial = BigQuerySink(
            TokenomicsConfig(
                mode="prod",
                sink="bigquery",
                project_id="my-test-project",
                flush_interval_seconds=60.0,
            ),
            max_retries=2,
            retry_base_delay=0.001,
        )
        partial_client = PartialErrorBQClient()
        sink_partial._client = partial_client
        for i in range(3):
            sink_partial.write_turn(
                {
                    "session_id": f"row_{i}",
                    "input_tokens": 10,
                    "output_tokens": 5,
                }
            )
        sink_partial.close()
        self.assertEqual(len(partial_client.persisted), 3)
        self.assertEqual(
            {r["session_id"] for r in partial_client.persisted},
            {"row_0", "row_1", "row_2"},
        )
        self.assertEqual(sink_partial.dropped_rows_count, 0)

        # Large shutdown drain (>500 rows) chunks to <=500 rows per insert_rows_json call
        sink_chunk = BigQuerySink(
            TokenomicsConfig(
                mode="prod",
                sink="bigquery",
                project_id="my-test-project",
                flush_interval_seconds=60.0,
            ),
            max_retries=1,
            retry_base_delay=0.001,
        )
        chunk_client = FakeBQClient(fail_times=0)
        sink_chunk._client = chunk_client
        for _i in range(1100):
            sink_chunk.write_turn(
                {"session_id": "bulk", "input_tokens": 1, "output_tokens": 1}
            )
        sink_chunk.close()
        self.assertEqual(len(chunk_client.rows), 1100)
        self.assertLessEqual(max(chunk_client.batch_sizes), 500)

        # Invalid BigQuery identifiers fail closed with ValueError
        with self.assertRaises(ValueError):
            BigQuerySink(
                TokenomicsConfig(
                    mode="prod", sink="bigquery", project_id="bad; DROP TABLE x"
                )
            )
        with self.assertRaises(ValueError):
            BigQuerySink(
                TokenomicsConfig(
                    mode="prod",
                    sink="bigquery",
                    project_id="ok-proj",
                    dataset_id="bad-ds-dash",
                )
            )

    def test_parallel_after_tool_skill_attributed_to_calling_agent(self):
        tower = TokenControlTower(mode="dev")
        plugin = tower.create_adk_plugin(app_name="parallel_skills_app")

        ctx_a = type(
            "CallbackContext",
            (),
            {
                "session_id": "s_par",
                "invocation_id": "inv_shared",
                "agent_name": "agent_a",
            },
        )()
        ctx_b = type(
            "CallbackContext",
            (),
            {
                "session_id": "s_par",
                "invocation_id": "inv_shared",
                "agent_name": "agent_b",
            },
        )()
        req = type(
            "LlmRequest",
            (),
            {
                "model": "gemini-3.8-flash",
                "contents": [
                    type(
                        "Content",
                        (),
                        {
                            "role": "user",
                            "parts": [type("Part", (), {"text": "Research"})()],
                        },
                    )()
                ],
            },
        )()

        plugin.before_model_callback(callback_context=ctx_a, llm_request=req)
        plugin.before_model_callback(callback_context=ctx_b, llm_request=req)

        # Agent A emits function_call load_skill(skill="arxiv"), then Agent B finishes plain text BEFORE Agent A's tool finishes
        fc_a = type(
            "FunctionCall",
            (),
            {"name": "load_skill", "args": {"skill": "arxiv"}},
        )()
        resp_a = type(
            "LlmResponse",
            (),
            {
                "usage_metadata": {
                    "prompt_token_count": 1000,
                    "cached_content_token_count": 0,
                    "candidates_token_count": 50,
                },
                "model_version": "gemini-3.8-flash",
                "candidates": [
                    type(
                        "Cand",
                        (),
                        {
                            "content": type(
                                "Ct",
                                (),
                                {
                                    "parts": [
                                        type(
                                            "P",
                                            (),
                                            {
                                                "text": None,
                                                "function_call": fc_a,
                                            },
                                        )()
                                    ]
                                },
                            )()
                        },
                    )()
                ],
            },
        )()
        resp_b = type(
            "LlmResponse",
            (),
            {
                "usage_metadata": {
                    "prompt_token_count": 2000,
                    "cached_content_token_count": 2500,
                    "candidates_token_count": 80,
                },
                "model_version": "gemini-3.8-flash",
                "candidates": [
                    type(
                        "Cand",
                        (),
                        {
                            "content": type(
                                "Ct",
                                (),
                                {
                                    "parts": [
                                        type(
                                            "P",
                                            (),
                                            {
                                                "text": "Done B",
                                                "function_call": None,
                                            },
                                        )()
                                    ]
                                },
                            )()
                        },
                    )()
                ],
            },
        )()

        plugin.after_model_callback(callback_context=ctx_a, llm_response=resp_a)
        plugin.after_model_callback(callback_context=ctx_b, llm_response=resp_b)
        plugin.after_tool_callback(
            tool=type("Tool", (), {"name": "load_skill"})(),
            args={"skill": "arxiv"},
            tool_context=ctx_a,
            tool_response="Skill loaded",
        )

        turn_a, turn_b = (
            tower.tracker.turns_history[0],
            tower.tracker.turns_history[1],
        )
        self.assertIn("arxiv", turn_a["skills_active"])
        self.assertNotIn("arxiv", turn_b["skills_active"])
        # Verify cached_content_token_count > prompt_token_count on resp_b was clamped to prompt_token_count (2000)
        self.assertEqual(turn_b["cached_tokens"], 2000)
        self.assertEqual(turn_b["input_tokens"], 0)

        # Verify double callback registration (Runner + attach_to_agent) does not leak tool onto subsequent turn
        plugin.before_model_callback(callback_context=ctx_a, llm_request=req)
        fc_dup = type(
            "FunctionCall", (), {"name": "web_search", "args": {"q": "adk"}}
        )()
        resp_dup = type(
            "LlmResponse",
            (),
            {
                "usage_metadata": {
                    "prompt_token_count": 900,
                    "cached_content_token_count": 0,
                    "candidates_token_count": 30,
                },
                "model_version": "gemini-3.8-flash",
                "candidates": [
                    type(
                        "Cand",
                        (),
                        {
                            "content": type(
                                "Ct",
                                (),
                                {
                                    "parts": [
                                        type(
                                            "P",
                                            (),
                                            {
                                                "text": None,
                                                "function_call": fc_dup,
                                            },
                                        )()
                                    ]
                                },
                            )()
                        },
                    )()
                ],
            },
        )()
        plugin.after_model_callback(
            callback_context=ctx_a, llm_response=resp_dup
        )
        plugin.after_model_callback(
            callback_context=ctx_a, llm_response=resp_dup
        )
        search_tool = type("Tool", (), {"name": "web_search"})()
        plugin.after_tool_callback(
            tool=search_tool,
            tool_args={"q": "adk"},
            tool_context=ctx_a,
            result="ok",
        )
        plugin.after_tool_callback(
            tool=search_tool,
            args={"q": "adk"},
            tool_context=ctx_a,
            tool_response="ok",
        )

        plugin.before_model_callback(callback_context=ctx_a, llm_request=req)
        resp_followup = type(
            "LlmResponse",
            (),
            {
                "usage_metadata": {
                    "prompt_token_count": 1100,
                    "cached_content_token_count": 0,
                    "candidates_token_count": 40,
                },
                "model_version": "gemini-3.8-flash",
                "candidates": [],
            },
        )()
        plugin.after_model_callback(
            callback_context=ctx_a, llm_response=resp_followup
        )
        self.assertEqual(
            tower.tracker.turns_history[-2]["tools_called"], ["web_search"]
        )
        self.assertEqual(tower.tracker.turns_history[-1]["tools_called"], [])

    def test_resumed_session_across_plugin_instances_preserves_turns(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Use a custom non-default live buffer filename to also verify arbitrary auto_export_path finalization
            live_path = os.path.join(tmp_dir, "custom_live_trace.json")
            req = type(
                "Req", (), {"model": "gemini-3.8-flash", "contents": []}
            )()

            # Process / Instance 1 writes 3 turns for sess_X and finalizes on exit
            p1 = TokenControlTower(mode="dev").create_adk_plugin(
                app_name="app", auto_export_path=live_path
            )
            for i in range(3):
                c = type(
                    "Ctx",
                    (),
                    {
                        "session_id": "sess_X",
                        "invocation_id": f"inv_{i}",
                        "agent_name": "root",
                    },
                )()
                p1.before_model_callback(callback_context=c, llm_request=req)
                resp = type(
                    "Resp",
                    (),
                    {
                        "usage_metadata": {
                            "prompt_token_count": 1000 + i,
                            "cached_content_token_count": 0,
                            "candidates_token_count": 10,
                        },
                        "model_version": "gemini-3.8-flash",
                        "candidates": [],
                    },
                )()
                p1.after_model_callback(callback_context=c, llm_response=resp)
            p1._atexit_finalize_session()

            archive_x = os.path.join(tmp_dir, "sess_X.json")
            self.assertTrue(os.path.exists(archive_x))
            with open(archive_x, encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)["turns"]), 3)

            # Process / Instance 2 resumes sess_X, adds 1 turn, and finalizes
            p2 = TokenControlTower(mode="dev").create_adk_plugin(
                app_name="app", auto_export_path=live_path
            )
            c2 = type(
                "Ctx",
                (),
                {
                    "session_id": "sess_X",
                    "invocation_id": "inv_9",
                    "agent_name": "root",
                },
            )()
            p2.before_model_callback(callback_context=c2, llm_request=req)
            resp2 = type(
                "Resp",
                (),
                {
                    "usage_metadata": {
                        "prompt_token_count": 5000,
                        "cached_content_token_count": 0,
                        "candidates_token_count": 20,
                    },
                    "model_version": "gemini-3.8-flash",
                    "candidates": [],
                },
            )()
            p2.after_model_callback(callback_context=c2, llm_response=resp2)
            p2._atexit_finalize_session()

            with open(archive_x, encoding="utf-8") as f:
                merged_data = json.load(f)
            self.assertEqual(len(merged_data["turns"]), 4)
            self.assertEqual(merged_data["summary"]["total_turns"], 4)
            self.assertEqual(
                [t["invocation_id"] for t in merged_data["turns"]],
                ["inv_0", "inv_1", "inv_2", "inv_9"],
            )

    def test_export_traces_atomic_preserves_previous_file_on_failure(self):
        import json
        import tempfile
        import threading

        with tempfile.TemporaryDirectory() as tmp_dir:
            trace_path = os.path.join(tmp_dir, "atomic_trace.json")
            tracker = TokenTracker(sink=InMemorySink())
            tracker.record_turn(
                session_id="s_atomic",
                app_name="app",
                model_name="gemini-3.8-flash",
                user_query="Turn 1",
                agent_response="OK",
                input_tokens=500,
                cached_tokens=0,
                output_tokens=50,
            )
            tracker.export_traces(trace_path)

            # Record a turn with non-JSON-serializable raw_json and verify failed export leaves previous file intact
            tracker.record_turn(
                session_id="s_atomic",
                app_name="app",
                model_name="gemini-3.8-flash",
                user_query="Turn 2",
                agent_response="Bad",
                input_tokens=500,
                cached_tokens=0,
                output_tokens=50,
                raw_json={"unserializable": object()},
            )
            with self.assertRaises(TypeError):
                tracker.export_traces(trace_path)

            with open(trace_path, encoding="utf-8") as f:
                preserved = json.load(f)
            self.assertEqual(len(preserved["turns"]), 1)
            self.assertEqual(preserved["turns"][0]["user_query"], "Turn 1")
            # Ensure temp file was cleaned up
            self.assertEqual(os.listdir(tmp_dir), ["atomic_trace.json"])

            # Multi-threaded reader/writer stress test: zero partial/corrupt JSON reads
            live_concurrent = os.path.join(tmp_dir, "concurrent_live.json")
            tower = TokenControlTower(mode="dev")
            plugin = tower.create_adk_plugin(
                app_name="conc_app", auto_export_path=live_concurrent
            )
            decode_errors = []
            stop_evt = threading.Event()

            def _reader():
                while not stop_evt.is_set():
                    try:
                        with open(live_concurrent, encoding="utf-8") as rf:
                            json.load(rf)
                    except FileNotFoundError:
                        pass
                    except json.JSONDecodeError as err:
                        decode_errors.append(err)

            def _writer(worker_id: int):
                for j in range(25):
                    ev = type(
                        "Ev",
                        (),
                        {
                            "usage_metadata": {
                                "prompt_token_count": 100 + j,
                                "candidates_token_count": 10,
                            },
                            "model_version": "gemini-3.8-flash",
                        },
                    )()
                    plugin.on_event(
                        ev,
                        session_id="sess_conc",
                        invocation_id=f"inv_{worker_id}_{j}",
                        sub_agent_name=f"ag_{worker_id}",
                    )

            reader_thread = threading.Thread(target=_reader)
            reader_thread.start()
            writers = [
                threading.Thread(target=_writer, args=(w,)) for w in range(4)
            ]
            for w in writers:
                w.start()
            for w in writers:
                w.join()
            stop_evt.set()
            reader_thread.join()

            self.assertEqual(decode_errors, [])
            with open(live_concurrent, encoding="utf-8") as rf:
                final_payload = json.load(rf)
            self.assertEqual(len(final_payload["turns"]), 100)

    def test_import_does_not_shadow_google_cloud_namespace(self):
        import subprocess
        import tempfile
        import textwrap

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as fake_site:
            gcs_dir = os.path.join(fake_site, "google", "cloud", "storage")
            os.makedirs(gcs_dir, exist_ok=True)
            with open(
                os.path.join(fake_site, "google", "cloud", "exceptions.py"),
                "w",
                encoding="utf-8",
            ) as ef:
                ef.write(
                    textwrap.dedent("""\
                    class GoogleCloudError(Exception):
                        pass
                    class NotFound(GoogleCloudError):
                        pass
                    class Forbidden(GoogleCloudError):
                        pass
                """)
                )
            with open(
                os.path.join(gcs_dir, "__init__.py"), "w", encoding="utf-8"
            ) as sf:
                sf.write(
                    "from google.cloud.exceptions import Forbidden, NotFound\nSTORAGE_OK = True\n"
                )

            code = textwrap.dedent(f"""\
                import sys
                sys.path.insert(0, {fake_site!r})
                sys.path.insert(1, {repo_root!r})
                import adk_tokenomics_plugin
                assert "google.cloud.exceptions" not in sys.modules or hasattr(sys.modules["google.cloud.exceptions"], "Forbidden")
                from google.cloud.exceptions import Forbidden
                import google.cloud.storage as gcs
                assert gcs.STORAGE_OK is True
            """)
            proc = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_otel_cost_and_cache_savings_histogram_emission(self):
        import types
        from unittest.mock import patch

        recorded = []

        class FakeHistogram:
            def record(self, value, attributes=None):
                recorded.append((value, dict(attributes or {})))

        class FakeMeter:
            def create_histogram(self, name, unit="", description=""):
                recorded.append(("CREATE", name, unit))
                return FakeHistogram()

        fake_otel = types.ModuleType("opentelemetry")
        fake_metrics = types.ModuleType("opentelemetry.metrics")
        fake_metrics.get_meter = lambda name: FakeMeter()  # type: ignore[attr-defined]
        fake_otel.metrics = fake_metrics  # type: ignore[attr-defined]

        with patch.dict(
            sys.modules,
            {"opentelemetry": fake_otel, "opentelemetry.metrics": fake_metrics},
        ):
            tracker = TokenTracker(sink=InMemorySink())
            tracker.record_turn(
                session_id="s_otel",
                app_name="finops_app",
                model_name="publishers/google/models/gemini-3.8-flash",
                user_query="Analyze spend",
                agent_response="Summary",
                input_tokens=10000,
                cached_tokens=40000,
                output_tokens=2000,
                thinking_tokens=1000,
                raw_json={"sub_agent": "cost_router"},
            )

        self.assertEqual(
            recorded[0], ("CREATE", "gen_ai.client.cost.usd", "USD")
        )
        self.assertEqual(len(recorded), 3)
        actual_val, actual_attrs = recorded[1]
        savings_val, savings_attrs = recorded[2]
        self.assertAlmostEqual(actual_val, 0.02175, places=8)
        self.assertEqual(actual_attrs["gen_ai.cost.type"], "actual")
        self.assertEqual(actual_attrs["gen_ai.agent.name"], "cost_router")
        self.assertEqual(
            actual_attrs["gen_ai.response.model"],
            "publishers/google/models/gemini-3.8-flash",
        )
        self.assertAlmostEqual(savings_val, 0.0270, places=8)
        self.assertEqual(savings_attrs["gen_ai.cost.type"], "cache_savings")

    def test_p2_1_tier_token_guard_on_prefix_matching(self):
        engine = PricingEngine()

        # Unregistered tier variants (gpt-5-mini, gpt-5-nano) skip parent 'gpt-5' and hit OpenAI Mini heuristic ($0.15/$0.60)
        for mini_id in (
            "gpt-5-mini",
            "gpt-5-nano",
            "openai/gpt-5-mini-2025-08-07",
        ):
            rate = engine.get_rate(mini_id)
            self.assertEqual(rate["provider"], "openai")
            self.assertAlmostEqual(rate["input_per_million"], 0.15, places=6)
            self.assertAlmostEqual(rate["cached_per_million"], 0.075, places=6)
            self.assertAlmostEqual(rate["output_per_million"], 0.60, places=6)

        # Version / date suffixes still match configured 'gpt-5' ($1.25 / $10.00)
        gpt5_dated = engine.get_rate("gpt-5-2025-08-07")
        self.assertAlmostEqual(gpt5_dated["input_per_million"], 1.25, places=6)
        self.assertAlmostEqual(
            gpt5_dated["cached_per_million"], 0.125, places=6
        )
        self.assertAlmostEqual(
            gpt5_dated["output_per_million"], 10.00, places=6
        )

        # Custom prefix matches version suffix ('v2'), while explicit longer tier registration wins when registered
        engine.register_model(
            "acme-llm",
            input_per_million=4.0,
            output_per_million=12.0,
            provider="custom",
        )
        self.assertAlmostEqual(
            engine.get_rate("acme-llm-v2")["input_per_million"], 4.0, places=6
        )

        engine.register_model(
            "gpt-5-mini",
            input_per_million=0.25,
            output_per_million=2.00,
            cached_per_million=0.025,
            provider="openai",
        )
        self.assertAlmostEqual(
            engine.get_rate("gpt-5-mini-2025-09-01")["input_per_million"],
            0.25,
            places=6,
        )

        # Google cheapest tier remains gemini-3.8-flash ($0.75)
        self.assertEqual(
            engine.get_cheapest_model("google")["short_id"], "gemini-3.8-flash"
        )

    def test_p2_2_resumed_session_archive_buffer_isolates_live_totals(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            live_path = os.path.join(tmp_dir, "live_adk_session.json")
            req = type(
                "Req", (), {"model": "gemini-3.8-flash", "contents": []}
            )()

            # Process 1 records 3 turns for sess_p2_resume and finalizes
            tower1 = TokenControlTower(mode="dev")
            p1 = tower1.create_adk_plugin(
                app_name="resume_app", auto_export_path=live_path
            )
            for i in range(3):
                ctx1 = type(
                    "Ctx",
                    (),
                    {
                        "session_id": "sess_p2_resume",
                        "invocation_id": f"inv_p1_{i}",
                        "agent_name": "router",
                    },
                )()
                p1.before_model_callback(callback_context=ctx1, llm_request=req)
                resp1 = type(
                    "Resp",
                    (),
                    {
                        "usage_metadata": {
                            "prompt_token_count": 1000,
                            "cached_content_token_count": 200,
                            "candidates_token_count": 100,
                            "thoughts_token_count": 50,
                        },
                        "model_version": "gemini-3.8-flash",
                        "candidates": [],
                    },
                )()
                p1.after_model_callback(
                    callback_context=ctx1, llm_response=resp1
                )
            p1.finalize_session()

            # Process 2 resumes sess_p2_resume and records 1 live turn
            tower2 = TokenControlTower(mode="dev")
            p2 = tower2.create_adk_plugin(
                app_name="resume_app", auto_export_path=live_path
            )
            ctx2 = type(
                "Ctx",
                (),
                {
                    "session_id": "sess_p2_resume",
                    "invocation_id": "inv_p2_0",
                    "agent_name": "router",
                },
            )()
            p2.before_model_callback(callback_context=ctx2, llm_request=req)
            resp2 = type(
                "Resp",
                (),
                {
                    "usage_metadata": {
                        "prompt_token_count": 2000,
                        "cached_content_token_count": 500,
                        "candidates_token_count": 300,
                        "thoughts_token_count": 100,
                    },
                    "model_version": "gemini-3.8-flash",
                    "candidates": [],
                },
            )()
            p2.after_model_callback(callback_context=ctx2, llm_response=resp2)

            # In-process totals in proc2 reflect ONLY proc2's 1 live turn
            live_summary = tower2.get_summary()
            self.assertEqual(live_summary["total_turns"], 1)
            self.assertEqual(
                tower2.tracker.get_session_summary()["total_turns"], 1
            )
            self.assertEqual(len(tower2.tracker.turns_history), 1)
            self.assertEqual(live_summary["input_tokens"], 1500)
            self.assertEqual(live_summary["cached_tokens"], 500)
            self.assertEqual(live_summary["output_tokens"], 300)
            self.assertEqual(live_summary["thinking_tokens"], 100)

            # Meanwhile exported live_adk_session.json already merges all 4 turns (3 historical + 1 live)
            with open(live_path, encoding="utf-8") as lf:
                live_exported = json.load(lf)
            self.assertEqual(len(live_exported["turns"]), 4)
            self.assertEqual(live_exported["summary"]["total_turns"], 4)
            self.assertEqual(
                live_exported["summary"]["input_tokens"], 3 * 800 + 1500
            )
            self.assertEqual(
                live_exported["summary"]["cached_tokens"], 3 * 200 + 500
            )
            self.assertEqual(
                live_exported["summary"]["output_tokens"], 3 * 100 + 300
            )
            self.assertEqual(
                live_exported["summary"]["thinking_tokens"], 3 * 50 + 100
            )

            # Finalize proc2 and verify archived <session_id>.json also has all 4 merged turns + accurate summary
            archive_path = p2.finalize_session()
            self.assertIsNotNone(archive_path)
            with open(str(archive_path), encoding="utf-8") as af:
                archived = json.load(af)
            self.assertEqual(len(archived["turns"]), 4)
            self.assertEqual(archived["summary"]["total_turns"], 4)
            self.assertAlmostEqual(
                archived["summary"]["total_cost"],
                sum(t["total_cost"] for t in archived["turns"]),
                places=8,
            )

    def test_p2_3_fast_single_write_export_and_cached_live_session_id(self):
        import json
        import tempfile
        import time

        with tempfile.TemporaryDirectory() as tmp_dir:
            export_file = os.path.join(tmp_dir, "perf_150_turns.json")
            tracker = TokenTracker(sink=InMemorySink())
            for i in range(150):
                tracker.record_turn(
                    session_id="sess_perf",
                    app_name="perf_app",
                    model_name="publishers/google/models/gemini-3.8-flash",
                    user_query=f"Prompt number {i} with realistic payload text",
                    agent_response=f"Response number {i} with synthesized reasoning summary",
                    input_tokens=1200 + i,
                    cached_tokens=800,
                    output_tokens=250,
                    thinking_tokens=64,
                    tools_called=["search_docs"],
                    skills_active=["finops-skill"],
                    latency_ms=42,
                    invocation_id=f"inv_perf_{i}",
                )

            t0 = time.perf_counter()
            tracker.export_traces(export_file)
            elapsed = time.perf_counter() - t0
            self.assertLess(
                elapsed,
                0.5,
                f"Expected export_traces(150 turns) < 0.5s, took {elapsed:.3f}s",
            )

            with open(export_file, encoding="utf-8") as f:
                loaded = json.load(f)
            self.assertEqual(len(loaded["turns"]), 150)
            self.assertEqual(loaded["summary"]["total_turns"], 150)

            # Verify ADKTokenomicsPlugin caches _last_exported_file_session_id so turns 2..N do zero disk re-reads
            live_file = os.path.join(tmp_dir, "live_cached_sid.json")
            with open(live_file, "w", encoding="utf-8") as f:
                json.dump({"session_id": "sess_cached_1", "turns": []}, f)

            tower = TokenControlTower(mode="dev")
            plugin = tower.create_adk_plugin(
                app_name="cached_sid_app", auto_export_path=live_file
            )
            read_calls = []
            orig_read = plugin._read_trace_file_session_id

            def _counting_read(fp: str):
                read_calls.append(fp)
                return orig_read(fp)

            plugin._read_trace_file_session_id = _counting_read  # type: ignore[method-assign]
            for i in range(15):
                ev = type(
                    "Ev",
                    (),
                    {
                        "usage_metadata": {
                            "prompt_token_count": 500 + i,
                            "candidates_token_count": 25,
                        },
                        "model_version": "gemini-3.8-flash",
                    },
                )()
                plugin.on_event(
                    ev, session_id="sess_cached_1", invocation_id=f"inv_c_{i}"
                )

            self.assertEqual(len(read_calls), 1)

    def test_p2_5_bigquery_sink_write_deep_copies_record(self):
        import json
        import threading

        from adk_tokenomics_plugin.core.sinks import BigQuerySink

        captured_rows = []

        class CapturingBQClient:
            project = "my-test-project"

            def insert_rows_json(self, table: str, rows):
                captured_rows.extend(rows)
                return []

        sink = BigQuerySink(
            TokenomicsConfig(
                mode="prod",
                sink="bigquery",
                project_id="my-test-project",
                flush_interval_seconds=60.0,
            ),
            max_retries=0,
        )
        sink._client = CapturingBQClient()

        record = {
            "session_id": "sess_race_guard",
            "app_name": "race_app",
            "model_name": "gemini-3.8-flash",
            "input_tokens": 500,
            "cached_tokens": 100,
            "output_tokens": 50,
            "thinking_tokens": 0,
            "total_cost": 0.001,
            "cost_breakdown": {
                "total_cost": 0.001,
                "rates_used": {"input": 0.75},
            },
            "tools_called": ["load_skill"],
            "skills_active": ["initial-skill"],
            "raw_json": {"nested": ["a"]},
        }
        sink.write(record)

        # Mutate caller lists/dicts after sink.write
        record["skills_active"].append("post_write_mutated_skill")
        record["tools_called"].append("post_write_mutated_tool")
        record["cost_breakdown"]["total_cost"] = 999.0
        record["raw_json"]["nested"].append("mutated")

        # Also verify safe fallback when raw_json contains a non-deepcopyable Lock object
        undeepcopyable_record = {
            "session_id": "sess_lock_fallback",
            "app_name": "race_app",
            "model_name": "gemini-3.8-flash",
            "input_tokens": 100,
            "output_tokens": 10,
            "tools_called": ["t1"],
            "skills_active": ["s1"],
            "raw_json": {"lock": threading.Lock()},
        }
        sink.write_turn(undeepcopyable_record)
        undeepcopyable_record["skills_active"].append("s2_after")

        queued_first = sink._queue.queue[0]
        self.assertEqual(queued_first["skills_active"], ["initial-skill"])
        self.assertEqual(queued_first["tools_called"], ["load_skill"])
        self.assertEqual(queued_first["cost_breakdown"]["total_cost"], 0.001)
        self.assertEqual(queued_first["raw_json"]["nested"], ["a"])

        sink.close()
        self.assertEqual(len(captured_rows), 2)
        attrs_0 = json.loads(captured_rows[0]["attributes"])
        attrs_1 = json.loads(captured_rows[1]["attributes"])
        self.assertEqual(attrs_0["skills_active"], ["initial-skill"])
        self.assertEqual(attrs_0["tools_called"], ["load_skill"])
        self.assertEqual(attrs_1["skills_active"], ["s1"])

    def test_n2_validate_bq_identifier_table_and_view_hyphens_and_leading_digits(
        self,
    ):
        from adk_tokenomics_plugin.core.config import validate_bq_identifier

        # Leading digits and single hyphens are valid for BigQuery table_id and view_id
        self.assertEqual(
            validate_bq_identifier("2026_events-v1", "table_id"),
            "2026_events-v1",
        )
        self.assertEqual(
            validate_bq_identifier("2026_events-v1", "table"), "2026_events-v1"
        )
        self.assertEqual(
            validate_bq_identifier("2026_v-llm-response", "view_id"),
            "2026_v-llm-response",
        )
        self.assertEqual(
            validate_bq_identifier("99_rollup-view", "view"), "99_rollup-view"
        )

        # Datasets allow leading digits and underscores, while rejecting hyphens and SQL injection characters
        self.assertEqual(
            validate_bq_identifier("2026_dataset", "dataset_id"), "2026_dataset"
        )
        self.assertEqual(
            validate_bq_identifier("2026_dataset", "identifier"), "2026_dataset"
        )
        for bad_ds in ("2026_events-v1", "my-dataset", "bad;drop"):
            with self.assertRaises(ValueError):
                validate_bq_identifier(bad_ds, "dataset_id")
            with self.assertRaises(ValueError):
                validate_bq_identifier(bad_ds, "identifier")

        # SQL comments (--), leading hyphens, backticks, semicolons, quotes, whitespace, and slashes are rejected
        for malicious_tbl in (
            "tbl--drop",
            "tbl`; DROP",
            "-leading-hyphen",
            "tbl space",
            "tbl/slash",
            "tbl'quote",
            'tbl"dquote',
            "",
        ):
            with self.assertRaises(
                ValueError, msg=f"Expected ValueError for {malicious_tbl!r}"
            ):
                validate_bq_identifier(malicious_tbl, "table_id")
            with self.assertRaises(
                ValueError, msg=f"Expected ValueError for {malicious_tbl!r}"
            ):
                validate_bq_identifier(malicious_tbl, "view_id")

    def test_shared_auto_export_path_interleaved_plugins_preserve_isolated_archives(
        self,
    ):
        import json
        import tempfile
        from unittest.mock import patch

        from adk_tokenomics_plugin.core.config import (
            resolve_models_config_path,
            resolve_writable_models_config_path,
        )
        from adk_tokenomics_plugin.core.sinks import BigQuerySink

        # 1. LIFO atexit order: p1:A(a1) -> p2:B(b1) -> p1:A(a2) -> p2:B(b2) -> p2 atexit -> p1 atexit
        with tempfile.TemporaryDirectory() as tmp_dir:
            live_path = os.path.join(tmp_dir, "live_adk_session.json")
            p1 = TokenControlTower(mode="dev", sink="memory").create_adk_plugin(
                "app1", auto_export_path=live_path
            )
            p2 = TokenControlTower(mode="dev", sink="memory").create_adk_plugin(
                "app2", auto_export_path=live_path
            )

            def ev(tok=100):
                return type(
                    "E",
                    (),
                    {
                        "usage_metadata": {
                            "prompt_token_count": tok,
                            "candidates_token_count": 10,
                        }
                    },
                )()

            p1.on_event(ev(100), session_id="A", invocation_id="a1")
            p2.on_event(ev(200), session_id="B", invocation_id="b1")
            p1.on_event(ev(300), session_id="A", invocation_id="a2")
            p2.on_event(ev(400), session_id="B", invocation_id="b2")

            p2._atexit_finalize_session()
            p1._atexit_finalize_session()

            with open(os.path.join(tmp_dir, "A.json"), encoding="utf-8") as fa:
                arch_a = json.load(fa)
            with open(os.path.join(tmp_dir, "B.json"), encoding="utf-8") as fb:
                arch_b = json.load(fb)

            self.assertEqual(arch_a["session_id"], "A")
            self.assertEqual(
                [t["invocation_id"] for t in arch_a["turns"]], ["a1", "a2"]
            )
            self.assertTrue(
                all(t["session_id"] == "A" for t in arch_a["turns"])
            )
            self.assertEqual(arch_a["summary"]["total_turns"], 2)

            self.assertEqual(arch_b["session_id"], "B")
            self.assertEqual(
                [t["invocation_id"] for t in arch_b["turns"]], ["b1", "b2"]
            )
            self.assertTrue(
                all(t["session_id"] == "B" for t in arch_b["turns"])
            )
            self.assertEqual(arch_b["summary"]["total_turns"], 2)
            self.assertFalse(os.path.exists(live_path))

        # 2. Reverse finalize order (p1 finalizes "A" while live file holds "B", then p2 finalizes "B")
        with tempfile.TemporaryDirectory() as tmp_dir2:
            live_path2 = os.path.join(tmp_dir2, "live_adk_session.json")
            p1_r = TokenControlTower(
                mode="dev", sink="memory"
            ).create_adk_plugin("app1", auto_export_path=live_path2)
            p2_r = TokenControlTower(
                mode="dev", sink="memory"
            ).create_adk_plugin("app2", auto_export_path=live_path2)

            p1_r.on_event(ev(110), session_id="A", invocation_id="a1")
            p2_r.on_event(ev(210), session_id="B", invocation_id="b1")
            p1_r.on_event(ev(310), session_id="A", invocation_id="a2")
            p2_r.on_event(ev(410), session_id="B", invocation_id="b2")

            p1_r.finalize_session(session_id="A")
            p2_r.finalize_session(session_id="B")

            with open(
                os.path.join(tmp_dir2, "A.json"), encoding="utf-8"
            ) as fa2:
                arch_a2 = json.load(fa2)
            with open(
                os.path.join(tmp_dir2, "B.json"), encoding="utf-8"
            ) as fb2:
                arch_b2 = json.load(fb2)

            self.assertEqual(
                [t["invocation_id"] for t in arch_a2["turns"]], ["a1", "a2"]
            )
            self.assertTrue(
                all(t["session_id"] == "A" for t in arch_a2["turns"])
            )
            self.assertEqual(
                [t["invocation_id"] for t in arch_b2["turns"]], ["b1", "b2"]
            )
            self.assertTrue(
                all(t["session_id"] == "B" for t in arch_b2["turns"])
            )

            # 3. Verify OverflowError guard on float('inf'), get_rate() shallow copy + gpt-5.1/gpt-5-pro,
            # BigQuerySink post-close drop guard, and TOKENOMICS_MODELS_CONFIG env alias
            inf_rec = p1_r.on_event(
                type(
                    "InfEv",
                    (),
                    {
                        "usage_metadata": {
                            "prompt_token_count": float("inf"),
                            "candidates_token_count": 5,
                        }
                    },
                )(),
                session_id="C",
                invocation_id="c1",
            )
            self.assertIsNotNone(inf_rec)
            self.assertEqual(inf_rec["input_tokens"], 0)
            self.assertEqual(inf_rec["output_tokens"], 5)

            engine = PricingEngine()
            mutated_rate = engine.get_rate("gpt-5")
            mutated_rate["input_per_million"] = 999.0
            self.assertAlmostEqual(
                engine.get_rate("gpt-5")["input_per_million"], 1.25, places=6
            )
            self.assertAlmostEqual(
                engine.get_rate("gpt-5.1")["input_per_million"], 1.25, places=6
            )
            self.assertAlmostEqual(
                engine.get_rate("gpt-5.1-mini")["input_per_million"],
                0.15,
                places=6,
            )
            self.assertGreater(
                engine.get_rate("gpt-5-pro")["input_per_million"], 2.50
            )

            bq_sink = BigQuerySink(
                TokenomicsConfig(
                    mode="prod",
                    sink="bigquery",
                    project_id="ok-proj",
                    flush_interval_seconds=0.0,
                ),
                max_retries=0,
            )
            self.assertGreaterEqual(bq_sink._flush_interval_seconds, 0.05)
            bq_sink.close()
            bq_sink.write_turn(
                {
                    "session_id": "after_close",
                    "input_tokens": 10,
                    "output_tokens": 2,
                }
            )
            self.assertEqual(bq_sink.dropped_rows_count, 1)

            custom_cfg_file = os.path.join(tmp_dir2, "alias_models.json")
            with open(custom_cfg_file, "w", encoding="utf-8") as cf:
                json.dump({"models": []}, cf)
            with patch.dict(
                os.environ,
                {
                    "TOKENOMICS_MODELS_CONFIG": custom_cfg_file,
                    "TOKENOMICS_MODELS_CONFIG_PATH": "",
                },
                clear=False,
            ):
                self.assertEqual(
                    os.path.abspath(resolve_models_config_path()),
                    os.path.abspath(custom_cfg_file),
                )
                self.assertEqual(
                    os.path.abspath(resolve_writable_models_config_path()),
                    os.path.abspath(custom_cfg_file),
                )

    def test_v102_canonicalize_model_id_deleted_ids_and_replace_defaults(self):
        import json
        import tempfile

        import adk_tokenomics_plugin as pkg
        from adk_tokenomics_plugin.core.config import canonicalize_model_id

        self.assertEqual(pkg.__version__, "1.0.2")

        # Exact case-insensitive match against DEFAULT_MODELS_CONFIG after stripping ONLY Google prefixes
        self.assertEqual(
            canonicalize_model_id("gemini-3.8-flash"),
            "publishers/google/models/gemini-3.8-flash",
        )
        self.assertEqual(
            canonicalize_model_id("models/gemini-3.8-flash"),
            "publishers/google/models/gemini-3.8-flash",
        )
        self.assertEqual(
            canonicalize_model_id("GEMINI-4-ARGON"),
            "publishers/google/models/gemini-4-argon",
        )
        self.assertEqual(
            canonicalize_model_id("models/claude-opus-4"), "claude-opus-4"
        )

        # Distinct custom IDs and non-Google routing prefixes return unchanged without fuzzy prefix overwrite (M-a)
        self.assertEqual(
            canonicalize_model_id("gemini-3.8-flash-custom-test"),
            "gemini-3.8-flash-custom-test",
        )
        self.assertEqual(
            canonicalize_model_id("custom/gemini-3.8-flash"),
            "custom/gemini-3.8-flash",
        )
        self.assertEqual(
            canonicalize_model_id("vertex_ai/gemini-3.8-flash"),
            "vertex_ai/gemini-3.8-flash",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            user_cfg_path = os.path.join(tmp_dir, "models_config.json")

            # 1. Default seeded catalog count == 20
            with open(user_cfg_path, "w", encoding="utf-8") as f:
                json.dump({"models": []}, f)
            base_models = TokenomicsConfig(
                models_config_path=user_cfg_path
            ).get_models_config()["models"]
            self.assertEqual(len(base_models), 20)

            # 2. Deleting claude-opus-4 via deleted_ids suppresses seeded default (count == 19)
            with open(user_cfg_path, "w", encoding="utf-8") as f:
                json.dump({"models": [], "deleted_ids": ["claude-opus-4"]}, f)
            after_del = TokenomicsConfig(
                models_config_path=user_cfg_path
            ).get_models_config()["models"]
            self.assertEqual(len(after_del), 19)
            self.assertNotIn("claude-opus-4", {m["id"] for m in after_del})

            # 3. Re-adding claude-opus-4 in user 'models' survives EVEN IF deleted_ids still lists it, AND when cleared (M-a)
            readded_opus = {
                "id": "claude-opus-4",
                "name": "Claude Opus 4 Restored",
                "provider": "anthropic",
                "pricing": {"input": 12.0, "cached": 1.2, "output": 60.0},
            }
            with open(user_cfg_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "models": [readded_opus],
                        "deleted_ids": ["claude-opus-4"],
                    },
                    f,
                )
            restored_with_stale_del = TokenomicsConfig(
                models_config_path=user_cfg_path
            ).get_models_config()["models"]
            self.assertEqual(len(restored_with_stale_del), 20)
            opus_entry = next(
                m for m in restored_with_stale_del if m["id"] == "claude-opus-4"
            )
            self.assertEqual(opus_entry["pricing"]["input"], 12.0)

            # 4. Adding gemini-3.8-flash-custom-test preserves default gemini-3.8-flash without overwrite (21 models)
            custom_flash_variant = {
                "id": "gemini-3.8-flash-custom-test",
                "name": "Gemini Flash Custom Experiment",
                "provider": "google",
                "pricing": {"input": 0.42, "cached": 0.042, "output": 1.99},
            }
            short_flash_override = {
                "id": "gemini-3.8-flash",
                "name": "Gemini 3.8 Flash",
                "provider": "google",
                "pricing": {"input": 0.70, "cached": 0.07, "output": 3.50},
            }
            with open(user_cfg_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "models": [
                            readded_opus,
                            short_flash_override,
                            custom_flash_variant,
                        ],
                        "deleted_ids": [],
                    },
                    f,
                )
            with_custom_variant = TokenomicsConfig(
                models_config_path=user_cfg_path
            ).get_models_config()["models"]
            self.assertEqual(len(with_custom_variant), 21)
            by_id = {m["id"]: m for m in with_custom_variant}
            self.assertEqual(
                by_id["publishers/google/models/gemini-3.8-flash"]["pricing"][
                    "input"
                ],
                0.70,
            )
            self.assertEqual(
                by_id["gemini-3.8-flash-custom-test"]["pricing"]["input"], 0.42
            )

            # 5. replace_defaults=True skips seeding DEFAULT_MODELS_CONFIG
            with open(user_cfg_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "replace_defaults": True,
                        "models": [custom_flash_variant],
                    },
                    f,
                )
            replaced = TokenomicsConfig(
                models_config_path=user_cfg_path
            ).get_models_config()["models"]
            self.assertEqual(len(replaced), 1)
            self.assertEqual(replaced[0]["id"], "gemini-3.8-flash-custom-test")

    def test_v102_bq_env_precedence_explicit_sentinel_and_rule_validation(self):
        import types
        from unittest.mock import patch

        from adk_tokenomics_plugin.core.config import (
            OptimizationRulesConfig,
            resolve_env_bq_identifiers,
        )

        clean_bq_env = {
            "BQ_ANALYTICS_DATASET_ID": "",
            "BIGQUERY_DATASET_ID": "",
            "BIGQUERY_DATASET": "",
            "BQ_DATASET_ID": "",
            "BQ_ANALYTICS_TABLE_ID": "",
            "BIGQUERY_TABLE_ID": "",
            "BIGQUERY_TABLE": "",
            "BQ_TABLE_ID": "",
            "BQ_ANALYTICS_VIEW_ID": "",
            "BIGQUERY_VIEW_ID": "",
            "BIGQUERY_VIEW": "",
            "BQ_VIEW_ID": "",
        }
        with patch.dict(os.environ, clean_bq_env, clear=False):
            self.assertEqual(
                resolve_env_bq_identifiers(include_dotenv=False),
                ("bq_adk_ds", "adk_agent_events", "v_llm_response"),
            )

        # Precedence chain: BQ_ANALYTICS_* > BIGQUERY_*_ID > BIGQUERY_* > BQ_*_ID
        precedence_env = {
            **clean_bq_env,
            "BQ_DATASET_ID": "ds_tier4",
            "BIGQUERY_DATASET": "ds_tier3",
            "BIGQUERY_DATASET_ID": "ds_tier2",
            "BQ_ANALYTICS_DATASET_ID": "ds_tier1",
            "BIGQUERY_TABLE": "tbl_tier3",
            "BQ_VIEW_ID": "vw_tier4",
        }
        with patch.dict(os.environ, precedence_env, clear=False):
            env_cfg = TokenomicsConfig()
            self.assertEqual(env_cfg.dataset_id, "ds_tier1")
            self.assertEqual(env_cfg.table_id, "tbl_tier3")
            self.assertEqual(env_cfg.view_id, "vw_tier4")

            # Explicit TokenomicsConfig(dataset_id="bq_adk_ds") beats env vars!
            explicit_cfg = TokenomicsConfig(
                dataset_id="bq_adk_ds",
                table_id="adk_agent_events",
                view_id="v_llm_response",
            )
            self.assertEqual(explicit_cfg.dataset_id, "bq_adk_ds")
            self.assertEqual(explicit_cfg.table_id, "adk_agent_events")
            self.assertEqual(explicit_cfg.view_id, "v_llm_response")

            explicit_tower = TokenControlTower(
                mode="dev", dataset_id="bq_adk_ds", view_id="custom_view_v2"
            )
            self.assertEqual(explicit_tower.config.dataset_id, "bq_adk_ds")
            self.assertEqual(explicit_tower.config.view_id, "custom_view_v2")

        # Verify TokenControlTower.provision_cloud_infra() forwards view_id to BigQuery DDL provisioning
        executed_ddls = []

        class ProvisionClient:
            project = "tower-prov-proj"

            def create_dataset(self, ds, exists_ok=False):
                return ds

            def query(self, sql):
                executed_ddls.append(sql)
                return type("Job", (), {"result": lambda self_j: []})()

        fake_google = sys.modules.get("google") or types.ModuleType("google")
        fake_cloud = sys.modules.get("google.cloud") or types.ModuleType(
            "google.cloud"
        )
        fake_bq = types.ModuleType("google.cloud.bigquery")
        fake_bq.Client = lambda project=None: ProvisionClient()  # type: ignore[attr-defined]
        fake_bq.Dataset = lambda ds_id: types.SimpleNamespace(
            dataset_id=ds_id, location="US"
        )  # type: ignore[attr-defined]
        fake_cloud.bigquery = fake_bq  # type: ignore[attr-defined]
        fake_google.cloud = fake_cloud  # type: ignore[attr-defined]

        with patch.dict(
            sys.modules,
            {
                "google": fake_google,
                "google.cloud": fake_cloud,
                "google.cloud.bigquery": fake_bq,
            },
        ):
            prov_tower = TokenControlTower(
                mode="dev",
                sink="memory",
                project_id="tower-prov-proj",
                dataset_id="2026_finops_ds",
                table_id="2026_adk-events",
                view_id="custom_llm_view",
            )
            self.assertTrue(prov_tower.provision_cloud_infra())
        self.assertEqual(len(executed_ddls), 2)
        self.assertIn(
            "`tower-prov-proj.2026_finops_ds.2026_adk-events`", executed_ddls[0]
        )
        self.assertIn(
            "CREATE VIEW IF NOT EXISTS `tower-prov-proj.2026_finops_ds.custom_llm_view`",
            executed_ddls[1],
        )

        # Rule validation negative checks
        with self.assertRaises(ValueError):
            OptimizationRulesConfig(modular_skills_reduction_ratio=1.5)
        with self.assertRaises(ValueError):
            OptimizationRulesConfig(compaction_turn_threshold=0)
        with self.assertRaises(ValueError):
            TokenomicsConfig(optimization_rules={"nonexistent_rule": 123})  # type: ignore[arg-type]
        tower_rules = TokenControlTower(mode="dev")
        with self.assertRaises(ValueError):
            tower_rules.configure_optimization_rules(unknown_threshold=10)
        with self.assertRaises(ValueError):
            tower_rules.configure_optimization_rules(min_cache_tokens=-5)

    def test_v102_bq_ddl_auto_detect_and_no_such_field_fallback_guardrails(
        self,
    ):
        import types

        from adk_tokenomics_plugin.core.sinks import BigQuerySink

        # 1. Existing v1.0.1 flat get_table().schema auto-uses flat rows and caches detection on success
        class ExistingFlatTableClient:
            project = "flat-detect-proj"

            def __init__(self):
                self.get_table_calls = 0
                self.inserted = []

            def get_table(self, table_ref: str):
                self.get_table_calls += 1
                return types.SimpleNamespace(
                    schema=[
                        types.SimpleNamespace(name="timestamp"),
                        types.SimpleNamespace(name="session_id"),
                        types.SimpleNamespace(name="input_tokens"),
                        types.SimpleNamespace(name="raw_json"),
                    ]
                )

            def insert_rows_json(self, table_ref: str, rows):
                for r in rows:
                    if "attributes" in r or "event_type" in r:
                        return [
                            {
                                "index": 0,
                                "errors": [
                                    {"message": "no such field: event_type."}
                                ],
                            }
                        ]
                self.inserted.extend(rows)
                return []

        sink_existing_flat = BigQuerySink(
            TokenomicsConfig(
                mode="prod",
                sink="bigquery",
                project_id="flat-detect-proj",
                table_id="adk_agent_events",
                flush_interval_seconds=60.0,
            ),
            max_retries=0,
        )
        flat_client = ExistingFlatTableClient()
        sink_existing_flat._client = flat_client
        sink_existing_flat.write_turn(
            {"session_id": "s_f1", "input_tokens": 200, "output_tokens": 20}
        )
        sink_existing_flat.flush()
        sink_existing_flat.write_turn(
            {"session_id": "s_f2", "input_tokens": 300, "output_tokens": 30}
        )
        sink_existing_flat.close()
        self.assertEqual(len(flat_client.inserted), 2)
        self.assertEqual(flat_client.get_table_calls, 1)
        self.assertIn("input_tokens", flat_client.inserted[0])

        # 2. Transient get_table() failure (not cached!) + "no such field" error on ADK rows auto-flips & retries once (M-b)
        class TransientGetTableThenFlatClient:
            project = "transient-proj"

            def __init__(self, fail_get_table_always: bool = False):
                self.fail_get_table_always = fail_get_table_always
                self.get_table_calls = 0
                self.insert_calls = 0
                self.inserted: list[Any] = []

            def get_table(self, table_ref: str):
                self.get_table_calls += 1
                if self.fail_get_table_always or self.get_table_calls == 1:
                    raise RuntimeError("503 get_table unavailable")
                return types.SimpleNamespace(
                    schema=[
                        types.SimpleNamespace(name="timestamp"),
                        types.SimpleNamespace(name="input_tokens"),
                    ]
                )

            def insert_rows_json(self, table_ref: str, rows):
                self.insert_calls += 1
                for idx, r in enumerate(rows):
                    if "attributes" in r:
                        return [
                            {
                                "index": idx,
                                "errors": [
                                    {
                                        "location": "attributes",
                                        "message": "no such field: attributes.",
                                    }
                                ],
                            }
                        ]
                self.inserted.extend(rows)
                return []

        for always_fail_gt in (False, True):
            sink_flip = BigQuerySink(
                TokenomicsConfig(
                    mode="prod",
                    sink="bigquery",
                    project_id="transient-proj",
                    table_id="2026_adk-events",
                    flush_interval_seconds=60.0,
                ),
                max_retries=0,
            )
            trans_client = TransientGetTableThenFlatClient(
                fail_get_table_always=always_fail_gt
            )
            sink_flip._client = trans_client
            sink_flip.write_turn(
                {
                    "session_id": "s_flip_1",
                    "input_tokens": 400,
                    "output_tokens": 40,
                }
            )
            sink_flip.flush()
            self.assertEqual(sink_flip.dropped_rows_count, 0)
            self.assertEqual(len(trans_client.inserted), 1)
            self.assertEqual(trans_client.insert_calls, 2)
            # Subsequent batch uses cached flat format immediately (1 insert call, 0 retries)
            sink_flip.write_turn(
                {
                    "session_id": "s_flip_2",
                    "input_tokens": 500,
                    "output_tokens": 50,
                }
            )
            sink_flip.close()
            self.assertEqual(len(trans_client.inserted), 2)
            self.assertEqual(trans_client.insert_calls, 3)

        # 3. Critic Note 1 negative guard: ADK table returning "no such field: unrelated_col" must NOT flip cache to flat
        class AdkSchemaUnrelatedFieldErrorClient:
            project = "adk-guard-proj"

            def get_table(self, table_ref: str):
                raise RuntimeError("403 bigquery.tables.get denied")

            def insert_rows_json(self, table_ref: str, rows):
                return [
                    {
                        "index": 0,
                        "errors": [
                            {
                                "message": "no such field: custom_unrelated_column."
                            }
                        ],
                    }
                ]

        sink_guard = BigQuerySink(
            TokenomicsConfig(
                mode="prod",
                sink="bigquery",
                project_id="adk-guard-proj",
                flush_interval_seconds=60.0,
            ),
            max_retries=0,
        )
        sink_guard._client = AdkSchemaUnrelatedFieldErrorClient()
        sink_guard.write_turn(
            {"session_id": "s_guard", "input_tokens": 100, "output_tokens": 10}
        )
        sink_guard.close()
        self.assertEqual(sink_guard.dropped_rows_count, 1)
        self.assertNotEqual(
            sink_guard._table_format_cache.get(
                "adk-guard-proj.bq_adk_ds.adk_agent_events"
            ),
            "flat",
        )

    def test_v102_parse_bq_turn_record_eight_cell_matrix_and_fetch_recent(self):
        import json

        from adk_tokenomics_plugin.core.sinks import (
            BigQuerySink,
            parse_bq_turn_record,
        )

        # Writer 1: Upstream Google ADK BigQueryAgentAnalyticsPlugin (1000 prompt + 200 tool_use = 1200, 300 cached, 150 out, 50 think)
        upstream_content = json.dumps(
            {
                "model_version": "gemini-3.8-flash",
                "user_query": "Check portfolio",
                "response": "Portfolio balanced.",
                "usage": {"prompt": 1000, "completion": 150, "total": 1400},
            }
        )
        upstream_attrs = json.dumps(
            {
                "model_version": "gemini-3.8-flash",
                "usage_metadata": {
                    "prompt_token_count": 1000,
                    "tool_use_prompt_token_count": 200,
                    "cached_content_token_count": 300,
                    "candidates_token_count": 150,
                    "thoughts_token_count": 50,
                    "total_token_count": 1400,
                },
            }
        )

        # Writer 2: BigQuerySink._format_rows (inp=900, cac=300 => prompt=1200, zero tool_use fields written)
        sink_formatter = BigQuerySink(
            TokenomicsConfig(
                mode="prod",
                sink="bigquery",
                project_id="matrix-proj",
                flush_interval_seconds=60.0,
            ),
            max_retries=0,
        )
        try:
            sink_raw_row = sink_formatter._format_rows(
                [
                    {
                        "timestamp": "2026-11-04T12:00:00+00:00",
                        "session_id": "s_matrix",
                        "invocation_id": "inv_m",
                        "app_name": "matrix_app",
                        "model_name": "gemini-3.8-flash",
                        "user_query": "Check portfolio",
                        "agent_response": "Portfolio balanced.",
                        "input_tokens": 900,
                        "cached_tokens": 300,
                        "output_tokens": 150,
                        "thinking_tokens": 50,
                        "total_cost": 0.00145,
                        "latency_ms": 85,
                    }
                ],
                row_format="adk",
            )[0]
        finally:
            sink_formatter.close()

        sink_content_dict = json.loads(sink_raw_row["content"])
        sink_attrs_dict = json.loads(sink_raw_row["attributes"])
        self.assertEqual(
            sink_content_dict["usage"],
            {"prompt": 1200, "completion": 150, "total": 1400},
        )
        self.assertNotIn(
            "tool_use_prompt_token_count", sink_attrs_dict["usage_metadata"]
        )
        self.assertNotIn("usage_tool_use_tokens", sink_attrs_dict)

        eight_cells = {
            "upstream_view__upstream_writer": {
                "timestamp": "2026-11-04T12:00:00Z",
                "event_type": "LLM_RESPONSE",
                "agent": "matrix_app",
                "session_id": "s_matrix",
                "invocation_id": "inv_m",
                "model_version": "gemini-3.8-flash",
                "usage_prompt_tokens": 1000,
                "usage_tool_use_tokens": 200,
                "usage_cached_tokens": 300,
                "usage_completion_tokens": 150,
                "usage_thinking_tokens": 50,
                "total_ms": 85,
            },
            "upstream_view__bqsink_writer": {
                "timestamp": "2026-11-04T12:00:00Z",
                "event_type": "LLM_RESPONSE",
                "agent": "matrix_app",
                "session_id": "s_matrix",
                "invocation_id": "inv_m",
                "model_version": "gemini-3.8-flash",
                "usage_prompt_tokens": sink_content_dict["usage"]["prompt"],
                "usage_tool_use_tokens": 0,
                "usage_cached_tokens": 300,
                "usage_completion_tokens": 150,
                "usage_thinking_tokens": 50,
                "total_ms": 85,
            },
            "v102_view__upstream_writer": {
                "timestamp": "2026-11-04T12:00:00Z",
                "event_type": "LLM_RESPONSE",
                "agent": "matrix_app",
                "session_id": "s_matrix",
                "invocation_id": "inv_m",
                "model_version": "gemini-3.8-flash",
                "usage_prompt_tokens": 1000,
                "usage_tool_use_tokens": 200,
                "usage_cached_tokens": 300,
                "usage_completion_tokens": 150,
                "usage_thinking_tokens": 50,
                "total_ms": 85,
                "content": upstream_content,
                "attributes": upstream_attrs,
            },
            "v102_view__bqsink_writer": {
                "timestamp": "2026-11-04T12:00:00Z",
                "event_type": "LLM_RESPONSE",
                "agent": "matrix_app",
                "session_id": "s_matrix",
                "invocation_id": "inv_m",
                "model_version": "gemini-3.8-flash",
                "usage_prompt_tokens": 1200,
                "usage_tool_use_tokens": 0,
                "usage_cached_tokens": 300,
                "usage_completion_tokens": 150,
                "usage_thinking_tokens": 50,
                "total_ms": 85,
                "content": sink_raw_row["content"],
                "attributes": sink_raw_row["attributes"],
            },
            "prev102_view__upstream_writer": {
                "timestamp": "2026-11-04T12:00:00Z",
                "event_type": "LLM_RESPONSE",
                "agent": "matrix_app",
                "session_id": "s_matrix",
                "invocation_id": "inv_m",
                "model_version": "gemini-3.8-flash",
                "usage_prompt_tokens": 1000,
                "usage_cached_tokens": 300,
                "usage_completion_tokens": 150,
                "usage_thinking_tokens": 50,
                "total_ms": 85,
                "content": upstream_content,
                "attributes": upstream_attrs,
            },
            "prev102_view__bqsink_writer": {
                "timestamp": "2026-11-04T12:00:00Z",
                "event_type": "LLM_RESPONSE",
                "agent": "matrix_app",
                "session_id": "s_matrix",
                "invocation_id": "inv_m",
                "model_version": "gemini-3.8-flash",
                "usage_prompt_tokens": 1200,
                "usage_cached_tokens": 300,
                "usage_completion_tokens": 150,
                "usage_thinking_tokens": 50,
                "total_ms": 85,
                "content": sink_raw_row["content"],
                "attributes": sink_raw_row["attributes"],
            },
            "raw_base_table__upstream_writer": {
                "timestamp": "2026-11-04T12:00:00Z",
                "event_type": None,
                "agent": "matrix_app",
                "session_id": "s_matrix",
                "invocation_id": "inv_m",
                "content": upstream_content,
                "attributes": upstream_attrs,
                "latency_ms": '{"total_ms": 85}',
            },
            "raw_base_table__bqsink_writer": sink_raw_row,
        }

        for cell_name, row_payload in eight_cells.items():
            parsed = parse_bq_turn_record(row_payload)
            self.assertEqual(
                parsed["prompt_tokens"],
                1200,
                msg=f"Failed prompt_tokens on {cell_name}",
            )
            self.assertEqual(
                parsed["input_tokens"],
                900,
                msg=f"Failed input_tokens on {cell_name}",
            )
            self.assertEqual(
                parsed["cached_tokens"],
                300,
                msg=f"Failed cached_tokens on {cell_name}",
            )
            self.assertEqual(
                parsed["output_tokens"],
                150,
                msg=f"Failed output_tokens on {cell_name}",
            )
            self.assertEqual(
                parsed["thinking_tokens"],
                50,
                msg=f"Failed thinking_tokens on {cell_name}",
            )
            self.assertEqual(
                parsed["event_type"],
                "LLM_RESPONSE",
                msg=f"Failed event_type on {cell_name}",
            )

        # Verify FinOpsAggregator.aggregate_records(..., pricing_engine=custom_pe)
        custom_cfg = TokenomicsConfig(
            custom_pricing={
                "gemini-3.8-flash": {
                    "input": 2.0,
                    "cached": 0.2,
                    "output": 10.0,
                }
            }
        )
        custom_pe = PricingEngine(custom_cfg)
        agg = FinOpsAggregator.aggregate_records(
            [eight_cells["raw_base_table__upstream_writer"]],
            pricing_engine=custom_pe,
        )
        # 900 * 2/1M (0.0018) + 300 * 0.2/1M (0.00006) + 200 * 10/1M (0.0020) = 0.00386
        self.assertAlmostEqual(agg["kpis"]["total_spend"], 0.00386, places=5)

    def test_v102_bigquery_sink_flush_inflight_and_plugin_close_lifecycle(self):
        import asyncio
        import json
        import tempfile
        import threading

        from adk_tokenomics_plugin.core.sinks import BigQuerySink

        # 1. BigQuerySink.flush() waits for BOTH in-flight worker batches AND queued batches without closing sink (L-a)
        inflight_entered = threading.Event()
        release_inflight = threading.Event()
        persisted_batches = []
        persist_lock = threading.Lock()

        class BlockingInflightBQClient:
            project = "flush-test-proj"

            def insert_rows_json(self, table_ref: str, rows):
                batch_sids = [r["session_id"] for r in rows]
                if "s_inflight" in batch_sids:
                    inflight_entered.set()
                    release_inflight.wait(timeout=5.0)
                with persist_lock:
                    persisted_batches.extend(batch_sids)
                return []

        sink = BigQuerySink(
            TokenomicsConfig(
                mode="prod",
                sink="bigquery",
                project_id="flush-test-proj",
                batch_size=1,
                flush_interval_seconds=0.05,
            ),
            max_retries=0,
        )
        sink._client = BlockingInflightBQClient()
        try:
            sink.write_turn(
                {
                    "session_id": "s_inflight",
                    "input_tokens": 100,
                    "output_tokens": 10,
                }
            )
            self.assertTrue(inflight_entered.wait(timeout=3.0))
            sink.write_turn(
                {
                    "session_id": "s_queued",
                    "input_tokens": 200,
                    "output_tokens": 20,
                }
            )

            flush_done = threading.Event()
            flush_thread = threading.Thread(
                target=lambda: (sink.flush(), flush_done.set())
            )
            flush_thread.start()

            # While worker's in-flight insert is still blocked, flush() MUST NOT have returned yet!
            self.assertFalse(flush_done.wait(timeout=0.08))
            release_inflight.set()
            flush_thread.join(timeout=3.0)
            self.assertTrue(flush_done.is_set())
            self.assertFalse(sink._closed)
            self.assertEqual(set(persisted_batches), {"s_inflight", "s_queued"})

            # 2. ADKTokenomicsPlugin.close() flushes sink, finalizes real session, skips default_session, & keeps streaming alive (L-d)
            with tempfile.TemporaryDirectory() as tmp_dir:
                live_path = os.path.join(tmp_dir, "live_adk_session.json")
                tracker = TokenTracker(config=sink.config, sink=sink)
                plugin = ADKTokenomicsPlugin(
                    tracker=tracker,
                    app_name="close_app",
                    auto_export_path=live_path,
                )

                ev1 = type(
                    "E1",
                    (),
                    {
                        "usage_metadata": {
                            "prompt_token_count": 300,
                            "candidates_token_count": 30,
                        }
                    },
                )()
                ev_def = type(
                    "ED",
                    (),
                    {
                        "usage_metadata": {
                            "prompt_token_count": 150,
                            "candidates_token_count": 15,
                        }
                    },
                )()
                plugin.on_event(
                    ev1, session_id="sess_real_01", invocation_id="inv_r1"
                )
                plugin.on_event(
                    ev_def,
                    session_id="default_session",
                    invocation_id="inv_def",
                )

                asyncio.run(plugin.close())

                self.assertTrue(
                    os.path.exists(os.path.join(tmp_dir, "sess_real_01.json"))
                )
                self.assertFalse(
                    os.path.exists(
                        os.path.join(tmp_dir, "default_session.json")
                    )
                )
                self.assertIn("sess_real_01", persisted_batches)
                self.assertIn("default_session", persisted_batches)
                self.assertFalse(sink._closed)

                # Subsequent turns AFTER await plugin.close() still stream cleanly, and atexit is idempotent
                ev2 = type(
                    "E2",
                    (),
                    {
                        "usage_metadata": {
                            "prompt_token_count": 450,
                            "candidates_token_count": 45,
                        }
                    },
                )()
                plugin.on_event(
                    ev2, session_id="sess_after_close", invocation_id="inv_r2"
                )
                sink.flush()
                self.assertIn("sess_after_close", persisted_batches)
                plugin._atexit_finalize_session()
                self.assertTrue(
                    os.path.exists(
                        os.path.join(tmp_dir, "sess_after_close.json")
                    )
                )
                with open(
                    os.path.join(tmp_dir, "sess_real_01.json"),
                    encoding="utf-8",
                ) as rf:
                    self.assertEqual(len(json.load(rf)["turns"]), 1)
        finally:
            release_inflight.set()
            sink.close()

        # Critic Note 2: sink lacking a .flush attribute does not raise in await plugin.close()
        class FlushlessSink:
            def write_turn(self, record):
                pass

            def fetch_recent(self, limit=50):
                return []

        plugin_no_flush = ADKTokenomicsPlugin(
            tracker=TokenTracker(sink=FlushlessSink()),  # type: ignore[arg-type]
            app_name="flushless_app",
        )
        asyncio.run(plugin_no_flush.close())

    def test_v102_merge_archive_fast_path_unload_skill_flow_and_generator_decorator(
        self,
    ):
        import asyncio
        import inspect
        import json
        import tempfile

        # 1. _merge_turns_into_archive fast-paths os.replace ONLY when not valid_arch and live_on_disk_count == len(merged)
        with tempfile.TemporaryDirectory() as tmp_dir:
            plugin = TokenControlTower(
                mode="dev", sink="memory"
            ).create_adk_plugin("arch_test")
            stale_live_path = os.path.join(tmp_dir, "live_adk_session.json")
            t1 = {
                "session_id": "s_merge",
                "invocation_id": "i1",
                "timestamp": "2026-11-04T10:00:00Z",
                "input_tokens": 100,
                "output_tokens": 10,
            }
            t2 = {
                "session_id": "s_merge",
                "invocation_id": "i2",
                "timestamp": "2026-11-04T10:01:00Z",
                "input_tokens": 200,
                "output_tokens": 20,
            }
            with open(stale_live_path, "w", encoding="utf-8") as lf:
                json.dump(
                    {
                        "session_id": "s_merge",
                        "turns": [t1],
                        "summary": {"total_turns": 1},
                    },
                    lf,
                )

            # Disk file only has 1 turn while merged has 2 turns (live_on_disk_count=1 != 2) -> MUST NOT fast-path os.replace!
            arch_path = plugin._merge_turns_into_archive(
                trace_dir=tmp_dir,
                sid="s_merge",
                extra_turns=[t1, t2],
                remove_live_path=stale_live_path,
                live_on_disk_count=1,
            )
            self.assertIsNotNone(arch_path)
            self.assertFalse(os.path.exists(stale_live_path))
            with open(str(arch_path), encoding="utf-8") as af:
                merged_doc = json.load(af)
            self.assertEqual(len(merged_doc["turns"]), 2)
            self.assertEqual(
                [t["invocation_id"] for t in merged_doc["turns"]], ["i1", "i2"]
            )

        # 2. unload_skill excluded from skills_active while preserved in tools_called, and "AgentTool • Parallel • Loop" flow
        plugin_flow = TokenControlTower(
            mode="dev", sink="memory"
        ).create_adk_plugin("flow_skill_app")
        loop_parent = type(
            "LoopAgent", (), {"name": "retry_loop", "parent_agent": None}
        )()
        par_parent = type(
            "ParallelAgent",
            (),
            {"name": "fanout_par", "parent_agent": loop_parent},
        )()
        leaf_child = type(
            "LlmAgent",
            (),
            {"name": "worker_leaf", "parent_agent": par_parent, "tools": []},
        )()
        agent_tool = type(
            "AgentTool", (), {"name": "worker_leaf", "agent": leaf_child}
        )()
        root_ag = type(
            "LlmAgent",
            (),
            {
                "name": "root_ag",
                "parent_agent": None,
                "tools": [agent_tool],
                "sub_agents": [],
            },
        )()
        plugin_flow._discover_agent_tools(root_ag)

        leaf_ctx = type(
            "Ctx",
            (),
            {
                "agent": leaf_child,
                "agent_name": "worker_leaf",
                "session_id": "s_flow",
                "invocation_id": "inv_flow",
            },
        )()
        self.assertEqual(
            plugin_flow._extract_orchestration_flow(
                leaf_ctx, plugin_flow._agent_tool_children
            ),
            "AgentTool • Parallel • Loop",
        )

        plugin_flow.before_model_callback(
            callback_context=leaf_ctx, llm_request=None
        )
        plugin_flow.after_tool_callback(
            tool=type("Tool", (), {"name": "load_skill"})(),
            tool_args={"skill_name": "bq-sql"},
            tool_context=leaf_ctx,
        )
        plugin_flow.after_tool_callback(
            tool=type("Tool", (), {"name": "unload_skill"})(),
            tool_args={"skill_name": "stale-skill"},
            tool_context=leaf_ctx,
        )
        fc_unload = type(
            "FunctionCall",
            (),
            {
                "name": "unload_skill",
                "args": {"skill_name": "another-unloaded"},
            },
        )()
        resp = type(
            "LlmResponse",
            (),
            {
                "usage_metadata": {
                    "prompt_token_count": 600,
                    "candidates_token_count": 60,
                },
                "model_version": "gemini-3.8-flash",
                "candidates": [
                    type(
                        "Cand",
                        (),
                        {
                            "content": type(
                                "Ct",
                                (),
                                {
                                    "parts": [
                                        type(
                                            "P",
                                            (),
                                            {
                                                "text": "Done",
                                                "function_call": fc_unload,
                                            },
                                        )()
                                    ]
                                },
                            )()
                        },
                    )()
                ],
            },
        )()
        plugin_flow.after_model_callback(
            callback_context=leaf_ctx, llm_response=resp
        )
        recorded_turn = plugin_flow.tracker.turns_history[-1]
        self.assertEqual(
            recorded_turn["orchestration_flow"], "AgentTool • Parallel • Loop"
        )
        self.assertEqual(recorded_turn["skills_active"], ["bq-sql"])
        self.assertIn("unload_skill", recorded_turn["tools_called"])

        # 3. @track_tokens adds tool_use_prompt_token_count and wraps sync/async generators
        dec_tracker = TokenTracker(sink=InMemorySink())

        @track_tokens(
            app_name="sync_stream_app",
            model_name="gemini-3.8-flash",
            tracker=dec_tracker,
        )
        def stream_sync(prompt: str):
            yield type(
                "Chunk", (), {"text": "Hello ", "usage_metadata": None}
            )()
            yield type(
                "Chunk",
                (),
                {
                    "text": "sync world!",
                    "usage_metadata": {
                        "prompt_token_count": 1000,
                        "tool_use_prompt_token_count": 200,
                        "cached_content_token_count": 300,
                        "candidates_token_count": 50,
                        "thoughts_token_count": 25,
                    },
                },
            )()

        @track_tokens(
            app_name="async_stream_app",
            model_name="gemini-3.8-flash",
            tracker=dec_tracker,
        )
        async def stream_async(prompt: str):
            yield ("Async ", None)
            yield (
                "world!",
                {
                    "prompt_tokens": 800,
                    "toolUsePromptTokenCount": 150,
                    "cached_tokens": 250,
                    "output_tokens": 40,
                    "thinking_tokens": 10,
                },
            )

        @track_tokens(
            app_name="broken_stream_app",
            model_name="gemini-3.8-flash",
            tracker=dec_tracker,
        )
        def stream_broken(prompt: str):
            yield "partial"
            raise RuntimeError("stream interrupted")

        self.assertTrue(inspect.isgeneratorfunction(stream_sync))
        self.assertTrue(inspect.isasyncgenfunction(stream_async))

        sync_chunks = [c.text for c in stream_sync("q_sync")]
        self.assertEqual("".join(sync_chunks), "Hello sync world!")

        async def _consume_async():
            out = []
            async for txt, _ in stream_async("q_async"):
                out.append(txt)
            return "".join(out)

        self.assertEqual(asyncio.run(_consume_async()), "Async world!")

        with self.assertRaises(RuntimeError):
            list(stream_broken("q_err"))

        self.assertEqual(len(dec_tracker.turns_history), 2)
        t_sync, t_async = dec_tracker.turns_history
        self.assertEqual(t_sync["agent_response"], "Hello sync world!")
        self.assertEqual(t_sync["input_tokens"], 900)
        self.assertEqual(t_sync["cached_tokens"], 300)
        self.assertEqual(t_sync["output_tokens"], 50)
        self.assertEqual(t_sync["thinking_tokens"], 25)

        self.assertEqual(t_async["agent_response"], "Async world!")
        self.assertEqual(t_async["input_tokens"], 700)
        self.assertEqual(t_async["cached_tokens"], 250)
        self.assertEqual(t_async["output_tokens"], 40)
        self.assertEqual(t_async["thinking_tokens"], 10)


if __name__ == "__main__":
    unittest.main()
