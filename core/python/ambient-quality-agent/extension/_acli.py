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

"""Shared plumbing for the `agents-cli` extension wrappers beside this file.

`agents-cli-extension.yaml` at the repository root makes AQuA an agents-cli
extension: it overrides `infra single-project` and `deploy` so that, given
`--apply-aqua` and `--deploy-aqua`, AQuA is provisioned and deployed alongside
the agent it observes, and adds `aqua` for querying it. Each override is a
wrapper -- it runs the built-in first, then AQuA's step when asked for. See
`acli_infra.py` and `acli_deploy.py`.

Four properties of the extension system shape everything here:

* A wrapper runs with **cwd set to the observed agent's project root**, and the
  user's argv appended to the declared `run:` vector verbatim. Anything the
  built-in accepts therefore arrives unparsed, and is forwarded unparsed.
* `AGENTS_CLI_DISABLE_OVERRIDES=1` is already in the environment. That is what
  lets a wrapper shell back into `agents-cli` and reach the *built-in* rather
  than re-entering itself.
* `AGENTS_CLI_EXTENSION_DIR` points at the vendored copy of this repository,
  normally `<project>/extensions/aqua/`. It is where AQuA's Terraform, its
  Dockerfile and its own manifest live.
* That vendored copy is deleted and recreated wholesale by `agents-cli install`
  and `agents-cli extension update`. **Nothing that has to survive may be
  written into it** -- hence :data:`STATE_DIR_NAME`.

Every wrapper branches on two layouts. In the vendored install above, the
project in the working directory is the observed agent. In AQuA's own checkout
the manifest and the extension file are one directory, agents-cli loads the
extension inline, and the project is AQuA: the observed agent is elsewhere and
is named by :class:`ObservedOptions`, so the built-in steps are dropped.
:func:`is_aqua_own_checkout` tells the two apart.

Deliberately uses standard library only, plus `ambient_quality_shared.terraform_state`
(which is also standard library only). The manifest runs these wrappers under
`uv run --no-project` with no installed environment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple, NoReturn

# Add `src/` to sys.path so standalone wrappers can import shared state helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ambient_quality_shared.terraform_state import (
    ATTACH_INSTANCE_OUTPUT,
    ATTACH_ROOT,
    BOOTSTRAP_KEY,
    DEPLOYMENT_KEY,
    STATE_DIR_NAME,
    build_attach_key,
    build_state_path,
    list_attached_agents,
    read_state_outputs,
    run_terraform,
)

# Re-exported for sibling extension wrappers.
__all__ = [
    "ATTACH_INSTANCE_OUTPUT",
    "ATTACH_ROOT",
    "BOOTSTRAP_KEY",
    "DEPLOYMENT_KEY",
    "STATE_DIR_NAME",
    "build_attach_key",
    "build_state_path",
    "list_attached_agents",
    "read_state_outputs",
    "run_terraform",
]

# Subdirectory under :data:`STATE_DIR_NAME` where `acli_ui.py` stages the build context.
UI_BUILD_DIR_NAME = "ui-build"

# Written by `agents-cli deploy` into its working directory. The AQuA deploy
# runs with cwd inside the vendored tree, so the file is copied out afterwards.
METADATA_FILE = "deployment_metadata.json"

# Manifest file that `agents-cli` searches for when walking up to find the project root.
MANIFEST_FILE = "agents-cli-manifest.yaml"

# The extension schema lacks a skills field, so wrappers install this bundled skill directly.
SKILL_NAME = "agents-cli-aqua"

# Caps setup output logged on failure to provide diagnostic context without flooding logs.
_SETUP_FAILURE_TAIL_LINES = 5

# Ignore pattern that `ensure_state_dir_ignored` writes to ignore files.
_IGNORE_ENTRY = f"/{STATE_DIR_NAME}/"
_IGNORE_COMMENT = (
    "# AQuA's Terraform state and build scratch. Never committed, and never "
    "uploaded with the agent."
)

# Directive in `.gcloudignore` that includes `.gitignore`. When present, a single
# entry covers both git and Cloud Build uploads.
_GCLOUDIGNORE_INCLUDE = "#!include:.gitignore"

EXTENSION_DIR_ENV = "AGENTS_CLI_EXTENSION_DIR"

# Options naming the agent AQuA watches. The `TF_VAR_*` spellings are read too,
# since Terraform takes these as variables and reads them from the environment
# on its own.
OBSERVED_DEPLOYMENT_NAME = "--observed-deployment-name"
OBSERVED_AGENT_NAME = "--observed-agent-name"
_OBSERVED_DEPLOYMENT_NAME_ENV = "TF_VAR_observed_deployment_name"
_OBSERVED_AGENT_NAME_ENV = "TF_VAR_observed_agent_name"

# Add AQuA's step to `infra single-project` and `deploy` in the observed agent's
# project, which leave it out otherwise. AQuA's own checkout has no other step.
APPLY_AQUA = "--apply-aqua"
DEPLOY_AQUA = "--deploy-aqua"

# Names AQuA's resources in its own checkout when it is provisioned with no
# observed agent, and so no observed deployment to be named after.
STANDALONE_DEPLOYMENT_NAME = "aqua-solo"

_OBSERVED_HELP = (
    f"  Pass {OBSERVED_DEPLOYMENT_NAME} <deployment> {OBSERVED_AGENT_NAME} <adk-name>,\n"
    f"  or export {_OBSERVED_DEPLOYMENT_NAME_ENV} and {_OBSERVED_AGENT_NAME_ENV}.\n"
    "  The deployment is the observed agent's project name, and names every "
    "resource AQuA creates; the ADK name is the one its code passes to "
    "`Agent(name=...)`.\n"
    "  Leave out both to provision AQuA with no observed agent, named "
    f"'{STANDALONE_DEPLOYMENT_NAME}', and attach one later with "
    "`agents-cli aqua attach`."
)

# Table in the observed agent's telemetry dataset that AQuA reads. Cloud
# Logging names a sink table after the log id, which differs per deployment
# target, so the module's single default cannot be right for all of them and
# the wrapper passes the name that agents-cli's own telemetry.tf created.
# Getting it wrong is silent: AQuA reads an empty window rather than failing.
_TELEMETRY_TABLES = {
    "agent_runtime": "aiplatform_googleapis_com_reasoning_engine_stdout"
}
_DEFAULT_TELEMETRY_TABLE = "gen_ai_client_inference_operation_details"


def echo(message: str) -> None:
    """Write a progress line to stderr, leaving stdout for wrapped commands.

    Args:
        message: Progress text to display.
    """
    print(message, file=sys.stderr, flush=True)


def die(message: str) -> NoReturn:
    """Report a wrapper-level failure and exit non-zero.

    Args:
        message: Error message describing the failure.

    Raises:
        SystemExit: Always raised with exit code 1.
    """
    print(f"AQuA extension: {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def resolve_extension_root() -> Path:
    """Resolve the root path of the vendored extension copy.

    `AGENTS_CLI_EXTENSION_DIR` is authoritative when set by agents-cli. Falling
    back to the parent repository root keeps scripts runnable manually.

    Returns:
        Path to the extension root directory.
    """
    from_env = os.environ.get(EXTENSION_DIR_ENV)
    if from_env:
        return Path(from_env).resolve()
    return Path(__file__).resolve().parent.parent


def run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command with no shell, inheriting the environment plus ``env``.

    Args:
        argv: Command-line arguments vector.
        cwd: Working directory for the process.
        env: Additional environment variables to overlay.
        capture: If True, captures stdout and stderr.

    Returns:
        CompletedProcess instance containing execution results.
    """
    return subprocess.run(  # noqa: S603 - explicit argument list without shell execution
        list(argv),
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **env} if env else None,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def resolve_agents_cli_path() -> str:
    """Resolve the absolute path to the `agents-cli` executable.

    Resolves from PATH to avoid bare FileNotFoundError errors from subprocess.

    Returns:
        Absolute path to `agents-cli`.

    Raises:
        SystemExit: If `agents-cli` is not found on PATH.
    """
    found = shutil.which("agents-cli")
    if not found:
        die(
            "`agents-cli` is not on PATH.\n"
            "  The wrapper re-invokes the CLI to reach the command it overrides."
        )
    return found


