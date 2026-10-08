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
route goes through. What these check is what the CLI sends, what it shows the
operator before writing, and that it asks first -- the engine side, which
decides what is stored, is `tests/test_observed_agent_config_commands.py`.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

import pytest
from ambient_quality_agent.tools.evaluation.models import (
    MULTI_TURN_METRICS,
    SINGLE_TURN_METRICS,
)
from ambient_quality_cli import aqua_cli
from ambient_quality_cli import grants as grants_lib
from click.testing import CliRunner

_OBJECT = "gs://jobs-bucket/agents/watched_agent.json"

_TARGET = {
    "region": "us-central1",
    "aqua_engine": "projects/p2/locations/us-central1/reasoningEngines/7",
    "ambient_topic": "projects/p2/topics/aqua-solo-aqua-ambient",
    "ambient_caller_email": "aqua-solo-aqua-caller@p2.iam.gserviceaccount.com",
}
"""The trigger target the fake engine reports."""


class _FakeDeployment:
    """Answers `agents/*` routes from a script and records every call."""

    def __init__(self, plan: dict[str, Any] | None = None, listing=None):
        """Initializes the fake with a scripted plan and listing.

        Args:
            plan: Fields that override the default attach response.
            listing: Response for `agents/list`; a single watched agent if None.
        """
        self.plan = {
            "agent_name": "watched_agent",
            "object": _OBJECT,
            "attached": True,
            "watched": True,
            "environment_agent": "watched_agent",
            "changes": [],
            "adjusted": [],
            "needs_apply": [],
            "triggers": {
                "investigation_schedule": "0 6 * * *",
                "scheduled_trigger_enabled": True,
                "observed_engine_id": "42",
                "observed_project_id": "agent-project",
            },
            "trigger_target": dict(_TARGET),
            "written": False,
            **(plan or {}),
        }
        self.listing = listing or {
            "prefix": "gs://jobs-bucket/agents/",
            "agents": [{"agent_name": "watched_agent", "watched": True}],
            "default_agent": "watched_agent",
            "environment_agent": "watched_agent",
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
            if payload.get("check_access") and self.after_checks:
                self.plan.update(self.after_checks.pop(0))
            return response
        if path == "agents/list":
            return self.listing
        if path == "agents/applied":
            return {
                "agent_name": payload["agent_name"],
                "applied_at": "2026-10-01T00:00:00+00:00",
                "unapplied_fields": [],
            }
        if path == "agents/detach":
            return {
                "agent_name": payload["agent_name"],
                "detached": True,
                "watched": True,
            }
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
    }


def test_without_a_name_the_deployment_picks_the_agent(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch)

    _invoke(runner, "attach", "--dry-run")

    assert fake.calls[0][1]["agent_name"] == ""


def test_attach_plans_before_it_writes(runner, monkeypatch) -> None:
    fake = _install_deployment(monkeypatch, plan=_A_CHANGE)

    result = _invoke(runner, "attach", "--lookback-days", "30", "--yes")

    assert result.exit_code == 0, result.output
    assert [p["dry_run"] for _, p in fake.calls] == [True, False]
    assert "Attached watched_agent." in result.output


def test_attach_never_touches_storage_itself(runner, monkeypatch) -> None:
    """The CLI talks to AQuA only through its API. Minting a token is the first
    step of any direct bucket call, so an attach that needed one would fail
    here."""
    _install_deployment(monkeypatch, plan=_A_CHANGE)

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
    """It moves the settings out of the environment and changes nothing about
    how the agent is watched, so there is nothing to confirm."""
    fake = _install_deployment(monkeypatch, plan={"attached": False})

    result = _invoke(runner, "attach")

    assert result.exit_code == 0, result.output
    assert "starting from the engine's environment" in result.output
    assert len(fake.list_writes()) == 1


def test_a_value_the_deployment_adjusted_is_pointed_out(
    runner, monkeypatch
) -> None:
    _install_deployment(
        monkeypatch,
        plan={
            "changes": [
                {"field": "data_lookback_window", "before": 14, "after": 64}
            ],
            "adjusted": [
                {
                    "field": "data_lookback_window",
                    "requested": 9999,
                    "stored": 64,
                }
            ],
        },
    )

    result = _invoke(runner, "attach", "--lookback-days", "9999", "--dry-run")

    assert "asked for 9999, the deployment stores 64" in result.output


