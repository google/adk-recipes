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
      aqua_cli.py publish-source [AGENT_NAME] [--source-root DIR] [--dry-run]

* **Attaching an agent** -- how the deployment observes it. Interactive: each
  prints a plan and asks before it overwrites or deletes (``--yes`` skips)::

      aqua_cli.py attach [AGENT_NAME] [--lookback-days 30] [--dry-run] ...
      aqua_cli.py attach --observed-agent-resource \\
          projects/P/locations/L/reasoningEngines/ID
      aqua_cli.py detach AGENT_NAME

  With ``--observed-agent-resource``, the settings no flag gives are discovered
  from the observed engine (:mod:`ambient_quality_cli.discovery`). For each
  read the deployment is denied, ``attach`` prints the command that grants it,
  and ``--apply`` runs those commands, then the ones a re-check calls for
  once a skipped read becomes possible (:mod:`ambient_quality_cli.grants`),
  and records them on the attachment; ``detach --apply`` revokes those it
  recorded.

  With ``--source-root``, ``attach`` also publishes the agent's source for
  the engine's newest revision, as ``publish-source`` does
  (:mod:`ambient_quality_cli.source_snapshot`).

  ``--apply`` on ``attach`` also applies the agent's triggers -- the scheduled
  investigation and the audit sink on its updates -- with Terraform, wired to the
  engine as it reports itself, with their state in ``./.aqua/``; on ``detach``
  it destroys them.

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
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

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
from ambient_quality_cli import traces as traces_lib
from ambient_quality_shared.agent_client import (
    AgentRuntimeClient,
    build_client_from_env,
    get_adc_token,
    resolve_a2a_base_url,
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
    SOURCE_COMMIT_ROUTE,
    SOURCE_ROUTE,
    SOURCE_UPLOAD_ROUTE,
)
from ambient_quality_shared.terraform_state import (
    ATTACH_INSTANCE_OUTPUT,
    ATTACH_ROOT,
    STATE_DIR_NAME,
    build_attach_key,
    build_state_path,
    read_state_outputs,
    run_terraform,
)

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
def _build_client(url: str | None, resource: str | None):
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


def _echo_target(url: str | None, resource: str | None) -> None:
    """Prints the resolved target to stderr to keep stdout reserved for JSON output.

    Args:
        url: Explicit engine endpoint URL, if provided.
        resource: Explicit engine resource name, if provided.
    """
    target = (
        url
        or resource
        or os.getenv("AGENT_ENGINE_RESOURCE_ID")
        or "(env-configured)"
    )
    click.echo(f"Querying agent: {target}", err=True)


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


