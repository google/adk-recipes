"""Regression tests for issues found by the real `agents-cli` end-to-end run (round 2).

Covers: structured `raw_json.usage` (F7), visible skipped eval files and labeled
estimates (F9), web banner flushing (F10), extension-aware command names and
BigQuery install hints, `--help` guidance in the manifest, and the profiler's
clean-session message.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = REPO_ROOT / "scripts" / "run_tokenomics.py"
MANIFEST_PATH = REPO_ROOT / "agents-cli-extension.yaml"
APP_JS = REPO_ROOT / "adk_tokenomics_plugin" / "web" / "app.js"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "ADK_TOKENOMICS_PROG",
                                                            "AGENTS_CLI_EXTENSION_DIR")}
    env.update(extra)
    return env


def _launcher(args: list[str], cwd: Path, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LAUNCHER_PATH), *args],
        cwd=str(cwd), env=_clean_env(**env), capture_output=True, text=True, timeout=60,
    )


def _write_session_trace(path: Path, session_id: str, fresh: int = 500, cached: int = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "session_id": session_id,
        "turns": [{
            "timestamp": "2026-11-18T12:00:00Z", "session_id": session_id, "invocation_id": "inv_1",
            "app_name": "demo_app", "sub_agent": "demo_agent",
            "model_name": "publishers/google/models/gemini-3.8-flash",
            "user_query": "Weather in SF?", "agent_response": "60F and foggy.",
            "input_tokens": fresh, "cached_tokens": cached, "output_tokens": 40, "thinking_tokens": 0,
            "latency_ms": 100,
        }],
    }), encoding="utf-8")


def _write_uninstrumented_eval(path: Path) -> None:
    """What `agents-cli eval generate` writes: eval cases with no token usage metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"eval_cases": [
        {"eval_case_id": "c1", "prompt": {"role": "user", "parts": [{"text": "What's the weather in SF?"}]},
         "responses": [{"response": {"role": "model", "parts": [{"text": "It's 60 degrees and foggy."}]}}],
         "agent_data": {"agents": {"demo": {"instruction": "You are a helpful assistant."}}}},
        {"eval_case_id": "c2", "prompt": None, "responses": None, "agent_data": None},
    ]}), encoding="utf-8")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestStructuredUsageMetadata(unittest.TestCase):
    """F7: `raw_json.usage` must be JSON data, not a Python repr string."""

    def setUp(self):
        try:
            from google.genai import types  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            self.skipTest(f"google-genai not installed: {exc}")

    def test_serialize_usage_variants(self):
        from google.genai import types

        from adk_tokenomics_plugin.middleware.adk_callback import _serialize_usage

        usage = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=187, candidates_token_count=17, thoughts_token_count=41, total_token_count=245,
            prompt_tokens_details=[types.ModalityTokenCount(modality="TEXT", token_count=187)],
            traffic_type="ON_DEMAND",
        )
        data = _serialize_usage(usage)
        self.assertIsInstance(data, dict)
        self.assertEqual(data["prompt_token_count"], 187)
        self.assertEqual(data["thoughts_token_count"], 41)
        self.assertEqual(data["prompt_tokens_details"], [{"modality": "TEXT", "token_count": 187}])
        self.assertEqual(data["traffic_type"], "ON_DEMAND")
        self.assertNotIn("cached_content_token_count", data)  # None fields dropped
        json.dumps(data)  # JSON-native

        self.assertIsNone(_serialize_usage(None))
        self.assertEqual(_serialize_usage({"prompt_token_count": 3}), {"prompt_token_count": 3})
        duck = type("Usage", (), {"prompt_token_count": 5, "candidates_token_count": 2, "note": "x"})()
        self.assertEqual(_serialize_usage(duck), {"prompt_token_count": 5, "candidates_token_count": 2})
        self.assertIsInstance(_serialize_usage(object()), str)  # last-resort fallback never raises

    def test_plugin_records_structured_usage_end_to_end(self):
        from google.genai import types

        from adk_tokenomics_plugin import TokenControlTower

        tower = TokenControlTower(mode="dev")
        plugin = tower.create_adk_plugin(app_name="usage_bot")
        usage = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=300, cached_content_token_count=100, candidates_token_count=40,
            thoughts_token_count=10, total_token_count=350,
        )
        part = type("Part", (), {"text": "done"})()
        cand = type("Candidate", (), {"content": type("Content", (), {"parts": [part]})()})()
        resp = type("LlmResponse", (), {"usage_metadata": usage, "candidates": [cand],
                                        "model_version": "publishers/google/models/gemini-3.8-flash"})()
        ctx = type("CallbackContext", (), {"session_id": "sess_usage", "invocation_id": "inv_1"})()
        plugin.after_model_callback(callback_context=ctx, llm_response=resp)

        raw_usage = tower.tracker.turns_history[-1]["raw_json"]["usage"]
        self.assertIsInstance(raw_usage, dict)
        self.assertEqual(raw_usage["prompt_token_count"], 300)
        self.assertEqual(raw_usage["cached_content_token_count"], 100)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "trace.json"
            tower.tracker.export_traces(str(out))
            exported = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(exported["turns"][-1]["raw_json"]["usage"]["candidates_token_count"], 40)


