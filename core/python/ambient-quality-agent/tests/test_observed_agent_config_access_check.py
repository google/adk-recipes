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

"""The reads `aqua attach` tries, as AQuA's service account, before storing.

The fetcher, the GCS client and the log reader are fakes that answer or raise
the way the Google Cloud clients do, so what these check is how each answer
becomes a result, and which checks a failure skips.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.config import ObservedAgentConfig
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.observed_agent_config import access_check
from google.api_core import exceptions as api_exceptions

PROJECT = "aqua-project"
AGENT = "it_support_agent"
TABLE = "gen_ai_client_inference_operation_details"
TABLE_REF = f"{PROJECT}.agent_telemetry.{TABLE}"
REF = "gs://payload-bucket/2026/turn1_inputs.jsonl"
NOW = dt.datetime(2026, 9, 30, 12, tzinfo=dt.UTC)
SA = "aqua-engine@aqua-project.iam.gserviceaccount.com"


class _FakeFetcher:
    """Answers the reads the check makes of a telemetry fetcher."""

    FOLLOWS_PAYLOAD_REFS = True

    def __init__(
        self,
        *,
        table: Exception | None = None,
        sessions: int | Exception = 3,
        by_agent: dict[str, int] | Exception | None = None,
        ref: str | Exception | None = REF,
    ) -> None:
        """Scripts each read's answer; an exception is raised instead.

        Args:
            table: What `load_table` raises, or None to succeed.
            sessions: What `count_scanned` returns.
            by_agent: What `count_by_agent` returns; empty if None.
            ref: What `get_payload_ref` returns.
        """
        self._table = table
        self._sessions = sessions
        self._by_agent = {} if by_agent is None else by_agent
        self._ref = ref
        self.counted: list[tuple[Any, ...]] = []

    def load_table(self) -> object:
        if self._table:
            raise self._table
        return object()

    def count_scanned(self, *args: Any) -> int:
        self.counted.append(args)
        if isinstance(self._sessions, Exception):
            raise self._sessions
        return self._sessions

    def count_by_agent(self, *args: Any) -> dict[str, int]:
        del args
        if isinstance(self._by_agent, Exception):
            raise self._by_agent
        return self._by_agent

    def get_payload_ref(self, *args: Any) -> str | None:
        del args
        if isinstance(self._ref, Exception):
            raise self._ref
        return self._ref


class _FakeBlob:
    def __init__(self, client: _FakeStorageClient, uri: str) -> None:
        self._client = client
        self._uri = uri

    def reload(self) -> None:
        self._client.read.append(self._uri)
        if self._client.error:
            raise self._client.error


class _FakeBucket:
    def __init__(self, client: _FakeStorageClient, name: str) -> None:
        self._client = client
        self._name = name

    def blob(self, path: str) -> _FakeBlob:
        return _FakeBlob(self._client, f"gs://{self._name}/{path}")

    def test_iam_permissions(self, permissions: list[str]) -> list[str]:
        """Records the test and answers it from the client's `held`.

        Args:
            permissions: Permissions to test.

        Returns:
            Those of `permissions` the client holds.

        Raises:
            Exception: The client's `error`, when set.
        """
        self._client.tested.append((self._name, list(permissions)))
        if self._client.error:
            raise self._client.error
        return [p for p in permissions if p in self._client.held]


class _FakeStorageClient:
    """Records the objects read and the bucket permissions tested, holds the
    bucket permissions in `held`, and raises `error` when set."""

    def __init__(
        self, error: Exception | None = None, held: tuple[str, ...] = ()
    ) -> None:
        self.error = error
        self.held = held
        self.read: list[str] = []
        self.tested: list[tuple[str, list[str]]] = []

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self, name)


class _FakeLogFetcher:
    def __init__(self, answer: bool | Exception = True) -> None:
        self._answer = answer

    def has_entries(self, start: dt.datetime) -> bool:
        del start
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


@pytest.fixture(autouse=True)
def service_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access_check, "resolve_service_account", lambda: SA)


def _build_agent(**overrides: Any) -> ObservedAgentConfig:
    """Builds an agent observed through a Cloud Logging sink table.

    Args:
        **overrides: Fields to change.

    Returns:
        The agent's configuration.
    """
    fields: dict[str, Any] = {
        "observed_agent_name": AGENT,
        "telemetry_ingestion_source": "cloud_logging",
        "telemetry_dataset": "agent_telemetry",
        "telemetry_table": TABLE,
        "telemetry_location": "us-central1",
        "data_lookback_window": 7,
    }
    return ObservedAgentConfig(**{**fields, **overrides})


def _check(
    agent: ObservedAgentConfig | None = None,
    fetcher: _FakeFetcher | None = None,
    storage: _FakeStorageClient | None = None,
    log_fetcher: _FakeLogFetcher | None = None,
    payload_bucket: str = "",
) -> dict[str, Any]:
    """Runs the check against fakes.

    Args:
        agent: Agent to check; `_build_agent()` if None.
        fetcher: Telemetry fetcher; one that finds everything if None.
        storage: GCS client; one that reads everything if None.
        log_fetcher: Log reader; one that reads entries if None.
        payload_bucket: Bucket the attachment names; none if empty.

    Returns:
        The check's response.
    """
    fetcher = fetcher or _FakeFetcher()
    storage = storage or _FakeStorageClient()
    log_fetcher = log_fetcher or _FakeLogFetcher()
    return access_check.check_access(
        agent or _build_agent(),
        project_id=PROJECT,
        payload_bucket=payload_bucket,
        fetcher_factory=lambda _agent, _project: fetcher,
        storage_client_factory=lambda _project: storage,
        log_fetcher_factory=lambda _project: log_fetcher,
        now=NOW,
    )


def _extract_statuses(result: dict[str, Any]) -> dict[str, str]:
    """Extracts each check's status.

    Args:
        result: The check's response.

    Returns:
        Status keyed by check name, in order.
    """
    return {c["name"]: c["status"] for c in result["checks"]}


def test_every_read_passing() -> None:
    storage = _FakeStorageClient()

    result = _check(storage=storage)

    assert result["service_account"] == SA
    assert _extract_statuses(result) == {
        "table": "passed",
        "rows": "passed",
        "payload": "passed",
    }
    assert result["checks"][0]["target"] == TABLE_REF
    assert (
        "3 session(s) for it_support_agent in the last 7 day(s)"
        in (result["checks"][1]["detail"])
    )
    assert storage.read == [REF]


def test_rows_are_counted_the_way_an_investigation_counts_them() -> None:
    """Sessions of this agent over the lookback window ending now, through the
    fetcher's own count."""
    fetcher = _FakeFetcher()

    _check(agent=_build_agent(data_lookback_window=14), fetcher=fetcher)

    assert fetcher.counted == [
        (AGENT, MetricType.MULTI_TURN, NOW - dt.timedelta(days=14), NOW)
    ]


