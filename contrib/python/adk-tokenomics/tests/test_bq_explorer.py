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

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from adk_tokenomics_plugin import cli_web as server
from adk_tokenomics_plugin.cli_profiler import profile_from_bigquery
from adk_tokenomics_plugin.core.pricing import PricingEngine

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGED_MODELS_CFG = os.path.join(
    REPO_ROOT, "adk_tokenomics_plugin", "models_config.json"
)


def _sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _has_real_bigquery() -> bool:
    try:
        return importlib.util.find_spec("google.cloud.bigquery") is not None
    except ModuleNotFoundError:
        return False


def _build_scoped_bq_stub_modules():
    google_mod = types.ModuleType("google")
    google_mod.__path__ = []  # type: ignore[attr-defined]
    cloud_mod = types.ModuleType("google.cloud")
    cloud_mod.__path__ = []  # type: ignore[attr-defined]
    bq_mod = types.ModuleType("google.cloud.bigquery")
    exc_mod = types.ModuleType("google.cloud.exceptions")

    class StubScalarQueryParameter:
        def __init__(self, name, type_, value):
            self.name = name
            self.type_ = type_
            self.value = value

    class StubQueryJobConfig:
        def __init__(self, *args, **kwargs):
            self.query_parameters = list(kwargs.get("query_parameters") or [])

    class StubBQClient:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("Unpatched StubBQClient")

    class StubNotFound(Exception):
        pass

    bq_mod.Client = StubBQClient  # type: ignore[attr-defined]
    bq_mod.QueryJobConfig = StubQueryJobConfig  # type: ignore[attr-defined]
    bq_mod.ScalarQueryParameter = StubScalarQueryParameter  # type: ignore[attr-defined]
    exc_mod.NotFound = StubNotFound  # type: ignore[attr-defined]
    google_mod.cloud = cloud_mod  # type: ignore[attr-defined]
    cloud_mod.bigquery = bq_mod  # type: ignore[attr-defined]
    cloud_mod.exceptions = exc_mod  # type: ignore[attr-defined]

    updates = {
        "google.cloud.bigquery": bq_mod,
        "google.cloud.exceptions": exc_mod,
    }
    if "google" not in sys.modules:
        updates["google"] = google_mod
    if "google.cloud" not in sys.modules:
        updates["google.cloud"] = cloud_mod
    return updates, bq_mod


class _CaptureJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        if isinstance(self._rows, Exception):
            raise self._rows
        return list(self._rows)