class TestSkippedAndEstimatedVisibility(unittest.TestCase):
    """F9: skipped eval files are reported; character-count estimates are labeled."""

    def _project(self, tmp: str, with_session: bool = True) -> Path:
        proj = Path(tmp).resolve()
        traces = proj / "artifacts" / "traces"
        _write_uninstrumented_eval(traces / "traces_20261118_000000.json")
        if with_session:
            _write_session_trace(traces / "d8eed97b.json", "d8eed97b")
        return proj

    def test_assert_reports_skipped_eval_files_without_changing_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._project(tmp)
            for args in (["assert", "--traces", "artifacts/traces/"], ["assert", "--traces", "."], ["assert"]):
                res = _launcher([*args, "--max-cost-per-turn", "0.05", "--format", "json"], proj)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                data = json.loads(res.stdout)
                self.assertTrue(data["passed"])
                self.assertEqual(data["total_turns"], 1)
                self.assertEqual(len(data["skipped_artifacts"]), 1, args)
                self.assertTrue(data["skipped_artifacts"][0]["file"].endswith("traces_20261118_000000.json"))
                self.assertIn("without token usage", data["skipped_artifacts"][0]["reason"])
            text = _launcher(["assert", "--traces", "artifacts/traces/", "--max-cost-per-turn", "0.05"], proj)
            self.assertEqual(text.returncode, 0, text.stdout)
            self.assertIn("[INFO] Skipped 1 eval file(s) without token usage metadata", text.stdout)
            self.assertIn("traces_20261118_000000.json", text.stdout)

    def test_uninstrumented_only_still_fails_closed_and_reports_nothing_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._project(tmp, with_session=False)
            res = _launcher(["assert", "--traces", ".", "--max-cost-per-turn", "0.05", "--format", "json"], proj)
            self.assertEqual(res.returncode, 1, res.stdout)
            data = json.loads(res.stdout)
            self.assertFalse(data["passed"])
            self.assertNotIn("skipped_artifacts", data)

    def test_profile_directory_notice_counts_skipped_eval_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._project(tmp)
            res = _launcher(["profile", "-t", "artifacts/traces/", "--format", "json"], proj)
            self.assertEqual(res.returncode, 0, res.stderr)
            data = json.loads(res.stdout)
            self.assertEqual(data["session_files_in_directory"], 1)
            self.assertEqual(len(data["skipped_artifacts"]), 1)
            self.assertNotIn("tokens_estimated", data)
            self.assertIn("1 eval file(s) without token usage skipped: traces_20261118_000000.json", res.stderr)

    def test_profile_labels_character_count_estimates(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._project(tmp, with_session=False)
            target = "artifacts/traces/traces_20261118_000000.json"
            res = _launcher(["profile", "-t", target, "--format", "json"], proj)
            self.assertEqual(res.returncode, 0, res.stderr)
            data = json.loads(res.stdout)
            self.assertTrue(data["tokens_estimated"])
            self.assertIn("character-count estimates", data["warning"])
            self.assertTrue(data["turns"])
            self.assertTrue(all(t.get("tokens_estimated") for t in data["turns"]))
            text = _launcher(["profile", "-t", target], proj)
            self.assertIn("⚠️  ESTIMATED:", text.stdout)

            # Real usage is never labeled as estimated
            _write_session_trace(proj / "real.json", "real")
            real = json.loads(_launcher(["profile", "-t", "real.json", "--format", "json"], proj).stdout)
            self.assertNotIn("tokens_estimated", real)
            self.assertFalse(any(t.get("tokens_estimated") for t in real["turns"]))


class TestWebDashboardFixes(unittest.TestCase):
    """F9 (web) and F10."""

    def test_web_turns_carry_tokens_estimated_flag(self):
        from adk_tokenomics_plugin.cli_web import TokenomicsWebHandler

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            traces = Path(tmp).resolve() / "traces"
            _write_uninstrumented_eval(traces / "traces_20261118_000000.json")
            for expect_estimated in (True, False):
                TokenomicsWebHandler.set_server_context(project_id="", traces_path=str(traces))
                try:
                    handler = object.__new__(TokenomicsWebHandler)
                    turns = handler._load_local_trace_turns()
                finally:
                    TokenomicsWebHandler.set_server_context(project_id="", traces_path=None)
                self.assertTrue(turns)
                self.assertTrue(all(t["tokens_estimated"] is expect_estimated for t in turns), turns)
                # Second pass: a real session trace supersedes the eval file -> nothing estimated
                _write_session_trace(traces / "real.json", "real")

    def test_web_turns_table_marks_estimated_costs(self):
        """Runs the real app.js renderer (Node VM harness) against a real server and inspects the table HTML."""
        if not Path("/usr/bin/node").exists():
            self.skipTest("node not available")
        spec = importlib.util.spec_from_file_location("e2e_harness", REPO_ROOT / "tests" / "test_e2e_comprehensive.py")
        e2e = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(e2e)
        harness = e2e.NODE_APP_HARNESS_JS.replace(
            "      liveRows,\n      csvExports\n",
            "      liveRows,\n      csvExports,\n"
            "      sessionTurnsHtml: elementsById['session-turns-history-table-body'] ? "
            "String(elementsById['session-turns-history-table-body'].innerHTML || '') : ''\n",
        )
        self.assertIn("sessionTurnsHtml", harness, "harness output hook not found")

        from adk_tokenomics_plugin.cli_web import TokenomicsWebHandler

        def render(traces_dir: Path) -> str:
            TokenomicsWebHandler.set_server_context(project_id="", traces_path=str(traces_dir))
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TokenomicsWebHandler)
            th = threading.Thread(target=httpd.serve_forever, daemon=True)
            th.start()
            try:
                with mock.patch.object(e2e, "NODE_APP_HARNESS_JS", harness):
                    out = e2e._run_node_frontend_vm(f"http://127.0.0.1:{httpd.server_address[1]}", enable_chart=False)
                self.assertEqual(out["runtimeErrors"], [])
                return out["sessionTurnsHtml"]
            finally:
                httpd.shutdown()
                httpd.server_close()
                TokenomicsWebHandler.set_server_context(project_id="", traces_path=None)

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            est_dir = Path(tmp).resolve() / "est"
            _write_uninstrumented_eval(est_dir / "traces_20261118_000000.json")
            est_html = render(est_dir)
            self.assertIn("≈$", est_html)
            self.assertIn("Estimated from character counts", est_html)

            real_dir = Path(tmp).resolve() / "real"
            _write_session_trace(real_dir / "real.json", "real")
            real_html = render(real_dir)
            self.assertIn("$0.", real_html)
            self.assertNotIn("≈$", real_html)

    def test_web_banner_is_flushed_to_piped_stdout(self):
        if os.name == "nt":
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp).resolve()
            _write_session_trace(proj / "artifacts" / "traces" / "s1.json", "s1")
            port = _free_port()
            proc = subprocess.Popen(
                [sys.executable, str(LAUNCHER_PATH), "web", "-t", "artifacts/traces/", "--port", str(port), "--no-browser"],
                cwd=str(proj), env=_clean_env(XDG_CONFIG_HOME=str(proj / "xdg")),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            seen, buf = False, b""
            fd = proc.stdout.fileno()
            try:
                deadline = time.time() + 30
                while time.time() < deadline and proc.poll() is None and not seen:
                    ready, _, _ = select.select([fd], [], [], 0.5)
                    if ready:
                        chunk = os.read(fd, 65536)  # raw read: no Python-side buffering to hide data from select()
                        if not chunk:
                            break
                        buf += chunk
                        seen = b"Dashboard URL" in buf
                self.assertTrue(seen, f"banner not visible while server runs; got: {buf!r}")
                self.assertIsNone(proc.poll(), "server exited early")
            finally:
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                proc.stdout.close()


class TestExtensionAwareCommandNames(unittest.TestCase):
    """Usage lines, remediation hints, and install hints name commands the user actually has."""

    def test_usage_and_hints_use_agents_cli_names_under_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp).resolve()
            _write_session_trace(proj / "artifacts" / "traces" / "s1.json", "s1", fresh=5000)
            ext = {"AGENTS_CLI_EXTENSION_DIR": str(REPO_ROOT)}
            prof_h = _launcher(["profile", "-h"], proj, **ext)
            self.assertEqual(prof_h.returncode, 0)
            self.assertTrue(prof_h.stdout.startswith("usage: agents-cli tokenomics profile"), prof_h.stdout[:80])
            assert_h = _launcher(["assert", "-h"], proj, **ext)
            self.assertTrue(assert_h.stdout.startswith("usage: agents-cli tokenomics assert"), assert_h.stdout[:80])
            self.assertIn("agents-cli eval budget", assert_h.stdout)
            gate = _launcher(["assert", "--traces", "artifacts/traces/", "--max-cost-per-turn", "0.0000001"], proj, **ext)
            self.assertEqual(gate.returncode, 1)
            self.assertIn("-> Run: agents-cli tokenomics profile -t", gate.stdout)
            self.assertNotIn("Run: adk-tokenomics", gate.stdout)

            # Standalone (no agents-cli) keeps the console-script name
            plain = _launcher(["profile", "-h"], proj)
            self.assertTrue(plain.stdout.startswith("usage: adk-tokenomics profile"), plain.stdout[:80])

    def test_in_process_launcher_restores_prog_env(self):
        spec = importlib.util.spec_from_file_location("run_tokenomics_ext2", LAUNCHER_PATH)
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        with mock.patch.dict(os.environ, {"AGENTS_CLI_EXTENSION_DIR": str(REPO_ROOT)}):
            os.environ.pop("ADK_TOKENOMICS_PROG", None)
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                rc = launcher.run(["profile", "-h"])
            self.assertEqual(rc, 0)
            self.assertTrue(out.getvalue().startswith("usage: agents-cli tokenomics profile"))
            self.assertNotIn("ADK_TOKENOMICS_PROG", os.environ)

    def test_bigquery_install_hint_matches_launch_mode(self):
        from adk_tokenomics_plugin.core.config import bigquery_install_hint

        with mock.patch.dict(os.environ, {"ADK_TOKENOMICS_PROG": "agents-cli tokenomics"}):
            hint = bigquery_install_hint()
            self.assertIn("agents-cli install", hint)
            self.assertIn("uv add --no-workspace ./extensions/adk-tokenomics --extra bigquery", hint)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ADK_TOKENOMICS_PROG", None)
            hint = bigquery_install_hint()
            self.assertIn("pip install google-cloud-bigquery", hint)
        for mode in ("agents-cli tokenomics", ""):
            with mock.patch.dict(os.environ, {"ADK_TOKENOMICS_PROG": mode}):
                self.assertNotIn("adk-tokenomics-plugin[bigquery]", bigquery_install_hint())  # not on PyPI

    def test_manifest_help_text_points_to_dash_h(self):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover
            self.skipTest(f"PyYAML not installed: {exc}")
        added = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))["commands"]["add"]
        self.assertIn("-h", added["tokenomics"]["description"])
        self.assertIn("agents-cli eval budget -h", added["eval.budget"]["description"])


class TestProfilerCleanSessionMessage(unittest.TestCase):

    def test_small_prompt_session_explains_zero_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp).resolve()
            _write_session_trace(proj / "small.json", "small", fresh=187)
            res = _launcher(["profile", "-t", "small.json"], proj)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("No optimization opportunities detected", res.stdout)
            self.assertIn("1,024-token context-caching threshold (largest: 187)", res.stdout)
            self.assertNotIn("active context management", res.stdout)

    def test_large_uncached_prompt_gets_caching_recommendation_not_the_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp).resolve()
            _write_session_trace(proj / "large.json", "large", fresh=5000)
            res = _launcher(["profile", "-t", "large.json"], proj)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("[Context Caching]", res.stdout)
            self.assertNotIn("context-caching threshold (largest", res.stdout)


if __name__ == "__main__":
    unittest.main()