def test_a_missing_table_skips_the_reads_that_need_it() -> None:
    result = _check(
        fetcher=_FakeFetcher(
            table=api_exceptions.NotFound("Not found: Dataset")
        )
    )

    assert _extract_statuses(result) == {
        "table": "missing",
        "rows": "skipped",
        "payload": "skipped",
    }
    assert result["checks"][0]["detail"] == "Not found: Dataset"


def test_no_dataset_set_is_missing_telemetry() -> None:
    result = _check(agent=_build_agent(telemetry_dataset=""))

    assert _extract_statuses(result)["table"] == "missing"


def test_a_refused_table_read_is_denied() -> None:
    result = _check(
        fetcher=_FakeFetcher(table=api_exceptions.Forbidden("Access Denied"))
    )

    assert _extract_statuses(result)["table"] == "denied"


def test_a_refused_query_is_denied() -> None:
    """Metadata alone is readable with less than a query needs."""
    result = _check(
        fetcher=_FakeFetcher(
            sessions=api_exceptions.Forbidden("bigquery.tables.getData")
        )
    )

    assert _extract_statuses(result) == {
        "table": "passed",
        "rows": "denied",
        "payload": "skipped",
    }


def test_no_sessions_in_the_window_is_empty_not_a_failure() -> None:
    result = _check(fetcher=_FakeFetcher(sessions=0))

    assert _extract_statuses(result) == {
        "table": "passed",
        "rows": "empty",
        "payload": "skipped",
    }
    assert "No agent recorded any there" in result["checks"][1]["detail"]


def test_an_empty_window_names_the_agents_the_table_does_hold() -> None:
    """The likeliest cause of an empty window on a busy table is a wrong name."""
    result = _check(
        fetcher=_FakeFetcher(
            sessions=0, by_agent={"it_support_agent_v2": 12, "root_agent": 3}
        )
    )

    detail = result["checks"][1]["detail"]
    assert "'it_support_agent_v2' (12), 'root_agent' (3)" in detail
    assert "attach it under that name" in detail


def test_a_full_agent_list_is_called_the_busiest() -> None:
    by_agent = {
        f"agent_{i:02}": 1 for i in range(access_check.AGENT_COUNT_LIMIT)
    }

    result = _check(fetcher=_FakeFetcher(sessions=0, by_agent=by_agent))

    assert (
        f"(the {access_check.AGENT_COUNT_LIMIT} busiest)"
        in (result["checks"][1]["detail"])
    )


