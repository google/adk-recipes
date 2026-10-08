# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The AQuA dashboard, deployed onto the Cloud Run service Terraform created.

This module implements step 3 of the `agents-cli deploy` chain (see `acli_deploy.py`)::

    agents-cli deploy
      |
      +-- 1. the built-in       the observed agent
      +-- 2. acli_deploy.py     the AQuA engine that watches it
      +-- 3. this file          the dashboard that reads the engine

It runs last so the engine it reads is already up to date, not because it has to:
Terraform sets `AGENT_ENGINE_RESOURCE_ID`, and the dashboard calls the engine only
at runtime. Steps 2 and 3 run only with `--deploy-aqua`, and `--skip-aqua-ui`
omits this step. See `_acli.py` for override mechanics.

## The build context is assembled, not pointed at

`ui/` is an agents-cli `cloud_run` project, but one required tree still lives outside
it: `src/ambient_quality_shared`, the wire client `app.py` imports at module scope.
`--source .` from `ui/` cannot reach it, and building from repo root fails because
`.gcloudignore` excludes `/ui/`. :func:`build_ui_staging_dir_for_deployment` copies
(rather than symlinks, which extension vendoring skips) both trees into one directory
so `agents-cli deploy` can run there instead of a custom `gcloud run deploy`.

## Where the context is staged, and the entry that makes it safe

The build context is staged at `.aqua/ui-build/`, beside AQuA's Terraform state in
`.aqua/` (preserved across `agents-cli install`). It is rebuilt on each deploy and left
on disk to inspect failures.
This location is safe only because :func:`_acli.ensure_state_dir_ignored` enforces
ignoring `.aqua/` before the agent's deploy runs: without the ignore rule, 195 of the
231 files in the agent package came from this build directory. The wire client is
located outside `ui/`, requiring this staging directory (see `ui/README.md`).

## Why the steps run one after another

The steps run sequentially by choice: both deploys touch separate resources, run in
separate directories, and share no state (`deployment_metadata.json` cannot collide).
They stream terminal progress, and Cloud Run redraws build lines character-by-character
with carriage returns. Interleaving is unreadable, and capturing loses live progress.
Buffering is unjustified: this step takes 2-3 minutes against 5-15 minutes for each
Agent Runtime deploy, `--no-wait` exists, and skipping unchanged sources would save
those 2-3 minutes without concurrency.

TODO: b/559110247 - skip this step when the dashboard's sources are unchanged.

## Why this step refuses to create the service

Terraform owns the service and the deploy owns only the image. `ui.tf` configures
a dedicated service account, `AGENT_ENGINE_RESOURCE_ID`, ingress settings, and optional
IAP, ignoring image changes so applies do not overwrite deployed code. Creating the
service here would lack `AGENT_ENGINE_RESOURCE_ID`, fall back to the Compute Engine
default service account, default to `all` ingress (rejected under
`constraints/run.allowedIngress`), and break future `terraform apply` calls until
imported or deleted. Because Cloud Run rejects `agents-cli deploy --update-only`
(b/555632530), we enforce this check by querying Cloud Run before deploying.

## A missing service is skipped, not an error

`agents-cli infra single-project --apply` always provisions the service, so a
missing service indicates AQuA infrastructure was never applied or the service
was removed out of band. Unlike `--update-only`, this step skips deployment with
exit code zero rather than failing, keeping `agents-cli deploy` functional for
projects that have not yet provisioned AQuA. The printed notice identifies the
command that creates the service.
This is why :func:`find_ui_service_for_deployment` uses `list` instead of `describe`:
`describe` cannot distinguish a missing service from a failed lookup (such as
missing permissions, an invalid region, or a disabled Cloud Run API). Treating
lookup failures as missing services would falsely report broken deploys as successful.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from _acli import (
    UI_BUILD_DIR_NAME,
    build_aqua_infra_command,
    build_aqua_ui_service_name,
    die,
    echo,
    ensure_aqua_state_dir,
    extract_option_value,
    resolve_agents_cli_path,
    resolve_extension_root,
    resolve_gcloud_path,
    resolve_project,
    run,
)

# Source directories staged into the build context. `ui/` provides the manifest,
# the Dockerfile and `app.py` at its root; `src/ambient_quality_shared` is
# imported as a package.
PROJECT_TREE = Path("ui")
SHARED_TREE = Path("src") / "ambient_quality_shared"