def test_a_schedule_change_says_it_needs_terraform(runner, monkeypatch) -> None:
    _install_deployment(
        monkeypatch,
        plan={
            "changes": [
                {
                    "field": "investigation_schedule",
                    "before": "",
                    "after": "0 12 * * *",
                }
            ],
            "needs_apply": ["investigation_schedule"],
        },
    )

    result = _invoke(runner, "attach", "--schedule", "0 12 * * *", "--dry-run")

    assert "not yet in effect: investigation_schedule" in result.output
    assert "Terraform" in result.output


def test_attaching_an_agent_the_deployment_does_not_watch_says_so(
    runner, monkeypatch
) -> None:
    """Until one AQuA watches several agents, that object is never read."""
    fake = _install_deployment(
        monkeypatch,
        plan={"agent_name": "other", "watched": False, "attached": False},
    )

    result = _invoke(runner, "attach", "other", "--dry-run")

    assert (
        "This deployment investigates watched_agent, not other" in result.output
    )
    # An attach cannot displace the agent the environment names.
    assert "only a separate AQuA deployment can investigate other" in (
        result.output
    )
    assert "--default" not in result.output
    # The plan already names it, so no listing is needed.
    assert [path for path, _ in fake.calls] == ["agents/attach"]


_LISTING_WITH_DEFAULT_FIRST_AGENT = {
    "prefix": "gs://jobs-bucket/agents/",
    "agents": [{"agent_name": "first_agent", "watched": True}],
    "default_agent": "first_agent",
    "environment_agent": "",
    "trigger_target": dict(_TARGET),
}
"""A deployment whose environment names no agent, watching its one attachment."""


def test_an_agent_behind_the_default_attachment_is_named_with_the_remedy(
    runner, monkeypatch
) -> None:
    """With no agent in the environment, the deployment investigates its
    default attached agent, and `--default` moves that to this one."""
    _install_deployment(
        monkeypatch,
        plan={
            "agent_name": "other",
            "watched": False,
            "environment_agent": "",
            "attached": False,
        },
        listing=_LISTING_WITH_DEFAULT_FIRST_AGENT,
    )

    result = _invoke(runner, "attach", "other", "--dry-run")

    assert (
        "This deployment investigates first_agent, not other" in result.output
    )
    assert "attach it with --default" in result.output
    assert "another agent" not in result.output


