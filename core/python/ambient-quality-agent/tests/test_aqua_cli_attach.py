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

"""`aqua attach`, `list-agents` and `detach` at the command line.

The deployment is faked at `_fetch_command_result`, the one seam every command
route goes through, and the update trigger's APIs and gcloud commands by
`_FakeSinks`. What these check is what the CLI sends, what it shows the
operator before writing, and that it asks first -- the engine side, which
decides what is stored, is `tests/test_observed_agent_config_commands.py`.
"""

from __future__ import annotations

import json
import pathlib
import shlex
from typing import Any

import pytest
from ambient_quality_agent.tools.evaluation.models import (
    MULTI_TURN_METRICS,
    SINGLE_TURN_METRICS,
)
from ambient_quality_cli import aqua_cli
from ambient_quality_cli import grants as grants_lib
from ambient_quality_cli import rest_client as rest_client_lib
from ambient_quality_cli import update_trigger as update_trigger_lib
from ambient_quality_cli.rest_client import ApiError
from click.testing import CliRunner

_OBJECT = "gs://jobs-bucket/agents/watched_agent.json"

_TARGET = {
    "region": "us-central1",
    "aqua_engine": "projects/p2/locations/us-central1/reasoningEngines/7",
    "ambient_topic": "projects/p2/topics/aqua-solo-aqua-ambient",
    "ambient_caller_email": "aqua-solo-aqua-caller@p2.iam.gserviceaccount.com",
}
"""The trigger target the fake engine reports."""

_KEY = "agent-project__watched_agent"
"""The key the fake engine's plan names: the agent, in the project its
triggers name."""

_SINK = f"aqua-solo-aqua-ambient-{_KEY}-updates"
"""The update sink of the agent `_KEY`, in its project."""

_WRITER = "serviceAccount:service-9@gcp-sa-logging.iam.gserviceaccount.com"
"""The writer identity every sink the fake creates gets."""


class _FakeSinks:
    """The Logging and Pub/Sub APIs the update trigger reads, and its gcloud
    commands, which change what the APIs report.

    Starts with the agent's sink in place on engine 42, allowed to publish, so
    that an attach of the default plan has no trigger command to run.
    """

    def __init__(self):
        """Initializes the fake with the agent's sink in place."""
        self.sinks: dict[tuple[str, str], dict[str, str]] = {}
        self.publishers: set[str] = {_WRITER}
        self.ran: list[list[str]] = []
        self.status = 0
        self.add("agent-project", _SINK, "42")

    def add(
        self, project: str, name: str, engine_id: str, topic: str = ""
    ) -> None:
        """Adds a sink.

        Args:
            project: Its project.
            name: Its name.
            engine_id: The engine its filter matches.
            topic: Where it publishes; the fake engine's topic if empty.
        """
        self.sinks[(project, name)] = {
            "name": name,
            "destination": update_trigger_lib.build_destination(
                topic or _TARGET["ambient_topic"]
            ),
            "filter": update_trigger_lib.build_sink_filter(engine_id),
            "writerIdentity": _WRITER,
        }

    def drop(self) -> None:
        """Removes every sink, so that an attach has one to create."""
        self.sinks.clear()

    def get_json(self, url, params=None):
        """Answers a GET of a sink, a project's sinks or the topic's policy.

        Args:
            url: Request URL.
            params: Ignored query parameters.

        Returns:
            The reply.

        Raises:
            ApiError: 404 for a sink that does not exist.
        """
        if url.endswith(":getIamPolicy"):
            return {
                "bindings": [
                    {
                        "role": update_trigger_lib.PUBLISHER_ROLE,
                        "members": sorted(self.publishers),
                    }
                ]
            }
        parts = url.split("/")
        if parts[-1] == "sinks":
            project = parts[-2]
            return {
                "sinks": [s for (p, _), s in self.sinks.items() if p == project]
            }
        project, name = parts[-3], parts[-1]
        if (project, name) not in self.sinks:
            raise ApiError(404, "not found")
        return self.sinks[(project, name)]

    def __call__(self, argv: list[str]) -> int:
        """Records one gcloud command and applies it to the fake's state.

        Args:
            argv: Command-line arguments vector.

        Returns:
            The scripted exit status.
        """
        self.ran.append(argv)
        if self.status:
            return self.status
        flags = dict(
            a[2:].split("=", 1) for a in argv if a.startswith("--") and "=" in a
        )
        if argv[1:3] == ["logging", "sinks"]:
            key = (flags["project"], argv[4])
            if argv[3] == "delete":
                self.sinks.pop(key, None)
            else:
                engine = flags["log-filter"].split("/reasoningEngines/")[1]
                self.add(*key, engine.split("$")[0])
        elif argv[3] == "add-iam-policy-binding":
            self.publishers.add(flags["member"])
        elif argv[3] == "remove-iam-policy-binding":
            self.publishers.discard(flags["member"])
        return 0

    def list_verbs(self) -> list[str]:
        """Lists what each command that ran did.

        Returns:
            `sinks create`, `topics add-iam-policy-binding` and the like.
        """
        return [f"{argv[2]} {argv[3]}" for argv in self.ran]


@pytest.fixture(autouse=True)
def sinks(monkeypatch) -> _FakeSinks:
    """Replaces the update trigger's API reads and its gcloud commands.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The installed fake.
    """
    fake = _FakeSinks()
    monkeypatch.setattr(
        rest_client_lib, "RestClient", lambda *args, **kwargs: fake
    )
    monkeypatch.setattr(aqua_cli, "_run_command", fake)
    monkeypatch.setattr(
        update_trigger_lib.shutil, "which", lambda p: f"/bin/{p}"
    )

    def refuse_grant(argv: list[str]) -> int:
        raise AssertionError(f"a grant ran with no runner installed: {argv}")

    # Attach runs the grants it prints; one must never reach a real `bq`.
    monkeypatch.setattr(grants_lib, "_run_command", refuse_grant)
    return fake


class _FakeDeployment:
    """Answers `agents/*` routes from a script and records every call."""

    def __init__(self, plan: dict[str, Any] | None = None, listing=None):
        """Initializes the fake with a scripted plan and listing.

        Args:
            plan: Fields that override the default attach response.
            listing: Response for `agents/list`; a single watched agent if None.
        """
        self.plan = {
            "agent_key": _KEY,
            "agent_name": "watched_agent",
            "object": _OBJECT,
            "attached": True,
            "watched": True,
            "triggers_name_agent": True,
            "changes": [],
            "adjusted": [],
            "needs_apply": [],
            "triggers": {
                "observed_engine_id": "42",
                "observed_project_id": "agent-project",
            },
            "unapplied_fields": [],
            "trigger_target": dict(_TARGET),
            "written": False,
            **(plan or {}),
        }
        self.listing = listing or {
            "prefix": "gs://jobs-bucket/agents/",
            "agents": [
                {
                    "agent_key": _KEY,
                    "agent_name": "watched_agent",
                    "watched": True,
                }
            ],
            "default_agent": _KEY,
            "trigger_target": dict(_TARGET),
        }
        self.calls: list[tuple[str, Any]] = []
        self.after_checks: list[dict[str, Any]] = []
        """Plan fields applied after each access check, the plan's own first,
        one per check, as a binding propagating between checks would change
        them."""

    def __call__(self, *, url, resource, path, payload=None):
        """Answers one route call, standing in for `_fetch_command_result`.

        Args:
            url: Ignored engine URL.
            resource: Ignored engine resource name.
            path: Route path.
            payload: Request body.

        Returns:
            The scripted response for the route.

        Raises:
            AssertionError: For a route this fake does not serve.
        """
        self.calls.append((path, payload))
        if path == "agents/attach":
            response = {**self.plan, "written": not payload["dry_run"]}
            # As the engine does: only a dry run that asks fills anything in,
            # and never a field the request sets.
            requested = payload.get("fields") or {}
            response["filled"] = [
                f
                for f in self.plan.get("filled") or []
                if payload.get("fill_from_table")
                and f["field"] not in requested
            ]
            if payload.get("check_access") and self.after_checks:
                self.plan.update(self.after_checks.pop(0))
            return response
        if path == "agents/list":
            return self.listing
        if path == "agents/applied":
            return {
                "agent_key": payload["agent_key"],
                "applied_at": "2026-10-01T00:00:00+00:00",
                "unapplied_fields": [],
            }
        if path == "agents/detach":
            return {"agent_key": payload["agent_key"], "detached": True}
        raise AssertionError(f"unexpected route {path}")

    def list_writes(self) -> list[Any]:
        """Lists the attach calls that were not dry runs.

        Returns:
            Request bodies of the attach calls that stored something.
        """
        return [
            p
            for path, p in self.calls
            if path == "agents/attach" and not p["dry_run"]
        ]