def resolve_gcloud_path() -> str:
    """Resolve the absolute path to `gcloud` from PATH.

    Returns:
        Absolute path to `gcloud`.

    Raises:
        SystemExit: If `gcloud` is not found on PATH.
    """
    found = shutil.which("gcloud")
    if not found:
        die(
            "`gcloud` is not on PATH.\n"
            "  The dashboard step asks it whether the Cloud Run service exists."
        )
    return found


def run_builtin(command: Sequence[str], argv: Sequence[str]) -> None:
    """Run the built-in agents-cli command this wrapper overrides.

    Uses `AGENTS_CLI_DISABLE_OVERRIDES=1` from the environment to reach the
    built-in command without recursion.

    Args:
        command: Subcommand tokens to execute (e.g. `['deploy']`).
        argv: Additional CLI arguments to forward.

    Raises:
        SystemExit: If the built-in command exits non-zero.
    """
    result = run([resolve_agents_cli_path(), *command, *argv])
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def load_project_info() -> dict[str, Any]:
    """Load project metadata by executing `agents-cli info --json`.

    Returns:
        Dictionary containing project configuration.

    Raises:
        SystemExit: If info retrieval fails or no project root exists.
    """
    result = run([resolve_agents_cli_path(), "info", "--json"], capture=True)
    if result.returncode != 0:
        die(
            f"`agents-cli info --json` failed:\n  {(result.stderr or '').strip()}"
        )
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        die(f"could not parse `agents-cli info --json`: {e}")
    if not info.get("project_root"):
        die(
            "no agents-cli project found here.\n"
            "  Run this from a project scaffolded by `agents-cli create`."
        )
    return info