def test_an_unreadable_listing_still_shows_the_note(
    runner, monkeypatch
) -> None:
    """The name only makes the note clearer; failing to read it fails nothing."""
    _install_deployment(
        monkeypatch,
        plan={
            "agent_name": "other",
            "watched": False,
            "environment_agent": "",
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


def test_a_denied_read_is_named_and_the_attach_goes_ahead(
    runner, monkeypatch
) -> None:
    """The attachment is right; the grant can follow it."""
    fake = _install_deployment(
        monkeypatch,
        plan={**_A_CHANGE, **_build_access(("table", "denied", _TABLE))},
    )

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


def test_a_denied_read_prints_its_grant_and_runs_nothing(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch, plan=_DENIED_TABLE)
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach")

    assert result.exit_code == 0, result.output
    assert f"To let {_SA} make those reads" in result.output
    assert f"  {_GRANT_LINE}" in result.output
    assert "Or run attach with --apply to run them." in result.output
    assert grant_runner.ran == []


def test_apply_runs_exactly_the_printed_commands_then_checks_again(
    runner, monkeypatch, terraform_runs
) -> None:
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", "--apply", "--yes")

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
    assert [run["apply"] for run in terraform_runs] == [True]


def test_apply_asks_before_granting(
    runner, monkeypatch, terraform_runs
) -> None:
    fake = _install_deployment(monkeypatch, plan=_DENIED_TABLE)
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", "--apply", stdin="n\n")

    assert result.exit_code != 0
    assert "Run 1 grant command(s) with your credentials?" in result.output
    assert grant_runner.ran == []
    assert fake.list_writes() == []


def test_a_dry_run_grants_nothing_even_with_apply(
    runner, monkeypatch, terraform_runs
) -> None:
    fake = _install_deployment(monkeypatch, plan=_DENIED_TABLE)
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", "--apply", "--dry-run", "--yes")

    assert result.exit_code == 0, result.output
    assert "nothing was written or granted" in result.output
    assert grant_runner.ran == []


def test_apply_grants_even_when_no_setting_changes(
    runner, monkeypatch, terraform_runs
) -> None:
    """Re-attaching an agent whose reads are denied is how the grants are
    made for an attachment already stored."""
    fake = _install_deployment(monkeypatch, plan=_DENIED_TABLE)
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert len(grant_runner.ran) == 1
    assert fake.list_writes() == []


def test_a_grant_still_denied_is_checked_again_and_reported(
    runner, monkeypatch, terraform_runs
) -> None:
    """Propagation is not instant; the attach still goes ahead, as it does
    for any denied read."""
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_DENIED_TABLE
    )

    result = _invoke(runner, "attach", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert grant_runner.sleeps == [
        d for d in aqua_cli._RECHECK_DELAYS_SECONDS if d
    ]
    assert (
        "Still denied after the grants: proj:agent_telemetry, so rows could "
        "not be checked yet" in result.output
    )
    assert "run attach --apply again to grant what they need" in result.output
    assert len(fake.list_writes()) == 1


def test_a_read_denied_behind_the_granted_one_waits_for_propagation(
    runner, monkeypatch, terraform_runs
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

    result = _invoke(runner, "attach", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert grant_runner.sleeps == [
        d for d in aqua_cli._RECHECK_DELAYS_SECONDS if d
    ]
    assert "Or run attach --apply again" not in result.output


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


def test_a_read_denied_behind_the_granted_one_is_granted_in_the_same_apply(
    runner, monkeypatch, terraform_runs
) -> None:
    """The payload check is skipped until the rows are readable, so its
    bucket's grant can only be known after the dataset's. One --apply makes
    both, printing the bucket's before running it."""
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=[_PAYLOAD_DENIED, _PAYLOAD_PASSED]
    )

    result = _invoke(runner, "attach", "--apply", "--yes")

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
    assert "Or run attach --apply again" not in result.output
    assert len(fake.list_writes()) == 1
    assert _list_applied_calls(fake)[:2] == [
        {"agent_name": "watched_agent", "granted": [_DATASET_RECORD]},
        {"agent_name": "watched_agent", "granted": [_BUCKET_RECORD]},
    ]
    assert [r["apply"] for r in terraform_runs] == [True]


def test_each_round_of_grants_is_confirmed(
    runner, monkeypatch, terraform_runs
) -> None:
    """Without --yes, the grant a re-check reveals is asked about on its own,
    and declining it stops the attach before the triggers are applied. The
    attachment is already stored, with the first round's grant recorded on it
    for detach --apply to revoke."""
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=[_PAYLOAD_DENIED, _PAYLOAD_PASSED]
    )

    result = _invoke(runner, "attach", "--apply", stdin="y\ny\nn\n")

    assert result.exit_code == 1
    assert result.output.count("grant command(s) with your credentials?") == 2
    assert [argv[0] for argv in grant_runner.ran] == ["bq"]
    assert result.output.index(
        "add-iam-policy-binding gs://proj-agent-logs"
    ) < result.output.rindex("grant command(s) with your credentials?")
    assert len(fake.list_writes()) == 1
    assert _list_applied_calls(fake) == [
        {"agent_name": "watched_agent", "granted": [_DATASET_RECORD]}
    ]
    assert terraform_runs == []


def test_a_failing_grant_in_a_later_round_keeps_the_earlier_round_recorded(
    runner, monkeypatch, terraform_runs
) -> None:
    """The earlier round's grant stays recorded on the stored attachment, so
    detach --apply still revokes it, and the attach stops before applying the
    triggers."""
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    grant_runner = _install_grant_runner(
        monkeypatch,
        fake,
        access_after=[_PAYLOAD_DENIED, _PAYLOAD_PASSED],
        status=[0, 1],
    )

    result = _invoke(runner, "attach", "--apply", "--yes")

    assert result.exit_code == 1
    assert [argv[0] for argv in grant_runner.ran] == ["bq", "gcloud"]
    assert len(fake.list_writes()) == 1
    assert _list_applied_calls(fake) == [
        {"agent_name": "watched_agent", "granted": [_DATASET_RECORD]}
    ]
    assert terraform_runs == []


def test_a_grant_revealed_while_another_propagates_runs_once_that_lands(
    runner, monkeypatch, terraform_runs
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

    result = _invoke(runner, "attach", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert grant_runner.sleeps == [aqua_cli._RECHECK_DELAYS_SECONDS[1]]
    assert [argv[:3] for argv in grant_runner.ran] == [
        ["bq", "query", "--nouse_legacy_sql"],
        ["gcloud", "projects", "add-iam-policy-binding"],
    ]
    assert "Still denied after the grants" not in result.output
    assert len(fake.list_writes()) == 1


def test_grants_stop_after_the_last_round(
    runner, monkeypatch, terraform_runs
) -> None:
    """A check that keeps revealing new grants cannot loop: after the last
    round, what is left is printed for the next --apply."""
    fake = _install_deployment(monkeypatch, plan=_DENIED_TABLE)
    rounds = [
        _build_access(("payload", "denied", f"gs://bucket-{n}/x.json"))
        for n in range(aqua_cli._MAX_GRANT_ROUNDS)
    ]
    grant_runner = _install_grant_runner(monkeypatch, fake, access_after=rounds)

    result = _invoke(runner, "attach", "--apply", "--yes")

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
    assert "Or run attach --apply again to run them." in result.output


def test_missing_telemetry_refuses_the_attach_before_any_grant(
    runner, monkeypatch, terraform_runs
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

    result = _invoke(runner, "attach", "--apply", "--yes")

    assert result.exit_code == 1
    assert "Not attaching" in result.output
    assert "--apply to run them" not in result.output
    assert grant_runner.ran == []
    assert fake.list_writes() == []


def test_apply_with_every_read_allowed_has_nothing_to_grant(
    runner, monkeypatch, terraform_runs
) -> None:
    fake = _install_deployment(monkeypatch, plan=_PASSED_TABLE)
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert "Nothing to grant." in result.output
    assert grant_runner.ran == []


def test_a_failed_grant_fails_the_attach_after_storing_it(
    runner, monkeypatch, terraform_runs
) -> None:
    """Stored first, so that a grant made before the failure has an
    attachment to be recorded on."""
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE, status=1
    )

    result = _invoke(runner, "attach", "--apply", "--yes")

    assert result.exit_code == 1
    assert "Exit status 1 from: bq query" in result.output
    assert len(fake.list_writes()) == 1
    assert terraform_runs == []


def test_apply_against_a_deployment_that_checks_nothing_says_so(
    runner, monkeypatch, terraform_runs
) -> None:
    _install_deployment(monkeypatch)

    result = _invoke(runner, "attach", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert "checked no reads, so there is nothing to grant" in result.output


# --- list-agents and detach ------------------------------------------------------------ #


def test_list_agents_prints_the_deployments_answer_as_json(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "list-agents")

    assert result.exit_code == 0
    assert json.loads(result.output)["default_agent"] == "watched_agent"
    assert fake.calls == [("agents/list", None)]


def test_detaching_asks_first(runner, monkeypatch) -> None:
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "detach", "watched_agent", stdin="n\n")

    assert result.exit_code != 0
    assert [path for path, _ in fake.calls] == ["agents/list"]


def test_detaching_says_where_the_settings_come_from(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "detach", "watched_agent", "--yes")

    assert result.exit_code == 0, result.output
    assert ("agents/detach", {"agent_name": "watched_agent"}) in fake.calls
    assert "come from the engine's environment" in result.output


def test_detaching_what_is_not_attached_deletes_nothing(
    runner, monkeypatch
) -> None:
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "detach", "nobody", "--yes")

    assert result.exit_code == 0
    assert "not attached" in result.output
    assert [path for path, _ in fake.calls] == ["agents/list"]


# --- --apply: the triggers, with Terraform ------------------------------------ #


@pytest.fixture
def terraform_runs(tmp_path, monkeypatch) -> list[dict[str, Any]]:
    """Makes an empty directory the working directory and records Terraform runs.

    Nothing of AQuA's own Terraform is there: `--apply` must not need it.

    Args:
        tmp_path: Temporary directory fixture, made the working directory.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The keyword arguments of each `run_terraform` call, in order.
    """
    monkeypatch.chdir(tmp_path)
    runs: list[dict[str, Any]] = []
    monkeypatch.setattr(
        aqua_cli,
        "run_terraform",
        lambda root, **kwargs: runs.append({"root": root, **kwargs}),
    )
    return runs


def _write_attach_state(tmp_path, agent: str, target=None) -> Any:
    """Writes an agent's attach state as an earlier `--apply` would leave it.

    Args:
        tmp_path: The working directory.
        agent: Agent name.
        target: The instance it was applied against; `_TARGET` if None.

    Returns:
        The state file's path.
    """
    state = tmp_path / ".aqua" / f"attach-{agent}.tfstate"
    state.parent.mkdir(exist_ok=True)
    state.write_text(
        json.dumps(
            {"outputs": {"instance": {"value": target or dict(_TARGET)}}}
        ),
        encoding="utf-8",
    )
    return state


def test_apply_stores_then_applies_then_records_what_it_applied(
    runner, monkeypatch, terraform_runs
) -> None:
    fake = _install_deployment(
        monkeypatch,
        plan={
            "changes": [
                {
                    "field": "investigation_schedule",
                    "before": "",
                    "after": "0 6 * * *",
                }
            ],
            "needs_apply": ["investigation_schedule"],
        },
    )

    result = _invoke(
        runner, "attach", "--schedule", "0 6 * * *", "--apply", "--yes"
    )

    assert result.exit_code == 0, result.output
    assert [path for path, _ in fake.calls] == [
        "agents/attach",
        "agents/attach",
        "agents/applied",
    ]
    (run,) = terraform_runs
    assert run["root"].parts[-2:] == ("examples", "attach")
    assert run["state_key"] == "attach-watched_agent"
    assert run["apply"] and not run["destroy"]
    assert run["tf_vars"] == {
        **_TARGET,
        "observed_agent_name": "watched_agent",
        "investigation_schedule": "0 6 * * *",
        "scheduled_trigger_enabled": "true",
        "observed_engine_id": "42",
        # The audit sink goes where the observed engine is.
        "observed_project_id": "agent-project",
    }
    assert fake.calls[-1][1] == {
        "agent_name": "watched_agent",
        "applied": fake.plan["triggers"],
    }
    assert "not yet in effect" not in result.output


def test_apply_with_nothing_to_store_still_applies(
    runner, monkeypatch, terraform_runs
) -> None:
    """What is stored may never have been applied, as after a plain attach."""
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "attach", "--apply")

    assert result.exit_code == 0, result.output
    assert fake.list_writes() == []
    assert len(terraform_runs) == 1
    assert fake.calls[-1][0] == "agents/applied"


def test_a_dry_run_with_apply_plans_the_triggers_and_writes_nothing(
    runner, monkeypatch, terraform_runs
) -> None:
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "attach", "--dry-run", "--apply")

    assert result.exit_code == 0, result.output
    (run,) = terraform_runs
    assert not run["apply"]
    assert [path for path, _ in fake.calls] == ["agents/attach"]


def test_unset_trigger_fields_are_left_to_the_roots_defaults(
    runner, monkeypatch, terraform_runs
) -> None:
    _install_deployment(
        monkeypatch,
        plan={
            "triggers": {
                "investigation_schedule": "",
                "scheduled_trigger_enabled": False,
                "observed_engine_id": "",
                "observed_project_id": "",
            }
        },
    )

    result = _invoke(runner, "attach", "--apply")

    assert result.exit_code == 0, result.output
    tf_vars = terraform_runs[0]["tf_vars"]
    assert "investigation_schedule" not in tf_vars
    assert "observed_engine_id" not in tf_vars
    assert "observed_project_id" not in tf_vars
    assert tf_vars["scheduled_trigger_enabled"] == "false"


def test_apply_needs_nothing_of_aquas_own_terraform(
    runner, monkeypatch, terraform_runs, tmp_path
) -> None:
    """The engine's resource name is all `--apply` is given."""
    _install_deployment(monkeypatch)

    result = _invoke(runner, "attach", "--apply")

    assert result.exit_code == 0, result.output
    assert [p.name for p in (tmp_path / ".aqua").iterdir()] == []
    assert terraform_runs[0]["state_root"] == tmp_path / ".aqua"


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"region": "us-central1", "aqua_engine": "projects/p2/x"},
    ],
)
def test_apply_refuses_an_engine_that_reports_no_target_before_storing(
    runner, monkeypatch, terraform_runs, target
) -> None:
    """One that names its observed agent runs that agent's triggers itself, and
    a second scheduler would investigate every window twice."""
    fake = _install_deployment(
        monkeypatch,
        plan={
            "trigger_target": target,
            "changes": [
                {"field": "data_lookback_window", "before": 7, "after": 9}
            ],
        },
    )

    result = _invoke(
        runner, "attach", "--lookback-days", "9", "--apply", "--yes"
    )

    assert result.exit_code == 1
    assert "reports nothing to wire triggers to" in result.output
    assert "agents-cli infra single-project --apply" in result.output
    assert fake.list_writes() == []
    assert terraform_runs == []


