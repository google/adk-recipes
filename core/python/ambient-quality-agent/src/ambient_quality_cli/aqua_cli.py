#!/usr/bin/env python3
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

"""Command-line client for the Ambient Quality Agent (AQA).

Talks to a deployed AQA agent. Typed subcommands POST to HTTP command routes
over the shared wire client in :mod:`ambient_quality_shared`, the same path the
web UI uses. ``run`` hands the turn to ``agents-cli run --mode a2a``, which
reaches the chat agent -- the agent behind the dashboard's chat -- over A2A, so
the CLI carries no A2A client of its own.

This is the CLI ``agents-cli aqua`` runs. `agents-cli` fetches this package
into a project's ``.aqua`` alongside the Terraform and the agent sources, and
invokes this file by path, so **the command surface here is the command surface
there** -- a subcommand added below is available to `agents-cli` the next time
those sources are refreshed, eliminating duplicate implementations.

Two ways to use it
------------------
* **Typed subcommands** (recommended for scripts / coding harnesses): each maps
  to one AQA action and prints **only** its structured JSON result to stdout::

      aqua_cli.py list-investigations
      aqua_cli.py investigation-stats
      aqua_cli.py schedule-investigation --wait
      aqua_cli.py get-investigation <run_id> --wait
      aqua_cli.py list-insights [--status RECURRING] [--run-id <id>]
      aqua_cli.py get-insight <insight_id> [--run-id <id>] [--no-traces]
      aqua_cli.py show-config
      aqua_cli.py list-memories
      aqua_cli.py list-agents
      aqua_cli.py dump-traces [--since 7d] [--until ...] [--output FILE]
      aqua_cli.py publish-source [AGENT_KEY] [--source-root DIR] [--dry-run]

* **Attaching an agent** -- how the deployment observes it. Interactive: each
  prints a plan and asks before it overwrites or deletes (``--yes`` skips)::

      aqua_cli.py attach [AGENT_NAME] [--lookback-days 30] [--dry-run] ...
      aqua_cli.py attach --observed-agent-resource \\
          projects/P/locations/L/reasoningEngines/ID
      aqua_cli.py detach AGENT_NAME

  With ``--observed-agent-resource``, the settings no flag gives are discovered
  from the observed engine (:mod:`ambient_quality_cli.discovery`); an attached
  agent keeps its stored values over discovered ones. Every change outside AQuA
  is a gcloud or bq command that ``attach`` prints, then runs with the user's
  credentials once confirmed:

  - for each read the deployment is denied, the command that grants it, then
    the ones a re-check calls for once a skipped read becomes possible
    (:mod:`ambient_quality_shared.grant_commands`), recorded on the attachment for
    ``detach`` to revoke;
  - when discovery reads the agent's Cloud Trace spans through a BigQuery link
    that does not exist yet, the commands that enable the Observability API
    and create the ``_Trace`` bucket and the link, whichever are missing
    (:mod:`ambient_quality_cli.trace_link`); ``detach`` leaves them;
  - the agent's update trigger -- the audit sink on its updates and its
    publisher binding on AQuA's topic -- wired to the engine as it reports
    itself (:mod:`ambient_quality_cli.update_trigger`); ``detach`` removes it.

  With ``--source-root``, ``attach`` also publishes the agent's source for
  the engine's newest revision, as ``publish-source`` does
  (:mod:`ambient_quality_cli.source_snapshot`). The engine reads the schedule
  from the attachment on every scheduler tick.

* **Free-form turn** (`run`): send one natural-language turn to the chat agent
  and stream the reply. ``--session-id`` continues a conversation::

      aqua_cli.py run "how is the agent doing?"
      aqua_cli.py run --session-id <id> "yes, run that investigation"

From a checkout, run it as ``python -m ambient_quality_cli.aqua_cli`` or by
path.

Target selection (all commands)
-------------------------------
Pass ``--aqua-resource projects/P/locations/L/reasoningEngines/ID`` (also
accepted as ``--resource``) or a full engine ``--url``; with neither, the
env-configured backend is used
(``AGENT_ENGINE_RESOURCE_ID``, or a local agent server
(``ambient_quality_agent.fast_api_app``) via ``AGENT_ADK_BASE_URL`` +
``AQA_BACKEND=adk``). Auth is Application Default
Credentials.

Agent selection
---------------
The commands that read one agent's investigations, insights, configuration,
memories or code metrics take ``--agent-key``, the key ``list-agents`` shows.
Without it, they take the key ``attach`` last recorded in
``./.aqua/attached_aqua.json`` for the AQuA the command reaches, so that the
observed agent's own directory needs no flag; ``attach`` records none in
AQuA's own checkout. With neither, or with ``--agent-key ''``, AQuA answers
for its default agent. ``run`` always reaches the default agent: ``agents-cli
run`` sends no A2A metadata to select another.

Exit codes
----------
``0`` success, ``1`` the call failed -- CLI / transport error, no JSON payload,
or a tool that reported one (an unknown run id, a denied read), ``2`` the run's
status is ``failed``, ``3`` ``--wait`` timed out before the run reached a
terminal status. ``2`` and ``3`` are specific to the two investigation
commands; every subcommand can exit ``0`` or ``1``. ``run`` is the exception:
once it reaches ``agents-cli``, it exits with ``agents-cli run``'s code.

A tool reports a handled failure in the payload without raising, so the exit
code is what separates "no results" from "the read did not work" -- both of
which otherwise print plausible JSON.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any, TypeVar

import click

if not __package__:
    # Add the parent directory to sys.path when executed directly by path,
    # ensuring ambient_quality_shared resolves.
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

from ambient_quality_cli import discovery as discovery_lib
from ambient_quality_cli import gcs as gcs_lib
from ambient_quality_cli import grants as grants_lib
from ambient_quality_cli import metrics as metrics_lib
from ambient_quality_cli import rest_client as rest_client_lib
from ambient_quality_cli import source_snapshot as source_snapshot_lib
from ambient_quality_cli import trace_link as trace_link_lib
from ambient_quality_cli import traces as traces_lib
from ambient_quality_cli import update_trigger as update_trigger_lib
from ambient_quality_shared import grant_commands
from ambient_quality_shared.agent_client import (
    AdkHttpClient,
    AgentRuntimeClient,
    build_client_from_env,
    get_adc_token,
    resolve_a2a_base_url,
)
from ambient_quality_shared.agent_key import AGENT_KEY_RE, is_storage_key
from ambient_quality_shared.attached_aqua import (
    ATTACHED_AQUA_FILE,
    normalize_target,
    resolve_recorded_agent_key,
    save_attached_aqua_fields,
)
from ambient_quality_shared.protocol import (
    AGENTS_APPLIED_ROUTE,
    AGENTS_ATTACH_ROUTE,
    AGENTS_DETACH_ROUTE,
    AGENTS_LIST_ROUTE,
    CHAT_A2A_APP,
    CONFIG_ROUTE,
    INSIGHTS_GET_ROUTE,
    INSIGHTS_LIST_ROUTE,
    INVESTIGATIONS_GET_ROUTE,
    INVESTIGATIONS_LIST_ROUTE,
    INVESTIGATIONS_SCHEDULE_ROUTE,
    INVESTIGATIONS_STATS_ROUTE,
    MEMORIES_LIST_ROUTE,
    METRICS_DESTROY_ROUTE,
    METRICS_LIST_ROUTE,
    METRICS_PUBLISH_ROUTE,
    SOURCE_COMMIT_ROUTE,
    SOURCE_ROUTE,
    SOURCE_UPLOAD_ROUTE,
    AgentKeyArgs,
    AgentSelector,
    AttachAgentArgs,
    DestroyMetricsArgs,
    GetInsightArgs,
    GetInvestigationArgs,
    GetSourceSnapshotArgs,
    ListInsightsArgs,
    PublishMetricsArgs,
    RecordAppliedArgs,
    Route,
    WindowArgs,
)
from ambient_quality_shared.terraform_state import STATE_DIR_NAME

ArgsT = TypeVar("ArgsT")
_AgentArgsT = TypeVar("_AgentArgsT", bound=AgentKeyArgs)

# Stable exit codes for caller branching without parsing output.
EXIT_OK = 0
EXIT_ERROR = 1  # Matches click.ClickException exit code.
EXIT_FAILED = 2  # Indicates investigation status is "failed".
# Returned when the --wait deadline expires before reaching a terminal status.
EXIT_TIMEOUT = 3

# Statuses indicating a completed investigation run.
_TERMINAL_STATUSES = frozenset({"done", "failed", "skipped"})

# Complete set of run statuses, used to distinguish run records from statusless error envelopes.
_RUN_STATUSES = _TERMINAL_STATUSES | {"scheduled", "pending", "running"}


# --------------------------------------------------------------------------- #
# Client construction                                                          #
# --------------------------------------------------------------------------- #
def _build_client(
    url: str | None, resource: str | None
) -> AgentRuntimeClient | AdkHttpClient:
    """Creates a client for the requested target.

    Args:
        url: Full Agent Runtime engine endpoint URL.
        resource: Engine resource name in projects/P/locations/L/reasoningEngines/ID format.

    Returns:
        Configured client instance targeting the specified or environment-configured backend.

    Raises:
        click.UsageError: If both url and resource are provided.
        click.ClickException: If the target is malformed, or none is given and
            the environment configures none.
    """
    if url and resource:
        raise click.UsageError("Pass only one of --url or --aqua-resource.")
    try:
        if url or resource:
            return AgentRuntimeClient(resource_id=resource, url=url)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    # Without an explicit target, default to the environment-configured backend.
    try:
        return build_client_from_env()
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(
            f"{exc}\nOr pass --aqua-resource "
            "projects/P/locations/L/reasoningEngines/ID."
        ) from exc


def _resolve_target_name(url: str | None, resource: str | None) -> str:
    """Resolves the name of the AQuA a command reaches.

    Args:
        url: Explicit engine endpoint URL, if provided.
        resource: Explicit engine resource name, if provided.

    Returns:
        The URL or engine resource name, or an empty string when the target is
        configured by other environment variables.
    """
    return url or resource or os.getenv("AGENT_ENGINE_RESOURCE_ID") or ""


def _echo_target(
    url: str | None, resource: str | None, agent_key: str = ""
) -> None:
    """Prints the resolved target to stderr to keep stdout reserved for JSON output.

    Args:
        url: Explicit engine endpoint URL, if provided.
        resource: Explicit engine resource name, if provided.
        agent_key: The agent the command acts on; empty for AQuA's default
            agent.
    """
    target = _resolve_target_name(url, resource) or "(env-configured)"
    # A key recorded by attach selects the agent with no flag, so name it.
    selected = f" (agent key {agent_key})" if agent_key else ""
    click.echo(f"Querying agent: {target}{selected}", err=True)


def _extract_status(payload: Any) -> str | None:
    return payload.get("status") if isinstance(payload, dict) else None


def _extract_error(payload: Any) -> str | None:
    """Extracts the error message from an error envelope.

    Differentiates API error envelopes from failed run records: run records
    contain a status field alongside investigation errors, whereas error
    envelopes lack run status entirely.

    Args:
        payload: Decoded response payload.

    Returns:
        Error message string if payload represents an error envelope, or None.
    """
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not error or _extract_status(payload) in _RUN_STATUSES:
        return None
    return str(error)


# --------------------------------------------------------------------------- #
# Shared click options + small helpers for the JSON subcommands                #
# --------------------------------------------------------------------------- #
def _add_target_options(func):
    """Attaches the --url and --aqua-resource target options to a Click command.

    Args:
        func: Click command callback to wrap.

    Returns:
        Wrapped command callback with options attached.
    """
    func = click.option(
        "--url", default=None, help="Full Agent Runtime engine URL."
    )(func)
    func = click.option(
        "--aqua-resource",
        "--resource",
        "resource",
        default=None,
        help="AQuA's engine, projects/P/locations/L/reasoningEngines/ID.",
    )(func)
    return func


def _add_agent_option(func):
    """Attaches the --agent-key option to a Click command that acts on one agent.

    Args:
        func: Click command callback to wrap.

    Returns:
        Wrapped command callback with the option attached.
    """
    return click.option(
        "--agent-key",
        default=None,
        help=(
            "The attached agent to act on, by the key list-agents shows. "
            "Default: the agent attach last recorded in ./.aqua/ for this AQuA, "
            "else AQuA's default agent, which --agent-key '' also selects."
        ),
    )(func)


def _resolve_agent_key(agent_key: str | None, *, url, resource) -> str:
    """Resolves the agent a command acts on.

    1. `--agent-key`, when given, even empty.
    2. The key `attach` recorded in `./.aqua/attached_aqua.json`, if it was
       recorded for the AQuA this command reaches: a key names an attachment
       on one AQuA only.
    3. No key, which leaves AQuA to answer for its default agent.

    Args:
        agent_key: The `--agent-key` value; None when not given.
        url: Explicit engine endpoint URL.
        resource: Explicit engine resource name.

    Returns:
        The agent key, or an empty string for AQuA's default agent.
    """
    if agent_key is not None:
        return agent_key.strip()
    return resolve_recorded_agent_key(
        pathlib.Path.cwd() / STATE_DIR_NAME, _resolve_target_name(url, resource)
    )


def _build_agent_args(args: _AgentArgsT, agent_key: str) -> _AgentArgsT:
    """Adds the selected agent to a route's request body.

    Args:
        args: The request body, of a route that takes `agent_key`.
        agent_key: The agent to act on; empty for AQuA's default agent.

    Returns:
        `args`, with `agent_key` set when an agent is selected.
    """
    if agent_key:
        args["agent_key"] = agent_key
    return args


def _build_agent_key_args(
    args_type: type[_AgentArgsT], agent_key: str
) -> _AgentArgsT | None:
    """Builds the body of a route called with nothing but the agent.

    Args:
        args_type: The route's request body type.
        agent_key: The agent to act on; empty for AQuA's default agent.

    Returns:
        The body naming the agent, or None when no agent is selected, which
        sends no body.
    """
    return args_type(agent_key=agent_key) if agent_key else None


def _fetch_command_result(
    *, url, resource, path: Route[ArgsT], payload: ArgsT | None = None
) -> Any:
    """Posts to an agent command route and returns the parsed JSON response.

    Args:
        url: Explicit engine endpoint URL.
        resource: Explicit engine resource name.
        path: Command route.
        payload: Optional request body, of the route's `TypedDict`.

    Returns:
        Decoded JSON response from the command endpoint.

    Raises:
        click.ClickException: If the network request fails or authentication errors occur.
    """
    client = _build_client(url, resource)
    selected = payload.get("agent_key", "") if isinstance(payload, dict) else ""
    _echo_target(url, resource, str(selected))
    try:
        return asyncio.run(client.post_command(path, payload))
    except Exception as exc:  # Surface auth, network, and agent errors as user-facing CLI exceptions.
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc


def _emit_json(payload: Any) -> None:
    """Formats and prints a JSON payload to stdout.

    Args:
        payload: Data structure to serialize.

    Raises:
        click.ClickException: If payload is None.
    """
    if payload is None:
        raise click.ClickException("No JSON payload found in the agent reply.")
    click.echo(json.dumps(payload, indent=2))


def _emit_result(payload: Any) -> None:
    """Prints a payload and terminates with an error exit code if it represents an error envelope.

    Emits the full envelope to stdout so programmatic consumers receive error
    details while automation checking exit codes detects the failure.

    Args:
        payload: Decoded response payload to emit.
    """
    _emit_json(payload)
    if _extract_error(payload):
        sys.exit(EXIT_ERROR)


def _emit_run_record(payload: Any, *, timed_out: bool = False) -> None:
    """Prints an investigation run record and exits with a status-mapped code.

    Args:
        payload: Run record or error payload to display.
        timed_out: Whether a polling deadline expired prior to completion.
    """
    _emit_json(payload)
    # Prioritize error envelopes over timeout checks to expose underlying API failures.
    if _extract_error(payload):
        sys.exit(EXIT_ERROR)
    status = _extract_status(payload)
    if timed_out and status not in _TERMINAL_STATUSES:
        sys.exit(EXIT_TIMEOUT)
    if status == "failed":
        sys.exit(EXIT_FAILED)


# --------------------------------------------------------------------------- #
# Polling (used by --wait)                                                     #
# --------------------------------------------------------------------------- #
async def _poll_until_terminal(
    client: AgentRuntimeClient | AdkHttpClient,
    run_id: str,
    *,
    interval: float,
    timeout: float,
    agent_key: str = "",
) -> tuple[Any, bool]:
    """Polls the investigation status endpoint until a terminal state or timeout is reached.

    Args:
        client: API client for issuing requests.
        run_id: Unique identifier of the investigation run.
        interval: Delay in seconds between polling attempts.
        timeout: Maximum duration in seconds to wait before giving up.
        agent_key: The agent the run belongs to; empty for AQuA's default
            agent.

    Returns:
        Tuple of (latest_payload, timed_out_boolean).
    """
    deadline = time.monotonic() + timeout
    while True:
        payload = await client.post_command(
            INVESTIGATIONS_GET_ROUTE,
            _build_agent_args(GetInvestigationArgs(run_id=run_id), agent_key),
        )
        # Treat error envelopes as terminal to fail immediately on permanent errors such as invalid IDs.
        if (
            payload is None
            or _extract_error(payload)
            or _extract_status(payload) in _TERMINAL_STATUSES
        ):
            return payload, False
        if time.monotonic() >= deadline:
            return payload, True
        await asyncio.sleep(interval)


def _add_wait_options(func):
    """Attaches polling parameters (--wait, --interval, --timeout) to a Click command.

    Args:
        func: Click command callback to wrap.

    Returns:
        Wrapped command callback with polling options attached.
    """
    func = click.option(
        "--wait",
        is_flag=True,
        default=False,
        help="Poll until the run reaches a terminal status (done/failed/skipped).",
    )(func)
    func = click.option(
        "--interval",
        default=30.0,
        show_default=True,
        help="Seconds between polls when --wait is set.",
    )(func)
    func = click.option(
        "--timeout",
        default=1800.0,
        show_default=True,
        help="Give up waiting after this many seconds (exit 3).",
    )(func)
    return func


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
@click.group()
def cli() -> None:
    """Command-line client for the Ambient Quality Agent (AQA)."""


def _build_a2a_run_argv(
    agents_cli: str,
    message: str,
    base_url: str,
    *,
    session_id: str | None,
    files: tuple[str, ...],
) -> list[str]:
    """Builds the `agents-cli run` argv that sends one turn to the chat agent.

    Args:
        agents_cli: Path to the `agents-cli` executable.
        message: Prompt text for the turn.
        base_url: Base URL that serves the agent's A2A routes.
        session_id: Session to continue, or None to start one.
        files: Paths of files to attach to the turn.

    Returns:
        The argv list, executable first.
    """
    argv = [
        agents_cli,
        "run",
        "--mode",
        "a2a",
        "--url",
        base_url,
        "--app-name",
        CHAT_A2A_APP,
    ]
    if session_id:
        argv += ["--session-id", session_id]
    for path in files:
        argv += ["--file", path]
    # The message goes after `--` so agents-cli never parses one that starts
    # with `-` as an option.
    argv += ["--", message]
    return argv


@cli.command("run")
@click.argument("message")
@_add_target_options
@click.option(
    "--session-id",
    default=None,
    help="Continue an existing conversation.",
)
@click.option(
    "--file",
    "files",
    multiple=True,
    type=click.Path(exists=True, readable=True),
    help="Attach a file (image / PDF / audio / ...). Repeatable.",
)
def run_prompt_cmd(
    message: str,
    *,
    url: str | None,
    resource: str | None,
    session_id: str | None,
    files: tuple[str, ...],
) -> None:
    """Send one prompt to AQuA's chat agent and stream the reply.

    The chat agent is the agent behind the dashboard's chat. The turn runs
    through ``agents-cli run --mode a2a``, so ``agents-cli`` must be on PATH;
    its output streams through unchanged and its exit code is this command's.

    \b
    Continue a conversation with --session-id, for example to approve a custom
    investigation the agent proposed:
      run --session-id <id> "yes, run it"
    """
    # The client only resolves the A2A base URL; agents-cli carries the turn.
    client = _build_client(url, resource)
    try:
        base_url, _ = resolve_a2a_base_url(client)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    agents_cli = shutil.which("agents-cli")
    if not agents_cli:
        raise click.ClickException(
            "agents-cli is not on PATH. `run` sends the turn through "
            "`agents-cli run --mode a2a`; install agents-cli and retry."
        )
    argv = _build_a2a_run_argv(
        agents_cli,
        message,
        base_url,
        session_id=session_id,
        files=files,
    )
    with subprocess.Popen(argv) as process:  # noqa: S603 - explicit argument list without shell execution
        # Ctrl-C reaches the whole process group; ignoring it here lets
        # agents-cli alone handle it and report the exit code. Ignore only after
        # the spawn: an ignored SIGINT is inherited, and agents-cli would then
        # ignore Ctrl-C too.
        previous_sigint_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            returncode = process.wait()
        finally:
            signal.signal(signal.SIGINT, previous_sigint_handler)
    # A child killed by signal N has return code -N; exit as a shell would.
    sys.exit(128 - returncode if returncode < 0 else returncode)


@cli.command("list-investigations")
@_add_target_options
@_add_agent_option
def list_investigations_cmd(*, url, resource, agent_key) -> None:
    """List the 50 most recent runs as JSON (``{"runs": [...]}``), newest last.

    The ``events`` field in each entry is empty; see
    ``get-investigation --help`` for run record fields.
    """
    _emit_result(
        _fetch_command_result(
            url=url,
            resource=resource,
            path=INVESTIGATIONS_LIST_ROUTE,
            payload=_build_agent_key_args(
                WindowArgs,
                _resolve_agent_key(agent_key, url=url, resource=resource),
            ),
        )
    )


@cli.command("investigation-stats")
@_add_target_options
@_add_agent_option
def get_investigation_stats_cmd(*, url, resource, agent_key) -> None:
    """Total every run's counters as JSON (``{"stats": {...}}``).

    Unlike ``list-investigations``, which returns one capped page, this is
    summed over every investigation the deployment ever recorded.
    """
    _emit_result(
        _fetch_command_result(
            url=url,
            resource=resource,
            path=INVESTIGATIONS_STATS_ROUTE,
            payload=_build_agent_key_args(
                WindowArgs,
                _resolve_agent_key(agent_key, url=url, resource=resource),
            ),
        )
    )


@cli.command("get-investigation")
@click.argument("run_id")
@_add_target_options
@_add_agent_option
@_add_wait_options
def get_investigation_cmd(
    run_id: str, *, url, resource, agent_key, wait, interval, timeout
) -> None:
    """Look up one run by RUN_ID as JSON; with --wait, poll until terminal.

    \b
    Fields of a run record:
      status        scheduled (waiting for its delayed trigger), pending,
                    running, done, failed, or skipped (duplicate trigger)
      trigger_type  manual, custom (selected conversations), scheduled, or
                    task_fire (delayed run after an agent update)
      due_at        when a scheduled run's delayed trigger is due
      events        progress log; empty in ``list-investigations``

    \b
    A run writes no report. To view findings from a finished run, call:
      list-insights --run-id RUN_ID
    """
    agent_key = _resolve_agent_key(agent_key, url=url, resource=resource)
    client = _build_client(url, resource)
    _echo_target(url, resource, agent_key)
    try:
        if wait:
            payload, timed_out = asyncio.run(
                _poll_until_terminal(
                    client,
                    run_id,
                    interval=interval,
                    timeout=timeout,
                    agent_key=agent_key,
                )
            )
        else:
            payload = asyncio.run(
                client.post_command(
                    INVESTIGATIONS_GET_ROUTE,
                    _build_agent_args(
                        GetInvestigationArgs(run_id=run_id), agent_key
                    ),
                )
            )
            timed_out = False
    except Exception as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
    _emit_run_record(payload, timed_out=timed_out)


@cli.command("schedule-investigation")
@_add_target_options
@_add_agent_option
@_add_wait_options
def schedule_investigation_cmd(
    *, url, resource, agent_key, wait, interval, timeout
) -> None:
    """Start a run and print the new run record as JSON.

    With --wait, poll the new run_id until it reaches a terminal status. See
    ``get-investigation --help`` for run record fields.
    """
    agent_key = _resolve_agent_key(agent_key, url=url, resource=resource)
    client = _build_client(url, resource)
    _echo_target(url, resource, agent_key)
    try:
        payload = asyncio.run(
            client.post_command(
                INVESTIGATIONS_SCHEDULE_ROUTE,
                _build_agent_key_args(AgentKeyArgs, agent_key),
            )
        )
        timed_out = False
        run_id = payload.get("run_id") if isinstance(payload, dict) else None
        if wait and run_id:
            payload, timed_out = asyncio.run(
                _poll_until_terminal(
                    client,
                    run_id,
                    interval=interval,
                    timeout=timeout,
                    agent_key=agent_key,
                )
            )
    except Exception as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
    _emit_run_record(payload, timed_out=timed_out)


@cli.command("list-insights")
@click.option(
    "--status",
    default="",
    help="Optional lifecycle filter: NEW, RECURRING, or RESOLVED (default: all).",
)
@click.option("--run-id", default="", help="Only issues seen in this sweep.")
# Defaults to None so omitting the flag remains distinct from --root-cause=false
# in the backend tri-state filter.
@click.option(
    "--root-cause",
    "root_cause",
    type=click.Choice(["true", "false"], case_sensitive=False),
    default=None,
    help="Only issues with (or without) a recorded root cause (default: both).",
)
@click.option(
    "--page-token",
    default="",
    help="Token from a prior call's next_page_token.",
)
@_add_target_options
@_add_agent_option
def list_insights_cmd(
    *, status, run_id, root_cause, page_token, url, resource, agent_key
) -> None:
    """List durable quality insights as JSON (``{"insights": [...], ...}``).

    Returns a paginated triage list of deduplicated agent issues, most recently
    seen first (30 per page). Insights an operator dismissed or merged into
    another are omitted. Pass ``next_page_token`` to ``--page-token`` to fetch
    subsequent pages, and call ``get-insight`` for occurrences and rubric
    evidence on a specific issue.

    ``total`` counts all matching insights across all pages, while
    ``conversations`` counts distinct conversations across those insights. An
    entry's ``trace_count`` sums conversations across occurrences, so a
    conversation observed in two runs counts twice. With ``--run-id``, an
    entry's summary fields still describe the insight's full history.
    """
    args = ListInsightsArgs()
    if status:
        args["status"] = status
    if run_id:
        args["run_id"] = run_id
    if root_cause is not None:
        args["has_root_cause"] = root_cause
    if page_token:
        args["page_token"] = page_token
    _emit_result(
        _fetch_command_result(
            url=url,
            resource=resource,
            path=INSIGHTS_LIST_ROUTE,
            payload=_build_agent_args(
                args, _resolve_agent_key(agent_key, url=url, resource=resource)
            ),
        )
    )


@cli.command("get-insight")
@click.argument("insight_id")
@click.option(
    "--run-id",
    default="",
    help="Only this sweep's occurrence (default: full history).",
)
@click.option(
    "--no-traces",
    is_flag=True,
    default=False,
    help=(
        "Omit rubric conversation traces and occurrence trajectories "
        "(Cloud Trace links) to reduce payload size."
    ),
)
@click.option(
    "--page-token",
    default="",
    help="Token from a prior call's next_page_token.",
)
@_add_target_options
@_add_agent_option
def get_insight_cmd(
    insight_id: str,
    *,
    run_id,
    no_traces,
    page_token,
    url,
    resource,
    agent_key,
) -> None:
    """Get one insight's occurrences and rubric evidence as JSON.

    Returns the full occurrence history (newest first, paginated); ``--run-id``
    narrows results to a single sweep. Top-level ``root_causes`` contains the
    latest diagnosis for each occurrence across the entire insight, regardless of
    pagination or ``--run-id``.

    \b
    Each root-cause edit has:
      path        file path relative to the repository root
      start_line  first line (1-based, inclusive)
      end_line    last line (1-based, inclusive)
      before      code at specified line range before proposed edit
      after       proposed replacement code
      rationale   explanation for proposed edit

    Line numbers and ``before`` refer to the root cause's ``agent_revision``
    snapshot.
    """
    args = GetInsightArgs(insight_id=insight_id)
    if run_id:
        args["run_id"] = run_id
    if no_traces:
        args["include_traces"] = False
    if page_token:
        args["page_token"] = page_token
    _emit_result(
        _fetch_command_result(
            url=url,
            resource=resource,
            path=INSIGHTS_GET_ROUTE,
            payload=_build_agent_args(
                args, _resolve_agent_key(agent_key, url=url, resource=resource)
            ),
        )
    )


@cli.command("show-config")
@_add_target_options
@_add_agent_option
def show_config_cmd(*, url, resource, agent_key) -> None:
    """Show the effective AQA configuration as JSON."""
    _emit_result(
        _fetch_command_result(
            url=url,
            resource=resource,
            path=CONFIG_ROUTE,
            payload=_build_agent_key_args(
                AgentKeyArgs,
                _resolve_agent_key(agent_key, url=url, resource=resource),
            ),
        )
    )


@cli.command("list-memories")
@_add_target_options
@_add_agent_option
def list_memories_cmd(*, url, resource, agent_key) -> None:
    """List what the developer asked AQuA to remember about the agent, newest first.

    Read-only: memories are created only by the developer, in AQuA's chat.
    Exits with an error unless the memories were read, so an empty list
    always means nothing is remembered.
    """
    payload = _fetch_command_result(
        url=url,
        resource=resource,
        path=MEMORIES_LIST_ROUTE,
        payload=_build_agent_key_args(
            AgentKeyArgs,
            _resolve_agent_key(agent_key, url=url, resource=resource),
        ),
    )
    _emit_result(payload)
    if not isinstance(payload, dict) or payload.get("available") is not True:
        sys.exit(EXIT_ERROR)


# --------------------------------------------------------------------------
# metrics: the code-metric library the deployment scores with
# --------------------------------------------------------------------------


def _post_metrics_route(
    path: Route[ArgsT], payload: ArgsT | None, *, url, resource
) -> dict[str, Any]:
    """Posts to a metrics route and returns its answer, refusing an error envelope.

    Args:
        path: The metrics route.
        payload: Request body, of the route's `TypedDict`.
        url: Deployment HTTP endpoint, if connecting directly.
        resource: Agent Runtime resource name.

    Returns:
        The route's answer.

    Raises:
        click.ClickException: If the request fails or the engine answers with
            an error, such as a library it refuses or no metrics bucket.
    """
    result = _fetch_command_result(
        url=url, resource=resource, path=path, payload=payload
    )
    if not isinstance(result, dict):
        raise click.ClickException(f"Unexpected answer from {path}: {result!r}")
    if error := _extract_error(result):
        raise click.ClickException(error)
    return result


@cli.group("metrics")
def metrics_group() -> None:
    """Publish, inspect and delete the code-metric library."""


@metrics_group.command("publish")
@click.argument(
    "directory",
    default="tests/eval",
    type=click.Path(file_okay=False, path_type=pathlib.Path),
)
@click.option(
    "--dry-run", is_flag=True, help="Validate and plan, upload nothing."
)
@click.option("--yes", is_flag=True, help="Do not ask before deleting.")
@_add_target_options
@_add_agent_option
def publish_metrics_cmd(
    directory, dry_run, yes, *, url, resource, agent_key
) -> None:
    """Validate DIRECTORY on the deployment and mirror it into its metric library.

    The deployment validates the library with the loader that runs it, and
    writes it with its own credentials.
    """
    try:
        files = metrics_lib.encode_files(metrics_lib.read_directory(directory))
    except metrics_lib.ValidationError as exc:
        raise click.ClickException(f"Refusing to publish: {exc}") from exc

    agent_key = _resolve_agent_key(agent_key, url=url, resource=resource)
    plan = _post_metrics_route(
        METRICS_PUBLISH_ROUTE,
        _build_agent_args(
            PublishMetricsArgs(files=files, dry_run=True), agent_key
        ),
        url=url,
        resource=resource,
    )
    for line in metrics_lib.render_report_lines(plan.get("report") or {}):
        click.echo(line)
    click.echo(f"\n{plan.get('location', '')}")
    for relative in plan.get("uploads") or []:
        click.echo(f"  upload  {relative}")
    deletions = plan.get("deletions") or []
    for obsolete in deletions:
        click.echo(f"  delete  {obsolete}")
    # The default agent's publish replaces the instance's library it read.
    inherited = plan.get("inherited_deletions") or []
    if inherited:
        click.echo(
            "The instance's library, which this agent scores with while its "
            "own is empty:"
        )
    for obsolete in inherited:
        click.echo(f"  delete  {obsolete}")
    if dry_run:
        click.echo("\nDry run: nothing was uploaded.")
        return

    # Require operator confirmation before deleting remote objects to prevent accidental removal of active metrics.
    if deletions and not yes:
        click.confirm(
            f"\nDelete {len(deletions)} object(s) not present in {directory}?",
            abort=True,
        )
    if inherited and not yes:
        click.confirm(
            f"\nDelete the instance's library ({len(inherited)} object(s)), "
            "which this agent's replaces?",
            abort=True,
        )

    published = _post_metrics_route(
        METRICS_PUBLISH_ROUTE,
        _build_agent_args(
            PublishMetricsArgs(files=files, dry_run=False), agent_key
        ),
        url=url,
        resource=resource,
    )
    deleted = [
        *(published.get("deletions") or []),
        *(published.get("inherited_deletions") or []),
    ]
    click.echo(
        f"\nPublished {len(published.get('uploads') or [])} object(s), "
        f"deleted {len(deleted)}."
    )


@metrics_group.command("list")
@_add_target_options
@_add_agent_option
def list_metrics_cmd(*, url, resource, agent_key) -> None:
    """List and validate the metrics the deployment scores with."""
    result = _fetch_command_result(
        url=url,
        resource=resource,
        path=METRICS_LIST_ROUTE,
        payload=_build_agent_key_args(
            AgentKeyArgs,
            _resolve_agent_key(agent_key, url=url, resource=resource),
        ),
    )
    if not isinstance(result, dict):
        raise click.ClickException(f"Unexpected answer: {result!r}")
    if "location" not in result and (error := _extract_error(result)):
        raise click.ClickException(error)
    click.echo(str(result.get("location", "")))
    if result.get("inherited"):
        click.echo(
            "  (the instance's library, which the default agent scores with "
            "while its own is empty)"
        )
    files = result.get("files") or []
    if not files:
        click.echo("  (empty -- this deployment has nothing to score with)")
        return
    for item in files:
        click.echo(f"  {item.get('path', '')}  {item.get('size', '?')}B")
    # A broken library still lists its files, then says why it does not load.
    if error := _extract_error(result):
        raise click.ClickException(error)
    click.echo("")
    for line in metrics_lib.render_report_lines(result.get("report") or {}):
        click.echo(line)


@metrics_group.command("destroy")
@click.option(
    "--apply", "apply_", is_flag=True, help="Delete. Plans otherwise."
)
@_add_target_options
@_add_agent_option
def destroy_metrics_cmd(apply_, *, url, resource, agent_key) -> None:
    """Delete the published metric library. Plans by default."""
    result = _post_metrics_route(
        METRICS_DESTROY_ROUTE,
        _build_agent_args(
            DestroyMetricsArgs(dry_run=not apply_),
            _resolve_agent_key(agent_key, url=url, resource=resource),
        ),
        url=url,
        resource=resource,
    )
    deletions = result.get("deletions") or []
    if not deletions:
        # Treat an already-empty library as successfully destroyed.
        click.echo(f"{result.get('location', '')} is already empty.")
        return
    for name in deletions:
        click.echo(f"  {'delete' if apply_ else 'would delete'}  {name}")
    if not apply_:
        click.echo(
            f"\n{len(deletions)} object(s). Re-run with --apply to delete them."
        )
        return
    click.echo(
        f"\nDeleted {len(deletions)} object(s). This deployment now has "
        "nothing to score with, so investigations run session review alone "
        "until you publish again."
    )


# --------------------------------------------------------------------------
# dump-traces: the spans the deployment exported, as a portable archive
# --------------------------------------------------------------------------

# Read __version__ directly from source file because the agent package is not importable here.
_VERSION_SOURCE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "ambient_quality_agent"
    / "__init__.py"
)
_VERSION_RE = re.compile(
    r"^__version__\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE
)

_PRIVACY_WARNING = """\
======================================================================
 PRIVACY: this archive contains conversation content. Spans carry user
 turns, model replies and tool arguments verbatim. Until now they sat
 in a bucket inside the project that produced them, reachable only by
 the module's trace_readers; this file is what takes them out of it.
 Grant access by adding people to trace_readers instead of passing the
 archive around, and handle it as you would the conversations.
