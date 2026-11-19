# ruff: noqa: PLW1510
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from adk_tokenomics_plugin import cli_web as server
from adk_tokenomics_plugin.cli_profiler import (
    TokenProfiler,
    parse_trace_or_eval_payload,
)
from adk_tokenomics_plugin.core.pricing import PricingEngine

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGED_MODELS_CFG = os.path.join(
    REPO_ROOT, "adk_tokenomics_plugin", "models_config.json"
)
WEB_APP_JS = os.path.join(REPO_ROOT, "adk_tokenomics_plugin", "web", "app.js")
WEB_INDEX_HTML = os.path.join(
    REPO_ROOT, "adk_tokenomics_plugin", "web", "index.html"
)


def _sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


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


class _WebTestBase(unittest.TestCase):
    def setUp(self):
        self._packaged_sha_before = _sha256_file(PACKAGED_MODELS_CFG)
        self._orig_web_dir = server.WEB_DIR
        self._tmp_xdg = tempfile.TemporaryDirectory(prefix="tokenomics_xdg_")
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
        os.environ.pop("TOKENOMICS_ALLOWED_HOSTS", None)
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
        self.addCleanup(self._reset_handler_state)

    def _reset_handler_state(self):
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
        self.assertEqual(
            self._packaged_sha_before,
            sha_after,
            "Packaged adk_tokenomics_plugin/models_config.json was mutated during test!",
        )


class _CaptureJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        if isinstance(self._rows, Exception):
            raise self._rows
        return list(self._rows)


class TestFinOpsDashboard(_WebTestBase):
    def test_fetch_finops_summary_real_pricing_and_coalesce_sql(self):
        captured_sql = []
        group_rows = [
            {
                "model_name": "publishers/google/models/gemini-3.8-flash",
                "app_name": "caching_app",
                "turns": 10,
                "input_tokens": 50_000,
                "cached_tokens": 150_000,
                "output_tokens": 4_000,
                "thinking_tokens": 1_000,
            },
            {
                "model_name": "claude-sonnet-5",
                "app_name": "compaction_app",
                "turns": 5,
                "input_tokens": 80_000,
                "cached_tokens": 20_000,
                "output_tokens": 2_500,
                "thinking_tokens": 500,
            },
        ]
        timeline_rows = [
            {
                "date_label": "2026-08-27",
                "model_name": "publishers/google/models/gemini-3.8-flash",
                "turns": 10,
                "input_tokens": 50_000,
                "cached_tokens": 150_000,
                "output_tokens": 4_000,
                "thinking_tokens": 1_000,
                "daily_tokens": 205_000,
            },
            {
                "date_label": "2026-08-27",
                "model_name": "claude-sonnet-5",
                "turns": 5,
                "input_tokens": 80_000,
                "cached_tokens": 20_000,
                "output_tokens": 2_500,
                "thinking_tokens": 500,
                "daily_tokens": 103_000,
            },
        ]
        responses = [_CaptureJob(group_rows), _CaptureJob(timeline_rows)]

        class FakeClient:
            def __init__(self, project=None, **kwargs):
                self.project = project

            def query(self, sql, job_config=None):
                captured_sql.append(sql)
                return responses.pop(0)

        handler = object.__new__(server.TokenomicsWebHandler)
        with patch("google.cloud.bigquery.Client", FakeClient):
            result = handler.fetch_finops_summary(
                timeframe="all", provider="all"
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(captured_sql), 2)
        for sql in captured_sql:
            self.assertIn("COALESCE(usage_prompt_tokens, 0)", sql)
            self.assertIn("COALESCE(usage_cached_tokens, 0)", sql)
            self.assertIn(
                "JSON_VALUE(attributes, '$.usage_metadata.thoughts_token_count')",
                sql,
            )
            self.assertNotIn("usage_thinking_tokens", sql)
            self.assertIn(
                "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)", sql
            )

        pe = PricingEngine()
        c_google = pe.calculate_turn_cost(
            "publishers/google/models/gemini-3.8-flash",
            50_000,
            150_000,
            4_000,
            1_000,
        )
        c_claude = pe.calculate_turn_cost(
            "claude-sonnet-5", 80_000, 20_000, 2_500, 500
        )
        expected_spend = c_google["total_cost"] + c_claude["total_cost"]
        expected_savings = (
            c_google["savings_dollars"] + c_claude["savings_dollars"]
        )

        kpis = result["kpis"]
        self.assertAlmostEqual(kpis["total_spend"], expected_spend, places=5)
        self.assertAlmostEqual(
            kpis["optimization_savings_dollars"], expected_savings, places=4
        )
        self.assertEqual(kpis["total_tokens"], 308_000)
        self.assertEqual(kpis["total_input"], 130_000)
        self.assertEqual(kpis["total_cached"], 170_000)
        self.assertEqual(kpis["total_output"], 6_500)
        self.assertEqual(kpis["total_thinking"], 1_500)
        self.assertEqual(kpis["total_turns"], 15)
        self.assertEqual(kpis["top_provider"], "Anthropic Claude")

        pb = result["provider_breakdown"]
        self.assertAlmostEqual(
            pb["Google Gemini"]["spend"], c_google["total_cost"], places=5
        )
        self.assertAlmostEqual(
            pb["Anthropic Claude"]["spend"], c_claude["total_cost"], places=5
        )
        self.assertEqual(len(result["timeline"]), 1)
        self.assertAlmostEqual(
            result["timeline"][0]["daily_cost"], expected_spend, places=5
        )

    def test_fetch_finops_summary_base_table_fallback_and_provider_filter(self):
        captured_sql = []
        group_rows = [
            {
                "model_name": "claude-opus-4",
                "app_name": "planner",
                "turns": 2,
                "input_tokens": 10_000,
                "cached_tokens": 5_000,
                "output_tokens": 1_000,
                "thinking_tokens": 500,
            },
            {
                "model_name": "gemini-3.8-flash",
                "app_name": "router",
                "turns": 4,
                "input_tokens": 4_000,
                "cached_tokens": 0,
                "output_tokens": 200,
                "thinking_tokens": 0,
            },
        ]
        timeline_rows = [
            {
                "date_label": "2026-10-01",
                "model_name": "claude-opus-4",
                "turns": 2,
                "input_tokens": 10_000,
                "cached_tokens": 5_000,
                "output_tokens": 1_000,
                "thinking_tokens": 500,
                "daily_tokens": 16_500,
            }
        ]
        responses = [
            _CaptureJob(
                RuntimeError(
                    "400 BadRequest: Unrecognized name: usage_thinking_tokens"
                )
            ),
            _CaptureJob(group_rows),
            _CaptureJob(timeline_rows),
        ]

        class FakeClient:
            def __init__(self, project=None, **kwargs):
                self.project = project

            def query(self, sql, job_config=None):
                captured_sql.append(sql)
                return responses.pop(0)

        handler = object.__new__(server.TokenomicsWebHandler)
        with patch("google.cloud.bigquery.Client", FakeClient):
            result = handler.fetch_finops_summary(
                timeframe="7d", provider="anthropic"
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(captured_sql), 3)
        self.assertIn("adk_agent_events", captured_sql[1])
        self.assertIn("INTERVAL 7 DAY", captured_sql[1])
        expected_opus = PricingEngine().calculate_cost(
            "claude-opus-4", 10_000, 5_000, 1_000, 500
        )
        self.assertAlmostEqual(
            result["kpis"]["total_spend"], expected_opus, places=5
        )
        self.assertEqual(len(result["matrix_rows"]), 1)
        self.assertEqual(
            result["matrix_rows"][0]["model_name"], "claude-opus-4"
        )

    def test_fetch_finops_summary_empty_and_missing_bigquery_graceful(self):
        class EmptyClient:
            def __init__(self, project=None, **kwargs):
                self.project = project

            def query(self, sql, job_config=None):
                return _CaptureJob([])

        handler = object.__new__(server.TokenomicsWebHandler)
        with patch("google.cloud.bigquery.Client", EmptyClient):
            empty_res = handler.fetch_finops_summary(
                timeframe="today", provider="google"
            )
        self.assertEqual(empty_res["status"], "success")
        self.assertEqual(empty_res["kpis"]["total_spend"], 0.0)
        self.assertEqual(empty_res["matrix_rows"], [])

        with patch.dict(sys.modules, {"google.cloud.bigquery": None}):
            no_bq_res = handler.fetch_finops_summary()
        self.assertEqual(no_bq_res["status"], "error")