def test_detach_with_apply_destroys_the_triggers_then_detaches(
    runner, monkeypatch, terraform_runs, tmp_path
) -> None:
    state = _write_attach_state(tmp_path, "watched_agent")
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "detach", "watched_agent", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    (run,) = terraform_runs
    assert run["destroy"] and run["apply"]
    assert run["state_key"] == "attach-watched_agent"
    assert run["tf_vars"] == {
        **_TARGET,
        "observed_agent_name": "watched_agent",
        "scheduled_trigger_enabled": "true",
    }
    assert not state.exists()
    assert [path for path, _ in fake.calls] == ["agents/list", "agents/detach"]


def test_detach_with_apply_and_no_triggers_here_still_detaches(
    runner, monkeypatch, terraform_runs
) -> None:
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "detach", "watched_agent", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert terraform_runs == []
    assert (
        "No triggers of watched_agent were applied from here" in result.output
    )
    assert ("agents/detach", {"agent_name": "watched_agent"}) in fake.calls


def test_detach_with_apply_removes_triggers_left_behind(
    runner, monkeypatch, terraform_runs, tmp_path
) -> None:
    """An object detached without --apply leaves its triggers running."""
    _write_attach_state(tmp_path, "gone")
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "detach", "gone", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert terraform_runs[0]["destroy"]
    assert [path for path, _ in fake.calls] == ["agents/list"]


