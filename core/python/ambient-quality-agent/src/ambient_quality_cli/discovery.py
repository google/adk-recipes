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

"""How an observed agent is configured, read from its running engine.

`attach --observed-agent-resource` seeds this with the observed engine's
resource name, and :func:`discover_observed_agent` fills in the attach fields
nobody gave. It runs on the user's Application Default Credentials: listing logging
sinks needs `logging.sinks.list`, which AQuA's service account does not hold.
Whether AQuA itself can read what is found is checked by the deployment, as
its own service account.

The observed engine may be in another project than AQuA. Its telemetry is
read from the engine's own project, which is reported by id: a grant, a table
reference and the audit sink's project take the id, and a resource name may
carry the project number.

Cloud Trace stores an agent's OpenTelemetry spans in the project's
observability bucket `_Trace`, and `cloud_ops` ingestion reads them through a
BigQuery link on its `Spans` dataset (:mod:`ambient_quality_cli.trace_link`).
Discovery prefers those spans over a logging sink that exports the agent's
inference events, notes the sinks it passed over, and reports the link as
:attr:`Discovery.trace_link`; `attach` enables the Observability API
and creates the bucket and the link when they do not exist yet. Discovery
selects a sink's table when `--telemetry-dataset` matches the sink's dataset
or when the Observability API cannot be used (either because the user cannot
enable it or a lookup fails).

An agent that records with ADK's BigQuery Agent Analytics plugin is read by
`big_query` ingestion. Discovery does not choose `big_query` itself: the
engine's `BQ_ANALYTICS_*` variables say where the plugin would write, not that
the agent's code loads it, and an agents-cli project has the logging sink as
well. Given `--telemetry-source big_query`, discovery skips the sinks and spans
and reads the plugin's dataset from the engine's environment. The plugin
creates its table on the first event, so the table need not exist.

The calls go through :mod:`ambient_quality_cli.rest_client`::

    discover_observed_agent
      ├── GET  aiplatform  v1/<engine>                  displayName, env
      ├── GET  aiplatform  reasoningEngines/v1/<engine>/api/list-apps
      ├── GET  cloudresourcemanager  v3/projects/<p>    project id
      ├── given big_query, the plugin's table:
      │     GET  bigquery  datasets/<d>/tables/<t>      location, label
      │     GET  bigquery  datasets/<d>                 location, with no table
      ├── GET  logging     v2/projects/<p>/sinks
      ├── per sink into BigQuery:
      │     GET  bigquery  datasets/<d>                 location
      │     GET  bigquery  datasets/<d>/tables[/<t>]    tables, schemas
      │     POST bigquery  queries                      rows per engine, agent
      └── Cloud Trace, preferred to the sinks, through trace_link:
            GET  observability  locations/-/buckets     the _Trace bucket
            GET  observability  .../datasets/Spans/links
            GET  bigquery  datasets/<link>              location
            POST bigquery  queries                      the agent's traces
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Mapping
from typing import Any

from ambient_quality_cli import trace_link as trace_link_lib
from ambient_quality_cli.rest_client import (
    DEFAULT_TIMEOUT_SECONDS,
    ApiError,
    JsonApi,
)
from ambient_quality_cli.trace_link import (  # noqa: F401 - DEFAULT_TRACE_LINK is part of this module's interface
    DEFAULT_TRACE_LINK,
    SPANS_VIEW,
    TRACE_BUCKET,
    TRACE_DATASET,
    TraceLink,
    TraceLinkError,
)

# The event agents emit for each model call. A sink whose filter selects it
# exports the inference rows the `cloud_logging` fetcher reads.
_INFERENCE_EVENT = "gen_ai.client.inference.operation.details"

# Where the agent's OpenTelemetry instrumentation uploads message content too
# large to inline, as `gs://<bucket>/<prefix>`. The telemetry rows point into
# it, and AQuA needs read access to it before any row exists to point.
_UPLOAD_BASE_PATH = "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH"
_GCS_URI_RE = re.compile(r"^gs://(?P<bucket>[a-z0-9][a-z0-9._-]*)(?:/.*)?$")

# Without that upload, message content reaches the `cloud_logging` and
# `cloud_ops` sources only through the google-genai instrumentation, which
# writes it into its log events and spans. Under the experimental GenAI
# conventions it reads the capture mode and rejects `true`; under the default
# ones it records content for `true` alone. Content ADK writes into its own
# `call_llm` spans does not count, because ingestion drops it there.
_CAPTURE_MODE = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
_SEMCONV_OPT_IN = "OTEL_SEMCONV_STABILITY_OPT_IN"
_EXPERIMENTAL_GENAI = "gen_ai_latest_experimental"
_EXPERIMENTAL_CONTENT_MODES = frozenset(
    {"EVENT_ONLY", "SPAN_ONLY", "SPAN_AND_EVENT"}
)

_CLOUD_LOGGING = "cloud_logging"
_CLOUD_OPS = "cloud_ops"
_BIG_QUERY = "big_query"

# Where the BigQuery Agent Analytics plugin writes, as the agent code
# agents-cli renders for `--bq-analytics` configures it: the dataset from this
# variable, which agents-cli's Terraform sets on the engine, else the default
# that code falls back to; the table is the plugin's default. The plugin labels
# a table it creates with its schema version.
_ANALYTICS_DATASET_VAR = "BQ_ANALYTICS_DATASET_ID"
_DEFAULT_ANALYTICS_DATASET = "adk_agent_analytics"
_ANALYTICS_TABLE = "agent_events"
_ANALYTICS_SCHEMA_LABEL = "adk_schema_version"

# The span attribute `cloud_ops` ingestion selects an agent's traces by.
_SPAN_AGENT_ATTRIBUTE = "gen_ai.agent.name"

# Where an Agent Runtime engine's stdout lands when a sink exports it: the
# table is named after the log id
# `aiplatform.googleapis.com/reasoning_engine_stdout`. A sink creates it on the
# first row, so a new agent may not have it yet.
ENGINE_LOG_TABLE = "aiplatform_googleapis_com_reasoning_engine_stdout"

_AGENT_LABEL = "gen_ai_agent_name"
_ENGINE_LABEL = "reasoning_engine_id"

_ENGINE_RE = re.compile(
    r"^projects/(?P<project>[^/]+)/locations/(?P<location>[^/]+)"
    r"/reasoningEngines/(?P<engine_id>[^/]+)$"
)
_BIGQUERY_DESTINATION_RE = re.compile(
    r"^bigquery\.googleapis\.com/projects/(?P<project>[^/]+)"
    r"/datasets/(?P<dataset>[^/]+)$"
)

_BIGQUERY = "https://bigquery.googleapis.com/bigquery/v2"
_LOGGING = "https://logging.googleapis.com/v2"
_RESOURCE_MANAGER = "https://cloudresourcemanager.googleapis.com/v3"


class DiscoveryError(Exception):
    """Discovery cannot go on: the seed is malformed or out of reach."""


@dataclasses.dataclass(frozen=True)
class DiscoveredValue:
    """One attach field's value, and where it was found."""

    value: Any
    source: str