======================================================================\
"""

_CAPTURE_VAR = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"

# Modes that attach conversation content directly to spans.
_SPAN_BEARING_CAPTURE_MODES = frozenset({"SPAN_ONLY", "SPAN_AND_EVENT"})

# Warns about current deployment configuration; spans in the dump window may reflect earlier settings.
_NO_CAPTURE_NOTICE = (
    "The deployment is not capturing conversation content "
    f"({_CAPTURE_VAR} is {{mode}}): spans exported under that setting replay "
    "without turns."
)

# In EVENT_ONLY mode, content is recorded in log events rather than span attributes, leaving span archives empty.
_EVENT_ONLY_NOTICE = (
    "The deployment captures conversation content onto log records rather than "
    f"spans ({_CAPTURE_VAR} is EVENT_ONLY): this archive holds spans, so it "
    "replays without turns. SPAN_ONLY or SPAN_AND_EVENT puts the turns where "
    "the dump can reach them."
)


def _read_cli_version() -> str:
    """Reads this source tree's version, which hatch takes from the agent package.

    Returns:
        Version string, or "unknown" if unreadable.
    """
    try:
        found = _VERSION_RE.search(_VERSION_SOURCE.read_text(encoding="utf-8"))
    except OSError:
        return "unknown"
    return found.group(1) if found else "unknown"


def _extract_engine_project(engine: str) -> str:
    """Extracts the project ID from an Agent Runtime resource name.

    Args:
        engine: Resource identifier in `projects/{project}/...` format.

    Returns:
        Extracted project ID, or an empty string if format does not match.
    """
    parts = engine.split("/")
    return parts[1] if len(parts) > 1 and parts[0] == "projects" else ""


def _extract_engine_location(engine: str) -> str:
    """Extracts the region from an Agent Runtime resource name.

    The dump templates it into the archive README, where `bq mk` and `bq load`
    must name the same location for the load to succeed.

    Args:
        engine: Resource identifier in `projects/{p}/locations/{l}/...` format.

    Returns:
        Extracted region, or an empty string if the format does not match.
    """
    parts = engine.split("/")
    return parts[3] if len(parts) > 3 and parts[2] == "locations" else ""


def _fetch_deployment_config(url, resource) -> dict[str, Any]:
    """Fetches deployment configuration for the manifest if reachable.

    Recording configuration helps interpret the dumped spans (e.g. active metrics
    and thresholds). If the deployment is unreachable, records the error in the
    manifest without aborting the trace dump.

    Args:
        url: Deployment HTTP endpoint, if connecting directly.
        resource: Agent Runtime resource name.

    Returns:
        Dictionary with `config` payload or `config_error` details.
    """
    try:
        # Wrapped in try-except because client construction fails if no target is configured.
        client = _build_client(url, resource)
        payload = asyncio.run(client.post_command(CONFIG_ROUTE))
        # The route answers `{"config": {...}}`; keep the manifest one level flat.
        if isinstance(payload, dict) and "config" in payload:
            payload = payload["config"]
        return {"config": payload}
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        click.echo(
            f"Could not read the deployment's configuration ({reason}). "
            "Recording that in the manifest and carrying on.",
            err=True,
        )
        return {"config": None, "config_error": reason}


def _resolve_content_capture_notice(
    manifest_extra: dict[str, Any],
) -> str | None:
    """Generates a warning if the deployment capture mode omits content from spans.

    Args:
        manifest_extra: Manifest dictionary containing the collected `config`
            payload.

    Returns:
        Warning message if spans lack conversation content, or None if capture
        is enabled or deployment configuration was unreachable.
    """
    config = manifest_extra.get("config")
    if not isinstance(config, dict) or "content_capture_mode" not in config:
        return None
    mode = str(config["content_capture_mode"]).strip().upper()
    if mode in _SPAN_BEARING_CAPTURE_MODES:
        return None
    if mode == "EVENT_ONLY":
        return _EVENT_ONLY_NOTICE
    # OpenTelemetry defaults to NO_CONTENT when unset.
    return _NO_CAPTURE_NOTICE.format(mode=mode or "unset, so NO_CONTENT")


def _build_default_output_path(
    time_range: traces_lib.TimeRange,
) -> pathlib.Path:
    """Generates the default zip archive filename for a time range.

    Args:
        time_range: Requested time window.

    Returns:
        Default destination Path.
    """
    return pathlib.Path(
        f"aqa-traces-{time_range.since:%Y%m%dT%H%M%SZ}"
        f"-{time_range.until:%Y%m%dT%H%M%SZ}.zip"
    )


@cli.command("dump-traces")
@click.option(
    "--since",
    default="7d",
    show_default=True,
    help=(
        "Start of the range, in UTC: a duration back from now (7d, 36h, 90m) "
        "or an ISO date/datetime (2026-09-01, 2026-09-01T12:00:00). A value "
        "without a zone is read as UTC, not as local time."
    ),
)
@click.option(
    "--until",
    default="",
    help="End of the range, in UTC, same formats as --since. Defaults to now.",
)
@click.option(
    "--output",
    default=None,
    type=click.Path(dir_okay=False, path_type=pathlib.Path),
    help="Zip to write. Defaults to aqa-traces-<since>-<until>.zip here.",
)
@_add_target_options
def dump_traces_cmd(since, until, output, *, url, resource) -> None:
    """Collects exported OpenTelemetry spans into a portable zip archive.

    Resolves the traces bucket from `.aqua/` Terraform state so spans can be
    retrieved even when the deployment is offline. Timestamps are evaluated in UTC.
    The resulting archive contains NDJSON span records, BigQuery schema, manifest
    metadata, and replay documentation.
    """
    now = dt.datetime.now(dt.UTC)
    try:
        time_range = traces_lib.TimeRange(
            traces_lib.parse_moment(since, now=now),
            traces_lib.parse_moment(until, now=now) if until else now,
        )
        if time_range.since > time_range.until:
            raise traces_lib.DumpError(
                f"--since ({_format_timestamp(time_range.since)}) is after --until "
                f"({_format_timestamp(time_range.until)})."
            )
        bucket = traces_lib.resolve_traces_bucket(
            pathlib.Path.cwd() / STATE_DIR_NAME
        )
    except traces_lib.DumpError as exc:
        raise click.ClickException(str(exc)) from exc

    destination = output or _build_default_output_path(time_range)
    click.echo(
        f"Dumping gs://{bucket}/{traces_lib.SPAN_OBJECT_PREFIX} "
        f"from {_format_timestamp(time_range.since)} to {_format_timestamp(time_range.until)} "
        f"into {destination}.",
        err=True,
    )

    engine = url or resource or os.getenv("AGENT_ENGINE_RESOURCE_ID") or ""
    # Fetch deployment config before listing spans so progress output remains uninterrupted.
    click.echo(
        f"Collecting the effective config for the manifest from: "
        f"{engine or '(env-configured)'}",
        err=True,
    )
    extra = {
        "cli_version": _read_cli_version(),
        "generated_at": _format_timestamp(now),
        "engine": {
            "resource": engine,
            "project": _extract_engine_project(engine),
        },
        **_fetch_deployment_config(url, resource),
    }

    token = get_adc_token()
    try:
        names = traces_lib.list_span_objects(bucket, token, time_range.widen())
    except gcs_lib.GcsError as exc:
        raise click.ClickException(str(exc)) from exc
    if not names:
        # An empty result is valid; the archive will record the requested query.
        click.echo(
            f"No exported spans in that range. gs://{bucket} exists but holds "
            "no telemetry for it -- check the range, and check that the "
            "deployment exports spans at all.",
            err=True,
        )
    else:
        click.echo(f"{len(names)} object(s) to read.", err=True)

    try:
        result = traces_lib.write_dump(
            output=destination,
            bucket=bucket,
            token=token,
            time_range=time_range,
            object_names=names,
            manifest_extra=lambda _: extra,
            progress=lambda line: click.echo(line, err=True),
            location=_extract_engine_location(engine),
        )
    except (traces_lib.DumpError, gcs_lib.GcsError, OSError) as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc

    if result.spans:
        click.secho(_PRIVACY_WARNING, fg="yellow", bold=True, err=True)
        notice = _resolve_content_capture_notice(extra)
        if notice:
            click.secho(notice, fg="yellow", err=True)
    click.echo(f"Wrote {destination}.", err=True)
    _emit_json(
        {
            "output": str(destination),
            "bucket": bucket,
            "requested_range": time_range.to_json(),
            "objects": result.objects_read,
            "spans": result.spans,
            "spans_by_agent": dict(sorted(result.spans_by_agent.items())),
        }
    )


def _format_timestamp(moment: dt.datetime) -> str:
    """Formats a UTC datetime in ISO 8601 format for logs and manifests.

    Args:
        moment: Datetime to format.

    Returns:
        Formatted UTC timestamp string.
    """
    return f"{moment.astimezone(dt.UTC):%Y-%m-%dT%H:%M:%SZ}"


# --------------------------------------------------------------------------
# attach: how the deployment observes an agent
# --------------------------------------------------------------------------
#
# All three commands go through the deployment's command routes. The CLI names
# the fields to change, and the engine merges them over what is stored,
# validates the result the way the runtime does, and stores it. The CLI needs no
# access to the jobs bucket and carries no copy of the allowed values or the
# defaults.
#
# The update trigger is the one part that lives outside the engine: an audit
# log sink in the observed agent's project that acts on two of the stored
# fields. The engine says both which values to wire and the topic to wire them
# to, so `attach` needs nothing of AQuA but the resource name it called, and
# runs the gcloud commands of `update_trigger` with the user's credentials.


def _add_attach_options(func):
    """Adds the flags that set how an agent is observed.

    There is one flag per stored field, because `attach` is the only writer: a
    field with no flag could not be changed at all. The deployment checks the
    values, so the CLI keeps no choice lists that could drift from it.

    Args:
        func: Click command function to decorate.

    Returns:
        The decorated command function.
    """
    options = [
        click.option(
            "--deployment-name",
            default=None,
            help="The observed agent's deployment, as agents-cli knows it.",
        ),
        click.option(
            "--default/--no-default",
            "default_",
            default=None,
            help="Answer requests that name no agent.",
        ),
        click.option(
            "--telemetry-source",
            default=None,
            help=(
                "Where the telemetry is read from: big_query, cloud_ops or "
                "cloud_logging. Without it, a table in only one source's "
                "format is read as that source."
            ),
        ),
        click.option(
            "--telemetry-dataset", default=None, help="BigQuery dataset."
        ),
        click.option(
            "--telemetry-table", default=None, help="Table or view in it."
        ),
        click.option(
            "--telemetry-location",
            default=None,
            help=(
                "Its BigQuery location. Without it, the table's, once AQuA "
                "can read the table."
            ),
        ),
        click.option(
            "--telemetry-payload-bucket",
            default=None,
            help=(
                "Bucket the agent uploads message content to, checked and "
                "granted before the agent has telemetry rows."
            ),
        ),
        click.option(
            "--quality-analysis-mode",
            default=None,
            help="How sessions are scored: session_review or eval_service.",
        ),
        click.option(
            "--multi-turn-metric",
            "multi_turn_metrics",
            multiple=True,
            help=(
                "Prebuilt conversation metric for eval_service mode "
                "(task_success, tool_use_quality, trajectory_quality). "
                "Repeatable; replaces the set. Published code metrics need "
                "no flag; session review scores them."
            ),
        ),
        click.option(
            "--single-turn-metric",
            "single_turn_metrics",
            multiple=True,
            help=(
                "Prebuilt turn metric for eval_service mode "
                "(final_response_quality, hallucination, safety, "
                "tool_use_quality). Repeatable; replaces the set. Published "
                "code metrics need no flag; session review scores them."
            ),
        ),
        click.option(
            "--lookback-days",
            type=int,
            default=None,
            help="Days of telemetry an investigation considers.",
        ),
        click.option(
            "--evaluation-cap",
            type=int,
            default=None,
            help="Most records one investigation pulls in.",
        ),
        click.option(
            "--auto-resolve-days",
            type=int,
            default=None,
            help="Resolve an insight after this long unseen.",
        ),
        click.option(
            "--verification/--no-verification",
            "insights_verification_enabled",
            default=None,
            help="Check candidate issues against their own traces.",
        ),
        click.option(
            "--enforce-verification/--no-enforce-verification",
            "insights_verification_enforced",
            default=None,
            help="Withhold a candidate a negative verdict rejected.",
        ),
        click.option(
            "--revision-delay-seconds",
            type=int,
            default=None,
            help="Wait this long after the agent is updated before investigating.",
        ),
        click.option(
            "--schedule",
            default=None,
            help=(
                "Cron for the scheduled sweep, in UTC; daily at 12:00 by "
                "default. Run for the agent the deployment investigates, up "
                "to 15 minutes late, from the next 15-minute tick."
            ),
        ),
        click.option(
            "--scheduled-trigger/--no-scheduled-trigger",
            "scheduled_trigger_enabled",
            default=None,
            help="Whether that schedule is live, from the next 15-minute tick.",
        ),
        click.option(
            "--observed-project",
            default=None,
            help=(
                "The project the agent runs in, when it is not AQuA's: its "
                "telemetry is read there, and its update trigger created there. "
                "Discovered from --observed-agent-resource."
            ),
        ),
        click.option(
            "--observed-agent-resource",
            default=None,
            help=(
                "The observed engine, projects/P/locations/L/reasoningEngines/ID. "
                "Discovery reads the agent's settings from it, and its updates "
                "trigger an investigation."
            ),
        ),
    ]
    for option in reversed(options):
        func = option(func)
    return func


_FLAG_TO_FIELD = {
    "deployment_name": "observed_deployment_name",
    "default_": "default",
    "telemetry_source": "telemetry_ingestion_source",
    "observed_project": "observed_project_id",
    "lookback_days": "data_lookback_window",
    "evaluation_cap": "data_evaluation_cap",
    "auto_resolve_days": "insights_auto_resolve_days",
    "revision_delay_seconds": "agent_revision_trigger_delay_seconds",
    "schedule": "investigation_schedule",
}
"""Flags whose name differs from the field they set; the rest map one to one."""

_LIST_FLAGS = frozenset({"multi_turn_metrics", "single_turn_metrics"})
"""Repeatable flags, sent as lists."""

_DISCOVERY_WINDOW_DAYS = 7
"""How far back discovery counts telemetry rows without `--lookback-days`."""


def _extract_requested_fields(flags: dict[str, Any]) -> dict[str, Any]:
    """Extracts the fields the caller set from the parsed attach flags.

    Args:
        flags: Parsed flag values keyed by Click parameter name.

    Returns:
        The set fields keyed by field name. Fields left out keep their current
        value.

    Raises:
        click.BadParameter: If `--observed-agent-resource` is not an engine
            resource name.
    """
    fields: dict[str, Any] = {}
    for flag, value in flags.items():
        if flag == "observed_agent_resource":
            if value is not None:
                try:
                    engine = discovery_lib.parse_engine_resource(value)
                except discovery_lib.DiscoveryError as exc:
                    raise click.BadParameter(
                        str(exc), param_hint="--observed-agent-resource"
                    ) from exc
                # The audit sink's filter is composed from the bare id. Its
                # project, where the sink goes, is discovered as an id.
                fields["observed_engine_id"] = engine.engine_id
        elif flag in _LIST_FLAGS:
            # Click gives an empty tuple for a repeatable option nobody passed,
            # and that means the list is unchanged.
            if value:
                fields[flag] = list(value)
        elif value is not None:
            fields[_FLAG_TO_FIELD.get(flag, flag)] = value
    return fields


def _fetch_agents_result(
    path: Route[ArgsT], payload: ArgsT | None, *, url, resource
) -> dict[str, Any]:
    """Posts to an `agents/*` route and fails the command on an error envelope.

    Args:
        path: Command route.
        payload: Optional request body, of the route's `TypedDict`.
        url: Explicit engine endpoint URL.
        resource: Explicit engine resource name.

    Returns:
        Decoded JSON response from the route.

    Raises:
        click.ClickException: If the route reports an error or returns no object.
    """
    result = _fetch_command_result(
        url=url, resource=resource, path=path, payload=payload
    )
    if error := _extract_error(result):
        raise click.ClickException(error)
    if not isinstance(result, dict):
        raise click.ClickException("No JSON payload found in the agent reply.")
    return result


def _format_value(value: Any) -> str:
    """Formats a field value for the attach plan.

    Args:
        value: Field value from the plan.

    Returns:
        Display text for the value.
    """
    if value is None:
        return "(deployment default)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "(none)"
    return str(value) if value != "" else '""'


def _render_plan_lines(
    plan: dict[str, Any], *, default_agent: str = ""
) -> list[str]:
    """Renders an attach plan as the lines shown before confirming.

    Args:
        plan: Dry-run response from the attach route.
        default_agent: The agent an engine that investigates only its default
            agent investigates, when the plan's agent is not it.

    Returns:
        Lines to print, in order.
    """
    lines = [str(plan.get("object", ""))]
    if not plan.get("attached"):
        lines.append("  (not attached yet -- starting from the defaults)")
    changes = plan.get("changes") or []
    if not changes:
        lines.append("  (no change)")
    reasons = {f["field"]: f["reason"] for f in plan.get("filled") or []}
    for change in changes:
        line = (
            f"  {change['field']}: {_format_value(change['before'])} -> "
            f"{_format_value(change['after'])}"
        )
        if reason := reasons.get(change["field"]):
            line += f" ({reason})"
        lines.append(line)
    for adjusted in plan.get("adjusted") or []:
        lines.append(
            f"\n{adjusted['field']}: asked for {_format_value(adjusted['requested'])}, "
            f"the deployment stores {_format_value(adjusted['stored'])}."
        )
    if _is_ignored_by_triggers(plan):
        lines.append(
            f"\nThis deployment investigates {default_agent or 'another agent'}, "
            f"not {plan.get('agent_name')}. An attachment for any other agent is "
            "stored, but this engine does not read it.\n"
            f"{_render_watch_remedy(plan, default_agent)}"
        )
    return lines


def _is_ignored_by_triggers(plan: dict[str, Any]) -> bool:
    """Checks whether no trigger of the deployment investigates the plan's agent.

    An engine that reports `triggers_name_agent` investigates each agent by
    its own triggers. One that does not investigates its default agent whatever
    trigger fires, and reads no other attachment.

    Args:
        plan: Dry-run response from the attach route.

    Returns:
        Whether the engine investigates only its default agent, and the plan's
        agent is not it.
    """
    return not plan.get("triggers_name_agent") and not plan.get("watched")


def _fetch_default_agent(target: dict[str, Any]) -> str:
    """Fetches the default agent, from the deployment's agent listing.

    An engine that investigates only its default agent investigates this one,
    the default attached agent.

    Args:
        target: The `url`, `resource` and `user_id` of the deployment.

    Returns:
        The agent's name; empty when the listing names none or fails, since
        the name only makes a message clearer.
    """
    try:
        listing = _fetch_agents_result(AGENTS_LIST_ROUTE, None, **target)
    except click.ClickException:
        return ""
    return str(listing.get("default_agent") or "")


def _render_watch_remedy(plan: dict[str, Any], default_agent: str) -> str:
    """Renders how to make an engine that ignores the agent investigate it.

    An engine that reports `triggers_name_agent` investigates every attached
    agent. One that does not investigates its default attached agent, which
    `--default` changes.

    Args:
        plan: Dry-run response from the attach route.
        default_agent: The agent the deployment investigates; empty if unknown.

    Returns:
        One sentence, without a trailing newline.
    """
    name = plan.get("agent_name")
    watched = default_agent or "the agent it investigates now"
    upgrade = (
        "Upgrading AQuA to a release that investigates every attached agent "
        f"lets it investigate {name} beside {watched}."
    )
    return (
        f"{upgrade} Otherwise, to investigate {name} here instead, attach it "
        f"with --default; {watched} stays attached, but is no longer "
        "investigated."
    )


_ACCESS_STATUS_LABELS = {
    "passed": "ok",
    "empty": "empty",
    "missing": "MISSING",
    "mismatch": "MISMATCH",
    "denied": "DENIED",
    "error": "error",
    "skipped": "skipped",
}
"""How each access check status is shown; the capitals are the ones that need
the operator to act."""


def _render_access_lines(access: dict[str, Any]) -> list[str]:
    """Renders the deployment's access check as lines of the attach plan.

    Args:
        access: The plan's `access` section: `service_account` and `checks`.

    Returns:
        Lines to print, in order.
    """
    account = access.get("service_account") or "AQuA's credentials"
    lines = [f"\nReads, as {account}:"]
    checks = access.get("checks") or []
    width = max((len(str(c.get("name", ""))) for c in checks), default=0)
    for check in checks:
        status = str(check.get("status", ""))
        label = _ACCESS_STATUS_LABELS.get(status, status)
        target = f" {check['target']}" if check.get("target") else ""
        lines.append(
            f"  {str(check.get('name', '')).ljust(width)}  {label}{target}"
        )
        if check.get("detail"):
            lines.append(f"  {' ' * width}  {check['detail']}")
    denied = [
        c.get("target") or c.get("name")
        for c in checks
        if c.get("status") == "denied"
    ]
    if denied:
        lines.append(
            f"\nInvestigations cannot read {', '.join(str(d) for d in denied)} until "
            f"{account} is granted read access."
        )
    return lines


def _echo_access_check(
    access: dict[str, Any],
) -> tuple[list[grant_commands.Grant], list[str]]:
    """Prints the access check and the grant commands for its denied reads.

    Args:
        access: The plan's `access` section; empty when nothing was checked.

    Returns:
        Tuple of (the grant commands printed, the denied targets with no
        command).
    """
    for line in _render_access_lines(access) if access else []:
        click.echo(line)
    grants, ungranted = grant_commands.build_grants(access)
    for line in grants_lib.render_grant_lines(
        grants, ungranted, account=str(access.get("service_account") or "")
    ):
        click.echo(line)
    return grants, ungranted


def _validate_access(access: dict[str, Any]) -> str:
    """Checks that the telemetry an attach points at exists and can be queried.

    A table with no rows, or one AQuA may not read yet, does not stop the attach:
    a new agent may have had no traffic, and a grant can follow. A schema
    mismatch, or a `table` or `rows` read that fails otherwise, does:
    investigations run the same queries, so they would fail the same way.

    Args:
        access: The plan's `access` section.

    Returns:
        The reason the attach is refused, or an empty string if it is not.
    """
    checks = access.get("checks") or []
    missing = [c for c in checks if c.get("status") == "missing"]
    if missing:
        targets = ", ".join(
            str(c.get("target") or c.get("name")) for c in missing
        )
        return (
            f"Not attaching: {targets} does not exist, so there is nothing to "
            "investigate. For an agents-cli project, `agents-cli infra "
            "single-project --apply` creates the telemetry sink and dataset; "
            "`agents-cli deploy` alone creates neither."
        )
    mismatch = next((c for c in checks if c.get("status") == "mismatch"), None)
    if mismatch is not None:
        # With no other source that fits, another --telemetry-source would
        # only mismatch again, so the hint names the table instead.
        sources = mismatch.get("matching_sources") or []
        if len(sources) == 1:
            hint = f"Pass --telemetry-source {sources[0]} to read it as {sources[0]}."
        elif sources:
            hint = f"Pass --telemetry-source with one of {', '.join(sources)}."
        else:
            hint = (
                "Name a table in that format with --telemetry-dataset and "
                "--telemetry-table, or fix what writes this one."
            )
        return (
            f"Not attaching: {mismatch.get('target') or mismatch.get('name')} is "
            "not in the telemetry source's format, as the schema check above "
            f"shows. {hint}"
        )
    failed = [
        c
        for c in checks
        if c.get("status") == "error" and c.get("name") in ("table", "rows")
    ]
    if not failed:
        return ""
    targets = ", ".join(
        sorted({str(c.get("target") or c.get("name")) for c in failed})
    )
    return (
        f"Not attaching: reading {targets} failed, and investigations would run "
        "the same query. If the table holds another source's telemetry, pass "
        "--telemetry-source (big_query, cloud_logging or cloud_ops) to match it; "
        "the error is in the reads above."
    )


def _render_link_lines(link: discovery_lib.TraceLink) -> list[str]:
    """Renders the commands that create a Cloud Trace link.

    Args:
        link: A link that does not exist yet.

    Returns:
        Lines to print, in order.
    """
    return [
        f"{discovery_lib.TRACE_BUCKET}/{discovery_lib.TRACE_DATASET} in "
        f"{link.project} needs a BigQuery link for AQuA to read the agent's "
        "spans. To create it and what it needs, run with your own credentials:",
        *(f"  {shlex.join(argv)}" for argv in link.build_needed_argvs()),
    ]


def _create_trace_link(
    link: discovery_lib.TraceLink,
    *,
    run_command: Callable[[list[str]], int] | None = None,
    echo: Callable[[str], None] = click.echo,
    location: str | None = None,
    link_id: str | None = None,
) -> tuple[discovery_lib.TraceLink, list[list[str]]]:
    """Creates a Cloud Trace link, and what it needs, with the user's credentials.

    Args:
        link: A link that does not exist yet.
        run_command: Runs one command and returns its exit status;
            :func:`_run_command` if None.
        echo: Prints a progress line.
        location: The bucket location the operator gave, if any.
        link_id: The link id the operator gave, if any.

    Returns:
        The link as created, and the commands that ran.

    Raises:
        click.ClickException: If gcloud is not on PATH or a step fails.
    """
    if shutil.which("gcloud") is None:
        raise click.ClickException(
            "Not on PATH: gcloud. Install the Google Cloud CLI, or run the "
            "commands above wherever it is installed."
        )
    try:
        return trace_link_lib.create_trace_link(
            link,
            api=rest_client_lib.RestClient(get_adc_token),
            run_command=run_command or _run_command,
            echo=echo,
            location=location,
            link_id=link_id,
        )
    except trace_link_lib.TraceLinkError as exc:
        raise click.ClickException(str(exc)) from exc


def _run_command(argv: list[str]) -> int:
    """Runs one command with no shell, its output going to the terminal.

    Args:
        argv: Command-line arguments vector.

    Returns:
        The command's exit status.
    """
    return subprocess.run(argv, check=False).returncode  # noqa: S603 - explicit argument list without shell execution


def _run_command_to_stderr(argv: list[str]) -> int:
    """Runs one command with no shell, keeping its output off stdout.

    For commands whose stdout carries only their JSON result: the command's
    stdout is captured and written to stderr once it exits.

    Args:
        argv: Command-line arguments vector.

    Returns:
        The command's exit status.
    """
    result = subprocess.run(  # noqa: S603 - explicit argument list without shell execution
        argv, check=False, stdout=subprocess.PIPE, text=True
    )
    if result.stdout:
        click.echo(result.stdout.rstrip("\n"), err=True)
    return result.returncode


_RECHECK_DELAYS_SECONDS = (0, 15, 45, 60, 60)
"""How long to wait before each access check after the grants run. A new IAM
binding can take a minute or more to reach every service that enforces it, and
while it has not, the checks behind it stay skipped and their grants unknown;
three minutes in all gives a single `attach` room to reach them."""

_MAX_GRANT_ROUNDS = 3
"""How many rounds of grants one `attach` runs. A check that needs an
earlier read is skipped until that read is allowed, so its grant only shows up
after the earlier grant: the payload check waits for the rows check, which
waits for the table check, unless the attachment names the payload bucket. The
table and rows checks share the dataset grant, so that chain takes two rounds;
the third is spare, and the cap stops a check that keeps calling for new
grants."""


def _apply_grants(
    grants: list[grant_commands.Grant],
    request: AttachAgentArgs,
    *,
    yes: bool,
    target: dict[str, Any],
    agent_key: str,
) -> None:
    """Runs the grant commands the plan printed, then checks access again.

    A check skipped behind a denied read can turn out denied once that read
    is granted, so the re-check can call for grants the plan could not show.
    Those run in a further round, after they are printed and confirmed, so a
    grant is never made that the user was not shown.

    Steps:
    1. Confirm the round's commands, unless --yes or this is the first
       round, which the caller confirmed.
    2. Run them with the user's credentials, and record on the attachment
       each one that ran, even when a later one failed.
    3. Ask the deployment to check again, waiting between tries while a
       denied read still needs a grant that already ran.
    4. If the check filled in settings from a table the grants made readable,
       store them once confirmed, so that any further round grants for them.
    5. If the check calls for grants none of the rounds made, print them and
       start the next round with them, up to `_MAX_GRANT_ROUNDS`.
    6. Show the last check's result, the grants still denied, any grant left
       unrun, and the denied reads no command covers.

    Args:
        grants: Commands the plan printed, already confirmed.
        request: The attach request, without `dry_run`; storing adds the
            filled-in settings to its fields.
        yes: Whether to skip the confirmation of later rounds and of storing.
        target: Engine target options for the route call.
        agent_key: Key of the attached agent, as the plan resolved it.

    Raises:
        click.ClickException: If a command cannot run or fails.
        click.Abort: If the user declines a later round, or storing.
    """
    ran: set[tuple[str, str]] = set()
    pending = grants
    plan: dict[str, Any] = {}
    access: dict[str, Any] = {}
    needed: list[grant_commands.Grant] = []
    ungranted: list[str] = []
    new: list[grant_commands.Grant] = []
    for round_number in range(1, _MAX_GRANT_ROUNDS + 1):
        if round_number > 1 and not yes:
            click.confirm(
                f"\nRun {len(pending)} grant command(s) with your credentials?",
                abort=True,
            )
        click.echo()
        try:
            grants_lib.run_grants(pending, echo=click.echo)
        except grants_lib.GrantError as exc:
            made = [
                g for g in exc.completed if isinstance(g, grant_commands.Grant)
            ]
            if made:
                _record_grants(agent_key, made, target)
            raise click.ClickException(str(exc)) from exc
        _record_grants(agent_key, pending, target)
        ran.update(grant.key for grant in pending)

        for delay in _RECHECK_DELAYS_SECONDS:
            if delay:
                click.echo(
                    f"Still denied; IAM changes take time to propagate. "
                    f"Checking again in {delay} s."
                )
                time.sleep(delay)
            plan = _fetch_agents_result(
                AGENTS_ATTACH_ROUTE,
                AttachAgentArgs(
                    **request,
                    dry_run=True,
                    check_access=True,
                    fill_from_table=True,
                ),
                **target,
            )
            access = plan.get("access") or {}
            needed, ungranted = grant_commands.build_grants(access)
            if not any(grant.key in ran for grant in needed):
                break
        if plan.get("filled"):
            _store_filled(plan, request, yes=yes, target=target)

        new = [grant for grant in needed if grant.key not in ran]
        if not new or round_number == _MAX_GRANT_ROUNDS:
            break
        for line in _render_access_lines(access):
            click.echo(line)
        targets = ", ".join(grant.resource for grant in new)
        click.echo(
            f"\nThe grants that just ran made {targets} checkable, and "
            f"{access.get('service_account') or 'AQuA'} is denied there too. "
            "Running next, with your credentials:"
        )
        click.echo("\n".join(f"  {grant.render()}" for grant in new))
        pending = new

    for line in _render_access_lines(access):
        click.echo(line)
    if still_denied := [g.resource for g in needed if g.key in ran]:
        skipped = [
            str(check.get("name"))
            for check in access.get("checks") or []
            if check.get("status") == "skipped"
        ]
        if skipped:
            # A skipped check may need a grant of its own, which only a check
            # after the binding propagates can name.
            click.echo(
                f"\nStill denied after the grants: {', '.join(still_denied)}, "
                f"so {', '.join(skipped)} could not be checked yet. Once the "
                "binding has propagated, run attach again to grant "
                "what they need."
            )
        else:
            click.echo(
                f"\nStill denied after the grants: {', '.join(still_denied)}. "
                "A binding can take several minutes to propagate; check again "
                "with attach --dry-run."
            )
    if new:
        click.echo(
            f"\nStopped after {_MAX_GRANT_ROUNDS} rounds of grants; each "
            "re-check called for another."
        )
    lines = grants_lib.render_grant_lines(
        new, ungranted, account=str(access.get("service_account") or "")
    )
    for line in lines:
        click.echo(line)
    if new:
        click.echo("Or run attach again to run them.")


def _extract_filled(plan: dict[str, Any]) -> dict[str, Any]:
    """Extracts what an attach plan filled in from the telemetry table.

    Args:
        plan: Dry-run response from the attach route.

    Returns:
        The filled-in values keyed by field name; empty when the deployment
        filled in nothing, as one that predates `fill_from_table` does.
    """
    return {f["field"]: f["value"] for f in plan.get("filled") or []}


def _store_filled(
    plan: dict[str, Any],
    request: AttachAgentArgs,
    *,
    yes: bool,
    target: dict[str, Any],
) -> None:
    """Stores what a check after grants filled in from the telemetry table.

    The attachment is stored before any grant runs, while AQuA may not read
    the table yet, so a check after the grants can be the first to fill
    anything in.

    Args:
        plan: That check's plan, which filled something in.
        request: The attach request; the filled-in values join its fields,
            so later checks send them as given.
        yes: Whether to store them without asking.
        target: Engine target options for the route call.

    Raises:
        click.Abort: If the user declines.
    """
    filled = plan["filled"]
    click.echo("\nNow that AQuA can read the telemetry table, it fills in:")
    for setting in filled:
        click.echo(
            f"  {setting['field']}: {_format_value(setting['value'])} "
            f"({setting['reason']})"
        )
    if not yes:
        click.confirm(f"\nStore {len(filled)} setting(s)?", abort=True)
    request["fields"] = {**request.get("fields", {}), **_extract_filled(plan)}
    _fetch_agents_result(
        AGENTS_ATTACH_ROUTE, AttachAgentArgs(**request, dry_run=False), **target
    )
    click.echo("Stored them.")


def _build_agent_selector(agent_key: str) -> AgentSelector:
    """Builds the fields that name an attachment to the `agents/*` routes.

    An engine that takes no `agent_key` reads `agent_name`, and every
    attachment such an engine holds is stored under its bare name, which is
    the key.

    Args:
        agent_key: What the attachment is stored under.

    Returns:
        `{"agent_key", "agent_name"}`, both the key.
    """
    return AgentSelector(agent_key=agent_key, agent_name=agent_key)


def _is_aqua_own_checkout() -> bool:
    """Checks whether the working directory is the source tree this CLI runs from.

    In AQuA's own checkout, it is. agents-cli runs the copy it vendors into a
    subdirectory of the observed agent's project, from the project root above
    that copy.

    Returns:
        Whether the working directory is in this CLI's source tree.
    """
    source_root = pathlib.Path(__file__).resolve().parents[2]
    return pathlib.Path.cwd().resolve().is_relative_to(source_root)


def _save_agent_key(agent_key: str, target: dict[str, Any]) -> None:
    """Records the attached agent in the project, for later commands here.

    Commands then act on this agent with no `--agent-key`, on the AQuA it was
    attached to. Not in AQuA's own checkout, where an operator attaches one
    agent after another and commands act on AQuA's default agent; nor for a
    target only other environment variables configure, which has no name to
    record the key for. A failure to write is reported, not raised: the
    attachment is already stored.

    Args:
        agent_key: Key of the attached agent.
        target: AQuA's `url` and `resource`.
    """
    aqua = _resolve_target_name(target["url"], target["resource"])
    state_root = pathlib.Path.cwd() / STATE_DIR_NAME
    if (
        not aqua
        or _is_aqua_own_checkout()
        or resolve_recorded_agent_key(state_root, aqua) == agent_key
    ):
        return
    try:
        save_attached_aqua_fields(
            state_root,
            agent_key=agent_key,
            agent_key_target=normalize_target(aqua),
        )
    except OSError as exc:
        click.echo(
            f"\nCould not record {agent_key} in {STATE_DIR_NAME}/"
            f"{ATTACHED_AQUA_FILE} ({exc}); pass --agent-key {agent_key} to "
            "later commands."
        )
        return
    click.echo(
        f"\nRecorded {agent_key} in {STATE_DIR_NAME}/{ATTACHED_AQUA_FILE}; "
        "later commands here act on it without --agent-key."
    )


def _delete_agent_key(agent_key: str, target: dict[str, Any]) -> None:
    """Removes a detached agent from the project's record, if it is the one there.

    Args:
        agent_key: Key of the detached agent.
        target: AQuA's `url` and `resource`.
    """
    aqua = _resolve_target_name(target["url"], target["resource"])
    state_root = pathlib.Path.cwd() / STATE_DIR_NAME
    if not aqua or resolve_recorded_agent_key(state_root, aqua) != agent_key:
        return
    try:
        save_attached_aqua_fields(
            state_root, agent_key=None, agent_key_target=None
        )
    except OSError as exc:
        click.echo(
            f"Could not remove {agent_key} from {STATE_DIR_NAME}/"
            f"{ATTACHED_AQUA_FILE} ({exc}); pass --agent-key to later commands."
        )
        return
    click.echo(
        f"Removed {agent_key} from {STATE_DIR_NAME}/{ATTACHED_AQUA_FILE}; later "
        "commands here act on AQuA's default agent."
    )


def _record_grants(
    agent_key: str, grants: list[grant_commands.Grant], target: dict[str, Any]
) -> None:
    """Records grants that ran on the attachment, for `detach` to revoke.

    A refused record does not fail the command: the grants are already made,
    and the rest of `attach` still has to run. The user is shown how to
    revoke them instead, since `detach` will not.

    Args:
        agent_key: Key of the attached agent.
        grants: Grants that ran.
        target: Engine target options for the route call.
    """
    try:
        _fetch_agents_result(
            AGENTS_APPLIED_ROUTE,
            RecordAppliedArgs(
                **_build_agent_selector(agent_key),
                granted=[g.to_record() for g in grants],
            ),
            **target,
        )
    except click.ClickException as exc:
        revocations, _ = grant_commands.build_revocations(
            [g.to_record() for g in grants]
        )
        click.echo(
            f"\nCould not record the grants on the attachment: "
            f"{exc.format_message()}\ndetach will not revoke them; "
            "to revoke them by hand:"
        )
        for revocation in revocations:
            click.echo(f"  {revocation.render()}")
        return
    click.echo(
        f"\nRecorded {len(grants)} grant(s) on the attachment; "
        "detach revokes them."
    )


_KEY_FIELDS = frozenset({"observed_project_id"})
"""Fields whose discovered value is always sent: the attachment's key is built
from them, and the deployment refuses a change."""

_FIELDS_DISCOVERED_FOR = {
    "telemetry_ingestion_source": frozenset(
        {"telemetry_dataset", "telemetry_table", "telemetry_location"}
    ),
    "telemetry_dataset": frozenset({"telemetry_location"}),
}
"""For each field, the fields discovery resolves from its value.

