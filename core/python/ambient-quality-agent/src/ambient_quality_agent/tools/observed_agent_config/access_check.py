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

"""Checks that AQuA can read what an attachment points it at.

`aqua attach` runs this in its dry run, on the fields it is about to store. The
reads run in the engine with the engine's own credentials, so a check that
passes is a read an investigation can make: dataset ACLs, IAM conditions and
organization policy are all applied by the service that answers, and none of
them has to be modeled here.

Each read goes through the telemetry fetcher an investigation would build, so
the table, the filter and the columns are the ones ingestion uses:

1. `table`: read the table's metadata, which proves it exists.
2. `rows`: count the agent's sessions in the lookback window. When there are
   none, name the agents the window does hold, in case the name is wrong.
3. `payload`: read one ``gs://`` payload object the agent's telemetry points
   at, for the sources whose ingestion follows those references
   (`cloud_logging` and `cloud_ops`). With no reference to sample, as for an
   agent with no traffic yet, test object reads on the bucket the attachment
   names instead, so the grant is not left until the first rows arrive.
4. `log_view`: for `cloud_ops`, read an entry through the log view the fetcher
   reads message content from.

A check that cannot run because an earlier one failed is reported as skipped,
with the reason, rather than left out.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

import google.auth
import google.auth.transport.requests
from ambient_quality_agent.config import ObservedAgentConfig
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion.base import (
    AGENT_COUNT_LIMIT,
    BigQueryJobFetcher,
    GcsClientReader,
)
from ambient_quality_agent.tools.ingestion.bigquery_fetcher import (
    BigQueryFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_logging_fetcher import (
    CloudLoggingFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_ops_fetcher import (
    CloudOpsFetcher,
)
from ambient_quality_agent.tools.ingestion.log_fetcher import LogFetcher
from google.api_core import exceptions as api_exceptions
from google.cloud import storage

logger = logging.getLogger(__name__)

PASSED = "passed"
"""The read worked."""

EMPTY = "empty"
"""The table is readable but holds no sessions for the agent in the window. A
new agent may have had no traffic yet, so the attach goes ahead."""

MISSING = "missing"
"""The dataset or table does not exist. Nothing could be investigated, so the
attach does not go ahead."""

DENIED = "denied"
"""AQuA's service account may not read it, and needs a grant."""

ERROR = "error"
"""The read failed for a reason that is neither of the above."""

SKIPPED = "skipped"
"""The check did not run; its detail says why."""

_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

_OBJECT_READ = "storage.objects.get"
"""The permission ingestion needs on the payload bucket."""

_FOLLOWS_PAYLOAD_REFS = {
    fetcher.TELEMETRY_SOURCE: fetcher.FOLLOWS_PAYLOAD_REFS
    for fetcher in (BigQueryFetcher, CloudLoggingFetcher, CloudOpsFetcher)
}
"""Whether each source's ingestion follows payload references, for when the
check has no fetcher to ask."""


@dataclasses.dataclass(frozen=True)
class CheckResult:
    """The outcome of one read."""

    name: str
    """Which check: `table`, `rows`, `payload` or `log_view`."""

    status: str
    """One of the status constants in this module."""

    target: str
    """What was read: a table reference, a ``gs://`` URI or a project."""

    detail: str
    """What happened, for the operator."""


FetcherFactory = Callable[[ObservedAgentConfig, str], BigQueryJobFetcher]
StorageClientFactory = Callable[[str], storage.Client]
LogFetcherFactory = Callable[[str], LogFetcher]


