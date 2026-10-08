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
observed agent is another project, so there is no built-in step to wrap and `--observed-*`
names the agent the roots are applied for. Without `--observed-agent-name`,
AQuA is provisioned with no observed agent, and one is attached later with
`agents-cli aqua attach`.

Two roots because an enabled API is project-scoped and has to outlive any single
deployment: separate state is what stops a `terraform destroy` of one AQuA from
switching an API off under everything else in the project. Neither is made
redundant by the agent's own root, which enables only the APIs *it* needs and
never asks for `storage` or `bigqueryconnection`.

They are applied separately rather than sharing one variable set, because
Terraform rejects a value for a variable a root does not declare and `bootstrap`
declares `project_id` alone.

AQuA provisioned with no observed agent leaves out the agent's triggers -- the
scheduled tick and the audit sink on its updates. They are a third root,
`terraform/examples/attach`, applied once per attached agent by `agents-cli aqua
attach --apply`, since its values are the ones `attach` stored, which only the
deployed engine can say.

`--destroy` takes AQuA back down again; see :func:`destroy_aqua`.

In vendored layouts, provisioning infrastructure also installs AQuA's
coding-agent skill into the observed project; see
:func:`_acli.install_skill_best_effort`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from _acli import (
    APPLY_AQUA,
    ATTACH_INSTANCE_OUTPUT,
    ATTACH_ROOT,
    BOOTSTRAP_KEY,
    DEPLOYMENT_KEY,
    METADATA_FILE,
    ObservedOptions,
    build_attach_key,
    build_state_path,
    die,
    echo,
    ensure_aqua_state_dir,
    extract_option_value,
    install_skill_best_effort,
    is_aqua_own_checkout,
    list_attached_agents,
    load_infra_outputs,
    load_project_info,
    read_state_outputs,
    resolve_extension_root,
    resolve_observed_deployment_name,
    resolve_observed_identity,
    resolve_observed_telemetry_table,
    resolve_project,
    run_builtin,
    run_terraform,
    take_aqua_flag,
    take_flag,
    take_observed_options,
)

_AQUA_LEFT_OUT = (
    f"\nAQuA: its Terraform is left alone. Pass {APPLY_AQUA} to provision AQuA "
    "beside this agent, or attach the agent to an AQuA deployed elsewhere:\n"
    "    agents-cli aqua attach --apply --aqua-resource "
    "projects/…/reasoningEngines/…"
)

BOOTSTRAP_ROOT = Path("terraform") / "bootstrap"
DEPLOYMENT_ROOT = Path("terraform") / "examples" / "single-project"

_NO_OBSERVED_AGENT = (
    "\nAQuA: no --observed-agent-name, so AQuA is provisioned without an "
    "observed agent.\n"
    "  Attach one, which also grants AQuA read access to its telemetry and "
    "applies its scheduled investigation and update trigger:\n"
    "    agents-cli aqua attach --apply --observed-agent-resource "
    "projects/…/reasoningEngines/…\n"
    "  Remove its triggers, before AQuA is destroyed, with `agents-cli aqua "
    "detach --apply` from the same directory."
)

# Where the agent's own root publishes the engine it created. AQuA matches the
# composed resource name against `protoPayload.resourceName` in the audit log,
# which carries the project *id*, so the id is what gets composed in below.
_AGENT_ENGINE_OUTPUT = "agent_runtime_resource_name"


def read_observed_engine_resource_name(
    project: str | None, region: str
) -> str | None:
    """Resource name of the engine the agent's own root just created, or None.

    None whenever there is nothing to name, which is a normal state rather than
    an error: a plan before the first apply has created no engine, and a
    Cloud Run or GKE agent never will. AQuA's variable is optional and a missing
    one means "watch no engine, keep the schedule", so the caller omits it.

    `infra show` reports no outputs when the root has no state yet, which is
    exactly the not-created-yet case.

    Args:
        project: GCP project ID, or None.
        region: GCP region string.

    Returns:
        Full engine resource name string, or None if unavailable.
    """
    if not project:
        return None
    raw = str(load_infra_outputs().get(_AGENT_ENGINE_OUTPUT) or "").strip()
    if not raw:
        return None
    # Normalize bare IDs or full resource names to the canonical resource name format.
    engine_id = raw.rstrip("/").rsplit("/", 1)[-1]
    return f"projects/{project}/locations/{region}/reasoningEngines/{engine_id}"


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
    triggers of every agent attached from this directory go first.

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

    destroy_attached_triggers(root, state_root, apply=apply)
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