@dataclasses.dataclass(frozen=True)
class EngineResource:
    """A reasoning engine's resource name, taken apart."""

    project: str
    location: str
    engine_id: str

    @property
    def name(self) -> str:
        """The full resource name."""
        return (
            f"projects/{self.project}/locations/{self.location}"
            f"/reasoningEngines/{self.engine_id}"
        )


@dataclasses.dataclass(frozen=True)
class TelemetryCandidate:
    """A table a sink exports inference rows to, and what it holds."""

    sink: str
    project: str
    dataset: str
    table: str
    location: str
    exists: bool
    agent_rows: int
    engine_rows: int

    @property
    def table_ref(self) -> str:
        """`dataset.table`, as shown to the operator."""
        return f"{self.dataset}.{self.table}"


@dataclasses.dataclass
class Discovery:
    """What discovery found: values by field, the tables it read, notes."""

    values: dict[str, DiscoveredValue] = dataclasses.field(default_factory=dict)
    candidates: list[TelemetryCandidate] = dataclasses.field(
        default_factory=list
    )
    chosen: TelemetryCandidate | None = None
    trace_link: TraceLink | None = None
    """The Cloud Trace link the discovered `cloud_ops` telemetry is read
    through, when that is what discovery chose."""
    notes: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class _TableCounts:
    """Rows in one table, by engine id and agent name."""

    sink: str
    project: str
    dataset: str
    table: str
    location: str
    exists: bool
    rows: tuple[tuple[str | None, str | None, int], ...]


def parse_engine_resource(resource: str) -> EngineResource:
    """Parses `projects/P/locations/L/reasoningEngines/ID`.

    Args:
        resource: The resource name.

    Returns:
        Its parts.

    Raises:
        DiscoveryError: If ``resource`` is not an engine resource name.
    """
    match = _ENGINE_RE.match(resource.strip().rstrip("/"))
    if not match:
        raise DiscoveryError(
            f"Not an engine resource name: {resource!r}. Expected "
            "projects/P/locations/L/reasoningEngines/ID."
        )
    return EngineResource(**match.groupdict())


