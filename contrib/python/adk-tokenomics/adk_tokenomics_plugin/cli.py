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

# ruff: noqa: S104, S105, S110
"""
ADK Token Control Tower - CLI Developer & FinOps Suite
Primary commands:
  • web     : Interactive visual dashboard & telemetry observatory (like 'adk web')
  • profile : Turn-by-turn context waterfall & automated optimization recommendations
  • assert  : Automated CI/CD financial budget and token regression test gate (exit code 0/1)
Phase 2 subcommands (hidden from top-level --help):
  • arena, finops, deploy
"""

import argparse
import glob
import json
import math
import os
import socket
import sys
from pathlib import Path

from adk_tokenomics_plugin.core.config import (
    resolve_env_bq_identifiers,
    validate_bq_identifier,
    validate_bq_project_id,
)


def _finite_nonneg_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected a finite non-negative float, got {value!r}."
        ) from exc
    if not math.isfinite(parsed) or parsed < 0.0:
        raise argparse.ArgumentTypeError(
            f"Expected a finite non-negative float (>= 0), got {value!r}."
        )
    return parsed


def _unit_interval_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected a finite ratio between 0.0 and 1.0, got {value!r}."
        ) from exc
    if not math.isfinite(parsed) or not (0.0 <= parsed <= 1.0):
        raise argparse.ArgumentTypeError(
            f"Expected a finite ratio in [0.0, 1.0], got {value!r}."
        )
    return parsed


def _nonneg_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected a non-negative integer, got {value!r}."
        ) from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(
            f"Expected a non-negative integer (>= 0), got {value!r}."
        )
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected a positive integer (>= 1), got {value!r}."
        ) from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(
            f"Expected a positive integer (>= 1), got {value!r}."
        )
    return parsed


def _port_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected an integer TCP port (1-65535), got {value!r}."
        ) from exc
    if not (1 <= parsed <= 65535):
        raise argparse.ArgumentTypeError(
            f"Expected a TCP port between 1 and 65535, got {value!r}."
        )
    return parsed