@pytest.fixture
def runner():
    return CliRunner()


def _install_deployment(monkeypatch, **kwargs) -> _FakeDeployment:
    """Replaces the CLI's route call with a `_FakeDeployment`.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        **kwargs: Arguments for `_FakeDeployment`.

    Returns:
        The installed fake.
    """
    fake = _FakeDeployment(**kwargs)
    monkeypatch.setattr(aqua_cli, "_fetch_command_result", fake)
    return fake


def _invoke(runner, *args, stdin=None):
    """Runs one CLI command against a fixed engine resource.

    Args:
        runner: Click test runner.
        *args: Command and its arguments.
        stdin: Text fed to prompts.

    Returns:
        The Click invocation result.
    """
    return runner.invoke(aqua_cli.cli, [*args, "--resource", "r"], input=stdin)


_A_CHANGE = {
    "changes": [{"field": "data_lookback_window", "before": 14, "after": 30}]
}


# --- what attach sends ----------------------------------------------------------- #


def test_attach_sends_only_the_flags_given_under_their_field_names(
    runner, monkeypatch
) -> None:
    """Everything not mentioned keeps its stored value, which only works if the
    CLI does not fill in the rest."""
    fake = _install_deployment(monkeypatch)

    _invoke(
        runner,
        "attach",
        "watched_agent",
        "--lookback-days",
        "30",
        "--telemetry-source",
        "cloud_ops",
        "--multi-turn-metric",
        "task_success",
        "--no-verification",
        "--dry-run",
    )

    path, payload = fake.calls[0]
    assert path == "agents/attach"
    assert payload == {
        "agent_name": "watched_agent",
        "fields": {
            "data_lookback_window": 30,
            "telemetry_ingestion_source": "cloud_ops",
            "multi_turn_metrics": ["task_success"],
            "insights_verification_enabled": False,
        },
        "dry_run": True,
        "check_access": True,
        "fill_from_table": True,
    }


def test_without_a_name_the_cli_leaves_the_name_to_the_deployment(
    runner, monkeypatch
) -> None:
    """The deployment refuses an attach that names no agent and no key."""
    fake = _install_deployment(monkeypatch)

    _invoke(runner, "attach", "--dry-run")

    assert fake.calls[0][1]["agent_name"] == ""


def test_attach_plans_before_it_writes(runner, monkeypatch) -> None:
    fake = _install_deployment(monkeypatch, plan=_A_CHANGE)

    result = _invoke(runner, "attach", "--lookback-days", "30", "--yes")

    assert result.exit_code == 0, result.output
    assert [p["dry_run"] for _, p in fake.calls] == [True, False]
    assert f"Attached watched_agent as {_KEY}." in result.output


def test_attach_never_touches_storage_itself(runner, monkeypatch) -> None:
    """The CLI talks to AQuA only through its API. Minting a token is the first
    step of any direct bucket call, so an attach that needed one would fail
    here. The engine reports no trigger target, so the only Google APIs the
    attach could call are AQuA's storage."""
    _install_deployment(monkeypatch, plan={**_A_CHANGE, "trigger_target": {}})

    def refuse_token():
        raise AssertionError(
            "attach must not mint a token for direct storage access"
        )

    monkeypatch.setattr(aqua_cli, "get_adc_token", refuse_token)

    result = _invoke(runner, "attach", "--lookback-days", "30", "--yes")

    assert result.exit_code == 0, result.output


# --- what the operator sees first ---------------------------------------------------- #


def test_a_dry_run_shows_the_plan_and_writes_nothing(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch, plan=_A_CHANGE)

    result = _invoke(runner, "attach", "--lookback-days", "30", "--dry-run")

    assert result.exit_code == 0
    assert "data_lookback_window: 14 -> 30" in result.output
    assert "Dry run" in result.output
    assert fake.list_writes() == []


def test_changing_a_setting_asks_first(runner, monkeypatch) -> None:
    fake = _install_deployment(monkeypatch, plan=_A_CHANGE)

    result = _invoke(runner, "attach", "--lookback-days", "30", stdin="n\n")

    assert result.exit_code != 0
    assert fake.list_writes() == []


def test_re_attaching_with_no_change_writes_nothing(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "attach")

    assert result.exit_code == 0
    assert "(no change)" in result.output
    assert fake.list_writes() == []


def test_a_first_attach_with_no_change_is_stored_without_asking(
    runner, monkeypatch
) -> None:
    """Storing it attaches the agent with the defaults it is planned from, and
    changes no setting, so there is nothing to confirm."""
    fake = _install_deployment(monkeypatch, plan={"attached": False})

    result = _invoke(runner, "attach")

    assert result.exit_code == 0, result.output
    assert "(not attached yet -- starting from the defaults)" in result.output
    assert len(fake.list_writes()) == 1


def test_a_value_the_deployment_adjusted_is_pointed_out(
    runner, monkeypatch
) -> None:
    _install_deployment(
        monkeypatch,
        plan={
            "changes": [
                {"field": "data_evaluation_cap", "before": 500, "after": 1000}
            ],
            "adjusted": [
                {
                    "field": "data_evaluation_cap",
                    "requested": 9999,
                    "stored": 1000,
                }
            ],
        },
    )

    result = _invoke(runner, "attach", "--evaluation-cap", "9999", "--dry-run")

    assert "asked for 9999, the deployment stores 1000" in result.output


_FILLED_LOCATION = {
    "changes": [
        {"field": "telemetry_location", "before": "us-central1", "after": "EU"}
    ],
    "filled": [
        {
            "field": "telemetry_location",
            "value": "EU",
            "reason": "the location of proj.agent_telemetry.t",
        }
    ],
}
"""A plan that filled in the location of the table the request names."""


def test_what_the_table_decides_is_shown_with_its_reason_and_stored(
    runner, monkeypatch
) -> None:
    """The deployment reads nothing from BigQuery when storing, so the write
    sends what the dry run filled in."""
    fake = _install_deployment(monkeypatch, plan=_FILLED_LOCATION)

    result = _invoke(runner, "attach", "--telemetry-table", "t", "--yes")

    assert result.exit_code == 0, result.output
    assert (
        "  telemetry_location: us-central1 -> EU "
        "(the location of proj.agent_telemetry.t)" in result.output
    )
    [write] = fake.list_writes()
    assert write["fields"] == {
        "telemetry_table": "t",
        "telemetry_location": "EU",
    }


def test_an_engine_change_shows_the_trigger_commands(
    runner, monkeypatch, sinks
) -> None:
    sinks.drop()
    _install_deployment(
        monkeypatch,
        plan={
            "changes": [
                {"field": "observed_engine_id", "before": "", "after": "42"}
            ],
            "needs_apply": ["observed_engine_id"],
        },
    )

    result = _invoke(runner, "attach", "--dry-run")

    assert result.exit_code == 0, result.output
    assert f"gcloud logging sinks create {_SINK}" in result.output
    assert "create it for engine 42" in result.output
    assert sinks.ran == []


def test_a_schedule_change_takes_effect_without_a_trigger_command(
    runner, monkeypatch, sinks
) -> None:
    fake = _install_deployment(
        monkeypatch,
        plan={
            "changes": [
                {
                    "field": "investigation_schedule",
                    "before": "0 12 * * *",
                    "after": "0 6 * * *",
                }
            ],
        },
    )

    result = _invoke(runner, "attach", "--schedule", "0 6 * * *", "--yes")

    assert result.exit_code == 0, result.output
    assert sinks.ran == []
    assert fake.calls[-1][1]["fields"] == {
        "investigation_schedule": "0 6 * * *"
    }