def test_detach_with_apply_refuses_triggers_wired_to_another_aqua(
    runner, monkeypatch, terraform_runs, tmp_path
) -> None:
    """Detaching from one AQuA must not destroy what calls another."""
    _write_attach_state(
        tmp_path,
        "watched_agent",
        {
            **_TARGET,
            "aqua_engine": "projects/p2/locations/x/reasoningEngines/8",
        },
    )
    fake = _install_deployment(monkeypatch)

    result = _invoke(runner, "detach", "watched_agent", "--apply", "--yes")

    assert result.exit_code == 1
    assert "not the AQuA this command reached" in result.output
    assert terraform_runs == []
    assert [path for path, _ in fake.calls] == ["agents/list"]


def test_apply_refuses_an_agent_the_deployment_does_not_watch(
    runner, monkeypatch, terraform_runs
) -> None:
    """Its triggers would investigate the watched agent a second time."""
    fake = _install_deployment(
        monkeypatch,
        plan={"agent_name": "other", "watched": False, "attached": False},
    )

    result = _invoke(runner, "attach", "other", "--apply", "--yes")

    assert result.exit_code == 1
    assert "this deployment investigates watched_agent" in result.output
    assert "Nothing was granted, stored or applied" in result.output
    assert fake.list_writes() == []
    assert terraform_runs == []