def test_an_empty_window_whose_agents_cannot_be_listed_is_still_empty() -> None:
    result = _check(
        fetcher=_FakeFetcher(
            sessions=0, by_agent=RuntimeError("quota exceeded")
        )
    )

    rows = result["checks"][1]
    assert rows["status"] == "empty"
    assert "check the agent name" in rows["detail"]


def test_rows_with_no_payload_reference_skip_the_payload_read() -> None:
    storage = _FakeStorageClient()

    result = _check(fetcher=_FakeFetcher(ref=None), storage=storage)

    assert _extract_statuses(result)["payload"] == "skipped"
    assert storage.read == []


def test_a_failed_reference_query_skips_the_payload_read() -> None:
    result = _check(fetcher=_FakeFetcher(ref=RuntimeError("boom")))

    payload = result["checks"][2]
    assert payload["status"] == "skipped"
    assert (
        payload["detail"]
        == "Could not look up a payload reference: RuntimeError: boom"
    )


def test_a_source_read_from_the_table_alone_needs_no_payload_read() -> None:
    """BigQuery Agent Analytics rows carry their content, and ingestion reads
    no object for them."""
    fetcher = _FakeFetcher()
    fetcher.FOLLOWS_PAYLOAD_REFS = False
    storage = _FakeStorageClient()

    result = _check(
        agent=_build_agent(telemetry_ingestion_source="big_query"),
        fetcher=fetcher,
        storage=storage,
    )

    payload = result["checks"][2]
    assert payload["status"] == "skipped"
    assert "from the table itself" in payload["detail"]
    assert storage.read == []


def test_a_refused_payload_read_is_denied() -> None:
    result = _check(
        storage=_FakeStorageClient(api_exceptions.Forbidden("objects.get"))
    )

    payload = result["checks"][2]
    assert payload["status"] == "denied"
    assert payload["target"] == REF


def test_a_payload_object_that_is_gone_is_an_error_not_missing_telemetry() -> (
    None
):
    """Only a missing table stops an attach; a vanished object says nothing
    about whether the grant works."""
    result = _check(
        storage=_FakeStorageClient(api_exceptions.NotFound("No such object"))
    )

    assert _extract_statuses(result)["payload"] == "error"


def test_any_other_failure_is_an_error() -> None:
    result = _check(
        fetcher=_FakeFetcher(table=RuntimeError("connection reset"))
    )

    table = result["checks"][0]
    assert table["status"] == "error"
    assert table["detail"] == "RuntimeError: connection reset"


def test_a_fetcher_that_cannot_be_built_is_an_error_not_a_crash() -> None:
    """Building the fetcher builds its clients, which fails without credentials;
    the plan reports that rather than the dry run failing outright."""

    def refuse(_agent: ObservedAgentConfig, _project: str) -> Any:
        raise RuntimeError("no default credentials")

    result = access_check.check_access(
        _build_agent(),
        project_id=PROJECT,
        fetcher_factory=refuse,
        storage_client_factory=lambda _project: _FakeStorageClient(),
        now=NOW,
    )

    assert _extract_statuses(result) == {
        "table": "error",
        "rows": "skipped",
        "payload": "skipped",
    }
    assert (
        result["checks"][0]["detail"] == "RuntimeError: no default credentials"
    )


def test_cloud_ops_also_reads_through_the_log_view() -> None:
    result = _check(
        agent=_build_agent(telemetry_ingestion_source="cloud_ops"),
        log_fetcher=_FakeLogFetcher(
            api_exceptions.PermissionDenied("logging.views.access")
        ),
    )

    log_view = result["checks"][-1]
    assert log_view["name"] == "log_view"
    assert log_view["status"] == "denied"
    assert log_view["target"] == f"projects/{PROJECT}"


def test_other_sources_do_not_read_the_log_view() -> None:
    result = _check(agent=_build_agent(telemetry_ingestion_source="big_query"))

    assert "log_view" not in _extract_statuses(result)


# --- resolve_service_account ----------------------------------------------------- #


class _FakeCredentials:
    """Names its account only after a refresh, like metadata-server credentials."""

    def __init__(self, email_after_refresh: str) -> None:
        self.service_account_email = "default"
        self._email = email_after_refresh

    def refresh(self, request: Any) -> None:
        del request
        self.service_account_email = self._email


def test_the_service_account_is_named_after_a_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()
    monkeypatch.setattr(
        access_check.google.auth,
        "default",
        lambda scopes: (_FakeCredentials(SA), PROJECT),
    )

    assert access_check.resolve_service_account() == SA