def load_infra_outputs() -> dict[str, Any]:
    """Load the outputs of the project's own Terraform root via `infra show`.

    `infra show` is the contract for what `infra single-project --apply`
    provisioned; the alternative is a `terraform output` against whichever
    directory the scaffold puts its root in, which is not ours to depend on. It
    reports no outputs, rather than failing, when the root has no state yet.

    Returns:
        The `outputs` mapping, or an empty dict when there is none or the
        command fails.
    """
    result = run(
        [resolve_agents_cli_path(), "infra", "show", "--json"], capture=True
    )
    if result.returncode != 0:
        return {}
    try:
        outputs = json.loads(result.stdout).get("outputs")
    except (AttributeError, json.JSONDecodeError):
        return {}
    return outputs if isinstance(outputs, dict) else {}


def find_project_root() -> Path | None:
    """Find the observed project root by walking parent directories for the manifest.

    Walks directories directly to locate the manifest before running built-in
    commands that do not require an existing project.

    Returns:
        Path to project root, or None if not found.
    """
    cwd = Path.cwd()
    for directory in (cwd, *cwd.parents):
        if (directory / MANIFEST_FILE).is_file():
            return directory
    return None


def is_aqua_own_checkout() -> bool:
    """Check whether the working directory is AQuA's own repository checkout.

    Returns:
        True if the current project root matches the extension root.
    """
    project_root = find_project_root()
    return (
        project_root is not None
        and project_root.resolve() == resolve_extension_root()
    )


def _append_ignore_entry(ignore_file: Path) -> None:
    """Append :data:`_IGNORE_ENTRY` to ``ignore_file``, creating it if missing.

    Args:
        ignore_file: File path to update.
    """
    existing = (
        ignore_file.read_text(encoding="utf-8") if ignore_file.is_file() else ""
    )
    if _IGNORE_ENTRY in existing.split():
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    ignore_file.write_text(
        f"{existing}{separator}\n{_IGNORE_COMMENT}\n{_IGNORE_ENTRY}\n",
        encoding="utf-8",
    )
    echo(f"AQuA: added {_IGNORE_ENTRY} to {ignore_file.name}.")