class TestWebSecurityAndHttpBreakIt(_WebTestBase):
    def _start_live_server(self, traces_path=None, copy_web=True):
        tmp_web = None
        if copy_web:
            tmp_web_dir = tempfile.TemporaryDirectory(prefix="tokenomics_web_")
            self.addCleanup(tmp_web_dir.cleanup)
            tmp_web = os.path.join(tmp_web_dir.name, "web")
            shutil.copytree(self._orig_web_dir, tmp_web)
            shutil.copy(
                PACKAGED_MODELS_CFG, os.path.join(tmp_web, "models_config.json")
            )
            server.WEB_DIR = tmp_web
        else:
            server.WEB_DIR = self._orig_web_dir

        server.TokenomicsWebHandler.set_server_context(
            project_id="",
            dataset_id="bq_adk_ds",
            table_id="adk_agent_events",
            view_id="v_llm_response",
            traces_path=traces_path,
        )
        httpd = server.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), server.TokenomicsWebHandler
        )
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd, httpd.server_address[1], tmp_web

    @staticmethod
    def _raw_http(port: int, payload: bytes, timeout: float = 3.0) -> bytes:
        with socket.create_connection(
            ("127.0.0.1", port), timeout=timeout
        ) as sock:
            sock.sendall(payload)
            chunks = []
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
            return b"".join(chunks)

    def test_p0_1_csrf_dns_rebinding_cors_and_packaged_config_immutable(self):
        _, port, tmp_web = self._start_live_server(copy_web=True)
        cfg_file = os.path.join(tmp_web, "models_config.json")
        cfg_sha_before = _sha256_file(cfg_file)

        evil_body = json.dumps(
            {
                "id": "publishers/google/models/gemini-3.8-flash",
                "pricing": {"input": 0.0, "cached": 0.0, "output": 0.0},
            }
        )

        # 1. Evil Host + Evil Origin + text/plain -> 403
        r1 = self._raw_http(
            port,
            (
                f"POST /api/models HTTP/1.0\r\n"
                f"Host: evil.example:{port}\r\n"
                f"Origin: https://evil.example\r\n"
                f"Content-Type: text/plain\r\n"
                f"Content-Length: {len(evil_body)}\r\n\r\n"
                f"{evil_body}"
            ).encode(),
        )
        self.assertIn(b"403", r1.split(b"\r\n", 1)[0])
        self.assertNotIn(b"Access-Control-Allow-Origin: *", r1)

        # 2. Local Host + Evil Origin + application/json -> 403
        r2 = self._raw_http(
            port,
            (
                f"POST /api/models HTTP/1.0\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Origin: https://evil.example\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(evil_body)}\r\n\r\n"
                f"{evil_body}"
            ).encode(),
        )
        self.assertIn(b"403", r2.split(b"\r\n", 1)[0])

        # 3. Local Host + No Origin + text/plain simple CSRF -> 403
        r3 = self._raw_http(
            port,
            (
                f"POST /api/models HTTP/1.0\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Content-Type: text/plain\r\n"
                f"Content-Length: {len(evil_body)}\r\n\r\n"
                f"{evil_body}"
            ).encode(),
        )
        self.assertIn(b"403", r3.split(b"\r\n", 1)[0])

        # 4. Cross-origin DELETE and missing Content-Type DELETE -> 403
        r4 = self._raw_http(
            port,
            (
                b"DELETE /api/models?id=claude-3-5-haiku HTTP/1.0\r\n"
                b"Host: evil.example\r\n"
                b"Origin: https://evil.example\r\n\r\n"
            ),
        )
        self.assertIn(b"403", r4.split(b"\r\n", 1)[0])

        # 5. Cross-origin ADK proxy POST /api/run -> 403 before proxying
        r5 = self._raw_http(
            port,
            (
                f"POST /api/run HTTP/1.0\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Origin: https://evil.example\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: 2\r\n\r\n{{}}"
            ).encode(),
        )
        self.assertIn(b"403", r5.split(b"\r\n", 1)[0])

        # Verify file sha256 was untouched across all attack vectors
        cfg_sha_after = _sha256_file(cfg_file)
        self.assertEqual(cfg_sha_before, cfg_sha_after)

        # 6. Verify CORS header is echoed ONLY for exact local Origin
        r_good_cors = self._raw_http(
            port,
            f"GET /api/models HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nOrigin: http://127.0.0.1:{port}\r\n\r\n".encode(),
        )
        hdr_block = r_good_cors.split(b"\r\n\r\n", 1)[0].decode("utf-8")
        self.assertIn(
            f"Access-Control-Allow-Origin: http://127.0.0.1:{port}", hdr_block
        )
        self.assertNotIn("Access-Control-Allow-Origin: *", hdr_block)

        # 7. Default WEB_DIR write goes to user XDG path, NEVER packaged models_config.json
        _, default_port, _ = self._start_live_server(copy_web=False)
        valid_upsert = json.dumps(
            {
                "id": "custom/test-safe-override",
                "name": "Test Safe Override",
                "provider": "custom",
                "pricing": {"input": 1.25, "cached": 0.25, "output": 5.0},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{default_port}/api/models",
            data=valid_upsert,
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{default_port}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            self.assertEqual(resp.status, 200)
        expected_user_cfg = os.path.join(
            self._tmp_xdg.name, "adk-tokenomics", "models_config.json"
        )
        self.assertTrue(os.path.exists(expected_user_cfg))
        pkg_sha_now = _sha256_file(PACKAGED_MODELS_CFG)
        self.assertEqual(self._packaged_sha_before, pkg_sha_now)

    def test_p0_3_nan_infinity_negative_and_excessive_rates_rejected(self):
        _, port, tmp_web = self._start_live_server(copy_web=True)
        cfg_file = os.path.join(tmp_web, "models_config.json")

        bad_payloads = [
            {
                "id": "bad-nan",
                "pricing": {"input": "NaN", "cached": 0.1, "output": 1.0},
            },
            {
                "id": "bad-inf",
                "pricing": {"input": 1.0, "cached": "nan", "output": "inf"},
            },
            {
                "id": "bad-neg",
                "pricing": {"input": -5.0, "cached": 0.0, "output": 1.0},
            },
            {
                "id": "bad-huge",
                "pricing": {"input": 10000.01, "cached": 0.0, "output": 1.0},
            },
            {
                "id": "bad-bool",
                "pricing": {"input": True, "cached": 0.0, "output": 1.0},
            },
        ]
        for bad in bad_payloads:
            body = json.dumps(bad)
            raw_resp = self._raw_http(
                port,
                (
                    f"POST /api/models HTTP/1.0\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                    f"{body}"
                ).encode(),
            )
            self.assertIn(
                b"400", raw_resp.split(b"\r\n", 1)[0], f"Expected 400 for {bad}"
            )

        # Raw bare NaN token in JSON text -> 400
        bare_nan = (
            '{"id":"bare-nan","pricing":{"input":NaN,"cached":0,"output":1}}'
        )
        raw_nan_resp = self._raw_http(
            port,
            (
                f"POST /api/models HTTP/1.0\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(bare_nan)}\r\n\r\n"
                f"{bare_nan}"
            ).encode(),
        )
        self.assertIn(b"400", raw_nan_resp.split(b"\r\n", 1)[0])

        def _forbid_const(c):
            raise ValueError(f"Forbidden constant {c}")

        disk_parsed = json.loads(
            _read_text(cfg_file), parse_constant=_forbid_const
        )
        self.assertEqual(len(disk_parsed["models"]), 20)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/models", timeout=3
        ) as resp:
            api_text = resp.read().decode("utf-8")
        api_parsed = json.loads(api_text, parse_constant=_forbid_const)
        self.assertEqual(len(api_parsed["models"]), 20)

    def test_p1_2_concurrent_upserts_no_lost_updates_and_valid_json(self):
        _, port, tmp_web = self._start_live_server(copy_web=True)
        cfg_file = os.path.join(tmp_web, "models_config.json")
        errors = []

        def _upsert_worker(idx: int):
            try:
                body = json.dumps(
                    {
                        "id": f"concurrent-model-{idx}",
                        "name": f"Concurrent Model {idx}",
                        "provider": "custom",
                        "pricing": {
                            "input": 1.0 + idx * 0.01,
                            "cached": 0.1,
                            "output": 2.0,
                        },
                    }
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/models",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status != 200:
                        errors.append(f"HTTP {resp.status}")
            except Exception as exc:
                errors.append(str(exc))

        def _reader_worker():
            for _ in range(10):
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/models", timeout=10
                    ) as resp:
                        raw = resp.read().decode("utf-8")
                    parsed = json.loads(raw)
                    if not isinstance(parsed.get("models"), list):
                        errors.append("Invalid models list")
                except Exception as exc:
                    errors.append(str(exc))

        threads = [
            threading.Thread(target=_upsert_worker, args=(i,))
            for i in range(20)
        ]
        readers = [threading.Thread(target=_reader_worker) for _ in range(3)]
        for t in threads + readers:
            t.start()
        for t in threads + readers:
            t.join()

        self.assertEqual(errors, [])
        persisted = json.loads(_read_text(cfg_file))
        ids = {m["id"] for m in persisted["models"]}
        for i in range(20):
            self.assertIn(f"concurrent-model-{i}", ids)

    def test_p1_3_malformed_content_length_oversized_payload_and_bad_query_params(
        self,
    ):
        _, port, _ = self._start_live_server(copy_web=True)

        for bad_cl in ("abc", "-5"):
            resp = self._raw_http(
                port,
                f"POST /api/models HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nContent-Length: {bad_cl}\r\n\r\n{{}}".encode(),
            )
            self.assertIn(b"400", resp.split(b"\r\n", 1)[0])

        import time as _time

        t0 = _time.monotonic()
        resp_413 = self._raw_http(
            port,
            (
                f"POST /api/models HTTP/1.0\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: 1000000000\r\n\r\n{{}}"
            ).encode(),
            timeout=2.5,
        )
        elapsed = _time.monotonic() - t0
        self.assertIn(b"413", resp_413.split(b"\r\n", 1)[0])
        self.assertLess(elapsed, 1.5)

        for bad_qs in ("limit=abc", "limit=0", "offset=-3", "offset=xyz"):
            resp_qs = self._raw_http(
                port,
                f"GET /api/bq/logs?{bad_qs} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode(),
            )
            self.assertIn(
                b"400",
                resp_qs.split(b"\r\n", 1)[0],
                f"Expected 400 for {bad_qs}",
            )

    def test_p0_4_and_p1_10_local_trace_variants_v1_to_v4_profiler_parity(self):
        import contextlib
        import io

        pe = PricingEngine()
        variants = {
            "V1_eval_full_cache": {
                "payload": {
                    "eval_case_results": [
                        {
                            "eval_id": "c1",
                            "session_id": "s-eval",
                            "eval_metric_result_per_invocation": [
                                {
                                    "actual_invocation": {
                                        "invocation_id": "i1",
                                        "user_content": {
                                            "parts": [{"text": "hi"}]
                                        },
                                        "model_version": "gemini-3.8-flash",
                                        "usage_metadata": {
                                            "prompt_token_count": 5000,
                                            "cached_content_token_count": 5000,
                                            "candidates_token_count": 100,
                                        },
                                    }
                                }
                            ],
                        }
                    ]
                },
                "expected_fresh": 0,
                "expected_cached": 5000,
                "expected_prompt": 5000,
                "expected_out": 100,
                "expected_think": 0,
            },
            "V2_turns_prompt_and_zero_fresh": {
                "payload": {
                    "turns": [
                        {
                            "session_id": "s2",
                            "model_name": "gemini-3.8-flash",
                            "prompt_tokens": 5000,
                            "input_tokens": 0,
                            "cached_tokens": 5000,
                            "output_tokens": 100,
                            "thinking_tokens": 0,
                        }
                    ]
                },
                "expected_fresh": 0,
                "expected_cached": 5000,
                "expected_prompt": 5000,
                "expected_out": 100,
                "expected_think": 0,
            },
            "V3_raw_input_and_cached": {
                "payload": {
                    "turns": [
                        {
                            "session_id": "s3",
                            "model_name": "gemini-3.8-flash",
                            "input_tokens": 1000,
                            "cached_tokens": 400,
                            "output_tokens": 10,
                        }
                    ]
                },
                "expected_fresh": 600,
                "expected_cached": 400,
                "expected_prompt": 1000,
                "expected_out": 10,
                "expected_think": 0,
            },
            "V4_plugin_record": {
                "payload": {
                    "turns": [
                        {
                            "session_id": "s4",
                            "model_name": "gemini-3.8-flash",
                            "input_tokens": 600,
                            "cached_tokens": 400,
                            "output_tokens": 10,
                            "thinking_tokens": 5,
                            "total_tokens": 1015,
                        }
                    ]
                },
                "expected_fresh": 600,
                "expected_cached": 400,
                "expected_prompt": 1000,
                "expected_out": 10,
                "expected_think": 5,
            },
        }

        for name, spec in variants.items():
            with self.subTest(variant=name):
                with tempfile.TemporaryDirectory(prefix="trace_var_") as tdir:
                    tfile = os.path.join(tdir, "trace.json")
                    with open(tfile, "w", encoding="utf-8") as f:
                        json.dump(spec["payload"], f)

                    _, port, _ = self._start_live_server(
                        traces_path=tdir, copy_web=True
                    )
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/metrics?session_id=global",
                        timeout=3,
                    ) as resp:
                        web_data = json.loads(resp.read())

                    parsed_turns = parse_trace_or_eval_payload(spec["payload"])
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        TokenProfiler().profile_session(
                            [dict(t) for t in parsed_turns], "parity"
                        )
                    m = re.search(
                        r"TOTAL\s+\|.*?\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|\s*\$([\d.]+)",
                        buf.getvalue(),
                    )
                    self.assertIsNotNone(m)
                    prof_fresh = int(m.group(1).replace(",", ""))
                    prof_cost = float(m.group(5))

                    expected_cost = pe.calculate_cost(
                        "gemini-3.8-flash",
                        spec["expected_fresh"],
                        spec["expected_cached"],
                        spec["expected_out"],
                        spec["expected_think"],
                    )
                    wt = web_data["turns"][0]
                    self.assertEqual(wt["input_tokens"], spec["expected_fresh"])
                    self.assertEqual(
                        wt["prompt_tokens"], spec["expected_prompt"]
                    )
                    self.assertEqual(
                        wt["cached_tokens"], spec["expected_cached"]
                    )
                    self.assertEqual(prof_fresh, spec["expected_fresh"])
                    self.assertAlmostEqual(
                        web_data["total_cost"], expected_cost, places=6
                    )
                    self.assertAlmostEqual(
                        web_data["total_cost"], prof_cost, places=5
                    )

    def test_p2_dev_ui_html_escapes_trace_label_and_dead_endpoints_removed(
        self,
    ):
        xss_trace = '/tmp/<img src=x onerror="alert(1)">'
        _, port, _ = self._start_live_server(
            traces_path=xss_trace, copy_web=True
        )
        with patch.object(server.TokenomicsWebHandler, "adk_proxy_port", 9):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/dev-ui/", timeout=3
            ) as resp:
                html_body = resp.read().decode("utf-8")
        self.assertNotIn("<img src=x", html_body)
        self.assertIn("&lt;img src=x", html_body)
        self.assertNotIn("tab-btn-finops", html_body)

        dead_req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/set_model",
            data=b'{"model_name":"x"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(dead_req, timeout=3)
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

    def test_r2_p1_a_dns_rebinding_get_blocked_and_loopback_get_allowed(self):
        with tempfile.TemporaryDirectory(prefix="trace_rebind_") as tdir:
            trace_file = os.path.join(tdir, "secret_trace.json")
            with open(trace_file, "w", encoding="utf-8") as tf:
                json.dump(
                    {
                        "turns": [
                            {
                                "session_id": "sess-secret-42",
                                "app_name": "secret_agent",
                                "model_name": "gemini-3.8-flash",
                                "user_query": "SECRET_PROMPT_42",
                                "agent_response": "SECRET_RESPONSE_42",
                                "input_tokens": 200,
                                "cached_tokens": 0,
                                "output_tokens": 40,
                                "thinking_tokens": 0,
                            }
                        ]
                    },
                    tf,
                )

            _, port, _ = self._start_live_server(
                traces_path=trace_file, copy_web=True
            )

            # 1. Hostile DNS-rebinding GET and HEAD requests (with and without port) -> 403, zero secret leak
            for evil_host in (
                f"evil.example:{port}",
                "evil.example",
                "127.0.0.1.evil.example",
                "localhost.",
            ):
                for method, target_path in (
                    ("GET", "/api/metrics?session_id=global"),
                    ("GET", "/api/sessions"),
                    ("GET", "/api/models"),
                    ("GET", "/api/bq/config"),
                    ("GET", "/"),
                    ("GET", "/dev-ui/"),
                    ("HEAD", "/api/metrics?session_id=global"),
                ):
                    raw_resp = self._raw_http(
                        port,
                        f"{method} {target_path} HTTP/1.0\r\nHost: {evil_host}\r\n\r\n".encode(),
                    )
                    status_line = raw_resp.split(b"\r\n", 1)[0]
                    self.assertIn(
                        b"403",
                        status_line,
                        f"Expected 403 for {method} {target_path} Host={evil_host}",
                    )
                    self.assertNotIn(b"SECRET_PROMPT_42", raw_resp)
                    self.assertNotIn(b"SECRET_RESPONSE_42", raw_resp)

            # 2. Cross-site Sec-Fetch-Site subresource GET to /api/* -> 403
            r_cross_site = self._raw_http(
                port,
                (
                    f"GET /api/metrics?session_id=global HTTP/1.0\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    f"Sec-Fetch-Site: cross-site\r\n\r\n"
                ).encode(),
            )
            self.assertIn(b"403", r_cross_site.split(b"\r\n", 1)[0])
            self.assertNotIn(b"SECRET_PROMPT_42", r_cross_site)

            # 3. Legitimate loopback GETs without Origin header -> 200
            r_root = self._raw_http(
                port,
                f"GET / HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode(),
            )
            self.assertIn(b"200", r_root.split(b"\r\n", 1)[0])

            r_metrics = self._raw_http(
                port,
                f"GET /api/metrics?session_id=global HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode(),
            )
            self.assertIn(b"200", r_metrics.split(b"\r\n", 1)[0])
            self.assertIn(b"SECRET_PROMPT_42", r_metrics)

            r_head_ok = self._raw_http(
                port,
                f"HEAD /api/metrics?session_id=global HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode(),
            )
            head_hdrs, head_body = r_head_ok.split(b"\r\n\r\n", 1)
            self.assertIn(b"200", head_hdrs.split(b"\r\n", 1)[0])
            self.assertEqual(head_body, b"")

    def test_r2_p1_b_container_allowed_hosts_https_and_port_mapping(self):
        tmp_web_dir = tempfile.TemporaryDirectory(
            prefix="tokenomics_container_web_"
        )
        self.addCleanup(tmp_web_dir.cleanup)
        tmp_web = os.path.join(tmp_web_dir.name, "web")
        shutil.copytree(self._orig_web_dir, tmp_web)
        shutil.copy(
            PACKAGED_MODELS_CFG, os.path.join(tmp_web, "models_config.json")
        )
        server.WEB_DIR = tmp_web

        server.TokenomicsWebHandler.set_server_context(
            project_id="",
            dataset_id="bq_adk_ds",
            table_id="adk_agent_events",
            view_id="v_llm_response",
            traces_path=None,
            allowed_hosts=None,
            bound_host="0.0.0.0",
        )
        httpd = server.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), server.TokenomicsWebHandler
        )
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]

        model_body = json.dumps(
            {
                "id": "custom/container-rate-test",
                "name": "Container Rate Test",
                "provider": "custom",
                "pricing": {"input": 1.5, "cached": 0.15, "output": 6.0},
            }
        )

        # 1. Without TOKENOMICS_ALLOWED_HOSTS: 0.0.0.0 still fails closed (403) for external Host & mapped port
        r_no_env_cr = self._raw_http(
            port,
            (
                f"POST /api/models HTTP/1.0\r\n"
                f"Host: svc-abc.a.run.app\r\n"
                f"Origin: https://svc-abc.a.run.app\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(model_body)}\r\n\r\n"
                f"{model_body}"
            ).encode(),
        )
        self.assertIn(b"403", r_no_env_cr.split(b"\r\n", 1)[0])

        r_no_env_docker = self._raw_http(
            port,
            (
                f"POST /api/models HTTP/1.0\r\n"
                f"Host: localhost:9090\r\n"
                f"Origin: http://localhost:9090\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(model_body)}\r\n\r\n"
                f"{model_body}"
            ).encode(),
        )
        self.assertIn(b"403", r_no_env_docker.split(b"\r\n", 1)[0])

        # 2. With TOKENOMICS_ALLOWED_HOSTS="svc-abc.a.run.app,localhost:9090"
        with patch.dict(
            os.environ,
            {"TOKENOMICS_ALLOWED_HOSTS": "svc-abc.a.run.app,localhost:9090"},
        ):
            r_cr_ok = self._raw_http(
                port,
                (
                    f"POST /api/models HTTP/1.0\r\n"
                    f"Host: svc-abc.a.run.app\r\n"
                    f"Origin: https://svc-abc.a.run.app\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(model_body)}\r\n\r\n"
                    f"{model_body}"
                ).encode(),
            )
            self.assertIn(b"200", r_cr_ok.split(b"\r\n", 1)[0])
            self.assertIn(
                b"Access-Control-Allow-Origin: https://svc-abc.a.run.app",
                r_cr_ok.split(b"\r\n\r\n", 1)[0],
            )

            r_docker_ok = self._raw_http(
                port,
                (
                    f"POST /api/models HTTP/1.0\r\n"
                    f"Host: localhost:9090\r\n"
                    f"Origin: http://localhost:9090\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(model_body)}\r\n\r\n"
                    f"{model_body}"
                ).encode(),
            )
            self.assertIn(b"200", r_docker_ok.split(b"\r\n", 1)[0])

            # Mismatched Host + evil Origin -> 403
            r_mismatch = self._raw_http(
                port,
                (
                    f"POST /api/models HTTP/1.0\r\n"
                    f"Host: svc-abc.a.run.app\r\n"
                    f"Origin: https://evil.example\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(model_body)}\r\n\r\n"
                    f"{model_body}"
                ).encode(),
            )
            self.assertIn(b"403", r_mismatch.split(b"\r\n", 1)[0])

            # Unlisted port on localhost -> 403
            r_wrong_port = self._raw_http(
                port,
                (
                    f"POST /api/models HTTP/1.0\r\n"
                    f"Host: localhost:9999\r\n"
                    f"Origin: http://localhost:9999\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(model_body)}\r\n\r\n"
                    f"{model_body}"
                ).encode(),
            )
            self.assertIn(b"403", r_wrong_port.split(b"\r\n", 1)[0])

        # 3. Subdomain wildcard (*.run.app / *.a.run.app): allows service URL, blocks cross-tenant Origin & bare .run.app
        server.TokenomicsWebHandler.set_server_context(
            project_id="",
            allowed_hosts=["*.run.app"],
            bound_host="0.0.0.0",
        )
        r_wild_ok = self._raw_http(
            port,
            (
                f"POST /api/models HTTP/1.0\r\n"
                f"Host: svc-abc.a.run.app\r\n"
                f"Origin: https://svc-abc.a.run.app\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(model_body)}\r\n\r\n"
                f"{model_body}"
            ).encode(),
        )
        self.assertIn(b"200", r_wild_ok.split(b"\r\n", 1)[0])

        r_cross_tenant = self._raw_http(
            port,
            (
                f"POST /api/models HTTP/1.0\r\n"
                f"Host: svc-abc.a.run.app\r\n"
                f"Origin: https://attacker-other.a.run.app\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(model_body)}\r\n\r\n"
                f"{model_body}"
            ).encode(),
        )
        self.assertIn(b"403", r_cross_tenant.split(b"\r\n", 1)[0])

        for bad_wild_host in (".run.app", "run.app", "evil-run.app"):
            r_bad_wild = self._raw_http(
                port,
                f"GET /api/models HTTP/1.0\r\nHost: {bad_wild_host}\r\n\r\n".encode(),
            )
            self.assertIn(
                b"403",
                r_bad_wild.split(b"\r\n", 1)[0],
                f"Expected 403 for Host={bad_wild_host}",
            )

    def test_r2_p2_load_models_config_skips_single_invalid_entry(self):
        with tempfile.TemporaryDirectory(
            prefix="models_partial_cfg_"
        ) as tmp_dir:
            custom_cfg_path = os.path.join(tmp_dir, "models_config.json")
            hand_edited = {
                "models": [
                    {
                        "id": "custom/valid-alpha",
                        "name": "Valid Alpha",
                        "provider": "custom",
                        "pricing": {
                            "input": 1.25,
                            "cached": 0.125,
                            "output": 5.0,
                        },
                    },
                    {
                        "id": "custom/corrupt-negative",
                        "name": "Corrupt Negative",
                        "provider": "custom",
                        "pricing": {
                            "input": -10.0,
                            "cached": 0.1,
                            "output": 2.0,
                        },
                    },
                    {
                        "id": "custom/corrupt-out-of-bounds",
                        "name": "Corrupt Huge",
                        "provider": "custom",
                        "pricing": {
                            "input": 50000.0,
                            "cached": 0.1,
                            "output": 2.0,
                        },
                    },
                    {
                        "id": "custom/corrupt-non-numeric",
                        "name": "Corrupt String",
                        "provider": "custom",
                        "pricing": {
                            "input": "not-a-float",
                            "cached": 0.1,
                            "output": 2.0,
                        },
                    },
                    {
                        "id": "custom/valid-omega",
                        "name": "Valid Omega",
                        "provider": "custom",
                        "pricing": {
                            "input": 2.5,
                            "cached": 0.25,
                            "output": 10.0,
                        },
                    },
                ]
            }
            with open(custom_cfg_path, "w", encoding="utf-8") as cf:
                json.dump(hand_edited, cf)

            with patch.object(server, "WEB_DIR", tmp_dir):
                with self.assertLogs(
                    "adk_tokenomics_plugin.cli_web", level="WARNING"
                ) as cm:
                    server.TokenomicsWebHandler.set_server_context(
                        project_id="", traces_path=None
                    )
                    handler = object.__new__(server.TokenomicsWebHandler)
                    loaded = handler.load_models_config()

                self.assertTrue(
                    any("custom/corrupt-negative" in msg for msg in cm.output)
                )
                loaded_ids = [m["id"] for m in loaded["models"]]
                self.assertEqual(
                    loaded_ids, ["custom/valid-alpha", "custom/valid-omega"]
                )
                by_id = {m["id"]: m for m in loaded["models"]}
                self.assertEqual(
                    by_id["custom/valid-alpha"]["pricing"]["input"], 1.25
                )
                self.assertEqual(
                    by_id["custom/valid-omega"]["pricing"]["output"], 10.0
                )