def test_apply_refusing_an_unwatched_agent_runs_none_of_the_printed_grants(
    runner, monkeypatch, terraform_runs
) -> None:
    """The grant commands are printed before the refusal, so the refusal has to
    say they were not run, and suggest the flag that lets them run."""
    fake = _install_deployment(
        monkeypatch,
        plan={
            **_DENIED_TABLE,
            "agent_name": "other",
            "watched": False,
            "environment_agent": "",
            "attached": False,
        },
        listing=_LISTING_WITH_DEFAULT_FIRST_AGENT,
    )
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", "other", "--apply", "--yes")

    assert result.exit_code == 1
    assert _GRANT_LINE in result.output
    assert "this deployment investigates first_agent" in result.output
    assert "Nothing was granted, stored or applied" in result.output
    assert "attach it with --default" in result.output
    # The plan's own note would repeat what the refusal says.
    assert "An attachment for any other agent is stored" not in result.output
    assert grant_runner.ran == []
    assert fake.list_writes() == []
    assert terraform_runs == []


def test_the_cli_expects_the_target_the_engine_reports() -> None:
    """The engine's side is pinned to the attach root's variables in
    `test_observed_agent_config_commands.py`; this pins the CLI to the same."""
    assert set(_TARGET) == aqua_cli._TRIGGER_TARGET_KEYS