def _fetch_command_result(*, url, resource, path, payload=None) -> Any:
    """Posts to an agent command route and returns the parsed JSON response.

    Args:
        url: Explicit engine endpoint URL.
        resource: Explicit engine resource name.
        path: Relative command route path.
        payload: Optional JSON request body.

    Returns:
        Decoded JSON response from the command endpoint.

    Raises:
        click.ClickException: If the network request fails or authentication errors occur.
    """
    client = _build_client(url, resource)
    _echo_target(url, resource)
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
    client, run_id: str, *, interval: float, timeout: float
) -> tuple[Any, bool]:
    """Polls the investigation status endpoint until a terminal state or timeout is reached.

    Args:
        client: API client for issuing requests.
        run_id: Unique identifier of the investigation run.
        interval: Delay in seconds between polling attempts.
        timeout: Maximum duration in seconds to wait before giving up.

    Returns:
        Tuple of (latest_payload, timed_out_boolean).
    """
    deadline = time.monotonic() + timeout
    while True:
        payload = await client.post_command(
            INVESTIGATIONS_GET_ROUTE, {"run_id": run_id}
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
def list_investigations_cmd(*, url, resource) -> None:
    """List the 50 most recent runs as JSON (``{"runs": [...]}``), newest last.

    The ``events`` field in each entry is empty; see
    ``get-investigation --help`` for run record fields.
    """
    _emit_result(
        _fetch_command_result(
            url=url,
            resource=resource,
            path=INVESTIGATIONS_LIST_ROUTE,
        )
    )


@cli.command("investigation-stats")
@_add_target_options
def get_investigation_stats_cmd(*, url, resource) -> None:
    """Total every run's counters as JSON (``{"stats": {...}}``).

    Unlike ``list-investigations``, which returns one capped page, this is
    summed over every investigation the deployment ever recorded.
    """
    _emit_result(
        _fetch_command_result(
            url=url,
            resource=resource,
            path=INVESTIGATIONS_STATS_ROUTE,
        )
    )


@cli.command("get-investigation")
@click.argument("run_id")
@_add_target_options
@_add_wait_options
def get_investigation_cmd(
    run_id: str, *, url, resource, wait, interval, timeout
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
    client = _build_client(url, resource)
    _echo_target(url, resource)
    try:
        if wait:
            payload, timed_out = asyncio.run(
                _poll_until_terminal(
                    client, run_id, interval=interval, timeout=timeout
                )
            )
        else:
            payload = asyncio.run(
                client.post_command(
                    INVESTIGATIONS_GET_ROUTE, {"run_id": run_id}
                )
            )
            timed_out = False
    except Exception as exc:
        raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc
    _emit_run_record(payload, timed_out=timed_out)


@cli.command("schedule-investigation")
@_add_target_options
@_add_wait_options
def schedule_investigation_cmd(
    *, url, resource, wait, interval, timeout
) -> None:
    """Start a run and print the new run record as JSON.

    With --wait, poll the new run_id until it reaches a terminal status. See
    ``get-investigation --help`` for run record fields.
    """
    client = _build_client(url, resource)
    _echo_target(url, resource)
    try:
        payload = asyncio.run(
            client.post_command(INVESTIGATIONS_SCHEDULE_ROUTE)
        )
        timed_out = False
        run_id = payload.get("run_id") if isinstance(payload, dict) else None
        if wait and run_id:
            payload, timed_out = asyncio.run(
                _poll_until_terminal(
                    client, run_id, interval=interval, timeout=timeout
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
def list_insights_cmd(
    *, status, run_id, root_cause, page_token, url, resource
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
    args: dict[str, Any] = {}
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
            payload=args,
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
def get_insight_cmd(
    insight_id: str,
    *,
    run_id,
    no_traces,
    page_token,
    url,
    resource,
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
    args: dict[str, Any] = {"insight_id": insight_id}
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
            payload=args,
        )
    )


@cli.command("show-config")
@_add_target_options
def show_config_cmd(*, url, resource) -> None:
    """Show the effective AQA configuration as JSON."""
    _emit_result(
        _fetch_command_result(url=url, resource=resource, path=CONFIG_ROUTE)
    )


@cli.command("list-memories")
@_add_target_options
def list_memories_cmd(*, url, resource) -> None:
    """List what the developer asked AQuA to remember about the agent, newest first.

    Read-only: memories are created only by the developer, in AQuA's chat.
    Exits with an error unless the memories were read, so an empty list
    always means nothing is remembered.
    """
    payload = _fetch_command_result(
        url=url, resource=resource, path=MEMORIES_LIST_ROUTE
    )
    _emit_result(payload)
    if not isinstance(payload, dict) or payload.get("available") is not True:
        sys.exit(EXIT_ERROR)


# --------------------------------------------------------------------------
# metrics: the code-metric library the deployment scores with
# --------------------------------------------------------------------------


def _resolve_metrics_bucket(bucket: str, *, url, resource) -> str:
    """Resolves the GCS bucket for metric publishing from CLI flags or deployment configuration.

    Args:
        bucket: Explicit bucket name from CLI flag, if provided.
        url: Deployment HTTP endpoint, if connecting directly.
        resource: Agent Runtime resource name.

    Returns:
        Resolved GCS bucket name.

    Raises:
        click.ClickException: If no bucket is specified and the deployment does not configure one.
    """
    if bucket:
        return bucket
    payload = _fetch_command_result(
        url=url, resource=resource, path=CONFIG_ROUTE
    )
    name = ""
    if isinstance(payload, dict):
        inner = payload.get("config")
        config = inner if isinstance(inner, dict) else payload
        name = str(config.get("metrics_gcs_bucket") or "")
    if not name:
        raise click.ClickException(
            "This deployment reports no metrics_gcs_bucket. Apply the Terraform "
            "that creates it, or pass --bucket."
        )
    return name


def _render_report_lines(report: metrics_lib.Report) -> list[str]:
    """Formats summary lines for a validated metric library report.

    Args:
        report: Validation report containing runnable, skipped, and costly metrics.

    Returns:
        List of formatted display strings for terminal output.
    """
    lines = [f"{len(report.metrics)} metric(s) AQuA will run:"]
    lines += [
        f"  {m.name}  ({m.source_path})  {m.expected}" for m in report.metrics
    ]
    if report.skipped:
        lines.append(f"{len(report.skipped)} skipped, which AQuA cannot run:")
        lines += [f"  {name}: {why}" for name, why in report.skipped]
    costly = report.list_costly_metrics()
    if costly:
        lines.append(
            f"WARNING: {len(costly)} metric(s) import a model client. Each costs "
            "one model call per session per sweep, on the deployment's "
            "credentials, up to DATA_EVALUATION_CAP sessions:"
        )
        lines += [f"  {m.name}: {', '.join(m.model_modules)}" for m in costly]
    judged = report.list_judged_metrics()
    if judged:
        calls = sum(m.judge_samples for m in judged)
        names = ", ".join(
            m.name
            if m.judge_samples == 1
            else f"{m.name} ({m.judge_samples} samples)"
            for m in judged
        )
        lines.append(
            f"{len(judged)} metric(s) are judged by the eval service's model, "
            f"{calls} model call(s) per session per sweep: {names}"
        )
        lines += [
            f"  {m.name}: reads {{prompt}}, which is set only on single-turn "
            "sessions, as in agents-cli; it errors on multi-turn ones"
            for m in judged
            if m.reads_prompt
        ]
        lines += [
            f"  {m.name}: scores only -- no threshold, so it files no findings"
            for m in judged
            if m.scores_only
        ]
    return lines


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
    "--bucket", default="", help="Target bucket, else ask the deployment."
)
@click.option(
    "--dry-run", is_flag=True, help="Validate and plan, upload nothing."
)
@click.option("--yes", is_flag=True, help="Do not ask before deleting.")
@_add_target_options
def publish_metrics_cmd(
    directory, bucket, dry_run, yes, *, url, resource
) -> None:
    """Validate DIRECTORY and mirror it into the deployment's metrics bucket."""
    try:
        local = metrics_lib.read_directory(directory)
        report = metrics_lib.validate(local)
    except metrics_lib.ValidationError as exc:
        raise click.ClickException(f"Refusing to publish: {exc}") from exc

    for line in _render_report_lines(report):
        click.echo(line)

    name = _resolve_metrics_bucket(bucket, url=url, resource=resource)
    token = get_adc_token()
    try:
        uploads, deletions = metrics_lib.compute_mirror_plan(
            local, metrics_lib.list_objects(name, token)
        )
    except metrics_lib.ValidationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"\ngs://{name}/{metrics_lib.LIBRARY_PREFIX}")
    for relative in uploads:
        click.echo(f"  upload  {relative}")
    for obsolete in deletions:
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

    try:
        # Upload modules before configuration so partial failures leave a working configuration referencing existing files.
        for relative in uploads:
            metrics_lib.upload(name, token, relative, local[relative])
        # Delete after upload so a partial failure leaves the previous library intact.
        for obsolete in deletions:
            metrics_lib.delete(name, token, obsolete)
    except metrics_lib.ValidationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"\nPublished {len(uploads)} object(s), deleted {len(deletions)}."
    )


@metrics_group.command("list")
@click.option(
    "--bucket", default="", help="Target bucket, else ask the deployment."
)
@_add_target_options
def list_metrics_cmd(bucket, *, url, resource) -> None:
    """List and validate metrics currently published in the deployment bucket."""
    name = _resolve_metrics_bucket(bucket, url=url, resource=resource)
    token = get_adc_token()
    try:
        items = metrics_lib.list_objects(name, token)
    except metrics_lib.ValidationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"gs://{name}/{metrics_lib.LIBRARY_PREFIX}")
    if not items:
        click.echo("  (empty -- this deployment has nothing to score with)")
        return
    for item in items:
        click.echo(
            f"  {item['name']}  {item.get('size', '?')}B  {item.get('updated', '')}"
        )

    try:
        report = metrics_lib.validate(
            metrics_lib.download(name, token, [i["name"] for i in items])
        )
    except metrics_lib.ValidationError as exc:
        raise click.ClickException(
            f"The published library is broken: {exc}"
        ) from exc
    click.echo("")
    for line in _render_report_lines(report):
        click.echo(line)


