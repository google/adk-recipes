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

"""The Cloud Trace bucket and link lookup, their creation, and `link-traces`.

REST is faked at the `JsonApi` seam and gcloud at `run_command`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from ambient_quality_cli import aqua_cli
from ambient_quality_cli import rest_client as rest_client_lib
from ambient_quality_cli import trace_link as trace_link_lib
from ambient_quality_cli.rest_client import ApiError
from click.testing import CliRunner

_PROJECT = "p2"
_LOCATIONS = (
    f"https://observability.googleapis.com/v1/projects/{_PROJECT}/locations"
)
_BUCKETS = f"{_LOCATIONS}/-/buckets"
_SETTINGS = f"{_LOCATIONS}/global/settings"
_PERMISSIONS = (
    "https://cloudresourcemanager.googleapis.com/v3/projects/"
    f"{_PROJECT}:testIamPermissions"
)
_DATASETS = (
    f"https://bigquery.googleapis.com/bigquery/v2/projects/{_PROJECT}/datasets"
)
_DISABLED = ApiError(403, "Observability API is disabled", "SERVICE_DISABLED")
_MAY_ENABLE = {"permissions": ["serviceusage.services.enable"]}
_ENABLE = [
    "gcloud",
    "services",
    "enable",
    "observability.googleapis.com",
    f"--project={_PROJECT}",
]


def _build_links_url(location: str) -> str:
    """Builds the URL listing the links on `_Trace/Spans`.

    Args:
        location: The bucket's location.

    Returns:
        The URL.
    """
    return f"{_LOCATIONS}/{location}/buckets/_Trace/datasets/Spans/links"


def _build_project_responses(
    bucket: str | None = None, link: str | None = None, **more: Any
) -> dict[str, Any]:
    """Builds the replies of a project with an optional bucket and link.

    Args:
        bucket: The `_Trace` bucket's location; None for no bucket.
        link: The id of the link on it; None for no link.
        **more: More replies, by URL.

    Returns:
        Replies by URL.
    """
    buckets = [{"name": f"projects/{_PROJECT}/locations/global/buckets/x"}]
    responses: dict[str, Any] = {_BUCKETS: {"buckets": buckets}}
    if bucket:
        buckets.append(
            {"name": f"projects/{_PROJECT}/locations/{bucket}/buckets/_Trace"}
        )
        parent = f"projects/{_PROJECT}/locations/{bucket}/buckets/_Trace"
        responses[_build_links_url(bucket)] = {
            "links": (
                [{"name": f"{parent}/datasets/Spans/links/{link}"}]
                if link
                else []
            )
        }
    responses.update(more)
    return responses


class _FakeApi:
    """Answers GETs and POSTs by URL and records them."""

    def __init__(self, responses: dict[str, Any]):
        """Initializes the fake.

        Args:
            responses: Reply per URL: a dict, a callable, or an `ApiError`.
        """
        self.responses = responses
        self.urls: list[str] = []

    def get_json(self, url, params=None):
        """Answers a GET.

        Args:
            url: Request URL.
            params: Query parameters.

        Returns:
            The scripted reply.
        """
        return self.post_json(url, params)

    def post_json(self, url, body):
        """Answers a POST.

        Args:
            url: Request URL.
            body: Request body.

        Returns:
            The scripted reply.

        Raises:
            ApiError: For a URL with no reply, or one scripted to fail.
        """
        self.urls.append(url)
        reply = self.responses.get(url, ApiError(404, f"no fake for {url}"))
        if isinstance(reply, ApiError):
            raise reply
        return reply() if callable(reply) else reply


class _Gcloud:
    """Stands in for gcloud: records commands and reports a status."""

    def __init__(self, status: int = 0, on_run=None):
        """Initializes the fake.

        Args:
            status: The exit status of every command.
            on_run: Called with each command before it reports.
        """
        self.status = status
        self.on_run = on_run
        self.commands: list[list[str]] = []

    def __call__(self, argv: list[str]) -> int:
        """Records a command.

        Args:
            argv: Command-line arguments vector.

        Returns:
            The scripted exit status.
        """
        self.commands.append(argv)
        if self.on_run:
            self.on_run(argv)
        return self.status


def _create(link, api, gcloud=None, **kwargs):
    """Runs `create_trace_link` with no real waiting.

    Args:
        link: What to create.
        api: Fake REST client.
        gcloud: Fake command runner; a succeeding one if None.
        **kwargs: More arguments.

    Returns:
        What `create_trace_link` returns.
    """
    return trace_link_lib.create_trace_link(
        link,
        api=api,
        run_command=gcloud or _Gcloud(),
        echo=lambda line: None,
        sleep=lambda seconds: None,
        **kwargs,
    )


def _turn_api_on_after_enable(api: _FakeApi, replies: dict[str, Any]):
    """Makes the project answer with ``replies`` once the API is enabled.

    The first lookup after enabling still answers SERVICE_DISABLED.

    Args:
        api: Fake REST client to change.
        replies: The project's replies once the API is on.

    Returns:
        A command hook for `_Gcloud`.
    """
    disabled_once = [True]

    def list_buckets():
        if disabled_once.pop() if disabled_once else False:
            raise _DISABLED
        return replies[_BUCKETS]

    def switch_api_on(argv: list[str]) -> None:
        if argv == _ENABLE:
            api.responses.update(replies)
            api.responses[_BUCKETS] = list_buckets

    return switch_api_on


# --- the lookup ---------------------------------------------------------------


@pytest.mark.parametrize("link_id", ["trace_spans_linked", "their_spans"])
def test_an_existing_link_is_taken_whatever_its_id(link_id):
    api = _FakeApi(
        _build_project_responses(
            "us-east1",
            link_id,
            **{f"{_DATASETS}/{link_id}": {"location": "us-east1"}},
        )
    )

    link = trace_link_lib.resolve_trace_link(api, _PROJECT)

    assert (link.link_id, link.location, link.bigquery_location) == (
        link_id,
        "us-east1",
        "us-east1",
    )
    assert link.build_needed_argvs() == []


def test_a_bucket_with_no_link_plans_the_link_at_its_location():
    api = _FakeApi(_build_project_responses("eu"))

    link = trace_link_lib.resolve_trace_link(api, _PROJECT)

    assert link.link_id == "trace_spans_linked"
    assert link.bigquery_location == "EU"
    assert [argv[:6] for argv in link.build_needed_argvs()] == [
        ["gcloud", "observability", "buckets", "datasets", "links", "create"]
    ]
    assert "--location=eu" in link.build_needed_argvs()[0]


@pytest.mark.parametrize(
    ("settings", "given", "location"),
    [
        ({"defaultStorageLocation": "europe-west1"}, None, "europe-west1"),
        (ApiError(403, "denied"), None, "us"),
        (
            {"defaultStorageLocation": "europe-west1"},
            "asia-east1",
            "asia-east1",
        ),
    ],
)
def test_no_bucket_is_planned_at_the_given_then_default_location(
    settings, given, location
):
    api = _FakeApi(_build_project_responses(None, **{_SETTINGS: settings}))

    link = trace_link_lib.resolve_trace_link(api, _PROJECT, location=given)

    assert link.build_needed_argvs() == [
        [
            "gcloud",
            "observability",
            "buckets",
            "create",
            "_Trace",
            f"--location={location}",
            f"--project={_PROJECT}",
        ],
        link.build_needed_argvs()[1],
    ]
    assert link.link_id == "trace_spans_linked"


def test_a_disabled_api_the_user_may_enable_is_planned_on():
    api = _FakeApi({_BUCKETS: _DISABLED, _PERMISSIONS: _MAY_ENABLE})

    link = trace_link_lib.resolve_trace_link(api, _PROJECT)

    assert link.api_disabled and link.can_enable_api
    assert link.build_needed_argvs()[0] == _ENABLE
    assert len(link.build_needed_argvs()) == 3


@pytest.mark.parametrize("permissions", [{}, ApiError(403, "denied")])
def test_a_disabled_api_the_user_may_not_enable_says_who_can(permissions):
    api = _FakeApi({_BUCKETS: _DISABLED, _PERMISSIONS: permissions})

    link = trace_link_lib.resolve_trace_link(api, _PROJECT)

    assert not link.can_enable_api
    (note,) = link.notes
    assert "you may not enable it" in note
    assert note.endswith(" ".join(_ENABLE))


def test_another_lookup_error_fails():
    api = _FakeApi({_BUCKETS: ApiError(500, "backend error")})

    with pytest.raises(trace_link_lib.TraceLinkError, match="backend error"):
        trace_link_lib.resolve_trace_link(api, _PROJECT)


# --- creation -------------------------------------------------------------------


def test_the_bucket_is_created_before_the_link_and_the_dataset_awaited():
    replies = [ApiError(404, "Not found")]

    def get_dataset():
        if replies:
            raise replies.pop()
        return {"location": "US"}

    api = _FakeApi(
        _build_project_responses(
            None, **{f"{_DATASETS}/trace_spans_linked": get_dataset}
        )
    )
    link = trace_link_lib.resolve_trace_link(api, _PROJECT)
    gcloud = _Gcloud()

    created, ran = _create(link, api, gcloud)

    assert ran == gcloud.commands == link.build_needed_argvs()
    assert [argv[3] for argv in ran] == ["create", "datasets"]
    assert created.bucket_exists and created.exists
    assert created.bigquery_location == "US"
    assert api.urls.count(f"{_DATASETS}/trace_spans_linked") == 2


def test_a_dataset_that_never_shows_fails():
    api = _FakeApi(_build_project_responses("us"))
    link = trace_link_lib.resolve_trace_link(api, _PROJECT)

    with pytest.raises(trace_link_lib.TraceLinkError, match="Gave up waiting"):
        _create(link, api, delays=(0, 1, 1))


def test_a_denied_dataset_read_fails_without_waiting():
    api = _FakeApi(
        _build_project_responses(
            "us",
            **{f"{_DATASETS}/trace_spans_linked": ApiError(403, "denied")},
        )
    )
    link = trace_link_lib.resolve_trace_link(api, _PROJECT)

    with pytest.raises(
        trace_link_lib.TraceLinkError, match=r"bigquery\.datasets\.get"
    ):
        _create(link, api)
    assert api.urls.count(f"{_DATASETS}/trace_spans_linked") == 1


def test_a_failed_command_stops_and_names_what_creating_needs():
    api = _FakeApi(_build_project_responses(None))
    link = trace_link_lib.resolve_trace_link(api, _PROJECT)
    gcloud = _Gcloud(status=1)

    with pytest.raises(
        trace_link_lib.TraceLinkError, match=r"roles/observability\.editor"
    ):
        _create(link, api, gcloud)
    assert len(gcloud.commands) == 1


def test_a_disabled_api_is_enabled_then_looked_up_for_real():
    api = _FakeApi({_BUCKETS: _DISABLED, _PERMISSIONS: _MAY_ENABLE})
    planned = trace_link_lib.resolve_trace_link(api, _PROJECT)
    gcloud = _Gcloud(
        on_run=_turn_api_on_after_enable(
            api,
            _build_project_responses(
                "us",
                "their_spans",
                **{f"{_DATASETS}/their_spans": {"location": "US"}},
            ),
        )
    )

    created, ran = _create(planned, api, gcloud)

    assert ran == [_ENABLE]
    # The real lookup's link, not the plan's default.
    assert created.link_id == "their_spans"


def test_a_given_link_id_is_created_or_refused_beside_another():
    api = _FakeApi(
        _build_project_responses(
            "us", **{f"{_DATASETS}/my_link": {"location": "US"}}
        )
    )
    created, ran = _create(
        trace_link_lib.resolve_trace_link(api, _PROJECT),
        api,
        link_id="my_link",
    )
    assert created.link_id == "my_link"
    assert ran[0][6].endswith("/links/my_link")

    linked = _FakeApi(
        _build_project_responses(
            "us",
            "their_spans",
            **{f"{_DATASETS}/their_spans": {"location": "US"}},
        )
    )
    with pytest.raises(trace_link_lib.TraceLinkError, match="their_spans"):
        _create(
            trace_link_lib.resolve_trace_link(linked, _PROJECT),
            linked,
            link_id="my_link",
        )


def test_an_api_the_user_may_not_enable_creates_nothing():
    api = _FakeApi({_BUCKETS: _DISABLED, _PERMISSIONS: {}})
    gcloud = _Gcloud()

    with pytest.raises(trace_link_lib.TraceLinkError, match="may not enable"):
        _create(trace_link_lib.resolve_trace_link(api, _PROJECT), api, gcloud)
    assert gcloud.commands == []


# --- link-traces ------------------------------------------------------------------


@pytest.fixture
def gcloud(monkeypatch) -> _Gcloud:
    """Replaces the commands link-traces runs, and their waiting."""
    fake = _Gcloud()
    monkeypatch.setattr(aqua_cli, "_run_command_to_stderr", fake)
    monkeypatch.setattr(aqua_cli.shutil, "which", lambda p: f"/bin/{p}")
    monkeypatch.setattr(trace_link_lib.time, "sleep", lambda seconds: None)
    return fake


def _invoke_link_traces(monkeypatch, responses: dict[str, Any], *args: str):
    """Runs `link-traces` for the test project against a fake API.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        responses: Replies by URL.
        *args: More arguments.

    Returns:
        The fake API and the Click invocation result.
    """
    api = _FakeApi(responses)
    monkeypatch.setattr(rest_client_lib, "RestClient", lambda *a, **k: api)
    result = CliRunner().invoke(
        aqua_cli.cli, ["link-traces", f"--project={_PROJECT}", *args]
    )
    return api, result


def test_link_traces_is_hidden_from_help_but_runs(monkeypatch, gcloud):
    """It is plumbing for the infra wrapper, not a command users type."""
    listed = CliRunner().invoke(aqua_cli.cli, ["--help"])
    _, result = _invoke_link_traces(monkeypatch, _build_project_responses(None))

    assert listed.exit_code == 0
    assert "link-traces" not in listed.output
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["link_id"] == "trace_spans_linked"


def test_link_traces_plans_by_default(monkeypatch, gcloud):
    _, result = _invoke_link_traces(monkeypatch, _build_project_responses(None))

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["applied"] is False
    assert report["link_id"] == "trace_spans_linked"
    assert report["telemetry_table"] == "_AllSpans"
    assert report["bigquery_location"] == "US"
    assert report["commands"][0].startswith(
        "gcloud observability buckets create _Trace"
    )
    assert "gcloud observability buckets create _Trace" in result.stderr
    assert gcloud.commands == []


def test_link_traces_apply_creates_and_reports_what_ran(monkeypatch, gcloud):
    _, result = _invoke_link_traces(
        monkeypatch,
        _build_project_responses(
            None,
            **{
                _SETTINGS: {"defaultStorageLocation": "eu"},
                f"{_DATASETS}/trace_spans_linked": {"location": "EU"},
            },
        ),
        "--apply",
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["applied"] is True
    assert report["bigquery_location"] == "EU"
    assert len(report["commands"]) == len(gcloud.commands) == 2


def test_link_traces_with_both_existing_runs_nothing(monkeypatch, gcloud):
    _, result = _invoke_link_traces(
        monkeypatch,
        _build_project_responses(
            "us",
            "trace_spans_linked",
            **{f"{_DATASETS}/trace_spans_linked": {"location": "US"}},
        ),
        "--apply",
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["commands"] == []
    assert gcloud.commands == []


def test_link_traces_enables_a_disabled_api_on_apply(monkeypatch, gcloud):
    replies = _build_project_responses(
        "us", **{f"{_DATASETS}/trace_spans_linked": {"location": "US"}}
    )
    api = _FakeApi({_BUCKETS: _DISABLED, _PERMISSIONS: _MAY_ENABLE})
    monkeypatch.setattr(rest_client_lib, "RestClient", lambda *a, **k: api)
    gcloud.on_run = _turn_api_on_after_enable(api, replies)

    result = CliRunner().invoke(
        aqua_cli.cli, ["link-traces", f"--project={_PROJECT}", "--apply"]
    )

    assert result.exit_code == 0, result.output
    assert gcloud.commands[0] == _ENABLE
    assert json.loads(result.stdout)["commands"][0] == " ".join(_ENABLE)


def test_link_traces_reports_an_api_the_user_may_not_enable(
    monkeypatch, gcloud
):
    responses = {_BUCKETS: _DISABLED, _PERMISSIONS: {}}

    _, plan = _invoke_link_traces(monkeypatch, responses)
    _, apply = _invoke_link_traces(monkeypatch, responses, "--apply")

    assert plan.exit_code == 0, plan.output
    assert "you may not enable it" in plan.stderr
    assert apply.exit_code == 1
    assert apply.stdout == ""
    assert gcloud.commands == []


def test_gcloud_output_stays_off_stdout(monkeypatch):
    """The wrapper parses stdout as JSON."""
    monkeypatch.setattr(
        aqua_cli.subprocess,
        "run",
        lambda argv, **kwargs: aqua_cli.subprocess.CompletedProcess(
            argv, 0, "Created bucket [_Trace].\n", None
        ),
    )

    with CliRunner().isolation() as (out, err, _):
        assert aqua_cli._run_command_to_stderr(["gcloud", "x"]) == 0

    assert out.getvalue() == b""
    assert b"Created bucket [_Trace]." in err.getvalue()