def test_detach_with_apply_refuses_an_aqua_that_reports_no_target(
    runner, monkeypatch, terraform_runs, tmp_path
) -> None:
    """Without the reached engine's name there is no telling what the triggers
    here call."""
    _write_attach_state(tmp_path, "watched_agent")
    fake = _install_deployment(monkeypatch)
    fake.listing["trigger_target"] = {}

    result = _invoke(runner, "detach", "watched_agent", "--apply", "--yes")

    assert result.exit_code == 1
    assert "which reports no trigger target" in result.output
    assert terraform_runs == []


# --- grants recorded on the attachment, and revoked by detach --apply ----------------- #

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


def test_apply_stores_then_grants_then_records_the_grants_it_ran(
    runner, monkeypatch, terraform_runs
) -> None:
    fake = _install_deployment(
        monkeypatch, plan={**_A_CHANGE, "attached": False, **_DENIED_TABLE}
    )
    _install_grant_runner(monkeypatch, fake, access_after=_PASSED_TABLE)

    result = _invoke(runner, "attach", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert [
        "applied" if path == "agents/applied" else p["dry_run"]
        for path, p in fake.calls
    ] == [True, False, "applied", True, "applied"]
    granted, triggers = _list_applied_calls(fake)
    assert granted == {
        "agent_name": "watched_agent",
        "granted": [_DATASET_RECORD],
    }
    assert "granted" not in triggers
    assert "detach --apply revokes them" in result.output


def test_apply_records_its_grants_on_an_attachment_it_leaves_unchanged(
    runner, monkeypatch, terraform_runs
) -> None:
    fake = _install_deployment(monkeypatch, plan=_DENIED_TABLE)
    _install_grant_runner(monkeypatch, fake, access_after=_PASSED_TABLE)

    result = _invoke(runner, "attach", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert fake.list_writes() == []
    assert _list_applied_calls(fake)[0] == {
        "agent_name": "watched_agent",
        "granted": [_DATASET_RECORD],
    }


def test_an_apply_with_nothing_to_grant_records_no_grant(
    runner, monkeypatch, terraform_runs
) -> None:
    fake = _install_deployment(monkeypatch, plan=_PASSED_TABLE)

    result = _invoke(runner, "attach", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert [list(p) for p in _list_applied_calls(fake)] == [
        ["agent_name", "applied"]
    ]


@pytest.mark.parametrize("attached", [True, False])
def test_a_grant_that_fails_records_those_made_before_it(
    runner, monkeypatch, terraform_runs, attached
) -> None:
    """Otherwise the dataset grant would outlive every detach --apply: a rerun
    finds that read allowed, and grants and records nothing for it."""
    fake = _install_deployment(
        monkeypatch, plan={"attached": attached, **_DENIED_TABLE_AND_PAYLOAD}
    )
    _install_grant_runner(monkeypatch, fake, access_after={}, status=[0, 1])

    result = _invoke(runner, "attach", "--apply", "--yes")

    assert result.exit_code == 1
    assert "Exit status 1 from: gcloud storage" in result.output
    assert _list_applied_calls(fake) == [
        {"agent_name": "watched_agent", "granted": [_DATASET_RECORD]}
    ]
    assert len(fake.list_writes()) == (0 if attached else 1)
    assert terraform_runs == []


def test_a_grant_record_the_deployment_refuses_shows_how_to_revoke_it(
    runner, monkeypatch, terraform_runs
) -> None:
    """An engine older than the CLI takes no grants; the triggers still get
    applied, and the user learns what detach --apply cannot undo."""
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

    result = _invoke(runner, "attach", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert "Could not record the grants" in result.output
    assert f"  {_REVOKE_LINE}" in result.output
    assert len(terraform_runs) == 1


def test_declining_the_grants_stores_nothing(
    runner, monkeypatch, terraform_runs
) -> None:
    """Both questions come before anything is done."""
    fake = _install_deployment(monkeypatch, plan={**_A_CHANGE, **_DENIED_TABLE})
    grant_runner = _install_grant_runner(
        monkeypatch, fake, access_after=_PASSED_TABLE
    )

    result = _invoke(runner, "attach", "--apply", stdin="y\nn\n")

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
            "environment_agent": "watched_agent",
            "trigger_target": dict(_TARGET),
        },
    )


def test_detach_with_apply_revokes_the_recorded_grants_and_records_that(
    runner, monkeypatch, terraform_runs, tmp_path
) -> None:
    _write_attach_state(tmp_path, "watched_agent")
    fake = _install_recorded_grants(monkeypatch, _DATASET_RECORD)
    grant_runner = _install_grant_runner(monkeypatch, fake, access_after={})

    result = _invoke(runner, "detach", "watched_agent", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert [shlex.join(argv) for argv in grant_runner.ran] == [_REVOKE_LINE]
    assert [path for path, _ in fake.calls] == [
        "agents/list",
        "agents/applied",
        "agents/detach",
    ]
    assert _list_applied_calls(fake) == [
        {"agent_name": "watched_agent", "revoked": [_DATASET_RECORD]}
    ]
    assert terraform_runs[0]["destroy"]


def test_detach_with_apply_asks_before_revoking(
    runner, monkeypatch, terraform_runs
) -> None:
    fake = _install_recorded_grants(monkeypatch, _DATASET_RECORD)
    grant_runner = _install_grant_runner(monkeypatch, fake, access_after={})

    result = _invoke(runner, "detach", "watched_agent", "--apply", stdin="n\n")

    assert result.exit_code != 0
    assert f"  {_REVOKE_LINE}" in result.output
    assert "and revoke 1 grant(s)?" in result.output
    assert grant_runner.ran == []
    assert [path for path, _ in fake.calls] == ["agents/list"]


def test_detach_without_apply_shows_the_grants_it_leaves_and_runs_nothing(
    runner, monkeypatch
) -> None:
    fake = _install_recorded_grants(monkeypatch, _DATASET_RECORD)
    grant_runner = _install_grant_runner(monkeypatch, fake, access_after={})

    result = _invoke(runner, "detach", "watched_agent", "--yes")

    assert result.exit_code == 0, result.output
    assert "left in place and forgotten once detached" in result.output
    assert f"  {_REVOKE_LINE}" in result.output
    assert grant_runner.ran == []
    assert _list_applied_calls(fake) == []


def test_a_failed_revocation_records_those_made_and_keeps_the_attachment(
    runner, monkeypatch, terraform_runs, tmp_path
) -> None:
    """Running detach --apply again then retries only the rest."""
    _write_attach_state(tmp_path, "watched_agent")
    fake = _install_recorded_grants(
        monkeypatch, _DATASET_RECORD, _BUCKET_RECORD
    )
    _install_grant_runner(monkeypatch, fake, access_after={}, status=[0, 1])

    result = _invoke(runner, "detach", "watched_agent", "--apply", "--yes")

    assert result.exit_code == 1
    assert "remove-iam-policy-binding gs://proj-agent-logs" in result.output
    assert "watched_agent stays attached" in result.output
    assert "detach without --apply forgets them" in result.output
    assert _list_applied_calls(fake) == [
        {"agent_name": "watched_agent", "revoked": [_DATASET_RECORD]}
    ]
    assert terraform_runs == []
    assert "agents/detach" not in [path for path, _ in fake.calls]


def test_a_recorded_grant_with_no_revoke_command_is_named(
    runner, monkeypatch, terraform_runs
) -> None:
    fake = _install_recorded_grants(
        monkeypatch, {**_DATASET_RECORD, "role": "roles/owner"}
    )
    grant_runner = _install_grant_runner(monkeypatch, fake, access_after={})

    result = _invoke(runner, "detach", "watched_agent", "--apply", "--yes")

    assert result.exit_code == 0, result.output
    assert (
        "No revoke command for the recorded grant on proj:agent_telemetry"
        in result.output
    )
    assert grant_runner.ran == []
    assert ("agents/detach", {"agent_name": "watched_agent"}) in fake.calls


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
