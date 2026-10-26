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

"""`agents-cli infra single-project`, then AQuA's own two Terraform roots.

Declared as an override in `agents-cli-extension.yaml`; `_acli.py` explains the
mechanics an override runs under. The built-in provisions the observed agent,
then, with `--apply-aqua`, this provisions AQuA beside it from
`terraform/bootstrap` and `terraform/examples/single-project`. Without the flag
AQuA is left out, as for an agent attached to an AQuA deployed elsewhere.

Run from AQuA's own checkout, only the second half happens, flag or not: the
observed agent is another project, so there is no built-in step to wrap, and
`--observed-deployment-name` names AQuA's resources.

Two roots because an enabled API is project-scoped and has to outlive any single
deployment: separate state is what stops a `terraform destroy` of one AQuA from
switching an API off under everything else in the project. Neither is made
redundant by the agent's own root, which enables only the APIs *it* needs and
never asks for `storage` or `bigqueryconnection`.

They are applied separately rather than sharing one variable set, because
Terraform rejects a value for a variable a root does not declare and `bootstrap`
declares `project_id` alone.

The agents AQuA watches are configured by `agents-cli aqua attach`, not by
these roots. It stores each agent's settings where the engine reads them,
checks and grants AQuA's access to its telemetry, and creates its update
trigger with gcloud. In the observed
agent's project, `agents-cli deploy --deploy-aqua` runs that attach once AQuA
is deployed; see `acli_deploy.py`.

An engine whose environment names its observed agent is replaced by this
root, since the environment it writes names none. Before the deployment root
is applied, that agent's settings are copied from the engine into an
attachment, which outlives the engine; see :func:`attach_environment_agent`.

`--destroy` takes AQuA down; see :func:`destroy_aqua`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from _acli import (
    APPLY_AQUA,
    AQUA_CLI_COMMAND,
    BOOTSTRAP_KEY,
    DEPLOYMENT_KEY,
    METADATA_FILE,
    ObservedOptions,
    build_state_path,
    die,
    echo,
    ensure_aqua_state_dir,
    extract_option_value,
    is_aqua_own_checkout,
    load_project_info,
    read_state_outputs,
    resolve_extension_root,
    resolve_observed_deployment_name,
    resolve_project,
    run,
    run_builtin,
    run_terraform,
    take_aqua_flag,
    take_flag,
    take_observed_options,
)

_AQUA_LEFT_OUT = (
    f"\nAQuA: its Terraform is left alone. Pass {APPLY_AQUA} to provision AQuA "
    "beside this agent, or attach the agent to an AQuA deployed elsewhere:\n"
    "    agents-cli aqua attach --aqua-resource "
    "projects/…/reasoningEngines/…"
)

BOOTSTRAP_ROOT = Path("terraform") / "bootstrap"
DEPLOYMENT_ROOT = Path("terraform") / "examples" / "single-project"

# The deployment root's output naming the engine it manages.
_ENGINE_OUTPUT = "engine_resource_name"

_ATTACH_IN_DEPLOY = (
    "\nAQuA: `agents-cli deploy --deploy-aqua` attaches this agent once AQuA "
    "is deployed, which also grants AQuA read access to its telemetry and "
    "creates its update trigger."
)

_ATTACH_BY_HAND = (
    "\nAQuA: once it is deployed, attach the agents it watches, which also "
    "grants AQuA read access to their telemetry and creates their update "
    "triggers:\n"
    "    agents-cli aqua attach --observed-agent-resource "
    "projects/…/reasoningEngines/…"
)


def require_sources(root: Path, *tf_roots: Path) -> None:
    """Verify that expected Terraform source directories exist.

    Args:
        root: Extension root path.
        *tf_roots: Relative paths to required Terraform directories.

    Raises:
        SystemExit: If any expected directory is missing.
    """
    for tf_root in tf_roots:
        if not (root / tf_root).is_dir():
            die(
                f"AQuA's sources at '{root}' have no '{tf_root}' directory.\n"
                "  Restore them with: agents-cli install"
            )


def destroy_aqua(
    argv: list[str], options: ObservedOptions, *, standalone: bool
) -> int:
    """Tear down what AQuA provisioned, and only that.

    The built-in has no destroy of its own, so this step does not wrap one: the
    observed agent's infrastructure is left untouched, and so is
    `terraform/bootstrap`, which enables project APIs that outlive any single
    deployment and are shared with everything else in the project. The
    update trigger of every attached agent goes first.

    Plan-by-default, like the command it overrides: `--destroy` reports what
    would be removed, `--destroy --apply` performs the teardown.

    Args:
        argv: Command-line arguments vector.
        options: Observed agent options from command line or environment.
        standalone: True if running from AQuA's own checkout.

    Returns:
        Process exit code (0 on success).
    """
    info = load_project_info()
    root = resolve_extension_root()
    state_root = ensure_aqua_state_dir(info)
    state = build_state_path(state_root, DEPLOYMENT_KEY)
    if not state.is_file():
        echo(
            f"\nAQuA: no Terraform state at {state}, so there is nothing to destroy."
        )
        return 0
    require_sources(root, DEPLOYMENT_ROOT)

    apply = "--apply" in argv
    # Only the two variables the root declares without a default. The rest
    # describe what to build, and a destroy builds nothing -- hence no ADK name.
    tf_vars = {
        "region": extract_option_value(argv, "--region") or info["region"],
        "observed_deployment_name": resolve_observed_deployment_name(
            options, info, standalone=standalone
        ),
    }
    project = resolve_project(argv)
    if project:
        tf_vars["project_id"] = project

    delete_attached_triggers(state_root, apply=apply)
    echo(
        f"\n{'Destroying' if apply else 'Planning the teardown of'} AQuA, from "
        f"{root / DEPLOYMENT_ROOT}\n"
        "  The observed agent's own infrastructure is untouched, and so are the "
        "project APIs AQuA enabled."
    )
    run_terraform(
        root / DEPLOYMENT_ROOT,
        state_key=DEPLOYMENT_KEY,
        state_root=state_root,
        apply=apply,
        destroy=True,
        tf_vars=tf_vars,
    )
    if apply:
        # The engine `agents-cli aqua` reads from this file is one of the
        # resources just destroyed, so the record of it now points at nothing.
        (state_root / METADATA_FILE).unlink(missing_ok=True)
    return 0


def read_destroyed_engine(state_root: Path) -> str | None:
    """Read the resource name of the engine `--destroy` is about to delete.

    Args:
        state_root: Directory storing relocated state files.

    Returns:
        The engine's resource name from the `deployment_metadata.json` the
        deploy recorded, or None when no deploy recorded one.
    """
    try:
        record = json.loads(
            (state_root / METADATA_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    engine = (
        str(record.get("remote_agent_runtime_id") or "")
        if isinstance(record, dict)
        else ""
    )
    return engine.rstrip("/") or None


def delete_attached_triggers(state_root: Path, *, apply: bool) -> None:
    """Remove every attached agent's update trigger, before the topic it publishes to.

    Left behind, a sink would export to a deleted topic. `agents-cli aqua
    delete-triggers` asks the AQuA being destroyed which agents are attached,
    and plans or removes each one's sink by its name, so it finds the triggers
    `attach` wired from any directory. With no engine recorded there is no
    AQuA to ask, and a failure is reported rather than blocking the teardown.

    Args:
        state_root: Directory storing relocated state files.
        apply: If True, removes the triggers; if False, plans their removal.
    """
    engine = read_destroyed_engine(state_root)
    if not engine:
        echo(
            "\nAQuA: no deployed engine is recorded here, so no attached "
            "agent's update trigger is looked for. Remove each one with "
            "`agents-cli aqua detach <agent key>` while AQuA still runs."
        )
        return
    command = [
        *AQUA_CLI_COMMAND,
        "delete-triggers",
        f"--aqua-resource={engine}",
    ]
    if apply:
        command.append("--apply")
    echo(
        f"\n{'Removing' if apply else 'Planning the removal of'} the update "
        f"triggers of the agents attached to {engine}"
    )
    if run(command).returncode != 0:
        echo(
            "\nAQuA: the attached agents' update triggers could not be "
            "removed, and a sink left behind exports to a deleted topic. "
            "Delete each sink named `<topic>-<agent key>-updates` in its "
            "agent's project by hand."
        )


def attach_environment_agent(
    state_root: Path, *, apply: bool, standalone: bool
) -> None:
    """Copy the agent the deployed engine's environment names into an attachment.

    The deployment root writes no agent into the engine's environment, so
    applying it replaces an engine whose environment names one. A first attach
    of that agent copies the environment's settings into an attachment, which
    is kept in the jobs bucket and outlives the engine, so the revision delay
    and the schedule survive the replacement. The telemetry settings are
    discovered again by the attach that `deploy --deploy-aqua` runs.

    Runs `agents-cli aqua list-agents` against the engine in AQuA's Terraform
    state, and `attach <agent> --no-discover` when its environment names an
    agent that is not attached. Nothing happens on a first apply, which has no
    engine, or when the engine names no agent. A failure is reported rather
    than raised: the apply goes on, and the settings are set again with
    `attach`.

    Args:
        state_root: Directory storing relocated state files.
        apply: If True, attaches the agent; if False, only says that it would.
        standalone: True if running from AQuA's own checkout, where no deploy
            attaches the agent with its update trigger afterwards.
    """
    engine = str(
        read_state_outputs(state_root, DEPLOYMENT_KEY).get(_ENGINE_OUTPUT) or ""
    )
    if not engine:
        return
    target = {"AGENT_ENGINE_RESOURCE_ID": engine}
    listing = run(
        [*AQUA_CLI_COMMAND, "list-agents"], env=target, capture_stdout=True
    )
    try:
        agents = (
            json.loads(listing.stdout or "") if listing.returncode == 0 else {}
        )
    except json.JSONDecodeError:
        agents = {}
    if not isinstance(agents, dict) or not agents:
        echo(
            f"\nAQuA: could not list the agents attached to {engine}, so no "
            "settings are copied from its environment. If it names its observed "
            "agent, set any setting you customized again with "
            "`agents-cli aqua attach` once AQuA is deployed."
        )
        return
    name = str(agents.get("environment_agent") or "")
    key = str(agents.get("environment_agent_key") or name)
    attached = {
        str(entry.get("agent_key"))
        for entry in agents.get("agents") or []
        if isinstance(entry, dict)
    }
    if not name or key in attached:
        return
    if not apply:
        echo(
            f"\nAQuA: {engine} names {name} in its environment. With --apply, "
            "its settings are first copied into an attachment, since the engine "
            "is replaced with one that names no agent."
        )
        return
    echo(
        f"\nAQuA: copying the settings of {name} from the environment of {engine} "
        "into an attachment, before the engine is replaced with one that names "
        "no agent"
    )
    attach = [*AQUA_CLI_COMMAND, "attach", name, "--no-discover"]
    if run(attach, env=target).returncode != 0:
        # The CLI has already said why.
        echo(
            f"\nAQuA: {name} was not attached, so its settings are not copied. "
            "Once AQuA is deployed, set any setting you customized with "
            "`agents-cli aqua attach`."
        )
    elif standalone:
        echo(
            f"\nAQuA: once AQuA is deployed, create the update trigger of {name} "
            "with:\n"
            f"    agents-cli aqua attach {name} --no-discover "
            "--observed-agent-resource projects/…/reasoningEngines/…"
        )


def main(argv: list[str]) -> int:
    argv, destroy = take_flag(argv, "--destroy")
    # `--destroy` takes only AQuA down, so the opt-in adds nothing to it.
    argv, apply_aqua = take_aqua_flag(argv, APPLY_AQUA)
    argv, options = take_observed_options(argv)
    standalone = is_aqua_own_checkout()
    if destroy:
        return destroy_aqua(argv, options, standalone=standalone)

    if standalone:
        echo(
            "\nAQuA's own checkout, so only AQuA is provisioned.\n"
            "  The observed agent's engine and the telemetry export AQuA reads "
            "are provisioned from its own project."
        )
    else:
        run_builtin(["infra", "single-project"], argv)
        if not apply_aqua:
            echo(_AQUA_LEFT_OUT)
            return 0

    info = load_project_info()
    deployment_name = resolve_observed_deployment_name(
        options, info, standalone=standalone
    )
    root = resolve_extension_root()
    require_sources(root, BOOTSTRAP_ROOT, DEPLOYMENT_ROOT)

    apply = "--apply" in argv
    project = resolve_project(argv)
    region = extract_option_value(argv, "--region") or info["region"]
    state_root = ensure_aqua_state_dir(info)

    bootstrap_vars = {}
    aqua_vars = {
        # AQuA's own default is us-central1, which would silently place it away
        # from the agent it observes.
        "region": region,
        # Names every resource AQuA creates, so one project can hold one AQuA
        # per observed deployment.
        "observed_deployment_name": deployment_name,
    }
    if project:
        bootstrap_vars["project_id"] = project
        aqua_vars["project_id"] = project
    echo(_ATTACH_BY_HAND if standalone else _ATTACH_IN_DEPLOY)

    echo(
        f"\nEnabling AQuA's project APIs and Cloud Build grant from {root / BOOTSTRAP_ROOT}"
    )
    run_terraform(
        root / BOOTSTRAP_ROOT,
        state_key=BOOTSTRAP_KEY,
        state_root=state_root,
        apply=apply,
        tf_vars=bootstrap_vars,
    )

    attach_environment_agent(state_root, apply=apply, standalone=standalone)
    echo(f"\nProvisioning AQuA from {root / DEPLOYMENT_ROOT}")
    run_terraform(
        root / DEPLOYMENT_ROOT,
        state_key=DEPLOYMENT_KEY,
        state_root=state_root,
        apply=apply,
        tf_vars=aqua_vars,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
