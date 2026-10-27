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

Once AQuA is deployed, `agents-cli aqua attach --yes` attaches the observed
agent to it, unless an attachment already records the agent's engine; see
:func:`attach_observed_agent`. The attach is what points AQuA at the agent,
grants it read access to the telemetry and creates the update trigger.

A further step follows: the dashboard in `acli_ui.py`. Pass `--skip-aqua-ui` to
skip only the dashboard. Without `--deploy-aqua`, both AQuA steps are left out,
as for an agent attached to an AQuA deployed elsewhere.

Run AQuA standalone, outside of the observed agent source tree.

Always to Agent Runtime, whatever the observed agent targets: AQuA runs its
investigations as durable long-running query jobs submitted against itself,
which is an Agent Runtime facility with no Cloud Run or GKE equivalent. AQuA's
own manifest already records that target; only the service name and the region
are overridden, because both describe the observed agent rather than AQuA.

`--update-only` is what keeps this an update of the engine AQuA's Terraform
created. Creating one here instead would produce an engine that can never work:
the module owns the `AQA_*` variables that name AQuA's own resources, so the
container raises on the first missing one while importing its config and the
platform reports, minutes later, only that it failed to start.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import acli_aqua
import acli_ui
from _acli import (
    AQUA_CLI_COMMAND,
    DEPLOY_AQUA,
    DEPLOYMENT_KEY,
    METADATA_FILE,
    OBSERVED_DEPLOYMENT_NAME,
    STATE_DIR_NAME,
    build_aqua_infra_command,
    build_aqua_service_name,
    die,
    echo,
    ensure_aqua_state_dir,
    ensure_state_dir_ignored,
    extract_option_value,
    find_project_root,
    is_aqua_own_checkout,
    load_project_info,
    read_state_outputs,
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

# Name of the Terraform output holding the AQuA engine resource name.
_ENGINE_OUTPUT = "engine_resource_name"


def _extract_engine_id(resource: object) -> str:
    """Extract the numeric engine ID from an engine resource name.

    Args:
        resource: `projects/P/locations/L/reasoningEngines/ID`, or a falsy
            value when nothing is recorded.

    Returns:
        The last path segment, or an empty string for no resource.
    """
    return str(resource or "").rstrip("/").rsplit("/", 1)[-1]


def list_stale_metadata_files(root: Path, state_root: Path) -> list[Path]:
    """List deploy records that reference an engine replaced by Terraform.

    Changing the engine's environment causes Terraform to replace the engine.
    The built-in `deploy` reads the target engine from
    `deployment_metadata.json` in `root` (the vendored tree), and
    `agents-cli aqua` reads the copy in `.aqua/`. If those files still name the
    deleted engine, `deploy` fails trying to update it rather than finding the
    new engine by display name.

    Args:
        root: Vendored AQuA tree where the deploy runs.
        state_root: Project `.aqua/` directory holding AQuA's Terraform state
            and the copied metadata record.

    Returns:
        Existing metadata files whose engine ID differs from the engine in
        AQuA's Terraform state, or an empty list if the state holds no engine.
    """
    current_id = _extract_engine_id(
        read_state_outputs(state_root, DEPLOYMENT_KEY).get(_ENGINE_OUTPUT)
    )
    if not current_id:
        return []
    stale = []
    for record in (root / METADATA_FILE, state_root / METADATA_FILE):
        # Compare only the engine ID because deployment metadata may record the
        # project number while Terraform state records the project ID.
        recorded_id = _extract_engine_id(
            acli_aqua.load_deployment_metadata(record).get(
                "remote_agent_runtime_id"
            )
        )
        if recorded_id and recorded_id != current_id:
            stale.append(record)
    return stale


def remove_stale_metadata_files(root: Path, state_root: Path) -> None:
    """Delete deploy records that reference an engine replaced by Terraform.

    Without a metadata file, the built-in `deploy` looks up the engine by
    display name and writes fresh metadata.

    Args:
        root: Vendored AQuA tree where the deploy runs.
        state_root: Project `.aqua/` directory.
    """
    for stale in list_stale_metadata_files(root, state_root):
        stale.unlink(missing_ok=True)
        echo(
            f"AQuA: removed {stale}, which names an engine AQuA's Terraform "
            "replaced; the deploy finds the current one by its name."
        )


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
        *AQUA_CLI_COMMAND,
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


def is_observed_engine_attached(engine_id: str) -> bool | None:
    """Check whether an attachment of the deployed AQuA records ``engine_id``.

    Args:
        engine_id: The observed engine's numeric id.

    Returns:
        True or False from AQuA's agent listing, or None when the listing
        cannot be read.
    """
    listing = run([*AQUA_CLI_COMMAND, "list-agents"], capture_stdout=True)
    if listing.returncode != 0:
        return None
    try:
        agents = json.loads(listing.stdout or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(agents, dict):
        return None
    return any(
        isinstance(entry, dict)
        and str(entry.get("observed_engine_id") or "") == engine_id
        for entry in agents.get("agents") or []
    )


def attach_observed_agent(project_root: Path, argv: list[str]) -> bool:
    """Attaches the observed agent to the AQuA just deployed, the first time.

    Runs `agents-cli aqua attach --yes --rediscover`, which discovers the
    agent's settings from its engine and the project, stores them, runs the
    grants and creates the update trigger, and publishes the source snapshot.
    `--rediscover` lets the discovered settings replace those `infra
    --apply-aqua` copied from the replaced engine's environment, which store
    defaults for the telemetry. Once an attachment records the agent's engine,
    its settings are changed with `attach` alone: attaching again on every
    deploy would replace a setting changed by hand with the discovered one.

    Args:
        project_root: Root directory of the observed agent project.
        argv: Command-line arguments passed to the deployment command.

    Returns:
        True when nothing is left to publish: the attach ran and published the
        source, or `--no-wait` leaves both to the commands it names.
    """
    retry = "    agents-cli aqua attach --rediscover"
    if "--no-wait" in argv:
        # The new revision does not exist yet, so neither an attach nor a
        # snapshot can name it. One notice names both, since the attach also
        # publishes.
        echo(
            "\nAQuA: --no-wait given, so the agent is not attached and no "
            "source snapshot is published.\n"
            "  Once the deploy finishes, attach the agent if this is its first "
            "deploy with AQuA, which also publishes its source:\n"
            f"{retry}\n"
            "  or else publish its source:\n"
            "    agents-cli aqua publish-source"
        )
        return True
    engine_id = _extract_engine_id(
        acli_aqua.load_deployment_metadata(project_root / METADATA_FILE).get(
            "remote_agent_runtime_id"
        )
    )
    if not engine_id:
        echo(
            f"\nAQuA: no engine in ./{METADATA_FILE}, so the agent is not "
            f"attached.\n  Once it is deployed, attach it with:\n{retry}"
        )
        return False
    attached = is_observed_engine_attached(engine_id)
    if attached:
        return False
    if attached is None:
        echo(
            "\nAQuA: could not list the agents attached to AQuA, so the agent "
            f"is not attached.\n  Attach it with:\n{retry}"
        )
        return False
    echo(f"\n🔗 Attaching the observed agent's engine {engine_id} to AQuA")
    # `acli_aqua.add_source_root` adds `--source-root`, since the project has
    # the deployment record read above, so the attach publishes the source.
    if (
        run([*AQUA_CLI_COMMAND, "attach", "--yes", "--rediscover"]).returncode
        != 0
    ):
        # The CLI has already said why.
        echo(f"\nAQuA: the agent was not attached. Attach it with:\n{retry}")
        return False
    return True


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

    info = load_project_info()
    # The engine and the dashboard are named from the deployment alone.
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
    if not read_only:
        remove_stale_metadata_files(
            root, Path(info["project_root"]) / STATE_DIR_NAME
        )
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

    if (
        not standalone
        and project_root
        and not attach_observed_agent(project_root, argv)
    ):
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
