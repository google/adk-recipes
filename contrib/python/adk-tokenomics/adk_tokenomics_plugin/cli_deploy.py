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

# ruff: noqa: S603
"""
ADK Token Control Tower - Native Cloud Run Deployment Module
Mirrors the official Google ADK 'adk deploy cloud_run' pattern with full parameter overrides and pass-through flags.
"""

import os
import re
import shutil
import subprocess
import sys

from adk_tokenomics_plugin.core.config import (
    validate_bq_identifier,
    validate_bq_project_id,
)

RESERVED_CLOUD_RUN_ENV_VARS = frozenset(
    {
        "PORT",
        "K_SERVICE",
        "K_REVISION",
        "K_CONFIGURATION",
    }
)

_SERVICE_NAME_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,47}[a-z0-9])?$")
_REGION_RE = re.compile(r"^[a-z]+-[a-z0-9-]+$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COMMA_ENV_SPLIT_RE = re.compile(r",(?=[A-Za-z_][A-Za-z0-9_]*=)")


def resolve_cloud_run_region(explicit_region: str | None = None) -> str:
    """Resolves a valid Cloud Run region, ignoring non-regional Vertex locations like 'global'."""
    if explicit_region and explicit_region.strip():
        return explicit_region.strip()
    cr_region = (os.environ.get("CLOUD_RUN_REGION") or "").strip()
    if cr_region and cr_region.lower() != "global":
        return cr_region
    for env_key in ("GOOGLE_CLOUD_LOCATION", "LOCATION"):
        loc = (os.environ.get(env_key) or "").strip()
        if loc and loc.lower() not in ("global", "us", "eu", "asia"):
            return loc
    return "us-central1"


def _parse_user_env_vars(env_vars: str) -> list[tuple[str, str]]:
    """
    Parses user --env_vars supporting both gcloud custom delimiter syntax ('^@^K1=V1@K2=V2')
    and key-aware comma splitting ('A=1,B=x,y' -> [('A', '1'), ('B', 'x,y')]).
    Raises ValueError on malformed entries or reserved Cloud Run keys.
    """
    if any(ch in env_vars for ch in ("\x00", "\r", "\n")):
        raise ValueError(
            "Environment variables in --env_vars cannot contain NUL or newline characters."
        )
    raw = env_vars.strip()
    if not raw:
        return []

    if raw.startswith("^") and len(raw) >= 3 and "^" in raw[1:]:
        end_caret = raw.index("^", 1)
        delim = raw[1:end_caret]
        if not delim:
            raise ValueError("Empty custom delimiter in --env_vars.")
        body = raw[end_caret + 1 :]
        items = [part for part in body.split(delim) if part != ""]
    else:
        items = [
            part.strip()
            for part in _COMMA_ENV_SPLIT_RE.split(raw)
            if part.strip()
        ]

    parsed: list[tuple[str, str]] = []
    for item in items:
        if "=" not in item:
            raise ValueError(
                f"Malformed environment variable assignment {item!r} (expected KEY=VALUE)."
            )
        key, val = item.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            raise ValueError(f"Invalid environment variable name {key!r}.")
        if key in RESERVED_CLOUD_RUN_ENV_VARS:
            raise ValueError(
                f"Reserved Cloud Run environment variable {key!r} cannot be set via --env_vars "
                f"(reserved names: {', '.join(sorted(RESERVED_CLOUD_RUN_ENV_VARS))})."
            )
        parsed.append((key, val))
    return parsed


_STATIC_GCLOUD_DELIMS = ("@", "|", ":", ";", "~", "#", "%", "+")


def _validate_env_no_control_chars(key: str, val: str) -> None:
    if any(ch in key or ch in val for ch in ("\x00", "\r", "\n")):
        raise ValueError(
            f"Environment variable {key!r} contains NUL or newline characters, which cannot be encoded for gcloud."
        )


def _format_gcloud_env_vars(pairs: list[tuple[str, str]]) -> str:
    """
    Formats KEY=VALUE pairs for gcloud --set-env-vars.
    Escapes commas using a collision-free gcloud custom delimiter ('^DELIM^K1=V1DELIMK2=V2'),
    dynamically synthesizing a multi-character delimiter if all single-character candidates appear in values.
    """
    if not pairs:
        return ""
    formatted: list[str] = []
    for k, v in pairs:
        _validate_env_no_control_chars(k, v)
        if not _ENV_KEY_RE.match(k):
            raise ValueError(f"Invalid environment variable name {k!r}.")
        formatted.append(f"{k}={v}")

    if not any("," in v for _, v in pairs):
        return ",".join(formatted)

    for candidate_delim in _STATIC_GCLOUD_DELIMS:
        if all(candidate_delim not in entry for entry in formatted):
            joined = candidate_delim.join(formatted)
            if joined.split(candidate_delim) == formatted:
                return f"^{candidate_delim}^" + joined

    idx = 1
    while True:
        candidate_delim = f"~#{idx}#~"
        if all(candidate_delim not in entry for entry in formatted):
            joined = candidate_delim.join(formatted)
            if joined.split(candidate_delim) == formatted:
                return f"^{candidate_delim}^" + joined
        idx += 1


def _resolve_deploy_source_dir(explicit_source: str | None = None) -> str:
    """Resolves the deployment source directory (preferring explicit --source or cwd, never bare site-packages)."""
    if explicit_source:
        return os.path.abspath(os.path.expanduser(explicit_source))
    cwd = os.path.abspath(os.getcwd())
    if os.path.isfile(os.path.join(cwd, "Dockerfile")):
        return cwd
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isfile(os.path.join(pkg_parent, "Dockerfile")):
        return pkg_parent
    return cwd


def deploy_cloud_run(
    project: str | None = None,
    region: str | None = None,
    service_name: str = "adk-token-tower",
    port: int = 8080,
    dataset: str = "bq_adk_ds",
    table: str = "adk_agent_events",
    view: str = "v_llm_response",
    allow_unauthenticated: bool = False,
    min_instances: int | None = None,
    max_instances: int | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    env_vars: str | None = None,
    source_dir: str | None = None,
    passthrough_args: list[str] | None = None,
) -> int:
    """Executes native Cloud Run deployment with parameter overrides and pass-through support."""
    # 1. Resolve & validate GCP Project, Region, Service Name & BigQuery Identifiers
    if not project:
        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get(
            "GCP_PROJECT"
        )
    if not project:
        print(
            "[ERROR] No GCP project specified. Pass --project <PROJECT_ID> or set GOOGLE_CLOUD_PROJECT.",
            file=sys.stderr,
        )
        return 1

    try:
        project = validate_bq_project_id(project)
        dataset = validate_bq_identifier(dataset, "dataset")
        table = validate_bq_identifier(table, "table")
        view = validate_bq_identifier(view, "view")
    except ValueError as ve:
        print(f"[ERROR] {ve}", file=sys.stderr)
        return 1

    region = resolve_cloud_run_region(region)
    if region.lower() == "global" or not _REGION_RE.match(region):
        print(
            f"[ERROR] Invalid Cloud Run region {region!r}. Specify a regional endpoint such as 'us-central1' "
            "(via --region or CLOUD_RUN_REGION).",
            file=sys.stderr,
        )
        return 1

    if not isinstance(service_name, str) or not _SERVICE_NAME_RE.match(
        service_name
    ):
        print(
            f"[ERROR] Invalid Cloud Run service name {service_name!r}. Must be lowercase RFC 1123 label (1-49 chars).",
            file=sys.stderr,
        )
        return 1

    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not (1 <= port <= 65535)
    ):
        print(
            f"[ERROR] Invalid port {port!r}. Must be an integer between 1 and 65535.",
            file=sys.stderr,
        )
        return 1

    # 2. Validate user environment variables (never allow reserved Cloud Run env vars like PORT)
    extra_env_pairs: list[tuple[str, str]] = []
    if env_vars:
        try:
            extra_env_pairs = _parse_user_env_vars(env_vars)
        except ValueError as ve:
            print(f"[ERROR] {ve}", file=sys.stderr)
            return 1

    # 3. Resolve & validate source directory contains a Dockerfile
    resolved_source_dir = _resolve_deploy_source_dir(source_dir)
    dockerfile_path = os.path.join(resolved_source_dir, "Dockerfile")
    if not os.path.isdir(resolved_source_dir) or not os.path.isfile(
        dockerfile_path
    ):
        print(
            f"[ERROR] Dockerfile not found in deployment source directory: {resolved_source_dir}. "
            "Run from the project root containing Dockerfile or pass --source <DIR>.",
            file=sys.stderr,
        )
        return 1

    # 4. Check if gcloud is installed
    gcloud_bin = shutil.which("gcloud")
    if not gcloud_bin:
        fallback_gcloud = os.path.expanduser("~/google-cloud-sdk/bin/gcloud")
        if os.path.exists(fallback_gcloud):
            gcloud_bin = fallback_gcloud
    if not gcloud_bin:
        print(
            "[ERROR] 'gcloud' CLI not found on PATH. Please install Google Cloud SDK: https://cloud.google.com/sdk/docs/install",
            file=sys.stderr,
        )
        return 1

    print("\n" + "=" * 85)
    print(" 🚀 ADK TOKEN CONTROL TOWER - CLOUD RUN DEPLOYMENT")
    print("=" * 85)
    print(f" • GCP Project   : {project}")
    print(f" • Cloud Region  : {region}")
    print(f" • Service Name  : {service_name}")
    print(f" • Source Dir    : {resolved_source_dir}")
    print(f" • Serving Port  : {port}")
    print(f" • BigQuery Host : {project}.{dataset}.{view}")
    if min_instances is not None:
        print(f" • Min Instances : {min_instances}")
    if max_instances is not None:
        print(f" • Max Instances : {max_instances}")
    if cpu:
        print(f" • CPU Allocation: {cpu}")
    if memory:
        print(f" • Memory Limit  : {memory}")
    if passthrough_args:
        print(f" • Passthrough   : {' '.join(passthrough_args)}")
    print("=" * 85 + "\n")

    # 5. Assemble Environment Variables (omit reserved PORT; --port sets Cloud Run's container port)
    user_env_keys = {k for k, _ in extra_env_pairs}
    env_pairs: list[tuple[str, str]] = [
        ("GOOGLE_CLOUD_PROJECT", project),
        ("BQ_ANALYTICS_DATASET_ID", dataset),
        ("BQ_ANALYTICS_TABLE_ID", table),
        ("BQ_ANALYTICS_VIEW_ID", view),
    ]
    if "TOKENOMICS_ALLOWED_HOSTS" not in user_env_keys:
        env_pairs.append(("TOKENOMICS_ALLOWED_HOSTS", "*.run.app"))
    env_pairs.extend(extra_env_pairs)

    # 6. Assemble gcloud command
    try:
        formatted_env_vars = _format_gcloud_env_vars(env_pairs)
    except ValueError as ve:
        print(f"[ERROR] {ve}", file=sys.stderr)
        return 1

    cmd = [
        gcloud_bin,
        "run",
        "deploy",
        service_name,
        "--source",
        resolved_source_dir,
        "--project",
        project,
        "--region",
        region,
        "--port",
        str(port),
        "--set-env-vars",
        formatted_env_vars,
    ]

    if allow_unauthenticated:
        cmd.append("--allow-unauthenticated")
    else:
        cmd.append("--no-allow-unauthenticated")

    if min_instances is not None:
        cmd.extend(["--min-instances", str(min_instances)])
    if max_instances is not None:
        cmd.extend(["--max-instances", str(max_instances)])
    if cpu:
        cmd.extend(["--cpu", str(cpu)])
    if memory:
        cmd.extend(["--memory", str(memory)])

    if passthrough_args:
        cmd.extend(passthrough_args)

    print(f"Executing: {' '.join(cmd)}\n")

    try:
        subprocess.run(cmd, check=True)
        print("\n🎉 Deployment completed successfully!")
        return 0
    except subprocess.CalledProcessError as e:
        print(
            f"\n🚨 Cloud Run deployment failed with exit code {e.returncode}",
            file=sys.stderr,
        )
        return e.returncode or 1
    except Exception as e:
        print(f"\n🚨 Unexpected error during deployment: {e}", file=sys.stderr)
        return 1