def test_user_credentials_name_no_service_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A standalone run reads as whoever is signed in; there is no account to
    grant anything to."""
    monkeypatch.undo()
    monkeypatch.setattr(
        access_check.google.auth, "default", lambda scopes: (object(), PROJECT)
    )

    assert access_check.resolve_service_account() == ""


def test_telemetry_in_another_project_is_read_there_by_jobs_run_here() -> None:
    """The targets name the agent's project, which is where a denied read's
    grant has to go; the query jobs still run in AQuA's."""
    built: list[str] = []
    logs_read: list[str] = []

    def build_fetcher(
        _agent: ObservedAgentConfig, project: str
    ) -> _FakeFetcher:
        built.append(project)
        return _FakeFetcher()

    def build_log_fetcher(project: str) -> _FakeLogFetcher:
        logs_read.append(project)
        return _FakeLogFetcher()

    result = access_check.check_access(
        _build_agent(
            observed_project_id="agent-project",
            telemetry_ingestion_source="cloud_ops",
        ),
        project_id=PROJECT,
        fetcher_factory=build_fetcher,
        storage_client_factory=lambda _project: _FakeStorageClient(),
        log_fetcher_factory=build_log_fetcher,
        now=NOW,
    )

    targets = {c["name"]: c["target"] for c in result["checks"]}
    assert targets["table"] == f"agent-project.agent_telemetry.{TABLE}"
    assert targets["log_view"] == "projects/agent-project"
    assert built == [PROJECT]
    assert logs_read == ["agent-project"]


@pytest.mark.parametrize("source", ["big_query", "cloud_ops", "cloud_logging"])
def test_the_default_fetcher_reads_the_agents_project(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    monkeypatch.setattr(
        "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
        mock.MagicMock(),
    )
    monkeypatch.setattr(access_check.storage, "Client", mock.MagicMock())
    monkeypatch.setattr(
        "ambient_quality_agent.tools.ingestion.cloud_ops_fetcher.LogFetcher",
        mock.MagicMock(),
    )

    fetcher = access_check._create_default_fetcher(
        _build_agent(
            observed_project_id="agent-project",
            telemetry_ingestion_source=source,
        ),
        PROJECT,
    )

    assert fetcher.table_ref == f"agent-project.agent_telemetry.{TABLE}"


# --- the payload bucket, before there is a reference to read ---------------- #


@pytest.mark.parametrize(
    ("fetcher", "reason"),
    [
        (_FakeFetcher(sessions=0), "Needs the agent's rows."),
        (_FakeFetcher(ref=None), "carries no payload reference"),
        (_FakeFetcher(ref=RuntimeError("boom")), "RuntimeError: boom"),
    ],
)
def test_no_reference_to_read_tests_the_named_bucket_instead(
    fetcher: _FakeFetcher, reason: str
) -> None:
    """A new agent has no rows, and its first attach must still be able to
    grant the bucket its content will be uploaded to."""
    storage = _FakeStorageClient()

    result = _check(fetcher=fetcher, storage=storage, payload_bucket="logs")

    payload = result["checks"][2]
    assert payload["status"] == "denied"
    assert payload["target"] == "gs://logs"
    assert reason in payload["detail"]
    assert storage.tested == [("logs", ["storage.objects.get"])]
    assert storage.read == []


def test_a_readable_bucket_passes() -> None:
    storage = _FakeStorageClient(held=("storage.objects.get",))

    result = _check(
        fetcher=_FakeFetcher(sessions=0), storage=storage, payload_bucket="logs"
    )

    assert _extract_statuses(result)["payload"] == "passed"


def test_a_bucket_that_does_not_exist_is_an_error_not_missing_telemetry() -> (
    None
):
    """Missing telemetry refuses the attach; a missing bucket must not."""
    result = _check(
        fetcher=_FakeFetcher(sessions=0),
        storage=_FakeStorageClient(error=api_exceptions.NotFound("no bucket")),
        payload_bucket="logs",
    )

    assert _extract_statuses(result)["payload"] == "error"


def test_a_sampled_reference_is_read_rather_than_the_bucket_tested() -> None:
    storage = _FakeStorageClient()

    result = _check(storage=storage, payload_bucket="logs")

    assert _extract_statuses(result)["payload"] == "passed"
    assert storage.read == [REF]
    assert storage.tested == []


def test_a_source_read_from_the_table_tests_no_bucket_without_rows() -> None:
    storage = _FakeStorageClient()

    result = _check(
        agent=_build_agent(telemetry_ingestion_source="big_query"),
        fetcher=_FakeFetcher(sessions=0),
        storage=storage,
        payload_bucket="logs",
    )

    assert _extract_statuses(result)["payload"] == "skipped"
    assert storage.tested == []
