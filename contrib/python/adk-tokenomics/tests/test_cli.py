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

# ruff: noqa: PLW1510, S103
"""
Unit and Break-It regression tests for the adk_tokenomics_plugin CLI, FinOps, Assertions, and Packaging suite.
"""

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest.mock import MagicMock, patch

from adk_tokenomics_plugin.cli import (
    detect_default_gcp_project,
    find_available_port,
    get_default_cloud_run_region,
    is_port_in_use,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUBPROCESS_ENV = {
    **os.environ,
    "PYTHONPATH": REPO_ROOT
    + (
        os.pathsep + os.environ["PYTHONPATH"]
        if os.environ.get("PYTHONPATH")
        else ""
    ),
    "PYTHONDONTWRITEBYTECODE": "1",
}
_MISSING = object()


@contextmanager
def _scoped_fake_bigquery(client_cls=None):
    """
    Installs a test-scoped fake google.cloud.bigquery + google.cloud.exceptions into sys.modules
    without polluting global sys.modules outside the context block.
    """
    try:
        import google as google_pkg  # type: ignore
    except ImportError:
        google_pkg = types.ModuleType("google")

    prev_cloud_attr = getattr(google_pkg, "cloud", _MISSING)
    cloud_mod = sys.modules.get("google.cloud") or types.ModuleType(
        "google.cloud"
    )
    prev_bq_attr = getattr(cloud_mod, "bigquery", _MISSING)
    prev_exc_attr = getattr(cloud_mod, "exceptions", _MISSING)

    bq_mod = types.ModuleType("google.cloud.bigquery")
    exc_mod = types.ModuleType("google.cloud.exceptions")

    class FakeNotFound(Exception):
        pass

    class FakeBadRequest(Exception):
        pass

    class FakeQueryJobConfig:
        def __init__(self, *args, **kwargs):
            self.query_parameters = kwargs.get("query_parameters", [])

    class FakeScalarQueryParameter:
        def __init__(self, name, type_, value):
            self.name = name
            self.type_ = type_
            self.value = value

    class FakeDataset:
        def __init__(self, dataset_ref):
            self.dataset_ref = dataset_ref
            self.location = None

    bq_mod.Client = client_cls if client_cls is not None else MagicMock()  # type: ignore[attr-defined]
    bq_mod.QueryJobConfig = FakeQueryJobConfig  # type: ignore[attr-defined]
    bq_mod.ScalarQueryParameter = FakeScalarQueryParameter  # type: ignore[attr-defined]
    bq_mod.Dataset = FakeDataset  # type: ignore[attr-defined]
    exc_mod.NotFound = FakeNotFound  # type: ignore[attr-defined]
    exc_mod.BadRequest = FakeBadRequest  # type: ignore[attr-defined]

    google_pkg.cloud = cloud_mod  # type: ignore[attr-defined]
    cloud_mod.bigquery = bq_mod  # type: ignore[attr-defined]
    cloud_mod.exceptions = exc_mod  # type: ignore[attr-defined]

    try:
        with patch.dict(
            sys.modules,
            {
                "google": google_pkg,
                "google.cloud": cloud_mod,
                "google.cloud.bigquery": bq_mod,
                "google.cloud.exceptions": exc_mod,
            },
        ):
            yield bq_mod, exc_mod
    finally:
        if prev_bq_attr is _MISSING:
            if hasattr(cloud_mod, "bigquery"):
                delattr(cloud_mod, "bigquery")
        else:
            cloud_mod.bigquery = prev_bq_attr  # type: ignore[attr-defined]

        if prev_exc_attr is _MISSING:
            if hasattr(cloud_mod, "exceptions"):
                delattr(cloud_mod, "exceptions")
        else:
            cloud_mod.exceptions = prev_exc_attr  # type: ignore[attr-defined]

        if prev_cloud_attr is _MISSING:
            if hasattr(google_pkg, "cloud"):
                delattr(google_pkg, "cloud")
        else:
            google_pkg.cloud = prev_cloud_attr  # type: ignore[attr-defined]


class TestTokenomicsCLI(unittest.TestCase):
    @patch.dict("os.environ", {"GOOGLE_CLOUD_PROJECT": "test-gcp-project-123"})
    def test_detect_default_gcp_project_env(self):
        project = detect_default_gcp_project()
        self.assertEqual(project, "test-gcp-project-123")

    @patch.dict("os.environ", {}, clear=True)
    def test_detect_default_gcp_project_client(self):
        instantiated = []

        class _ClientWithProject:
            def __init__(self):
                instantiated.append(True)
                self.project = "mock-bq-project-456"

        with _scoped_fake_bigquery(_ClientWithProject):
            project = detect_default_gcp_project()
        self.assertEqual(project, "mock-bq-project-456")
        self.assertEqual(len(instantiated), 1)

        # When google.cloud.bigquery is unavailable and google.auth is unavailable, returns None cleanly
        with patch.dict(
            sys.modules, {"google.cloud.bigquery": None, "google.auth": None}
        ):
            self.assertIsNone(detect_default_gcp_project())

    def test_find_available_port(self):
        # Bind a real ephemeral loopback socket so start_port is genuinely occupied
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied_sock:
            occupied_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied_sock.bind(("127.0.0.1", 0))
            occupied_sock.listen(1)
            occupied_port = occupied_sock.getsockname()[1]

            self.assertTrue(is_port_in_use(occupied_port, "127.0.0.1"))
            resolved = find_available_port(
                occupied_port, "127.0.0.1", max_tries=15
            )
            self.assertNotEqual(resolved, occupied_port)
            self.assertGreater(resolved, occupied_port)
            self.assertFalse(is_port_in_use(resolved, "127.0.0.1"))

    def test_web_handler_local_trace_schema(self):
        from adk_tokenomics_plugin.cli_web import TokenomicsWebHandler

        sample_trace = {
            "turns": [
                {
                    "timestamp": "2026-09-29T02:27:29Z",
                    "session_id": "sess-delhi-01",
                    "app_name": "travel_planner_agent",
                    "model_name": "gemini-3.8-flash",
                    "user_query": "[concierge_router] Plan a 5-day trip to New Delhi",
                    "agent_response": "Normalized trip brief.",
                    "input_tokens": 440,
                    "cached_tokens": 0,
                    "output_tokens": 62,
                    "thinking_tokens": 131,
                    "total_cost": 0.0006145,
                    "tools_called": ["save_trip_state"],
                    "raw_json": {"sub_agent": "concierge_router"},
                }
            ]
        }
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False
        ) as tf:
            json.dump(sample_trace, tf)
            trace_file = tf.name

        try:
            TokenomicsWebHandler.set_server_context(
                project_id="", traces_path=trace_file
            )
            dummy = object.__new__(TokenomicsWebHandler)
            sessions = dummy.fetch_sessions()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["session_id"], "sess-delhi-01")
            self.assertIn("start_time", sessions[0])
            self.assertIn("last_active_time", sessions[0])
            self.assertEqual(sessions[0]["turn_count"], 1)
            self.assertEqual(
                sessions[0]["first_query"], "Plan a 5-day trip to New Delhi"
            )

            latest_resp = dummy.fetch_metrics("latest")
            self.assertEqual(latest_resp["active_session_id"], "sess-delhi-01")
            self.assertEqual(latest_resp["total_turns"], 1)

            metrics_resp = dummy.fetch_metrics("global")
            self.assertEqual(metrics_resp["source"], "local_traces")
            self.assertEqual(metrics_resp["total_turns"], 1)
            self.assertIn("metrics", metrics_resp)
            self.assertIn("concierge_router", metrics_resp["metrics"])
            self.assertEqual(
                metrics_resp["metrics"]["concierge_router"]["turns"], 1
            )
            self.assertIn("save_trip_state", metrics_resp["tool_counts"])
        finally:
            os.remove(trace_file)

    def test_models_registry_crud_and_live_recalculation(self):
        from adk_tokenomics_plugin import cli_web
        from adk_tokenomics_plugin.cli_profiler import (
            TokenProfiler,
            parse_trace_or_eval_payload,
        )
        from adk_tokenomics_plugin.cli_web import TokenomicsWebHandler
        from adk_tokenomics_plugin.testing.assertions import run_cli_assertions

        with tempfile.TemporaryDirectory() as tmp_web_dir:
            real_cfg = cli_web.get_models_config_path()
            if os.path.exists(real_cfg):
                shutil.copy(
                    real_cfg, os.path.join(tmp_web_dir, "models_config.json")
                )
            with patch.object(cli_web, "WEB_DIR", tmp_web_dir):
                dummy = object.__new__(TokenomicsWebHandler)
                upserted = dummy.upsert_model_entry(
                    {
                        "id": "gemini-3.8-flash-custom-test",
                        "name": "Gemini 3.8 Flash Custom Test",
                        "input_rate": 2.0,
                        "cached_rate": 0.2,
                        "output_rate": 10.0,
                        "thinking_rate": 10.0,
                    }
                )
                ids = [m["id"] for m in upserted["models"]]
                self.assertIn("gemini-3.8-flash-custom-test", ids)
                self.assertIn(
                    "gemini-3.8-flash-custom-test",
                    TokenomicsWebHandler.pricing.pricing_table,
                )

                sample_trace = {
                    "turns": [
                        {
                            "timestamp": "2026-10-01T02:55:00Z",
                            "session_id": "sess-rate-test",
                            "app_name": "travel_planner_agent",
                            "model_name": "gemini-3.8-flash-custom-test",
                            "user_query": "[concierge_router] Test custom rate",
                            "agent_response": "Done.",
                            "input_tokens": 1_000_000,
                            "cached_tokens": 0,
                            "output_tokens": 100_000,
                            "thinking_tokens": 0,
                            "total_cost": 0.0001,
                        }
                    ]
                }
                trace_file = os.path.join(tmp_web_dir, "trace.json")
                with open(trace_file, "w", encoding="utf-8") as tf:
                    json.dump(sample_trace, tf)

                TokenomicsWebHandler.set_server_context(
                    project_id="", traces_path=trace_file
                )
                metrics_resp = dummy.fetch_metrics("sess-rate-test")
                self.assertAlmostEqual(
                    metrics_resp["turns"][0]["total_cost"], 3.0, places=4
                )

                parsed = parse_trace_or_eval_payload(sample_trace)
                captured_costs = []
                profiler = TokenProfiler(
                    custom_callbacks=[
                        lambda ctx: (
                            captured_costs.append(ctx["total_cost"]) or None
                        )
                    ]
                )
                with redirect_stdout(io.StringIO()):
                    profiler.profile_session(
                        parsed, session_title="Rate Card Consistency Check"
                    )
                self.assertAlmostEqual(captured_costs[0], 3.0, places=4)
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(
                        run_cli_assertions(
                            traces_path=trace_file,
                            max_cost_per_turn=1.0,
                            output_format="json",
                        ),
                        1,
                    )
                    self.assertEqual(
                        run_cli_assertions(
                            traces_path=trace_file,
                            max_cost_per_turn=3.5,
                            output_format="json",
                        ),
                        0,
                    )

                after_delete = dummy.delete_model_entry(
                    "gemini-3.8-flash-custom-test"
                )
                ids_after = [m["id"] for m in after_delete["models"]]
                self.assertNotIn("gemini-3.8-flash-custom-test", ids_after)

    def test_assert_fail_closed_and_adk_evalset_result_support(self):
        from adk_tokenomics_plugin.cli_profiler import profile_from_trace_file
        from adk_tokenomics_plugin.testing.assertions import run_cli_assertions

        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                run_cli_assertions(
                    traces_path="/tmp/non_existent_trace_99999.json",
                    output_format="json",
                ),
                1,
            )
            self.assertEqual(
                run_cli_assertions(
                    traces_path="/tmp/non_existent_trace_99999.json",
                    allow_empty=True,
                    output_format="json",
                ),
                0,
            )

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False
        ) as bad_f:
            json.dump({"unrelated_key": 123}, bad_f)
            bad_path = bad_f.name
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    run_cli_assertions(
                        traces_path=bad_path, output_format="json"
                    ),
                    1,
                )
        finally:
            os.remove(bad_path)

        adk_evalset_result = {
            "eval_set_result_id": "eval_res_001",
            "eval_set_id": "travel_eval_set",
            "eval_case_results": [
                {
                    "eval_id": "case_tokyo",
                    "session_id": "adk-eval-session-111",
                    "final_eval_status": 1,
                    "eval_metric_result_per_invocation": [
                        {
                            "actual_invocation": {
                                "invocation_id": "inv_eval_1",
                                "user_content": {
                                    "parts": [
                                        {"text": "Plan a 3-day Tokyo itinerary"}
                                    ]
                                },
                                "duration": 1.25,
                                "intermediate_data": {
                                    "invocation_events": [
                                        {
                                            "author": "concierge_router",
                                            "model_version": "publishers/google/models/gemini-3.8-flash",
                                            "content": {
                                                "parts": [
                                                    {
                                                        "text": "Routing to researcher"
                                                    }
                                                ]
                                            },
                                            "usage_metadata": {
                                                "prompt_token_count": 6000,
                                                "cached_content_token_count": 1000,
                                                "tool_use_prompt_token_count": 200,
                                                "candidates_token_count": 300,
                                                "thoughts_token_count": 600,
                                            },
                                        }
                                    ]
                                },
                            }
                        }
                    ],
                }
            ],
        }
        with tempfile.NamedTemporaryFile(
            "w", suffix=".evalset_result.json", delete=False
        ) as ef:
            json.dump(adk_evalset_result, ef)
            eval_path = ef.name
        try:
            with redirect_stdout(io.StringIO()):
                code_pass = run_cli_assertions(
                    traces_path=eval_path,
                    max_cost_per_turn=0.05,
                    min_cache_rate=0.10,
                    max_tokens_per_turn=10000,
                    max_latency_ms=5000,
                    output_format="json",
                )
                self.assertEqual(code_pass, 0)

                code_fail = run_cli_assertions(
                    traces_path=eval_path,
                    min_cache_rate=0.80,
                    output_format="json",
                )
                self.assertEqual(code_fail, 1)

                recs = profile_from_trace_file(eval_path, output_format="json")
            self.assertIsNotNone(recs)
            assert recs is not None
            self.assertTrue(any("load_skill" in r for r in recs))
            self.assertTrue(
                any(
                    "Thinking Budget Tuning" in r and "est. savings: $" in r
                    for r in recs
                )
            )
        finally:
            os.remove(eval_path)

    def test_latency_gate_fails_closed_and_web_does_not_fabricate_latency(self):
        from adk_tokenomics_plugin.cli_web import TokenomicsWebHandler
        from adk_tokenomics_plugin.testing.assertions import run_cli_assertions

        def write_trace(dir_path, latency_ms):
            path = os.path.join(dir_path, "trace.json")
            with open(path, "w") as f:
                json.dump(
                    {
                        "session_id": "lat",
                        "turns": [
                            {
                                "session_id": "lat",
                                "input_tokens": 100,
                                "cached_tokens": 0,
                                "output_tokens": 10,
                                "thinking_tokens": 0,
                                "total_tokens": 110,
                                "latency_ms": latency_ms,
                                "model_name": "publishers/google/models/gemini-3.8-flash",
                                "user_query": "q",
                            }
                        ],
                    },
                    f,
                )
            return path

        with tempfile.TemporaryDirectory() as no_lat_dir:
            write_trace(no_lat_dir, 0)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    run_cli_assertions(
                        traces_path=no_lat_dir,
                        max_latency_ms=5000,
                        output_format="json",
                    ),
                    1,
                )
                self.assertEqual(
                    run_cli_assertions(
                        traces_path=no_lat_dir,
                        max_cost_per_turn=1.0,
                        output_format="json",
                    ),
                    0,
                )

            TokenomicsWebHandler.set_server_context(
                project_id="", traces_path=no_lat_dir
            )
            handler = object.__new__(TokenomicsWebHandler)
            turns = handler._load_local_trace_turns()
            self.assertEqual(turns[0]["latency_ms"], 0)

        with tempfile.TemporaryDirectory() as lat_dir:
            write_trace(lat_dir, 900)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    run_cli_assertions(
                        traces_path=lat_dir,
                        max_latency_ms=5000,
                        output_format="json",
                    ),
                    0,
                )
                self.assertEqual(
                    run_cli_assertions(
                        traces_path=lat_dir,
                        max_latency_ms=500,
                        output_format="json",
                    ),
                    1,
                )

        # Dict-valued and float-string latency_ms shapes must also parse cleanly
        with tempfile.TemporaryDirectory() as dict_lat_dir:
            write_trace(dict_lat_dir, {"total_ms": 900})
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    run_cli_assertions(
                        traces_path=dict_lat_dir,
                        max_latency_ms=1000,
                        output_format="json",
                    ),
                    0,
                )
            write_trace(dict_lat_dir, "12.5")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    run_cli_assertions(
                        traces_path=dict_lat_dir,
                        max_latency_ms=100,
                        output_format="json",
                    ),
                    0,
                )

    def test_post_exit_rotated_live_session_fallback(self):
        from adk_tokenomics_plugin.cli_profiler import profile_from_trace_file
        from adk_tokenomics_plugin.cli_web import TokenomicsWebHandler
        from adk_tokenomics_plugin.testing.assertions import run_cli_assertions

        with tempfile.TemporaryDirectory() as trace_dir:
            rotated_path = os.path.join(trace_dir, "sess_rotated_001.json")
            with open(rotated_path, "w") as f:
                json.dump(
                    {
                        "session_id": "sess_rotated_001",
                        "turns": [
                            {
                                "session_id": "sess_rotated_001",
                                "input_tokens": 200,
                                "cached_tokens": 800,
                                "output_tokens": 50,
                                "thinking_tokens": 0,
                                "total_tokens": 1050,
                                "latency_ms": 300,
                                "model_name": "publishers/google/models/gemini-3.8-flash",
                                "user_query": "Test rotated fallback",
                            }
                        ],
                    },
                    f,
                )
            for candidate_name in (
                "live_adk_session.json",
                "session.json",
                "ci_eval.json",
            ):
                missing_live_path = os.path.join(trace_dir, candidate_name)
                with redirect_stdout(io.StringIO()):
                    recs = profile_from_trace_file(
                        missing_live_path, output_format="json"
                    )
                    self.assertIsNotNone(recs)
                    self.assertEqual(
                        run_cli_assertions(
                            traces_path=missing_live_path,
                            max_cost_per_turn=0.05,
                            min_cache_rate=0.70,
                            output_format="json",
                        ),
                        0,
                    )
                TokenomicsWebHandler.set_server_context(
                    project_id="", traces_path=missing_live_path
                )
                handler = object.__new__(TokenomicsWebHandler)
                turns = handler._load_local_trace_turns()
                self.assertEqual(len(turns), 1)
                self.assertEqual(turns[0]["session_id"], "sess_rotated_001")

    # =========================================================================
    # CHAMP Round 1 Break-It Regression Suite (P0-1..3 & P1-1..12)
    # =========================================================================

    def test_assert_rejects_nan_and_out_of_range_thresholds(self):
        from adk_tokenomics_plugin.testing.assertions import (
            assert_cache_ratio_above,
            assert_cost_under,
            assert_thinking_budget,
            assert_token_budget,
            run_cli_assertions,
        )

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False
        ) as tf:
            json.dump(
                {
                    "turns": [
                        {
                            "user_query": "expensive uncached turn",
                            "prompt_tokens": 900_000,
                            "cached_tokens": 0,
                            "output_tokens": 90_000,
                            "latency_ms": 99_999,
                            "model_name": "publishers/google/models/gemini-3.8-flash",
                        }
                    ]
                },
                tf,
            )
            exp_path = tf.name

        try:
            invalid_cases = [
                {"max_cost_per_turn": float("nan")},
                {"max_cost_per_turn": float("inf")},
                {"max_cost_per_turn": -0.01},
                {"min_cache_rate": float("nan")},
                {"min_cache_rate": float("inf")},
                {"min_cache_rate": -5.0},
                {"min_cache_rate": 60.0},
                {"max_tokens_per_turn": -1},
                {"max_latency_ms": -10},
            ]
            for kwargs in invalid_cases:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = run_cli_assertions(
                        traces_path=exp_path, output_format="json", **kwargs
                    )
                self.assertEqual(rc, 1, f"Expected exit 1 for {kwargs}")
                payload = json.loads(buf.getvalue())
                self.assertFalse(payload["passed"])
                self.assertTrue(
                    any("Invalid Threshold" in v for v in payload["violations"])
                )

            # CLI subprocess verification
            for flag_pair in (
                ["--min-cache-rate", "nan"],
                ["--max-cost-per-turn", "nan"],
                ["--max-cost-per-turn", "inf"],
                ["--min-cache-rate", "-5"],
                ["--min-cache-rate", "60"],
            ):
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "adk_tokenomics_plugin",
                        "assert",
                        "-t",
                        exp_path,
                        *flag_pair,
                    ],
                    env=SUBPROCESS_ENV,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertNotEqual(
                    proc.returncode, 0, f"CLI should reject {flag_pair}"
                )

            # Negative token counts in trace artifact must fail closed rather than canceling positive turns
            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False
            ) as neg_f:
                json.dump(
                    {
                        "turns": [
                            {
                                "user_query": "q1",
                                "prompt_tokens": 900_000,
                                "output_tokens": 90_000,
                            },
                            {
                                "user_query": "q2",
                                "prompt_tokens": -900_000,
                                "output_tokens": -90_000,
                            },
                        ]
                    },
                    neg_f,
                )
                neg_path = neg_f.name
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(
                        run_cli_assertions(
                            traces_path=neg_path,
                            max_cost_per_turn=0.0001,
                            output_format="json",
                        ),
                        1,
                    )
                self.assertFalse(json.loads(buf.getvalue())["passed"])
            finally:
                os.remove(neg_path)

            # SDK helpers must also reject NaN/Inf thresholds and empty summary dicts
            valid_summary = {
                "total_turns": 1,
                "total_cost": 0.01,
                "cache_read_ratio_pct": 50.0,
                "total_tokens": 1000,
                "thinking_tokens": 100,
            }
            for bad_val in (float("nan"), float("inf"), -1.0):
                with self.assertRaises((AssertionError, ValueError)):
                    assert_cost_under(valid_summary, bad_val)
                with self.assertRaises((AssertionError, ValueError)):
                    assert_cache_ratio_above(valid_summary, bad_val)
                with self.assertRaises((AssertionError, ValueError)):
                    assert_token_budget(valid_summary, bad_val)  # type: ignore[arg-type]
                with self.assertRaises((AssertionError, ValueError)):
                    assert_thinking_budget(valid_summary, max_tokens=bad_val)  # type: ignore[arg-type]

            for fn, arg in (
                (assert_cost_under, 1.0),
                (assert_cache_ratio_above, 0.0),
                (assert_token_budget, 1000),
                (assert_thinking_budget, 0),
            ):
                with self.assertRaises(AssertionError):
                    fn({}, arg)
        finally:
            os.remove(exp_path)

    def test_bigquery_identifiers_rejected_before_query(self):
        from adk_tokenomics_plugin.cli_finops import run_finops_summary_cli
        from adk_tokenomics_plugin.cli_profiler import profile_from_bigquery
        from adk_tokenomics_plugin.finops.bq_schema import (
            provision_bigquery_table,
        )

        captured_queries = []

        class CapturingClient:
            def __init__(self, project=None):
                self.project = project

            def create_dataset(self, *args, **kwargs):
                captured_queries.append(("create_dataset", args))

            def query(self, sql, job_config=None):
                captured_queries.append(("query", sql))
                raise AssertionError(
                    "client.query must NEVER be reached on invalid identifiers!"
                )

        sqli_dataset = "d`; DROP TABLE `victim.ds.adk_agent_events`; --"
        sqli_view = "v`; DROP TABLE `victim.ds.adk_agent_events`; --"
        sqli_project = "proj`; DELETE FROM x WHERE TRUE; --"

        with _scoped_fake_bigquery(CapturingClient):
            # 1. profile_from_bigquery
            buf = io.StringIO()
            with redirect_stdout(buf):
                res = profile_from_bigquery(
                    session_id="sess_1",
                    project_id="valid-gcp-proj",
                    dataset_id=sqli_dataset,
                    output_format="json",
                )
            self.assertIsNone(res)
            self.assertIn("error", json.loads(buf.getvalue()))
            self.assertEqual(captured_queries, [])

            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                res2 = profile_from_bigquery(
                    session_id="sess_1",
                    project_id=sqli_project,
                    dataset_id="bq_adk_ds",
                    output_format="json",
                )
            self.assertIsNone(res2)
            self.assertEqual(captured_queries, [])

            # 2. run_finops_summary_cli
            buf3 = io.StringIO()
            with redirect_stdout(buf3):
                fin_rc = run_finops_summary_cli(
                    project_id="valid-gcp-proj",
                    dataset_id="bq_adk_ds",
                    view_id=sqli_view,
                    output_format="json",
                )
            self.assertEqual(fin_rc, 1)
            self.assertIn("error", json.loads(buf3.getvalue()))
            self.assertEqual(captured_queries, [])

            # Invalid --days / --limit ranges must also exit 1 before querying
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    run_finops_summary_cli(project_id="valid-gcp-proj", days=0),
                    1,
                )
                self.assertEqual(
                    run_finops_summary_cli(
                        project_id="valid-gcp-proj", limit_agents=-2
                    ),
                    1,
                )
            self.assertEqual(captured_queries, [])

            # 3. provision_bigquery_table
            with redirect_stdout(io.StringIO()):
                ok = provision_bigquery_table(
                    project_id="valid-gcp-proj",
                    dataset_id=sqli_dataset,
                )
            self.assertFalse(ok)
            self.assertEqual(captured_queries, [])

        # 4. CLI subprocess with injected dataset exits non-zero with valid JSON error
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "adk_tokenomics_plugin",
                "profile",
                "-s",
                "sess_1",
                "-p",
                "valid-gcp-proj",
                "-d",
                sqli_dataset,
                "--format",
                "json",
            ],
            env=SUBPROCESS_ENV,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("error", json.loads(proc.stdout))

    def test_deploy_cloud_run_never_sets_reserved_port_env(self):
        from adk_tokenomics_plugin.cli_deploy import deploy_cloud_run

        captured_cmds = []

        def fake_run(cmd, check=False, **kwargs):
            captured_cmds.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0)

        with (
            patch("shutil.which", return_value="/bin/gcloud"),
            patch("subprocess.run", side_effect=fake_run),
        ):
            with redirect_stdout(io.StringIO()):
                rc = deploy_cloud_run(
                    project="my-gcp-project",
                    region="us-central1",
                    port=8080,
                    source_dir=REPO_ROOT,
                )
            self.assertEqual(rc, 0)
            self.assertEqual(len(captured_cmds), 1)
            cmd = captured_cmds[0]
            self.assertIn("--port", cmd)
            self.assertEqual(cmd[cmd.index("--port") + 1], "8080")
            sev = cmd[cmd.index("--set-env-vars") + 1]
            self.assertNotIn("PORT=", sev)
            self.assertIn("TOKENOMICS_ALLOWED_HOSTS=*.run.app", sev)

            # User --env_vars override for TOKENOMICS_ALLOWED_HOSTS must replace default without duplicating
            captured_cmds.clear()
            with redirect_stdout(io.StringIO()):
                rc_custom_hosts = deploy_cloud_run(
                    project="my-gcp-project",
                    region="us-central1",
                    env_vars="TOKENOMICS_ALLOWED_HOSTS=tower.corp.example.com",
                    source_dir=REPO_ROOT,
                )
            self.assertEqual(rc_custom_hosts, 0)
            sev_custom = captured_cmds[0][
                captured_cmds[0].index("--set-env-vars") + 1
            ]
            self.assertIn(
                "TOKENOMICS_ALLOWED_HOSTS=tower.corp.example.com", sev_custom
            )
            self.assertNotIn("*.run.app", sev_custom)
            self.assertEqual(sev_custom.count("TOKENOMICS_ALLOWED_HOSTS="), 1)

            # User --env_vars with comma inside value must use gcloud custom delimiter without splitting
            captured_cmds.clear()
            with redirect_stdout(io.StringIO()):
                rc_comma = deploy_cloud_run(
                    project="my-gcp-project",
                    region="us-central1",
                    env_vars="A=1,B=x,y",
                    source_dir=REPO_ROOT,
                )
            self.assertEqual(rc_comma, 0)
            sev_comma = captured_cmds[0][
                captured_cmds[0].index("--set-env-vars") + 1
            ]
            self.assertTrue(
                sev_comma.startswith("^@^"),
                f"Expected ^@^ delimiter, got {sev_comma!r}",
            )
            self.assertIn("B=x,y", sev_comma)
            self.assertNotIn("PORT=", sev_comma)

            # Reserved Cloud Run env vars in --env_vars must be rejected before subprocess.run
            captured_cmds.clear()
            for reserved_key in (
                "PORT",
                "K_SERVICE",
                "K_REVISION",
                "K_CONFIGURATION",
            ):
                err_buf = io.StringIO()
                with patch("sys.stderr", err_buf):
                    rc_bad = deploy_cloud_run(
                        project="my-gcp-project",
                        region="us-central1",
                        env_vars=f"{reserved_key}=bad",
                        source_dir=REPO_ROOT,
                    )
                self.assertEqual(rc_bad, 1)
            self.assertEqual(captured_cmds, [])

            # Invalid --service_name must be rejected
            with patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    deploy_cloud_run(
                        project="my-gcp-project",
                        service_name="INVALID_SERVICE!",
                        source_dir=REPO_ROOT,
                    ),
                    1,
                )
            self.assertEqual(captured_cmds, [])

        # Bare 'adk-tokenomics deploy' and non-deploy '--' passthrough must exit non-zero
        bare_deploy = subprocess.run(
            [sys.executable, "-m", "adk_tokenomics_plugin", "deploy"],
            env=SUBPROCESS_ENV,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(bare_deploy.returncode, 0)

        bad_passthrough = subprocess.run(
            [
                sys.executable,
                "-m",
                "adk_tokenomics_plugin",
                "assert",
                "--allow-empty",
                "--",
                "--unknown-flag",
            ],
            env=SUBPROCESS_ENV,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(bad_passthrough.returncode, 0)

    def test_profile_autodetect_picks_newest_mtime(self):
        import glob as _glob

        with tempfile.TemporaryDirectory() as tmp_cwd:
            traces_dir = os.path.join(tmp_cwd, "artifacts", "traces")
            os.makedirs(traces_dir)
            for idx in range(6):
                p = os.path.join(traces_dir, f"sess_{idx}.json")
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "turns": [
                                {
                                    "user_query": f"q_{idx}",
                                    "prompt_tokens": 100,
                                    "output_tokens": 10,
                                }
                            ]
                        },
                        f,
                    )

            raw_order = _glob.glob(os.path.join(traces_dir, "*.json"))
            now = time.time()
            # Give the last item in glob order the OLDEST mtime, and the first item the NEWEST mtime
            for k, path in enumerate(raw_order):
                age = 1000 * (k + 1)
                os.utime(path, (now - age, now - age))
            expected_newest = os.path.basename(raw_order[0])

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "adk_tokenomics_plugin",
                    "profile",
                    "--format",
                    "json",
                ],
                cwd=tmp_cwd,
                env=SUBPROCESS_ENV,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertTrue(
                payload["file"].endswith(expected_newest),
                f"Expected newest file {expected_newest}, got {payload['file']}",
            )

    def test_rule5_flags_thinking_with_zero_output(self):
        from adk_tokenomics_plugin.cli_profiler import TokenProfiler

        profiler = TokenProfiler()
        with redirect_stdout(io.StringIO()):
            recs = profiler.profile_session(
                [
                    {
                        "user_query": "[planner] pure reasoning step",
                        "prompt_tokens": 500,
                        "output_tokens": 0,
                        "thinking_tokens": 20_000,
                        "model_name": "publishers/google/models/gemini-3.8-flash",
                    }
                ],
                session_title="Zero Output Heavy Thinking",
            )
        self.assertTrue(any("Thinking Budget" in r for r in recs))
        think_rec = next(r for r in recs if "Thinking Budget" in r)
        self.assertNotIn("infx", think_rec)
        self.assertNotIn("0.00x", think_rec)
        self.assertIn("est. savings: $", think_rec)

        # Mixed-provider session: Turn 1 is already cheapest Google model (Flash), Turn 2 is Claude Opus 4
        with redirect_stdout(io.StringIO()):
            mixed_recs = profiler.profile_session(
                [
                    {
                        "user_query": "[router_g] route intent",
                        "prompt_tokens": 400,
                        "output_tokens": 30,
                        "thinking_tokens": 0,
                        "model_name": "publishers/google/models/gemini-3.8-flash",
                    },
                    {
                        "user_query": "[router_a] classify intent",
                        "prompt_tokens": 2000,
                        "output_tokens": 50,
                        "thinking_tokens": 0,
                        "model_name": "claude-opus-4",
                    },
                ],
                session_title="Mixed Provider Tiering",
            )
        self.assertTrue(
            any(
                "Sub-Agent Model Tiering" in r and "claude-3-5-haiku" in r
                for r in mixed_recs
            )
        )

    def test_profile_bigquery_uses_exact_session_match(self):
        from adk_tokenomics_plugin.cli_finops import run_finops_summary_cli
        from adk_tokenomics_plugin.cli_profiler import profile_from_bigquery

        captured = []

        class FakeJob:
            def __init__(self, rows):
                self._rows = rows

            def result(self):
                return self._rows

        sample_row = {
            "timestamp": "2026-10-20T12:00:00Z",
            "agent": "router_agent",
            "session_id": "abc",
            "invocation_id": "inv_1",
            "sub_agent": "router_agent",
            "orchestration_flow": "Root",
            "user_query": "Hello",
            "prompt_tokens": 500,
            "cached_tokens": 0,
            "output_tokens": 50,
            "thinking_tokens": 0,
            "latency_ms": 120,
            "model_name": "publishers/google/models/gemini-3.8-flash",
        }

        class FakeExactClient:
            def __init__(self, project=None):
                self.project = project

            def query(self, sql, job_config=None):
                params = [
                    (p.name, p.value)
                    for p in getattr(job_config, "query_parameters", [])
                ]
                captured.append((sql, params))
                return FakeJob([sample_row])

        with _scoped_fake_bigquery(FakeExactClient):
            buf = io.StringIO()
            with redirect_stdout(buf):
                recs = profile_from_bigquery(
                    session_id="abc",
                    project_id="my-gcp-project",
                    dataset_id="bq_adk_ds",
                    table_id="adk_agent_events",
                    output_format="json",
                )
            self.assertIsNotNone(recs)
            self.assertEqual(len(captured), 1)
            sql, params = captured[0]
            self.assertNotIn("LIKE", sql.upper())
            self.assertEqual(params, [("session_id", "abc")])
            parsed_out = json.loads(buf.getvalue())
            self.assertEqual(parsed_out["session_id"], "abc")

        # Verify cli_finops falls back to base table when native ADK view raises BadRequest ("Unrecognized name")
        finops_queries = []

        class FakeBadRequestFallbackClient:
            def __init__(self, project=None):
                self.project = project

            def query(self, sql, job_config=None):
                finops_queries.append(sql)
                if len(finops_queries) == 1:
                    raise RuntimeError(
                        "400 Unrecognized name: usage_thinking_tokens at [10:25]"
                    )
                if len(finops_queries) == 2:
                    return FakeJob(
                        [
                            {
                                "model_name": "gemini-3.8-flash",
                                "total_turns": 2,
                                "total_sessions": 1,
                                "fleet_unique_sessions": 1,
                                "total_prompt_tokens": 1000,
                                "total_cached_tokens": 500,
                                "total_output_tokens": 100,
                                "total_thinking_tokens": 50,
                                "total_latency_ms": 400,
                            }
                        ]
                    )
                return FakeJob(
                    [
                        {
                            "agent_name": "travel_bot",
                            "model_name": "gemini-3.8-flash",
                            "turns": 2,
                            "prompt_tok": 1000,
                            "cached_tok": 500,
                            "out_tok": 100,
                            "thk_tok": 50,
                        }
                    ]
                )

        with _scoped_fake_bigquery(FakeBadRequestFallbackClient):
            f_buf = io.StringIO()
            with redirect_stdout(f_buf):
                rc = run_finops_summary_cli(
                    project_id="my-gcp-project",
                    dataset_id="bq_adk_ds",
                    view_id="v_llm_response",
                    table_id="custom_events_tbl",
                    output_format="json",
                )
            self.assertEqual(rc, 0)
            self.assertEqual(len(finops_queries), 3)
            self.assertIn("custom_events_tbl", finops_queries[1])
            fin_json = json.loads(f_buf.getvalue())
            self.assertEqual(fin_json["total_turns"], 2)

    def test_profile_json_error_is_json_and_nonzero(self):
        # 1. Missing trace file
        p_missing = subprocess.run(
            [
                sys.executable,
                "-m",
                "adk_tokenomics_plugin",
                "profile",
                "-t",
                "/nonexistent_987654.json",
                "--format",
                "json",
            ],
            env=SUBPROCESS_ENV,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(p_missing.returncode, 1)
        self.assertTrue(bool(json.loads(p_missing.stdout).get("error")))

        # 2. Malformed JSON trace file + bad prompt placeholder
        with tempfile.TemporaryDirectory() as tmp_dir:
            malformed = os.path.join(tmp_dir, "bad.json")
            with open(malformed, "w", encoding="utf-8") as f:
                f.write("{not valid json")
            p_mal = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "adk_tokenomics_plugin",
                    "profile",
                    "-t",
                    malformed,
                    "--format",
                    "json",
                ],
                env=SUBPROCESS_ENV,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_mal.returncode, 1)
            self.assertTrue(bool(json.loads(p_mal.stdout).get("error")))

            bad_prompts = os.path.join(tmp_dir, "bad_prompts.json")
            with open(bad_prompts, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "context_caching": "Turns {turns_str} {unknown_placeholder_xyz}"
                    },
                    f,
                )
            big_trace = os.path.join(tmp_dir, "big.json")
            with open(big_trace, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "turns": [
                            {
                                "user_query": "q",
                                "prompt_tokens": 5000,
                                "output_tokens": 20,
                            }
                        ]
                    },
                    f,
                )
            p_ph = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "adk_tokenomics_plugin",
                    "profile",
                    "-t",
                    big_trace,
                    "--prompts-file",
                    bad_prompts,
                    "--format",
                    "json",
                ],
                env=SUBPROCESS_ENV,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_ph.returncode, 1)
            self.assertTrue(bool(json.loads(p_ph.stdout).get("error")))

    def test_prompts_file_missing_errors_and_cli_flags_win(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            trace_path = os.path.join(tmp_dir, "uncached_3k.json")
            with open(trace_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "turns": [
                            {
                                "user_query": "q",
                                "prompt_tokens": 3000,
                                "output_tokens": 800,
                            }
                        ]
                    },
                    f,
                )

            # 1. Missing --prompts-file must fail with non-zero exit and valid JSON error
            p_missing = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "adk_tokenomics_plugin",
                    "profile",
                    "-t",
                    trace_path,
                    "--prompts-file",
                    os.path.join(tmp_dir, "missing.json"),
                    "--format",
                    "json",
                ],
                env=SUBPROCESS_ENV,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(p_missing.returncode, 0)
            self.assertIn("error", json.loads(p_missing.stdout))

            # 2. List-shaped --prompts-file must fail with non-zero exit
            list_pf = os.path.join(tmp_dir, "list_prompts.json")
            with open(list_pf, "w", encoding="utf-8") as f:
                json.dump(["not", "a", "dict"], f)
            p_list = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "adk_tokenomics_plugin",
                    "profile",
                    "-t",
                    trace_path,
                    "--prompts-file",
                    list_pf,
                    "--format",
                    "json",
                ],
                env=SUBPROCESS_ENV,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(p_list.returncode, 0)
            self.assertIn("error", json.loads(p_list.stdout))

            # 3. Explicit CLI flag (--min-cache-tokens 100) MUST override prompts-file rule (999999)
            rules_pf = os.path.join(tmp_dir, "rules_prompts.json")
            with open(rules_pf, "w", encoding="utf-8") as f:
                json.dump({"rules": {"min_cache_tokens": 999_999}}, f)
            p_win = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "adk_tokenomics_plugin",
                    "profile",
                    "-t",
                    trace_path,
                    "--prompts-file",
                    rules_pf,
                    "--min-cache-tokens",
                    "100",
                    "--format",
                    "json",
                ],
                env=SUBPROCESS_ENV,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p_win.returncode, 0, p_win.stderr)
            recs = json.loads(p_win.stdout)["recommendations"]
            self.assertTrue(any("Context Caching" in r for r in recs))

    def test_deploy_source_dir_contains_dockerfile(self):
        from adk_tokenomics_plugin import cli_deploy

        # 1. Explicit --source directory without Dockerfile must fail cleanly (rc=1) without calling gcloud
        with tempfile.TemporaryDirectory() as empty_src:
            with (
                patch("shutil.which", return_value="/bin/gcloud"),
                patch("subprocess.run") as mock_run,
                patch("sys.stderr", io.StringIO()),
            ):
                rc = cli_deploy.deploy_cloud_run(
                    project="my-gcp-project",
                    region="us-central1",
                    source_dir=empty_src,
                )
                self.assertEqual(rc, 1)
                mock_run.assert_not_called()

        # 2. Simulate wheel install where __file__ lives inside site-packages (no Dockerfile in parent)
        with (
            tempfile.TemporaryDirectory() as fake_site_pkgs,
            tempfile.TemporaryDirectory() as app_cwd,
        ):
            fake_pkg_dir = os.path.join(fake_site_pkgs, "adk_tokenomics_plugin")
            os.makedirs(fake_pkg_dir)
            fake_module_file = os.path.join(fake_pkg_dir, "cli_deploy.py")

            with (
                patch.object(cli_deploy, "__file__", fake_module_file),
                patch("os.getcwd", return_value=app_cwd),
            ):
                # Without Dockerfile in cwd OR site-packages -> fails cleanly (rc=1)
                with (
                    patch("shutil.which", return_value="/bin/gcloud"),
                    patch("subprocess.run") as mock_run,
                    patch("sys.stderr", io.StringIO()),
                ):
                    self.assertEqual(
                        cli_deploy.deploy_cloud_run(
                            project="my-gcp-project", region="us-central1"
                        ),
                        1,
                    )
                    mock_run.assert_not_called()

                # With Dockerfile in cwd -> deploys from cwd, NOT site-packages
                with open(
                    os.path.join(app_cwd, "Dockerfile"), "w", encoding="utf-8"
                ) as df:
                    df.write("FROM python:3.13-slim\n")
                captured = []

                def fake_ok(cmd, check=False, **kw):
                    captured.append(list(cmd))
                    return subprocess.CompletedProcess(cmd, 0)

                with (
                    patch("shutil.which", return_value="/bin/gcloud"),
                    patch("subprocess.run", side_effect=fake_ok),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(
                        cli_deploy.deploy_cloud_run(
                            project="my-gcp-project", region="us-central1"
                        ),
                        0,
                    )
                src_arg = captured[0][captured[0].index("--source") + 1]
                self.assertEqual(
                    os.path.abspath(src_arg), os.path.abspath(app_cwd)
                )

    def test_cloud_run_region_ignores_global_location(self):
        # 1. Python CLI region resolution
        with patch.dict(
            os.environ, {"GOOGLE_CLOUD_LOCATION": "global"}, clear=True
        ):
            self.assertEqual(get_default_cloud_run_region(), "us-central1")
        with patch.dict(
            os.environ,
            {
                "GOOGLE_CLOUD_LOCATION": "global",
                "CLOUD_RUN_REGION": "europe-west1",
            },
            clear=True,
        ):
            self.assertEqual(get_default_cloud_run_region(), "europe-west1")
        with patch.dict(
            os.environ, {"GOOGLE_CLOUD_LOCATION": "us-east4"}, clear=True
        ):
            self.assertEqual(get_default_cloud_run_region(), "us-east4")

        # 2. Verify .env.example + deploy.sh resolves region != 'global' and omits reserved PORT env var
        with tempfile.TemporaryDirectory() as tmp_dir:
            shutil.copy(
                os.path.join(REPO_ROOT, ".env.example"),
                os.path.join(tmp_dir, ".env"),
            )
            shutil.copy(
                os.path.join(REPO_ROOT, "deploy.sh"),
                os.path.join(tmp_dir, "deploy.sh"),
            )
            fake_bin = os.path.join(tmp_dir, "bin")
            os.makedirs(fake_bin)
            argv_out = os.path.join(tmp_dir, "gcloud_args.txt")
            fake_gcloud = os.path.join(fake_bin, "gcloud")
            with open(fake_gcloud, "w", encoding="utf-8") as fg:
                fg.write(
                    f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {argv_out}\nexit 0\n"
                )
            os.chmod(fake_gcloud, 0o755)

            env = {
                "PATH": fake_bin + os.pathsep + (os.environ.get("PATH") or ""),
                "HOME": tmp_dir,
            }
            proc = subprocess.run(
                ["bash", "deploy.sh"],
                cwd=tmp_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(argv_out, encoding="utf-8") as af:
                gcloud_args = af.read().splitlines()
            region_val = gcloud_args[gcloud_args.index("--region") + 1]
            self.assertNotEqual(region_val, "global")
            self.assertEqual(region_val, "us-central1")
            sev_arg = next(
                a for a in gcloud_args if a.startswith("--set-env-vars=")
            )
            self.assertNotIn("PORT=", sev_arg)
            self.assertIn("TOKENOMICS_ALLOWED_HOSTS=*.run.app", sev_arg)
            self.assertIn("--port", gcloud_args)
            self.assertEqual(
                gcloud_args[gcloud_args.index("--port") + 1], "8080"
            )

    def test_dockerignore_excludes_build_artifacts(self):
        with open(
            os.path.join(REPO_ROOT, ".dockerignore"), encoding="utf-8"
        ) as f:
            lines = {
                line.strip().rstrip("/")
                for line in f
                if line.strip() and not line.strip().startswith("#")
            }
        self.assertIn("build", lines)
        self.assertIn("dist", lines)
        self.assertIn("*.egg-info", lines)

        with open(
            os.path.join(REPO_ROOT, "Dockerfile"), encoding="utf-8"
        ) as df:
            dockerfile_text = df.read()
        self.assertIn("PYTHONUNBUFFERED=1", dockerfile_text)
        self.assertIn("USER ", dockerfile_text)

    def test_setup_py_and_pyproject_find_same_packages(self):
        expected_packages = {
            "adk_tokenomics_plugin",
            "adk_tokenomics_plugin.core",
            "adk_tokenomics_plugin.middleware",
            "adk_tokenomics_plugin.finops",
            "adk_tokenomics_plugin.testing",
        }
        # Every package directory must contain __init__.py so classic find_packages discovers all 5
        for pkg in expected_packages:
            rel_dir = pkg.replace(".", os.sep)
            init_path = os.path.join(REPO_ROOT, rel_dir, "__init__.py")
            self.assertTrue(
                os.path.isfile(init_path), f"Missing __init__.py at {init_path}"
            )

        with open(os.path.join(REPO_ROOT, "pyproject.toml"), "rb") as pf:
            pyproject = tomllib.load(pf)
        self.assertEqual(
            pyproject["project"]["dependencies"], ["python-dotenv>=1.0.0"]
        )
        self.assertIn("adk", pyproject["project"]["optional-dependencies"])
        self.assertIn("bigquery", pyproject["project"]["optional-dependencies"])
        self.assertEqual(
            pyproject["tool"]["setuptools"]["packages"]["find"]["include"],
            ["adk_tokenomics_plugin*"],
        )

        setup_py_path = os.path.join(REPO_ROOT, "setup.py")
        if os.path.exists(setup_py_path):
            with open(setup_py_path, encoding="utf-8") as sf:
                setup_src = sf.read()
            self.assertIn("setup()", setup_src)
            self.assertNotIn("extras_require", setup_src)

        import setuptools  # type: ignore

        find_pkgs_fn = getattr(setuptools, "find_packages", None)
        if callable(find_pkgs_fn):
            discovered = set(
                find_pkgs_fn(
                    where=REPO_ROOT, include=["adk_tokenomics_plugin*"]
                )
            )
            self.assertTrue(expected_packages.issubset(discovered))

    def test_evalset_invocation_latency_not_multiplied(self):
        from adk_tokenomics_plugin.testing.assertions import run_cli_assertions

        eval_payload = {
            "eval_case_results": [
                {
                    "eval_id": "case_multi_call",
                    "eval_metric_result_per_invocation": [
                        {
                            "actual_invocation": {
                                "invocation_id": "inv_3s",
                                "user_content": {
                                    "parts": [{"text": "Book trip"}]
                                },
                                "duration": 3.0,
                                "intermediate_data": {
                                    "invocation_events": [
                                        {
                                            "author": "router",
                                            "usage_metadata": {
                                                "prompt_token_count": 100,
                                                "candidates_token_count": 10,
                                            },
                                        },
                                        {
                                            "author": "researcher",
                                            "usage_metadata": {
                                                "prompt_token_count": 100,
                                                "candidates_token_count": 10,
                                            },
                                        },
                                        {
                                            "author": "writer",
                                            "usage_metadata": {
                                                "prompt_token_count": 100,
                                                "candidates_token_count": 10,
                                            },
                                        },
                                    ]
                                },
                            }
                        }
                    ],
                }
            ]
        }
        with tempfile.NamedTemporaryFile(
            "w", suffix=".evalset_result.json", delete=False
        ) as ef:
            json.dump(eval_payload, ef)
            ev_path = ef.name
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = run_cli_assertions(
                    traces_path=ev_path,
                    max_latency_ms=2000,
                    output_format="json",
                )
            self.assertEqual(rc, 0)
            parsed = json.loads(buf.getvalue())
            self.assertEqual(parsed["total_turns"], 3)
            self.assertAlmostEqual(parsed["avg_latency_ms"], 1000.0, delta=1.0)
        finally:
            os.remove(ev_path)

    def test_tokenomics_test_case_isolation_and_docstring_kwargs(self):
        from adk_tokenomics_plugin.testing.test_case import TokenomicsTestCase

        class _SampleCase(TokenomicsTestCase):
            def runTest(self):
                with self.track_tokens() as t1:
                    t1.record_turn(
                        session_id="s1",
                        app_name="app",
                        model_name="gemini-3.8-flash",
                        user_query="q1",
                        agent_response="r1",
                        input_tokens=100,
                        cached_tokens=300,
                        output_tokens=20,
                    )
                    self.assertCostLessThan(t1, max_dollars=0.05)
                    self.assertCacheHitRatioGreaterThan(t1, min_ratio_pct=50.0)
                    self.assertCacheHitRatioGreaterThan(t1, min_ratio=50.0)

                with self.track_tokens() as t2:
                    # Isolated sink: t2's sink must have 0 turns before recording
                    self.assertEqual(len(t2.sink.fetch_recent(limit=10)), 0)
                    t2.record_turn(
                        session_id="s2",
                        app_name="app",
                        model_name="gemini-3.8-flash",
                        user_query="q2",
                        agent_response="r2",
                        input_tokens=50,
                        cached_tokens=50,
                        output_tokens=10,
                    )
                    self.assertEqual(len(t2.sink.fetch_recent(limit=10)), 1)

        tc = _SampleCase()
        tc.setUp()
        tc.runTest()

    def test_finops_aggregator_null_safety_and_provider_boundaries(self):
        from adk_tokenomics_plugin.finops.aggregator import FinOpsAggregator

        self.assertNotEqual(
            FinOpsAggregator.classify_provider("cohere/command-r-pro"),
            "Google Gemini",
        )
        self.assertEqual(
            FinOpsAggregator.classify_provider("cohere/command-r-pro"), "Other"
        )
        self.assertNotEqual(
            FinOpsAggregator.classify_provider("phi-4-o1-distill"), "OpenAI"
        )
        self.assertEqual(
            FinOpsAggregator.classify_provider("phi-4-o1-distill"),
            "Open-Weight / MaaS",
        )
        self.assertEqual(
            FinOpsAggregator.classify_provider("o1-mini"), "OpenAI"
        )
        self.assertEqual(
            FinOpsAggregator.classify_provider("openai/o3-mini"), "OpenAI"
        )
        self.assertEqual(
            FinOpsAggregator.classify_provider("gemini-2.5-pro"),
            "Google Gemini",
        )
        self.assertEqual(
            FinOpsAggregator.classify_provider("google/gemma-3-27b-it"),
            "Open-Weight / MaaS",
        )

        # NULL / None numeric fields in records must never raise TypeError
        agg = FinOpsAggregator.aggregate_records(
            [
                {
                    "model_name": None,
                    "app_name": None,
                    "total_cost": None,
                    "input_tokens": None,
                    "cached_tokens": None,
                    "output_tokens": None,
                    "thinking_tokens": None,
                },
                {
                    "model_name": "cohere/command-r-pro",
                    "app_name": "rag_bot",
                    "total_cost": 0.02,
                    "input_tokens": 1000,
                    "cached_tokens": 0,
                    "output_tokens": 200,
                    "thinking_tokens": 0,
                },
            ]
        )
        self.assertEqual(agg["status"], "success")
        self.assertEqual(agg["kpis"]["total_turns"], 2)
        self.assertAlmostEqual(agg["kpis"]["total_spend"], 0.02, places=4)
        self.assertEqual(agg["kpis"]["top_provider"], "Other")

    def test_arena_cli_execution(self):
        from adk_tokenomics_plugin.cli_arena import run_arena_cli

        buf = io.StringIO()
        with redirect_stdout(buf):
            run_arena_cli(
                models_str="gemini-3.8-flash,claude-3-5-haiku",
                avg_input=1000,
                avg_output=200,
                cache_ratio=0.5,
            )
        out = buf.getvalue()
        self.assertIn("ADK MODEL PRICE & QUALITY ARENA", out)
        self.assertIn("gemini-3.8-flash", out)

    def test_web_cli_allowed_host_flag_plumbing_and_validation(self):
        from adk_tokenomics_plugin.cli import main as cli_main

        captured = {}

        def fake_run_web_server(**kwargs):
            captured.update(kwargs)

        with (
            patch(
                "adk_tokenomics_plugin.cli_web.run_web_server",
                side_effect=fake_run_web_server,
            ),
            patch.object(
                sys,
                "argv",
                [
                    "adk-tokenomics",
                    "web",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "9090",
                    "--allowed-host",
                    "svc-abc.a.run.app",
                    "--allowed-host",
                    "localhost:9090",
                    "--no-browser",
                ],
            ),
            redirect_stdout(io.StringIO()),
        ):
            cli_main()

        self.assertEqual(captured["host"], "0.0.0.0")
        self.assertEqual(captured["port"], 9090)
        self.assertEqual(
            captured["allowed_hosts"], ["svc-abc.a.run.app", "localhost:9090"]
        )

        # Wildcard '*' must fail closed at CLI validation
        proc_bad = subprocess.run(
            [
                sys.executable,
                "-m",
                "adk_tokenomics_plugin",
                "web",
                "--allowed-host",
                "*",
                "--no-browser",
            ],
            env=SUBPROCESS_ENV,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertNotEqual(proc_bad.returncode, 0)

    def test_assert_cli_format_json_invalid_threshold_emits_valid_json(self):
        invalid_cli_invocations = [
            ["assert", "--max-cost-per-turn", "nan", "--format", "json"],
            ["assert", "--min-cache-rate", "60", "--format", "json"],
            ["assert", "--max-cost-per-turn", "notanumber", "--format", "json"],
            ["assert", "--max-tokens-per-turn", "abc", "--format=json"],
            ["assert", "--max-latency-ms", "-5", "--format", "json"],
            ["assert", "--unknown-flag-xyz", "--format", "json"],
        ]
        for cli_args in invalid_cli_invocations:
            res = subprocess.run(
                [sys.executable, "-m", "adk_tokenomics_plugin", *cli_args],
                env=SUBPROCESS_ENV,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(
                res.returncode, 0, f"Expected non-zero exit for {cli_args}"
            )
            payload = json.loads(res.stdout)
            self.assertIs(
                payload["passed"],
                False,
                f"Expected passed=False for {cli_args}, got {payload}",
            )
            self.assertTrue(len(payload.get("violations", [])) >= 1)

    def test_cli_table_and_view_flags_accept_hyphen_and_leading_digit(self):
        from adk_tokenomics_plugin.cli import main as cli_main

        captured_web = {}

        def fake_run_web_server(**kwargs):
            captured_web.update(kwargs)

        with (
            patch(
                "adk_tokenomics_plugin.cli_web.run_web_server",
                side_effect=fake_run_web_server,
            ),
            patch.object(
                sys,
                "argv",
                [
                    "adk-tokenomics",
                    "web",
                    "--project",
                    "my-gcp-project",
                    "--dataset",
                    "bq_adk_ds_2026",
                    "--table",
                    "2026_adk-events",
                    "--view",
                    "2026_v-llm",
                    "--no-browser",
                ],
            ),
            redirect_stdout(io.StringIO()),
        ):
            cli_main()

        self.assertEqual(captured_web["dataset_id"], "bq_adk_ds_2026")
        self.assertEqual(captured_web["table_id"], "2026_adk-events")
        self.assertEqual(captured_web["view_id"], "2026_v-llm")

        captured_deploy = {}

        def fake_deploy_cloud_run(**kwargs):
            captured_deploy.update(kwargs)
            return 0

        with (
            patch(
                "adk_tokenomics_plugin.cli_deploy.deploy_cloud_run",
                side_effect=fake_deploy_cloud_run,
            ),
            patch.object(
                sys,
                "argv",
                [
                    "adk-tokenomics",
                    "deploy",
                    "cloud_run",
                    "--project",
                    "my-gcp-project",
                    "--dataset",
                    "bq_adk_ds_2026",
                    "--table",
                    "2026_adk-events",
                    "--view",
                    "2026_v-llm",
                ],
            ),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli_main()
            self.assertEqual(ctx.exception.code, 0)

        self.assertEqual(captured_deploy["table"], "2026_adk-events")
        self.assertEqual(captured_deploy["view"], "2026_v-llm")

        from adk_tokenomics_plugin.cli import _bq_dataset_arg

        self.assertEqual(
            _bq_dataset_arg("2026_leading_digit"), "2026_leading_digit"
        )

        # Strict dataset naming & SQL comment sequences ('--') on table/view must fail closed
        for bad_args in (
            ["web", "--dataset", "2026-bad", "--no-browser"],
            ["web", "--dataset", "bad-dataset-hyphen", "--no-browser"],
            ["web", "--table", "bad--comment", "--no-browser"],
            ["web", "--view", "bad--view", "--no-browser"],
            [
                "finops",
                "--project",
                "my-gcp-project",
                "--dataset",
                "2026-bad",
                "--format",
                "json",
            ],
            [
                "finops",
                "--project",
                "my-gcp-project",
                "--table",
                "bad--comment",
                "--format",
                "json",
            ],
        ):
            proc = subprocess.run(
                [sys.executable, "-m", "adk_tokenomics_plugin", *bad_args],
                env=SUBPROCESS_ENV,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertNotEqual(
                proc.returncode, 0, f"Expected failure for {bad_args}"
            )

    def test_format_gcloud_env_vars_exhausts_static_delimiters_without_collision(
        self,
    ):
        from adk_tokenomics_plugin.cli_deploy import (
            _format_gcloud_env_vars,
            _parse_user_env_vars,
            deploy_cloud_run,
        )

        pairs = [
            ("GOOGLE_CLOUD_PROJECT", "my-gcp-project"),
            ("ALL_DELIMS_VAL", "item1,item2@|:;~^#%+,~#1#~,trailing~"),
            ("NEXT_KEY", "~#1#~value_with,comma_and_@_and_|"),
        ]
        encoded = _format_gcloud_env_vars(pairs)
        self.assertTrue(
            encoded.startswith("^"),
            f"Expected custom gcloud delimiter prefix, got {encoded!r}",
        )
        self.assertFalse(
            encoded.startswith("^@^"),
            f"Must not collide with '@' inside value: {encoded!r}",
        )

        end_caret = encoded.index("^", 1)
        delim = encoded[1:end_caret]
        self.assertTrue(
            len(delim) > 1, f"Expected multi-char delimiter, got {delim!r}"
        )
        self.assertNotIn("^", delim)

        body = encoded[end_caret + 1 :]
        split_entries = body.split(delim)
        self.assertEqual(split_entries, [f"{k}={v}" for k, v in pairs])
        self.assertEqual(_parse_user_env_vars(encoded), pairs)

        # Control characters (\x00 / \n) cannot be encoded for gcloud and must fail closed
        for bad_val in ("abc\x00def,ghi", "line1\nline2,x"):
            with self.assertRaises(ValueError):
                _format_gcloud_env_vars([("BAD_VAR", bad_val)])
            with (
                patch("shutil.which", return_value="/bin/gcloud"),
                patch("subprocess.run") as mock_run,
                patch("sys.stderr", io.StringIO()),
            ):
                rc = deploy_cloud_run(
                    project="my-gcp-project",
                    region="us-central1",
                    env_vars=f"BAD_VAR={bad_val}",
                    source_dir=REPO_ROOT,
                )
                self.assertEqual(rc, 1)
                mock_run.assert_not_called()

    def test_deploy_sh_sets_allowed_hosts_and_no_reserved_port_env(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            shutil.copy(
                os.path.join(REPO_ROOT, "deploy.sh"),
                os.path.join(tmp_dir, "deploy.sh"),
            )
            fake_bin = os.path.join(tmp_dir, "bin")
            os.makedirs(fake_bin)
            argv_out = os.path.join(tmp_dir, "gcloud_args.txt")
            fake_gcloud = os.path.join(fake_bin, "gcloud")
            with open(fake_gcloud, "w", encoding="utf-8") as fg:
                fg.write(
                    f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {argv_out}\nexit 0\n"
                )
            os.chmod(fake_gcloud, 0o755)

            env = {
                "PATH": fake_bin + os.pathsep + (os.environ.get("PATH") or ""),
                "HOME": tmp_dir,
                "PORT": "9090",
                "TOKENOMICS_ALLOWED_HOSTS": "tower.corp.example.com,*.run.app",
            }
            proc = subprocess.run(
                ["bash", "deploy.sh"],
                cwd=tmp_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(argv_out, encoding="utf-8") as af:
                gcloud_args = af.read().splitlines()
            self.assertIn("--port", gcloud_args)
            self.assertEqual(
                gcloud_args[gcloud_args.index("--port") + 1], "9090"
            )
            sev_arg = next(
                a for a in gcloud_args if a.startswith("--set-env-vars=")
            )
            self.assertNotIn("PORT=", sev_arg)
            self.assertTrue(sev_arg.startswith("--set-env-vars=^@^"))

            raw = sev_arg.split("--set-env-vars=", 1)[1]
            if raw.startswith("^") and len(raw) >= 3 and raw[2] == "^":
                delim, body = raw[1], raw[3:]
                pairs = body.split(delim)
            else:
                pairs = raw.split(",")

            parsed = {}
            for pair in pairs:
                self.assertIn(
                    "=",
                    pair,
                    f"Malformed KEY=VALUE pair {pair!r} in {sev_arg!r}",
                )
                k, v = pair.split("=", 1)
                self.assertFalse(
                    k.startswith("^") or k.endswith("^"),
                    f"Leading/trailing '^' in key {k!r}",
                )
                self.assertFalse(
                    v.startswith("^") or v.endswith("^"),
                    f"Leading/trailing '^' in value {v!r}",
                )
                parsed[k] = v

            self.assertNotIn("PORT", parsed)
            self.assertEqual(
                parsed["TOKENOMICS_ALLOWED_HOSTS"],
                "tower.corp.example.com,*.run.app",
            )
            self.assertEqual(parsed["GOOGLE_GENAI_USE_VERTEXAI"], "TRUE")

    def test_no_bare_safe_cast_latency_ms_or_coalesce_json_columns_in_sql(self):
        import re

        from adk_tokenomics_plugin.cli_finops import run_finops_summary_cli
        from adk_tokenomics_plugin.cli_profiler import profile_from_bigquery
        from adk_tokenomics_plugin.finops.bq_schema import (
            ADK_LLM_RESPONSE_VIEW_DDL,
            provision_bigquery_table,
        )

        forbidden_sql_regex = re.compile(
            r"(?:SAFE_CAST\s*\(\s*latency_ms\s+AS"
            r"|(?<!SAFE_)CAST\s*\(\s*(?:latency_ms|content|attributes)\s+AS"
            r"|(?<!SAFE_)CAST\s*\(\s*ROUND\s*\("
            r"|COALESCE\s*\(\s*(?:content|attributes)\s*,)",
            re.IGNORECASE,
        )

        # 1. Static check on ADK_LLM_RESPONSE_VIEW_DDL
        self.assertIsNone(
            forbidden_sql_regex.search(ADK_LLM_RESPONSE_VIEW_DDL),
            f"ADK_LLM_RESPONSE_VIEW_DDL contains bare JSON cast/coalesce: {ADK_LLM_RESPONSE_VIEW_DDL}",
        )
        self.assertIsNone(
            re.search(
                r"(?<!SAFE_)CAST\s*\(\s*ROUND\s*\(", ADK_LLM_RESPONSE_VIEW_DDL
            )
        )
        self.assertIn("SAFE_CAST(ROUND(", ADK_LLM_RESPONSE_VIEW_DDL)

        executed_queries = []

        class FakeJob:
            def __init__(self, rows):
                self._rows = rows

            def result(self):
                return self._rows

        sample_profile_row = {
            "timestamp": "2026-11-04T02:00:00Z",
            "agent": "router_agent",
            "session_id": "sess_json_test",
            "invocation_id": "inv_1",
            "sub_agent": "router_agent",
            "orchestration_flow": "Root",
            "user_query": "Hello",
            "prompt_tokens": 500,
            "cached_tokens": 0,
            "output_tokens": 50,
            "thinking_tokens": 0,
            "latency_ms": 120,
            "model_name": "publishers/google/models/gemini-3.8-flash",
        }

        with _scoped_fake_bigquery() as (bq_mod, exc_mod):
            BadRequest = exc_mod.BadRequest
            NotFound = exc_mod.NotFound

            class StrictJsonTypeCheckingClient:
                def __init__(self, project=None):
                    self.project = project

                def create_dataset(self, *args, **kwargs):
                    return None

                def query(self, sql, job_config=None):
                    executed_queries.append(sql)
                    if forbidden_sql_regex.search(sql):
                        raise BadRequest("400 Invalid cast from JSON to INT64")
                    if "CREATE TABLE" in sql or "CREATE VIEW" in sql:
                        return FakeJob([])
                    if "WHERE session_id = @session_id" in sql:
                        return FakeJob([sample_profile_row])
                    if "FROM `my-gcp-project.bq_adk_ds.v_llm_response`" in sql:
                        raise NotFound(
                            "404 Not found: Table my-gcp-project:bq_adk_ds.v_llm_response"
                        )
                    if "GROUP BY model_name" in sql:
                        return FakeJob(
                            [
                                {
                                    "model_name": "gemini-3.8-flash",
                                    "total_turns": 3,
                                    "total_sessions": 1,
                                    "fleet_unique_sessions": 1,
                                    "total_prompt_tokens": 1000,
                                    "total_cached_tokens": 500,
                                    "total_output_tokens": 100,
                                    "total_thinking_tokens": 50,
                                    "total_latency_ms": 600,
                                    "latency_turns": 2,
                                }
                            ]
                        )
                    # Agent breakdown: cheap_high_prompt has more prompt_tok, expensive_argon has higher USD cost
                    return FakeJob(
                        [
                            {
                                "agent_name": "cheap_high_prompt",
                                "model_name": "gemini-3.8-flash",
                                "turns": 2,
                                "prompt_tok": 10_000,
                                "cached_tok": 9_500,
                                "out_tok": 10,
                                "thk_tok": 0,
                            },
                            {
                                "agent_name": "expensive_argon",
                                "model_name": "gemini-4-argon",
                                "turns": 1,
                                "prompt_tok": 2_000,
                                "cached_tok": 0,
                                "out_tok": 3_000,
                                "thk_tok": 4_000,
                            },
                        ]
                    )

            bq_mod.Client = StrictJsonTypeCheckingClient

            # A. provision_bigquery_table
            with redirect_stdout(io.StringIO()):
                self.assertTrue(
                    provision_bigquery_table(
                        project_id="my-gcp-project", dataset_id="bq_adk_ds"
                    )
                )

            # B. profile_from_bigquery succeeds without triggering Invalid cast from JSON to INT64
            p_buf = io.StringIO()
            with redirect_stdout(p_buf):
                recs = profile_from_bigquery(
                    session_id="sess_json_test",
                    project_id="my-gcp-project",
                    dataset_id="bq_adk_ds",
                    table_id="adk_agent_events",
                    output_format="json",
                )
            self.assertIsNotNone(recs)

            # C. run_finops_summary_cli succeeds on base-table fallback, averages latency over latency_turns (600/2=300),
            #    and sorts top_agents by USD cost descending (expensive_argon before cheap_high_prompt)
            f_buf = io.StringIO()
            with redirect_stdout(f_buf):
                fin_rc = run_finops_summary_cli(
                    project_id="my-gcp-project",
                    dataset_id="bq_adk_ds",
                    view_id="v_llm_response",
                    table_id="adk_agent_events",
                    output_format="json",
                )
            self.assertEqual(fin_rc, 0)
            fin_payload = json.loads(f_buf.getvalue())
            self.assertAlmostEqual(
                fin_payload["avg_latency_ms"], 300.0, places=1
            )
            self.assertEqual(
                [a["agent_name"] for a in fin_payload["top_agents"]],
                ["expensive_argon", "cheap_high_prompt"],
            )

            # D. Verify BadRequest SQL compile error in profile_from_bigquery is NOT swallowed as missing table
            compile_err_calls = []

            class BadRequestCompileErrorClient:
                def __init__(self, project=None):
                    self.project = project

                def query(self, sql, job_config=None):
                    compile_err_calls.append(sql)
                    raise BadRequest(
                        "400 Invalid cast from JSON to INT64 at [15:99]"
                    )

            bq_mod.Client = BadRequestCompileErrorClient
            err_buf = io.StringIO()
            with redirect_stdout(err_buf):
                res_err = profile_from_bigquery(
                    session_id="sess_json_test",
                    project_id="my-gcp-project",
                    dataset_id="bq_adk_ds",
                    table_id="custom_tbl",
                    output_format="json",
                )
            self.assertIsNone(res_err)
            self.assertEqual(
                len(compile_err_calls),
                1,
                "BadRequest compile error must not fall back to other table names",
            )
            err_json = json.loads(err_buf.getvalue())
            self.assertIn("Invalid cast from JSON to INT64", err_json["error"])
            self.assertNotIn("No BigQuery records found", err_json["error"])

            # Verify CLI entrypoint also exits non-zero (1) on BadRequest SQL compile error in profile -s
            from adk_tokenomics_plugin.cli import main as cli_main

            cli_err_buf = io.StringIO()
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "adk-tokenomics",
                        "profile",
                        "-s",
                        "sess_json_test",
                        "-p",
                        "my-gcp-project",
                        "-d",
                        "bq_adk_ds",
                        "--format",
                        "json",
                    ],
                ),
                redirect_stdout(cli_err_buf),
            ):
                with self.assertRaises(SystemExit) as exit_ctx:
                    cli_main()
                self.assertEqual(exit_ctx.exception.code, 1)
            cli_err_payload = json.loads(cli_err_buf.getvalue())
            self.assertIn(
                "Invalid cast from JSON to INT64", cli_err_payload["error"]
            )
            self.assertNotIn(
                "No BigQuery records found", cli_err_payload["error"]
            )

            # Verify run_finops_summary_cli also fails immediately on BadRequest compile error without falling back
            compile_err_calls.clear()
            fin_err_buf = io.StringIO()
            with redirect_stdout(fin_err_buf):
                fin_err_rc = run_finops_summary_cli(
                    project_id="my-gcp-project",
                    dataset_id="bq_adk_ds",
                    output_format="json",
                )
            self.assertEqual(fin_err_rc, 1)
            self.assertEqual(len(compile_err_calls), 1)
            self.assertIn(
                "Invalid cast from JSON to INT64",
                json.loads(fin_err_buf.getvalue())["error"],
            )

    # =========================================================================
    # v1.0.2 CLI Heuristics, BigQuery Session Parity, CI Assertions & Docs Suite
    # =========================================================================

    def test_rule2_intra_invocation_baseline_partial_cache_and_evalset_per_session_turns(
        self,
    ):
        from adk_tokenomics_plugin.cli_profiler import (
            TokenProfiler,
            parse_trace_or_eval_payload,
        )

        profiler = TokenProfiler()

        # 1. Intra-invocation tool call (inv_1: 100 -> 900) must NOT overwrite inv_1 baseline (100),
        #    and partial caching (cached_tokens=2000) must still detect +380 fresh-token growth on inv_2 (480 > 100 + 350)!
        intra_tool_turns = [
            {
                "session_id": "sess_comp_1",
                "invocation_id": "inv_1",
                "sub_agent": "root_agent",
                "user_query": "Turn 1 initial",
                "input_tokens": 100,
                "cached_tokens": 2000,
                "output_tokens": 40,
                "model_name": "publishers/google/models/gemini-3.8-flash",
            },
            {
                "session_id": "sess_comp_1",
                "invocation_id": "inv_1",
                "sub_agent": "root_agent",
                "user_query": "Turn 1 post-tool",
                "input_tokens": 900,
                "cached_tokens": 2000,
                "output_tokens": 60,
                "model_name": "publishers/google/models/gemini-3.8-flash",
            },
            {
                "session_id": "sess_comp_1",
                "invocation_id": "inv_2",
                "sub_agent": "root_agent",
                "user_query": "Turn 2 follow-up",
                "input_tokens": 480,
                "cached_tokens": 2000,
                "output_tokens": 50,
                "model_name": "publishers/google/models/gemini-3.8-flash",
            },
        ]
        with redirect_stdout(io.StringIO()):
            recs_growth = profiler.profile_session(
                intra_tool_turns,
                session_title="Intra-Invocation & Partial Cache",
            )
        self.assertTrue(
            any(
                "EventsCompactionConfig" in r or "History Compaction" in r
                for r in recs_growth
            ),
            f"Expected History Compaction recommendation on 100->900(intra)->480 growth, got: {recs_growth}",
        )

        # 2. 4x1-turn .evalset_result.json (4 separate eval cases, 1 user turn per session_id)
        #    must have max_per_session_user_turns == 1 and MUST NOT falsely trigger History Compaction!
        four_case_evalset = {
            "eval_set_result_id": "res_4x1",
            "eval_case_results": [
                {
                    "eval_id": f"case_{idx}",
                    "session_id": f"eval_sess_{idx}",
                    "eval_metric_result_per_invocation": [
                        {
                            "actual_invocation": {
                                "invocation_id": f"inv_{idx}_1",
                                "user_content": {
                                    "parts": [{"text": f"Prompt {idx}"}]
                                },
                                "intermediate_data": {
                                    "invocation_events": [
                                        {
                                            "author": "root_agent",
                                            "model_version": "publishers/google/models/gemini-3.8-flash",
                                            "usage_metadata": {
                                                "prompt_token_count": 200
                                                + idx * 500,
                                                "cached_content_token_count": 100,
                                                "candidates_token_count": 30,
                                            },
                                        }
                                    ]
                                },
                            }
                        }
                    ],
                }
                for idx in range(4)
            ],
        }
        parsed_4x1 = parse_trace_or_eval_payload(four_case_evalset)
        self.assertEqual(len(parsed_4x1), 4)
        captured_total_turns = []
        check_profiler = TokenProfiler(
            custom_callbacks=[
                lambda ctx: (
                    captured_total_turns.append(ctx["total_turns"]) or None
                )
            ]
        )
        with redirect_stdout(io.StringIO()):
            recs_4x1 = check_profiler.profile_session(
                parsed_4x1, session_title="4x1 EvalSet"
            )
        self.assertEqual(captured_total_turns, [1])
        self.assertFalse(
            any(
                "EventsCompactionConfig" in r or "History Compaction" in r
                for r in recs_4x1
            ),
            f"4x1-turn evalset must NOT trigger History Compaction, got: {recs_4x1}",
        )

        # 3. Mixed invocation_id + None turns within a single session_id count cleanly (inv_a x2 + None x2 = 3 < 4; +1 None -> 4)
        mixed_turns = [
            {
                "session_id": "s_mix",
                "invocation_id": "inv_a",
                "input_tokens": 100,
                "cached_tokens": 100,
                "output_tokens": 20,
            },
            {
                "session_id": "s_mix",
                "invocation_id": "inv_a",
                "input_tokens": 120,
                "cached_tokens": 100,
                "output_tokens": 20,
            },
            {
                "session_id": "s_mix",
                "invocation_id": None,
                "input_tokens": 130,
                "cached_tokens": 100,
                "output_tokens": 20,
            },
            {
                "session_id": "s_mix",
                "invocation_id": None,
                "input_tokens": 140,
                "cached_tokens": 100,
                "output_tokens": 20,
            },
            {
                "session_id": "s_mix",
                "invocation_id": "inv_b",
                "input_tokens": 150,
                "cached_tokens": 100,
                "output_tokens": 20,
            },
        ]
        mix_counts = []
        mix_profiler = TokenProfiler(
            custom_callbacks=[
                lambda ctx: mix_counts.append(ctx["total_turns"]) or None
            ]
        )
        with redirect_stdout(io.StringIO()):
            recs_mix = mix_profiler.profile_session(
                mixed_turns, session_title="Mixed Invocations"
            )
        self.assertEqual(mix_counts, [4])
        self.assertTrue(
            any(
                "EventsCompactionConfig" in r or "History Compaction" in r
                for r in recs_mix
            )
        )

    def test_rules_2_3_4_per_turn_model_rates_open_weight_tiering_and_stage_width(
        self,
    ):
        import re

        from adk_tokenomics_plugin.cli_profiler import TokenProfiler

        profiler = TokenProfiler()
        # Turn 1: cheap gemini-3.8-flash ($0.75/1M) with 1,000 fresh tokens
        # Turn 2: expensive claude-opus-4 ($15.00/1M) with 10,000 fresh tokens -> rapid growth triggers Rule 2 & >5000 triggers Rule 3
        mixed_model_turns = [
            {
                "session_id": "s_rates",
                "invocation_id": "inv_1",
                "sub_agent": "worker_parallel",
                "orchestration_flow": "AgentTool • Parallel • Loop",
                "user_query": "Step 1",
                "input_tokens": 1_000,
                "cached_tokens": 0,
                "output_tokens": 700,
                "model_name": "publishers/google/models/gemini-3.8-flash",
            },
            {
                "session_id": "s_rates",
                "invocation_id": "inv_2",
                "sub_agent": "worker_parallel",
                "orchestration_flow": "AgentTool • Parallel • Loop",
                "user_query": "Step 2",
                "input_tokens": 10_000,
                "cached_tokens": 0,
                "output_tokens": 700,
                "model_name": "claude-opus-4",
            },
        ]
        out_buf = io.StringIO()
        with redirect_stdout(out_buf):
            recs = profiler.profile_session(
                mixed_model_turns,
                session_title="Mixed Model Savings & Stage Width",
            )
        rendered_table = out_buf.getvalue()

        # Verify Stage column does NOT truncate "AgentTool • Parallel" or "AgentTool • Parallel • Loop" at 11 chars
        self.assertIn("AgentTool • Parallel • Loop", rendered_table)

        # Expected Rule 2 (0.35): (1000*0.35/1M)*0.75 + (10000*0.35/1M)*15.0 = 0.0002625 + 0.0525 = $0.05276
        comp_rec = next(
            r
            for r in recs
            if "EventsCompactionConfig" in r or "History Compaction" in r
        )
        m_comp = re.search(r"\$([0-9]+\.[0-9]+)", comp_rec)
        self.assertIsNotNone(m_comp, comp_rec)
        self.assertAlmostEqual(float(m_comp.group(1)), 0.05276, places=4)

        # Expected Rule 3 (0.60): (1000*0.60/1M)*0.75 + (10000*0.60/1M)*15.0 = 0.00045 + 0.0900 = $0.09045
        skills_rec = next(
            r for r in recs if "load_skill" in r or "Modular Skills" in r
        )
        m_sk = re.search(r"\$([0-9]+\.[0-9]+)", skills_rec)
        self.assertIsNotNone(m_sk, skills_rec)
        self.assertAlmostEqual(float(m_sk.group(1)), 0.09045, places=4)

        # Rule 4 on paid open_weight turn must select meta/llama-3.1-8b-instruct, NEVER $0 ollama/local-open-weight
        with redirect_stdout(io.StringIO()):
            ow_recs = profiler.profile_session(
                [
                    {
                        "user_query": "[ow_router] route ticket",
                        "input_tokens": 2_000,
                        "cached_tokens": 0,
                        "output_tokens": 50,
                        "thinking_tokens": 0,
                        "model_name": "meta/llama-3.1-405b-instruct",
                    }
                ],
                session_title="Open Weight Tiering",
            )
        tier_rec = next(r for r in ow_recs if "Sub-Agent Model Tiering" in r)
        self.assertIn("meta/llama-3.1-8b-instruct", tier_rec)
        self.assertNotIn("ollama", tier_rec.lower())
        self.assertNotIn("100.0%", tier_rec)

    def test_profile_from_bigquery_multievent_correlation_and_explicit_table_zero_row_stop(
        self,
    ):
        from adk_tokenomics_plugin.cli_profiler import profile_from_bigquery

        upstream_events = [
            {
                "timestamp": "2026-11-04T10:00:00Z",
                "event_type": "USER_MESSAGE_RECEIVED",
                "agent": "root_concierge",
                "session_id": "sess_upstream_1",
                "invocation_id": "inv_u1",
                "content": json.dumps(
                    {"parts": [{"text": "Analyze SEC filing and cloud spend"}]}
                ),
                "attributes": "{}",
                "latency_ms": None,
            },
            {
                "timestamp": "2026-11-04T10:00:01Z",
                "event_type": "LLM_RESPONSE",
                "agent": "root_concierge",
                "session_id": "sess_upstream_1",
                "invocation_id": "inv_u1",
                "content": json.dumps(
                    {
                        "usage": {
                            "prompt": 1000,
                            "completion": 80,
                            "total": 1280,
                        },
                        "response": {
                            "parts": [
                                {
                                    "function_call": {
                                        "name": "parallel_sec_helper",
                                        "args": {"ticker": "GOOG"},
                                    }
                                },
                            ]
                        },
                    }
                ),
                "attributes": json.dumps(
                    {
                        "model_version": "gemini-4-argon",
                        "usage_metadata": {
                            "prompt_token_count": 1000,
                            "tool_use_prompt_token_count": 200,
                            "cached_content_token_count": 300,
                            "candidates_token_count": 80,
                            "thoughts_token_count": 0,
                        },
                    }
                ),
                "latency_ms": json.dumps({"total_ms": 420}),
            },
            {
                "timestamp": "2026-11-04T10:00:02Z",
                "event_type": "TOOL_COMPLETED",
                "agent": "root_concierge",
                "session_id": "sess_upstream_1",
                "invocation_id": "inv_u1",
                "content": json.dumps(
                    {
                        "tool": "load_skill",
                        "args": {"skill_name": "sec-filings"},
                    }
                ),
                "attributes": "{}",
                "latency_ms": None,
            },
            {
                "timestamp": "2026-11-04T10:00:03Z",
                "event_type": "TOOL_COMPLETED",
                "agent": "root_concierge",
                "session_id": "sess_upstream_1",
                "invocation_id": "inv_u1",
                "content": json.dumps(
                    {
                        "tool": "unload_skill",
                        "args": {"skill_name": "sec-filings"},
                    }
                ),
                "attributes": "{}",
                "latency_ms": None,
            },
            {
                # Missing / None event_type must be treated as LLM_RESPONSE per M-c, and AgentTool • Parallel inferred!
                "timestamp": "2026-11-04T10:00:04Z",
                "event_type": None,
                "agent": "parallel_sec_helper",
                "session_id": "sess_upstream_1",
                "invocation_id": "inv_u1",
                "content": json.dumps(
                    {
                        "response": "SEC summary ready",
                        "usage": {"prompt": 600, "completion": 50},
                    }
                ),
                "attributes": json.dumps({"model": "gemini-3.8-flash"}),
                "latency_ms": "180",
            },
        ]

        queried_tables: list[str] = []
        captured_sql: list[str] = []

        class FakeJob:
            def __init__(self, rows):
                self._rows = rows

            def result(self):
                return self._rows

        with _scoped_fake_bigquery() as (bq_mod, exc_mod):
            NotFound = exc_mod.NotFound

            class UpstreamCorrelationClient:
                def __init__(self, project=None):
                    self.project = project

                def query(self, sql, job_config=None):
                    captured_sql.append(sql)
                    if (
                        ".`custom_empty_tbl`" in sql
                        or ".custom_empty_tbl`" in sql
                    ):
                        queried_tables.append("custom_empty_tbl")
                        return FakeJob([])
                    if ".adk_agent_events`" in sql:
                        queried_tables.append("adk_agent_events")
                        raise NotFound(
                            "404 Not found: Table proj:ds.adk_agent_events"
                        )
                    if ".agent_events`" in sql:
                        queried_tables.append("agent_events")
                        return FakeJob(upstream_events)
                    return FakeJob([])

            bq_mod.Client = UpstreamCorrelationClient

            # A. Default table_id="adk_agent_events" 404s -> falls back to upstream "agent_events"
            p_buf = io.StringIO()
            with redirect_stdout(p_buf):
                recs = profile_from_bigquery(
                    session_id="sess_upstream_1",
                    project_id="my-gcp-project",
                    dataset_id="bq_adk_ds",
                    table_id="adk_agent_events",
                    output_format="json",
                )
            self.assertIsNotNone(recs)
            self.assertEqual(
                queried_tables, ["adk_agent_events", "agent_events"]
            )
            self.assertIn("WHERE session_id = @session_id", captured_sql[0])
            self.assertIn("ORDER BY timestamp ASC", captured_sql[0])

            out = json.loads(p_buf.getvalue())
            self.assertEqual(
                out["source"], "my-gcp-project.bq_adk_ds.agent_events"
            )
            self.assertEqual(out["total_turns"], 2)
            t0, t1 = out["turns"][0], out["turns"][1]
            self.assertEqual(
                t0["user_query"], "Analyze SEC filing and cloud spend"
            )
            self.assertEqual(t0["prompt_tokens"], 1200)
            self.assertEqual(t0["input_tokens"], 900)
            self.assertEqual(t0["cached_tokens"], 300)
            self.assertIn("parallel_sec_helper", t0["tools_called"])
            self.assertIn("load_skill", t0["tools_called"])
            self.assertEqual(t0["skills_active"], ["sec-filings"])
            self.assertEqual(t0["orchestration_flow"], "Root")
            self.assertEqual(
                t1["user_query"], "Analyze SEC filing and cloud spend"
            )
            self.assertEqual(t1["orchestration_flow"], "AgentTool • Parallel")

            # B. Explicit non-default --table succeeding with 0 rows MUST stop immediately without falling back!
            queried_tables.clear()
            err_buf = io.StringIO()
            with redirect_stdout(err_buf):
                res_empty = profile_from_bigquery(
                    session_id="sess_upstream_1",
                    project_id="my-gcp-project",
                    dataset_id="bq_adk_ds",
                    table_id="custom_empty_tbl",
                    output_format="json",
                )
            self.assertIsNone(res_empty)
            self.assertEqual(queried_tables, ["custom_empty_tbl"])
            self.assertIn(
                "custom_empty_tbl", json.loads(err_buf.getvalue())["error"]
            )

    def test_parse_payload_c4_double_encoded_camelcase_model_duration_and_fail_closed_assert(
        self,
    ):
        from adk_tokenomics_plugin.cli_profiler import (
            parse_trace_or_eval_payload,
        )
        from adk_tokenomics_plugin.testing.assertions import run_cli_assertions

        camel_evalset = {
            "evalSetResultId": "camel_001",
            "evalCaseResults": [
                {
                    "evalId": "camel_case_1",
                    "sessionId": "sess_camel_1",
                    "evalMetricResultPerInvocation": [
                        {
                            "actualInvocation": {
                                "invocationId": "inv_c1",
                                "userContent": {
                                    "parts": [{"text": "Hello camelCase"}]
                                },
                                # modelDuration (0.0004s -> 1ms floor) must win over duration (9.5s)!
                                "duration": 9.5,
                                "modelDuration": 0.0004,
                                "intermediateData": {
                                    "invocationEvents": [
                                        {
                                            "author": "agent_a",
                                            "modelVersion": "gemini-3.8-flash",
                                            "usageMetadata": {
                                                "promptTokens": 500,
                                                "toolUsePromptTokenCount": 150,
                                                "cachedContentTokenCount": 100,
                                                "candidatesTokenCount": 40,
                                            },
                                        },
                                        {
                                            "author": "agent_b",
                                            "modelVersion": "gemini-3.8-flash",
                                            "usageMetadata": {
                                                "promptTokens": 300,
                                                "toolUsePromptTokenCount": 50,
                                                "cachedContentTokenCount": 50,
                                                "candidatesTokenCount": 20,
                                            },
                                        },
                                    ]
                                },
                            }
                        }
                    ],
                }
            ],
        }
        double_encoded = json.dumps(camel_evalset)
        turns = parse_trace_or_eval_payload(double_encoded)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["prompt_tokens"], 650)
        self.assertEqual(turns[0]["input_tokens"], 550)
        self.assertEqual(turns[1]["prompt_tokens"], 350)
        self.assertEqual(turns[0]["latency_ms"], 1)
        self.assertEqual(turns[1]["latency_ms"], 1)

        # Also verify tool_use_prompt_token_count in actual.usage_metadata, eval_cases, and events branches
        actual_only_turns = parse_trace_or_eval_payload(
            {
                "evalCaseResults": [
                    {
                        "evalId": "c2",
                        "evalMetricResultPerInvocation": [
                            {
                                "actualInvocation": {
                                    "model_duration": 0.45,
                                    "duration": 5.0,
                                    "usageMetadata": {
                                        "prompt_token_count": 400,
                                        "tool_use_prompt_token_count": 100,
                                        "candidates_token_count": 25,
                                    },
                                }
                            }
                        ],
                    }
                ]
            }
        )
        self.assertEqual(actual_only_turns[0]["prompt_tokens"], 500)
        self.assertEqual(actual_only_turns[0]["latency_ms"], 450)

        ev_branch_turns = parse_trace_or_eval_payload(
            {
                "events": [
                    {
                        "user_query": "ev_q",
                        "usage_metadata": {
                            "prompt_token_count": 700,
                            "tool_use_prompt_token_count": 100,
                            "cached_content_token_count": 200,
                            "candidates_token_count": 30,
                        },
                    }
                ]
            }
        )
        self.assertEqual(ev_branch_turns[0]["prompt_tokens"], 800)
        self.assertEqual(ev_branch_turns[0]["input_tokens"], 600)

        # Legacy eval_cases WITHOUT usage metadata: allowed in profile (allow_estimated_tokens=True), rejected in assert!
        uninstrumented_eval_cases = {
            "eval_cases": [
                {
                    "eval_case_id": "legacy_no_usage",
                    "prompt": {"parts": [{"text": "Book a hotel in Rome"}]},
                    "responses": [
                        {"response": {"parts": [{"text": "Booked."}]}}
                    ],
                }
            ]
        }
        est_turns = parse_trace_or_eval_payload(
            uninstrumented_eval_cases, allow_estimated_tokens=True
        )
        self.assertEqual(len(est_turns), 1)
        with self.assertRaises(ValueError):
            parse_trace_or_eval_payload(
                uninstrumented_eval_cases, allow_estimated_tokens=False
            )

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False
        ) as uf:
            json.dump(uninstrumented_eval_cases, uf)
            uninst_path = uf.name
        try:
            j_buf = io.StringIO()
            with redirect_stdout(j_buf):
                rc = run_cli_assertions(
                    traces_path=uninst_path,
                    max_cost_per_turn=1.0,
                    output_format="json",
                )
            self.assertEqual(rc, 1)
            j_out = json.loads(j_buf.getvalue())
            self.assertFalse(j_out["passed"])
            self.assertEqual(j_out["failed_artifacts"], [uninst_path])
        finally:
            os.remove(uninst_path)

    def test_assertions_cross_file_dedup_eval_history_discovery_and_remediation_hints(
        self,
    ):
        from adk_tokenomics_plugin.testing.assertions import run_cli_assertions

        # 1. Coexisting ci_eval.json / live_adk_session.json + <session_id>.json deduplicates turns across files
        shared_turn = {
            "timestamp": "2026-11-04T12:00:00Z",
            "session_id": "sess_dup_01",
            "invocation_id": "inv_dup_1",
            "sub_agent": "router",
            "model_name": "publishers/google/models/gemini-3.8-flash",
            "input_tokens": 1000,
            "cached_tokens": 3000,
            "output_tokens": 100,
            "thinking_tokens": 0,
            "latency_ms": 250,
        }
        with tempfile.TemporaryDirectory() as trace_dir:
            for fn in (
                "ci_eval.json",
                "live_adk_session.json",
                "sess_dup_01.json",
            ):
                with open(
                    os.path.join(trace_dir, fn), "w", encoding="utf-8"
                ) as f:
                    json.dump(
                        {"session_id": "sess_dup_01", "turns": [shared_turn]}, f
                    )

            dedup_buf = io.StringIO()
            with redirect_stdout(dedup_buf):
                rc_dedup = run_cli_assertions(
                    traces_path=trace_dir,
                    max_cost_per_turn=0.05,
                    output_format="json",
                )
            self.assertEqual(rc_dedup, 0)
            dedup_payload = json.loads(dedup_buf.getvalue())
            self.assertEqual(dedup_payload["total_turns"], 1)
            self.assertEqual(dedup_payload["failed_artifacts"], [])

        # 2. Agent directory discovery: skips *.evalset.json & test_config.json, discovers .adk/eval_history/*.evalset_result.json,
        #    and checks .adk/eval_history when default artifacts/traces is absent; emits failed_artifacts + profile -t hints!
        with tempfile.TemporaryDirectory() as agent_dir:
            with open(
                os.path.join(agent_dir, "suite.evalset.json"),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(
                    {"eval_set_id": "suite", "eval_cases": [{"eval_id": "c1"}]},
                    f,
                )
            with open(
                os.path.join(agent_dir, "test_config.json"),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump({"criteria": {"tool_trajectory_avg_score": 1.0}}, f)

            eval_hist = os.path.join(agent_dir, ".adk", "eval_history")
            os.makedirs(eval_hist)
            result_file = os.path.join(eval_hist, "run_01.evalset_result.json")
            # Double-encoded JSON string as written by ADK eval_history
            eval_res_payload = json.dumps(
                {
                    "eval_set_result_id": "run_01",
                    "eval_case_results": [
                        {
                            "eval_id": "c1",
                            "session_id": "s_eval_1",
                            "eval_metric_result_per_invocation": [
                                {
                                    "actual_invocation": {
                                        "invocation_id": "inv_1",
                                        "model_duration": 0.4,
                                        "intermediate_data": {
                                            "invocation_events": [
                                                {
                                                    "author": "agent",
                                                    "model_version": "claude-opus-4",
                                                    "usage_metadata": {
                                                        "prompt_token_count": 50_000,
                                                        "cached_content_token_count": 0,
                                                        "candidates_token_count": 5_000,
                                                    },
                                                }
                                            ]
                                        },
                                    }
                                }
                            ],
                        }
                    ],
                }
            )
            with open(result_file, "w", encoding="utf-8") as rf:
                json.dump(eval_res_payload, rf)

            # Passing <agent_dir> skips suite.evalset.json & test_config.json and evaluates run_01.evalset_result.json
            pass_buf = io.StringIO()
            with redirect_stdout(pass_buf):
                rc_ok = run_cli_assertions(
                    traces_path=agent_dir,
                    max_cost_per_turn=5.0,
                    output_format="json",
                )
            self.assertEqual(rc_ok, 0, pass_buf.getvalue())
            self.assertEqual(json.loads(pass_buf.getvalue())["total_turns"], 1)

            # Failing threshold reports failed_artifacts in JSON and '-> Run: adk-tokenomics profile -t <file>' in text
            fail_json_buf = io.StringIO()
            with redirect_stdout(fail_json_buf):
                rc_fail_j = run_cli_assertions(
                    traces_path=agent_dir,
                    max_cost_per_turn=0.0001,
                    output_format="json",
                )
            self.assertEqual(rc_fail_j, 1)
            fail_j = json.loads(fail_json_buf.getvalue())
            self.assertEqual(fail_j["failed_artifacts"], [result_file])

            fail_txt_buf = io.StringIO()
            with redirect_stdout(fail_txt_buf):
                rc_fail_t = run_cli_assertions(
                    traces_path=agent_dir,
                    max_cost_per_turn=0.0001,
                    output_format="text",
                )
            self.assertEqual(rc_fail_t, 1)
            self.assertIn(
                f"-> Run: adk-tokenomics profile -t {result_file}",
                fail_txt_buf.getvalue(),
            )

            # Default discovery (no --traces) falls back to .adk/eval_history when artifacts/traces is absent
            proc_cwd = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "adk_tokenomics_plugin",
                    "assert",
                    "--max-cost-per-turn",
                    "5.0",
                    "--format",
                    "json",
                ],
                cwd=agent_dir,
                env=SUBPROCESS_ENV,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                proc_cwd.returncode, 0, proc_cwd.stdout + proc_cwd.stderr
            )
            self.assertEqual(json.loads(proc_cwd.stdout)["total_turns"], 1)

    def test_cli_finops_c1_tool_use_view_fallback_agent_events_and_group_by_agent_model(
        self,
    ):
        from adk_tokenomics_plugin.cli_finops import run_finops_summary_cli

        class FakeJob:
            def __init__(self, rows):
                self._rows = rows

            def result(self):
                return self._rows

        # 1. Pre-v1.0.2 view raises 'Unrecognized name: usage_tool_use_tokens' -> retries view without usage_tool_use_tokens,
        #    and aggregates multi-model agent ('hybrid_agent' running both gemini-3.8-flash and claude-opus-4) via GROUP BY agent_name, model_name!
        view_sqls: list[str] = []
        test_self = self

        class PreV102ViewClient:
            def __init__(self, project=None):
                self.project = project

            def query(self, sql, job_config=None):
                view_sqls.append(sql)
                if "usage_tool_use_tokens" in sql:
                    raise RuntimeError(
                        "400 Unrecognized name: usage_tool_use_tokens at [11:45]"
                    )
                if "GROUP BY model_name" in sql:
                    return FakeJob(
                        [
                            {
                                "model_name": "publishers/google/models/gemini-3.8-flash",
                                "total_turns": 1,
                                "total_sessions": 1,
                                "fleet_unique_sessions": 1,
                                "total_prompt_tokens": 1_000_000,
                                "total_cached_tokens": 0,
                                "total_output_tokens": 0,
                                "total_thinking_tokens": 0,
                                "total_latency_ms": 100,
                                "latency_turns": 1,
                            },
                            {
                                "model_name": "claude-opus-4",
                                "total_turns": 1,
                                "total_sessions": 1,
                                "fleet_unique_sessions": 1,
                                "total_prompt_tokens": 1_000_000,
                                "total_cached_tokens": 0,
                                "total_output_tokens": 0,
                                "total_thinking_tokens": 0,
                                "total_latency_ms": 200,
                                "latency_turns": 1,
                            },
                        ]
                    )
                test_self.assertIn("GROUP BY agent_name, model_name", sql)
                return FakeJob(
                    [
                        {
                            "agent_name": "hybrid_agent",
                            "model_name": "publishers/google/models/gemini-3.8-flash",
                            "turns": 1,
                            "prompt_tok": 1_000_000,
                            "cached_tok": 0,
                            "out_tok": 0,
                            "thk_tok": 0,
                        },
                        {
                            "agent_name": "hybrid_agent",
                            "model_name": "claude-opus-4",
                            "turns": 1,
                            "prompt_tok": 1_000_000,
                            "cached_tok": 0,
                            "out_tok": 0,
                            "thk_tok": 0,
                        },
                    ]
                )

        with _scoped_fake_bigquery(PreV102ViewClient):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = run_finops_summary_cli(
                    project_id="my-gcp-project",
                    dataset_id="bq_adk_ds",
                    output_format="json",
                )
            self.assertEqual(rc, 0)
            self.assertEqual(len(view_sqls), 3)
            self.assertIn("usage_tool_use_tokens", view_sqls[0])
            self.assertNotIn("usage_tool_use_tokens", view_sqls[1])
            self.assertNotIn("usage_tool_use_tokens", view_sqls[2])
            out = json.loads(buf.getvalue())
            # $0.75 (Flash 1M in) + $15.00 (Opus 1M in) = $15.75 exact across both models on hybrid_agent!
            self.assertAlmostEqual(out["total_spend_usd"], 15.75, places=4)
            self.assertEqual(len(out["top_agents"]), 1)
            self.assertEqual(out["top_agents"][0]["agent_name"], "hybrid_agent")
            self.assertAlmostEqual(
                out["top_agents"][0]["estimated_cost_usd"], 15.75, places=4
            )
            self.assertAlmostEqual(
                out["top_agents"][0]["share_pct"], 100.0, places=2
            )

        # 2. View 404 + adk_agent_events 404 -> falls back to upstream 'agent_events' with content.usage.prompt + tool_use_prompt_token_count
        fb_targets: list[str] = []
        with _scoped_fake_bigquery() as (bq_mod, exc_mod):
            NotFound = exc_mod.NotFound

            class AgentEventsFallbackClient:
                def __init__(self, project=None):
                    self.project = project

                def query(self, sql, job_config=None):
                    if ".v_llm_response`" in sql:
                        fb_targets.append("v_llm_response")
                        raise NotFound("404 Not found: view v_llm_response")
                    if ".adk_agent_events`" in sql:
                        fb_targets.append("adk_agent_events")
                        raise NotFound("404 Not found: table adk_agent_events")
                    fb_targets.append("agent_events")
                    test_self.assertIn("$.usage.prompt", sql)
                    test_self.assertIn("tool_use_prompt_token_count", sql)
                    if "GROUP BY model_name" in sql:
                        return FakeJob(
                            [
                                {
                                    "model_name": "gemini-3.8-flash",
                                    "total_turns": 1,
                                    "total_sessions": 1,
                                    "fleet_unique_sessions": 1,
                                    "total_prompt_tokens": 1200,
                                    "total_cached_tokens": 200,
                                    "total_output_tokens": 100,
                                    "total_thinking_tokens": 0,
                                    "total_latency_ms": 150,
                                    "latency_turns": 1,
                                }
                            ]
                        )
                    return FakeJob(
                        [
                            {
                                "agent_name": "upstream_bot",
                                "model_name": "gemini-3.8-flash",
                                "turns": 1,
                                "prompt_tok": 1200,
                                "cached_tok": 200,
                                "out_tok": 100,
                                "thk_tok": 0,
                            }
                        ]
                    )

            bq_mod.Client = AgentEventsFallbackClient
            fb_buf = io.StringIO()
            with redirect_stdout(fb_buf):
                rc_fb = run_finops_summary_cli(
                    project_id="my-gcp-project",
                    dataset_id="bq_adk_ds",
                    output_format="json",
                )
            self.assertEqual(rc_fb, 0)
            self.assertEqual(
                fb_targets,
                [
                    "v_llm_response",
                    "adk_agent_events",
                    "agent_events",
                    "agent_events",
                ],
            )
            fb_out = json.loads(fb_buf.getvalue())
            self.assertEqual(
                fb_out["source"], "my-gcp-project.bq_adk_ds.agent_events"
            )

    def test_arena_zero_dollar_self_hosted_ranks_first_and_cli_env_defaults(
        self,
    ):
        from adk_tokenomics_plugin.cli import (
            get_default_dataset,
            get_default_table,
            get_default_view,
        )
        from adk_tokenomics_plugin.cli_arena import run_arena_cli

        arena_buf = io.StringIO()
        with redirect_stdout(arena_buf):
            run_arena_cli(
                models_str="claude-opus-4,ollama/local-open-weight,gemini-3.8-flash",
                avg_input=2000,
                avg_output=400,
                cache_ratio=0.5,
            )
        arena_text = arena_buf.getvalue()
        self.assertIn("∞ (Free) ⭐ (Best Value)", arena_text)
        self.assertIn(
            "Most Cost-Efficient: `ollama/local-open-weight`", arena_text
        )

        # Verify cli.py get_default_* delegates to resolve_env_bq_identifiers()
        with patch.dict(
            os.environ,
            {
                "BQ_DATASET_ID": "2026_ds_env",
                "BQ_TABLE_ID": "2026_tbl-env",
                "BQ_VIEW_ID": "2026_vw-env",
            },
            clear=True,
        ):
            self.assertEqual(get_default_dataset(), "2026_ds_env")
            self.assertEqual(get_default_table(), "2026_tbl-env")
            self.assertEqual(get_default_view(), "2026_vw-env")

    def test_deploy_sh_delimiter_collision_fallback_and_docs_contract_sync(
        self,
    ):
        # 1. When an env value contains '@', deploy.sh switches cleanly from '^@^' to '^|^'
        with tempfile.TemporaryDirectory() as tmp_dir:
            shutil.copy(
                os.path.join(REPO_ROOT, "deploy.sh"),
                os.path.join(tmp_dir, "deploy.sh"),
            )
            fake_bin = os.path.join(tmp_dir, "bin")
            os.makedirs(fake_bin)
            argv_out = os.path.join(tmp_dir, "gcloud_args.txt")
            fake_gcloud = os.path.join(fake_bin, "gcloud")
            with open(fake_gcloud, "w", encoding="utf-8") as fg:
                fg.write(
                    f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {argv_out}\nexit 0\n"
                )
            os.chmod(fake_gcloud, 0o755)

            env = {
                "PATH": fake_bin + os.pathsep + (os.environ.get("PATH") or ""),
                "HOME": tmp_dir,
                "TOKENOMICS_ALLOWED_HOSTS": "user@proxy.example.com,*.run.app",
            }
            proc = subprocess.run(
                ["bash", "deploy.sh"],
                cwd=tmp_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(argv_out, encoding="utf-8") as af:
                gcloud_args = af.read().splitlines()
            sev_arg = next(
                a for a in gcloud_args if a.startswith("--set-env-vars=")
            )
            self.assertTrue(
                sev_arg.startswith("--set-env-vars=^|^"),
                f"Expected ^|^ on '@' collision, got {sev_arg!r}",
            )
            raw_pairs = sev_arg.split("--set-env-vars=^|^", 1)[1].split("|")
            parsed = dict(p.split("=", 1) for p in raw_pairs)
            self.assertEqual(
                parsed["TOKENOMICS_ALLOWED_HOSTS"],
                "user@proxy.example.com,*.run.app",
            )

        # 2. Verify .env.example, README.md, and SQL_REFERENCE.md contract sync
        with open(
            os.path.join(REPO_ROOT, ".env.example"), encoding="utf-8"
        ) as ef:
            env_example = ef.read()
        self.assertIn("TOKENOMICS_DEFAULT_MODEL=", env_example)
        self.assertNotIn("MODEL_NAME=", env_example)

        with open(os.path.join(REPO_ROOT, "README.md"), encoding="utf-8") as rf:
            readme = rf.read()
        self.assertIn('gen_ai.operation.name="generate_content"', readme)
        self.assertIn("--format json", readme)
        self.assertIn("XDG_CONFIG_HOME", readme)
        self.assertIn("legacy_flat_schema=True", readme)

        with open(
            os.path.join(REPO_ROOT, "SQL_REFERENCE.md"), encoding="utf-8"
        ) as sf:
            sql_ref = sf.read()
        self.assertIn("usage_tool_use_tokens", sql_ref)
        self.assertIn("tool_use_prompt_token_count", sql_ref)
        self.assertIn("$.usage.prompt", sql_ref)


if __name__ == "__main__":
    unittest.main()