# Excluded from staging: virtual environments, caches, and the front end's
# `node_modules` and `static_v2`, which the Dockerfile's node stage installs and
# builds for itself. `ui/.gcloudignore` excludes the same directories from gcloud
# uploads, while `_UNWANTED` bounds what is copied into the staging directory
# (where `node_modules` alone is ~200 MB). Both are required and are not
# interchangeable: `shutil.ignore_patterns` matches bare names at any depth,
# whereas `.gcloudignore` entries match paths.
_UNWANTED = shutil.ignore_patterns(
    ".venv",
    "__pycache__",
    "*.py[cod]",
    ".pytest_cache",
    "node_modules",
    "static_v2",
)


def find_ui_service_for_deployment(
    ui_service_name: str, *, project: str, region: str
) -> str | None:
    """Return the dashboard service name if it exists, or None.

    Queries `gcloud run services list` rather than `describe` to distinguish
    between a service that does not exist and an API/credential failure.

    Args:
        ui_service_name: Target Cloud Run service name.
        project: GCP project ID.
        region: GCP region string.

    Returns:
        Service name string if found, or None if absent.

    Raises:
        SystemExit: If the gcloud query fails with an error.
    """
    result = run(
        [
            resolve_gcloud_path(),
            "run",
            "services",
            "list",
            f"--project={project}",
            f"--region={region}",
            f"--filter=metadata.name={ui_service_name}",
            "--format=value(metadata.name)",
        ],
        capture=True,
    )
    if result.returncode != 0:
        reason = (result.stderr or "").strip().splitlines()
        die(
            f"could not ask Cloud Run whether '{ui_service_name}' exists in "
            f"{project}/{region}.\n"
            f"  gcloud said: {reason[-1] if reason else 'nothing'}"
        )
    return (result.stdout or "").strip() or None


def build_ui_staging_dir_for_deployment(root: Path, state_root: Path) -> Path:
    """Assemble the dashboard build context from scratch, dropping deleted files.

    Args:
        root: Extension root path.
        state_root: Scratch and state directory path.

    Returns:
        Path to the assembled staging directory.

    Raises:
        SystemExit: If required UI source directories are missing.
    """
    for tree in (PROJECT_TREE, SHARED_TREE):
        if not (root / tree).is_dir():
            die(
                f"AQuA's sources at '{root}' have no '{tree}' directory.\n"
                "  Restore them with: agents-cli install"
            )

    context = state_root / UI_BUILD_DIR_NAME
    shutil.rmtree(context, ignore_errors=True)
    shutil.copytree(root / PROJECT_TREE, context, ignore=_UNWANTED)
    shutil.copytree(
        root / SHARED_TREE, context / SHARED_TREE.name, ignore=_UNWANTED
    )
    return context


def deploy_ui_image(
    info: Mapping[str, Any], argv: Sequence[str], deployment_name: str
) -> int:
    """Deploy a container image to the dashboard service if it exists.

    Parses ``argv`` from `agents-cli deploy` for target options (`--project`,
    `--region`, `--no-wait`), deliberately omitting agent-specific arguments
    like `--image`.

    Args:
        info: `agents-cli info --json` for the project in the working directory.
        argv: Command-line arguments from `agents-cli deploy`.
        deployment_name: The observed agent's deployment, which `ui.tf` named
            the service after. Not from ``info``, which describes AQuA itself in
            a standalone deployment.

    Returns:
        Process exit code (0 on success or skip).

    Raises:
        SystemExit: If deployment subprocess exits non-zero.
    """
    ui_service_name = build_aqua_ui_service_name(deployment_name)
    region = extract_option_value(argv, "--region") or info["region"]
    project = resolve_project(argv)
    if not project:
        die(
            "cannot tell which project the dashboard is in.\n"
            "  Pass --project, or set one with `gcloud config set project`."
        )

    if not find_ui_service_for_deployment(
        ui_service_name, project=project, region=region
    ):
        echo(
            f"\nAQuA UI: no Cloud Run service '{ui_service_name}' in "
            f"{project}/{region}, so there is nothing to deploy onto.\n"
            f"  Provision it with: {build_aqua_infra_command()}"
        )
        return 0

    context = build_ui_staging_dir_for_deployment(
        resolve_extension_root(), ensure_aqua_state_dir(info)
    )
    args = [
        "deploy",
        "--service-name",
        ui_service_name,
        "--project",
        project,
        "--region",
        region,
    ]
    if "--no-wait" in argv:
        args.append("--no-wait")

    echo(
        f"\n📊 Deploying the AQuA dashboard from {context} as '{ui_service_name}'"
    )
    result = run([resolve_agents_cli_path(), *args], cwd=context)
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    return 0