def discover_observed_agent(
    resource: str,
    *,
    api: JsonApi,
    given: Mapping[str, Any],
    window_days: int,
) -> Discovery:
    """Discovers the attach fields of the agent ``resource`` runs.

    1. GET the engine; its `displayName` is the deployment's name.
    2. Ask its ADK server's `list-apps` for the root agent's name.
    3. Resolve the engine's project to its id: the observed agent's project,
       unless ``given`` names it.
    4. Given a `big_query` source, find the analytics plugin's table in that
       project and stop.
    5. List the project's logging sinks that export inference events to a
       BigQuery dataset in the same project, and count rows in their tables by
       engine and agent.
    6. If `list-apps` did not return an agent name, use the name from this
       engine's rows.
    7. Read the agent's spans through the BigQuery link on the project's
       Cloud Trace bucket, plan any missing resources, and note the sinks
       passed over.
    8. If spans are not selected, choose the sink table holding the agent's
       rows.

    Fields in ``given`` are not discovered. A given `big_query` source skips
    sinks and spans, `cloud_ops` skips sinks, `cloud_logging` skips spans, and
    a given dataset or table filters the candidates. A given dataset matching a
    sink also skips spans.

    Args:
        resource: The observed engine's resource name.
        api: Google REST client.
        given: Fields the operator set, by field name.
        window_days: How far back to count rows.

    Returns:
        The values found for fields not in ``given``, with their sources. The
        engine id is the seed's, so it is not among them.

    Raises:
        DiscoveryError: If ``resource`` is malformed or cannot be read.
    """
    engine = parse_engine_resource(resource)
    discovery = Discovery()
    found = discovery.values

    endpoint = f"https://{engine.location}-aiplatform.googleapis.com"
    try:
        body = api.get_json(f"{endpoint}/v1/{engine.name}")
    except ApiError as exc:
        raise DiscoveryError(f"Cannot read {engine.name}: {exc}") from exc
    if display_name := body.get("displayName"):
        found["observed_deployment_name"] = DiscoveredValue(
            display_name, "displayName of the observed engine"
        )
    # Recorded empty when the engine names no bucket, so that re-attaching
    # forgets one an earlier attach found and the check stops offering it.
    env = _extract_engine_env(body)
    bucket = _extract_payload_bucket(env)
    found["telemetry_payload_bucket"] = DiscoveredValue(
        bucket,
        f"{_UPLOAD_BASE_PATH} of the observed engine"
        if bucket
        else f"no gs:// {_UPLOAD_BASE_PATH} in the observed engine",
    )

    agent_name = given.get("observed_agent_name") or ""
    if not agent_name:
        agent_name = _fetch_root_agent_name(api, endpoint, engine, discovery)
        if agent_name:
            found["observed_agent_name"] = DiscoveredValue(
                agent_name, "rootAgentName from list-apps"
            )
    # The analytics plugin behind `big_query` records content on its own.
    if given.get("telemetry_ingestion_source") != _BIG_QUERY:
        _check_content_capture(env, bucket, discovery)

    # A given project is the one sinks are matched against, and needs no
    # lookup of the engine's.
    project = given.get("observed_project_id") or _resolve_project_id(
        api, engine.project, discovery
    )
    found["observed_project_id"] = DiscoveredValue(
        project, "project of the observed engine"
    )

    source = given.get("telemetry_ingestion_source")
    if source == _BIG_QUERY:
        _resolve_analytics_table(api, project, env, given, discovery)
        return _drop_given(discovery, given)
    if source not in (None, _CLOUD_LOGGING, _CLOUD_OPS):
        return _drop_given(discovery, given)
    # List sinks even when spans win: their rows provide the agent name if
    # `list-apps` did not, and the plan reports which sinks were passed over.
    tables: list[_TableCounts] = []
    if source != _CLOUD_OPS:
        tables = _list_telemetry_tables(
            api, engine, project, given, window_days, discovery
        )
        if not agent_name:
            agent_name = _resolve_agent_from_rows(engine, tables, discovery)
    # If the given dataset matches a sink, use the sink and skip spans.
    names_sink = bool(given.get("telemetry_dataset")) and bool(tables)
    if source != _CLOUD_LOGGING and not names_sink:
        _resolve_trace_link(
            api,
            project,
            given,
            agent_name,
            window_days,
            discovery,
            explicit=source == _CLOUD_OPS,
        )
    if source == _CLOUD_OPS:
        return _drop_given(discovery, given)
    if discovery.trace_link is not None:
        _record_sinks_passed_over(tables, discovery)
    elif tables:
        discovery.candidates = [
            _build_candidate(counts, engine.engine_id, agent_name)
            for counts in tables
        ]
        _resolve_telemetry(discovery, agent_name, window_days)
    else:
        discovery.notes.append(
            f"No logging sink in {engine.project} exports {_INFERENCE_EVENT} "
            "to BigQuery, so the telemetry fields keep their values."
        )
    return _drop_given(discovery, given)


def _record_sinks_passed_over(
    tables: list[_TableCounts], discovery: Discovery
) -> None:
    """Records a note listing the logging sinks passed over in favor of spans.

    Args:
        tables: The sink tables found.
        discovery: Receives the note.
    """
    sinks = list(dict.fromkeys(t.sink for t in tables))
    if not sinks:
        return
    listed = ", ".join(sinks)
    subject = (
        f"Logging sink {listed} exports"
        if len(sinks) == 1
        else f"Logging sinks {listed} export"
    )
    discovery.notes.append(
        f"{subject} {_INFERENCE_EVENT} as well. The spans are preferred; "
        "--telemetry-source cloud_logging reads the sink's table instead."
    )


def _extract_engine_env(engine: Mapping[str, Any]) -> dict[str, str]:
    """Extracts the plain environment variables of an engine's deployment.

    Args:
        engine: The engine, as the aiplatform GET returns it.

    Returns:
        Values by name; a secret, which carries no value, is left out.
    """
    spec = engine.get("spec") or {}
    return {
        str(variable["name"]): str(variable.get("value") or "")
        for variable in (spec.get("deploymentSpec") or {}).get("env") or []
        if isinstance(variable, dict) and variable.get("name")
    }


def _extract_payload_bucket(env: Mapping[str, str]) -> str:
    """Extracts the bucket the agent uploads message content to.

    Args:
        env: The engine's environment variables.

    Returns:
        The bucket's name, or an empty string when the engine's environment
        names none, as for an agent that inlines content.
    """
    match = _GCS_URI_RE.match(env.get(_UPLOAD_BASE_PATH, ""))
    return match["bucket"] if match else ""


