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

import shlex
from typing import Any

import pytest
from ambient_quality_cli import grants

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


def test_a_denied_table_grants_the_dataset_with_a_grant_statement() -> None:
    """The statement writes the same READER entry Terraform's
    `telemetry_reader` makes, in one call that changes nothing when repeated."""
    built, ungranted = grants.build_grants(
        _build_access(("table", "denied", _TABLE))
    )

    assert ungranted == []
    assert [g.argv for g in built] == [
        (
            "bq",
            "query",
            "--nouse_legacy_sql",
            "--project_id=proj",
            "GRANT `roles/bigquery.dataViewer` ON SCHEMA `proj.agent_telemetry` "
            f'TO "serviceAccount:{_SA}"',
        )
    ]
    assert built[0].resource == "proj:agent_telemetry"


def test_two_denied_reads_of_one_dataset_share_one_command() -> None:
    built, _ = grants.build_grants(
        _build_access(("table", "denied", _TABLE), ("rows", "denied", _TABLE))
    )

    assert len(built) == 1
    assert built[0].checks == ("table", "rows")


def test_a_domain_scoped_project_keeps_its_dots() -> None:
    built, _ = grants.build_grants(
        _build_access(("table", "denied", "example.com:proj.ds.tbl"))
    )

    assert built[0].resource == "example.com:proj:ds"
    assert "--project_id=example.com:proj" in built[0].argv


def test_a_denied_payload_grants_its_bucket() -> None:
    built, _ = grants.build_grants(
        _build_access(
            ("payload", "denied", "gs://proj-agent-logs/completions/a/b.json")
        )
    )

    assert [g.argv for g in built] == [
        (
            "gcloud",
            "storage",
            "buckets",
            "add-iam-policy-binding",
            "gs://proj-agent-logs",
            f"--member=serviceAccount:{_SA}",
            "--role=roles/storage.objectViewer",
        )
    ]


def test_a_denied_bucket_with_no_object_grants_that_bucket() -> None:
    """The access check names the bare bucket when it had no object to read."""
    built, ungranted = grants.build_grants(
        _build_access(("payload", "denied", "gs://proj-agent-logs"))
    )

    assert ungranted == []
    assert [g.resource for g in built] == ["gs://proj-agent-logs"]


def test_a_denied_log_view_grants_the_view_accessor_role() -> None:
    built, _ = grants.build_grants(
        _build_access(("log_view", "denied", "projects/proj"))
    )

    assert [g.argv for g in built] == [
        (
            "gcloud",
            "projects",
            "add-iam-policy-binding",
            "proj",
            f"--member=serviceAccount:{_SA}",
            "--role=roles/logging.viewAccessor",
            "--condition=None",
        )
    ]


def test_only_denied_checks_get_a_command() -> None:
    built, ungranted = grants.build_grants(
        _build_access(
            ("table", "passed", _TABLE),
            ("rows", "empty", _TABLE),
            ("payload", "error", "gs://bucket/x"),
            ("log_view", "missing", "projects/proj"),
        )
    )

    assert built == []
    assert ungranted == []


def test_without_a_service_account_nothing_can_be_granted() -> None:
    """A standalone run reads as the user, whom no grant is for."""
    built, ungranted = grants.build_grants(
        _build_access(("table", "denied", _TABLE), account="")
    )

    assert built == []
    assert ungranted == [_TABLE]


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("table", "dataset_only"),
        ("table", "proj.data set.tbl"),
        ("table", "proj.ds`; DROP.tbl"),
        ("payload", "https://example.com/x"),
        ("payload", "gs://Bad Bucket/x"),
        ("log_view", "folders/123"),
        ("table", "proj.ds\n.tbl"),
        ("table", "proj.dä.tbl"),
        ("some_future_check", "anything"),
    ],
)
def test_a_target_outside_the_known_shapes_gets_no_command(
    name, target
) -> None:
    """Nothing the engine reports reaches the command line or the SQL
    statement unless it has the shape of what it claims to be."""
    built, ungranted = grants.build_grants(
        _build_access((name, "denied", target))
    )

    assert built == []
    assert ungranted == [target]


def test_the_rendered_line_is_the_command_as_run() -> None:
    built, _ = grants.build_grants(_build_access(("table", "denied", _TABLE)))

    assert tuple(shlex.split(built[0].render())) == built[0].argv


def test_the_lines_name_the_account_and_each_command() -> None:
    built, _ = grants.build_grants(_build_access(("table", "denied", _TABLE)))

    lines = grants.render_grant_lines(built, ["projects/other"], account=_SA)

    assert f"To let {_SA} make those reads" in lines[0]
    assert lines[1] == f"  {built[0].render()}"
    assert "No grant command for projects/other" in lines[2]


