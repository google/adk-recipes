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

"""`agents-cli aqua` -- AQuA's own CLI, pointed at the project's deployment.

Declared as `commands.add` in `agents-cli-extension.yaml`. AQuA's CLI takes its
target from the environment, so all this adds is the one value the observed
project knows and the CLI does not: the engine id the deploy wrapper recorded in
`.aqua/deployment_metadata.json`, or, for a project that deploys no AQuA of its
own, the one `attach --aqua-resource` recorded in `.aqua/attached_aqua.json`.

Two details are not exposed by the underlying CLI: the targeted engine
resource name and the Cloud Run dashboard service provisioned by Terraform.
`info` is therefore handled here. Without flags, it prints a JSON object
containing the recorded deployment metadata and the dashboard service name and
URL; `--resource`, `--region`, `--ui-service`, and `--ui-url` each print a single
value for shell scripts.

`ui-proxy` is handled here for the same reason: it is `gcloud run services
proxy` with those two values filled in, which is the whole of what makes the
command awkward to type.

`attach` is seeded here before the CLI sees it. The project records the
observed engine, which becomes `--observed-agent-resource` for the CLI to
discover the agent's settings from, and knows how it set the agent up -- its ADK
name, its deployment and the telemetry sink `infra single-project` created --
which goes along as hints that discovery outranks; see
:func:`resolve_discovered_options`. `attach` and `publish-source` are also
given the project as the source to publish; see :func:`add_source_root`. Once
an `attach` that names `--aqua-resource` succeeds, that engine is recorded for
the commands after it; see :func:`save_attached_aqua_resource`.

The CLI is executed in this process rather than as a subprocess so that its exit
code -- a contract callers branch on, not just success or failure -- reaches the
caller untouched, and so that only one interpreter has to carry its three
third-party imports.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path
from typing import Any, NamedTuple

from _acli import (
    DEPLOYMENT_KEY,
    METADATA_FILE,
    STATE_DIR_NAME,
    build_aqua_infra_command,
    echo,
    ensure_state_dir_ignored,
    extract_option_value,
    find_project_root,
    is_aqua_own_checkout,
    load_infra_outputs,
    load_project_info,
    read_state_outputs,
    resolve_extension_root,
    resolve_gcloud_path,
    resolve_observed_agent_name,
    resolve_observed_telemetry_table,
    resolve_project,
    run,
)

CLI_ENTRYPOINT = Path("src") / "ambient_quality_cli" / "aqua_cli.py"

# Records the AQuA a successful `attach --aqua-resource` reached, so that later
# commands in the project reach it too. Kept apart from METADATA_FILE, which
# records the AQuA this project deploys: `infra single-project --destroy` reads
# that one to tell which attached triggers call the engine it deletes.
ATTACHED_AQUA_FILE = "attached_aqua.json"

# Set by the CLI's own `--aqua-resource` (or `--resource`) / `--url`, which must
# win over the recorded deployment: they are how a user targets another engine
# or a local ADK server.
_TARGET_FLAGS = ("--aqua-resource", "--resource", "--url")

# `info` flags that each print a single value to stdout. Mutually exclusive.
_INFO_FLAGS = ("--resource", "--region", "--ui-service", "--ui-url")

_INFO_USAGE = "  Usage: agents-cli aqua info [--resource | --region | --ui-service | --ui-url]"

_NO_TARGET = (
    "No AQuA deployment recorded for this project.\n"
    "  Deploy it with: agents-cli deploy --deploy-aqua\n"
    "  Or attach to one deployed elsewhere:\n"
    "    agents-cli aqua attach --aqua-resource projects/…/reasoningEngines/…\n"
    "  Or name one:    AGENT_ENGINE_RESOURCE_ID=projects/…/reasoningEngines/…"
)


def render_no_ui_message() -> str:
    """Render the report for a project with no AQuA dashboard provisioned.

    Returns:
        The message, naming the command that provisions it here.
    """
    return (
        "No AQuA dashboard provisioned for this project.\n"
        f"  Create it with: {build_aqua_infra_command()}"
    )


# `attach` options that take no value. Every other option consumes the next
# token unless spelled `--name=value`, which is how AGENT_NAME is told apart
# from an option's value. A test pins this set to the CLI's own definition.
_ATTACH_SWITCHES = frozenset(
    {
        "--help",
        "--dry-run",
        "--apply",
        "--yes",
        "--no-discover",
        "--default",
        "--no-default",
        "--verification",
        "--no-verification",
        "--enforce-verification",
        "--no-enforce-verification",
        "--scheduled-trigger",
        "--no-scheduled-trigger",
    }
)

# The hint for `attach`'s positional AGENT_NAME, which a given one outranks.
HINTED_AGENT_NAME = "observed_agent_name"

# The only telemetry source agents-cli provisions: `infra single-project`
# creates a Cloud Logging sink into a BigQuery dataset.
_AGENTS_CLI_TELEMETRY_SOURCE = "cloud_logging"

# The `infra show` output naming that sink's dataset.
_TELEMETRY_DATASET_OUTPUT = "telemetry_dataset_id"


class DiscoveredOption(NamedTuple):
    """One `attach` option or hint discovered from the project, and its source.

    An option is named by its flag (`--observed-agent-resource`), a hint by the
    field it suggests (`telemetry_dataset`).
    """

    option: str
    value: str
    source: str


def build_metadata_path() -> Path:
    """Return the filesystem path where deployment metadata is stored.

    Returns:
        Path to `.aqua/deployment_metadata.json` in the current working directory.
    """
    return Path.cwd() / STATE_DIR_NAME / METADATA_FILE


def load_recorded_deployment() -> dict[str, Any]:
    """Load deployment record written by `agents-cli deploy` for this project.

    Returns an empty dict if the file is missing or unreadable. A missing target
    is reported by the CLI rather than raising an error here.

    Returns:
        Parsed deployment metadata dictionary, or empty dict if unreadable.
    """
    return load_deployment_metadata(build_metadata_path())


def load_deployment_metadata(metadata: Path) -> dict[str, Any]:
    """Load a JSON record such as the `deployment_metadata.json` a deploy writes.

    Args:
        metadata: Path to the file.

    Returns:
        Parsed dictionary, or empty dict if missing, unreadable or not an
        object.
    """
    if not metadata.is_file():
        return {}
    try:
        record = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError covers both non-UTF-8 bytes (UnicodeDecodeError) and
        # non-JSON text (JSONDecodeError).
        return {}
    return record if isinstance(record, dict) else {}


def read_recorded_engine_id() -> str | None:
    """Read the AQuA engine recorded for this project.

    The AQuA this project deploys wins over one `attach --aqua-resource`
    recorded, since a later deploy is the newer statement of which AQuA the
    project uses.

    Returns:
        Remote Agent Runtime resource ID string, or None if unrecorded.
    """
    return (
        load_recorded_deployment().get("remote_agent_runtime_id")
        or read_attached_aqua_resource()
    )


def build_attached_aqua_path() -> Path:
    """Return the path of the record `attach --aqua-resource` writes.

    Returns:
        Path to `.aqua/attached_aqua.json` in the current working directory.
    """
    return Path.cwd() / STATE_DIR_NAME / ATTACHED_AQUA_FILE


def read_attached_aqua_resource() -> str | None:
    """Read the AQuA engine a successful `attach --aqua-resource` recorded.

    Returns:
        The engine resource name, or None if missing or unreadable.
    """
    resource = load_deployment_metadata(build_attached_aqua_path()).get(
        "aqua_resource"
    )
    return str(resource) if resource else None


def extract_aqua_resource_to_save(argv: list[str]) -> str | None:
    """Extract the AQuA engine an `attach` run should record once it succeeds.

    A dry run attaches nothing, and `--url` may name a local server, so
    neither is recorded.

    Args:
        argv: The full `agents-cli aqua` arguments, subcommand first.

    Returns:
        The `--aqua-resource` (or `--resource`) value, or None when there is
        nothing to record.
    """
    if not argv or argv[0] != "attach":
        return None
    rest = argv[1:]
    if "--dry-run" in rest or "--help" in rest:
        return None
    return extract_option_value(
        rest, "--aqua-resource"
    ) or extract_option_value(rest, "--resource")


def save_attached_aqua_resource(resource: str) -> None:
    """Record the AQuA engine `attach` just reached, for later commands here.

    Steps:
    1. Leave a project that deploys its own AQuA alone, saying so when the
       attach reached a different one.
    2. Write `.aqua/attached_aqua.json`, and make sure `.aqua/` is ignored.

    A failure to write is reported and swallowed: the attach itself succeeded.

    Args:
        resource: The engine resource name the attach was sent to.
    """
    resource = resource.rstrip("/")
    deployed = str(
        load_recorded_deployment().get("remote_agent_runtime_id") or ""
    )
    if deployed:
        if deployed.rstrip("/") != resource:
            echo(
                f"AQuA: this project deploys its own AQuA, {deployed}, and later "
                "commands here keep reaching it. Pass --aqua-resource "
                f"{resource} to reach the one just attached to."
            )
        return
    previous = read_attached_aqua_resource()
    if previous == resource:
        return
    path = build_attached_aqua_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"aqua_resource": resource}, indent=2) + "\n",
            encoding="utf-8",
        )
        project_root = find_project_root()
        if project_root:
            ensure_state_dir_ignored(project_root)
    except OSError as error:
        echo(f"AQuA: could not record {resource} in {path} ({error}).")
        return
    replaced = f", replacing {previous}" if previous else ""
    echo(
        f"AQuA: recorded {resource} in {STATE_DIR_NAME}/{ATTACHED_AQUA_FILE}"
        f"{replaced}; later `agents-cli aqua` commands here reach it."
    )


def read_provisioned_ui() -> tuple[str | None, str | None]:
    """Read Cloud Run dashboard service name and URL from Terraform state.

    Returns `(None, None)` until the deployment root has been applied.

    Returns:
        Tuple of `(ui_service_name, ui_service_uri)`.
    """
    outputs = read_state_outputs(Path.cwd() / STATE_DIR_NAME, DEPLOYMENT_KEY)
    return outputs.get("ui_service_name"), outputs.get("ui_service_uri")


def extract_engine_location(resource: str | None) -> str | None:
    """Extract the GCP location from an engine resource name.

    Parses the region segment from `projects/P/locations/L/reasoningEngines/ID`.

    Args:
        resource: Full engine resource name string.

    Returns:
        Extracted region string, or None if not found.
    """
    parts = (resource or "").split("/")
    if len(parts) > 3 and parts[2] == "locations":
        return parts[3]
    return None


def show_info_cmd(argv: list[str]) -> int:
    """Report deployment metadata and dashboard service details for this project.

    Without flags, writes a JSON object to stdout containing deployment keys,
    `attached_aqua_resource` when the engine comes from `attach`, and `ui`.
    With a specific flag (`--resource`, `--region`, `--ui-service`,
    `--ui-url`), prints that single value alone.

    Args:
        argv: Command-line arguments for the info subcommand.

    Returns:
        Exit code (0 on success, non-zero on error).
    """
    selected = list(dict.fromkeys(arg for arg in argv if arg in _INFO_FLAGS))
    unknown = [arg for arg in argv if arg not in _INFO_FLAGS]
    if unknown:
        print(
            f"agents-cli aqua info: unexpected argument '{unknown[0]}'.\n{_INFO_USAGE}",
            file=sys.stderr,
        )
        return 2
    if len(selected) > 1:
        print(
            f"agents-cli aqua info: '{selected[0]}' and '{selected[1]}' each print "
            f"a value alone, so only one may be given.\n{_INFO_USAGE}",
            file=sys.stderr,
        )
        return 2

    # An explicit target outranks the recorded one everywhere else, so report
    # what a command run right now would actually reach.
    override = os.environ.get("AGENT_ENGINE_RESOURCE_ID")
    record = load_recorded_deployment()
    attached = read_attached_aqua_resource()
    engine = override or record.get("remote_agent_runtime_id") or attached
    ui_service_name, ui_url = read_provisioned_ui()

    if selected:
        value, missing = {
            "--resource": (engine, _NO_TARGET),
            "--region": (extract_engine_location(engine), _NO_TARGET),
            "--ui-service": (ui_service_name, render_no_ui_message()),
            "--ui-url": (ui_url, render_no_ui_message()),
        }[selected[0]]
        if not value:
            print(missing, file=sys.stderr)
            return 1
        print(value)
        return 0

    metadata = build_metadata_path()
    if not metadata.is_file() and not attached:
        print(f"No {metadata}.", file=sys.stderr)
    report: dict[str, Any] = dict(record)
    if attached and not record.get("remote_agent_runtime_id"):
        report["attached_aqua_resource"] = attached
    report["ui"] = {"service_name": ui_service_name, "url": ui_url}
    print(json.dumps(report, indent=2))
    if override:
        print(
            f"\nAGENT_ENGINE_RESOURCE_ID is set, and wins: {override}",
            file=sys.stderr,
        )
    return 0 if engine else 1


def proxy_ui_cmd(argv: list[str]) -> int:
    """Tunnel to the dashboard via `gcloud run services proxy`.

    Resolves service name and region from state and forwards additional
    arguments to `gcloud`.

    Args:
        argv: Command-line arguments passed to `ui-proxy`.

    Returns:
        Exit code from the `gcloud` subprocess.
    """
    ui_service_name, _ = read_provisioned_ui()
    if not ui_service_name:
        print(render_no_ui_message(), file=sys.stderr)
        return 1

    engine = (
        os.environ.get("AGENT_ENGINE_RESOURCE_ID") or read_recorded_engine_id()
    )
    region = extract_engine_location(engine)
    if not region:
        print(_NO_TARGET, file=sys.stderr)
        return 1

    command = [
        resolve_gcloud_path(),
        "run",
        "services",
        "proxy",
        ui_service_name,
    ]
    if extract_option_value(argv, "--region") is None:
        command.append(f"--region={region}")
    project = extract_option_value(argv, "--project") or resolve_project(argv)
    if extract_option_value(argv, "--project") is None and project:
        command.append(f"--project={project}")
    command.extend(argv)

    # Naming the project is what makes a stale `GOOGLE_CLOUD_PROJECT` visible:
    # it outranks gcloud's own configured project, and gcloud's error for the
    # wrong one is about a disabled API rather than about the project.
    echo(
        f"Tunnelling to '{ui_service_name}' in {project or '(no project)'}/{region}."
        " Ctrl-C to stop."
    )
    result = run(command)
    if result.returncode != 0:
        # gcloud offers to install it, then cannot when the component manager is
        # disabled.
        print(
            "\nIf that failed for a missing `cloud-run-proxy` component:\n"
            "  sudo apt-get install google-cloud-cli-cloud-run-proxy",
            file=sys.stderr,
        )
    return result.returncode


def has_agent_name_argument(argv: list[str]) -> bool:
    """Check whether `attach` arguments already name the agent.

    Args:
        argv: Arguments after `attach`.

    Returns:
        True if a positional argument is present.
    """
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return index + 1 < len(argv)
        if not arg.startswith("-"):
            return True
        if "=" not in arg and arg not in _ATTACH_SWITCHES:
            index += 1
        index += 1
    return False


def is_option_given(argv: list[str], name: str) -> bool:
    """Check whether option ``name`` appears in ``argv`` in either spelling.

    Args:
        argv: Command-line arguments vector.
        name: Option name string (e.g. `--observed-agent-resource`).

    Returns:
        True if the option is present.
    """
    return any(arg == name or arg.startswith(f"{name}=") for arg in argv)


def should_discover_options(argv: list[str]) -> bool:
    """Decide whether `attach` options are discovered from the project.

    Not in AQuA's own checkout, where the project is AQuA rather than the agent
    it observes, not for `--help`, which needs no values, and not for
    `--no-discover`, which stores only what the user typed.

    Args:
        argv: Arguments after `attach`.

    Returns:
        True if the working directory is an observed agent's project.
    """
    return (
        "--help" not in argv
        and "--no-discover" not in argv
        and find_project_root() is not None
        and not is_aqua_own_checkout()
    )


def resolve_discovered_options(
    argv: list[str], info: dict[str, Any]
) -> tuple[list[DiscoveredOption], list[str]]:
    """Derive the `attach` values the project knows and the user did not give.

    The observed engine becomes an option the CLI discovers the agent's
    settings from. Everything else the wrapper already reads for `infra
    single-project` goes to the CLI as `--hints`, which discovery outranks:
    `root_agent_name` is fixed at scaffold time, and the manifest's region need
    not be where the dataset is.

    1. `--observed-agent-resource` from the agent's own
       `deployment_metadata.json` at the project root; `.aqua/` holds AQuA's.
    2. `observed_agent_name` and `observed_deployment_name` hints from
       `agents-cli info`.
    3. The telemetry hints from the Cloud Logging sink `infra single-project`
       created, found through `infra show`. They are hinted together or not at
       all, and not when the user named a different source.

    Args:
        argv: Arguments after `attach`.
        info: Output of `agents-cli info --json`.

    Returns:
        Tuple of (options to add, notes on values that could not be found). A
        hint is an option named after its field.
    """
    discovered_options: list[DiscoveredOption] = []
    notes: list[str] = []

    def add(option: str, value: Any, source: str, flag: str = "") -> None:
        """Adds a value unless the user already gave it.

        Args:
            option: The option or hint to add.
            value: Its value; nothing is added when empty.
            source: Where the value came from.
            flag: The flag that gives the same value, when ``option`` is a
                hint named after its field.
        """
        given = (
            has_agent_name_argument(argv)
            if option == HINTED_AGENT_NAME
            else is_option_given(argv, flag or option)
        )
        if not given and value:
            discovered_options.append(
                DiscoveredOption(option, str(value), source)
            )

    project_root = Path(info.get("project_root") or Path.cwd())
    record = load_deployment_metadata(project_root / METADATA_FILE)
    engine = str(record.get("remote_agent_runtime_id") or "").rstrip("/")
    seeded = bool(engine) or is_option_given(argv, "--observed-agent-resource")
    add("--observed-agent-resource", engine, f"./{METADATA_FILE}")
    if not seeded:
        notes.append(
            f"no engine in ./{METADATA_FILE}, so nothing is discovered from the "
            "running agent. `agents-cli deploy` records one."
        )

    agent_source = (
        "root_agent_name from agents-cli info"
        if info.get("root_agent_name")
        else "project_name from agents-cli info, as the scaffold names the agent"
    )
    add(HINTED_AGENT_NAME, resolve_observed_agent_name(info), agent_source)
    add(
        "observed_deployment_name",
        info.get("project_name"),
        "project_name from agents-cli info",
        "--deployment-name",
    )

    source = extract_option_value(argv, "--telemetry-source")
    if source not in (None, _AGENTS_CLI_TELEMETRY_SOURCE):
        return discovered_options, notes
    dataset_given = is_option_given(argv, "--telemetry-dataset")
    dataset = (
        None
        if dataset_given
        else load_infra_outputs().get(_TELEMETRY_DATASET_OUTPUT)
    )
    if not dataset_given and not dataset:
        if not seeded:
            notes.append(
                "--telemetry-*: `agents-cli infra show` names no telemetry "
                "dataset, so they keep their stored values.\n"
                "  Provision it with: agents-cli infra single-project --apply"
            )
        return discovered_options, notes
    add(
        "telemetry_ingestion_source",
        _AGENTS_CLI_TELEMETRY_SOURCE,
        "the only sink agents-cli creates",
        "--telemetry-source",
    )
    add(
        "telemetry_dataset",
        dataset,
        f"{_TELEMETRY_DATASET_OUTPUT} from agents-cli infra show",
        "--telemetry-dataset",
    )
    target = info.get("deployment_target")
    add(
        "telemetry_table",
        resolve_observed_telemetry_table(str(target or "")),
        f"deployment_target {target} from agents-cli info",
        "--telemetry-table",
    )
    add(
        "telemetry_location",
        info.get("region"),
        "region from agents-cli info",
        "--telemetry-location",
    )
    return discovered_options, notes


def is_hint(discovered: DiscoveredOption) -> bool:
    """Check whether a discovered value goes to the CLI as a hint.

    Args:
        discovered: A value from :func:`resolve_discovered_options`.

    Returns:
        True for a hint, False for an option added to the command line.
    """
    return not discovered.option.startswith("-")


def render_discovered_options(
    discovered_options: list[DiscoveredOption], notes: list[str]
) -> list[str]:
    """Render the options added to `attach` as lines printed before the plan.

    Hints are left out: the CLI shows each with its source, beside what
    discovery found.

    Args:
        discovered_options: Values added to the command line.
        notes: Values that could not be found.

    Returns:
        Lines to print, in order; empty when there is nothing to say.
    """
    lines = [
        f"AQuA: {option.option} {option.value}  ({option.source})"
        for option in discovered_options
        if not is_hint(option)
    ]
    lines.extend(f"AQuA: {note}" for note in notes)
    return lines


def build_attach_argv(
    argv: list[str], discovered_options: list[DiscoveredOption]
) -> list[str]:
    """Build the `attach` arguments with the discovered options added.

    Args:
        argv: Arguments after `attach`, as the user gave them.
        discovered_options: Options and hints to add.

    Returns:
        Arguments after `attach` to hand to the CLI.
    """
    options = [
        f"{discovered.option}={discovered.value}"
        for discovered in discovered_options
        if not is_hint(discovered)
    ]
    hints = {
        discovered.option: {
            "value": discovered.value,
            "source": discovered.source,
        }
        for discovered in discovered_options
        if is_hint(discovered)
    }
    if hints:
        options.append(f"--hints={json.dumps(hints)}")
    if "--" in argv:
        split = argv.index("--")
        return [*argv[:split], *options, *argv[split:]]
    return [*argv, *options]


def add_source_root(command: str, argv: list[str]) -> list[str]:
    """Name the observed agent's project as the source to publish, unless given.

    `publish-source` publishes this project's source. `attach` takes the first
    snapshot once the agent is deployed, which its `deployment_metadata.json`
    shows; before then there is no revision to key it by. Not in AQuA's own
    checkout, where the project is AQuA rather than the agent it observes.

    Args:
        command: `attach` or `publish-source`.
        argv: Arguments after the command.

    Returns:
        The arguments, with `--source-root` added when it applies.
    """
    root = find_project_root()
    if (
        "--help" in argv
        or is_option_given(argv, "--source-root")
        or root is None
        or is_aqua_own_checkout()
    ):
        return argv
    if command == "attach" and not (root / METADATA_FILE).is_file():
        return argv
    option = f"--source-root={root}"
    if "--" in argv:
        split = argv.index("--")
        return [*argv[:split], option, *argv[split:]]
    return [*argv, option]


def main(argv: list[str]) -> int:
    if argv and argv[0] == "info":
        return show_info_cmd(argv[1:])
    if argv and argv[0] == "ui-proxy":
        return proxy_ui_cmd(argv[1:])
    if argv and argv[0] == "attach" and should_discover_options(argv[1:]):
        discovered_options, notes = resolve_discovered_options(
            argv[1:], load_project_info()
        )
        for line in render_discovered_options(discovered_options, notes):
            echo(line)
        argv = ["attach", *build_attach_argv(argv[1:], discovered_options)]
    if argv and argv[0] in ("attach", "publish-source"):
        argv = [argv[0], *add_source_root(argv[0], argv[1:])]

    entrypoint = resolve_extension_root() / CLI_ENTRYPOINT
    if not entrypoint.is_file():
        print(
            f"AQuA extension: AQuA's CLI is not at '{entrypoint}'.\n"
            "  Restore the sources with: agents-cli install",
            file=sys.stderr,
        )
        return 1

    targeted = any(arg.split("=", 1)[0] in _TARGET_FLAGS for arg in argv)
    if not targeted and not os.environ.get("AGENT_ENGINE_RESOURCE_ID"):
        engine = read_recorded_engine_id()
        if engine:
            os.environ["AGENT_ENGINE_RESOURCE_ID"] = engine

    sys.argv = [str(entrypoint), *argv]
    to_save = extract_aqua_resource_to_save(argv)
    try:
        runpy.run_path(str(entrypoint), run_name="__main__")
    except SystemExit as exit_:
        # Click ends every run with SystemExit, success included.
        if to_save and exit_.code in (0, None):
            save_attached_aqua_resource(to_save)
        raise
    if to_save:
        save_attached_aqua_resource(to_save)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
