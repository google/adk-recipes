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

"""`agents-cli deploy`, then the AQuA observer beside the agent it watches.

Declared as an override in `agents-cli-extension.yaml`; `_acli.py` explains the
mechanics an override runs under. The built-in deploys the observed agent, then,
with `--deploy-aqua`, this deploys AQuA by re-invoking the *same* built-in with the vendored AQuA tree
as the working directory -- AQuA is an ordinary agents-cli project, carrying its
own manifest and Dockerfile, so nothing AQuA-specific is needed to deploy it.

A third step follows: the dashboard in `acli_ui.py`. Pass `--skip-aqua-ui` to
skip only the dashboard. Without `--deploy-aqua`, both AQuA steps are left out,
as for an agent attached to an AQuA deployed elsewhere. In vendored layouts,
deployments that include AQuA install its coding-agent skill into the project
before deploying the observer; see :func:`_acli.install_skill_best_effort`.

Run AQuA standalone, outside of the observed agent source tree.

Always to Agent Runtime, whatever the observed agent targets: AQuA runs its
investigations as durable long-running query jobs submitted against itself,
which is an Agent Runtime facility with no Cloud Run or GKE equivalent. AQuA's
own manifest already records that target; only the service name and the region
are overridden, because both describe the observed agent rather than AQuA.

`--update-only` is what keeps this an update of the engine AQuA's Terraform
created. Creating one here instead would produce an engine that can never work:
the module owns every `AQA_*` variable, so the container raises on the first
missing one while importing its config and the platform reports, minutes later,
only that it failed to start.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import acli_aqua
import acli_ui
from _acli import (
    DEPLOY_AQUA,
    METADATA_FILE,
    OBSERVED_DEPLOYMENT_NAME,
    build_aqua_infra_command,
    build_aqua_service_name,
    die,
    echo,
    ensure_aqua_state_dir,
    ensure_state_dir_ignored,
    extract_option_value,
    find_project_root,
    install_skill_best_effort,
    is_aqua_own_checkout,
    load_project_info,
    resolve_agents_cli_path,
    resolve_extension_root,
    resolve_observed_deployment_name,
    resolve_project,
    run,
    run_builtin,
    take_aqua_flag,
    take_flag,
    take_observed_options,
)

_AQUA_LEFT_OUT = (
    f"\nAQuA: not deployed. Pass {DEPLOY_AQUA} to deploy AQuA and its dashboard "
    "beside this agent."
)

# Built-in flags that make `deploy` report rather than deploy. The AQuA step has
# to sit them out too, or `deploy --status` would deploy something.
_READ_ONLY_FLAGS = ("--dry-run", "--status", "--list")

# `agents-cli aqua publish-source`, run as the manifest's `aqua` command runs
# it: AQuA's CLI needs third-party imports this stdlib-only wrapper does not
# have. Invoked by path rather than through `agents-cli aqua`, because this
# wrapper runs with overrides disabled. Keep the `--with` list in step with the
# `aqua` command in `agents-cli-extension.yaml`.
_AQUA_CLI_COMMAND = (
    "uv",
    "run",
    "--no-project",
    "--with",
    "click",
    "--with",
    "google-auth",
    "--with",
    "httpx",
    "--with",
    "pathspec",
    "--with",
    "pyyaml",
    "--with",
    "requests",
    "python",
    str(Path(__file__).resolve().parent / "acli_aqua.py"),
)

# Enables conversation content capture on AQuA's `generate_content` spans for
# trace replay. Passed during deployment because `agents-cli` sets NO_CONTENT in
# `deploymentSpec.env`, which overrides the container image ENV.
#
# `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` deliberately stays at `false` as
# configured by `agents-cli`. Ingestion strips `call_llm` payloads, and enabling
# it increases span attribute sizes from ~500 to ~60,000 characters.
_CONTENT_CAPTURE_ENV = {
    "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "SPAN_ONLY",
}


def merge_update_env_vars(argv: list[str]) -> str:
    """Builds the `--update-env-vars` string for AQuA deployment.

    Args:
        argv: Command-line arguments from `agents-cli deploy`. Any
            operator-supplied `--update-env-vars` values take precedence over
            AQuA's default environment variables.

    Returns:
        Comma-separated `KEY=VALUE` pairs for `--update-env-vars`.
    """
    merged = dict(_CONTENT_CAPTURE_ENV)
    for pair in (extract_option_value(argv, "--update-env-vars") or "").split(
        ","
    ):
        key, separator, value = pair.partition("=")
        if separator and key.strip():
            merged[key.strip()] = value
    return ",".join(f"{key}={value}" for key, value in merged.items())


def publish_observed_source(project_root: Path, argv: list[str]) -> None:
    """Publishes the deployed observed source to AQuA for root-cause analysis.

    Runs `agents-cli aqua publish-source`, which uploads the snapshot through
    AQuA's API, so it reaches the AQuA deployed beside this agent and one it is
    attached to alike. Without either there is nothing to publish to, which is
    reported as a note. Failures log a warning without failing the deployment.

    Args:
        project_root: Root directory of the observed agent project.
        argv: Command-line arguments passed to the deployment command.
    """
    command = [
        *_AQUA_CLI_COMMAND,
        "publish-source",
        f"--source-root={project_root}",
    ]
    if "--no-wait" in argv:
        # Asynchronous deployments have not created the new revision yet; publishing
        # now would misattribute the snapshot to the prior revision.
        echo(
            "\nAQuA: --no-wait given, so no source snapshot is published.\n"
            "  Once the deploy finishes, publish it with:\n"
            "    agents-cli aqua publish-source"
        )
        return
    if not acli_aqua.read_recorded_engine_id() and not os.environ.get(
        "AGENT_ENGINE_RESOURCE_ID"
    ):
        echo(
            "\nAQuA: no AQuA is deployed or attached here, so no source snapshot "
            "is published. `agents-cli aqua attach` publishes one."
        )
        return

    echo("\n📦 Publishing the observed agent's source for root-cause analysis")
    if run(command).returncode != 0:
        # The CLI has already said why.
        echo(
            "AQuA: could not publish the source snapshot; root-cause analysis "
            "will have no code for this revision. Retry with: "
            "agents-cli aqua publish-source"
        )


def main(argv: list[str]) -> int:
    argv, deploy_aqua = take_aqua_flag(argv, DEPLOY_AQUA)
    argv, skip_aqua_ui = take_flag(argv, "--skip-aqua-ui")
    argv, options = take_observed_options(argv)
    standalone = is_aqua_own_checkout()

    # Assert the ignore entry before the built-in packages the project root,
    # even when skipping AQuA, so an unignored `.aqua/` is never uploaded.
    project_root = find_project_root()
    if project_root:
        ensure_state_dir_ignored(project_root)

    read_only = [flag for flag in _READ_ONLY_FLAGS if flag in argv]

    if standalone:
        if extract_option_value(argv, "--service-name"):
            die(
                "--service-name is not accepted in the standalone mode: AQuA's"
                f" engine is `<deployment>-aqua`, from {OBSERVED_DEPLOYMENT_NAME}."
            )
    else:
        run_builtin(["deploy"], argv)

        # Read-only operations do not deploy changes. Snapshot the observed agent
        # for all actual deployments, after AQuA's own when it is deployed too,
        # so that the engine answering the upload runs the current code.
        if not deploy_aqua:
            if not read_only:
                if project_root:
                    _publish_best_effort(project_root, argv)
                echo(_AQUA_LEFT_OUT)
            return 0

        if read_only:
            echo(
                f"\nAQuA: {read_only[0]} given, so the AQuA observer is left alone."
            )
            return 0

        if project_root:
            install_skill_best_effort(project_root)

    info = load_project_info()
    # The engine and the dashboard are named from the deployment alone, so an
    # AQuA provisioned with no observed agent deploys the same way.
    deployment_name = resolve_observed_deployment_name(
        options, info, standalone=standalone
    )
    service_name = build_aqua_service_name(deployment_name)
    root = resolve_extension_root()
    if not (root / "Dockerfile").is_file():
        die(
            f"AQuA's sources at '{root}' have no Dockerfile to build.\n"
            "  Restore them with: agents-cli install"
        )

    args = ["deploy", "--update-only", "--service-name", service_name]
    project = resolve_project(argv)
    if project:
        args.extend(["--project", project])
    args.extend(
        ["--region", extract_option_value(argv, "--region") or info["region"]]
    )
    args.extend(["--update-env-vars", merge_update_env_vars(argv)])
    if "--no-wait" in argv:
        args.append("--no-wait")
    # Standalone only, where the AQuA engine is what there is to report on.
    args.extend(read_only)

    echo(f"\n🔬 Deploying AQuA from {root} as '{service_name}'")
    result = run([resolve_agents_cli_path(), *args], cwd=root)
    if result.returncode != 0:
        # `--update-only` fails on an absent engine without saying what makes one.
        echo(
            f"\nAQuA: deploying '{service_name}' failed. If it reports no such "
            "engine, Terraform is what creates it:\n"
            f"  {build_aqua_infra_command()}"
        )
        raise SystemExit(result.returncode)
    if read_only:
        return 0

    # `deploy` writes the metadata into its working directory, which here is the
    # vendored tree that the next `install` replaces. `agents-cli aqua` reads the
    # engine id out of the copy, so the copy is the one that has to survive.
    written = root / METADATA_FILE
    if written.is_file():
        shutil.copy2(written, ensure_aqua_state_dir(info) / METADATA_FILE)

    if not standalone and project_root:
        _publish_best_effort(project_root, argv)

    if skip_aqua_ui:
        return 0
    return acli_ui.deploy_ui_image(info, argv, deployment_name)


def _publish_best_effort(project_root: Path, argv: list[str]) -> None:
    """Publishes the observed source, reporting rather than raising a failure.

    Args:
        project_root: Root directory of the observed agent project.
        argv: Command-line arguments passed to the deployment command.
    """
    try:
        publish_observed_source(project_root, argv)
    except (OSError, RuntimeError, SystemExit) as error:
        echo(f"AQuA: source snapshot skipped ({error}).")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