def ensure_state_dir_ignored(project_root: Path) -> None:
    """Ensure :data:`STATE_DIR_NAME` is ignored by git and deployment uploads.

    Updates `.gitignore` and standalone `.gcloudignore` files to prevent
    committing Terraform state or uploading scratch files.

    Args:
        project_root: Root directory of the observed agent project.
    """
    _append_ignore_entry(project_root / ".gitignore")
    gcloudignore = project_root / ".gcloudignore"
    if gcloudignore.is_file():
        text = gcloudignore.read_text(encoding="utf-8")
        if _GCLOUDIGNORE_INCLUDE not in text:
            _append_ignore_entry(gcloudignore)


def ensure_aqua_state_dir(info: Mapping[str, Any]) -> Path:
    """Return the path to :data:`STATE_DIR_NAME`, creating and ignoring it.

    Args:
        info: Project metadata dictionary containing `project_root`.

    Returns:
        Path to the verified state directory.
    """
    project_root = Path(info["project_root"])
    path = project_root / STATE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    ensure_state_dir_ignored(project_root)
    return path


# TODO(b/567862679): Delete once `agents-cli extension add` installs extension
# skills (b/567822506), and declare the skill in agents-cli-extension.yaml.
def install_skill_best_effort(project_root: Path) -> None:
    """Install AQuA's coding-agent skill into the project without failing callers.

    Because the skill is an optional aid for coding agents, installation errors
    must not abort deployment or infrastructure workflows.

    Execution sequence:
    1. Verify the skill source contains a `SKILL.md` manifest.
    2. Run `agents-cli setup --workspace --skills-source` to install into `.agents/skills/`
       with agents-cli's own installer. Setup also runs its
       `uv tool install google-agents-cli` step.
    3. Capture subprocess output so installer progress does not obscure deploy logs.
    4. Log summary error details on failure and swallow exceptions.

    Args:
        project_root: Root directory of the observed agent project.
    """
    source = resolve_extension_root() / "skills" / SKILL_NAME
    if not (source / "SKILL.md").is_file():
        echo(f"AQuA: skill install skipped (no SKILL.md in {source}).")
        return
    argv = [
        resolve_agents_cli_path(),
        "setup",
        "--workspace",
        "--skip-auth",
        "--skills-source",
        str(source),
    ]
    try:
        result = run(argv, cwd=project_root, capture=True)
    except OSError as error:
        echo(f"AQuA: skill install skipped ({error}).")
        return
    if result.returncode == 0:
        echo(
            f"AQuA: installed the {SKILL_NAME} skill into .agents/skills/ "
            "(agents-cli setup)."
        )
        return
    echo(
        f"AQuA: skill install skipped (agents-cli setup exited {result.returncode})."
    )
    output = (
        f"{result.stdout or ''}\n{result.stderr or ''}".strip().splitlines()
    )
    for line in output[-_SETUP_FAILURE_TAIL_LINES:]:
        echo(f"  {line}")


def resolve_observed_agent_name(info: Mapping[str, Any]) -> str:
    """Resolve the ADK agent name AQuA matches telemetry on.

    Telemetry rows match on `gen_ai.agent.name`, which corresponds to the name
    passed to `Agent(name=...)` rather than the infrastructure project name.

    Args:
        info: Project metadata mapping.

    Returns:
        The matched agent name string.
    """
    from_info = info.get("root_agent_name")
    if from_info:
        return str(from_info)
    return re.sub(r"\W+", "_", str(info.get("project_name") or "")).lower()


class ObservedOptions(NamedTuple):
    """What the command line and the environment say about the observed agent."""

    deployment_name: str | None
    agent_name: str | None


class Observed(NamedTuple):
    """The agent AQuA watches, once resolved."""

    deployment_name: str
    agent_name: str