@metrics_group.command("destroy")
@click.option(
    "--bucket", default="", help="Target bucket, else ask the deployment."
)
@click.option(
    "--apply", "apply_", is_flag=True, help="Delete. Plans otherwise."
)
@_add_target_options
def destroy_metrics_cmd(bucket, apply_, *, url, resource) -> None:
    """Delete the published metric library from GCS. Plans by default."""
    name = _resolve_metrics_bucket(bucket, url=url, resource=resource)
    token = get_adc_token()
    try:
        items = metrics_lib.list_objects(name, token)
    except metrics_lib.ValidationError as exc:
        raise click.ClickException(str(exc)) from exc
    if not items:
        # Treat an already-empty bucket as successfully destroyed.
        click.echo(
            f"gs://{name}/{metrics_lib.LIBRARY_PREFIX} is already empty."
        )
        return
    for item in items:
        click.echo(
            f"  {'delete' if apply_ else 'would delete'}  {item['name']}"
        )
    if not apply_:
        click.echo(
            f"\n{len(items)} object(s). Re-run with --apply to delete them."
        )
        return
    try:
        for item in items:
            metrics_lib.delete(name, token, item["name"])
    except metrics_lib.ValidationError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"\nDeleted {len(items)} object(s). This deployment now has nothing to "
        "score with, so investigations run session review alone until you "
        "publish again."
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
# `--apply` is the one step that is not an API call. Cloud Scheduler and the
# audit log sink act on three of the stored fields, and they are Terraform
# resources in `terraform/examples/attach`, applied once per agent with its own
# state in `./.aqua/`. The engine says both which values to apply and which
# instance to wire them to -- its region, resource name, topic and caller
# account -- so `--apply` needs nothing of AQuA but the resource name it called.


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
            help="Where the telemetry is read from: big_query, cloud_ops or cloud_logging.",
        ),
        click.option(
            "--telemetry-dataset", default=None, help="BigQuery dataset."
        ),
        click.option(
            "--telemetry-table", default=None, help="Table or view in it."
        ),
        click.option(
            "--telemetry-location", default=None, help="Its BigQuery location."
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
            help="Cron for the scheduled sweep. Takes effect when Terraform is applied.",
        ),
        click.option(
            "--scheduled-trigger/--no-scheduled-trigger",
            "scheduled_trigger_enabled",
            default=None,
            help="Whether that schedule is live. Takes effect when Terraform is applied.",
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
                "trigger a sweep once Terraform is applied."
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
    path: str, payload: Any, *, url, resource
) -> dict[str, Any]:
    """Posts to an `agents/*` route and fails the command on an error envelope.

    Args:
        path: Relative command route path.
        payload: Optional JSON request body.
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
    plan: dict[str, Any], *, apply: bool = False, watched_agent: str = ""
) -> list[str]:
    """Renders an attach plan as the lines shown before confirming.

    Args:
        plan: Dry-run response from the attach route.
        apply: Whether the triggers are applied in the same run, which puts
            the Terraform-backed fields into effect.
        watched_agent: The agent the deployment investigates, when the plan's
            agent is not it.

    Returns:
        Lines to print, in order.
    """
    lines = [str(plan.get("object", ""))]
    if not plan.get("attached"):
        base = (
            "the engine's environment"
            if plan.get("watched")
            else "the defaults"
        )
        lines.append(f"  (not attached yet -- starting from {base})")
    changes = plan.get("changes") or []
    if not changes:
        lines.append("  (no change)")
    for change in changes:
        lines.append(
            f"  {change['field']}: {_format_value(change['before'])} -> "
            f"{_format_value(change['after'])}"
        )
    for adjusted in plan.get("adjusted") or []:
        lines.append(
            f"\n{adjusted['field']}: asked for {_format_value(adjusted['requested'])}, "
            f"the deployment stores {_format_value(adjusted['stored'])}."
        )
    if (needs_apply := plan.get("needs_apply")) and not apply:
        lines.append(
            f"\nStored, but not yet in effect: {', '.join(needs_apply)}.\n"
            "Cloud Scheduler and the audit log sink act on those, so they take "
            "effect when attach --apply applies them with Terraform."
        )
    # With --apply, the refusal explains this instead.
    if not plan.get("watched") and not apply:
        lines.append(
            f"\nThis deployment investigates {watched_agent or 'another agent'}, "
            f"not {plan.get('agent_name')}. An attachment for any other agent is "
            "stored, but not read until one AQuA can watch several.\n"
            f"{_render_watch_remedy(plan, watched_agent)}"
        )
    return lines


def _fetch_watched_agent(target: dict[str, Any]) -> str:
    """Fetches the agent the deployment investigates, from its agent listing.

    The engine's environment names it, or else it is the default attached
    agent; the listing reports both.

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
    return str(
        listing.get("environment_agent") or listing.get("default_agent") or ""
    )