def test_attaching_an_agent_besides_the_default_adds_no_note(
    runner, monkeypatch
) -> None:
    """Each agent is investigated by its own triggers, so there is nothing to
    warn about, and no listing to read."""
    fake = _install_deployment(
        monkeypatch,
        plan={"agent_name": "other", "watched": False, "attached": False},
    )

    result = _invoke(runner, "attach", "other", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "investigates" not in result.output
    assert "--default" not in result.output
    assert [path for path, _ in fake.calls] == ["agents/attach"]


_ENGINE_IGNORING_OTHERS = {"triggers_name_agent": None}
"""An engine that does not report `triggers_name_agent`: it investigates its
default agent whatever trigger fires."""


_LISTING_WITH_DEFAULT_FIRST_AGENT = {
    "prefix": "gs://jobs-bucket/agents/",
    "agents": [{"agent_name": "first_agent", "watched": True}],
    "default_agent": "first_agent",
    "trigger_target": dict(_TARGET),
}
"""A deployment watching its one attachment."""


def test_an_agent_behind_the_default_attachment_is_named_with_the_remedy(
    runner, monkeypatch
) -> None:
    """An engine that does not report `triggers_name_agent` never reads that
    object: it investigates its default attached agent, which only the listing
    names, and `--default` moves that to this one."""
    fake = _install_deployment(
        monkeypatch,
        plan={
            **_ENGINE_IGNORING_OTHERS,
            "agent_name": "other",
            "watched": False,
            "attached": False,
        },
        listing=_LISTING_WITH_DEFAULT_FIRST_AGENT,
    )

    result = _invoke(runner, "attach", "other", "--dry-run")

    assert (
        "This deployment investigates first_agent, not other" in result.output
    )
    assert "Upgrading AQuA" in result.output
    assert "attach it with --default" in result.output
    assert "another agent" not in result.output
    assert [path for path, _ in fake.calls] == ["agents/attach", "agents/list"]


def test_an_unreadable_listing_still_shows_the_note(
    runner, monkeypatch
) -> None:
    """The name only makes the note clearer; failing to read it fails nothing."""
    _install_deployment(
        monkeypatch,
        plan={
            **_ENGINE_IGNORING_OTHERS,
            "agent_name": "other",
            "watched": False,
            "attached": False,
        },
        listing={"error": "listing failed"},
    )

    result = _invoke(runner, "attach", "other", "--dry-run")

    assert result.exit_code == 0
    assert (
        "This deployment investigates another agent, not other" in result.output
    )
    assert "attach it with --default" in result.output


def test_a_refusal_from_the_deployment_fails_the_command(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch)
    fake.plan = {
        "error": "Refusing to attach watched_agent: quality_analysis_mode ..."
    }

    result = _invoke(
        runner, "attach", "--quality-analysis-mode", "vibes", "--yes"
    )

    assert result.exit_code == 1
    assert "Refusing to attach" in result.output
    assert len(fake.calls) == 1


# --- the access check ------------------------------------------------------------------ #

_SA = "aqua@proj.iam.gserviceaccount.com"
_TABLE = "proj.agent_telemetry.gen_ai_client_inference_operation_details"


def _build_access(*checks: tuple[str, str, str]) -> dict[str, Any]:
    """Builds the plan's `access` section.

    Args:
        *checks: `(name, status, target)` for each check, in order.

    Returns:
        The section as the deployment returns it.
    """
    return {
        "access": {
            "service_account": _SA,
            "checks": [
                {"name": n, "status": s, "target": t, "detail": f"{n} detail"}
                for n, s, t in checks
            ],
        }
    }


def test_the_plan_shows_each_read_and_whose_it_was(runner, monkeypatch) -> None:
    _install_deployment(
        monkeypatch,
        plan=_build_access(
            ("table", "passed", _TABLE),
            ("rows", "empty", _TABLE),
            ("payload", "skipped", ""),
        ),
    )

    result = _invoke(runner, "attach", "--dry-run")

    assert result.exit_code == 0, result.output
    assert f"Reads, as {_SA}:" in result.output
    assert f"table    ok {_TABLE}" in result.output
    assert f"rows     empty {_TABLE}" in result.output
    assert "rows detail" in result.output


def test_missing_telemetry_refuses_the_attach(runner, monkeypatch) -> None:
    """There is nothing to investigate, and a bare `agents-cli deploy` is the
    usual way to get here."""
    fake = _install_deployment(
        monkeypatch,
        plan={
            **_A_CHANGE,
            **_build_access(
                ("table", "missing", _TABLE), ("rows", "skipped", _TABLE)
            ),
        },
    )

    result = _invoke(runner, "attach", "--lookback-days", "30", "--yes")

    assert result.exit_code == 1
    assert f"Not attaching: {_TABLE} does not exist" in result.output
    assert "agents-cli infra single-project --apply" in result.output
    assert fake.list_writes() == []


def test_a_failed_telemetry_read_refuses_the_attach(
    runner, monkeypatch
) -> None:
    """A rows query the table rejects (the wrong telemetry source, say) means no
    investigation could read it either."""
    fake = _install_deployment(
        monkeypatch,
        plan={
            **_A_CHANGE,
            **_build_access(
                ("table", "passed", _TABLE),
                ("rows", "error", _TABLE),
                ("payload", "skipped", ""),
            ),
        },
    )

    result = _invoke(runner, "attach", "--lookback-days", "30", "--yes")

    assert result.exit_code == 1
    assert f"Not attaching: reading {_TABLE} failed" in result.output
    assert "--telemetry-source" in result.output
    assert fake.list_writes() == []


_REFUSED_FORMAT = (
    f"Not attaching: {_TABLE} is not in the telemetry source's format"
)


def _build_mismatch(*matching_sources: str) -> dict[str, Any]:
    """Builds an attach plan whose schema check failed.

    Args:
        *matching_sources: The other sources whose columns the table has.

    Returns:
        The plan as the deployment returns it.
    """
    plan = {
        **_A_CHANGE,
        **_build_access(
            ("table", "passed", _TABLE),
            ("schema", "mismatch", _TABLE),
            ("rows", "skipped", _TABLE),
            ("payload", "skipped", ""),
        ),
    }
    plan["access"]["checks"][1]["matching_sources"] = list(matching_sources)
    return plan


@pytest.mark.parametrize(
    ("matching_sources", "hint"),
    [
        (
            ["cloud_ops"],
            "Pass --telemetry-source cloud_ops to read it as cloud_ops.",
        ),
        (
            ["big_query", "cloud_logging"],
            "Pass --telemetry-source with one of big_query, cloud_logging.",
        ),
        (
            [],
            "Name a table in that format with --telemetry-dataset and "
            "--telemetry-table, or fix what writes this one.",
        ),
    ],
)
def test_a_schema_mismatch_refuses_the_attach(
    runner, monkeypatch, matching_sources: list[str], hint: str
) -> None:
    """Every hint names something that can work: with no source that fits,
    another --telemetry-source would only mismatch again."""
    fake = _install_deployment(
        monkeypatch, plan=_build_mismatch(*matching_sources)
    )

    result = _invoke(runner, "attach", "--lookback-days", "30", "--yes")

    assert result.exit_code == 1
    assert f"schema   MISMATCH {_TABLE}" in result.output
    assert (
        f"{_REFUSED_FORMAT}, as the schema check above shows." in result.output
    )
    assert hint in result.output
    assert fake.list_writes() == []


def test_a_dry_run_with_a_schema_mismatch_is_refused(
    runner, monkeypatch
) -> None:
    _install_deployment(monkeypatch, plan=_build_mismatch("cloud_ops"))

    result = _invoke(runner, "attach", "--dry-run")

    assert result.exit_code == 1
    assert _REFUSED_FORMAT in result.output
    assert "Dry run" not in result.output


def test_a_schema_mismatch_refuses_before_any_grant(
    runner, monkeypatch, sinks
) -> None:
    """Granting reads on a table in the wrong format would not make it
    readable."""
    plan = _build_mismatch()
    plan["access"]["checks"].append(
        {
            "name": "log_view",
            "status": "denied",
            "target": "projects/proj",
            "detail": "log_view detail",
        }
    )
    fake = _install_deployment(monkeypatch, plan=plan)
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 1
    assert _REFUSED_FORMAT in result.output
    assert grant_runner.ran == []
    assert fake.list_writes() == []
    assert sinks.ran == []


def test_a_missing_table_outranks_a_schema_mismatch(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(
        monkeypatch,
        plan={
            **_A_CHANGE,
            **_build_access(
                ("table", "missing", _TABLE), ("schema", "mismatch", _TABLE)
            ),
        },
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 1
    assert f"Not attaching: {_TABLE} does not exist" in result.output
    assert _REFUSED_FORMAT not in result.output
    assert fake.list_writes() == []


def test_a_schema_mismatch_outranks_a_failed_read(runner, monkeypatch) -> None:
    fake = _install_deployment(
        monkeypatch,
        plan={
            **_A_CHANGE,
            **_build_access(
                ("table", "passed", _TABLE),
                ("schema", "mismatch", _TABLE),
                ("rows", "error", _TABLE),
            ),
        },
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 1
    assert _REFUSED_FORMAT in result.output
    assert "failed, and investigations" not in result.output
    assert fake.list_writes() == []


def test_a_passing_schema_check_does_not_refuse(runner, monkeypatch) -> None:
    fake = _install_deployment(
        monkeypatch,
        plan={
            **_A_CHANGE,
            **_build_access(
                ("table", "passed", _TABLE),
                ("schema", "passed", _TABLE),
                ("rows", "passed", _TABLE),
            ),
        },
    )

    result = _invoke(runner, "attach", "--lookback-days", "30", "--yes")

    assert result.exit_code == 0, result.output
    assert f"schema  ok {_TABLE}" in result.output
    assert len(fake.list_writes()) == 1


def test_a_failed_payload_read_does_not_refuse_the_attach(
    runner, monkeypatch
) -> None:
    """The telemetry itself is readable; only one sampled object is not."""
    fake = _install_deployment(
        monkeypatch,
        plan={
            **_A_CHANGE,
            **_build_access(
                ("table", "passed", _TABLE),
                ("rows", "passed", _TABLE),
                ("payload", "error", "gs://b/o"),
            ),
        },
    )

    result = _invoke(runner, "attach", "--lookback-days", "30", "--yes")

    assert result.exit_code == 0, result.output
    assert len(fake.list_writes()) == 1


def test_a_denied_read_is_named_and_the_attach_goes_ahead(
    runner, monkeypatch
) -> None:
    """The attachment is right; the grant can follow it."""
    fake = _install_deployment(
        monkeypatch,
        plan={**_A_CHANGE, **_build_access(("table", "denied", _TABLE))},
    )
    _install_grant_runner(monkeypatch, fake, access_after=_PASSED_TABLE)

    result = _invoke(runner, "attach", "--lookback-days", "30", "--yes")

    assert result.exit_code == 0, result.output
    assert f"table  DENIED {_TABLE}" in result.output
    assert f"cannot read {_TABLE} until {_SA} is granted" in result.output
    assert len(fake.list_writes()) == 1


def test_the_write_does_not_ask_for_the_check_again(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch, plan=_A_CHANGE)

    _invoke(runner, "attach", "--lookback-days", "30", "--yes")

    assert [p.get("check_access") for _, p in fake.calls] == [True, None]
    assert [p.get("fill_from_table") for _, p in fake.calls] == [True, None]


# --- grants ---------------------------------------------------------------------------- #

_DENIED_TABLE = _build_access(
    ("table", "denied", _TABLE), ("rows", "skipped", _TABLE)
)
_PASSED_TABLE = _build_access(
    ("table", "passed", _TABLE), ("rows", "passed", _TABLE)
)
_GRANT_LINE = (
    "bq query --nouse_legacy_sql --project_id=proj "
    "'GRANT `roles/bigquery.dataViewer` ON SCHEMA `proj.agent_telemetry` "
    f'TO "serviceAccount:{_SA}"\''
)


class _GrantRunner:
    """Stands in for running a grant command, and records each one.

    Once a command succeeds, the fake deployment's access check reports
    `access_after`, as a real one would once the binding propagates. A list
    gives what it reports after each successful command in turn, the last
    entry for every command after that.
    """

    def __init__(
        self,
        fake: _FakeDeployment,
        access_after: dict[str, Any] | list[dict[str, Any]],
        status: int | list[int] = 0,
    ):
        """Initializes the runner.

        Args:
            fake: The deployment whose access check the grants change.
            access_after: Plan fields the deployment returns after the grants,
                or after each successful command in turn.
            status: Exit status every command returns, or each command's in
                turn, the last for every command after that.
        """
        self.fake = fake
        self.access_after = (
            access_after if isinstance(access_after, list) else [access_after]
        )
        self.statuses = status if isinstance(status, list) else [status]
        self.successes = 0
        self.ran: list[list[str]] = []
        self.sleeps: list[float] = []

    def __call__(self, argv: list[str]) -> int:
        """Records one command and updates what the deployment reports.

        Args:
            argv: Command-line arguments vector.

        Returns:
            The scripted exit status.
        """
        self.ran.append(argv)
        status = self.statuses[min(len(self.ran), len(self.statuses)) - 1]
        if status == 0:
            self.successes += 1
            index = min(self.successes, len(self.access_after)) - 1
            self.fake.plan.update(self.access_after[index])
        return status


def _install_grant_runner(
    monkeypatch, fake: _FakeDeployment, **kwargs
) -> _GrantRunner:
    """Replaces running grant commands, finding programs and sleeping.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        fake: The installed deployment.
        **kwargs: Arguments for `_GrantRunner`.

    Returns:
        The installed runner.
    """
    runner = _GrantRunner(fake, **kwargs)
    monkeypatch.setattr(grants_lib, "_run_command", runner)
    monkeypatch.setattr(grants_lib.shutil, "which", lambda p: f"/bin/{p}")
    monkeypatch.setattr(aqua_cli.time, "sleep", runner.sleeps.append)
    return runner


def test_a_dry_run_prints_the_grant_and_runs_nothing(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch, plan=_DENIED_TABLE)
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", "--dry-run", "--yes")

    assert result.exit_code == 0, result.output
    assert f"To let {_SA} make those reads" in result.output
    assert f"  {_GRANT_LINE}" in result.output
    assert "nothing was written, granted or created" in result.output
    assert grant_runner.ran == []


def test_attach_runs_exactly_the_printed_commands_then_checks_again(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert [shlex.join(argv) for argv in grant_runner.ran] == [_GRANT_LINE]
    assert f"$ {_GRANT_LINE}" in result.output
    assert f"table  ok {_TABLE}" in result.output
    assert [
        (p["dry_run"], p.get("check_access"))
        for path, p in fake.calls
        if path == "agents/attach"
    ] == [(True, True), (False, None), (True, True)]
    assert grant_runner.sleeps == []


def test_attach_asks_before_granting(runner, monkeypatch) -> None:
    fake = _install_deployment(monkeypatch, plan=_DENIED_TABLE)
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", stdin="n\n")

    assert result.exit_code != 0
    assert "Run 1 grant command(s) with your credentials?" in result.output
    assert grant_runner.ran == []
    assert fake.list_writes() == []


def test_attach_grants_even_when_no_setting_changes(
    runner, monkeypatch
) -> None:
    """Re-attaching an agent whose reads are denied is how the grants are
    made for an attachment already stored."""
    fake = _install_deployment(monkeypatch, plan=_DENIED_TABLE)
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert len(grant_runner.ran) == 1
    assert fake.list_writes() == []


def test_what_the_table_decides_once_granted_is_stored_after_the_grants(
    runner, monkeypatch, sinks
) -> None:
    """The attachment is stored before the grants, when AQuA cannot read the
    table yet, so the check after them is the first to fill anything in."""
    sinks.drop()
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    _install_grant_runner(
        monkeypatch, fake, access_after={**_PASSED_TABLE, **_FILLED_LOCATION}
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert (
        "  telemetry_location: EU (the location of proj.agent_telemetry.t)"
        in result.output
    )
    first, second = fake.list_writes()
    assert "telemetry_location" not in first["fields"]
    assert second["fields"]["telemetry_location"] == "EU"
    assert sinks.ran


def test_declining_what_the_table_decides_stops_before_storing_it(
    runner, monkeypatch, sinks
) -> None:
    sinks.drop()
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    _install_grant_runner(
        monkeypatch, fake, access_after={**_PASSED_TABLE, **_FILLED_LOCATION}
    )

    result = _invoke(runner, "attach", stdin="y\ny\ny\nn\n")

    assert result.exit_code != 0
    assert "Store 1 setting(s)?" in result.output
    assert len(fake.list_writes()) == 1
    assert sinks.ran == []


def test_a_grant_still_denied_is_checked_again_and_reported(
    runner, monkeypatch
) -> None:
    """Propagation is not instant; the attach still goes ahead, as it does
    for any denied read."""
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_DENIED_TABLE
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert grant_runner.sleeps == [
        d for d in aqua_cli._RECHECK_DELAYS_SECONDS if d
    ]
    assert (
        "Still denied after the grants: proj:agent_telemetry, so rows could "
        "not be checked yet" in result.output
    )
    assert "run attach again to grant what they need" in result.output
    assert len(fake.list_writes()) == 1


def test_a_read_denied_behind_the_granted_one_waits_for_propagation(
    runner, monkeypatch
) -> None:
    """The dataset grant reaches the metadata read before the query: `rows`
    is denied on the same dataset, so the grant that ran is still the fix."""
    fake = _install_deployment(monkeypatch, plan=_DENIED_TABLE)
    grant_runner = _install_grant_runner(
        monkeypatch,
        fake,
        access_after=_build_access(
            ("table", "passed", _TABLE), ("rows", "denied", _TABLE)
        ),
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert grant_runner.sleeps == [
        d for d in aqua_cli._RECHECK_DELAYS_SECONDS if d
    ]
    assert "Or run attach again" not in result.output


_PAYLOAD_DENIED = _build_access(
    ("table", "passed", _TABLE),
    ("rows", "passed", _TABLE),
    ("payload", "denied", "gs://proj-agent-logs/x.json"),
)
_PAYLOAD_PASSED = _build_access(
    ("table", "passed", _TABLE),
    ("rows", "passed", _TABLE),
    ("payload", "passed", "gs://proj-agent-logs/x.json"),
)


def test_a_read_denied_behind_the_granted_one_is_granted_in_the_same_attach(
    runner, monkeypatch
) -> None:
    """The payload check is skipped until the rows are readable, so its
    bucket's grant can only be known after the dataset's. One attach makes
    both, printing the bucket's before running it."""
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=[_PAYLOAD_DENIED, _PAYLOAD_PASSED]
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert grant_runner.sleeps == []
    assert [argv[0] for argv in grant_runner.ran] == ["bq", "gcloud"]
    # Printed before it runs (the `$ ` echo).
    shown = result.output.index(
        "made gs://proj-agent-logs checkable, and "
        f"{_SA} is denied there too. Running next"
    )
    assert shown < result.output.index("$ gcloud storage buckets")
    assert "payload  ok gs://proj-agent-logs/x.json" in result.output
    assert "Still denied after the grants" not in result.output
    assert "Or run attach again" not in result.output
    assert len(fake.list_writes()) == 1
    assert _list_applied_calls(fake)[:2] == [
        {"agent_key": _KEY, "agent_name": _KEY, "granted": [_DATASET_RECORD]},
        {"agent_key": _KEY, "agent_name": _KEY, "granted": [_BUCKET_RECORD]},
    ]


def test_what_the_table_decides_is_stored_before_granting_what_it_needs(
    runner, monkeypatch
) -> None:
    """What the table decides can call for reads of its own, which are granted
    only once it is stored."""
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after={**_PAYLOAD_DENIED, **_FILLED_LOCATION}
    )

    result = _invoke(runner, "attach", stdin="y\ny\nn\n")

    assert result.exit_code != 0
    assert "Store 1 setting(s)?" in result.output
    assert result.output.count("grant command(s) with your credentials?") == 1
    assert len(grant_runner.ran) == 1


def test_what_the_plan_filled_in_is_sent_with_every_later_request(
    runner, monkeypatch
) -> None:
    """Sent as given, it is neither lost by the store nor filled in again."""
    fake = _install_deployment(
        monkeypatch, plan={**_PAYLOAD_DENIED, **_FILLED_LOCATION}
    )
    _install_grant_runner(monkeypatch, fake, access_after=_PAYLOAD_PASSED)

    result = _invoke(runner, "attach", "--telemetry-table", "t", "--yes")

    assert result.exit_code == 0, result.output
    later = [p for path, p in fake.calls if path == "agents/attach"][1:]
    assert [p["dry_run"] for p in later] == [False, True]
    assert [p["fields"]["telemetry_location"] for p in later] == ["EU", "EU"]


def test_each_round_of_grants_is_confirmed(runner, monkeypatch, sinks) -> None:
    """Without --yes, the grant a re-check reveals is asked about on its own,
    and declining it stops the attach before the trigger is wired. The
    attachment is already stored, with the first round's grant recorded on it
    for detach to revoke."""
    sinks.drop()
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=[_PAYLOAD_DENIED, _PAYLOAD_PASSED]
    )

    result = _invoke(runner, "attach", stdin="y\ny\ny\nn\n")

    assert result.exit_code == 1
    assert result.output.count("grant command(s) with your credentials?") == 2
    assert [argv[0] for argv in grant_runner.ran] == ["bq"]
    assert result.output.index(
        "add-iam-policy-binding gs://proj-agent-logs"
    ) < result.output.rindex("grant command(s) with your credentials?")
    assert len(fake.list_writes()) == 1
    assert _list_applied_calls(fake) == [
        {"agent_key": _KEY, "agent_name": _KEY, "granted": [_DATASET_RECORD]}
    ]
    assert sinks.ran == []


def test_a_failing_grant_in_a_later_round_keeps_the_earlier_round_recorded(
    runner, monkeypatch, sinks
) -> None:
    """The earlier round's grant stays recorded on the stored attachment, so
    detach still revokes it, and the attach stops before wiring the
    trigger."""
    sinks.drop()
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    grant_runner = _install_grant_runner(
        monkeypatch,
        fake,
        access_after=[_PAYLOAD_DENIED, _PAYLOAD_PASSED],
        status=[0, 1],
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 1
    assert [argv[0] for argv in grant_runner.ran] == ["bq", "gcloud"]
    assert len(fake.list_writes()) == 1
    assert _list_applied_calls(fake) == [
        {"agent_key": _KEY, "agent_name": _KEY, "granted": [_DATASET_RECORD]}
    ]
    assert sinks.ran == []


def test_a_grant_revealed_while_another_propagates_runs_once_that_lands(
    runner, monkeypatch
) -> None:
    """The re-check waits while the dataset grant is still denied; once it
    lands, the newly denied read gets its grant in the next round."""
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    log_view_denied = _build_access(
        ("table", "passed", _TABLE),
        ("rows", "passed", _TABLE),
        ("log_view", "denied", "projects/proj"),
    )
    grant_runner = _install_grant_runner(
        monkeypatch,
        fake,
        access_after=[
            _build_access(
                ("table", "denied", _TABLE),
                ("rows", "skipped", _TABLE),
                ("log_view", "denied", "projects/proj"),
            ),
            _PASSED_TABLE,
        ],
    )
    # The plan's own check comes first and changes nothing.
    fake.after_checks = [{}, log_view_denied]

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert grant_runner.sleeps == [aqua_cli._RECHECK_DELAYS_SECONDS[1]]
    assert [argv[:3] for argv in grant_runner.ran] == [
        ["bq", "query", "--nouse_legacy_sql"],
        ["gcloud", "projects", "add-iam-policy-binding"],
    ]
    assert "Still denied after the grants" not in result.output
    assert len(fake.list_writes()) == 1


def test_grants_stop_after_the_last_round(runner, monkeypatch) -> None:
    """A check that keeps revealing new grants cannot loop: after the last
    round, what is left is printed for the next attach."""
    fake = _install_deployment(monkeypatch, plan=_DENIED_TABLE)
    rounds = [
        _build_access(("payload", "denied", f"gs://bucket-{n}/x.json"))
        for n in range(aqua_cli._MAX_GRANT_ROUNDS)
    ]
    grant_runner = _install_grant_runner(monkeypatch, fake, access_after=rounds)

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert len(grant_runner.ran) == aqua_cli._MAX_GRANT_ROUNDS
    last = aqua_cli._MAX_GRANT_ROUNDS - 1
    assert (
        f"  gcloud storage buckets add-iam-policy-binding gs://bucket-{last} "
        in result.output
    )
    assert (
        f"Stopped after {aqua_cli._MAX_GRANT_ROUNDS} rounds of grants"
        in result.output
    )
    assert "Or run attach again to run them." in result.output


def test_missing_telemetry_refuses_the_attach_before_any_grant(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(
        monkeypatch,
        plan=_build_access(
            ("table", "missing", _TABLE),
            ("log_view", "denied", "projects/proj"),
        ),
    )
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 1
    assert "Not attaching" in result.output
    assert "$ " not in result.output
    assert grant_runner.ran == []
    assert fake.list_writes() == []


def test_attach_with_every_read_allowed_has_nothing_to_grant(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch, plan=_PASSED_TABLE)
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert "grant command" not in result.output
    assert grant_runner.ran == []


def test_a_failed_grant_fails_the_attach_after_storing_it(
    runner, monkeypatch, sinks
) -> None:
    """Stored first, so that a grant made before the failure has an
    attachment to be recorded on."""
    sinks.drop()
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE, status=1
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 1
    assert "Exit status 1 from: bq query" in result.output
    assert len(fake.list_writes()) == 1
    assert sinks.ran == []


def test_attach_against_a_deployment_that_checks_nothing_says_so(
    runner, monkeypatch
) -> None:
    _install_deployment(monkeypatch)

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert "checked no reads, so there is nothing to grant" in result.output


# --- list-agents and detach ------------------------------------------------------------ #


def test_list_agents_prints_the_deployments_answer_as_json(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "list-agents")

    assert result.exit_code == 0
    assert json.loads(result.output)["default_agent"] == _KEY
    assert fake.calls == [("agents/list", None)]


def test_detaching_asks_first(runner, monkeypatch) -> None:
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "detach", _KEY, stdin="n\n")

    assert result.exit_code != 0
    assert [path for path, _ in fake.calls] == ["agents/list"]


def test_detaching_names_the_detached_agent(runner, monkeypatch) -> None:
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "detach", _KEY, "--yes")

    assert result.exit_code == 0, result.output
    assert (
        "agents/detach",
        {"agent_key": _KEY, "agent_name": _KEY},
    ) in fake.calls
    assert f"Detached {_KEY}." in result.output


def test_detaching_what_is_not_attached_deletes_nothing(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "detach", "nobody", "--yes")

    assert result.exit_code == 0
    assert "not attached" in result.output
    assert [path for path, _ in fake.calls] == ["agents/list"]


# --- the agent key recorded for later commands here ------------------------- #


def _load_agent_record(directory) -> dict[str, Any] | None:
    """Reads `.aqua/attached_aqua.json`.

    Args:
        directory: The project directory.

    Returns:
        The record, or None if there is none.
    """
    record = directory / ".aqua" / "attached_aqua.json"
    if not record.is_file():
        return None
    return json.loads(record.read_text(encoding="utf-8"))


def test_attach_records_the_key_for_the_aqua_it_reached(
    runner, monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    _install_deployment(monkeypatch)

    result = _invoke(runner, "attach")

    assert result.exit_code == 0, result.output
    assert _load_agent_record(tmp_path) == {
        "agent_key": _KEY,
        "agent_key_target": "r",
    }
    assert f"Recorded {_KEY} in .aqua/attached_aqua.json" in result.output


def test_re_attaching_the_recorded_agent_says_nothing_of_it(
    runner, monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    _install_deployment(monkeypatch)
    _invoke(runner, "attach")

    result = _invoke(runner, "attach")

    assert result.exit_code == 0, result.output
    assert "Recorded" not in result.output


def test_attach_keeps_what_agents_cli_recorded(
    runner, monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".aqua").mkdir()
    (tmp_path / ".aqua" / "attached_aqua.json").write_text(
        json.dumps({"aqua_resource": "r"}), encoding="utf-8"
    )
    _install_deployment(monkeypatch)

    _invoke(runner, "attach")

    assert _load_agent_record(tmp_path) == {
        "aqua_resource": "r",
        "agent_key": _KEY,
        "agent_key_target": "r",
    }


def test_a_dry_run_records_no_key(runner, monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    _install_deployment(monkeypatch)

    result = _invoke(runner, "attach", "--dry-run")

    assert result.exit_code == 0, result.output
    assert _load_agent_record(tmp_path) is None


def test_attach_records_no_key_in_aquas_own_checkout(
    runner, monkeypatch, tmp_path
) -> None:
    """There, an operator attaches one agent after another."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aqua_cli, "_is_aqua_own_checkout", lambda: True)
    _install_deployment(monkeypatch)

    result = _invoke(runner, "attach")

    assert result.exit_code == 0, result.output
    assert _load_agent_record(tmp_path) is None


def test_only_the_cli_source_tree_is_aquas_own_checkout(
    monkeypatch, tmp_path
) -> None:
    checkout = pathlib.Path(aqua_cli.__file__).resolve().parents[2]
    monkeypatch.chdir(checkout / "tests")
    assert aqua_cli._is_aqua_own_checkout()

    monkeypatch.chdir(tmp_path)
    assert not aqua_cli._is_aqua_own_checkout()


@pytest.mark.parametrize(
    ("detached", "kept"), [(_KEY, False), ("other_agent", True)]
)
def test_detach_removes_the_recorded_key_only_for_that_agent(
    runner, monkeypatch, tmp_path, detached, kept
) -> None:
    monkeypatch.chdir(tmp_path)
    fake = _install_deployment(monkeypatch)
    _invoke(runner, "attach")
    fake.listing["agents"].append({"agent_key": "other_agent"})

    result = _invoke(runner, "detach", detached, "--yes")

    assert result.exit_code == 0, result.output
    assert bool(_load_agent_record(tmp_path)) == kept


def test_detaching_a_recorded_agent_that_is_gone_removes_its_key(
    runner, monkeypatch, sinks, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    fake = _install_deployment(monkeypatch)
    _invoke(runner, "attach")
    fake.listing["agents"] = []
    sinks.drop()

    result = _invoke(runner, "detach", _KEY, "--yes")

    assert result.exit_code == 0, result.output
    assert "not attached" in result.output
    assert _load_agent_record(tmp_path) == {}


# --- the update trigger ---------------------------------------------------------------- #

_ENGINE_CHANGE = {
    "changes": [{"field": "observed_engine_id", "before": "", "after": "42"}],
    "needs_apply": ["observed_engine_id"],
    "unapplied_fields": ["observed_engine_id"],
}


def test_attach_creates_a_missing_trigger_then_records_what_it_wired(
    runner, monkeypatch, sinks
) -> None:
    """The sink goes where the observed engine is, and its identity is bound
    on AQuA's topic once the sink reports it."""
    sinks.drop()
    fake = _install_deployment(monkeypatch, plan=_ENGINE_CHANGE)

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert sinks.list_verbs() == [
        "sinks create",
        "topics add-iam-policy-binding",
    ]
    create, grant = sinks.ran
    assert create[4] == _SINK
    assert "--project=agent-project" in create
    assert grant[4] == _TARGET["ambient_topic"]
    assert f"--member={_WRITER}" in grant
    assert [path for path, _ in fake.calls] == [
        "agents/attach",
        "agents/attach",
        "agents/applied",
    ]
    assert fake.calls[-1][1] == {
        "agent_key": _KEY,
        "agent_name": _KEY,
        "applied": fake.plan["triggers"],
    }
    assert f"Wired the update trigger of {_KEY}." in result.output


def test_a_trigger_in_place_runs_and_records_nothing(
    runner, monkeypatch, sinks
) -> None:
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert sinks.ran == []
    assert "Update trigger: agent-project/sinks/" in result.output
    assert "in place" in result.output
    assert "agents/applied" not in [path for path, _ in fake.calls]


def test_a_trigger_in_place_but_never_recorded_is_recorded(
    runner, monkeypatch, sinks
) -> None:
    """A sink the attachment does not record, such as one an attach left
    when it failed before recording it, is taken over as it is."""
    fake = _install_deployment(
        monkeypatch, plan={"unapplied_fields": ["observed_engine_id"]}
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert sinks.ran == []
    assert fake.calls[-1][0] == "agents/applied"


def test_an_engine_that_does_not_report_what_is_unapplied_is_recorded(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch)
    del fake.plan["unapplied_fields"]

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert fake.calls[-1][0] == "agents/applied"


def test_a_sink_on_another_engine_is_updated(
    runner, monkeypatch, sinks
) -> None:
    sinks.add("agent-project", _SINK, "7")
    _install_deployment(monkeypatch)

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert sinks.list_verbs() == ["sinks update"]


def test_a_dry_run_shows_the_trigger_commands_and_runs_none(
    runner, monkeypatch, sinks
) -> None:
    sinks.drop()
    fake = _install_deployment(monkeypatch, plan=_ENGINE_CHANGE)

    result = _invoke(runner, "attach", "--dry-run")

    assert result.exit_code == 0, result.output
    assert f"gcloud logging sinks create {_SINK}" in result.output
    assert update_trigger_lib.WRITER_PLACEHOLDER in result.output
    assert sinks.ran == []
    assert [path for path, _ in fake.calls] == ["agents/attach"]


def test_declining_the_trigger_commands_stores_nothing(
    runner, monkeypatch, sinks
) -> None:
    """Every question comes before anything is done."""
    sinks.drop()
    fake = _install_deployment(monkeypatch, plan=_ENGINE_CHANGE)

    result = _invoke(runner, "attach", stdin="y\nn\n")

    assert result.exit_code != 0
    assert "Run 2 update trigger command(s) with your credentials?" in (
        result.output
    )
    assert fake.list_writes() == []
    assert sinks.ran == []


def test_without_an_engine_the_trigger_is_removed(
    runner, monkeypatch, sinks
) -> None:
    """An agent re-pointed at no engine is no longer watched for updates."""
    _install_deployment(
        monkeypatch,
        plan={
            "triggers": {
                "observed_engine_id": "",
                "observed_project_id": "agent-project",
            },
            "unapplied_fields": ["observed_engine_id"],
        },
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert sinks.list_verbs() == [
        "topics remove-iam-policy-binding",
        "sinks delete",
    ]


def test_a_sink_of_the_agents_name_publishing_elsewhere_stops_before_storing(
    runner, monkeypatch, sinks
) -> None:
    """It belongs to another AQuA whose topic has the same name."""
    sinks.add("agent-project", _SINK, "42", topic="projects/x/topics/t")
    fake = _install_deployment(monkeypatch, plan=_A_CHANGE)

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 1
    assert "leaving it alone" in result.output
    assert "Nothing was changed." in result.output
    assert fake.list_writes() == []
    assert sinks.ran == []


def test_a_failed_trigger_command_fails_the_attach_after_storing_it(
    runner, monkeypatch, sinks
) -> None:
    sinks.drop()
    sinks.status = 1
    fake = _install_deployment(monkeypatch, plan=_ENGINE_CHANGE)

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 1
    assert "Exit status 1 from: gcloud logging sinks create" in result.output
    assert len(fake.list_writes()) == 1
    assert "agents/applied" not in [path for path, _ in fake.calls]


def test_attach_needs_nothing_of_aquas_own_terraform(
    runner, monkeypatch, sinks, tmp_path
) -> None:
    """The engine's resource name is all attach is given, and no state is kept
    for the trigger: its name finds it again from any directory."""
    monkeypatch.chdir(tmp_path)
    sinks.drop()
    _install_deployment(monkeypatch, plan=_ENGINE_CHANGE)

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert [p.name for p in (tmp_path / ".aqua").iterdir()] == [
        "attached_aqua.json"
    ]


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"region": "us-central1", "aqua_engine": "projects/p2/x"},
    ],
)
def test_an_engine_that_reports_no_topic_gets_no_trigger(
    runner, monkeypatch, sinks, target
) -> None:
    """A local run reports no topic, and there is nothing to wire a sink to."""
    sinks.drop()
    fake = _install_deployment(
        monkeypatch, plan={**_ENGINE_CHANGE, "trigger_target": target}
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert "reports no topic for update triggers" in result.output
    assert len(fake.list_writes()) == 1
    assert sinks.ran == []
    assert "agents/applied" not in [path for path, _ in fake.calls]


def test_attach_wires_the_trigger_of_an_agent_besides_the_default(
    runner, monkeypatch, sinks
) -> None:
    """Each trigger investigates its own agent, so any agent gets one."""
    sinks.drop()
    other_key = "agent-project__other"
    _install_deployment(
        monkeypatch,
        plan={
            **_ENGINE_CHANGE,
            "agent_key": other_key,
            "agent_name": "other",
            "watched": False,
            "attached": False,
        },
    )

    result = _invoke(runner, "attach", "other", "--yes")

    assert result.exit_code == 0, result.output
    assert sinks.ran[0][4] == f"aqua-solo-aqua-ambient-{other_key}-updates"


def test_an_agent_the_engine_ignores_gets_no_trigger(
    runner, monkeypatch, sinks
) -> None:
    """Its updates would investigate the default agent a second time; the
    attachment is still stored, as the plan's note says."""
    sinks.drop()
    fake = _install_deployment(
        monkeypatch,
        plan={
            **_ENGINE_CHANGE,
            **_ENGINE_IGNORING_OTHERS,
            "agent_name": "other",
            "watched": False,
            "attached": False,
        },
    )

    result = _invoke(runner, "attach", "other", "--yes")

    assert result.exit_code == 0, result.output
    assert "No update trigger for other" in result.output
    assert f"investigates {_KEY} whatever trigger fires" in result.output
    assert len(fake.list_writes()) == 1
    assert sinks.ran == []


def test_the_default_agent_of_an_engine_that_ignores_others_gets_a_trigger(
    runner, monkeypatch, sinks
) -> None:
    """Its default agent is the one every trigger investigates."""
    sinks.drop()
    _install_deployment(
        monkeypatch, plan={**_ENGINE_CHANGE, **_ENGINE_IGNORING_OTHERS}
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert sinks.list_verbs()[0] == "sinks create"


def test_detach_removes_the_trigger_then_detaches(
    runner, monkeypatch, sinks
) -> None:
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "detach", _KEY, "--yes")

    assert result.exit_code == 0, result.output
    assert sinks.list_verbs() == [
        "topics remove-iam-policy-binding",
        "sinks delete",
    ]
    assert [path for path, _ in fake.calls] == ["agents/list", "agents/detach"]


def test_detach_keeps_the_binding_another_sink_publishes_with(
    runner, monkeypatch, sinks
) -> None:
    """Cloud Logging can give every sink in a project one writer identity."""
    sinks.add("agent-project", "aqua-solo-aqua-ambient-other-updates", "7")
    _install_deployment(monkeypatch)

    result = _invoke(runner, "detach", _KEY, "--yes")

    assert result.exit_code == 0, result.output
    assert sinks.list_verbs() == ["sinks delete"]


def test_detach_removes_a_trigger_left_behind(
    runner, monkeypatch, sinks
) -> None:
    """A detach that could not remove the trigger leaves it publishing."""
    gone = "agent-project__gone"
    sinks.add("agent-project", f"aqua-solo-aqua-ambient-{gone}-updates", "5")
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "detach", gone, stdin="y\n")

    assert result.exit_code == 0, result.output
    assert f"Remove the update trigger of {gone}?" in result.output
    assert sinks.list_verbs()[-1] == "sinks delete"
    assert [path for path, _ in fake.calls] == ["agents/list"]


def test_detach_finds_the_sink_in_the_project_the_attachment_records(
    runner, monkeypatch, sinks
) -> None:
    """A bare name says nothing of the project; the listing does."""
    sinks.add(
        "agent-project", "aqua-solo-aqua-ambient-watched_agent-updates", "42"
    )
    _install_deployment(
        monkeypatch,
        listing={
            "prefix": "gs://jobs-bucket/agents/",
            "agents": [
                {
                    "agent_key": "watched_agent",
                    "agent_name": "watched_agent",
                    "observed_project_id": "agent-project",
                }
            ],
            "trigger_target": dict(_TARGET),
        },
    )

    result = _invoke(runner, "detach", "watched_agent", "--yes")

    assert result.exit_code == 0, result.output
    assert "--project=agent-project" in sinks.ran[-1]


def test_detach_from_an_aqua_that_reports_no_topic_removes_no_trigger(
    runner, monkeypatch, sinks
) -> None:
    fake = _install_deployment(monkeypatch)
    fake.listing["trigger_target"] = {}

    result = _invoke(runner, "detach", _KEY, "--yes")

    assert result.exit_code == 0, result.output
    assert sinks.ran == []
    assert fake.calls[-1][0] == "agents/detach"


def test_delete_triggers_plans_every_agents_removal(
    runner, monkeypatch, sinks
) -> None:
    """What `infra single-project --destroy` shows before AQuA goes."""
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "delete-triggers")

    assert result.exit_code == 0, result.output
    assert f"gcloud logging sinks delete {_SINK}" in result.output
    assert "Re-run with --apply" in result.output
    assert sinks.ran == []
    assert [path for path, _ in fake.calls] == ["agents/list"]


def test_delete_triggers_with_apply_removes_them_and_keeps_the_attachments(
    runner, monkeypatch, sinks
) -> None:
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "delete-triggers", "--apply")

    assert result.exit_code == 0, result.output
    assert sinks.list_verbs()[-1] == "sinks delete"
    assert sinks.sinks == {}
    assert [path for path, _ in fake.calls] == ["agents/list"]


def test_attach_has_no_apply_flag(runner) -> None:
    result = runner.invoke(aqua_cli.cli, ["attach", "--apply"])

    assert result.exit_code == 2
    assert "No such option '--apply'" in result.output


# --- grants recorded on the attachment, and revoked by detach ------------------------- #

_PAYLOAD = "gs://proj-agent-logs/x.json"
_DENIED_TABLE_AND_PAYLOAD = _build_access(
    ("table", "denied", _TABLE), ("payload", "denied", _PAYLOAD)
)
_DATASET_RECORD = {
    "resource": "proj:agent_telemetry",
    "role": "roles/bigquery.dataViewer",
    "member": f"serviceAccount:{_SA}",
}
_BUCKET_RECORD = {
    "resource": "gs://proj-agent-logs",
    "role": "roles/storage.objectViewer",
    "member": f"serviceAccount:{_SA}",
}
_REVOKE_LINE = (
    "bq query --nouse_legacy_sql --project_id=proj "
    "'REVOKE `roles/bigquery.dataViewer` ON SCHEMA `proj.agent_telemetry` "
    f'FROM "serviceAccount:{_SA}"\''
)


def _list_applied_calls(fake: _FakeDeployment) -> list[Any]:
    """Lists the bodies sent to `agents/applied`, in order.

    Args:
        fake: The installed deployment.

    Returns:
        Request bodies of the record calls.
    """
    return [p for path, p in fake.calls if path == "agents/applied"]


def test_attach_stores_then_grants_then_records_the_grants_it_ran(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(
        monkeypatch, plan={**_A_CHANGE, "attached": False, **_DENIED_TABLE}
    )
    _install_grant_runner(monkeypatch, fake, access_after=_PASSED_TABLE)

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert [
        "applied" if path == "agents/applied" else p["dry_run"]
        for path, p in fake.calls
    ] == [True, False, "applied", True]
    (granted,) = _list_applied_calls(fake)
    assert granted == {
        "agent_key": _KEY,
        "agent_name": _KEY,
        "granted": [_DATASET_RECORD],
    }
    assert "detach revokes them" in result.output


def test_attach_records_its_grants_on_an_attachment_it_leaves_unchanged(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch, plan=_DENIED_TABLE)
    _install_grant_runner(monkeypatch, fake, access_after=_PASSED_TABLE)

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert fake.list_writes() == []
    assert _list_applied_calls(fake)[0] == {
        "agent_key": _KEY,
        "agent_name": _KEY,
        "granted": [_DATASET_RECORD],
    }


def test_an_attach_with_nothing_to_grant_records_no_grant(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(
        monkeypatch,
        plan={**_PASSED_TABLE, "unapplied_fields": ["observed_engine_id"]},
    )

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert [list(p) for p in _list_applied_calls(fake)] == [
        ["agent_key", "agent_name", "applied"]
    ]


@pytest.mark.parametrize("attached", [True, False])
def test_a_grant_that_fails_records_those_made_before_it(
    runner, monkeypatch, sinks, attached
) -> None:
    """Otherwise the dataset grant would outlive every detach: a rerun finds
    that read allowed, and grants and records nothing for it."""
    sinks.drop()
    fake = _install_deployment(
        monkeypatch, plan={"attached": attached, **_DENIED_TABLE_AND_PAYLOAD}
    )
    _install_grant_runner(monkeypatch, fake, access_after={}, status=[0, 1])

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 1
    assert "Exit status 1 from: gcloud storage" in result.output
    assert _list_applied_calls(fake) == [
        {"agent_key": _KEY, "agent_name": _KEY, "granted": [_DATASET_RECORD]}
    ]
    assert len(fake.list_writes()) == (0 if attached else 1)
    assert sinks.ran == []


def test_a_grant_record_the_deployment_refuses_shows_how_to_revoke_it(
    runner, monkeypatch, sinks
) -> None:
    """An engine older than the CLI takes no grants; the trigger is still
    wired, and the user learns what detach cannot undo."""
    sinks.drop()
    fake = _install_deployment(monkeypatch, plan=_DENIED_TABLE)
    _install_grant_runner(monkeypatch, fake, access_after=_PASSED_TABLE)
    answer = fake.__call__

    def refuse_grants(**kwargs):
        if kwargs["path"] == "agents/applied" and "granted" in (
            kwargs["payload"] or {}
        ):
            fake.calls.append((kwargs["path"], kwargs["payload"]))
            return {"error": "applied is not a JSON object: None"}
        return answer(**kwargs)

    monkeypatch.setattr(aqua_cli, "_fetch_command_result", refuse_grants)

    result = _invoke(runner, "attach", "--yes")

    assert result.exit_code == 0, result.output
    assert "Could not record the grants" in result.output
    assert f"  {_REVOKE_LINE}" in result.output
    assert sinks.list_verbs()[0] == "sinks create"


def test_declining_the_grants_stores_nothing(runner, monkeypatch) -> None:
    """Both questions come before anything is done."""
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", stdin="y\nn\n")

    assert result.exit_code != 0
    assert "Change 1 setting(s)?" in result.output
    assert "Run 1 grant command(s) with your credentials?" in result.output
    assert grant_runner.ran == []
    assert fake.list_writes() == []


def _install_recorded_grants(
    monkeypatch, *records: dict[str, str]
) -> _FakeDeployment:
    """Installs a deployment whose watched agent has grants recorded.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        *records: Grants as the attachment recorded them.

    Returns:
        The installed fake.
    """
    agent: dict[str, Any] = {
        "agent_key": _KEY,
        "agent_name": "watched_agent",
        "watched": True,
        "grants": list(records),
    }
    return _install_deployment(
        monkeypatch,
        listing={
            "prefix": "gs://jobs-bucket/agents/",
            "agents": [agent],
            "default_agent": "watched_agent",
            "trigger_target": dict(_TARGET),
        },
    )


def test_detach_revokes_the_recorded_grants_and_records_that(
    runner, monkeypatch, sinks
) -> None:
    fake = _install_recorded_grants(monkeypatch, _DATASET_RECORD)
    grant_runner = _install_grant_runner(monkeypatch, fake, access_after={})

    result = _invoke(runner, "detach", _KEY, "--yes")

    assert result.exit_code == 0, result.output
    assert [shlex.join(argv) for argv in grant_runner.ran] == [_REVOKE_LINE]
    assert [path for path, _ in fake.calls] == [
        "agents/list",
        "agents/applied",
        "agents/detach",
    ]
    assert _list_applied_calls(fake) == [
        {"agent_key": _KEY, "agent_name": _KEY, "revoked": [_DATASET_RECORD]}
    ]
    assert sinks.list_verbs()[-1] == "sinks delete"


def test_detach_reads_a_listing_that_names_agents_only_by_name(
    runner, monkeypatch
) -> None:
    """An engine that lists no `agent_key` stores each attachment under its
    bare name, so that name finds the attachment and its recorded grants."""
    fake = _install_deployment(
        monkeypatch,
        listing={
            "prefix": "gs://jobs-bucket/agents/",
            "agents": [
                {
                    "agent_name": "watched_agent",
                    "watched": True,
                    "grants": [_DATASET_RECORD],
                }
            ],
            "default_agent": "watched_agent",
            "trigger_target": dict(_TARGET),
        },
    )
    grant_runner = _install_grant_runner(monkeypatch, fake, access_after={})

    result = _invoke(runner, "detach", "watched_agent", "--yes")

    assert result.exit_code == 0, result.output
    assert [shlex.join(argv) for argv in grant_runner.ran] == [_REVOKE_LINE]
    assert (
        "agents/detach",
        {"agent_key": "watched_agent", "agent_name": "watched_agent"},
    ) in fake.calls


def test_detach_asks_before_revoking(runner, monkeypatch) -> None:
    fake = _install_recorded_grants(monkeypatch, _DATASET_RECORD)
    grant_runner = _install_grant_runner(monkeypatch, fake, access_after={})

    result = _invoke(runner, "detach", _KEY, stdin="n\n")

    assert result.exit_code != 0
    assert f"  {_REVOKE_LINE}" in result.output
    assert "and revoke 1 grant(s) and remove its update trigger?" in (
        result.output
    )
    assert grant_runner.ran == []
    assert [path for path, _ in fake.calls] == ["agents/list"]


def test_detach_keeping_the_grants_shows_them_and_revokes_nothing(
    runner, monkeypatch
) -> None:
    fake = _install_recorded_grants(monkeypatch, _DATASET_RECORD)
    grant_runner = _install_grant_runner(monkeypatch, fake, access_after={})

    result = _invoke(runner, "detach", _KEY, "--keep-grants", "--yes")

    assert result.exit_code == 0, result.output
    assert "left in place and forgotten once detached" in result.output
    assert f"  {_REVOKE_LINE}" in result.output
    assert grant_runner.ran == []
    assert _list_applied_calls(fake) == []


def test_a_failed_revocation_records_those_made_and_keeps_the_attachment(
    runner, monkeypatch, sinks
) -> None:
    """Running detach again then retries only the rest, and the trigger is
    left for it."""
    fake = _install_recorded_grants(
        monkeypatch, _DATASET_RECORD, _BUCKET_RECORD
    )
    _install_grant_runner(monkeypatch, fake, access_after={}, status=[0, 1])

    result = _invoke(runner, "detach", _KEY, "--yes")

    assert result.exit_code == 1
    assert "remove-iam-policy-binding gs://proj-agent-logs" in result.output
    assert "watched_agent stays attached" in result.output
    assert "detach --keep-grants forgets them" in result.output
    assert _list_applied_calls(fake) == [
        {"agent_key": _KEY, "agent_name": _KEY, "revoked": [_DATASET_RECORD]}
    ]
    assert sinks.ran == []
    assert "agents/detach" not in [path for path, _ in fake.calls]


def test_a_recorded_grant_with_no_revoke_command_is_named(
    runner, monkeypatch
) -> None:
    fake = _install_recorded_grants(
        monkeypatch, {**_DATASET_RECORD, "role": "roles/owner"}
    )
    grant_runner = _install_grant_runner(monkeypatch, fake, access_after={})

    result = _invoke(runner, "detach", _KEY, "--yes")

    assert result.exit_code == 0, result.output
    assert (
        "No revoke command for the recorded grant on proj:agent_telemetry"
        in result.output
    )
    assert grant_runner.ran == []
    assert (
        "agents/detach",
        {"agent_key": _KEY, "agent_name": _KEY},
    ) in fake.calls


@pytest.mark.parametrize(
    ("flag", "known"),
    [
        ("--multi-turn-metric", MULTI_TURN_METRICS),
        ("--single-turn-metric", SINGLE_TURN_METRICS),
    ],
)
def test_the_metric_flags_name_every_metric_the_runtime_accepts(
    runner, flag: str, known: dict[str, Any]
) -> None:
    """The CLI does not import the runtime, so its help repeats the names."""
    result = runner.invoke(aqua_cli.cli, ["attach", "--help"])

    help_text = " ".join(result.output.split())
    start = help_text.index(flag)
    entry = help_text[start : help_text.index(" --", start + len(flag))]
    for name in known:
        assert name in entry