def _check_content_capture(
    env: Mapping[str, str], bucket: str, discovery: Discovery
) -> None:
    """Notes an engine whose telemetry carries no message content.

    Without content, `cloud_logging` and `cloud_ops` ingestion finds no
    conversation turns and drops every session, however its reads are granted.

    Args:
        env: The engine's environment variables.
        bucket: The bucket the agent uploads content to; empty if none.
        discovery: Receives the note.
    """
    mode = env.get(_CAPTURE_MODE, "").strip()
    experimental = _EXPERIMENTAL_GENAI in (
        token.strip() for token in env.get(_SEMCONV_OPT_IN, "").split(",")
    )
    if bucket or (
        mode.upper() in _EXPERIMENTAL_CONTENT_MODES
        if experimental
        else mode.lower() == "true"
    ):
        return
    reason = (
        f"{_CAPTURE_MODE}={mode}, which records content only under "
        f"{_SEMCONV_OPT_IN}={_EXPERIMENTAL_GENAI}"
        if mode.upper() in _EXPERIMENTAL_CONTENT_MODES
        else f"{_CAPTURE_MODE}={mode or '(unset)'}"
    )
    discovery.notes.append(
        f"The observed engine records no message content ({reason}, and no "
        f"gs:// {_UPLOAD_BASE_PATH}), so investigations drop every "
        "conversation. Redeploy the agent with `agents-cli deploy "
        f"--update-env-vars {_SEMCONV_OPT_IN}={_EXPERIMENTAL_GENAI},"
        f"{_CAPTURE_MODE}=EVENT_ONLY`, then send it new traffic."
    )


def _drop_given(discovery: Discovery, given: Mapping[str, Any]) -> Discovery:
    """Removes the fields the operator gave from the discovered values.

    Args:
        discovery: What was found.
        given: Fields the operator set.

    Returns:
        ``discovery``, changed in place.
    """
    for field in given:
        discovery.values.pop(field, None)
    return discovery


def _fetch_root_agent_name(
    api: JsonApi, endpoint: str, engine: EngineResource, discovery: Discovery
) -> str:
    """Asks the engine's ADK server for its root agent's name.

    Only a container deploy running ADK's FastAPI server serves `list-apps`,
    and it answers 400 when the root agent is not an `LlmAgent`.

    Args:
        api: Google REST client.
        endpoint: The engine's regional aiplatform endpoint.
        engine: The engine.
        discovery: Collects a note when the call fails.

    Returns:
        The root agent's name, or an empty string.
    """
    url = f"{endpoint}/reasoningEngines/v1/{engine.name}/api/list-apps"
    try:
        body = api.get_json(url, params={"detailed": "true"})
    except ApiError as exc:
        discovery.notes.append(
            f"list-apps did not name the agent ({exc}); trying its telemetry."
        )
        return ""
    names = [
        str(app["rootAgentName"])
        for app in body.get("apps") or []
        if isinstance(app, dict) and app.get("rootAgentName")
    ]
    if len(names) > 1:
        discovery.notes.append(
            f"list-apps names {len(names)} agents ({', '.join(names)}); "
            f"taking {names[0]}. AGENT_NAME picks another."
        )
    return names[0] if names else ""


def _resolve_project_id(
    api: JsonApi, project: str, discovery: Discovery
) -> str:
    """Resolves a project number to the project's id.

    Args:
        api: Google REST client.
        project: Project id or number, as the engine's resource name has it.
        discovery: Collects a note when a number cannot be resolved.

    Returns:
        The project id; ``project`` itself when it is one already or cannot be
        resolved.
    """
    if not project.isdigit():
        return project
    try:
        body = api.get_json(f"{_RESOURCE_MANAGER}/projects/{project}")
    except ApiError as exc:
        discovery.notes.append(
            f"Cannot resolve project {project} to its id ({exc}); using the "
            "number. --observed-project names the project by id."
        )
        return project
    return str(body.get("projectId") or project)


def _resolve_analytics_table(
    api: JsonApi,
    project_id: str,
    env: Mapping[str, str],
    given: Mapping[str, Any],
    discovery: Discovery,
) -> None:
    """Records the table the BigQuery Agent Analytics plugin writes to.

    The plugin creates its table on the agent's first event, so a new agent may
    not have one: the names are recorded either way, and only the location
    needs the table or its dataset to exist.

    1. Take the dataset from the engine's `BQ_ANALYTICS_DATASET_ID`, else the
       agents-cli default, and the plugin's default table.
    2. GET the table for its location, and note one the plugin did not create
       or a given one that does not exist.
    3. Without the table's location or a given one, GET the dataset for its
       location.

    Args:
        api: Google REST client.
        project_id: The engine's project, by id, which the plugin writes to.
        env: The engine's environment variables.
        given: Fields the operator set; a given dataset or table is the one
            looked up.
        discovery: Receives the values and notes.
    """
    found = discovery.values
    dataset = str(given.get("telemetry_dataset") or "")
    if not dataset:
        dataset = env.get(_ANALYTICS_DATASET_VAR, "").strip()
        dataset_source = f"{_ANALYTICS_DATASET_VAR} of the observed engine"
        if not dataset:
            dataset = _DEFAULT_ANALYTICS_DATASET
            dataset_source = (
                "the default of agents-cli's analytics code; the observed "
                f"engine sets no {_ANALYTICS_DATASET_VAR}"
            )
        found["telemetry_dataset"] = DiscoveredValue(dataset, dataset_source)
    given_table = str(given.get("telemetry_table") or "")
    table = given_table or _ANALYTICS_TABLE
    base = f"{_BIGQUERY}/projects/{project_id}/datasets/{dataset}"
    table_ref = f"{project_id}.{dataset}.{table}"

    try:
        body = api.get_json(f"{base}/tables/{table}")
    except ApiError as exc:
        table_source = "the plugin's default table"
        if exc.status != 404:
            discovery.notes.append(f"Cannot read table {table_ref} ({exc}).")
        elif given_table:
            # The plugin creates its default table on its own; a table named
            # otherwise exists only if the agent's code configures the plugin
            # with that name.
            discovery.notes.append(
                f"Table {table_ref} does not exist. The plugin creates it on "
                "the agent's first event only if the agent's code names it."
            )
        else:
            table_source = (
                "the plugin's default table; not created yet, the plugin "
                "creates it on the agent's first event"
            )
        if not given_table:
            found["telemetry_table"] = DiscoveredValue(table, table_source)
    else:
        if not given_table:
            found["telemetry_table"] = DiscoveredValue(
                table, "the plugin's default table, which exists"
            )
        if _ANALYTICS_SCHEMA_LABEL not in (body.get("labels") or {}):
            discovery.notes.append(
                f"{table_ref} has no {_ANALYTICS_SCHEMA_LABEL} label, so the "
                "BigQuery Agent Analytics plugin did not create it, and "
                "big_query ingestion expects the plugin's columns."
            )
        if location := str(body.get("location") or ""):
            found["telemetry_location"] = DiscoveredValue(
                location, f"location of table {dataset}.{table}"
            )
            return
    if given.get("telemetry_location"):
        return

    try:
        location = str(api.get_json(base).get("location") or "")
    except ApiError as exc:
        discovery.notes.append(
            f"Cannot read dataset {project_id}.{dataset} ({exc}), so its "
            "location is not known. agents-cli's analytics code creates the "
            "dataset when the agent starts; --telemetry-location names it."
        )
        return
    if location:
        found["telemetry_location"] = DiscoveredValue(
            location, f"location of dataset {dataset}"
        )