def _render_watch_remedy(plan: dict[str, Any], watched_agent: str) -> str:
    """Renders how to make the deployment investigate the planned agent.

    An agent the engine's environment names cannot be displaced by an attach.
    Otherwise the deployment investigates its default attached agent, which
    `--default` changes.

    Args:
        plan: Dry-run response from the attach route.
        watched_agent: The agent the deployment investigates; empty if unknown.

    Returns:
        One sentence, without a trailing newline.
    """
    name = plan.get("agent_name")
    watched = watched_agent or "the agent it investigates now"
    if plan.get("environment_agent"):
        return (
            f"The engine's environment names {watched}, so only a separate AQuA "
            f"deployment can investigate {name}."
        )
    return (
        f"To investigate {name} here instead, attach it with --default; "
        f"{watched} stays attached, but is no longer investigated."
    )


_ACCESS_STATUS_LABELS = {
    "passed": "ok",
    "empty": "empty",
    "missing": "MISSING",
    "denied": "DENIED",
    "error": "error",
    "skipped": "skipped",
}
"""How each access check status is shown; the capitals are the two that need
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


def _validate_access(access: dict[str, Any]) -> str:
    """Checks that the telemetry an attach points at exists.

    A table with no rows, or one AQuA may not read yet, does not stop the attach:
    a new agent may have had no traffic, and a grant can follow.

    Args:
        access: The plan's `access` section.

    Returns:
        The reason the attach is refused, or an empty string if it is not.
    """
    missing = [
        c for c in access.get("checks") or [] if c.get("status") == "missing"
    ]
    if not missing:
        return ""
    targets = ", ".join(str(c.get("target") or c.get("name")) for c in missing)
    return (
        f"Not attaching: {targets} does not exist, so there is nothing to investigate. "
        "For an agents-cli project, `agents-cli infra single-project --apply` creates "
        "the telemetry sink and dataset; `agents-cli deploy` alone creates neither."
    )


_RECHECK_DELAYS_SECONDS = (0, 15, 45, 60, 60)
"""How long to wait before each access check after the grants run. A new IAM
binding can take a minute or more to reach every service that enforces it, and
while it has not, the checks behind it stay skipped and their grants unknown;
three minutes in all gives a single `--apply` room to reach them."""

_MAX_GRANT_ROUNDS = 3
"""How many rounds of grants one `attach --apply` runs. A check that needs an
earlier read is skipped until that read is allowed, so its grant only shows up
after the earlier grant: the payload check waits for the rows check, which
waits for the table check, unless the attachment names the payload bucket. The
table and rows checks share the dataset grant, so that chain takes two rounds;
the third is spare, and the cap stops a check that keeps calling for new
grants."""


def _apply_grants(
    grants: list[grants_lib.Grant],
    request: dict[str, Any],
    *,
    yes: bool,
    target: dict[str, Any],
    agent_name: str,
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
    4. If the check calls for grants none of the rounds made, print them and
       start the next round with them, up to `_MAX_GRANT_ROUNDS`.
    5. Show the last check's result, the grants still denied, any grant left
       unrun, and the denied reads no command covers.

    Args:
        grants: Commands the plan printed, already confirmed.
        request: The attach request, without `dry_run`.
        yes: Whether to skip the confirmation of later rounds.
        target: Engine target options for the route call.
        agent_name: The attached agent, as the plan resolved it.

    Raises:
        click.ClickException: If a command cannot run or fails.
        click.Abort: If the user declines a later round.
    """
    ran: set[tuple[str, str]] = set()
    pending = grants
    access: dict[str, Any] = {}
    needed: list[grants_lib.Grant] = []
    ungranted: list[str] = []
    new: list[grants_lib.Grant] = []
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
            made = [g for g in exc.completed if isinstance(g, grants_lib.Grant)]
            if made:
                _record_grants(agent_name, made, target)
            raise click.ClickException(str(exc)) from exc
        _record_grants(agent_name, pending, target)
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
                {**request, "dry_run": True, "check_access": True},
                **target,
            )
            access = plan.get("access") or {}
            needed, ungranted = grants_lib.build_grants(access)
            if not any(grant.key in ran for grant in needed):
                break

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
                "binding has propagated, run attach --apply again to grant "
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
        click.echo("Or run attach --apply again to run them.")


def _record_grants(
    agent_name: str, grants: list[grants_lib.Grant], target: dict[str, Any]
) -> None:
    """Records grants that ran on the attachment, for `detach --apply` to revoke.

    A refused record does not fail the command: the grants are already made,
    and the rest of `--apply` still has to run. The user is shown how to
    revoke them instead, since `detach --apply` will not.

    Args:
        agent_name: Attached agent.
        grants: Grants that ran.
        target: Engine target options for the route call.
    """
    try:
        _fetch_agents_result(
            AGENTS_APPLIED_ROUTE,
            {
                "agent_name": agent_name,
                "granted": [g.to_record() for g in grants],
            },
            **target,
        )
    except click.ClickException as exc:
        revocations, _ = grants_lib.build_revocations(
            [g.to_record() for g in grants]
        )
        click.echo(
            f"\nCould not record the grants on the attachment: "
            f"{exc.format_message()}\ndetach --apply will not revoke them; "
            "to revoke them by hand:"
        )
        for revocation in revocations:
            click.echo(f"  {revocation.render()}")
        return
    click.echo(
        f"\nRecorded {len(grants)} grant(s) on the attachment; "
        "detach --apply revokes them."
    )