def check_access(
    agent: ObservedAgentConfig,
    *,
    project_id: str,
    payload_bucket: str = "",
    fetcher_factory: FetcherFactory | None = None,
    storage_client_factory: StorageClientFactory | None = None,
    log_fetcher_factory: LogFetcherFactory | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Tries every read an investigation of `agent` makes, as this process.

    Args:
        agent: How the agent would be observed once the attachment is stored.
        project_id: AQuA's project, which runs the query jobs. The telemetry
            is in the observed agent's project, `agent.observed_project_id`,
            when that is set.
        payload_bucket: Bucket the agent uploads message content to, checked
            when the telemetry has no payload reference to sample; empty to
            skip that.
        fetcher_factory: Builds the telemetry fetcher from `agent` and
            `project_id`.
        storage_client_factory: Builds the GCS client for `project_id`.
        log_fetcher_factory: Builds the Cloud Logging reader for the project
            the telemetry is in.
        now: End of the lookback window; the current time when None.

    Returns:
        `service_account`, the identity the reads ran as (empty when it cannot
        be determined), and `checks`, one `CheckResult` as a dictionary per
        check, in order.
    """
    end = now or dt.datetime.now(dt.UTC)
    start = end - dt.timedelta(days=agent.data_lookback_window)

    checks: list[CheckResult] = []
    fetcher = _check_telemetry(
        agent,
        project_id=project_id,
        build_fetcher=fetcher_factory or _create_default_fetcher,
        start=start,
        end=end,
        checks=checks,
    )
    checks.append(
        _check_payload(
            fetcher,
            agent.observed_agent_name,
            source=agent.telemetry_ingestion_source,
            payload_bucket=payload_bucket,
            start=start,
            end=end,
            project_id=project_id,
            storage_client_factory=storage_client_factory
            or _create_default_storage_client,
        )
    )
    if agent.telemetry_ingestion_source == "cloud_ops":
        checks.append(
            _check_log_view(
                agent.observed_project_id or project_id,
                start,
                log_fetcher_factory or _create_default_log_fetcher,
            )
        )
    return {
        "service_account": resolve_service_account(),
        "checks": [dataclasses.asdict(check) for check in checks],
    }


def _check_telemetry(
    agent: ObservedAgentConfig,
    *,
    project_id: str,
    build_fetcher: FetcherFactory,
    start: dt.datetime,
    end: dt.datetime,
    checks: list[CheckResult],
) -> BigQueryJobFetcher | None:
    """Runs the `table` and `rows` checks, appending their results to `checks`.

    Args:
        agent: How the agent would be observed.
        project_id: AQuA's project, which owns the table unless
            `agent.observed_project_id` names another.
        build_fetcher: Builds the telemetry fetcher.
        start: Start of the lookback window.
        end: End of the lookback window.
        checks: Results so far; this function appends two.

    Returns:
        The fetcher when the agent has readable rows in the window, for the
        payload check to sample; None otherwise.
    """
    # The grant for a denied read is built from this reference, so it names
    # the project that owns the dataset.
    owner = agent.observed_project_id or project_id
    table_ref = f"{owner}.{agent.telemetry_dataset}.{agent.telemetry_table}"
    if not agent.telemetry_dataset or not agent.telemetry_table:
        checks.append(
            CheckResult(
                "table",
                MISSING,
                table_ref,
                "No telemetry dataset or table is set.",
            )
        )
        checks.append(
            CheckResult("rows", SKIPPED, table_ref, "Needs the table.")
        )
        return None

    try:
        fetcher = build_fetcher(agent, project_id)
        fetcher.load_table()
    except Exception as exc:
        checks.append(_classify_failure("table", table_ref, exc))
        checks.append(
            CheckResult("rows", SKIPPED, table_ref, "Needs the table.")
        )
        return None
    checks.append(CheckResult("table", PASSED, table_ref, "The table exists."))

    name = agent.observed_agent_name
    try:
        sessions = fetcher.count_scanned(
            name, MetricType.MULTI_TURN, start, end
        )
    except Exception as exc:
        checks.append(_classify_failure("rows", table_ref, exc))
        return None
    days = agent.data_lookback_window
    if sessions == 0:
        detail = _render_empty_window_detail(
            name, days, _count_agents(fetcher, start, end)
        )
        checks.append(CheckResult("rows", EMPTY, table_ref, detail))
        return None
    checks.append(
        CheckResult(
            "rows",
            PASSED,
            table_ref,
            f"{sessions} session(s) for {name} in the last {days} day(s).",
        )
    )
    return fetcher


def _count_agents(
    fetcher: BigQueryJobFetcher, start: dt.datetime, end: dt.datetime
) -> dict[str, int] | None:
    """Counts the sessions in the window per agent name, to explain an empty one.

    Args:
        fetcher: Telemetry fetcher whose table was just read.
        start: Start of the lookback window.
        end: End of the lookback window.

    Returns:
        Session counts keyed by agent name, or None when they could not be
        taken. The `rows` check already has its answer, so a failure here only
        loses detail.
    """
    try:
        return fetcher.count_by_agent(MetricType.MULTI_TURN, start, end)
    except Exception:
        logger.warning(
            "access check: counting sessions per agent failed", exc_info=True
        )
        return None


def _render_empty_window_detail(
    agent_name: str, days: int, present: Mapping[str, int] | None
) -> str:
    """Explains a window with no sessions for the agent, naming what the table holds.

    Args:
        agent_name: Agent the attach names.
        days: Length of the lookback window.
        present: Session counts per agent name the window does hold, from
            `BaseFetcher.count_by_agent`, or None when they could not be taken.

    Returns:
        The `rows` check's detail.
    """
    head = f"No sessions for {agent_name} in the last {days} day(s)."
    if present is None:
        return (
            f"{head} A new agent may have had no traffic yet; if it has, check the "
            "agent name."
        )
    if not present:
        return (
            f"{head} No agent recorded any there: either the agent served no "
            "traffic, or it does not export to this table."
        )
    names = ", ".join(f"'{name}' ({count})" for name, count in present.items())
    busiest = (
        f" (the {len(present)} busiest)"
        if len(present) >= AGENT_COUNT_LIMIT
        else ""
    )
    return (
        f"{head} Agents with sessions there{busiest}: {names}. If one of them is "
        "this agent, attach it under that name."
    )


def _check_payload(
    fetcher: BigQueryJobFetcher | None,
    agent_name: str,
    *,
    source: str,
    payload_bucket: str,
    start: dt.datetime,
    end: dt.datetime,
    project_id: str,
    storage_client_factory: StorageClientFactory,
) -> CheckResult:
    """Reads one payload object the agent's telemetry points at.

    Reads the object's metadata, which takes the same `storage.objects.get`
    permission as its content, without downloading a payload of any size. With
    no reference to read, tests that permission on `payload_bucket` instead.

    Args:
        fetcher: Telemetry fetcher, when the agent has rows in the window.
        agent_name: Agent the attach names.
        source: The agent's telemetry source.
        payload_bucket: Bucket the agent uploads to; empty if unknown.
        start: Start of the lookback window.
        end: End of the lookback window.
        project_id: AQuA's project, for the GCS client.
        storage_client_factory: Builds the GCS client.

    Returns:
        The `payload` check's result.
    """
    # The fetcher is built for `source`, so the source alone says whether its
    # ingestion follows references, rows or not.
    if not _FOLLOWS_PAYLOAD_REFS.get(source, False):
        return CheckResult(
            "payload",
            SKIPPED,
            "",
            "Ingestion reads this source's content from the table itself, so no "
            "payload bucket needs to be readable.",
        )
    sample_ref, reason = None, "Needs the agent's rows."
    if fetcher is not None:
        try:
            sample_ref = fetcher.get_payload_ref(agent_name, start, end)
            reason = "The agent's telemetry in the window carries no payload reference to read."
        except Exception as exc:
            # The rows were just read, so a failure here is not the table's. The
            # `log_view` check reports a denied log read on its own.
            logger.warning(
                "access check: sampling a payload reference failed",
                exc_info=True,
            )
            reason = f"Could not look up a payload reference: {_extract_message(exc)}"
    if not sample_ref:
        if not payload_bucket:
            return CheckResult("payload", SKIPPED, "", reason)
        return _check_payload_bucket(
            payload_bucket,
            reason,
            project_id=project_id,
            storage_client_factory=storage_client_factory,
        )
    parsed = urlparse(sample_ref)
    if parsed.scheme != "gs" or not parsed.netloc:
        return CheckResult(
            "payload",
            ERROR,
            sample_ref,
            "The payload reference is not a gs:// URI.",
        )
    try:
        client = storage_client_factory(project_id)
        client.bucket(parsed.netloc).blob(parsed.path.lstrip("/")).reload()
    except api_exceptions.NotFound:
        return CheckResult(
            "payload",
            ERROR,
            sample_ref,
            "The object the agent's telemetry points at does not exist.",
        )
    except Exception as exc:
        return _classify_failure("payload", sample_ref, exc)
    return CheckResult(
        "payload", PASSED, sample_ref, "The payload object is readable."
    )


def _check_payload_bucket(
    bucket: str,
    reason: str,
    *,
    project_id: str,
    storage_client_factory: StorageClientFactory,
) -> CheckResult:
    """Tests whether this process may read objects in the payload bucket.

    `testIamPermissions` needs no permission of its own, so it answers for a
    bucket this process cannot otherwise touch, and it reads no object. It
    answers whether the bucket's IAM policy allows the read, not whether a read
    succeeds: object ACLs, VPC Service Controls and requester-pays settings are
    not reflected.

    Args:
        bucket: The bucket's name.
        reason: Why no payload object was read instead.
        project_id: AQuA's project, for the GCS client.
        storage_client_factory: Builds the GCS client.

    Returns:
        `passed`, `denied` or `error` on ``gs://<bucket>``, which a denied
        result's grant names. A bucket that does not exist is an `error`
        rather than `missing`, so it does not refuse the attach.
    """
    target = f"gs://{bucket}"
    try:
        client = storage_client_factory(project_id)
        held = client.bucket(bucket).test_iam_permissions([_OBJECT_READ])
    except api_exceptions.NotFound:
        return CheckResult(
            "payload",
            ERROR,
            target,
            f"{reason} The bucket the agent uploads to does not exist.",
        )
    except Exception as exc:
        return _classify_failure("payload", target, exc)
    if _OBJECT_READ in held:
        return CheckResult(
            "payload",
            PASSED,
            target,
            f"{reason} Objects in the bucket the agent uploads to are readable.",
        )
    return CheckResult(
        "payload",
        DENIED,
        target,
        f"{reason} Objects in the bucket the agent uploads to are not readable.",
    )


def _check_log_view(
    project_id: str, start: dt.datetime, log_fetcher_factory: LogFetcherFactory
) -> CheckResult:
    """Reads an entry through the log view `cloud_ops` ingestion reads.

    Args:
        project_id: Project whose logs hold the agent's message content.
        start: Start of the lookback window.
        log_fetcher_factory: Builds the Cloud Logging reader.

    Returns:
        The `log_view` check's result.
    """
    target = f"projects/{project_id}"
    try:
        found = log_fetcher_factory(project_id).has_entries(start)
    except Exception as exc:
        return _classify_failure("log_view", target, exc)
    if not found:
        return CheckResult(
            "log_view",
            EMPTY,
            target,
            "The log view is readable, but holds no entries.",
        )
    return CheckResult("log_view", PASSED, target, "The log view is readable.")


def _classify_failure(name: str, target: str, exc: Exception) -> CheckResult:
    """Turns a failed read into a check result.

    Args:
        name: Which check failed.
        target: What it read.
        exc: What the read raised.

    Returns:
        `missing` for a resource that does not exist, `denied` for a refused
        read, and `error` for anything else.
    """
    message = getattr(exc, "message", "") or str(exc)
    if isinstance(exc, api_exceptions.NotFound):
        return CheckResult(name, MISSING, target, message)
    if isinstance(exc, api_exceptions.Forbidden):
        return CheckResult(name, DENIED, target, message)
    logger.warning("access check: %s on %s failed", name, target, exc_info=True)
    return CheckResult(name, ERROR, target, _extract_message(exc))


def _extract_message(exc: Exception) -> str:
    """Extracts a one-line description of a failure for the operator.

    Args:
        exc: What a read raised.

    Returns:
        The exception's type and message.
    """
    return f"{type(exc).__name__}: {getattr(exc, 'message', '') or exc}"


def resolve_service_account() -> str:
    """Resolves the service account this process runs as.

    On Agent Runtime the credentials come from the metadata server, which names
    the account only once they are refreshed.

    Returns:
        The account's email, or an empty string for credentials that are not a
        service account's (a user's, in a standalone run) or cannot be read.
    """
    try:
        credentials, _ = google.auth.default(scopes=_SCOPES)
        email = getattr(credentials, "service_account_email", "")
        if email == "default":
            credentials.refresh(google.auth.transport.requests.Request())
            email = getattr(credentials, "service_account_email", "")
    except Exception:
        logger.warning(
            "access check: resolving the service account failed", exc_info=True
        )
        return ""
    return "" if email in (None, "default") else str(email)


def _create_default_fetcher(
    agent: ObservedAgentConfig, project_id: str
) -> BigQueryJobFetcher:
    """Builds the fetcher an investigation of `agent` would use.

    Args:
        agent: How the agent would be observed.
        project_id: AQuA's project, which runs the query jobs.

    Returns:
        The fetcher for the agent's telemetry source, with no selector and no
        trajectory recorder: the check samples nothing.
    """
    dataset, table = agent.telemetry_dataset, agent.telemetry_table
    location = agent.telemetry_location
    source = agent.telemetry_ingestion_source
    owner = agent.observed_project_id
    if source == "big_query":
        return BigQueryFetcher(
            project_id=project_id,
            dataset=dataset,
            table=table,
            location=location,
            observed_project_id=owner,
        )
    reader = GcsClientReader(_create_default_storage_client(project_id))
    if source == "cloud_ops":
        return CloudOpsFetcher(
            project_id=project_id,
            dataset=dataset,
            table=table,
            location=location,
            observed_project_id=owner,
            gcs_reader=reader,
        )
    return CloudLoggingFetcher(
        project_id=project_id,
        dataset=dataset,
        table=table,
        location=location,
        observed_project_id=owner,
        gcs_reader=reader,
    )


def _create_default_storage_client(project_id: str) -> storage.Client:
    """Builds the GCS client the payload check reads with.

    Args:
        project_id: AQuA's project.

    Returns:
        A storage client on this process's credentials.
    """
    return storage.Client(project=project_id)


def _create_default_log_fetcher(project_id: str) -> LogFetcher:
    """Builds the Cloud Logging reader the log view check reads with.

    Args:
        project_id: Project whose logs `cloud_ops` ingestion reads.

    Returns:
        A log fetcher on this process's credentials.
    """
    return LogFetcher(project_id)
