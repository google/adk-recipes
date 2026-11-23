"""Regression suite covering all post-UAT remediation items (ISS-01 through ISS-20)."""

from __future__ import annotations

import ast
from contextlib import redirect_stderr, redirect_stdout
import http.server
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

from adk_tokenomics_plugin import (
    ADKTokenomicsPlugin,
    OptimizationRulesConfig,
    PricingEngine,
    TokenControlTower,
    TokenomicsConfig,
    TokenomicsTestCase,
    assert_cost_under,
)
from adk_tokenomics_plugin.cli_profiler import TokenProfiler, profile_from_trace_file
from adk_tokenomics_plugin.cli_web import TokenomicsWebHandler, run_web_server
from adk_tokenomics_plugin.testing.assertions import run_cli_assertions
import scripts.run_tokenomics as launcher

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestUATRemediations(TokenomicsTestCase):
    """End-to-end verification for ISS-01..ISS-20 distilled from Gemini Carbon & Claude Opus 5.5 UAT."""

    def test_iss01_google_gemini_catalog_rate_card_propagation_and_estimated_flags(self) -> None:
        engine = PricingEngine()
        # 1. All 4 Google Gemini models present with exact pricing; gemini-3.8-flash stays cheapest Google tier
        self.assertEqual(engine.get_cheapest_model("google")["short_id"], "gemini-3.8-flash")
        for mid, expected_rates in (
            ("gemini-3.8-flash", (0.75, 0.075, 3.75)),
            ("gemini-4-hydrogen", (1.00, 0.10, 4.00)),
            ("gemini-4-argon", (2.00, 0.10, 10.00)),
            ("gemini-4-neon", (4.00, 0.20, 20.00)),
        ):
            r = engine.get_rate(mid)
            self.assertFalse(r["pricing_estimated"], mid)
            self.assertEqual(r["rate_card_match"], "exact", mid)
            self.assertEqual(
                (r["input_per_million"], r["cached_per_million"], r["output_per_million"]),
                expected_rates,
            )

        # Prefix match stays pricing_estimated=False
        pref = engine.get_rate("gemini-4-hydrogen-001")
        self.assertFalse(pref["pricing_estimated"])
        self.assertEqual(pref["rate_card_match"], "prefix")
        self.assertEqual(pref["input_per_million"], 1.00)

        # 2. Unregistered family & unknown models set pricing_estimated=True and log one warning per model
        with self.assertLogs("adk_tokenomics_plugin.core.pricing", level="WARNING") as cm:
            u_pro = engine.get_rate("unlisted-pro-preview")
            u_pro_again = engine.get_rate("unlisted-pro-preview")
            u_unknown = engine.get_rate("totally-unknown-vendor-model")
        self.assertEqual(len(cm.output), 2)
        self.assertTrue(u_pro["pricing_estimated"])
        self.assertEqual(u_pro["rate_card_match"], "family_heuristic")
        self.assertIsNone(u_pro["id"])
        self.assertEqual(u_pro["input_per_million"], 2.00)
        self.assertEqual(u_pro_again["input_per_million"], 2.00)
        self.assertTrue(u_unknown["pricing_estimated"])
        self.assertEqual(u_unknown["rate_card_match"], "default_fallback")
        self.assertEqual(u_unknown["input_per_million"], 0.75)

        # 3. Editing canonical Argon in Rate Card propagates to unlisted family heuristic models (Carbon F-1)
        engine.register_model(
            "publishers/google/models/gemini-4-argon",
            input_per_million=4.00,
            cached_per_million=0.20,
            output_per_million=20.00,
            provider="google",
            name="Gemini 4 Argon 2x",
        )
        u_pro_doubled = engine.get_rate("unlisted-pro-preview")
        self.assertEqual(u_pro_doubled["input_per_million"], 4.00)
        self.assertEqual(u_pro_doubled["output_per_million"], 20.00)
        self.assertTrue(u_pro_doubled["pricing_estimated"])

    def test_iss02_sparse_web_model_entry_defaults_to_zero_not_invented_rates(self) -> None:
        norm = TokenomicsWebHandler._normalize_model_entry({"id": "custom/sparse-model"})
        self.assertEqual(norm["pricing"]["input"], 0.0)
        self.assertEqual(norm["pricing"]["cached"], 0.0)
        self.assertEqual(norm["pricing"]["output"], 0.0)
        self.assertEqual(norm["pricing"]["input_per_million"], 0.0)
        self.assertEqual(norm["pricing"]["cached_per_million"], 0.0)
        self.assertEqual(norm["pricing"]["output_per_million"], 0.0)

    def test_iss03_iss06_iss09_iss13_budget_gates_session_caps_strict_pricing_and_epsilon(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_file = Path(tmpdir) / "sess_gate.json"
            # 2 turns whose exact mean cost is 0.00051925 (triggers IEEE-754 drift in naive > check)
            trace_file.write_text(
                json.dumps({
                    "turns": [
                        {
                            "session_id": "s1",
                            "model_name": "gemini-3.8-flash",
                            "input_tokens": 300,
                            "cached_tokens": 110,
                            "output_tokens": 76,
                            "thinking_tokens": 0,
                            "latency_ms": 120,
                        },
                        {
                            "session_id": "s1",
                            "model_name": "gemini-3.8-flash",
                            "input_tokens": 300,
                            "cached_tokens": 110,
                            "output_tokens": 76,
                            "thinking_tokens": 0,
                            "latency_ms": 140,
                        },
                    ]
                }),
                encoding="utf-8",
            )
            # ISS-03: Zero threshold flags on non-empty trace fails closed (exit 1)
            buf0 = io.StringIO()
            with redirect_stdout(buf0):
                rc_zero = run_cli_assertions(traces_path=str(trace_file), output_format="json")
            self.assertEqual(rc_zero, 1)
            self.assertIn("No Budget Thresholds Configured", buf0.getvalue())

            # --allow-empty on missing path with zero flags still exits 0
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    run_cli_assertions(traces_path=str(Path(tmpdir) / "missing.json"), allow_empty=True, output_format="json"),
                    0,
                )

            # ISS-13: Missing explicit --traces path does NOT claim default dirs were checked
            buf_miss = io.StringIO()
            with redirect_stdout(buf_miss):
                rc_miss = run_cli_assertions(
                    traces_path=str(Path(tmpdir) / "typo_dir"),
                    max_cost_per_turn=0.01,
                    output_format="json",
                )
            self.assertEqual(rc_miss, 1)
            self.assertNotIn("./artifacts/traces/", buf_miss.getvalue())
            self.assertIn("Checked --traces", buf_miss.getvalue())

            # ISS-09: Exact float boundary equality passes cleanly
            exact_turn_cost = (300 * 0.75 + 110 * 0.075 + 76 * 3.75) / 1_000_000.0
            self.assertAlmostEqual(exact_turn_cost, 0.00051825, places=9)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    run_cli_assertions(
                        traces_path=str(trace_file),
                        max_cost_per_turn=exact_turn_cost,
                        max_session_cost=exact_turn_cost * 2,
                        max_session_tokens=972,
                        strict_pricing=True,
                        output_format="json",
                    ),
                    0,
                )

            # ISS-06: Session cost & session token breaches fail closed even when avg/turn is low
            buf_sess = io.StringIO()
            with redirect_stdout(buf_sess):
                rc_sess = run_cli_assertions(
                    traces_path=str(trace_file),
                    max_cost_per_turn=0.01,
                    max_session_cost=0.0008,
                    max_session_tokens=500,
                    output_format="json",
                )
            self.assertEqual(rc_sess, 1)
            sess_json = json.loads(buf_sess.getvalue())
            self.assertTrue(any("Session Cost Regression" in v for v in sess_json["violations"]))
            self.assertTrue(any("Session Context Bloat" in v for v in sess_json["violations"]))

            # ISS-01: --strict-pricing fails closed on unregistered models
            est_trace = Path(tmpdir) / "sess_unlisted.json"
            est_trace.write_text(
                json.dumps({
                    "turns": [
                        {
                            "session_id": "s_est",
                            "model_name": "mystery-unlisted-llm",
                            "input_tokens": 100,
                            "cached_tokens": 0,
                            "output_tokens": 20,
                        }
                    ]
                }),
                encoding="utf-8",
            )
            buf_strict = io.StringIO()
            with redirect_stdout(buf_strict):
                rc_strict = run_cli_assertions(
                    traces_path=str(est_trace),
                    strict_pricing=True,
                    output_format="json",
                )
            self.assertEqual(rc_strict, 1)
            strict_json = json.loads(buf_strict.getvalue())
            self.assertTrue(strict_json["pricing_estimated"])
            self.assertIn("mystery-unlisted-llm", strict_json["unregistered_models"])

            # Python assert_cost_under float epsilon check
            assert_cost_under({"total_cost": 0.0005192500000000001, "total_turns": 1}, max_dollars=0.00051925)

    def test_iss04_iss05_live_unopt_and_opt_profiler_parity_and_iss08_prompts_error(self) -> None:
        profiler = TokenProfiler()

        # 1. Replay live Vertex AI unoptimized 9-turn Argon session (Opus 3.3): ALL 5 rules must fire!
        live_unopt_turns = [
            {"session_id": "u1", "invocation_id": "i1", "sub_agent": "concierge_router", "model_name": "gemini-4-argon", "input_tokens": 8122, "cached_tokens": 0, "output_tokens": 66, "thinking_tokens": 217},
            {"session_id": "u1", "invocation_id": "i1", "sub_agent": "flight_hotel_finder", "model_name": "gemini-4-argon", "input_tokens": 475, "cached_tokens": 0, "output_tokens": 83, "thinking_tokens": 93},
            {"session_id": "u1", "invocation_id": "i1", "sub_agent": "flight_hotel_finder", "model_name": "gemini-4-argon", "input_tokens": 936, "cached_tokens": 0, "output_tokens": 389, "thinking_tokens": 47},
            {"session_id": "u1", "invocation_id": "i1", "sub_agent": "concierge_router", "model_name": "gemini-4-argon", "input_tokens": 2741, "cached_tokens": 6068, "output_tokens": 476, "thinking_tokens": 3314},
            {"session_id": "u1", "invocation_id": "i2", "sub_agent": "concierge_router", "model_name": "gemini-4-argon", "input_tokens": 3817, "cached_tokens": 8810, "output_tokens": 29, "thinking_tokens": 99},
            {"session_id": "u1", "invocation_id": "i2", "sub_agent": "itinerary_planner", "model_name": "gemini-4-argon", "input_tokens": 2046, "cached_tokens": 0, "output_tokens": 1741, "thinking_tokens": 619},
            {"session_id": "u1", "invocation_id": "i3", "sub_agent": "itinerary_planner", "model_name": "gemini-4-argon", "input_tokens": 4431, "cached_tokens": 0, "output_tokens": 29, "thinking_tokens": 1176},
            {"session_id": "u1", "invocation_id": "i4", "sub_agent": "concierge_router", "model_name": "gemini-4-argon", "input_tokens": 2293, "cached_tokens": 12628, "output_tokens": 1395, "thinking_tokens": 1100},
            {"session_id": "u1", "invocation_id": "i4", "sub_agent": "concierge_router", "model_name": "gemini-4-argon", "input_tokens": 2514, "cached_tokens": 14922, "output_tokens": 512, "thinking_tokens": 103},
        ]
        with redirect_stdout(io.StringIO()):
            unopt_recs = profiler.profile_session(live_unopt_turns, session_title="Live Unopt")
        joined_unopt = "\n".join(unopt_recs)
        self.assertIn("[Context Caching]", joined_unopt)
        self.assertIn("[History Compaction]", joined_unopt)
        self.assertIn("[Modular Skills]", joined_unopt)
        self.assertIn("[Thinking Budget Tuning]", joined_unopt)
        self.assertIn("[Sub-Agent Model Tiering]", joined_unopt)

        # 2. Replay live Vertex AI optimized 9-turn session (with App FinOps config metadata): 0 false-positive warnings!
        opt_meta = {
            "app_finops_config": {
                "context_cache_configured": True,
                "events_compaction_configured": True,
                "compaction_interval": 4,
                "skill_toolset_configured": True,
            }
        }
        live_opt_turns = [
            {"session_id": "o1", "invocation_id": "i1", "sub_agent": "concierge_router", "model_name": "gemini-3.8-flash", "input_tokens": 1019, "cached_tokens": 0, "output_tokens": 58, "thinking_tokens": 0, "tools_called": ["load_skill", "flight_hotel_finder"], "skills_active": ["japan-guide"], "raw_json": opt_meta},
            {"session_id": "o1", "invocation_id": "i1", "sub_agent": "flight_hotel_finder", "model_name": "gemini-3.8-flash", "input_tokens": 277, "cached_tokens": 0, "output_tokens": 68, "thinking_tokens": 0, "raw_json": opt_meta},
            {"session_id": "o1", "invocation_id": "i1", "sub_agent": "flight_hotel_finder", "model_name": "gemini-3.8-flash", "input_tokens": 499, "cached_tokens": 0, "output_tokens": 228, "thinking_tokens": 0, "raw_json": opt_meta},
            {"session_id": "o1", "invocation_id": "i1", "sub_agent": "concierge_router", "model_name": "gemini-3.8-flash", "input_tokens": 1529, "cached_tokens": 0, "output_tokens": 23, "thinking_tokens": 0, "raw_json": opt_meta},
            {"session_id": "o1", "invocation_id": "i1", "sub_agent": "itinerary_planner", "model_name": "gemini-4-argon", "input_tokens": 1607, "cached_tokens": 0, "output_tokens": 1185, "thinking_tokens": 0, "raw_json": opt_meta},
            {"session_id": "o1", "invocation_id": "i2", "sub_agent": "itinerary_planner", "model_name": "gemini-4-argon", "input_tokens": 2820, "cached_tokens": 0, "output_tokens": 1773, "thinking_tokens": 0, "raw_json": opt_meta},
            {"session_id": "o1", "invocation_id": "i3", "sub_agent": "itinerary_planner", "model_name": "gemini-4-argon", "input_tokens": 3016, "cached_tokens": 1603, "output_tokens": 29, "thinking_tokens": 0, "tools_called": ["transfer_to_agent"], "raw_json": opt_meta},
            {"session_id": "o1", "invocation_id": "i3", "sub_agent": "concierge_router", "model_name": "gemini-3.8-flash", "input_tokens": 5118, "cached_tokens": 0, "output_tokens": 753, "thinking_tokens": 0, "raw_json": opt_meta},
            {"session_id": "o1", "invocation_id": "i4", "sub_agent": "concierge_router", "model_name": "gemini-3.8-flash", "input_tokens": 10, "cached_tokens": 5872, "output_tokens": 365, "thinking_tokens": 0, "raw_json": opt_meta},
        ]
        with redirect_stdout(io.StringIO()):
            opt_recs = profiler.profile_session(live_opt_turns, session_title="Live Opt")
        self.assertEqual(opt_recs, [], f"Expected 0 warnings on configured live_opt_turns, got: {opt_recs}")

        # 3. ISS-08: Malformed --prompts-file blames prompts file, NOT trace file
        with tempfile.TemporaryDirectory() as tmpdir:
            valid_trace = Path(tmpdir) / "valid_trace.json"
            valid_trace.write_text(json.dumps({"turns": live_opt_turns[:1]}), encoding="utf-8")
            bad_prompts = Path(tmpdir) / "broken_prompts.json"
            bad_prompts.write_text("{bad json", encoding="utf-8")
            err_buf = io.StringIO()
            with redirect_stdout(err_buf):
                res = profile_from_trace_file(
                    str(valid_trace),
                    prompts_config_path=str(bad_prompts),
                    output_format="json",
                )
            self.assertIsNone(res)
            err_payload = json.loads(err_buf.getvalue())
            self.assertIn("broken_prompts.json", err_payload["error"])
            self.assertNotIn("Failed to profile trace file", err_payload["error"])

    def test_iss11_iss16_iss17_compound_sequential_stage_record_turn_defaults_and_redaction(self) -> None:
        # ISS-11: SequentialAgent parent preserved alongside Parallel / Loop
        seq_parent = type("SequentialAgent", (), {"name": "seq_root", "parent_agent": None})()
        par_parent = type("ParallelAgent", (), {"name": "par_stage", "parent_agent": seq_parent})()
        leaf_worker = type("LlmAgent", (), {"name": "flight_branch", "parent_agent": par_parent})()
        ctx = type("Ctx", (), {"agent": leaf_worker, "agent_name": "flight_branch"})()
        self.assertEqual(
            ADKTokenomicsPlugin._extract_orchestration_flow(ctx),
            "Sequential • Parallel",
        )

        # ISS-16: README TokenomicsTestCase keyword-only record_turn works without user_query / agent_response
        with self.track_tokens() as tracker:
            rec = tracker.record_turn(
                session_id="test_s1",
                app_name="router",
                model_name="gemini-3.8-flash",
                input_tokens=400,
                cached_tokens=1600,
                output_tokens=120,
            )
            self.assertEqual(rec["user_query"], "")
            self.assertEqual(rec["agent_response"], "")
            self.assertCostLessThan(tracker, max_dollars=0.005)

        # ISS-17: redact_content=True / TOKENOMICS_REDACT_CONTENT=1 scrubs prompt & response text
        with patch.dict(os.environ, {"TOKENOMICS_REDACT_CONTENT": "1"}, clear=False):
            tower = TokenControlTower(mode="dev")
            plugin = ADKTokenomicsPlugin(tracker=tower.tracker, app_name="secret_app")
            self.assertTrue(plugin.redact_content)
            sim_ctx = type(
                "SimCtx",
                (),
                {
                    "session_id": "s_sec",
                    "invocation_id": "inv_sec",
                    "agent_name": "vault_agent",
                    "agent": leaf_worker,
                    "user_content": "SSN 123-45-6789 secret query",
                },
            )()
            plugin.before_model_callback(callback_context=sim_ctx, llm_request=None)
            sim_usage = type(
                "U",
                (),
                {
                    "prompt_token_count": 500,
                    "cached_content_token_count": 0,
                    "candidates_token_count": 50,
                    "thoughts_token_count": 0,
                },
            )()
            sim_part = type("P", (), {"text": "Secret customer PII response", "function_call": None})()
            sim_cand = type("C", (), {"content": type("Cnt", (), {"parts": [sim_part]})()})()
            sim_resp = type("R", (), {"model_version": "gemini-3.8-flash", "usage_metadata": sim_usage, "candidates": [sim_cand], "partial": False})()
            plugin.after_model_callback(callback_context=sim_ctx, llm_response=sim_resp)
            turn = tower.tracker.turns_history[-1]
            self.assertNotIn("123-45-6789", turn["user_query"])
            self.assertIn("[REDACTED]", turn["user_query"])
            self.assertEqual(turn["agent_response"], "[REDACTED]")
            self.assertEqual(turn["input_tokens"], 500)

    def test_iss07_iss10_iss12_iss14_iss15_cli_web_port_prog_and_py310_floor(self) -> None:
        # ISS-12: eval.budget --prog-override produces `usage: agents-cli eval budget`
        out_h = io.StringIO()
        with patch.dict(os.environ, {"AGENTS_CLI_EXTENSION_DIR": str(REPO_ROOT)}, clear=False), redirect_stdout(out_h):
            rc_h = launcher.run(["--prog-override", "agents-cli eval budget", "assert", "-h"])
        self.assertEqual(rc_h, 0)
        self.assertIn("usage: agents-cli eval budget", out_h.getvalue())

        # ISS-10: TOKENOMICS_ALLOWED_HOSTS='*' fails fast at startup
        with patch.dict(os.environ, {"TOKENOMICS_ALLOWED_HOSTS": "*"}, clear=False):
            with self.assertRaises(ValueError):
                run_web_server(port=0, host="127.0.0.1", open_browser=False)

        # ISS-15: Explicit --port N fails closed (exit 1) when port N is already bound
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            busy_port = sock.getsockname()[1]
            proc = subprocess.run(
                [sys.executable, "-m", "adk_tokenomics_plugin", "web", "--port", str(busy_port), "--no-browser"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("already in use", proc.stderr)

        # ISS-07: Nonexistent explicit .json file fails fast (exit 1) on `web --traces`
        proc_bad_json = subprocess.run(
            [sys.executable, "-m", "adk_tokenomics_plugin", "web", "--traces", "/tmp/definitely_missing_uat_999.json", "--no-browser"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(proc_bad_json.returncode, 1)
        self.assertIn("Trace file not found", proc_bad_json.stderr)

        # ISS-14: Verify zero Python 3.11+ StrEnum imports in adk_tokenomics_plugin/ or examples/
        for sub in ("adk_tokenomics_plugin", "examples", "scripts"):
            for py_path in (REPO_ROOT / sub).rglob("*.py"):
                tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module == "enum":
                        imported = {alias.name for alias in node.names}
                        self.assertNotIn("StrEnum", imported, f"Python 3.11+ StrEnum found in {py_path}")


if __name__ == "__main__":
    unittest.main()