def _resolve_discovered_fields(
    agent_name: str,
    fields: dict[str, Any],
    *,
    observed_agent_resource: str | None,
    hints: dict[str, discovery_lib.DiscoveredValue],
    window_days: int,
) -> tuple[str, dict[str, Any], list[str]]:
    """Fills in the attach fields no flag set: from the engine, then hints.

    Args:
        agent_name: AGENT_NAME as given; empty if not.
        fields: Fields the flags set.
        observed_agent_resource: The observed engine, if given.
        hints: Values a wrapper read from the project's own files.
        window_days: How far back discovery counts telemetry rows.

    Returns:
        Tuple of (agent name, fields to send, lines to show before the plan).

    Raises:
        click.ClickException: If discovery cannot go on.
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
    lines = discovery_lib.render_discovery_lines(
        observed_agent_resource, resolved, discovery
    )
    return agent_name, filled, lines


def _resolve_attach_root() -> pathlib.Path:
    """Resolves the Terraform root holding one agent's triggers.

    This file sits at `src/ambient_quality_cli/` in AQuA's checkout and in the
    copy agents-cli vendors, so the root is at the same place relative to it in
    both.

    Returns:
        The root directory.

    Raises:
        click.ClickException: If the root is not there.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / ATTACH_ROOT
    if not root.is_dir():
        raise click.ClickException(
            f"AQuA's Terraform for the triggers is not at {root}."
        )
    return root


_TRIGGER_TARGET_KEYS = frozenset(
    {"region", "aqua_engine", "ambient_topic", "ambient_caller_email"}
)
"""What the engine reports as its trigger target: the attach root's instance
variables."""


def _validate_trigger_target(target: Any) -> dict[str, str]:
    """Checks the trigger target an engine reported.

    Args:
        target: The `trigger_target` of an attach or list response.

    Returns:
        The target, keyed by the attach root's instance variables.

    Raises:
        click.ClickException: If the engine reported none, or not all of it.
    """
    if not isinstance(target, dict) or set(target) != _TRIGGER_TARGET_KEYS:
        raise click.ClickException(
            "This AQuA reports nothing to wire triggers to. Either:\n"
            "  - it names its observed agent, and its own Terraform runs that "
            "agent's schedule and update trigger: change them with "
            "`agents-cli infra single-project --apply`;\n"
            "  - its engine predates attach --apply: re-apply its Terraform with "
            "`agents-cli infra single-project --apply`, then `agents-cli deploy`;\n"
            "    in the agent's project, add --apply-aqua and --deploy-aqua;\n"
            "  - or it is not deployed on Agent Runtime."
        )
    return {key: str(value) for key, value in target.items()}


def _build_trigger_vars(
    agent_name: str, triggers: dict[str, Any]
) -> dict[str, str]:
    """Builds the attach root's agent variables from the values to apply.

    A field the object leaves empty is left to the root's default.

    Args:
        agent_name: Attached agent.
        triggers: Terraform-backed field values, as the attach route returns.

    Returns:
        Variables keyed by name, as strings for `-var`.
    """
    tf_vars = {
        "observed_agent_name": agent_name,
        "scheduled_trigger_enabled": (
            "true"
            if triggers.get("scheduled_trigger_enabled", True)
            else "false"
        ),
    }
    if triggers.get("investigation_schedule"):
        tf_vars["investigation_schedule"] = str(
            triggers["investigation_schedule"]
        )
    if triggers.get("observed_engine_id"):
        tf_vars["observed_engine_id"] = str(triggers["observed_engine_id"])
    if triggers.get("observed_project_id"):
        tf_vars["observed_project_id"] = str(triggers["observed_project_id"])
    return tf_vars


class _TriggerInstance:
    """The attach root and the AQuA instance one agent's triggers are wired to."""

    def __init__(self, target: dict[str, str]) -> None:
        """Resolves both before anything is stored or applied.

        Args:
            target: The instance variables, as the engine reports them or as an
                earlier apply recorded them.

        Raises:
            click.ClickException: If the root is missing.
        """
        self.root = _resolve_attach_root()
        self.target = target
        self.state_root = pathlib.Path.cwd() / STATE_DIR_NAME

    def run(
        self,
        agent_name: str,
        triggers: dict[str, Any],
        *,
        apply: bool,
        destroy: bool = False,
    ) -> None:
        """Plans, applies or destroys one agent's triggers with Terraform.

        Args:
            agent_name: Attached agent; names its state.
            triggers: Terraform-backed field values to apply.
            apply: If True, applies or destroys; if False, plans.
            destroy: If True, removes the triggers instead.

        Raises:
            SystemExit: If Terraform fails.
        """
        self.state_root.mkdir(parents=True, exist_ok=True)
        run_terraform(
            self.root,
            state_key=build_attach_key(agent_name),
            state_root=self.state_root,
            apply=apply,
            destroy=destroy,
            tf_vars={
                **self.target,
                **_build_trigger_vars(agent_name, triggers),
            },
        )