class TestBigQueryExplorer(unittest.TestCase):
    def setUp(self):
        self._packaged_sha_before = _sha256_file(PACKAGED_MODELS_CFG)
        self._orig_web_dir = server.WEB_DIR
        self._tmp_xdg = tempfile.TemporaryDirectory(prefix="tokenomics_bq_xdg_")
        self.addCleanup(self._tmp_xdg.cleanup)

        self._env_patcher = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": self._tmp_xdg.name,
                "TOKENOMICS_MODE": "prod",
                "TOKENOMICS_SINK": "bigquery",
            },
            clear=False,
        )
        self._env_patcher.start()
        os.environ.pop("TOKENOMICS_MODELS_CONFIG_PATH", None)
        self.addCleanup(self._env_patcher.stop)

        if not _has_real_bigquery():
            updates, bq_mod = _build_scoped_bq_stub_modules()
            self._bq_patcher = patch.dict(sys.modules, updates)
            self._bq_patcher.start()
            if "google" in sys.modules and not hasattr(
                sys.modules["google"], "cloud"
            ):
                sys.modules["google"].cloud = sys.modules["google.cloud"]  # type: ignore[attr-defined]
            sys.modules["google.cloud"].bigquery = bq_mod  # type: ignore[attr-defined]
            self.addCleanup(self._bq_patcher.stop)

        server.TokenomicsWebHandler.set_server_context(
            project_id="mock-gcp-project",
            dataset_id="bq_adk_ds",
            table_id="adk_agent_events",
            view_id="v_llm_response",
            traces_path=None,
        )
        self.handler = object.__new__(server.TokenomicsWebHandler)
        self.addCleanup(self._reset_state)

    def _reset_state(self):
        server.WEB_DIR = self._orig_web_dir
        server.TokenomicsWebHandler.set_server_context(
            project_id="",
            dataset_id="bq_adk_ds",
            table_id="adk_agent_events",
            view_id="v_llm_response",
            traces_path=None,
        )
        server.TokenomicsWebHandler.pricing = PricingEngine()
        server.TokenomicsWebHandler._clear_bq_cache()
        sha_after = _sha256_file(PACKAGED_MODELS_CFG)
        self.assertEqual(self._packaged_sha_before, sha_after)

    def test_p0_2_bq_config_sql_injection_rejected_and_identifiers_validated(
        self,
    ):
        httpd = server.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), server.TokenomicsWebHandler
        )
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]

        before_cfg = self.handler.get_bq_config()
        sqli_payloads = [
            {
                "project_id": "victim-proj",
                "dataset_id": "bq_adk_ds.adk_agent_events`; DROP TABLE `victim-proj.bq_adk_ds.adk_agent_events`; SELECT 1 FROM `x",
            },
            {"project_id": "bad`proj; DROP TABLE x"},
            {"table_id": "adk_agent_events` WHERE 1=1; --"},
            {"view_id": "v_llm_response; SELECT 1"},
        ]
        for evil in sqli_payloads:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/bq/config",
                data=json.dumps(evil).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Origin": f"http://127.0.0.1:{port}",
                },
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=3)
            self.assertEqual(ctx.exception.code, 400)
            ctx.exception.close()
            self.assertEqual(self.handler.get_bq_config(), before_cfg)

        with self.assertRaises(ValueError):
            server.TokenomicsWebHandler.set_server_context(
                project_id="evil; DROP TABLE x"
            )
        with self.assertRaises(ValueError):
            server.TokenomicsWebHandler.set_server_context(
                project_id="valid-proj", dataset_id="bad`ds"
            )
        with self.assertRaises(ValueError):
            server.run_web_server(
                project_id="valid-proj",
                table_id="bad-table; --",
                open_browser=False,
            )

        # Defense-in-depth: even if an attribute is mutated directly in memory, SQL query methods never send SQLi
        captured = []

        class CaptureClient:
            def __init__(self, project=None, **kwargs):
                pass

            def query(self, sql, job_config=None):
                captured.append(sql)
                return _CaptureJob([])

        tampered = object.__new__(server.TokenomicsWebHandler)
        tampered.project_id = "mock-gcp-project"
        tampered.dataset_id = "bq_adk_ds`; DROP TABLE x; --"
        with patch("google.cloud.bigquery.Client", CaptureClient):
            res_stats = tampered.fetch_bq_stats()
            res_logs = tampered.fetch_bq_explorer_logs()
            res_fin = tampered.fetch_finops_summary()
            res_met = tampered.fetch_metrics(session_id="global")
        self.assertEqual(res_stats["status"], "error")
        self.assertEqual(res_logs["status"], "error")
        self.assertEqual(res_fin["status"], "error")
        self.assertEqual(res_met["turns"], [])
        self.assertEqual(captured, [])

    def test_p1_6_and_p1_3_fetch_bq_explorer_logs_filters_params_and_limit_clamp(
        self,
    ):
        captured_calls = []
        count_rows = [{"total": 42}]
        data_rows = [
            {
                "timestamp": "2026-08-27T12:00:00Z",
                "event_type": "LLM_RESPONSE",
                "agent": "skills_app",
                "session_id": "sess-123",
                "invocation_id": "inv-1",
                "model_version": "publishers/google/models/gemini-3.8-flash",
                "usage_prompt_tokens": 1200,
                "usage_cached_tokens": 200,
                "usage_completion_tokens": 250,
                "total_ms": 310,
                "user_query": "What is the capital of France?",
                "agent_response": "Paris is the capital.",
                "attributes": json.dumps(
                    {
                        "usage_metadata": {"thoughts_token_count": 50},
                        "tools_called": ["activate_skill(paris-travel)"],
                        "skills_active": ["paris-travel"],
                    }
                ),
                "content": "{}",
            }
        ]
        jobs = [_CaptureJob(count_rows), _CaptureJob(data_rows)]

        class FakeClient:
            def __init__(self, project=None, **kwargs):
                self.project = project

            def query(self, sql, job_config=None):
                captured_calls.append((sql, job_config))
                return jobs.pop(0)

        with patch("google.cloud.bigquery.Client", FakeClient):
            result = self.handler.fetch_bq_explorer_logs(
                limit=99999,
                offset=0,
                app_name="skills_app",
                search="Paris",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total_rows"], 42)
        self.assertEqual(len(captured_calls), 2)

        count_sql, count_cfg = captured_calls[0]
        data_sql, data_cfg = captured_calls[1]
        for sql, cfg in ((count_sql, count_cfg), (data_sql, data_cfg)):
            self.assertIn("@app", sql)
            self.assertIn("@q", sql)
            self.assertIn("TIMESTAMP_SUB", sql)
            self.assertNotIn("SELECT *", sql)
            self.assertNotIn(";", sql)
            self.assertIsNotNone(cfg)
            params = {p.name: p.value for p in cfg.query_parameters}
            self.assertEqual(params["app"], "skills_app")
            self.assertEqual(params["q"], "Paris")

        self.assertIn("LIMIT 500 OFFSET 0", data_sql)

        row = result["rows"][0]
        self.assertEqual(row["session_id"], "sess-123")
        self.assertEqual(row["app_name"], "skills_app")
        self.assertEqual(row["prompt_tokens"], 1200)
        self.assertEqual(row["input_tokens"], 1000)
        self.assertEqual(row["cached_tokens"], 200)
        self.assertEqual(row["output_tokens"], 250)
        self.assertEqual(row["thinking_tokens"], 50)
        expected_cost = PricingEngine().calculate_cost(
            "publishers/google/models/gemini-3.8-flash", 1000, 200, 250, 50
        )
        self.assertAlmostEqual(row["estimated_cost"], expected_cost, places=6)

    def test_p1_7_fetch_bq_stats_groups_by_model_and_sums_per_model_cost(self):
        captured_sql = []
        grouped_rows = [
            {
                "model_version": "claude-opus-4",
                "total_turns": 5,
                "total_input": 1_000_000,
                "total_cached": 200_000,
                "total_output": 100_000,
                "total_thinking": 0,
                "unique_sessions": 3,
                "fleet_unique_sessions": 4,
            },
            {
                "model_version": "publishers/google/models/gemini-3.8-flash",
                "total_turns": 10,
                "total_input": 1_000_000,
                "total_cached": 400_000,
                "total_output": 100_000,
                "total_thinking": 0,
                "unique_sessions": 2,
                "fleet_unique_sessions": 4,
            },
        ]

        class FakeClient:
            def __init__(self, project=None, **kwargs):
                self.project = project

            def query(self, sql, job_config=None):
                captured_sql.append(sql)
                return _CaptureJob(grouped_rows)

        with patch("google.cloud.bigquery.Client", FakeClient):
            stats = self.handler.fetch_bq_stats()

        self.assertEqual(stats["status"], "success")
        self.assertEqual(len(captured_sql), 1)
        self.assertIn("GROUP BY model_version", captured_sql[0])
        self.assertIn("TIMESTAMP_SUB", captured_sql[0])
        self.assertNotIn("ANY_VALUE(model_version)", captured_sql[0])
        self.assertNotIn(";", captured_sql[0])

        pe = PricingEngine()
        expected_cost = pe.calculate_cost(
            "claude-opus-4", 800_000, 200_000, 100_000, 0
        ) + pe.calculate_cost(
            "publishers/google/models/gemini-3.8-flash",
            600_000,
            400_000,
            100_000,
            0,
        )
        self.assertAlmostEqual(stats["total_cost"], expected_cost, places=5)
        self.assertEqual(stats["total_turns"], 15)
        self.assertEqual(stats["total_input"], 2_000_000)
        self.assertEqual(stats["total_fresh_input"], 1_400_000)
        self.assertEqual(stats["total_cached"], 600_000)
        self.assertEqual(stats["total_output"], 200_000)
        self.assertEqual(stats["unique_sessions"], 4)

    def test_p1_8_fetch_metrics_bq_partition_filter_no_select_star_and_ttl_cache(
        self,
    ):
        captured_calls = []
        sample_rows = [
            {
                "timestamp": "2026-10-05T12:00:00Z",
                "event_type": "LLM_RESPONSE",
                "agent": "planner",
                "session_id": "sess-bq-cache",
                "invocation_id": "inv-1",
                "model_version": "claude-opus-4",
                "usage_prompt_tokens": 100_000,
                "usage_cached_tokens": 80_000,
                "usage_completion_tokens": 1_000,
                "total_ms": 500,
                "user_query": "Design architecture",
                "agent_response": "Here is the plan.",
                "attributes": "{}",
                "content": "{}",
            }
        ]

        class FakeClient:
            def __init__(self, project=None, **kwargs):
                self.project = project

            def query(self, sql, job_config=None):
                captured_calls.append((sql, job_config))
                return _CaptureJob(sample_rows)

        with patch("google.cloud.bigquery.Client", FakeClient):
            m1 = self.handler.fetch_metrics(session_id="sess-bq-cache")
            m2 = self.handler.fetch_metrics(session_id="sess-bq-cache")

        self.assertEqual(
            len(captured_calls),
            1,
            "Second fetch_metrics within 10s TTL must hit cache!",
        )
        sql_sent, cfg_sent = captured_calls[0]
        self.assertIn(
            "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)", sql_sent
        )
        self.assertNotIn("SELECT *", sql_sent)
        self.assertIsNotNone(cfg_sent)
        self.assertEqual(m1["source"], "bigquery")
        self.assertEqual(m1["total_turns"], 1)
        self.assertEqual(m1["total_cost"], m2["total_cost"])

        pe = PricingEngine()
        bd = pe.calculate_turn_cost("claude-opus-4", 20_000, 80_000, 1_000, 0)
        self.assertAlmostEqual(m1["total_cost"], bd["total_cost"], places=6)
        self.assertAlmostEqual(
            m1["savings_dollars"], bd["savings_dollars"], places=6
        )

    def test_fetch_bq_explorer_logs_base_table_fallback(self):
        adk_event_row = {
            "timestamp": "2026-09-30T10:00:00Z",
            "event_type": "LLM_RESPONSE",
            "agent": "travel_concierge",
            "session_id": "bq-sess-999",
            "invocation_id": "inv-99",
            "content": json.dumps(
                {
                    "user_query": "Find flights to Tokyo",
                    "response": {
                        "parts": [{"text": "Here are 3 flights to Tokyo."}]
                    },
                    "usage": {"prompt": 2400, "completion": 180, "total": 2580},
                }
            ),
            "attributes": json.dumps(
                {
                    "model_version": "gemini-3.8-flash",
                    "usage_metadata": {
                        "prompt_token_count": 2400,
                        "cached_content_token_count": 1800,
                        "candidates_token_count": 180,
                        "thoughts_token_count": 40,
                    },
                    "tools_called": ["search_flights"],
                }
            ),
            "latency_ms": json.dumps({"total_ms": 640}),
        }
        jobs = [
            _CaptureJob(RuntimeError("Not found: Table v_llm_response")),
            _CaptureJob([{"total": 1}]),
            _CaptureJob([adk_event_row]),
        ]

        class FakeClient:
            def __init__(self, project=None, **kwargs):
                self.project = project

            def query(self, sql, job_config=None):
                return jobs.pop(0)

        with patch("google.cloud.bigquery.Client", FakeClient):
            result = self.handler.fetch_bq_explorer_logs(limit=25, offset=0)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["source"], "bigquery")
        self.assertEqual(
            result["target"], "mock-gcp-project.bq_adk_ds.adk_agent_events"
        )
        self.assertEqual(len(result["rows"]), 1)
        row = result["rows"][0]
        self.assertEqual(row["session_id"], "bq-sess-999")
        self.assertEqual(row["app_name"], "travel_concierge")
        self.assertEqual(row["prompt_tokens"], 2400)
        self.assertEqual(row["input_tokens"], 600)
        self.assertEqual(row["cached_tokens"], 1800)
        self.assertEqual(row["output_tokens"], 180)
        self.assertEqual(row["thinking_tokens"], 40)
        self.assertEqual(row["latency_ms"], 640)
        self.assertEqual(row["agent_response"], "Here are 3 flights to Tokyo.")

    def test_fetch_bq_explorer_logs_never_fakes_local_traces(self):
        class FailingClient:
            def __init__(self, project=None, **kwargs):
                pass

            def query(self, sql, job_config=None):
                raise RuntimeError("404 Dataset bq_adk_ds not found")

        with patch("google.cloud.bigquery.Client", FailingClient):
            result = self.handler.fetch_bq_explorer_logs(limit=25, offset=0)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["rows"], [])

    def test_prod_bq_rate_card_recalculation_and_single_models_config_parity(
        self,
    ):
        self.assertTrue(os.path.exists(PACKAGED_MODELS_CFG))
        self.assertFalse(
            os.path.exists(os.path.join(REPO_ROOT, "models_config.json"))
        )
        self.assertFalse(
            os.path.exists(
                os.path.join(
                    REPO_ROOT,
                    "adk_tokenomics_plugin",
                    "web",
                    "models_config.json",
                )
            )
        )
        with open(PACKAGED_MODELS_CFG, encoding="utf-8") as cf:
            self.assertEqual(len(json.load(cf).get("models", [])), 20)

        with tempfile.TemporaryDirectory() as tmp_web_dir:
            shutil.copy(
                PACKAGED_MODELS_CFG,
                os.path.join(tmp_web_dir, "models_config.json"),
            )
            with patch.object(server, "WEB_DIR", tmp_web_dir):
                server.TokenomicsWebHandler.set_server_context(
                    project_id="prod-bq-proj", traces_path=None
                )
                real_handler = object.__new__(server.TokenomicsWebHandler)

                real_handler.upsert_model_entry(
                    {
                        "id": "publishers/google/models/gemini-4-argon",
                        "name": "Gemini 4 Argon",
                        "provider": "google",
                        "pricing": {
                            "input": 10.0,
                            "cached": 2.0,
                            "output": 50.0,
                        },
                    }
                )

                stats_row = {
                    "total_turns": 5,
                    "model_version": "publishers/google/models/gemini-4-argon",
                    "total_input": 1_000_000,
                    "total_cached": 0,
                    "total_output": 100_000,
                    "total_thinking": 0,
                    "unique_sessions": 1,
                }
                grp_row = {
                    "model_name": "publishers/google/models/gemini-4-argon",
                    "app_name": "flight_agent",
                    "turns": 5,
                    "input_tokens": 500_000,
                    "cached_tokens": 500_000,
                    "output_tokens": 80_000,
                    "thinking_tokens": 20_000,
                }
                tl_row = {
                    "date_label": "2026-10-20",
                    "model_name": "publishers/google/models/gemini-4-argon",
                    "turns": 5,
                    "input_tokens": 500_000,
                    "cached_tokens": 500_000,
                    "output_tokens": 80_000,
                    "thinking_tokens": 20_000,
                    "daily_tokens": 1_100_000,
                }
                prof_row = {
                    "timestamp": "2026-10-20T12:00:00Z",
                    "agent": "flight_agent",
                    "session_id": "bq-sess-parity",
                    "invocation_id": "inv-bq-1",
                    "sub_agent": "flight_agent",
                    "orchestration_flow": "Parallel",
                    "user_query": "Book flight to Tokyo",
                    "prompt_tokens": 6000,
                    "cached_tokens": 0,
                    "output_tokens": 200,
                    "thinking_tokens": 600,
                    "latency_ms": 420,
                    "model_name": "publishers/google/models/gemini-4-argon",
                }
                jobs = [
                    _CaptureJob([stats_row]),
                    _CaptureJob([grp_row]),
                    _CaptureJob([tl_row]),
                    _CaptureJob([prof_row]),
                ]

                class FakeClient:
                    def __init__(self, project=None, **kwargs):
                        self.project = project

                    def query(self, sql, job_config=None):
                        return jobs.pop(0)

                with patch("google.cloud.bigquery.Client", FakeClient):
                    bq_stats = real_handler.fetch_bq_stats()
                    self.assertEqual(bq_stats["status"], "success")
                    self.assertAlmostEqual(
                        bq_stats["total_cost"], 15.0, places=4
                    )

                    finops = real_handler.fetch_finops_summary()
                    self.assertAlmostEqual(
                        finops["kpis"]["total_spend"], 11.0, places=4
                    )
                    self.assertAlmostEqual(
                        finops["kpis"]["optimization_savings_dollars"],
                        4.0,
                        places=4,
                    )

                    recs = profile_from_bigquery(
                        session_id="bq-sess-parity",
                        project_id="prod-bq-proj",
                        output_format="json",
                    )
                    self.assertTrue(
                        any("ContextCacheConfig" in r for r in recs)
                    )
                    self.assertTrue(
                        any("Thinking Budget Tuning" in r for r in recs)
                    )

    def test_p1_2_bq_explorer_sql_json_column_compatibility_and_hyphen_table_names(
        self,
    ):
        server.TokenomicsWebHandler.set_server_context(
            project_id="mock-gcp-project",
            dataset_id="bq_adk_ds",
            table_id="2026_adk-events",
            view_id="v-llm_response",
            traces_path=None,
        )
        captured_sqls = []
        jobs = [
            _CaptureJob(RuntimeError("Not found: View v-llm_response")),
            _CaptureJob([{"total": 1}]),
            _CaptureJob(
                [
                    {
                        "timestamp": "2026-11-01T12:00:00Z",
                        "event_type": "LLM_RESPONSE",
                        "agent": "planner",
                        "session_id": "sess-json-1",
                        "invocation_id": "inv-json-1",
                        "content": {
                            "user_query": "Hello JSON",
                            "response": {"parts": [{"text": "World"}]},
                        },
                        "attributes": {
                            "model_version": "gemini-3.8-flash",
                            "usage_metadata": {
                                "prompt_token_count": 500,
                                "cached_content_token_count": 100,
                                "candidates_token_count": 50,
                            },
                        },
                        "latency_ms": {"total_ms": 120},
                    }
                ]
            ),
        ]

        class FakeClient:
            def __init__(self, project=None, **kwargs):
                self.project = project

            def query(self, sql, job_config=None):
                captured_sqls.append(sql)
                return jobs.pop(0)

        with patch("google.cloud.bigquery.Client", FakeClient):
            res = self.handler.fetch_bq_explorer_logs(
                limit=10, offset=0, app_name="planner", search="Hello"
            )

        self.assertEqual(res["status"], "success")
        self.assertEqual(
            res["target"], "mock-gcp-project.bq_adk_ds.2026_adk-events"
        )
        self.assertEqual(len(captured_sqls), 3)
        self.assertIn(
            "`mock-gcp-project.bq_adk_ds.v-llm_response`", captured_sqls[0]
        )
        self.assertIn(
            "`mock-gcp-project.bq_adk_ds.2026_adk-events`", captured_sqls[1]
        )
        self.assertIn(
            "`mock-gcp-project.bq_adk_ds.2026_adk-events`", captured_sqls[2]
        )

        for sql in captured_sqls:
            self.assertIn("CONTAINS_SUBSTR(content, @q)", sql)
            self.assertIn("CONTAINS_SUBSTR(attributes, @q)", sql)
            self.assertNotIn("COALESCE(content,", sql)
            self.assertNotIn("COALESCE(attributes,", sql)
            self.assertNotIn("SAFE_CAST(latency_ms AS", sql)

    def test_p1_1_and_p2_2_bq_truncated_window_flag_and_malformed_row_resilience(
        self,
    ):
        base_row = {
            "timestamp": "2026-11-01T00:00:00Z",
            "event_type": "LLM_RESPONSE",
            "agent": "planner",
            "session_id": "s-ok",
            "invocation_id": "inv-ok",
            "model_version": "gemini-3.8-flash",
            "usage_prompt_tokens": 1000,
            "usage_cached_tokens": 0,
            "usage_completion_tokens": 100,
            "total_ms": 20,
            "attributes": "{}",
            "content": "{}",
        }
        malformed_row = dict(
            base_row,
            session_id="s-ok",
            usage_prompt_tokens=None,
            attributes=json.dumps(
                {"usage_metadata": {"prompt_token_count": "12x"}}
            ),
        )
        jobs = [_CaptureJob([base_row, malformed_row])]

        class FakeClient:
            def __init__(self, project=None, **kwargs):
                self.project = project

            def query(self, sql, job_config=None):
                return jobs.pop(0)

        with patch("google.cloud.bigquery.Client", FakeClient):
            m_bad = self.handler.fetch_metrics(session_id="s-ok")

        self.assertEqual(m_bad["source"], "bigquery")
        self.assertGreaterEqual(len(m_bad["turns"]), 1)
        self.assertFalse(m_bad["turns_truncated"])

        server.TokenomicsWebHandler._clear_bq_cache()
        many_rows = [
            dict(
                base_row,
                session_id="global",
                timestamp=f"2026-11-01T00:{i // 60:02d}:{i % 60:02d}Z",
            )
            for i in range(500)
        ]
        agg_rows = [
            {
                "app_name": "planner",
                "model_name": "gemini-3.8-flash",
                "turns": 5000,
                "prompt_tokens": 5_000_000,
                "cached_tokens": 0,
                "output_tokens": 500_000,
                "thinking_tokens": 0,
            }
        ]
        jobs = [_CaptureJob(many_rows), _CaptureJob(agg_rows)]
        with patch("google.cloud.bigquery.Client", FakeClient):
            m_big = self.handler.fetch_metrics(session_id="global")

        self.assertTrue(m_big["turns_truncated"])
        self.assertEqual(len(m_big["turns"]), 500)
        self.assertEqual(m_big["total_turns"], 5000)
        self.assertEqual(m_big["metrics"]["planner"]["turns"], 5000)
        self.assertAlmostEqual(m_big["total_cost"], 5.625, places=6)
        self.assertAlmostEqual(
            m_big["metrics"]["planner"]["cost"], 5.625, places=6
        )

    def test_v102_c1_parse_bq_event_row_eight_cell_matrix_and_function_call_extraction(
        self,
    ):
        from adk_tokenomics_plugin.core.config import TokenomicsConfig
        from adk_tokenomics_plugin.core.sinks import BigQuerySink

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

        pe = PricingEngine()
        expected_cost = pe.calculate_cost("gemini-3.8-flash", 900, 300, 150, 50)
        for cell_name, row_payload in eight_cells.items():
            with self.subTest(cell=cell_name):
                parsed = server.TokenomicsWebHandler._parse_bq_event_row(
                    row_payload, pricing=pe
                )
                self.assertEqual(
                    parsed["event_type"], "LLM_RESPONSE", msg=cell_name
                )
                self.assertEqual(parsed["prompt_tokens"], 1200, msg=cell_name)
                self.assertEqual(parsed["input_tokens"], 900, msg=cell_name)
                self.assertEqual(parsed["cached_tokens"], 300, msg=cell_name)
                self.assertEqual(parsed["output_tokens"], 150, msg=cell_name)
                self.assertEqual(parsed["thinking_tokens"], 50, msg=cell_name)
                self.assertEqual(parsed["latency_ms"], 85, msg=cell_name)
                self.assertAlmostEqual(
                    parsed["estimated_cost"],
                    expected_cost,
                    places=6,
                    msg=cell_name,
                )

        # Upstream LLM_RESPONSE row without attributes.tools_called extracts content.parts[].function_call + skills (excluding unload)
        fc_row = {
            "timestamp": "2026-11-04T12:05:00Z",
            "event_type": None,
            "agent": "concierge",
            "session_id": "s_fc",
            "invocation_id": "inv_fc",
            "content": json.dumps(
                {
                    "parts": [
                        {
                            "function_call": {
                                "name": "search_flights",
                                "args": {"city": "Paris", "days": 3},
                            }
                        },
                        {
                            "function_call": {
                                "name": "load_skill",
                                "args": {"skill_name": "eu-travel"},
                            }
                        },
                        {
                            "function_call": {
                                "name": "unload_skill",
                                "args": {"name": "unload"},
                            }
                        },
                    ],
                    "usage": {"prompt": 500, "completion": 40, "total": 540},
                }
            ),
            "attributes": json.dumps({"model_version": "gemini-3.8-flash"}),
        }
        parsed_fc = server.TokenomicsWebHandler._parse_bq_event_row(
            fc_row, pricing=pe
        )
        self.assertEqual(parsed_fc["event_type"], "LLM_RESPONSE")
        self.assertEqual(
            parsed_fc["tools_called"],
            ["search_flights", "load_skill", "unload_skill"],
        )
        self.assertEqual(parsed_fc["skills_active"], ["eu-travel"])
        self.assertNotIn("unload", parsed_fc["skills_active"])

        # Multi-arg tool signature string is NOT split at internal commas
        multi_arg_row = {
            "timestamp": "2026-11-04T12:06:00Z",
            "event_type": "LLM_RESPONSE",
            "agent": "concierge",
            "session_id": "s_fc",
            "invoked_tools": "search(city='Paris', days=3), get_weather(city='Lyon', units='metric')",
            "usage_prompt_tokens": 300,
            "usage_completion_tokens": 20,
        }
        parsed_multi = server.TokenomicsWebHandler._parse_bq_event_row(
            multi_arg_row, pricing=pe
        )
        self.assertEqual(
            parsed_multi["tools_called"],
            [
                "search(city='Paris', days=3)",
                "get_weather(city='Lyon', units='metric')",
            ],
        )

    def test_v102_pre_v102_view_tool_use_fallback_explicit_zero_row_stop_and_no_exceptions_module(
        self,
    ):
        # 1. Explicit project_id overrides TOKENOMICS_MODE=dev / TOKENOMICS_SINK=memory in _is_local_dev_mode
        with patch.dict(
            os.environ,
            {"TOKENOMICS_MODE": "dev", "TOKENOMICS_SINK": "memory"},
            clear=False,
        ):
            server.TokenomicsWebHandler.set_server_context(
                project_id="prod-explicit-proj",
                dataset_id="bq_adk_ds",
                table_id="custom_empty_tbl",
                view_id="v_llm_response",
                traces_path=None,
            )
            h_explicit = object.__new__(server.TokenomicsWebHandler)
            self.assertFalse(
                server.TokenomicsWebHandler._is_local_dev_mode(h_explicit)
            )

            # 2. Without google.cloud.exceptions installed, LookupError("404 Not found") falls back to custom_empty_tbl,
            #    and 0-row success on explicit non-default custom_empty_tbl STOPS without querying adk_agent_events (M6)!
            queried_sqls = []

            class ZeroRowCustomTableClient:
                def __init__(self, project=None, **kwargs):
                    self.project = project

                def query(self, sql, job_config=None):
                    queried_sqls.append(sql)
                    if "`prod-explicit-proj.bq_adk_ds.v_llm_response`" in sql:
                        raise LookupError(
                            "404 Not found: Table prod-explicit-proj:bq_adk_ds.v_llm_response"
                        )
                    if "`prod-explicit-proj.bq_adk_ds.custom_empty_tbl`" in sql:
                        return _CaptureJob([])
                    raise AssertionError(
                        f"Should NOT fall back past 0-row explicit custom table! Got SQL: {sql}"
                    )

            with (
                patch.dict(sys.modules, {"google.cloud.exceptions": None}),
                patch("google.cloud.bigquery.Client", ZeroRowCustomTableClient),
            ):
                m_res = h_explicit.fetch_metrics(session_id="global")
                s_res = h_explicit.fetch_sessions()
                stats_res = h_explicit.fetch_bq_stats()

            self.assertEqual(m_res["source"], "bigquery")
            self.assertEqual(m_res["turns"], [])
            self.assertEqual(m_res["metrics"], {})
            self.assertEqual(len(s_res), 1)
            self.assertEqual(stats_res["status"], "success")
            self.assertTrue(any("custom_empty_tbl" in q for q in queried_sqls))
            self.assertFalse(any("adk_agent_events" in q for q in queried_sqls))

        # 3. Pre-v1.0.2 view raising "Unrecognized name: usage_tool_use_tokens" retries view SQL cleanly
        server.TokenomicsWebHandler.set_server_context(
            project_id="legacy-view-proj",
            dataset_id="bq_adk_ds",
            table_id="adk_agent_events",
            view_id="v_llm_response",
            traces_path=None,
        )
        h_legacy = object.__new__(server.TokenomicsWebHandler)
        legacy_calls = []

        class PreV102ViewClient:
            def __init__(self, project=None, **kwargs):
                self.project = project

            def query(self, sql, job_config=None):
                legacy_calls.append(sql)
                if (
                    "usage_tool_use_tokens" in sql
                    and "'$.usage_tool_use_tokens'" not in sql
                ):
                    raise RuntimeError(
                        "400 Unrecognized name: usage_tool_use_tokens at [5:52]"
                    )
                if "GROUP BY model_version" in sql:
                    return _CaptureJob(
                        [
                            {
                                "model_version": "gemini-3.8-flash",
                                "total_turns": 1,
                                "total_input": 1200,
                                "total_cached": 200,
                                "total_output": 100,
                                "total_thinking": 0,
                                "unique_sessions": 1,
                                "fleet_unique_sessions": 1,
                            }
                        ]
                    )
                if "GROUP BY model_name, app_name" in sql:
                    return _CaptureJob(
                        [
                            {
                                "model_name": "gemini-3.8-flash",
                                "app_name": "planner",
                                "turns": 1,
                                "input_tokens": 1000,
                                "cached_tokens": 200,
                                "output_tokens": 100,
                                "thinking_tokens": 0,
                            }
                        ]
                    )
                if "GROUP BY date_label, model_name" in sql:
                    return _CaptureJob(
                        [
                            {
                                "date_label": "2026-11-04",
                                "model_name": "gemini-3.8-flash",
                                "turns": 1,
                                "input_tokens": 1000,
                                "cached_tokens": 200,
                                "output_tokens": 100,
                                "thinking_tokens": 0,
                                "daily_tokens": 1300,
                            }
                        ]
                    )
                return _CaptureJob([])

        with patch("google.cloud.bigquery.Client", PreV102ViewClient):
            st_legacy = h_legacy.fetch_bq_stats()
            fin_legacy = h_legacy.fetch_finops_summary()

        self.assertEqual(st_legacy["status"], "success")
        self.assertEqual(st_legacy["total_input"], 1200)
        self.assertEqual(fin_legacy["status"], "success")
        self.assertEqual(fin_legacy["kpis"]["total_input"], 1000)
        self.assertFalse(any("adk_agent_events" in q for q in legacy_calls))

        # 4. Non-missing-table error (403 Access Denied) surfaces structured BQ error details on /api/finops/summary
        class DeniedClient:
            def __init__(self, project=None, **kwargs):
                pass

            def query(self, sql, job_config=None):
                raise PermissionError(
                    "403 Access Denied: Project legacy-view-proj: User lacks bigquery.jobs.create"
                )

        with patch("google.cloud.bigquery.Client", DeniedClient):
            fin_err = h_legacy.fetch_finops_summary()
        self.assertEqual(fin_err["status"], "error")
        self.assertIn("403 Access Denied", fin_err["message"])
        self.assertIn("403 Access Denied", fin_err["error"])
        self.assertEqual(fin_err["source"], "bigquery")
        self.assertIn("friendly_title", fin_err)
        self.assertIn("remediation_cmd", fin_err)

    def test_is_missing_bq_table_error_uuid_and_project_digits_do_not_block_fallback(
        self,
    ):
        server.TokenomicsWebHandler.set_server_context(
            project_id="finops-240315",
            dataset_id="bq_adk_ds",
            table_id="adk_agent_events",
            view_id="v_llm_response",
            traces_path=None,
        )
        h_uuid = object.__new__(server.TokenomicsWebHandler)

        stored_event_row = {
            "session_id": "sess-uuid-403",
            "event_type": "LLM_RESPONSE",
            "content": "{}",
            "attributes": json.dumps(
                {
                    "model_version": "gemini-3.8-flash",
                    "usage_metadata": {
                        "prompt_token_count": 1000,
                        "tool_use_prompt_token_count": 200,
                        "cached_content_token_count": 0,
                        "candidates_token_count": 50,
                        "thoughts_token_count": 0,
                    },
                }
            ),
        }
        queried_sqls = []

        class UuidDigitsFallbackClient:
            def __init__(self, project=None, **kwargs):
                self.project = project

            def query(self, sql, job_config=None):
                queried_sqls.append(sql)
                if "`finops-240315.bq_adk_ds.v_llm_response`" in sql:
                    raise LookupError(
                        "404 Not found: Table finops-240315:bq_adk_ds.v_llm_response was not found in location US\n"
                        "Job ID: 7c1a403e-9401-4b2a-8f00-123456789abc"
                    )
                if "`finops-240315.bq_adk_ds.adk_agent_events`" in sql:
                    attrs = json.loads(stored_event_row["attributes"])
                    um = attrs.get("usage_metadata") or {}
                    p_tok = (
                        int(um.get("prompt_token_count", 0))
                        if "'$.usage_metadata.prompt_token_count'" in sql
                        else 0
                    )
                    tu_tok = (
                        int(um.get("tool_use_prompt_token_count", 0))
                        if "'$.usage_metadata.tool_use_prompt_token_count'"
                        in sql
                        else 0
                    )
                    return _CaptureJob(
                        [
                            {
                                "model_version": attrs.get(
                                    "model_version", "gemini-3.8-flash"
                                ),
                                "total_turns": 1,
                                "total_input": p_tok + tu_tok,
                                "total_cached": int(
                                    um.get("cached_content_token_count", 0)
                                ),
                                "total_output": int(
                                    um.get("candidates_token_count", 0)
                                ),
                                "total_thinking": int(
                                    um.get("thoughts_token_count", 0)
                                ),
                                "unique_sessions": 1,
                                "fleet_unique_sessions": 1,
                            }
                        ]
                    )
                raise AssertionError(f"Unexpected SQL target: {sql}")

        with patch("google.cloud.bigquery.Client", UuidDigitsFallbackClient):
            stats = h_uuid.fetch_bq_stats()

        self.assertEqual(stats["status"], "success")
        self.assertEqual(stats["total_input"], 1200)
        self.assertTrue(
            any(
                "`finops-240315.bq_adk_ds.adk_agent_events`" in q
                for q in queried_sqls
            )
        )

        self.assertIs(
            server.TokenomicsWebHandler._is_missing_bq_table_error(
                LookupError("404 Not found: Dataset proj-1401:bq_adk_ds")
            ),
            True,
        )
        self.assertIs(
            server.TokenomicsWebHandler._is_missing_bq_table_error(
                PermissionError("403 Access Denied: Table acme:ds.tbl")
            ),
            False,
        )

        class Forbidden(Exception):
            pass

        class Unauthorized(Exception):
            pass

        class PermissionDenied(Exception):
            pass

        class NotFound(Exception):
            pass

        self.assertIs(
            server.TokenomicsWebHandler._is_missing_bq_table_error(
                NotFound("Table missing")
            ),
            True,
        )
        self.assertIs(
            server.TokenomicsWebHandler._is_missing_bq_table_error(
                Forbidden("Table not found")
            ),
            False,
        )
        self.assertIs(
            server.TokenomicsWebHandler._is_missing_bq_table_error(
                Unauthorized("Invalid token")
            ),
            False,
        )
        self.assertIs(
            server.TokenomicsWebHandler._is_missing_bq_table_error(
                PermissionDenied("Denied")
            ),
            False,
        )
        self.assertIs(
            server.TokenomicsWebHandler._is_missing_bq_table_error(
                RuntimeError(
                    "400 Syntax error in query\nJob ID: aaaa-0404-bbbb"
                )
            ),
            False,
        )


if __name__ == "__main__":
    unittest.main()