def take_observed_options(
    argv: Sequence[str],
) -> tuple[list[str], ObservedOptions]:
    """Remove AQuA observed-agent options from ``argv`` and return parsed values.

    Args:
        argv: Command-line arguments vector.

    Returns:
        Tuple of (filtered arguments list, parsed ObservedOptions).

    Raises:
        SystemExit: If an observed-agent option is given without a value.
    """
    # Checked here because `take_option` drops an option with no value, which
    # would otherwise fall through to a default rather than fail.
    for index, arg in enumerate(argv):
        if arg in (OBSERVED_DEPLOYMENT_NAME, OBSERVED_AGENT_NAME) and (
            index + 1 == len(argv) or argv[index + 1].startswith("-")
        ):
            die(f"{arg} needs a value.\n{_OBSERVED_HELP}")
    remaining, deployment_name = take_option(argv, OBSERVED_DEPLOYMENT_NAME)
    remaining, agent_name = take_option(remaining, OBSERVED_AGENT_NAME)
    # `--name=` gives an empty value, which would also fall through.
    for option, value in (
        (OBSERVED_DEPLOYMENT_NAME, deployment_name),
        (OBSERVED_AGENT_NAME, agent_name),
    ):
        if value == "":
            die(f"{option} needs a value.\n{_OBSERVED_HELP}")
    return remaining, ObservedOptions(
        deployment_name or os.environ.get(_OBSERVED_DEPLOYMENT_NAME_ENV),
        agent_name or os.environ.get(_OBSERVED_AGENT_NAME_ENV),
    )


def resolve_observed_deployment_name(
    options: ObservedOptions, info: Mapping[str, Any], *, standalone: bool
) -> str:
    """Resolve the deployment name of the observed agent.

    In AQuA's own checkout with neither observed option given, AQuA has no
    observed agent, and its resources are named :data:`STANDALONE_DEPLOYMENT_NAME`.

    Args:
        options: Command line and environment options.
        info: Project metadata mapping.
        standalone: True if running from AQuA's own checkout.

    Returns:
        Observed deployment name string.

    Raises:
        SystemExit: If standalone and only the ADK agent name is given.
    """
    if not standalone:
        return options.deployment_name or str(info["project_name"])
    if options.deployment_name:
        return options.deployment_name
    if not options.agent_name:
        echo(
            f"AQuA: no {OBSERVED_DEPLOYMENT_NAME}, so AQuA is named "
            f"'{STANDALONE_DEPLOYMENT_NAME}'."
        )
        return STANDALONE_DEPLOYMENT_NAME
    die(
        f"AQuA's own checkout does not say which deployment "
        f"'{options.agent_name}' belongs to.\n{_OBSERVED_HELP}"
    )


def resolve_observed_identity(
    options: ObservedOptions, info: Mapping[str, Any], *, standalone: bool
) -> Observed:
    """Resolve the deployment name and ADK agent name for the observed agent.

    Args:
        options: Command line and environment options.
        info: Project metadata mapping.
        standalone: True if running from AQuA's own checkout.

    Returns:
        Observed identity containing deployment_name and agent_name.

    Raises:
        SystemExit: If identity details cannot be resolved.
    """
    deployment_name = resolve_observed_deployment_name(
        options, info, standalone=standalone
    )
    agent_name = options.agent_name or (
        None if standalone else resolve_observed_agent_name(info)
    )
    if not agent_name:
        die(
            f"AQuA's own checkout does not say what ADK calls '{deployment_name}'.\n"
            f"{_OBSERVED_HELP}"
        )
    return Observed(deployment_name, agent_name)


def build_aqua_service_name(project_name: str) -> str:
    """Build the display name of the AQuA engine observing ``project_name``.

    Names the engine `<observed_deployment_name>-aqua`, the `display_name`
    Terraform gives it in `service.tf`, so the deploy updates that engine.

    Args:
        project_name: Observed project or deployment name.

    Returns:
        Formatted service name string.
    """
    return f"{project_name or 'agent'}-aqua"


def build_aqua_ui_service_name(project_name: str) -> str:
    """Build the dashboard Cloud Run service name matching `ui.tf`.

    Args:
        project_name: Observed project or deployment name.

    Returns:
        Formatted UI service name string.
    """
    return f"{build_aqua_service_name(project_name)}-ui"


