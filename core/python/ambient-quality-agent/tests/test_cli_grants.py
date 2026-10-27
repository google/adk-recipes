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

"""The grant commands `aqua attach` prints for denied reads, and runs."""

from __future__ import annotations

from typing import Any

import pytest
from ambient_quality_cli import grants
from ambient_quality_shared import grant_commands

_SA = "aqua-solo-aqua@proj.iam.gserviceaccount.com"
_TABLE = (
    "proj.agent_telemetry.aiplatform_googleapis_com_reasoning_engine_stdout"
)


def _build_access(
    *checks: tuple[str, str, str], account: str = _SA
) -> dict[str, Any]:
    """Builds an access section as the deployment returns it.

    Args:
        *checks: `(name, status, target)` for each check, in order.
        account: The service account the reads ran as.

    Returns:
        The access section.
    """
    return {
        "service_account": account,
        "checks": [
            {"name": n, "status": s, "target": t, "detail": ""}
            for n, s, t in checks
        ],
    }


def test_the_lines_name_the_account_and_each_command() -> None:
    built, _ = grant_commands.build_grants(
        _build_access(("table", "denied", _TABLE))
    )

    lines = grants.render_grant_lines(built, ["projects/other"], account=_SA)

    assert f"To let {_SA} make those reads" in lines[0]
    assert lines[1] == f"  {built[0].render()}"
    assert "No grant command for projects/other" in lines[2]


def test_nothing_denied_renders_nothing() -> None:
    assert grants.render_grant_lines([], [], account=_SA) == []


def _build_two_grants() -> list[grant_commands.Grant]:
    """Builds a dataset grant and a bucket grant.

    Returns:
        The two grants, dataset first.
    """
    built, _ = grant_commands.build_grants(
        _build_access(
            ("table", "denied", _TABLE),
            ("payload", "denied", "gs://proj-agent-logs/x.json"),
        )
    )
    return built


class _RecordingRunner:
    """Stands in for running a command, and records each one."""

    def __init__(self, status: int = 0, succeed_first: int = 0):
        """Initializes the runner.

        Args:
            status: Exit status every command returns after the first
                `succeed_first`.
            succeed_first: How many commands succeed before `status` applies.
        """
        self.status = status
        self.succeed_first = succeed_first
        self.ran: list[list[str]] = []

    def __call__(self, argv: list[str]) -> int:
        """Records one command.

        Args:
            argv: Command-line arguments vector.

        Returns:
            The scripted exit status.
        """
        self.ran.append(argv)
        return 0 if len(self.ran) <= self.succeed_first else self.status


def test_running_grants_runs_each_command_as_printed() -> None:
    built = _build_two_grants()
    runner = _RecordingRunner()
    echoed: list[str] = []

    grants.run_grants(
        built,
        runner=runner,
        which=lambda program: f"/usr/bin/{program}",
        echo=echoed.append,
    )

    assert runner.ran == [list(g.argv) for g in built]
    assert echoed == [f"$ {g.render()}" for g in built]


def test_a_missing_program_fails_before_any_grant_is_made() -> None:
    runner = _RecordingRunner()

    with pytest.raises(grants.GrantError, match="Not on PATH: bq"):
        grants.run_grants(
            _build_two_grants(),
            runner=runner,
            which=lambda program: None if program == "bq" else program,
            echo=lambda line: None,
        )

    assert runner.ran == []


def test_a_failing_command_stops_the_rest() -> None:
    runner = _RecordingRunner(status=2)

    with pytest.raises(
        grants.GrantError, match="Exit status 2 from: bq"
    ) as raised:
        grants.run_grants(
            _build_two_grants(),
            runner=runner,
            which=lambda program: program,
            echo=lambda line: None,
        )

    assert len(runner.ran) == 1
    # No grant was made, so the message must not say one was.
    assert "were made" not in str(raised.value)


def test_a_failing_revocation_does_not_stop_the_rest() -> None:
    """One whose binding is already gone must not keep the others in place."""
    built = _build_two_grants()
    revocations, _ = grant_commands.build_revocations(
        [g.to_record() for g in built]
    )
    runner = _RecordingRunner(status=1)

    with pytest.raises(grants.GrantError) as raised:
        grants.run_revocations(
            revocations,
            runner=runner,
            which=lambda program: program,
            echo=lambda line: None,
        )

    assert len(runner.ran) == 2
    assert raised.value.completed == ()
    assert str(raised.value).count("Exit status 1 from:") == 2
    assert "were made" not in str(raised.value)


def test_a_failing_revocation_reports_those_that_succeeded() -> None:
    built = _build_two_grants()
    revocations, _ = grant_commands.build_revocations(
        [g.to_record() for g in built]
    )

    with pytest.raises(grants.GrantError) as raised:
        grants.run_revocations(
            revocations,
            runner=_RecordingRunner(status=1, succeed_first=1),
            which=lambda program: program,
            echo=lambda line: None,
        )

    assert raised.value.completed == (revocations[0],)
    assert "The other revocations were made" in str(raised.value)


def test_a_failing_grant_reports_those_made_before_it() -> None:
    built = _build_two_grants()

    with pytest.raises(grants.GrantError) as raised:
        grants.run_grants(
            built,
            runner=_RecordingRunner(status=1, succeed_first=1),
            which=lambda program: program,
            echo=lambda line: None,
        )

    assert raised.value.completed == (built[0],)
    assert "Grants before it were made" in str(raised.value)