def test_nothing_denied_renders_nothing() -> None:
    assert grants.render_grant_lines([], [], account=_SA) == []


def _build_two_grants() -> list[grants.Grant]:
    """Builds a dataset grant and a bucket grant.

    Returns:
        The two grants, dataset first.
    """
    built, _ = grants.build_grants(
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


def test_a_service_account_with_a_trailing_newline_gets_no_command() -> None:
    built, ungranted = grants.build_grants(
        _build_access(("table", "denied", _TABLE), account=f"{_SA}\n")
    )

    assert built == []
    assert ungranted == [_TABLE]


def test_a_project_number_is_granted_like_an_id() -> None:
    """On Agent Runtime the engine reports its targets by project number."""
    built, ungranted = grants.build_grants(
        _build_access(
            ("table", "denied", "874861634860.agent_telemetry.tbl"),
            ("log_view", "denied", "projects/874861634860"),
        )
    )

    assert ungranted == []
    assert built[0].resource == "874861634860:agent_telemetry"
    assert "--project_id=874861634860" in built[0].argv
    assert built[1].argv[3] == "874861634860"


# --- revoking what was recorded -------------------------------------------- #


def test_each_grant_records_the_binding_it_made() -> None:
    built = _build_two_grants()

    assert [g.to_record() for g in built] == [
        {
            "resource": "proj:agent_telemetry",
            "role": "roles/bigquery.dataViewer",
            "member": f"serviceAccount:{_SA}",
        },
        {
            "resource": "gs://proj-agent-logs",
            "role": "roles/storage.objectViewer",
            "member": f"serviceAccount:{_SA}",
        },
    ]


def test_each_recorded_grant_is_revoked_by_its_inverse() -> None:
    built, _ = grants.build_grants(
        _build_access(
            ("table", "denied", "example.com:proj.ds.tbl"),
            ("payload", "denied", "gs://proj-agent-logs/x.json"),
            ("log_view", "denied", "projects/874861634860"),
        )
    )

    revocations, unrevocable = grants.build_revocations(
        [g.to_record() for g in built]
    )

    assert unrevocable == []
    assert [r.argv for r in revocations] == [
        (
            "bq",
            "query",
            "--nouse_legacy_sql",
            "--project_id=example.com:proj",
            "REVOKE `roles/bigquery.dataViewer` ON SCHEMA `example.com:proj.ds` "
            f'FROM "serviceAccount:{_SA}"',
        ),
        (
            "gcloud",
            "storage",
            "buckets",
            "remove-iam-policy-binding",
            "gs://proj-agent-logs",
            f"--member=serviceAccount:{_SA}",
            "--role=roles/storage.objectViewer",
        ),
        (
            "gcloud",
            "projects",
            "remove-iam-policy-binding",
            "874861634860",
            f"--member=serviceAccount:{_SA}",
            "--role=roles/logging.viewAccessor",
            "--condition=None",
        ),
    ]
    assert [r.to_record() for r in revocations] == [
        g.to_record() for g in built
    ]


_RECORD = {
    "resource": "proj:agent_telemetry",
    "role": "roles/bigquery.dataViewer",
    "member": f"serviceAccount:{_SA}",
}


@pytest.mark.parametrize(
    "record",
    [
        {**_RECORD, "resource": "proj:ds`; DROP"},
        {**_RECORD, "resource": "nodataset"},
        {**_RECORD, "member": "user:someone@example.com"},
        {**_RECORD, "member": f'serviceAccount:{_SA}" OR "x'},
        {**_RECORD, "role": "roles/owner"},
        {**_RECORD, "role": "roles/storage.objectViewer", "resource": "b"},
        {
            **_RECORD,
            "role": "roles/logging.viewAccessor",
            "resource": "folders/1",
        },
        "proj:agent_telemetry",
    ],
)
def test_a_record_outside_the_shapes_a_grant_has_gets_no_command(
    record,
) -> None:
    """The object is the engine's, but what is substituted into a command or
    a statement is checked here as strictly as when it was granted."""
    revocations, unrevocable = grants.build_revocations([record])

    assert revocations == []
    assert unrevocable == [
        record["resource"]
        if isinstance(record, dict)
        else "an unreadable record"
    ]


def test_an_unprintable_resource_is_not_echoed() -> None:
    _, unrevocable = grants.build_revocations(
        [{**_RECORD, "role": "roles/owner", "resource": "x\x1b[2J"}]
    )

    assert unrevocable == ["an unreadable record"]


def test_a_failing_revocation_does_not_stop_the_rest() -> None:
    """One whose binding is already gone must not keep the others in place."""
    built = _build_two_grants()
    revocations, _ = grants.build_revocations([g.to_record() for g in built])
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
    revocations, _ = grants.build_revocations([g.to_record() for g in built])

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