def _list_telemetry_tables(
    api: JsonApi,
    engine: EngineResource,
    project_id: str,
    given: Mapping[str, Any],
    window_days: int,
    discovery: Discovery,
) -> list[_TableCounts]:
    """Lists the tables sinks export inference rows to, with their counts.

    Args:
        api: Google REST client.
        engine: The observed engine; its project's sinks are listed.
        project_id: The engine's project, by id. Ingestion reads the
            telemetry there, so a sink exporting to another project is not
            considered.
        given: Fields the operator set; a dataset or table narrows the search.
        window_days: How far back to count rows.
        discovery: Collects notes.

    Returns:
        One entry per table, in sink order.
    """
    try:
        body = api.get_json(f"{_LOGGING}/projects/{engine.project}/sinks")
    except ApiError as exc:
        discovery.notes.append(
            f"Cannot list the logging sinks of {engine.project} ({exc})."
        )
        return []

    tables: list[_TableCounts] = []
    for sink in body.get("sinks") or []:
        match = _BIGQUERY_DESTINATION_RE.match(str(sink.get("destination", "")))
        if (
            not match
            or sink.get("disabled")
            or _INFERENCE_EVENT not in str(sink.get("filter", ""))
        ):
            continue
        name = str(sink.get("name", ""))
        project, dataset = match["project"], match["dataset"]
        if given.get("telemetry_dataset") not in (None, dataset):
            continue
        if project != project_id:
            discovery.notes.append(
                f"Sink {name} exports to {project}.{dataset}, outside the "
                f"observed project {project_id}; not considered."
            )
            continue
        tables.extend(
            _list_sink_tables(
                api, name, project, dataset, given, window_days, discovery
            )
        )
    return tables


def _list_sink_tables(
    api: JsonApi,
    sink: str,
    project: str,
    dataset: str,
    given: Mapping[str, Any],
    window_days: int,
    discovery: Discovery,
) -> list[_TableCounts]:
    """Lists one sink dataset's tables, with their row counts.

    Args:
        api: Google REST client.
        sink: The sink's name.
        project: The dataset's project.
        dataset: The dataset.
        given: Fields the operator set; a table narrows the search.
        window_days: How far back to count rows.
        discovery: Collects notes.

    Returns:
        One entry per table.
    """
    base = f"{_BIGQUERY}/projects/{project}/datasets/{dataset}"
    try:
        location = str(api.get_json(base).get("location", ""))
        listing = api.get_json(f"{base}/tables", params={"maxResults": 1000})
    except ApiError as exc:
        discovery.notes.append(
            f"Cannot read dataset {project}.{dataset} of sink {sink} ({exc})."
        )
        return []
    existing = [
        str(t["tableReference"]["tableId"])
        for t in listing.get("tables") or []
        if t.get("type") == "TABLE"
    ]
    names = list(existing)
    wanted = given.get("telemetry_table")
    if wanted:
        names = [n for n in names if n == wanted]
    elif ENGINE_LOG_TABLE not in names:
        # The sink has not written for an engine yet; the table it will
        # write to is still the one to attach.
        names.append(ENGINE_LOG_TABLE)

    tables = []
    for table in names:
        exists = table in existing
        rows: tuple[tuple[str | None, str | None, int], ...] = ()
        if exists:
            rows = _count_table_rows(
                api, project, dataset, table, location, window_days, discovery
            )
        tables.append(
            _TableCounts(sink, project, dataset, table, location, exists, rows)
        )
    return tables