def resolve_observed_telemetry_table(deployment_target: str) -> str:
    """Resolve telemetry table written by observed agent on ``deployment_target``.

    Args:
        deployment_target: Target platform identifier (e.g. `agent_runtime`).

    Returns:
        Name of BigQuery telemetry table.
    """
    return _TELEMETRY_TABLES.get(deployment_target, _DEFAULT_TELEMETRY_TABLE)


def take_flag(argv: Sequence[str], flag: str) -> tuple[list[str], bool]:
    """Remove ``flag`` from ``argv``, reporting whether it was present.

    Args:
        argv: Command-line arguments vector.
        flag: Flag string to extract.

    Returns:
        Tuple of (filtered arguments list, was_present boolean).
    """
    remaining = [arg for arg in argv if arg != flag]
    return remaining, len(remaining) != len(argv)


def take_aqua_flag(argv: Sequence[str], flag: str) -> tuple[list[str], bool]:
    """Remove the flag that adds AQuA's step to a wrapped command.

    In the observed agent's project, `infra single-project` and `deploy` leave
    AQuA out unless asked, so that a project attached to an AQuA deployed
    elsewhere does not get a second one beside it.

    Args:
        argv: Command-line arguments vector.
        flag: The opt-in flag, `--apply-aqua` or `--deploy-aqua`.

    Returns:
        Tuple of (filtered arguments list, whether the flag was present).

    Raises:
        SystemExit: If `--skip-aqua` is given, which the built-in would reject
            with no hint of the opt-in flag.
    """
    if "--skip-aqua" in argv:
        die(
            f"--skip-aqua is not an option: AQuA is left out unless {flag} is "
            "given."
        )
    return take_flag(argv, flag)


def build_aqua_infra_command() -> str:
    """Build the command that provisions AQuA from the working directory.

    Returns:
        `agents-cli infra single-project --apply`, with :data:`APPLY_AQUA` in
        the observed agent's project, where AQuA is left out without it.
    """
    command = "agents-cli infra single-project --apply"
    return command if is_aqua_own_checkout() else f"{command} {APPLY_AQUA}"


def take_option(argv: Sequence[str], name: str) -> tuple[list[str], str | None]:
    """Remove option and its value from ``argv`` and return the first value found.

    Args:
        argv: Command-line arguments vector.
        name: Option name string (e.g. `--project`).

    Returns:
        Tuple of (filtered arguments list, extracted value or None).
    """
    remaining: list[str] = []
    value: str | None = None
    prefix = f"{name}="
    skip = False
    for index, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg == name:
            # A trailing `--name` carries no value, and is dropped all the same.
            if index + 1 < len(argv) and value is None:
                value = argv[index + 1]
            skip = index + 1 < len(argv)
            continue
        if arg.startswith(prefix):
            if value is None:
                value = arg[len(prefix) :]
            continue
        remaining.append(arg)
    return remaining, value


def extract_option_value(argv: Sequence[str], name: str) -> str | None:
    """Extract the value of option ``name`` from ``argv`` without modifying it.

    Args:
        argv: Command-line arguments vector.
        name: Option name string.

    Returns:
        Extracted value string, or None if absent.
    """
    prefix = f"{name}="
    for index, arg in enumerate(argv):
        if arg == name and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return None


def resolve_project(argv: Sequence[str]) -> str | None:
    """Resolve the GCP project ID from arguments, environment, or gcloud config.

    Checks `--project`, then `GOOGLE_CLOUD_PROJECT`, then gcloud defaults to
    keep both wrapper steps aligned.

    Args:
        argv: Command-line arguments vector.

    Returns:
        Resolved GCP project ID string, or None.
    """
    from_argv = extract_option_value(argv, "--project")
    if from_argv:
        return from_argv
    from_env = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if from_env:
        return from_env
    gcloud = shutil.which("gcloud")
    if not gcloud:
        return None
    result = run([gcloud, "config", "get-value", "project"], capture=True)
    value = (result.stdout or "").strip()
    return (
        value
        if result.returncode == 0 and value and value != "(unset)"
        else None
    )