When a flag changes the field, their stored values describe the old value: a
dataset of the old source, or the location of the old dataset."""


def _list_kept_stored_values(
    agent_name: str,
    fields: dict[str, Any],
    *,
    discovered: list[str],
    target: dict[str, Any],
) -> list[dict[str, Any]]:
    """Lists the discovered values an attached agent keeps its stored ones over.

    Discovery reads the observed agent's project, which does not know what the
    attachment stores, so on an attached agent a discovered value fills only a
    setting stored empty. A flag that changes a field lets discovery
    replace the fields resolved for it (`_FIELDS_DISCOVERED_FOR`).

    Args:
        agent_name: The agent's name, as resolved.
        fields: Fields to send: the flags' and the discovered ones.
        discovered: Which of ``fields`` discovery or a hint filled in.
        target: The `url` and `resource` of the deployment.

    Returns:
        The plan's `{field, before, after}` change of each discovered field to
        leave out; empty when the agent is not attached yet.

    Raises:
        click.ClickException: If the deployment refuses the plan.
    """
    plan = _fetch_agents_result(
        AGENTS_ATTACH_ROUTE,
        AttachAgentArgs(agent_name=agent_name, fields=fields, dry_run=True),
        **target,
    )
    if not plan.get("attached"):
        return []
    changes = {c["field"]: c for c in plan.get("changes") or []}
    replaceable = set(_KEY_FIELDS)
    for field, resolved in _FIELDS_DISCOVERED_FOR.items():
        if field in changes and field not in discovered:
            replaceable |= resolved
    return [
        changes[field]
        for field in discovered
        if field in changes
        and field not in replaceable
        and changes[field]["before"] not in (None, "")
    ]


def _render_kept_lines(kept: list[dict[str, Any]]) -> list[str]:
    """Renders the stored values an attached agent keeps over discovered ones.

    Args:
        kept: The plan's change of each discovered field left out.

    Returns:
        Lines to print; empty when nothing was kept.
    """
    if not kept:
        return []
    width = max(len(c["field"]) for c in kept)
    return [
        "Stored values kept over discovered ones (set a flag to change one, or "
        "pass --rediscover to take them all):",
        *(
            f"  {c['field']:<{width}}  {_format_value(c['before'])}  "
            f"(discovered {_format_value(c['after'])})"
            for c in kept
        ),
    ]


def _resolve_discovered_fields(
    agent_name: str,
    fields: dict[str, Any],
    *,
    observed_agent_resource: str | None,
    hints: dict[str, discovery_lib.DiscoveredValue],
    window_days: int,
    target: dict[str, Any],
    keep_stored: bool,
) -> tuple[str, dict[str, Any], list[str], discovery_lib.TraceLink | None]:
    """Fills in the attach fields no flag set: from the engine, then hints.

    With ``keep_stored``, an attached agent keeps the stored value of each
    field filled in, except where `_list_kept_stored_values` lets the
    discovered one replace it.

    Args:
        agent_name: AGENT_NAME as given; empty if not.
        fields: Fields the flags set.
        observed_agent_resource: The observed engine, if given.
        hints: Values a wrapper read from the project's own files.
        window_days: How far back discovery counts telemetry rows.
        target: The `url` and `resource` of the deployment, which says what
            an attached agent stores.
        keep_stored: Whether an attached agent's stored values outrank
            discovered ones; false for --rediscover.

    Returns:
        Tuple of (agent name, fields to send, lines to show before the plan,
        the Cloud Trace link those fields read through when it does not exist
        yet).

    Raises:
        click.ClickException: If discovery cannot go on, or the deployment
            refuses the plan.
    """
    given = dict(fields)
    if agent_name:
        given["observed_agent_name"] = agent_name
    discovery = None
    if observed_agent_resource:
        try:
            discovery = discovery_lib.discover_observed_agent(
                observed_agent_resource,
                api=rest_client_lib.RestClient(get_adc_token),
                given=given,
                window_days=window_days,
            )
        except discovery_lib.DiscoveryError as exc:
            raise click.ClickException(str(exc)) from exc
    resolved = discovery_lib.resolve_attach_fields(
        given, discovery.values if discovery else {}, hints
    )
    filled = dict(fields)
    for value in resolved:
        if value.field == "observed_agent_name":
            # The route takes the agent's name beside the fields: it is the
            # attachment's key.
            agent_name = str(value.value)
        else:
            filled[value.field] = value.value
    discovered = [field for field in filled if field not in fields]
    kept = (
        _list_kept_stored_values(
            agent_name, filled, discovered=discovered, target=target
        )
        if keep_stored and set(discovered) - _KEY_FIELDS
        else []
    )
    kept_fields = {change["field"] for change in kept}
    filled = {f: v for f, v in filled.items() if f not in kept_fields}
    resolved = [value for value in resolved if value.field not in kept_fields]
    lines = discovery_lib.render_discovery_lines(
        observed_agent_resource, resolved, discovery
    )
    lines.extend(_render_kept_lines(kept))
    link = discovery.trace_link if discovery else None
    if (
        link is None
        or not link.build_needed_argvs()
        or filled.get("telemetry_ingestion_source") != "cloud_ops"
        or filled.get("telemetry_dataset") != link.link_id
    ):
        link = None
    return agent_name, filled, lines, link


_NO_TRIGGER_TOPIC = (
    "This AQuA reports no topic for update triggers, so attach wires none: it "
    "runs locally, its engine predates triggers wired by attach, or it is not "
    "deployed on Agent Runtime."
)


def _resolve_trigger_topic(response: dict[str, Any]) -> str:
    """Resolves the topic an attached agent's update trigger publishes to.

    Args:
        response: An attach or list response, with the `trigger_target` the
            engine reports.

    Returns:
        The topic id, or an empty string when the engine reports none.
    """
    target = response.get("trigger_target")
    if not isinstance(target, dict):
        return ""
    return str(target.get("ambient_topic") or "")


def _resolve_sink_project(
    observed_project: str, agent_key: str, topic: str
) -> str:
    """Resolves the project holding an agent's update sink: the observed engine's.

    Args:
        observed_project: The attachment's `observed_project_id`; empty when
            unknown or unset.
        agent_key: What the attachment is stored under. A key names the
            project the agent runs in; a bare name, AQuA's own.
        topic: AQuA's topic id, whose project is AQuA's.

    Returns:
        The project.

    Raises:
        update_trigger_lib.TriggerError: If the topic is not a topic id.
    """
    if observed_project:
        return observed_project
    if match := AGENT_KEY_RE.fullmatch(agent_key):
        return match["project"]
    return update_trigger_lib.parse_topic(topic)[0]


def _plan_update_trigger(
    *, topic: str, agent_key: str, observed_project: str, engine_id: str
) -> update_trigger_lib.TriggerPlan:
    """Plans an agent's update trigger from what exists, with the user's credentials.

    Args:
        topic: AQuA's topic id.
        agent_key: What the attachment is stored under.
        observed_project: The attachment's `observed_project_id`, if known.
        engine_id: The observed engine's id; empty to remove the trigger.

    Returns:
        The plan.

    Raises:
        click.ClickException: If the trigger cannot be read or planned.
    """
    try:
        return update_trigger_lib.plan_trigger(
            rest_client_lib.RestClient(get_adc_token),
            topic=topic,
            agent_key=agent_key,
            project=_resolve_sink_project(observed_project, agent_key, topic),
            engine_id=engine_id,
        )
    except update_trigger_lib.TriggerError as exc:
        raise click.ClickException(f"{exc}\nNothing was changed.") from exc


def _apply_update_trigger(plan: update_trigger_lib.TriggerPlan) -> None:
    """Runs the commands of an update trigger plan with the user's credentials.

    Args:
        plan: The plan, as printed.

    Raises:
        click.ClickException: If gcloud is missing or a command fails.
    """
    try:
        update_trigger_lib.apply_trigger(
            plan,
            api=rest_client_lib.RestClient(get_adc_token),
            runner=_run_command,
            echo=click.echo,
        )
    except update_trigger_lib.TriggerError as exc:
        raise click.ClickException(str(exc)) from exc


def _plan_attach_trigger(
    plan: dict[str, Any], agent_key: str, default_agent: str
) -> update_trigger_lib.TriggerPlan | None:
    """Plans the update trigger an attach wires, or says why it wires none.

    Args:
        plan: Dry-run response from the attach route.
        agent_key: Key of the attached agent, as the plan resolved it.
        default_agent: The agent an engine that investigates only its default
            agent investigates, when the plan's agent is not it.

    Returns:
        The plan, or None when the engine reports no topic, or investigates
        only its default agent and the plan's agent is not it: that agent's
        updates would investigate the default one again.

    Raises:
        click.ClickException: If the trigger cannot be read or planned.
    """
    triggers = plan.get("triggers") or {}
    engine_id = str(triggers.get("observed_engine_id") or "")
    topic = _resolve_trigger_topic(plan)
    if not topic:
        if engine_id:
            click.echo(f"\n{_NO_TRIGGER_TOPIC}")
        return None
    if _is_ignored_by_triggers(plan):
        if engine_id:
            watched = default_agent or "another agent"
            click.echo(
                f"\nNo update trigger for {plan.get('agent_name')}: this "
                f"deployment investigates {watched} whatever trigger fires, "
                f"so its updates would investigate {watched} again."
            )
        return None
    return _plan_update_trigger(
        topic=topic,
        agent_key=agent_key,
        observed_project=str(triggers.get("observed_project_id") or ""),
        engine_id=engine_id,
    )


@cli.command("attach")
@click.argument("agent_name", default="")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show the plan and its commands; write, grant and create nothing.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Do not ask before changing a setting or running a command.",
)
@click.option(
    "--no-discover",
    is_flag=True,
    help="Store only the settings given, discovering none.",
)
@click.option(
    "--rediscover",
    is_flag=True,
    help=(
        "On an attached agent, let every discovered value replace the stored "
        "one, for example after redeploying the observed agent to its engine."
    ),
)
@click.option(
    "--source-root",
    type=click.Path(file_okay=False, path_type=pathlib.Path),
    default=None,
    help=(
        "The observed agent's project directory. Once attached, its source is "
        "published to AQuA for the engine's newest revision, as publish-source "
        "does."
    ),
)
@click.option(
    "--hints",
    default="",
    hidden=True,
    help=(
        'Values a wrapper read from the project, as {"<field>": {"value": ..., '
        '"source": "..."}}. A flag or discovery outranks each.'
    ),
)
@_add_attach_options
@_add_target_options
def attach_agent_cmd(
    agent_name,
    dry_run,
    yes,
    no_discover,
    rediscover,
    source_root,
    hints,
    *,
    url,
    resource,
    **flags,
) -> None:
    """Record how AQuA observes AGENT_NAME, and set up what that needs.

    The deployment validates the settings and stores them; investigations read
    them from the next run on. Only the settings given change, and everything
    else keeps its current value. Without AGENT_NAME, the name is discovered
    from --observed-agent-resource.

    With --observed-agent-resource, every setting no flag gives is discovered
    from the observed engine with your credentials: the agent's name from its
    ADK server, its deployment's name, and its telemetry: the agent's spans in
    Cloud Trace, read through a BigQuery link on the project's `_Trace`
    bucket. When the bucket or the link does not exist, attach enables the
    Observability API and creates them, and detach leaves them in place.
    Discovery selects a logging sink's table only if --telemetry-dataset names
    the sink's dataset or the Observability API is unavailable; otherwise, the
    plan notes any sinks passed over. The plan names each value's source.

    On an attached agent, each stored value is kept over a discovered one,
    including a default stored by the first attach, and the plan lists them; a
    discovered value fills only a setting stored empty. A flag that changes
    --telemetry-source lets discovery replace the telemetry dataset, table and
    location, and one that changes --telemetry-dataset, the location, since
    the stored ones describe the old source or dataset. With --rediscover,
    every discovered value replaces the stored one, for example to take the
    deployment name or payload bucket of an agent redeployed to its engine.

    The observed agent may be in another project than AQuA. Its telemetry is
    read there, the grant commands name that project's dataset, and the audit
    sink on the agent's updates is created there, so your credentials need to
    reach both projects.

    The attachment is keyed <project id>__<agent name>, with the project the
    agent runs in, so the same agent in a staging and a prod project is two
    attachments. An agent in AQuA's own project keeps its bare name as its key.
    detach and the update sink's name name the attachment by its key.

    First, the deployment reads from the telemetry table what no flag or
    discovery gives -- the table's location, and its source when the table is
    in only one source's format -- and the plan names the table beside each.
    It then tries the reads an investigation makes, as its own service
    account, and the plan shows each result. Telemetry that does not
    exist, or that the telemetry query fails on, refuses the attach. For each
    read it is denied, the plan prints the command that grants it; those
    commands run with your credentials, and the reads are tried again. A read
    that was skipped behind a denied one and turns out denied gets its grant
    printed and run in the same attach.

    The plan also prints the gcloud commands that create or update the agent's
    update trigger: an audit log sink on the observed engine's updates, and its
    permission to publish to AQuA's topic. Every command runs once you confirm
    it, or with --yes.

    With --source-root, the project's source is then published to AQuA for the
    engine's newest revision, unless it already is.

    Unless this is AQuA's own checkout, or AQuA's engine is configured by
    environment variables other than AGENT_ENGINE_RESOURCE_ID, the agent's key
    is recorded in ./.aqua/attached_aqua.json, and later commands in this
    directory that reach the same AQuA act on the agent without --agent-key.
    """
    # 1. Fill in what no flag gave: discovered from the observed engine, then
    #    the wrapper's hints, unless --no-discover. An attached agent keeps
    #    its stored values, unless --rediscover.
    # 2. Ask the deployment for the plan and its access check, and show both,
    #    with a grant command for each denied read. A Cloud Trace link still
    #    to be created leaves the access check for after it. What the plan
    #    filled in from the table is sent from then on as if a flag gave it.
    # 3. Unless --dry-run, create the Cloud Trace link, and check access again.
    # 4. Refuse telemetry that does not exist or that the telemetry query fails
    #    on.
    # 5. Plan the update trigger from what exists, and show its commands.
    # 6. Stop for a dry run.
    # 7. Confirm any change, any grant and the trigger commands, unless --yes.
    # 8. Store the change.
    # 9. Record the agent in ./.aqua/, for later commands here.
    # 10. Run the grant commands, record those that ran, and check again, with
    #     further confirmed rounds for the grants it reveals. What a check
    #     fills in from a table AQuA could not read before the grants is
    #     stored, once confirmed, before any further round.
    # 11. With --source-root, publish the source of the engine's newest
    #     revision, unless AQuA already has it.
    # 12. Run the trigger commands and record what is in effect.
    target = {"url": url, "resource": resource}
    if no_discover and rediscover:
        raise click.UsageError(
            "--rediscover needs discovery; it cannot be combined with "
            "--no-discover."
        )
    fields = _extract_requested_fields(flags)
    pending_link: discovery_lib.TraceLink | None = None
    if not no_discover:
        try:
            parsed_hints = discovery_lib.parse_hints(hints)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--hints") from exc
        lookback = flags.get("lookback_days")
        # The deployment checks the stored value; discovery only needs a
        # window that can hold rows.
        window_days = (
            max(lookback, 1) if lookback is not None else _DISCOVERY_WINDOW_DAYS
        )
        agent_name, fields, lines, pending_link = _resolve_discovered_fields(
            agent_name,
            fields,
            observed_agent_resource=flags.get("observed_agent_resource"),
            hints=parsed_hints,
            window_days=window_days,
            target=target,
            keep_stored=not rediscover,
        )
        if pending_link:
            lines.extend(_render_link_lines(pending_link))
        for line in lines:
            click.echo(line)
        if lines:
            click.echo()
    creating_link = pending_link is not None and not dry_run
    if observed := flags.get("observed_agent_resource"):
        # Discovery resolves a project number to the id; without it, the
        # resource name's own spelling is what there is.
        fields.setdefault(
            "observed_project_id",
            discovery_lib.parse_engine_resource(observed).project,
        )
    request = AttachAgentArgs(agent_name=agent_name, fields=fields)

    plan = _fetch_agents_result(
        AGENTS_ATTACH_ROUTE,
        AttachAgentArgs(
            **request, dry_run=True, check_access=True, fill_from_table=True
        ),
        **target,
    )
    fields.update(_extract_filled(plan))
    default_agent = (
        _fetch_default_agent(target) if _is_ignored_by_triggers(plan) else ""
    )
    for line in _render_plan_lines(plan, default_agent=default_agent):
        click.echo(line)
    access = plan.get("access") or {}
    # Before the link exists, its table is missing and every read behind it
    # is skipped, so the check is shown once the link is created.
    grants, _ = ([], []) if creating_link else _echo_access_check(access)
    name = str(plan.get("agent_name") or agent_name)
    key = str(plan.get("agent_key") or name)

    if creating_link and pending_link is not None:
        if not yes:
            click.confirm(
                "\nRun the commands above with your credentials?", abort=True
            )
        click.echo()
        try:
            created, _ = _create_trace_link(
                pending_link, link_id=flags.get("telemetry_dataset")
            )
        except click.ClickException as exc:
            raise click.ClickException(
                f"{exc.format_message()}\nNothing was stored or granted."
            ) from exc
        pending_link = None
        # When the API was disabled, the initial plan used default values; update
        # the request with the actual dataset ID and location resolved after
        # enabling the API.
        for field, value in (
            ("telemetry_dataset", created.link_id),
            ("telemetry_location", created.bigquery_location),
        ):
            if flags.get(field) is None:
                fields[field] = value
        request["fields"] = fields
        plan = _fetch_agents_result(
            AGENTS_ATTACH_ROUTE,
            AttachAgentArgs(**request, dry_run=True, check_access=True),
            **target,
        )
        access = plan.get("access") or {}
        grants, _ = _echo_access_check(access)

    if pending_link is not None:
        # Only a dry run gets here with the link still to create, and the
        # table the check found missing is the one the link creates.
        click.echo(
            "\nWithout --dry-run, attach creates the link first, then checks "
            "the reads again."
        )
    elif refusal := _validate_access(access):
        raise click.ClickException(refusal)
    trigger = _plan_attach_trigger(plan, key, default_agent)
    for line in trigger.render_lines() if trigger else []:
        click.echo(line)
    if dry_run:
        click.echo("\nDry run: nothing was written, granted or created.")
        return

    changes = plan.get("changes") or []
    # A first attach is stored even when it changes nothing: storing it is what
    # attaches the agent.
    storing = not plan.get("attached") or bool(changes)
    trigger_commands = trigger.commands if trigger else ()
    if not yes:
        if storing and changes:
            click.confirm(f"\nChange {len(changes)} setting(s)?", abort=True)
        if grants:
            click.confirm(
                f"\nRun {len(grants)} grant command(s) with your credentials?",
                abort=True,
            )
        if trigger_commands:
            click.confirm(
                f"\nRun {len(trigger_commands)} update trigger command(s) "
                "with your credentials?",
                abort=True,
            )
    # Stored before any grant runs, so that every grant that runs, even in a
    # run that then fails, has an attachment to be recorded on.
    if storing:
        result = _fetch_agents_result(
            AGENTS_ATTACH_ROUTE,
            AttachAgentArgs(**request, dry_run=False),
            **target,
        )
        click.echo(f"\nAttached {result.get('agent_name', name)} as {key}.")
    # Every later call names the attachment by its key, which the plan built.
    request["agent_key"] = key
    _save_agent_key(key, target)
    if grants:
        _apply_grants(grants, request, yes=yes, target=target, agent_key=key)
    elif not access:
        click.echo(
            "\nThis deployment checked no reads, so there is nothing to grant."
        )
    if source_root:
        _publish_first_snapshot(
            source_root.resolve(),
            key,
            flags.get("observed_agent_resource"),
            target,
        )
    if trigger is None:
        return

    if trigger_commands:
        click.echo(f"\nWiring the update trigger of {key}:")
        _apply_update_trigger(trigger)
    # An engine that does not report `unapplied_fields` gets a record every
    # time, which only rewrites what it holds.
    if not trigger_commands and plan.get("unapplied_fields", True) == []:
        return
    recorded = _fetch_agents_result(
        AGENTS_APPLIED_ROUTE,
        RecordAppliedArgs(
            **_build_agent_selector(key), applied=plan.get("triggers") or {}
        ),
        **target,
    )
    if trigger_commands:
        click.echo(f"\nWired the update trigger of {key}.")
    if unapplied := recorded.get("unapplied_fields"):
        # Another attach landed while the commands ran.
        click.echo(
            f"Changed since this attach began: {', '.join(unapplied)}. "
            "Run attach again."
        )


@cli.command("list-agents")
@_add_target_options
def list_agents_cmd(*, url, resource) -> None:
    """List the attached agents as JSON, by key, with which one is the default."""
    _emit_result(
        _fetch_command_result(
            url=url, resource=resource, path=AGENTS_LIST_ROUTE
        )
    )


def _revoke_grants(
    agent_key: str,
    revocations: list[grant_commands.Revocation],
    target: dict[str, Any],
) -> None:
    """Revokes the grants `attach` recorded, and records them revoked.

    Every revocation is tried, and each that succeeded is recorded before a
    failure is reported, so that running `detach` again retries only the ones
    that failed.

    Args:
        agent_key: Key of the attached agent.
        revocations: Commands for the recorded grants.
        target: Engine target options for the route call.

    Raises:
        click.ClickException: If a command cannot run or fails.
    """
    click.echo(f"\nRevoking the grants attach made for {agent_key}:")
    failure = None
    try:
        grants_lib.run_revocations(revocations, echo=click.echo)
        done = list(revocations)
    except grants_lib.GrantError as exc:
        failure = exc
        done = [
            r for r in exc.completed if isinstance(r, grant_commands.Revocation)
        ]
    if done:
        _fetch_agents_result(
            AGENTS_APPLIED_ROUTE,
            RecordAppliedArgs(
                **_build_agent_selector(agent_key),
                revoked=[r.to_record() for r in done],
            ),
            **target,
        )
    if failure is not None:
        raise click.ClickException(
            f"{failure}\n{agent_key} stays attached, with the failed grants "
            "still recorded; detach again retries them. If those bindings are "
            "already gone, detach --keep-grants forgets them and removes the "
            "update trigger."
        ) from failure


@cli.command("detach")
@click.argument("agent_key")
@click.option("--yes", is_flag=True, help="Do not ask before detaching.")
@click.option(
    "--keep-grants",
    is_flag=True,
    help=(
        "Leave the grants attach made in place. They are forgotten with the "
        "attachment."
    ),
)
@_add_target_options
def detach_agent_cmd(agent_key, yes, keep_grants, *, url, resource) -> None:
    """Forget how AQuA observes the agent AGENT_KEY, and undo what attach set up.

    The grants attach recorded on the attachment are revoked with your
    credentials, and no others, unless --keep-grants. The agent's update
    trigger is removed, even when the attachment is already gone.
    `list-agents` lists the keys.
    """
    # 1. Read the attachment, the grants recorded on it and AQuA's topic.
    # 2. Show the revoke commands, and the commands that remove the update
    #    trigger, planned from what exists.
    # 3. Confirm.
    # 4. Revoke the grants unless --keep-grants, then remove the trigger.
    # 5. Delete the attachment.
    # 6. Remove it from ./.aqua/ if attach recorded it there.
    target = {"url": url, "resource": resource}
    listing = _fetch_agents_result(AGENTS_LIST_ROUTE, None, **target)
    # An engine that lists no `agent_key` stores each attachment under its
    # bare name, which is then the key.
    entries = {
        a.get("agent_key") or a.get("agent_name"): a
        for a in listing.get("agents") or []
    }
    attached = agent_key in entries
    entry = entries.get(agent_key, {})
    revocations, unrevocable = grant_commands.build_revocations(
        entry.get("grants") or []
    )
    if revocations:
        if keep_grants:
            click.echo(
                "Grants attach made, left in place and forgotten once detached:"
            )
        else:
            click.echo("Grants attach made, revoked with your credentials:")
        for revocation in revocations:
            click.echo(f"  {revocation.render()}")
    if unrevocable:
        click.echo(
            f"No revoke command for the recorded grant on "
            f"{', '.join(unrevocable)}: revoke it by hand if it is no longer "
            "needed."
        )
    revoke = bool(revocations) and not keep_grants
    topic = _resolve_trigger_topic(listing)
    trigger = (
        _plan_update_trigger(
            topic=topic,
            agent_key=agent_key,
            observed_project=str(entry.get("observed_project_id") or ""),
            engine_id="",
        )
        if topic and is_storage_key(agent_key)
        else None
    )
    removing = bool(trigger and trigger.commands)
    for line in trigger.render_lines() if trigger else []:
        click.echo(line)
    if not attached and not removing:
        click.echo(f"{agent_key} is not attached.")
        _delete_agent_key(agent_key, target)
        return
    if not yes:
        if not attached:
            question = f"Remove the update trigger of {agent_key}?"
        else:
            also = [
                *([f"revoke {len(revocations)} grant(s)"] if revoke else []),
                *(["remove its update trigger"] if removing else []),
            ]
            question = (
                f"Detach {agent_key} from {listing.get('prefix', '')}"
                f"{''.join(f' and {a}' for a in also)}?"
            )
        click.confirm(f"\n{question}", abort=True)

    if revoke:
        _revoke_grants(agent_key, revocations, target)
    if trigger and removing:
        click.echo(f"\nRemoving the update trigger of {agent_key}:")
        _apply_update_trigger(trigger)
    if not attached:
        _delete_agent_key(agent_key, target)
        return

    _fetch_agents_result(
        AGENTS_DETACH_ROUTE, _build_agent_selector(agent_key), **target
    )
    click.echo(f"Detached {agent_key}.")
    _delete_agent_key(agent_key, target)


@cli.command("delete-triggers", hidden=True)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help="Remove them, with your credentials. Plans otherwise.",
)
@_add_target_options
def delete_triggers_cmd(apply_, *, url, resource) -> None:
    """Remove the update trigger of every attached agent, keeping the attachments.

    Hidden because `agents-cli infra single-project --destroy` runs it before
    it deletes the topic the triggers publish to, which would leave each sink
    exporting to nothing. `detach` removes one agent's trigger.
    """
    target = {"url": url, "resource": resource}
    listing = _fetch_agents_result(AGENTS_LIST_ROUTE, None, **target)
    topic = _resolve_trigger_topic(listing)
    if not topic:
        click.echo(
            "This AQuA reports no topic for update triggers, so attach wired "
            "none to it."
        )
        return
    planned = 0
    for entry in listing.get("agents") or []:
        key = str(entry.get("agent_key") or entry.get("agent_name") or "")
        if not is_storage_key(key):
            continue
        trigger = _plan_update_trigger(
            topic=topic,
            agent_key=key,
            observed_project=str(entry.get("observed_project_id") or ""),
            engine_id="",
        )
        for line in trigger.render_lines():
            click.echo(line)
        planned += len(trigger.commands)
        if apply_ and trigger.commands:
            _apply_update_trigger(trigger)
    if not planned:
        click.echo("No attached agent has an update trigger to remove.")
    elif not apply_:
        click.echo("\nRe-run with --apply to run them.")


# --------------------------------------------------------------------------- #
# Cloud Trace spans                                                            #
# --------------------------------------------------------------------------- #
@cli.command("link-traces", hidden=True)
@click.option(
    "--project",
    envvar="GOOGLE_CLOUD_PROJECT",
    required=True,
    help="The project whose spans to link (default: GOOGLE_CLOUD_PROJECT).",
)
@click.option(
    "--location",
    default=None,
    help=(
        "Where to create the _Trace bucket when there is none (default: the "
        "project's default storage location, else us). Ignored when the "
        "bucket exists."
    ),
)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help="Create the bucket and the link, with your credentials.",
)
def link_traces_cmd(project, location, apply_) -> None:
    """Make a project's Cloud Trace spans readable from BigQuery.

    Prints the gcloud commands that enable the Observability API and create
    the `_Trace` bucket and its BigQuery link, whichever are missing; --apply
    runs them with your credentials, which needs roles/observability.editor,
    bigquery.datasets.get and gcloud 586.0.0 or later. Nothing is ever
    deleted: the bucket and the link are project-wide.

    Hidden because `attach` links traces for its agent directly. Requires no
    AQuA deployment.
    Progress goes to stderr; stdout carries only the JSON result: the link id,
    span view and BigQuery location AQuA reads, and the commands run or
    planned.
    """
    api = rest_client_lib.RestClient(get_adc_token)
    try:
        link = trace_link_lib.resolve_trace_link(
            api, project, location=location
        )
    except trace_link_lib.TraceLinkError as exc:
        raise click.ClickException(str(exc)) from exc
    for note in link.notes:
        click.echo(note, err=True)
    commands = link.build_needed_argvs()
    if apply_ and commands:
        link, commands = _create_trace_link(
            link,
            run_command=_run_command_to_stderr,
            echo=lambda line: click.echo(line, err=True),
            location=location,
        )
    elif commands:
        for line in _render_link_lines(link):
            click.echo(line, err=True)
    else:
        click.echo(
            f"{trace_link_lib.TRACE_BUCKET} in {link.location} is linked to "
            f"BigQuery as {link.link_id}.",
            err=True,
        )
    _emit_json(
        trace_link_lib.format_trace_link_report(link, commands, applied=apply_)
    )


# --------------------------------------------------------------------------- #
# Source snapshots                                                             #
# --------------------------------------------------------------------------- #
def _publish_source(
    *,
    root: pathlib.Path,
    agent_key: str,
    observed_agent_resource: str | None,
    revision: str,
    force: bool,
    dry_run: bool,
    target: dict[str, Any],
) -> dict[str, Any]:
    """Snapshots a project's source and uploads it to AQuA, unless already there.

    Steps:
    1. Find the observed engine: the one given, else the project's
       `deployment_metadata.json`.
    2. Resolve the revision with the user's credentials: the newest one the
       engine reports, unless given.
    3. Find the key of the agent the snapshot belongs to, unless given: the
       attachment that records the engine, else the agent the deployment
       watches.
    4. Ask the deployment whether that revision is already published, and stop
       if it is, unless forced.
    5. Pack the files and upload them, manifest last. A dry run stops before.

    Progress goes to stderr, so a command's stdout stays its JSON result.

    Args:
        root: The observed agent's project root.
        agent_key: Key of the agent the snapshot belongs to; empty to find it.
        observed_agent_resource: The observed engine; None to read it from the
            project's deployment metadata.
        revision: Revision to publish as; empty for the newest one.
        force: Upload even if the revision is already published.
        dry_run: Pack and report, and upload nothing.
        target: AQuA's `url` and `resource`.

    Returns:
        `{"agent_key", "revision", "published", ...}`: the stored snapshot's
        summary when it was uploaded or already there, else what would be.

    Raises:
        source_snapshot_lib.SnapshotError: If the snapshot cannot be resolved,
            packed or uploaded.
        click.ClickException: If AQuA cannot be reached.
    """
    metadata: dict[str, Any] = {}
    if (
        not observed_agent_resource
        or (root / source_snapshot_lib.METADATA_FILE).is_file()
    ):
        metadata = source_snapshot_lib.load_deployment_metadata(root)
    deployed = str(metadata.get("remote_agent_runtime_id") or "")
    engine = observed_agent_resource or deployed
    if deployed and deployed != engine:
        raise source_snapshot_lib.SnapshotError(
            f"the project at {root} deployed {deployed}, not {engine}, so its "
            "source is not that engine's"
        )

    if not revision:
        api = rest_client_lib.RestClient(get_adc_token)
        try:
            revision = source_snapshot_lib.resolve_newest_revision(
                source_snapshot_lib.list_revisions(engine, api)
            )
        except rest_client_lib.ApiError as exc:
            raise source_snapshot_lib.SnapshotError(
                f"could not list the runtime revisions of {engine}: {exc}"
            ) from exc
    if not agent_key:
        listing = _fetch_agents_result(AGENTS_LIST_ROUTE, None, **target)
        agent_key = source_snapshot_lib.resolve_agent_for_engine(
            listing, engine
        )
        if not agent_key:
            raise source_snapshot_lib.SnapshotError(
                f"no attached agent observes {engine}; attach it first, or "
                "name the agent's key"
            )

    if not force and not dry_run:
        status = _fetch_command_result(
            path=SOURCE_ROUTE,
            payload=GetSourceSnapshotArgs(
                **source_snapshot_lib.build_agent_selector(agent_key),
                revision=revision,
            ),
            **target,
        )
        # The revision is compared too: an engine that predates `revision`
        # on this route reports its newest snapshot, whichever that is.
        if (
            isinstance(status, dict)
            and status.get("available")
            and status.get("revision") == revision
        ):
            click.echo(
                f"AQuA: revision {revision} of {agent_key} is already "
                f"published at {status.get('uri', '')}; --force uploads it again.",
                err=True,
            )
            return {
                "agent_key": agent_key,
                "revision": revision,
                "published": False,
                **status,
            }

    snapshot = source_snapshot_lib.pack(
        root,
        engine=engine,
        agent_directory=str(metadata.get("agent_directory") or ""),
    )
    click.echo(
        f"AQuA: snapshotting {engine} revision {revision} as {agent_key}: "
        f"{len(snapshot.files)} files, {snapshot.total_bytes} bytes "
        f"({snapshot.truncated_files} truncated, "
        f"{snapshot.manifest['omitted_files']} omitted, "
        f"{snapshot.skipped_files} media skipped)",
        err=True,
    )
    if dry_run:
        return {
            "agent_key": agent_key,
            "revision": revision,
            "published": False,
            "file_count": len(snapshot.files),
            "total_bytes": snapshot.total_bytes,
            "truncated_files": snapshot.truncated_files,
            "omitted_files": snapshot.manifest["omitted_files"],
            "skipped_files": snapshot.skipped_files,
        }

    client = _build_client(target["url"], target["resource"])

    def post(route: Route[ArgsT], body: ArgsT) -> dict[str, Any]:
        try:
            return asyncio.run(client.post_command(route, body))
        except Exception as exc:  # Surface auth, network, and agent errors as user-facing CLI exceptions.
            raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc

    def report(done: int, total: int) -> None:
        click.echo(f"  uploaded batch {done}/{total}", err=True)

    result = source_snapshot_lib.upload(
        post,
        agent_key=agent_key,
        revision=revision,
        snapshot=snapshot,
        upload_route=SOURCE_UPLOAD_ROUTE,
        commit_route=SOURCE_COMMIT_ROUTE,
        on_batch=report,
    )
    click.echo(f"AQuA: published {result.get('uri', '')}", err=True)
    return {"published": True, **result}


def _publish_first_snapshot(
    root: pathlib.Path,
    agent_key: str,
    observed_agent_resource: str | None,
    target: dict[str, Any],
) -> None:
    """Publishes an attached agent's source, reporting rather than raising a failure.

    The agent is already attached when this runs, so a snapshot that cannot be
    published does not undo or fail the attach; root-cause analysis only cites
    no code until one is.

    Args:
        root: The observed agent's project root.
        agent_key: Key of the agent just attached.
        observed_agent_resource: The observed engine, if given.
        target: AQuA's `url` and `resource`.
    """
    click.echo(f"\nPublishing the source of {agent_key} from {root}:")
    try:
        _publish_source(
            root=root,
            agent_key=agent_key,
            observed_agent_resource=observed_agent_resource,
            revision="",
            force=False,
            dry_run=False,
            target=target,
        )
    except (source_snapshot_lib.SnapshotError, click.ClickException) as exc:
        message = (
            exc.format_message()
            if isinstance(exc, click.ClickException)
            else str(exc)
        )
        click.echo(
            f"No source snapshot was published ({message}). Root-cause analysis "
            "cites no code until one is; publish it with: agents-cli aqua "
            "publish-source"
        )


@cli.command("publish-source")
@click.argument("agent_key", default="")
@click.option(
    "--source-root",
    type=click.Path(file_okay=False, path_type=pathlib.Path),
    default=pathlib.Path(),
    help="The observed agent's project directory (default: the current one).",
)
@click.option(
    "--observed-agent-resource",
    default=None,
    help=(
        "The observed engine, projects/P/locations/L/reasoningEngines/ID "
        "(default: from the project's deployment_metadata.json)."
    ),
)
@click.option(
    "--revision",
    default="",
    help="Revision to publish as (default: the newest one the engine reports).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Upload even if this revision is already published.",
)
@click.option(
    "--dry-run", is_flag=True, help="Pack and report, and upload nothing."
)
@_add_target_options
def publish_source_cmd(
    agent_key,
    source_root,
    observed_agent_resource,
    revision,
    force,
    dry_run,
    *,
    url,
    resource,
) -> None:
    """Publish the observed agent's source, as deployed, to AQuA.

    Root-cause analysis cites the source of the revision that ran. This packs
    the source files -- code, configuration and prompts -- among those
    `agents-cli deploy` packages, and AQuA stores them under
    AGENT_KEY and the engine's newest revision. Without AGENT_KEY, the
    attached agent that observes the engine, else the one AQuA watches.
    `list-agents` shows each attached agent's key.

    `agents-cli deploy` and `agents-cli aqua attach` run this in a project
    with AQuA added; run it by hand to publish what they did not.
    """
    try:
        result = _publish_source(
            root=source_root.resolve(),
            agent_key=agent_key,
            observed_agent_resource=observed_agent_resource,
            revision=revision,
            force=force,
            dry_run=dry_run,
            target={"url": url, "resource": resource},
        )
    except source_snapshot_lib.SnapshotError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit_json(result)


if __name__ == "__main__":
    cli()