def _count_table_rows(
    api: JsonApi,
    project: str,
    dataset: str,
    table: str,
    location: str,
    window_days: int,
    discovery: Discovery,
) -> tuple[tuple[str | None, str | None, int], ...]:
    """Counts a table's recent rows by engine id and agent name.

    A sink's table gains a `labels` column per label it has seen, so the
    schema says which of the two exist before the query names them.

    Args:
        api: Google REST client.
        project: The table's project; the query runs there.
        dataset: The table's dataset.
        table: The table.
        location: The dataset's location.
        window_days: How far back to count.
        discovery: Collects a note when the query fails.

    Returns:
        `(engine_id, agent_name, rows)` triples; empty when the table has no
        agent name label or the query fails.
    """
    url = f"{_BIGQUERY}/projects/{project}/datasets/{dataset}/tables/{table}"
    try:
        schema = api.get_json(url).get("schema") or {}
    except ApiError as exc:
        discovery.notes.append(f"Cannot read {dataset}.{table} ({exc}).")
        return ()
    fields = {f.get("name"): f for f in schema.get("fields") or []}
    labels = _list_subfields(fields.get("labels"))
    resource = {
        f.get("name"): f
        for f in (fields.get("resource") or {}).get("fields") or []
    }
    if _AGENT_LABEL not in labels or "timestamp" not in fields:
        return ()
    engine = (
        f"resource.labels.{_ENGINE_LABEL}"
        if _ENGINE_LABEL in _list_subfields(resource.get("labels"))
        else "CAST(NULL AS STRING)"
    )
    query = f"""
SELECT {engine} AS engine, labels.{_AGENT_LABEL} AS agent, COUNT(*) AS n
FROM `{project}.{dataset}.{table}`
WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
GROUP BY engine, agent
"""  # noqa: S608 - identifiers from the BigQuery API; values are parameterized
    rows = _run_query(
        api,
        project,
        location,
        query,
        {"days": ("INT64", str(window_days))},
        f"{dataset}.{table}",
        discovery,
    )
    return tuple(
        (cells[0]["v"], cells[1]["v"], int(cells[2]["v"] or 0))
        for cells in rows or []
    )


def _run_query(
    api: JsonApi,
    project: str,
    location: str,
    query: str,
    parameters: Mapping[str, tuple[str, str]],
    table_ref: str,
    discovery: Discovery,
) -> list[list[dict[str, Any]]] | None:
    """Runs a counting query and waits for its rows.

    Args:
        api: Google REST client.
        project: Project the query runs in.
        location: BigQuery location of the table it reads.
        query: GoogleSQL with named parameters.
        parameters: `(type, value)` per parameter name.
        table_ref: The table counted, for the notes.
        discovery: Collects a note when the query fails or runs too long.

    Returns:
        Each row's cells, as `jobs.query` returns them; None when the query
        failed or did not finish in time.
    """
    try:
        body = api.post_json(
            f"{_BIGQUERY}/projects/{project}/queries",
            {
                "query": query,
                "useLegacySql": False,
                "location": location,
                # Shorter than the client's HTTP timeout, so a slow query
                # answers `jobComplete: false` before the request times out.
                "timeoutMs": int((DEFAULT_TIMEOUT_SECONDS - 10) * 1000),
                "parameterMode": "NAMED",
                "queryParameters": [
                    {
                        "name": name,
                        "parameterType": {"type": kind},
                        "parameterValue": {"value": value},
                    }
                    for name, (kind, value) in parameters.items()
                ],
            },
        )
    except ApiError as exc:
        discovery.notes.append(f"Cannot count rows in {table_ref} ({exc}).")
        return None
    if not body.get("jobComplete", False):
        discovery.notes.append(
            f"Counting rows in {table_ref} did not finish in time."
        )
        return None
    return [row["f"] for row in body.get("rows") or []]


def _list_subfields(field: Mapping[str, Any] | None) -> set[str]:
    """Lists the names of a RECORD column's subfields.

    Args:
        field: A schema field, or None.

    Returns:
        The subfield names; empty for a missing or non-RECORD field.
    """
    return {str(f.get("name")) for f in (field or {}).get("fields") or []}


def _resolve_agent_from_rows(
    engine: EngineResource, tables: list[_TableCounts], discovery: Discovery
) -> str:
    """Takes the agent name this engine's telemetry rows carry.

    Args:
        engine: The observed engine.
        tables: Counted tables.
        discovery: Receives the value, or a note when several names compete.

    Returns:
        The agent name with the most rows for the engine, or an empty string.
    """
    by_agent: dict[str, tuple[int, str]] = {}
    for table in tables:
        for engine_id, agent, rows in table.rows:
            if engine_id == engine.engine_id and agent:
                total, where = by_agent.get(agent, (0, ""))
                by_agent[agent] = (total + rows, where or table.table)
    if not by_agent:
        discovery.notes.append(
            "No agent name found: list-apps did not answer and this engine has "
            "no telemetry rows. Name it with AGENT_NAME."
        )
        return ""
    ranked = sorted(by_agent.items(), key=lambda item: -item[1][0])
    agent, (_, where) = ranked[0]
    discovery.values["observed_agent_name"] = DiscoveredValue(
        agent, f"{_AGENT_LABEL} of this engine's rows in {where}"
    )
    if len(ranked) > 1:
        others = ", ".join(name for name, _ in ranked[1:])
        discovery.notes.append(
            f"This engine's rows also name {others}; AGENT_NAME picks another."
        )
    return agent


def _build_candidate(
    counts: _TableCounts, engine_id: str, agent_name: str
) -> TelemetryCandidate:
    """Sums a table's rows for the observed agent and its engine.

    Args:
        counts: The table's rows by engine and agent.
        engine_id: The observed engine's id.
        agent_name: The observed agent's name, or empty if unknown.

    Returns:
        The table as a candidate.
    """
    return TelemetryCandidate(
        sink=counts.sink,
        project=counts.project,
        dataset=counts.dataset,
        table=counts.table,
        location=counts.location,
        exists=counts.exists,
        agent_rows=sum(
            n
            for _, agent, n in counts.rows
            if agent_name and agent == agent_name
        ),
        engine_rows=sum(
            n for engine, _, n in counts.rows if engine == engine_id
        ),
    )