def read_destroyed_engine_id(state_root: Path) -> str | None:
    """Read the id of the engine `--destroy` is about to delete.

    Args:
        state_root: Directory storing relocated state files.

    Returns:
        The numeric id from the `deployment_metadata.json` the deploy recorded,
        or None when no deploy recorded one.
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
    return engine.rstrip("/").rsplit("/", 1)[-1] or None


def destroy_attached_triggers(
    root: Path, state_root: Path, *, apply: bool
) -> None:
    """Tear down every attached agent's triggers, before the instance they call.

    Left behind, a scheduler job would call a deleted engine on every tick, and
    a sink would export to a deleted topic. Each agent's state came from
    `agents-cli aqua attach --apply` in this directory, and records the instance
    it was applied against; states for another instance are left alone.
    Triggers applied from another directory have their state there, and are
    named in a note rather than destroyed.

    Args:
        root: Extension root path.
        state_root: Directory storing relocated state files.
        apply: If True, destroys; if False, plans the teardown only.

    Raises:
        SystemExit: If an agent's state does not record its instance, the attach
            root's sources are missing, or Terraform fails.
    """
    echo(
        "\nAQuA: triggers applied with `agents-cli aqua attach --apply` from "
        "another directory are not destroyed here; remove them first with "
        "`agents-cli aqua detach <agent> --apply` from there."
    )
    agents = list_attached_agents(state_root)
    if not agents:
        return
    require_sources(root, ATTACH_ROOT)
    engine = read_destroyed_engine_id(state_root)
    for agent in agents:
        key = build_attach_key(agent)
        instance = read_state_outputs(state_root, key).get(
            ATTACH_INSTANCE_OUTPUT
        )
        if (
            isinstance(instance, dict)
            and engine
            and (
                str(instance.get("aqua_engine", "")).rsplit("/", 1)[-1]
                != engine
            )
        ):
            echo(
                f"\nAQuA: the triggers of {agent} here call "
                f"{instance.get('aqua_engine')}, not this AQuA, so they stay."
            )
            continue
        if not isinstance(instance, dict):
            die(
                f"{build_state_path(state_root, key)} does not say which AQuA "
                f"the triggers of {agent} call, so they cannot be destroyed "
                "before it.\n"
                "  Delete them by hand, then remove that file."
            )
        echo(
            f"\n{'Destroying' if apply else 'Planning the teardown of'} the "
            f"triggers of {agent}, from {root / ATTACH_ROOT}"
        )
        run_terraform(
            root / ATTACH_ROOT,
            state_key=key,
            state_root=state_root,
            apply=apply,
            destroy=True,
            tf_vars={**instance, "observed_agent_name": agent},
        )
        if apply:
            # An agent is listed by its state file alone, so one left behind
            # would make the next teardown look for triggers that are gone.
            build_state_path(state_root, key).unlink(missing_ok=True)


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
    if not standalone:
        install_skill_best_effort(Path(info["project_root"]))
    # Only AQuA's own checkout can leave the agent out: a vendored install is
    # always in the observed agent's project, which names it.
    observed = (
        resolve_observed_identity(options, info, standalone=standalone)
        if not standalone or options.agent_name
        else None
    )
    deployment_name = (
        observed.deployment_name
        if observed
        else resolve_observed_deployment_name(
            options, info, standalone=standalone
        )
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
    if observed:
        aqua_vars["observed_agent_name"] = observed.agent_name
    else:
        aqua_vars["configure_observed_agent"] = "false"
        echo(_NO_OBSERVED_AGENT)
    if not standalone:
        # Follows the observed agent's deployment target, which only its own
        # project knows. Left to the root's default and `TF_VAR_telemetry_table`.
        aqua_vars["telemetry_table"] = resolve_observed_telemetry_table(
            info["deployment_target"]
        )
    if project:
        bootstrap_vars["project_id"] = project
        aqua_vars["project_id"] = project

    # Turns AQuA's update trigger on: an audit sink fires an investigation when
    # the observed engine is redeployed. `infra show` reads the observed agent's
    # own Terraform root, which is out of reach standalone.
    engine = (
        None
        if standalone
        else read_observed_engine_resource_name(project, region)
    )
    if engine:
        aqua_vars["observed_engine_resource_name"] = engine
    elif standalone and observed:
        echo(
            "\nAQuA: set TF_VAR_observed_engine_id to the numeric id of the "
            "engine to watch, or update triggering stays off and investigations "
            "run on AQuA's schedule alone."
        )
    elif not standalone:
        echo(
            "\nAQuA: no observed engine to watch yet, so update triggering stays "
            "off and investigations run on AQuA's schedule alone.\n"
            "  It is switched on by the next `--apply` once the agent's engine "
            "exists."
        )

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