class TestFrontendAppJsBreakIt(unittest.TestCase):
    def test_p1_11_static_dom_ids_and_pinned_chartjs_sri(self):
        app_src = _read_text(WEB_APP_JS)
        html_src = _read_text(WEB_INDEX_HTML)

        app_ids = set(
            re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", app_src)
        )
        html_ids = set(re.findall(r'id="([^"]+)"', html_src))
        runtime_created_allowlist = {"model-save-toast"}
        missing = app_ids - html_ids - runtime_created_allowlist
        self.assertEqual(
            missing, set(), f"app.js references missing HTML IDs: {missing}"
        )
        self.assertIn("costLineChart", html_ids)
        self.assertIn("chart.js@4.4.4/dist/chart.umd.min.js", html_src)
        self.assertIn('integrity="sha384-', html_src)
        self.assertIn('crossorigin="anonymous"', html_src)

    def test_frontend_node_vm_p0_5_p0_6_p1_1_p1_4_p1_5_p1_8_p1_12(self):
        node_script = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const repoRoot = process.env.REPO_ROOT;
const APP = fs.readFileSync(repoRoot + '/adk_tokenomics_plugin/web/app.js', 'utf8');
const HTML = fs.readFileSync(repoRoot + '/adk_tokenomics_plugin/web/index.html', 'utf8');
const MODELS = JSON.parse(fs.readFileSync(repoRoot + '/adk_tokenomics_plugin/models_config.json', 'utf8'));
const HTML_IDS = new Set([...HTML.matchAll(/id="([^"]+)"/g)].map(m => m[1]));

function makeEl(id) {
  const attrs = {};
  const el = {
    id, _html: '', textContent: '', value: '', disabled: false, checked: false, title: '', className: '',
    style: {}, children: [], options: [], _listeners: {},
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); } },
    addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
    appendChild(c) { this.children.push(c); return c; },
    querySelectorAll() { return []; }, querySelector() { return null; }, closest() { return null; },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
    setAttribute(k, v) { attrs[k] = String(v); },
    scrollIntoView() {}, remove() {}, click() {}, getContext() { return {}; }, reset() {},
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    configurable: true,
    set(v) {
      this._html = String(v);
      const opts = [...this._html.matchAll(/<option value="([^"]*)"/g)].map(m => ({ value: m[1] }));
      if (opts.length) {
        this.options = opts;
        if (!opts.some(o => o.value === this.value)) this.value = opts[0].value;
      }
    }
  });
  return el;
}