def _resolve_telemetry(
    discovery: Discovery, agent_name: str, window_days: int
) -> None:
    """Chooses the table to attach and records its four telemetry fields.

    The table with the agent's rows wins, and the engine's rows break a tie.
    With no rows anywhere, the engine's log table in the only sink dataset is
    taken, since a new agent may have had no traffic.

    Args:
        discovery: Holds the candidates; receives the values and notes.
        agent_name: The observed agent's name, or empty if unknown.
        window_days: The window the rows were counted over.
    """
    candidates = discovery.candidates
    if not candidates:
        return
    ranked = sorted(
        candidates, key=lambda c: (c.agent_rows, c.engine_rows), reverse=True
    )
    best = ranked[0]
    window = f"in the last {window_days} days"
    if best.agent_rows or best.engine_rows:
        if best.agent_rows:
            table_source = f"{best.agent_rows} rows for {agent_name} {window}"
        else:
            table_source = f"{best.engine_rows} rows for this engine {window}"
            discovery.notes.append(
                f"{best.table_ref} has rows for this engine, but none named "
                f"{agent_name or 'after the agent'}. The fetcher selects rows "
                f"by {_AGENT_LABEL}."
            )
    else:
        datasets = {(c.project, c.dataset) for c in candidates}
        fallback = [c for c in candidates if c.table == ENGINE_LOG_TABLE]
        if len(datasets) > 1 or not fallback:
            listed = ", ".join(c.table_ref for c in candidates)
            discovery.notes.append(
                f"No rows for the agent {window} in {listed}, and nothing to "
                "choose between them by. --telemetry-dataset picks one."
            )
            return
        best = fallback[0]
        table_source = (
            f"no rows {window}; the table an engine's logs land in"
            if best.exists
            else "not created yet; the sink creates it on the first row"
        )

    discovery.chosen = best
    sink_source = f"sink {best.sink}"
    discovery.values.update(
        {
            "telemetry_ingestion_source": DiscoveredValue(
                _CLOUD_LOGGING, sink_source
            ),
            "telemetry_dataset": DiscoveredValue(best.dataset, sink_source),
            "telemetry_table": DiscoveredValue(best.table, table_source),
            "telemetry_location": DiscoveredValue(
                best.location, f"location of dataset {best.dataset}"
            ),
        }
    )


def _resolve_trace_link(
    api: JsonApi,
    project_id: str,
    given: Mapping[str, Any],
    agent_name: str,
    window_days: int,
    discovery: Discovery,
    *,
    explicit: bool,
) -> bool:
    """Takes the agent's Cloud Trace spans as its telemetry.

    1. Look up the project's `_Trace` bucket and the BigQuery links on its
       `Spans` dataset. If the Observability API is disabled, plan to enable it,
       or skip spans if the user lacks permission.
    2. Select the link matching the given dataset, or the single existing link.
       If none exists, plan a new link named after the given dataset or
       `DEFAULT_TRACE_LINK`.
    3. If the link exists, count the agent's traces in it.
    4. Record the four telemetry fields and the link.

    Args:
        api: Google REST client.
        project_id: The observed engine's project, by id.
        given: Explicit flag values from the caller.
        agent_name: The observed agent's name, or empty if unknown.
        window_days: How far back to count traces.
        discovery: Receives the values, the link and notes.
        explicit: Whether the operator gave `cloud_ops` as the source. Only
            then does an unlinked given dataset name a link to create;
            otherwise it names a sink dataset.

    Returns:
        True if the spans were selected as telemetry; False otherwise (with a
        reason recorded in `discovery.notes`).
    """

    def record_no_spans(reason: str) -> bool:
        """Records why spans were not selected and returns False.

        Args:
            reason: The reason, with or without a trailing period.

        Returns:
            Always False.
        """
        reason = reason.rstrip(".")
        if explicit:
            discovery.notes.append(f"{reason}.")
        else:
            discovery.notes.append(f"{reason}; looking for a logging sink.")
        return False

    try:
        link = trace_link_lib.resolve_trace_link(api, project_id)
    except TraceLinkError as exc:
        return record_no_spans(str(exc))
    if link.api_disabled and not link.can_enable_api:
        return record_no_spans(" ".join(link.notes))
    discovery.notes.extend(link.notes)
    spans = f"{TRACE_BUCKET}/{TRACE_DATASET}"
    wanted = given.get("telemetry_dataset")
    if wanted and wanted != link.link_id:
        if link.exists:
            # An observability dataset takes one link, so no second can be made.
            return record_no_spans(
                f"{spans} is linked to BigQuery as {link.link_id}, not "
                f"{wanted}; --telemetry-dataset {link.link_id} reads it"
            )
        if not explicit:
            return record_no_spans(f"{spans} has no BigQuery link {wanted}")
        link = dataclasses.replace(link, link_id=wanted)

    discovery.trace_link = link
    via = f"Cloud Trace bucket {TRACE_BUCKET} in {link.location}"
    if link.exists:
        table_source = _count_agent_traces(
            api,
            link,
            given.get("telemetry_table") or SPANS_VIEW,
            link.bigquery_location,
            agent_name,
            window_days,
            discovery,
        )
        dataset_source = f"BigQuery link on {spans}"
        location_source = f"location of dataset {link.link_id}"
    else:
        dataset_source = "not linked yet; attach creates the link"
        table_source = "the span view of the linked dataset"
        location_source = f"location of {via}"
        discovery.notes.append(
            f"{spans} in {project_id} has no BigQuery link yet, so there are "
            "no traces to count."
        )
    discovery.values.update(
        {
            "telemetry_ingestion_source": DiscoveredValue(_CLOUD_OPS, via),
            "telemetry_dataset": DiscoveredValue(link.link_id, dataset_source),
            "telemetry_table": DiscoveredValue(SPANS_VIEW, table_source),
            "telemetry_location": DiscoveredValue(
                link.bigquery_location, location_source
            ),
        }
    )
    return True