@cli.command("attach")
@click.argument("agent_name", default="")
@click.option(
    "--dry-run", is_flag=True, help="Show the plan, write and grant nothing."
)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help=(
        "Also run the grant commands the plan prints, with your credentials, "
        "and apply the schedule and the update trigger with Terraform. With "
        "--dry-run, plan the triggers and grant nothing."
    ),
)
@click.option(
    "--yes",
    is_flag=True,
    help="Do not ask before changing a setting or running a grant.",
)
@click.option(
    "--no-discover",
    is_flag=True,
    help="Store only the settings given, discovering none.",
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
    apply_,
    yes,
    no_discover,
    source_root,
    hints,
    *,
    url,
    resource,
    **flags,
) -> None:
    """Record how AQuA observes AGENT_NAME.

    The deployment validates the settings and stores them; investigations read
    them from the next run on. Only the settings given change, and everything
    else keeps its current value. Without AGENT_NAME, the agent the deployment
    already watches.

    With --observed-agent-resource, every setting no flag gives is discovered
    from the observed engine with your credentials: the agent's name from its
    ADK server, its deployment's name, and the telemetry table a logging sink
    exports its inference events to. The plan names each value's source.

    The observed agent may be in another project than AQuA. Its telemetry is
    read there, the grant commands name that project's dataset, and --apply
    creates the audit sink on the agent's updates there, so your credentials
    need to reach both projects.

    First, the deployment tries the reads an investigation makes, as its own
    service account, and the plan shows each result. Telemetry that does not
    exist refuses the attach. For each read it is denied, the plan prints the
    command that grants it; with --apply, those commands run with your
    credentials, and the reads are tried again. A read that was skipped
    behind a denied one and turns out denied gets its grant printed and run
    in the same --apply. --apply also applies the agent's schedule and update
    trigger with Terraform.

    With --source-root, the project's source is then published to AQuA for the
    engine's newest revision, unless it already is.
    """
    # 1. Fill in what no flag gave: discovered from the observed engine, then
    #    the wrapper's hints, unless --no-discover.
    # 2. Ask the deployment for the plan and its access check, and show both,
    #    with a grant command for each denied read.
    # 3. With --apply, refuse an agent the deployment does not watch, and
    #    resolve the AQuA instance, so one that cannot be applied to stores
    #    nothing.
    # 4. Refuse telemetry that does not exist.
    # 5. Stop for a dry run, after planning the triggers with --apply.
    # 6. Confirm any change and any grant, unless --yes.
    # 7. Store the change.
    # 8. With --apply, run the grant commands, record those that ran, and
    #    check again, with further confirmed rounds for the grants it reveals.
    # 9. With --source-root, publish the source of the engine's newest
    #    revision, unless AQuA already has it.
    # 10. With --apply, apply the triggers and record what was applied.
    target = {"url": url, "resource": resource}
    fields = _extract_requested_fields(flags)
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
        agent_name, fields, lines = _resolve_discovered_fields(
            agent_name,
            fields,
            observed_agent_resource=flags.get("observed_agent_resource"),
            hints=parsed_hints,
            window_days=window_days,
        )
        for line in lines:
            click.echo(line)
        if lines:
            click.echo()
    if observed := flags.get("observed_agent_resource"):
        # Discovery resolves a project number to the id; without it, the
        # resource name's own spelling is what there is.
        fields.setdefault(
            "observed_project_id",
            discovery_lib.parse_engine_resource(observed).project,
        )
    request = {"agent_name": agent_name, "fields": fields}

    plan = _fetch_agents_result(
        AGENTS_ATTACH_ROUTE,
        {**request, "dry_run": True, "check_access": True},
        **target,
    )
    # The plan names the agent the environment names; only the default
    # attached agent takes a listing to name.
    watched_agent = (
        ""
        if plan.get("watched")
        else str(plan.get("environment_agent") or "")
        or _fetch_watched_agent(target)
    )
    for line in _render_plan_lines(
        plan, apply=apply_, watched_agent=watched_agent
    ):
        click.echo(line)
    access = plan.get("access") or {}
    for line in _render_access_lines(access) if access else []:
        click.echo(line)
    grants, ungranted = grants_lib.build_grants(access)
    for line in grants_lib.render_grant_lines(
        grants, ungranted, account=str(access.get("service_account") or "")
    ):
        click.echo(line)
    name = str(plan.get("agent_name") or agent_name)
    triggers = plan.get("triggers") or {}
    if apply_ and not plan.get("watched"):
        # A trigger names no agent, and the deployment investigates the one it
        # watches, so this agent's triggers would investigate that one again.
        watched = watched_agent or "another agent"
        raise click.ClickException(
            f"Not attaching {name} with --apply: this deployment investigates "
            f"{watched}, whatever trigger fires, so {name}'s triggers would "
            f"investigate {watched} again. Nothing was granted, stored or "
            f"applied.\n{_render_watch_remedy(plan, watched_agent)}"
        )
    # Resolved first, so that an --apply that cannot run stores nothing.
    instance = (
        _TriggerInstance(_validate_trigger_target(plan.get("trigger_target")))
        if apply_
        else None
    )

    refusal = _validate_access(access)
    if grants and not refusal:
        if not apply_:
            click.echo("Or run attach with --apply to run them.")
        elif dry_run:
            click.echo("Without --dry-run, --apply runs them.")

    if refusal:
        raise click.ClickException(refusal)
    if dry_run:
        if instance:
            click.echo(f"\nPlanning the triggers of {name}:")
            instance.run(name, triggers, apply=False)
        click.echo("\nDry run: nothing was written or granted.")
        return

    changes = plan.get("changes") or []
    # A first attach is stored even when it changes nothing: storing it is what
    # moves the agent's settings from the engine's environment into the object.
    storing = not plan.get("attached") or bool(changes)
    granting = apply_ and bool(grants)
    if not yes:
        if storing and changes:
            click.confirm(f"\nChange {len(changes)} setting(s)?", abort=True)
        if granting:
            click.confirm(
                f"\nRun {len(grants)} grant command(s) with your credentials?",
                abort=True,
            )
    # Stored before any grant runs, so that every grant that runs, even in a
    # run that then fails, has an attachment to be recorded on.
    if storing:
        result = _fetch_agents_result(
            AGENTS_ATTACH_ROUTE, {**request, "dry_run": False}, **target
        )
        click.echo(f"\nAttached {result.get('agent_name', name)}.")
    if granting:
        _apply_grants(grants, request, yes=yes, target=target, agent_name=name)
    elif apply_ and not access:
        click.echo(
            "\nThis deployment checked no reads, so there is nothing to grant."
        )
    elif apply_ and not ungranted:
        click.echo("\nNothing to grant.")
    if source_root:
        _publish_first_snapshot(
            source_root.resolve(),
            name,
            flags.get("observed_agent_resource"),
            target,
        )
    if not instance:
        return

    click.echo(f"\nApplying the triggers of {name}:")
    instance.run(name, triggers, apply=True)
    recorded = _fetch_agents_result(
        AGENTS_APPLIED_ROUTE,
        {"agent_name": name, "applied": triggers},
        **target,
    )
    click.echo(f"\nApplied the triggers of {name}.")
    if unapplied := recorded.get("unapplied_fields"):
        # Another attach landed while Terraform ran.
        click.echo(
            f"Changed since this apply began: {', '.join(unapplied)}. "
            "Run attach --apply again."
        )