function createEnv({ withChart = true, fetchImpl, initialStorage = {}, withBlob = false }) {
  const els = {};
  const intervals = [];
  const createdLinks = [];
  const document = {
    _dcl: [],
    addEventListener(ev, fn) { if (ev === 'DOMContentLoaded') this._dcl.push(fn); },
    getElementById(id) {
      if (els[id]) return els[id];
      if (!HTML_IDS.has(id)) return null;
      els[id] = makeEl(id);
      return els[id];
    },
    createElement(tag) {
      const e = makeEl('<' + tag + '>');
      if (tag === 'div') {
        Object.defineProperty(e, 'id', {
          get() { return e._id || '<div>'; },
          set(v) { e._id = v; els[v] = e; }
        });
      }
      if (tag === 'a') createdLinks.push(e);
      return e;
    },
    querySelectorAll(sel) {
      if (sel === '.btn-delete-model' && els['modal-models-table-body']) {
        this._deleteButtons = els['modal-models-table-body'].children.map(tr => {
          const m = /class="btn btn-delete-model" data-id="([^"]*)"/.exec(tr._html || '');
          const b = makeEl('<button>');
          if (m) b.setAttribute('data-id', m[1]);
          return b;
        });
        return this._deleteButtons;
      }
      return [];
    },
    body: { appendChild(c) { if (c && c.id) els[c.id] = c; } },
  };
  const storageMap = Object.assign({}, initialStorage);
  const ctx = {
    document, console: { log() {}, warn() {}, error() {} },
    window: {},
    localStorage: {
      getItem(k) { return Object.prototype.hasOwnProperty.call(storageMap, k) ? storageMap[k] : null; },
      setItem(k, v) { storageMap[k] = String(v); },
      _map: storageMap,
    },
    navigator: { clipboard: { writeText: async () => {} } },
    setInterval: (fn, ms) => { intervals.push(ms); return intervals.length; },
    clearInterval: () => {},
    setTimeout: () => 1,
    clearTimeout: () => {},
    fetch: fetchImpl,
    confirm: () => true,
    alert: () => {},
    AbortController,
    Date, Math, JSON, Number, String, Object, Array, Promise, Set, RegExp, Error,
    isNaN, parseFloat, parseInt, encodeURIComponent, encodeURI,
  };
  if (withChart) {
    ctx.Chart = function (c, cfg) { this.data = cfg.data; this.update = () => {}; this.resize = () => {}; };
    ctx.Chart.defaults = { font: {} };
  }
  if (withBlob) {
    ctx.Blob = class FakeBlob { constructor(parts, opts) { this.parts = parts; this.opts = opts; } };
    ctx.URL = {
      _blobs: {},
      createObjectURL(b) { const u = 'blob:http://127.0.0.1/' + Object.keys(this._blobs).length; this._blobs[u] = b; return u; },
      revokeObjectURL() {},
    };
  }
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(APP, ctx, { filename: 'app.js' });
  return { ctx, els, intervals, createdLinks, storageMap, boot() { document._dcl.forEach(f => f()); } };
}

const flush = () => new Promise(r => setImmediate(r));
async function settle(n = 25) { for (let i = 0; i < n; i++) await flush(); }
function jsonResp(obj, ok = true, status = 200) {
  return Promise.resolve({ ok, status, json: async () => obj, text: async () => JSON.stringify(obj) });
}
const rate = id => MODELS.models.find(x => x.id === id || x.id.endsWith('/' + id)).pricing;
const calcCost = (id, f, c, o, th) => {
  const p = rate(id);
  return (f * p.input + c * p.cached + (o + th) * p.output) / 1e6;
};
function metricsPayload(turns, sid = 'global', source = 'local_traces') {
  const metrics = {}; let tc = 0;
  turns.forEach(t => {
    const k = t.app_name;
    metrics[k] = metrics[k] || { name: k, turns: 0, input: 0, cached: 0, output: 0, thinking: 0, cost: 0 };
    const m = metrics[k];
    m.turns++; m.input += t.prompt_tokens; m.cached += t.cached_tokens;
    m.output += t.output_tokens; m.thinking += t.thinking_tokens;
    m.cost += (t.estimated_cost || 0); tc += (t.estimated_cost || 0);
  });
  return { source, active_session_id: sid, metrics, turns, simulations: {}, tool_counts: {}, total_cost: tc };
}
function baseFetch(metricsFn, extra = {}) {
  return (url, opts) => {
    if (extra.onFetch) extra.onFetch(url, opts);
    if (url.startsWith('/api/models') && (!opts || !opts.method || opts.method === 'GET')) {
      return extra.modelsResp ? extra.modelsResp() : jsonResp(MODELS);
    }
    if (url.startsWith('/api/models') && opts && opts.method) {
      return extra.mutateModelsResp ? extra.mutateModelsResp(url, opts) : jsonResp({ status: 'success', models: MODELS.models });
    }
    if (url.startsWith('/api/sessions')) return jsonResp(extra.sessions || []);
    if (url.startsWith('/api/bq/config')) return jsonResp(extra.bqConfig || { project_id: '', dataset_id: 'bq_adk_ds' });
    if (url.includes('live_metrics.json')) return metricsFn(url, opts);
    if (url.startsWith('/api/bq/logs')) return jsonResp(extra.bqlogs || { status: 'success', rows: [], total_rows: 0 });
    if (url.startsWith('/api/bq/stats')) return jsonResp(extra.bqstats || { status: 'success', total_turns: 2, total_cost: 0.5, total_input: 1000, total_fresh_input: 300, total_cached: 700, unique_sessions: 1 });
    return jsonResp({ status: 'success' });
  };
}