def _bq_project_arg(value: str) -> str:
    try:
        return validate_bq_project_id(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _bq_dataset_arg(value: str) -> str:
    try:
        return validate_bq_identifier(value, "dataset_id")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _bq_table_arg(value: str) -> str:
    try:
        return validate_bq_identifier(value, "table_id")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _bq_view_arg(value: str) -> str:
    try:
        return validate_bq_identifier(value, "view_id")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


_bq_identifier_arg = _bq_dataset_arg


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Returns True if the specified TCP host:port is already bound."""
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((probe_host, port)) == 0


def find_available_port(
    start_port: int = 8080, host: str = "127.0.0.1", max_tries: int = 20
) -> int:
    """Finds the first available TCP port starting from start_port."""
    for offset in range(max_tries):
        candidate = start_port + offset
        if not is_port_in_use(candidate, host):
            return candidate
    return start_port


def detect_default_gcp_project() -> str | None:
    """Attempt to auto-detect GCP project from environment, BigQuery client, or google.auth."""
    project = (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCP_PROJECT")
        or os.environ.get("BIGQUERY_PROJECT_ID")
    )
    if project and project.strip():
        return project.strip()
    try:
        from google.auth.exceptions import GoogleAuthError
    except ImportError:
        GoogleAuthError = ImportError  # type: ignore[misc,assignment]
    try:
        from google.cloud import bigquery

        client = bigquery.Client()
        if getattr(client, "project", None):
            return client.project
    except (ImportError, GoogleAuthError, OSError):
        pass
    try:
        import google.auth

        _, auth_project = google.auth.default()
        if auth_project:
            return auth_project
    except (ImportError, GoogleAuthError, OSError):
        pass
    return None


def load_env_file(env_path: str = ".env") -> None:
    """Loads environment variables from a .env file if present without overriding existing env vars."""
    if not os.path.isfile(env_path):
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=env_path, override=False)
        return
    except ImportError:
        pass
    try:
        with open(env_path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'").strip('"')
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


def load_env_from_trace_ancestors(traces_path: str) -> None:
    """Walks up from traces_path (up to 3 parent levels) to load any project .env file."""
    if not traces_path:
        return
    try:
        curr = Path(traces_path).expanduser().resolve()
        if curr.is_file():
            curr = curr.parent
        for _ in range(4):
            candidate = curr / ".env"
            if candidate.is_file():
                load_env_file(str(candidate))
            if curr.parent == curr:
                break
            curr = curr.parent
    except Exception:
        pass


def get_default_dataset() -> str:
    return resolve_env_bq_identifiers()[0]


def get_default_table() -> str:
    return resolve_env_bq_identifiers()[1]


def get_default_view() -> str:
    return resolve_env_bq_identifiers()[2]


def get_default_cloud_run_region() -> str:
    """Resolves default Cloud Run region, ignoring non-regional Vertex locations like 'global'."""
    from adk_tokenomics_plugin.cli_deploy import resolve_cloud_run_region

    return resolve_cloud_run_region(None)


def _first_subcommand_token(args_list: list[str]) -> str | None:
    for token in args_list:
        if token == "--":
            break
        if not token.startswith("-"):
            return token
    return None


def _wants_json_output(args_list: list[str]) -> bool:
    for idx, tok in enumerate(args_list):
        if tok == "--":
            break
        if tok == "--format=json":
            return True
        if (
            tok == "--format"
            and idx + 1 < len(args_list)
            and args_list[idx + 1] == "json"
        ):
            return True
    return False


def _parse_assert_cli_thresholds(args: argparse.Namespace):
    """Parses raw string thresholds for 'assert', returning (parsed_dict, violations_list)."""
    violations: list[str] = []
    parsed_cost: float | None = None
    parsed_cache: float | None = None
    parsed_tokens: int | None = None
    parsed_latency: int | None = None

    if args.max_cost_per_turn is not None:
        try:
            val = float(args.max_cost_per_turn)
            if not math.isfinite(val) or val < 0.0:
                raise ValueError("non-finite or negative")
            parsed_cost = val
        except ValueError:
            violations.append(
                f"❌ [Invalid Threshold]: --max-cost-per-turn must be a finite non-negative number, got {args.max_cost_per_turn!r}"
            )

    if args.min_cache_rate is not None:
        try:
            val = float(args.min_cache_rate)
            if not math.isfinite(val) or not (0.0 <= val <= 1.0):
                raise ValueError("out of [0.0, 1.0]")
            parsed_cache = val
        except ValueError:
            violations.append(
                f"❌ [Invalid Threshold]: --min-cache-rate must be a finite ratio in [0.0, 1.0], got {args.min_cache_rate!r}"
            )

    if args.max_tokens_per_turn is not None:
        try:
            ival = int(args.max_tokens_per_turn)
            if ival < 0:
                raise ValueError("negative")
            parsed_tokens = ival
        except ValueError:
            violations.append(
                f"❌ [Invalid Threshold]: --max-tokens-per-turn must be a finite non-negative integer, got {args.max_tokens_per_turn!r}"
            )

    if args.max_latency_ms is not None:
        try:
            ival = int(args.max_latency_ms)
            if ival < 0:
                raise ValueError("negative")
            parsed_latency = ival
        except ValueError:
            violations.append(
                f"❌ [Invalid Threshold]: --max-latency-ms must be a finite non-negative integer, got {args.max_latency_ms!r}"
            )

    return {
        "max_cost_per_turn": parsed_cost,
        "min_cache_rate": parsed_cache,
        "max_tokens_per_turn": parsed_tokens,
        "max_latency_ms": parsed_latency,
    }, violations


def main():
    load_env_file()
    raw_args = list(sys.argv[1:])
    passthrough_args: list[str] = []
    json_mode = _wants_json_output(raw_args)
    subcmd_hint = _first_subcommand_token(raw_args)

    class _CLIArgumentParser(argparse.ArgumentParser):
        def error(self, message: str):
            if json_mode:
                if subcmd_hint == "assert":
                    print(
                        json.dumps(
                            {
                                "passed": False,
                                "total_turns": 0,
                                "failed_artifacts": [],
                                "violations": [
                                    f"❌ [Invalid Threshold]: {message}"
                                ],
                                "error": message,
                            },
                            indent=2,
                        )
                    )
                else:
                    print(json.dumps({"error": message}, indent=2))
                self.exit(2)
            super().error(message)

    parser = _CLIArgumentParser(
        prog="adk-tokenomics",
        description="ADK Token Control Tower - Developer Context Profiling, Visual Web Observatory, and CI/CD Budget Assertions.",
    )

    if "--" in raw_args:
        subcmd = _first_subcommand_token(raw_args)
        if subcmd == "deploy":
            idx = raw_args.index("--")
            passthrough_args = raw_args[idx + 1 :]
            raw_args = raw_args[:idx]
        else:
            parser.error(
                "Passthrough arguments after '--' are only supported for 'deploy'."
            )

    subparsers = parser.add_subparsers(
        dest="subcommand",
        metavar="{web,profile,assert}",
        help="Available subcommands",
    )

    # 1. WEB command (Interactive Visual Dashboard)
    web_p = subparsers.add_parser(
        "web",
        aliases=["serve", "ui"],
        help="Launch interactive visual dashboard & telemetry observatory (like 'adk web')",
    )
    web_p.add_argument(
        "-p",
        "--project",
        type=_bq_project_arg,
        default=None,
        help="GCP Project ID for BigQuery (default: auto-detected or $GOOGLE_CLOUD_PROJECT)",
    )
    web_p.add_argument(
        "-d",
        "--dataset",
        type=_bq_dataset_arg,
        default=None,
        help="BigQuery Dataset ID (default: $BQ_ANALYTICS_DATASET_ID or bq_adk_ds)",
    )
    web_p.add_argument(
        "-v",
        "--view",
        type=_bq_view_arg,
        default=None,
        help="BigQuery View ID (default: $BQ_ANALYTICS_VIEW_ID or v_llm_response)",
    )
    web_p.add_argument(
        "--table",
        type=_bq_table_arg,
        default=None,
        help="BigQuery Table ID (default: $BQ_ANALYTICS_TABLE_ID or adk_agent_events)",
    )
    web_p.add_argument(
        "-t",
        "--traces",
        "--trace",
        "--traces-dir",
        dest="traces",
        help="Path to local trace JSON file or directory to inspect (e.g. artifacts/traces/)",
    )
    web_p.add_argument(
        "--port",
        type=_port_int,
        default=8080,
        help="Port to listen on (default: 8080)",
    )
    web_p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind to (default: 127.0.0.1)",
    )
    web_p.add_argument(
        "--allowed-host",
        action="append",
        dest="allowed_hosts",
        default=None,
        help=(
            "Repeatable external hostname, subdomain wildcard (e.g. *.run.app), or host:port pattern "
            "allowed in Host/Origin headers when binding --host 0.0.0.0 behind Docker port mapping "
            "or an HTTPS reverse proxy (also configurable via $TOKENOMICS_ALLOWED_HOSTS)"
        ),
    )
    web_p.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open web browser on startup",
    )

    # 2. PROFILE command
    profile_p = subparsers.add_parser(
        "profile",
        help="Turn-by-turn context waterfall and optimization recommendations",
    )
    profile_p.add_argument(
        "-t",
        "--traces",
        "--trace",
        "--traces-dir",
        dest="traces",
        help="Path to trace JSON file or directory (e.g. artifacts/traces/traces_xxx.json)",
    )
    profile_p.add_argument(
        "-s", "--session", help="Session ID to query directly from BigQuery"
    )
    profile_p.add_argument(
        "-p",
        "--project",
        type=_bq_project_arg,
        default=None,
        help="GCP Project ID for BigQuery (default: auto-detected or $GOOGLE_CLOUD_PROJECT)",
    )
    profile_p.add_argument(
        "-d",
        "--dataset",
        type=_bq_dataset_arg,
        default=None,
        help="BigQuery Dataset ID (default: $BQ_ANALYTICS_DATASET_ID or bq_adk_ds)",
    )
    profile_p.add_argument(
        "--table",
        type=_bq_table_arg,
        default=None,
        help="BigQuery Table ID (default: $BQ_ANALYTICS_TABLE_ID or adk_agent_events)",
    )
    profile_p.add_argument(
        "--prompts-file",
        default=None,
        help="Path to custom optimization prompts/rules JSON file",
    )
    profile_p.add_argument(
        "--min-cache-tokens",
        type=_nonneg_int,
        default=None,
        help="Custom token threshold for Context Caching recommendation (default: 1024)",
    )
    profile_p.add_argument(
        "--compaction-turns",
        type=_positive_int,
        default=None,
        help="Custom turn threshold for History Compaction recommendation (default: 4)",
    )
    profile_p.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format: text or json (default: text)",
    )

    # 3. ARENA command (Phase 2 — hidden from top-level --help)
    arena_p = subparsers.add_parser("arena")
    arena_p.add_argument(
        "-m",
        "--models",
        help="Comma-separated model names to compare (e.g. gemini-3.8-flash,gemini-4-argon,claude-3-5-haiku)",
    )
    arena_p.add_argument(
        "-r",
        "--results",
        help="Path to agents-cli grade results JSON file (e.g. artifacts/grade_results/results_xxx.json)",
    )
    arena_p.add_argument(
        "-d",
        "--dataset",
        help="Evaluation dataset name (e.g. travel_suite.json)",
    )
    arena_p.add_argument(
        "--avg-input",
        type=_nonneg_int,
        default=2000,
        help="Average prompt tokens per turn (default: 2000)",
    )
    arena_p.add_argument(
        "--avg-output",
        type=_nonneg_int,
        default=400,
        help="Average output tokens per turn (default: 400)",
    )
    arena_p.add_argument(
        "--cache-ratio",
        type=_unit_interval_float,
        default=0.75,
        help="Estimated context cache hit ratio 0.0-1.0 (default: 0.75)",
    )

    # 4. ASSERT command
    assert_p = subparsers.add_parser(
        "assert",
        help="Automated CI/CD financial budget and token regression test gate",
    )
    assert_p.add_argument(
        "-t",
        "--traces",
        "--trace",
        "--traces-dir",
        dest="traces",
        help="Path to trace JSON file or directory (default: artifacts/traces/)",
    )
    assert_p.add_argument(
        "--max-cost-per-turn",
        default=None,
        help="Maximum allowed dollar cost per turn (e.g. 0.002)",
    )
    assert_p.add_argument(
        "--min-cache-rate",
        default=None,
        help="Minimum required context cache hit rate 0.0-1.0 (e.g. 0.60)",
    )
    assert_p.add_argument(
        "--max-tokens-per-turn",
        default=None,
        help="Maximum allowed total tokens per turn (e.g. 8192)",
    )
    assert_p.add_argument(
        "--max-latency-ms",
        default=None,
        help="Maximum allowed average turn latency in milliseconds (e.g. 2000)",
    )
    assert_p.add_argument(
        "--allow-empty",
        action="store_true",
        help="Exit 0 instead of 1 when no trace files are found",
    )
    assert_p.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format: text or json (default: text)",
    )

    # 5. FINOPS command (Phase 2 — hidden from top-level --help)
    finops_p = subparsers.add_parser("finops")
    finops_p.add_argument(
        "-p",
        "--project",
        type=_bq_project_arg,
        default=None,
        help="GCP Project ID (default: auto-detected or $GOOGLE_CLOUD_PROJECT)",
    )
    finops_p.add_argument(
        "-d",
        "--dataset",
        type=_bq_dataset_arg,
        default=None,
        help="BigQuery Dataset ID (default: $BQ_ANALYTICS_DATASET_ID or bq_adk_ds)",
    )
    finops_p.add_argument(
        "-v",
        "--view",
        type=_bq_view_arg,
        default=None,
        help="BigQuery View ID (default: $BQ_ANALYTICS_VIEW_ID or v_llm_response)",
    )
    finops_p.add_argument(
        "--table",
        type=_bq_table_arg,
        default=None,
        help="BigQuery Base Table ID (default: $BQ_ANALYTICS_TABLE_ID or adk_agent_events)",
    )
    finops_p.add_argument(
        "--days",
        type=_positive_int,
        default=7,
        help="Reporting window in days (default: 7)",
    )
    finops_p.add_argument(
        "--limit",
        type=_positive_int,
        default=5,
        help="Number of top costliest agents to display",
    )
    finops_p.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format: text or json (default: text)",
    )

    # 6. DEPLOY command (Phase 2 — hidden from top-level --help)
    deploy_p = subparsers.add_parser("deploy")
    deploy_sub = deploy_p.add_subparsers(
        dest="target", help="Deployment target (e.g. cloud_run)"
    )

    cr_p = deploy_sub.add_parser(
        "cloud_run",
        aliases=["cloud-run"],
        help="Deploy Token Control Tower to Google Cloud Run",
    )
    cr_p.add_argument(
        "--project",
        type=_bq_project_arg,
        default=None,
        help="Google Cloud project ID",
    )
    cr_p.add_argument(
        "--region",
        default=get_default_cloud_run_region(),
        help="Google Cloud Run region (default: $CLOUD_RUN_REGION or us-central1)",
    )
    cr_p.add_argument(
        "--service_name",
        default="adk-token-tower",
        help="Cloud Run service name (default: adk-token-tower)",
    )
    cr_p.add_argument(
        "--source",
        default=None,
        help="Source directory containing Dockerfile (default: current working directory)",
    )
    cr_p.add_argument(
        "--port",
        type=_port_int,
        default=8080,
        help="Serving port (default: 8080)",
    )
    cr_p.add_argument(
        "--dataset",
        type=_bq_dataset_arg,
        default=None,
        help="BigQuery Dataset ID (default: $BQ_ANALYTICS_DATASET_ID or bq_adk_ds)",
    )
    cr_p.add_argument(
        "--table",
        type=_bq_table_arg,
        default=None,
        help="BigQuery Table ID (default: $BQ_ANALYTICS_TABLE_ID or adk_agent_events)",
    )
    cr_p.add_argument(
        "--view",
        type=_bq_view_arg,
        default=None,
        help="BigQuery View ID (default: $BQ_ANALYTICS_VIEW_ID or v_llm_response)",
    )
    cr_p.add_argument(
        "--allow_unauthenticated",
        action="store_true",
        default=False,
        help="Allow unauthenticated access (default: False)",
    )
    cr_p.add_argument(
        "--no_allow_unauthenticated",
        dest="allow_unauthenticated",
        action="store_false",
        help="Require IAM authentication (default)",
    )
    cr_p.add_argument(
        "--min_instances",
        type=_nonneg_int,
        default=None,
        help="Minimum container instances",
    )
    cr_p.add_argument(
        "--max_instances",
        type=_nonneg_int,
        default=None,
        help="Maximum container instances",
    )
    cr_p.add_argument("--cpu", default=None, help="CPU allocation (e.g. 1, 2)")
    cr_p.add_argument(
        "--memory", default=None, help="Memory limit (e.g. 512Mi, 1Gi, 2Gi)"
    )
    cr_p.add_argument(
        "--env_vars",
        default=None,
        help="Additional environment variables (e.g. KEY1=VAL1,KEY2=VAL2 or ^@^K1=V1@K2=V2)",
    )

    args = parser.parse_args(raw_args)

    if args.subcommand in ["web", "serve", "ui"]:
        from adk_tokenomics_plugin.cli_web import (
            run_web_server,
            validate_allowed_hosts,
        )

        if args.traces:
            load_env_from_trace_ancestors(args.traces)
        project_id = (
            args.project
            if args.project is not None
            else (detect_default_gcp_project() or "")
        )
        dataset_id = args.dataset or get_default_dataset()
        table_id = args.table or get_default_table()
        view_id = args.view or get_default_view()

        try:
            if project_id:
                project_id = validate_bq_project_id(project_id)
            dataset_id = validate_bq_identifier(dataset_id, "dataset_id")
            table_id = validate_bq_identifier(table_id, "table_id")
            view_id = validate_bq_identifier(view_id, "view_id")
            if args.allowed_hosts is not None:
                validate_allowed_hosts(args.allowed_hosts)
        except ValueError as ve:
            print(f"[ERROR] {ve}", file=sys.stderr)
            sys.exit(1)

        # In container / 0.0.0.0 mode, bind strictly to the requested port ($PORT) instead of drifting
        if args.host in ("127.0.0.1", "localhost") and not os.environ.get(
            "K_SERVICE"
        ):
            resolved_port = find_available_port(args.port, args.host)
            if resolved_port != args.port:
                print(
                    f"[INFO] Port {args.port} is in use; using available port {resolved_port}."
                )
        else:
            resolved_port = args.port

        run_web_server(
            port=resolved_port,
            host=args.host,
            project_id=project_id,
            dataset_id=dataset_id,
            table_id=table_id,
            view_id=view_id,
            traces_path=args.traces,
            open_browser=not args.no_browser,
            allowed_hosts=args.allowed_hosts,
        )

    elif args.subcommand == "profile":
        from adk_tokenomics_plugin.cli_profiler import (
            profile_from_bigquery,
            profile_from_trace_file,
        )
        from adk_tokenomics_plugin.core.config import OptimizationRulesConfig

        rules_cfg = OptimizationRulesConfig()
        cli_rule_overrides = {}
        if args.min_cache_tokens is not None:
            rules_cfg.min_cache_tokens = args.min_cache_tokens
            cli_rule_overrides["min_cache_tokens"] = args.min_cache_tokens
        if args.compaction_turns is not None:
            rules_cfg.compaction_turn_threshold = args.compaction_turns
            cli_rule_overrides["compaction_turn_threshold"] = (
                args.compaction_turns
            )

        if args.traces:
            res = profile_from_trace_file(
                args.traces,
                prompts_config_path=args.prompts_file,
                rules_config=rules_cfg,
                output_format=args.format,
                cli_rule_overrides=cli_rule_overrides,
            )
            sys.exit(0 if res is not None else 1)
        elif args.session:
            project_id = args.project or detect_default_gcp_project()
            dataset_id = args.dataset or get_default_dataset()
            table_id = args.table or get_default_table()
            res = profile_from_bigquery(
                session_id=args.session,
                project_id=project_id,
                dataset_id=dataset_id,
                table_id=table_id,
                prompts_config_path=args.prompts_file,
                rules_config=rules_cfg,
                output_format=args.format,
                cli_rule_overrides=cli_rule_overrides,
            )
            sys.exit(0 if res is not None else 1)
        else:
            traces = (
                glob.glob("artifacts/traces/*.json")
                or glob.glob("agent-nexus/artifacts/traces/*.json")
                or glob.glob(".adk/eval_history/*.evalset_result.json")
            )
            if traces:
                newest_trace = max(traces, key=os.path.getmtime)
                if args.format != "json":
                    print(f"[INFO] Auto-detected trace file: {newest_trace}")
                res = profile_from_trace_file(
                    newest_trace,
                    prompts_config_path=args.prompts_file,
                    rules_config=rules_cfg,
                    output_format=args.format,
                    cli_rule_overrides=cli_rule_overrides,
                )
                sys.exit(0 if res is not None else 1)
            else:
                if args.format == "json":
                    print(
                        json.dumps(
                            {
                                "error": "No trace file or --session specified, and no trace artifacts found in artifacts/traces/."
                            }
                        )
                    )
                else:
                    profile_p.print_help()
                sys.exit(1)

    elif args.subcommand == "arena":
        from adk_tokenomics_plugin.cli_arena import run_arena_cli

        run_arena_cli(
            models_str=args.models,
            results_file=args.results,
            dataset=args.dataset,
            avg_input=args.avg_input,
            avg_output=args.avg_output,
            cache_ratio=args.cache_ratio,
        )

    elif args.subcommand == "assert":
        from adk_tokenomics_plugin.testing.assertions import run_cli_assertions

        parsed_thresholds, threshold_violations = _parse_assert_cli_thresholds(
            args
        )
        if threshold_violations:
            if args.format == "json":
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
                    print(f"[ERROR] {v}", file=sys.stderr)
            sys.exit(1)

        exit_code = run_cli_assertions(
            traces_path=args.traces,
            max_cost_per_turn=parsed_thresholds["max_cost_per_turn"],
            min_cache_rate=parsed_thresholds["min_cache_rate"],
            max_tokens_per_turn=parsed_thresholds["max_tokens_per_turn"],
            max_latency_ms=parsed_thresholds["max_latency_ms"],
            allow_empty=args.allow_empty,
            output_format=args.format,
        )
        sys.exit(exit_code)

    elif args.subcommand == "finops":
        from adk_tokenomics_plugin.cli_finops import run_finops_summary_cli

        project_id = args.project or detect_default_gcp_project()
        dataset_id = args.dataset or get_default_dataset()
        view_id = args.view or get_default_view()
        table_id = args.table or get_default_table()
        exit_code = run_finops_summary_cli(
            project_id=project_id,
            dataset_id=dataset_id,
            view_id=view_id,
            table_id=table_id,
            days=args.days,
            limit_agents=args.limit,
            output_format=args.format,
        )
        sys.exit(exit_code)

    elif args.subcommand == "deploy":
        if args.target in ["cloud_run", "cloud-run"]:
            from adk_tokenomics_plugin.cli_deploy import deploy_cloud_run

            exit_code = deploy_cloud_run(
                project=args.project
                or os.environ.get("GOOGLE_CLOUD_PROJECT")
                or None,
                region=args.region,
                service_name=args.service_name,
                port=args.port,
                dataset=args.dataset or get_default_dataset(),
                table=args.table or get_default_table(),
                view=args.view or get_default_view(),
                allow_unauthenticated=args.allow_unauthenticated,
                min_instances=args.min_instances,
                max_instances=args.max_instances,
                cpu=args.cpu,
                memory=args.memory,
                env_vars=args.env_vars,
                source_dir=args.source,
                passthrough_args=passthrough_args,
            )
            sys.exit(exit_code)
        else:
            deploy_p.print_help()
            sys.exit(2)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