@cli.command("list-agents")
@_add_target_options
def list_agents_cmd(*, url, resource) -> None:
    """List the attached agents as JSON, with which one is the default."""
    _emit_result(
        _fetch_command_result(
            url=url, resource=resource, path=AGENTS_LIST_ROUTE
        )
    )


def _load_applied_target(
    state_root: pathlib.Path, agent_name: str, reported: Any
) -> dict[str, str]:
    """Loads the instance an agent's triggers were applied against, to destroy them.

    The triggers' own state holds it, and it has to be the engine this command
    reached: detaching from one AQuA must not destroy what calls another.

    Args:
        state_root: Directory holding the agent's attach state.
        agent_name: Attached agent.
        reported: The trigger target the engine reports now, if any.

    Returns:
        The instance variables the triggers were applied with.

    Raises:
        click.ClickException: If the state records none, the reached engine
            reports no trigger target, or the triggers call a different engine.
    """
    state = build_state_path(state_root, build_attach_key(agent_name))
    recorded = read_state_outputs(state_root, build_attach_key(agent_name)).get(
        ATTACH_INSTANCE_OUTPUT
    )
    if not isinstance(recorded, dict):
        raise click.ClickException(
            f"{state} does not say which AQuA its triggers call, so they cannot "
            "be destroyed from here."
        )
    reported_engine = (
        reported.get("aqua_engine") if isinstance(reported, dict) else None
    )
    if reported_engine != recorded.get("aqua_engine"):
        reached = reported_engine or "which reports no trigger target"
        raise click.ClickException(
            f"The triggers in {state} call {recorded.get('aqua_engine')}, not "
            f"the AQuA this command reached ({reached})."
        )
    return {key: str(value) for key, value in recorded.items()}


def _revoke_grants(
    agent_name: str,
    revocations: list[grants_lib.Revocation],
    target: dict[str, Any],
) -> None:
    """Revokes the grants `attach --apply` recorded, and records them revoked.

    Every revocation is tried, and each that succeeded is recorded before a
    failure is reported, so that running `detach --apply` again retries only
    the ones that failed.

    Args:
        agent_name: Attached agent.
        revocations: Commands for the recorded grants.
        target: Engine target options for the route call.

    Raises:
        click.ClickException: If a command cannot run or fails.
    """
    click.echo(f"\nRevoking the grants attach --apply made for {agent_name}:")
    failure = None
    try:
        grants_lib.run_revocations(revocations, echo=click.echo)
        done = list(revocations)
    except grants_lib.GrantError as exc:
        failure = exc
        done = [
            r for r in exc.completed if isinstance(r, grants_lib.Revocation)
        ]
    if done:
        _fetch_agents_result(
            AGENTS_APPLIED_ROUTE,
            {
                "agent_name": agent_name,
                "revoked": [r.to_record() for r in done],
            },
            **target,
        )
    if failure is not None:
        raise click.ClickException(
            f"{failure}\n{agent_name} stays attached, with the failed grants "
            "still recorded; detach --apply again retries them. If those "
            "bindings are already gone, detach without --apply forgets them, "
            "and detach --apply then destroys the triggers."
        ) from failure


@cli.command("detach")
@click.argument("agent_name")
@click.option("--yes", is_flag=True, help="Do not ask before detaching.")
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help=(
        "Also revoke the grants attach --apply made, with your credentials, "
        "and destroy the schedule and the update trigger with Terraform."
    ),
)
@_add_target_options
def detach_agent_cmd(agent_name, yes, apply_, *, url, resource) -> None:
    """Forget how AQuA observes AGENT_NAME.

    For the agent the deployment watches, its settings then come from the
    engine's environment. With --apply, the grants attach --apply recorded on
    the attachment are revoked, and no others.
    """
    # 1. Read the attachment and the grants recorded on it.
    # 2. Show the revoke commands: run with --apply, left in place without.
    # 3. Resolve the instance the triggers here call, then confirm.
    # 4. With --apply, revoke the grants, then destroy the triggers.
    # 5. Delete the attachment.
    target = {"url": url, "resource": resource}
    listing = _fetch_agents_result(AGENTS_LIST_ROUTE, None, **target)
    entries = {a.get("agent_name"): a for a in listing.get("agents") or []}
    attached = set(entries)
    revocations, unrevocable = grants_lib.build_revocations(
        entries.get(agent_name, {}).get("grants") or []
    )
    if revocations:
        if apply_:
            click.echo(
                "Grants attach --apply made, revoked with your credentials:"
            )
        else:
            click.echo(
                "Grants attach --apply made, left in place and forgotten once "
                "detached; detach --apply revokes them:"
            )
        for revocation in revocations:
            click.echo(f"  {revocation.render()}")
    if unrevocable:
        click.echo(
            f"No revoke command for the recorded grant on "
            f"{', '.join(unrevocable)}: revoke it by hand if it is no longer "
            "needed."
        )
    revoke = apply_ and bool(revocations)
    state = build_state_path(
        pathlib.Path.cwd() / STATE_DIR_NAME, build_attach_key(agent_name)
    )
    # Triggers applied from here outlive a detach that forgot to remove them,
    # so `--apply` removes them even when the object is already gone.
    destroy = apply_ and state.is_file()
    if apply_ and not destroy:
        click.echo(f"No triggers of {agent_name} were applied from here.")
    if agent_name not in attached and not destroy:
        click.echo(f"{agent_name} is not attached.")
        return
    instance = (
        _TriggerInstance(
            _load_applied_target(
                state.parent, agent_name, listing.get("trigger_target")
            )
        )
        if destroy
        else None
    )
    if not yes:
        if agent_name not in attached:
            question = f"Destroy the triggers of {agent_name}?"
        else:
            also = [
                *([f"revoke {len(revocations)} grant(s)"] if revoke else []),
                *(["destroy its triggers"] if destroy else []),
            ]
            question = (
                f"Detach {agent_name} from {listing.get('prefix', '')}"
                f"{''.join(f' and {a}' for a in also)}?"
            )
        click.confirm(question, abort=True)

    if revoke:
        _revoke_grants(agent_name, revocations, target)
    if instance:
        click.echo(f"\nDestroying the triggers of {agent_name}:")
        instance.run(agent_name, {}, apply=True, destroy=True)
        state.unlink(missing_ok=True)
    if agent_name not in attached:
        return

    result = _fetch_agents_result(
        AGENTS_DETACH_ROUTE, {"agent_name": agent_name}, **target
    )
    if result.get("watched"):
        click.echo(
            f"Detached {agent_name}. Its settings come from the engine's environment."
        )
    else:
        click.echo(f"Detached {agent_name}.")