(async () => {
  // 1. P0-5: Reasoning & Output KPI math (100 visible + 900 thinking = 1,000, 90.0%)
  {
    const flash = 'publishers/google/models/gemini-3.8-flash';
    const turns = [{ app_name: 'planner', session_id: 's1', model_name: flash, prompt_tokens: 1000, input_tokens: 1000, cached_tokens: 0, output_tokens: 100, thinking_tokens: 900, estimated_cost: calcCost(flash, 1000, 0, 100, 900) }];
    const env = createEnv({ fetchImpl: baseFetch(() => jsonResp(metricsPayload(turns))) });
    env.boot(); await settle();
    assert.strictEqual(env.els['kpi-output-tokens'].textContent, '1,000');
    assert.strictEqual(env.els['kpi-output-desc'].textContent, 'Visible: 100 | Thoughts: 900 (90.0%)');

    // Also verify P0-3 client normalizer bounds directly
    for (const bad of [
      { id: 'x', pricing: { input: 'NaN', cached: 0, output: 1 } },
      { id: 'x', pricing: { input: Infinity, cached: 0, output: 1 } },
      { id: 'x', pricing: { input: -0.01, cached: 0, output: 1 } },
      { id: 'x', pricing: { input: 10001, cached: 0, output: 1 } },
    ]) {
      assert.throws(() => env.ctx.window.normalizeModelEntryClient(bad));
    }
  }

  // 2. P0-6: Mixed-model per-turn cache savings (Opus 200k cached + Flash 500 fresh => $2.70000)
  {
    const opus = 'claude-opus-4', flash = 'publishers/google/models/gemini-3.8-flash';
    const turns = [
      { app_name: 'architect', session_id: 's', model_name: opus, prompt_tokens: 200000, input_tokens: 0, cached_tokens: 200000, output_tokens: 2000, thinking_tokens: 0, estimated_cost: calcCost(opus, 0, 200000, 2000, 0) },
      { app_name: 'router', session_id: 's', model_name: flash, prompt_tokens: 500, input_tokens: 500, cached_tokens: 0, output_tokens: 20, thinking_tokens: 0, estimated_cost: calcCost(flash, 500, 0, 20, 0) },
    ];
    const env = createEnv({ fetchImpl: baseFetch(() => jsonResp(metricsPayload(turns))) });
    env.boot(); await settle();
    assert(env.els['kpi-cache-desc'].textContent.startsWith('Saved $2.70000'), 'Got: ' + env.els['kpi-cache-desc'].textContent);
  }

  // 3. P1-1 + P2: Out-of-order poll responses discarded + session_id URL-encoded
  {
    const pending = {};
    const seenUrls = [];
    const mk = sid => metricsPayload([{ app_name: 'ag_' + sid, session_id: sid, model_name: 'publishers/google/models/gemini-3.8-flash', prompt_tokens: sid === 'A' ? 1000000 : 2000000, input_tokens: sid === 'A' ? 1000000 : 2000000, cached_tokens: 0, output_tokens: 0, thinking_tokens: 0, estimated_cost: 0 }], sid);
    const env = createEnv({
      fetchImpl: baseFetch((url) => {
        seenUrls.push(url);
        const sid = decodeURIComponent(url.split('session_id=')[1]);
        if (sid === 'A' || sid === 'B') {
          return new Promise(res => { pending[sid] = () => res({ ok: true, json: async () => mk(sid) }); });
        }
        return jsonResp(mk(sid));
      }, { sessions: [{ session_id: 'A', turn_count: 1 }, { session_id: 'B', turn_count: 1 }, { session_id: 's+1#2&3', turn_count: 1 }] })
    });
    env.boot(); await settle();
    const sel = env.els['adk-session-select'];
    sel.value = 's+1#2&3'; sel._listeners.change.forEach(f => f()); await settle(4);
    assert(seenUrls.some(u => u.endsWith('session_id=s%2B1%232%263')));

    sel.value = 'A'; sel._listeners.change.forEach(f => f()); await settle(2);
    sel.value = 'B'; sel._listeners.change.forEach(f => f()); await settle(2);
    pending['B'](); await settle();
    pending['A'](); await settle();
    assert.strictEqual(env.els['kpi-total-cost'].textContent, '$1.50000');
  }

  // 4. P1-4: Offline Chart.js missing -> boots cleanly, polls, and renders KPIs
  {
    let fetchedMetrics = false;
    const turns = [{ app_name: 'offline_ag', session_id: 'so', model_name: 'publishers/google/models/gemini-3.8-flash', prompt_tokens: 1000, input_tokens: 1000, cached_tokens: 0, output_tokens: 50, thinking_tokens: 50, estimated_cost: 0.001 }];
    const env = createEnv({
      withChart: false,
      fetchImpl: baseFetch(() => { fetchedMetrics = true; return jsonResp(metricsPayload(turns)); })
    });
    env.boot(); await settle();
    assert.strictEqual(fetchedMetrics, true);
    assert.strictEqual(env.els['kpi-output-tokens'].textContent, '100');
  }

  // 5. P1-5 & P1-11: CSV export '#' preservation + formula sanitization + Fresh token display
  {
    const rows = [{ timestamp: '2026-10-01T00:00:00Z', session_id: 's1', app_name: 'skills_app', model_name: 'gemini-3.8-flash', prompt_tokens: 1000, input_tokens: 250, cached_tokens: 750, output_tokens: 50, thinking_tokens: 10, user_query: 'Use issue #42 template', agent_response: '=CMD|\' /C calc\'!A0', estimated_cost: 0.01 }];
    // 5a: data: URI fallback
    const envData = createEnv({
      withBlob: false,
      fetchImpl: baseFetch(() => jsonResp(metricsPayload([])), { bqlogs: { status: 'success', rows, total_rows: 1 } })
    });
    envData.boot(); await settle();
    envData.els['tab-btn-bq']._listeners.click.forEach(f => f()); await settle();
    assert.strictEqual(envData.els['bq-stat-total-tokens'].textContent, '300 / 700');
    assert(envData.els['bq-logs-table-body'].innerHTML.includes('>250</span>'));
    envData.els['btn-bq-export-csv']._listeners.click.forEach(f => f());
    const dataHref = envData.createdLinks[0].getAttribute('href');
    assert.strictEqual(new URL(dataHref).hash, '');
    const decodedCsv = decodeURIComponent(dataHref.slice('data:text/csv;charset=utf-8,'.length));
    assert(decodedCsv.includes('"Use issue #42 template"'));
    assert(decodedCsv.includes('"\'=CMD|'));

    // 5b: Blob + URL.createObjectURL path
    const envBlob = createEnv({
      withBlob: true,
      fetchImpl: baseFetch(() => jsonResp(metricsPayload([])), { bqlogs: { status: 'success', rows, total_rows: 1 } })
    });
    envBlob.boot(); await settle();
    envBlob.els['tab-btn-bq']._listeners.click.forEach(f => f()); await settle();
    envBlob.els['btn-bq-export-csv']._listeners.click.forEach(f => f());
    const blobHref = envBlob.createdLinks[0].getAttribute('href');
    assert(blobHref.startsWith('blob:'));
    const blobCsv = envBlob.ctx.URL._blobs[blobHref].parts.join('');
    assert(blobCsv.includes('"Use issue #42 template"'));
    assert(blobCsv.includes('"\'=CMD|'));
  }

  // 6. P1-8: BigQuery mode backs off polling interval to >= 15,000ms
  {
    const env = createEnv({
      fetchImpl: baseFetch(() => jsonResp(metricsPayload([], 'global', 'bigquery')), { bqConfig: { project_id: 'prod-proj', dataset_id: 'bq_adk_ds' } })
    });
    env.boot(); await settle();
    assert(env.intervals.some(ms => ms >= 15000), 'Expected >=15s BQ backoff interval, got ' + JSON.stringify(env.intervals));
  }

  // 7. P1-12 & P0-1: Server authoritative over stale localStorage, rollback on 400, DELETE sends JSON Content-Type
  {
    const staleLocal = JSON.stringify({
      models: [{ id: 'publishers/google/models/gemini-3.8-flash', name: 'Gemini 3.8 Flash', provider: 'google', pricing: { input: 999, cached: 999, output: 999 } }]
    });
    const mutateCalls = [];
    const turns = [{ app_name: 'a', session_id: 's', model_name: 'publishers/google/models/gemini-3.8-flash', prompt_tokens: 1000000, input_tokens: 1000000, cached_tokens: 0, output_tokens: 0, thinking_tokens: 0, estimated_cost: 0.75 }];
    const env = createEnv({
      initialStorage: { 'adk_tokenomics_models_config_v1': staleLocal },
      fetchImpl: baseFetch(() => jsonResp(metricsPayload(turns)), {
        mutateModelsResp: (url, opts) => {
          mutateCalls.push({ url, opts });
          if (opts.method === 'POST') return jsonResp({ status: 'error', message: 'Rejected by server' }, false, 400);
          return jsonResp({ status: 'success', models: MODELS.models.slice(1) });
        }
      })
    });
    env.boot(); await settle();
    // Authoritative server rate ($0.75/1M) wins over stale 999 in localStorage!
    assert.strictEqual(env.els['kpi-total-cost'].textContent, '$0.75000');

    // Attempt raw JSON save that fails on server with 400 -> must NOT toast "✅ Saved", must roll back
    env.els['raw-json-editor-textarea'].value = JSON.stringify({
      models: [{ id: 'publishers/google/models/gemini-3.8-flash', name: 'Gemini 3.8 Flash', provider: 'google', pricing: { input: 500, cached: 50, output: 500 } }]
    });
    env.els['btn-save-raw-json']._listeners.click.forEach(f => f()); await settle();
    const toastTxt = env.els['model-save-toast'] ? env.els['model-save-toast'].textContent : '';
    assert(!toastTxt.includes('✅ Saved'), 'Should not show false success toast on 400: ' + toastTxt);
    const savedStorage = JSON.parse(env.storageMap['adk_tokenomics_models_config_v1']);
    const flashAfter400 = savedStorage.models.find(m => m.id === 'publishers/google/models/gemini-3.8-flash');
    assert.strictEqual(flashAfter400.pricing.input, 0.75);

    // Trigger model modal render & verify DELETE actually invokes fetch(DELETE) with Content-Type: application/json
    env.els['btn-manage-models']._listeners.click.forEach(f => f({ preventDefault() {} })); await settle();
    const delBtns = env.ctx.document._deleteButtons || [];
    assert(delBtns.length > 0, 'Expected rendered .btn-delete-model buttons');
    delBtns[0]._listeners.click.forEach(f => f({ currentTarget: delBtns[0] })); await settle();
    const delMatch = mutateCalls.filter(c => c.opts && c.opts.method === 'DELETE');
    assert.strictEqual(delMatch.length, 1, 'Expected 1 DELETE /api/models call');
    assert.strictEqual(delMatch[0].opts.headers['Content-Type'], 'application/json');
  }

  // 8. Break-It P1-1: 500-turn row cap must NOT overwrite full-window 5,000-turn BigQuery Spend/Savings KPIs
  {
    const flash = 'publishers/google/models/gemini-3.8-flash';
    const sampleTurns = [];
    for (let i = 0; i < 500; i++) {
      sampleTurns.push({
        app_name: 'planner',
        session_id: 'global',
        model_name: flash,
        prompt_tokens: 1000,
        input_tokens: 1000,
        cached_tokens: 0,
        output_tokens: 100,
        thinking_tokens: 0,
        estimated_cost: 0.001125,
        savings_dollars: 0.00025,
      });
    }
    const truncatedPayload = {
      source: 'bigquery',
      active_session_id: 'global',
      turns_truncated: true,
      total_turns: 5000,
      total_cost: 5.625,
      savings_dollars: 1.25,
      metrics: {
        planner: {
          name: 'planner',
          turns: 5000,
          input: 5000000,
          cached: 1851852,
          output: 500000,
          thinking: 0,
          cost: 5.625,
          savings_dollars: 1.25,
        }
      },
      turns: sampleTurns,
      simulations: {},
      tool_counts: {},
    };
    const env = createEnv({ fetchImpl: baseFetch(() => jsonResp(truncatedPayload)) });
    env.boot(); await settle();
    assert.strictEqual(env.els['kpi-total-cost'].textContent, '$5.62500');
    assert.strictEqual(env.els['kpi-cost-desc'].textContent, 'Avg $0.00113 / turn (5000 turns)');
    assert(env.els['kpi-cache-desc'].textContent.startsWith('Saved $1.25000'), 'Got: ' + env.els['kpi-cache-desc'].textContent);
    assert.strictEqual(env.els['toolbar-turns-badge'].textContent, '5000 turns');
  }

  // 9. P2-1: Client/server model rate parity for boundary tokens and tier suffixes
  for (const [model, serverRatePerM] of [
    ['deepseek-prover-v2', 0.27],
    ['gpt-5-pro', 2.5],
    ['o3-mini', 0.15],
    ['gemini-3.8-flash-001', 0.75],
  ]) {
    const cost = serverRatePerM;
    const turns = [{ app_name: 'a', session_id: 's', model_name: model, prompt_tokens: 1e6, input_tokens: 1e6, cached_tokens: 0, output_tokens: 0, thinking_tokens: 0, estimated_cost: cost, savings_dollars: 0 }];
    const payload = { source: 'local_traces', active_session_id: 'global', metrics: { a: { name: 'a', turns: 1, input: 1e6, cached: 0, output: 0, thinking: 0, cost, savings_dollars: 0 } }, turns, simulations: {}, tool_counts: {} };
    const env = createEnv({ fetchImpl: baseFetch(() => jsonResp(payload)) });
    env.boot(); await settle();
    assert.strictEqual(env.els['kpi-total-cost'].textContent, '$' + cost.toFixed(5), `Mismatch for ${model}`);
  }

  console.log('ALL_FRONTEND_INVARIANTS_OK');
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""
        proc = subprocess.run(
            ["/usr/bin/node", "-e", node_script],
            env={**os.environ, "REPO_ROOT": REPO_ROOT},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node harness failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("ALL_FRONTEND_INVARIANTS_OK", proc.stdout)

    def test_v102_frontend_observatory_parity_cost_share_badges_latency_rate_card_and_csv(
        self,
    ):
        styles_src = _read_text(
            os.path.join(
                REPO_ROOT, "adk_tokenomics_plugin", "web", "styles.css"
            )
        )
        app_src = _read_text(WEB_APP_JS)
        html_src = _read_text(WEB_INDEX_HTML)

        for badge_cls in (".badge-purple", ".badge-danger", ".badge-warning"):
            self.assertIn(badge_cls, styles_src)
        self.assertNotIn("init-bq", app_src)
        self.assertNotIn("1. Naive", app_src)
        self.assertNotIn("4. Modular Skills", app_src)
        self.assertIn("provision_cloud_infra()", app_src)
        self.assertIn("BigQueryAgentAnalyticsPlugin", app_src)
        self.assertIn("Cost Share (%)", html_src)
        self.assertIn('id="modal-bq-latency"', html_src)
        self.assertIn('id="modal-models-config-path"', html_src)
        self.assertIn('<option value="custom">', html_src)
        self.assertIn('<option value="32768">', html_src)
        self.assertIn('<option value="65536">', html_src)
        self.assertIn('<option value="medium">', html_src)

        node_script = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const repoRoot = process.env.REPO_ROOT;
const APP = fs.readFileSync(repoRoot + '/adk_tokenomics_plugin/web/app.js', 'utf8');
const HTML = fs.readFileSync(repoRoot + '/adk_tokenomics_plugin/web/index.html', 'utf8');
const MODELS = JSON.parse(fs.readFileSync(repoRoot + '/adk_tokenomics_plugin/models_config.json', 'utf8'));
const HTML_IDS = new Set([...HTML.matchAll(/id="([^"]+)"/g)].map(m => m[1]));

function stripTags(s) {
  return String(s || '').replace(/<[^>]*>/g, '').trim();
}

function makeEl(id, tagName = 'DIV') {
  const attrs = {};
  const el = {
    id, tagName, _html: '', textContent: '', value: '', disabled: false, checked: false, title: '', className: '',
    style: {}, children: [], options: [], _listeners: {}, parentElement: null,
    classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); }, contains(c) { return this._s.has(c); } },
    addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
    appendChild(c) {
      c.parentElement = this;
      const idx = this.children.indexOf(c);
      if (idx >= 0) this.children.splice(idx, 1);
      this.children.push(c);
      return c;
    },
    querySelectorAll(sel) {
      if (sel === 'tr') return this.children.filter(c => c.tagName === 'TR');
      if (sel === 'thead th' && this._headers) return this._headers;
      return [];
    },
    querySelector(sel) {
      if (sel === 'td[colspan]') return this.children.find(c => c.tagName === 'TD' && c.getAttribute('colspan') !== null) || null;
      if (sel === '.sort-indicator') return this.children.find(c => c.className === 'sort-indicator') || null;
      return null;
    },
    closest(sel) { return sel === 'table' ? (this._tableOwner || null) : null; },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
    setAttribute(k, v) { attrs[k] = String(v); },
    scrollIntoView() {}, remove() {}, click() {}, getContext() { return {}; }, reset() {},
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    configurable: true,
    set(v) {
      this._html = String(v);
      this.children = [];
      if (this.tagName === 'SELECT') {
        const opts = [...this._html.matchAll(/<option([^>]*)>([\s\S]*?)<\/option>/gi)].map(m => {
          const vmMatch = /value="([^"]*)"/.exec(m[1]);
          return { value: vmMatch ? vmMatch[1] : stripTags(m[2]), textContent: stripTags(m[2]) };
        });
        if (opts.length) {
          this.options = opts;
          if (!opts.some(o => o.value === this.value)) this.value = opts[0].value;
        }
      } else if (this.tagName === 'TR') {
        for (const tm of this._html.matchAll(/<td([^>]*)>([\s\S]*?)<\/td>/gi)) {
          const td = makeEl('', 'TD');
          const dsv = /data-sort-val="([^"]*)"/.exec(tm[1]);
          if (dsv) td.setAttribute('data-sort-val', dsv[1]);
          const cs = /colspan="([^"]*)"/.exec(tm[1]);
          if (cs) td.setAttribute('colspan', cs[1]);
          td._html = tm[2];
          td.textContent = stripTags(tm[2]);
          this.children.push(td);
        }
      } else if (this.tagName === 'TBODY') {
        for (const rm of this._html.matchAll(/<tr([^>]*)>([\s\S]*?)<\/tr>/gi)) {
          const tr = makeEl('', 'TR');
          tr.innerHTML = rm[2];
          this.children.push(tr);
        }
      }
    }
  });
  return el;
}