def _count_agent_traces(
    api: JsonApi,
    link: TraceLink,
    table: str,
    location: str,
    agent_name: str,
    window_days: int,
    discovery: Discovery,
) -> str:
    """Counts the agent's traces in a trace link's span view.

    Args:
        api: Google REST client.
        link: An existing link.
        table: The span view in the linked dataset.
        location: The linked dataset's BigQuery location.
        agent_name: The observed agent's name, or empty if unknown.
        window_days: How far back to count.
        discovery: Collects notes.

    Returns:
        The source shown for the table: the count, or what the view is when
        there is nothing to count by or the count fails.
    """
    view_source = "the view Cloud Trace spans land in"
    if not agent_name:
        return view_source
    query = f"""
SELECT COUNT(DISTINCT trace_id) AS n
FROM `{link.project}.{link.link_id}.{table}`
WHERE start_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
  AND JSON_VALUE(attributes, '$."{_SPAN_AGENT_ATTRIBUTE}"') = @agent
"""  # noqa: S608 - identifiers from the Observability API; values are parameterized
    table_ref = f"{link.link_id}.{table}"
    rows = _run_query(
        api,
        link.project,
        location,
        query,
        {"days": ("INT64", str(window_days)), "agent": ("STRING", agent_name)},
        table_ref,
        discovery,
    )
    if rows is None:
        return view_source
    traces = int(rows[0][0]["v"] or 0) if rows else 0
    window = f"in the last {window_days} days"
    if traces:
        return f"{traces} traces for {agent_name} {window}"
    discovery.notes.append(
        f"{table_ref} has no traces for {agent_name} {window}. The fetcher "
        f"selects spans by {_SPAN_AGENT_ATTRIBUTE}. The spans stay the "
        "telemetry; --telemetry-source cloud_logging reads a logging sink "
        "instead."
    )
    return f"no traces for {agent_name} {window}; {view_source}"


def parse_hints(text: str) -> dict[str, DiscoveredValue]:
    """Parses the hints a wrapper passes as JSON.

    Args:
        text: `{"<field>": {"value": ..., "source": "..."}}`.

    Returns:
        The hints by field.

    Raises:
        ValueError: If ``text`` is not that shape.
    """
    raw = json.loads(text) if text else {}
    if not isinstance(raw, dict):
        raise ValueError("hints must be a JSON object")
    hints = {}
    for field, hint in raw.items():
        if not isinstance(hint, dict) or "value" not in hint:
            raise ValueError(f"hint {field!r} needs a value")
        hints[str(field)] = DiscoveredValue(
            hint["value"], str(hint.get("source", "a hint"))
        )
    return hints


@dataclasses.dataclass(frozen=True)
class ResolvedValue:
    """A field's value as attach will send it, and what else was said."""

    field: str
    value: Any
    source: str
    disagreeing: DiscoveredValue | None = None


def resolve_attach_fields(
    given: Mapping[str, Any],
    discovered: Mapping[str, DiscoveredValue],
    hints: Mapping[str, DiscoveredValue],
) -> list[ResolvedValue]:
    """Resolves the fields attach fills in: a flag, then discovery, then hints.

    Args:
        given: Fields the operator set.
        discovered: Values discovery found.
        hints: Values a wrapper read from the project's own files.

    Returns:
        The fields not given, with their values, in discovery's order and then
        the hints'.
    """
    resolved = []
    for field in dict.fromkeys([*discovered, *hints]):
        if field in given:
            continue
        hint = hints.get(field)
        if field in discovered:
            found = discovered[field]
            disagreeing = (
                hint if hint is not None and hint.value != found.value else None
            )
            resolved.append(
                ResolvedValue(field, found.value, found.source, disagreeing)
            )
        elif hint is not None and hint.value not in (None, ""):
            resolved.append(ResolvedValue(field, hint.value, hint.source))
    return resolved


def render_discovery_lines(
    seed: str | None,
    resolved: list[ResolvedValue],
    discovery: Discovery | None,
) -> list[str]:
    """Renders what discovery filled in, shown before the attach plan.

    Args:
        seed: The observed engine discovery started from, if any.
        resolved: The fields filled in.
        discovery: What discovery found, for its other tables and notes.

    Returns:
        Lines to print; empty when nothing was filled in or noted.
    """
    lines: list[str] = []
    if resolved:
        origin = f"from {seed}" if seed else "from this project"
        lines.append(f"Discovered {origin} (a flag you give wins):")
        width = max(len(r.field) for r in resolved)
        for r in resolved:
            lines.append(f"  {r.field:<{width}}  {r.value}  ({r.source})")
            if r.disagreeing is not None:
                lines.append(
                    f"  {'':<{width}}  {r.disagreeing.source} says "
                    f"{r.disagreeing.value}"
                )
    if discovery is not None:
        others = [c for c in discovery.candidates if c != discovery.chosen]
        if discovery.chosen is not None and others:
            lines.append("Other telemetry tables a sink exports to:")
            lines.extend(
                f"  {c.table_ref}  ({c.agent_rows} rows for the agent, "
                f"sink {c.sink})"
                for c in others
            )
        lines.extend(f"Discovery: {note}" for note in discovery.notes)
    return lines