# --------------------------------------------------------------------------- #
# Source snapshots                                                             #
# --------------------------------------------------------------------------- #
def _publish_source(
    *,
    root: pathlib.Path,
    agent_name: str,
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
    3. Find the agent the snapshot belongs to, unless given: the attachment
       that records the engine, else the agent the deployment watches.
    4. Ask the deployment whether that revision is already published, and stop
       if it is, unless forced.
    5. Pack the files and upload them, manifest last. A dry run stops before.

    Progress goes to stderr, so a command's stdout stays its JSON result.

    Args:
        root: The observed agent's project root.
        agent_name: Agent the snapshot belongs to; empty to find it.
        observed_agent_resource: The observed engine; None to read it from the
            project's deployment metadata.
        revision: Revision to publish as; empty for the newest one.
        force: Upload even if the revision is already published.
        dry_run: Pack and report, and upload nothing.
        target: AQuA's `url` and `resource`.

    Returns:
        `{"agent_name", "revision", "published", ...}`: the stored snapshot's
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
    if not agent_name:
        listing = _fetch_agents_result(AGENTS_LIST_ROUTE, None, **target)
        agent_name = source_snapshot_lib.resolve_agent_for_engine(
            listing, engine
        )
        if not agent_name:
            raise source_snapshot_lib.SnapshotError(
                f"no attached agent observes {engine}; attach it first, or "
                "name the agent"
            )

    if not force and not dry_run:
        status = _fetch_command_result(
            path=SOURCE_ROUTE,
            payload={"agent_name": agent_name, "revision": revision},
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
                f"AQuA: revision {revision} of {agent_name} is already "
                f"published at {status.get('uri', '')}; --force uploads it again.",
                err=True,
            )
            return {
                "agent_name": agent_name,
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
        f"AQuA: snapshotting {engine} revision {revision} as {agent_name}: "
        f"{len(snapshot.files)} files, {snapshot.total_bytes} bytes "
        f"({snapshot.truncated_files} truncated, "
        f"{snapshot.manifest['omitted_files']} omitted, "
        f"{snapshot.skipped_files} media skipped)",
        err=True,
    )
    if dry_run:
        return {
            "agent_name": agent_name,
            "revision": revision,
            "published": False,
            "file_count": len(snapshot.files),
            "total_bytes": snapshot.total_bytes,
            "truncated_files": snapshot.truncated_files,
            "omitted_files": snapshot.manifest["omitted_files"],
            "skipped_files": snapshot.skipped_files,
        }

    client = _build_client(target["url"], target["resource"])

    def post(route: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return asyncio.run(client.post_command(route, body))
        except Exception as exc:  # Surface auth, network, and agent errors as user-facing CLI exceptions.
            raise click.ClickException(f"{type(exc).__name__}: {exc}") from exc

    def report(done: int, total: int) -> None:
        click.echo(f"  uploaded batch {done}/{total}", err=True)

    result = source_snapshot_lib.upload(
        post,
        agent_name=agent_name,
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
    agent_name: str,
    observed_agent_resource: str | None,
    target: dict[str, Any],
) -> None:
    """Publishes an attached agent's source, reporting rather than raising a failure.

    The agent is already attached when this runs, so a snapshot that cannot be
    published does not undo or fail the attach; root-cause analysis only cites
    no code until one is.

    Args:
        root: The observed agent's project root.
        agent_name: The agent just attached.
        observed_agent_resource: The observed engine, if given.
        target: AQuA's `url` and `resource`.
    """
    click.echo(f"\nPublishing the source of {agent_name} from {root}:")
    try:
        _publish_source(
            root=root,
            agent_name=agent_name,
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
@click.argument("agent_name", default="")
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
    agent_name,
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
    AGENT_NAME and the engine's newest revision. Without AGENT_NAME, the
    attached agent that observes the engine, else the one AQuA watches.

    `agents-cli deploy` and `agents-cli aqua attach` run this in a project
    with AQuA added; run it by hand to publish what they did not.
    """
    try:
        result = _publish_source(
            root=source_root.resolve(),
            agent_name=agent_name,
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