const flush = () => new Promise(r => setImmediate(r));
async function settle(n = 25) { for (let i = 0; i < n; i++) await flush(); }
function jsonResp(obj, ok = true, status = 200) {
  return Promise.resolve({ ok, status, json: async () => obj, text: async () => JSON.stringify(obj) });
}

(async () => {
  const els = {};
  for (const m of HTML.matchAll(/<([a-zA-Z0-9]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const tag = m[1].toUpperCase();
    const id = m[2];
    const el = makeEl(id, tag);
    if (tag === 'SELECT') {
      const selIdx = m.index;
      const closeIdx = HTML.indexOf('</select>', selIdx);
      if (closeIdx > selIdx) {
        el.innerHTML = HTML.slice(selIdx, closeIdx + 9);
      }
    }
    els[id] = el;
  }

  ['adk-live-table-body', 'session-turns-history-table-body', 'session-tools-table-body'].forEach(tbId => {
    const tb = els[tbId];
    const tbl = makeEl('', 'TABLE');
    const colCount = tbId === 'adk-live-table-body' ? 8 : (tbId === 'session-turns-history-table-body' ? 10 : 4);
    tbl._headers = Array.from({ length: colCount }, () => makeEl('', 'TH'));
    tb._tableOwner = tbl;
  });

  const fetchCalls = [];
  let savedPayloads = [];
  let bqStatsCalls = 0;
  let bqLogsCalls = 0;

  const turns = [
    {
      app_name: 'planner_root',
      session_id: 's_mix',
      orchestration_flow: 'Root',
      model_name: 'publishers/google/models/gemini-4-argon',
      user_query: 'Plan trip to Paris',
      agent_response: 'Calling specialist',
      prompt_tokens: 2000,
      input_tokens: 2000,
      cached_tokens: 0,
      output_tokens: 200,
      thinking_tokens: 100,
      latency_ms: 425,
      estimated_cost: 0.007,
      tools_called: ["search(city='Paris', days=3)"],
      skills_active: ['eu-travel'],
      invoked_tools: "search(city='Paris', days=3)",
      invoked_skills: 'eu-travel',
      timestamp: '2026-11-04T12:00:00Z'
    },
    {
      app_name: 'tool_worker',
      session_id: 's_mix',
      orchestration_flow: 'AgentTool • Parallel',
      model_name: 'publishers/google/models/gemini-3.8-flash',
      user_query: 'Plan trip to Paris',
      agent_response: 'Found flights',
      prompt_tokens: 1000,
      input_tokens: 1000,
      cached_tokens: 0,
      output_tokens: 100,
      thinking_tokens: 0,
      latency_ms: 180,
      estimated_cost: 0.001125,
      tools_called: [],
      skills_active: [],
      invoked_tools: '',
      invoked_skills: '',
      timestamp: '2026-11-04T12:00:01Z'
    }
  ];

  const metricsResp = {
    source: 'local_traces',
    active_session_id: 's_mix',
    metrics: {
      planner_root: { name: 'planner_root', orchestration_flow: 'Root', turns: 1, input: 2000, cached: 0, output: 200, thinking: 100, cost: 0.007, savings_dollars: 0 },
      tool_worker: { name: 'tool_worker', orchestration_flow: 'AgentTool • Parallel', turns: 1, input: 1000, cached: 0, output: 100, thinking: 0, cost: 0.001125, savings_dollars: 0 }
    },
    turns,
    simulations: {},
    tool_counts: {
      "search(city='Paris', days=3)": { name: "search(city='Paris', days=3)", kind: 'tool', label: 'Sub-Agent: planner_root', icon: '🛠️', count: 1, targets: { planner_root: 1 } },
      'eu-travel': { name: 'eu-travel', kind: 'skill', label: 'Activated Skill (planner_root)', icon: '⚡', count: 1, targets: { planner_root: 1 } }
    }
  };

  const document = {
    _dcl: [],
    addEventListener(ev, fn) { if (ev === 'DOMContentLoaded') this._dcl.push(fn); },
    getElementById(id) { return els[id] || null; },
    createElement(tag) {
      const e = makeEl('<' + tag + '>', String(tag).toUpperCase());
      return e;
    },
    querySelectorAll(sel) {
      if (sel === '.btn-delete-model' && els['modal-models-table-body']) {
        return els['modal-models-table-body'].children.map(tr => {
          const m = /class="btn btn-delete-model" data-id="([^"]*)"/.exec(tr._html || '');
          const b = makeEl('<button>', 'BUTTON');
          if (m) b.setAttribute('data-id', m[1]);
          return b;
        });
      }
      return [];
    },
    body: { appendChild(c) { if (c && c.id) els[c.id] = c; } }
  };

  const ctx = {
    document,
    window: {},
    console: { log() {}, warn() {}, error() {} },
    localStorage: { _m: {}, getItem(k) { return this._m[k] || null; }, setItem(k, v) { this._m[k] = String(v); } },
    navigator: { clipboard: { writeText: async () => {} } },
    setInterval: () => 1, clearInterval: () => {}, setTimeout: () => 1, clearTimeout: () => {},
    confirm: () => true, alert: () => {}, AbortController,
    fetch: (url, opts) => {
      fetchCalls.push({ url, opts });
      if (url.startsWith('/api/models') && (!opts || !opts.method || opts.method === 'GET')) {
        return jsonResp({ models: MODELS.models, config_path: '/tmp/xdg/adk-tokenomics/models_config.json', models_config_path: '/tmp/xdg/adk-tokenomics/models_config.json' });
      }
      if (url.startsWith('/api/models') && opts && opts.method === 'POST') {
        const body = JSON.parse(opts.body);
        savedPayloads.push(body);
        return jsonResp({ status: 'success', models: MODELS.models, config_path: '/tmp/xdg/adk-tokenomics/models_config.json' });
      }
      if (url.startsWith('/api/models') && opts && opts.method === 'DELETE') {
        return jsonResp({ status: 'success', models: MODELS.models.slice(1), deleted_ids: [MODELS.models[0].id], config_path: '/tmp/xdg/adk-tokenomics/models_config.json' });
      }
      if (url.startsWith('/api/sessions')) return jsonResp([{ session_id: 's_mix', turn_count: 2 }]);
      if (url.startsWith('/api/bq/config')) return jsonResp({ project_id: 'prod-bq-proj', dataset_id: 'bq_adk_ds', view_id: 'v_llm_response' });
      if (url.includes('live_metrics.json')) return jsonResp(metricsResp);
      if (url.startsWith('/api/bq/stats')) {
        bqStatsCalls += 1;
        return jsonResp({ status: 'success', total_turns: 9, total_cost: 0.42, total_input: 5000, total_fresh_input: 3000, total_cached: 2000, unique_sessions: 3 });
      }
      if (url.startsWith('/api/bq/logs')) {
        bqLogsCalls += 1;
        return jsonResp({
          status: 'success',
          total_rows: 1,
          target: 'prod-bq-proj.bq_adk_ds.v_llm_response',
          rows: [{ timestamp: '2026-11-04T12:00:00Z', event_type: 'LLM_RESPONSE', app_name: 'skills_router_agent', orchestration_flow: 'AgentTool', model_name: 'gemini-3.8-flash', prompt_tokens: 1200, input_tokens: 900, cached_tokens: 300, output_tokens: 100, thinking_tokens: 20, latency_ms: 315, estimated_cost: 0.0011, user_query: 'Query', agent_response: '  =CMD()' }]
        });
      }
      return jsonResp({ status: 'success' });
    },
    Date, Math, JSON, Number, String, Object, Array, Promise, Set, Map, RegExp, Error,
    isNaN, parseFloat, parseInt, encodeURIComponent, decodeURIComponent
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(APP, ctx, { filename: 'app.js' });
  document._dcl.forEach(fn => fn());
  await settle();

  // 1. Orchestration badges (AgentTool + all 4 compounds)
  assert(ctx.window.formatOrchestrationBadge('AgentTool').includes('🧰 AgentTool'));
  assert(ctx.window.formatOrchestrationBadge('AgentTool • Parallel').includes('🧰 AgentTool • ⚡ Parallel'));
  assert(ctx.window.formatOrchestrationBadge('AgentTool • Loop').includes('🧰 AgentTool • 🔁 Loop'));
  assert(ctx.window.formatOrchestrationBadge('AgentTool • Sequential').includes('🧰 AgentTool • ➡️ Sequential'));
  assert(ctx.window.formatOrchestrationBadge('AgentTool • Parallel • Loop').includes('🧰 AgentTool • ⚡ Parallel • 🔁 Loop'));

  // 2. Sortable Cost Share (%) column + Multi-Model Pills in Agent Breakdown (#adk-live-table-body, column index 7) & No redundant toolbar-model-badge
  assert.strictEqual(els['toolbar-model-badge'], undefined, 'Redundant toolbar-model-badge must be removed from top toolbar');
  const liveRows = els['adk-live-table-body'].children;
  assert.strictEqual(liveRows.length, 2);
  assert.strictEqual(liveRows[0].children.length, 8);
  assert(liveRows[0].children[0]._html.includes('gemini-4-argon'), 'Agent Breakdown row 0 must render planner_root model pill');
  assert(liveRows[1].children[0]._html.includes('gemini-3.8-flash'), 'Agent Breakdown row 1 must render tool_worker model pill');

  // Sub-agent filter scopes toolbar Turns/Spend badges dynamically, then restores session summary when cleared
  els['adk-app-select'].value = 'planner_root';
  els['adk-app-select']._listeners.change.forEach(f => f());
  await settle();
  assert.strictEqual(els['toolbar-turns-badge'].textContent, '1 turn');
  els['adk-app-select'].value = 'all';
  els['adk-app-select']._listeners.change.forEach(f => f());
  await settle();
  assert.strictEqual(els['toolbar-turns-badge'].textContent, '2 turns');

  const topShare = parseFloat(els['adk-live-table-body'].children[0].children[7].getAttribute('data-sort-val'));
  const botShare = parseFloat(els['adk-live-table-body'].children[1].children[7].getAttribute('data-sort-val'));
  assert(topShare > botShare && Math.abs(topShare + botShare - 100.0) < 0.01);
  // Click Cost Share header twice -> desc then asc
  const shareTh = els['adk-live-table-body']._tableOwner._headers[7];
  shareTh._listeners.click.forEach(f => f()); // desc
  shareTh._listeners.click.forEach(f => f()); // asc
  assert(parseFloat(els['adk-live-table-body'].children[0].children[7].getAttribute('data-sort-val')) < parseFloat(els['adk-live-table-body'].children[1].children[7].getAttribute('data-sort-val')));

  // 3. Per-call Latency & Model Pill in Tab 1 Waterfall & Modal + intact multi-arg tool signature + skills in Tool & Skill table
  const wfRows = els['session-turns-history-table-body'].children;
  assert.strictEqual(wfRows.length, 2);
  assert.strictEqual(wfRows[0].children.length, 10);
  assert(wfRows[0].children[2]._html.includes('gemini-4-argon'), 'Waterfall turn #1 must render gemini-4-argon pill');
  assert(wfRows[1].children[2]._html.includes('gemini-3.8-flash'), 'Waterfall turn #2 must render gemini-3.8-flash pill');
  assert.strictEqual(wfRows[0].children[6].textContent, '425 ms');
  assert(wfRows[0]._html.includes("search(city=&#039;Paris&#039;, days=3)"), 'Multi-arg tool must stay intact');
  assert(!wfRows[0]._html.includes('>🌐 days=3)'), 'Must NOT split multi-arg tool at comma');

  ctx.window.inspectSessionTurn(0);
  assert.strictEqual(els['modal-bq-latency'].textContent, '425 ms');

  const toolSkillHtml = els['session-tools-table-body']._html;
  assert(toolSkillHtml.includes("search(city=&#039;Paris&#039;, days=3)"));
  assert(toolSkillHtml.includes('eu-travel'));
  assert(toolSkillHtml.includes('⚡'));

  // 4. Sticky hasAutoSwitchedForLocalTrace fixed: switching to Tab 2 renders real BigQuery stats/logs & real agent name (NOT '4. Modular Skills' or 'Local Dev Mode')
  els['tab-btn-bq']._listeners.click.forEach(f => f());
  await settle();
  assert.strictEqual(els['bq-stat-total-turns'].textContent, '9 turns');
  assert(els['bq-logs-table-body']._html.includes('skills_router_agent'));
  assert(!els['bq-logs-table-body']._html.includes('4. Modular Skills'));
  ctx.window.inspectBqRow(0);
  assert.strictEqual(els['modal-bq-latency'].textContent, '315 ms');

  // 5. Rate Card modal: form is HIDDEN by default on open, opens when selecting a model row (or + Add Model), hides on Cancel, marks ALL active session models Active 🟢, and refreshes Tab 2 on save!
  els['btn-manage-models']._listeners.click.forEach(f => f({ preventDefault() {} }));
  await settle();
  assert.strictEqual(els['modal-form-section'].style.display, 'none', 'Edit form must be hidden when Rate Card modal first opens');
  const activeModalRows = els['modal-models-table-body'].children.filter(tr => (tr._html || '').includes('Active 🟢'));
  assert.strictEqual(activeModalRows.length, 2, 'Both gemini-4-argon and gemini-3.8-flash must be marked Active 🟢');
  assert.strictEqual(els['modal-models-config-path'].textContent, '/tmp/xdg/adk-tokenomics/models_config.json');

  // Clicking a model row opens the Edit form populated with that model; Cancel hides it again; + Add Model re-opens blank
  const argonRow = els['modal-models-table-body'].children.find(tr => (tr._html || '').includes('gemini-4-argon'));
  argonRow._listeners.click.forEach(f => f({ target: argonRow }));
  assert.strictEqual(els['modal-form-section'].style.display, 'block', 'Selecting a model row must reveal the Edit form');
  assert.strictEqual(els['form-model-id'].value, 'publishers/google/models/gemini-4-argon');
  els['btn-cancel-edit']._listeners.click.forEach(f => f({ preventDefault() {} }));
  assert.strictEqual(els['modal-form-section'].style.display, 'none', 'Cancel must hide the Edit form');
  els['btn-add-new-model']._listeners.click.forEach(f => f({ preventDefault() {} }));
  assert.strictEqual(els['modal-form-section'].style.display, 'block', '+ Add Model must reveal the blank form');

  // Select gemini-4-argon again, update rates, and submit -> saves & closes/hides form
  argonRow._listeners.click.forEach(f => f({ target: argonRow }));
  const statsBeforeSave = bqStatsCalls;
  const logsBeforeSave = bqLogsCalls;
  els['form-price-input'].value = '2.5';
  els['form-price-cached'].value = '0.25';
  els['form-price-output'].value = '12.0';
  els['model-editor-form']._listeners.submit.forEach(f => f({ preventDefault() {} }));
  await settle();
  assert.strictEqual(els['modal-form-section'].style.display, 'none', 'Form must reset to hidden after saving');

  assert.strictEqual(savedPayloads.length, 1);
  assert.strictEqual(savedPayloads[0].thinking_budget, 'medium');
  assert.strictEqual(savedPayloads[0].max_output_tokens, 65536);
  assert.deepStrictEqual(savedPayloads[0].allowed_thinking_budgets, ['low', 'medium', 'high']);
  assert.deepStrictEqual(savedPayloads[0].allowed_max_output_tokens, [4096, 8192, 16384, 32768, 65536]);
  assert(bqStatsCalls > statsBeforeSave && bqLogsCalls > logsBeforeSave, 'Tab 2 must refresh after Rate Card save');

  // 6. CSV sanitization quotes leading whitespace formula "  =CMD()"
  assert.strictEqual(ctx.window.sanitizeCsvCell('  =CMD()'), '"\'  =CMD()"');

  // 7. P2-d: Non-numeric cost in metrics payload never renders "NaN%" or "$NaN" in renderLiveMetrics
  metricsResp.turns = [];
  metricsResp.metrics = {
    valid_agent: { name: 'valid_agent', orchestration_flow: 'Root', turns: 1, input: 1000, cached: 0, output: 100, thinking: 0, cost: 0.5 },
    bad_cost_agent: { name: 'bad_cost_agent', orchestration_flow: 'AgentTool', turns: 1, input: 500, cached: 0, output: 50, thinking: 0, cost: 'non-numeric-cost', cost_share_pct: 'bad-pct' }
  };
  els['adk-session-select']._listeners.change.forEach(f => f());
  await settle();
  const nonNumHtml = els['adk-live-table-body']._html || els['adk-live-table-body'].children.map(r => r._html).join('');
  assert(!nonNumHtml.includes('NaN'), 'Non-numeric cost must never render NaN: ' + nonNumHtml);
  assert(nonNumHtml.includes('100.0%') && nonNumHtml.includes('0.0%'), 'Valid agent gets 100.0% and bad agent gets 0.0%: ' + nonNumHtml);

  console.log('V102_FRONTEND_PARITY_OK');
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""
        proc = subprocess.run(
            ["/usr/bin/node", "-e", node_script],
            env={**os.environ, "REPO_ROOT": REPO_ROOT},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node v1.0.2 parity failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("V102_FRONTEND_PARITY_OK", proc.stdout)

    def test_v102_c2_rate_card_crud_delete_readd_custom_id_mtime_reload_and_replace_defaults(
        self,
    ):
        from adk_tokenomics_plugin.core.config import TokenomicsConfig

        tmp_cfg_dir = tempfile.TemporaryDirectory(prefix="tokenomics_c2_crud_")
        self.addCleanup(tmp_cfg_dir.cleanup)
        user_cfg_path = os.path.join(tmp_cfg_dir.name, "models_config.json")

        with patch.dict(
            os.environ,
            {"TOKENOMICS_MODELS_CONFIG_PATH": user_cfg_path},
            clear=False,
        ):
            with tempfile.TemporaryDirectory(prefix="trace_mtime_") as tdir:
                tfile = os.path.join(tdir, "session.json")
                with open(tfile, "w", encoding="utf-8") as tf:
                    json.dump(
                        {
                            "turns": [
                                {
                                    "session_id": "s_rate",
                                    "app_name": "architect",
                                    "model_name": "claude-opus-4",
                                    "input_tokens": 1_000_000,
                                    "cached_tokens": 0,
                                    "output_tokens": 0,
                                    "thinking_tokens": 0,
                                }
                            ]
                        },
                        tf,
                    )

                server.TokenomicsWebHandler.set_server_context(
                    project_id="", traces_path=tdir
                )
                httpd = server.http.server.ThreadingHTTPServer(
                    ("127.0.0.1", 0), server.TokenomicsWebHandler
                )
                srv_t = threading.Thread(
                    target=httpd.serve_forever, daemon=True
                )
                srv_t.start()
                self.addCleanup(httpd.server_close)
                self.addCleanup(httpd.shutdown)
                port = httpd.server_address[1]
                base = f"http://127.0.0.1:{port}"
                json_hdrs = {"Content-Type": "application/json", "Origin": base}

                # 1. Default count == 20 + config_path + writable_config_path returned (P2-b)
                with urllib.request.urlopen(
                    f"{base}/api/models", timeout=3
                ) as r0:
                    d0 = json.loads(r0.read())
                self.assertEqual(len(d0["models"]), 20)
                self.assertIn("config_path", d0)
                self.assertIn("models_config_path", d0)
                self.assertEqual(d0.get("writable_config_path"), user_cfg_path)
                self.assertEqual(
                    len(
                        server.TokenomicsWebHandler._build_sanitized_pricing_engine()._canonical_entries
                    ),
                    20,
                )

                # 2. Delete claude-opus-4 -> purged from Web, PricingEngine, AND TokenomicsConfig (19 models)
                del_req = urllib.request.Request(
                    f"{base}/api/models?id=claude-opus-4",
                    headers=json_hdrs,
                    method="DELETE",
                )
                with urllib.request.urlopen(del_req, timeout=3) as r_del:
                    d_del = json.loads(r_del.read())
                self.assertEqual(len(d_del["models"]), 19)
                self.assertIn("claude-opus-4", d_del["deleted_ids"])
                self.assertNotIn(
                    "claude-opus-4",
                    {
                        m["id"]
                        for m in TokenomicsConfig().get_models_config()[
                            "models"
                        ]
                    },
                )
                self.assertNotIn(
                    "claude-opus-4",
                    {
                        m["id"]
                        for m in server.TokenomicsWebHandler._build_sanitized_pricing_engine()._canonical_entries
                    },
                )

                # 3. POST /api/models_config_raw preserves deleted_ids
                raw_req = urllib.request.Request(
                    f"{base}/api/models_config_raw",
                    data=json.dumps(
                        {"raw_json": json.dumps({"models": d_del["models"]})}
                    ).encode("utf-8"),
                    headers=json_hdrs,
                    method="POST",
                )
                with urllib.request.urlopen(raw_req, timeout=3) as r_raw:
                    d_raw = json.loads(r_raw.read())
                self.assertEqual(len(d_raw["models"]), 19)
                disk_after_raw = json.loads(_read_text(user_cfg_path))
                self.assertIn(
                    "claude-opus-4", disk_after_raw.get("deleted_ids", [])
                )

                # 4. Re-add claude-opus-4 via POST /api/models -> clears deleted_ids & survives in models (M-a)
                readd_req = urllib.request.Request(
                    f"{base}/api/models",
                    data=json.dumps(
                        {
                            "id": "claude-opus-4",
                            "name": "Claude Opus 4 Restored",
                            "provider": "anthropic",
                            "pricing": {
                                "input": 12.0,
                                "cached": 1.2,
                                "output": 60.0,
                            },
                        }
                    ).encode("utf-8"),
                    headers=json_hdrs,
                    method="POST",
                )
                with urllib.request.urlopen(readd_req, timeout=3) as r_readd:
                    d_readd = json.loads(r_readd.read())
                self.assertEqual(len(d_readd["models"]), 20)
                disk_after_readd = json.loads(_read_text(user_cfg_path))
                self.assertNotIn(
                    "claude-opus-4", disk_after_readd.get("deleted_ids", [])
                )
                self.assertIn(
                    "claude-opus-4",
                    {
                        m["id"]
                        for m in TokenomicsConfig().get_models_config()[
                            "models"
                        ]
                    },
                )

                # 5. Distinct custom ID "gemini-3.8-flash-custom-test" preserved without overwriting gemini-3.8-flash (M-a)
                cust_req = urllib.request.Request(
                    f"{base}/api/models",
                    data=json.dumps(
                        {
                            "id": "gemini-3.8-flash-custom-test",
                            "name": "Custom Finetuned Flash",
                            "provider": "custom",
                            "pricing": {
                                "input": 1.1,
                                "cached": 0.11,
                                "output": 4.4,
                            },
                            "thinking_budget": "medium",
                            "max_output_tokens": 65536,
                        }
                    ).encode("utf-8"),
                    headers=json_hdrs,
                    method="POST",
                )
                with urllib.request.urlopen(cust_req, timeout=3) as r_cust:
                    d_cust = json.loads(r_cust.read())
                ids_after_cust = {m["id"]: m for m in d_cust["models"]}
                self.assertEqual(len(ids_after_cust), 21)
                self.assertIn("gemini-3.8-flash-custom-test", ids_after_cust)
                self.assertIn(
                    "publishers/google/models/gemini-3.8-flash", ids_after_cust
                )
                self.assertAlmostEqual(
                    ids_after_cust["publishers/google/models/gemini-3.8-flash"][
                        "pricing"
                    ]["input"],
                    0.75,
                    places=6,
                )
                self.assertEqual(
                    ids_after_cust["gemini-3.8-flash-custom-test"][
                        "thinking_budget"
                    ],
                    "medium",
                )
                self.assertEqual(
                    ids_after_cust["gemini-3.8-flash-custom-test"][
                        "max_output_tokens"
                    ],
                    65536,
                )

                # 5b. P2-a: Partial pricing update with *_per_million only updates paired keys without stale overwrite
                partial_pm_req = urllib.request.Request(
                    f"{base}/api/models",
                    data=json.dumps(
                        {
                            "id": "claude-opus-4",
                            "pricing": {"input_per_million": 9.99},
                        }
                    ).encode("utf-8"),
                    headers=json_hdrs,
                    method="POST",
                )
                with urllib.request.urlopen(partial_pm_req, timeout=3) as r_pm:
                    d_pm = json.loads(r_pm.read())
                self.assertAlmostEqual(
                    d_pm["model"]["pricing"]["input"], 9.99, places=6
                )
                self.assertAlmostEqual(
                    d_pm["model"]["pricing"]["input_per_million"],
                    9.99,
                    places=6,
                )
                self.assertAlmostEqual(
                    d_pm["model"]["pricing"]["cached"], 1.2, places=6
                )
                self.assertAlmostEqual(
                    d_pm["model"]["pricing"]["cached_per_million"],
                    1.2,
                    places=6,
                )
                self.assertAlmostEqual(
                    d_pm["model"]["pricing"]["output"], 60.0, places=6
                )
                self.assertAlmostEqual(
                    d_pm["model"]["pricing"]["output_per_million"],
                    60.0,
                    places=6,
                )

                # 6. External file edit on disk triggers automatic mtime-based pricing reload
                disk_mod = json.loads(_read_text(user_cfg_path))
                for m in disk_mod["models"]:
                    if m["id"] == "claude-opus-4":
                        m["pricing"] = {
                            "input": 25.0,
                            "cached": 2.5,
                            "output": 100.0,
                        }
                with open(user_cfg_path, "w", encoding="utf-8") as wf:
                    json.dump(disk_mod, wf)
                st_cur = os.stat(user_cfg_path)
                os.utime(
                    user_cfg_path,
                    ns=(st_cur.st_atime_ns, st_cur.st_mtime_ns + 10_000_000),
                )

                with urllib.request.urlopen(
                    f"{base}/api/metrics?session_id=s_rate", timeout=3
                ) as r_met:
                    m_after_mtime = json.loads(r_met.read())
                self.assertAlmostEqual(
                    m_after_mtime["total_cost"], 25.0, places=5
                )
                self.assertAlmostEqual(
                    server.TokenomicsWebHandler.pricing.get_rate(
                        "claude-opus-4"
                    )["input"],
                    25.0,
                    places=5,
                )

                # 7. replace_defaults=True skips seeding DEFAULT_MODELS_CONFIG in _build_sanitized_pricing_engine
                with open(user_cfg_path, "w", encoding="utf-8") as wf:
                    json.dump(
                        {
                            "replace_defaults": True,
                            "models": [
                                {
                                    "id": "custom/sole-enterprise-model",
                                    "name": "Sole Enterprise Model",
                                    "provider": "custom",
                                    "pricing": {
                                        "input": 2.0,
                                        "cached": 0.2,
                                        "output": 8.0,
                                    },
                                }
                            ],
                        },
                        wf,
                    )
                sole_engine = server.TokenomicsWebHandler._build_sanitized_pricing_engine()
                self.assertEqual(
                    [m["id"] for m in sole_engine._canonical_entries],
                    ["custom/sole-enterprise-model"],
                )

    def test_v102_orchestration_flow_agent_tool_skills_and_no_hardcoded_demo_names(
        self,
    ):
        handler = object.__new__(server.TokenomicsWebHandler)
        empty_resp = handler._build_metrics_response(
            [], source="local_traces", active_session_id="empty_s"
        )
        self.assertEqual(empty_resp["metrics"], {})
        self.assertEqual(empty_resp["tool_counts"], {})

        turns = [
            {
                "session_id": "s_flow",
                "app_name": "concierge_root",
                "orchestration_flow": "",
                "prompt_tokens": 1000,
                "input_tokens": 1000,
                "cached_tokens": 0,
                "output_tokens": 100,
                "thinking_tokens": 0,
                "estimated_cost": 0.003,
                "model_name": "gemini-3.8-flash",
                "tools_called": [
                    "research_agent_tool(query='Q1, Q2')",
                    "search(city='Paris', days=3)",
                ],
                "skills_active": ["finops-audit"],
                "invoked_tools": "research_agent_tool(query='Q1, Q2'), search(city='Paris', days=3)",
                "invoked_skills": "finops-audit",
            },
            {
                "session_id": "s_flow",
                "app_name": "research_agent_tool",
                "orchestration_flow": "",
                "prompt_tokens": 500,
                "input_tokens": 500,
                "cached_tokens": 0,
                "output_tokens": 50,
                "thinking_tokens": 0,
                "estimated_cost": 0.001,
                "model_name": "gemini-3.8-flash",
                "tools_called": [],
                "skills_active": [],
                "invoked_tools": "",
                "invoked_skills": "",
            },
        ]
        built = handler._build_metrics_response(
            turns, source="local_traces", active_session_id="s_flow"
        )
        self.assertEqual(
            built["metrics"]["concierge_root"]["orchestration_flow"], "Root"
        )
        self.assertEqual(
            built["metrics"]["research_agent_tool"]["orchestration_flow"],
            "AgentTool",
        )
        self.assertEqual(
            built["metrics"]["concierge_root"]["models"], ["gemini-3.8-flash"]
        )
        self.assertAlmostEqual(
            built["metrics"]["concierge_root"]["cost_share_pct"], 75.0, places=2
        )
        self.assertAlmostEqual(
            built["metrics"]["research_agent_tool"]["cost_share_pct"],
            25.0,
            places=2,
        )
        self.assertIn("search(city='Paris', days=3)", built["tool_counts"])
        self.assertNotIn("days=3)", built["tool_counts"])
        self.assertIn("finops-audit", built["tool_counts"])
        self.assertEqual(built["tool_counts"]["finops-audit"]["icon"], "⚡")


if __name__ == "__main__":
    unittest.main()
