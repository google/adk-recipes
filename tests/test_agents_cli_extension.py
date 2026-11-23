"""Unit and end-to-end regression tests for the `agents-cli` extension integration.

Validates:
1. `agents-cli-extension.yaml` against the official `agents-cli-extension/v1alpha1`
   JSON Schema and all `google/agents-cli` loader invariants (protected commands,
   command groups, interpreter-first `run:` vectors, and relative paths).
2. `skills/adk-tokenomics/SKILL.md` against `agents-cli` skill installation rules
   (slug naming, reserved prefixes, ignored/secret paths, symlink containment, and
   YAML frontmatter parity).
3. `scripts/run_tokenomics.py` extension-root resolution, `sys.path` bootstrapping,
   and isolated vendored execution (`AGENTS_CLI_EXTENSION_DIR` outside `REPO_ROOT`
   with `PYTHONPATH` cleared) for `tokenomics` (`web`, `profile`, `assert`) and
   `eval.budget`.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "agents-cli-extension.yaml"
LAUNCHER_PATH = REPO_ROOT / "scripts" / "run_tokenomics.py"
SKILL_DIR = REPO_ROOT / "skills" / "adk-tokenomics"
SKILL_FILE = SKILL_DIR / "SKILL.md"

# Official `google/agents-cli` v1alpha1 JSON Schema (`schemas/agents-cli-extension-v1alpha1.schema.json`)
AGENTS_CLI_EXTENSION_V1ALPHA1_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://raw.githubusercontent.com/google/agents-cli/main/schemas/agents-cli-extension-v1alpha1.schema.json",
    "$defs": {
        "CommandEntry": {
            "additionalProperties": False,
            "properties": {
                "run": {"items": {"type": "string"}, "minItems": 1, "type": "array"},
                "description": {"default": "", "anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["run"],
            "type": "object",
        },
        "Commands": {
            "additionalProperties": False,
            "properties": {
                "override": {
                    "anyOf": [
                        {"additionalProperties": {"$ref": "#/$defs/CommandEntry"}, "type": "object"},
                        {"type": "null"},
                    ]
                },
                "add": {
                    "anyOf": [
                        {"additionalProperties": {"$ref": "#/$defs/CommandEntry"}, "type": "object"},
                        {"type": "null"},
                    ]
                },
            },
            "type": "object",
        },
        "Requires": {
            "additionalProperties": False,
            "properties": {
                "agents_cli": {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "null"}], "default": None},
                "on_incompatible": {
                    "default": "warn",
                    "anyOf": [{"enum": ["warn", "error"], "type": "string"}, {"type": "null"}],
                },
            },
            "type": "object",
        },
        "SkillEntry": {
            "additionalProperties": False,
            "properties": {"path": {"minLength": 1, "type": "string"}},
            "required": ["path"],
            "type": "object",
        },
    },
    "additionalProperties": False,
    "properties": {
        "schema": {
            "default": "agents-cli-extension/v1alpha1",
            "anyOf": [{"const": "agents-cli-extension/v1alpha1", "type": "string"}, {"type": "null"}],
        },
        "name": {"anyOf": [{"minLength": 1, "type": "string"}, {"type": "null"}], "default": None},
        "description": {"default": "", "anyOf": [{"type": "string"}, {"type": "null"}]},
        "requires": {"anyOf": [{"$ref": "#/$defs/Requires"}, {"type": "null"}], "default": None},
        "commands": {"anyOf": [{"$ref": "#/$defs/Commands"}, {"type": "null"}]},
        "skills": {
            "anyOf": [
                {
                    "additionalProperties": {"$ref": "#/$defs/SkillEntry"},
                    "propertyNames": {"maxLength": 64, "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
                    "type": "object",
                },
                {"type": "null"},
            ]
        },
    },
    "type": "object",
}

RECOVERY_COMMANDS = frozenset(
    {
        "install",
        "extension",
        "extension.add",
        "extension.list",
        "extension.remove",
        "extension.update",
        "info",
        "login",
        "setup",
        "update",
    }
)
BUILTIN_TOP_LEVEL_COMMANDS = frozenset(
    {
        "setup",
        "update",
        "login",
        "scaffold",
        "create",
        "playground",
        "run",
        "lint",
        "install",
        "build",
        "command-center",
        "acc",
        "cc",
        "data-ingestion",
        "eval",
        "deploy",
        "publish",
        "infra",
        "extension",
        "info",
    }
)
BUILTIN_COMMAND_GROUPS = frozenset({"scaffold", "eval", "publish", "infra", "extension"})
SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".js", ".ts", ".rb", ".pl"})
IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        "node_modules",
        ".next",
        "dist",
        "build",
        ".ds_store",
        ".vscode",
        ".idea",
        ".mypy_cache",
        ".ruff_cache",
        ".ty",
        ".coverage",
        "htmlcov",
        ".tox",
        ".cache",
        ".terraform",
        ".adk",
    }
)


def _load_yaml_file(path: Path) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise unittest.SkipTest(f"PyYAML not installed: {exc}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AssertionError(f"Expected YAML mapping in {path}, got {type(data)!r}")
    return data


def _load_launcher_module():
    spec = importlib.util.spec_from_file_location("run_tokenomics_ext", LAUNCHER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse_skill_frontmatter(markdown_text: str) -> dict:
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", markdown_text, flags=re.DOTALL)
    if not match:
        raise AssertionError("SKILL.md is missing leading YAML frontmatter ('--- ... ---').")
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(match.group(1))
        if not isinstance(parsed, dict):
            raise AssertionError("SKILL.md frontmatter must parse to a mapping.")
        return parsed
    except ImportError:  # pragma: no cover
        result = {}
        for line in match.group(1).splitlines():
            if ":" in line and not line.startswith(" "):
                k, v = line.split(":", 1)
                result[k.strip()] = v.strip()
        return result


class TestAgentsCliExtensionManifestAndSkill(unittest.TestCase):

    def test_manifest_exists_and_matches_official_v1alpha1_schema(self):
        self.assertTrue(MANIFEST_PATH.is_file(), f"Missing {MANIFEST_PATH}")
        raw_text = MANIFEST_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "https://raw.githubusercontent.com/google/agents-cli/main/schemas/agents-cli-extension-v1alpha1.schema.json",
            raw_text,
        )

        data = _load_yaml_file(MANIFEST_PATH)
        try:
            import jsonschema  # type: ignore

            jsonschema.validate(instance=data, schema=AGENTS_CLI_EXTENSION_V1ALPHA1_SCHEMA)
        except ImportError:  # pragma: no cover
            pass

        self.assertEqual(data.get("schema"), "agents-cli-extension/v1alpha1")
        self.assertEqual(data.get("name"), "adk-tokenomics")
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9._-]+", data["name"]))
        self.assertTrue(data.get("description", "").strip())

        requires = data.get("requires") or {}
        self.assertEqual(requires.get("agents_cli"), ">=1.10,<2")
        self.assertEqual(requires.get("on_incompatible"), "warn")

    def test_commands_respect_agents_cli_loader_and_runner_rules(self):
        data = _load_yaml_file(MANIFEST_PATH)
        commands = data.get("commands") or {}
        added = commands.get("add") or {}
        overridden = commands.get("override") or {}

        self.assertIn("tokenomics", added)
        self.assertIn("eval.budget", added)
        self.assertEqual(overridden, {})

        for kind, mapping in (("add", added), ("override", overridden)):
            for cmd_name, entry in mapping.items():
                self.assertNotIn(
                    cmd_name,
                    RECOVERY_COMMANDS,
                    f"Extension must never claim protected recovery command {cmd_name!r}",
                )
                if "." in cmd_name:
                    parts = cmd_name.split(".")
                    self.assertEqual(
                        len(parts),
                        2,
                        f"agents-cli v1alpha1 only supports single-level group.sub, got {cmd_name!r}",
                    )
                    group_name, sub_name = parts
                    self.assertIn(
                        group_name,
                        BUILTIN_COMMAND_GROUPS,
                        f"Dotted subcommand parent {group_name!r} must be a built-in command group",
                    )
                    self.assertTrue(sub_name)
                elif kind == "add":
                    self.assertNotIn(
                        cmd_name,
                        BUILTIN_TOP_LEVEL_COMMANDS,
                        f"commands.add.{cmd_name} collides with a built-in command",
                    )

                run_vec = entry.get("run")
                self.assertIsInstance(run_vec, list)
                self.assertGreaterEqual(len(run_vec), 1)
                first_tok = run_vec[0]
                self.assertNotIn(
                    PurePosixPath(first_tok).suffix,
                    SCRIPT_SUFFIXES,
                    f"commands.{kind}.{cmd_name} run[0]={first_tok!r} must be an executable, not a bare script",
                )
                self.assertEqual(run_vec[:4], ["uv", "run", "--no-sync", "python"])
                self.assertTrue(entry.get("description", "").strip())

                for tok in run_vec:
                    if "/" in tok or "\\" in tok:
                        self.assertNotIn("\\", tok, "Use forward slashes in manifest paths")
                        target = (REPO_ROOT / tok).resolve()
                        self.assertTrue(
                            target.is_relative_to(REPO_ROOT.resolve()),
                            f"Path token {tok!r} escapes REPO_ROOT",
                        )
                        self.assertTrue(
                            target.is_file(),
                            f"Manifest command {cmd_name!r} references missing file {tok!r}",
                        )

        self.assertEqual(
            added["eval.budget"]["run"],
            [
                "uv",
                "run",
                "--no-sync",
                "python",
                "scripts/run_tokenomics.py",
                "--prog-override",
                "agents-cli eval budget",
                "assert",
            ],
        )

    def test_bundled_skill_passes_agents_cli_skill_validation(self):
        data = _load_yaml_file(MANIFEST_PATH)
        skills = data.get("skills") or {}
        self.assertEqual(list(skills.keys()), ["adk-tokenomics"])

        for skill_name, entry in skills.items():
            self.assertLessEqual(len(skill_name), 64)
            self.assertRegex(skill_name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            self.assertFalse(
                skill_name.startswith("google-agents-cli-"),
                "Skill names starting with 'google-agents-cli-' are reserved by agents-cli setup",
            )

            rel_path = entry["path"]
            self.assertNotIn("\\", rel_path)
            posix_path = PurePosixPath(rel_path)
            self.assertFalse(posix_path.is_absolute())
            self.assertNotIn("..", posix_path.parts)
            for part in posix_path.parts:
                self.assertNotIn(part.lower(), IGNORED_DIR_NAMES)
                self.assertFalse(part.lower().startswith(".env"))

            resolved_skill_dir = (REPO_ROOT / rel_path).resolve()
            self.assertTrue(resolved_skill_dir.is_relative_to(REPO_ROOT.resolve()))
            self.assertTrue(resolved_skill_dir.is_dir())

            skill_md = resolved_skill_dir / "SKILL.md"
            self.assertTrue(skill_md.is_file())
            self.assertFalse(skill_md.is_symlink())

            text = skill_md.read_text(encoding="utf-8")
            fm = _parse_skill_frontmatter(text)
            self.assertEqual(fm.get("name"), skill_name)
            self.assertTrue(str(fm.get("description", "")).strip())
            self.assertIn("agents-cli tokenomics profile", text)
            self.assertIn("agents-cli eval budget", text)
            self.assertIn("agents-cli tokenomics web", text)
            self.assertIn("ADKTokenomicsPlugin", text)


class TestRunTokenomicsVendoredEntrypoint(unittest.TestCase):

    def test_resolve_extension_root_and_bootstrap_sys_path(self):
        launcher = _load_launcher_module()

        with tempfile.TemporaryDirectory() as tmp:
            fake_ext = Path(tmp) / "vendored_ext"
            pkg_dir = fake_ext / "adk_tokenomics_plugin"
            pkg_dir.mkdir(parents=True)
            (pkg_dir / "__init__.py").write_text("__version__ = '0.0.0-test'\n", encoding="utf-8")

            # 1. Valid AGENTS_CLI_EXTENSION_DIR wins
            resolved = launcher.resolve_extension_root({"AGENTS_CLI_EXTENSION_DIR": str(fake_ext)})
            self.assertEqual(resolved, fake_ext.resolve())

            # 2. Invalid or empty AGENTS_CLI_EXTENSION_DIR falls back to script parent
            empty_dir = Path(tmp) / "empty_dir"
            empty_dir.mkdir()
            fallback_invalid = launcher.resolve_extension_root({"AGENTS_CLI_EXTENSION_DIR": str(empty_dir)})
            self.assertEqual(fallback_invalid, REPO_ROOT.resolve())
            fallback_unset = launcher.resolve_extension_root({})
            self.assertEqual(fallback_unset, REPO_ROOT.resolve())

            # 3. bootstrap_sys_path places target at sys.path[0] without duplicates and fails closed on missing pkg
            orig_sys_path = sys.path[:]
            try:
                sys.path.append(str(fake_ext))
                bootstrapped = launcher.bootstrap_sys_path(fake_ext)
                self.assertEqual(bootstrapped, fake_ext.resolve())
                self.assertEqual(Path(sys.path[0]).resolve(), fake_ext.resolve())
                matches = [p for p in sys.path if p and Path(p).resolve() == fake_ext.resolve()]
                self.assertEqual(len(matches), 1)
            finally:
                sys.path[:] = orig_sys_path

            with self.assertRaises(RuntimeError):
                launcher.bootstrap_sys_path(empty_dir)

    def test_in_process_run_preserves_sys_argv_and_exit_codes(self):
        launcher = _load_launcher_module()
        orig_argv = sys.argv[:]

        # Help output (main() returns None -> normalized to 0)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc_help = launcher.run([])
        self.assertEqual(rc_help, 0)
        self.assertEqual(sys.argv, orig_argv)

        # Missing trace file on assert -> fails closed with exit code 1
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "nonexistent_traces")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc_fail = launcher.run(["assert", "--traces", missing, "--format", "json"])
            self.assertEqual(rc_fail, 1)
            self.assertEqual(sys.argv, orig_argv)

    def test_vendored_extension_subprocess_simulation_outside_repo(self):
        """Simulate `agents-cli` vendoring into a clean project with no PYTHONPATH or pip install."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            vendored_ext = tmp_root / "user_project" / "extensions" / "adk-tokenomics"
            user_project = tmp_root / "user_project"
            traces_dir = user_project / "artifacts" / "traces"
            traces_dir.mkdir(parents=True)

            # Copy extension files exactly as `agents-cli extension add` vendors them
            vendored_ext.mkdir(parents=True)
            shutil.copy2(MANIFEST_PATH, vendored_ext / "agents-cli-extension.yaml")
            shutil.copytree(REPO_ROOT / "scripts", vendored_ext / "scripts")
            shutil.copytree(REPO_ROOT / "skills", vendored_ext / "skills")
            shutil.copytree(
                REPO_ROOT / "adk_tokenomics_plugin",
                vendored_ext / "adk_tokenomics_plugin",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

            trace_payload = {
                "turns": [
                    {
                        "timestamp": "2026-11-16T12:00:00Z",
                        "session_id": "sess-ext-01",
                        "app_name": "concierge_app",
                        "model_name": "publishers/google/models/gemini-3.8-flash",
                        "user_query": "[router_agent] Book flights to Tokyo",
                        "agent_response": "Routing to flight specialist.",
                        "input_tokens": 1600,
                        "cached_tokens": 0,
                        "output_tokens": 120,
                        "thinking_tokens": 0,
                        "latency_ms": 420,
                        "tools_called": ["lookup_flights"],
                        "raw_json": {"sub_agent": "router_agent"},
                    }
                ]
            }
            (traces_dir / "sess-ext-01.json").write_text(
                json.dumps(trace_payload, indent=2), encoding="utf-8"
            )

            vendored_script = str((vendored_ext / "scripts" / "run_tokenomics.py").resolve())
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("PYTHONPATH", "PYTHONHOME")
            }
            clean_env["AGENTS_CLI_EXTENSION_DIR"] = str(vendored_ext.resolve())
            clean_env["AGENTS_CLI_DISABLE_OVERRIDES"] = "1"
            clean_env["PYTHONDONTWRITEBYTECODE"] = "1"

            # 1. Verify `agents-cli tokenomics` (no subcommand -> prints help, exits 0)
            help_proc = subprocess.run(
                [sys.executable, vendored_script],
                cwd=str(user_project),
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(help_proc.returncode, 0, help_proc.stderr)
            self.assertIn("ADK Token Control Tower", help_proc.stdout)

            # 2. Verify `agents-cli tokenomics profile -t artifacts/traces/ --format json`
            profile_proc = subprocess.run(
                [sys.executable, vendored_script, "profile", "-t", "artifacts/traces/", "--format", "json"],
                cwd=str(user_project),
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(profile_proc.returncode, 0, profile_proc.stderr)
            profile_json = json.loads(profile_proc.stdout)
            self.assertEqual(profile_json["total_turns"], 1)
            self.assertIn("router_agent", profile_json["sub_agent_breakdown"])
            self.assertGreaterEqual(len(profile_json["recommendations"]), 1)

            # 3. Verify `agents-cli eval budget` pass gate (`run_tokenomics.py assert ...`)
            budget_pass = subprocess.run(
                [
                    sys.executable,
                    vendored_script,
                    "assert",
                    "--traces",
                    "artifacts/traces/",
                    "--max-cost-per-turn",
                    "0.05",
                    "--max-latency-ms",
                    "2000",
                    "--format",
                    "json",
                ],
                cwd=str(user_project),
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(budget_pass.returncode, 0, budget_pass.stderr)
            pass_json = json.loads(budget_pass.stdout)
            self.assertTrue(pass_json["passed"])
            self.assertEqual(pass_json["total_turns"], 1)

            # 4. Verify `agents-cli eval budget` fail-closed breach gate (`exit 1`)
            budget_fail = subprocess.run(
                [
                    sys.executable,
                    vendored_script,
                    "assert",
                    "--traces",
                    "artifacts/traces/",
                    "--max-cost-per-turn",
                    "0.000001",
                    "--format",
                    "json",
                ],
                cwd=str(user_project),
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(budget_fail.returncode, 1)
            fail_json = json.loads(budget_fail.stdout)
            self.assertFalse(fail_json["passed"])
            self.assertGreaterEqual(len(fail_json["violations"]), 1)

            # 5. If `uv` is installed (e.g. ~/.local/bin/uv or PATH), also verify the exact `["uv", "run", "--no-sync", "python", ...]` vector!
            uv_bin = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
            if os.path.isfile(uv_bin) and os.access(uv_bin, os.X_OK):
                uv_proc = subprocess.run(
                    [
                        uv_bin,
                        "run",
                        "--no-sync",
                        "python",
                        vendored_script,
                        "assert",
                        "--traces",
                        "artifacts/traces/",
                        "--max-cost-per-turn",
                        "0.05",
                        "--format",
                        "json",
                    ],
                    cwd=str(user_project),
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(uv_proc.returncode, 0, uv_proc.stderr)
                self.assertTrue(json.loads(uv_proc.stdout)["passed"])

    def test_agents_cli_scaffold_and_eval_generate_coexistence(self):
        """
        Regression test for post-`agents-cli` integration:
        1. `agents-cli create` scaffolds `deployment_metadata.json` in project root `.`
           while traces live in `./artifacts/traces/`. `agents-cli eval budget --traces .`
           must skip `deployment_metadata.json` and discover `./artifacts/traces/*.json`.
        2. `agents-cli eval generate` writes Stage-2 `artifacts/traces/traces_<ts>.json`
           (`{"eval_cases": [...]}` without `usage_metadata`, including multi-turn `"prompt": null`
           cases) into the same folder as `ADKTokenomicsPlugin` `<session_id>.json` traces.
           `assert` / `eval budget`, `profile`, and `web` must evaluate the real instrumented
           turns without failing on or double-counting `traces_<ts>.json` — while STILL failing
           closed (`exit 1`) when ONLY uninstrumented `traces_<ts>.json` exists!
        """
        import time
        from adk_tokenomics_plugin.cli_web import TokenomicsWebHandler

        with tempfile.TemporaryDirectory(prefix="acli_coexist_") as tmp:
            tmp_path = Path(tmp)
            vendored_ext = tmp_path / "vendored" / "adk-tokenomics"
            vendored_ext.mkdir(parents=True)
            shutil.copy2(MANIFEST_PATH, vendored_ext / "agents-cli-extension.yaml")
            shutil.copytree(REPO_ROOT / "scripts", vendored_ext / "scripts")
            shutil.copytree(REPO_ROOT / "skills", vendored_ext / "skills")
            shutil.copytree(
                REPO_ROOT / "adk_tokenomics_plugin",
                vendored_ext / "adk_tokenomics_plugin",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            vendored_script = str((vendored_ext / "scripts" / "run_tokenomics.py").resolve())

            proj = tmp_path / "demo_agent_proj"
            traces_dir = proj / "artifacts" / "traces"
            traces_dir.mkdir(parents=True, exist_ok=True)

            # Scaffolded `agents-cli create` root metadata files
            (proj / "deployment_metadata.json").write_text(
                json.dumps({"remote_agent_runtime_id": None, "deployment_timestamp": None}),
                encoding="utf-8",
            )
            (proj / "biome.json").write_text(json.dumps({"formatter": {"enabled": True}}), encoding="utf-8")

            # Uninstrumented Stage-2 `traces_<ts>.json` written by `agents-cli eval generate` (including prompt=None)
            stage2_eval_file = traces_dir / "traces_20261118_202253.json"
            stage2_eval_file.write_text(
                json.dumps(
                    {
                        "eval_cases": [
                            {
                                "eval_case_id": "single_turn",
                                "prompt": {"role": "user", "parts": [{"text": "Weather in SF?"}]},
                                "responses": [{"response": {"role": "model", "parts": [{"text": "60F foggy"}]}}],
                                "agent_data": {"agents": {"demo_agent": {"instruction": "Helpful assistant"}}},
                            },
                            {
                                "eval_case_id": "multi_turn_null_prompt",
                                "prompt": None,
                                "responses": None,
                                "agent_data": None,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            clean_env = {
                k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")
            }
            clean_env["AGENTS_CLI_EXTENSION_DIR"] = str(vendored_ext.resolve())
            clean_env["AGENTS_CLI_DISABLE_OVERRIDES"] = "1"
            clean_env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")

            # Case A: When ONLY uninstrumented `traces_<ts>.json` exists (user forgot ADKTokenomicsPlugin),
            # `agents-cli eval budget` MUST STILL FAIL CLOSED (`exit 1`)!
            uninst_only = subprocess.run(
                [sys.executable, vendored_script, "assert", "--traces", ".", "--max-cost-per-turn", "0.05", "--format", "json"],
                cwd=str(proj),
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(uninst_only.returncode, 1, uninst_only.stdout + uninst_only.stderr)
            self.assertFalse(json.loads(uninst_only.stdout)["passed"])

            # Also verify Web `None`-safety when ONLY `traces_<ts>.json` (with prompt=None) exists
            TokenomicsWebHandler.set_server_context(project_id="", traces_path=str(traces_dir))
            try:
                handler = object.__new__(TokenomicsWebHandler)
                null_safe_turns = handler._load_local_trace_turns()
                self.assertEqual(len(null_safe_turns), 2)
            finally:
                TokenomicsWebHandler.set_server_context(project_id="", traces_path=None)

            # Case B: Add real `ADKTokenomicsPlugin` session trace AND give `traces_<ts>.json` a NEWER mtime
            sess_file = traces_dir / "d8eed97b-1e41-40c8-b8d2-e9ec178f98d3.json"
            sess_file.write_text(
                json.dumps(
                    {
                        "session_id": "d8eed97b-1e41-40c8-b8d2-e9ec178f98d3",
                        "turns": [
                            {
                                "session_id": "d8eed97b-1e41-40c8-b8d2-e9ec178f98d3",
                                "invocation_id": "inv_1",
                                "app_name": "demo_agent",
                                "sub_agent": "demo_agent",
                                "model_name": "publishers/google/models/gemini-3.8-flash",
                                "user_query": "Weather in SF?",
                                "agent_response": "60F and foggy.",
                                "input_tokens": 500,
                                "cached_tokens": 1500,
                                "output_tokens": 80,
                                "thinking_tokens": 32,
                                "latency_ms": 120,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            now_ts = time.time()
            os.utime(sess_file, (now_ts - 10, now_ts - 10))
            os.utime(stage2_eval_file, (now_ts, now_ts))

            # 1. `agents-cli eval budget --traces .` (documented README/SKILL command) -> exit 0, 1 real turn!
            dot_budget = subprocess.run(
                [sys.executable, vendored_script, "assert", "--traces", ".", "--max-cost-per-turn", "0.05", "--format", "json"],
                cwd=str(proj),
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(dot_budget.returncode, 0, dot_budget.stdout + dot_budget.stderr)
            dot_json = json.loads(dot_budget.stdout)
            self.assertTrue(dot_json["passed"])
            self.assertEqual(dot_json["total_turns"], 1)

            # 2. `agents-cli eval budget` (default discovery) -> exit 0, 1 real turn!
            def_budget = subprocess.run(
                [sys.executable, vendored_script, "assert", "--max-cost-per-turn", "0.05", "--format", "json"],
                cwd=str(proj),
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(def_budget.returncode, 0, def_budget.stdout + def_budget.stderr)
            self.assertEqual(json.loads(def_budget.stdout)["total_turns"], 1)

            # 3. `agents-cli tokenomics profile` (no-arg AND `-t .`) -> selects real session trace over newer `traces_<ts>.json`!
            for prof_args in (["profile", "--format", "json"], ["profile", "-t", ".", "--format", "json"]):
                prof_res = subprocess.run(
                    [sys.executable, vendored_script, *prof_args],
                    cwd=str(proj),
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(prof_res.returncode, 0, prof_res.stdout + prof_res.stderr)
                p_data = json.loads(prof_res.stdout)
                self.assertIn("d8eed97b-1e41-40c8-b8d2-e9ec178f98d3.json", p_data["file"])
                self.assertEqual(p_data["turns"][0]["cached_tokens"], 1500)

            # 4. Web `_load_local_trace_turns` on `proj` or `traces_dir` -> exact 1 turn (zero double-counting!)
            for web_target in (str(proj), str(traces_dir)):
                TokenomicsWebHandler.set_server_context(project_id="", traces_path=web_target)
                try:
                    handler = object.__new__(TokenomicsWebHandler)
                    loaded_turns = handler._load_local_trace_turns()
                    self.assertEqual(len(loaded_turns), 1)
                    self.assertEqual(loaded_turns[0]["cached_tokens"], 1500)
                finally:
                    TokenomicsWebHandler.set_server_context(project_id="", traces_path=None)


def _write_trace(path: Path, session_id: str, cost_tokens: int = 1600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "turns": [
                    {
                        "timestamp": "2026-11-18T12:00:00Z",
                        "session_id": session_id,
                        "app_name": "demo_app",
                        "sub_agent": "demo_agent",
                        "model_name": "publishers/google/models/gemini-3.8-flash",
                        "user_query": "hello",
                        "agent_response": "hi",
                        "input_tokens": cost_tokens,
                        "cached_tokens": 0,
                        "output_tokens": 40,
                        "thinking_tokens": 0,
                        "latency_ms": 100,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _port_open(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launcher_pid_alive(pid: int) -> bool:
    """True while `pid` exists and is not a zombie (re-parented processes may linger as zombies briefly)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return fh.read().decode(errors="replace").rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False


class TestLauncherUserExperienceFixes(unittest.TestCase):
    """Regression tests for findings from the real agents-cli E2E run (F4, F6, F8)."""

    def test_relative_paths_resolve_from_user_shell_directory(self):
        launcher = _load_launcher_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "proj"
            app = root / "app"
            app.mkdir(parents=True)
            trace = root / "artifacts" / "traces" / "s1.json"
            _write_trace(trace, "s1")
            env_sub = {"PWD": str(app)}

            # Shell-relative path typed from app/ -> rewritten to absolute
            out = launcher.resolve_user_relative_paths(
                ["profile", "-t", "../artifacts/traces/s1.json", "--format", "json"], cwd=root, env=env_sub
            )
            self.assertEqual(out, ["profile", "-t", str(trace), "--format", "json"])
            # --flag=value form
            out = launcher.resolve_user_relative_paths(
                ["assert", "--traces=../artifacts/traces/", "--max-cost-per-turn", "0.05"], cwd=root, env=env_sub
            )
            self.assertEqual(out, ["assert", f"--traces={(root / 'artifacts' / 'traces').resolve()}", "--max-cost-per-turn", "0.05"])
            # Project-root-relative path (does not exist under app/) -> unchanged (backward compatible)
            out = launcher.resolve_user_relative_paths(["profile", "-t", "artifacts/traces/s1.json"], cwd=root, env=env_sub)
            self.assertEqual(out, ["profile", "-t", "artifacts/traces/s1.json"])
            # Absolute paths and non-path flag values are never touched
            out = launcher.resolve_user_relative_paths(["profile", "-t", str(trace), "--port", "../x"], cwd=root, env=env_sub)
            self.assertEqual(out, ["profile", "-t", str(trace), "--port", "../x"])
            # $PWD equal to cwd, outside the project, or missing -> unchanged
            argv = ["profile", "-t", "../artifacts/traces/s1.json"]
            for env in ({"PWD": str(root)}, {"PWD": str(Path(tmp).resolve())}, {}):
                self.assertEqual(launcher.resolve_user_relative_paths(argv, cwd=root, env=env), argv)

    def test_subfolder_relative_path_end_to_end_via_real_shell(self):
        """`cd app && <launcher> profile -t ../artifacts/...` with cwd forced to the project root, like agents-cli."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "proj"
            (root / "app").mkdir(parents=True)
            _write_trace(root / "artifacts" / "traces" / "s1.json", "s1")
            env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
            env["PWD"] = str(root / "app")  # what bash exports after `cd app`
            proc = subprocess.run(
                [sys.executable, str(LAUNCHER_PATH), "profile", "-t", "../artifacts/traces/s1.json", "--format", "json"],
                cwd=str(root),  # agents-cli always runs extension commands at the project root
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["total_turns"], 1)

    def test_parent_watchdog_detects_dead_ancestor_with_real_processes(self):
        launcher = _load_launcher_module()
        if os.name == "nt":
            self.skipTest("POSIX-only")
        self.assertEqual(launcher.parent_pid_of(os.getpid()), os.getppid())
        middle = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys, time\n"
                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "print(c.pid, flush=True)\n"
                "time.sleep(60)\n",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        child_pid = int(middle.stdout.readline().strip())
        try:
            snapshot = launcher.snapshot_ancestors(depth=1, pid=child_pid)
            self.assertEqual(snapshot, {middle.pid: os.getpid()})
            self.assertFalse(launcher.ancestors_changed(snapshot))
            middle.kill()
            middle.wait(timeout=10)
            self.assertTrue(launcher.ancestors_changed(snapshot))
        finally:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
            if middle.poll() is None:
                middle.kill()
            middle.stdout.close()

    def test_web_stops_when_launching_process_is_killed(self):
        """Mimic agents-cli -> uv -> python: SIGKILL the middle process; the web server must not be orphaned."""
        self._assert_web_not_orphaned(close_pipes=False)

    def test_web_stops_when_launcher_dies_and_its_output_pipes_close(self):
        """CI runners capture output and close the pipes on timeout; the shutdown notice must not crash the watchdog."""
        self._assert_web_not_orphaned(close_pipes=True)

    def _assert_web_not_orphaned(self, close_pipes: bool):
        if os.name == "nt":
            self.skipTest("POSIX-only")
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp).resolve()
            _write_trace(proj / "artifacts" / "traces" / "s1.json", "s1")
            port = _free_port()
            env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
            env["AGENTS_CLI_EXTENSION_DIR"] = str(REPO_ROOT)
            env["XDG_CONFIG_HOME"] = str(proj / "xdg")
            web_cmd = [sys.executable, str(LAUNCHER_PATH), "web", "-t", "artifacts/traces/", "--port", str(port), "--no-browser"]
            middle = subprocess.Popen(
                [sys.executable, "-c",
                 "import subprocess, sys\np = subprocess.Popen(sys.argv[1:])\nprint(p.pid, flush=True)\np.wait()\n", *web_cmd],
                cwd=str(proj), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE if close_pipes else subprocess.DEVNULL, text=True,
            )
            web_pid = int(middle.stdout.readline().strip())
            try:
                deadline = time.time() + 30
                while time.time() < deadline and not _port_open(port):
                    time.sleep(0.2)
                self.assertTrue(_port_open(port), "web server never came up")
                middle.kill()  # like a CI timeout killing agents-cli
                middle.wait(timeout=10)
                if close_pipes:
                    middle.stdout.close()
                    middle.stderr.close()
                deadline = time.time() + 15
                while time.time() < deadline and _port_open(port):
                    time.sleep(0.2)
                self.assertFalse(_port_open(port), "web server was orphaned after its launcher died")
                deadline = time.time() + 10
                while time.time() < deadline and launcher_pid_alive(web_pid):
                    time.sleep(0.2)
                self.assertFalse(launcher_pid_alive(web_pid), "web process still running after its port closed")
            finally:
                try:
                    os.kill(web_pid, 9)
                except ProcessLookupError:
                    pass
                for stream in (middle.stdout, middle.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_web_watchdog_only_runs_under_agents_cli(self):
        launcher = _load_launcher_module()
        self.assertTrue(launcher._is_web_command(["web", "-t", "x"]))
        self.assertTrue(launcher._is_web_command(["ui"]))
        self.assertFalse(launcher._is_web_command(["profile", "-t", "web"]))
        self.assertFalse(launcher._is_web_command(["assert"]))

    def test_profile_directory_reports_which_session_it_picked(self):
        with tempfile.TemporaryDirectory() as tmp:
            traces = Path(tmp).resolve() / "traces"
            _write_trace(traces / "older.json", "older")
            os.utime(traces / "older.json", (time.time() - 60, time.time() - 60))
            _write_trace(traces / "newer.json", "newer")
            env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
            proc = subprocess.run(
                [sys.executable, str(LAUNCHER_PATH), "profile", "-t", str(traces), "--format", "json"],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = json.loads(proc.stdout)  # stdout must stay pure JSON
            self.assertEqual(data["session_files_in_directory"], 2)
            self.assertTrue(data["file"].endswith("newer.json"))
            self.assertIn("contains 2 session trace(s); profiling the most recent: newer.json", proc.stderr)

            # A single-file target prints no notice and adds no directory field
            single = subprocess.run(
                [sys.executable, str(LAUNCHER_PATH), "profile", "-t", str(traces / "older.json"), "--format", "json"],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(single.returncode, 0, single.stderr)
            self.assertNotIn("session_files_in_directory", json.loads(single.stdout))
            self.assertNotIn("[INFO]", single.stderr)

    def test_not_found_errors_show_resolved_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
            prof = subprocess.run(
                [sys.executable, str(LAUNCHER_PATH), "profile", "-t", "nope/x.json", "--format", "json"],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=30,
            )
            err = json.loads(prof.stdout)["error"]
            self.assertIn("(resolved to /", err)
            self.assertTrue(err.endswith(os.path.join("nope", "x.json") + ")"), err)
            gate = subprocess.run(
                [sys.executable, str(LAUNCHER_PATH), "assert", "--traces", "nope/", "--max-cost-per-turn", "0.05", "--format", "json"],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(gate.returncode, 1)
            self.assertIn("--traces resolved to", json.loads(gate.stdout)["violations"][0])


if __name__ == "__main__":
    unittest.main()
